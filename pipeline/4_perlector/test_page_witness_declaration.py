"""The Perlector independently enforces page-witness scope.

Unit 10A moves the source of truth from the fixture to the sealed model
configuration: scope is each configured occupant's `witness_scope`. What does
not change is that this side reads it for itself. Trusting the producer here
would leave a malformed roster able to erase page coverage without
contradicting any attachment in the run tree.
"""

from types import SimpleNamespace

import pytest

from common.chairs.models import ChairIdentity
from common.contracts.errors import SchemaRefusal
from common.page_testimonia import declared_page_witness_chairs


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
    assert declared_page_witness_chairs(context) == {"attestator_1"}


def test_no_page_scoped_occupant_is_a_valid_empty_declaration():
    """A roster with no page-scoped occupant is empty scope, not a refusal."""
    assert declared_page_witness_chairs(_context(scopes={"attestator_1": "act"})) == set()


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
        declared_page_witness_chairs(_context(roster))
    message = str(caught.value)
    assert "Page-witness scope cannot be derived" in message
    assert "Start a new run" in message


def test_a_duplicate_scope_chair_is_refused():
    """Set conversion must not hide a roster the producer refuses."""
    with pytest.raises(SchemaRefusal, match="unique list of chair names"):
        declared_page_witness_chairs(_context(["attestator_1", "attestator_1"]))


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
        declared_page_witness_chairs(_context([chair]))


def test_an_unknown_scope_chair_is_refused_and_both_halves_are_named():
    """An unknown roster entry can agree with every record while losing coverage."""
    with pytest.raises(SchemaRefusal) as caught:
        declared_page_witness_chairs(_context(["attestator_33"]))
    message = str(caught.value)
    assert "attestator_33" in message
    assert "attestator_1" in message and "attestator_3" in message


def test_an_unknown_scope_chair_with_a_surrogate_is_refused_printably():
    """The roster refusal must remain encodable when the chair name is not."""
    with pytest.raises(SchemaRefusal) as caught:
        declared_page_witness_chairs(_context(["attestator_\ud800"]))
    str(caught.value).encode("utf-8")


@pytest.mark.parametrize("chair", ("NaN", "attestator_\0"))
def test_hostile_but_encodable_scope_chair_strings_are_refused_printably(chair):
    with pytest.raises(SchemaRefusal) as caught:
        declared_page_witness_chairs(_context([chair]))
    str(caught.value).encode("utf-8")
