"""Small shared predicates for sealed calibration provenance."""

from __future__ import annotations


def calibrated_claim_has_sample_evidence(
    calibrated_for_this_corpus: bool, sample_count: int | None
) -> bool:
    """A calibration claim with a published count needs at least one sample.

    ``None`` is deliberately distinct from zero: geometry provenance does not
    publish a count, so this predicate cannot invent one. Callers validate the
    boolean and count types before applying this semantic relation.
    """
    return not calibrated_for_this_corpus or sample_count is None or sample_count > 0
