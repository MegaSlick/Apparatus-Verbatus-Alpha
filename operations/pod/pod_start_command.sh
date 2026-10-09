#!/bin/sh
# shellcheck disable=SC2016 # Single-quoted text is the printed command's own shell code.
# Prints the container start command that arms a new pod's guard, for
#   START=$(sh operations/pod/pod_start_command.sh <hours|off> <sha>) &&
#   runpodctl pod create ... --docker-args "$START"
# The && keeps a refusal (exit 2, nothing printed) from creating an unguarded pod.
#
# The guard keeps its records on the volume at /workspace/private (a network volume or
# the pod's own disk): the one mount path the bootstrap and the data gate accept. A
# global volume (object storage) never holds them; it is mounted elsewhere, for results.
#
# The command fetches pod_guard.sh from this public repository at commit <sha>, trying
# for a few minutes, and runs it. Then it hands over to the image's own /start.sh, or
# keeps the container alive without it. Whether the guard deletes an idle pod is
# ladder_delete in this checkout's config/spend.toml.
#
# pod_budget in config/spend.toml (or VERBATUS_POD_BUDGET, which overrides it) decides
# what bounds the pod in time:
#   off, with `off`:   no deadline (an earlier start's deadline file is removed) and no
#                      backstop; the guard warns while the pod is idle.
#   off, with <hours>: a deadline <hours> from container start, and a backstop that
#                      deletes the pod an hour after the deadline even if the guard never
#                      started.
#   on, with <hours>:  the budget. The backstop also deletes the pod once the hard
#                      maximum has passed; the window is cut at container start so the
#                      deadline falls inside it, and `off` is refused.
# The deadline is written as the guard's deadline file when there is none yet, so the
# guard (which keeps an existing deadline, and so any extension) ends the pod in order
# before the backstop; the backstop reads the same file.
#
# The hard maximum is the sealed VERBATUS_HARD_MAX_SECONDS when set, else
# hard_max_seconds in config/spend.toml. It counts from when this command is printed, just
# before the pod is created, so a printed command belongs to the one pod created with it.
# The command records that instant on the volume as created-<pod id> for the finish estimate.
set -eu

refuse() { echo "pod_start_command: $*" >&2; exit 2; }
hours=${1:?usage: pod_start_command.sh <hours|off> <sha>}
sha=${2:?usage: pod_start_command.sh <hours|off> <sha>}
case $sha in *[!0-9a-f]* | '') refuse "<sha> must be a commit hash" ;; esac
case $hours in
  off) ;;
  *[!0-9.]* | '' | . | *.*.*) refuse "<hours> must be a number or off" ;;
  *) awk -v h="$hours" 'BEGIN { exit !(h + 0 > 0) }' || refuse "<hours> must be more than 0" ;;
esac

