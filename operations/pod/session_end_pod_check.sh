#!/bin/sh
# Claude Code SessionEnd hook: when a session closes while RunPod pods are running, send the
# lead one phone ping naming them. It only reports; each pod's own guard does the deleting.
root=$(cd "$(dirname "$0")/../.." && pwd)
last="$HOME/.cache/verbatus/pods-reported"
(
  pods=$(runpodctl get pod 2>/dev/null | awk '$NF == "RUNNING" { print $1 }' | sort | tr '\n' ' ')
  [ -n "$pods" ] || exit 0
  # The same pods already reported in the last two hours are not reported again.
  if [ "$(cat "$last" 2>/dev/null)" = "$pods" ] && [ -n "$(find "$last" -mmin -120 2>/dev/null)" ]; then
    exit 0
  fi
  mkdir -p "$(dirname "$last")" && printf '%s' "$pods" >"$last"
  sh "$root/operations/notify/notify.sh" decision \
    "RunPod pods still running after a Claude session closed: $pods" >/dev/null 2>&1
) </dev/null >/dev/null 2>&1 &
exit 0
