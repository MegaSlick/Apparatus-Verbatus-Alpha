#!/bin/sh
# Runs on a RunPod pod and deletes that same pod when its approved time runs out or when
# it has done no work (no GPU, CPU or network use), so a pod left behind by a crashed
# session or a closed laptop stops billing on its own. The network volume survives.
#
#   pod_guard.sh <max_hours> [idle_minutes]
#
# Uses RUNPOD_POD_ID and the pod-scoped RUNPOD_API_KEY that RunPod sets in every pod, and
# keeps its deadline, keep-alive file and log in $POD_GUARD_DIR (default
# /workspace/private/.pod_guard, on the network volume at the pod's mount path). To extend
# the deadline, write the new epoch second to a temporary file and move it over
# deadline-<pod id>. Touching keepalive-<pod id> counts as work at that moment: the idle
# limit then runs from the touch. The guard touches heartbeat-<pod id> on every tick, so
# a reader can tell a live guard from a deadline file nobody watches; a released-<pod id>
# file (pod_run --no-hold writes the run and its outcome there) is quoted in the delete
# notice, so a finished run's notice differs from one whose time ran out mid-run.
set -u

max_hours=${1:?usage: pod_guard.sh <max_hours> [idle_minutes]}
idle_minutes=${2:-30}
pod=${RUNPOD_POD_ID:?RUNPOD_POD_ID is not set}
dir=${POD_GUARD_DIR:-/workspace/private/.pod_guard}
interval=${POD_GUARD_INTERVAL:-60}
idle_limit=${POD_GUARD_IDLE_SECONDS:-$((idle_minutes * 60))}
busy_percent=${POD_GUARD_BUSY_PERCENT:-5}
busy_cpu_percent=${POD_GUARD_BUSY_CPU_PERCENT:-50}
busy_net_kbps=${POD_GUARD_BUSY_NET_KBPS:-256}
cgroup=${POD_GUARD_CGROUP:-/sys/fs/cgroup}
netdev=${POD_GUARD_NETDEV:-/proc/net/dev}
log="$dir/guard.log"
deadline_file="$dir/deadline-$pod"

# Without a writable log nothing below can be trusted, so the guard exits and the start
# command's own backstop does the deleting instead.
{ mkdir -p "$dir" && touch "$log"; } 2>/dev/null || exit 3
# A release notice belongs to the run that wrote it: a restarted pod keeps its id, and an
# old notice would make this guard's delete read as that earlier run's ending.
rm -f "$dir/released-$pod"

if command -v timeout >/dev/null 2>&1; then limit="timeout 60"; else limit=""; fi
# A lost volume must not cost the delete: without a writable log, output goes nowhere.
limited() {
  out=$log
  [ -w "$log" ] || out=/dev/null
  # shellcheck disable=SC2086
  $limit "$@" >>"$out" 2>&1
}
say() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$log" 2>/dev/null; }
is_epoch() { case $1 in '' | *[!0-9]*) return 1 ;; esac; }
# A deadline more than a week out is a typo (a millisecond value, an extra digit), not an approval.
sane_deadline() { is_epoch "$1" && [ "$1" -le $(($(date +%s) + 7 * 86400)) ]; }

deadline=$(cat "$deadline_file" 2>/dev/null)
if ! sane_deadline "$deadline"; then
  deadline=$(awk -v h="$max_hours" -v now="$(date +%s)" 'BEGIN { printf "%d", now + h * 3600 }')
  printf '%s\n' "$deadline" >"$deadline_file"
fi

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
  # The response echoes the topic, a bearer secret, so it never reaches the log.
  limited curl -fsS --max-time 30 -K "$config" -H "Title: Pod guard" -d "$1" -o /dev/null
  rm -f "$config"
}

# The delete ends this container, so the loop only ever ends that way: a request that
# reports success while the pod lives on is simply repeated, and stopping is the fallback.
shut_down() {
  reason=$1
  released="$dir/released-$pod"
  if [ -r "$released" ]; then
    ended=$(tr -cd 'A-Za-z0-9 ._-' <"$released" | cut -c 1-160)
    [ -z "$ended" ] || reason="$reason; pod_run reported: $ended"
  fi
  say "deleting pod $pod: $reason"
  attempt=0
  stopped=""
  while :; do
    attempt=$((attempt + 1))
    if delete_pod; then
      say "delete requested (attempt $attempt)"
      [ "$attempt" -eq 1 ] && notify "Pod $pod: its guard requested deletion ($reason)."
    else
      say "delete attempt $attempt failed"
      [ "$attempt" -eq 1 ] && notify "Pod $pod: its guard could not delete it ($reason) and keeps trying."
    fi
    if [ "$attempt" -ge 3 ] && [ -z "$stopped" ] && stop_pod; then
      stopped=yes
      say "stop requested (attempt $attempt)"
      notify "Pod $pod: delete did not take, so its guard stopped it. Check RunPod."
    fi
    sleep "$interval"
  done
}

