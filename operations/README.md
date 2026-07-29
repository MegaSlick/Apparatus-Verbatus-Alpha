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

**`start` and `milestone` exit 0 whether or not delivery succeeded, so their exit status says
nothing about the phone.** That is deliberate — a session must not die because a ping did not
land — but it means the status cannot be read as evidence. Read stderr instead: every failure
prints one line beginning `notify: NOT DELIVERED`, and a real delivery prints nothing at all.
`decision` and `done` exit non-zero on failure, because somebody is waiting on those and a
session that thinks it was heard will wait forever.

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
