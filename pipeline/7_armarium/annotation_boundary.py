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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias, runtime_checkable

AnnotationValue: TypeAlias = bool | int | None


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
    """One structured annotation, anchored to positions rather than text."""

    annotation_id: str
    kind: str
    spans: tuple[TextSpan, ...]
    attributes: tuple[AnnotationAttribute, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.annotation_id, str)
            or not self.annotation_id
            or not isinstance(self.kind, str)
            or not self.kind
        ):
            raise ValueError("an annotation needs an identity and kind")
        if not self.spans:
            raise ValueError("an annotation needs at least one anchored span")


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
