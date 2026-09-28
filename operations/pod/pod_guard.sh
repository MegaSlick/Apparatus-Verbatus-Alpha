#!/bin/sh
# Runs on a RunPod pod and deletes that same pod when its approved time runs out or when
# it has sat idle, so a pod left behind by a crashed session or a closed laptop stops
# billing on its own. The network volume survives the delete.
#
#   pod_guard.sh <max_hours> [idle_minutes]
#
# Works with RUNPOD_POD_ID and the pod-scoped RUNPOD_API_KEY that RunPod sets in every
# pod, and keeps its deadline, keep-alive file and log in $POD_GUARD_DIR (default
# /workspace/.pod_guard, on the network volume). To extend the deadline, write a new
# epoch second into deadline-<pod id>; touching keepalive counts as activity.
set -u

max_hours=${1:?usage: pod_guard.sh <max_hours> [idle_minutes]}
idle_minutes=${2:-30}
pod=${RUNPOD_POD_ID:?RUNPOD_POD_ID is not set}
dir=${POD_GUARD_DIR:-/workspace/.pod_guard}
interval=${POD_GUARD_INTERVAL:-60}
idle_limit=${POD_GUARD_IDLE_SECONDS:-$((idle_minutes * 60))}
busy=${POD_GUARD_BUSY:-'python|vllm|(^|/)uv |pip |huggingface|(^|/)hf |(^|/)git |rsync|curl|wget|(^|/)tar |pytest|apt'}

mkdir -p "$dir"
log="$dir/guard.log"
deadline_file="$dir/deadline-$pod"
say() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$log"; }

if [ ! -s "$deadline_file" ]; then
  awk -v h="$max_hours" -v now="$(date +%s)" 'BEGIN { printf "%d\n", now + h * 3600 }' >"$deadline_file"
fi
say "armed for pod $pod: deadline $(cat "$deadline_file"), idle limit ${idle_limit}s"

notify() {
  [ -s "$dir/ntfy_topic" ] || return 0
  config=$(mktemp) || return 0
  printf 'url = "https://ntfy.sh/%s"\n' "$(tr -d ' \n' <"$dir/ntfy_topic")" >"$config"
  curl -fsS -K "$config" -H "Title: Pod guard" -d "$1" >/dev/null 2>&1
  rm -f "$config"
}

delete_pod() {
  runpodctl pod delete "$pod" >>"$log" 2>&1 && return 0
  runpodctl remove pod "$pod" >>"$log" 2>&1 && return 0
  [ -n "${RUNPOD_API_KEY:-}" ] || return 1
  config=$(mktemp) || return 1
  printf 'header = "Authorization: Bearer %s"\n' "$RUNPOD_API_KEY" >"$config"
  curl -fsS -K "$config" -X DELETE "https://api.runpod.io/v2/pods/$pod" >>"$log" 2>&1
  status=$?
  rm -f "$config"
  return "$status"
}

terminate() {
  say "deleting pod $pod: $1"
  notify "Pod $pod deleted by its guard: $1"
  attempt=1
  while [ "$attempt" -le 5 ]; do
    if delete_pod; then
      say "delete requested for pod $pod"
      exit 0
    fi
    say "delete attempt $attempt failed"
    attempt=$((attempt + 1))
    sleep "$interval"
  done
  notify "Pod $pod guard could NOT delete it: check RunPod now"
  exit 1
}

is_busy() {
  if command -v nvidia-smi >/dev/null 2>&1 &&
    [ -n "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null)" ]; then
    return 0
  fi
  pgrep -f -- "$busy" >/dev/null && return 0
  [ -n "$(find "$dir" -maxdepth 1 -name keepalive -mmin "-$(((idle_limit + 59) / 60))" 2>/dev/null)" ]
}

idle_for=0
while :; do
  if [ "$(date +%s)" -ge "$(cat "$deadline_file")" ]; then
    terminate "approved time is up"
  fi
  if is_busy; then
    idle_for=0
  else
    idle_for=$((idle_for + interval))
    if [ "$idle_for" -ge "$idle_limit" ]; then
      terminate "idle for ${idle_for}s"
    fi
  fi
  sleep "$interval"
done
