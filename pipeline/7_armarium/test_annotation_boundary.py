"""Tests for the deliberately non-writing annotation boundary.

This is a boundary test, not an annotator test: the annotation layer remains
unapproved and unconnected to every pipeline stage.  The AST guard checks the
module's public shape so a later convenience method cannot quietly add a
text-writing route beside the narrow ``annotate`` protocol.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
from pathlib import Path

import pytest
from annotation_boundary import (
    ACT_TYPES,
    ANNOTATION_KINDS,
    FLAG_KINDS,
    KINSHIP_RELATIONS,
    PERSON_ROLES,
    Annotation,
    AnnotationAttribute,
    AnnotationInput,
    AnnotationPort,
    AnnotationProvenance,
    AnnotationResult,
    GapAnchor,
    TextSpan,
    mark_uncertainty_overlap,
    verify_annotation_binding,
    verify_annotations_anchor_to_text,
)

MODULE_PATH = Path(__file__).with_name("annotation_boundary.py")
ROOT = Path(__file__).resolve().parents[2]
_FORBIDDEN_TEXT_WRITERS = frozenset(
    {
        "append_text",
        "establish_text",
        "open",
        "put_blob",
        "publish",
        "publish_text",
        "replace_text",
        "save",
        "set_text",
        "text_output",
        "write",
        "write_bytes",
        "write_text",
        "writelines",
    }
)
_FORBIDDEN_TEXT_RESULT_FIELDS = frozenset(
    {
        "canonical_clean_text",
        "canonical_text",
        "established_text",
        "literal_text",
        "reading",
        "replacement_text",
        "text",
    }
)
_FORBIDDEN_RUNTIME_IMPORT_PREFIXES = (
    "pipeline.",
    "common.runtree",
    "common.stage",
)
_TEXT_FREE_OUTPUT_CLASSES = frozenset(
    {
        "Annotation",
        "AnnotationAttribute",
        "AnnotationProvenance",
        "AnnotationResult",
    }
)


def _assert_read_only_annotation_boundary(source: str) -> None:
    """Assert that source offers no route to write or emit a literal reading."""

    tree = ast.parse(source)
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } | {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    forbidden = sorted((function_names | calls) & _FORBIDDEN_TEXT_WRITERS)
    assert not forbidden, f"annotation boundary exposes a text-writing route: {forbidden}"

    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    wired = sorted(
        imported for imported in imports if imported.startswith(_FORBIDDEN_RUNTIME_IMPORT_PREFIXES)
    )
    assert not wired, f"annotation boundary is wired into runtime stages: {wired}"

    output_classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in _TEXT_FREE_OUTPUT_CLASSES
    ]
    assert {node.name for node in output_classes} == _TEXT_FREE_OUTPUT_CLASSES
    for output_class in output_classes:
        output_fields = {
            statement.target.id
            for statement in output_class.body
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
        }
        unsafe_fields = sorted(output_fields & _FORBIDDEN_TEXT_RESULT_FIELDS)
        assert not unsafe_fields, f"{output_class.name} carries literal text: {unsafe_fields}"


INPUT_TEXT = "L'an mil sept cent"
TEXT = "L'an mil sept cent trente, Pierre fils de Jean Boucher"


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _input() -> AnnotationInput:
    return AnnotationInput(
        act_id="act-17",
        canonical_text_sha256=_text_hash(INPUT_TEXT),
        canonical_clean_text=INPUT_TEXT,
        uncertainty_spans=(TextSpan(0, 1),),
        gap_spans=(),
        layout_anchors=(),
    )


def _result(*, act_id: str = "act-17", text_hash: str | None = None) -> AnnotationResult:
    return AnnotationResult(
        act_id=act_id,
        canonical_text_sha256=text_hash or _text_hash(INPUT_TEXT),
        annotations=(
            Annotation("annotation-1", "act-type", (TextSpan(0, 1),), act_type="baptism"),
        ),
        provenance=AnnotationProvenance("future-annotator", "unapproved", "b" * 64),
    )


def test_annotation_boundary_has_no_text_writing_or_stage_wiring_api():
    _assert_read_only_annotation_boundary(MODULE_PATH.read_text(encoding="utf-8"))

    public_port_methods = {
        name
        for name, member in AnnotationPort.__dict__.items()
        if not name.startswith("_") and inspect.isfunction(member)
    }
    assert public_port_methods == {"annotate"}


def test_no_pipeline_program_wires_the_unapproved_annotation_boundary():
    programs = (
        ROOT / "pipeline" / "7_armarium" / "run.py",
        ROOT / "pipeline" / "orchestrator" / "run.py",
    )
    for program in programs:
        imports = _imports_in_source(program.read_text(encoding="utf-8"))
        assert "annotation_boundary" not in imports, (
            f"{program.relative_to(ROOT)} wired an annotation layer before architecture approval"
        )


def test_the_ast_guard_rejects_a_test_only_text_writer_control():
    control = """
