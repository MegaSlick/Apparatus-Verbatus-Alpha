#!/bin/sh
# Run one tracked, read-only Codex seat with a closed stdin and hard deadline.
#
#   sh operations/codex/seat.sh <seat> "<prompt>"
#   sh operations/codex/seat.sh <seat> - < prompt.txt
#   sh operations/codex/seat.sh --dry-run <seat> "<prompt>"
#
# Seats are evidence providers. Claude's main session remains responsible for
# scope, decisions, integrated changes, verification, and the user conversation.

set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd -P)
seats="$root/operations/codex/seats.conf"
dryrun=0
input_timeout_s=30
kill_grace_s=10
max_timeout_s=3600

if [ "${1:-}" = "--dry-run" ]; then
  dryrun=1
  shift
  if [ "${1:-}" = "--seats" ]; then
    [ "$#" -ge 2 ] || { echo "seat: --seats needs a file" >&2; exit 2; }
    seats=$2
    shift 2
  fi
elif [ "${1:-}" = "--seats" ]; then
  echo "seat: alternate seat files are validation-only; add --dry-run" >&2
  exit 2
fi

usage() {
  echo "usage: seat.sh [--dry-run [--seats file]] <seat> <prompt|->" >&2
  if [ -r "$seats" ]; then
    sed -n 's/^\([a-z][a-z0-9_-]*\)  *\([^ ]*\)  *\([^ ]*\)  *\([^ ]*\).*/  \1  (\2, \3, \4s)/p' \
      "$seats" >&2
  fi
  exit 2
}

[ "$#" -eq 2 ] || usage
seat=$1
prompt=$2

timeout_cmd=""
if [ "$prompt" = "-" ] || [ "$dryrun" -eq 0 ]; then
  if command -v timeout >/dev/null 2>&1; then
    timeout_cmd=timeout
  elif command -v gtimeout >/dev/null 2>&1; then
    timeout_cmd=gtimeout
  else
    echo "seat: no timeout command on PATH; install coreutils" >&2
    exit 2
  fi
fi

if [ "$dryrun" -eq 0 ]; then
  command -v codex >/dev/null 2>&1 ||
    { echo "seat: codex is not installed" >&2; exit 2; }
fi

if [ "$prompt" = "-" ]; then
  if prompt=$("$timeout_cmd" "--kill-after=$kill_grace_s" "$input_timeout_s" cat); then
    :
  else
    status=$?
    echo "seat: prompt input did not close within ${input_timeout_s}s" >&2
    exit "$status"
  fi
fi
[ -n "$prompt" ] || { echo "seat: empty prompt" >&2; exit 2; }
[ -r "$seats" ] || { echo "seat: no $seats" >&2; exit 2; }

matches=$(awk -v want="$seat" '
  /^[[:space:]]*#/ { next }
  NF == 0 { next }
  $1 == want { print }
' "$seats")
count=$(printf '%s' "$matches" | grep -c . || true)
[ "$count" -ne 0 ] || { echo "seat: no seat named '$seat'" >&2; usage; }
[ "$count" -eq 1 ] ||
  { echo "seat: '$seat' is declared $count times" >&2; exit 2; }

field_count=$(printf '%s\n' "$matches" | awk '{print NF}')
[ "$field_count" -eq 4 ] ||
  { echo "seat: '$seat' must have exactly four fields" >&2; exit 2; }

model=$(printf '%s\n' "$matches" | awk '{print $2}')
effort=$(printf '%s\n' "$matches" | awk '{print $3}')
timeout_s=$(printf '%s\n' "$matches" | awk '{print $4}')

case $model in
  -*|" "|""|*[!A-Za-z0-9._-]*)
    echo "seat: '$model' is not a plain model name" >&2
    exit 2 ;;
esac
case $effort in
  none|minimal|low|medium|high|xhigh|max) : ;;
  *) echo "seat: '$effort' is not an allowed effort" >&2; exit 2 ;;
esac
case $timeout_s in
  ""|*[!0-9]*)
    echo "seat: timeout must be a positive whole number" >&2
    exit 2 ;;
esac
if ! [ "$timeout_s" -gt 0 ] 2>/dev/null ||
  [ "$timeout_s" -gt "$max_timeout_s" ]; then
  echo "seat: timeout must be between 1 and ${max_timeout_s}s" >&2
  exit 2
fi

report_target_s=$((timeout_s * 9 / 10))
prompt="Time budget: ${timeout_s}s. Finish and report by ${report_target_s}s.
The main session owns scope and decisions. Return evidence or a proposal only.
Do not mutate, push, merge, spend, disclose, or expand scope.

Task:
$prompt"

set -- codex exec \
  --ephemeral \
  --ignore-user-config \
  --strict-config \
  -m "$model" \
  -c "model_reasoning_effort=$effort" \
  -s read-only \
  -C "$root" \
  --skip-git-repo-check \
  -- \
  "$prompt"

echo "seat: $seat -> $model, effort $effort, read-only, timeout ${timeout_s}s" >&2

if [ "$dryrun" -eq 1 ]; then
  for arg in "$@"; do
    printf '%s\n' "$arg"
  done
  exit 0
fi

if "$timeout_cmd" "--kill-after=$kill_grace_s" "$timeout_s" "$@" </dev/null; then
  status=0
else
  status=$?
fi
case $status in
  124) echo "seat: '$seat' hit its ${timeout_s}s ceiling; output may be incomplete" >&2 ;;
  137) echo "seat: '$seat' was hard-killed; output may be incomplete" >&2 ;;
esac
exit "$status"
