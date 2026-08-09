"""The single candidate/dossier/Perlectio shape used by the fake harness.

Names and literal text in these values are private evidence.  They never belong in a
public history finding; ``redaction.py`` is the only public projection boundary.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .encoding import canonical_json_bytes, is_sha256, sha256_bytes
from .errors import MatrixRefusal, MeasurementRefusal
from .normalization import PROFILES, normalize_text


class Condition(StrEnum):
    """The three predeclared experimental conditions."""

    LECTIO_NUDA = "lectio_nuda"
    WITNESS_PRIMED = "witness_primed"
    IMAGE_ABSENT_CONTROL = "image_absent_control"


ALL_CONDITIONS: tuple[Condition, ...] = tuple(Condition)


class OutputStatus(StrEnum):
    """A response state is evidence, never an excuse to drop a planned cell."""

    COMPLETE = "complete"
    TRUNCATED = "truncated"
    NO_READABLE_TEXT = "no_readable_text"
    REFUSED = "refused"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"


class DeliveryMode(StrEnum):
    """Where an adapter would receive a dossier; no transport is implemented here."""

    LOCAL = "local"
    EXTERNAL = "external"


class MaterialClass(StrEnum):
    """The provenance class of image bytes at the execution boundary.

    A class rather than a real/fake boolean, because the sealed manifest binds a
    class and cannot bind a caller's opinion of its own bytes.
    """

    SYNTHETIC = "synthetic"
    CLEARED_PUBLIC = "cleared_public"
    PRIVATE_REGISTER = "private_register"


class ReferenceStatus(StrEnum):
    """Keep checked ink, a true blank, and unread ink structurally distinct."""

    CHECKED = "checked"
    NO_READABLE_TEXT = "no_readable_text"
    UNRESOLVED_GAP = "unresolved_gap"


# UAX #15's Stream-Safe Text Format caps a run of non-starters at 30, and this
# instrument adopts that cap for a measured reason: uniseg's grapheme
# segmentation is quadratic in the length of a *single* cluster. Measured
# 2026-08-09 against uniseg 0.10.1 -- one base character carrying 4,000
# combining marks segments in 5.9s and 8,000 in 23.5s, so MAX_TEXT_LENGTH alone
# would let 20 KB of vendor output cost minutes of CPU per scored cell. Real
# diplomatic transcription never stacks more than a handful of marks on one
# character; polytonic Greek reaches three.
MAX_COMBINING_RUN = 30


def require_measurable_text(value: str, field: str) -> None:
    """Refuse text this instrument can hold but cannot hash or segment.

    Both arrive from an ordinary vendor JSON body: ``json.loads`` decodes
    ``"\\ud800"`` into a perfectly valid ``str``, and a model may emit any number
    of combining marks.  Refused at the boundary where a response is built, so an
    adapter can record the predeclared ``malformed`` state for that cell;
    discovered later, inside a digest or a score, either one takes the whole
    matrix down with an error naming neither the act nor the reason.
    """

    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise MeasurementRefusal(
            f"{field} contains an unpaired surrogate, which no digest or comparison in "
            "this instrument can encode; that is a malformed response, not a reading"
        ) from error
    run = 0
    for character in value:
        if unicodedata.category(character).startswith("M"):
            run += 1
            if run > MAX_COMBINING_RUN:
                raise MeasurementRefusal(
                    f"{field} stacks more than {MAX_COMBINING_RUN} combining marks on one "
                    "character; no reading does that, and segmenting it is quadratic"
                )
        else:
            run = 0


def _require_nonempty(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MeasurementRefusal(f"{field} must be a non-empty string")
    require_measurable_text(value, field)


# The one text bound in this instrument, for every field that can reach a
# quadratic comparison: scoring.py's Levenshtein.editops is worse-than-linear in
# the product of its two input lengths, and adjudication.py's SequenceMatcher is
# quadratic outright on repetitive input. One act's diplomatic transcription --
# an entry, or at most a short letter or essay (GLOSSARY's "act") -- is never
# near this length; text this long is a mis-pasted file, not a reading.
MAX_TEXT_LENGTH = 20_000


def _require_status_conditioned_text(status: OutputStatus, text: str | None, label: str) -> None:
    """Text is present and non-blank exactly when status is complete or truncated.

    Every other status is a non-answer under the response-state table (README
    section 7).  Without this, a status the scorer treats as an empty hypothesis
    could still carry stray text into a dossier shown to a live candidate.
    """

    if status in (OutputStatus.COMPLETE, OutputStatus.TRUNCATED):
        if not isinstance(text, str) or not text.strip():
            raise MeasurementRefusal(f"a complete or truncated {label} must carry non-blank text")
        if len(text) > MAX_TEXT_LENGTH:
            raise MeasurementRefusal(
                f"a {label} of {len(text)} characters exceeds the {MAX_TEXT_LENGTH}-character "
                "bound for one act"
            )
        require_measurable_text(text, label)
    elif text is not None:
        raise MeasurementRefusal(f"a non-reading {label} carries status, not text")


def _require_finite_nonnegative_or_none(value: float | None, field: str, label: str) -> None:
    if value is not None and (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise MeasurementRefusal(f"{label} {field} must be finite, non-negative, or None")


def _text_lineage_sha256s(text: str) -> frozenset[str]:
    """Hash raw text and every closed evaluation-comparison representation.

    Binding the raw form alone would leave a caller free to tune on the
    normalized text this framework itself hands them.
    """

    return frozenset(
        {
            sha256_bytes(text.encode("utf-8")),
            *(
                sha256_bytes(normalize_text(text, profile).encode("utf-8"))
                for profile in PROFILES.values()
            ),
        }
    )


@dataclass(frozen=True, slots=True)
class GapSpan:
    """Ink the adjudicators could not read, marked where it sits in the reference.

    Offsets are character offsets into the *raw* checked text, never into a
    normalized or excised string, whose length shifts under normalization and
    would make a stored offset drift silently.

    ``start == end`` is a legitimate zero-width gap: unread ink at a single point
    with no characters of its own, which is what keeps "we could not read it"
    from decaying into "there was nothing to read".  A whole crop of unread ink
    is not a gap: it is ``ReferenceStatus.UNRESOLVED_GAP``, and it never becomes
    a checked reference.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        for name, value in (("start", self.start), ("end", self.end)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise MeasurementRefusal(f"a GapSpan {name} must be an integer")
        if self.start < 0 or self.end < self.start:
            raise MeasurementRefusal(
                f"a GapSpan must satisfy 0 <= start <= end, got ({self.start}, {self.end})"
            )

    def record(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}


def _validated_gaps(text: str, gaps: tuple[GapSpan, ...]) -> None:
    previous_end = 0
    for gap in gaps:
        if gap.start < previous_end:
            raise MeasurementRefusal(
                "gap spans must be supplied sorted and non-overlapping; "
                f"the span at {gap.start} starts before the previous one ended"
            )
        if gap.end > len(text):
            raise MeasurementRefusal(
                f"gap span ({gap.start}, {gap.end}) reaches past the reference it was "
                f"recorded against (length {len(text)})"
            )
        previous_end = gap.end


def excise_gaps(text: str, gaps: tuple[GapSpan, ...]) -> str:
    """The reference text CER/WER actually compares against, with unread ink removed.

    Unread reference characters do not enter the denominator.  Named limitation:
    this is a text-only exclusion, not a spatial alignment.  Candidate characters
    cannot be located at the gap and may align as edits elsewhere, so the scorer
    cannot determine whether they were produced *inside* it.  That is a harder
    problem this instrument does not solve.
    """

    if not gaps:
        return text
    pieces: list[str] = []
    cursor = 0
    for gap in gaps:
        pieces.append(text[cursor : gap.start])
        cursor = gap.end
    pieces.append(text[cursor:])
    return "".join(pieces)


@dataclass(frozen=True, slots=True)
class ResolvedIdentity:
    """The resolved candidate identity retained in private run evidence.

    ``public_slot`` is a non-identifying integer whose private mapping to this identity
    can be used to aggregate a public-safe finding.  It is not a ranking or a choice.
    """

    candidate_key: str
    public_slot: int
    source_ref: str
    revision: str
    artifact_digest: str
    delivery: DeliveryMode
    provider: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.candidate_key, "candidate_key")
        _require_nonempty(self.source_ref, "source_ref")
        _require_nonempty(self.revision, "revision")
        if not isinstance(self.public_slot, int) or isinstance(self.public_slot, bool):
            raise MeasurementRefusal("public_slot must be an integer")
        if self.public_slot < 1:
            raise MeasurementRefusal("public_slot must be positive")
        if not is_sha256(self.artifact_digest):
            raise MeasurementRefusal("artifact_digest must be a lowercase SHA-256")
        if not isinstance(self.delivery, DeliveryMode):
            raise MeasurementRefusal("delivery must be a DeliveryMode")
        if self.delivery is DeliveryMode.EXTERNAL or self.provider is not None:
            _require_nonempty(self.provider or "", "provider")

    def private_record(self) -> dict[str, object]:
        return {
            "candidate_key": self.candidate_key,
            "public_slot": self.public_slot,
            "source_ref": self.source_ref,
            "revision": self.revision,
            "artifact_digest": self.artifact_digest,
            "delivery": self.delivery.value,
            "provider": self.provider,
        }


