"""The approval-record contract: schema, self-hash, and the closed ingress record.

Covers the approval record itself (an `exclusion` or a `salvage-promotion` needs the
project lead's approval) and the closed fixture-or-real ingress record every run
authority carries. Real input needs no per-run approval: real material never reaches
git whatever is signed off, so no action exists to approve it.
"""

from pathlib import Path

import pytest

from common.contracts.approval import (
    ACTIONS,
    FINDINGS,
    MAX_APPROVAL_REASON_BYTES,
    MAX_APPROVAL_SUBJECTS,
    PAGE_DECISIONS,
    REAL_INGRESS,
    SYNTHETIC_FIXTURE_INGRESS,
    UNIT_DECISIONS,
    build_approval_record,
    build_review_decision_record,
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


def test_the_approvable_actions_are_exactly_these_four():
    """No per-run approval of real input exists to be claimed, so `data-gate` is
    refused; an exclusion still needs the project lead's approval."""
    assert "data-gate" not in ACTIONS
    assert set(ACTIONS) == {"advance", "exclusion", "salvage-promotion", "review", "other"}
    with pytest.raises(ApprovalRefusal, match="not one of"):
        approval(action="data-gate")


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


def test_an_approval_naming_someone_other_than_the_project_lead_is_refused():
    record = approval()
    record["approver"] = "an agent"
    with pytest.raises(ApprovalRefusal, match="only the project lead"):
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


def test_a_non_string_extra_field_is_a_named_schema_refusal_not_a_sorting_crash():
    record = approval()
    record[1] = "not a JSON object key"
    # Two unexpected keys, of two types, because one key is never sorted against
    # anything. With a single offender this test stayed green after `key=repr`
    # was deleted -- it named a crash it could not reach.
    record["also unexpected"] = "a second offender"

    with pytest.raises(ApprovalRefusal, match="unexpected fields"):
        validate_approval_record(record)


# --- approval-record.v1: an operator review decision ----------------------------


def decision(**overrides):
    fields = {
        "run_id": "run-1",
        "scope": "unit",
        "subject_id": "act-1",
        "page_id": "page-1",
        "decision": "release",
        "finding": None,
        "basis_digest": "b" * 64,
        "reason": "the reading matches the ink",
        "timestamp": "2026-09-30T12:00:00Z",
    }
    fields.update(overrides)
    return build_review_decision_record(**fields)


def resealed(record):
    record.pop("self_hash")
    record["self_hash"] = self_hash(record)
    return record


def test_a_review_decision_is_v1_bound_to_its_basis_and_validates_unchanged():
    record = decision()
    assert record["schema"] == "approval-record.v1"
    assert record["action"] == "review"
    assert record["subject_ids"] == ["act-1"]
    assert record["target_version_hash"] == "b" * 64
    assert record["review"] == {
        "run_id": "run-1",
        "scope": "unit",
        "page_id": "page-1",
        "decision": "release",
        "finding": None,
    }
    assert validate_approval_record(record) == record


def test_v0_records_are_still_read_and_still_written_for_advance_and_exclusion():
    for action in ("advance", "exclusion"):
        record = approval(action=action)
        assert record["schema"] == "approval-record.v0"
        assert validate_approval_record(record) == record


def test_the_v0_builder_refuses_the_review_action():
    with pytest.raises(ApprovalRefusal, match="approval-record.v1"):
        approval(action="review")


def test_a_v0_record_claiming_the_review_action_is_refused():
    record = approval()
    record["action"] = "review"
    with pytest.raises(ApprovalRefusal, match="the review action is approval-record.v1's"):
        validate_approval_record(resealed(record))


def test_a_v1_record_with_another_action_is_refused():
    record = decision()
    record["action"] = "exclusion"
    with pytest.raises(ApprovalRefusal, match="the review action is approval-record.v1's"):
        validate_approval_record(resealed(record))


def test_a_v1_record_without_its_review_block_is_refused():
    record = decision()
    del record["review"]
    with pytest.raises(ApprovalRefusal, match="missing"):
        validate_approval_record(resealed(record))


def test_a_v0_record_carrying_a_review_block_is_refused():
    record = approval()
    record["review"] = decision()["review"]
    with pytest.raises(ApprovalRefusal, match="unexpected fields"):
        validate_approval_record(resealed(record))


def test_the_review_block_is_closed():
    record = decision()
    record["review"]["corrected_text"] = "a transcription"
    with pytest.raises(ApprovalRefusal, match="review block must hold exactly"):
        validate_approval_record(resealed(record))


def test_an_edited_review_decision_fails_its_self_hash():
    record = decision()
    record["review"]["decision"] = "exclude"
    with pytest.raises(ApprovalRefusal, match="self-hash"):
        validate_approval_record(record)


@pytest.mark.parametrize("name", UNIT_DECISIONS)
def test_every_unit_decision_builds_in_unit_scope(name):
    finding = "text-misread" if name == "hold" else None
    assert validate_approval_record(decision(decision=name, finding=finding))


@pytest.mark.parametrize("name", PAGE_DECISIONS)
def test_every_page_decision_builds_in_page_scope(name):
    finding = "other" if name == "hold" else None
    record = decision(scope="page", subject_id="page-1", decision=name, finding=finding)
    assert validate_approval_record(record)


@pytest.mark.parametrize(
    ("scope", "name"),
    [("unit", "no-missed-act"), ("unit", "re-shoot"), ("page", "release"), ("page", "exclude")],
)
def test_a_decision_outside_its_scope_is_refused(scope, name):
    with pytest.raises(ApprovalRefusal, match=f"is not a {scope} decision"):
        decision(scope=scope, subject_id="page-1", decision=name)


@pytest.mark.parametrize("name", ["correct-text", "split", "merge", "clear-continuation-link"])
def test_correcting_splitting_merging_and_unlinking_are_not_decisions(name):
    with pytest.raises(ApprovalRefusal, match="is not a unit decision"):
        decision(decision=name)


def test_a_hold_names_a_finding_from_the_closed_list():
    assert "text-misread" in FINDINGS
    with pytest.raises(ApprovalRefusal, match="names its finding"):
        decision(decision="hold")
    with pytest.raises(ApprovalRefusal, match="names its finding"):
        decision(decision="hold", finding="the text should read Jean")


def test_only_a_hold_names_a_finding():
    with pytest.raises(ApprovalRefusal, match="only a hold names a finding"):
        decision(finding="text-misread")


def test_a_page_decision_names_its_own_page():
    with pytest.raises(ApprovalRefusal, match="subject is the page it names"):
        decision(scope="page", subject_id="page-2", decision="missed-act")


def test_a_review_decision_names_exactly_one_subject():
    record = decision()
    record["subject_ids"] = ["act-1", "act-2"]
    with pytest.raises(ApprovalRefusal, match="exactly one subject"):
        validate_approval_record(resealed(record))


@pytest.mark.parametrize("field", ["run_id", "page_id"])
def test_a_review_decision_names_its_run_and_page(field):
    with pytest.raises(ApprovalRefusal, match=f"{field} must be non-blank"):
        decision(**{field: " "})


def test_a_review_decision_is_bound_to_a_basis_digest():
    with pytest.raises(ApprovalRefusal, match="lowercase sha256"):
        decision(basis_digest="not-a-digest")


# --- The closed ingress record: fixture or real, and nothing else ----------------


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
    """No automated agent may act as the project lead: only the lead approves.

    `approver` is a string compare against a constant this module stamps itself,
    so a record's authority rests entirely on *who wrote the file* -- nothing in
    the bytes distinguishes the project lead's record from one a stage wrote for itself. The
    gates that consume approval records (the Perlector's two sampled arms)
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
            if any(
                name in source
                for name in (
                    "build_approval_record",
                    "build_review_decision_record",
                    "write_approval_record",
                )
            ):
                offenders.append(relative)
    assert scanned > 20, f"only {scanned} modules were inspected; the scan lost its subject"
    assert not offenders, (
        f"{offenders} mint or store an approval record from pipeline code; only the "
        "project lead approves, and a stage that writes its own approval has approved itself"
    )


def test_the_minting_exemption_list_names_only_files_that_exist():
    """An exemption for a moved or deleted module would silently widen the rule."""
    root = Path(__file__).resolve().parents[2]
    assert all((root / relative).is_file() for relative in APPROVAL_MINTING_MODULES)
