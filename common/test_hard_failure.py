"""The run-level hard-failure cap: policy loading and the disk-recomputed tally.

Meta-invariant #86 in spirit: the tally is exercised against a real `RunTree`
publishing real envelopes, not a hand-built dict standing in for one, so a
change to what `build_manifest` actually verifies would be visible here too.
"""

from pathlib import Path

import pytest

from common.contracts.canonical import digest_bytes
from common.contracts.envelope import build_envelope
from common.contracts.errors import ContractError
from common.contracts.identities import artifact_id
from common.contracts.stages import ARCHETYPUS, DESIGNATOR, DOOR, PERLECTOR, RECENSOR
from common.hard_failure import (
    DEFAULT_HARD_FAILURE_CONFIG_PATH,
    RULED_THRESHOLD,
    load_hard_failure_policy,
    tally_hard_failures,
)
from common.runtree.store import RunTree

CONFIG_DIGEST = "d" * 64
SOURCE = [{"relative_path": "proof/page-1.png", "sha256": "e" * 64, "ordinal": 1}]
RECIPES = {
    "door": "fake-door-v0",
    "designator": "fake-designator-v0",
    "perlector": "fake-perlector-v0",
    "recensor": "fake-recensor-v0",
    "archetypus": "fake-archetypus-v0",
}


def make_run(tmp_path, run_id="r1"):
    return RunTree.create(
        tmp_path,
        run_id,
        source_manifest=SOURCE,
        config_digest=CONFIG_DIGEST,
        adapter_recipes=RECIPES,
        witness_chairs=["attestator_1"],
    )


def publish(
    tree,
    *,
    stage,
    kind,
    subject,
    outcome,
    adapter_revision,
    attempt=None,
    inputs=None,
    **payload,
):
    envelope = build_envelope(
        run_id=tree.run_id,
        artifact_id=artifact_id(stage, kind, subject, attempt),
        subject_id=subject,
        stage=stage,
        kind=kind,
        outcome=outcome,
        config_digest=CONFIG_DIGEST,
        adapter_revision=adapter_revision,
        inputs=inputs or [],
        payload=payload or {"x": 1},
        attempt=attempt,
    )
    tree.publish_artifact(envelope)
    tree.write_manifest(stage)


def write_policy(tmp_path, text: str) -> Path:
    path = tmp_path / "hard_failure.toml"
    path.write_text(text, encoding="utf-8")
    return path


# --- The shipped default -------------------------------------------------------


def test_the_shipped_default_config_loads_at_the_ruled_boundary():
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    assert policy["threshold"] == RULED_THRESHOLD
    assert (PERLECTOR, "failed") in policy["kinds"]
    assert (DESIGNATOR, "failed") in policy["kinds"]
    assert (RECENSOR, "failed") in policy["kinds"]
    assert (ARCHETYPUS, "refused") in policy["kinds"]
    assert ("door", "refused", "corrupt") in policy["reason_kinds"]
    assert ("door", "refused", "unreadable") in policy["reason_kinds"]
    # `truncated` is a dense page, not a damaged one (the old pipeline's own
    # Tyrel-ruled distinction) -- a bounded-retry matter, never the run-level
    # systemic-breakage signal. See config/hard_failure.toml's own comment.
    assert (PERLECTOR, "truncated") not in policy["kinds"]
    # The exclusions argued in the config's own comments.
    assert ("door", "refused") not in policy["kinds"]
    assert ("exemplar", "refused") not in policy["kinds"]
    assert ("recensor", "held-for-review") not in policy["kinds"]
    assert ("attestatores", "failed") not in policy["kinds"]


# --- Policy validation: refuse before any tally is trusted ----------------------


@pytest.mark.parametrize("threshold", [0, 1, 3])
def test_a_threshold_other_than_the_ruled_value_is_refused(tmp_path, threshold):
    path = write_policy(
        tmp_path,
        f'threshold = {threshold}\n[[kind]]\nstage = "perlector"\noutcome = "failed"\n',
    )
    with pytest.raises(ContractError, match="ruled value is exactly"):
        load_hard_failure_policy(path)


def test_an_unclassified_outcome_pair_is_refused(tmp_path):
    path = write_policy(
        tmp_path, 'threshold = 2\n[[kind]]\nstage = "perlector"\noutcome = "read"\n'
    )
    with pytest.raises(ContractError, match="does not classify FAILED"):
        load_hard_failure_policy(path)


