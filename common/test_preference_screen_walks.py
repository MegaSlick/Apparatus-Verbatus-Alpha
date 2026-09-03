"""The no-picker screens are enumerated here, and none of them may recurse.

GOVERNANCE 3 is enforced at runtime by a family of walks that refuse a
preference-bearing field anywhere in a payload. They were converted to explicit
worklists one at a time, each conversion arguing the same case in its own
docstring: the value is untrusted or model-derived, so depth must cost the walk
its own list rather than the interpreter stack, and a `RecursionError` is a
crash naming neither the record nor the field.

The conversions were tracked as prose, and prose miscounted. A round of that
work reported "four preference screens, enumeration complete"; there were six.
`cross_capture_dissent._refuse_scalar_claim_keys` had already been converted and
was simply not listed, and `dossier.assert_no_order_bearing_field` was still
recursing -- missed because it lives in dossier assembly and is not *called* a
preference screen, while doing the same forbidden-vocabulary walk over a
structure carrying every Testimonium verbatim, on the production path, before
the digest is taken.

`physical_act_partition._refuse_textual` is listed below as a seventh entry. It
screens textual evidence rather than preference, so it is not one of the six --
but it is the same walk over the same untrusted payloads, it was converted in
the same round for the same reason, and a guard that watched its siblings and
not it would be drawing a line the defect does not respect.

So the list is here and it is mechanical. A screen added to the family and not
added below is not guarded by this file, which nothing can fix from inside a
test -- but a screen that is listed can never quietly go back to recursing, and
a listed name that stops existing fails loudly instead of silently guarding
nothing. That is the half worth automating.
"""

import ast
from pathlib import Path

import pytest

from common.contracts.errors import SchemaRefusal
from common.corpus_register import refuse_capture_preference

ROOT = Path(__file__).resolve().parent.parent

# Far past any interpreter's recursion allowance, so a screen that reaches the
# bottom of this proves it is not spending the interpreter stack.
PATHOLOGICAL_DEPTH = 1_000_000

# Every runtime screen standing over GOVERNANCE 3, as (file, function). Each
# walks a payload it does not control -- caller JSON, witness output, or a
# dossier carrying testimonia verbatim -- looking for a field that would name a
# preference among witnesses.
PREFERENCE_SCREENS = (
    # The reference implementation. Iterative from the start; the others were
    # converted to match it or delegate to it.
    ("common/corpus_register.py", "refuse_capture_preference"),
    ("common/physical_act_partition.py", "_refuse_preference"),
    ("common/physical_act_partition.py", "_refuse_textual"),
    ("common/cross_capture_autopsia.py", "_reject_preference"),
    ("common/cross_capture_dissent.py", "_refuse_scalar_claim_keys"),
    ("operations/operator/triage.py", "_refuse_preference_named"),
    # The one the prose enumeration missed.
    ("pipeline/4_perlector/dossier.py", "assert_no_order_bearing_field"),
)


def _function(relative_path: str, name: str) -> ast.FunctionDef:
    # Pinned encoding: the scanned files carry non-ASCII characters, and a guard
    # that cannot run on a non-UTF-8 locale is a failure, not a pass.
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{relative_path} no longer defines {name}; this guard is stale")


def _self_calls(function: ast.FunctionDef) -> list[int]:
    """Lines where the function calls itself, by bare name or through a module."""
    lines = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        if isinstance(called, ast.Name) and called.id == function.name:
            lines.append(node.lineno)
        elif isinstance(called, ast.Attribute) and called.attr == function.name:
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize(("relative_path", "name"), PREFERENCE_SCREENS)
def test_a_preference_screen_never_walks_the_interpreter_stack(relative_path, name):
    """A screen that recurses answers a deep payload with a `RecursionError`.

    That is a crash, not a refusal: it names neither the record that carried the
    forbidden field nor the field itself, which is the whole job of these
    functions. Every one of them screens its value *before* any shape check
    closes it, so the depth is the caller's to choose and not this build's.
    """
    function = _function(relative_path, name)
    self_calls = _self_calls(function)
    assert not self_calls, (
        f"{relative_path}::{name} calls itself at line(s) {self_calls}. A no-picker "
        "screen walks an explicit worklist so a deeply nested payload is refused by "
        "name; see common/corpus_register.py::refuse_capture_preference."
    )


def test_the_reference_screen_walks_a_pathological_payload_to_the_bottom():
    """The static guard above proves no screen calls itself. It cannot prove one
    reaches the bottom, so the implementation the others delegate to or were
    written to match is exercised at depth here.

    This pin was missing. `common/test_corpus_register.py` does carry a
    1,000,000-level case, and it reads like this one, but it is a pin on the
    *JSON parser*: `validate_register_bytes` refusing a register file whose
    brackets defeat `json.loads` before any walk begins. The walk itself --
    `refuse_capture_preference`, called on already-parsed values from six other
    modules -- was only ever exercised on shallow fixtures.
    """
    nested: object = {"leaf": 1}
    for _ in range(PATHOLOGICAL_DEPTH):
        nested = {"nested": [nested]}

    # Clean to the bottom: depth alone must not stop the screen.
    refuse_capture_preference(nested)
    del nested

    # An exact field name: this screen matches whole keys, not fragments.
    buried: object = {"preferred": "one of them"}
    for _ in range(PATHOLOGICAL_DEPTH):
        buried = {"nested": [buried]}
    with pytest.raises(SchemaRefusal, match="may not express capture preference"):
        refuse_capture_preference(buried)
