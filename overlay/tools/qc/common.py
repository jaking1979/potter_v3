"""Shared helpers for quality control. Standard library only; every path and every command is passed in."""
from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CATEGORIES = ("did", "will_do", "reminder_set", "scheduled", "filed", "saved", "built", "fixed", "verified",
              "sent", "other")


def audit_date(now: datetime, tz: str) -> str:
    """The day a run audits: yesterday in the owner's time zone (the jobs run just after midnight).

    The recorder and the verifier both call this, so they always agree on the file name. In the old system each
    computed the day its own way and the nightly pass could look at a different file from the one just written.
    """
    return (now.astimezone(ZoneInfo(tz)) - timedelta(days=1)).date().isoformat()


def log(state: Path, msg: str, now: datetime | None = None) -> None:
    try:
        p = Path(state) / "logs" / "qc.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{(now or datetime.now(timezone.utc)).isoformat()} {msg}\n")
    except OSError:
        pass                                    # a full disk must not turn a check into a crash


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        with Path(path).open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    except OSError:
        pass
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def find_session_files(roots: list[Path], since_ts: float) -> list[Path]:
    """Session transcripts (.jsonl, not trajectory files) changed since `since_ts` (epoch seconds)."""
    out = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for p in root.iterdir():
            if p.suffix != ".jsonl" or ".trajectory." in p.name:
                continue
            try:
                if p.stat().st_mtime >= since_ts:
                    out.append(p)
            except OSError:
                pass
    return sorted(out)


def to_ms(ts) -> float | None:
    """A transcript timestamp as epoch milliseconds. Accepts ms, seconds or ISO text. None = unreadable."""
    if isinstance(ts, bool) or ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts) if ts > 1e11 else float(ts) * 1000
    try:
        return to_ms(float(ts))
    except (TypeError, ValueError):
        pass
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp() * 1000


def extract_messages(path: Path, since_ms: float = 0, until_ms: float = float("inf")) -> list[tuple]:
    """A session file as [(timestamp, role, text)], text blocks only, inside the time window.

    A message whose timestamp cannot be read is kept: the file itself changed inside the window, and dropping
    such messages would make the recorder quietly record nothing.
    """
    msgs = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("type") != "message":
                    continue
                m = d.get("message") if isinstance(d.get("message"), dict) else {}
                ts = m.get("timestamp") if m.get("timestamp") is not None else d.get("timestamp")
                ms = to_ms(ts)
                if ms is not None and (ms < since_ms or ms > until_ms):
                    continue
                content, txt = m.get("content"), ""
                if isinstance(content, str):
                    txt = content
                elif isinstance(content, list):
                    txt = "\n".join(str(b.get("text") or "") for b in content
                                    if isinstance(b, dict) and b.get("type") in ("text", "input_text"))
                txt = txt.strip()
                if txt:
                    msgs.append((ts, m.get("role"), txt))
    except OSError:
        pass
    return msgs


def chunk_transcript(msgs: list[tuple], max_chars: int = 40000) -> list[str]:
    """Join messages into chunks no longer than max_chars, split on message boundaries."""
    chunks, cur = [], ""
    for _ts, role, txt in msgs:
        block = f"[{role}]\n{txt[:max_chars - 50]}\n"
        if cur and len(cur) + len(block) > max_chars:
            chunks.append(cur)
            cur = block
        else:
            cur += block
    if cur:
        chunks.append(cur)
    return chunks


def claim_id(source: str, text: str) -> str:
    return hashlib.sha1(f"{Path(source).name}|{text.strip().lower()}".encode()).hexdigest()[:12]


def model_output_text(stdout: str) -> str | None:
    """The reply text from a model command: OpenClaw's JSON ({"payloads":[{"text":..}]}) or plain text."""
    out = (stdout or "").strip()
    if not out:
        return None
    try:
        d = json.loads(out)
    except ValueError:
        return out
    if isinstance(d, dict) and isinstance(d.get("payloads"), list):
        for p in d["payloads"]:
            if isinstance(p, dict) and str(p.get("text") or "").strip():
                return p["text"].strip()
        return None
    return out


def make_model(cmd: str | None, timeout: int = 150, run=subprocess.run):
    """Turn a command line into ask(prompt) -> text | None. No command, no model: returns None.

    The prompt is appended as the last argument. The command names its own model and reads its own credentials
    from the environment or from OpenClaw; nothing about a model is stored here.
    """
    if not cmd or not cmd.strip():
        return None
    argv = shlex.split(cmd)

    def ask(prompt: str) -> str | None:
        try:
            r = run(argv + [prompt], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        return model_output_text(r.stdout) if r.returncode == 0 else None

    return ask
