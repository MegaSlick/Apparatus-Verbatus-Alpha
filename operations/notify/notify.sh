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
#
# The cost of that choice is paid on stderr. For start and milestone, exit 0
# means only "the caller may carry on"; it does not mean the phone rang, and
# the two must never be confused by a reader of a transcript. Every failure
# therefore prints a NOT DELIVERED line, and only a real delivery prints
# nothing. An instrument may not report a measurement it did not make.

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

# One line means one line by every reader's definition. A bare carriage return
# breaks the message across lines in the phone client exactly as a newline does,
# so checking only for LF let a two-line notification through the check whose
# whole job was to refuse one.
nl=$(printf '\n_'); nl=${nl%_}
cr=$(printf '\r_'); cr=${cr%_}
if [ -z "$message" ] ||
  [ "${message#*"$nl"}" != "$message" ] ||
  [ "${message#*"$cr"}" != "$message" ]; then
  echo "notify: the message is one non-empty line — that is the contract" >&2
  exit 2
fi

# A delivery failure is fatal only for the events where somebody is waiting.
# It is never *silent*, whichever event it was: the reason goes out first, and
# a start or milestone that exits 0 anyway says in plain words that it did not
# arrive. Exit 0 there is a promise about the caller, not about the phone.
fail() {
  echo "notify: $1" >&2
  case $event in
    decision|done)
      echo "notify: NOT DELIVERED ($event) — Tyrel was NOT reached" >&2
      exit 1
      ;;
    *)
      echo "notify: NOT DELIVERED ($event) — Tyrel was NOT reached; exit 0 keeps the caller running and does not mean the notification arrived" >&2
      exit 0
      ;;
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
  # starts, with no error to explain why.
  #
  # Say exactly what `-f` establishes, because it used to be described as more
  # than it is. It excludes a directory, a FIFO, a device and a dangling link.
  # It does NOT refuse a working symlink to a regular file: `-f` follows the
  # link, and so does the `sed` below. That is deliberate here. Whoever can
  # plant a symlink in `private/` can plant a regular file there just as
  # easily, so refusing the link would buy nothing against the only attacker it
  # could be aimed at — while an operator keeping the topic in a password
  # manager's export directory and linking to it has a real use for one. The
  # stamp below reaches the opposite conclusion on purpose; the reasons are
  # written there.
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
suppress_window_s=900

# The stamp is evidence that a ping was delivered, so it is checked like
# evidence rather than trusted because something exists at the path.
#
# `find "$stamp" -mmin -15` asked one question — is there anything here that
# was touched recently — and four different objects answered yes. A directory
# left by a crashed run, a FIFO, a symlink aimed at some file the machine
# rewrites every minute, or a stamp dated in the future (a negative age is
# still "less than fifteen minutes", and a clock skew or a stray `touch -t`
# suppresses every start for as long as the date says) each silenced a real
# notification and printed nothing. This script runs from the SessionStart
# hook, where nobody is watching for a message that did not arrive.
#
# So: a regular file, not a symlink, holding the epoch second it was written.
# Unlike the config above, there is no legitimate reason to symlink a
# suppression stamp anywhere, and here the file is also WRITTEN — a link would
# redirect that write outside `private/`. Both reasons point the same way, so
# this path refuses links where the config accepts them.
#
# Every refusal below returns "not fresh", which sends the ping. That is the
# safe direction: the cost of being wrong is one duplicate notification, and
# the cost of the other direction is a session start nobody hears about.
start_was_delivered_recently() {
  [ -e "$stamp" ] || [ -L "$stamp" ] || return 1

  if [ -L "$stamp" ]; then
    echo "notify: the start stamp is a symlink; not trusting it to suppress a ping" >&2
    return 1
  fi
  if [ ! -f "$stamp" ]; then
    echo "notify: the start stamp is not a regular file; not trusting it to suppress a ping" >&2
    return 1
  fi

  stamp_now=$(date +%s 2>/dev/null) || stamp_now=""
  stamped=$(head -n 1 "$stamp" 2>/dev/null) || stamped=""
  case ${stamp_now} in
    ""|*[!0-9]*)
      echo "notify: cannot read the clock; not suppressing the start ping" >&2
      return 1 ;;
  esac
  case ${stamped} in
    ""|*[!0-9]*)
      # Includes every stamp written by the older `touch`-based version, which
      # left the file empty. One extra ping per machine, once.
      echo "notify: the start stamp carries no readable timestamp; not suppressing" >&2
      return 1 ;;
  esac

  stamp_age=$(( stamp_now - stamped ))
  if [ "${stamp_age}" -lt 0 ]; then
    echo "notify: the start stamp is dated in the future; not suppressing" >&2
    return 1
  fi
  [ "${stamp_age}" -lt "${suppress_window_s}" ]
}

# Write the stamp only where reading it would have been trusted. Refusing to
# write through a symlink or onto a non-regular file is the same rule as above,
# in the direction that matters more: a redirected write leaves the topic's
# neighbourhood entirely.
record_start_delivery() {
  if [ -L "$stamp" ] || { [ -e "$stamp" ] && [ ! -f "$stamp" ]; }; then
    echo "notify: the start stamp path is not a regular file; could not record its suppression stamp; duplicates may follow" >&2
    return 0
  fi
  stamp_now=$(date +%s 2>/dev/null) || stamp_now=""
  case ${stamp_now} in
    ""|*[!0-9]*)
      echo "notify: could not record its suppression stamp; duplicates may follow" >&2
      return 0 ;;
  esac
  if ! { printf '%s\n' "${stamp_now}" > "$stamp"; } 2>/dev/null; then
    echo "notify: could not record its suppression stamp; duplicates may follow" >&2
  fi
}

if [ "$event" = start ] && start_was_delivered_recently; then
  # "delivered", not "attempted": the stamp below is written only inside the
  # success branch, so a failed post never sets it. Saying "attempted" would
  # leave a reader unable to tell this suppression from a swallowed failure.
  echo "notify: a start ping was already delivered in the last 15 minutes — suppressed" >&2
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
  if [ "$event" = start ]; then
    record_start_delivery
  fi
  exit 0
fi

fail "the server did not accept the post"
