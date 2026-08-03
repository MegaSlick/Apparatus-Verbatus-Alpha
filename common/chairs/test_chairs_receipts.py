"""Spec 02, test 3 — Receipts.

"Schema round-trip; a receipt missing revision, digest or any #41 field is
refused (#42); the receipt is refused by `StageContext.publish` and accepted by
the run receipt writer."

The last clause is the load-bearing one. A serving receipt carries a live
endpoint and a start moment; `envelope.py`'s docstring already settles where such
a thing may live — "in an approval record or a run receipt, both of which are
honestly non-deterministic and neither of which is a stage artifact" — so
publishing one through `StageContext.publish` would break spec 01's determinism
test. The refusal side of that lives in `common/test_stage_seats.py`, which has a
`StageContext` to call; what is checked here is the value, its schema, and every
way an incomplete one is refused.
"""

import pytest

from common.chairs.errors import ReceiptRefusal, UnresolvedChairRefusal
from common.chairs.models import ChairIdentity
from common.chairs.receipts import RECEIPT_SCHEMA, build_receipt, receipt_record, validate_receipt

from .conftest import (
    absent_chair,
    config_of,
    hf_chair,
    local_chair,
    registry_for,
    serving_details,
)

DIGEST = "d" * 64

# Exactly the fields spec 02's "serving receipt" section names, with the four it
# calls out in bold — tokenizer revision, seed, and the context and pixel caps —
# among them. A receipt missing any one of these is refused (#42).
REQUIRED_FIELDS = (
    "schema",
    "seat",
    "source",
    "resolved",
    "revision",
    "revision_kind",
    "digest_manifest",
    "tokenizer_revision",
    "seed",
    "context_cap",
    "pixel_cap",
    "engine",
    "engine_version",
    "dtype",
    "adapter_identity",
    "endpoint",
    "started_at",
)


@pytest.fixture
def identity(tmp_path) -> ChairIdentity:
    return config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", DIGEST)}).chairs[
        "attestator_1"
    ]


# --- Schema round-trip -----------------------------------------------------------


def test_a_receipt_carries_every_named_field_and_round_trips_through_its_validator(identity):
    record = receipt_record(build_receipt(identity, serving_details()))

    assert set(record) == set(REQUIRED_FIELDS)
    assert record["schema"] == RECEIPT_SCHEMA
    assert record["seat"] == "attestator_1"
    assert record["resolved"] == "fixture-org/attestator_1"
    assert record["revision"] == "a" * 40
    assert record["revision_kind"] == "git-commit"
    assert record["digest_manifest"] == DIGEST
    assert (record["tokenizer_revision"], record["seed"]) == ("f" * 40, 7)
    assert (record["context_cap"], record["pixel_cap"]) == (8192, 1_000_000)
    assert (record["engine"], record["engine_version"]) == ("fixture-engine", "1.0.0")
    assert record["endpoint"] == "http://127.0.0.1:8000"
    assert validate_receipt(record) == record


def test_a_local_repository_receipt_records_its_revision_equivalent_and_says_which(tmp_path):
    """A local seat has no git revision, so the verified manifest hash stands in
    — and `revision_kind` keeps that from being read as a commit id."""
    identity = config_of(
        tmp_path, {"perlector": local_chair("perlector", DIGEST)}, model_root="model-fixtures"
    ).chairs["perlector"]

    record = receipt_record(build_receipt(identity, serving_details()))

    assert record["revision_kind"] == "digest-manifest"
    assert record["revision"] == record["digest_manifest"] == DIGEST
    assert record["resolved"] == "perlector"


def test_an_adapter_identity_travels_in_full_or_not_at_all(tmp_path):
    chairs = {
        "attestator_1": hf_chair("attestator_1", DIGEST, adapter_of="base"),
        "base": hf_chair("base", DIGEST),
    }
    config = config_of(tmp_path, chairs)
    adapter, base = config.chairs["attestator_1"], config.chairs["base"]

    record = receipt_record(build_receipt(adapter, serving_details(adapter_identity=base)))
    assert record["adapter_identity"] == base.to_record()
    assert validate_receipt(record) == record

    bare = receipt_record(build_receipt(adapter, serving_details()))
    assert bare["adapter_identity"] is None


# --- Every omission is refused (#42) ------------------------------------------------


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_a_receipt_missing_any_required_field_is_refused(identity, field):
    record = receipt_record(build_receipt(identity, serving_details()))
    del record[field]
    with pytest.raises(ReceiptRefusal) as caught:
        validate_receipt(record)
    assert field in str(caught.value)


