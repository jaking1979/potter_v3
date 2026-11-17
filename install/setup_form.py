#!/usr/bin/env python3
"""One-time setup web page for the Potter installer (stdlib only).

Run inside the new container: it serves a single form protected by a random one-time token, validates the
answers, hands them to bootstrap.sh as POTTER_* environment variables, shows live progress, then exits.
Secrets are never echoed back or logged. The page stops accepting input after one submit or 30 minutes.
"""
from __future__ import annotations

import email.parser
import email.policy
import hmac
import html
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
LIFETIME_S = 30 * 60
RETRY_WINDOW_S = 4 * 60 * 60     # after a failed install the page stays open this long for a fix and a retry
DRAFT_PATH = Path(os.environ.get("POTTER_SETUP_DRAFT", "/root/.potter-setup-draft.json"))
MAX_BUNDLE = 150 * 1024 * 1024
BUNDLE_PATH = Path(os.environ.get("POTTER_V2_BUNDLE_PATH", "/root/v2-export.zip"))

# (key, label, help, kind, required, default)
FIELDS = [
    ("OWNER_NAME", "Your first name", "Potter will use this name.", "text", True, ""),
    ("TIMEZONE", "Your time zone", "For example America/New_York. The Morning Brief uses it.", "text", True, "America/New_York"),
    ("BRIEF_TIME", "Morning Brief time", "24-hour HH:MM, weekdays only.", "text", True, "08:30"),
    ("OWNER_TELEGRAM_ID", "Your Telegram user ID", "A number. Message @userinfobot on Telegram to see yours.", "text", True, ""),
    ("TELEGRAM_TOKEN", "Telegram bot token", "From @BotFather: send /newbot, copy the token it gives you.", "secret", True, ""),
    ("OPENROUTER_KEY", "Model API key (OpenRouter)", "Create a key named potter-v3 and set its credit limit before pasting it here.", "secret", True, ""),
    ("COMPOSIO_API_KEY", "Composio API key (optional)", "Gives Potter the apps you connected in Composio (Google, Todoist and others). From the Composio dashboard. Leave blank to add it later.", "secret", False, ""),
    ("COMPOSIO_MCP_URL", "Composio MCP address (optional)", "Leave blank to use Composio's standard address. Only fill this in if Composio gave you a personal MCP address.", "text", False, ""),
    ("AGENTMAIL_API_KEY", "AgentMail API key (optional)", "For Potter's own email inbox. Leave blank to add it later from chat.", "secret", False, ""),
    ("AGENTMAIL_INBOX", "AgentMail inbox address (optional)", "Potter's own email address, like potter@agentmail.to. With it, incoming mail from your addresses reaches Potter (starts in standby).", "text", False, ""),
    ("OWNER_EMAILS", "Your email addresses (optional)", "Comma-separated. Mail from these addresses can talk to Potter by email.", "text", False, ""),
    ("CO_OWNER_EMAILS", "Co-owner email addresses (optional)", "Comma-separated. People Potter also answers by email (for example a spouse). They cannot change Potter's settings.", "text", False, ""),
    ("WORK_CALENDAR", "Work calendar address (optional)", "Used only for busy/free times, so the evening check-in lands about 20 minutes after your last session. Leave blank for a 6pm check-in.", "text", False, ""),
    ("EXCLUDED_IDS", "Pages Potter must never touch (optional)", "Comma-separated ids, for example a Notion page id. Any tool call that mentions one is blocked outright.", "text", False, ""),
    ("OLLAMA_URL", "Ollama address on your Mac (optional)", "For the local model tier (Potter Local). The Mac's Tailscale address and port, like http://macbook.your-tailnet.ts.net:11434. Leave blank to add later.", "text", False, ""),
    ("OPENAI_VOICE_KEY", "OpenAI API key for voice (optional)", "Turns on voice talk in the web app. Set a spend limit on this key in OpenAI first. Leave blank to skip voice.", "secret", False, ""),
    ("HA_URL", "Home Assistant address (optional)", "For example http://homeassistant.local:8123. Read-only home status.", "text", False, ""),
    ("HA_TOKEN", "Home Assistant token (optional)", "A long-lived token for a dedicated Potter user you create in Home Assistant.", "secret", False, ""),
    ("TAILSCALE_AUTHKEY", "Tailscale auth key (optional)", "Joins this container to your tailnet and sets up Potter's funnel. Create it in the Tailscale admin page.", "secret", False, ""),
]
SECRET_KEYS = {k for k, _, _, kind, _, _ in FIELDS if kind == "secret"}


