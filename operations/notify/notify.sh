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
  start) title="Session started"; priority=2; tag=computer ;;
  milestone) title="Milestone"; priority=3; tag=white_check_mark ;;
  decision) title="Needs a decision"; priority=4; tag=warning ;;
  done) title="Session complete"; priority=3; tag=checkered_flag ;;
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
# announcement of an unattended result. The SessionStart hook runs async, so a
# failed ping still cannot block a session.
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
# Printing it is safe: control reaches here only for the public constant.
if [ "$topic" = "verbatus-test-sink" ]; then
  printf 'NOTIFY_SUPPRESSED %s\n' "$topic"
  echo "notify: test sink — not sent ($event): $message" >&2
  exit 0
fi

# `start` fires from a hook, once per session the app opens: at most one per 15
# minutes. Deliberate events are never rate-limited; that could swallow a result.
stamp="$root/private/.notify-start-stamp"
suppress_window_s=900

# The stamp is evidence of delivery: trust only a regular file, never a symlink (which
# would also redirect the write out of private/), holding a past epoch second.
# `find -mmin` once accepted directories, FIFOs, symlinks and future dates. Every
# refusal sends the ping: a duplicate is cheaper than a start nobody hears about.
stamp_is_plain_file() {
  if [ -L "$stamp" ]; then
    echo "notify: the start stamp is a symlink; not trusting it" >&2
    return 1
  fi
  if [ -e "$stamp" ] && [ ! -f "$stamp" ]; then
    echo "notify: the start stamp is not a regular file; not trusting it" >&2
    return 1
  fi
}

epoch_now() {
  now=$(date +%s 2>/dev/null) || now=""
  case $now in
    ""|*[!0-9]*) return 1 ;;
  esac
  printf '%s' "$now"
}

start_was_delivered_recently() {
  [ -e "$stamp" ] || [ -L "$stamp" ] || return 1
  stamp_is_plain_file || return 1

  stamp_now=$(epoch_now) || {
    echo "notify: cannot read the clock; not suppressing the start ping" >&2
    return 1
  }
  # Safe from FIFO blocking: a regular file is established above. `read` fails on
  # a final line without newline after assigning it, so the default goes first.
  stamped=""
  read -r stamped < "$stamp" 2>/dev/null || true
  case $stamped in
    ""|*[!0-9]*)
      echo "notify: the start stamp carries no readable timestamp; not suppressing" >&2
      return 1 ;;
  esac

  stamp_age=$(( stamp_now - stamped ))
  if [ "$stamp_age" -lt 0 ]; then
    echo "notify: the start stamp is dated in the future; not suppressing" >&2
    return 1
  fi
  [ "$stamp_age" -lt "$suppress_window_s" ]
}

# Write only where a read would be trusted. An unwritable stamp is reported, never
# fatal: the ping already went.
record_start_delivery() {
  unwritable="notify: could not record its suppression stamp; duplicates may follow"
  stamp_is_plain_file || { echo "$unwritable" >&2; return 0; }
  stamp_now=$(epoch_now) || { echo "$unwritable" >&2; return 0; }
  { printf '%s\n' "$stamp_now" > "$stamp"; } 2>/dev/null || echo "$unwritable" >&2
}

if [ "$event" = start ] && start_was_delivered_recently; then
  # The stamp is written only after a successful post, so this is never a swallowed failure.
  echo "notify: a start ping was already delivered in the last $((suppress_window_s / 60)) minutes — suppressed" >&2
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
  # Two racing sessions may each ping: a duplicate, never a loss.
  if [ "$event" = start ]; then
    record_start_delivery
  fi
  # Silence must never read as delivered (a stalled session once resent pings).
  # stderr only: stdout is the bridges'. A closed stderr must not fail a delivered post.
  echo "notify: delivered ($event)" >&2 || true
  exit 0
fi

fail "server did not accept the post"
