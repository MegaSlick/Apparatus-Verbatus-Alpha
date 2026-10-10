# Notifications: how a message reaches the lead's phone

## Sending one

```sh
sh operations/notify/notify.sh <milestone|decision|done|queue-done> "<one line>"
```

The message must be one non-empty line; a newline or carriage return is refused rather than
truncated (`client.py` also refuses a null byte). **Only the main session notifies; a
subagent never does.** Nothing in the script enforces that rule.

| Event | Title on the phone | Priority | Sent by |
|---|---|---|---|
| `milestone` | Milestone | 3 | the session, the operator tool with `--notify`, a pod lease's launch, close and balance reports, a bake-off queue's arms (including `arm failed: ...`) |
| `decision` | Needs a decision | 4 | the session, the operator tool with `--notify`, the pod's systemic and deadline-at-risk alarms, the session-end pod check, a bake-off queue that cannot go on by itself |
| `done` | Session complete | 3 | the session, when it closes |
| `queue-done` | Queue finished | 3 | a bake-off queue when its last arm has ended (the session is not over) |

Any other event name is refused.

## Delivery is reported honestly

**Every event exits non-zero when delivery failed** and prints `NOT DELIVERED` with the
reason; no failure is reported as success. A milestone is often the only announcement of a
long unattended result, so misreporting it is the worst case. Keeping a caller non-blocking
is the caller's job, never bought by misreporting.

**If a send fails, say so in the session.** A decision ping nobody hears is a session
waiting on a message that was never sent.

A delivered post prints `notify: delivered (<event>)` on stderr and exits 0, with stdout
untouched. Before resending anything, read that line (or the topic's delivery log), not the
absence of output, which cannot be told apart from a hang.

## The topic is a bearer secret

Anyone holding the topic can publish to the lead's phone. It lives in the gitignored
`private/ntfy.conf`, or in `NTFY_TOPIC` in the environment. **It never enters a script, a
note, a commit, a transcript or a command line.** The script keeps it out of `curl`'s
arguments, which any process lister can see. Do not echo it to check it; check that the file
exists.

The destination is fixed to `https://ntfy.sh`. `NTFY_SERVER` is refused, so no environment
variable can redirect messages to another host.

## The test sink

The topic value `verbatus-test-sink` (exactly; `verbatus-test-sink-2` notifies normally)
makes the script print what it would have sent to stderr and exit 0 without calling `curl`.
It is a backstop for tests that forget to stub a notifying hook; a test that reaches this
script at all is still a defect (inject a fake runner, or use the `silent` notifier).

Two places set it:

- the root `conftest.py`, in a session-scoped autouse fixture, covering every pytest session
  and its child processes;
- `.githooks/check-all.sh`, just above its pytest line, because the full gate runs inside the
  checkout holding the real topic. It reads the value out of `conftest.py` (no literal
  `NTFY_TOPIC=<topic>` for `.githooks/check_ingress.py` to refuse) and fails closed if the
  constant is renamed, since an empty `NTFY_TOPIC` falls back to `private/ntfy.conf`.

The sink exits 0 rather than refusing, so suites that assert on delivered versus `NOT
DELIVERED` measure the same thing. To keep that from reading as delivered, it prints one line
on stdout, which nothing else writes to:

    NOTIFY_SUPPRESSED verbatus-test-sink

Every Python caller goes through `operations/notify/client.py`, which reads that marker word
(never the topic) and returns a third state, `attempted=True, delivered=False,
suppressed=True`, printed as "Phone notification: suppressed (test sink)."

## Tests

`operations/notify/test_notify.py` covers the event table, the one-line rule, the exit codes,
the topic handling, and the client and script agreeing. It drives its own copy of the script
with a scrubbed `NTFY_` environment and a fake `curl`.
