#!/bin/bash
# potter-update: Potter updates itself straight from GitHub, read-only.
#
#   potter-update --setup <owner/repo> [branch]   one time: make a read-only deploy key, print its public half
#   potter-update --check                         fetch and list what is new; change nothing
#   potter-update [--profile <zip>]               fetch, fast-forward /opt/potter/src, run install/update.sh from it
#
# The key is a GitHub deploy key: one repository, read-only, root-only on this machine. It never leaves it.
# Only fast-forwards are taken (a rewritten branch stops the update). The previous code is kept once for rollback.
set -euo pipefail
# This file is replaced by the pull; run from a private copy so bash never reads a half-new script.
if [ -z "${POTTER_SELF_COPY:-}" ]; then
  t="$(mktemp)"; cp "$0" "$t"; chmod 700 "$t"; POTTER_SELF_COPY="$t" exec bash "$t" "$@"
fi
trap 'rm -f "$POTTER_SELF_COPY"' EXIT

SRC="${POTTER_SRC:-/opt/potter/src}"
SECRETS_DIR="${POTTER_SECRETS_DIR:-/etc/potter/secrets}"
CONF="${POTTER_GITHUB_CONF:-/etc/potter/github}"
KEY="$SECRETS_DIR/github_deploy"
KH="$SECRETS_DIR/github_known_hosts"
say() { echo "$*"; }
die() { echo "potter-update: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"

# GitHub's published SSH host key (docs.github.com, "GitHub's SSH key fingerprints"); also served on port 443.
GH_ED25519="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

need_tools() {
  command -v git >/dev/null && command -v ssh >/dev/null && return 0
  command -v apt-get >/dev/null || die "git and ssh are needed"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git openssh-client >/dev/null || die "could not install git and ssh"
}

if [ "${1:-}" = "--setup" ]; then
  slug="${2:-}"; branch="${3:-v3-core}"
  [[ "$slug" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "usage: potter-update --setup <owner/repo> [branch]"
  need_tools
  mkdir -p "$SECRETS_DIR" /etc/potter; chmod 700 "$SECRETS_DIR"
  [ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "potter-v3 read-only deploy key" -f "$KEY"
  chmod 600 "$KEY"
  printf 'github.com %s\n[ssh.github.com]:443 %s\n' "$GH_ED25519" "$GH_ED25519" > "$KH"; chmod 600 "$KH"
  printf 'REPO=%s\nBRANCH=%s\n' "$slug" "$branch" > "$CONF"; chmod 600 "$CONF"
  say "Add this as a read-only deploy key on GitHub (repository $slug > Settings > Deploy keys > Add deploy key;"
  say "title: potter-v3; leave \"Allow write access\" OFF):"
  say ""
  cat "$KEY.pub"
  say ""
  say "Then say \"update yourself\"."
  exit 0
fi

[ -f "$CONF" ] && [ -f "$KEY" ] || die "not set up: run potter-update --setup <owner/repo> first"
# shellcheck disable=SC1090
. "$CONF"
need_tools
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$KH -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=15"
URLS=("git@github.com:$REPO.git" "ssh://git@ssh.github.com:443/$REPO.git")     # port 22, then 443 if 22 is blocked
[ -n "${POTTER_GIT_URL:-}" ] && URLS=("$POTTER_GIT_URL")                         # tests: a local repository

fetch() {   # $1 = a git directory; leaves FETCH_HEAD
  local u
  for u in "${URLS[@]}"; do
    git -C "$1" remote set-url origin "$u" 2>/dev/null || git -C "$1" remote add origin "$u"
    if git -C "$1" fetch -q --depth 200 origin "$BRANCH" 2>/tmp/potter-fetch.err; then return 0; fi
  done
  if grep -qi "permission denied\|publickey" /tmp/potter-fetch.err; then
    die "GitHub refused the key: add it as a deploy key (potter-update --setup prints it again)"
  fi
  die "could not reach GitHub: $(tail -n1 /tmp/potter-fetch.err)"
}

old="$(cat "$SRC/.potter-rev" 2>/dev/null || git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo none)"
if [ ! -d "$SRC/.git" ]; then
  # First pull: the code here was a copy without history. Fetch into a fresh checkout and swap it in.
  new_dir="$SRC.new"; rm -rf "$new_dir"; mkdir -p "$new_dir"; git -C "$new_dir" init -q
  fetch "$new_dir"
  git -C "$new_dir" checkout -q -B "$BRANCH" FETCH_HEAD
  if [ "${1:-}" = "--check" ]; then
    say "Current: $old (a copy without history). Available: $(git -C "$new_dir" log -1 --format='%h %s')"
    rm -rf "$new_dir"; exit 0
  fi
  rm -rf "$SRC.prev"; [ -d "$SRC" ] && mv "$SRC" "$SRC.prev"; mv "$new_dir" "$SRC"
else
  fetch "$SRC"
  head="$(git -C "$SRC" rev-parse HEAD)"; next="$(git -C "$SRC" rev-parse FETCH_HEAD)"
  if [ "$head" = "$next" ]; then say "Already up to date: $(git -C "$SRC" log -1 --format='%h %s')"; exit 0; fi
  git -C "$SRC" merge-base --is-ancestor "$head" "$next" || die "the branch was rewritten on GitHub; not taking it (ask the owner)"
  say "New since $(git -C "$SRC" rev-parse --short "$head"):"
  git -C "$SRC" log --format='  %h %s' "$head..$next" | head -n 20
  [ "${1:-}" = "--check" ] && exit 0
  [ -z "$(git -C "$SRC" status --porcelain --untracked-files=no)" ] || die "files in $SRC were edited by hand; not overwriting them"
  rm -rf "$SRC.prev"; cp -a "$SRC" "$SRC.prev"
  git -C "$SRC" merge -q --ff-only FETCH_HEAD
fi
git -C "$SRC" log -1 --format=%h > "$SRC/.potter-rev"
say "Code: $old -> $(cat "$SRC/.potter-rev"). Previous code kept at $SRC.prev."
exec bash "$SRC/install/update.sh" "$@"
