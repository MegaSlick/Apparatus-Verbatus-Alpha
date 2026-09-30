"""The Perlector's planned reading time, from its slowest measured live call.

The slowest measured act call decoded 441 answer tokens in about 33 s beside
~6,500 prompt tokens on an 80 GB card, against a mean of 12 s. An act call is
planned above it, and a whole-page answer may run to the sealed page cap at
that decoding rate. A live pass refuses to start work its reading deadline
cannot hold at these rates.
"""

from __future__ import annotations

import math
from typing import Final

SLOWEST_MEASURED_CALL_SECONDS: Final = 33
SLOWEST_MEASURED_CALL_ANSWER_TOKENS: Final = 441
PLANNED_SECONDS_PER_CALL: Final = 40


def planned_seconds_per_page(page_max_tokens: int) -> int:
    """The planned time of one whole-page call whose answer may run to `page_max_tokens`."""
    return math.ceil(
        page_max_tokens * SLOWEST_MEASURED_CALL_SECONDS / SLOWEST_MEASURED_CALL_ANSWER_TOKENS
    )