def test_a_receipt_carrying_a_field_nothing_declared_is_refused(identity):
    """Unknown fields are refused rather than ignored: a receipt with a stray
    `confidence` is a receipt somebody has been editing."""
    record = receipt_record(build_receipt(identity, serving_details()))
    record["confidence"] = "high"
    with pytest.raises(ReceiptRefusal, match="unknown field"):
        validate_receipt(record)


def test_a_receipt_under_another_schema_label_is_refused(identity):
    record = receipt_record(build_receipt(identity, serving_details()))
    record["schema"] = "seat-serving-receipt.v0"
    with pytest.raises(ReceiptRefusal, match="schema"):
        validate_receipt(record)


@pytest.mark.parametrize(
    "field", ["tokenizer_revision", "engine", "engine_version", "dtype", "endpoint", "started_at"]
)
def test_a_blank_serving_field_is_refused_before_a_receipt_exists(identity, field):
    with pytest.raises(ReceiptRefusal) as caught:
        build_receipt(identity, serving_details(**{field: "   "}))
    assert field in str(caught.value)


@pytest.mark.parametrize("field", ["seed", "context_cap", "pixel_cap"])
@pytest.mark.parametrize("value", [-1, "8192", None, True])
def test_a_cap_or_seed_that_is_not_a_non_negative_integer_is_refused(identity, field, value):
    with pytest.raises(ReceiptRefusal) as caught:
        build_receipt(identity, serving_details(**{field: value}))
    assert field in str(caught.value)


def test_a_huggingface_receipt_whose_revision_is_not_its_commit_id_is_refused(identity):
    record = receipt_record(build_receipt(identity, serving_details()))
    record["revision"] = "main"
    with pytest.raises(ReceiptRefusal, match="40-hex git commit"):
        validate_receipt(record)


def test_a_local_receipt_whose_revision_is_not_its_digest_manifest_is_refused(tmp_path):
    identity = config_of(
        tmp_path, {"perlector": local_chair("perlector", DIGEST)}, model_root="model-fixtures"
    ).chairs["perlector"]
    record = receipt_record(build_receipt(identity, serving_details()))
    record["revision"] = "e" * 64
    with pytest.raises(ReceiptRefusal, match="verified digest-manifest hash"):
        validate_receipt(record)


def test_a_receipt_that_is_not_an_object_at_all_is_refused(identity):
    for value in (None, [], "receipt", 3):
        with pytest.raises(ReceiptRefusal, match="not an object"):
            validate_receipt(value)


def test_a_receipt_is_refused_for_an_identity_that_is_not_exactly_pinned(tmp_path):
    """The seat's own pin has to be complete before there is anything to
    receipt: an identity carrying a branch name never reaches a receipt."""
    broken = ChairIdentity(
        role="attestator_1",
        source="huggingface",
        repo="fixture-org/attestator_1",
        path=None,
        revision="main",
        digest_manifest=DIGEST,
        manifest="manifests/attestator_1.json",
        adapter_of=None,
        serving_recipe="fixture",
        license_note="fixture",
    )
    with pytest.raises(ReceiptRefusal, match="not exactly pinned"):
        build_receipt(broken, serving_details())


# --- The registry only receipts the seat it still has a pin for ---------------------


def test_the_registry_refuses_a_receipt_for_an_identity_that_is_not_the_configured_pin(tmp_path):
    config = config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", DIGEST)})
    registry = registry_for(config, tmp_path)
    identity = registry.resolve("attestator_1")
    neighbouring = ChairIdentity(**{**identity.to_record(), "revision": "b" * 40})

    with pytest.raises(UnresolvedChairRefusal, match="neighbouring revision"):
        registry.receipt(neighbouring, serving_details())


def test_the_registry_refuses_a_receipt_for_an_explicitly_absent_chair(tmp_path):
    config = config_of(
        tmp_path,
        {"attestator_1": hf_chair("attestator_1", DIGEST), "attestator_2": absent_chair()},
        witness_floor=2,
    )
    registry = registry_for(config, tmp_path)
    stolen = ChairIdentity(
        **{**registry.resolve("attestator_1").to_record(), "role": "attestator_2"}
    )

    with pytest.raises(UnresolvedChairRefusal, match="explicitly absent"):
        registry.receipt(stolen, serving_details())
