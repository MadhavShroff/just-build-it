#!/usr/bin/env bash
# install.sh: install just-build-it for one user, as one systemd user service.
#
#   ./install.sh [--dry-run]
#
# It links the CLI entry point into ~/.local/bin, creates the state and run folders,
# renders the systemd user unit, and enables just-build-it.service so a run resumes
# after a restart. `./uninstall.sh` removes each item.
# Overrides for a test: HOME, SYSTEMCTL.
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    *) echo "install: unknown option $1" >&2; exit 2 ;;
  esac
done
run() { echo "  $*"; [ "$DRY" = 1 ] || "$@"; }
render() {  # render <template> <target>
  echo "  render $2"; [ "$DRY" = 1 ] && return 0
  mkdir -p "$(dirname "$2")"; sed -e "s|@ROOT@|$ROOT|g" -e "s|@HOME@|$HOME|g" "$1" > "$2.tmp.$$" && mv -f "$2.tmp.$$" "$2"
}

echo "runtime check: required programs and Python packages"
for executable in python3 git tmux gh ci-conductor bwrap; do
  command -v "$executable" >/dev/null 2>&1 || {
    printf 'install: required executable is missing: %s\n' "$executable" >&2
    exit 1
  }
done
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -c 'import jbi, claude_mux_lite' || {
  echo "install: local jbi and claude-mux-lite modules must import" >&2
  exit 1
}

JBI_HOME="${JBI_HOME:-$HOME/.just-build-it}"
echo "  state folder: $JBI_HOME"

echo "just-build-it: CLI link, state folders and systemd user unit"
run mkdir -p "$HOME/.local/bin" "$JBI_HOME/runs" "$HOME/.local/state/just-build-it"
run ln -sfn "$ROOT/just-build-it" "$HOME/.local/bin/just-build-it"
for unit in just-build-it.service; do
  render "$ROOT/systemd/$unit" "$HOME/.config/systemd/user/$unit"
done
run "$SYSTEMCTL" --user daemon-reload
run "$SYSTEMCTL" --user enable just-build-it.service
echo "install: done. Check: systemctl --user status just-build-it.service"