# A GPU that cannot report its use (MIG, some virtual GPUs, a failed or hung query) counts
# as busy; only a pod with no nvidia-smi at all has no GPU to watch.
gpu_busy() {
  command -v nvidia-smi >/dev/null 2>&1 || return 1
  # shellcheck disable=SC2086
  reading=$($limit nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null) || return 0
  [ -n "$reading" ] || return 0
  printf '%s\n' "$reading" |
    awk -v floor="$busy_percent" '$1 !~ /^[0-9]+$/ || $1 + 0 >= floor { busy = 1 } END { exit !busy }'
}

# CPU seconds this container has used, from its own cgroup: the host's load average on a
# shared RunPod machine counts other tenants' work.
cpu_usec() {
  if [ -r "$cgroup/cpu.stat" ]; then
    awk '$1 == "usage_usec" { print $2 }' "$cgroup/cpu.stat"
  elif [ -r "$cgroup/cpuacct/cpuacct.usage" ]; then
    awk '{ printf "%.0f", $1 / 1000 }' "$cgroup/cpuacct/cpuacct.usage"
  fi
}

# Bytes this container has received on every interface but loopback: a download bound by
# the network can use little CPU and no GPU. Each line is split at its first colon, because
# the kernel pads interface names only up to six characters and a longer name runs straight
# into the colon. %.0f, not %d: some awks clamp %d at 2^31, which a byte counter passes.
net_bytes() {
  [ -r "$netdev" ] && awk 'NR > 2 {
    sub(/^[ \t]+/, "")
    colon = index($0, ":")
    if (colon == 0 || substr($0, 1, colon - 1) == "lo") next
    split(substr($0, colon + 1), field, " ")
    total += field[1]
  } END { printf "%.0f", total }' "$netdev"
}

cpu_before=$(cpu_usec)
net_before=$(net_bytes)
say "armed for pod $pod: deadline $deadline, idle limit ${idle_limit}s, cpu ${cpu_before:-unreadable} usec, net ${net_before:-unreadable} bytes"

# A reading that comes back unreadable (a file caught mid-write) keeps the last good one.
net_busy() {
  now=$(net_bytes)
  is_epoch "$now" || return 1
  before=$net_before
  net_before=$now
  is_epoch "$before" || return 1
  [ $((now - before)) -ge $((interval * busy_net_kbps * 1024)) ]
}

cpu_busy() {
  now=$(cpu_usec)
  is_epoch "$now" || return 1
  before=$cpu_before
  cpu_before=$now
  is_epoch "$before" || return 1
  [ $((now - before)) -ge $((interval * 10000 * busy_cpu_percent)) ]
}

# Seconds since the keep-alive file was last touched; fails when there is none.
keepalive_age() {
  touched=$(stat -c %Y "$dir/keepalive-$pod" 2>/dev/null || stat -f %m "$dir/keepalive-$pod" 2>/dev/null)
  is_epoch "$touched" || return 1
  age=$(($(date +%s) - touched))
  [ "$age" -ge 0 ] || age=0
  printf '%s' "$age"
}

idle_for=0
while :; do
  touch "$dir/heartbeat-$pod" 2>/dev/null
  latest=$(cat "$deadline_file" 2>/dev/null)
  if [ "$latest" != "$deadline" ] && [ "$latest" != "${ignored-}" ]; then
    if sane_deadline "$latest"; then
      deadline=$latest
      say "deadline now $deadline"
    else
      ignored=$latest
      say "ignored deadline file value '$latest'"
    fi
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then shut_down "approved time is up"; fi
  cpu_busy
  cpu=$?
  net_busy
  net=$?
  if [ "$cpu" -eq 0 ] || [ "$net" -eq 0 ] || gpu_busy; then
    idle_for=0
  else
    idle_for=$((idle_for + interval))
    # Idle time counts from the later of the last busy sample and the last keep-alive touch.
    if age=$(keepalive_age) && [ "$age" -lt "$idle_for" ]; then idle_for=$age; fi
    if [ "$idle_for" -ge "$idle_limit" ]; then shut_down "no GPU, CPU or network work for ${idle_for}s"; fi
  fi
  sleep "$interval"
done