def test_a_misspelled_outcome_names_itself_in_the_refusal(tmp_path):
    """A typo'd outcome is not a member of the stage's outcome algebra at all --
    a different refusal than a real outcome classifying the wrong way -- and the
    message should let a config author spot their own typo rather than just
    learn something was rejected."""
    path = write_policy(
        tmp_path, 'threshold = 2\n[[kind]]\nstage = "perlector"\noutcome = "faild"\n'
    )
    with pytest.raises(ContractError, match="in no terminal set") as caught:
        load_hard_failure_policy(path)
    assert "perlector" in str(caught.value)
    assert "faild" in str(caught.value)


def test_an_unresolved_outcome_pair_is_refused(tmp_path):
    """`held-for-review` is exactly the ordinary, working shape of this pipeline;
    configuring it into a systemic-breakage cap would stop every real run."""
    path = write_policy(
        tmp_path, 'threshold = 2\n[[kind]]\nstage = "recensor"\noutcome = "held-for-review"\n'
    )
    with pytest.raises(ContractError, match="does not classify FAILED"):
        load_hard_failure_policy(path)


def test_an_unknown_stage_is_refused(tmp_path):
    path = write_policy(
        tmp_path, 'threshold = 2\n[[kind]]\nstage = "chandra"\noutcome = "failed"\n'
    )
    with pytest.raises(ContractError, match="unknown stage"):
        load_hard_failure_policy(path)


def test_a_duplicate_kind_is_refused(tmp_path):
    path = write_policy(
        tmp_path,
        "threshold = 2\n"
        '[[kind]]\nstage = "perlector"\noutcome = "failed"\n'
        '[[kind]]\nstage = "perlector"\noutcome = "failed"\n',
    )
    with pytest.raises(ContractError, match="more than once"):
        load_hard_failure_policy(path)


def test_no_kind_entries_is_refused(tmp_path):
    path = write_policy(tmp_path, "threshold = 2\n")
    with pytest.raises(ContractError, match="no \\[\\[kind\\]\\] entries"):
        load_hard_failure_policy(path)


def test_a_negative_threshold_is_refused(tmp_path):
    path = write_policy(
        tmp_path, 'threshold = -1\n[[kind]]\nstage = "perlector"\noutcome = "failed"\n'
    )
    with pytest.raises(ContractError, match="non-negative integer threshold"):
        load_hard_failure_policy(path)


def test_an_unreadable_path_is_refused(tmp_path):
    with pytest.raises(ContractError, match="could not be read as a policy"):
        load_hard_failure_policy(tmp_path / "does-not-exist.toml")


# --- The tally: computed from disk, never from a running counter ---------------


def test_the_tally_is_zero_over_an_empty_run(tmp_path):
    tree = make_run(tmp_path)
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally == {
        "threshold": RULED_THRESHOLD,
        "count": 0,
        "breached": False,
        "by_kind": {
            **{f"{stage}:{outcome}": [] for stage, outcome in sorted(policy["kinds"])},
            **{
                f"{stage}:{outcome}:{reason}": []
                for stage, outcome, reason in sorted(policy["reason_kinds"])
            },
        },
        "instrument_by_kind": {},
        "instrument_count": 0,
        "subjects": [],
    }


def test_one_hard_failure_is_a_fluke_and_does_not_breach(tmp_path):
    tree = make_run(tmp_path)
    publish(
        tree,
        stage=PERLECTOR,
        kind="perlectio",
        subject="act_0000000000000001",
        outcome="failed",
        adapter_revision="fake-perlector-v0",
    )
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally["count"] == 1
    assert tally["breached"] is False


def test_a_record_only_tally_leaves_stale_lineage_to_its_consumer_boundary(tmp_path):
    """Record-only tallying leaves stale lineage for its consumer to diagnose."""
    tree = make_run(tmp_path)
    source = tree.resolve("source/input.bin")
    source.parent.mkdir(parents=True)
    source.write_bytes(b"sealed input")
    publish(
        tree,
        stage=PERLECTOR,
        kind="perlectio",
        subject="act_0000000000000001",
        outcome="failed",
        adapter_revision="fake-perlector-v0",
        inputs=[
            {
                "relative_path": str(source.relative_to(tree.root)),
                "sha256": digest_bytes(source.read_bytes()),
            }
        ],
    )
    source.write_bytes(b"changed input")
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)

    with pytest.raises(ContractError, match="bytes changed under a sealed reference"):
        tally_hard_failures(tree, policy)

    tally = tally_hard_failures(tree, policy, verify_inputs=False)
    assert tally["count"] == 1
    assert tally["subjects"] == ["perlector:act_0000000000000001"]


