"""Spec 02, test 6 — the contract suite.

"The full protocol against two independent implementations, with the skeleton's
calling stages parameterized over both; a deliberately incompatible third fails
naming the protocol clause."

Two implementations are independent, in the spec's own words, "when neither
imports the other and both are exercised by the same contract suite". Here that
is `common.chairs.registry.ChairRegistry` — real, filesystem- and Hugging
Face-backed — and `DeterministicChairRegistry` in this package's `conftest.py`,
in-memory and network-free. Every test below runs once per implementation, from
one body, so a claim that holds for one and not the other cannot pass.

The clause about the *stages* is discharged where the stages are:
`pipeline/test_chair_parameterization.py` runs all eight stage programs over both
implementations. And the claim stays the size the spec sized it — exercising an
interface against two implementations proves those two implement that interface,
and nothing whatever about model churn.
"""

import pytest

from common.chairs.errors import ProtocolClauseRefusal, UnresolvedChairRefusal
from common.chairs.models import AbsentChair, ChairIdentity, VerifiedSnapshot
from common.chairs.protocol import ChairProtocol, exercise_contract
from common.chairs.receipts import build_receipt

from .conftest import (
    DeterministicChairRegistry,
    RecordingFetcher,
    absent_chair,
    hf_chair,
    local_chair,
    pin_snapshot,
    registry_for,
    serving_details,
    write_models_toml,
    write_snapshot,
)

CONFIGURED = "attestator_1"
ABSENT = "attestator_2"
UNCONFIGURED = "haruspex_of_the_marginalia"
FILES = {"config.json": b'{"fixture": true}\n', "nested/weights.bin": b"fixture weights\n"}


@pytest.fixture(params=["registry", "deterministic"])
def implementation(request, tmp_path):
    """The identical configuration, handed to each implementation in turn."""
    write_snapshot(tmp_path / "model-fixtures" / CONFIGURED, dict(FILES))
    pin = pin_snapshot(
        tmp_path / "model-fixtures" / CONFIGURED, tmp_path / "manifests" / f"{CONFIGURED}.json"
    )
    config_path = write_models_toml(
        tmp_path,
        {CONFIGURED: local_chair(CONFIGURED, pin), ABSENT: absent_chair("fixture absence")},
        witness_floor=2,
        model_root="model-fixtures",
    )
    if request.param == "registry":
        from common.chairs.config import load_models_toml

        return registry_for(load_models_toml(config_path), tmp_path, RecordingFetcher({}))
    return DeterministicChairRegistry(config_path, tmp_path / "deterministic-snapshot")


# --- The shape itself --------------------------------------------------------------


def test_both_implementations_satisfy_the_protocol_without_inheriting_it(implementation):
    """`ChairProtocol` is structural: neither implementation subclasses it, and
    neither could, because one of them must be able to exist without the other."""
    assert isinstance(implementation, ChairProtocol)


def test_neither_implementation_imports_or_delegates_to_the_other():
    """Independence measured on the implementations, not on the files holding
    them: `conftest.py` imports `ChairRegistry` for the *other* tests' plumbing,
    which says nothing about whether the deterministic chair leans on it."""
    import inspect

    import common.chairs.registry as registry_module

    body = inspect.getsource(DeterministicChairRegistry)
    body = body[body.index('"""', body.index('"""') + 3) + 3 :]  # past the class docstring

    assert "common.chairs.registry" not in body
    assert "ChairRegistry(" not in body
    assert not issubclass(DeterministicChairRegistry, registry_module.ChairRegistry)
    assert "conftest" not in inspect.getsource(registry_module)
    assert "Deterministic" not in inspect.getsource(registry_module)


# --- resolve ------------------------------------------------------------------------


def test_resolve_returns_the_same_exact_identity_from_both(implementation):
    identity = implementation.resolve(CONFIGURED)

    assert isinstance(identity, ChairIdentity)
    assert identity.role == CONFIGURED
    assert identity.source == "local-repository"
    assert identity.revision is None
    assert identity.receipt_revision == identity.digest_manifest


def test_resolve_returns_the_same_explicit_absence_from_both(implementation):
    absence = implementation.resolve(ABSENT)

    assert absence == AbsentChair(role=ABSENT, reason="fixture absence")


def test_resolve_of_an_unconfigured_role_refuses_the_same_way_in_both(implementation):
    with pytest.raises(UnresolvedChairRefusal) as caught:
        implementation.resolve(UNCONFIGURED)
    assert caught.value.chair == UNCONFIGURED


# --- ensure ------------------------------------------------------------------------


def test_ensure_returns_a_verified_snapshot_of_the_resolved_pin_from_both(implementation):
    identity = implementation.resolve(CONFIGURED)
    snapshot = implementation.ensure(identity)

    assert isinstance(snapshot, VerifiedSnapshot)
    assert snapshot.identity == identity
    assert snapshot.manifest_digest == identity.digest_manifest


