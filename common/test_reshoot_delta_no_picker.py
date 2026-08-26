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
    return [ast.parse(source.read_text()) for source in SOURCES]


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
    assert not read & SHAPE_ONE_WORDS, sorted(read & SHAPE_ONE_WORDS)


@pytest.mark.parametrize("tree", _trees(), ids=[source.name for source in SOURCES])
def test_the_unit20_modules_call_no_forbidden_selector(tree: ast.Module):
    """§7 shapes 2, 3, 11: no positional, extremal, or arbitrary selection."""
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls & FORBIDDEN_CALLS, sorted(calls & FORBIDDEN_CALLS)
    subscripts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ]
    assert subscripts == [], [ast.unparse(node) for node in subscripts]
