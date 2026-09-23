#!/usr/bin/env bash
# Reviewed worker-launch helper for live payloads.  The ignored payload copy is
# updated only after this helper and its pinned-image regression are reviewed.

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

drain_worker_uid() {
  local user="$1" uid status deadline
  if ! uid="$(worker_uid "$user")"; then
    return 1
  fi

  /usr/bin/pkill -TERM -u "$uid" || {
    status=$?
    [[ "$status" -eq 1 ]] || return "$status"
  }
  deadline=$((SECONDS + 10))
  while /usr/bin/pgrep -u "$uid" >/dev/null 2>&1; do
    [[ "$SECONDS" -lt "$deadline" ]] || break
    /bin/sleep 1
  done
  if /usr/bin/pgrep -u "$uid" >/dev/null 2>&1; then
    /usr/bin/pkill -KILL -u "$uid" || {
      status=$?
      [[ "$status" -eq 1 ]] || return "$status"
    }
    deadline=$((SECONDS + 5))
    while /usr/bin/pgrep -u "$uid" >/dev/null 2>&1; do
      [[ "$SECONDS" -lt "$deadline" ]] || break
      /bin/sleep 1
    done
  fi
  if /usr/bin/pgrep -u "$uid" >/dev/null 2>&1; then
    printf 'worker cleanup failed: uid %s still has live processes\n' "$uid" >&2
    return 1
  fi
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
