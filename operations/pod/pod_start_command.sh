#!/bin/sh
# Prints the container start command that arms a new pod's guard, for
#   START=$(sh operations/pod/pod_start_command.sh <hours> <sha>) &&
#   runpodctl pod create ... --docker-args "$START"
# The && keeps a refusal (exit 2, nothing printed) from creating an unguarded pod.
#
# The guard keeps its records on the network volume, which must be mounted at
# /workspace/private: the one mount path the bootstrap and the data gate accept.
#
# The command fetches pod_guard.sh from this public repository at commit <sha> and runs it,
# and separately runs a backstop that deletes the pod an hour after its deadline (the one
# the guard keeps on the volume, so extensions count) even if the guard never started, and
# in any case once the budget's hard maximum has passed. Then it hands over to the image's
# own /start.sh, or keeps the container alive without it.
#
# The hard maximum is the sealed VERBATUS_HARD_MAX_SECONDS when set, else hard_max_seconds
# in this checkout's config/spend.toml. It counts from when this command is printed, just
# before the pod is created, so a printed command belongs to the one pod created with it.
# The command records that instant on the volume as created-<pod id> for the finish estimate.
set -eu

refuse() { echo "pod_start_command: $*" >&2; exit 2; }
hours=${1:?usage: pod_start_command.sh <hours> <sha>}
sha=${2:?usage: pod_start_command.sh <hours> <sha>}
case $sha in *[!0-9a-f]* | '') refuse "<sha> must be a commit hash" ;; esac
case $hours in *[!0-9.]* | '' | . | *.*.*) refuse "<hours> must be a number" ;; esac

policy="$(dirname "$0")/../../config/spend.toml"
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
window=$(awk -v h="$hours" 'BEGIN { printf "%d", h * 3600 }')
created=$(date +%s)
cap=$((created + hard_max))
grace=${POD_BACKSTOP_GRACE:-3600}
poll=${POD_BACKSTOP_POLL:-300}
guard_dir=/workspace/private/.pod_guard
url="https://raw.githubusercontent.com/MegaSlick/Apparatus-Verbatus-Alpha/$sha/operations/pod/pod_guard.sh"

cat <<COMMAND
bash -c 'd=\${POD_GUARD_DIR:-$guard_dir}; export POD_GUARD_DIR=\$d; first=\$((\$(date +%s) + $window)); \
(mkdir -p "\$d" && echo $created > "\$d/created-\$RUNPOD_POD_ID") 2>/dev/null; \
(curl -fsSL --max-time 120 $url -o /tmp/pod_guard.sh && sh /tmp/pod_guard.sh $hours 30) > /tmp/pod_guard.out 2>&1 & \
t=; command -v timeout >/dev/null && t="timeout 60"; \
(while :; do dl=\$(cat "\$d/deadline-\$RUNPOD_POD_ID" 2>/dev/null); case \$dl in ""|*[!0-9]*) dl=\$first;; esac; \
[ "\$dl" -le \$((\$(date +%s) + 604800)) ] || dl=\$first; \
now=\$(date +%s); if [ "\$now" -ge \$((dl + $grace)) ] || [ "\$now" -ge $cap ]; then \$t runpodctl pod delete "\$RUNPOD_POD_ID" || \$t runpodctl remove pod "\$RUNPOD_POD_ID" || \$t runpodctl pod stop "\$RUNPOD_POD_ID"; fi; \
sleep $poll; done) > /tmp/pod_backstop.out 2>&1 & \
if [ -x /start.sh ]; then exec /start.sh; fi; exec sleep infinity'
COMMAND
