"""Turning two independent transcriptions into one checked reference.

The protocol document's section 5 states the human procedure: two qualified
people transcribe the Exemplar crop independently, a third adjudicates every
disagreement against the crop, and unread ink is marked as a gap rather than
guessed.  Lane B declared that procedure in prose and modelled only its digests;
lane A built it.  This module is lane A's implementation, grafted and adapted to
lane B's ``GroundTruth`` shape, because a procedure nothing executes is a
promise, and the whole point of the double-keying method is that no disagreement
is quietly dropped.

What makes it mechanical rather than a promise: ``disagreement_spans`` computes
the disputed spans from the two drafts, and ``reconcile`` uses that same
computation as the *required key set* for the adjudicator's resolutions.  A span
left unresolved, or a resolution for a span that does not exist, refuses.  Both
drafts are retained on the record unedited beside the reconciled reading --
GOVERNANCE 4, evidence is never overwritten -- and so are the resolutions, so the
adjudicator's decision at every disputed span survives alongside its outcome.

The method itself is standard rather than invented here: pairing two independent
annotators and resolving differences through a third, more experienced
adjudicator is the consistently recommended shape across digital-humanities and
OCR ground-truth work, and documents whose disagreement cannot be reconciled are
excluded from the gold standard rather than guessed into it.  Lane A recorded
that reading (2026-08-05) and this module keeps its conclusion:
``reconcile`` refuses rather than producing a blank checked reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Mapping

from .encoding import canonical_json_bytes, sha256_bytes
from .errors import AdjudicationRefusal
from .models import GapSpan, GroundTruth, ReferenceStatus

ILLEGIBLE = "ILLEGIBLE"
"""The one resolution that produces a gap instead of characters.

An adjudicator who cannot read a disputed span says so.  "We could not read it"
becomes a zero-width gap anchor, never a plausible reconstruction and never an
empty span that looks like agreement on nothing --
``TYREL_RULINGS_2026-08-05.md`` ruling 3, "we don't want it making shit up".
"""


@dataclass(frozen=True, slots=True)
class TranscriptionDraft:
    """One person's independent diplomatic transcription of one crop.

    The digest is of the *record* -- transcriber and text together -- not of the
    bare text.  Two people transcribing an easy act will produce byte-identical
    text, which is the good case; hashing bare text would make that
    indistinguishable from one draft counted twice, and ``GroundTruth`` refuses
    two equal draft digests.
    """

    transcriber_id: str
    text: str

    # disagreement_spans/_derive_adjudication run difflib.SequenceMatcher with
    # autojunk=False (deliberately: autojunk's heuristic would let it silently
    # ignore a heavily-repeated character run, exactly the kind of scribal
    # abbreviation or damage pattern this instrument must not treat as noise).
    # That is also what removes SequenceMatcher's own defence against its
    # quadratic worst case: two 40,000-character drafts built from a repetitive
    # pattern measured at over 100 seconds in one comparison. A single act's
    # diplomatic transcription -- an entry, or at most a short letter or essay
    # (GLOSSARY's "act") -- is never near this length; a draft this long is a
    # mis-pasted file, not a transcription, and is refused rather than hung on.
    MAX_TEXT_LENGTH = 20_000

    def __post_init__(self) -> None:
        if not isinstance(self.transcriber_id, str) or not self.transcriber_id.strip():
            raise AdjudicationRefusal("a transcription draft must name its transcriber")
        if not isinstance(self.text, str):
            raise AdjudicationRefusal("a transcription draft must carry Unicode text")
        if len(self.text) > self.MAX_TEXT_LENGTH:
            raise AdjudicationRefusal(
                f"a transcription draft of {len(self.text)} characters exceeds the "
                f"{self.MAX_TEXT_LENGTH}-character bound for one act; this is not a "
                "single act's diplomatic transcription"
            )

    def record(self) -> dict[str, str]:
        return {
            "schema": "spec05-transcription-draft.v1",
            "transcriber_id": self.transcriber_id,
            "text": self.text,
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.record()))


def _opcodes(first: str, second: str) -> list[tuple[str, int, int, int, int]]:
    """The one matcher construction every disagreement computation replays.

    ``autojunk=False`` is deliberate: its heuristic would silently treat a
    heavily-repeated character run as noise, exactly the kind of scribal
    abbreviation or damage pattern this instrument must not discount -- and is
    also what removes ``SequenceMatcher``'s own defence against its quadratic
    worst case (see ``TranscriptionDraft.MAX_TEXT_LENGTH``).  The bound is
    enforced here, not only at ``TranscriptionDraft`` construction, so every
    caller of this function is covered, including ``disagreement_spans``
    called directly on bare strings.
    """

    for text in (first, second):
        if len(text) > TranscriptionDraft.MAX_TEXT_LENGTH:
            raise AdjudicationRefusal(
                f"a text of {len(text)} characters exceeds the "
                f"{TranscriptionDraft.MAX_TEXT_LENGTH}-character bound for one act's "
                "diplomatic transcription"
            )
    return SequenceMatcher(None, first, second, autojunk=False).get_opcodes()


def _spans_from_opcodes(
    opcodes: Iterable[tuple[str, int, int, int, int]],
) -> tuple[tuple[int, int], ...]:
    """Every non-"equal" opcode's ``(start, end)`` offsets in the first draft, in order."""

    return tuple((i1, i2) for tag, i1, i2, _j1, _j2 in opcodes if tag != "equal")


