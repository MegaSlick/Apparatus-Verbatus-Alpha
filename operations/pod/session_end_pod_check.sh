#!/bin/sh
# Claude Code SessionEnd hook: if any RunPod pod is still running when a session closes,
# send the lead one phone ping naming it. It only reports; the pod guard does the deleting.
root=$(cd "$(dirname "$0")/../.." && pwd)
(
  running=$(runpodctl get pod 2>/dev/null | grep -c RUNNING)
  if [ "${running:-0}" -gt 0 ]; then
    sh "$root/operations/notify/notify.sh" decision \
      "$running RunPod pod(s) still running after a Claude session closed: check RunPod." \
      >/dev/null 2>&1
  fi
) </dev/null >/dev/null 2>&1 &
exit 0
