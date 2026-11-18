#!/usr/bin/env bash
# Update a running Potter in place from the code in /opt/potter/src (or this checkout): no reinstall, no secrets
# re-entered. Installs or refreshes potter-core (v3 control + brain), Merrick's Jev decider, memory search and the
# workspace's shipped skills and rules.
#
# It never runs inside Potter's own turn: it hands itself to a separate system service, which waits a few seconds
# (so Potter's "update started" reply is delivered), stops the gateway (plugin installs and memory indexing need it
# stopped), does the work, starts the gateway, checks it, and reports to the owner on Telegram itself.
#   update.sh                      start the update in the background (what Potter runs)
#   update.sh --profile ZIP        also re-import the owner's profile from a v2 export (the zip is deleted afterwards)
#   update.sh --check              say what would change, change nothing
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
# One way in: when GitHub updates are set up, a direct run fetches first (through potter-update), so a stale
# local copy is never installed by mistake. potter-update sets POTTER_PULLED; the detached run sets POTTER_UPDATE_RUN.
if [ -z "${POTTER_PULLED:-}" ] && [ -z "${POTTER_UPDATE_RUN:-}" ] && [ -f "${POTTER_GITHUB_CONF:-/etc/potter/github}" ] \
   && [ -f "$HERE/self_update.sh" ] && [ "${1:-}" != "--check" ]; then
  echo "GitHub updates are set up: fetching the latest code first (potter-update)."
  exec bash "$HERE/self_update.sh" "$@"
