#!/usr/bin/env bash
# Runs INSIDE the target machine or container (Debian/Ubuntu, as root): installs the current Node,
# the latest OpenClaw, applies the Potter overlay, and starts the gateway.
# Inputs come from POTTER_* environment variables (the wizard in install.sh collects them).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

[ "$(id -u)" = 0 ] || die "run as root"
# Do not trust the PATH this was started with (pct exec and the setup form can hand over a minimal one): the
# commands installed below live in these folders.
export PATH="$POTTER_HOME/openclaw/bin:$POTTER_HOME/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"
need_cmd curl; need_cmd python3; need_cmd tar

collect_inputs
if [ -z "${POTTER_SKIP_NET_CHECKS:-}" ]; then
  say "Checking the Telegram bot token"
  curl -fsS -m 20 "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getMe" | python3 -c 'import json,sys; assert json.load(sys.stdin)["ok"]' \
    || die "Telegram rejected that bot token"
fi

say "Installing Node (current LTS line OpenClaw requires)"
if ! command -v node >/dev/null 2>&1 || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 24 ]; then
  mkdir -p "$POTTER_HOME"
  NODE_VER="$(curl -fsS -m 30 https://nodejs.org/dist/index.json | python3 -c 'import json,sys; print([x["version"] for x in json.load(sys.stdin) if x["version"].startswith("v24.")][0])')"
  arch="$(uname -m)"; case "$arch" in x86_64) narch=x64;; aarch64) narch=arm64;; *) die "unsupported CPU $arch";; esac
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  curl -fsS -m 300 -o "$tmp/node.tar.xz" "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-$narch.tar.xz"
  curl -fsS -m 30  -o "$tmp/SHASUMS" "https://nodejs.org/dist/$NODE_VER/SHASUMS256.txt"
  want="$(grep " node-$NODE_VER-linux-$narch.tar.xz\$" "$tmp/SHASUMS" | cut -d' ' -f1)"
  [ -n "$want" ] && echo "$want  $tmp/node.tar.xz" | sha256sum -c - >/dev/null || die "Node checksum mismatch"
  rm -rf "$POTTER_HOME/node"; mkdir -p "$POTTER_HOME/node"
  tar -xf "$tmp/node.tar.xz" -C "$POTTER_HOME/node" --strip-components=1
  ln -sf "$POTTER_HOME/node/bin/node" /usr/local/bin/node
  ln -sf "$POTTER_HOME/node/bin/npm"  /usr/local/bin/npm
  ln -sf "$POTTER_HOME/node/bin/npx"  /usr/local/bin/npx
fi
export PATH="$POTTER_HOME/node/bin:$PATH"

say "Installing OpenClaw (${POTTER_OPENCLAW_VERSION:-latest})"
npm install -g --prefix "$POTTER_HOME/openclaw" "openclaw@${POTTER_OPENCLAW_VERSION:-latest}" --allow-scripts=openclaw >/dev/null
# npm 11.19 runs OpenClaw's own setup script when it is allowed but then skips linking the command (found on a real
# install). Link it from the package's declared entry point when npm did not.
OC_PKG="$POTTER_HOME/openclaw/lib/node_modules/openclaw"
if [ ! -x "$POTTER_HOME/openclaw/bin/openclaw" ]; then
  oc_entry="$(python3 -c 'import json,sys; b=json.load(open(sys.argv[1]))["bin"]; print(b if isinstance(b,str) else b["openclaw"])' "$OC_PKG/package.json")" \
    || die "OpenClaw installed without a command entry point"
  mkdir -p "$POTTER_HOME/openclaw/bin"
  ln -sf "$OC_PKG/$oc_entry" "$POTTER_HOME/openclaw/bin/openclaw"
fi
ln -sf "$POTTER_HOME/openclaw/bin/openclaw" /usr/local/bin/openclaw
hash -r
openclaw --version || die "OpenClaw was installed but its command does not run"

say "Saving secrets (root-only)"
umask 077; mkdir -p "$SECRETS_DIR"
printf '%s' "$TELEGRAM_TOKEN" > "$SECRETS_DIR/telegram.token"
chmod 700 "$SECRETS_DIR"; chmod 600 "$SECRETS_DIR/telegram.token"

# A rerun (retry after a failed install) keeps the password of the first run: the gateway, the settings page and
# the owner's browser already use it.
if [ -s "$SECRETS_DIR/gateway.password" ]; then
  GATEWAY_PASSWORD="$(cat "$SECRETS_DIR/gateway.password")"
else
  GATEWAY_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')"
  printf '%s' "$GATEWAY_PASSWORD" > "$SECRETS_DIR/gateway.password"; chmod 600 "$SECRETS_DIR/gateway.password"
fi
# A gateway left running by an earlier attempt is stopped; it is started again, with this run's settings, below.
if [ -z "${POTTER_NO_DAEMON:-}" ] && command -v systemctl >/dev/null 2>&1; then
  systemctl stop potter-gateway.service >/dev/null 2>&1 || true
else
  if [ -s "$POTTER_HOME/gateway.pid" ]; then kill "$(cat "$POTTER_HOME/gateway.pid")" >/dev/null 2>&1 || true; sleep 2; fi
fi

say "Onboarding OpenClaw (non-interactive)"
# A rerun finds OpenClaw already set up: onboarding then parks a changed key as an inactive replacement and exits
# with an error. That is expected; the next step puts the key in place and the key check below proves it.
RERUN=""; oc config file >/dev/null 2>&1 && [ -s "$(oc config file 2>/dev/null)" ] && RERUN=1
if ! oc onboard --non-interactive --accept-risk --flow manual --mode local \
   --auth-choice openrouter-api-key --openrouter-api-key "$OPENROUTER_KEY" \
   --gateway-bind loopback --gateway-auth password --gateway-password "$GATEWAY_PASSWORD" --agent-name Potter \
   --no-install-daemon --skip-health >/dev/null; then
  [ -n "$RERUN" ] || die "OpenClaw onboarding failed"
