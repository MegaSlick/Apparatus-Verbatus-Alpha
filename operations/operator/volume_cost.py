"""What the network volume goes on costing after the pod is gone.

`close` prints this on **every** close, verified or not. The pod is the part an
operator watches stop; the volume is the part they forget, because the
expensive-looking thing has just visibly ended.

The close report already carries the volume's *observed* hourly rate, and that
figure stays the number this surface quotes. What is added here is the two facts
that make the figure matter, and they are quoted from RunPod's own documentation
rather than asserted:

- **"Data is retained when Pods terminate or Serverless workers scale to zero."**
  Closing the pod does not delete the volume.
- **"Storage charges continue to accrue while the Pod is stopped, so if your
  balance stays at $0 and these charges can't be covered, the network volume may
  eventually be terminated."** The storage meter does not stop when the compute
  meter does.

Both were read at `https://docs.runpod.io/storage/network-volumes` on
`CHECKED_ON` below. That is a page read on one day, not a live lookup and not
this account's bill — so the note's *primary* content is the instruction to look
in the console, and everything else is context around it. GOVERNANCE 10: claims
are made only about what was actually measured, and nothing here measures a
storage rate.

This deliberately quotes no per-GB figure. The published tiers exist (first 1 TB
and beyond, plus a high-performance class) and were read on the same day, but
which one applies depends on the volume's size and storage class — neither of
which this tool knows — and a per-GB-month figure printed beside a per-hour one
is two numbers in two units for a reader who wanted one. The console has both.
"""

from __future__ import annotations

from typing import Final

CHECKED_ON: Final = "2026-08-09"
"""The day the page below was actually fetched and read. Not a claim about today."""

SOURCE: Final = "https://docs.runpod.io/storage/network-volumes"

RETENTION_FACT: Final = "Data is retained when Pods terminate or Serverless workers scale to zero."
ACCRUAL_FACT: Final = (
    "Storage charges continue to accrue while the Pod is stopped, so if your balance stays "
    "at $0 and these charges can't be covered, the network volume may eventually be terminated."
)


def volume_cost_lines(*, volume_id: str | None, hourly_usd: str) -> list[str]:
    """The ongoing-cost note, as separate lines the operator can read one at a time.

    `hourly_usd` is the figure the close report observed, passed through as text
    so this module cannot round, restate or recompute it.
    """

    subject = f"the retained volume {volume_id}" if volume_id else "the retained volume"
    return [
        f"Closing the pod does not delete {subject}, and it keeps its own charge.",
        f"Its recorded ongoing price is ${hourly_usd} per hour.",
        (
            f'RunPod\'s own documentation says: "{RETENTION_FACT}" and '
            f'"{ACCRUAL_FACT}" '
            f"(read at {SOURCE} on {CHECKED_ON}; this tool does not re-check it)."
        ),
        (
            "For what it is actually costing you now, open the RunPod console and look at "
            "this volume's size and current rate. That is the only figure to trust."
        ),
        "Deleting a volume is a separate decision that this tool cannot make for you.",
    ]


__all__ = ["ACCRUAL_FACT", "CHECKED_ON", "RETENTION_FACT", "SOURCE", "volume_cost_lines"]
