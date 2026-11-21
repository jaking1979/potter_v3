"""Intent router: decide, without a model where possible, which known request this is.

Step 1 is a pattern table (free, instant, the same answer every time). Step 2, only when the table has no answer
and a decision function was injected, is one decision-model call whose answer is validated against the known
intents. Anything else is a fallback, with a reason, so the path log can show which requests to add to the table.

  route(text, house, decide=None) -> Route
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

# Intents the fast path can execute. Adding one here means adding a handler in fastpath.py and tests for both.
INTENTS = ("set_temperature", "turn_off", "hold_away", "check_temperature", "check_perimeter", "house_status",
           "heating_estimate", "close_up", "refuse_protected", "not_home", "bedtime", "coming_up", "arrived",
           "night_report")
MODEL_MIN_CONFIDENCE = 0.7

_QUESTION = re.compile(r"^(is|are|was|were|did|do|does|what|what's|which|how|how's|any|has|have|"
                       r"(can|could) you (tell|check|see))\b")
_POLITE = re.compile(r"^(please|can you|could you|would you|will you|pls)\b")
_PROTECTED = re.compile(r"\b(un)?lock(s|ed|ing)?\b|\b(dis)?arm(s|ed|ing)?\b|\balarm\b|"
                        r"\bwater (valve|shut ?off|main)\b|\b(main|shut ?off) valve\b|\bvalve\b|"
                        r"\b(open|close|shut)\b.*\b(door|window|garage)s?\b")
_CLOSE_UP = re.compile(r"\bclose (up|down) (the )?(house|cabin|place|home)\b|\bclosing up\b|"
                       r"\b(secure|button up) the (house|cabin|place|home)\b|\bdeparture (sequence|routine)\b|"
                       r"^(please |can you |could you )?lock (it |everything |the (house|cabin|place|home) )?up\b")
_BEDTIME = re.compile(r"\b(going to bed|go(ing)? to sleep|heading to bed|off to bed|bedtime|turning in|"
                      r"good ?night|sleep program|wind (the house )?down|put the house to bed)\b")
_COMING_UP = re.compile(r"\b(coming up|heading up|driving up|going up|on (our|my) way( up)?|we'?ll be (up|there)|"
                        r"(we|i)'?m coming|(we|i) arrive|arriving)\b")
_ARRIVED = re.compile(r"\b(we'?re here|we are here|i'?m here|i am here|(we|i) (just )?(got|arrived|made it) "
                      r"(here|in|up|to the house)|just arrived)\b")
_NIGHT_REPORT = re.compile(r"\bhow did (the|last) night go\b|\b(rate|grade|score) (the |last |how )?night\b|"
                           r"\bhow was (the|last) night\b|\bnight report\b")
_REASONING = re.compile(r"\b(why|investigate|diagnos\w*|explain|analy[sz]e\w*|compare|trend|history|"
                        r"should (i|we)|what if|recommend\w*|optimi[sz]e\w*|figure out|look into|plan)\b")
_LATER = re.compile(r"\bat (1?\d|2[0-4])(:\d{2})?\b(?! ?(degrees|deg|f)\b)|\b(by|before|after|until|till) \d|\b\d{1,2}(:\d{2})? ?(am|pm)\b|\b(tonight|tomorrow|"
                    r"later|morning|evening|weekend|next week|mon|tues|wednes|thurs|fri|satur|sun)(day)?\b|"
                    r"\bin \d+ ?(min|minute|hour|hr|day)s?\b|\bfor (\d+|an?|the) ?(min|minute|hour|hr|day|night|"
                    r"week|weekend)s?\b|\b(when|once|while|whenever|as soon as|every)\b|\barriv\w*\b")
_COMPOUND = re.compile(r"\b(and then|then|and also|, and)\b|;")
_AUTOMATION = re.compile(r"\b(automation|script|scene|schedule|routine|yaml|dashboard)s?\b")
_HOW_LONG = re.compile(r"\bhow (long|many hours|much time)\b.*\b(heat|warm|get|reach|bring|recover)\w*\b")
_SET_VERB = re.compile(r"\b(set|make|put|change|raise|lower|bump|drop|heat|warm|cool|turn (it |the \w+ )?(up|down))\b")
_OFF = re.compile(r"\b(turn|shut|switch|power)\b.*\boff\b|\boff\b.*\b(heat|heating|hvac|thermostat|ac|split)\b")
_CLIMATE_WORD = re.compile(r"\b(heat|heating|hvac|thermostat|ac|a/c|air|climate|split|mini-?split|furnace|temp\w*)\b")
_AWAY = re.compile(r"\b(away|vacancy|vacant) (mode|hold|temp\w*|setting)\b|\b(set|put|switch)\b.*\bto away\b|"
                   r"\bhold (temp\w*|setting)\b")
_TEMP_Q = re.compile(r"\b(temp|temperature|how (warm|cold|hot|cool))\b|\bwhat('s| is) it (set|at)\b|\bsetpoint\b|"
                     r"\b(is|are) the (heat|heating|ac|hvac|thermostat|furnace|split)\b")
_GENERIC_ALIAS = re.compile(r"^(the )?(house|home|main house|heat|heating|hvac|thermostat|ac|air|climate|furnace)$")
_PERIMETER = re.compile(r"\b(door|window|perimeter|entry|entries)s?\b")
_STATUS = re.compile(r"\b(house|home|cabin) (status|check|ok|okay|report|state)\b|\bstatus of the (house|home)\b|"
                     r"\b(is|how is|how's) (the |my )?(house|home|cabin|place)( doing| ok| okay| fine| alright)?\b|"
                     r"^(status|house status|home status)\W*$|\bcheck (on )?the (house|home|cabin)\b")
_ROOM_WORD = re.compile(r"\b(\w+ ?room|bedroom|basement|upstairs|downstairs|kitchen|office|garage|attic|den|"
                        r"bathroom|bath|hall\w*|porch|loft|studio|apartment)\b")
_NUMBER = re.compile(r"(?<![\d.:])(\d{2,3}(?:\.\d)?)(?![\d:])(?! ?(am|pm|min|minute|hour|hr|day|%|percent))")


@dataclass
class Route:
    intent: str                       # one of INTENTS, or "fallback"
    path: str                         # "fast" | "fallback"
    source: str                       # "table" | "model" | "none"
    reason: str = ""                  # why it fell back (the thing to count)
    slots: dict = field(default_factory=dict)   # zone, temperature


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower().replace("’", "'").replace("°", " ")).strip()


def _is_question(t: str) -> bool:
    return bool(_QUESTION.search(t)) or (t.endswith("?") and not _POLITE.search(t))


def _alias_hits(t: str, table: dict) -> tuple[dict, dict]:
    """Return ({id: longest specific alias length}, {id: longest generic alias length}) for names found in t."""
    strong, weak = {}, {}
    for key, entry in (table or {}).items():
        for alias in [key.replace("_", " "), *((entry or {}).get("aliases") or [])]:
            a = _norm(alias)
            if a and re.search(r"\b" + re.escape(a) + r"\b", t):
                bucket = weak if _GENERIC_ALIAS.match(a) else strong
                bucket[key] = max(bucket.get(key, 0), len(a))
    return strong, weak


def find_zone(text: str, house: dict) -> tuple[Optional[str], bool, bool]:
    """Return (zone id, named, several). A specific name ("family room") beats a generic one ("heat").

    named=False means nothing in the request named a zone and the default zone was assumed.
    several=True means two or more zones were named specifically; the fast path does not guess between them.
    """
    strong, weak = _alias_hits(_norm(text), house.get("zones"))
    if strong:
        return max(strong, key=strong.get), True, _distinct(text, house, strong)
    if weak:
        return max(weak, key=weak.get), True, False
    return house.get("default_zone"), False, False


def _distinct(text: str, house: dict, strong: dict) -> bool:
    """True when, after removing the best zone's own names from the text, another zone is still named."""
    t = _norm(text)
    best = max(strong, key=strong.get)
    entry = (house.get("zones") or {}).get(best) or {}
    for alias in sorted([best.replace("_", " "), *(entry.get("aliases") or [])], key=len, reverse=True):
        t = re.sub(r"\b" + re.escape(_norm(alias)) + r"\b", " ", t)
    rest, _ = _alias_hits(t, {z: v for z, v in (house.get("zones") or {}).items() if z != best})
    return bool(rest)


