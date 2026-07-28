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

`notify/notify.sh` accepts its bearer topic only from the `NTFY_TOPIC` process environment
and refuses the retired `private/ntfy.conf` without reading it. It does not decide how the
topic is stored or injected; notifications remain off until Tyrel chooses that fileless
credential boundary. Delivery is fixed to `https://ntfy.sh`; the client refuses an ambient
`NTFY_SERVER` override so stale process state cannot redirect the bearer topic. Start and
milestone delivery failures are reported but do not fail their caller; decision and done
failures exit non-zero because somebody may be waiting. The client requires Python 3 for
in-memory JSON encoding and `curl` for HTTPS delivery.

`codex/seat.sh` pins a seat's requested model, effort, sandbox, work root, and elapsed-time
ceiling. It uses Codex's ephemeral mode so the CLI does not persist a second session record.
It does **not** cap tokens or cost, prove the runtime-resolved model, or retain the result.
A caller must preserve the raw output and account for the dispatch separately.
Live calls require the Codex CLI and GNU `timeout` (named `gtimeout` on a normal macOS
coreutils installation); stdin prompt intake also requires that timeout. A direct-prompt
dry run needs neither, and the wrapper refuses every path that would otherwise run uncapped.