def test_instrument_arm_failures_are_visible_but_do_not_spend_the_ruled_cap(tmp_path):
    tree = make_run(tmp_path)
    for kind in ("lectio-nuda", "lectio-prior", "primed-without-prior"):
        publish(
            tree,
            stage=PERLECTOR,
            kind=kind,
            subject=f"act_{kind}",
            outcome="failed",
            adapter_revision="fake-perlector-v0",
        )
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally["count"] == 0
    assert tally["breached"] is False
    assert tally["instrument_by_kind"]["perlector:failed"] == [
        "act_lectio-nuda",
        "act_lectio-prior",
        "act_primed-without-prior",
    ]
    assert tally["instrument_count"] == 3


def test_a_production_perlectio_failure_still_spends_the_ruled_cap(tmp_path):
    tree = make_run(tmp_path)
    publish(
        tree,
        stage=PERLECTOR,
        kind="perlectio",
        subject="act_production",
        outcome="failed",
        adapter_revision="fake-perlector-v0",
    )
    tally = tally_hard_failures(tree, load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH))
    assert tally["count"] == 1
    assert tally["instrument_by_kind"] == {}
    assert tally["instrument_count"] == 0


def test_a_subject_failing_on_both_arms_appears_in_both_lists_and_spends_the_cap(tmp_path):
    """The partition's documented delicate case: the instrument arm neither
    excuses nor doubles the production incident for the same subject."""

    tree = make_run(tmp_path)
    publish(
        tree,
        stage=PERLECTOR,
        kind="perlectio",
        subject="act_both_arms",
        outcome="failed",
        adapter_revision="fake-perlector-v0",
    )
    publish(
        tree,
        stage=PERLECTOR,
        kind="lectio-nuda",
        subject="act_both_arms",
        outcome="failed",
        adapter_revision="fake-perlector-v0",
    )
    tally = tally_hard_failures(tree, load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH))
    assert tally["count"] == 1
    assert tally["by_kind"]["perlector:failed"] == ["act_both_arms"]
    assert tally["instrument_by_kind"]["perlector:failed"] == ["act_both_arms"]
    assert tally["instrument_count"] == 1


def test_exactly_two_hard_failures_is_an_early_warning_and_does_not_breach(tmp_path):
    tree = make_run(tmp_path)
    publish(
        tree,
        stage=PERLECTOR,
        kind="perlectio",
        subject="act_0000000000000001",
        outcome="failed",
        adapter_revision="fake-perlector-v0",
    )
    publish(
        tree,
        stage=DOOR,
        kind="admission",
        subject="source-0000000000000002",
        outcome="refused",
        adapter_revision="fake-door-v0",
        reason="corrupt: structural validation failed",
    )
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally["count"] == 2
    assert tally["breached"] is False, "two is an early warning, not a halt (Tyrel's ruling)"


def test_a_third_hard_failure_breaches(tmp_path):
    tree = make_run(tmp_path)
    publish(
        tree,
        stage=PERLECTOR,
        kind="perlectio",
        subject="act_0000000000000001",
        outcome="failed",
        adapter_revision="fake-perlector-v0",
    )
    publish(
        tree,
        stage=DOOR,
        kind="admission",
        subject="source-0000000000000002",
        outcome="refused",
        adapter_revision="fake-door-v0",
        reason="unreadable: the page would not decode",
    )
    publish(
        tree,
        stage=RECENSOR,
        kind="review",
        subject="act_0000000000000003",
        outcome="failed",
        adapter_revision="fake-recensor-v0",
    )
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally["count"] == 3
    assert tally["breached"] is True
    assert tally["subjects"] == [
        "door:source-0000000000000002",
        "perlector:act_0000000000000001",
        "recensor:act_0000000000000003",
    ]
    assert tally["by_kind"]["perlector:failed"] == ["act_0000000000000001"]
    assert tally["by_kind"]["door:refused:unreadable"] == ["source-0000000000000002"]
    assert tally["by_kind"]["recensor:failed"] == ["act_0000000000000003"]