fi
# On a rerun, onboarding keeps the earlier key active and parks a new one as an inactive replacement. The key from
# this run always goes into the default sign-in (through stdin, never the command line), which also clears any
# cooldown the old key caused; parked replacements from earlier attempts are removed.
printf '%s\n' "$OPENROUTER_KEY" | oc models auth paste-api-key --provider openrouter --profile-id openrouter:default \
  --agent potter >/dev/null 2>&1 || die "the OpenRouter key could not be saved"
python3 - "$(oc models auth list --provider openrouter --agent potter --json 2>/dev/null)" <<'PY' | while read -r stale; do
import json, sys
text = sys.argv[1]
try:
    d = json.JSONDecoder().raw_decode(text, text.index("{"))[0]
except ValueError:
    d = {}
for p in d.get("profiles") or []:
    if p.get("id") != "openrouter:default":
        print(p["id"])
PY
  q oc models auth logout "$stale" --agent potter --yes
done
if [ -z "${POTTER_SKIP_NET_CHECKS:-}" ]; then
  say "Checking that OpenRouter accepts the key for Potter's models"
  # One real call per configured model, made while the gateway is stopped (the check refuses to run beside it). A refused key, no credit or a model OpenRouter does not offer stops here
  # with OpenRouter's own reason, before anything is scheduled (the setup page keeps your entries for a retry).
  probe="$(oc models status --agent potter --probe --probe-provider openrouter --probe-timeout 45000 --json 2>&1 || true)"
  why="$(printf '%s' "$probe" | python3 "$HERE/probe_result.py")"
  case "$why" in
    "") ;;
    REFUSED:*) die "OpenRouter refused the request (${why#REFUSED: }). Check the key, its credit and its limit on openrouter.ai, then press Install again." ;;
    *) warn "the OpenRouter check could not be read (${why#UNCLEAR: }); going on. If Potter does not answer on Telegram, ask for: openclaw models status --probe" ;;
  esac
fi

say "Applying the Potter overlay"
export OWNER_NAME TIMEZONE OWNER_TELEGRAM_ID SECRETS_DIR
patch="$(mktemp --suffix=.json5)"; render_overlay "$REPO/overlay/config.base.json5" "$patch"
oc config patch --file "$patch" --dry-run >/dev/null
oc config patch --file "$patch" >/dev/null; rm -f "$patch"
WS="$(oc config get agents.entries.potter.workspace | tr -d '"')"
[ -d "$WS" ] || die "agent workspace not found"
for d in rules learnings state; do mkdir -p "$WS/$d"; cp -rn "$REPO/overlay/workspace/$d/." "$WS/$d/"; done
cp "$REPO/overlay/workspace/AGENTS.md" "$WS/AGENTS.md"
for f in IDENTITY.md SOUL.md USER.md; do
  render_overlay "$REPO/overlay/workspace/$f" "$WS/$f"
done
rm -f "$WS/BOOTSTRAP.md"
cp "$REPO/overlay/workspace/avatar.svg" "$WS/avatar.svg"
mkdir -p "$WS/skills"; cp -r "$REPO/overlay/workspace/skills/." "$WS/skills/"

if [ -n "${AGENTMAIL_API_KEY:-}" ]; then
  # The inbox watcher (a command job, outside the gateway) reads its key from a root-only file.
  mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"; umask 077
  printf 'AGENTMAIL_API_KEY=%s\nAGENTMAIL_INBOX=%s\n' "$AGENTMAIL_API_KEY" "${AGENTMAIL_INBOX:-}" > "$SECRETS_DIR/agentmail.env"; umask 022
  say "Installing the official AgentMail plugin (Potter's own inbox)"
  install_plugin clawhub:@agentmail/agentmail agentmail
  am="$(mktemp --suffix=.json5)"
  AGENTMAIL_API_KEY="$AGENTMAIL_API_KEY" AGENTMAIL_INBOX="${AGENTMAIL_INBOX:-}" OWNER_EMAILS="${OWNER_EMAILS:-}" python3 - "$am" <<'PY'
import json, os, sys
key = os.environ["AGENTMAIL_API_KEY"]
cfg = {"plugins": {"entries": {"agentmail": {"enabled": True, "config": {"apiKey": key}}}}}
if os.environ.get("AGENTMAIL_INBOX"):
    # The mail channel shares a feed with v2's listener, so it starts in STANDBY: an empty allowFrom denies every
    # sender, which keeps the plugin and its CLI skill working. "take over email" sets allowFrom to the owner.
    cfg["channels"] = {"agentmail": {"enabled": True, "apiKey": key, "inboxId": os.environ["AGENTMAIL_INBOX"],
                                     "dmPolicy": "allowlist", "allowFrom": []}}
json.dump(cfg, open(sys.argv[1], "w"))
PY
  oc config patch --file "$am" --dry-run >/dev/null && oc config patch --file "$am" >/dev/null
  rm -f "$am"
  if [ -n "${AGENTMAIL_INBOX:-}" ]; then
    mkdir -p /etc/potter
    OWNER_EMAILS="${OWNER_EMAILS:-},${CO_OWNER_EMAILS:-}" python3 - <<'PY'
import json, os
p = "/etc/potter/handover.json"
reg = json.load(open(p)) if os.path.exists(p) else {"crons": [], "apps": [], "secrets_needed": []}
owners = [e.strip() for e in os.environ.get("OWNER_EMAILS", "").split(",") if e.strip()]
def once(kind, entry):                      # a rerun must not list the same item twice
    lst = reg.setdefault(kind, [])
    if not any(e.get("name") == entry["name"] for e in lst):
        lst.append(entry)
once("crons", {"name": "potter-mail-intake", "enabled": False,
    "purpose": "reads new mail in Potter's inbox (newsletters, forwards); off until the old listener is stopped"})
once("channels", {"name": "agentmail", "enabled": False,
    "enable": "openclaw config set channels.agentmail.allowFrom '" + json.dumps(owners) + "' --strict-json",
    "disable": "openclaw config set channels.agentmail.allowFrom '[]' --strict-json",
    "purpose": "email to Potter's own inbox (standby = no sender allowed)"})
json.dump(reg, open(p, "w"), indent=1)
PY
  fi
fi

