# Notifications — how a message reaches his phone

This file owns the notification mechanism and event meanings. `CLAUDE.md` only routes
sessions here when a notification is needed.

## Sending one

```sh
sh operations/notify/notify.sh <start|milestone|decision|done> "<one line>"
```

The message must be a single non-empty line. A newline or a null in it is refused rather
than truncated, so a multi-line message never arrives as a misleading fragment.

**Main session only. A subagent never notifies.** Nothing in the script enforces that;
this file owns the rule.

## What each event does

| Event | Title on the phone | Priority | Sent by |
|---|---|---|---|
| `start` | Session started | 2 | the `SessionStart` hook, never a hand |
| `milestone` | Milestone | 3 | the session |
| `decision` | Needs a decision | 4 | the session |
| `done` | Session complete | 3 | `/session-end` |

Any other event name is refused.

## Failure is reported honestly

**Every event exits non-zero when delivery failed**, and prints `NOT DELIVERED` with the
reason. There is no event whose failure is reported as success.

That is deliberate and was not always true: `start` and `milestone` once exited 0 after
printing `NOT DELIVERED`, so a caller checking the status was told the phone had it. Two
reviewers found it. A milestone is often the only announcement of a long unattended
result, which makes it the worst one to lie about.

A failing ping cannot kill a session: the `SessionStart` hook is declared `"async": true`
in `.claude/settings.json`, so it runs detached. Keeping a caller non-blocking is the
caller's job — never buy it by misreporting delivery.

**If a send fails, say so in the session.** A decision ping nobody hears is a session
waiting on a message that was never sent.

## The topic is a bearer secret

Anyone holding the topic can publish to his phone. It lives in `private/ntfy.conf`, which
is gitignored, or in `NTFY_TOPIC` in the environment.

**It never enters a script, a note, a commit, a transcript, or a command line.** The
script reads it from the file and keeps it out of `curl`'s arguments, because arguments
are visible to anything that can list processes. Do not echo it to check it; check that
the file exists instead.

The destination is fixed to `https://ntfy.sh`. Setting `NTFY_SERVER` is refused outright
rather than honoured, so a redirect to another host cannot be arranged by an environment
variable.

## The test sink

**One topic value is reserved: `verbatus-test-sink`.** With it, the script prints what it
would have sent to stderr and exits 0 without calling `curl`. It is a literal, not a
prefix — a near-miss like `verbatus-test-sink-2` notifies normally, because a matching
rule loose enough to catch a typo would be loose enough to silence him.

It exists because the injected-runner seam every caller is meant to use is only as good
as the caller. Three tests in `operations/pod/test_pod_runtime.py` drove the pod CLI with
`--notify`, stubbed the launch hook, and left the balance hook real; a single gate run
posted nine identical `pod balance` milestones to his phone. Every earlier gate had run in
a worktree with no `private/ntfy.conf`, where the script failed "no topic configured" —
so the missing stub read as a passing test for as long as it did.

Two places set it, and both are deliberate rather than inherited:

- the root `conftest.py`, in a session-scoped autouse fixture, so any pytest session and
  every process it spawns is covered
- `.githooks/check-all.sh`, immediately above its pytest line, because the gate is the one
  run that happens inside the checkout holding the real topic. It *reads* the value out of
  `conftest.py` rather than restating it — one source of truth, and no literal
  `NTFY_TOPIC=<topic>` for `.githooks/check_ingress.py` to refuse, which it rightly would.
  It fails closed: a constant that has been renamed stops the gate, because an empty
  `NTFY_TOPIC` is not "no sink", it is `private/ntfy.conf`

**Exit 0, not a refusal.** A guard that failed the send would change what the suites it
protects measure — several assert on delivered versus `NOT DELIVERED` — and an instrument
that constrains its subject is what GOVERNANCE 10 refuses. The swallowed message goes to
stderr instead, so a leak stays visible without being fatal.

**And exit 0 alone was a second lie.** Every Python bridge over this script —
`operations/pod/notify_bridge.py`, `operations/pod/notify_hooks.py`,
`operations/operator/notify_bridge.py` — mapped exit 0 to `delivered=True`, so under the
sink each of them printed "Phone notification: sent." for a notification that never left
the machine. The exit code still stays 0, for the reason above; the distinction is carried
on **stdout**, which nothing else in this script writes to: one stable line,

    NOTIFY_SUPPRESSED verbatus-test-sink

Each bridge reads that marker word and returns a third state — `attempted=True`,
`delivered=False`, `suppressed=True` — whose printed line is "Phone notification:
suppressed (test sink)." The bridges match the marker word and never the topic, which is
normally a bearer secret; the topic is safe to print in that one line because control
reaches it only when the topic is exactly the reserved public constant.

The sink is a backstop, not the seam. A test that reaches this script at all is still a
defect: inject a fake runner, or use the `silent` notifier.

`operations/notify/test_notify.py` drives its own copy of the script with a scrubbed
`NTFY_` environment and a fake `curl`, so the sink never blocks the tests of the script
itself.

## Why `start` is rate-limited and the others are not

The desktop app can open several sessions in one launch, and each fires the hook. Four
pings for one sitting is noise, and noise is what teaches him to ignore the next one. So
`start` is suppressed if another was delivered within fifteen minutes, using a stamp at
`private/.notify-start-stamp`.

**`milestone`, `decision` and `done` are never suppressed.** A rate limit on those could
swallow a real result.

The stamp is treated as evidence rather than trusted for existing: it must be a regular
file, not a symlink, holding a plausible past clock reading. A directory left by a
crashed run, a FIFO, a symlink, or a future-dated stamp each silenced real notifications
before that check existed. Every refusal returns "not fresh" and therefore *sends* the
ping — the cost of being wrong that way is one duplicate, and the cost of the other way
is a session start nobody hears about. The stamp records a clock reading and nothing
else; the topic never enters it.

## Tests

`operations/notify/test_notify.py` covers the event table, the one-line rule, the
honest exit codes, the topic handling, and the stamp's refusals. Run it with the rest of
the suite.