@dataclass(frozen=True, slots=True)
class ImageEvidence:
    """The exact image payload shown to a candidate, retained only in memory.

    Neither the runner nor its public projector writes these bytes to disk; only
    the digests travel into a dossier or a record.
    """

    opaque_page_id: str
    sha256: str
    source_page_sha256: str
    material_class: MaterialClass
    payload: bytes

    def __post_init__(self) -> None:
        _require_nonempty(self.opaque_page_id, "opaque_page_id")
        if not is_sha256(self.source_page_sha256):
            raise MeasurementRefusal("image source_page_sha256 must be a lowercase SHA-256")
        if not isinstance(self.material_class, MaterialClass):
            raise MeasurementRefusal("image material_class must be a MaterialClass")
        if not isinstance(self.payload, bytes):
            raise MeasurementRefusal("image payload must be bytes")
        if sha256_bytes(self.payload) != self.sha256:
            raise MeasurementRefusal("image payload does not match its declared SHA-256")

    def private_reference(self) -> dict[str, str]:
        return {
            "opaque_page_id": self.opaque_page_id,
            "sha256": self.sha256,
            "source_page_sha256": self.source_page_sha256,
            "material_class": self.material_class.value,
        }


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """An independently adjudicated reference, with raw text kept private.

    A checked reference may carry gaps, and that is the common case rather than
    the edge one.  ``TYREL_RULINGS_2026-08-05.md`` ruling 3: "many of our records
    are damaged", so an act of three readable words and fifty unread spans is "a
    successful partial reading of three words plus honest gaps -- not a failure".
    Without ``gaps`` the only way to record that act is ``UNRESOLVED_GAP`` for the
    whole crop, which throws away the three words and the fifty positions alike.
    """

    text: str | None
    adjudication_digest: str
    reference_revision: str
    status: ReferenceStatus = ReferenceStatus.CHECKED
    independent_draft_sha256s: tuple[str, ...] = ()
    gaps: tuple[GapSpan, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, ReferenceStatus):
            raise MeasurementRefusal("ground-truth status must be a ReferenceStatus")
        if any(not isinstance(gap, GapSpan) for gap in self.gaps):
            raise MeasurementRefusal("ground-truth gaps must be GapSpan values")
        if self.status is ReferenceStatus.CHECKED:
            _require_nonempty(self.text or "", "ground-truth text")
            if len(self.text or "") > MAX_TEXT_LENGTH:
                raise MeasurementRefusal(
                    f"a ground-truth text of {len(self.text or '')} characters exceeds the "
                    f"{MAX_TEXT_LENGTH}-character bound for one act"
                )
            _validated_gaps(self.text or "", self.gaps)
            if not excise_gaps(self.text or "", self.gaps).strip():
                raise MeasurementRefusal(
                    "a checked reference whose every character is inside a gap has no ink "
                    "anyone read; it is unresolved_gap, not a checked reference"
                )
        else:
            if self.text is not None:
                raise MeasurementRefusal(
                    "no_readable_text and unresolved_gap carry no surrogate CER/WER text"
                )
            if self.gaps:
                raise MeasurementRefusal(
                    "a gap is a span inside a checked reference; an unread crop is a status"
                )
        if not is_sha256(self.adjudication_digest):
            raise MeasurementRefusal("ground-truth adjudication_digest must be a SHA-256")
        _require_nonempty(self.reference_revision, "ground-truth reference_revision")
        if len(self.independent_draft_sha256s) not in (0, 2) or any(
            not is_sha256(value) for value in self.independent_draft_sha256s
        ):
            raise MeasurementRefusal(
                "ground truth carries either no fixture drafts or exactly two independent draft digests"
            )
        # Sound only because these are digests of a draft *record* -- transcriber
        # plus text -- as `adjudication.py` builds them. Two people transcribing an
        # easy act produce byte-identical text, which is the good case; hashing
        # bare text could not tell it from one draft counted twice.
        if len(set(self.independent_draft_sha256s)) != len(self.independent_draft_sha256s):
            raise MeasurementRefusal(
                "ground-truth independent draft digests must differ; two identical draft "
                "records are one draft counted twice, not two independent transcriptions"
            )

    @property
    def scoreable_text(self) -> str:
        """The reference CER/WER compares against: checked text with its gaps removed."""

        if self.text is None:
            return ""
        return excise_gaps(self.text, self.gaps)

    def private_record(self) -> dict[str, object]:
        """Return the private reference record used to bind held-out evidence."""

        return {
            "text": self.text,
            "adjudication_digest": self.adjudication_digest,
            "reference_revision": self.reference_revision,
            "status": self.status.value,
            "independent_draft_sha256s": list(self.independent_draft_sha256s),
            "gaps": [gap.record() for gap in self.gaps],
        }

    @property
    def held_out_evidence_sha256s(self) -> frozenset[str]:
        """Every private reference representation that must stay out of later uses."""

        values = {
            self.adjudication_digest,
            *self.independent_draft_sha256s,
            sha256_bytes(canonical_json_bytes(self.private_record())),
        }
        if self.text is not None:
            values.update(_text_lineage_sha256s(self.text))
            # The gap-excised form is what a scorer, a bench, or a tuning caller
            # would actually hold, so it is bound too. Binding only the raw text
            # would leave the comparison form free to travel.
            values.update(_text_lineage_sha256s(self.scoreable_text))
        return frozenset(values)


