#!/usr/bin/env bash
# Reviewed worker-launch helper for live payloads.  The ignored payload copy is
# updated only after this helper and its pinned-image regression are reviewed.

require_session() {
  : "${SESSION_ID:?set SESSION_ID}"
  : "${TIMEOUT_SECONDS:?set TIMEOUT_SECONDS}"
  [[ "$SESSION_ID" =~ ^[A-Za-z0-9._-]+$ && "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]
}

require_sha() {
  : "${MERGED_SHA:?set MERGED_SHA}"
  [[ "$MERGED_SHA" =~ ^[0-9a-f]{40}$ ]]
}

worker_uid() {
  local user="$1" uid
  if ! uid="$(/usr/bin/id -u "$user" 2>/dev/null)"; then
    printf 'worker cleanup refused: cannot resolve user %q\n' "$user" >&2
    return 1
  fi
  if [[ ! "$uid" =~ ^[1-9][0-9]*$ ]]; then
    printf 'worker cleanup refused: user %q resolved to unsafe uid %q\n' "$user" "$uid" >&2
    return 1
  fi
  printf '%s\n' "$uid"
}

worker_processes_remain() {
  local uid="$1" state states status
  if states="$(/usr/bin/ps -o stat= -u "$uid" 2>/dev/null)"; then
    :
  else
    status=$?
    printf 'worker cleanup failed: ps for uid %s returned status %s\n' "$uid" "$status" >&2
    return 2
  fi
  for state in $states; do
    case "$state" in
      # Zombies and Linux dead tasks cannot run inference or retain file descriptors.
      Z*|X*) ;;
      R*|S*|D*|T*|t*|I*|W*|P*) return 0 ;;
      *)
        printf 'worker cleanup failed: ps for uid %s returned unknown state %q\n' "$uid" "$state" >&2
        return 2
        ;;
    esac
  done
  return 1
}

wait_for_worker_exit() {
  local uid="$1" deadline="$2" status
  while :; do
    if worker_processes_remain "$uid"; then
      if [[ "$SECONDS" -ge "$deadline" ]]; then
        return 1
      fi
      /bin/sleep 1
      continue
    else
      status=$?
    fi
    if [[ "$status" -eq 1 ]]; then
      return 0
    fi
    return "$status"
  done
}

drain_worker_uid() {
  local user="$1" uid status deadline
  if [[ "$EUID" -ne 0 ]]; then
    printf 'worker cleanup refused: effective uid is not root\n' >&2
    return 1
  fi
  if ! uid="$(worker_uid "$user")"; then
    return 1
  fi

  /usr/bin/pkill -TERM -u "$uid" || {
    status=$?
    [[ "$status" -eq 1 ]] || return "$status"
  }
  deadline=$((SECONDS + 10))
  if wait_for_worker_exit "$uid" "$deadline"; then
    return 0
  else
    status=$?
  fi
  if [[ "$status" -ne 1 ]]; then
    return "$status"
  fi
  if worker_processes_remain "$uid"; then
    /usr/bin/pkill -KILL -u "$uid" || {
      status=$?
      [[ "$status" -eq 1 ]] || return "$status"
    }
    deadline=$((SECONDS + 5))
    if wait_for_worker_exit "$uid" "$deadline"; then
      return 0
    else
      status=$?
    fi
    if [[ "$status" -ne 1 ]]; then
      return "$status"
    fi
  else
    status=$?
    [[ "$status" -eq 1 ]] || return "$status"
    return 0
  fi
  printf 'worker cleanup failed: uid %s still has live processes\n' "$uid" >&2
  return 1
}

run_worker() {
  local limit="$1" status
  shift
  if env -i PATH=/opt/verbatus/.venv/bin:/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 HOME=/home/verbatus-worker HF_HOME=/workspace/private/hf-client-cache VERBATUS_SESSION_ID="$SESSION_ID" /usr/sbin/runuser -u verbatus-worker -- /usr/local/bin/verbatus-worker-exec /usr/bin/timeout --foreground --signal=TERM --kill-after=30s "$limit" "$@"; then
    status=0
  else
    status=$?
  fi
  if [[ "$status" -ne 0 ]] && ! drain_worker_uid verbatus-worker; then
    printf 'worker cleanup did not complete after worker status %s\n' "$status" >&2
  fi
  return "$status"
}
