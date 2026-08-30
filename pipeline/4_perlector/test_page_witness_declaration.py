"""The Perlector independently enforces page-witness scope.

Unit 10A moves the source of truth from the fixture to the sealed model
configuration: scope is each configured occupant's `witness_scope`. What does
not change is that this side reads it for itself. Trusting the producer here
would leave a malformed roster able to erase page coverage without
contradicting any attachment in the run tree.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.chairs.models import ChairIdentity
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


def _identity(role: str, scope: str) -> ChairIdentity:
    return ChairIdentity(
        role=role,
        source="local-repository",
        repo=None,
        path=role,
        revision=None,
        digest_manifest="a" * 64,
        manifest=f"manifests/{role}.json",
        adapter_of=None,
        serving_recipe="fixture",
        license_note="fixture",
        witness_adapter="churro.v1",
        witness_scope=scope,
    )


def _context(chairs=None, *, fixture=None, scopes=None):
    scopes = scopes or {"attestator_1": "page", "attestator_3": "act"}
    configured = {role: _identity(role, scope) for role, scope in scopes.items()}
    return SimpleNamespace(
        fixture={} if fixture is None else fixture,
        witness_chairs=list(scopes) if chairs is None else chairs,
        registry=SimpleNamespace(config=SimpleNamespace(chairs=configured)),
    )


def test_scope_is_read_from_the_configured_occupants_not_the_fixture():
    context = _context(fixture={"page_witness_chairs": ["attestator_3"]})
    assert perlector.declared_page_witness_chairs(context) == {"attestator_1"}


def test_no_page_scoped_occupant_is_a_valid_empty_declaration():
    """A roster with no page-scoped occupant is empty scope, not a refusal."""
    assert perlector.declared_page_witness_chairs(_context(scopes={"attestator_1": "act"})) == set()


@pytest.mark.parametrize(
    "roster",
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
def test_a_scope_roster_that_is_not_a_list_of_chair_names_is_refused(roster):
    with pytest.raises(SchemaRefusal, match="unique list of chair names") as caught:
        perlector.declared_page_witness_chairs(_context(roster))
    message = str(caught.value)
    assert "Page-witness scope cannot be derived" in message
    assert "Start a new run" in message


def test_a_duplicate_scope_chair_is_refused():
    """Set conversion must not hide a roster the producer refuses."""
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
    """The exact-type rule, not `isinstance`: a subclass can break set construction
    or refusal rendering, and this reader must stop before either."""
    with pytest.raises(SchemaRefusal, match="unique list of chair names"):
        perlector.declared_page_witness_chairs(_context([chair]))


def test_an_unknown_scope_chair_is_refused_and_both_halves_are_named():
    """An unknown roster entry can agree with every record while losing coverage."""
    with pytest.raises(SchemaRefusal) as caught:
        perlector.declared_page_witness_chairs(_context(["attestator_33"]))
    message = str(caught.value)
    assert "attestator_33" in message
    assert "attestator_1" in message and "attestator_3" in message


def test_an_all_held_preflight_still_validates_the_run_roster():
    """Held acts skip Testimonium accounting but still produce immutable
    ``not-run`` Perlectiones. The run-global roster reading must refuse before
    those writes rather than disappearing behind the held-act shortcut."""
    context = _context(["attestator_33"])

    with pytest.raises(SchemaRefusal, match="absent from the current models configuration"):
        perlector.preflight_testimonia_denominator(context, [{"outcome": "held"}])


def test_an_unknown_scope_chair_with_a_surrogate_is_refused_printably():
    """The roster refusal must remain encodable when the chair name is not."""
    with pytest.raises(SchemaRefusal) as caught:
        perlector.declared_page_witness_chairs(_context(["attestator_\ud800"]))
    str(caught.value).encode("utf-8")


@pytest.mark.parametrize("chair", ("NaN", "attestator_\0"))
def test_hostile_but_encodable_scope_chair_strings_are_refused_printably(chair):
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
    # A list, and a roster whose one occupant is page-scoped: the merged reader
    # requires an exact list and derives scope from the configured occupants.
    context = _context(chairs=["attestator_1"], scopes={"attestator_1": "page"})
    context.tree = tree
    monkeypatch.setattr(perlector, "latest_attempt", lambda records, label, operation: records[0])

    with pytest.raises(SchemaRefusal, match="malformed attachment"):
        perlector.act_attachment_view(
            context,
            {"act_id": "act_0123456789abcdef", "act_key": "a1"},
            [{"payload": {"chair": "attestator_1"}}],
            # `bases` joined the signature with work/continuation-page-evidence's
            # per-(chair, page) accounting, and `proposal_region_ids` with
            # Unit 10C's geometric attachment; the refusal under test fires
            # before either is read.
            [],
            set(),
        )


@pytest.mark.parametrize(
    ("change", "message"),
    (
        pytest.param(
            {"attachment_basis": "geometric-overlap"},
            "names an attachment basis other than",
            id="basis",
        ),
        pytest.param(
            {"span": {"start": 0, "end": 4}},
            "claims an alignment span",
            id="span",
        ),
    ),
)
def test_each_unattached_fault_is_refused_by_the_field_that_caused_it(monkeypatch, change, message):
    """One message per fault. A single refusal naming only `span` sent the
    operator to a field that was already null whenever the real fault was the
    basis, and left them to guess the rest from a stage exit."""
    attachment = {
        "chair": "attestator_3",
        "page_witness": False,
        "page_ordinal": None,
        "testimonium_ref": {},
        "attached": False,
        # pr/12 added `comparable` to `ATTACHMENT_FIELDS`. Without it the closed
        # shape check refuses this row first, and each case below proved that
        # refusal instead of the per-field one it names. False is the consistent
        # value beside `attached: False` -- unattached text cannot be comparable.
        "comparable": False,
        "attachment_basis": "unattached",
        "content_health": {},
        "alignment": None,
        "span": None,
    }
    attachment.update(change)
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
    context = _context(chairs=["attestator_3"], scopes={"attestator_3": "act"})
    context.tree = tree
    monkeypatch.setattr(perlector, "latest_attempt", lambda records, label, operation: records[0])

    with pytest.raises(SchemaRefusal, match=message):
        perlector.act_attachment_view(
            context,
            {"act_id": "act_0123456789abcdef", "act_key": "a1"},
            [{"payload": {"chair": "attestator_3"}}],
            [],
            set(),
            all_proposal_regions=[],
        )