fi
. "$HERE/lib.sh"
export PATH="$POTTER_HOME/openclaw/bin:$POTTER_HOME/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"
CHECK=""; PROFILE_ZIP=""
while [ $# -gt 0 ]; do case "$1" in --check) CHECK=1;; --profile) PROFILE_ZIP="${2:-}"; shift;; esac; shift; done
[ "$(id -u)" = 0 ] || die "run as root"
SYSTEMD=""; command -v systemctl >/dev/null 2>&1 && [ -z "${POTTER_NO_DAEMON:-}" ] && SYSTEMD=1
if [ -z "$CHECK" ] && [ -n "$SYSTEMD" ] && [ -z "${POTTER_UPDATE_RUN:-}" ]; then
  unit="potter-update-$(date +%s)"
  systemd-run --unit="$unit" --collect --quiet --setenv=POTTER_UPDATE_RUN=1 --setenv=POTTER_HOME="$POTTER_HOME" \
    --setenv=POTTER_SECRETS_DIR="$SECRETS_DIR" bash "$HERE/update.sh" ${PROFILE_ZIP:+--profile "$PROFILE_ZIP"}
  echo "Update started in the background ($unit). In about 10 seconds the gateway stops for a minute or two; a"
  echo "Telegram message reports the result when it is back. Progress: journalctl -u $unit"
  exit 0
fi
notify() {   # straight to the owner's Telegram, no model involved; the token goes to curl on stdin, never argv
  [ -n "$SYSTEMD" ] || return 0
  local tok body; tok="$(cat "$SECRETS_DIR/telegram.token" 2>/dev/null)"; [ -n "$tok" ] || return 0
  body="$(mktemp)"; chmod 600 "$body"
  python3 -c 'import json,sys; json.dump({"chat_id": sys.argv[1], "text": sys.argv[2][:3900]}, open(sys.argv[3], "w"))' "$id" "$1" "$body"
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\nheader = "Content-Type: application/json"\ndata-binary = "@%s"\nsilent\nmax-time = 20\n' \
    "$tok" "$body" | curl --config - -o /dev/null || true
  rm -f "$body"
}
C="$(dirname "$(oc config file)")"; WS="$(oc config get agents.entries.potter.workspace | tr -d '"')"
id="$(oc config get channels.telegram.allowFrom | python3 -c 'import json,sys; print(json.load(sys.stdin)[0])')"
rev="$(git -C "$REPO" log -1 --format=%h 2>/dev/null || cat "$REPO/.potter-rev" 2>/dev/null || echo unknown)"
echo "Updating Potter from code $rev"
[ -n "$CHECK" ] && { echo "check only: would refresh potter-core, Merrick's Jev decider, memory search, shipped skills${PROFILE_ZIP:+, your profile}; restart the gateway"; exit 0; }
LOG=()
step() { echo "$1"; LOG+=("$1"); }
if [ -n "$SYSTEMD" ]; then
  trap 'systemctl start potter-gateway; notify "Potter update FAILED at: $(tail -n1 <<<"${LOG[*]:-start}"). The gateway was restarted on the old setup. Details: journalctl -u potter-update-*"' ERR
  sleep 8                                                     # let Potter's "update started" reply go out
  systemctl stop potter-gateway
fi

# 1. potter-core plugin
d="$(mktemp -d)"; cp -r "$REPO/overlay/plugins/potter-core/"{index.js,lib,openclaw.plugin.json,package.json} "$d/"
install_plugin_if_changed "$d" potter-core --force; rm -rf "$d"
p="$(mktemp --suffix=.json5)"
POTTER_TZ="$(oc config get agents.defaults.userTimezone 2>/dev/null | tr -d '"' || echo UTC)" python3 - "$id" "$C" "$WS" "$SECRETS_DIR" "$p" <<'PY'
import json, os, sys
owner, state, ws, secrets, out = sys.argv[1:6]
cfg = {"workspace": ws, "timezone": os.environ.get("POTTER_TZ", "UTC"), "brain": {"enabled": True, "budget": 7000},
       "kg": {"enabled": True, "dbPath": os.path.join(state, "potter-kg", "potter-kg.sqlite"), "budget": 2500,
              "capture": True, "queuePath": os.path.join(state, "potter-core", "kg-capture-queue.jsonl"),
              "usagePath": os.path.join(state, "potter-core", "kg-usage.jsonl")}, "mode": os.environ.get("POTTER_CONTROL_MODE", "enforce"), "agents": ["potter"], "ownerIds": [owner],
       "standingGrants": [], "stateDir": os.path.join(state, "potter-core"),
       "jevEnvFile": os.path.join(secrets, "jev.env"), "policyPath": os.path.join(ws, "state", "model-policy.json"),
       "governor": {"enabled": True, "jev": True, "timeoutMs": 2500}, "fastPath": {"enabled": True, "merrick": "merrick"},
       "broker": {"enabled": True, "mode": "enforce", "escalationDailyCapUsd": 3, "escalationDailyMax": 20}}
json.dump({"plugins": {"entries": {"potter-core": {"enabled": True, "hooks": {"allowConversationAccess": True}}}}}, open(out, "w"))
json.dump(cfg, open(out + ".cfg", "w"))
PY
q oc config patch --file "$p"
# keep standing grants the owner already gave ("take over X")
python3 - "$p.cfg" "$(oc config get plugins.entries.potter-core.config 2>/dev/null || echo '{}')" <<'PY'
import json, sys
new = json.load(open(sys.argv[1]))
try:
    old = json.loads(sys.argv[2])
except ValueError:
    old = {}
new["standingGrants"] = old.get("standingGrants") or []
new["mode"] = old.get("mode", new["mode"])
if isinstance(old.get("brain"), dict):
    new["brain"] = {**new["brain"], **old["brain"]}
if isinstance(old.get("kg"), dict):
    new["kg"] = {**new["kg"], **old["kg"]}
if isinstance(old.get("broker"), dict):
    new["broker"] = {**new["broker"], **old["broker"]}      # the owner's broker settings win
json.dump({"plugins": {"entries": {"potter-core": {"config": new}}}}, open(sys.argv[1], "w"))
PY
q oc config patch --file "$p.cfg" --replace-path plugins.entries.potter-core.config; rm -f "$p" "$p.cfg"
a="$(mktemp --suffix=.json5)"
oc plugins list --json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
ids = sorted({p["id"] for p in d["plugins"] if p.get("origin") == "bundled" or p.get("enabled")})
json.dump({"plugins": {"allow": ids}}, open(sys.argv[1], "w"))' "$a"
q oc config patch --file "$a" --replace-path plugins.allow; rm -f "$a"
step "potter-core: installed and configured"

# 1a. potter-kg plugin (the graph file and its config are kept; only the code is replaced)
rm -rf "$POTTER_HOME/plugins/potter-kg"; mkdir -p "$POTTER_HOME/plugins"
cp -r "$REPO/overlay/plugins/potter-kg" "$POTTER_HOME/plugins/potter-kg"; rm -rf "$POTTER_HOME/plugins/potter-kg/tests"
kgcfg="$(oc config get plugins.entries.potter-kg.config 2>/dev/null || echo '{}')"
install_plugin_if_changed "$POTTER_HOME/plugins/potter-kg" potter-kg --force
kp="$(mktemp --suffix=.json5)"
python3 - "$kgcfg" "$C/potter-kg/potter-kg.sqlite" "$kp" <<'PY'
import json, sys
try:
    old = json.loads(sys.argv[1])
except ValueError:
    old = {}
cfg = {"dbPath": sys.argv[2], **(old if isinstance(old, dict) else {})}
json.dump({"plugins": {"entries": {"potter-kg": {"enabled": True, "config": cfg}}}}, open(sys.argv[3], "w"))
PY
q oc config patch --file "$kp"; rm -f "$kp"
step "potter-kg: installed ($(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$REPO/overlay/plugins/potter-kg/openclaw.plugin.json"))"

