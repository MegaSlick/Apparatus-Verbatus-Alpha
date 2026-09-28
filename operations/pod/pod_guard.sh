#!/bin/sh
# Runs on a RunPod pod and deletes that same pod when its approved time runs out or when
# its GPU has sat idle, so a pod left behind by a crashed session or a closed laptop stops
# billing on its own. The network volume survives the delete.
#
#   pod_guard.sh <max_hours> [idle_minutes]
#
# Uses RUNPOD_POD_ID and the pod-scoped RUNPOD_API_KEY that RunPod sets in every pod, and
# keeps its deadline, keep-alive file and log in $POD_GUARD_DIR (default
# /workspace/.pod_guard, on the network volume). To extend the deadline, write the new
# epoch second to a temporary file and move it over deadline-<pod id>. Touching
# keepalive-<pod id> counts as work while the GPU is idle (downloads, CPU-only stages).
set -u

max_hours=${1:?usage: pod_guard.sh <max_hours> [idle_minutes]}
idle_minutes=${2:-30}
pod=${RUNPOD_POD_ID:?RUNPOD_POD_ID is not set}
dir=${POD_GUARD_DIR:-/workspace/.pod_guard}
interval=${POD_GUARD_INTERVAL:-60}
idle_limit=${POD_GUARD_IDLE_SECONDS:-$((idle_minutes * 60))}
busy_percent=${POD_GUARD_BUSY_PERCENT:-5}
log="$dir/guard.log"
deadline_file="$dir/deadline-$pod"

# Without a writable log nothing below can be trusted, so the guard exits and the start
# command's own backstop does the deleting instead.
{ mkdir -p "$dir" && touch "$log"; } 2>/dev/null || exit 3

if command -v timeout >/dev/null 2>&1; then limit="timeout 60"; else limit=""; fi
limited() {
  # shellcheck disable=SC2086
  $limit "$@" >>"$log" 2>&1
}
say() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$log" 2>/dev/null; }
is_epoch() { case $1 in '' | *[!0-9]*) return 1 ;; esac; }

deadline=$(cat "$deadline_file" 2>/dev/null)
if ! is_epoch "$deadline"; then
  deadline=$(awk -v h="$max_hours" -v now="$(date +%s)" 'BEGIN { printf "%d", now + h * 3600 }')
  printf '%s\n' "$deadline" >"$deadline_file"
fi
say "armed for pod $pod: deadline $deadline, idle limit ${idle_limit}s"

delete_pod() {
  limited runpodctl pod delete "$pod" && return 0
  limited runpodctl remove pod "$pod" && return 0
  [ -n "${RUNPOD_API_KEY:-}" ] || return 1
  config=$(mktemp) || return 1
  printf 'header = "Authorization: Bearer %s"\n' "$RUNPOD_API_KEY" >"$config"
  limited curl -fsS --max-time 60 -K "$config" -X DELETE "https://api.runpod.io/v2/pods/$pod"
  status=$?
  rm -f "$config"
  return "$status"
}

stop_pod() {
  limited runpodctl pod stop "$pod" || limited runpodctl stop pod "$pod"
}

notify() {
  topic=$(tr -d ' \r\n' <"$dir/ntfy_topic" 2>/dev/null)
  case $topic in '' | *[!A-Za-z0-9_-]*) return 0 ;; esac
  config=$(mktemp) || return 0
  printf 'url = "https://ntfy.sh/%s"\n' "$topic" >"$config"
  limited curl -fsS --max-time 30 -K "$config" -H "Title: Pod guard" -d "$1"
  rm -f "$config"
}

# The delete ends this container, so the loop only ever ends that way: a request that
# reports success while the pod lives on is simply repeated, and stopping is the fallback.
shut_down() {
  say "deleting pod $pod: $1"
  attempt=0
  while :; do
    attempt=$((attempt + 1))
    if delete_pod; then say "delete requested (attempt $attempt)"; else say "delete attempt $attempt failed"; fi
    [ "$attempt" -eq 1 ] && notify "Pod $pod is being deleted by its guard: $1"
    if [ "$attempt" -ge 3 ] && stop_pod; then say "stop requested (attempt $attempt)"; fi
    sleep "$interval"
  done
}

gpu_busy() {
  command -v nvidia-smi >/dev/null 2>&1 || return 1
  # shellcheck disable=SC2086
  $limit nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null |
    awk -v floor="$busy_percent" '$1 + 0 >= floor { busy = 1 } END { exit !busy }'
}

kept_alive() {
  [ -n "$(find "$dir" -maxdepth 1 -name "keepalive-$pod" -mmin "-$(((idle_limit + 59) / 60))" 2>/dev/null)" ]
}

idle_for=0
while :; do
  latest=$(cat "$deadline_file" 2>/dev/null)
  if is_epoch "$latest"; then deadline=$latest; fi
  if [ "$(date +%s)" -ge "$deadline" ]; then shut_down "approved time is up"; fi
  if gpu_busy || kept_alive; then
    idle_for=0
  else
    idle_for=$((idle_for + interval))
    if [ "$idle_for" -ge "$idle_limit" ]; then shut_down "GPU idle for ${idle_for}s"; fi
  fi
  sleep "$interval"
done
