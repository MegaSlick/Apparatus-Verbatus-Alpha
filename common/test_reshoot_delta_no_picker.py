"""§7/§8.6 static guards for Unit 20's own consumer surface.

`common/reshoot_delta.py` is exercised by `common/test_reshoot_delta.py`'s
dynamic tests, but -- like Unit 19A before `common/test_unit19_no_picker.py`
landed, and like 19B/19C before `pipeline/test_unit19d_no_picker.py` closed
their own gap -- carried no source scan for the forbidden shapes §7 names as
binding review vocabulary. Dynamic tests prove the mechanism for the inputs
they construct; a source scan is what catches a selection idiom a future edit
introduces before any test happens to exercise it (see
`common/test_unit19_no_picker.py`'s own docstring: "neither is sufficient
alone").

The whole module is new Unit 20 surface with no unrelated legacy code to
false-positive on, so this scans the whole file rather than a named function
subset -- the same shape `common/test_unit19_no_picker.py` uses for
`physical_act_partition.py`.
"""

import ast
from pathlib import Path

import pytest

from common.test_unit19_no_picker import FORBIDDEN_CALLS, SHAPE_ONE_WORDS

# `capture_comparability.py` joins the scan for the same reason: it decides
# whether two captures are comparable, which is the nearest neighbour a
# preference could disguise itself as -- "this capture is the comparable one" is
# a picker with a euphemism for a name.
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