def disagreement_spans(first: str, second: str) -> tuple[tuple[int, int], ...]:
    """Every span where two drafts disagree, in the first draft's own offsets.

    One ``(start, end)`` per non-"equal" opcode from ``_opcodes``, in order.
    Text present in the second draft but not the first is a zero-width span at
    the point in the first where it would have gone.

    ``SequenceMatcher`` is deterministic for a given pair of strings, which is
    what lets this double as the required key set for ``reconcile``: an
    adjudicator can recompute exactly the keys that will be demanded of them
    without running anything else -- ``_derive_adjudication`` calls this same
    ``_opcodes``/``_spans_from_opcodes`` pair rather than reimplementing the
    comparison, so that guarantee is enforced by sharing the one computation,
    not by keeping two in step by hand.
    """

    return _spans_from_opcodes(_opcodes(first, second))


def _derive_adjudication(
    *,
    first: TranscriptionDraft,
    second: TranscriptionDraft,
    adjudicator_id: str,
    resolutions: Mapping[tuple[int, int], str],
    reference_revision: str,
) -> tuple[tuple[tuple[tuple[int, int], str], ...], str, tuple[GapSpan, ...]]:
    """Validate and replay the one authoritative adjudication construction path."""

    if not isinstance(first, TranscriptionDraft) or not isinstance(second, TranscriptionDraft):
        raise AdjudicationRefusal("adjudication requires two TranscriptionDraft values")
    if not isinstance(adjudicator_id, str) or not adjudicator_id.strip():
        raise AdjudicationRefusal("adjudication requires a named adjudicator")
    if not isinstance(reference_revision, str) or not reference_revision.strip():
        raise AdjudicationRefusal("adjudication requires a reference revision to record")
    if first.transcriber_id == second.transcriber_id:
        raise AdjudicationRefusal(
            "the two drafts name the same transcriber; that is one person's reading twice, "
            "not two independent transcriptions"
        )
    if adjudicator_id in (first.transcriber_id, second.transcriber_id):
        raise AdjudicationRefusal(
            "the adjudicator is one of the two transcribers; a third person resolves the "
            "disagreement, or the resolution is just one draft winning"
        )

    opcodes = _opcodes(first.text, second.text)
    spans = _spans_from_opcodes(opcodes)
    if len(set(spans)) != len(spans):
        raise AdjudicationRefusal(
            "two disagreements share one span, so a resolution could not be attributed "
            "to either; this pair of drafts cannot be adjudicated span-by-span"
        )
    supplied = dict(resolutions)
    if set(supplied) != set(spans):
        missing = sorted(set(spans) - set(supplied))
        unexpected = sorted(set(supplied) - set(spans))
        raise AdjudicationRefusal(
            "resolutions must cover exactly the computed disagreement spans and nothing "
            f"else -- missing {missing}, unexpected {unexpected}"
        )
    for span, resolution in supplied.items():
        if not isinstance(resolution, str):
            raise AdjudicationRefusal(f"the resolution for span {span} is not text")

    pieces: list[str] = []
    gaps: list[GapSpan] = []
    written = 0
    for tag, i1, i2, _j1, _j2 in opcodes:
        if tag == "equal":
            pieces.append(first.text[i1:i2])
            written += i2 - i1
            continue
        resolution = supplied[(i1, i2)]
        if resolution == ILLEGIBLE:
            gaps.append(GapSpan(start=written, end=written))
            continue
        pieces.append(resolution)
        written += len(resolution)

    reconciled_text = "".join(pieces)
    if not reconciled_text.strip():
        raise AdjudicationRefusal(
            "nothing readable survived adjudication; an act nobody could read is "
            "unresolved_gap, recorded in the private sample accounting, never a checked "
            "reference with no ink in it"
        )
    return tuple(sorted(supplied.items())), reconciled_text, tuple(gaps)


