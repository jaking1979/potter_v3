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
    ("COMPOSIO_MCP_URL", "Composio MCP URL (optional)", "Gives Potter your Google, Todoist and other connected apps. Leave blank to add it later from chat.", "secret", False, ""),
    ("AGENTMAIL_API_KEY", "AgentMail API key (optional)", "For Potter's own email inbox. Leave blank to add it later from chat.", "secret", False, ""),
    ("AGENTMAIL_INBOX", "AgentMail inbox address (optional)", "Potter's own email address, like potter@agentmail.to. With it, incoming mail from your addresses reaches Potter (starts in standby).", "text", False, ""),
    ("OWNER_EMAILS", "Your email addresses (optional)", "Comma-separated. Only mail from these addresses can talk to Potter by email.", "text", False, ""),
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
    if inbox and not owners and "OWNER_EMAILS" not in err:
        err["OWNER_EMAILS"] = "Add at least one of your addresses, or nobody will be allowed to email Potter."
    if inbox and not v.get("AGENTMAIL_API_KEY", "").strip():
        err["AGENTMAIL_API_KEY"] = "An inbox needs the AgentMail API key."
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
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            plan = import_v2.build_plan(z)
    except (import_v2.BundleError, zipfile.BadZipFile) as e:
        return f"That file is not a usable v2 export: {e}"
    if plan["problems"]:
        return "The export has problems: " + "; ".join(plan["problems"][:3])
    return ""


def render_form(token: str, errors: dict[str, str], values: dict) -> str:
    rows = []
    for key, label, help_, kind, required, default in FIELDS:
        val = "" if kind == "secret" else html.escape(values.get(key, default))
        msg = f'<div class="err">{html.escape(errors[key])}</div>' if key in errors else ""
        rows.append(
            f'<label for="{key}">{html.escape(label)}{"" if required else " <span>optional</span>"}</label>'
            f'<input id="{key}" name="{key}" type="{"password" if kind == "secret" else "text"}" value="{val}" '
            f'autocomplete="off" spellcheck="false">'
            f'<div class="help">{html.escape(help_)}</div>{msg}')
    rows.append('<label for="V2_BUNDLE">Potter v2 export file <span>optional</span></label>'
                '<input id="V2_BUNDLE" name="V2_BUNDLE" type="file" accept=".zip">'
                '<div class="help">The zip your v2 Potter makes when you paste it the export message. It brings v2\'s rules, '
                'prompts, schedules and apps into v3. Leave empty to install without it.</div>' +
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
    def __init__(self, token: str, bootstrap: list[str]):
        self.token, self.bootstrap = token, bootstrap
        self.submitted = False
        self.started = time.time()
        self.log: list[str] = []
        self.done = self.ok = False
        self.final = ""
        self.secrets: list[str] = []
        self.lock = threading.Lock()

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
            return hmac.compare_digest(given, state.token) and time.time() - state.started < LIFETIME_S

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
            if state.submitted:
                return self._send(200, PAGE.format(title="Installing Potter", body=PROGRESS))
            return self._send(200, render_form(state.token, {}, {}))

        def do_POST(self):
            if not self._authorized() or urllib.parse.urlparse(self.path).path != "/submit" or state.submitted:
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
            errors = validate(form)
            if upload:
                msg = check_bundle(upload)
                if msg:
                    errors["V2_BUNDLE"] = msg
            if errors:
                return self._send(400, render_form(state.token, errors, form))
            state.submitted = True
            env = to_env(form)
            if upload:
                BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(BUNDLE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(upload)
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
    state = State(token, [str(HERE / "bootstrap.sh")])
    srv = http.server.ThreadingHTTPServer((host, port), make_handler(state))
    shown = os.environ.get("POTTER_FORM_URL_HOST") or "<this-container-address>"
    print(f"\nOpen this page in your browser to set up Potter:\n  http://{shown}:{port}/?t={token}\n", flush=True)
    srv.timeout = 1
    while time.time() - state.started < LIFETIME_S and not state.done:
        srv.handle_request()
    # Linger briefly so the browser can read the final status.
    end = time.time() + 60
    while state.done and time.time() < end:
        srv.handle_request()
    return 0 if state.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
