"""Tests for the deliberately non-writing annotation boundary.

This is a boundary test, not an annotator test: the annotation layer remains
unapproved and unconnected to every pipeline stage.  The AST guard checks the
module's public shape so a later convenience method cannot quietly add a
text-writing route beside the narrow ``annotate`` protocol.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest
from annotation_boundary import (
    Annotation,
    AnnotationAttribute,
    AnnotationInput,
    AnnotationPort,
    AnnotationProvenance,
    AnnotationResult,
    TextSpan,
    verify_annotation_binding,
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
        imported
        for imported in imports
        if imported.startswith(_FORBIDDEN_RUNTIME_IMPORT_PREFIXES)
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
        assert not unsafe_fields, (
            f"{output_class.name} carries literal text: {unsafe_fields}"
        )


def _input() -> AnnotationInput:
    return AnnotationInput(
        act_id="act-17",
        canonical_text_sha256="a" * 64,
        canonical_clean_text="L'an mil sept cent",
        uncertainty_spans=(TextSpan(0, 1),),
        gap_spans=(),
        layout_anchors=(),
    )


def _result(*, act_id: str = "act-17", text_hash: str = "a" * 64) -> AnnotationResult:
    return AnnotationResult(
        act_id=act_id,
        canonical_text_sha256=text_hash,
        annotations=(Annotation("annotation-1", "uncertain", (TextSpan(0, 1),)),),
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
    assert _result().canonical_text_sha256 == "a" * 64


def test_annotations_are_bound_to_the_exact_act_and_established_text_hash():
    annotation_input = _input()

    verify_annotation_binding(annotation_input, _result())
    with pytest.raises(ValueError, match="different act"):
        verify_annotation_binding(annotation_input, _result(act_id="act-18"))
    with pytest.raises(ValueError, match="different canonical text"):
        verify_annotation_binding(annotation_input, _result(text_hash="c" * 64))


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
