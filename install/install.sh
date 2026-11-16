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
    3. optional: a Composio MCP URL, an AgentMail key, a Tailscale auth key
  You fill these in on a web page; the installer prints its address.
  Everything else (Google, Notion, Home Assistant, ...) is connected later by chatting with Potter.

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
  run bash -c "printf 'lxc.cgroup2.devices.allow: c 10:200 rwm\nlxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file\n' >> /etc/pve/lxc/$CTID.conf"
  run pct reboot "$CTID"
  run sleep 10
  run pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl ca-certificates python3 xz-utils whiptail git"
  say "Copying the installer into the container"
  run tar -C "$REPO" --exclude=.git -czf /tmp/potter-src.tgz .
  run pct push "$CTID" /tmp/potter-src.tgz /root/potter-src.tgz
  run pct exec "$CTID" -- bash -c "mkdir -p /opt/potter/src && tar -xzf /root/potter-src.tgz -C /opt/potter/src"
  ct_ip="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
  say "Opening the setup page. Visit the address it prints in your browser."
  run pct exec "$CTID" -- bash -c "cd /opt/potter/src && POTTER_FORM_URL_HOST='${ct_ip:-<container-ip>}' python3 install/setup_form.py"
  run pct exec "$CTID" -- bash -c "cd /opt/potter/src && ./install/verify_host.sh"
  say "Container $CTID is ready."
else
  [ -n "$DRY" ] && { echo "DRY: would run install/bootstrap.sh then install/verify_host.sh"; exit 0; }
  "$HERE/bootstrap.sh"
  "$HERE/verify_host.sh"
fi
