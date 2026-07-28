#!/bin/sh
# Send one notification to Tyrel. One way, outbound only, nothing is read back.
#
#   sh operations/notify/notify.sh <event> "<message>"
#
# <event> is one of: start, milestone, decision, done.
#
# The topic lives in private/ntfy.conf and nowhere else. The old repository
# hardcoded it in five shell scripts, which is how it ended up in cleartext in
# a census dump; a value that exists once can be rotated once.
#
# Exit codes carry meaning: start and milestone never fail the caller, but a
# decision or done that cannot be delivered exits 1 — a session blocked on
# Tyrel must know he was not reached, or it waits on a message nobody got.

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
  start)     title="Session started";   prio=low;     tags=computer ;;
  milestone) title="Milestone";         prio=default; tags=white_check_mark ;;
  decision)  title="Needs a decision";  prio=high;    tags=warning ;;
  done)      title="Session complete";  prio=default; tags=checkered_flag ;;
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

root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
conf="$root/private/ntfy.conf"

[ -r "$conf" ] || fail "no $conf — notifications are off until it exists"

# Read the two values as data. The config is never sourced: a config file that
# executes is a config file that can do anything, and this one runs from a hook.
NTFY_TOPIC=$(sed -n 's/^NTFY_TOPIC=//p' "$conf" | tail -n 1 | tr -d '"' | tr -d "'")
NTFY_SERVER=$(sed -n 's/^NTFY_SERVER=//p' "$conf" | tail -n 1 | tr -d '"' | tr -d "'")

[ -n "$NTFY_TOPIC" ] || fail "$conf sets no NTFY_TOPIC"

# The topic is a bearer credential and also becomes part of a URL. A typo that
# smuggles a slash or a space would look like a network problem forever.
case $NTFY_TOPIC in
  *[!A-Za-z0-9_-]*) fail "NTFY_TOPIC contains characters outside [A-Za-z0-9_-]" ;;
esac

server=${NTFY_SERVER:-https://ntfy.sh}
case $server in
  https://*) : ;;
  *) fail "NTFY_SERVER must be https:// — a bearer credential does not travel in clear" ;;
esac

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

# --fail so a rejected post is an error; --max-time so a hung network cannot
# block a session start. curl's own stderr is discarded: the topic is a bearer
# secret and no error text is worth risking it in a transcript.
if printf '%s' "$message" | curl -fsS --max-time 10 \
  -H "Title: $title" \
  -H "Priority: $prio" \
  -H "Tags: $tags" \
  --data-binary @- \
  "$server/$NTFY_TOPIC" > /dev/null 2>&1; then
  # Stamp only a *delivered* start, so a failed post never suppresses the retry.
  # Two sessions racing the check can each send one ping; the failure mode of
  # that race is a duplicate, never a loss.
  [ "$event" = start ] && { touch "$stamp" 2>/dev/null || true; }
  exit 0
fi

fail "post failed ($event) — Tyrel was NOT reached"
