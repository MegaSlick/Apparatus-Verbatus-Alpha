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

# No `waiting` field any more. It marked `start` and `milestone` as events whose
# failure was reported as success, and every event now reports failure honestly.
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

# Every event exits non-zero when delivery failed, including `start` and
# `milestone`. Two reviewers found those two returning 0 after printing NOT
# DELIVERED, so a caller checking the status was told the phone had it. A milestone
# is often the only announcement of a long unattended result, which makes it the
# worst one to lie about.
#
# It used to exit 0 for those two deliberately, so that a session could not die
# because a ping did not land. That reason no longer needs the exit code: the
# `SessionStart` hook in `.claude/settings.json` declares `"async": true`, so it
# runs detached and cannot block or fail the session whatever it returns. Keeping a
# caller non-blocking is the caller's job; misreporting delivery to buy it was
# paying in the one currency this script exists to protect.
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

# One reserved topic value means "a test harness is driving this; do not send."
#
# The seam every caller is supposed to use is an injected runner, and on the
# night this was added three tests were not using it: they drove `pod` with
# `--notify` against a fake provider, stubbed the launch hook and left the
# balance hook real, and posted nine identical milestones to his phone. That
# was invisible for as long as it was, because every earlier gate ran in a
# worktree with no `private/ntfy.conf` — the script failed "no topic
# configured" and the missing seam looked like a passing test. The gate that
# finally ran in the checkout that *does* hold the topic sent them.
#
# So the harness stops relying on every caller getting its seam right: the root
# `conftest.py` exports this value for the whole session and `.githooks/
# check-all.sh` exports it for its pytest line. A test that reaches this script
# anyway is reported here, loudly, on stderr, and no post is made.
#
# Deliberately *not* a failure. Exiting non-zero would make the guard change
# what the suites measure — several tests assert on delivered/NOT DELIVERED
# outcomes — and a guard that rewrites its subject's results is the shape
# GOVERNANCE 10 refuses. It exits 0 and says what it swallowed, so the
# behaviour under test is unchanged and the leak is still visible to anyone
# reading stderr.
#
# The value is a literal, not a pattern: a prefix or suffix rule would make a
# mistyped real topic silently stop notifying him.
if [ "$topic" = "verbatus-test-sink" ]; then
  echo "notify: test sink — not sent ($event): $message" >&2
  exit 0
fi

# `start` fires from a hook, not a hand. The desktop app can open several
# sessions in one launch — each fires the hook, and four pings for one sitting
# is noise, which is what makes the next one get ignored. One start per quarter
# hour is a heartbeat. Deliberate events (milestone, decision, done) are never
# suppressed: a rate limit on those could swallow a real result, and a decision
# ping nobody hears is a session waiting on a message that was never sent.
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
# notification and printed nothing.
#
# So: a regular file, not a symlink, holding the epoch second it was written.
# Unlike the config above, there is no legitimate reason to symlink a
# suppression stamp anywhere, and here the file is also WRITTEN — a link would
# redirect that write outside `private/`. Both reasons point the same way, so
# this path refuses links where the config accepts them.
#
# Every refusal below returns "not fresh", which sends the ping. That is the
# safe direction: the cost of being wrong is one duplicate notification, and
# the cost of the other direction is a session start nobody hears about. The
# stamp records a clock reading and nothing else — the topic never enters it.
# Two properties are being asserted, and both paths assert both: the path is a
# plain regular file we own, and its contents are a plausible past clock reading.
# Each was written out twice, in two spellings, so a hardening applied to the
# read side and not the write side would have been invisible. One statement each.
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
  # `read` rather than `head`: no subprocess, and the guards above have already
  # established this is a regular file, so it cannot block the way a FIFO would.
  # It returns non-zero on a last line with no trailing newline *having already
  # assigned it*, so the default is set beforehand rather than in an `||` that
  # would throw away a perfectly good stamp.
  stamped=""
  read -r stamped < "$stamp" 2>/dev/null || true
  case $stamped in
    ""|*[!0-9]*)
      # Includes every stamp written by the older `touch`-based version, which
      # left the file empty. One extra ping per machine, once.
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

# Write the stamp only where reading it would have been trusted — in the
# direction that matters more, since a redirected write leaves the topic's
# neighbourhood entirely. A stamp that cannot be written is reported and never
# fatal: the ping already went.
record_start_delivery() {
  unwritable="notify: could not record its suppression stamp; duplicates may follow"
  stamp_is_plain_file || { echo "$unwritable" >&2; return 0; }
  stamp_now=$(epoch_now) || { echo "$unwritable" >&2; return 0; }
  { printf '%s\n' "$stamp_now" > "$stamp"; } 2>/dev/null || echo "$unwritable" >&2
}

if [ "$event" = start ] && start_was_delivered_recently; then
  # "delivered", not "attempted": the stamp is written only inside the success
  # branch below, so a failed post never sets it. Saying "attempted" would leave
  # a reader unable to tell this suppression from a swallowed failure.
  echo "notify: a start ping was already delivered in the last $((suppress_window_s / 60)) minutes — suppressed" >&2
  exit 0
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
  # Stamp only a *delivered* start, so a failed post never suppresses the retry.
  # Two sessions racing the check can each send one ping; the failure mode of
  # that race is a duplicate, never a loss.
  if [ "$event" = start ]; then
    record_start_delivery
  fi
  exit 0
fi

fail "server did not accept the post"
