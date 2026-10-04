#!/bin/sh
# Claude Code SessionEnd hook: when a session closes while RunPod pods exist, send the lead
# one phone ping naming them. It only reports; each pod's own guard does the deleting.
root=$(cd "$(dirname "$0")/../.." && pwd)
last="$HOME/.cache/verbatus/pods-reported"
limit=${SESSION_END_LIST_SECONDS:-30}

# `runpodctl get pod`, stopped after $limit seconds so a hung CLI cannot leave this running.
list_pods() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "$limit" runpodctl get pod 2>/dev/null
    return
  fi
  out=$(mktemp) || return 1
  runpodctl get pod >"$out" 2>/dev/null &
  lister=$!
  # Its output goes nowhere: holding the caller's pipe, its sleep would make every listing
  # wait the full limit.
  (sleep "$limit" && kill "$lister" 2>/dev/null) >/dev/null 2>&1 &
  killer=$!
  wait "$lister"
  status=$?
  kill "$killer" 2>/dev/null
  cat "$out"
  rm -f "$out"
  return "$status"
}
(
  # Nothing here is ever read as "no pods" unless the listing says so: a missing CLI, a
  # failed or timed-out listing, an empty one and a table it does not recognise are each
  # reported. Only a STATUS header with no rows under it means no pods. Every pod is
  # named with its state, whatever the state: a stopped pod still bills its disk.
  if ! command -v runpodctl >/dev/null 2>&1; then
    pods="runpodctl-missing"
    message="runpodctl is not installed, so RunPod pods were not checked after a Claude session closed; check the RunPod console."
  elif ! listing=$(list_pods); then
    pods="unlisted"
    message="Could not list RunPod pods after a Claude session closed; check the RunPod console."
  elif [ -z "$listing" ]; then
    pods="unlisted"
    message="The RunPod pod listing came back empty after a Claude session closed; check the RunPod console."
  elif [ "$(printf '%s\n' "$listing" | awk 'NR == 1 { print $NF }')" != STATUS ]; then
    pods="unlisted"
    message="Could not read the RunPod pod listing after a Claude session closed (unrecognised table); check the RunPod console."
  else
    pods=$(printf '%s\n' "$listing" | awk 'NR > 1 && NF { print $1 ":" $NF }' | sort | tr '\n' ' ')
    [ -n "$pods" ] || exit 0
    message="RunPod pods still exist after a Claude session closed: $pods"
  fi
  # The same report, by pod id and state, sent in the last two hours is not sent again.
  if [ "$(cat "$last" 2>/dev/null)" = "$pods" ] && [ -n "$(find "$last" -mmin -120 2>/dev/null)" ]; then
    exit 0
  fi
  # Recorded only once delivered, so a failed ping is tried again at the next session end.
  sh "$root/operations/notify/notify.sh" decision "$message" >/dev/null 2>&1 &&
    mkdir -p "$(dirname "$last")" && printf '%s' "$pods" >"$last"
) </dev/null >/dev/null 2>&1 &
exit 0
