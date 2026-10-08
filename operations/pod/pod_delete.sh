#!/bin/sh
# Deletes one RunPod pod the way its guard does, for a caller on the pod (the bake-off
# queue) when no live guard is there to do it: runpodctl's two delete forms, then the
# REST API with the pod-scoped RUNPOD_API_KEY handed to curl in a config file, so the key
# never reaches a command line. Three attempts; after the third has failed it asks for a
# stop instead, which ends the card's billing but keeps the pod's disk. The network
# volume is never touched.
#
#   pod_delete.sh <pod id>
#
# Exits 0 when a delete was requested, 3 when only the stop was, 1 when nothing worked
# and 2 on a usage error. POD_DELETE_INTERVAL is the wait between attempts (default 20 s).
set -u

pod=${1:-}
case $pod in
  '' | *[!A-Za-z0-9]*) echo "usage: pod_delete.sh <pod id>" >&2; exit 2 ;;
esac
interval=${POD_DELETE_INTERVAL:-20}

if command -v timeout >/dev/null 2>&1; then limit="timeout 60"; else limit=""; fi
limited() {
  # shellcheck disable=SC2086
  $limit "$@"
}

delete_pod() {
  limited runpodctl pod delete "$pod" && return 0
  limited runpodctl remove pod "$pod" && return 0
  [ -n "${RUNPOD_API_KEY:-}" ] || return 1
  config=$(mktemp) || return 1
  printf 'header = "Authorization: Bearer %s"\n' "$RUNPOD_API_KEY" >"$config"
  limited curl -fsS --max-time 60 -K "$config" -X DELETE "https://api.runpod.io/v2/pods/$pod" -o /dev/null
  status=$?
  rm -f "$config"
  return "$status"
}

stop_pod() {
  limited runpodctl pod stop "$pod" || limited runpodctl stop pod "$pod"
}

attempt=1
while [ "$attempt" -le 3 ]; do
  if delete_pod; then
    echo "pod_delete: delete of pod $pod requested (attempt $attempt)"
    exit 0
  fi
  echo "pod_delete: delete attempt $attempt for pod $pod failed" >&2
  [ "$attempt" -eq 3 ] || sleep "$interval"
  attempt=$((attempt + 1))
done
if stop_pod; then
  echo "pod_delete: three deletes failed, so pod $pod was asked to stop; check RunPod" >&2
  exit 3
fi
echo "pod_delete: could not delete or stop pod $pod; delete it by hand" >&2
exit 1
