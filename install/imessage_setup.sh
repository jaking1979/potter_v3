#!/bin/bash
# potter-imessage-setup: connect Potter to iMessage through the owner's always-on Mac (Messages runs there under
# a dedicated macOS user signed in with Potter's own Apple ID; the Gateway reaches it over SSH).
#
#   potter-imessage-setup key                                  make the SSH key, print its public half
#   potter-imessage-setup connect <mac-host> <mac-user> <owner handles, comma separated>
#
# Messages Potter sends to anyone but the owner need the owner's approval (potter-core). Only the owner's handles
# can talk to Potter. See docs/IMESSAGE_SETUP.md.
set -euo pipefail
SECRETS_DIR="${POTTER_SECRETS_DIR:-/etc/potter/secrets}"
KEY="$SECRETS_DIR/imessage_ssh"; KH="$SECRETS_DIR/imessage_known_hosts"; WRAP=/etc/potter/imsg-ssh
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { echo "potter-imessage-setup: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"
read -r -a OC <<< "${OPENCLAW:-openclaw}"

case "${1:-}" in
  key)
    [ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "potter-v3 imessage" -f "$KEY"
    chmod 600 "$KEY"
    echo "Add this line to ~/.ssh/authorized_keys of the Mac user that runs Messages for Potter:"
    cat "$KEY.pub"
    ;;
  connect)
    host="${2:-}"; user="${3:-}"; handles="${4:-}"
    [ -n "$host" ] && [ -n "$user" ] && [ -n "$handles" ] || die "usage: connect <mac-host> <mac-user> <owner handles>"
    [ -f "$KEY" ] || die "no key yet: run 'potter-imessage-setup key' first and add it on the Mac"
    ssh-keyscan -T 10 -t ed25519 "$host" 2>/dev/null > "$KH" || true
    [ -s "$KH" ] || die "could not reach $host on port 22 (is Remote Login on, and is the address right?)"
    chmod 600 "$KH"
    mkdir -p /root/.ssh; chmod 700 /root/.ssh
    touch /root/.ssh/config; chmod 600 /root/.ssh/config
    if ! grep -q "^# potter-imessage$" /root/.ssh/config; then
      printf '\n# potter-imessage\nHost %s\n  User %s\n  IdentityFile %s\n  IdentitiesOnly yes\n  UserKnownHostsFile %s\n  StrictHostKeyChecking yes\n  BatchMode yes\n' \
        "$host" "$user" "$KEY" "$KH" >> /root/.ssh/config
    fi
    imsg="$(ssh -T "$user@$host" 'command -v imsg || ls /opt/homebrew/bin/imsg /usr/local/bin/imsg 2>/dev/null | head -1' 2>/dev/null | tail -1)"
    [ -n "$imsg" ] || die "SSH works only if imsg is installed for $user on the Mac (brew install steipete/tap/imsg)"
    printf '#!/bin/sh\nexec ssh -T %s@%s %s "$@"\n' "$user" "$host" "$imsg" > "$WRAP"; chmod 755 "$WRAP"
    "$WRAP" rpc --help >/dev/null 2>&1 || die "imsg did not answer over SSH (Full Disk Access and Automation on the Mac?)"
    "${OC[@]}" plugins inspect imessage >/dev/null 2>&1 || "${OC[@]}" plugins install @openclaw/imessage --accept-capabilities >/dev/null
    cfg="$(mktemp --suffix=.json)"
    python3 "$HERE/imessage_config.py" --mac-host "$host" --mac-user "$user" --owner-handles "$handles" --cli-path "$WRAP" --out "$cfg"
    "${OC[@]}" config patch --file "$cfg" >/dev/null; rm -f "$cfg"
    # Texting the owner's own handles is texting the owner (no approval); anyone else needs approval.
    python3 - "$handles" <<'PY'
import json, os, shlex, subprocess, sys
oc = shlex.split(os.environ.get("OPENCLAW", "openclaw"))
cur = subprocess.run(oc + ["config", "get", "plugins.entries.potter-core.config.ownerIds"], capture_output=True, text=True).stdout
try:
    ids = json.loads(cur)
except ValueError:
    ids = []
ids = list(dict.fromkeys([*ids, *[h.strip() for h in sys.argv[1].split(",") if h.strip()]]))
subprocess.run(oc + ["config", "set", "plugins.entries.potter-core.config.ownerIds", json.dumps(ids), "--strict-json"], check=True)
PY
    systemd-run --on-active=5 --collect --quiet systemctl restart potter-gateway 2>/dev/null || true
    echo "iMessage connected through $user@$host. The gateway restarts in a few seconds; then ask Potter to text you."
    ;;
  *) die "usage: potter-imessage-setup key | connect <mac-host> <mac-user> <owner handles>";;
esac
