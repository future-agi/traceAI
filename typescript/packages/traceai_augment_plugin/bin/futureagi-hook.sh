#!/bin/sh
# No output: SessionStart stdout is context; Stop stdout can block completion.
# Node is pinned during setup, never found via PATH or downloaded by a hook.
case "$0" in */*) HERE=${0%/*} ;; *) exit 0 ;; esac
(
  IFS= read -r NODE < "$HERE/node-path" || exit 0
  [ -x "$NODE" ] || exit 0
  "$NODE" "$HERE/../runtime/hook.cjs" "$HERE/registration.json" || :
) >/dev/null 2>&1
exit 0
