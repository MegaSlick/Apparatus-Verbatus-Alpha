# Notifications — how a message reaches the lead's phone

This file owns the notification mechanism and event meanings. Sessions come here when a
notification is needed.

## Sending one

```sh
sh operations/notify/notify.sh <milestone|decision|done|queue-done> "<one line>"
```

The message must be a single non-empty line. A newline or carriage return in it is refused
rather than truncated, so a multi-line message never arrives as a misleading fragment;
`client.py` also refuses a null byte, which a command-line argument cannot carry.

**Main session only. A subagent never notifies.** Nothing in the script enforces that;
this file owns the rule.

## What each event does

| Event | Title on the phone | Priority | Sent by |
|---|---|---|---|
| `milestone` | Milestone | 3 | the session, the operator tool with `--notify`, a pod lease's launch, close and balance reports, and a bake-off queue's arms (an arm that failed and that the queue retries or reports itself is a milestone starting `arm failed:`) |
| `decision` | Needs a decision | 4 | the session, the operator tool with `--notify`, the pod's systemic alarm, the session-end pod check, and a bake-off queue that cannot go on by itself (no pages, stopped, pod not ended) |
| `done` | Session complete | 3 | the session, when it closes |
| `queue-done` | Queue finished | 3 | a bake-off queue (`operations/bakeoff/queue_runner.py`), when its last arm has ended; the session is not over |

Any other event name is refused.

## Failure is reported honestly

**Every event exits non-zero when delivery failed**, and prints `NOT DELIVERED` with the
reason. There is no event whose failure is reported as success.

A caller checking the status must never be told the phone has a message it does not. A
milestone is often the only announcement of a long unattended result, which makes it the
worst one to misreport.

Keeping a caller non-blocking is the caller's job — never buy it by misreporting
delivery.

**If a send fails, say so in the session.** A decision ping nobody hears is a session
waiting on a message that was never sent.

**Success is reported too, on stderr.** A delivered post prints `notify: delivered
(<event>)`: exit 0, stdout untouched. Silence after a stalled earlier command in the same
chain cannot be told apart from a hang, so before resending anything, read this line (or
the topic's own delivery log), never the absence of output. `operations/notify/client.py` is unaffected: it keys on the exit
code and on `NOTIFY_SUPPRESSED` on stdout, and this line never reaches that stream.

## The topic is a bearer secret

Anyone holding the topic can publish to the lead's phone. It lives in `private/ntfy.conf`, which
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
rule loose enough to catch a typo would be loose enough to silence the lead.

It exists because the injected-runner seam every caller is meant to use is only as good
as the caller: a test that stubs one notifying hook and leaves another real would post to
his phone from inside the suite. In a worktree with no `private/ntfy.conf` the same missing
stub fails quietly with "no topic configured", so it can pass unnoticed until the suite
runs in the checkout that holds the real topic.

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
protects measure — several assert on delivered versus `NOT DELIVERED` — and a measuring
instrument must not change the thing it measures. The swallowed message goes to stderr
instead, so a leak stays visible without being fatal.

**Exit 0 alone would read as delivered.** A caller mapping exit 0 to `delivered=True`
would print "Phone notification: sent." for a notification that never left the machine.
The exit code stays 0, for the reason above; the distinction is carried on **stdout**,
which nothing else in this script writes to: one stable line,

    NOTIFY_SUPPRESSED verbatus-test-sink

Every Python caller goes through one client, `operations/notify/client.py`, which reads
that marker word and returns a third state — `attempted=True`, `delivered=False`,
`suppressed=True` — whose printed line is "Phone notification: suppressed (test sink)."
The client matches the marker word and never the topic, which is normally a bearer
secret; the topic is safe to print in that one line because control reaches it only when
the topic is exactly the reserved public constant. `test_notify.py` runs the real script
through the client to prove the two agree.

The sink is a backstop, not the seam. A test that reaches this script at all is still a
defect: inject a fake runner, or use the `silent` notifier.

`operations/notify/test_notify.py` drives its own copy of the script with a scrubbed
`NTFY_` environment and a fake `curl`, so the sink never blocks the tests of the script
itself.

## Tests

`operations/notify/test_notify.py` covers the event table, the one-line rule, the
honest exit codes, the topic handling, and the stamp's refusals. Run it with the rest of
the suite.
