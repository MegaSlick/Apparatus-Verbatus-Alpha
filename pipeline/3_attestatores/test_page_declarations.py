"""A page is witnessed by what the fixture declares a chair answered for it, or not at all.

A minted `genuinely-empty` outcome, with no provider or fixture response ever
asked for, would let a chair stand on disk as having read a page it was never
shown -- true on a synthetic white page, but evidence that did not exist. So a
page with no declared response resolves to `not-run`, and an empty reading is
derived only from a declared empty response. The end-to-end halves are the
`ink-free-page` and `ink-free-page-unwitnessed` scenarios.
"""

import pytest

from common.chairs.models import ChairIdentity
from common.contracts.errors import SchemaRefusal
from conftest import load_stage

attestatores = load_stage("3_attestatores")

CHAIR = "attestator_3"
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
    witness_adapter="churro.v1",
    witness_scope="page",
)


class _Context:
    """Only what `fixture_page_attempt` reaches for: the declared responses."""

    def __init__(self, scenario="ink-free-page", **tables):
        self.fixture = {table: list(rows) for table, rows in tables.items()}
        self.scenario = scenario


def _resolve(context, page_ordinal=3, ordinal=1):
    return attestatores.fixture_page_attempt(context, page_ordinal, CHAIR, CHAIR_IDENTITY, ordinal)


def test_an_undeclared_page_is_not_run_rather_than_empty():
    attempt = _resolve(_Context())

    assert attempt.outcome == "not-run"
    assert attempt.native_payload is None
    assert attempt.reason == "no response is declared for this configured chair on this page"
    # Emptiness is UNKNOWN here, not measured as empty: `no_response_health`
    # leaves every content fact `None`.
    assert attempt.health["empty"] is None
    assert attempt.health["truncated"] is None
    assert attempt.health["truncation_basis"] == "not-attempted"


def test_a_declared_empty_response_makes_the_page_genuinely_empty():
    context = _Context(
        witness_empty=[{"scenario": "ink-free-page", "page_ordinal": 3, "chair": CHAIR}]
    )
    attempt = _resolve(context)

    assert attempt.outcome == "genuinely-empty"
    assert attempt.native_payload == ""
    assert attempt.reason is None
    assert attempt.health["empty"] is True
    assert attempt.health["truncated"] is False


def test_a_declared_empty_testimony_response_reaches_the_same_outcome():
    """The outcome is derived from the retained payload, not from which table
    declared it, so no second spelling of the rule exists to drift."""
    context = _Context(testimony=[{"page_ordinal": 3, "chair": CHAIR, "payload": ""}])
    attempt = _resolve(context)

    assert attempt.outcome == "genuinely-empty"
    assert attempt.native_payload == ""


def test_a_declared_page_text_is_read():
    context = _Context(testimony=[{"page_ordinal": 3, "chair": CHAIR, "payload": "real ink"}])
    attempt = _resolve(context)

    assert attempt.outcome == "read"
    assert attempt.native_payload == "real ink"


def test_a_scenario_empty_response_overrides_the_base_table():
    """A scenario row outranks the scenario-agnostic base response, whatever its
    table: that is how a scenario says "this chair returned nothing here" over
    the base table's declared text."""
    context = _Context(
        testimony=[{"page_ordinal": 1, "chair": CHAIR, "payload": "base text"}],
        witness_empty=[{"scenario": "ink-free-page", "page_ordinal": 1, "chair": CHAIR}],
    )
    attempt = _resolve(context, page_ordinal=1)

    assert attempt.outcome == "genuinely-empty"
    assert attempt.native_payload == ""


def test_two_declared_responses_at_one_precedence_are_refused():
    """Two answers to one question, never resolved silently in either's favour."""
    context = _Context(
        testimony=[
            {"scenario": "ink-free-page", "page_ordinal": 3, "chair": CHAIR, "payload": "ink"}
        ],
        witness_empty=[{"scenario": "ink-free-page", "page_ordinal": 3, "chair": CHAIR}],
    )

    with pytest.raises(SchemaRefusal, match="declares 2 responses"):
        _resolve(context)


def test_a_declaration_belongs_to_its_own_attempt_ordinal():
    """An unnumbered row is attempt one; a later attempt reads only its own rows."""
    context = _Context(
        testimony=[
            {"page_ordinal": 3, "chair": CHAIR, "payload": "first"},
            {"page_ordinal": 3, "chair": CHAIR, "payload": "second", "attempt_ordinal": 2},
        ]
    )

    assert _resolve(context).native_payload == "first"
    assert _resolve(context, ordinal=2).native_payload == "second"
    assert _resolve(context, ordinal=3).outcome == "not-run"


def test_a_declared_failure_and_a_never_asked_chair_are_told_apart():
    failed = _resolve(
        _Context(witness_failure=[{"scenario": "ink-free-page", "page_ordinal": 3, "chair": CHAIR}])
    )
    never = _resolve(
        _Context(witness_not_run=[{"scenario": "ink-free-page", "page_ordinal": 3, "chair": CHAIR}])
    )

    assert failed.outcome == "failed"
    assert failed.reason == "the chair returned no usable response"
    assert failed.health["truncation_basis"] == "attempted-but-no-usable-response"
    assert never.outcome == "not-run"
    assert never.reason == "fixture declares that this configured chair was never attempted"
