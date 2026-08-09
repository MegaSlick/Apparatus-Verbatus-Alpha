"""The deliberately read-only boundary for a future annotation layer.

The annotation layer is not approved or wired into the pipeline.  This module
therefore defines only the narrow contract it may eventually occupy: it may
*read* an established Archetypus text, its hash, uncertainty/gap spans, and
layout anchors; it returns annotations keyed to that exact act and text hash.
It has no operation for creating, replacing, normalizing, publishing, or
otherwise emitting an established reading.

``canonical_clean_text`` is intentionally an input-only field.  It is the one
literal an annotator may inspect, but neither ``Annotation`` nor
``AnnotationResult`` has a field that can carry it back out.  Annotation kind
and attribute names are identifiers, not another reading of the act.

**The fields spec 11 names, in the shape it names them.** The spec asks a future
``annotator`` chair for ``act_type``, ``date`` (literal plus normalized),
``persons`` (literal name spans, roles), ``kinship`` edges and flags. Those are
here as a closed kind vocabulary with per-kind attributes rather than as free
text, which is what keeps the two requirements from fighting each other: the
spec needs a *normalized* date and a person's *role*, and this boundary needs no
unbounded string that a second transcription could ride in on. So every string an
annotation may carry is drawn from a closed set fixed in this file, except the
normalized date, which must match a strict ISO-8601 prefix form and can therefore
carry digits and hyphens and nothing else. A person is a span of the established
text plus a role from the closed set -- never a name this layer wrote down.

**The anchoring refusal is spec 11's test 7.** "A hallucinated person (not a span
of the text) is refused at the schema and recorded (annotations must anchor to
text spans)." ``verify_annotations_anchor_to_text`` is that refusal. It is
checkable today against synthetic text even though no annotator model exists to
produce a real annotation. *Recorded* is the half this build cannot finish: an
annotation refusal belongs in the export's ``refused-with-reason`` set, and the
terminal ledger has no annotation unit type because nothing produces annotations
to account for. That is named here rather than half-built.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Protocol, TypeAlias, runtime_checkable

AnnotationValue: TypeAlias = bool | int | None

ANNOTATION_KINDS: Final = frozenset({"act-type", "date", "person", "kinship", "flag"})

# An annotation vocabulary, not a definition of *act*. GLOSSARY.md refuses to define
# an act tightly on purpose -- "a narrow definition excludes material, and a missed act
# is worse than a poorly read one" -- so `other` is a first-class member here and an
# act whose type is not in this list is annotated `other` with its established text
# untouched. Widening the list is a code change Tyrel can rule on; nothing about the
# text depends on it.
ACT_TYPES: Final = frozenset(
    {"baptism", "marriage", "burial", "index-row", "letter", "note", "essay", "other"}
)
PERSON_ROLES: Final = frozenset(
    {
        "principal",
        "father",
        "mother",
        "spouse",
        "godparent",
        "godchild",
        "witness",
        "officiant",
        "other",
    }
)
KINSHIP_RELATIONS: Final = frozenset(
    {"parent", "child", "spouse", "sibling", "godparent", "godchild", "other"}
)
FLAG_KINDS: Final = frozenset(
    {"ambiguous-date", "ambiguous-identity", "damaged-context", "conflicting-witnesses", "other"}
)

# `YYYY`, `YYYY-MM` or `YYYY-MM-DD`. A register date that cannot be resolved to one of
# these is `None`, never an empty string -- Tyrel's 4c rule for `no_readable_text`
# applies to a normalized field for the same reason: an empty string is
# indistinguishable from a value that was lost.
_ISO_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


@dataclass(frozen=True)
class TextSpan:
    """A half-open character range in the established clean text."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if isinstance(self.start, bool) or not isinstance(self.start, int):
            raise TypeError("an annotation span start must be an integer")
        if isinstance(self.end, bool) or not isinstance(self.end, int):
            raise TypeError("an annotation span end must be an integer")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("an annotation span must be non-empty and non-negative")


@dataclass(frozen=True)
class LayoutAnchor:
    """The already-established image region corresponding to one text range."""

    region_id: str
    page_ordinal: int
    span: TextSpan

    def __post_init__(self) -> None:
        if not isinstance(self.region_id, str) or not self.region_id:
            raise ValueError("an annotation layout anchor needs a region identity")
        if (
            isinstance(self.page_ordinal, bool)
            or not isinstance(self.page_ordinal, int)
            or self.page_ordinal < 0
        ):
            raise ValueError("an annotation layout anchor needs a non-negative page ordinal")


