#!/usr/bin/env bash
# uninstall.sh: remove what install.sh made. It keeps the state folders unless --purge.
#
#   ./uninstall.sh [--dry-run] [--purge]
#
# Overrides for a test: HOME, SYSTEMCTL.
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
DRY=0; PURGE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --purge) PURGE=1 ;;
    *) echo "uninstall: unknown option $arg" >&2; exit 2 ;;
  esac
done
run() { echo "  $*"; [ "$DRY" = 1 ] || "$@" || echo "  (that step failed; continuing)" >&2; }

echo "just-build-it units"
for unit in just-build-it.service; do
  [ -e "$HOME/.config/systemd/user/$unit" ] && run "$SYSTEMCTL" --user disable --now "$unit"
done
for unit in just-build-it.service; do
  [ -e "$HOME/.config/systemd/user/$unit" ] && run rm -f "$HOME/.config/systemd/user/$unit"
done
run "$SYSTEMCTL" --user daemon-reload
echo "the CLI link"
run rm -f "$HOME/.local/bin/just-build-it"
if [ "$PURGE" = 1 ]; then
  echo "purge: runs and state"
  run rm -rf "$HOME/.just-build-it" "$HOME/.local/state/just-build-it"
else
  echo "kept: ~/.just-build-it ~/.local/state/just-build-it"
fi
echo "uninstall: done"
