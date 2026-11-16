#!/usr/bin/env bash
# Potter v3 installer.
#   On a Proxmox host:   ./install/install.sh --proxmox     (creates a fresh container, installs everything in it)
#   On a Debian/Ubuntu machine or container (as root):  ./install/install.sh
#   Add --dry-run to print what would happen without changing anything.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
. "$HERE/lib.sh"

MODE=local; DRY=""
for a in "$@"; do case "$a" in
  --proxmox) MODE=proxmox;; --dry-run) DRY=1;; -h|--help) sed -n 2,6p "${BASH_SOURCE[0]}"; exit 0;;
  *) die "unknown option $a";; esac; done
run() { if [ -n "$DRY" ]; then printf 'DRY: %s\n' "$*"; else "$@"; fi; }

cat <<'BANNER'

  Potter v3 setup
  This installs the latest OpenClaw, the Potter overlay and your assistant.
  Nothing outside the new container/machine is touched. Have ready:
    1. a Telegram bot token (from @BotFather) and your numeric Telegram user ID
    2. a model API key (OpenRouter) with a spend cap set
  Google, Notion, Home Assistant and other connections are added afterwards with: potter connect <name>

BANNER

if [ "$MODE" = proxmox ]; then
  need_cmd pct; need_cmd pveam
  ask CTID      "Container ID (unused number, e.g. 210)"        "210"
  ask CT_NAME   "Container hostname"                             "potter"
  ask CT_STORE  "Storage for the container disk"                 "local-lvm"
  ask CT_BRIDGE "Network bridge"                                 "vmbr0"
  ask CT_DISK   "Disk size in GB"                                "32"
  ask CT_MEM    "Memory in MB"                                   "4096"
  ask CT_CORES  "CPU cores"                                      "2"
  [[ "$CTID" =~ ^[0-9]+$ ]] || die "container ID must be a number"
  pct status "$CTID" >/dev/null 2>&1 && die "container $CTID already exists; choose another ID"
  say "Finding a Debian template"
  tmpl="$(pveam available --section system 2>/dev/null | awk '/debian-12-standard/ {print $2}' | sort | tail -1)"
  [ -n "$tmpl" ] || die "no Debian 12 template listed by pveam"
  run pveam download local "$tmpl" || true
  say "Creating container $CTID"
  run pct create "$CTID" "local:vztmpl/$tmpl" --hostname "$CT_NAME" --cores "$CT_CORES" --memory "$CT_MEM" \
      --rootfs "$CT_STORE:$CT_DISK" --net0 "name=eth0,bridge=$CT_BRIDGE,ip=dhcp" --unprivileged 1 \
      --features nesting=1 --onboot 1 --start 1
  run sleep 8
  run pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl ca-certificates python3 xz-utils whiptail git"
  collect_inputs
  envfile="$(mktemp)"; chmod 600 "$envfile"
  { for v in OWNER_NAME TIMEZONE OWNER_TELEGRAM_ID TELEGRAM_TOKEN OPENROUTER_KEY BRIEF_TIME; do
      printf 'export POTTER_%s=%q\n' "$v" "${!v}"; done; } > "$envfile"
  say "Copying the installer into the container"
  run tar -C "$REPO" --exclude=.git -czf /tmp/potter-src.tgz .
  run pct push "$CTID" /tmp/potter-src.tgz /root/potter-src.tgz
  run pct exec "$CTID" -- bash -c "mkdir -p /opt/potter/src && tar -xzf /root/potter-src.tgz -C /opt/potter/src"
  run pct push "$CTID" "$envfile" /root/potter-inputs.env --perms 600
  rm -f "$envfile"
  say "Installing inside the container"
  run pct exec "$CTID" -- bash -c "set -a; . /root/potter-inputs.env; set +a; cd /opt/potter/src && ./install/bootstrap.sh; rc=\$?; shred -u /root/potter-inputs.env 2>/dev/null || rm -f /root/potter-inputs.env; exit \$rc"
  run pct exec "$CTID" -- bash -c "cd /opt/potter/src && ./install/verify_host.sh"
  say "Container $CTID is ready."
else
  [ -n "$DRY" ] && { echo "DRY: would run install/bootstrap.sh then install/verify_host.sh"; exit 0; }
  "$HERE/bootstrap.sh"
  "$HERE/verify_host.sh"
fi
