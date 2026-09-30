"""The one page-witness roster reader every stage uses, and its roster check.

The Attestatores, the Perlector and the Recensor each call
`declared_page_witness_chairs` on their own run authority, so what it derives
from the sealed roster, and what it refuses, is every stage's answer.
"""

from types import SimpleNamespace

import pytest

from common.chairs.models import AbsentChair, ChairIdentity
from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.page_testimonia import declared_page_witness_chairs, require_page_roster


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


def _context(scopes: dict[str, str], *, roster=None, absent=(), fixture=None):
    configured: dict[str, object] = {role: _identity(role, scope) for role, scope in scopes.items()}
    for role in absent:
        configured[role] = AbsentChair(role=role, reason="fixture absence")
    return SimpleNamespace(
        fixture={} if fixture is None else fixture,
        witness_chairs=list(scopes) + list(absent) if roster is None else roster,
        registry=SimpleNamespace(config=SimpleNamespace(chairs=configured)),
    )


# Case 0 is the scope layout shipped in config/models.toml.
DERIVED = (
    (
        _context({"attestator_1": "page", "attestator_2": "act", "attestator_3": "page"}),
        {"attestator_1", "attestator_3"},
    ),
    (_context({"attestator_1": "act", "attestator_2": "act"}), set()),
    (
        _context({"attestator_1": "page", "attestator_2": "page"}),
        {"attestator_1", "attestator_2"},
    ),
    # An explicit absence parses no scope at all, so it is never page-scoped.
    (_context({"attestator_1": "page"}, absent=("attestator_2",)), {"attestator_1"}),
    # The retired fixture key must not reach the answer.
    (
        _context(
            {"attestator_1": "page", "attestator_2": "act"},
            fixture={"page_witness_chairs": ["attestator_2"]},
        ),
        {"attestator_1"},
    ),
)


@pytest.mark.parametrize(("context", "expected"), DERIVED, ids=range(len(DERIVED)))
def test_the_page_scoped_set_is_what_the_sealed_roster_implies(context, expected):
    assert declared_page_witness_chairs(context) == expected


REFUSED = (
    _context({"attestator_1": "page"}, roster="attestator_1"),
    _context({"attestator_1": "page"}, roster=["attestator_1", "attestator_1"]),
    _context({"attestator_1": "page"}, roster=["attestator_1", 3]),
    _context({"attestator_1": "page"}, roster=["attestator_1", "attestator_9"]),
)


@pytest.mark.parametrize("context", REFUSED, ids=range(len(REFUSED)))
def test_a_roster_no_scope_can_be_derived_from_is_refused(context):
    with pytest.raises(SchemaRefusal, match="sealed witness roster"):
        declared_page_witness_chairs(context)


def _record(chair: str) -> dict:
    return {"payload": {"chair": chair}}


def test_a_page_carries_exactly_the_configured_page_witnesses():
    chairs = {"attestator_1", "attestator_3"}
    require_page_roster("pg_1", [_record("attestator_1"), _record("attestator_3")], chairs)
    with pytest.raises(FatalAccounting, match=r"no current page Testimonium .*attestator_3"):
        require_page_roster("pg_1", [_record("attestator_1")], chairs)
    with pytest.raises(FatalAccounting, match=r"did not seal as page witnesses"):
        require_page_roster(
            "pg_1",
            [_record("attestator_1"), _record("attestator_2"), _record("attestator_3")],
            chairs,
        )