# 1b. model policy: replaced only if it is still a shipped one (an edited policy is the owner's and is kept)
OLD_SHIPPED="$(sed -n 's/^OLD_SHIPPED_POLICIES="\(.*\)"$/\1/p' "$HERE/bootstrap.sh")"
P="$WS/state/model-policy.json"
if [ -f "$P" ] && ! cmp -s "$P" "$REPO/overlay/jev/policy.json"; then
  h="$(sha256sum "$P" | cut -c1-64)"
  if { [ -f "$WS/state/model-policy.shipped.json" ] && cmp -s "$P" "$WS/state/model-policy.shipped.json"; } || \
     case " $OLD_SHIPPED " in *" $h "*) true;; *) false;; esac; then
    cp "$REPO/overlay/jev/policy.json" "$P"; cp "$REPO/overlay/jev/policy.json" "$WS/state/model-policy.shipped.json"
    m="$(mktemp --suffix=.json5)"
    python3 "$HERE/apply_models.py" "$P" --patch "$m" --agents potter$(oc agents list 2>/dev/null | grep -q merrick && echo ,merrick) \
      && q oc config patch --file "$m" --replace-path agents.defaults.models
    rm -f "$m"; step "model policy: updated to the shipped version"
  else
    step "model policy: yours was edited, kept as it is (the new default is overlay/jev/policy.json)"
  fi
fi

# 1c. zero data retention: name any model an account that requires it would refuse
zdr="$(python3 "$HERE/zdr_check.py" "$WS/state/model-policy.json" --embedding openai/text-embedding-3-small 2>/dev/null || true)"
[ -n "$zdr" ] && step "WARNING, zero data retention: $zdr" || step "zero data retention: every model Potter uses has a ZDR provider"

# 2. Jev for Merrick
mkdir -p "$POTTER_HOME/jev"; cp "$REPO/overlay/jev/"{client.js,policy.js,telemetry.js,index.js,merrick-decide.mjs,house-ideas.mjs} "$POTTER_HOME/jev/"
if [ -f /usr/local/bin/merrick ] && ! grep -q MERRICK_DECIDE_CMD /usr/local/bin/merrick; then
  sed -i "/^exec /i export MERRICK_DECIDE_CMD=\"node $POTTER_HOME/jev/merrick-decide.mjs\"\nexport POTTER_JEV_ENV=$SECRETS_DIR/jev.env\nexport POTTER_MODEL_POLICY=$WS/state/model-policy.json" /usr/local/bin/merrick