if [ -n "${OLLAMA_URL:-}" ]; then
  say "Pointing the local model tier at Ollama on your Mac"
  op="$(mktemp --suffix=.json5)"
  OLLAMA_URL="$OLLAMA_URL" python3 -c 'import json,os,sys
json.dump({"models":{"providers":{"ollama":{"baseUrl":os.environ["OLLAMA_URL"].rstrip("/"),"api":"ollama","models":[]}}}}, open(sys.argv[1],"w"))' "$op"
  q oc config patch --file "$op"; rm -f "$op"
  # Reachability is reported, not required: the Mac may be asleep during install.
  curl -fsS -m 8 "${OLLAMA_URL%/}/api/tags" >/dev/null 2>&1 || warn "Ollama did not answer at that address right now; the local tier will work once the Mac is reachable."
fi

if [ -n "${OPENAI_VOICE_KEY:-}" ]; then
  say "Turning on voice talk"
  vp="$(mktemp --suffix=.json5)"
  OPENAI_VOICE_KEY="$OPENAI_VOICE_KEY" python3 -c 'import json,os,sys; json.dump({"talk":{"realtime":{"provider":"openai","providers":{"openai":{"apiKey":os.environ["OPENAI_VOICE_KEY"]}}}}}, open(sys.argv[1],"w"))' "$vp"
  oc config patch --file "$vp" >/dev/null; rm -f "$vp"
fi

if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
  say "Joining your tailnet (Potter's web app is reachable from your own devices only)"
  command -v tailscale >/dev/null 2>&1 || { curl -fsSL https://tailscale.com/install.sh | sh >/dev/null; }
  # Userspace networking: works in an unprivileged container with no changes to the Proxmox host (as v2 does).
  mkdir -p /etc/default; printf 'PORT="41641"\nFLAGS="--tun=userspace-networking"\n' > /etc/default/tailscaled
  if command -v systemctl >/dev/null 2>&1 && [ -z "${POTTER_NO_DAEMON:-}" ]; then
    systemctl enable tailscaled >/dev/null 2>&1; systemctl restart tailscaled
  else
    tailscaled --tun=userspace-networking --state=/var/lib/tailscale/tailscaled.state >/dev/null 2>&1 &
  fi
  sleep 3
  tailscale up --authkey "$TAILSCALE_AUTHKEY" --hostname "${POTTER_TS_HOSTNAME:-potter-v3}" --accept-dns=false >/dev/null
  # serve = only devices on your tailnet can open Potter (the security audit flags a public funnel as critical).
  # POTTER_TS_MODE=funnel opens it to the internet (any browser, anywhere; still behind the password and per-device
  # approval). The owner's choice is remembered in /etc/potter/remote-access, so a rerun or update keeps it.
  TS_MODE="${POTTER_TS_MODE:-$(cat /etc/potter/remote-access 2>/dev/null || echo serve)}"
  mkdir -p /etc/potter; printf '%s\n' "$TS_MODE" > /etc/potter/remote-access
  oc config set gateway.tailscale.mode "$TS_MODE" >/dev/null
fi

# Notion is always registered (sign-in happens later from chat); Composio and Home Assistant when given.
if true; then
  say "Connecting MCP servers"
  mcp="$(mktemp --suffix=.json5)"
  COMPOSIO_MCP_URL="${COMPOSIO_MCP_URL:-}" COMPOSIO_API_KEY="${COMPOSIO_API_KEY:-}" HA_URL="${HA_URL:-}" HA_TOKEN="${HA_TOKEN:-}" python3 - "$mcp" <<'PY'
import json, os, sys
servers = {}
servers["notion"] = {"transport": "streamable-http", "url": "https://mcp.notion.com/mcp", "auth": "oauth"}
if os.environ.get("COMPOSIO_API_KEY") or os.environ.get("COMPOSIO_MCP_URL"):
    c = {"transport": "streamable-http", "url": os.environ.get("COMPOSIO_MCP_URL") or "https://connect.composio.dev/mcp"}
    if os.environ.get("COMPOSIO_API_KEY"):
        c["headers"] = {"x-consumer-api-key": os.environ["COMPOSIO_API_KEY"]}
    servers["composio"] = c
if os.environ.get("HA_URL"):
    # Home Assistant's built-in MCP server (Settings > Devices & services > Model Context Protocol Server).
    # /api/mcp is its streamable HTTP endpoint; /mcp_server/sse is the older SSE one.
    servers["homeassistant"] = {"transport": "streamable-http",
                                "url": os.environ["HA_URL"].rstrip("/") + "/api/mcp",
                                "headers": {"Authorization": "Bearer " + os.environ["HA_TOKEN"]}}
json.dump({"mcp": {"servers": servers}}, open(sys.argv[1], "w"))
PY
  oc config patch --file "$mcp" --dry-run >/dev/null && oc config patch --file "$mcp" >/dev/null
  rm -f "$mcp"
fi

say "Installing Potter's guardrails (hard wall, docs-before-build, config write guard)"
gdir="$(mktemp -d)"; cp "$REPO/overlay/plugins/potter-guardrails/"{index.js,guards.js,openclaw.plugin.json,package.json} "$gdir/"
install_plugin "$gdir" potter-guardrails --force
gp="$(mktemp --suffix=.json5)"
EXCLUDED_IDS="${EXCLUDED_IDS:-}" python3 -c 'import json,os,sys
ids=[x.strip() for x in os.environ["EXCLUDED_IDS"].split(",") if x.strip()]
json.dump({"plugins":{"entries":{"potter-guardrails":{"enabled":True,"config":{"excludedIds":ids}}}}}, open(sys.argv[1],"w"))' "$gp"
oc config patch --file "$gp" >/dev/null; rm -rf "$gp" "$gdir"

say "Installing the knowledge graph, monitors, quality control and finance tools"
mkdir -p "$POTTER_HOME/tools" "$POTTER_HOME/plugins" "$WS/state/qc" "$WS/state/monitors" "$WS/trading"
cp -r "$REPO/overlay/tools/." "$POTTER_HOME/tools/"
rm -rf "$POTTER_HOME/plugins/potter-kg" "$POTTER_HOME/jev"
cp -r "$REPO/overlay/plugins/potter-kg" "$POTTER_HOME/plugins/potter-kg"; rm -rf "$POTTER_HOME/plugins/potter-kg/tests"
cp -r "$REPO/overlay/jev" "$POTTER_HOME/jev"; rm -f "$POTTER_HOME/jev/"*.test.mjs
STATE_DIR="$(dirname "$(oc config file)")"
KG_DB="$STATE_DIR/potter-kg/potter-kg.sqlite"; mkdir -p "$STATE_DIR/potter-kg"
install_plugin "$POTTER_HOME/plugins/potter-kg" potter-kg --force
kp="$(mktemp --suffix=.json5)"
KG_DB="$KG_DB" python3 -c 'import json,os,sys; json.dump({"plugins":{"entries":{"potter-kg":{"enabled":True,"config":{"dbPath":os.environ["KG_DB"]}}}}}, open(sys.argv[1],"w"))' "$kp"
q oc config patch --file "$kp"; rm -f "$kp"

say "Installing Potter Core (v3.1 control: approvals and receipts; v3.2 brain: context governor and fast path)"
cdir="$(mktemp -d)"; cp -r "$REPO/overlay/plugins/potter-core/"{index.js,lib,openclaw.plugin.json,package.json} "$cdir/"
install_plugin "$cdir" potter-core --force
pcp="$(mktemp --suffix=.json5)"
OWNER_TELEGRAM_ID="$OWNER_TELEGRAM_ID" KG_DB="$KG_DB" WS="$WS" TIMEZONE="$TIMEZONE" SECRETS_DIR="$SECRETS_DIR" STATE_DIR="$STATE_DIR" python3 -c '
import json, os, sys
cfg = {"workspace": os.environ["WS"], "timezone": os.environ.get("TIMEZONE", "UTC"), "brain": {"enabled": True, "budget": 7000}, "kg": {"enabled": True, "dbPath": os.environ["KG_DB"], "budget": 2500, "capture": True, "queuePath": os.path.join(os.environ["STATE_DIR"], "potter-core", "kg-capture-queue.jsonl"), "usagePath": os.path.join(os.environ["STATE_DIR"], "potter-core", "kg-usage.jsonl")}, "mode": os.environ.get("POTTER_CONTROL_MODE", "enforce"), "agents": ["potter"],
       "ownerIds": [os.environ["OWNER_TELEGRAM_ID"]], "standingGrants": [],
       "stateDir": os.path.join(os.environ["STATE_DIR"], "potter-core"),
       "jevEnvFile": os.path.join(os.environ["SECRETS_DIR"], "jev.env"),
       "policyPath": os.path.join(os.environ["WS"], "state", "model-policy.json"),
       "governor": {"enabled": True, "jev": True, "timeoutMs": 2500}, "fastPath": {"enabled": True, "merrick": "merrick"},
       "broker": {"enabled": True, "mode": "enforce", "escalationDailyCapUsd": 3, "escalationDailyMax": 20}}
json.dump({"plugins": {"entries": {"potter-core": {"enabled": True, "hooks": {"allowConversationAccess": True}, "config": cfg}}}},
          open(sys.argv[1], "w"))' "$pcp"
q oc config patch --file "$pcp" --replace-path plugins.entries.potter-core.config; rm -f "$pcp"; rm -rf "$cdir"
# Model policy (ratified T28): one file says which model does which work. It replaces OpenClaw's default
# (openrouter/auto, open-ended routing) with the validated candidates, and every agent job names its model.
# A policy still equal to the one shipped last time is replaced by the new one; a policy the owner (or Potter on the
# owner's instruction) has edited is left alone.
# Fingerprints of policies shipped before the .shipped copy existed (an install from then has no copy to compare with).
OLD_SHIPPED_POLICIES="5b98a1beba3f17f60937ff86f4e6cf08887d0ebcd80594a8edfa6016f1f72daf f486a762959e31278abab1749baed8b9e0d2eabb932c7c013ce4983ca5af1320"
policy_is_shipped() {
  [ -f "$WS/state/model-policy.shipped.json" ] && cmp -s "$WS/state/model-policy.json" "$WS/state/model-policy.shipped.json" && return 0
  case " $OLD_SHIPPED_POLICIES " in *" $(sha256sum "$WS/state/model-policy.json" | cut -c1-64) "*) return 0;; esac
  return 1
}
if [ ! -f "$WS/state/model-policy.json" ] || policy_is_shipped; then
  cp "$REPO/overlay/jev/policy.json" "$WS/state/model-policy.json"
