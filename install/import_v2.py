#!/usr/bin/env python3
"""Import a Potter v2 export bundle into a v3 install.

The bundle is a zip produced BY v2 (see docs/V2_EXPORT_PROMPT.md): a manifest.json plus the files it names.
Nothing here reads v2 itself. Secrets are never imported: the manifest lists secret NAMES, files that look like
secrets are skipped and reported, and the owner fills values in through the settings page.

  import_v2.py plan  BUNDLE.zip
  import_v2.py apply BUNDLE.zip --workspace DIR --apps-dir DIR --state-dir DIR [--openclaw CMD] [--dry-run]

Safety defaults that are about not double-acting, not about limiting Potter:
  * every cron / app is imported DISABLED unless the manifest says touches_shared_external_state is false;
    the `handover` skill enables one when the owner says "take over X" (after v2's copy is stopped).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import PurePosixPath
from pathlib import Path

SCHEMA = "potter.v2-export/1"
MAX_FILES, MAX_TOTAL = 20000, 500 * 1024 * 1024
OVERLAY_OWNED = {"SOUL.md", "IDENTITY.md", "USER.md", "avatar.svg"}
OVERLAY_SKILLS = {"morning-brief", "connect-apps", "why-and-cost", "home-status", "inbox-triage", "handover"}
SECRET_NAME = re.compile(r"(^|/)(\.env(\..*)?|.*\.(pem|key|p12|pfx)|id_rsa.*|credentials?\.json|.*token.*\.json)$", re.I)
SECRET_TEXT = re.compile(r"(sk-or-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9]{32,}|\b\d{6,12}:[A-Za-z0-9_-]{30,}\b|"
                         r"ghp_[A-Za-z0-9]{30,}|tskey-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
CRON = re.compile(r"^[\d*/,\-]+( [\d*/,\-]+){4}$")


class BundleError(Exception):
    pass


def _safe_rel(p: str) -> str:
    pp = PurePosixPath(p)
    if pp.is_absolute() or ".." in pp.parts or not p or "\\" in p or "\x00" in p:
        raise BundleError(f"unsafe path in bundle: {p!r}")
    return str(pp)


def load_manifest(z: zipfile.ZipFile) -> dict:
    try:
        m = json.loads(z.read("manifest.json"))
    except KeyError:
        raise BundleError("manifest.json is missing")
    except ValueError:
        raise BundleError("manifest.json is not valid JSON")
    if not isinstance(m, dict) or m.get("schema") != SCHEMA:
        raise BundleError(f"manifest schema must be {SCHEMA}")
    return m


def check_zip(z: zipfile.ZipFile) -> None:
    infos = z.infolist()
    if len(infos) > MAX_FILES:
        raise BundleError("too many files in bundle")
    if sum(i.file_size for i in infos) > MAX_TOTAL:
        raise BundleError("bundle is too large when unpacked")
    for i in infos:
        _safe_rel(i.filename.rstrip("/") or "x")
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            raise BundleError(f"symlink in bundle: {i.filename}")


def build_plan(z: zipfile.ZipFile) -> dict:
    check_zip(z)
    m = load_manifest(z)
    names = set(z.namelist())
    problems, skipped = [], []
    files = []
    for f in (m.get("workspace") or {}).get("files", []):
        rel = _safe_rel(f)
        if f"workspace/{rel}" not in names:
            problems.append(f"workspace file listed but missing: {rel}")
        elif SECRET_NAME.search(rel):
            skipped.append({"file": rel, "why": "looks like a secret file"})
        else:
            files.append(rel)
    crons = []
    for c in m.get("crons", []):
        if not NAME.match(str(c.get("name", ""))):
            problems.append(f"cron has a bad name: {c.get('name')!r}")
            continue
        if not CRON.match(str(c.get("cron", ""))):
            problems.append(f"cron {c['name']}: bad schedule {c.get('cron')!r}")
            continue
        if not (c.get("message") or c.get("message_file")):
            problems.append(f"cron {c['name']}: no message")
            continue
        if c.get("message_file") and f"workspace/{_safe_rel(c['message_file'])}" not in names:
            problems.append(f"cron {c['name']}: message_file missing from bundle")
            continue
        shared = c.get("touches_shared_external_state", True) is not False
        crons.append({**c, "enabled_on_import": not shared})
    mcps = []
    for s in m.get("mcp_servers", []):
        if not NAME.match(str(s.get("name", ""))) or not (s.get("url") or s.get("command")):
            problems.append(f"mcp server entry is invalid: {s.get('name')!r}")
        else:
            mcps.append(s)
    apps = []
    for a in m.get("apps", []):
        if not NAME.match(str(a.get("name", ""))) or not a.get("path"):
            problems.append(f"app entry is invalid: {a.get('name')!r}")
            continue
        if not any(n.startswith(f"apps/{_safe_rel(a['path']).rstrip('/')}/") for n in names):
            problems.append(f"app {a['name']}: no files under apps/{a['path']}")
            continue
        shared = a.get("touches_shared_external_state", True) is not False
        apps.append({**a, "enabled_on_import": not shared})
    return {"files": files, "skipped": skipped, "crons": crons, "mcp_servers": mcps, "apps": apps,
            "secrets_needed": m.get("secrets_needed", []), "problems": problems, "manifest": m}


def public_plan(plan: dict) -> dict:
    return {"workspace_files": len(plan["files"]), "skipped_as_secret": plan["skipped"],
            "crons": [{"name": c["name"], "cron": c["cron"], "enabled_on_import": c["enabled_on_import"]}
                      for c in plan["crons"]],
            "mcp_servers": [s["name"] for s in plan["mcp_servers"]],
            "apps": [{"name": a["name"], "enabled_on_import": a["enabled_on_import"]} for a in plan["apps"]],
            "secrets_needed": [s.get("name") for s in plan["secrets_needed"]], "problems": plan["problems"]}


def _scan_text_for_secrets(data: bytes) -> bool:
    try:
        return bool(SECRET_TEXT.search(data.decode("utf-8", "ignore")))
    except Exception:
        return False


BUILTIN_KINDS = {"heartbeat", "skillCollectionReview"}   # OpenClaw creates these itself


def _every(ms) -> str | None:
    if not isinstance(ms, (int, float)) or ms < 60000:
        return None
    m = int(ms // 60000)
    return f"{m // 60}h" if m % 60 == 0 else f"{m}m"


def cron_commands(jobs: list, shared: set, now_iso: str, openclaw: list[str]) -> tuple[list, list]:
    """Turn v2's real cron inventory (inventory/cron.json) into `openclaw cron add` argv lists.

    Returns (commands, skipped). Each command is {"name", "argv", "standby"}; skipped is {"name", "why"}.
    A job named in `shared` (it trades, sends, posts or consumes a shared feed) is created disabled (standby).
    A job v2 had disabled stays disabled. Built-in heartbeat jobs and expired one-shots are skipped.
    """
    cmds, skipped = [], []
    for j in jobs if isinstance(jobs, list) else []:
        name = str(j.get("name", ""))
        if not NAME.match(name):
            skipped.append({"name": name[:60], "why": "bad name"})
            continue
        kind = j.get("payloadKind")
        sched = j.get("schedule") or {}
        if kind in BUILTIN_KINDS:
            skipped.append({"name": name, "why": "built into OpenClaw"})
            continue
        msg = j.get("message") or ""
        if kind not in ("agentTurn", "command", "systemEvent") or not msg:
            skipped.append({"name": name, "why": "no usable payload"})
            continue
        argv = openclaw + ["cron", "add", "--name", name, "--declaration-key", "v2-" + name]
        sk = sched.get("kind")
        if sk == "cron" and CRON.match(str(sched.get("cron", ""))):
            argv += ["--cron", sched["cron"], "--tz", sched.get("tz") or "UTC", "--exact"]
        elif sk == "every" and _every(sched.get("everyMs")):
            argv += ["--every", _every(sched["everyMs"])]
        elif sk == "at" and isinstance(sched.get("at"), str):
            if sched["at"] <= now_iso:
                skipped.append({"name": name, "why": "one-shot already in the past"})
                continue
            argv += ["--at", sched["at"]]
        else:
            skipped.append({"name": name, "why": "schedule not understood"})
            continue
        argv += {"agentTurn": ["--message", msg], "command": ["--command", msg],
                 "systemEvent": ["--system-event", msg]}[kind]
        target = j.get("sessionTarget")
        if isinstance(target, str) and target and kind != "systemEvent":
            argv += ["--session", target]
        if j.get("agentId") and kind == "agentTurn":
            argv += ["--agent", str(j["agentId"])]
        if j.get("displayName"):
            argv += ["--display-name", str(j["displayName"])[:80]]
        if j.get("delivery_mode") == "announce" and j.get("delivery_to"):
            argv += ["--channel", str(j.get("channel") or "telegram"), "--to", str(j["delivery_to"]),
                     "--announce", "--best-effort-deliver"]
        elif j.get("delivery_mode") == "none":
            argv += ["--no-deliver"]
        standby = name in shared
        if standby or j.get("enabled") is False:
            argv += ["--disabled"]
        cmds.append({"name": name, "argv": argv, "standby": standby, "enabled_in_v2": j.get("enabled") is not False})
    return cmds, skipped


INSTALL_OK = {
    "pip install -r requirements.txt": lambda d: [str(d / ".venv/bin/pip"), "install", "-r", "requirements.txt"],
    "python3 -m pip install -r requirements.txt": lambda d: [str(d / ".venv/bin/pip"), "install", "-r", "requirements.txt"],
    "npm ci": lambda d: ["npm", "ci"],
    "npm install": lambda d: ["npm", "install"],
}


def unit_text(name: str, path: Path, exec_line: str, env_dir: Path) -> str:
    safe = exec_line.replace("\n", " ").replace("%", "%%")
    return (f"[Unit]\nDescription=Potter app {name} (imported from v2)\nAfter=network-online.target\n\n"
            f"[Service]\nWorkingDirectory={path}\nEnvironment=PATH={path}/.venv/bin:/usr/local/bin:/usr/bin:/bin\n"
            f"EnvironmentFile=-{env_dir}/{name}.env\nExecStart=/bin/bash -lc '{safe.replace(chr(39), chr(34))}'\n"
            f"Restart=always\nRestartSec=10\n\n[Install]\nWantedBy=multi-user.target\n")


def apply(zpath: Path, workspace: Path, apps_dir: Path, state_dir: Path, openclaw: list[str], dry: bool,
          systemd_dir: Path | None = None, run_installs: bool = False) -> dict:
    with zipfile.ZipFile(zpath) as z:
        plan = build_plan(z)
        if plan["problems"]:
            raise BundleError("bundle has problems: " + "; ".join(plan["problems"][:5]))
        report = public_plan(plan)
        report["copied"], report["kept_overlay_version"], report["skipped_secret_text"] = 0, [], []
        if dry:
            return report
        ref = workspace / "imported-v2"
        for rel in plan["files"]:
            data = z.read(f"workspace/{rel}")
            if _scan_text_for_secrets(data):
                report["skipped_secret_text"].append(rel)
                continue
            parts = PurePosixPath(rel).parts
            overlay = rel in OVERLAY_OWNED or (len(parts) >= 2 and parts[0] == "skills" and parts[1] in OVERLAY_SKILLS)
            dest = (ref / rel) if overlay else (workspace / rel)
            if overlay:
                report["kept_overlay_version"].append(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            report["copied"] += 1
        # apps: copy source trees
        for a in plan["apps"]:
            src = _safe_rel(a["path"]).rstrip("/") + "/"
            dst = apps_dir / a["name"]
            for n in z.namelist():
                if n.startswith(f"apps/{src}") and not n.endswith("/") and not SECRET_NAME.search(n):
                    data = z.read(n)
                    if _scan_text_for_secrets(data):
                        report["skipped_secret_text"].append(n)
                        continue
                    out = dst / n[len(f"apps/{src}"):]
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(data)
        # app install steps (fixed safe list only) and service units (never started here)
        report["app_installs"], report["units"] = [], []
        for a in plan["apps"]:
            dst = apps_dir / a["name"]
            step = (a.get("install") or "").strip()
            if step and run_installs:
                make = INSTALL_OK.get(step)
                if make is None:
                    report["app_installs"].append({"app": a["name"], "ok": False, "error": "install step not on the safe list"})
                else:
                    if step.endswith("requirements.txt"):
                        subprocess.run([sys.executable, "-m", "venv", str(dst / ".venv")], capture_output=True)
                    r = subprocess.run(make(dst), cwd=dst, capture_output=True, text=True)
                    report["app_installs"].append({"app": a["name"], "ok": r.returncode == 0,
                                                   "error": r.stderr.strip()[-200:] if r.returncode else ""})
            ex = ((a.get("service") or {}).get("exec") or "").strip()
            if ex and systemd_dir is not None:
                systemd_dir.mkdir(parents=True, exist_ok=True)
                (systemd_dir / f"potter-app-{a['name']}.service").write_text(
                    unit_text(a["name"], dst, ex, state_dir / "env"), encoding="utf-8")
                report["units"].append(f"potter-app-{a['name']}.service")
        # crons
        results = []
        for c in plan["crons"]:
            msg = c.get("message")
            if not msg and c.get("message_file"):
                msg = z.read(f"workspace/{_safe_rel(c['message_file'])}").decode("utf-8")
            cmd = openclaw + ["cron", "add", "--name", c["name"], "--declaration-key", "v2-" + c["name"],
                              "--cron", c["cron"], "--tz", c.get("tz", "America/New_York"), "--exact",
                              "--message", msg]
            if c.get("channel"):
                cmd += ["--channel", c["channel"]]
            if c.get("to"):
                cmd += ["--to", str(c["to"])]
            if c.get("deliver", True):
                cmd += ["--announce", "--best-effort-deliver"]
            if not c["enabled_on_import"] or c.get("enabled") is False:
                cmd += ["--disabled"]
            r = subprocess.run(cmd, capture_output=True, text=True)
            results.append({"name": c["name"], "ok": r.returncode == 0,
                            "error": "" if r.returncode == 0 else r.stderr.strip()[-200:]})
        report["cron_results"] = results
        # MCP servers (no secret values; env names are left for the settings page)
        servers = {}
        for s in plan["mcp_servers"]:
            e = {"enabled": True}
            if s.get("url"):
                e.update(url=s["url"], transport=s.get("transport", "streamable-http"))
            else:
                e.update(command=s["command"], args=s.get("args", []))
            servers[s["name"]] = e
        if servers:
            (state_dir / "mcp-import.json").write_text(json.dumps({"mcp": {"servers": servers}}), encoding="utf-8")
        # handover registry
        reg = {"crons": [{"name": c["name"], "enabled": c["enabled_on_import"]} for c in plan["crons"]],
               "apps": [{"name": a["name"], "service": a.get("service"), "enabled": a["enabled_on_import"],
                         "path": str(apps_dir / a["name"])} for a in plan["apps"]],
               "secrets_needed": plan["secrets_needed"]}
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "handover.json").write_text(json.dumps(reg, indent=1), encoding="utf-8")
        return report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("bundle")
    a = sub.add_parser("apply"); a.add_argument("bundle")
    a.add_argument("--workspace", required=True); a.add_argument("--apps-dir", required=True)
    a.add_argument("--state-dir", required=True); a.add_argument("--openclaw", default="openclaw")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--systemd-dir"); a.add_argument("--run-installs", action="store_true")
    ns = ap.parse_args(argv)
    try:
        if ns.cmd == "plan":
            with zipfile.ZipFile(ns.bundle) as z:
                print(json.dumps(public_plan(build_plan(z)), indent=1))
            return 0
        rep = apply(Path(ns.bundle), Path(ns.workspace), Path(ns.apps_dir), Path(ns.state_dir),
                    ns.openclaw.split(), ns.dry_run,
                    Path(ns.systemd_dir) if ns.systemd_dir else None, ns.run_installs)
        print(json.dumps(rep, indent=1))
        return 0
    except (BundleError, zipfile.BadZipFile) as e:
        print(f"import failed: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
