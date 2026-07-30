# operations

Anything with a human, a machine, or money on the other end.

| Directory | Declared responsibility |
|---|---|
| `submit/` | the future page where images are handed in |
| `pod/` | future pod rental, shutdown, and provider-state/billing verification |
| `data/` | future movement of runs and exports between machines |
| `notify/` | the implemented, fileless one-way notification client |
| `codex/` | the implemented tracked-seat wrapper for time-bounded Codex calls |

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

`codex/seat.sh` pins a read-only evidence seat's requested model, effort, and elapsed-time
ceiling. Every seat runs at the repository root with closed stdin, ignores desktop model
defaults, and receives a short reminder that the main session owns scope and decisions. It
uses Codex's ephemeral mode so the CLI does not persist a second session record. It does
**not** cap tokens or cost, prove the runtime-resolved model, or retain the result. A caller
must preserve the raw output and account for the dispatch separately.
Live calls require the Codex CLI and GNU `timeout` (named `gtimeout` on a normal macOS
coreutils installation); stdin prompt intake also requires that timeout. A direct-prompt
dry run needs neither, and the wrapper refuses every path that would otherwise run uncapped.