@dataclass(frozen=True)
class AnnotationInput:
    """Read-only evidence supplied to a future annotation implementation."""

    act_id: str
    canonical_text_sha256: str
    canonical_clean_text: str
    uncertainty_spans: tuple[TextSpan, ...]
    gap_spans: tuple[TextSpan, ...]
    layout_anchors: tuple[LayoutAnchor, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.act_id, str) or not self.act_id:
            raise ValueError("an annotation input needs an act identity")
        if not _is_sha256(self.canonical_text_sha256):
            raise ValueError("an annotation input needs a lowercase canonical text sha256")
        if not isinstance(self.canonical_clean_text, str):
            raise TypeError("an annotation input needs the established clean text")
        _check_spans_within_text(
            self.canonical_clean_text,
            (*self.uncertainty_spans, *self.gap_spans),
        )
        for anchor in self.layout_anchors:
            _check_spans_within_text(self.canonical_clean_text, (anchor.span,))


@dataclass(frozen=True)
class AnnotationAttribute:
    """A non-text scalar fact attached to an annotation.

    Values deliberately exclude strings: a free-form string value would be an
    unbounded side channel for a second transcription.  Future approved
    annotation vocabularies can use the ``kind`` identifier plus bounded
    booleans and integers without carrying a competing reading.
    """

    name: str
    value: AnnotationValue

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("an annotation attribute needs a name")
        if isinstance(self.value, float) or not isinstance(self.value, (bool, int, type(None))):
            raise TypeError("an annotation attribute value must be a non-text scalar")


@dataclass(frozen=True)
class Annotation:
    """One structured annotation, anchored to positions rather than text.

    The per-kind fields are the ones spec 11 names. Each is legal only on its own
    kind and each draws from a closed set fixed in this module, so the whole record
    can be read as "which part of the established text, and which label from a list
    nobody can extend at run time". `overlaps_uncertainty` is the inheritance spec 11
    asks for -- annotations "inherit the text's uncertainty spans where they overlap"
    -- and `mark_uncertainty_overlap` is the one function that would ever set it.
    """

    annotation_id: str
    kind: str
    spans: tuple[TextSpan, ...]
    attributes: tuple[AnnotationAttribute, ...] = ()
    act_type: str | None = None
    role: str | None = None
    relation: str | None = None
    flag_kind: str | None = None
    normalized_date: str | None = None
    related_annotation_ids: tuple[str, ...] = ()
    overlaps_uncertainty: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.annotation_id, str)
            or not self.annotation_id
            or not isinstance(self.kind, str)
            or not self.kind
        ):
            raise ValueError("an annotation needs an identity and kind")
        if self.kind not in ANNOTATION_KINDS:
            raise ValueError(
                f"annotation kind {self.kind!r} is not one of {sorted(ANNOTATION_KINDS)}"
            )
        if not self.spans:
            raise ValueError("an annotation needs at least one anchored span")
        _require_closed_value(self, "act_type", "act-type", ACT_TYPES)
        _require_closed_value(self, "role", "person", PERSON_ROLES)
        _require_closed_value(self, "relation", "kinship", KINSHIP_RELATIONS)
        _require_closed_value(self, "flag_kind", "flag", FLAG_KINDS)
        if self.normalized_date is not None:
            if self.kind != "date":
                raise ValueError("only a date annotation may carry a normalized date")
            if not isinstance(self.normalized_date, str) or not _ISO_DATE.match(
                self.normalized_date
            ):
                raise ValueError(
                    "a normalized date must be YYYY, YYYY-MM or YYYY-MM-DD; an unresolvable "
                    "date is None, never an empty string"
                )
        if self.kind != "kinship" and self.related_annotation_ids:
            raise ValueError("only a kinship annotation relates other annotations")
        if self.kind == "kinship" and len(self.related_annotation_ids) != 2:
            raise ValueError("a kinship annotation relates exactly two person annotations")
        if not isinstance(self.overlaps_uncertainty, bool):
            raise TypeError("an annotation's uncertainty inheritance is a boolean")


def _require_closed_value(
    annotation: Annotation, field_name: str, kind: str, vocabulary: frozenset[str]
) -> None:
    """One closed vocabulary per kind, and no vocabulary on the wrong kind."""
    value = getattr(annotation, field_name)
    if value is None:
        if annotation.kind == kind:
            raise ValueError(f"a {kind} annotation needs its {field_name}")
        return
    if annotation.kind != kind:
        raise ValueError(f"only a {kind} annotation may carry a {field_name}")
    if value not in vocabulary:
        raise ValueError(f"{field_name} {value!r} is not one of {sorted(vocabulary)}")


