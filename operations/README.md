# operations

Anything with a human, a machine, or money on the other end.

| Directory | Declared responsibility |
|---|---|
| `submit/` | the future page where images are handed in |
| `pod/` | future pod rental, shutdown, and provider-state/billing verification |
| `data/` | future movement of runs and exports between machines |
| `notify/` | the implemented, fileless one-way notification client |
| `review/` | immutable review-candidate manifests and local receipts |

There is no `local/`, `remote/`, or `deploy/` here. A pod runs the very same stage
directories this repository holds. Where code runs is an operational fact, not an
organising principle.

Governance 8 will govern any implementation in `pod/`: a live pod needs Tyrel's explicit
permission in that session, and shutdown must be verified against provider state and
billing, never inferred from an acknowledgement. The current directory contains that
contract, not a launcher or verifier.

`notify/notify.sh` takes its bearer topic from `NTFY_TOPIC` when the environment sets one,
and otherwise reads `private/ntfy.conf`, which is gitignored. Tyrel's ruling: an ignored file
under `private/` is an acceptable home for it. The config is parsed as data and never sourced,
and only a regular file is read — a named pipe there would block forever inside the
`SessionStart` hook, which is a session that never starts rather than a ping that never
arrives. Delivery is fixed to `https://ntfy.sh`; the client refuses an ambient
`NTFY_SERVER` override so stale process state cannot redirect the bearer topic.

**Every event exits non-zero when delivery failed**, and prints one line beginning
`notify: NOT DELIVERED`. A real delivery prints nothing at all. So the exit status is evidence
for all four events, and a session that thinks it was heard cannot wait forever on a message
that was never sent.

`start` and `milestone` used to exit 0 even after printing that line, deliberately, so a
session could not die because a ping did not land. Two independent reviewers found the same
defect in it: a caller reading the status was told the phone had a message it never received,
and `milestone` is often the only announcement a long unattended run ever makes. The reason
the exit code was 0 is provided elsewhere — the `SessionStart` hook in `.claude/settings.json`
declares `"async": true`, so it runs detached and cannot block or fail the session whatever it
returns. Keeping a caller non-blocking is the caller's job; misreporting delivery to buy it
spent the one thing this script exists to protect.

The client requires Python 3 for in-memory JSON encoding and `curl` for HTTPS delivery.