def validate(v: dict) -> dict[str, str]:
    """Return {field: message} for every problem; empty means valid. Never includes the offending value."""
    err: dict[str, str] = {}
    for key, label, _, _, required, _ in FIELDS:
        if required and not str(v.get(key, "")).strip():
            err[key] = f"{label} is required."
    if "OWNER_NAME" not in err and not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{0,39}", v["OWNER_NAME"].strip()):
        err["OWNER_NAME"] = "Use letters only (up to 40)."
    if "TIMEZONE" not in err:
        try:
            ZoneInfo(v["TIMEZONE"].strip())
        except Exception:
            err["TIMEZONE"] = "Unknown time zone. Use a name like America/New_York."
    if "BRIEF_TIME" not in err and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v["BRIEF_TIME"].strip()):
        err["BRIEF_TIME"] = "Use 24-hour HH:MM, like 08:30."
    if "OWNER_TELEGRAM_ID" not in err and not re.fullmatch(r"\d{4,15}", v["OWNER_TELEGRAM_ID"].strip()):
        err["OWNER_TELEGRAM_ID"] = "That should be a number."
    if "TELEGRAM_TOKEN" not in err and not re.fullmatch(r"\d{5,12}:[A-Za-z0-9_-]{20,}", v["TELEGRAM_TOKEN"].strip()):
        err["TELEGRAM_TOKEN"] = "That does not look like a bot token (digits, a colon, then letters)."
    if "OPENROUTER_KEY" not in err and not re.fullmatch(r"sk-or-[A-Za-z0-9_-]{16,}", v["OPENROUTER_KEY"].strip()):
        err["OPENROUTER_KEY"] = "OpenRouter keys start with sk-or-."
    mcp = v.get("COMPOSIO_MCP_URL", "").strip()
    if mcp and not re.fullmatch(r"https://[A-Za-z0-9.-]+(/[^\s]*)?", mcp):
        err["COMPOSIO_MCP_URL"] = "That should be an https:// address."
    ck = v.get("COMPOSIO_API_KEY", "").strip()
    if ck and not re.fullmatch(r"[A-Za-z0-9_.-]{16,}", ck):
        err["COMPOSIO_API_KEY"] = "That does not look like a Composio key."
    am = v.get("AGENTMAIL_API_KEY", "").strip()
    if am and not re.fullmatch(r"[A-Za-z0-9_.-]{16,}", am):
        err["AGENTMAIL_API_KEY"] = "That does not look like an AgentMail key."
    email = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    inbox = v.get("AGENTMAIL_INBOX", "").strip()
    owners = v.get("OWNER_EMAILS", "").strip()
    if inbox and not re.fullmatch(email, inbox):
        err["AGENTMAIL_INBOX"] = "That does not look like an email address."
    if owners and not all(re.fullmatch(email, e.strip()) for e in owners.split(",")):
        err["OWNER_EMAILS"] = "Use email addresses separated by commas."
    co = v.get("CO_OWNER_EMAILS", "").strip()
    if co and not all(re.fullmatch(email, e.strip()) for e in co.split(",")):
        err["CO_OWNER_EMAILS"] = "Use email addresses separated by commas."
    if inbox and not owners and "OWNER_EMAILS" not in err:
        err["OWNER_EMAILS"] = "Add at least one of your addresses, or nobody will be allowed to email Potter."
    if inbox and not v.get("AGENTMAIL_API_KEY", "").strip():
        err["AGENTMAIL_API_KEY"] = "An inbox needs the AgentMail API key."
    wc = v.get("WORK_CALENDAR", "").strip()
    if wc and not re.fullmatch(email, wc):
        err["WORK_CALENDAR"] = "A calendar address looks like an email address."
    ex = v.get("EXCLUDED_IDS", "").strip()
    if ex and not all(re.fullmatch(r"[A-Za-z0-9-]{16,64}", x.strip()) for x in ex.split(",")):
        err["EXCLUDED_IDS"] = "Each id should be 16 or more letters and digits (dashes are fine)."
    ol = v.get("OLLAMA_URL", "").strip()
    if ol and (not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d{2,5})?/?", ol) or ol.rstrip("/").endswith("/v1")):
        err["OLLAMA_URL"] = "Use the plain address and port, like http://macbook.your-tailnet.ts.net:11434 (no /v1)."
    ov = v.get("OPENAI_VOICE_KEY", "").strip()
    if ov and not re.fullmatch(r"sk-[A-Za-z0-9_-]{20,}", ov):
        err["OPENAI_VOICE_KEY"] = "OpenAI keys start with sk-."
    ha = v.get("HA_URL", "").strip()
    if ha and not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d{2,5})?/?", ha):
        err["HA_URL"] = "Use an address like http://homeassistant.local:8123."
    ht = v.get("HA_TOKEN", "").strip()
    if bool(ha) != bool(ht):
        err["HA_TOKEN" if ha else "HA_URL"] = "Fill in both the Home Assistant address and token, or neither."
    elif ht and not re.fullmatch(r"[A-Za-z0-9_.-]{20,}", ht):
        err["HA_TOKEN"] = "That does not look like a Home Assistant token."
    ts = v.get("TAILSCALE_AUTHKEY", "").strip()
    if ts and not re.fullmatch(r"tskey-[A-Za-z0-9-]{10,}", ts):
        err["TAILSCALE_AUTHKEY"] = "Tailscale keys start with tskey-."
    return err


