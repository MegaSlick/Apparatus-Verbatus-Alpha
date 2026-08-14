"""Guards against the identity-bearing-structure risk class named in HANDOFF.md.

Two hand-written constructions of one structure that then feeds an identity or
a comparison fail *silently*: the two copies diverge, the identity computed
from one no longer describes what the other builds, and neither function's code
looks wrong (`_crop_transform` and `_bounds_of` carry the specifics). So the
guard has to be over the file's actual source rather than over today's belief
about it.

A targeted regression guard for two known shapes, not a static analyzer for the
risk class across the pipeline -- other stages' files carry their own instances
this cannot reach and does not attempt to.
"""

import ast
from pathlib import Path

RUN_PY = Path(__file__).resolve().parent / "run.py"


def _dict_literals(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            yield node


def _string_keys(node: ast.Dict) -> list[str | None]:
    return [key.value if isinstance(key, ast.Constant) else None for key in node.keys]


def _count_crop_transform_literals(tree: ast.AST) -> int:
    """Dict literals shaped like a crop transform: `"operation"` mapped to `"crop"`."""
    count = 0
    for node in _dict_literals(tree):
        keys = _string_keys(node)
        if "operation" not in keys:
            continue
        value = node.values[keys.index("operation")]
        if isinstance(value, ast.Constant) and value.value == "crop":
            count += 1
    return count


def _count_xywh_dict_comprehensions(tree: ast.AST) -> int:
    """Dict comprehensions over exactly the four-key tuple `("x", "y", "w", "h")`."""
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.DictComp):
            continue
        if len(node.generators) != 1:
            continue
        iterable = node.generators[0].iter
        if not isinstance(iterable, ast.Tuple):
            continue
        values = [
            element.value if isinstance(element, ast.Constant) else None
            for element in iterable.elts
        ]
        if values == ["x", "y", "w", "h"]:
            count += 1
    return count


def test_exactly_one_crop_transform_literal_exists():
    """`_crop_transform` is the only place this shape may be built."""
    tree = ast.parse(RUN_PY.read_text())
    assert _count_crop_transform_literals(tree) == 1


def test_deleting_the_shared_transform_builder_is_caught():
    """Prove the guard above can go red, over a patched copy held in memory."""
    source = RUN_PY.read_text()
    reintroduced = source.replace(
        'transform = _crop_transform(act["page_ordinal"], page_record["subject_id"], bounds)',
        (
            'transform = {"operation": "crop", '
            '"source_page_ordinal": act["page_ordinal"], '
            '"source_page_id": page_record["subject_id"], '
            '"bounds": bounds}'
        ),
    )
    assert reintroduced != source, "the known call site to patch was not found; guard is stale"
    assert _count_crop_transform_literals(ast.parse(reintroduced)) == 2


def test_exactly_one_hand_built_xywh_comprehension_exists():
    """`_bounds_of` is the only reader of a fixture row's `x, y, w, h` fields."""
    tree = ast.parse(RUN_PY.read_text())
    assert _count_xywh_dict_comprehensions(tree) == 1


def test_deleting_the_shared_bounds_reader_is_caught():
    """Prove the guard above can go red: reintroduce a second hand-built comprehension."""
    source = RUN_PY.read_text()
    reintroduced = source.replace(
        "bounds = _bounds_of(recovery[0])",
        'bounds = {key: recovery[0][key] for key in ("x", "y", "w", "h")}',
    )
    assert reintroduced != source, "the known call site to patch was not found; guard is stale"
    assert _count_xywh_dict_comprehensions(ast.parse(reintroduced)) == 2