def test_a_recovered_act_still_counts_the_incident_that_happened(tmp_path):
    """A hard failure that was later recovered away is still an incident: coverage
    recovery does not erase the record that a failure occurred (GOVERNANCE 2)."""
    tree = make_run(tmp_path)
    publish(
        tree,
        stage=PERLECTOR,
        kind="perlectio",
        subject="act_0000000000000001",
        outcome="failed",
        adapter_revision="fake-perlector-v0",
        attempt="att_0000000000000001",
    )
    publish(
        tree,
        stage=PERLECTOR,
        kind="perlectio",
        subject="act_0000000000000001",
        outcome="read",
        adapter_revision="fake-perlector-v0",
        attempt="att_0000000000000002",
    )
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally["count"] == 1
    assert tally["subjects"] == ["perlector:act_0000000000000001"]
    assert tally["by_kind"]["perlector:failed"] == ["act_0000000000000001"]


def test_repeated_failed_attempts_are_one_incident_in_the_total_and_evidence(tmp_path):
    """The per-kind evidence must use the same incident identity as the count."""
    tree = make_run(tmp_path)
    for attempt in ("att_0000000000000001", "att_0000000000000002"):
        publish(
            tree,
            stage=PERLECTOR,
            kind="perlectio",
            subject="act_0000000000000001",
            outcome="failed",
            adapter_revision="fake-perlector-v0",
            attempt=attempt,
        )
    tally = tally_hard_failures(tree, load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH))
    assert tally["count"] == 1
    assert tally["by_kind"]["perlector:failed"] == ["act_0000000000000001"]


def test_two_failing_stages_on_the_same_act_count_as_two_incidents(tmp_path):
    """Counted as (stage, subject) pairs: a genuinely different failure mode on
    the same act is a second incident, not the same one counted twice."""
    tree = make_run(tmp_path)
    publish(
        tree,
        stage=PERLECTOR,
        kind="perlectio",
        subject="act_0000000000000001",
        outcome="failed",
        adapter_revision="fake-perlector-v0",
    )
    publish(
        tree,
        stage=RECENSOR,
        kind="review",
        subject="act_0000000000000001",
        outcome="failed",
        adapter_revision="fake-recensor-v0",
    )
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally["count"] == 2


def test_an_ordinary_held_for_review_never_counts(tmp_path):
    """The expected, working shape of a run -- a hold, a recovery request -- must
    never be mistaken for the systemic-breakage signal this cap exists to catch."""
    tree = make_run(tmp_path)
    publish(
        tree,
        stage=RECENSOR,
        kind="review",
        subject="act_0000000000000001",
        outcome="held-for-review",
        adapter_revision="fake-recensor-v0",
    )
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally["count"] == 0
    assert tally["breached"] is False


# --- Reason-scoped kinds: the old pipeline's own hard/soft split -----------------


def test_a_truncated_reading_never_counts_toward_the_run_level_cap(tmp_path):
    """The old pipeline's own Tyrel-ruled distinction (page_health.py, 2026-07-25):
    a dense page is not a damaged one. Three truncated Perlectiones is heavy
    per-act recovery traffic, never evidence the run itself is going wrong."""
    tree = make_run(tmp_path)
    for ordinal in (1, 2, 3):
        publish(
            tree,
            stage=PERLECTOR,
            kind="perlectio",
            subject=f"act_000000000000000{ordinal}",
            outcome="truncated",
            adapter_revision="fake-perlector-v0",
        )
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally["count"] == 0
    assert tally["breached"] is False


def test_a_door_refusal_for_an_unmatched_reason_does_not_count(tmp_path):
    """`unrecognized-format` is routine bulk-corpus noise (a non-image file that
    should never have been submitted), not "a corrupt or unrenderable image" --
    the reason-scoped kind must not widen into every door refusal."""
    tree = make_run(tmp_path)
    publish(
        tree,
        stage=DOOR,
        kind="admission",
        subject="source-0000000000000001",
        outcome="refused",
        adapter_revision="fake-door-v0",
        reason="unrecognized-format: no decoder claimed these bytes",
    )
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally["count"] == 0
    assert tally["by_kind"]["door:refused:corrupt"] == []
    assert tally["by_kind"]["door:refused:unreadable"] == []


def test_a_door_refusal_with_no_reason_field_does_not_crash_the_reason_match(tmp_path):
    """A payload missing `reason` entirely (or carrying a non-string) has no code,
    never a crash -- `_reason_code` is total over anything a payload might hold."""
    tree = make_run(tmp_path)
    publish(
        tree,
        stage=DOOR,
        kind="admission",
        subject="source-0000000000000001",
        outcome="refused",
        adapter_revision="fake-door-v0",
    )
    publish(
        tree,
        stage=DOOR,
        kind="admission",
        subject="source-0000000000000002",
        outcome="refused",
        adapter_revision="fake-door-v0",
        reason=12,
    )
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally["count"] == 0