@dataclass(frozen=True, slots=True)
class Testimonium:
    """One unverified witness report supplied only as evidence to a candidate."""

    private_source_id: str
    public_source_index: int
    text: str | None
    status: OutputStatus = OutputStatus.COMPLETE

    def __post_init__(self) -> None:
        _require_nonempty(self.private_source_id, "private_source_id")
        if not isinstance(self.public_source_index, int) or isinstance(
            self.public_source_index, bool
        ):
            raise MeasurementRefusal("public_source_index must be an integer")
        if self.public_source_index < 1:
            raise MeasurementRefusal("public_source_index must be positive")
        if not isinstance(self.status, OutputStatus):
            raise MeasurementRefusal("Testimonium status must be an OutputStatus")
        _require_status_conditioned_text(self.status, self.text, "Testimonium")

    def private_record(self) -> dict[str, object]:
        return {
            "private_source_id": self.private_source_id,
            "public_source_index": self.public_source_index,
            "text": self.text,
            "status": self.status.value,
        }

    @property
    def held_out_evidence_sha256s(self) -> frozenset[str]:
        """Bind both the exact witness record and its raw text if present."""

        values = {sha256_bytes(canonical_json_bytes(self.private_record()))}
        if self.text is not None:
            values.update(_text_lineage_sha256s(self.text))
        return frozenset(values)


