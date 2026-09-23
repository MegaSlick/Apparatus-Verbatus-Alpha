#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  printf '%s\n' 'worker-cleanup regression requires root inside its disposable container' >&2
  exit 1
fi

source /repo/session-verification/live_payload_lib.sh

if ! id -u verbatus-worker >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash verbatus-worker
fi

cat >/usr/local/bin/verbatus-worker-exec <<'EOF'
#!/usr/bin/env bash
exec "$@"
EOF
chmod 0755 /usr/local/bin/verbatus-worker-exec

if drain_worker_uid does-not-exist; then
  printf '%s\n' 'unresolved worker user was accepted' >&2
  exit 1
fi
if drain_worker_uid root; then
  printf '%s\n' 'uid zero worker cleanup was accepted' >&2
  exit 1
fi

control_sentinel=/tmp/root-control-sentinel
/bin/sh -c 'trap "exit 0" TERM; while :; do /bin/sleep 1; done' &
control_pid=$!
printf '%s\n' "$control_pid" >"$control_sentinel"

cleanup() {
  kill -TERM "$control_pid" 2>/dev/null || true
  wait "$control_pid" 2>/dev/null || true
}
trap cleanup EXIT

export SESSION_ID=worker-cleanup-regression
escaped_pid_file=/tmp/escaped-worker.pid
stdout_link_file=/tmp/escaped-worker-stdout
rm -f "$escaped_pid_file" "$stdout_link_file"

run_worker 1 /bin/sh -ceu '
  setsid /bin/sh -ceu '\''trap "" TERM; readlink /proc/self/fd/1 >"$1"; echo $$ >"$2"; while :; do :; done'\'' ignored '"$stdout_link_file"' '"$escaped_pid_file"' &
  child=$!
  wait "$child"
' &
wrapper_pid=$!

deadline=$((SECONDS + 10))
while [[ ! -s "$escaped_pid_file" ]]; do
  [[ "$SECONDS" -lt "$deadline" ]] || {
    printf '%s\n' 'detached worker did not publish its pid' >&2
    exit 1
  }
  /bin/sleep 1
done
escaped_pid="$(cat "$escaped_pid_file")"
[[ "$escaped_pid" =~ ^[1-9][0-9]*$ ]] || {
  printf '%s\n' 'detached worker published an invalid pid' >&2
  exit 1
}
kill -0 "$escaped_pid"
[[ "$(cat "$stdout_link_file")" == "$(readlink /proc/$$/fd/1)" ]] || {
  printf '%s\n' 'detached worker did not retain the caller stdout descriptor' >&2
  exit 1
}

if wait "$wrapper_pid"; then
  printf '%s\n' 'timed worker unexpectedly succeeded' >&2
  exit 1
else
  status=$?
fi
[[ "$status" -eq 124 ]] || {
  printf 'expected timeout status 124, got %s\n' "$status" >&2
  exit 1
}
if kill -0 "$escaped_pid" 2>/dev/null; then
  printf '%s\n' 'detached worker survived UID cleanup' >&2
  exit 1
fi
kill -0 "$control_pid"
printf '%s\n' '{"schema":"verbatus-live-payload-worker-cleanup-regression.v1","outcome":"passed"}'
