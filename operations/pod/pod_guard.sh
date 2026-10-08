#!/bin/sh
# Runs on a RunPod pod and watches that same pod, so a pod left behind by a crashed
# session or a closed laptop does not bill unnoticed. It deletes the pod when a deadline
# passes, if there is one, and climbs an idle ladder while the pod does no work (no GPU,
# CPU or network use): a warning, then repeated urgent notices, then a verified backup of
# the run tree to the volume, and last, only with POD_GUARD_DELETE=on, the delete. Any
# work resets the ladder. The network volume is never deleted.
#
#   pod_guard.sh <max_hours|off> [idle_minutes]
#
# `off` arms no deadline; a valid deadline written to the file after the guard starts is
# still honoured. idle_minutes, when given, is when the first warning goes out.
#
# Uses RUNPOD_POD_ID and the pod-scoped RUNPOD_API_KEY that RunPod sets in every pod, and
# keeps its deadline, keep-alive file and log in $POD_GUARD_DIR (default
# /workspace/private/.pod_guard, on the network volume at the pod's mount path). To set or
# extend the deadline, write the new epoch second to a temporary file and move it over
# deadline-<pod id>. Touching keepalive-<pod id> counts as work at that moment: idle time
# then runs from the touch; pod_run touches it while the run keeps its pace, never for
# CPU time alone. pod_run also writes progress-<pod id>, one line
# "<epoch now> <epoch last ok> <ok|slow|stalled|bootstrapping> <check> <detail>"; while
# that line is fresh it decides instead of the counters: ok is work, anything else is idle
# time counted from the last ok, though only a stalled line, never a slow one, can reach the
# delete. backup-<pod id> names the paths the ladder backs up,
# one absolute path per line. alert-<pod id> holds the ladder's latest step. The guard
# touches heartbeat-<pod id> on every tick, so a reader can tell a live guard from a
# deadline file nobody watches; a released-<pod id> file (pod_run --no-hold writes the run
# and its outcome there) is quoted in the delete notice, so a finished run's notice
# differs from one whose time ran out mid-run.
set -u

max_hours=${1:?usage: pod_guard.sh <max_hours|off> [idle_minutes]}
case $max_hours in
  off) ;;
  *[!0-9.]* | '' | . | *.*.*) echo "pod_guard: <max_hours> must be a number or off" >&2; exit 2 ;;
esac
idle_minutes=${2:-15}
pod=${RUNPOD_POD_ID:?RUNPOD_POD_ID is not set}
dir=${POD_GUARD_DIR:-/workspace/private/.pod_guard}
interval=${POD_GUARD_INTERVAL:-60}
# The idle ladder, in seconds of no work.
warn_after=${POD_GUARD_WARN_SECONDS:-$((idle_minutes * 60))}
urgent_after=${POD_GUARD_URGENT_SECONDS:-1800}
urgent_repeat=${POD_GUARD_URGENT_REPEAT:-600}
backup_after=${POD_GUARD_BACKUP_SECONDS:-3600}
delete_after=${POD_GUARD_DELETE_SECONDS:-7200}
ladder_delete=${POD_GUARD_DELETE:-off}
busy_percent=${POD_GUARD_BUSY_PERCENT:-5}
busy_cpu_percent=${POD_GUARD_BUSY_CPU_PERCENT:-50}
busy_net_kbps=${POD_GUARD_BUSY_NET_KBPS:-256}
# A progress line older than this is a pod_run that stopped writing, not a verdict.
progress_fresh=${POD_GUARD_PROGRESS_FRESH_SECONDS:-300}
cgroup=${POD_GUARD_CGROUP:-/sys/fs/cgroup}
netdev=${POD_GUARD_NETDEV:-/proc/net/dev}
log="$dir/guard.log"
deadline_file="$dir/deadline-$pod"
alert_file="$dir/alert-$pod"
backup_list="$dir/backup-$pod"
progress_file="$dir/progress-$pod"

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