@dataclass(frozen=True, slots=True)
class DossierTestimonium:
    """Anonymous Testimonium view that is safe to hand to a candidate.

    A candidate may see a stable anonymous slot, literal testimony, and its response
    state.  It must not receive the private Attestator identity: source names in a
    prompt would invite a learned trust preference and recreate a picker at the input
    boundary.
    """

    public_source_index: int
    text: str | None
    status: OutputStatus

    def __post_init__(self) -> None:
        if not isinstance(self.public_source_index, int) or isinstance(
            self.public_source_index, bool
        ):
            raise MeasurementRefusal("dossier witness index must be an integer")
        if self.public_source_index < 1:
            raise MeasurementRefusal("dossier witness index must be positive")
        if not isinstance(self.status, OutputStatus):
            raise MeasurementRefusal("dossier testimony status must be an OutputStatus")
        _require_status_conditioned_text(self.status, self.text, "dossier testimony")

    def wire_record(self) -> dict[str, object]:
        """Return the anonymous testimony record delivered in a common dossier."""

        return {
            "public_source_index": self.public_source_index,
            "text": self.text,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class WitnessSource:
    """One sealed Attestator source identity, retained only in private evidence."""

    private_source_id: str
    public_source_index: int
    source_ref: str
    revision: str
    artifact_digest: str

    def __post_init__(self) -> None:
        _require_nonempty(self.private_source_id, "private witness source ID")
        if not isinstance(self.public_source_index, int) or isinstance(
            self.public_source_index, bool
        ):
            raise MeasurementRefusal("public witness source index must be an integer")
        if self.public_source_index < 1:
            raise MeasurementRefusal("public witness source index must be positive")
        _require_nonempty(self.source_ref, "witness source_ref")
        _require_nonempty(self.revision, "witness revision")
        if not is_sha256(self.artifact_digest):
            raise MeasurementRefusal("witness artifact_digest must be a lowercase SHA-256")

    def private_record(self) -> dict[str, object]:
        return {
            "private_source_id": self.private_source_id,
            "public_source_index": self.public_source_index,
            "source_ref": self.source_ref,
            "revision": self.revision,
            "artifact_digest": self.artifact_digest,
        }


@dataclass(frozen=True, slots=True)
class WitnessConfiguration:
    """The approved interim witness configuration, fixed across every act."""

    sources: tuple[WitnessSource, ...]
    approval_evidence_sha256: str

    def __post_init__(self) -> None:
        if not self.sources:
            raise MeasurementRefusal("witness configuration may not be empty")
        indices = [source.public_source_index for source in self.sources]
        source_ids = [source.private_source_id for source in self.sources]
        if len(set(indices)) != len(indices) or len(set(source_ids)) != len(source_ids):
            raise MeasurementRefusal("witness configuration repeats a source identity or index")
        if not is_sha256(self.approval_evidence_sha256):
            raise MeasurementRefusal("witness configuration needs an approval evidence SHA-256")

    def require_act(self, act: "EvaluationAct") -> None:
        actual = tuple(
            (item.private_source_id, item.public_source_index) for item in act.testimonia
        )
        expected = tuple(
            (source.private_source_id, source.public_source_index) for source in self.sources
        )
        if actual != expected:
            raise MatrixRefusal(
                "act Testimonia differ from the sealed witness configuration or order"
            )

    def require_distinct_from_candidates(self, identities: tuple[ResolvedIdentity, ...]) -> None:
        """Refuse self-witness overlap by source or exact model artifact."""

        witness_artifacts = {source.artifact_digest for source in self.sources}
        witness_sources = {source.source_ref for source in self.sources}
        for identity in identities:
            if (
                identity.artifact_digest in witness_artifacts
                or identity.source_ref in witness_sources
            ):
                raise MatrixRefusal(
                    "a Perlector candidate shares an Attestator source or artifact; "
                    "self-witness agreement is not evidence"
                )

    def private_record(self) -> dict[str, object]:
        return {
            "schema": "spec05-witness-configuration.v1",
            "sources": [source.private_record() for source in self.sources],
            "approval_evidence_sha256": self.approval_evidence_sha256,
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.private_record()))