def find_sensor(text: str, house: dict) -> Optional[str]:
    """A named temperature sensor (a room with no climate zone of its own, such as a basement)."""
    strong, _ = _alias_hits(_norm(text), house.get("sensors"))
    return max(strong, key=strong.get) if strong else None


def find_temperature(text: str) -> Optional[float]:
    """The setpoint the request names, unclamped (clamping is the Home Assistant layer's job, and is reported)."""
    t = _norm(text)
    m = re.search(r"\bto (\d{2,3}(?:\.\d)?)(?![\d:])", t)
    hits = [float(m.group(1))] if m else [float(x.group(1)) for x in _NUMBER.finditer(t)]
    hits = [h for h in hits if 30 <= h <= 99]
    return hits[0] if len(hits) == 1 else None


def _fallback(reason: str, source: str = "none") -> Route:
    return Route("fallback", "fallback", source, reason)


def _table(t: str, house: dict) -> Route:
    question = _is_question(t)
    for word in house.get("unsupported_location_words") or []:
        if re.search(r"\b" + re.escape(_norm(word)) + r"\b", t):
            return _fallback("other_location")
    if _NIGHT_REPORT.search(t):
        return Route("night_report", "fast", "table")
    routine = "bedtime" if _BEDTIME.search(t) else "arrived" if _ARRIVED.search(t) else \
        "coming_up" if _COMING_UP.search(t) else None
    if routine and (house.get("routines") or {}):
        if question or re.search(r"\b(what (would|do|will) you do|what happens|if i (say|tell))\b", t):
            return _fallback("question_about_a_routine")   # a hypothetical is answered, never run
        return Route(routine, "fast", "table")
    if _AUTOMATION.search(t):
        return _fallback("automation_or_schedule")     # never built on the fast path; the owner must ask explicitly
    if _CLOSE_UP.search(t):
        return Route("close_up", "fast", "table")
    if _PROTECTED.search(t) and not question:
        return Route("refuse_protected", "fast", "table")
    if _REASONING.search(t):
        return _fallback("needs_reasoning")
    zone, named, several = find_zone(t, house)
    sensor = find_sensor(t, house)
    if not named and not sensor and _ROOM_WORD.search(t) and not _PERIMETER.search(t):
        return _fallback("unknown_zone")               # a room was named that is not in the house file
    slots = {"zone": zone, "zone_named": named}
    if _HOW_LONG.search(t):
        temp = find_temperature(t)
        if several or (sensor and not named):
            return _fallback("multi_zone" if several else "unknown_zone")
        return Route("heating_estimate", "fast", "table", slots={**slots, "temperature": temp}) if temp \
            else _fallback("no_temperature")
    if _LATER.search(t):
        return _fallback("scheduled_or_conditional")
    if _COMPOUND.search(t):
        return _fallback("compound")
    control = None
    if _AWAY.search(t) and not question:
        control = Route("hold_away", "fast", "table", slots=slots)
    elif _OFF.search(t) and not question and (_CLIMATE_WORD.search(t) or named):
        control = Route("turn_off", "fast", "table", slots=slots)
    elif _SET_VERB.search(t) and not question:
        temp = find_temperature(t)
        if temp is not None:
            control = Route("set_temperature", "fast", "table", slots={**slots, "temperature": temp})
        elif _CLIMATE_WORD.search(t) or named or sensor:
            return _fallback("no_temperature")
    if control:
        if several:
            return _fallback("multi_zone")
        if sensor and not named:
            return _fallback("unknown_zone")           # that room has a sensor but nothing Merrick can control
        if control.intent == "hold_away" and house.get("away_hold_f") is None and not house.get("away_hold"):
            return _fallback("not_configured")
        return control
    if _PERIMETER.search(t) or (question and _PROTECTED.search(t)):
        return Route("check_perimeter", "fast", "table")
    if _STATUS.search(t):
        return Route("house_status", "fast", "table")
    if _TEMP_Q.search(t):
        if several:
            return _fallback("multi_zone")
        if sensor and not named:
            return Route("check_temperature", "fast", "table", slots={"sensor": sensor})
        return Route("check_temperature", "fast", "table", slots=slots)
    return _fallback("unrecognised")


