#!/bin/sh
# One outbound ntfy notification. The bearer topic comes from NTFY_TOPIC or
# ignored private/ntfy.conf and never appears in curl's arguments.

set +x
set -eu

if [ "$#" -lt 2 ]; then
  echo "usage: notify.sh <start|milestone|decision|done> <one-line message>" >&2
  exit 2
fi

event=$1
shift
message=$*

case $event in
  start) title="Session started"; priority=2; tag=computer; waiting=0 ;;
  milestone) title="Milestone"; priority=3; tag=white_check_mark; waiting=0 ;;
  decision) title="Needs a decision"; priority=4; tag=warning; waiting=1 ;;
  done) title="Session complete"; priority=3; tag=checkered_flag; waiting=1 ;;
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

fail() {
  echo "notify: NOT DELIVERED ($event) — $1" >&2
  [ "$waiting" -eq 0 ] && exit 0
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

if ! payload=$(NTFY_TOPIC=$topic NTFY_TITLE=$title NTFY_PRIORITY=$priority \
  NTFY_TAG=$tag NTFY_MESSAGE=$message python3 -c '
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
  exit 0
fi

fail "server did not accept the post"