def write_text(reading: str) -> None:
    return None
"""

    with pytest.raises(AssertionError, match="text-writing route"):
        _assert_read_only_annotation_boundary(control)


def test_annotation_result_has_no_literal_text_field():
    output_types = (Annotation, AnnotationAttribute, AnnotationProvenance, AnnotationResult)

    for output_type in output_types:
        result_fields = {field.name for field in dataclasses.fields(output_type)}
        assert not (result_fields & _FORBIDDEN_TEXT_RESULT_FIELDS)
    assert "canonical_clean_text" in AnnotationInput.__dataclass_fields__
    verify_annotation_binding(_input(), _result())


def test_annotations_are_bound_to_the_exact_act_and_established_text_hash():
    annotation_input = _input()

    verify_annotation_binding(annotation_input, _result())
    with pytest.raises(ValueError, match="different act"):
        verify_annotation_binding(annotation_input, _result(act_id="act-18"))
    with pytest.raises(ValueError, match="different canonical text"):
        verify_annotation_binding(annotation_input, _result(text_hash="c" * 64))


def test_annotation_input_hashes_the_text_it_actually_carries():
    with pytest.raises(ValueError, match="hash does not match"):
        AnnotationInput(
            act_id="act-17",
            canonical_text_sha256="a" * 64,
            canonical_clean_text=INPUT_TEXT,
            uncertainty_spans=(),
            gap_spans=(),
            layout_anchors=(),
        )


def test_annotation_gaps_are_zero_width_positions_not_text_ranges():
    AnnotationInput(
        act_id="act-17",
        canonical_text_sha256=_text_hash(INPUT_TEXT),
        canonical_clean_text=INPUT_TEXT,
        uncertainty_spans=(),
        gap_spans=(GapAnchor(len(INPUT_TEXT)),),
        layout_anchors=(),
    )
    with pytest.raises(ValueError, match="gap exceeds"):
        AnnotationInput(
            act_id="act-17",
            canonical_text_sha256=_text_hash(INPUT_TEXT),
            canonical_clean_text=INPUT_TEXT,
            uncertainty_spans=(),
            gap_spans=(GapAnchor(len(INPUT_TEXT) + 1),),
            layout_anchors=(),
        )


def _person(annotation_id: str, start: int, end: int, role: str = "principal") -> Annotation:
    return Annotation(annotation_id, "person", (TextSpan(start, end),), role=role)


def _anchored_result(*annotations: Annotation) -> AnnotationResult:
    return AnnotationResult(
        act_id="act-17",
        canonical_text_sha256=_text_hash(TEXT),
        annotations=annotations,
        provenance=AnnotationProvenance("future-annotator", "unapproved", "b" * 64),
    )


def _anchored_input(text: str = TEXT, uncertainty=()) -> AnnotationInput:
    return AnnotationInput(
        act_id="act-17",
        canonical_text_sha256=_text_hash(text),
        canonical_clean_text=text,
        uncertainty_spans=tuple(uncertainty),
        gap_spans=(),
        layout_anchors=(),
    )


def test_the_boundary_carries_the_five_fields_spec_11_names():
    """act_type, date (literal span plus normalized), persons with roles, kinship,
    flags -- each from a closed vocabulary rather than free text."""
    assert ANNOTATION_KINDS == {"act-type", "date", "person", "kinship", "flag"}

    pierre, jean = _person("p1", 27, 33), _person("p2", 44, 53, role="father")
    annotations = (
        Annotation("a1", "act-type", (TextSpan(0, len(TEXT)),), act_type="baptism"),
        Annotation("d1", "date", (TextSpan(0, 25),), normalized_date="1730"),
        pierre,
        jean,
        Annotation(
            "k1",
            "kinship",
            (TextSpan(27, 53),),
            relation="parent",
            related_annotation_ids=("p2", "p1"),
        ),
        Annotation("f1", "flag", (TextSpan(0, 5),), flag_kind="ambiguous-date"),
    )
    verify_annotations_anchor_to_text(_anchored_input(), _anchored_result(*annotations))


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"kind": "act-type"}, "needs its act_type"),
        ({"kind": "act-type", "act_type": "census"}, "is not one of"),
        ({"kind": "person"}, "needs its role"),
        ({"kind": "person", "role": "notary"}, "is not one of"),
        ({"kind": "date", "normalized_date": ""}, "YYYY"),
        ({"kind": "date", "normalized_date": "le 3 janvier"}, "YYYY"),
        ({"kind": "date", "normalized_date": "1730-99-99"}, "YYYY"),
        ({"kind": "date", "normalized_date": "1730-02-30"}, "YYYY"),
        ({"kind": "flag", "flag_kind": "looks-wrong"}, "is not one of"),
        (
            {"kind": "person", "role": "principal", "act_type": "baptism"},
            "only a act-type annotation",
        ),
        ({"kind": "kinship", "relation": "parent"}, "relates exactly two"),
    ],
)
def test_an_annotation_outside_the_closed_vocabulary_is_refused(kwargs, match):
    fields = {"annotation_id": "x", "spans": (TextSpan(0, 1),), **kwargs}
    with pytest.raises((ValueError, TypeError), match=match):
        Annotation(**fields)


def test_the_closed_vocabularies_all_admit_other_so_nothing_is_excluded():
    """GLOSSARY.md refuses to define an act tightly because a narrow definition
    excludes material. These are annotation labels, not definitions, and each keeps an
    `other` member so an unusual act is labelled rather than dropped."""
    for vocabulary in (ACT_TYPES, PERSON_ROLES, KINSHIP_RELATIONS, FLAG_KINDS):
        assert "other" in vocabulary


def test_a_person_who_is_not_a_span_of_the_text_is_refused_at_the_schema():
    """Spec 11 test 7. The refusal is the whole mechanism: an annotator may misread
    the ink -- that is the Perlector's problem -- but it may not report a person the
    established text has no room for."""
    with pytest.raises(ValueError, match="may not point outside the text"):
        verify_annotations_anchor_to_text(
            _anchored_input(), _anchored_result(_person("p1", 0, len(TEXT) + 1))
        )


def test_a_kinship_edge_cannot_introduce_an_unanchored_person():
    edge = Annotation(
        "k1",
        "kinship",
        (TextSpan(0, 5),),
        relation="parent",
        related_annotation_ids=("p1", "ghost"),
    )
    with pytest.raises(ValueError, match="not an anchored person"):
        verify_annotations_anchor_to_text(
            _anchored_input(), _anchored_result(_person("p1", 0, 5), edge)
        )


def test_one_annotation_identity_may_not_be_reported_twice():
    with pytest.raises(ValueError, match="reported twice"):
        verify_annotations_anchor_to_text(
            _anchored_input(), _anchored_result(_person("p1", 0, 5), _person("p1", 6, 9))
        )


def test_uncertainty_inheritance_is_an_overlap_of_ranges_and_nothing_else():
    """Spec 11: annotations "inherit the text's uncertainty spans where they overlap".
    Nothing in this build calls this: the Archetypus record carries no uncertainty
    layer for an annotation to inherit from yet."""
    uncertain = (TextSpan(10, 20),)
    assert mark_uncertainty_overlap(TextSpan(15, 25), uncertain)
    assert mark_uncertainty_overlap(TextSpan(0, 11), uncertain)
    assert not mark_uncertainty_overlap(TextSpan(0, 10), uncertain)
    assert not mark_uncertainty_overlap(TextSpan(20, 30), uncertain)


def test_uncertainty_inheritance_is_recomputed_not_trusted_from_the_result():
    uncertain = (TextSpan(10, 20),)
    overlapping = Annotation(
        "p1", "person", (TextSpan(15, 25),), role="principal", overlaps_uncertainty=True
    )
    verify_annotations_anchor_to_text(
        _anchored_input(uncertainty=uncertain), _anchored_result(overlapping)
    )

    for annotation in (
        Annotation("p1", "person", (TextSpan(15, 25),), role="principal"),
        Annotation("p1", "person", (TextSpan(0, 5),), role="principal", overlaps_uncertainty=True),
    ):
        with pytest.raises(ValueError, match="misstates its uncertainty overlap"):
            verify_annotations_anchor_to_text(
                _anchored_input(uncertainty=uncertain), _anchored_result(annotation)
            )


def _imports_in_source(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