def _from_model(answer, t: str, house: dict) -> Route:
    """Accept a model answer only if it names a known intent with enough confidence and usable slots."""
    if not isinstance(answer, dict):
        return _fallback("model_no_answer", "model")
    intent = answer.get("intent")
    try:
        conf = float(answer.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    if intent not in INTENTS or conf < MODEL_MIN_CONFIDENCE:
        return _fallback("model_unsure", "model")
    if intent in ("bedtime", "coming_up", "arrived"):
        return _fallback("routine_needs_clear_words", "model")   # many devices at once: only the table's phrases run one
    zones = house.get("zones") or {}
    zone = answer.get("zone") if answer.get("zone") in zones else None
    slots = {"zone": zone or house.get("default_zone"), "zone_named": bool(zone)}
    if intent in ("set_temperature", "heating_estimate"):
        temp = find_temperature(t)                 # the number must be in the owner's words, never the model's
        if temp is None:
            return _fallback("no_temperature", "model")
        slots["temperature"] = temp
    return Route(intent, "fast", "model", slots=slots)


def route(text: str, house: dict, decide: Optional[Callable[[str], Optional[dict]]] = None) -> Route:
    t = _norm(text)
    if not t:
        return _fallback("empty")
    r = _table(t, house)
    # The model is asked only about requests the table simply did not recognise. A request the table understood
    # well enough to send to the fallback (reasoning, schedules, automations) is not second-guessed.
    if r.path == "fast" or r.reason != "unrecognised" or decide is None:
        return r
    try:
        answer = decide(text)
    except Exception:                               # a broken or slow model must never break routing
        return _fallback("model_error", "model")
    return _from_model(answer, t, house)
