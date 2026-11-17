#!/usr/bin/env python3
"""Potter settings page: the place secrets go (never chat). Stdlib only, runs as a small service.

Sign in with the Potter web password. The page lists the secrets the v2 import still needs (from
/etc/potter/handover.json) plus any extra name you add, takes a value in a password box, stores it where it
belongs, and only ever shows "set" or "not set" afterwards.

A secret's target (set by the v2 export manifest) is one of:
  {"kind": "app_env",   "app": "alpha-bot"}                      -> /etc/potter/env/alpha-bot.env, app restarted
  {"kind": "mcp_env",   "server": "x"}                           -> mcp.servers.x.env.<NAME>
  {"kind": "mcp_header","server": "x", "header": "Authorization", "prefix": "Bearer "}
  {"kind": "openclaw_env"}                                       -> OpenClaw's env block
"""
from __future__ import annotations

import hmac
import html
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

STATE = Path(os.environ.get("POTTER_STATE_DIR", "/etc/potter"))
PORT = int(os.environ.get("POTTER_SETTINGS_PORT", "8789"))
NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
SERVER = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
MAX_FAILS, LOCK_S, SESSION_S = 5, 300, 3600


def valid_value(v: str) -> bool:
    return bool(v) and len(v) <= 4096 and "\n" not in v and "\r" not in v and "\x00" not in v


def default_runner(cmd: list[str]) -> int:
    return subprocess.run(cmd, capture_output=True).returncode


def apply_secret(name: str, value: str, target: dict, runner=default_runner, state: Path = STATE,
                 openclaw: list[str] | None = None) -> str:
    """Store one secret. Returns '' on success, otherwise a short problem (never containing the value)."""
    oc = openclaw or ["openclaw"]
    if not NAME.match(name) or not valid_value(value):
        return "invalid name or value"
    kind = target.get("kind")
    if kind == "app_env":
        app = str(target.get("app", ""))
        if not SERVER.match(app):
            return "invalid app"
        envdir = state / "env"
        envdir.mkdir(parents=True, exist_ok=True)
        os.chmod(envdir, 0o700)
        f = envdir / f"{app}.env"
        lines = [ln for ln in (f.read_text().splitlines() if f.exists() else []) if not ln.startswith(name + "=")]
        lines.append(f"{name}={value}")
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        runner(["systemctl", "try-restart", f"potter-app-{app}.service"])
        return ""
    if kind in ("mcp_env", "mcp_header"):
        server = str(target.get("server", ""))
        if not SERVER.match(server):
            return "invalid server"
        if kind == "mcp_env":
            patch = {"mcp": {"servers": {server: {"env": {name: value}}}}}
        else:
            header = str(target.get("header", ""))
            if not HEADER.match(header):
                return "invalid header"
            patch = {"mcp": {"servers": {server: {"headers": {header: str(target.get("prefix", "")) + value}}}}}
        return _patch(patch, runner, oc, state)
    if kind == "openclaw_env":
        return _patch({"env": {name: value}}, runner, oc, state)
    return "unknown target"


def _patch(patch: dict, runner, oc: list[str], state: Path) -> str:
    tmp = state / f".patch-{secrets.token_hex(4)}.json5"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(patch, fh)
    try:
        return "" if runner(oc + ["config", "patch", "--file", str(tmp)]) == 0 else "OpenClaw rejected the change"
    finally:
        tmp.unlink(missing_ok=True)


def secret_status(name: str, target: dict, state: Path = STATE) -> bool:
    """True if a value is already stored. Never returns the value."""
    if target.get("kind") == "app_env":
        f = state / "env" / f"{target.get('app')}.env"
        return f.exists() and any(ln.startswith(name + "=") for ln in f.read_text().splitlines())
    return False


class Auth:
    def __init__(self, password: str, clock=time.time):
        self._pw, self._clock = password, clock
        self.sessions: dict[str, float] = {}
        self.fails, self.locked_until = 0, 0.0

    def login(self, given: str) -> str | None:
        now = self._clock()
        if now < self.locked_until:
            return None
        if hmac.compare_digest(given.encode(), self._pw.encode()):
            self.fails = 0
            tok = secrets.token_urlsafe(24)
            self.sessions[tok] = now + SESSION_S
            return tok
        self.fails += 1
        if self.fails >= MAX_FAILS:
            self.locked_until, self.fails = now + LOCK_S, 0
        return None

    def valid(self, tok: str) -> bool:
        exp = self.sessions.get(tok or "")
        if exp is None or exp < self._clock():
            self.sessions.pop(tok or "", None)
            return False
        return True