def test_a_door_refusal_whose_reason_code_merely_starts_the_same_does_not_count(tmp_path):
    """`corrupted` is not `corrupt`. The reason match is exact, not a prefix.

    The existing near-miss coverage uses `unrecognized-format`, which shares no
    prefix with either hard reason -- so it would still pass if the comparison
    were ever loosened to `startswith`. A loosened match widens what the cap
    counts, and three ordinary door refusals would then halt a whole run at the
    next stage boundary on routine noise. Found by CodeRabbit.
    """
    tree = make_run(tmp_path)
    publish(
        tree,
        stage=DOOR,
        kind="admission",
        subject="source-0000000000000001",
        outcome="refused",
        adapter_revision="fake-door-v0",
        reason="corrupted: a reason code that merely starts like a hard one",
    )
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally["count"] == 0
    assert tally["by_kind"]["door:refused:corrupt"] == []


def test_two_door_refusals_for_the_two_hard_reasons_are_two_distinct_incidents(tmp_path):
    tree = make_run(tmp_path)
    publish(
        tree,
        stage=DOOR,
        kind="admission",
        subject="source-0000000000000001",
        outcome="refused",
        adapter_revision="fake-door-v0",
        reason="corrupt: structural validation failed",
    )
    publish(
        tree,
        stage=DOOR,
        kind="admission",
        subject="source-0000000000000002",
        outcome="refused",
        adapter_revision="fake-door-v0",
        reason="unreadable: the page would not decode",
    )
    policy = load_hard_failure_policy(DEFAULT_HARD_FAILURE_CONFIG_PATH)
    tally = tally_hard_failures(tree, policy)
    assert tally["count"] == 2
    assert tally["by_kind"]["door:refused:corrupt"] == ["source-0000000000000001"]
    assert tally["by_kind"]["door:refused:unreadable"] == ["source-0000000000000002"]


# --- Policy validation for the reason field --------------------------------------


def test_a_kind_entry_may_carry_reason(tmp_path):
    path = write_policy(
        tmp_path,
        'threshold = 2\n[[kind]]\nstage = "door"\noutcome = "refused"\nreason = "corrupt"\n',
    )
    policy = load_hard_failure_policy(path)
    assert policy["reason_kinds"] == [("door", "refused", "corrupt")]
    assert policy["kinds"] == []


def test_a_blank_reason_is_refused(tmp_path):
    path = write_policy(
        tmp_path,
        'threshold = 2\n[[kind]]\nstage = "door"\noutcome = "refused"\nreason = ""\n',
    )
    with pytest.raises(ContractError, match="non-empty string"):
        load_hard_failure_policy(path)


def test_a_kind_entry_with_an_extra_field_is_refused(tmp_path):
    path = write_policy(
        tmp_path,
        'threshold = 2\n[[kind]]\nstage = "perlector"\noutcome = "failed"\nextra = "x"\n',
    )
    with pytest.raises(ContractError, match="stage and outcome"):
        load_hard_failure_policy(path)


def test_the_same_stage_and_outcome_with_and_without_reason_are_not_duplicates(tmp_path):
    """A bare (stage, outcome) entry and a reason-scoped sibling name genuinely
    different populations and may coexist without tripping the duplicate guard."""
    path = write_policy(
        tmp_path,
        "threshold = 2\n"
        '[[kind]]\nstage = "door"\noutcome = "refused"\n'
        '[[kind]]\nstage = "door"\noutcome = "refused"\nreason = "corrupt"\n'
        '[[kind]]\nstage = "door"\noutcome = "refused"\nreason = "unreadable"\n',
    )
    policy = load_hard_failure_policy(path)
    assert policy["kinds"] == [("door", "refused")]
    assert policy["reason_kinds"] == [
        ("door", "refused", "corrupt"),
        ("door", "refused", "unreadable"),
    ]


def test_a_duplicate_reason_scoped_kind_is_refused(tmp_path):
    path = write_policy(
        tmp_path,
        "threshold = 2\n"
        '[[kind]]\nstage = "door"\noutcome = "refused"\nreason = "corrupt"\n'
        '[[kind]]\nstage = "door"\noutcome = "refused"\nreason = "corrupt"\n',
    )
    with pytest.raises(ContractError, match="more than once"):
        load_hard_failure_policy(path)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
