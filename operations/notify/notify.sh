#!/bin/sh
# One outbound ntfy notification. The bearer topic comes from NTFY_TOPIC or
# ignored private/ntfy.conf and never appears in curl's arguments.

set +x
set -eu

if [ "$#" -lt 2 ]; then
  echo "usage: notify.sh <milestone|decision|done|queue-done> <one-line message>" >&2
  exit 2
fi

event=$1
shift
message=$*

case $event in
  milestone) title="Milestone"; priority=3; tag=white_check_mark ;;
  decision) title="Needs a decision"; priority=4; tag=warning ;;
  done) title="Session complete"; priority=3; tag=checkered_flag ;;
  queue-done) title="Queue finished"; priority=3; tag=checkered_flag ;;
  *) echo "notify: unknown event '$event'" >&2; exit 2 ;;
esac

case $message in
  ""|*"
"*|*""*)
    echo "notify: message must be one non-empty line" >&2
    exit 2 ;;
esac

root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd -P)
conf="$root/private/ntfy.conf"
topic=${NTFY_TOPIC:-}
if [ -z "$topic" ] && [ -f "$conf" ] && [ -r "$conf" ]; then
  topic=$(sed -n 's/^NTFY_TOPIC=//p' "$conf" | tail -n 1 | tr -d "\"'")
fi

# Every event exits non-zero on failed delivery: a milestone is often the only
# announcement of an unattended result.
fail() {
  echo "notify: NOT DELIVERED ($event) — $1" >&2
  exit 1
}

[ -n "$topic" ] || fail "no topic configured"
case $topic in
  *[!A-Za-z0-9_-]*) fail "topic contains invalid characters" ;;
esac
[ "${#topic}" -le 64 ] || fail "topic is too long"

if [ "${NTFY_SERVER+x}" = x ]; then
  echo "notify: NTFY_SERVER is unsupported; destination is fixed to https://ntfy.sh" >&2
  exit 2
fi

# The reserved test-sink topic (exported by conftest.py and check-all.sh) never posts.
# Exit 0 keeps the suites' delivered/NOT DELIVERED outcomes unchanged; the bridges
# map exit 0 to delivered, so the stdout line NOTIFY_SUPPRESSED marks it.
# A literal, not a pattern, so a mistyped real topic never silently stops notifying.
# Safe to print here and nowhere else in this script: only the public constant gets here.
if [ "$topic" = "verbatus-test-sink" ]; then
  printf 'NOTIFY_SUPPRESSED %s\n' "$topic"
  echo "notify: test sink — not sent ($event): $message" >&2
  exit 0
fi

# Prefer the checkout's .venv: a PATH `python3` may be missing or a Command Line Tools stub.
python_bin=python3
if [ -x "$root/.venv/bin/python" ]; then
  python_bin="$root/.venv/bin/python"
fi

if ! payload=$(NTFY_TOPIC=$topic NTFY_TITLE=$title NTFY_PRIORITY=$priority \
  NTFY_TAG=$tag NTFY_MESSAGE=$message "$python_bin" -c '
import json, os
print(json.dumps({
    "topic": os.environ["NTFY_TOPIC"],
    "title": os.environ["NTFY_TITLE"],
    "priority": int(os.environ["NTFY_PRIORITY"]),
    "tags": [os.environ["NTFY_TAG"]],
    "message": os.environ["NTFY_MESSAGE"],
}, ensure_ascii=False, separators=(",", ":")))
'); then
  fail "could not encode payload"
fi

unset NTFY_TOPIC NTFY_SERVER
code=""
if code=$(printf '%s' "$payload" | curl -q -sS --max-time 10 \
  --output /dev/null --write-out '%{http_code}' \
  -H "Content-Type: application/json" --data-binary @- https://ntfy.sh/ 2>/dev/null) &&
  case $code in 2??) true ;; *) false ;; esac
then
  # Report success explicitly, so silence is never read as either outcome.
  # stderr only: stdout is the bridges'. A closed stderr must not fail a delivered post.
  echo "notify: delivered ($event)" >&2 || true
  exit 0
fi

fail "server did not accept the post"