deadline=""
if [ "$max_hours" != off ]; then
  deadline=$(cat "$deadline_file" 2>/dev/null)
  if ! sane_deadline "$deadline"; then
    deadline=$(awk -v h="$max_hours" -v now="$(date +%s)" 'BEGIN { printf "%d", now + h * 3600 }')
    printf '%s\n' "$deadline" >"$deadline_file"
  fi
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

# notify <message> [urgent]
notify() {
  topic=$(tr -d ' \r\n' <"$dir/ntfy_topic" 2>/dev/null)
  case $topic in '' | *[!A-Za-z0-9_-]*) return 0 ;; esac
  config=$(mktemp) || return 0
  printf 'url = "https://ntfy.sh/%s"\n' "$topic" >"$config"
  # The response echoes the topic, a bearer secret, so it never reaches the log.
  limited curl -fsS --max-time 30 -K "$config" -H "Title: Pod guard" \
    -H "Priority: ${2:-default}" -d "$1" -o /dev/null
  rm -f "$config"
}

# Text read from a file on the volume, cut to something safe to quote in a notice.
quoted() { printf '%s' "$1" | tr -cd 'A-Za-z0-9 ._:-' | cut -c 1-40; }

when() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "epoch $1"; }

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
say "armed for pod $pod: deadline ${deadline:-none (off)}, idle ladder warn ${warn_after}s, urgent ${urgent_after}s every ${urgent_repeat}s, backup ${backup_after}s, delete ${delete_after}s (deletion $ladder_delete), cpu ${cpu_before:-unreadable} usec, net ${net_before:-unreadable} bytes"
if [ "$max_hours" = off ]; then
  # A deadline left by an earlier start of this pod is not this start's: only one written
  # after arming counts.
  ignored=$(cat "$deadline_file" 2>/dev/null)
  if [ -n "$ignored" ]; then
    say "deadline file value '$ignored' predates this guard; not honoured"
    notify "Pod $pod: its guard started with no deadline; the deadline file's earlier value '$(quoted "$ignored")' is not honoured. Write a new one to set a deadline."
  fi
fi

# A reading that comes back unreadable (a file caught mid-write) keeps the last good one.
net_busy() {
  now=$(net_bytes)
  is_epoch "$now" || return 1
  before=$net_before
  net_before=$now
  is_epoch "$before" || return 1
  [ $((now - before)) -ge $((interval * busy_net_kbps * 1024)) ]
}

# An unreadable CPU counter never causes a deletion. One unreadable tick after a good
# reading is no evidence either way (exit 2: idle time neither reset nor added), and the
# next good reading is compared over every tick since the last one. A counter unreadable
# since arming, or for a second tick running, counts as busy and is read again every
# tick. Each episode is logged; the phone hears of one, and of its recovery, at most
# once an hour, so a flapping counter cannot flood it. The deadline still ends the pod.
cpu_unread=0
cpu_span=1
cpu_held=""
cpu_noticed_at=""
cpu_pair_open=""
cpu_busy() {
  now=$(cpu_usec)
  if ! is_epoch "$now"; then
    cpu_unread=$((cpu_unread + 1))
    if [ "$cpu_unread" -eq 1 ] && is_epoch "$cpu_before"; then
      cpu_span=$((cpu_span + 1))
      return 2
    fi
    cpu_before=""
    if [ -z "$cpu_held" ]; then
      cpu_held=yes
      if [ -n "$deadline" ]; then
        held="CPU idle detection unavailable on $pod; held until its deadline $(when "$deadline")."
      else
        held="CPU idle detection unavailable on $pod; counted as busy, and no deadline is set."
      fi
      say "$held"
      clock=$(date +%s)
      if ! is_epoch "$cpu_noticed_at" || [ $((clock - cpu_noticed_at)) -ge 3600 ]; then
        cpu_noticed_at=$clock
        cpu_pair_open=yes
        notify "$held"
      fi
    fi
    return 0
  fi
  cpu_unread=0
  if [ -n "$cpu_held" ]; then
    cpu_held=""
    say "CPU idle detection restored on $pod; idle counting resumes"
    if [ -n "$cpu_pair_open" ]; then
      cpu_pair_open=""
      notify "CPU idle detection restored on $pod; idle counting resumes."
    fi
  fi
  span=$cpu_span
  cpu_span=1
  before=$cpu_before
  cpu_before=$now
  is_epoch "$before" || return 2
  [ $((now - before)) -ge $((span * interval * 10000 * busy_cpu_percent)) ]
}

