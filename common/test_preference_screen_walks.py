"""The no-picker screens are enumerated here, and none of them may recurse.

That no step picks among witnesses is enforced at runtime by a family of walks
that refuse a preference-bearing field anywhere in a payload. They were converted to explicit
worklists one at a time, each conversion arguing the same case in its own
docstring: the value is untrusted or model-derived, so depth must cost the walk
its own list rather than the interpreter stack, and a `RecursionError` is a
crash naming neither the record nor the field.

The conversions were tracked as prose, and prose miscounted:
`dossier.assert_no_order_bearing_field` was still recursing -- missed because it
lives in dossier assembly and is not *called* a preference screen, while doing
the same forbidden-vocabulary walk over a structure carrying every Testimonium
verbatim, on the production path, before the digest is taken.

So the list is here and it is mechanical. A screen added to the family and not
added below is not guarded by this file, which nothing can fix from inside a
test -- but a screen that is listed can never quietly go back to recursing, and
a listed name that stops existing fails loudly instead of silently guarding
nothing. That is the half worth automating.
"""

import ast
import contextlib
import functools
import re
import signal
from pathlib import Path

import pytest

from common.contracts.errors import ContractError, SchemaRefusal
from common.corpus_register import refuse_capture_preference
from conftest import load_stage
from operations.operator import triage
from operations.operator.triage import TriageRefusal

ROOT = Path(__file__).resolve().parent.parent

# Far past any interpreter's recursion allowance, so a screen that reaches the
# bottom of this proves it is not spending the interpreter stack.
PATHOLOGICAL_DEPTH = 1_000_000

# The depth the whole family is driven at: 200 times the interpreter's
# configured recursion limit, and 20 times the deepest C-encoder allowance this
# repository has measured (`common/contracts/test_contracts_records.py` records
# roughly 9,997 levels), so a screen that reached the bottom of this is not
# spending the stack. A fifth of PATHOLOGICAL_DEPTH, because each screen
# driven twice each at a million levels is a minute of gate time to re-prove
# what the first hundred thousand already proved; the reference screen keeps its
# million-level case above.
FAMILY_DEPTH = 200_000


@functools.cache
def _dossier():
    """Loaded on first use, so the pure AST tests here do not pay for PIL at import."""
    return load_stage("4_perlector", "dossier")


