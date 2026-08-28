"""The approval-record contract: schema, self-hash, and the closed ingress record.

**Cut 2026-08-09, per Tyrel's ruling that session.** This file used to be almost
entirely about `require_current_data_gate_approval`, the `data-gate` action, and the
policy-hash currency check that backed a per-run approval requirement for real
input. All three are gone: real material never reaches git regardless of any
per-run sign-off, so the requirement bought nothing. What remains — and what this
file now covers — is the approval-record contract itself (`exclusion` and
`salvage-promotion` still need Tyrel's approval under GOVERNANCE 1) and the closed
fixture-or-real ingress record every run authority carries.
"""

from pathlib import Path

import pytest

from common.contracts.approval import (
    ACTIONS,
    MAX_APPROVAL_REASON_BYTES,
    MAX_APPROVAL_SUBJECTS,
    REAL_INGRESS,
    SYNTHETIC_FIXTURE_INGRESS,
    build_approval_record,
    parse_ingress_record,
    real_ingress_record,
    synthetic_fixture_ingress_record,
    validate_approval_record,
)
from common.contracts.canonical import self_hash
from common.contracts.errors import ApprovalRefusal


def approval(*, action="exclusion", target=None, timestamp="2026-08-04T12:00:00Z"):
    return build_approval_record(
        subject_ids=["some-subject"],
        action=action,
        reason="a reviewable reason a reader six weeks out can use",
        target_version_hash=target or "a" * 64,
        timestamp=timestamp,
    )


# --- data-gate is gone as an action, not merely disused ---------------------------


def test_data_gate_is_not_an_approvable_action():
    """Real input no longer needs a per-run approval, and this action no longer
    exists to claim one against. `exclusion` and `salvage-promotion` remain —
    GOVERNANCE 1 still requires Tyrel's approval for an exclusion."""
    assert "data-gate" not in ACTIONS
    assert set(ACTIONS) == {"exclusion", "salvage-promotion", "other"}
    with pytest.raises(ApprovalRefusal, match="not one of"):
        approval(action="data-gate")


# --- The record's shape: builder and validator agree exactly ---------------------


@pytest.mark.parametrize("timestamp", ["", "   ", None, 20260804])
def test_the_builder_refuses_a_timestamp_the_validator_would_reject(timestamp):
    """Builder and validator must agree; a self-hash proves no semantic validity."""
    with pytest.raises(ApprovalRefusal, match="no timestamp"):
        build_approval_record(
            subject_ids=["exclusion-subject"],
            action="exclusion",
            reason="a reviewable reason a reader six weeks out can use",
            target_version_hash="a" * 64,
            timestamp=timestamp,
        )


def test_a_well_formed_record_validates_unchanged():
    record = approval()
    assert validate_approval_record(record) == record


def test_an_unknown_action_is_refused():
    with pytest.raises(ApprovalRefusal, match="not one of"):
        approval(action="not-a-real-action")


def test_an_approval_naming_someone_other_than_tyrel_is_refused():
    record = approval()
    record["approver"] = "an agent"
    with pytest.raises(ApprovalRefusal, match="only Tyrel approves"):
        validate_approval_record(record)


def test_an_edited_record_fails_its_own_self_hash():
    record = approval()
    record["reason"] = "quietly widened after the fact"
    with pytest.raises(ApprovalRefusal, match="self-hash"):
        validate_approval_record(record)


def test_a_non_sha256_target_version_is_refused():
    with pytest.raises(ApprovalRefusal, match="lowercase sha256"):
        approval(target="not-a-digest")


def test_an_approval_that_names_no_subject_approves_nothing():
    with pytest.raises(ApprovalRefusal, match="names no subject"):
        build_approval_record(
            subject_ids=[],
            action="exclusion",
            reason="a reviewable reason a reader six weeks out can use",
            target_version_hash="a" * 64,
            timestamp="2026-08-04T12:00:00Z",
        )


def test_the_builder_refuses_a_string_instead_of_splitting_it_into_subjects():
    with pytest.raises(ApprovalRefusal, match="names no subject"):
        build_approval_record(
            subject_ids="not-a-list",
            action="exclusion",
            reason="a reviewable reason",
            target_version_hash="a" * 64,
            timestamp="2026-08-04T12:00:00Z",
        )


def test_the_builder_names_a_non_string_reason_as_an_approval_refusal():
    with pytest.raises(ApprovalRefusal, match="no reason"):
        build_approval_record(
            subject_ids=["x"],
            action="exclusion",
            reason=1,
            target_version_hash="a" * 64,
            timestamp="2026-08-04T12:00:00Z",
        )


def test_the_builder_bounds_subject_count_before_sorting_or_hashing_it():
    with pytest.raises(ApprovalRefusal, match=f"more than {MAX_APPROVAL_SUBJECTS} subjects"):
        build_approval_record(
            subject_ids=[f"subject-{index}" for index in range(MAX_APPROVAL_SUBJECTS + 1)],
            action="exclusion",
            reason="a reviewable reason",
            target_version_hash="a" * 64,
            timestamp="2026-08-04T12:00:00Z",
        )


def test_the_builder_bounds_reason_bytes_before_hashing_them():
    with pytest.raises(ApprovalRefusal, match=f"{MAX_APPROVAL_REASON_BYTES} UTF-8 bytes"):
        build_approval_record(
            subject_ids=["x"],
            action="exclusion",
            reason="x" * (MAX_APPROVAL_REASON_BYTES + 1),
            target_version_hash="a" * 64,
            timestamp="2026-08-04T12:00:00Z",
        )