elif ! cmp -s "$WS/state/model-policy.json" "$REPO/overlay/jev/policy.json"; then
  warn "state/model-policy.json was edited, so the new default policy was not applied (it is in overlay/jev/policy.json)"
fi
cp "$REPO/overlay/jev/policy.json" "$WS/state/model-policy.shipped.json"
apply_models() {
  local mp; mp="$(mktemp --suffix=.json5)"
  python3 "$HERE/apply_models.py" "$WS/state/model-policy.json" --patch "$mp" --agents "$1" \
    && q oc config patch --file "$mp" --replace-path agents.defaults.models \
    || warn "the model policy could not be applied; ask Potter to check state/model-policy.json"
  rm -f "$mp"
}
apply_models potter
# Command jobs that ask Jev (the knowledge graph curator) need the model key outside the gateway: a root-only file.
mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"; umask 077
printf 'OPENROUTER_API_KEY=%s\n' "$OPENROUTER_KEY" > "$SECRETS_DIR/jev.env"; chmod 600 "$SECRETS_DIR/jev.env"
umask 022
cat > /usr/local/bin/potter-kg-curate <<WRAP
#!/bin/sh
set -a; . "$SECRETS_DIR/jev.env"; set +a
exec node "$POTTER_HOME/plugins/potter-kg/scripts/kg-curate.mjs" --decider "$POTTER_HOME/jev/index.js" \
  --policy "$WS/state/model-policy.json" --telemetry "$WS/state/jev-telemetry.jsonl" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-kg-curate