# Reads pod_run's progress line. Succeeds only for a fresh, well-formed line, and sets
# progress (its status), progress_for (seconds since its last ok) and progress_what (its
# check and detail, cut to quote).
read_progress() {
  line=$(head -n 1 "$progress_file" 2>/dev/null) || return 1
  p_now=${line%% *}
  rest=${line#* }
  p_ok=${rest%% *}
  rest=${rest#* }
  progress=${rest%% *}
  progress_what=$(quoted "${rest#* }")
  is_epoch "$p_now" && is_epoch "$p_ok" || return 1
  case $progress in ok | slow | stalled | bootstrapping) ;; *) return 1 ;; esac
  clock=$(date +%s)
  [ $((clock - p_now)) -le "$progress_fresh" ] && [ $((p_now - clock)) -le "$progress_fresh" ] || return 1
  progress_for=$((clock - p_ok))
  [ "$progress_for" -ge 0 ] || progress_for=0
}

# Seconds since the keep-alive file was last touched; fails when there is none.
keepalive_age() {
  touched=$(stat -c %Y "$dir/keepalive-$pod" 2>/dev/null || stat -f %m "$dir/keepalive-$pod" 2>/dev/null)
  is_epoch "$touched" || return 1
  age=$(($(date +%s) - touched))
  [ "$age" -ge 0 ] || age=0
  printf '%s' "$age"
}

# The latest ladder step, one line "<epoch> <step> <detail>", for a reader on the volume.
alert() {
  printf '%s %s %s\n' "$(date +%s)" "$1" "$2" >"$alert_file.new" 2>/dev/null &&
    mv "$alert_file.new" "$alert_file" 2>/dev/null
}