def test_an_empty_reason_is_refused():
    with pytest.raises(ApprovalRefusal, match="unreviewable"):
        build_approval_record(
            subject_ids=["x"],
            action="exclusion",
            reason="   ",
            target_version_hash="a" * 64,
            timestamp="2026-08-04T12:00:00Z",
        )


def test_a_record_missing_a_required_field_is_refused():
    record = approval()
    del record["reason"]
    with pytest.raises(ApprovalRefusal, match="missing"):
        validate_approval_record(record)


def test_a_resealed_record_with_an_extra_field_is_not_the_approval_schema():
    record = approval()
    record["unbounded_extension"] = {"nested": ["not", "approval", "evidence"]}
    record["self_hash"] = self_hash(record)

    with pytest.raises(ApprovalRefusal, match="unexpected fields"):
        validate_approval_record(record)


def test_a_non_string_extra_field_is_a_named_schema_refusal_not_a_sorting_crash():
    record = approval()
    record[1] = "not a JSON object key"
    # Two unexpected keys, of two types, because one key is never sorted against
    # anything. With a single offender this test stayed green after `key=repr`
    # was deleted -- it named a crash it could not reach.
    record["also unexpected"] = "a second offender"

    with pytest.raises(ApprovalRefusal, match="unexpected fields"):
        validate_approval_record(record)


def test_a_resealed_subject_permutation_is_not_a_second_content_address_for_one_approval():
    record = build_approval_record(
        subject_ids=["a", "b"],
        action="exclusion",
        reason="a reviewable reason",
        target_version_hash="a" * 64,
        timestamp="2026-08-04T12:00:00Z",
    )
    record["subject_ids"] = ["b", "a"]
    record["self_hash"] = self_hash(record)

    with pytest.raises(ApprovalRefusal, match="canonical order"):
        validate_approval_record(record)


# --- The closed ingress record: fixture or real, and nothing else ----------------


def test_synthetic_fixture_ingress_round_trips():
    assert parse_ingress_record(synthetic_fixture_ingress_record()) == SYNTHETIC_FIXTURE_INGRESS


def test_real_ingress_round_trips():
    assert parse_ingress_record(real_ingress_record()) == REAL_INGRESS


def test_real_ingress_carries_no_approval_evidence():
    """A real run's ingress used to also carry a data-gate policy hash and an
    approval reference. Neither exists any more: it is just which route created
    the run."""
    assert real_ingress_record() == {"mode": "real"}


def test_an_unknown_ingress_mode_is_refused():
    with pytest.raises(ApprovalRefusal, match="closed fixture-or-real record"):
        parse_ingress_record({"mode": "something-else"})


def test_an_ingress_record_with_extra_fields_is_refused():
    with pytest.raises(ApprovalRefusal, match="closed fixture-or-real record"):
        parse_ingress_record({"mode": "real", "extra": "field"})


def test_a_non_dict_ingress_record_is_refused():
    with pytest.raises(ApprovalRefusal, match="closed fixture-or-real record"):
        parse_ingress_record("real")


# The three places the approval builder and writer may legitimately appear: the
# module that defines the builder, the store that defines the writer, and the
# package that re-exports the builder for tests and operator tooling. Anything
# else under `pipeline/` or `common/` would be pipeline code minting its own
# approval. Listed exactly, so a fourth entry is a deliberate, reviewable act.
APPROVAL_MINTING_MODULES = frozenset(
    {
        "common/contracts/approval.py",
        "common/contracts/__init__.py",
        "common/runtree/store.py",
    }
)


def test_no_pipeline_module_mints_its_own_approval_record():
    """GOVERNANCE: "No automated agent may act as the human in any rule here."

    `approver` is a string compare against a constant this module stamps itself,
    so a record's authority rests entirely on *who wrote the file* -- nothing in
    the bytes distinguishes Tyrel's record from one a stage wrote for itself. The
    gates that consume approval records (spec 08's two sampled Perlector arms)
    therefore depend on production code never reaching the builder or the writer.
    An unused writer leaves no runtime trace, so this reads the source: a stage
    that grows an approval of its own fails here even though no test calls it.

    Source inspection cannot authenticate a record placed by a writer with run-tree
    access; that stronger guarantee requires an out-of-band signature.
    """
    root = Path(__file__).resolve().parents[2]
    offenders = []
    # Fails closed on its own subject. `rglob` over a directory that is not there
    # yields nothing and raises nothing, so a renamed or moved `pipeline/` would
    # leave `offenders` empty and this guard green while it inspected no stage
    # source at all — the one check on a stage minting its own approval, dead and
    # reporting success. A check that cannot run is a failure, not a pass.
    scanned = 0
    for area in ("pipeline", "common"):
        assert (root / area).is_dir(), f"{area}/ is not where this guard looks for stage source"
        for path in sorted((root / area).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if path.name.startswith("test_") or path.name == "conftest.py":
                continue
            if relative in APPROVAL_MINTING_MODULES:
                continue
            source = path.read_text(encoding="utf-8")
            scanned += 1
            if "build_approval_record" in source or "write_approval_record" in source:
                offenders.append(relative)
    assert scanned > 20, f"only {scanned} modules were inspected; the scan lost its subject"
    assert not offenders, (
        f"{offenders} mint or store an approval record from pipeline code; only Tyrel "
        "approves, and a stage that writes its own approval has approved itself"
    )


def test_the_minting_exemption_list_names_only_files_that_exist():
    """An exemption for a moved or deleted module would silently widen the rule."""
    root = Path(__file__).resolve().parents[2]
    assert all((root / relative).is_file() for relative in APPROVAL_MINTING_MODULES)