# Knowledge-graph eval: how well the decision model sees the links in a graph (read-only; owner-run).
cat > /usr/local/bin/potter-kg-eval <<WRAP
#!/bin/sh
set -a; . "$SECRETS_DIR/jev.env"; set +a
exec node "$REPO/overlay/plugins/potter-kg/scripts/kg-eval.mjs" --decider "$POTTER_HOME/jev/index.js" \
  --policy "$WS/state/model-policy.json" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-kg-eval
cat > /usr/local/bin/potter-kg-import <<WRAP
#!/bin/sh
exec node "$REPO/overlay/plugins/potter-kg/scripts/kg-import.mjs" --into "$KG_DB" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-kg-import
cat > /usr/local/bin/potter-update <<WRAP
#!/bin/sh
exec bash "$REPO/install/self_update.sh" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-update
cat > /usr/local/bin/potter-kg-capture <<WRAP
#!/bin/sh
set -a; . "$SECRETS_DIR/jev.env"; set +a
exec node "$POTTER_HOME/plugins/potter-kg/scripts/kg-capture.mjs" --queue "$STATE_DIR/potter-core/kg-capture-queue.jsonl" --db "$KG_DB" \
  --staged "$WS/state/kg-staged.jsonl" --decider "$POTTER_HOME/jev/index.js" --policy "$WS/state/model-policy.json" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-kg-capture
cat > /usr/local/bin/potter-kg-freshness <<WRAP
#!/bin/sh
set -a; . "$SECRETS_DIR/jev.env"; set +a
exec node "$POTTER_HOME/plugins/potter-kg/scripts/kg-freshness.mjs" --db "$KG_DB" --review "$WS/state/kg-review.jsonl" \
  --usage "$STATE_DIR/potter-core/kg-usage.jsonl" --decider "$POTTER_HOME/jev/index.js" --policy "$WS/state/model-policy.json" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-kg-freshness
mkdir -p "$WS/state/forecasts"; rm -rf "$POTTER_HOME/cos"; cp -r "$REPO/overlay/cos" "$POTTER_HOME/cos"; rm -f "$POTTER_HOME/cos/"*.test.mjs
cat > /usr/local/bin/potter-cos-score <<WRAP
#!/bin/sh
set -a; . "$SECRETS_DIR/jev.env"; set +a
exec node "$POTTER_HOME/cos/score.mjs" --forecasts "$WS/state/forecasts" --db "$KG_DB" \
  --decider "$POTTER_HOME/jev/index.js" --policy "$WS/state/model-policy.json" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-cos-score
cat > /usr/local/bin/potter-mail-watch <<WRAP
#!/bin/sh
[ -f "$SECRETS_DIR/agentmail.env" ] || { echo "potter-mail-watch: AgentMail is not configured" >&2; exit 3; }
set -a; . "$SECRETS_DIR/jev.env"; . "$SECRETS_DIR/agentmail.env"; set +a
exec node "$POTTER_HOME/cos/mail-watch.mjs" --doc "$WS/state/compiling-doc.md" --state "$WS/state/mail-watch.json" \
  --decider "$POTTER_HOME/jev/index.js" --policy "$WS/state/model-policy.json" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-mail-watch
# Potter's vault: the plugin (tools say "not set up" until the owner connects a vault) and its setup command.
rm -rf "$POTTER_HOME/plugins/potter-vault"; mkdir -p "$POTTER_HOME/plugins"
cp -r "$REPO/overlay/plugins/potter-vault" "$POTTER_HOME/plugins/potter-vault"; rm -rf "$POTTER_HOME/plugins/potter-vault/tests"
install_plugin_if_changed "$POTTER_HOME/plugins/potter-vault" potter-vault --force
vp="$(mktemp --suffix=.json5)"
OWNER="$OWNER_TELEGRAM_ID" SECRETS="$SECRETS_DIR" VSTATE="$STATE_DIR/potter-vault" python3 -c 'import json,os,sys; json.dump({"plugins":{"entries":{"potter-vault":{"enabled":True,"hooks":{"allowConversationAccess":True},"config":{"envFile":os.path.join(os.environ["SECRETS"],"vault.env"),"ownerTarget":os.environ["OWNER"],"stateDir":os.environ["VSTATE"]}}}}}, open(sys.argv[1],"w"))' "$vp"
q oc config patch --file "$vp"; rm -f "$vp"
cp "$REPO/install/vault_form.py" "$POTTER_HOME/tools/vault_form.py"
cat > /usr/local/bin/potter-vault-setup <<WRAP
#!/bin/sh
# One time: connect Potter's own Vaultwarden account. Prints a link to a one-time form; the form closes itself.
command -v bw >/dev/null 2>&1 || { npm install -g --silent @bitwarden/cli >/dev/null 2>&1; [ -x "\$(npm root -g)/@bitwarden/cli/build/bw.js" ] && ln -sf "\$(npm root -g)/@bitwarden/cli/build/bw.js" /usr/local/bin/bw; }
command -v bw >/dev/null 2>&1 || { echo "Could not install the Bitwarden CLI (npm install -g @bitwarden/cli)." >&2; exit 1; }
host="\$(hostname -I 2>/dev/null | cut -d' ' -f1)"
systemctl stop potter-vault-form 2>/dev/null; rm -f /run/potter-vault-form.out
systemd-run --unit=potter-vault-form --collect --quiet -p RuntimeMaxSec=1800 -p StandardOutput=file:/run/potter-vault-form.out \
  python3 "$POTTER_HOME/tools/vault_form.py" --out "$SECRETS_DIR/vault.env" --host-label "\$host"
