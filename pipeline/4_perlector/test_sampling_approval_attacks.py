import os
from types import SimpleNamespace

import pytest
import run as perlector_run
from run import resolve_sampling_approval

from common.chairs import load_models_toml
from common.contracts.approval import build_approval_record
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import ContractError
from common.runtree.store import RECEIPTS_DIR, RunTree
from common.stage import (
    NUDA_APPROVAL_SUBJECT,
    PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT,
    load_fixture,
    run_config_bindings,
)
from conftest import ROOT

SUBJECTS = (NUDA_APPROVAL_SUBJECT, PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT)


def _context(tmp_path, config_digest: str):
    tree = RunTree.create(
        tmp_path,
        "approval-attack-run",
        source_manifest=[],
        config_digest=config_digest,
        adapter_recipes={},
        witness_chairs=[],
    )
    return SimpleNamespace(tree=tree, config_digest=config_digest)


def _record(subject: str, target_version_hash: str, *, reason: str = "test-only design"):
    return build_approval_record(
        subject_ids=[subject],
        action="other",
        reason=reason,
        target_version_hash=target_version_hash,
        timestamp="2026-08-22T00:00:00Z",
    )


def _resolve(context, subject):
    return resolve_sampling_approval(context, approval_ref=subject, subject=subject)


def _write_unchecked_receipt(tree, data: bytes):
    digest = digest_bytes(data)
    path = tree.resolve(tree.receipt_path(digest))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("subject", SUBJECTS)
def test_each_arm_refuses_a_stale_record_for_an_older_config(tmp_path, subject):
    context = _context(tmp_path, "a" * 64)
    reference, _ = context.tree.write_approval_record(_record(subject, "b" * 64))

    with pytest.raises(ContractError) as refusal:
        _resolve(context, subject)

    message = str(refusal.value)
    assert "not this run's sealed config_digest" in message
    assert reference.relative_path in message
    assert context.config_digest in message
    assert "start a new run tree" in message


@pytest.mark.parametrize(
    ("subject", "other_subject"),
    (
        (NUDA_APPROVAL_SUBJECT, PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT),
        (PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT, NUDA_APPROVAL_SUBJECT),
    ),
)
def test_each_arm_refuses_a_record_for_the_other_arm(tmp_path, subject, other_subject):
    context = _context(tmp_path, "a" * 64)
    context.tree.write_approval_record(_record(other_subject, context.config_digest))

    with pytest.raises(ContractError, match=f"no approval record names experiment {subject!r}"):
        _resolve(context, subject)


@pytest.mark.parametrize(
    ("subject", "old_rate", "new_rate", "rate_key", "selector_key"),
    (
        (NUDA_APPROVAL_SUBJECT, 250, 251, "nuda_per_mille", "nuda_approval_ref"),
        (
            PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT,
            250,
            251,
            "perlector_instrument_per_mille",
            "perlector_instrument_approval_ref",
        ),
    ),
)
def test_each_arm_refuses_when_its_rate_changed_after_the_record_was_sealed(
    tmp_path, subject, old_rate, new_rate, rate_key, selector_key
):
    models = load_models_toml(ROOT / "config" / "models.toml")
    fixture = load_fixture(str(ROOT / "proof"))
    old = run_config_bindings(
        models,
        fixture,
        "happy",
        **{rate_key: old_rate, selector_key: subject},
    )["config_digest"]
    new = run_config_bindings(
        models,
        fixture,
        "happy",
        **{rate_key: new_rate, selector_key: subject},
    )["config_digest"]
    assert old != new
    context = _context(tmp_path, new)
    context.tree.write_approval_record(_record(subject, old))

    with pytest.raises(ContractError, match="not this run's sealed config_digest"):
        _resolve(context, subject)


@pytest.mark.parametrize("subject", SUBJECTS)
def test_each_arm_refuses_two_records_even_when_one_is_current(tmp_path, subject):
    context = _context(tmp_path, "a" * 64)
    current, _ = context.tree.write_approval_record(
        _record(subject, context.config_digest, reason="test-only current design")
    )
    stale, _ = context.tree.write_approval_record(
        _record(subject, "b" * 64, reason="test-only superseded design")
    )

    with pytest.raises(ContractError) as refusal:
        _resolve(context, subject)

    message = str(refusal.value)
    assert "2 validated approval records" in message
    assert current.relative_path in message
    assert stale.relative_path in message
    assert context.config_digest in message
    assert "start a new run tree" in message


@pytest.mark.parametrize("subject", SUBJECTS)
@pytest.mark.parametrize(
    ("data", "cause"),
    ((b"[", "malformed JSON"), (b"[]", "JSON list, not an object")),
)
def test_each_arm_refuses_an_uninspectable_receipt_instead_of_skipping_it(
    tmp_path, subject, data, cause
):
    context = _context(tmp_path, "a" * 64)
    context.tree.write_approval_record(_record(subject, context.config_digest))
    _write_unchecked_receipt(context.tree, data)

    with pytest.raises(ContractError) as refusal:
        _resolve(context, subject)

    message = str(refusal.value)
    assert cause in message
    assert RECEIPTS_DIR in message
    assert "cannot prove exactly one approval" in message
    assert "hold this run for review" in message


@pytest.mark.parametrize("subject", SUBJECTS)
def test_each_arm_validates_every_candidate_before_calling_two_records_ambiguous(tmp_path, subject):
    context = _context(tmp_path, "a" * 64)
    context.tree.write_approval_record(_record(subject, context.config_digest))
    corrupt = _record(subject, context.config_digest, reason="test-only second design")
    corrupt["reason"] = "edited after approval"
    _write_unchecked_receipt(context.tree, canonical_bytes(corrupt))

    with pytest.raises(ContractError) as refusal:
        _resolve(context, subject)

    message = str(refusal.value)
    assert "self-hash" in message
    assert RECEIPTS_DIR in message
    assert context.config_digest in message
    assert "start a new run tree" in message