fi
[ -d "$POTTER_HOME/merrick" ] && { rm -rf "$POTTER_HOME/merrick"; cp -r "$REPO/overlay/merrick" "$POTTER_HOME/merrick"; rm -f "$POTTER_HOME/merrick/testing.py"; }
mkdir -p "$POTTER_HOME/tools"; cp "$REPO/overlay/tools/"*.py "$POTTER_HOME/tools/"
step "merrick: Jev decider wired"
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
exec node "$REPO/overlay/plugins/potter-kg/scripts/kg-import.mjs" --into "$C/potter-kg/potter-kg.sqlite" "\$@"
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
exec node "$POTTER_HOME/plugins/potter-kg/scripts/kg-capture.mjs" --queue "$C/potter-core/kg-capture-queue.jsonl" --db "$C/potter-kg/potter-kg.sqlite" \
  --staged "$WS/state/kg-staged.jsonl" --decider "$POTTER_HOME/jev/index.js" --policy "$WS/state/model-policy.json" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-kg-capture
cat > /usr/local/bin/potter-kg-freshness <<WRAP
#!/bin/sh
set -a; . "$SECRETS_DIR/jev.env"; set +a
exec node "$POTTER_HOME/plugins/potter-kg/scripts/kg-freshness.mjs" --db "$C/potter-kg/potter-kg.sqlite" --review "$WS/state/kg-review.jsonl" \
  --usage "$C/potter-core/kg-usage.jsonl" --decider "$POTTER_HOME/jev/index.js" --policy "$WS/state/model-policy.json" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-kg-freshness
mkdir -p "$WS/state/forecasts"; rm -rf "$POTTER_HOME/cos"; cp -r "$REPO/overlay/cos" "$POTTER_HOME/cos"; rm -f "$POTTER_HOME/cos/"*.test.mjs
cat > /usr/local/bin/potter-cos-score <<WRAP
#!/bin/sh
set -a; . "$SECRETS_DIR/jev.env"; set +a
exec node "$POTTER_HOME/cos/score.mjs" --forecasts "$WS/state/forecasts" --db "$C/potter-kg/potter-kg.sqlite" \
  --decider "$POTTER_HOME/jev/index.js" --policy "$WS/state/model-policy.json" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-cos-score
# AgentMail key for the inbox watcher: from the gateway's own config, once (root-only file; never printed).
if [ ! -f "$SECRETS_DIR/agentmail.env" ]; then
  python3 - "$(oc config file)" "$SECRETS_DIR/agentmail.env" <<'PY' && step "mail watch: AgentMail key found" || step "mail watch: no AgentMail key in the config (watcher stays off)"
import json, os, re, sys
try:
    cfg = json.loads(re.sub(r"(?m)^\s*//.*$", "", open(sys.argv[1]).read()))
except Exception:
    sys.exit(1)
def val(v):
    if isinstance(v, dict) and v.get("source") == "file":
        path = v.get("path") or (((cfg.get("secrets") or {}).get("providers") or {}).get(v.get("provider")) or {}).get("path")
        try:
            return open(path).read().strip() if path else ""
        except OSError:
            return ""
    return v if isinstance(v, str) else ""
ch = (cfg.get("channels") or {}).get("agentmail") or {}
key = val(ch.get("apiKey")) or val((((cfg.get("plugins") or {}).get("entries") or {}).get("agentmail") or {}).get("config", {}).get("apiKey"))
inbox = ch.get("inboxId") or ""
if not key or not inbox:
    sys.exit(1)
os.umask(0o077)
open(sys.argv[2], "w").write(f"AGENTMAIL_API_KEY={key}\nAGENTMAIL_INBOX={inbox}\n")
PY
fi
cat > /usr/local/bin/potter-mail-watch <<WRAP
#!/bin/sh
[ -f "$SECRETS_DIR/agentmail.env" ] || { echo "potter-mail-watch: AgentMail is not configured" >&2; exit 3; }
set -a; . "$SECRETS_DIR/jev.env"; . "$SECRETS_DIR/agentmail.env"; set +a
exec node "$POTTER_HOME/cos/mail-watch.mjs" --doc "$WS/state/compiling-doc.md" --state "$WS/state/mail-watch.json" --travel-inbox "$WS/state/travel/travel-inbox.jsonl" \
  --decider "$POTTER_HOME/jev/index.js" --policy "$WS/state/model-policy.json" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-mail-watch
