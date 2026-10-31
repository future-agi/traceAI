#!/bin/sh
# Optional shared-laptop guard. Normal installations do not need these files.
set -eu
heavy=$1
shift
status_script="$HOME/.futureagi-local/bin/host-status.sh"
check_host() {
  if test ! -x "$status_script"; then return; fi
  while :; do
    status=$("$status_script")
    echo "$status"
    free=$(printf '%s' "$status" | sed -n 's/.*free=\([0-9]*\)%.*/\1/p')
    load=$(printf '%s' "$status" | sed -n 's/.*load=\([0-9.]*\).*/\1/p')
    if awk -v f="$free" -v l="$load" 'BEGIN { if (f == "" || l == "" || f < 20 || l > 30) exit 1 }'; then return; fi
    echo 'Waiting 60 seconds for host budget.'
    sleep 60
  done
}
check_host
lock=/Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.lock
if test "$heavy" = heavy && test -d "$(dirname "$lock")"; then
  until mkdir "$lock" 2>/dev/null; do
    find "$lock" -maxdepth 0 -mmin +25 -exec rmdir {} \; 2>/dev/null || :
    sleep 10
  done
  trap 'rmdir "$lock" 2>/dev/null || :' EXIT HUP INT TERM
  check_host
fi
"$@"