@dataclass(frozen=True, slots=True)
class EvaluationAct:
    """One already-selected act, not a selector over a wider population."""

    opaque_act_id: str
    image: ImageEvidence
    ground_truth: GroundTruth
    testimonia: tuple[Testimonium, ...]

    def __post_init__(self) -> None:
        _require_nonempty(self.opaque_act_id, "opaque_act_id")
        if not self.testimonia:
            raise MatrixRefusal("an evaluation act has no Testimonia for primed/control cells")
        source_indices = [item.public_source_index for item in self.testimonia]
        if len(set(source_indices)) != len(source_indices):
            raise MatrixRefusal("an evaluation act repeats a public Testimonium source index")

    @property
    def private_evidence_sha256s(self) -> frozenset[str]:
        """Hashes of all non-image evaluation evidence, retained only privately."""

        values = set(self.ground_truth.held_out_evidence_sha256s)
        for testimonium in self.testimonia:
            values.update(testimonium.held_out_evidence_sha256s)
        return frozenset(values)

    def manifest_binding(
        self,
    ) -> tuple[str, str, str, MaterialClass, ReferenceStatus, tuple[str, ...]]:
        """Return the complete non-literal binding a sealed manifest must match."""

        return (
            self.opaque_act_id,
            self.image.source_page_sha256,
            self.image.sha256,
            self.image.material_class,
            self.ground_truth.status,
            tuple(sorted(self.private_evidence_sha256s)),
        )


