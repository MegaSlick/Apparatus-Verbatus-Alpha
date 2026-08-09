"""Guards against the identity-bearing-structure risk class named in HANDOFF.md.

`recovery_pass` once hand-built its own copy of `cut_region`'s `transform` dict,
purely to predict a would-be duplicate's `region_id` before ever cutting a crop
(`_proposal_transform`'s docstring tells the story). A reconnaissance sweep for
other instances of the same class -- two independently hand-written
constructions of a structure that then feeds an identity or a comparison --
found a second one inside this stage's own file: three call sites once each
rebuilt a continuation or recovery rectangle from a fixture row's `x, y, w, h`
fields by hand (`_bounds_of`'s docstring tells that story).

Both are now single-construction: `_proposal_transform` and `_bounds_of` are
the only places these shapes are built. These tests are the mechanical proof
that stays true, over the file's actual source rather than over today's belief
about it -- a future edit that reintroduces a second hand-built copy fails one
of these rather than silently reopening the class of defect this file was
already bitten by once.

This is a targeted regression guard for the two instances this stage already
found and fixed, not a general static analyzer for the whole risk class across
the pipeline -- a reconnaissance sweep found several more instances in other
stages' files, which this test cannot reach and does not attempt to (see this
build's report).
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
    """`_proposal_transform` is the only place this shape may be built.

    Deleting `_proposal_transform` and inlining its body at both of its two
    call sites (as the file used to do) makes this count 2 and fails here --
    checked directly below rather than only asserted in prose.
    """
    tree = ast.parse(RUN_PY.read_text())
    assert _count_crop_transform_literals(tree) == 1


def test_deleting_the_shared_transform_builder_is_caught():
    """Prove the guard above can go red: reintroduce the historical duplicate.

    This does not touch the file on disk -- it parses a patched copy of the
    same source in memory, so the guard is proven against the exact defect it
    exists to catch without ever leaving `run.py` in a broken state.
    """
    source = RUN_PY.read_text()
    reintroduced = source.replace(
        'transform = _proposal_transform(act["page_ordinal"], page_record["subject_id"], bounds)',
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
    """`_bounds_of` is the only reader of a fixture row's `x, y, w, h` fields.

    Its own body is necessarily the one place this comprehension is written;
    every other call site uses `_bounds_of` rather than rebuilding it.
    """
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