CSS = ("body{margin:0;font:16px/1.45 system-ui,sans-serif;background:#fff;color:#14181f}"
       "@media(prefers-color-scheme:dark){body{background:#12151a;color:#e8ebf0}}"
       "main{max-width:560px;margin:0 auto;padding:24px 16px}label{display:block;font-weight:600;margin-top:1em}"
       "input{box-sizing:border-box;width:100%;padding:.6em;margin-top:.3em;border:1px solid #8884;border-radius:8px;"
       "background:transparent;color:inherit;font:inherit}button{margin-top:1.2em;width:100%;padding:.8em;border:0;"
       "border-radius:10px;background:#1f5fd1;color:#fff;font:inherit;font-weight:600}.m{color:#7a8594;font-size:.9rem}")


def page(body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>Potter settings</title><style>{CSS}</style></head>'
            f'<body><main><h1>Potter settings</h1>{body}</main></body></html>')


def needed(state: Path) -> list[dict]:
    try:
        reg = json.loads((state / "handover.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for s in reg.get("secrets_needed", []):
        n = str(s.get("name", ""))
        if NAME.match(n):
            out.append({"name": n, "used_by": str(s.get("used_by", "")), "how": str(s.get("how_to_get", "")),
                        "target": s.get("target") or {"kind": "app_env", "app": s.get("used_by", "")}})
    return out


def make_handler(auth: Auth, state: Path, runner=default_runner, openclaw=None):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cookie(self) -> str:
            c = self.headers.get("Cookie", "")
            m = re.search(r"potter_s=([A-Za-z0-9_-]+)", c)
            return m.group(1) if m else ""

        def _send(self, code, body, extra=None):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _login_page(self, msg=""):
            return page('<form method="post" action="/login"><label>Potter password</label>'
                        '<input type="password" name="pw" autocomplete="current-password"><button>Sign in</button></form>'
                        f'<p class="m">{html.escape(msg)}</p>')

        def do_GET(self):
            if not auth.valid(self._cookie()):
                return self._send(200, self._login_page())
            rows = []
            for s in needed(state):
                st = "set" if secret_status(s["name"], s["target"], state) else "not set"
                rows.append(f'<label>{html.escape(s["name"])} <span class="m">({st}; for {html.escape(s["used_by"])})</span></label>'
                            f'<input type="password" name="{html.escape(s["name"])}" autocomplete="off">'
                            f'<div class="m">{html.escape(s["how"])}</div>')
            body = ('<form method="post" action="/save">' + ("".join(rows) or '<p class="m">Nothing is waiting for a secret.</p>')
                    + ('<button>Save</button>' if rows else "") + "</form>")
            self._send(200, page(body))

        def do_POST(self):
            n = min(int(self.headers.get("Content-Length") or 0), 65536)
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode(), keep_blank_values=True).items()}
            path = urllib.parse.urlparse(self.path).path
            if path == "/login":
                tok = auth.login(form.get("pw", ""))
                if tok:
                    return self._send(303, "", {"Location": "/", "Set-Cookie": f"potter_s={tok}; HttpOnly; SameSite=Strict; Path=/"})
                return self._send(403, self._login_page("Wrong password, or too many tries. Wait a few minutes."))
            if not auth.valid(self._cookie()):
                return self._send(403, self._login_page())
            if path == "/save":
                by_name = {s["name"]: s for s in needed(state)}
                msgs = []
                for k, v in form.items():
                    if k in by_name and v:
                        err = apply_secret(k, v, by_name[k]["target"], runner, state, openclaw)
                        msgs.append(f"{html.escape(k)}: {'saved' if not err else html.escape(err)}")
                return self._send(200, page("<p>" + "<br>".join(msgs or ["Nothing to save."]) + '</p><p><a href="/">Back</a></p>'))
            self._send(404, "Not found")
    return H


def main() -> int:
    pw_file = Path(os.environ.get("POTTER_SECRETS_DIR", "/etc/potter/secrets")) / "gateway.password"
    auth = Auth(pw_file.read_text().strip())
    srv = http.server.ThreadingHTTPServer((os.environ.get("POTTER_SETTINGS_HOST", "0.0.0.0"), PORT),
                                          make_handler(auth, STATE))
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