sleep 2; chmod 600 /run/potter-vault-form.out 2>/dev/null; cat /run/potter-vault-form.out
echo "(The form closes itself when done, or after 30 minutes.)"
WRAP
chmod 755 /usr/local/bin/potter-vault-setup
mkdir -p "$WS/state/t1"; chmod 700 "$WS/state/t1"; rm -rf "$POTTER_HOME/eval"; cp -r "$REPO/overlay/eval" "$POTTER_HOME/eval"; rm -f "$POTTER_HOME/eval/"*.test.mjs
cat > /usr/local/bin/potter-t1 <<WRAP
#!/bin/sh
set -a; . "$SECRETS_DIR/jev.env"; set +a
cmd="\$1"; [ \$# -gt 0 ] && shift
exec node "$POTTER_HOME/eval/t1.mjs" "\$cmd" --dir "$WS/state/t1" --decider "$POTTER_HOME/jev/index.js" --policy "$WS/state/model-policy.json" \
  --openclaw "openclaw ${OC_PROFILE_FLAG[*]:-}" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-t1
cat > /usr/local/bin/potter-house-ideas <<WRAP
#!/bin/sh
# Filter the week's house ideas with Jev (stdin: evidence, existing, ideas as JSON). Prints kept and dropped.
POTTER_JEV_ENV="$SECRETS_DIR/jev.env" POTTER_MODEL_POLICY="$WS/state/model-policy.json" exec node "$POTTER_HOME/jev/house-ideas.mjs"
WRAP
chmod 755 /usr/local/bin/potter-house-ideas
cp "$REPO/install/imessage_config.py" "$REPO/install/imessage_setup.sh" "$POTTER_HOME/tools/"
cat > /usr/local/bin/potter-imessage-setup <<WRAP
#!/bin/sh
OPENCLAW="openclaw ${OC_PROFILE_FLAG[*]:-}" exec bash "$POTTER_HOME/tools/imessage_setup.sh" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-imessage-setup
read -r -a JOB_MODEL_ARGS <<< "$(python3 "$HERE/apply_models.py" "$WS/state/model-policy.json" --job-args 2>/dev/null)"
MONITORS_READY=""
if [ -n "${POTTER_V2_BUNDLE:-}" ] && [ -f "$POTTER_V2_BUNDLE" ] && [ ! -f "$WS/state/monitors/config.json" ]; then
  # Your sources (venues, papers, shops, wine, deal blogs) come from the export; they are never stored in the repo.
  python3 "$HERE/monitors_from_v2.py" "$POTTER_V2_BUNDLE" "$WS/state/monitors/config.json" >/dev/null && MONITORS_READY=1 \
    || warn "monitor sources could not be read from the export; ask Potter to set up the monitors"
fi
[ -f "$WS/state/monitors/config.json" ] && MONITORS_READY=1
[ -f "$WS/state/monitors/config.json" ] || cp "$REPO/overlay/tools/monitors/config.example.json" "$WS/state/monitors/config.example.json"
[ -f "$WS/trading/MANDATE.md" ] || cp "$REPO/overlay/workspace/skills/trading-run/MANDATE.template.md" "$WS/trading/MANDATE.md"

say "Pinning the plugin trust list (bundled plugins plus the ones just installed)"
pa="$(mktemp --suffix=.json5)"
oc plugins list --json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
ids = sorted({p["id"] for p in d["plugins"] if p.get("origin") == "bundled" or p.get("enabled")})
json.dump({"plugins": {"allow": ids}}, open(sys.argv[1], "w"))' "$pa"
oc config patch --file "$pa" --replace-path plugins.allow >/dev/null && rm -f "$pa"

say "Starting the gateway"
if [ -z "${POTTER_NO_DAEMON:-}" ] && command -v systemctl >/dev/null 2>&1; then
  # Our own unit: starts at boot, restarts on failure. Independent of OpenClaw's per-user daemon installer.
  cat > /etc/systemd/system/potter-gateway.service <<UNIT
[Unit]
Description=Potter (OpenClaw gateway)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
Environment=PATH=$POTTER_HOME/node/bin:$POTTER_HOME/openclaw/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=/root
ExecStart=$POTTER_HOME/openclaw/bin/openclaw gateway run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now potter-gateway.service >/dev/null
  cat > /etc/systemd/system/potter-settings.service <<UNIT
[Unit]
Description=Potter settings page (where secrets go)
After=network-online.target

[Service]
Environment=POTTER_SECRETS_DIR=$SECRETS_DIR
Environment=PATH=$POTTER_HOME/node/bin:$POTTER_HOME/openclaw/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 $REPO/install/settings_page.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now potter-settings.service >/dev/null
else
  nohup openclaw "${OC_PROFILE_FLAG[@]}" gateway run >"${POTTER_HOME}/gateway.log" 2>&1 &
  echo $! > "$POTTER_HOME/gateway.pid"
fi
for i in $(seq 1 60); do oc health >/dev/null 2>&1 && break; sleep 2; [ "$i" = 60 ] && die "gateway did not become healthy"; done
# "Healthy" comes before the Potter agent has finished preparing (a fresh gateway, or a retry over an earlier run).
# Wait until it accepts a job: add a switched-off probe job, then remove it.
for i in $(seq 1 60); do
  if oc cron add --name potter-ready-probe --declaration-key potter-ready-probe --every 1h --agent potter --session isolated \
       --no-deliver --disabled --timeout-seconds 60 --message probe >/dev/null 2>&1; then
    python3 "$REPO/overlay/tools/cron_switch.py" remove potter-ready-probe --openclaw "openclaw ${OC_PROFILE_FLAG[*]:-}" >/dev/null 2>&1 || true
    break
  fi
  sleep 2; [ "$i" = 60 ] && die "the Potter agent did not finish starting (run: openclaw doctor --fix, then press Install again)"
done

if [ -n "${POTTER_V2_BUNDLE:-}" ] && [ -f "$POTTER_V2_BUNDLE" ]; then
  say "Building your personal profile from the v2 export (rules, preferences, lessons; no machinery is copied)"
  mkdir -p /etc/potter
  python3 "$HERE/profile_from_v2.py" apply "$POTTER_V2_BUNDLE" --workspace "$WS" --state-dir /etc/potter \
      > /etc/potter/profile-report.json || die "the v2 export could not be used (see the message above)"
  # Hard-wall ids named in your own rules are added to the guardrails automatically.
  xp="$(mktemp --suffix=.json5)"
  EXCLUDED_IDS="${EXCLUDED_IDS:-}" python3 -c 'import json,os,sys
ids=[x.strip() for x in os.environ["EXCLUDED_IDS"].split(",") if x.strip()]
ids+= [i for i in json.load(open("/etc/potter/excluded-ids.json")) if i not in ids]
json.dump({"plugins":{"entries":{"potter-guardrails":{"config":{"excludedIds":ids}}}}}, open(sys.argv[1],"w"))' "$xp"
  q oc config patch --file "$xp" --replace-path plugins.entries.potter-guardrails.config.excludedIds; rm -f "$xp"
  POTTER_V2_BUNDLE_COPY="$POTTER_V2_BUNDLE"       # still needed for Merrick's house file; removed at the end
fi


say "Turning on memory search (embeddings through OpenRouter; your preferences and rules indexed)"
. "$HERE/memory_search.sh"; potter_memory_search "$OPENROUTER_KEY"

say "Scheduling the Morning Brief"
HH="${BRIEF_TIME%:*}"; MM="${BRIEF_TIME#*:}"
oc cron add --name potter-morning-brief --declaration-key potter-morning-brief \
   --cron "$((10#$MM)) $((10#$HH)) * * 1-5" --tz "$TIMEZONE" --exact --agent potter \
   --channel telegram --to "$OWNER_TELEGRAM_ID" --announce --timeout-seconds 600 "${JOB_MODEL_ARGS[@]}" \
   --message "Run the morning-brief skill and reply with the brief only." >/dev/null

ui_host="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
mkdir -p /etc/potter
{
  echo "Potter is installed."
  [ -n "$ui_host" ] && echo "Open Potter: https://$ui_host/" || echo "Open Potter from this container's address on port 18789 (or add a Tailscale key later)."
  echo "Password for the web UI (shown once; also stored root-only in the container): $GATEWAY_PASSWORD"
  echo "Settings page (where secrets go, sign in with the same password): http://${ui_host:-<this-container-address>}:8789/"
  [ -f /etc/potter/handover.json ] && echo "Your v2 export is imported. Everything that acts on shared accounts is in standby. Tell Potter: what is in standby, and take over <name> when v2 has stopped doing it."
  echo "Next: open Telegram, message your bot, and say hello. Ask Potter to connect anything that is missing."
} > /etc/potter/final-note.txt
chmod 600 /etc/potter/final-note.txt
if [ -n "${AGENTMAIL_INBOX:-}" ]; then
  say "Scheduling mail intake (standby) and urgent-mail delivery"
  # Reads the same inbox as an older assistant's listener, so it starts OFF until you say "take over email".
  q oc cron add --name potter-mail-intake --declaration-key potter-mail-intake --every 10m --agent potter \
     --session isolated --no-deliver --timeout-seconds 300 --disabled "${JOB_MODEL_ARGS[@]}" \
     --message "Run the mail-intake skill. Output nothing."
  mkdir -p "$POTTER_HOME/tools"; cp "$REPO/overlay/tools/"*.py "$POTTER_HOME/tools/"
  q oc cron add --name potter-urgent-deliver --declaration-key potter-urgent-deliver --every 5m --no-deliver \
     --timeout-seconds 60 \
     --command "python3 $POTTER_HOME/tools/urgent_deliver.py --to $OWNER_TELEGRAM_ID --tz $TIMEZONE --queue $WS/state/urgent-mail-queue.jsonl --openclaw 'openclaw ${OC_PROFILE_FLAG[*]:-}'"
fi

say "Scheduling the evening check-in (one recurring job)"
mkdir -p "$POTTER_HOME/tools"; cp "$REPO/overlay/tools/"*.py "$POTTER_HOME/tools/"
cat > /usr/local/bin/potter-cron <<WRAP
#!/bin/sh
# Switch a job on or off by name: potter-cron on|off <name> [...]
exec python3 "$POTTER_HOME/tools/cron_switch.py" "\$@" --openclaw "openclaw ${OC_PROFILE_FLAG[*]:-}"
WRAP
chmod 755 /usr/local/bin/potter-cron
q oc cron add --name potter-evening-nudge --declaration-key potter-evening-nudge --cron "*/10 15-21 * * 1-5" \
   --tz "$TIMEZONE" --exact --no-deliver --timeout-seconds 120 \
   --command "python3 $POTTER_HOME/tools/evening_nudge.py --to $OWNER_TELEGRAM_ID --tz $TIMEZONE --state $WS/state/evening-nudge.json --calendar '${WORK_CALENDAR:-}' --openclaw 'openclaw ${OC_PROFILE_FLAG[*]:-}'"

if [ -n "${HA_URL:-}" ]; then
  say "Setting up Merrick, the home specialist"
  umask 077
  printf 'HA_URL=%s\nHA_TOKEN=%s\n' "${HA_URL%/}" "$HA_TOKEN" > "$SECRETS_DIR/ha.env"; chmod 600 "$SECRETS_DIR/ha.env"
  umask 022
  rm -rf "$POTTER_HOME/merrick"; cp -r "$REPO/overlay/merrick" "$POTTER_HOME/merrick"
  rm -f "$POTTER_HOME/merrick/testing.py" "$POTTER_HOME/merrick/house.example.json"
  mkdir -p "$POTTER_HOME/tools"; cp "$REPO/overlay/tools/"*.py "$POTTER_HOME/tools/"
  MWS="$(dirname "$WS")/workspace-merrick"; mkdir -p "$MWS/state"
  cp "$REPO/overlay/workspace-merrick/"*.md "$MWS/"
  cat > /usr/local/bin/merrick <<WRAP
#!/bin/sh
export MERRICK_STATE_DIR="$MWS/state"
export MERRICK_HOUSE="$MWS/state/house.json"
export MERRICK_HA_CMD="python3 $POTTER_HOME/tools/ha_bridge.py --env $SECRETS_DIR/ha.env"
# Jev for requests Merrick's own table does not recognise (one fast decision before any slow reasoning).
export MERRICK_DECIDE_CMD="node $POTTER_HOME/jev/merrick-decide.mjs"
export POTTER_JEV_ENV="$SECRETS_DIR/jev.env" POTTER_MODEL_POLICY="$WS/state/model-policy.json"
exec python3 -B "$POTTER_HOME/merrick" "\$@"
WRAP
  chmod 755 /usr/local/bin/merrick
  # The house file holds your own device names: it is built from your v2 export, never stored in the repo.
  MERRICK_READY=""
  if [ -n "${POTTER_V2_BUNDLE_COPY:-}" ] && [ -f "$POTTER_V2_BUNDLE_COPY" ]; then
    python3 "$HERE/house_from_v2.py" "$POTTER_V2_BUNDLE_COPY" "$MWS/state/house.json" >/dev/null && MERRICK_READY=1 \
      || warn "Merrick's house file could not be built from the export; ask Potter to build it from Home Assistant."
  fi
  oc agents list 2>/dev/null | grep -q "merrick" || q oc agents add merrick --workspace "$MWS" --non-interactive
  mp="$(mktemp --suffix=.json5)"
  printf '{ agents: { entries: { potter: { subagents: { allowAgents: ["merrick"] } } } } }' > "$mp"
  q oc config patch --file "$mp"; rm -f "$mp"
  apply_models potter,merrick
  OCF="openclaw ${OC_PROFILE_FLAG[*]:-}"
  # Reads only: on from day one (duplicate cold alerts are possible until the old assistant's watcher is stopped).
  q oc cron add --name potter-merrick-cold-watch --declaration-key potter-merrick-cold-watch --cron "0 */2 * * *" \
     --tz "$TIMEZONE" --exact --no-deliver --timeout-seconds 90 $([ -z "$MERRICK_READY" ] && echo --disabled) \
     --command "merrick --openclaw '$OCF' cold-watch --to $OWNER_TELEGRAM_ID"
  q oc cron add --name potter-merrick-collect --declaration-key potter-merrick-collect --cron "7 * * * *" \
     --tz "$TIMEZONE" --exact --no-deliver --timeout-seconds 180 $([ -z "$MERRICK_READY" ] && echo --disabled) \
     --command "merrick collect"
  # Changes thermostats the old assistant also controls: standby until you say "take over the house".
  q oc cron add --name potter-merrick-tick --declaration-key potter-merrick-tick --cron "*/5 * * * *" \
     --tz "$TIMEZONE" --exact --no-deliver --timeout-seconds 120 --disabled \
     --command "merrick --openclaw '$OCF' tick --to $OWNER_TELEGRAM_ID"
  MERRICK_READY="$MERRICK_READY" python3 - <<'PY'
import json, os
p = "/etc/potter/handover.json"
os.makedirs("/etc/potter", exist_ok=True)
reg = json.load(open(p)) if os.path.exists(p) else {"crons": [], "apps": [], "secrets_needed": []}
have = {c.get("name") for c in reg.setdefault("crons", [])}
add = [{"name": "potter-merrick-tick", "enabled": False,
        "purpose": "runs Merrick's scheduled thermostat changes; off until the old assistant stops controlling the house"}]
if not os.environ.get("MERRICK_READY"):
    add += [{"name": n, "enabled": False, "purpose": "needs Merrick's house file first"}
            for n in ("potter-merrick-cold-watch", "potter-merrick-collect")]
reg["crons"] += [a for a in add if a["name"] not in have]
json.dump(reg, open(p, "w"), indent=1)
PY
fi

say "Scheduling the knowledge graph, monitors, quality control and finance jobs"
mkdir -p /etc/potter
python3 "$HERE/add_jobs.py" --jobs "$HERE/jobs.json" --openclaw "openclaw ${OC_PROFILE_FLAG[*]:-}" \
   --set POTTER_HOME="$POTTER_HOME" --set WS="$WS" --set TZ="$TIMEZONE" --set TO="$OWNER_TELEGRAM_ID" \
   --set KG_DB="$KG_DB" --set SESSIONS="$STATE_DIR/agents/potter/sessions" \
   --set OC="openclaw ${OC_PROFILE_FLAG[*]:-}" --set JOB_MODEL_ARGS="${JOB_MODEL_ARGS[*]:-}" --handover /etc/potter/handover.json >/dev/null \
   || warn "some jobs could not be created; run verify_host.sh and ask Potter to fix them"
if [ -n "$MONITORS_READY" ]; then
  # Fetchers only read public pages and message no one: with sources in place they start now. A first quiet run
  # records what is already listed, so the first real run reports only what is new.
  # Each monitor keeps its own folder (seen list, findings, cursor); the skills read them by these names.
  for pair in city_events:city-events local_monitor:local-monitor wine_price:wine-watch feed_scan:financial-feeds; do
    m="${pair%%:*}"; d="${pair#*:}"
    python3 "$POTTER_HOME/tools/monitors/$m.py" --config "$WS/state/monitors/config.json" \
      --state-dir "$WS/state/monitors/$d" --baseline >/dev/null 2>&1 || warn "monitor $m could not take its first reading"
  done
  potter-cron on potter-fetch-city-events potter-fetch-local potter-fetch-wine potter-fetch-feeds >/dev/null || warn "could not switch on every page-checking job"
  python3 - <<'PY'
import json, os
p = "/etc/potter/handover.json"
if os.path.exists(p):
    reg = json.load(open(p))
    reg["crons"] = [c for c in reg.get("crons", []) if not c.get("name", "").startswith("potter-fetch-")]
    json.dump(reg, open(p, "w"), indent=1)
PY
fi

# OpenClaw creates its own "Proactive check" job at onboarding, before any working key exists, and it then fails on
# every run ("no recorded inference account"). Potter's schedule watchdog and evening check-in already cover it.
potter-cron off "Proactive check (potter)" >/dev/null 2>&1 || true

say "Adding the schedule watchdog (tells you when any job fails or does not run)"
mkdir -p "$POTTER_HOME/tools"; cp "$REPO/overlay/tools/"*.py "$POTTER_HOME/tools/"
oc cron add --name potter-schedule-watch --declaration-key potter-schedule-watch --every 15m --no-deliver \
   --timeout-seconds 120 \
   --command "python3 $POTTER_HOME/tools/schedule_watch.py --to $OWNER_TELEGRAM_ID --openclaw 'openclaw ${OC_PROFILE_FLAG[*]:-}'" >/dev/null

[ -n "${POTTER_V2_BUNDLE_COPY:-}" ] && { shred -u "$POTTER_V2_BUNDLE_COPY" 2>/dev/null || rm -f "$POTTER_V2_BUNDLE_COPY"; }

say "Done. Next: run verify_host.sh, then message your bot on Telegram."