def mark_uncertainty_overlap(span: TextSpan, uncertainty_spans: tuple[TextSpan, ...]) -> bool:
    """Whether one annotation span overlaps any uncertain range of the same text.

    Pure and generic: it knows two ranges and nothing about what an annotation is, so
    a future writer can hand it the input's own uncertainty spans without this module
    learning anything new. It is the only sanctioned way to set
    `Annotation.overlaps_uncertainty`, and it is unused in this build because the
    Archetypus record carries no uncertainty layer for anything to inherit from yet.
    """
    return any(
        uncertain.start < span.end and span.start < uncertain.end for uncertain in uncertainty_spans
    )


@dataclass(frozen=True)
class AnnotationProvenance:
    """The identity of the later annotation producer, never an act reading."""

    producer_id: str
    producer_revision: str
    configuration_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.producer_id, str)
            or not self.producer_id
            or not isinstance(self.producer_revision, str)
            or not self.producer_revision
        ):
            raise ValueError("annotation provenance needs a producer identity and revision")
        if not _is_sha256(self.configuration_sha256):
            raise ValueError("annotation provenance needs a lowercase configuration sha256")


@dataclass(frozen=True)
class AnnotationResult:
    """A text-free annotation result, bound to one established act reading."""

    act_id: str
    canonical_text_sha256: str
    annotations: tuple[Annotation, ...]
    provenance: AnnotationProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.act_id, str) or not self.act_id:
            raise ValueError("an annotation result needs an act identity")
        if not _is_sha256(self.canonical_text_sha256):
            raise ValueError("an annotation result needs a lowercase canonical text sha256")


@runtime_checkable
class AnnotationPort(Protocol):
    """The sole future entrypoint: inspect an input and return annotations."""

    def annotate(self, annotation_input: AnnotationInput) -> AnnotationResult:
        """Return structured annotations for the supplied established reading."""


def verify_annotation_binding(
    annotation_input: AnnotationInput,
    annotation_result: AnnotationResult,
) -> None:
    """Refuse annotations that claim a different act or established text.

    A caller uses this before projecting annotations.  It prevents a later
    annotator from accidentally attaching a valid-looking result to another act
    while preserving the contract's read-only direction.
    """

    if annotation_result.act_id != annotation_input.act_id:
        raise ValueError("annotation result is bound to a different act")
    if annotation_result.canonical_text_sha256 != annotation_input.canonical_text_sha256:
        raise ValueError("annotation result is bound to a different canonical text")


def verify_annotations_anchor_to_text(
    annotation_input: AnnotationInput,
    annotation_result: AnnotationResult,
) -> None:
    """Spec 11 test 7: an annotation that points at no real span of the text is refused.

    "A hallucinated person (not a span of the text) is refused at the schema and
    recorded (annotations must anchor to text spans)." A model misreading the ink is
    the Perlector's problem and not this check's; reporting a person, a date or a
    kinship edge that names no range of the established text at all is this check's,
    and it is refused rather than exported.

    A kinship edge is checked the same way one step out: it may only relate person
    annotations that are actually in this result, so an edge cannot introduce a
    person the text never anchored.
    """
    verify_annotation_binding(annotation_input, annotation_result)
    text_length = len(annotation_input.canonical_clean_text)
    person_ids = {
        annotation.annotation_id
        for annotation in annotation_result.annotations
        if annotation.kind == "person"
    }
    seen: set[str] = set()
    for annotation in annotation_result.annotations:
        if annotation.annotation_id in seen:
            raise ValueError(f"annotation {annotation.annotation_id!r} is reported twice")
        seen.add(annotation.annotation_id)
        for span in annotation.spans:
            if span.end > text_length:
                raise ValueError(
                    f"annotation {annotation.annotation_id!r} of kind {annotation.kind!r} "
                    f"names [{span.start}, {span.end}) in a text of length {text_length}; "
                    "an annotation may not point outside the text it was produced from"
                )
        for related in annotation.related_annotation_ids:
            if related not in person_ids:
                raise ValueError(
                    f"kinship annotation {annotation.annotation_id!r} relates "
                    f"{related!r}, which is not an anchored person in this result"
                )


def _check_spans_within_text(text: str, spans: tuple[TextSpan, ...]) -> None:
    for span in spans:
        if span.end > len(text):
            raise ValueError("an annotation span exceeds the established clean text")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