def to_env(v: dict) -> dict[str, str]:
    return {f"POTTER_{k}": str(v.get(k, "")).strip() for k, *_ in FIELDS}


def parse_multipart(ctype: str, raw: bytes) -> tuple[dict, bytes | None]:
    """Fields and the (single) uploaded file from a multipart/form-data body."""
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
    fields, upload = {}, None
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        if part.get_filename() is not None:
            data = part.get_payload(decode=True) or b""
            upload = data if data else upload
        else:
            fields[name] = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    return fields, upload


def check_bundle(data: bytes) -> str:
    """Empty string if the upload is a usable v2 export; otherwise a plain-language problem."""
    import io
    import zipfile
    sys.path.insert(0, str(HERE))
    import import_v2
    import profile_from_v2
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            try:
                schema = profile_from_v2.read_manifest(z).get("schema")
            except import_v2.BundleError:
                schema = None
            if schema == profile_from_v2.SCHEMA2:
                profile_from_v2.build_plan(z)
                return ""
            plan = import_v2.build_plan(z)
    except (import_v2.BundleError, zipfile.BadZipFile) as e:
        return f"That file is not a usable v2 export: {e}"
    if plan["problems"]:
        return "The export has problems: " + "; ".join(plan["problems"][:3])
    return ""


def render_form(token: str, errors: dict[str, str], values: dict, saved: frozenset = frozenset(),
                banner: str = "", bundle_saved: bool = False) -> str:
    rows = [f'<div class="err" role="alert">{html.escape(banner)}</div>'] if banner else []
    for key, label, help_, kind, required, default in FIELDS:
        val = "" if kind == "secret" else html.escape(values.get(key, default))
        msg = f'<div class="err">{html.escape(errors[key])}</div>' if key in errors else ""
        ph = ' placeholder="Saved. Leave blank to keep it."' if kind == "secret" and key in saved and key not in errors else ""
        rows.append(
            f'<label for="{key}">{html.escape(label)}{"" if required else " <span>optional</span>"}</label>'
            f'<input id="{key}" name="{key}" type="{"password" if kind == "secret" else "text"}" value="{val}"{ph} '
            f'autocomplete="off" spellcheck="false">'
            f'<div class="help">{html.escape(help_)}</div>{msg}')
    rows.append('<label for="V2_BUNDLE">Potter v2 export file <span>optional</span></label>'
                '<input id="V2_BUNDLE" name="V2_BUNDLE" type="file" accept=".zip">'
                '<div class="help">The zip your v2 Potter makes when you paste it the export message. It gives v3 your personal rules, '
                'preferences and past lessons (v3\'s own machinery is not copied from v2). Leave empty to install without it.' +
                (' <b>Already uploaded; leave empty to keep it.</b>' if bundle_saved else '') + '</div>' +
                (f'<div class="err">{html.escape(errors["V2_BUNDLE"])}</div>' if "V2_BUNDLE" in errors else ""))
    return PAGE.format(body=f'<form method="post" enctype="multipart/form-data" action="/submit?t={token}">' + "".join(rows) +
                       '<button type="submit">Install Potter</button></form>',
                       title="Set up Potter")


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#14181f;--mut:#566170;--line:#d5dae1;--acc:#1f5fd1;--err:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151a;--fg:#e8ebf0;--mut:#9aa5b4;--line:#2b323c;--acc:#6ea0ff;--err:#ff8a80}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,sans-serif}}
main{{max-width:560px;margin:0 auto;padding:24px 16px 48px}}h1{{font-size:1.5rem;margin:.2em 0 .6em}}
label{{display:block;font-weight:600;margin-top:1.1em}}label span{{font-weight:400;color:var(--mut)}}
input{{box-sizing:border-box;width:100%;padding:.65em .7em;margin-top:.3em;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit;font:inherit}}
.help{{color:var(--mut);font-size:.88rem;margin-top:.25em}}.err{{color:var(--err);font-size:.9rem;margin-top:.25em}}
button{{margin-top:1.6em;width:100%;padding:.85em;border:0;border-radius:10px;background:var(--acc);color:#fff;font:inherit;font-weight:600}}
pre{{white-space:pre-wrap;border:1px solid var(--line);border-radius:8px;padding:.8em;max-height:50vh;overflow:auto;font-size:.8rem}}
</style></head><body><main><h1>{title}</h1>{body}</main></body></html>"""

PROGRESS = """<p id="s">Installing. This takes a few minutes. Keep this page open.</p><pre id="log"></pre>
<script>
const t=new URLSearchParams(location.search).get('t');
async function tick(){{try{{const r=await fetch('/status?t='+t);const j=await r.json();
document.getElementById('log').textContent=j.log;
if(j.done){{document.getElementById('s').textContent=j.ok?'Potter is installed.':'The install stopped. Read the log above.';
if(j.final){{document.getElementById('log').textContent+='\\n\\n'+j.final}}return}}}}catch(e){{}}setTimeout(tick,2000)}}tick();
</script>"""


class State:
    def __init__(self, token: str, bootstrap: list[str], draft: Path | None = None):
        self.token, self.bootstrap, self.draft = token, bootstrap, draft
        self.submitted = False
        self.started = time.time()
        self.deadline = self.started + LIFETIME_S
        self.log: list[str] = []
        self.done = self.ok = False
        self.final = ""
        self.secrets: list[str] = []
        self.kept: dict[str, str] = {}          # what was entered, in memory only, so a mistake never means retyping
        self.kept_upload: bytes | None = None
        self.lock = threading.Lock()
        self._load_draft()

    # The draft: what was entered, root-only (600) inside this container, so a closed or restarted page never means
    # typing everything again. The same keys are stored in this container once Potter is installed; the draft is
    # deleted when the install succeeds.
    def _load_draft(self) -> None:
        if not self.draft or not self.draft.exists():
            return
        try:
            d = json.loads(self.draft.read_text())
        except (OSError, ValueError):
            return
        names = {k for k, *_ in FIELDS}
        self.kept = {k: str(v) for k, v in (d.get("fields") or {}).items() if k in names and str(v).strip()}
        if d.get("bundle") and BUNDLE_PATH.exists():
            try:
                self.kept_upload = BUNDLE_PATH.read_bytes()
            except OSError:
                pass

    def save_draft(self) -> None:
        if not self.draft:
            return
        try:
            self.draft.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.draft.with_name(self.draft.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump({"fields": self.kept, "bundle": self.kept_upload is not None}, fh)
            os.replace(tmp, self.draft)
        except OSError:
            pass

    def drop_draft(self) -> None:
        if self.draft:
            try:
                self.draft.unlink()
            except OSError:
                pass

    def keep_upload(self, data: bytes) -> None:
        self.kept_upload = data
        BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(BUNDLE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

    def merge(self, form: dict) -> dict:
        """A blank key field keeps the key entered earlier (the page never shows saved keys back)."""
        out = dict(form)
        for k in SECRET_KEYS:
            if not str(out.get(k, "")).strip() and self.kept.get(k):
                out[k] = self.kept[k]
        return out

    def keep(self, form: dict, errors: dict) -> None:
        for k, *_ in FIELDS:
            v = str(form.get(k, "")).strip()
            if k not in errors and v:
                self.kept[k] = v
            elif k in errors and k in SECRET_KEYS:
                self.kept.pop(k, None)           # a rejected key is not kept
        self.save_draft()

    def failure(self) -> str:
        for line in reversed(self.log):
            if line.strip():
                return line.strip()[:300]
        return "the installer stopped"

    def scrub(self, line: str) -> str:
        for s in self.secrets:
            if s:
                line = line.replace(s, "••••")
        return line

    def run(self, env_extra: dict) -> None:
        env = {**os.environ, **env_extra}
        try:
            p = subprocess.Popen(self.bootstrap, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                with self.lock:
                    self.log.append(self.scrub(line.rstrip()))
            rc = p.wait()
        except Exception as e:  # noqa: BLE001
            rc = 1
            with self.lock:
                self.log.append(f"installer failed to start: {type(e).__name__}")
        with self.lock:
            self.ok, self.done = rc == 0, True
            if self.ok:
                self.drop_draft()
            else:
                self.deadline = time.time() + RETRY_WINDOW_S
            marker = Path(os.environ.get("POTTER_FINAL_NOTE", "/etc/potter/final-note.txt"))
            if self.ok and marker.exists():
                self.final = marker.read_text()


def make_handler(state: State):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # never log request lines (tokens are in URLs)
            pass

        def _authorized(self) -> bool:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            given = (q.get("t") or [""])[0]
            return hmac.compare_digest(given, state.token) and time.time() < state.deadline

        def _send(self, code: int, body: str, ctype="text/html; charset=utf-8"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._authorized():
                return self._send(403, "Forbidden", "text/plain")
            path = urllib.parse.urlparse(self.path).path
            if path == "/status":
                with state.lock:
                    return self._send(200, json.dumps({"log": "\n".join(state.log[-200:]), "done": state.done,
                                                       "ok": state.ok, "final": state.final}), "application/json")
            if state.submitted and not (state.done and not state.ok):
                return self._send(200, PAGE.format(title="Installing Potter", body=PROGRESS))
            return self._send(200, self.form_page({}))

        def form_page(self, errors: dict, form: dict | None = None) -> str:
            failed = state.submitted and state.done and not state.ok
            banner = (f"The install stopped: {state.failure()} Everything you entered is still here. Fix what the "
                      "message points to and press Install again.") if failed else ""
            return render_form(state.token, errors, form if form is not None else state.kept,
                               frozenset(k for k in SECRET_KEYS if state.kept.get(k)), banner, state.kept_upload is not None)

        def do_POST(self):
            retry = state.submitted and state.done and not state.ok
            if not self._authorized() or urllib.parse.urlparse(self.path).path != "/submit" or (state.submitted and not retry):
                return self._send(403, "Forbidden", "text/plain")
            n = int(self.headers.get("Content-Length") or 0)
            ctype = self.headers.get("Content-Type", "")
            if n > MAX_BUNDLE + 65536 or (not ctype.startswith("multipart/") and n > 16384):
                return self._send(413, "Too large", "text/plain")
            raw = self.rfile.read(n)
            upload = None
            if ctype.startswith("multipart/"):
                form, upload = parse_multipart(ctype, raw)
            else:
                form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode(), keep_blank_values=True).items()}
            form = state.merge(form)
            errors = validate(form)
            if upload:
                msg = check_bundle(upload)
                if msg:
                    errors["V2_BUNDLE"] = msg
                else:
                    state.keep_upload(upload)
            upload = upload if upload and "V2_BUNDLE" not in errors else state.kept_upload
            state.keep(form, errors)
            if errors:
                return self._send(400, self.form_page(errors, form))
            with state.lock:
                state.submitted, state.done, state.ok, state.log, state.final = True, False, False, [], ""
                state.started = time.time()
                state.deadline = state.started + LIFETIME_S
            env = to_env(form)
            if upload:
                if state.kept_upload is not upload or not BUNDLE_PATH.exists():
                    state.keep_upload(upload)
                state.save_draft()
                env["POTTER_V2_BUNDLE"] = str(BUNDLE_PATH)
            state.secrets = [env[f"POTTER_{k}"] for k in SECRET_KEYS]
            threading.Thread(target=state.run, args=(env,), daemon=True).start()
            self.send_response(303)
            self.send_header("Location", f"/?t={state.token}")
            self.end_headers()
    return H


def main(argv: list[str]) -> int:
    port = int(os.environ.get("POTTER_FORM_PORT", "8788"))
    host = os.environ.get("POTTER_FORM_HOST", "0.0.0.0")
    token = secrets.token_urlsafe(24)
    state = State(token, [str(HERE / "bootstrap.sh")], DRAFT_PATH)
    srv = http.server.ThreadingHTTPServer((host, port), make_handler(state))
    shown = os.environ.get("POTTER_FORM_URL_HOST") or "<this-container-address>"
    print(f"\nOpen this page in your browser to set up Potter:\n  http://{shown}:{port}/?t={token}\n", flush=True)
    srv.timeout = 1
    while time.time() < state.deadline and not (state.done and state.ok):
        srv.handle_request()                     # a failed install keeps the page open for a fix and a retry
    # Linger briefly so the browser can read the final status.
    end = time.time() + 60
    while state.done and time.time() < end:
        srv.handle_request()
    return 0 if state.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
