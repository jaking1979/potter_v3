#!/usr/bin/env python3
"""One-time form for Potter's vault login (its own Vaultwarden account, which sees only the collection you share).

Serves one page protected by a random one-time token, checks what you type with the Bitwarden CLI, lets you fix
mistakes without retyping the rest, writes a root-only file, and exits. Nothing is printed or logged.

  vault_form.py --out /etc/potter/secrets/vault.env [--port 8766] [--host-label <address shown in the link>]
"""
from __future__ import annotations

import argparse
import html
import http.server
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import urllib.parse

FIELDS = [
    ("BW_SERVER", "Vault address", "Your Vaultwarden address, like https://vault.your-tailnet.ts.net", False),
    ("BW_CLIENTID", "Potter's API key: client_id", "In the web vault, signed in as POTTER: Account settings > Security > Keys > View API key. Starts with user.", False),
    ("BW_CLIENTSECRET", "Potter's API key: client_secret", "Same page.", True),
    ("BW_PASSWORD", "Potter's master password", "The master password of Potter's account (not yours).", True),
]


def validate(v: dict) -> dict:
    err = {}
    if not re.match(r"^https://[A-Za-z0-9.-]+(:\d+)?/?$", v.get("BW_SERVER", "")):
        err["BW_SERVER"] = "Use the https address of your vault, like https://vault.your-tailnet.ts.net"
    if not re.match(r"^user\.[0-9a-fA-F-]{36}$", v.get("BW_CLIENTID", "")):
        err["BW_CLIENTID"] = "That does not look like a client_id (it starts with user. and a long id)."
    if len(v.get("BW_CLIENTSECRET", "")) < 20:
        err["BW_CLIENTSECRET"] = "That does not look like a client_secret."
    if len(v.get("BW_PASSWORD", "")) < 8:
        err["BW_PASSWORD"] = "Potter's master password is at least 8 characters."
    return err


def live_check(v: dict, bw: str = "bw") -> dict:
    """Log in and unlock in a throwaway CLI profile. The password goes in the environment, never an argument."""
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "BITWARDENCLI_APPDATA_DIR": d, "BW_NOINTERACTION": "true", **v}
        run = lambda *a: subprocess.run([bw, *a], env=env, capture_output=True, text=True, timeout=60)  # noqa: E731
        try:
            run("config", "server", v["BW_SERVER"].rstrip("/"))
            li = run("login", "--apikey")
        except (OSError, subprocess.TimeoutExpired):
            return {"BW_SERVER": "Could not run the Bitwarden CLI or reach the vault. Is the address right?"}
        if li.returncode != 0:
            return {"BW_CLIENTSECRET": "The vault refused this API key. Check client_id and client_secret (Potter's account)."}
        un = run("unlock", "--passwordenv", "BW_PASSWORD", "--raw")
        if un.returncode != 0 or not un.stdout.strip():
            return {"BW_PASSWORD": "The key worked but this master password did not unlock Potter's vault."}
    return {}


def page(token: str, values: dict, errors: dict, done: bool = False) -> bytes:
    if done:
        body = "<h1>Done</h1><p>Potter's vault is connected. You can close this page and tell Potter \"what's in your vault\".</p>"
    else:
        rows = []
        for key, label, help_, secret in FIELDS:
            val = "" if secret else html.escape(values.get(key, ""), quote=True)
            keep = " (kept from last time; leave blank to keep)" if secret and values.get(key) and key not in errors else ""
            e = f'<div class="err">{html.escape(errors[key])}</div>' if key in errors else ""
            rows.append(f'<label>{html.escape(label)}{keep}<input name="{key}" type="{"password" if secret else "text"}" value="{val}" autocomplete="off"></label><div class="help">{html.escape(help_)}</div>{e}')
        body = f'<h1>Connect Potter\'s vault</h1><form method="post" action="/{token}">{"".join(rows)}<button>Connect</button></form>'
    return (f'<!doctype html><meta name="viewport" content="width=device-width"><title>Potter vault</title><style>body{{font:16px system-ui;max-width:560px;margin:2em auto;padding:0 1em}}label{{display:block;margin-top:1.2em;font-weight:600}}input{{width:100%;padding:.5em;font:inherit}}.help{{color:#555;font-size:14px}}.err{{color:#b00;font-size:14px}}button{{margin-top:1.5em;padding:.6em 1.2em;font:inherit}}</style>{body}').encode()


def serve(out: str, port: int, label: str, bw: str = "bw") -> None:
    token = secrets.token_urlsafe(24)
    state = {"values": {}, "errors": {}, "done": False}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # nothing is logged
            pass

        def _send(self, body: bytes, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != f"/{token}":
                return self._send(b"Not found", 404)
            self._send(page(token, state["values"], state["errors"], state["done"]))

        def do_POST(self):
            if self.path != f"/{token}" or state["done"]:
                return self._send(b"Not found", 404)
            n = min(int(self.headers.get("Content-Length", "0") or 0), 20000)
            form = {k: v[0].strip() for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
            for key, _, _, secret in FIELDS:
                if form.get(key) or not secret:
                    state["values"][key] = form.get(key, "")
            errs = validate(state["values"]) or live_check(state["values"], bw)
            state["errors"] = errs
            if not errs:
                fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    for key, *_ in FIELDS:
                        f.write(f"{key}={state['values'][key].rstrip('/') if key == 'BW_SERVER' else state['values'][key]}\n")
                state["done"] = True
                threading.Timer(2, srv.shutdown).start()
            self._send(page(token, state["values"], errs, state["done"]))

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
    print(f"Open this link on a device that can reach Potter (home network or Tailscale): http://{label}:{port}/{token}", flush=True)
    srv.serve_forever()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host-label", default="<potter's address>")
    ap.add_argument("--bw", default="bw")
    a = ap.parse_args(argv)
    os.makedirs(os.path.dirname(a.out), mode=0o700, exist_ok=True)
    serve(a.out, a.port, a.host_label, a.bw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