mkdir -p "$WS/state/travel"; chmod 700 "$WS/state/travel"
cat > /usr/local/bin/potter-travel <<WRAP
#!/bin/sh
# Trips from booking emails; reminders and (with an AeroAPI key in aeroapi.env) live flight status.
set -a; . "$SECRETS_DIR/jev.env"; [ -f "$SECRETS_DIR/aeroapi.env" ] && . "$SECRETS_DIR/aeroapi.env"; set +a
cmd="\$1"; [ \$# -gt 0 ] && shift
exec node "$POTTER_HOME/cos/travel.mjs" "\$cmd" --dir "$WS/state/travel" --policy "$WS/state/model-policy.json" \
  --to "$id" --tz "$(oc config get agents.defaults.userTimezone 2>/dev/null | tr -d '"' || echo America/New_York)" --openclaw "openclaw ${OC_PROFILE_FLAG[*]:-}" "\$@"
WRAP
chmod 755 /usr/local/bin/potter-travel
# Potter's vault: the plugin (tools say "not set up" until the owner connects a vault) and its setup command.
rm -rf "$POTTER_HOME/plugins/potter-vault"; mkdir -p "$POTTER_HOME/plugins"
cp -r "$REPO/overlay/plugins/potter-vault" "$POTTER_HOME/plugins/potter-vault"; rm -rf "$POTTER_HOME/plugins/potter-vault/tests"
install_plugin_if_changed "$POTTER_HOME/plugins/potter-vault" potter-vault --force
vp="$(mktemp --suffix=.json5)"
OWNER="$id" SECRETS="$SECRETS_DIR" VSTATE="$C/potter-vault" python3 -c 'import json,os,sys; json.dump({"plugins":{"entries":{"potter-vault":{"enabled":True,"hooks":{"allowConversationAccess":True},"config":{"envFile":os.path.join(os.environ["SECRETS"],"vault.env"),"ownerTarget":os.environ["OWNER"],"stateDir":os.environ["VSTATE"]}}}}}, open(sys.argv[1],"w"))' "$vp"
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
# The browser (v3.12): headless Chromium for web tasks; clicks that commit the owner need approval (potter-core).
if ! command -v chromium >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chromium >/dev/null 2>&1 || true
fi
if command -v chromium >/dev/null 2>&1; then
  bp="$(mktemp --suffix=.json)"
  python3 -c 'import json,sys; json.dump({"plugins":{"entries":{"browser":{"enabled":True}}},"browser":{"enabled":True,"headless":True,"noSandbox":True,"executablePath":"/usr/bin/chromium"}}, open(sys.argv[1],"w"))' "$bp"
  q oc config patch --file "$bp"; rm -f "$bp"
  BROWSER_OK=1
fi
step "browser: $([ -n "${BROWSER_OK:-}" ] && echo "headless Chromium ready" || echo "Chromium could not be installed")"
step "vault: potter-vault installed ($([ -f "$SECRETS_DIR/vault.env" ] && echo connected || echo "not connected yet: potter-vault-setup"))"
step "kg tools: potter-kg-eval and potter-kg-import ready"

# 2b. memory search, with the key already saved for Jev (no key is re-entered)
. "$HERE/memory_search.sh"
LOG+=("$(potter_memory_search "$(sed -n 's/^OPENROUTER_API_KEY=//p' "$SECRETS_DIR/jev.env" 2>/dev/null)" | tail -n1)")

# Jobs added since this install. Needs the gateway running; existing jobs, including ones the owner took over,
# are not touched.
add_new_jobs() {
TO="$id" python3 "$HERE/add_jobs.py" --jobs "$HERE/jobs.json" --openclaw "openclaw ${OC_PROFILE_FLAG[*]:-}" --only-missing \
  --set POTTER_HOME="$POTTER_HOME" --set WS="$WS" --set TZ="$(oc config get agents.defaults.userTimezone 2>/dev/null | tr -d '"' || echo UTC)" \
  --set TO="$id" --set KG_DB="$C/potter-kg/potter-kg.sqlite" --set SESSIONS="$C/agents/potter/sessions" \
  --set OC="openclaw ${OC_PROFILE_FLAG[*]:-}" --set JOB_MODEL_ARGS="$(python3 "$HERE/apply_models.py" "$WS/state/model-policy.json" --job-args 2>/dev/null)" \
  --handover /etc/potter/handover.json >/dev/null && step "jobs: any new ones added" || step "jobs: could not add new ones (see add_jobs output)"
}

# 3. shipped skills and global rules (personal rules and the index's personal rows are not touched)
for s in "$REPO/overlay/workspace/skills/"*/; do n="$(basename "$s")"; mkdir -p "$WS/skills/$n"; cp -r "$s". "$WS/skills/$n/"; done
cp "$REPO/overlay/workspace/rules/global/"*.md "$WS/rules/global/"
step "skills and global rules: refreshed"
# USER.md still the empty template, but v2's profile is in the workspace as reference (an install from before the
# importer made it live): promote it, without the reference banner. An owner-edited USER.md is never replaced.
if [ -z "$PROFILE_ZIP" ] && [ -f "$WS/reference/v2/USER.md" ] && [ "$(grep -c . "$WS/USER.md" 2>/dev/null || echo 0)" -le 3 ]; then
  cp "$WS/USER.md" "$WS/USER.md.template-backup" 2>/dev/null || true
  sed '1{/^> Reference only/d}' "$WS/reference/v2/USER.md" | sed '1{/^$/d}' > "$WS/USER.md"
  step "profile: USER.md now holds your v2 profile ($(grep -c . "$WS/USER.md") lines)"
fi
if [ -n "$PROFILE_ZIP" ] && [ -f "$PROFILE_ZIP" ]; then
  python3 "$HERE/profile_from_v2.py" apply "$PROFILE_ZIP" --workspace "$WS" --state-dir /etc/potter > /etc/potter/profile-report.json \
    && step "profile: re-imported from your v2 export ($(python3 -c 'import json;print(len(json.load(open("/etc/potter/profile-report.json"))["active_files"]))') personal files, including USER.md)" \
    || step "profile: could not be re-imported (see /etc/potter/profile-report.json)"
  shred -u "$PROFILE_ZIP" 2>/dev/null || rm -f "$PROFILE_ZIP"
fi
echo "$rev" > "$POTTER_HOME/.potter-rev"

# without systemd (a test machine) the gateway was never stopped
[ -n "$SYSTEMD" ] || add_new_jobs

# 4. start the gateway again and check it
if [ -n "$SYSTEMD" ]; then
  systemctl start potter-gateway
  for _ in $(seq 1 60); do oc health >/dev/null 2>&1 && break; sleep 2; done
  trap - ERR
  add_new_jobs
  potter-cron off potter-mail-observe >/dev/null 2>&1 || true   # replaced by the potter-mail-watch command job
  hooks="$(oc plugins inspect potter-core --runtime --json 2>/dev/null | grep -o '"name": *"[a-z_]*"' | wc -l)"
  vh="$("$HERE/verify_host.sh" 2>/dev/null || true)"
  health="$(grep -c '^PASS' <<<"$vh" || true)/$(grep -c -E '^(PASS|FAIL)' <<<"$vh" || true)"
  fails="$(grep '^FAIL' <<<"$vh" | sed 's/^FAIL  /failed: /' || true)"
  notify "Potter updated to $rev.
$(printf '%s\n' "${LOG[@]}")
potter-core hooks: $hooks. Health checks passed: $health.${fails:+
$fails}"
  echo "done: health $health"
fi