def test_ensure_refuses_a_neighbouring_revision_in_both(implementation):
    """Handing over an identity that is not the configured pin is the one way a
    caller can ask either implementation to serve something else. Both refuse."""
    identity = implementation.resolve(CONFIGURED)
    neighbouring = ChairIdentity(**{**identity.to_record(), "digest_manifest": "0" * 64})

    with pytest.raises(UnresolvedChairRefusal) as caught:
        implementation.ensure(neighbouring)
    assert caught.value.chair == CONFIGURED


def test_ensure_refuses_an_absent_chair_in_both(implementation):
    identity = implementation.resolve(CONFIGURED)
    stolen = ChairIdentity(**{**identity.to_record(), "role": ABSENT})

    with pytest.raises(UnresolvedChairRefusal, match="absent"):
        implementation.ensure(stolen)


# --- receipt ------------------------------------------------------------------------


def test_receipt_names_the_resolved_identity_in_both(implementation):
    identity = implementation.resolve(CONFIGURED)
    receipt = implementation.receipt(identity, serving_details())

    assert receipt.identity == identity
    assert receipt.to_record()["chair"] == CONFIGURED
    assert receipt.to_record()["revision"] == identity.digest_manifest


def test_receipt_refuses_an_identity_that_is_not_the_configured_pin_in_both(implementation):
    identity = implementation.resolve(CONFIGURED)
    neighbouring = ChairIdentity(**{**identity.to_record(), "serving_recipe": "something-else"})

    with pytest.raises(UnresolvedChairRefusal):
        implementation.receipt(neighbouring, serving_details())


# --- The whole protocol in one pass, and the third that breaks it -------------------


def test_the_full_protocol_passes_over_both_implementations(implementation):
    identity = implementation.resolve(CONFIGURED)
    snapshot = exercise_contract(
        implementation, role=CONFIGURED, expected_identity=identity, serving=serving_details()
    )
    assert snapshot.identity == identity


class _MissingEnsure:
    """No `ensure` at all: the protocol shape itself is broken."""

    def resolve(self, role):
        raise AssertionError("never reached")

    def receipt(self, identity, serving):
        raise AssertionError("never reached")


class _WrongChair:
    """Answers every request with one chair's identity — a picker with one option."""

    def __init__(self, identity: ChairIdentity):
        self._identity = ChairIdentity(**{**identity.to_record(), "role": "a_different_role"})

    def resolve(self, role):
        return self._identity

    def ensure(self, identity):
        return VerifiedSnapshot(identity, __import__("pathlib").Path("."), identity.digest_manifest)

    def receipt(self, identity, serving):
        return build_receipt(identity, serving)


class _WrongSnapshot:
    """Resolves correctly, then verifies something else and says it is fine."""

    def __init__(self, identity: ChairIdentity):
        self._identity = identity
        self._other = ChairIdentity(**{**identity.to_record(), "digest_manifest": "0" * 64})

    def resolve(self, role):
        return self._identity

    def ensure(self, identity):
        return VerifiedSnapshot(
            self._other, __import__("pathlib").Path("."), self._other.digest_manifest
        )

    def receipt(self, identity, serving):
        return build_receipt(identity, serving)


@pytest.mark.parametrize(
    ("broken", "clause"),
    [
        (_MissingEnsure, "protocol shape"),
        (_WrongChair, "resolve clause"),
        (_WrongSnapshot, "ensure clause"),
    ],
)
def test_a_deliberately_incompatible_third_fails_naming_the_clause_it_broke(
    implementation, broken, clause
):
    """Proof the suite discriminates rather than agreeing with itself. The same
    `exercise_contract` that both real implementations pass must fail here, and
    the failure has to say *which* clause — "it did not work" is not a finding."""
    identity = implementation.resolve(CONFIGURED)
    subject = broken() if broken is _MissingEnsure else broken(identity)

    with pytest.raises(ProtocolClauseRefusal) as caught:
        exercise_contract(
            subject, role=CONFIGURED, expected_identity=identity, serving=serving_details()
        )
    assert clause in str(caught.value)
    assert caught.value.chair == CONFIGURED


def test_the_two_implementations_agree_on_a_huggingface_pin_they_cannot_both_fetch(tmp_path):
    """One reads a config file and one reads the same config file; neither may
    disagree about what a `huggingface` pin resolves to, even though only one of
    them could ever fetch it."""
    config_path = write_models_toml(
        tmp_path, {CONFIGURED: hf_chair(CONFIGURED, "d" * 64)}, witness_floor=1
    )
    from common.chairs.config import load_models_toml

    real = registry_for(load_models_toml(config_path), tmp_path, RecordingFetcher({}))
    deterministic = DeterministicChairRegistry(config_path, tmp_path / "snapshot")

    assert real.resolve(CONFIGURED) == deterministic.resolve(CONFIGURED)