# Copies each path named in backup-<pod id> beside the guard's directory, under
# runs-guard-backup/<name>-<epoch>, and compares the copy with its source. Sets backup to
# verified, nothing (no list) or failed (a copy failed, or a listed path is missing).
back_up() {
  backup=nothing
  if [ ! -s "$backup_list" ]; then
    say "idle for ${idle_for}s: nothing to back up (no $backup_list)"
    return
  fi
  root="$dir/../runs-guard-backup"
  stamp=$(date +%s)
  n=0
  while IFS= read -r source || [ -n "$source" ]; do
    case $source in /*) ;; *) continue ;; esac
    n=$((n + 1))
    # A listed path that is missing may be a run tree lost or misnamed, so it blocks the
    # delete rather than counting as nothing to back up.
    if [ ! -e "$source" ]; then
      say "backup: $source is not there, so the backup failed"
      backup=failed
      continue
    fi
    target="$root/$(basename "$source")-$stamp"
    [ ! -e "$target" ] || target="$target-$n"
    if mkdir -p "$root" 2>/dev/null && cp -a "$source" "$target" 2>>"$log" &&
      diff -rq "$source" "$target" >>"$log" 2>&1; then
      say "backed up $source to $target and verified the copy"
      [ "$backup" = failed ] || backup=verified
    else
      say "backup of $source to $target failed or does not match"
      backup=failed
    fi
  done <"$backup_list"
  case $backup in
    verified) notify "Pod $pod: idle for $((idle_for / 60)) min; its guard backed up the run to the volume (runs-guard-backup) and checked the copy." urgent ;;
    failed) notify "Pod $pod: idle for $((idle_for / 60)) min; its guard could not back up the run to the volume, so it will not delete the pod. See guard.log." urgent ;;
  esac
}

# Steps up as idle time passes, and back to the start on any work.
warned=""
urgent_at=""
backup=""
held_noticed=""
ladder() {
  if [ "$idle_for" -lt "$warn_after" ]; then
    if [ -n "$warned" ]; then
      say "work resumed; the idle ladder starts over"
      notify "Pod $pod: work resumed; the idle warnings are over."
      alert resumed "$idle_for"
    fi
    warned=""
    urgent_at=""
    backup=""
    held_noticed=""
    return 0
  fi
  if [ -z "$warned" ]; then
    warned=yes
    if [ "$idle_what" = "$no_work" ]; then
      say "idle warning: idle for ${idle_for}s"
      notify "Pod $pod: no GPU, CPU or network work for $((idle_for / 60)) min. Touch its keep-alive if the wait is wanted."
    else
      say "idle warning: idle for ${idle_for}s; $idle_what"
      notify "Pod $pod: $idle_what for $((idle_for / 60)) min."
    fi
    alert warn "$idle_for"
  fi
  clock=$(date +%s)
  if [ "$idle_for" -ge "$urgent_after" ] &&
    { [ -z "$urgent_at" ] || [ $((clock - urgent_at)) -ge "$urgent_repeat" ]; }; then
    urgent_at=$clock
    say "urgent: idle for ${idle_for}s"
    notify "Pod $pod: still no work after $((idle_for / 60)) min ($idle_what) and still billing. Check it or delete it." urgent
    alert urgent "$idle_for"
  fi
  if [ "$idle_for" -ge "$backup_after" ] && [ -z "$backup" ]; then
    back_up
    alert backup "$backup"
  fi
  [ "$idle_for" -ge "$delete_after" ] || return 0
  if [ "$ladder_delete" = on ] && [ -n "$deletable" ] &&
    { [ "$backup" = verified ] || [ "$backup" = nothing ]; }; then
    alert delete "$idle_for"
    shut_down "$idle_what for ${idle_for}s"
  fi
  [ -z "$held_noticed" ] || return 0
  held_noticed=yes
  if [ -z "$deletable" ]; then
    why="the run reports $progress, not stalled: it is still working"
  elif [ "$ladder_delete" = on ]; then
    why="its run tree backup failed"
  else
    why="deletion is off (ladder_delete)"
  fi
  say "idle for ${idle_for}s; not deleting: $why"
  notify "Pod $pod: idle for $((idle_for / 60)) min; its guard will not delete it because $why. It keeps billing until someone deletes it." urgent
  alert held "$why"
}

no_work="no GPU, CPU or network work"
idle_what=$no_work
# Only a stalled run, or a pod the counters find idle with no fresh line, may be deleted:
# a slow stage still makes pages, so it is warned about and backed up, never deleted.
deletable=yes
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
      notify "Pod $pod: its guard ignored the deadline file value '$(quoted "$latest")' (not epoch seconds within a week); the deadline stays ${deadline:-unset}."
    fi
  fi
  if [ -n "$deadline" ] && [ "$(date +%s)" -ge "$deadline" ]; then shut_down "approved time is up"; fi
  # The counters are read every tick, so their baselines stay current while a fresh
  # progress line decides.
  cpu_busy
  cpu=$?
  net_busy
  net=$?
  deletable=yes
  if read_progress; then
    idle_what="the run reports $progress ($progress_what)"
    case $progress in slow | bootstrapping) deletable="" ;; esac
    if [ "$progress" = ok ]; then idle_for=0; else idle_for=$progress_for; fi
  elif [ "$cpu" -eq 0 ] || [ "$net" -eq 0 ] || gpu_busy; then
    idle_what=$no_work
    idle_for=0
  elif [ "$cpu" -eq 2 ]; then
    idle_what=$no_work
    say "no CPU reading to compare this tick; idle time unchanged at ${idle_for}s"
  else
    idle_what=$no_work
    idle_for=$((idle_for + interval))
    # Idle time counts from the later of the last busy sample and the last keep-alive touch.
    if age=$(keepalive_age) && [ "$age" -lt "$idle_for" ]; then idle_for=$age; fi
  fi
  ladder
  sleep "$interval"
done
