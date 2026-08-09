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
from typing import Mapping

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

    def __post_init__(self) -> None:
        if not isinstance(self.transcriber_id, str) or not self.transcriber_id.strip():
            raise AdjudicationRefusal("a transcription draft must name its transcriber")
        if not isinstance(self.text, str):
            raise AdjudicationRefusal("a transcription draft must carry Unicode text")

    def record(self) -> dict[str, str]:
        return {
            "schema": "spec05-transcription-draft.v1",
            "transcriber_id": self.transcriber_id,
            "text": self.text,
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.record()))


def disagreement_spans(first: str, second: str) -> tuple[tuple[int, int], ...]:
    """Every span where two drafts disagree, in the first draft's own offsets.

    One ``(start, end)`` per non-"equal" opcode from
    ``difflib.SequenceMatcher.get_opcodes()``, in order.  Text present in the
    second draft but not the first is a zero-width span at the point in the first
    where it would have gone.

    ``SequenceMatcher`` is deterministic for a given pair of strings, which is
    what lets this double as the required key set for ``reconcile``: an
    adjudicator can recompute exactly the keys that will be demanded of them
    without running anything else.
    """

    matcher = SequenceMatcher(None, first, second, autojunk=False)
    return tuple((i1, i2) for tag, i1, i2, _j1, _j2 in matcher.get_opcodes() if tag != "equal")


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

    Walks the first draft left to right using the opcodes ``disagreement_spans``
    computed.  An "equal" block is copied verbatim -- both transcribers agree, so
    either source is correct.  A disputed block becomes its resolution's literal
    text, or, for ``ILLEGIBLE``, a zero-width ``GapSpan`` at that point and no
    characters at all.

    Refuses when a computed span has no resolution, when a resolution names a
    span that was not computed (most likely a stale input from a different pair
    of drafts), when the adjudicator is unnamed, or when nothing readable
    survives.  That last one is the important refusal: an act every part of which
    is illegible is ``ReferenceStatus.UNRESOLVED_GAP`` -- ink that exists and
    nobody could read -- and it is held out of the gold standard rather than
    forced into a checked reference with no ink in it.
    """

    if not isinstance(adjudicator_id, str) or not adjudicator_id.strip():
        raise AdjudicationRefusal("reconcile() requires a named adjudicator")
    if not isinstance(reference_revision, str) or not reference_revision.strip():
        raise AdjudicationRefusal("reconcile() requires a reference revision to record")
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

    matcher = SequenceMatcher(None, first.text, second.text, autojunk=False)
    opcodes = matcher.get_opcodes()
    spans = tuple((i1, i2) for tag, i1, i2, _j1, _j2 in opcodes if tag != "equal")
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

    return AdjudicationRecord(
        first=first,
        second=second,
        adjudicator_id=adjudicator_id,
        resolutions=tuple(sorted(supplied.items())),
        reference_revision=reference_revision,
        reconciled_text=reconciled_text,
        gaps=tuple(gaps),
    )
