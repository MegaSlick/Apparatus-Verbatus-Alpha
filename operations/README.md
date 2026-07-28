# operations

Anything with a human, a machine, or money on the other end.

| Directory | What it is |
|---|---|
| `submit/` | the page where images are handed in |
| `pod/` | renting a machine, shutting it down, and **verifying** it shut down |
| `data/` | moving runs and exports between machines |
| `notify/` | the one-way line to Tyrel's phone |

There is no `local/`, `remote/`, or `deploy/` here. A pod runs the very same stage
directories this repository holds. Where code runs is an operational fact, not an
organising principle.

Governance 8 lives in `pod/`: a live pod needs Tyrel's explicit permission in that
session, and shutdown is verified against provider state and billing, never inferred
from an acknowledgement.
