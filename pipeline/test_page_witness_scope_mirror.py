"""Keep the independent producer and consumer scope readers in agreement.

Neither stage may trust the other's boundary check, so the implementations stay
separate. They must still derive identical answers and refusals from the same
sealed authority.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.chairs.models import AbsentChair, ChairIdentity
from common.contracts.errors import SchemaRefusal

ROOT = Path(__file__).resolve().parents[1]


def _load(stage: str, alias: str):
    path = ROOT / "pipeline" / stage / "run.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ATTESTATORES = _load("3_attestatores", "attestatores_scope_mirror")
PERLECTOR = _load("4_perlector", "perlector_scope_mirror")
READERS = (
    ATTESTATORES.declared_page_witness_chairs,
    PERLECTOR.declared_page_witness_chairs,
)


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


# Each case carries the set the sealed roster actually implies. Agreement alone
# would be satisfied by two readers sharing one bug -- both returning everything,
# or both returning nothing -- so the expected set is what makes this a
# measurement rather than a consistency check (GOVERNANCE 10). Case 0 is the
# scope layout shipped in config/models.toml.
AGREED = (
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
    # The retired fixture key must not reach the answer from either side.
    (
        _context(
            {"attestator_1": "page", "attestator_2": "act"},
            fixture={"page_witness_chairs": ["attestator_2"]},
        ),
        {"attestator_1"},
    ),
)


@pytest.mark.parametrize(("context", "expected"), AGREED, ids=range(len(AGREED)))
def test_both_stages_derive_the_same_page_scoped_set(context, expected):
    answers = [reader(context) for reader in READERS]
    assert answers[0] == answers[1], (
        "the Attestatores and the Perlector disagree about which occupants are "
        "page-scoped; the page join and the attachment it is validated against "
        "would then be built on two different rosters"
    )
    assert answers[0] == expected, (
        "both stages agree on a set the sealed roster does not imply; agreement "
        "between two readers is not evidence that either read the roster right"
    )


REFUSED = (
    _context({"attestator_1": "page"}, roster="attestator_1"),
    _context({"attestator_1": "page"}, roster=["attestator_1", "attestator_1"]),
    _context({"attestator_1": "page"}, roster=["attestator_1", 3]),
    _context({"attestator_1": "page"}, roster=["attestator_1", "attestator_9"]),
)


@pytest.mark.parametrize("context", REFUSED, ids=range(len(REFUSED)))
def test_both_stages_refuse_the_same_rosters_with_the_same_words(context):
    messages = []
    for reader in READERS:
        with pytest.raises(SchemaRefusal) as caught:
            reader(context)
        messages.append(str(caught.value))
    assert messages[0] == messages[1], (
        "both stages refuse, but say different things about the same sealed "
        "roster; an operator reading one refusal would be told a different fact"
    )
