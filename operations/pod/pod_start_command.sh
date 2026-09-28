#!/bin/sh
# Prints the container start command that arms a new pod's guard, for
# `runpodctl create pod ... --args "$(sh operations/pod/pod_start_command.sh <hours> <sha>)"`.
#
# The command fetches pod_guard.sh from this public repository at commit <sha> and runs it,
# and separately runs a backstop that deletes the pod an hour after its deadline (the one
# the guard keeps on the volume, so extensions count) even if the guard never started.
# Then it hands over to the image's own /start.sh, or keeps the container alive without it.
set -eu

hours=${1:?usage: pod_start_command.sh <hours> <sha>}
sha=${2:?usage: pod_start_command.sh <hours> <sha>}
case $sha in *[!0-9a-f]* | '') echo "pod_start_command: <sha> must be a commit hash" >&2; exit 2 ;; esac
case $hours in *[!0-9.]* | '' | .) echo "pod_start_command: <hours> must be a number" >&2; exit 2 ;; esac
window=$(awk -v h="$hours" 'BEGIN { printf "%d", h * 3600 }')
grace=${POD_BACKSTOP_GRACE:-3600}
poll=${POD_BACKSTOP_POLL:-300}
url="https://raw.githubusercontent.com/MegaSlick/Apparatus-Verbatus-Alpha/$sha/operations/pod/pod_guard.sh"

cat <<COMMAND
bash -c 'd=\${POD_GUARD_DIR:-/workspace/.pod_guard}; first=\$((\$(date +%s) + $window)); \
(curl -fsSL --max-time 120 $url -o /tmp/pod_guard.sh && sh /tmp/pod_guard.sh $hours 30) > /tmp/pod_guard.out 2>&1 & \
t=; command -v timeout >/dev/null && t="timeout 60"; \
(while :; do dl=\$(cat "\$d/deadline-\$RUNPOD_POD_ID" 2>/dev/null); case \$dl in ""|*[!0-9]*) dl=\$first;; esac; \
[ "\$dl" -le \$((\$(date +%s) + 604800)) ] || dl=\$first; \
if [ "\$(date +%s)" -ge \$((dl + $grace)) ]; then \$t runpodctl pod delete "\$RUNPOD_POD_ID" || \$t runpodctl remove pod "\$RUNPOD_POD_ID" || \$t runpodctl pod stop "\$RUNPOD_POD_ID"; fi; \
sleep $poll; done) > /tmp/pod_backstop.out 2>&1 & \
if [ -x /start.sh ]; then exec /start.sh; fi; exec sleep infinity'
COMMAND
