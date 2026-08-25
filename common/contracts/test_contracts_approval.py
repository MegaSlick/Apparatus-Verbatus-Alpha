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
    REAL_INGRESS,
    SYNTHETIC_FIXTURE_INGRESS,
    build_approval_record,
    parse_ingress_record,
    real_ingress_record,
    synthetic_fixture_ingress_record,
    validate_approval_record,
)
from common.contracts.errors import ApprovalRefusal


def approval(*, action="exclusion", target=None, timestamp="2026-08-04T12:00:00Z"):
    return build_approval_record(
        subject_ids=["some-subject"],
        action=action,
        reason="a reviewable reason a reader six weeks out can use",
        target_version_hash=target or "a" * 64,
        timestamp=timestamp,
    )


def test_data_gate_is_not_an_approvable_action():
    """Real input no longer needs a per-run approval, and this action no longer
    exists to claim one against. `exclusion` and `salvage-promotion` remain —
    GOVERNANCE 1 still requires Tyrel's approval for an exclusion."""
    assert "data-gate" not in ACTIONS
    assert set(ACTIONS) == {"advance", "exclusion", "salvage-promotion", "other"}
    with pytest.raises(ApprovalRefusal, match="not one of"):
        approval(action="data-gate")


@pytest.mark.parametrize("timestamp", ["", "   ", None, 20260804])
def test_the_builder_refuses_a_timestamp_the_validator_would_reject(timestamp):
    """The builder and the validator have to accept exactly the same records. The
    validator refuses a blank or non-string timestamp; the builder did not check it
    at all, so a caller could seal one no reader would ever accept back — and its
    self-hash would verify happily, because a hash covers whatever bytes were sealed
    rather than whether they meant anything."""
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


def test_synthetic_fixture_ingress_round_trips():
    assert parse_ingress_record(synthetic_fixture_ingress_record()) == SYNTHETIC_FIXTURE_INGRESS


def test_real_ingress_round_trips():
    assert parse_ingress_record(real_ingress_record()) == REAL_INGRESS


def test_real_ingress_carries_no_approval_evidence():
    """Real ingress names only its route; it carries no approval or policy authority."""
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

    Deliberately not a runtime check. This is a statement about the repository,
    and the honest enforcement of it is that the fact is checked and named rather
    than assumed. What it cannot do is stop a human, an operator script, or an
    agent with write access from placing a well-formed record in a run tree by
    hand; that residual is real, recorded here, and not closable without an
    out-of-band signature this project has not adopted.
    """
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for area in ("pipeline", "common"):
        for path in sorted((root / area).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if path.name.startswith("test_") or path.name == "conftest.py":
                continue
            if relative in APPROVAL_MINTING_MODULES:
                continue
            source = path.read_text(encoding="utf-8")
            if "build_approval_record" in source or "write_approval_record" in source:
                offenders.append(relative)
    assert not offenders, (
        f"{offenders} mint or store an approval record from pipeline code; only Tyrel "
        "approves, and a stage that writes its own approval has approved itself"
    )


def test_the_minting_exemption_list_names_only_files_that_exist():
    """An exemption for a moved or deleted module would silently widen the rule."""
    root = Path(__file__).resolve().parents[2]
    assert all((root / relative).is_file() for relative in APPROVAL_MINTING_MODULES)
