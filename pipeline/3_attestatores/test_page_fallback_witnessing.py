"""A Designator page-fallback act is witnessed like any other act, or not at all.

This stage used to recognize the minted fallback identity and hand every
configured chair a completed `genuinely-empty` outcome from it -- before
`testimony_for`, before any provider or fixture response boundary, before
anything was asked. The writer then gave each of those records the proposal
regions, marked it attempted, minted a serving receipt, and recorded
trusted-boundary health, so three chairs stood on disk as having independently
read a page none of them had been shown. The conclusion happened to be true on
the synthetic white page; the evidence for it did not exist (Sol-S1).

The predecessor of this file guarded the *selector* for that branch -- that it
matched the derived identity rather than the `page-fallback:` label a fixture
act or a hand-edited seal could also wear. An unforgeable selector for a branch
that must not exist is still the branch, so the branch is gone and this pins the
absence: `resolve_attempt` reads the act's key to look up a response and nothing
else about it, so a fallback act and an ordinary act with the same declarations
resolve identically, and a fallback act with no declaration resolves to
`not-run` and holds. The end-to-end halves live in
`pipeline/orchestrator/test_orchestrator_acceptance.py`
(`ink-free-page` and `ink-free-page-unwitnessed`).
"""

import importlib.util
from pathlib import Path

import pytest

from common.chairs.models import ChairIdentity
from common.contracts.errors import SchemaRefusal
from common.contracts.identities import act_id
from common.stage import fallback_page_act_key