@dataclass(frozen=True, slots=True)
class Dossier:
    """The identical semantic input given to every candidate for one planned cell."""

    opaque_act_id: str
    condition: Condition
    image: ImageEvidence | None
    testimonia: tuple[DossierTestimonium, ...]

    def __post_init__(self) -> None:
        _require_nonempty(self.opaque_act_id, "dossier opaque_act_id")
        if not isinstance(self.condition, Condition):
            raise MatrixRefusal("dossier condition must be a Condition")
        if self.condition in (Condition.LECTIO_NUDA, Condition.WITNESS_PRIMED):
            if self.image is None:
                raise MatrixRefusal(f"{self.condition.value} requires an image")
        elif self.image is not None:
            raise MatrixRefusal("image-absent control may not carry an image")
        if self.condition is Condition.LECTIO_NUDA:
            if self.testimonia:
                raise MatrixRefusal("Lectio nuda may not carry Testimonia")
        elif not self.testimonia:
            raise MatrixRefusal(f"{self.condition.value} requires Testimonia")

    def private_wire_record(self) -> dict[str, object]:
        """Return the byte-stable semantic message, never a public record."""

        return {
            "schema": "perlector-dossier.v1",
            "opaque_act_id": self.opaque_act_id,
            "condition": self.condition.value,
            "image": self.image.private_reference() if self.image else None,
            "testimonia": [item.wire_record() for item in self.testimonia],
        }

    @property
    def wire_bytes(self) -> bytes:
        return canonical_json_bytes(self.private_wire_record())

    @property
    def wire_sha256(self) -> str:
        return sha256_bytes(self.wire_bytes)


def anonymous_testimonia(act: EvaluationAct) -> tuple[DossierTestimonium, ...]:
    """One act's Testimonia in sealed order, stripped of their Attestator identities.

    The same view is what a primed candidate is shown and what a Lectio nuda
    reading is later compared against, so it is built here once rather than
    spelled out at each of those boundaries.
    """

    return tuple(
        DossierTestimonium(
            public_source_index=item.public_source_index,
            text=item.text,
            status=item.status,
        )
        for item in act.testimonia
    )


def dossier_for(act: EvaluationAct, condition: Condition) -> Dossier:
    """Build a condition solely by presence/absence of the same act evidence."""

    if condition is Condition.LECTIO_NUDA:
        return Dossier(act.opaque_act_id, condition, act.image, ())
    if condition is Condition.WITNESS_PRIMED:
        return Dossier(act.opaque_act_id, condition, act.image, anonymous_testimonia(act))
    if condition is Condition.IMAGE_ABSENT_CONTROL:
        return Dossier(act.opaque_act_id, condition, None, anonymous_testimonia(act))
    raise MatrixRefusal(f"unknown condition {condition!r}")


@dataclass(frozen=True, slots=True)
class CandidateRequest:
    """What a generic adapter receives; exact prompt bytes travel independently."""

    dossier: Dossier
    prompt_format_bytes: bytes
    prompt_format_sha256: str

    def __post_init__(self) -> None:
        if sha256_bytes(self.prompt_format_bytes) != self.prompt_format_sha256:
            raise MeasurementRefusal("CandidateRequest prompt bytes do not match their digest")

    @property
    def delivery_bytes(self) -> bytes:
        """The exact binary envelope the adapter is handed for this cell.

        A length-prefixed encoding preserves the declared prompt-format bytes
        byte-for-byte while binding them to the exact common dossier bytes.  It
        deliberately does not invent a candidate-specific chat renderer: that
        renderer is part of the candidate-owned declared format evidence.
        """

        dossier_bytes = self.dossier.wire_bytes
        return (
            len(self.prompt_format_bytes).to_bytes(8, "big")
            + self.prompt_format_bytes
            + len(dossier_bytes).to_bytes(8, "big")
            + dossier_bytes
        )

    @property
    def delivery_sha256(self) -> str:
        return sha256_bytes(self.delivery_bytes)