# Every runtime screen standing over the rule that no step may pick among
# witnesses, as (file, function). Each
# walks a payload it does not control -- caller JSON, witness output, or a
# dossier carrying testimonia verbatim -- looking for a field that would name a
# preference among witnesses.
PREFERENCE_SCREENS = (
    # The reference implementation. Iterative from the start; the others were
    # converted to match it or delegate to it.
    ("common/corpus_register.py", "refuse_capture_preference"),
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
    `refuse_capture_preference`, called on already-parsed values from other
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


# The family driven at run time, one entry per row of PREFERENCE_SCREENS: the
# screen's supported entry point, a key it must refuse and the refusal that must
# name it, then the refusal a payload containing itself must raise. The static
# guard above proves no screen calls itself; only running one proves it reaches
# the bottom of a payload, still screens down there, and stops at a loop.
DRIVEN_SCREENS = (
    (
        "refuse_capture_preference",
        lambda value: refuse_capture_preference(value),
        {"preferred": "one of them"},
        SchemaRefusal,
        "may not express capture preference",
        SchemaRefusal,
        "corpus register contains itself",
    ),
    (
        "triage._refuse_preference_named",
        lambda value: triage._refuse_preference_named(value, "queue"),
        {"preferred": "one of them"},
        TriageRefusal,
        "triage refusal queue-expresses-preference",
        TriageRefusal,
        "triage refusal queue-expresses-preference: queue contains itself",
    ),
    (
        "dossier.assert_no_order_bearing_field",
        lambda value: _dossier().assert_no_order_bearing_field(value),
        {"trust_score": 100},
        ContractError,
        "names a preference",
        ContractError,
        "contains itself",
    ),
)

# A cyclic payload is walked forever by a screen without the bookkeeping, so
# every case below runs under a wall-clock guard: a regression must fail this
# file rather than hang the suite that runs it. Generous on purpose -- the
# payloads are three objects each, so anything approaching this is a loop and
# not a slow machine.
CYCLE_TIME_LIMIT_SECONDS = 20.0


@contextlib.contextmanager
def _within(seconds: float, what: str):
    """Fail, rather than hang, if `what` does not return inside `seconds`.

    `SIGALRM` is delivered to the main thread between bytecodes, which is where
    these walks spend their time, so a screen appending to its worklist forever
    is interrupted and reported as the failure it is. Where the signal does not
    exist the body still runs: a guard that cannot arm must not silently skip
    the assertion it was guarding, and the platforms this build runs on have it.
    """
    if not hasattr(signal, "SIGALRM"):  # pragma: no cover - not a platform we run on
        yield
        return

    def fire(signum, frame):
        raise AssertionError(
            f"{what} did not return within {seconds}s on a payload that contains itself. "
            "A no-picker screen walks an explicit worklist, so a cycle has no stack to "
            "exhaust: without on-path bookkeeping it appends forever and the caller hangs "
            "instead of being refused by name."
        )

    previous = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _deep(leaf: object, depth: int = FAMILY_DEPTH) -> object:
    """`leaf` buried under `depth` alternating mappings and lists.

    Both container types, because every screen in the family walks both and a
    payload of only one would leave half of each walk unexercised.
    """
    value = leaf
    for _ in range(depth):
        value = {"nested": [value]}
    return value


@pytest.mark.parametrize(
    ("label", "screen", "forbidden", "refusal", "match", "cycle_refusal", "cycle_match"),
    DRIVEN_SCREENS,
    ids=[row[0] for row in DRIVEN_SCREENS],
)
def test_every_preference_screen_walks_a_pathological_payload_to_the_bottom(
    label, screen, forbidden, refusal, match, cycle_refusal, cycle_match
):
    """The static guard is a guard on shape; this is the behaviour it stands for.

    A screen that walks the interpreter stack answers a deep payload with a
    `RecursionError` -- a crash naming neither the record that carried the
    forbidden field nor the field. Both halves are needed: reaching the bottom of
    a clean payload proves the walk is not stopped by depth, and refusing a field
    buried at the same depth proves it is still screening down there rather than
    quietly giving up. Neither may raise `RecursionError`, which is why the clean
    half is run bare -- a `RecursionError` there fails the test as itself.

    Only the reference screen was exercised at depth before. The others were
    covered by the static guard alone, which cannot tell an explicit worklist
    that walks everything from one that stops early.
    """
    clean = _deep({"leaf": 1})
    screen(clean)
    del clean

    buried = _deep(forbidden)
    with pytest.raises(refusal, match=re.escape(match)):
        screen(buried)


@pytest.mark.parametrize(
    ("label", "screen", "forbidden", "refusal", "match", "cycle_refusal", "cycle_match"),
    DRIVEN_SCREENS,
    ids=[row[0] for row in DRIVEN_SCREENS],
)
def test_every_preference_screen_names_a_payload_that_contains_itself(
    label, screen, forbidden, refusal, match, cycle_refusal, cycle_match
):
    """Depth is not the only way a worklist walk fails to answer.

    Converting these screens from recursion removed the interpreter stack from
    the walk, and with it the accident that used to stop a self-referential
    payload: the recursive form ended a cycle by exhausting itself and raising
    `RecursionError`, which at least returned. A worklist has nothing to
    exhaust, so a value that is its own ancestor is appended forever and the
    caller hangs -- strictly less than the crash the conversion replaced, and
    from a guard whose entire job is to refuse by name.

    Only `assert_no_order_bearing_field` tracked the containers it had open.
    The round that converted the others recorded that as a live property rather
    than fixing it, on the argument that every remaining screen is fed values
    parsed from JSON bytes and JSON cannot be cyclic. That argument does not
    hold: `native_witness` and `perlector_audit` are called with in-memory
    structures the caller assembled, and each screen runs before any shape
    check closes what it walks. So every screen carries the same enter/exit
    bookkeeping now, and each is tested here through the same entry point the
    depth case uses -- under a wall-clock guard, because the failure this
    closes is a hang and a test that hangs is worse than no test.
    """
    looped: dict = {"nested": []}
    looped["nested"].append(looped)
    # A tuple cannot hold itself, but a list inside one can close the loop.
    through_tuple: list = []
    through_tuple.append({"nested": (through_tuple,)})

    for payload in (looped, through_tuple):
        with _within(CYCLE_TIME_LIMIT_SECONDS, label):
            with pytest.raises(cycle_refusal, match=re.escape(cycle_match)):
                screen(payload)


@pytest.mark.parametrize(
    ("label", "screen", "forbidden", "refusal", "match", "cycle_refusal", "cycle_match"),
    DRIVEN_SCREENS,
    ids=[row[0] for row in DRIVEN_SCREENS],
)
def test_a_value_shared_between_siblings_is_not_a_cycle(
    label, screen, forbidden, refusal, match, cycle_refusal, cycle_match
):
    """The other half of the bookkeeping: what it must *not* refuse.

    Tracking every container ever seen would be simpler and wrong. A record
    assembled by reference -- the same condition dict under two views, one
    findings list named twice -- is a directed graph, not a loop, and refusing
    it would turn a screen written to catch a picker into a screen that rejects
    ordinary well-formed input. Only ancestors on the current path are tracked,
    so the shared value is walked at each of its positions.

    Walked, not skipped: the forbidden field is buried inside the shared value,
    and reaching it proves the second visit screened rather than short-circuited.
    """
    shared: dict = {"leaf": 1}
    screen({"left": shared, "right": [shared, {"deeper": shared}]})

    offending: dict = dict(forbidden)
    with pytest.raises(refusal, match=re.escape(match)):
        screen({"left": {"clean": 1}, "right": [{"clean": 2}, offending]})


@pytest.mark.parametrize(
    ("label", "screen", "forbidden", "refusal", "match", "cycle_refusal", "cycle_match"),
    DRIVEN_SCREENS,
    ids=[row[0] for row in DRIVEN_SCREENS],
)
def test_a_forbidden_field_wrapped_in_a_tuple_is_not_hidden_from_any_screen(
    label, screen, forbidden, refusal, match, cycle_refusal, cycle_match
):
    """A tuple is not a leaf just because these walks used to treat it as one (F085).

    Every screen in the family descended only into `dict` and `list`, so a
    forbidden field buried inside a `tuple` reached the bottom of the walk
    unexamined -- and `common.contracts.canonical.canonical_bytes` serializes a
    tuple exactly like a list, so the hidden field still reached a sealed
    artifact looking like an ordinary array member. Reproduced directly before
    this test existed: every screen here passed a payload no different in kind
    from what it already refuses, only because the offending value sat one
    tuple below where the walk was willing to look.
    """
    with pytest.raises(refusal, match=re.escape(match)):
        screen({"nested": (dict(forbidden),)})
