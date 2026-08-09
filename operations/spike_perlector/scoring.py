"""Exact, predeclared CER/WER scoring with no implicit text normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from rapidfuzz.distance import Levenshtein

from .encoding import sha256_bytes
from .errors import MeasurementRefusal
from .models import OutputStatus
from .normalization import NormalizationProfile, character_units, normalize_text, word_units


@dataclass(frozen=True, slots=True)
class EditCounts:
    """Unit-cost Levenshtein operations, retained so every rate has a numerator."""

    matches: int
    substitutions: int
    insertions: int
    deletions: int

    def __post_init__(self) -> None:
        if min(self.matches, self.substitutions, self.insertions, self.deletions) < 0:
            raise MeasurementRefusal("edit counts may not be negative")

    @property
    def errors(self) -> int:
        return self.substitutions + self.insertions + self.deletions


@dataclass(frozen=True, slots=True)
class UnitScore:
    """One modality's reference-length edit rate for one act."""

    reference_units: int
    hypothesis_units: int
    edits: EditCounts

    def __post_init__(self) -> None:
        if self.reference_units <= 0:
            raise MeasurementRefusal("a zero-length checked reference is not a CER/WER denominator")
        if (
            self.edits.matches + self.edits.substitutions + self.edits.deletions
            != self.reference_units
        ):
            raise MeasurementRefusal("edit operations do not account for every reference unit")

    @property
    def rate(self) -> float:
        """Reference-length CER/WER; intentionally allowed to exceed 1.0."""

        return self.edits.errors / self.reference_units


@dataclass(frozen=True, slots=True)
class ActScore:
    """The candidate/witness score for a single planned act without raw text."""

    cer: UnitScore
    wer: UnitScore
    normalized_reference_sha256: str
    normalized_hypothesis_sha256: str


def _edit_counts(reference: Sequence[str], hypothesis: Sequence[str]) -> EditCounts:
    """Use RapidFuzz's pinned unit-cost edit script and verify its distance independently.

    ``processor=None`` is deliberate: all normalization is owned by
    ``normalization.py`` and is applied visibly before this call.
    """

    operations = Levenshtein.editops(reference, hypothesis, processor=None)
    substitutions = sum(operation.tag == "replace" for operation in operations)
    insertions = sum(operation.tag == "insert" for operation in operations)
    deletions = sum(operation.tag == "delete" for operation in operations)
    errors = substitutions + insertions + deletions
    distance = Levenshtein.distance(reference, hypothesis, weights=(1, 1, 1), processor=None)
    if errors != distance:
        raise MeasurementRefusal("RapidFuzz edit script and distance disagree")
    # matches is derived from which reference positions the edit script actually
    # touches, not from the substitution/deletion tag counts above: if editops
    # ever named the same reference position twice, those counts would overcount
    # it, and this is what would catch that -- not UnitScore.__post_init__'s own
    # arithmetic check, which holds by construction however matches is computed.
    touched_reference_positions = {
        operation.src_pos for operation in operations if operation.tag in ("replace", "delete")
    }
    if len(touched_reference_positions) != substitutions + deletions:
        raise MeasurementRefusal("RapidFuzz edit script touches one reference position twice")
    matches = len(reference) - len(touched_reference_positions)
    return EditCounts(
        matches=matches,
        substitutions=substitutions,
        insertions=insertions,
        deletions=deletions,
    )


def _score_units(reference: Sequence[str], hypothesis: Sequence[str]) -> UnitScore:
    if not reference:
        raise MeasurementRefusal("a blank checked reference is not scoreable by CER/WER")
    return UnitScore(
        reference_units=len(reference),
        hypothesis_units=len(hypothesis),
        edits=_edit_counts(reference, hypothesis),
    )


def hypothesis_for_status(status: OutputStatus, text: str | None) -> str:
    """Translate non-answers to an empty hypothesis, never an omitted measurement.

    A truncated response retains its partial text.  ``no_readable_text`` is a
    distinct positive response state, but like refused, missing, unavailable, and
    malformed it supplies an empty scoring hypothesis against checked readable ink.
    """

    if status in (OutputStatus.COMPLETE, OutputStatus.TRUNCATED):
        return text or ""
    return ""


def score_text(
    reference: str,
    hypothesis: str,
    *,
    profile: NormalizationProfile,
) -> ActScore:
    """Score one provided hypothesis after applying the same profile to both strings."""

    normalized_reference = normalize_text(reference, profile)
    normalized_hypothesis = normalize_text(hypothesis, profile)
    return ActScore(
        cer=_score_units(character_units(reference, profile), character_units(hypothesis, profile)),
        wer=_score_units(word_units(reference, profile), word_units(hypothesis, profile)),
        normalized_reference_sha256=sha256_bytes(normalized_reference.encode("utf-8")),
        normalized_hypothesis_sha256=sha256_bytes(normalized_hypothesis.encode("utf-8")),
    )


def score_response(
    reference: str,
    *,
    status: OutputStatus,
    text: str | None,
    profile: NormalizationProfile,
) -> ActScore:
    """Score an observed response state under the predeclared missing-output rule."""

    return score_text(reference, hypothesis_for_status(status, text), profile=profile)
