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
ln -sf "$POTTER_HOME/openclaw/bin/openclaw" /usr/local/bin/openclaw
openclaw --version

say "Saving secrets (root-only)"
umask 077; mkdir -p "$SECRETS_DIR"
printf '%s' "$TELEGRAM_TOKEN" > "$SECRETS_DIR/telegram.token"
chmod 700 "$SECRETS_DIR"; chmod 600 "$SECRETS_DIR/telegram.token"

GATEWAY_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')"
printf '%s' "$GATEWAY_PASSWORD" > "$SECRETS_DIR/gateway.password"; chmod 600 "$SECRETS_DIR/gateway.password"

say "Onboarding OpenClaw (non-interactive)"
oc onboard --non-interactive --accept-risk --flow manual --mode local \
   --auth-choice openrouter-api-key --openrouter-api-key "$OPENROUTER_KEY" \
   --gateway-bind loopback --gateway-auth password --gateway-password "$GATEWAY_PASSWORD" --agent-name Potter \
   --no-install-daemon >/dev/null

say "Applying the Potter overlay"
export OWNER_NAME TIMEZONE OWNER_TELEGRAM_ID SECRETS_DIR
patch="$(mktemp --suffix=.json5)"; render_overlay "$REPO/overlay/config.base.json5" "$patch"
oc config patch --file "$patch" --dry-run >/dev/null
oc config patch --file "$patch" >/dev/null; rm -f "$patch"
WS="$(oc config get agents.entries.potter.workspace | tr -d '"')"
[ -d "$WS" ] || die "agent workspace not found"
for f in IDENTITY.md SOUL.md USER.md; do
  render_overlay "$REPO/overlay/workspace/$f" "$WS/$f"
done
rm -f "$WS/BOOTSTRAP.md"
cp "$REPO/overlay/workspace/avatar.svg" "$WS/avatar.svg"
mkdir -p "$WS/skills"; cp -r "$REPO/overlay/workspace/skills/." "$WS/skills/"

if [ -n "${OPENAI_VOICE_KEY:-}" ]; then
  say "Turning on voice talk"
  vp="$(mktemp --suffix=.json5)"
  OPENAI_VOICE_KEY="$OPENAI_VOICE_KEY" python3 -c 'import json,os,sys; json.dump({"talk":{"realtime":{"provider":"openai","providers":{"openai":{"apiKey":os.environ["OPENAI_VOICE_KEY"]}}}}}, open(sys.argv[1],"w"))' "$vp"
  oc config patch --file "$vp" >/dev/null; rm -f "$vp"
fi

if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
  say "Joining your tailnet and enabling Potter's funnel"
  command -v tailscale >/dev/null 2>&1 || { curl -fsSL https://tailscale.com/install.sh | sh >/dev/null; }
  tailscaled --state=/var/lib/tailscale/tailscaled.state >/dev/null 2>&1 &
  sleep 3
  tailscale up --authkey "$TAILSCALE_AUTHKEY" --hostname "${POTTER_TS_HOSTNAME:-potter-v3}" --accept-dns=false >/dev/null
  oc config set gateway.tailscale.mode funnel >/dev/null
fi

if [ -n "${COMPOSIO_MCP_URL:-}" ] || [ -n "${AGENTMAIL_API_KEY:-}" ] || [ -n "${HA_URL:-}" ]; then
  say "Connecting MCP servers"
  mcp="$(mktemp --suffix=.json5)"
  COMPOSIO_MCP_URL="${COMPOSIO_MCP_URL:-}" AGENTMAIL_API_KEY="${AGENTMAIL_API_KEY:-}" HA_URL="${HA_URL:-}" HA_TOKEN="${HA_TOKEN:-}" python3 - "$mcp" <<'PY'
import json, os, sys
servers = {}
if os.environ.get("COMPOSIO_MCP_URL"):
    servers["composio"] = {"transport": "streamable-http", "url": os.environ["COMPOSIO_MCP_URL"]}
if os.environ.get("AGENTMAIL_API_KEY"):
    servers["agentmail"] = {"command": "npx", "args": ["-y", "agentmail-mcp"],
                            "env": {"AGENTMAIL_API_KEY": os.environ["AGENTMAIL_API_KEY"]}}
if os.environ.get("HA_URL"):
    # Home Assistant's built-in MCP server (state and control).
    servers["homeassistant"] = {"transport": "streamable-http",
                                "url": os.environ["HA_URL"].rstrip("/") + "/mcp_server/sse",
                                "headers": {"Authorization": "Bearer " + os.environ["HA_TOKEN"]}}
json.dump({"mcp": {"servers": servers}}, open(sys.argv[1], "w"))
PY
  oc config patch --file "$mcp" --dry-run >/dev/null && oc config patch --file "$mcp" >/dev/null
  rm -f "$mcp"
fi

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
fi
for i in $(seq 1 60); do oc health >/dev/null 2>&1 && break; sleep 2; [ "$i" = 60 ] && die "gateway did not become healthy"; done

if [ -n "${POTTER_V2_BUNDLE:-}" ] && [ -f "$POTTER_V2_BUNDLE" ]; then
  say "Importing your Potter v2 export (everything starts in standby until you say take over)"
  mkdir -p "$POTTER_HOME/apps" /etc/potter
  python3 "$HERE/import_v2.py" apply "$POTTER_V2_BUNDLE" --workspace "$WS" --apps-dir "$POTTER_HOME/apps" \
      --state-dir /etc/potter --openclaw "openclaw ${OC_PROFILE_FLAG[*]:-}" \
      --systemd-dir /etc/systemd/system --run-installs > /etc/potter/import-report.json
  if [ -f /etc/potter/mcp-import.json ]; then
    oc config patch --file /etc/potter/mcp-import.json >/dev/null || warn "some imported connections need attention"
  fi
  command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload || true
  # apps the bundle marked safe to run alongside v2 start now; the rest wait for "take over"
  python3 - <<'PY'
import json, subprocess
reg = json.load(open("/etc/potter/handover.json"))
for a in reg["apps"]:
    if a["enabled"] and a.get("service"):
        subprocess.run(["systemctl", "enable", "--now", f"potter-app-{a['name']}.service"], capture_output=True)
PY
  shred -u "$POTTER_V2_BUNDLE" 2>/dev/null || rm -f "$POTTER_V2_BUNDLE"
fi

say "Scheduling the Morning Brief"
HH="${BRIEF_TIME%:*}"; MM="${BRIEF_TIME#*:}"
oc cron add --name potter-morning-brief --declaration-key potter-morning-brief \
   --cron "$((10#$MM)) $((10#$HH)) * * 1-5" --tz "$TIMEZONE" --exact --agent potter \
   --channel telegram --to "$OWNER_TELEGRAM_ID" --announce --best-effort-deliver \
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
say "Done. Next: run verify_host.sh, then message your bot on Telegram."
