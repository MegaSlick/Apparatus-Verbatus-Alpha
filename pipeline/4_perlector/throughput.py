"""The Perlector's planned reading time: planning values, not measurements of a page.

The figures come from fifteen live act calls on an 80 GB card: the slowest
decoded 441 answer tokens in about 33 s beside ~6,500 prompt tokens, against a
mean of 12 s. An act call is planned above it. A whole-page call is planned as
its answer running to the sealed page cap at that call's overall rate; its
prompt (about 58,000 tokens for a dense page) is far larger than an act's, so
its prefill takes longer than the act call's did, and no page call has been
timed. A live pass refuses to start work its reading deadline cannot hold at
these rates.
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
