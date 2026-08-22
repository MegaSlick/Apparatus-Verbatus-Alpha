"""The two stages that read page-witness scope must read it the same way.

`declared_page_witness_chairs` exists twice on purpose — the Attestatores writes
the page join under it and the Perlector validates the attachment under it, and
neither stage is meant to take the other's word for scope. Deliberate
duplication is not the same as licensed divergence, though: both copies derive
from one sealed source, so any answer they disagree on is a defect in one of
them, and the consult that ordered this slice names mirrored readers that have
already drifted twice as the standing hazard.

This is the drift alarm the pair did not have. It does not require the two
functions to be textually identical — only to answer identically, including on
the rosters each refuses.
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


AGREED = (
    # The live roster's own shape: two page witnesses and one act witness.
    _context({"attestator_1": "page", "attestator_2": "act", "attestator_3": "page"}),
    # No page-scoped occupant at all is a valid, empty answer, not a fallback.
    _context({"attestator_1": "act", "attestator_2": "act"}),
    # Every occupant page-scoped.
    _context({"attestator_1": "page", "attestator_2": "page"}),
    # An explicit absence stays in the roster and is scope-less, never page.
    _context({"attestator_1": "page"}, absent=("attestator_2",)),
    # A stale fixture declaration must move neither reader.
    _context(
        {"attestator_1": "page", "attestator_2": "act"},
        fixture={"page_witness_chairs": ["attestator_2"]},
    ),
)


@pytest.mark.parametrize("context", AGREED, ids=range(len(AGREED)))
def test_both_stages_derive_the_same_page_scoped_set(context):
    answers = [reader(context) for reader in READERS]
    assert answers[0] == answers[1], (
        "the Attestatores and the Perlector disagree about which occupants are "
        "page-scoped; the page join and the attachment it is validated against "
        "would then be built on two different rosters"
    )


REFUSED = (
    # Not a list.
    _context({"attestator_1": "page"}, roster="attestator_1"),
    # A repeated chair.
    _context({"attestator_1": "page"}, roster=["attestator_1", "attestator_1"]),
    # A name no chair could be.
    _context({"attestator_1": "page"}, roster=["attestator_1", 3]),
    # A sealed roster naming an occupant models.toml does not declare.
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
