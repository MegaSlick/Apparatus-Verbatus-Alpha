"""The sealed, non-model policy for R5a's prior-draft protocol."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Final

from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.errors import ContractError

SELECTION_RULE: Final = "digest-threshold-over-frame-page-seed-act.v1"
PAGE_SHARED_PREFIX_POLICY: Final = "page-shared-prefix-first.v1"

# The neutral Pass-B form, verbatim from iterative_reader.md:49-50. This is the
# only text the pipeline puts in front of the reader *about* its own prior
# draft, and it is pinned here for the same reason the two names above are: a
# free-text field would leave GOVERNANCE 3 and GOVERNANCE 10's "the instrument
# may not constrain what it measures" enforced by nothing but a phrase blacklist.
# Measured before pinning: the blacklist below accepted "The prior reading
# contains errors. Find and fix them." (forces a change), "Trust the prior
# reading; reproduce it verbatim." (a picker instruction), and "Rate your
# confidence no higher than medium." (GOVERNANCE 10's own example of a budgeted
# confidence level). The fragment is also not a knob: `config/README.md`'s R5a
# toggle register lists `--draft-fed`, the instrument rate, and the selection-rule
# name, and deliberately not this. Rewording it is a reviewed two-file change,
# and the sealed bytes still ride on every record so a run says which form ran.
PASS_B_FRAGMENT: Final = (
    "This is a prior reading. It may be correct, incomplete, or wrong. Independently reread "
    "the image, preserve what the ink supports, and change only what the image justifies."
)
_FIELDS: Final = frozenset({"selection_rule", "page_shared_prefix_policy", "pass_b_fragment"})


def load(path: str | Path) -> tuple[dict[str, str], str]:
    """Read the exact policy bytes a Perlector pass will use."""
    try:
        raw = Path(path).read_bytes()
        record = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(
            f"the Perlector protocol declaration at {path} could not be read"
        ) from error
    if set(record) != _FIELDS or not all(isinstance(record[key], str) for key in _FIELDS):
        raise ContractError("the Perlector protocol declaration is not its closed string schema")
    if record["selection_rule"] != SELECTION_RULE:
        raise ContractError("the Perlector protocol declaration names an unknown selection rule")
    if record["page_shared_prefix_policy"] != PAGE_SHARED_PREFIX_POLICY:
        raise ContractError(
            "the Perlector protocol declaration names an unknown page-shared-prefix policy"
        )
    if not record["pass_b_fragment"].strip():
        raise ContractError("the Perlector protocol declaration has a blank Pass-B fragment")
    # Kept ahead of the equality check so the named constraint keeps its own
    # diagnosis: the declaration that trips this one is wrong for a stated
    # reason, not merely different from the pinned bytes.
    if "prior reading was wrong" in record["pass_b_fragment"].lower():
        raise ContractError(
            "the Pass-B fragment asserts that the prior was wrong; the protocol is neutral"
        )
    if record["pass_b_fragment"] != PASS_B_FRAGMENT:
        raise ContractError(
            "the Pass-B fragment is not the declared neutral form (iterative_reader.md:49-50); "
            "what this pipeline says to a reader about its own prior draft is not a free-text "
            "configuration field"
        )
    return record, digest_bytes(raw)


def is_control_sampled(
    act_id: str, *, frame_digest: str, page_digest: str, seed: str, per_mille: int
) -> bool:
    """Uniform digest threshold over run-stable corpus and act facts only.

    `int(digest[:8], 16) % 1000` draws from 2**32 = 4294967296 possible values,
    which is not a multiple of 1000 (4294967296 % 1000 == 296): the low 296
    thresholds each get one more input value than the other 704. The resulting
    bias is 1 part in about 4.29 million per threshold (~2.3e-5%) -- negligible
    at any corpus size this pipeline will ever sample, and recorded here rather
    than corrected with a rejection-sampling threshold because a threshold adds
    a retry path for a bias no real run could detect.
    """
    if not isinstance(per_mille, int) or isinstance(per_mille, bool) or not 0 <= per_mille <= 1000:
        raise ValueError(
            f"perlector_instrument_per_mille must be an integer in [0, 1000], got {per_mille!r}"
        )
    if per_mille == 0:
        return False
    digest = digest_of(
        {
            "purpose": "perlector-prior-control",
            "frame_digest": frame_digest,
            "page_digest": page_digest,
            "seed": seed,
            "act_id": act_id,
        }
    )
    return int(digest[:8], 16) % 1000 < per_mille