policy="$(dirname "$0")/../../config/spend.toml"
# One `key = "on"|"off"` line of the policy, or nothing.
switch() {
  awk -v key="$1" '$0 ~ "^[ \t]*" key "[ \t]*=" { n++; v = $0; sub(/^[^=]*=[ \t]*/, "", v); sub(/[ \t]*(#.*)?$/, "", v) }
    END { if (n == 1 && (v == "\"on\"" || v == "\"off\"")) { gsub(/"/, "", v); print v } }' "$policy" 2>/dev/null || true
}
if [ "${VERBATUS_POD_BUDGET+set}" = set ]; then
  budget=$VERBATUS_POD_BUDGET
  case $budget in on | off) ;; *) refuse "VERBATUS_POD_BUDGET ('$budget') must be on or off" ;; esac
else
  budget=$(switch pod_budget)
  [ -n "$budget" ] || refuse "cannot read pod_budget in $policy: it must be one line, \"on\" or \"off\""
fi

if [ "$budget" = on ]; then
  [ "$hours" != off ] || refuse "the budget is on (pod_budget), so the pod needs <hours>; off is for a budget that is off"
  if [ "${VERBATUS_HARD_MAX_SECONDS+set}" = set ]; then
    hard_max=$VERBATUS_HARD_MAX_SECONDS
    source="the sealed VERBATUS_HARD_MAX_SECONDS"
    case $hard_max in '' | *[!0-9]* | 0*)
      refuse "the sealed VERBATUS_HARD_MAX_SECONDS ('$hard_max') is not a positive whole number of seconds, so the hard maximum is unknown" ;;
    esac
  else
    source="hard_max_seconds in $policy"
    hard_max=$(awk '/^[ \t]*hard_max_seconds[ \t]*=/ { n++; v = $0; sub(/^[^=]*=[ \t]*/, "", v); sub(/[ \t]*(#.*)?$/, "", v) }
      END { if (n == 1 && v ~ /^[1-9][0-9]*$/) print v }' "$policy" 2>/dev/null) || hard_max=
    [ -n "$hard_max" ] || refuse "cannot read the hard maximum: $source is missing or not one positive whole number of seconds"
  fi
  if awk -v h="$hours" -v m="$hard_max" 'BEGIN { exit !(h * 3600 > m) }'; then
    refuse "$hours h is past the hard maximum of $hard_max seconds ($source)"
  fi
fi

ladder_delete=$(switch ladder_delete)
[ -n "$ladder_delete" ] || refuse "cannot read ladder_delete in $policy: it must be one line, \"on\" or \"off\""

created=$(date +%s)
grace=${POD_BACKSTOP_GRACE:-3600}
# The guard's deadline sits this far inside the cap, two of its ticks, so its orderly
# delete comes before the backstop's.
guard_margin=120
poll=${POD_BACKSTOP_POLL:-300}
guard_dir=/workspace/private/.pod_guard
url="https://raw.githubusercontent.com/MegaSlick/Apparatus-Verbatus-Alpha/$sha/operations/pod/pod_guard.sh"
id='"$d/deadline-$RUNPOD_POD_ID"'

record="(mkdir -p \"\$d\" && echo $created > \"\$d/created-\$RUNPOD_POD_ID\") 2>/dev/null;"
# Tries the fetch every 30 s, ten times by default, so a slow network at boot does not
# leave the pod without its guard.
guard() {
  echo "(n=0; until curl -fsSL --max-time 120 $url -o /tmp/pod_guard.sh; do n=\$((n + 1)); [ \"\$n\" -lt \${POD_GUARD_FETCH_TRIES:-10} ] || exit 1; sleep 30; done; \
POD_GUARD_DELETE=$ladder_delete sh /tmp/pod_guard.sh $1) > /tmp/pod_guard.out 2>&1 &"
}
handover='if [ -x /start.sh ]; then exec /start.sh; fi; exec sleep infinity'
# Writes the first deadline unless one is there, through a temporary file so a reader
# never sees it half written.
first_deadline="[ -s $id ] || { echo \$first > $id.new && mv $id.new $id; } 2>/dev/null;"
delete='$t runpodctl pod delete "$RUNPOD_POD_ID" || $t runpodctl remove pod "$RUNPOD_POD_ID" || $t runpodctl pod stop "$RUNPOD_POD_ID"'
read_deadline="dl=\$(cat $id 2>/dev/null); case \$dl in \"\"|*[!0-9]*) dl=\$first;; esac; \
[ \"\$dl\" -le \$((\$(date +%s) + 604800)) ] || dl=\$first;"

if [ "$hours" = off ]; then
  # A deadline file left by an earlier start of this pod is not this start's.
  cat <<COMMAND
bash -c 'd=\${POD_GUARD_DIR:-$guard_dir}; export POD_GUARD_DIR=\$d; \
$record \
rm -f $id 2>/dev/null; \
$(guard off) \
$handover'
COMMAND
  exit 0
fi

window=$(awk -v h="$hours" 'BEGIN { printf "%d", h * 3600 }')
[ "$window" -ge 1 ] || window=1
if [ "$budget" = off ]; then
  cat <<COMMAND
bash -c 'd=\${POD_GUARD_DIR:-$guard_dir}; export POD_GUARD_DIR=\$d; start=\$(date +%s); \
first=\$((start + $window)); gh=\$(awk "BEGIN { printf \"%.6f\", $window / 3600 }"); \
$record \
$first_deadline \
$(guard '$gh') \
t=; command -v timeout >/dev/null && t="timeout 60"; \
(while :; do $read_deadline \
if [ "\$(date +%s)" -ge \$((dl + $grace)) ]; then $delete; fi; sleep $poll; done) > /tmp/pod_backstop.out 2>&1 & \
$handover'
COMMAND
  exit 0
fi

cap=$((created + hard_max))
cat <<COMMAND
bash -c 'd=\${POD_GUARD_DIR:-$guard_dir}; export POD_GUARD_DIR=\$d; start=\$(date +%s); \
w=\$(($cap - $guard_margin - start)); [ "\$w" -gt $window ] && w=$window; [ "\$w" -lt 1 ] && w=1; \
first=\$((start + w)); gh=\$(awk -v s="\$w" "BEGIN { printf \"%.6f\", s / 3600 }"); \
$record \
$first_deadline \
$(guard '$gh') \
t=; command -v timeout >/dev/null && t="timeout 60"; \
(while :; do $read_deadline \
now=\$(date +%s); if [ "\$now" -ge \$((dl + $grace)) ] || [ "\$now" -ge $cap ]; then $delete; fi; \
s=\$(($cap - \$(date +%s))); [ "\$s" -gt $poll ] && s=$poll; [ "\$s" -lt 1 ] && s=1; sleep \$s; done) > /tmp/pod_backstop.out 2>&1 & \
$handover'
COMMAND