def _load_attestatores():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("attestatores_fallback_witnessing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attestatores = _load_attestatores()

CHAIR = "attestator_1"
FALLBACK_KEY = fallback_page_act_key(3)
CHAIR_IDENTITY = ChairIdentity(
    role=CHAIR,
    source="huggingface",
    repo="synthetic/witness",
    path=None,
    revision="a" * 40,
    digest_manifest="b" * 64,
    manifest="manifest.json",
    adapter_of=None,
    serving_recipe="synthetic",
    license_note="synthetic fixture identity",
)


class _Context:
    """Only what `resolve_attempt` actually reaches for: declared responses."""

    def __init__(self, testimony=(), scenario="ink-free-page"):
        self.fixture = {"testimony": list(testimony)}
        self.scenario = scenario


def _declarations(*, empty=(), ordinal=1):
    return {
        "ordinal": ordinal,
        "failures": set(),
        "empty": set(empty),
        "not_run": set(),
        "malformed": {},
    }


def _resolve(context, act_key, declarations, *, reread=False):
    return attestatores.resolve_attempt(
        context,
        {"act_key": act_key},
        CHAIR,
        CHAIR_IDENTITY,
        declarations,
        reread=reread,
    )


def test_an_undeclared_fallback_act_is_not_run_rather_than_empty():
    """The exact record the audit found: no response declared for the minted act."""
    attempt = _resolve(_Context(), FALLBACK_KEY, _declarations())

    assert attempt.outcome == "not-run"
    assert attempt.native_payload is None
    assert attempt.reason == "no attempt was made for this configured chair"
    # Emptiness is UNKNOWN here, not measured as empty. That is the whole
    # difference the old branch erased: `no_response_health` leaves every
    # content fact `None`, where the minted `genuinely-empty` recorded
    # `empty=True, truncated=False` about a response that never existed.
    assert attempt.health["empty"] is None
    assert attempt.health["truncated"] is None
    assert attempt.health["truncation_basis"] == "not-attempted"


def test_a_declared_empty_response_makes_the_fallback_act_genuinely_empty():
    """`ink-free-page`'s honest path: the chair was asked, and returned nothing."""
    attempt = _resolve(_Context(), FALLBACK_KEY, _declarations(empty={(FALLBACK_KEY, CHAIR)}))

    assert attempt.outcome == "genuinely-empty"
    assert attempt.native_payload == ""
    assert attempt.reason is None
    assert attempt.health["empty"] is True
    assert attempt.health["truncated"] is False


def test_a_declared_empty_testimony_response_reaches_the_same_outcome():
    """The outcome is derived from the retained payload, not from which table
    declared it -- so an ordinary `[[testimony]]` row whose body is empty is
    `genuinely-empty` too, and no second spelling of the rule exists to drift."""
    context = _Context(testimony=[{"act_key": FALLBACK_KEY, "chair": CHAIR, "payload": ""}])
    attempt = _resolve(context, FALLBACK_KEY, _declarations())

    assert attempt.outcome == "genuinely-empty"
    assert attempt.native_payload == ""


def test_the_fallback_act_resolves_exactly_as_an_ordinary_act_does():
    """No branch in this stage asks what kind of act it is reading."""
    declared = [{"act_key": FALLBACK_KEY, "chair": CHAIR, "payload": "tile text"}]
    fallback = _resolve(_Context(testimony=declared), FALLBACK_KEY, _declarations())
    ordinary = _resolve(
        _Context(testimony=[{**declared[0], "act_key": "a1"}]), "a1", _declarations()
    )

    assert fallback == ordinary
    assert fallback.outcome == "read"

    # And the undeclared halves are identical too: a fallback act nobody
    # declared a response for resolves to exactly the attempt an ordinary
    # undeclared act gets, not to a special absence of its own.
    undeclared_fallback = _resolve(_Context(), FALLBACK_KEY, _declarations())
    undeclared_ordinary = _resolve(_Context(), "a1", _declarations())
    assert undeclared_fallback == undeclared_ordinary
    assert undeclared_fallback.outcome == "not-run"


def test_a_fallback_shaped_key_never_blanks_an_ordinary_act():
    """The hazard the identity check existed for, now closed by construction:
    an ordinary act wearing the reserved label is read like any other act,
    because nothing consults the label at all."""
    context = _Context(testimony=[{"act_key": FALLBACK_KEY, "chair": CHAIR, "payload": "real ink"}])
    attempt = _resolve(context, FALLBACK_KEY, _declarations())

    assert attempt.outcome == "read"
    assert attempt.native_payload == "real ink"


def test_an_undeclared_fallback_reread_is_failed_rather_than_empty():
    """A reread names one chair on one act, so silence is an attempt that
    produced nothing -- still never an empty report."""
    attempt = _resolve(_Context(), FALLBACK_KEY, _declarations(), reread=True)

    assert attempt.outcome == "failed"
    assert attempt.native_payload is None
    assert attempt.reason == "the reread reached this chair and it returned no response"


def test_a_scenario_empty_response_overrides_the_base_table():
    """`witness_empty` is scenario-scoped, so it outranks the scenario-agnostic
    base response exactly as a scenario-specific `[[testimony]]` row does. That
    is how a blank scenario says "this chair returned nothing here" over the
    base table's declared text."""
    context = _Context(testimony=[{"act_key": "a1", "chair": CHAIR, "payload": "base text"}])
    attempt = _resolve(context, "a1", _declarations(empty={("a1", CHAIR)}))

    assert attempt.outcome == "genuinely-empty"
    assert attempt.native_payload == ""


def test_two_declared_responses_at_one_precedence_are_refused():
    """Two answers to one question. The elif chain would have resolved it
    silently in `witness_empty`'s favour."""
    context = _Context(
        testimony=[
            {
                "scenario": "ink-free-page",
                "act_key": FALLBACK_KEY,
                "chair": CHAIR,
                "payload": "ink",
            }
        ]
    )

    with pytest.raises(SchemaRefusal, match="both an empty response and a scenario response"):
        _resolve(context, FALLBACK_KEY, _declarations(empty={(FALLBACK_KEY, CHAIR)}))


def test_the_three_minted_act_classes_produce_three_different_identities():
    """The property that matters downstream is that three classes of act mint three
    different `act_id`s **on the same page, from the same bounds**, since that is
    the worst case: a page-fallback act and a residual both cover ink the
    structure pass did not propose, and identity is all that separates them.
    """
    page = "pg_0000000000000001"
    bounds = {"x": 0, "y": 0, "w": 10, "h": 10}

    proposed = act_id(page, "proposal", bounds)
    residual = act_id(page, "residual", bounds)
    fallback = act_id(page, "page-fallback", bounds)

    assert len({proposed, residual, fallback}) == 3


def test_fallback_key_remains_presentation_only():
    assert FALLBACK_KEY == "page-fallback:3"
    assert not hasattr(attestatores, "_is_page_fallback")