@dataclass(frozen=True, slots=True)
class AdjudicationRecord:
    """One act's full adjudication trail, and the checked reference it produced."""

    first: TranscriptionDraft
    second: TranscriptionDraft
    adjudicator_id: str
    resolutions: tuple[tuple[tuple[int, int], str], ...]
    reference_revision: str
    reconciled_text: str
    gaps: tuple[GapSpan, ...]

    def __post_init__(self) -> None:
        expected = _derive_adjudication(
            first=self.first,
            second=self.second,
            adjudicator_id=self.adjudicator_id,
            resolutions=dict(self.resolutions),
            reference_revision=self.reference_revision,
        )
        if (self.resolutions, self.reconciled_text, self.gaps) != expected:
            raise AdjudicationRefusal(
                "AdjudicationRecord differs from the resolutions replayed against its drafts"
            )

    def record(self) -> dict[str, object]:
        """The exact private trail this record's digest is taken over.

        Both draft digests, the adjudicator, every resolution, the reconciled
        text and its gaps: change any one of them and the digest changes, so a
        reference revision cannot quietly acquire a different reading behind a
        digest that already travelled into a sealed manifest.
        """

        return {
            "schema": "spec05-adjudication.v1",
            "first_draft_sha256": self.first.digest,
            "second_draft_sha256": self.second.digest,
            "adjudicator_id": self.adjudicator_id,
            "reference_revision": self.reference_revision,
            "resolutions": [
                {"start": start, "end": end, "resolution": resolution}
                for (start, end), resolution in self.resolutions
            ],
            "reconciled_text": self.reconciled_text,
            "gaps": [gap.record() for gap in self.gaps],
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.record()))

    def ground_truth(self) -> GroundTruth:
        """The checked reference this adjudication established."""

        return GroundTruth(
            text=self.reconciled_text,
            adjudication_digest=self.digest,
            reference_revision=self.reference_revision,
            status=ReferenceStatus.CHECKED,
            independent_draft_sha256s=(self.first.digest, self.second.digest),
            gaps=self.gaps,
        )


def reconcile(
    *,
    first: TranscriptionDraft,
    second: TranscriptionDraft,
    adjudicator_id: str,
    resolutions: Mapping[tuple[int, int], str],
    reference_revision: str,
) -> AdjudicationRecord:
    """Reconcile two drafts into one adjudicated record, or refuse and say why.

    Walks the first draft left to right using the same opcodes computation
    ``disagreement_spans`` uses (the shared ``_opcodes`` helper, so the two can
    never silently diverge).  An "equal" block is copied verbatim -- both
    transcribers agree, so either source is correct.  A disputed block becomes
    its resolution's literal text, or, for ``ILLEGIBLE``, a zero-width
    ``GapSpan`` at that point and no characters at all.

    Refuses when a computed span has no resolution, when a resolution names a
    span that was not computed (most likely a stale input from a different pair
    of drafts), when the adjudicator is unnamed, or when nothing readable
    survives.  That last one is the important refusal: an act every part of which
    is illegible is ``ReferenceStatus.UNRESOLVED_GAP`` -- ink that exists and
    nobody could read -- and it is held out of the gold standard rather than
    forced into a checked reference with no ink in it.
    """

    canonical_resolutions, reconciled_text, gaps = _derive_adjudication(
        first=first,
        second=second,
        adjudicator_id=adjudicator_id,
        resolutions=resolutions,
        reference_revision=reference_revision,
    )

    return AdjudicationRecord(
        first=first,
        second=second,
        adjudicator_id=adjudicator_id,
        resolutions=canonical_resolutions,
        reference_revision=reference_revision,
        reconciled_text=reconciled_text,
        gaps=gaps,
    )
