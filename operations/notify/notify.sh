#!/bin/sh
# Send one notification to Tyrel. One way and outbound only: no response body
# is consumed, only the HTTP status needed to establish server acceptance.
#
#   sh operations/notify/notify.sh <event> "<message>"
#
# <event> is one of: start, milestone, decision, done.
#
# The bearer topic comes from NTFY_TOPIC if the environment sets it, and
# otherwise from private/ntfy.conf, which is gitignored. Tyrel's ruling: an
# ignored file under private/ is an acceptable home for it. What the topic must
# never do is reach a commit, a script, a transcript or curl's argv — and none
# of the handling below lets it.
#
# Exit codes carry meaning: start and milestone never fail the caller, but a
# decision or done that cannot be delivered exits 1 — a session blocked on
# Tyrel must know he was not reached, or it waits on a message nobody got.

# A caller may itself be running with shell xtrace enabled. Disable it before
# reading the environment: otherwise the bearer topic is copied into stderr by
# assignment and comparison traces even though it never enters curl's argv.
set +x
set -eu

# Validate the interface before touching any configuration, so a bad call is
# reported as a bad call even on a machine with no topic configured.
if [ "$#" -lt 2 ]; then
  echo "usage: notify.sh <start|milestone|decision|done> <message>" >&2
  exit 2
fi

event=$1
shift
message=$*

case $event in
  start)     title="Session started";   prio=2; tags=computer ;;
  milestone) title="Milestone";         prio=3; tags=white_check_mark ;;
  decision)  title="Needs a decision";  prio=4; tags=warning ;;
  done)      title="Session complete";  prio=3; tags=checkered_flag ;;
  *)         echo "notify: unknown event '$event'" >&2; exit 2 ;;
esac

nl=$(printf '\n_'); nl=${nl%_}
if [ -z "$message" ] || [ "${message#*"$nl"}" != "$message" ]; then
  echo "notify: the message is one non-empty line — that is the contract" >&2
  exit 2
fi

# A delivery failure is fatal only for the events where somebody is waiting.
fail() {
  echo "notify: $1" >&2
  case $event in
    decision|done) exit 1 ;;
    *)             exit 0 ;;
  esac
}

root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd -P)
conf="$root/private/ntfy.conf"

# Two sources, in this order: an injected environment variable wins, and the
# gitignored config file is the default. Requiring the environment alone was
# tried and is wrong here — nothing in this repository populates it, so every
# `start` and `milestone` would exit 0 announcing "notifications are off" and
# the phone would simply go quiet. That is the failure mode nobody notices,
# which makes it worse than the one it was guarding against.
topic=${NTFY_TOPIC:-}
if [ -z "$topic" ] && [ -f "$conf" ] && [ -r "$conf" ]; then
  # Read as data. The config is never sourced: a config file that executes is a
  # config file that can do anything, and this one runs from a hook.
  #
  # `-f` and not merely `-r`: a named pipe at this path is readable, and reading
  # one blocks until something writes. This script runs from the SessionStart
  # hook, so that is not a failed notification — it is a session that never
  # starts, with no error to explain why. A regular file, or nothing.
  topic=$(sed -n 's/^NTFY_TOPIC=//p' "$conf" | tail -n 1 | tr -d '"' | tr -d "'")
fi
[ -n "$topic" ] || fail "no topic in the environment or $conf — notifications are off"

# The destination is part of the credential boundary. If an inherited variable
# could replace it, a stale shell or launcher could send the bearer topic and
# the notification text to an arbitrary HTTPS server that returns 2xx. Refuse
# the undocumented override rather than silently trusting ambient state.
if [ "${NTFY_SERVER+x}" = x ]; then
  echo "notify: NTFY_SERVER is unsupported; delivery is fixed to https://ntfy.sh" >&2
  exit 2
fi
server=https://ntfy.sh

# The topic is a bearer credential and also becomes part of a URL. A typo that
# smuggles a slash or a space would look like a network problem forever.
case $topic in
  *[!A-Za-z0-9_-]*) fail "NTFY_TOPIC contains characters outside [A-Za-z0-9_-]" ;;
esac
[ "${#topic}" -le 64 ] || fail "NTFY_TOPIC is longer than ntfy's 64-character limit"

# `start` fires from a hook, not a hand. The desktop app can open several
# sessions in one launch — each fires the hook, and four pings for one sitting
# is noise. One start per quarter hour is a heartbeat. Deliberate events
# (milestone, decision, done) are never suppressed: a rate limit on those
# could swallow a real result, and nothing is lost silently.
stamp="$root/private/.notify-start-stamp"
if [ "$event" = start ] && [ -n "$(find "$stamp" -mmin -15 2>/dev/null)" ]; then
  echo "notify: a start ping was already attempted in the last 15 minutes — suppressed" >&2
  exit 0
fi

# Publish JSON to the server root, which ntfy documents as its fileless
# publishing form. Python performs only JSON encoding: it never opens a socket.
# Building the payload in memory keeps the topic out of curl's URL and argv.
unset payload
if payload=$(NTFY_TOPIC=$topic NTFY_TITLE=$title NTFY_PRIORITY=$prio NTFY_TAGS=$tags \
  NTFY_MESSAGE=$message python3 -c '
import json
import os
import sys

json.dump(
    {
        "topic": os.environ["NTFY_TOPIC"],
        "message": os.environ["NTFY_MESSAGE"],
        "title": os.environ["NTFY_TITLE"],
        "priority": int(os.environ["NTFY_PRIORITY"]),
        "tags": [os.environ["NTFY_TAGS"]],
    },
    sys.stdout,
    ensure_ascii=False,
    separators=(",", ":"),
)
'); then
  :
else
  fail "could not encode the notification payload"
fi

# Do not pass the bearer topic to curl in its inherited environment. `-q` must
# be curl's first option so a machine-local .curlrc cannot weaken the request.
# Record the HTTP code explicitly: curl otherwise treats a 3xx response as a
# successful transfer even though ntfy did not accept the notification.
# curl's stderr is discarded because request diagnostics can include URLs.
unset NTFY_TOPIC NTFY_SERVER
http_code=""
if http_code=$(printf '%s' "$payload" | curl -q -sS --max-time 10 \
  --output /dev/null \
  --write-out '%{http_code}' \
  -H "Content-Type: application/json" \
  --data-binary @- \
  "$server/" 2>/dev/null) &&
  case "$http_code" in 2??) true ;; *) false ;; esac
then
  # Stamp only a *delivered* start, so a failed post never suppresses the retry.
  # Two sessions racing the check can each send one ping; the failure mode of
  # that race is a duplicate, never a loss.
  if [ "$event" = start ] && ! touch "$stamp" 2>/dev/null; then
    echo "notify: delivered start but could not record its suppression stamp; duplicates may follow" >&2
  fi
  exit 0
fi

fail "post failed ($event) — Tyrel was NOT reached"