@pytest.mark.parametrize("subject", SUBJECTS)
def test_each_gate_invocation_rereads_a_record_deleted_after_an_earlier_invocation(
    tmp_path, subject
):
    context = _context(tmp_path, "a" * 64)
    context.tree.write_approval_record(_record(subject, context.config_digest))
    resolved = _resolve(context, subject)
    context.tree.resolve(resolved.reference.relative_path).unlink()

    with pytest.raises(ContractError, match="no approval record names experiment"):
        _resolve(context, subject)


@pytest.mark.parametrize("subject", SUBJECTS)
def test_each_arm_refuses_an_in_tree_symlink_instead_of_following_approval_bytes(tmp_path, subject):
    context = _context(tmp_path, "a" * 64)
    reference, _ = context.tree.write_approval_record(_record(subject, context.config_digest))
    receipt = context.tree.resolve(reference.relative_path)
    alternate = context.tree.root / "approval-by-another-name.json"
    receipt.replace(alternate)
    receipt.symlink_to(alternate)

    with pytest.raises(ContractError, match="without following a redirect"):
        _resolve(context, subject)


@pytest.mark.parametrize("subject", SUBJECTS)
def test_each_arm_refuses_a_hard_linked_receipt_with_a_second_mutable_name(tmp_path, subject):
    context = _context(tmp_path, "a" * 64)
    data = canonical_bytes(_record(subject, context.config_digest))
    source = context.tree.root / "approval-by-another-name.json"
    source.write_bytes(data)
    receipt = context.tree.resolve(context.tree.receipt_path(digest_bytes(data)))
    receipt.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, receipt)

    with pytest.raises(ContractError, match="hard links"):
        _resolve(context, subject)


@pytest.mark.parametrize("subject", SUBJECTS)
def test_each_arm_refuses_a_case_variant_receipt_collision(tmp_path, subject):
    context = _context(tmp_path, "a" * 64)
    reference, _ = context.tree.write_approval_record(_record(subject, context.config_digest))
    receipt = context.tree.resolve(reference.relative_path)
    variant = receipt.with_name(f"{reference.sha256.upper()}.JSON")
    try:
        descriptor = os.open(variant, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        receipt.rename(variant)
        with pytest.raises(ContractError, match="noncanonical entry"):
            _resolve(context, subject)
        return
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(receipt.read_bytes())

    with pytest.raises(ContractError, match="case-variant names"):
        _resolve(context, subject)


def test_sampling_approval_scan_refuses_more_receipts_than_its_bound(tmp_path, monkeypatch):
    context = _context(tmp_path, "a" * 64)
    context.tree.write_approval_record(_record(SUBJECTS[0], context.config_digest))
    _write_unchecked_receipt(context.tree, b"{}")
    monkeypatch.setattr(perlector_run, "MAX_SAMPLING_APPROVAL_RECEIPTS", 1)

    with pytest.raises(ContractError, match="holds more than 1 entries"):
        _resolve(context, SUBJECTS[0])


def test_sampling_approval_scan_refuses_an_oversized_receipt_before_parsing(tmp_path, monkeypatch):
    context = _context(tmp_path, "a" * 64)
    _write_unchecked_receipt(context.tree, b'{"padding":"xxxxxxxx"}')
    monkeypatch.setattr(perlector_run, "MAX_SAMPLING_APPROVAL_RECEIPT_BYTES", 8)

    with pytest.raises(ContractError, match="larger than the 8-byte"):
        _resolve(context, SUBJECTS[0])


def test_sampling_approval_scan_compares_every_receipt_content_address(tmp_path):
    context = _context(tmp_path, "a" * 64)
    context.tree.write_approval_record(_record(SUBJECTS[0], context.config_digest))
    path = context.tree.resolve(context.tree.receipt_path("b" * 64))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}")

    with pytest.raises(ContractError, match="not its content-addressed name"):
        _resolve(context, SUBJECTS[0])


def test_sampling_approval_scan_refuses_ambiguous_noncanonical_json(tmp_path):
    context = _context(tmp_path, "a" * 64)
    context.tree.write_approval_record(_record(SUBJECTS[0], context.config_digest))
    _write_unchecked_receipt(context.tree, b'{"ignored":1,"ignored":2}')

    with pytest.raises(ContractError, match="not canonical JSON"):
        _resolve(context, SUBJECTS[0])


def test_sampling_approval_scan_names_deep_json_as_a_refusal(tmp_path):
    context = _context(tmp_path, "a" * 64)
    _write_unchecked_receipt(context.tree, b'{"nested":' * 10_000 + b"0" + b"}" * 10_000)

    with pytest.raises(ContractError, match="malformed JSON"):
        _resolve(context, SUBJECTS[0])


def test_sampling_approval_scan_refuses_a_directory_changed_during_validation(
    tmp_path, monkeypatch
):
    context = _context(tmp_path, "a" * 64)
    context.tree.write_approval_record(_record(SUBJECTS[0], context.config_digest))
    validate = perlector_run.validate_approval_record

    def validate_while_adding_receipt(record):
        _write_unchecked_receipt(context.tree, b"{}")
        return validate(record)

    monkeypatch.setattr(perlector_run, "validate_approval_record", validate_while_adding_receipt)

    with pytest.raises(ContractError, match="changed while the sampling gate inspected"):
        _resolve(context, SUBJECTS[0])
