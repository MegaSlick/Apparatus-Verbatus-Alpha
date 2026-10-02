#!/bin/sh
# Claude Code SessionEnd hook: when a session closes while RunPod pods exist, send the lead
# one phone ping naming them. It only reports; each pod's own guard does the deleting.
root=$(cd "$(dirname "$0")/../.." && pwd)
last="$HOME/.cache/verbatus/pods-reported"
(
  # A listing that fails is reported, never read as "no pods". A stopped (EXITED) pod
  # still bills its disk, so it is reported beside a running one.
  if listing=$(runpodctl get pod 2>/dev/null); then
    pods=$(printf '%s\n' "$listing" |
      awk '$NF == "RUNNING" || $NF == "EXITED" { print $1 }' | sort | tr '\n' ' ')
    [ -n "$pods" ] || exit 0
    message="RunPod pods still exist after a Claude session closed: $pods"
  else
    pods="unlisted"
    message="Could not list RunPod pods after a Claude session closed; check the RunPod console."
  fi
  # The same report sent in the last two hours is not sent again.
  if [ "$(cat "$last" 2>/dev/null)" = "$pods" ] && [ -n "$(find "$last" -mmin -120 2>/dev/null)" ]; then
    exit 0
  fi
  # Recorded only once delivered, so a failed ping is tried again at the next session end.
  sh "$root/operations/notify/notify.sh" decision "$message" >/dev/null 2>&1 &&
    mkdir -p "$(dirname "$last")" && printf '%s' "$pods" >"$last"
) </dev/null >/dev/null 2>&1 &
exit 0
