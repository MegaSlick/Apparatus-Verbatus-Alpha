"""The Perlector independently enforces the producer's page-witness declaration.

The declaration is a unique list drawn from the sealed witness roster. Trusting
the producer here would leave malformed fixtures able to erase page coverage
without contradicting any attachment in the run tree.
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
    """The fixture is allowed to declare no page witnesses."""
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
    """Set conversion must not hide a declaration the producer refuses."""
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
    """Unknown declarations can agree with every record while silently losing coverage."""
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
    """The roster refusal must remain encodable when the chair name is not."""
    with pytest.raises(SchemaRefusal) as caught:
        perlector.declared_page_witness_chairs(_context(["attestator_\ud800"]))
    str(caught.value).encode("utf-8")


@pytest.mark.parametrize("chair", ("NaN", "attestator_\0"))
def test_hostile_but_encodable_chair_strings_are_refused_printably(chair):
    with pytest.raises(SchemaRefusal) as caught:
        perlector.declared_page_witness_chairs(_context([chair]))
    str(caught.value).encode("utf-8")


def test_an_unhashable_attachment_chair_is_refused_before_duplicate_accounting(monkeypatch):
    attachment = {
        "chair": [],
        "page_witness": False,
        "testimonium_ref": {},
        "attached": False,
        "content_health": {},
        "alignment": None,
        "span": None,
    }
    record = {
        "payload": {
            "act_key": "a1",
            "attempt_ordinal": 1,
            "attachments": [attachment],
        }
    }
    tree = SimpleNamespace(
        build_manifest=lambda stage: {
            "artifacts": [
                {"kind": "act-attachment", "subject_id": "act_0123456789abcdef", "artifact_id": "x"}
            ]
        },
        read_artifact=lambda stage, kind, artifact_id: record,
    )
    context = _context([], chairs=("attestator_1",))
    context.tree = tree
    monkeypatch.setattr(perlector, "latest_attempt", lambda records, label, operation: records[0])

    with pytest.raises(SchemaRefusal, match="malformed attachment"):
        perlector.act_attachment_view(
            context,
            {"act_id": "act_0123456789abcdef", "act_key": "a1"},
            [{"payload": {"chair": "attestator_1"}}],
            # `bases` became required when the view began checking its spans
            # against verified regions. The refusal under test fires on the
            # malformed attachment before any region is consulted, so the
            # empty list is the honest argument here, not a stand-in.
            [],
        )
