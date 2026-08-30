"""Statically guard Unit 20's entire surface against picker idioms.

Dynamic fixtures cannot prove that selection vocabulary and positional or
extremal selectors are absent, so both owned production modules are scanned in
full.
"""

import ast
from pathlib import Path

import pytest

from common.test_unit19_no_picker import FORBIDDEN_CALLS, SHAPE_ONE_WORDS

# Comparability is included because preferring one capture could otherwise be
# disguised as declaring only that capture comparable.
SOURCES = (
    Path(__file__).with_name("reshoot_delta.py"),
    Path(__file__).with_name("capture_comparability.py"),
)


def _trees() -> list[ast.Module]:
    return [ast.parse(source.read_text(encoding="utf-8")) for source in SOURCES]


def test_the_forbidden_vocabularies_are_not_empty_so_the_scan_cannot_pass_vacuously():
    """A scan whose word lists have rotted away would pass by checking nothing."""
    assert SHAPE_ONE_WORDS
    assert FORBIDDEN_CALLS
    for source in SOURCES:
        assert source.is_file(), source
    # And the scan has a corpus with real names in it.
    for tree in _trees():
        assert any(isinstance(node, ast.Name) for node in ast.walk(tree))


@pytest.mark.parametrize("tree", _trees(), ids=[source.name for source in SOURCES])
def test_the_unit20_modules_name_no_shape_one_preference_word(tree: ast.Module):
    """§7 shape 1: no preference vocabulary anywhere in the Unit 20 surface."""
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            read.add(node.id)
        elif isinstance(node, ast.Attribute):
            read.add(node.attr)
        elif isinstance(node, ast.arg):
            read.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            read.add(node.value)
    # Fragment containment, lower-cased, exactly as the runtime preference
    # screens match: a helper named `preferred_view_for` or a constant
    # "best_capture_sha256" spells the forbidden idea without being a whole
    # member of the vocabulary. (Whole-string constants are still collected,
    # so a bare forbidden word is caught either way.)
    offences = sorted(
        f"{name!r} contains {word!r}"
        for name in read
        for word in SHAPE_ONE_WORDS
        if word.lower() in name.lower()
    )
    assert offences == [], offences


def _is_integer_literal(value: ast.AST) -> bool:
    # `x[-1]` parses as UnaryOp(USub, Constant(1)), not Constant(-1): a guard
    # matching only bare Constant ints would catch `x[0]` and wave through the
    # extremal "take the last one" this file exists to forbid.
    if isinstance(value, ast.Constant):
        return isinstance(value.value, int) and not isinstance(value.value, bool)
    return (
        isinstance(value, ast.UnaryOp)
        and isinstance(value.op, (ast.UAdd, ast.USub))
        and isinstance(value.operand, ast.Constant)
        and isinstance(value.operand.value, int)
        and not isinstance(value.operand.value, bool)
    )


def test_the_static_guard_sees_negative_positions_and_qualified_calls():
    synthetic = ast.parse("random.choice(rows)\nlast = rows[-1]\nfirst = rows[0]")
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(synthetic)
        if isinstance(node, ast.Call)
    }
    assert calls == {"choice"}
    positional = [
        node
        for node in ast.walk(synthetic)
        if isinstance(node, ast.Subscript) and _is_integer_literal(node.slice)
    ]
    assert sorted(ast.unparse(node) for node in positional) == ["rows[-1]", "rows[0]"]


@pytest.mark.parametrize("tree", _trees(), ids=[source.name for source in SOURCES])
def test_the_unit20_modules_call_no_forbidden_selector(tree: ast.Module):
    """§7 shapes 2, 3, 11: no positional, extremal, or arbitrary selection."""
    calls = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    assert not calls & FORBIDDEN_CALLS, sorted(calls & FORBIDDEN_CALLS)
    subscripts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript) and _is_integer_literal(node.slice)
    ]
    assert subscripts == [], [ast.unparse(node) for node in subscripts]
