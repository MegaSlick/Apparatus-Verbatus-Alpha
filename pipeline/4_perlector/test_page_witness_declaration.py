"""The consumer side of the page-witness declaration.

`pipeline/3_attestatores/run.py::declared_page_witness_chairs` is the producer's
key to this fixture field: a unique list of strings, every one of them a chair the
run was sealed with. The Perlector holds its own copy of that key rather than
trusting the producer's, because a consumer that trusts its producer has no
boundary — and R0's handoff test drives all seven boundaries for exactly that
reason.

It held a weaker one. The reader checked only that the declaration was a list of
strings, so two of the producer's three refusals had no counterpart here, and a
declaration naming nobody real read as sound: `expected_page_witness` is false for
every configured chair, every attachment and Testimonium in the tree agrees with
it because none of them is a page witness either, and the stage validates a run in
which the page-witness mechanism silently did not exist. That is a boundary
dropping coverage without saying so.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import SchemaRefusal


def _load_perlector():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("perlector_page_witness_declaration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()


class _UnhashableString(str):
    __hash__ = None


class _HostileReprString(str):
    def __repr__(self):
        raise RuntimeError("the refusal rendered an untrusted chair-name subclass")


def _context(declared, chairs=("attestator_1", "attestator_3")):
    return SimpleNamespace(fixture={"page_witness_chairs": declared}, witness_chairs=chairs)


def test_the_declared_page_witnesses_are_read_back_when_the_fixture_is_sound():
    assert perlector.declared_page_witness_chairs(_context(["attestator_1"])) == {"attestator_1"}


def test_an_absent_declaration_is_no_page_witnesses_rather_than_a_refusal():
    """Harvest #14: a seal that stops refusing bad things in order to stop refusing
    good things is not a fix. The fixture is allowed to declare none."""
    context = SimpleNamespace(fixture={}, witness_chairs=("attestator_1",))
    assert perlector.declared_page_witness_chairs(context) == set()


@pytest.mark.parametrize(
    "declared",
    (
        "attestator_1",
        {"attestator_1": True},
        ["attestator_1", 3],
        ["attestator_1", None],
        ["attestator_1", ["attestator_3"]],
        [float("nan")],
        [float("inf")],
        [1.5],
        [True],
        pytest.param([10**5000], id="huge-int"),
    ),
)
def test_a_declaration_that_is_not_a_list_of_chair_names_is_refused(declared):
    """A string-valued declaration would otherwise degrade into per-character
    membership and blame the attachment for the fixture's own malformation."""
    with pytest.raises(SchemaRefusal, match="unique list of chair names"):
        perlector.declared_page_witness_chairs(_context(declared))


def test_a_duplicated_chair_is_refused_here_exactly_as_the_producer_refuses_it():
    """`set(declared)` absorbs a duplicate in silence, so the reader used to accept
    a fixture the producer would have refused outright — a run reading as sound one
    stage after it could not have been produced."""
    with pytest.raises(SchemaRefusal, match="unique list of chair names"):
        perlector.declared_page_witness_chairs(_context(["attestator_1", "attestator_1"]))


@pytest.mark.parametrize(
    "chair",
    (
        pytest.param(_UnhashableString("attestator_1"), id="unhashable-string-subclass"),
        pytest.param(_HostileReprString("attestator_33"), id="hostile-repr-string-subclass"),
    ),
)
def test_a_chair_name_string_subclass_is_refused_before_set_or_rendering(chair):
    with pytest.raises(SchemaRefusal, match="unique list of chair names"):
        perlector.declared_page_witness_chairs(_context([chair]))


def test_a_chair_outside_the_configured_roster_is_refused_and_both_halves_named():
    """The silent case, and the reason this check is worth having on the reader's
    side at all: nothing in the run tree contradicts a declaration that names
    nobody, so without this the stage reports a sound run over vanished coverage."""
    with pytest.raises(SchemaRefusal) as caught:
        perlector.declared_page_witness_chairs(_context(["attestator_33"]))
    message = str(caught.value)
    assert "attestator_33" in message
    assert "attestator_1" in message and "attestator_3" in message


def test_an_all_held_preflight_still_validates_the_run_declaration():
    """Held acts skip Testimonium accounting but still produce immutable
    ``not-run`` Perlectiones. The run-global declaration must refuse before those
    writes rather than disappearing behind the held-act shortcut."""
    context = _context(["attestator_33"])

    with pytest.raises(SchemaRefusal, match="outside this run's configured witness roster"):
        perlector.preflight_testimonia_denominator(context, [{"outcome": "held"}])


def test_a_chair_name_carrying_a_surrogate_is_refused_printably():
    """It is a string, so it clears the shape check and reaches a message that an
    operator's stderr has to encode. `repr` escapes it; a raw interpolation would
    raise `UnicodeEncodeError` out of the report of the refusal itself."""
    with pytest.raises(SchemaRefusal) as caught:
        perlector.declared_page_witness_chairs(_context(["attestator_\ud800"]))
    str(caught.value).encode("utf-8")


@pytest.mark.parametrize("chair", ("NaN", "attestator_\0"))
def test_hostile_but_encodable_chair_strings_are_refused_printably(chair):
    with pytest.raises(SchemaRefusal) as caught:
        perlector.declared_page_witness_chairs(_context([chair]))
    str(caught.value).encode("utf-8")