@dataclass(frozen=True, slots=True)
class CandidateResponse:
    """An adapter observation, not a claim that a model read correctly."""

    status: OutputStatus
    text: str | None
    elapsed_ms: float | None
    cost_usd: float | None
    observed_prompt_sha256: str
    observed_dossier_sha256: str
    observed_delivery_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, OutputStatus):
            raise MeasurementRefusal("candidate response status must be an OutputStatus")
        _require_status_conditioned_text(self.status, self.text, "candidate response")
        for field, value in (("elapsed_ms", self.elapsed_ms), ("cost_usd", self.cost_usd)):
            _require_finite_nonnegative_or_none(value, field, "candidate response")
        for name, value in (
            ("prompt", self.observed_prompt_sha256),
            ("dossier", self.observed_dossier_sha256),
            ("delivery", self.observed_delivery_sha256),
        ):
            if not is_sha256(value):
                raise MeasurementRefusal(f"candidate response has no valid observed {name} digest")


@runtime_checkable
class Candidate(Protocol):
    """One interface for the stock model, vendor model, and checkpoint alike."""

    @property
    def identity(self) -> ResolvedIdentity:
        """Return the already-resolved identity that will be retained per run."""

    def read(self, request: CandidateRequest) -> CandidateResponse:
        """Return one response to one dossier without selecting witness content."""


@dataclass(frozen=True, slots=True)
class DissentSummary:
    """Structural comparison facts; they are never a quality score or preference."""

    compared: int
    departed: int
    unavailable: int

    def __post_init__(self) -> None:
        if min(self.compared, self.departed, self.unavailable) < 0:
            raise MeasurementRefusal("dissent counts may not be negative")
        if self.departed > self.compared:
            raise MeasurementRefusal("dissent departures may not exceed comparisons")


@dataclass(frozen=True, slots=True)
class Perlectio:
    """The one private output shape produced for every candidate and condition."""

    identity: ResolvedIdentity
    opaque_act_id: str
    condition: Condition
    status: OutputStatus
    text: str | None
    dossier_sha256: str
    prompt_format_sha256: str
    delivery_sha256: str
    image_present: bool
    testimonia_count: int
    dissent: DissentSummary
    elapsed_ms: float | None
    cost_usd: float | None

    def __post_init__(self) -> None:
        _require_nonempty(self.opaque_act_id, "Perlectio opaque_act_id")
        # public_slot travels from here into the public aggregation, so this has
        # to be a checked identity rather than anything that answers to the name.
        if not isinstance(self.identity, ResolvedIdentity):
            raise MeasurementRefusal("Perlectio must carry a checked ResolvedIdentity")
        if not isinstance(self.condition, Condition):
            raise MeasurementRefusal("Perlectio condition must be a Condition")
        if not isinstance(self.status, OutputStatus):
            raise MeasurementRefusal("Perlectio status must be an OutputStatus")
        _require_status_conditioned_text(self.status, self.text, "Perlectio")
        if not (
            is_sha256(self.dossier_sha256)
            and is_sha256(self.prompt_format_sha256)
            and is_sha256(self.delivery_sha256)
        ):
            raise MeasurementRefusal(
                "Perlectio must retain dossier, prompt, and delivery SHA-256 values"
            )
        if self.condition in (Condition.LECTIO_NUDA, Condition.WITNESS_PRIMED):
            if not self.image_present:
                raise MeasurementRefusal(
                    f"{self.condition.value} Perlectio claims no image was present"
                )
        elif self.image_present:
            raise MeasurementRefusal("image-absent Perlectio claims an image was present")
        if self.condition is Condition.LECTIO_NUDA:
            if self.testimonia_count:
                raise MeasurementRefusal("Lectio nuda Perlectio claims it saw Testimonia")
        elif not self.testimonia_count:
            raise MeasurementRefusal(
                f"{self.condition.value} Perlectio claims it saw no Testimonia"
            )
        for field, value in (("elapsed_ms", self.elapsed_ms), ("cost_usd", self.cost_usd)):
            _require_finite_nonnegative_or_none(value, field, "Perlectio")
