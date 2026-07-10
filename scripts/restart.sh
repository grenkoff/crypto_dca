#!/usr/bin/env bash
#
# Safely restart the crypto_dca daemons (trader / tgbot) without leaving orphans.
#
# Why this exists: the trader hangs on WebSocket shutdown, and its setsid-detached
# python child is re-parented to init (PPID 1) and keeps trading if only the `uv`
# wrapper is killed. `kill $(pgrep …)` also tends to signal the caller's own shell.
# Killing several of these over time left MULTIPLE traders racing the same account
# (duplicate grids, cancelled TPs). This script kills EVERY matching pid explicitly
# (wrapper + child + any stragglers), waits, escalates to SIGKILL, verifies none
# remain, then launches exactly one fresh detached instance and checks it is up.
#
# Usage:
#   scripts/restart.sh              # restart both trader and tgbot
#   scripts/restart.sh tgbot        # restart only tgbot
#   scripts/restart.sh trader tgbot # restart both, explicitly
#
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGDIR="${DCA_LOGDIR:-$REPO/logs}"
mkdir -p "$LOGDIR"
cd "$REPO"

# This script's own pid plus its whole ancestor chain — never signal the caller,
# no matter what text happens to be in its command line (a shell invoking the
# restart can legitimately contain " -m trader" in some other argument).
_excluded_pids() {
  local p=$$
  while [ "${p:-0}" -gt 1 ]; do
    echo "$p"
    p="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"
    [ -z "$p" ] && break
  done
}

# Pids running the module (`uv run python -m X` wrapper AND `python -m X` child),
# excluding this script and its ancestors.
svc_pids() { pgrep -f " -m $1" | grep -vxF -f <(_excluded_pids) || true; }

stop() {
  local svc="$1" pids
  pids="$(svc_pids "$svc")"
  if [ -z "$pids" ]; then echo "[$svc] not running"; return 0; fi
  echo "[$svc] stopping pids:" $pids
  # shellcheck disable=SC2086
  kill -TERM $pids 2>/dev/null || true
  for _ in $(seq 1 8); do
    sleep 1
    [ -z "$(svc_pids "$svc")" ] && break
  done
  pids="$(svc_pids "$svc")"
  if [ -n "$pids" ]; then
    echo "[$svc] graceful timeout, SIGKILL:" $pids
    # shellcheck disable=SC2086
    kill -KILL $pids 2>/dev/null || true
    sleep 2
  fi
  pids="$(svc_pids "$svc")"
  if [ -n "$pids" ]; then echo "[$svc] ERROR: still alive:" $pids; return 1; fi
  echo "[$svc] stopped"
}

start() {
  local svc="$1"
  setsid uv run python -m "$svc" > "$LOGDIR/$svc.log" 2>&1 < /dev/null &
  disown
  sleep 12
  local pids count
  pids="$(svc_pids "$svc")"
  count="$(printf '%s\n' "$pids" | grep -c .)"
  if [ "$count" -eq 0 ]; then
    echo "[$svc] ERROR: failed to start — last log lines:"
    tail -n 5 "$LOGDIR/$svc.log"
    return 1
  fi
  if [ "$count" -gt 2 ]; then
    echo "[$svc] WARNING: more than one instance ($count pids):" $pids
  fi
  echo "[$svc] started pids:" $pids
  tail -n 3 "$LOGDIR/$svc.log"
}

main() {
  local svcs=("$@")
  if [ ${#svcs[@]} -eq 0 ]; then svcs=(trader tgbot); fi
  local rc=0
  for s in "${svcs[@]}"; do
    case "$s" in
      trader|tgbot)
        echo "=== restart $s ==="
        stop "$s" && start "$s" || rc=1
        ;;
      *)
        echo "unknown service '$s' (use: trader | tgbot)"; rc=2
        ;;
    esac
  done
  return $rc
}

main "$@"
