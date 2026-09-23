"""The review surface opens an unfinished run and says what happened to it.

After the
witnesses had sealed their evidence, `ReadOnlyRun.projection()` raised
`OperatorError` because no Armarium export existed, and the same run opened only
after export. The evidence was intact and the refusal visible, but the one surface
a person reads was unavailable at exactly the moment they needed the images and
the reason the run stopped. These tests drive the real orchestrator to each
manual boundary and open the run there.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from common.contracts.canonical import digest_bytes
from common.runtree.store import RunTree
from operations.operator import cli, review, review_text
from operations.operator.errors import ErrorCode, OperatorError

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
RUN_ID = "r"
SCENARIO = "audit-reproof-cutoff"


def _orchestrate(run_root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            SCENARIO,
            "--run-id",
            RUN_ID,
            "--run-root",
            str(run_root),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _projection(run_root: Path) -> review.ReviewProjection:
    return review.ReadOnlyRun(run_root, RUN_ID).projection()


def _census(root: Path) -> dict[str, object]:
    seen: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_dir():
            seen[key + "/"] = "directory"
        else:
            status = path.stat()
            seen[key] = (digest_bytes(path.read_bytes()), status.st_size, status.st_mtime_ns)
    return seen


def _writable_copy(source: Path, target: Path) -> Path:
    """A private copy of a sealed run tree whose files a test may damage on purpose."""
    shutil.copytree(source, target)
    for path in target.rglob("*"):
        if path.is_file():
            path.chmod(path.stat().st_mode | 0o200)
        elif path.is_dir():
            path.chmod(path.stat().st_mode | 0o300)
    return target


@pytest.fixture(scope="module")
def witnessed_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One run carried to the Attestatores boundary and stopped there."""
    run_root = tmp_path_factory.mktemp("witnessed") / "runs"
    completed = _orchestrate(run_root, "--from", "door", "--to", "attestatores")
    assert completed.returncode == 0, completed.stderr
    return run_root


def test_a_run_stopped_after_the_witnesses_opens_with_its_images_and_names_what_has_not_run(
    witnessed_run: Path,
):
    """Failing before F3's repair: this projection raised CONSOLE_TREE_UNREADABLE."""
    projected = _projection(witnessed_run)
    tree = RunTree(witnessed_run, RUN_ID)

    assert projected.export["present"] is False
    assert projected.export["record_present"] is False
    assert projected.export["complete"] is False
    assert "no completed export" in projected.export["note"]
    assert projected.export["record_ref"] is None
    assert projected.export["review_items_absent_because"] == "no-export"
    assert {row["stage"]: row["state"] for row in projected.progress} == {
        "door": "sealed",
        "exemplar": "sealed",
        "ink-map": "sealed",
        "designator": "sealed",
        "attestatores": "sealed",
        "perlector": "not-run",
        "recensor": "not-run",
        "archetypus": "not-run",
        "armarium": "not-run",
    }
    assert projected.next_action["resume_from"] == "perlector"
    # The operator's own word, complete enough to type, and the stage census
    # said rather than inferred from the first stage that is not sealed.
    summary = projected.next_action["summary"]
    assert f"`verbatus run --run-id {RUN_ID}`" in summary
    assert "picks up from perlector" in summary
    assert "door, exemplar, ink-map, designator, attestatores sealed" in summary
    assert "perlector, recensor, archetypus, armarium left no record or seal here" in summary
    assert "--from" not in summary, "an operator surface prints no orchestrator flag"
    assert projected.next_action["held_acts"] == 0
    assert projected.next_action["hold_records"] == 0
    assert projected.review_items is None
    assert projected.holds == ()
    assert projected.pages_declared == 2

    # The exact images, verified against the sealed digests, each row naming
    # the Exemplar or Designator record it came from rather than an export.
    assert [page["ordinal"] for page in projected.pages] == [1, 2]
    for page in projected.pages:
        assert page["outcome"] == "sealed"
        assert page["image_path"].startswith("1_exemplar/blobs/sha256/")
        assert digest_bytes(tree.read_bytes(page["image_path"])) == page["image_sha256"]
        assert page["record_ref"]["relative_path"].startswith("1_exemplar/artifacts/page/")
    assert {act["act_key"] for act in projected.acts} == {"a1", "a2"}
    for act in projected.acts:
        assert act["category"] == "witnessed, awaiting the Perlector"
        assert "the Perlector has not read it" in act["reason"]
        assert act["crops"], "every marked-out act shows the crop the witnesses saw"
        for crop in act["crops"]:
            assert crop["image_path"].startswith("2_designator/blobs/sha256/")
            assert digest_bytes(tree.read_bytes(crop["image_path"])) == crop["image_sha256"]
            assert crop["origin"] == "proposal"
        assert act["record_ref"]["relative_path"].startswith(
            "2_designator/artifacts/proposal-seal/"
        )
        assert act["row"]["reading"] is None
        assert act["row"]["review"] is None
        assert act["row"]["established"] is None
        assert len(act["row"]["testimonia"]) == 3
        assert all(
            isinstance(witness["attempt_ordinal"], int) for witness in act["row"]["testimonia"]
        ), "each witness row says which attempt it was"

    text = "\n".join(review_text.render(dataclasses.asdict(projected)))
    assert "perlector: not-run" in text
    assert "witnessed, awaiting the Perlector" in text
    assert "Review queue: not produced (there is no Armarium export record" in text
    assert "Pages (2 of 2 declared)" in text
    assert f"`verbatus run --run-id {RUN_ID}`" in text
    assert "(attempt 1)" in text


def test_opening_an_unfinished_run_changes_no_path_bytes_size_or_mtime(witnessed_run: Path):
    before = _census(witnessed_run / RUN_ID)
    projected = _projection(witnessed_run)
    assert projected.pages and projected.acts
    assert _census(witnessed_run / RUN_ID) == before


def test_the_failed_reproof_run_can_be_opened_and_understood_before_export(
    witnessed_run: Path, tmp_path: Path
):
    """F1's held act is visible, with its reason and its images, before any export exists."""
    run_root = _writable_copy(witnessed_run, tmp_path / "runs")
    resumed = _orchestrate(run_root, "--from", "perlector", "--to", "recensor")
    assert resumed.returncode == 3, resumed.stderr
    assert "stopped at held recensor" in resumed.stdout

    projected = _projection(run_root)
    assert projected.export["present"] is False
    states = {row["stage"]: row["state"] for row in projected.progress}
    assert states["perlector"] == "sealed" and states["recensor"] == "sealed"
    assert states["archetypus"] == "not-run" and states["armarium"] == "not-run"

    (held,) = projected.holds
    assert held["act_key"] == "a1"
    assert held["source"] == "recensor"
    assert held["outcome"] == "held-for-review"
    assert held["audit_examination"] == "incomplete"
    assert "audit re-proof of this act did not complete" in held["reason"]
    assert held["record_ref"]["relative_path"].startswith("5_recensor/artifacts/review/")

    by_key = {act["act_key"]: act for act in projected.acts}
    cut = by_key["a1"]
    assert cut["category"] == "held-for-review"
    assert cut["reason"] == held["reason"]
    assert cut["row"]["reading"]["outcome"] == "read"
    assert cut["row"]["reading"]["truncation"]["classification"] == "complete"
    assert cut["row"]["reading"]["audit"] == {"unresolved": True, "examination": "incomplete"}
    assert isinstance(cut["row"]["reading"]["text"], str) and cut["row"]["reading"]["text"]
    assert cut["row"]["review"]["outcome"] == "held-for-review"
    assert cut["crops"], "the held act's crop is the image a person needs to see"
    assert by_key["a2"]["category"] == "accepted, awaiting establishment"
    assert by_key["a2"]["row"]["reading"]["audit"] == {
        "unresolved": False,
        "examination": "complete",
    }

    assert projected.next_action["held_acts"] == 1
    assert projected.next_action["resume_from"] == "archetypus"
    summary = projected.next_action["summary"]
    assert "`advance` records permission to pass one sealed stage boundary" in summary
    assert "neither certifies a reading nor clears a hold" in summary
    assert "outside the pipeline" in summary

    text = "\n".join(review_text.render(dataclasses.asdict(projected)))
    assert "held-for-review by the recensor [Recensor review]; audit examination incomplete" in text
    assert "did not complete" in text
    assert "machine reading:" in text

    # Between the two stages: the accepted act is established and waiting for an
    # export that does not exist yet, which is its own sentence and not "read,
    # awaiting the Recensor".
    established_run = _orchestrate(run_root, "--stage", "archetypus")
    assert established_run.returncode == 0, established_run.stderr
    between = _projection(run_root)
    assert between.export["present"] is False
    by_key = {act["act_key"]: act for act in between.acts}
    assert by_key["a2"]["category"] == "established, awaiting export"
    assert "the Armarium has not exported it" in by_key["a2"]["reason"]
    assert by_key["a2"]["row"]["established"]["text_status"]
    assert between.next_action["resume_from"] == "armarium"
    assert "established, awaiting export" in "\n".join(
        review_text.render(dataclasses.asdict(between))
    )

    # The export arrives; the same surface now shows its accounting, and the
    # hold neither disappears nor changes its reason.
    exported = _orchestrate(run_root, "--stage", "armarium")
    assert exported.returncode == 3, exported.stderr
    after = _projection(run_root)
    assert after.export["present"] is True
    # A held act means a partial export, and this is the run that produces one:
    # the surface says so in the record's own words instead of announcing "a
    # completed export" over a bundle that claims otherwise.
    assert after.export["complete"] is False
    assert after.export["outcome"] == "held-for-review"
    assert after.export["claims_status"] != "complete"
    assert "partial export (held-for-review)" in after.export["note"]
    assert after.export["record_ref"]["relative_path"].startswith("7_armarium/artifacts/export/")
    assert after.export["review_items_absent_because"] is None
    assert after.review_items is not None and len(after.review_items) == 1
    assert [hold["act_key"] for hold in after.holds] == ["a1"]
    assert after.holds[0]["reason"] == held["reason"]
    assert {act["act_key"]: act["category"] for act in after.acts} == {
        "a1": "held-for-review",
        "a2": "delivered",
    }
    assert after.next_action["resume_from"] is None
    assert after.next_action["held_acts"] == 1
    assert {row["stage"]: row["state"] for row in after.progress} == {
        stage: "sealed"
        for stage in (
            "door",
            "exemplar",
            "ink-map",
            "designator",
            "attestatores",
            "perlector",
            "recensor",
            "archetypus",
            "armarium",
        )
    }

    # The post-export plain rendering -- the most common real use of this
    # screen, and the one shape no test had ever rendered.
    exported_text = "\n".join(review_text.render(dataclasses.asdict(after)))
    assert "Export: present but partial" in exported_text
    assert "partial export (held-for-review)" in exported_text
    assert "Review queue (1)" in exported_text
    assert "delivered text:" in exported_text
    assert "Pages (2 of 2 declared)" in exported_text
    # The witnesses survive the export: the same act shows its chairs before and
    # after, under the export's own field name.
    assert "witnesses:" in exported_text
    before_chairs = {entry["chair"] for entry in by_key["a2"]["row"]["testimonia"]}
    exported_a2 = next(act for act in after.acts if act["act_key"] == "a2")
    exported_chairs = {entry["chair"] for entry in exported_a2["row"]["testimonia"]}
    assert exported_chairs, "the witnesses do not vanish once the run exports"
    assert exported_chairs <= before_chairs, (
        "the export names the evidence-backed witness basis, never a chair the "
        "Attestatores did not seal"
    )
    assert "review record:" in exported_text


@pytest.mark.parametrize("what", ["page", "crop"])
def test_an_image_whose_bytes_moved_is_refused_by_name_before_export(
    witnessed_run: Path, tmp_path: Path, what: str
):
    run_root = _writable_copy(witnessed_run, tmp_path / "runs")
    projected = _projection(run_root)
    if what == "page":
        relative = projected.pages[0]["image_path"]
    else:
        relative = projected.acts[0]["crops"][0]["image_path"]
    target = RunTree(run_root, RUN_ID).resolve(relative)
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))

    with pytest.raises(OperatorError) as refused:
        _projection(run_root)
    assert refused.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    detail = refused.value.detail or ""
    assert relative.rsplit("/", 1)[-1] in detail, "the refusal names the file whose bytes moved"


def test_a_seal_that_no_longer_verifies_is_named_as_that_and_never_as_a_stage_that_did_not_run(
    witnessed_run: Path, tmp_path: Path
):
    """The fourth stage state. The suite could only produce the other three."""
    run_root = _writable_copy(witnessed_run, tmp_path / "runs")
    tree = RunTree(run_root, RUN_ID)
    # The ink map's own records are not read by this projection, so removing one
    # moves the boundary the seal witnesses without making any record it shows
    # unreadable: the seal is stored, and it no longer describes the disk.
    removable = [
        row
        for row in tree.build_manifest("ink-map", verify_inputs=False)["artifacts"]
        if row["kind"] != "stage-seal"
    ]
    assert removable, "the ink map sealed at least one record in this fixture"
    tree.resolve(removable[0]["relative_path"]).unlink()

    projected = _projection(run_root)
    states = {row["stage"]: row for row in projected.progress}
    assert states["ink-map"]["state"] == "seal-invalid"
    assert "no longer verifies" in states["ink-map"]["note"]
    assert states["door"]["state"] == "sealed"
    assert projected.next_action["resume_from"] is None, (
        "a broken seal is evidence to investigate, never a stage to resume"
    )
    summary = projected.next_action["summary"]
    assert "ink-map's completion seal no longer verifies" in summary
    assert "preserve and investigate" in summary
    assert "ink-map stores a seal that no longer verifies" in summary

    text = "\n".join(review_text.render(dataclasses.asdict(projected)))
    assert "ink-map: seal-invalid" in text
    # The stage that has not run is still named as that, on the same screen.
    assert "perlector: not-run" in text
    assert "no record and no seal found here" in text


def _seal_row(payload: dict[str, object], artifact: str = "") -> dict[str, object]:
    """One stage-record row of the shape `_record_row` produces, for a seal test."""
    from common.contracts.canonical import self_hash
    from common.contracts.identities import artifact_id

    sealed = dict(payload)
    sealed["self_hash"] = self_hash(sealed)
    return {
        "stage": "designator",
        "artifact_id": artifact
        or artifact_id("designator", "proposal-seal", "proposal-seal", None),
        "kind": "proposal-seal",
        "subject_id": "proposal-seal",
        "outcome": "sealed",
        "record_ref": {
            "relative_path": "2_designator/artifacts/proposal-seal/x.json",
            "sha256": "",
        },
        "record": {"payload": sealed},
    }


def _review_record(act_id: str, outcome: str, payload: dict[str, object]) -> dict[str, object]:
    """A Recensor review record shaped the way `latest_attempt` insists on reading one.

    The attempt ordinal in the payload must be bound by the sealed attempt
    identity, and the identity derives from (subject, operation, ordinal). A
    synthetic record missing either field is refused for its shape before any
    assertion about the outcome vocabulary can be reached -- which would have
    made the refusal test pass for the wrong reason.
    """
    from common.contracts.identities import attempt_id

    ordinal = payload["attempt_ordinal"]
    attempt = attempt_id(act_id, "recense", ordinal)
    return {
        "stage": "recensor",
        "artifact_id": f"art_review_{ordinal}",
        "kind": "review",
        "subject_id": act_id,
        "outcome": outcome,
        "record_ref": {"relative_path": "5_recensor/artifacts/review/b.json", "sha256": ""},
        "record": {
            "artifact_id": f"art_review_{ordinal}",
            "subject_id": act_id,
            "attempt_id": attempt,
            "payload": dict(payload),
        },
    }


def _expected_row(act_id: str, outcome: str = "proposed") -> dict[str, object]:
    return {
        "act_id": act_id,
        "act_key": act_id,
        "page_id": "p1",
        "page_ordinal": 1,
        "has_continuation": False,
        "outcome": outcome,
        "evidence": [],
    }


@pytest.mark.parametrize(
    "damage, said",
    [
        ("artifact-id", "no canonical proposal seal"),
        ("self-hash", "does not verify against its own self-hash"),
        ("count", "do not reconcile"),
    ],
)
def test_a_proposal_seal_the_pipeline_would_refuse_is_refused_here_too(damage: str, said: str):
    """The act denominator is read with the pipeline's own checks, not a weaker set.

    `common/stage.py::expected_acts` verifies the payload's self-hash, that
    `count` reconciles with the rows, and that the seal is the run's one
    canonical proposal-seal artifact. A console that checked only "an object
    with an act_id" would show fewer acts than the seal itself claims, silently,
    while every stage below read the longer list.
    """
    rows = [_expected_row("act1"), _expected_row("act2")]
    seal = _seal_row({"expected_acts": rows, "count": len(rows)})
    if damage == "artifact-id":
        seal["artifact_id"] = "art_0000000000000000"
    elif damage == "self-hash":
        seal["record"]["payload"]["count"] = 2  # after the self-hash was computed
        seal["record"]["payload"]["expected_acts"] = [*rows, _expected_row("act3")]
    else:
        seal = _seal_row({"expected_acts": rows, "count": 3})

    with pytest.raises(OperatorError) as refused:
        review._expected_acts([seal])
    assert refused.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert said in (refused.value.detail or "")


def test_an_act_the_designator_ended_is_not_left_waiting_for_a_witness():
    """`excluded` and `failed` are terminal there; nothing downstream will speak."""
    for outcome, category in (
        ("excluded", "excluded by the Designator"),
        ("failed", "failed at the Designator"),
    ):
        summary = review._act_summary([], _expected_row("act1", outcome))
        assert summary["category"] == category
        assert "no witness, reading or review will follow" in summary["reason"]
    proposed = review._act_summary([], _expected_row("act1"))
    assert proposed["category"] == "marked out, awaiting witnesses"


def test_one_act_held_by_both_stages_is_two_labelled_records_and_one_held_act():
    """The Recensor writes its own held review quoting a Designator hold."""
    act_id = "act1"
    designator_hold = {
        "stage": "designator",
        "artifact_id": "art_1",
        "kind": "hold",
        "subject_id": act_id,
        "outcome": "held",
        "record_ref": {"relative_path": "2_designator/artifacts/hold/a.json", "sha256": ""},
        "record": {"payload": {"act_key": "a1", "reason": "the margin is torn"}},
    }
    recensor_review = _review_record(
        act_id,
        "held-for-review",
        {
            "act_key": "a1",
            "reason": "the Designator held this act",
            "attempt_ordinal": 1,
            "audit_examination": "complete",
        },
    )
    holds = review._holds([designator_hold, recensor_review], None)
    assert [hold["label"] for hold in holds] == ["Designator hold", "Recensor review of that hold"]
    action = review._next_action(RUN_ID, (), {"present": False}, holds)
    assert action["held_acts"] == 1, "one act, twice attested, is not two held acts"
    assert action["hold_records"] == 2
    assert "1 act(s) are held or unresolved, listed below as 2 record(s)" in action["summary"]

    text = "\n".join(review_text.render({"run_id": "r", "holds": [dict(hold) for hold in holds]}))
    assert "[Designator hold]" in text and "[Recensor review of that hold]" in text
    # F041: the header counts acts, not hold records -- one act attested twice
    # is "Held or unresolved acts (1)", matching the distinct count in the
    # summary sentence just above it, not len(holds).
    assert "Held or unresolved acts (1)" in text
    assert "Held or unresolved acts (2)" not in text


def test_a_review_outcome_outside_the_recensor_vocabulary_is_refused_not_skipped():
    """A widened vocabulary must not drop an act out of the holds list in silence."""
    from common.contracts.errors import FatalAccounting

    invented = _review_record(
        "act1", "sent-back-for-rework", {"act_key": "a1", "attempt_ordinal": 1}
    )
    with pytest.raises(FatalAccounting) as refused:
        review._holds([invented], None)
    assert "sent-back-for-rework" in str(refused.value), (
        "the refusal names the outcome word, so the attempt chain was read and the "
        "vocabulary check is what refused"
    )


def test_a_vocabulary_refusal_reaches_the_operator_as_a_named_console_refusal(
    witnessed_run: Path, monkeypatch: pytest.MonkeyPatch
):
    """What a person sees, not what the helper raises.

    The refusal cannot be staged on a real run tree: a record carrying an
    outcome outside its stage's vocabulary is refused by `validate_envelope`
    (which calls the same `classify`) as the projection reads it, and editing a
    sealed record's outcome breaks its self-hash first. `_holds`'s own check is
    therefore a second line rather than the first -- worth keeping, since it is
    the one that fires if the vocabulary widens under records already written --
    and what is pinned here is the real `projection()` turning such a refusal
    into the named console error instead of an unclassifiable problem.
    """
    from common.contracts.errors import FatalAccounting

    def refuse(stage_records, found):
        raise FatalAccounting(
            "recensor produced outcome 'sent-back-for-rework', which is in no terminal set"
        )

    monkeypatch.setattr(review, "_holds", refuse)
    with pytest.raises(OperatorError) as through_surface:
        _projection(witnessed_run)
    assert through_surface.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "sent-back-for-rework" in (through_surface.value.detail or "")


def test_an_act_with_two_archetypus_records_is_refused_rather_than_read_positionally():
    """The stage writes one established text per act; two is a tree, not a newer version."""
    rows = [
        {
            "stage": "archetypus",
            "artifact_id": f"art_{index}",
            "kind": "archetypus",
            "subject_id": "act1",
            "outcome": "established",
            "record_ref": {
                "relative_path": f"6_archetypus/artifacts/archetypus/{index}.json",
                "sha256": "",
            },
            "record": {"payload": {"text_status": "whole", "text_hash": "x"}},
        }
        for index in (1, 2)
    ]
    with pytest.raises(OperatorError) as refused:
        review._act_summary(rows, _expected_row("act1"))
    assert refused.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "2 Archetypus records" in (refused.value.detail or "")


def test_the_excluded_sentence_states_only_what_this_surface_read():
    """An exclusion is valid only with an approval, and nothing here reads one."""
    summary = review._act_summary([], _expected_row("act1", "excluded"))
    assert summary["category"] == "excluded by the Designator"
    assert "recorded this act as excluded" in summary["reason"]
    assert "which this surface does not read and does not claim" in summary["reason"]
    assert "with approval;" not in summary["reason"]


def test_a_terminal_act_with_downstream_records_says_the_two_disagree():
    """Nothing is hidden either way; the disagreement is named rather than left to notice."""
    testimonium = {
        "stage": "attestatores",
        "artifact_id": "art_t",
        "kind": "testimonium",
        "subject_id": "act1",
        "outcome": "read",
        "record_ref": {
            "relative_path": "3_attestatores/artifacts/testimonium/t.json",
            "sha256": "",
        },
        "record": {"payload": {"chair": "chair-a", "attempt_ordinal": 1}},
    }
    summary = review._act_summary([testimonium], _expected_row("act1", "excluded"))
    assert summary["category"] == "excluded by the Designator"
    assert "this run also holds witnesses for this act" in summary["reason"]
    assert "the export would refuse" in summary["reason"]
    # And an act with no downstream record says nothing of the kind.
    assert "also holds" not in review._act_summary([], _expected_row("act1", "excluded"))["reason"]


@pytest.mark.parametrize(
    "field, value, said",
    [
        ("page_id", "", "no page_id"),
        ("page_ordinal", True, "page_ordinal that is not an integer"),
        ("has_continuation", "yes", "has_continuation that is not true or false"),
        ("evidence", {}, "evidence value that is not a list"),
    ],
)
def test_the_seal_row_field_types_the_pipeline_requires_are_required_here(
    field: str, value: object, said: str
):
    """A seal the shared reader would refuse is not read out here as if it were whole."""
    row = _expected_row("act1")
    row[field] = value
    seal = _seal_row({"expected_acts": [row], "count": 1})
    with pytest.raises(OperatorError) as refused:
        review._expected_acts([seal])
    assert refused.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert said in (refused.value.detail or "")


def test_two_records_under_the_canonical_seal_id_refuse_rather_than_pick_the_first():
    """Two denominators are not a neighbour to name; nothing here chooses between them."""
    rows = [_expected_row("act1")]
    first = _seal_row({"expected_acts": rows, "count": 1})
    second = _seal_row({"expected_acts": rows + [_expected_row("act2")], "count": 2})
    second["record_ref"] = {
        "relative_path": "2_designator/artifacts/proposal-seal/again.json",
        "sha256": "",
    }
    with pytest.raises(OperatorError) as refused:
        review._expected_acts([first, second])
    assert refused.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "one of 2 records" in (refused.value.detail or "")
    assert "again.json" in (refused.value.detail or "")


@pytest.mark.parametrize(
    "value, repeated",
    [
        ("happy", "happy"),
        ("review-2.b_x", "review-2.b_x"),
        ("happy; rm -rf /", None),
        ("$(id)", None),
        ("", None),
        (" happy", None),
        ("a" * 65, None),
    ],
)
def test_only_a_scenario_token_is_repeated_into_the_resume_command(value: str, repeated):
    """The word is typed by a person next; a run tree does not get to write that line."""
    row = {
        "stage": review.ARMARIUM,
        "kind": "export",
        "subject_id": "r",
        "artifact_id": review.artifact_id(review.ARMARIUM, "export", "export", None),
        "record_ref": {"relative_path": "7_armarium/export.json", "sha256": ""},
        "record": {"payload": {"scenario": value}},
    }
    assert review._recorded_scenario([row]) == repeated


def test_a_foreign_proposal_seal_beside_the_canonical_one_is_named_not_a_refusal():
    """Stricter than the pipeline is the wrong kind of strict on a surface for damaged trees."""
    rows = [_expected_row("act1")]
    canonical = _seal_row({"expected_acts": rows, "count": 1})
    foreign = _seal_row({"expected_acts": rows, "count": 1}, artifact="art_0000000000000000")
    foreign["record_ref"] = {
        "relative_path": "2_designator/artifacts/proposal-seal/other.json",
        "sha256": "",
    }
    seal, expected, note = review._expected_acts([canonical, foreign])
    assert seal is canonical and expected == rows
    assert "1 further proposal-seal record(s)" in note
    assert "other.json" in note
    assert "are not read" in note
    # With no canonical seal at all, there is nothing to read and it refuses.
    with pytest.raises(OperatorError) as refused:
        review._expected_acts([foreign])
    assert "no canonical proposal seal" in (refused.value.detail or "")


def test_an_act_the_seal_calls_held_is_in_the_holds_list_even_with_no_hold_record():
    """One screen must not say an act is held in one section and count zero in another."""
    seal = _seal_row(
        {
            "expected_acts": [_expected_row("act1", "held"), _expected_row("act2", "failed")],
            "count": 2,
        }
    )
    found = review._expected_acts([seal])
    holds = review._holds([seal], found)
    assert [hold["act_id"] for hold in holds] == ["act1", "act2"]
    assert {hold["label"] for hold in holds} == {"proposal seal, no hold record found"}
    assert holds[0]["outcome"] == "held" and holds[1]["outcome"] == "failed"
    assert "no hold record carrying a reason was found beside it" in holds[0]["reason"]
    action = review._next_action("r", (), {"present": False}, holds)
    assert action["held_acts"] == 2

    # A COMPLETED-class outcome is not a hold: `proposed` and `excluded` stay out.
    ordinary = _seal_row(
        {
            "expected_acts": [_expected_row("act1"), _expected_row("act2", "excluded")],
            "count": 2,
        }
    )
    assert review._holds([ordinary], review._expected_acts([ordinary])) == ()


def test_an_act_count_with_no_denominator_says_so_rather_than_reading_as_no_acts():
    text = "\n".join(
        review_text.render(
            {
                "run_id": "r",
                "acts": [],
                "acts_denominator_note": "the Designator has sealed no proposal, so nothing "
                "in this tree declares how many acts this run has",
            }
        )
    )
    assert "Acts (0; the Designator has sealed no proposal" in text
    assert "Acts (0)\n" not in text


def test_a_newline_becomes_a_separator_and_the_length_notice_names_two_lengths():
    """Escaping first meant the replacement found nothing to replace."""
    assert review_text._one_line("alpha\nbeta") == "alpha / beta"
    cut = review_text._one_line("x" * 500, 300)
    assert cut.endswith("(first 300 characters as shown, of a 500-character value)")
    assert cut.count("x") == 300


def test_a_crop_line_says_which_attempt_it_was():
    text = "\n".join(
        review_text.render(
            {
                "run_id": "r",
                "acts": [
                    {
                        "act_id": "a1",
                        "act_key": "a1",
                        "category": "held-for-review",
                        "crops": [
                            {
                                "region_id": "r1",
                                "ordinal": 1,
                                "attempt_ordinal": 2,
                                "image_path": "2_designator/blobs/sha256/aa/bb",
                                "image_sha256": "aabb",
                            }
                        ],
                    }
                ],
            }
        )
    )
    assert "(attempt 2)" in text


def test_an_empty_queue_row_is_named_by_its_line_number():
    text = "\n".join(
        review_text.render(
            {
                "run_id": "r",
                "review_items": [
                    {"row": {}, "line": 4, "member": "review-items.jsonl", "bundle_path": "b.zip"}
                ],
            }
        )
    )
    assert "line 4 of review-items.jsonl: this queue row is empty" in text
    assert "None: None" not in text
    # An entry that is not this projection's wrapper is itself the row, and its
    # content still reaches the screen.
    raw = "\n".join(
        review_text.render({"run_id": "r", "review_items": [{"reason": "the margin is torn"}]})
    )
    assert "the margin is torn" in raw


def test_a_run_authority_that_is_not_an_object_is_a_note_beside_the_page_count(tmp_path: Path):
    """Not an unclassifiable problem that takes the whole screen with it."""

    class _Authority:
        root = tmp_path

        def read_run(self):
            return ["not", "an", "object"]

    class _ArrayAuthority:
        root = tmp_path

        def read_run(self):
            raise AttributeError("'list' object has no attribute 'get'")

    count, note = review._declared_page_count(_ArrayAuthority())
    assert count is None
    assert "could not be read as an object" in note
    count, note = review._declared_page_count(_Authority())
    assert count is None
    assert "is not an object" in note


def test_a_nonexistent_run_id_is_a_wrong_command_not_damaged_evidence(tmp_path: Path):
    """F035: a mistyped run id must not read as "preserve and investigate."

    `RunTree.__init__` only validates the id's shape, so a run id naming
    nothing reaches `projection()`'s own tree read, where it used to fall into
    the catch-all meant for a tree that exists and failed verification.
    """
    run_root = tmp_path / "runs"
    run_root.mkdir()

    with pytest.raises(OperatorError) as refused:
        review.ReadOnlyRun(run_root, "nope").projection()

    assert refused.value.code is ErrorCode.INVALID_COMMAND
    assert "nope" in refused.value.render()
    assert "does not exist" in refused.value.render()


def test_a_run_json_that_fails_verification_still_reads_as_damaged_not_missing(
    witnessed_run: Path, tmp_path: Path
):
    """The other half of F035's split: an existing, damaged tree is unaffected."""
    run_root = _writable_copy(witnessed_run, tmp_path / "runs")
    (run_root / RUN_ID / "run.json").write_bytes(b"not valid json")

    with pytest.raises(OperatorError) as refused:
        _projection(run_root)

    assert refused.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE


def test_a_stage_that_wrote_records_but_never_sealed_is_named_interrupted_not_damaged(
    witnessed_run: Path, tmp_path: Path
):
    run_root = _writable_copy(witnessed_run, tmp_path / "runs")
    tree = RunTree(run_root, RUN_ID)
    seals = [
        row
        for row in tree.build_manifest("attestatores", verify_inputs=False)["artifacts"]
        if row["kind"] == "stage-seal"
    ]
    assert seals, "the witnessed run sealed its Attestatores boundary"
    for row in seals:
        tree.resolve(row["relative_path"]).unlink()

    projected = _projection(run_root)
    states = {row["stage"]: row for row in projected.progress}
    assert states["attestatores"]["state"] == "unsealed"
    assert "interrupted or is still running" in states["attestatores"]["note"]
    assert states["designator"]["state"] == "sealed"
    assert states["perlector"]["state"] == "not-run"
    assert projected.next_action["resume_from"] == "attestatores"
    assert "Do not resume while a writer may still be active" in projected.next_action["summary"]
    # The stage's records are still shown, and still labelled as not complete.
    assert all(act["row"]["testimonia"] for act in projected.acts)
    assert all(act["category"] == "witnessed, awaiting the Perlector" for act in projected.acts)


def test_a_half_written_artifact_is_refused_rather_than_shown_as_a_complete_stage(
    witnessed_run: Path, tmp_path: Path
):
    run_root = _writable_copy(witnessed_run, tmp_path / "runs")
    partial_dir = run_root / RUN_ID / "4_perlector" / "artifacts" / "perlectio"
    partial_dir.mkdir(parents=True)
    (partial_dir / "art_0000000000000000.json").write_text('{"schema": "perlectio", "payl')

    with pytest.raises(OperatorError) as refused:
        _projection(run_root)
    assert refused.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "4_perlector" in (refused.value.detail or "")


def test_the_plain_rendering_keeps_hostile_text_inert():
    hostile = {
        "run_id": "r",
        "progress": [{"stage": "door", "state": "sealed", "note": "x\x1b[2Jwiped"}],
        "export": {"present": False, "note": "none\x07"},
        "next_action": {"summary": "resume\x1b]0;pwned\x07", "resume_from": None},
        "holds": [
            {
                "act_key": "a\x1bz",
                "act_id": "act",
                "source": "recensor",
                "outcome": "held",
                "reason": "adversarial\x1b escape",
                "audit_examination": None,
            }
        ],
        "pages": [],
        "acts": [{"act_id": "a1", "act_key": "x\x1b[2Jwiped", "category": "baptism", "crops": []}],
        "review_items": [{"reason": "adversarial\x1b]0;pwned\x07 escape sequence"}],
        "advance_records": [],
    }
    text = "\n".join(review_text.render(hostile))
    assert "\x1b" not in text and "\x07" not in text
    assert "wiped" in text and "pwned" in text
    assert "\\u001b" in text, "a control character is spelled out, not dropped"


def test_run_tree_text_cannot_imitate_the_tools_own_escaping():
    """A literal backslash is doubled first, so escaped and escaped-looking differ."""
    neutralised = review_text.inert("a\x1bz")
    imitation = review_text.inert("a\\u001bz")
    assert neutralised == "a\\u001bz"
    assert imitation == "a\\\\u001bz"
    assert neutralised != imitation


def test_a_shortened_line_says_it_was_shortened_and_how_long_the_text_is():
    """The plain view is the default; a silent cut on this screen is the whole risk."""
    long_text = "x" * 900
    projection = {
        "run_id": "r",
        "acts": [
            {
                "act_id": "a1",
                "act_key": "a1",
                "category": "held-for-review",
                "crops": [],
                "row": {"reading": {"outcome": "read", "text": long_text}},
            }
        ],
    }
    text = "\n".join(review_text.render(projection))
    assert "(first 300 characters as shown, of a 900-character value)" in text
    assert "x" * 300 in text


def test_the_review_verb_prints_plain_language_by_default_and_json_on_request(
    witnessed_run: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """The child still returns JSON only; the parent is what a person reads."""
    projected = dataclasses.asdict(_projection(witnessed_run))
    child_stdout = json.dumps(projected, sort_keys=True)

    class Backend:
        def launcher_failure(self, completed):
            return None

    def confined(command, *, writable, cwd, input_text):
        assert writable is None
        assert json.loads(input_text)["export"]["present"] is False
        return Backend(), types.SimpleNamespace(returncode=0, stdout=child_stdout, stderr="")

    monkeypatch.setattr(cli, "run_confined", confined)

    cli._review_in_custody(witnessed_run, RUN_ID, ROOT)
    plain = capsys.readouterr().out
    assert "What you can do next" in plain
    assert f"`verbatus run --run-id {RUN_ID}`" in plain
    assert "picks up from perlector" in plain
    assert "witnessed, awaiting the Perlector" in plain
    assert not plain.lstrip().startswith("{")

    cli._review_in_custody(witnessed_run, RUN_ID, ROOT, raw=True)
    raw = capsys.readouterr().out
    assert json.loads(raw)["export"]["present"] is False

    def broken(command, *, writable, cwd, input_text):
        return Backend(), types.SimpleNamespace(returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(cli, "run_confined", broken)
    with pytest.raises(OperatorError) as refused:
        cli._review_in_custody(witnessed_run, RUN_ID, ROOT)
    assert refused.value.code is ErrorCode.CONSOLE_PROJECTION_UNREADABLE


def test_the_seven_pinned_projection_fields_still_construct_by_position_and_keyword():
    """Callers that never learned the pre-export fields keep working unchanged."""
    by_position = review.ReviewProjection("r", (), (), (), (), None, ())
    by_keyword = review.ReviewProjection(
        run_id="r",
        stage_records=(),
        boundaries=(),
        pages=(),
        acts=(),
        review_items=None,
        advance_records=(),
    )
    assert by_position == by_keyword
    assert by_position.export == {} and by_position.progress == ()
    assert by_position.holds == () and by_position.next_action == {}


def test_a_projection_list_entry_that_is_not_an_object_is_refused_by_field_and_index(
    witnessed_run: Path, monkeypatch: pytest.MonkeyPatch
):
    """Refused, never skipped: a row the renderer passed over is a row nobody sees."""
    with pytest.raises(review_text.ProjectionShapeError) as refused:
        review_text.render({"run_id": "r", "progress": [{"stage": "door"}, "not a row"]})
    assert refused.value.field == "progress" and refused.value.index == 1
    with pytest.raises(review_text.ProjectionShapeError) as nested:
        review_text.render({"run_id": "r", "acts": [{"act_id": "a", "act_key": "a", "crops": [7]}]})
    assert nested.value.field == "acts[].crops"

    class Backend:
        def launcher_failure(self, completed):
            return None

    def confined(command, *, writable, cwd, input_text):
        return Backend(), types.SimpleNamespace(
            returncode=0, stdout=json.dumps({"run_id": "r", "holds": [1]}), stderr=""
        )

    monkeypatch.setattr(cli, "run_confined", confined)
    with pytest.raises(OperatorError) as error:
        cli._review_in_custody(witnessed_run, RUN_ID, ROOT)
    assert error.value.code is ErrorCode.CONSOLE_PROJECTION_UNREADABLE
    assert "'holds' entry 0" in (error.value.detail or "")


@pytest.mark.parametrize(
    "projection, field",
    [
        ({"export": "present"}, "export"),
        ({"next_action": 7}, "next_action"),
        ({"holds": [{"act_id": "a", "record_ref": "somewhere"}]}, "holds[].record_ref"),
        ({"acts": [{"act_id": "a", "row": "a row"}]}, "acts[].row"),
        (
            {"acts": [{"act_id": "a", "row": {"reading": {"audit": 3}}}]},
            "acts[].row.reading.audit",
        ),
        (
            {"acts": [{"act_id": "a", "row": {"reading": {"truncation": "length"}}}]},
            "acts[].row.reading.truncation",
        ),
        ({"progress": "sealed"}, "progress"),
    ],
)
def test_an_object_valued_projection_field_of_the_wrong_type_is_refused_by_name(
    projection: dict[str, object], field: str
):
    """Not an `AttributeError` the catch-all reports as an unclassifiable problem.

    The list fields were shape-checked from the first candidate and the object
    fields were not, so `export`, `next_action`, a hold's `record_ref`, a
    reading's `audit` and an act's `row` each crashed the renderer instead of
    refusing.
    """
    with pytest.raises(review_text.ProjectionShapeError) as refused:
        review_text.render({"run_id": "r", **projection})
    assert refused.value.field == field
    assert refused.value.index is None
    message = str(refused.value)
    assert "entry -1" not in message, "the field itself is wrong, not a row numbered -1"
    assert field in message


def _delivered_act(uncertainty: dict, text: str = "alpha beta") -> dict:
    return {
        "run_id": "r",
        "acts": [
            {
                "act_id": "a",
                "act_key": "a1",
                "category": "delivered",
                "crops": [],
                "row": {"text": text, "uncertainty": uncertainty},
            }
        ],
    }


def test_a_published_span_is_shown_beside_the_state_that_says_who_did_not_report_it():
    """The exhausted-cap projection mints spans on acts whose reader has no channel.

    With today's live reader that combination -- `not-assessed` beside real
    published spans -- is the only way a span reaches this surface at all, and
    the renderer used to print the state line and return, hiding exactly those.
    """
    lines = review_text.render(
        _delivered_act(
            {
                "assessment": {"state": "not-assessed", "problem": "this chair has no channel"},
                "uncertain_spans": [
                    {"start": 0, "end": 5, "alternatives": [], "confidence": "low"}
                ],
                "gaps": [{"position": "internal", "start": 6, "end": 6}],
            }
        )
    )
    text = "\n".join(lines)

    assert "doubts: not-assessed — this chair has no channel" in text
    assert "published beside that state, not by the reader: 1 uncertain span(s), 1 gap(s)" in text
    assert "[0, 5) 'alpha' confidence low" in text
    assert "gap (internal) at 6" in text


def test_a_doubt_layer_entry_that_is_not_an_object_is_refused_by_field_and_index():
    """The same rule as every other projection list, at the newest one."""
    with pytest.raises(review_text.ProjectionShapeError) as refused:
        review_text.render(
            _delivered_act(
                {
                    "assessment": {"state": "assessed", "problem": None},
                    "uncertain_spans": [{"start": 0, "end": 1}, "not a span"],
                    "gaps": [],
                }
            )
        )
    assert refused.value.field == "acts[].row.uncertainty.uncertain_spans"
    assert refused.value.index == 1

    with pytest.raises(review_text.ProjectionShapeError) as gaps:
        review_text.render(
            _delivered_act(
                {
                    "assessment": {"state": "assessed", "problem": None},
                    "uncertain_spans": [],
                    "gaps": "not a list",
                }
            )
        )
    assert gaps.value.field == "acts[].row.uncertainty.gaps"
    assert gaps.value.index is None
    assert "entry -1" not in str(gaps.value)


def test_an_audited_reading_does_not_credit_the_reader_with_the_audits_own_spans():
    """The union is not attributable on this surface, so it is not attributed.

    Under a sealed cap of 0 the audit mints exhausted-cap spans, and a reader
    that also assesses adds its own; the published layer holds both and nothing
    in it says which is which. Saying "assessed by the reader; 3 span(s)" would
    credit a person's reading of the screen to an instrument that reported one
    of them (principle 8).
    """
    projected = {"start": 0, "end": 5, "alternatives": [], "confidence": "low"}
    reader = {"start": 6, "end": 10, "alternatives": ["beta"], "confidence": "high"}
    audited = "\n".join(
        review_text.render(
            _delivered_act(
                {
                    "assessment": {"state": "assessed", "problem": None},
                    "uncertain_spans": [projected, reader],
                    "gaps": [],
                }
            )
        )
    )

    assert "assessed by the reader; this view cannot tell which of the span(s) below are" in (
        audited
    )
    assert "its report and which the audit's; 2 uncertain span(s), 0 gap(s)" in audited

    # A record with no audit behind it publishes only the reader's own spans, and
    # there the attribution is provable. Every Perlectio carries an audit, so
    # this form is reserved for a record kind that does not -- the instrument
    # readings, which no projection puts on this screen today. Kept as the
    # rule's other half rather than left to a reader to assume.
    instrument = "\n".join(
        review_text.render(
            {
                "run_id": "r",
                "acts": [
                    {
                        "act_id": "a",
                        "act_key": "a1",
                        "category": "read: read, awaiting the Recensor",
                        "crops": [],
                        "row": {
                            "reading": {
                                "outcome": "read",
                                "text": "alpha beta",
                                "audit": None,
                                "uncertainty_assessment": {"state": "assessed", "problem": None},
                                "uncertain_spans": [reader],
                                "gaps": [],
                            }
                        },
                    }
                ],
            }
        )
    )
    assert "doubts: assessed by the reader; 1 uncertain span(s), 0 gap(s)" in instrument


def test_an_identical_pair_is_one_line_with_the_count_and_no_claim_about_its_source():
    """The layer records that the characters were doubted twice, and nothing else.

    Dropping the repeat in the producer would have erased that the layer carried
    it twice, and printing it twice would say two doubts were found where one
    entry appears twice. Naming the two instruments would say a third thing no
    artifact records: nothing in the run names the instrument behind any one
    span, and two audit flags of different classes may share one location, so a
    fold is evidence of a repeat and of nothing else (principle 8).
    """
    span = {"start": 0, "end": 5, "alternatives": [], "confidence": "low"}
    for state, assessment in (
        ("assessed", {"state": "assessed", "problem": None}),
        ("not-assessed", {"state": "not-assessed", "problem": "no channel"}),
    ):
        text = "\n".join(
            review_text.render(
                _delivered_act(
                    {
                        "assessment": assessment,
                        "uncertain_spans": [dict(span), dict(span)],
                        "gaps": [],
                    }
                )
            )
        )

        assert "2 uncertain span(s)" in text, state
        assert text.count("[0, 5) 'alpha'") == 1, state
        assert "(shown as 1 line(s); identical entries are folded)" in text, state
        assert "carried 2 times in the layer" in text, state
        assert "instrument" not in text, state


def test_a_reading_sealed_before_the_doubt_contract_says_so_rather_than_nothing():
    """Absent is a fact about the record's age, and prints; malformed is a fault.

    Flattened together, both printed as no doubt line at all -- the pre-F2
    silence restored on the one surface a person reads.
    """
    absent = "\n".join(
        review_text.render(_delivered_act({"uncertain_spans": [], "gaps": [], "assessment": None}))
    )
    assert "doubts: not recorded — this reading was sealed before the reader's doubt" in absent

    with pytest.raises(review_text.ProjectionShapeError) as refused:
        review_text.render(
            _delivered_act({"uncertain_spans": [], "gaps": [], "assessment": "assessed"})
        )
    # The canonical layer's key is `assessment`; a person sent to look for
    # `uncertainty_assessment` inside an export row would not find one.
    assert refused.value.field == "acts[].row.uncertainty.assessment"
    # `index=None` says the FIELD is wrong, not one of its rows. `entry -1` sent
    # a person looking for a row that was never there (F3), and the rebase
    # brought that spelling back with F2's own two sites.
    assert refused.value.index is None
    assert "entry -1" not in str(refused.value)


def _reading_act(reading: dict) -> dict:
    return {
        "run_id": "r",
        "acts": [
            {
                "act_id": "a",
                "act_key": "a1",
                "category": "read: read, awaiting the Recensor",
                "crops": [],
                "row": {"reading": reading},
            }
        ],
    }


def test_a_delivered_act_with_no_uncertainty_layer_still_says_so():
    """An absent or damaged layer printed nothing at all after the export.

    Indistinguishable, to the person reviewing the act, from a reader that found
    no doubt -- the precise silence this change exists to remove, and on the
    half of the screen the act reaches last.
    """
    absent = "\n".join(
        review_text.render(
            {
                "run_id": "r",
                "acts": [
                    {
                        "act_id": "a",
                        "act_key": "a1",
                        "category": "delivered",
                        "crops": [],
                        "row": {"text": "alpha beta"},
                    }
                ],
            }
        )
    )
    assert "doubts: not recorded" in absent

    with pytest.raises(review_text.ProjectionShapeError) as refused:
        review_text.render(
            {
                "run_id": "r",
                "acts": [
                    {
                        "act_id": "a",
                        "act_key": "a1",
                        "category": "delivered",
                        "crops": [],
                        "row": {"text": "alpha beta", "uncertainty": "not an object"},
                    }
                ],
            }
        )
    assert refused.value.field == "acts[].row.uncertainty"
    assert refused.value.index is None


def test_the_doubted_characters_and_the_alternatives_are_cut_like_every_other_value():
    """A span may legitimately cover a whole act, and an act is longer than a line.

    The machine reading above it is cut at 300 with a notice; this line printed
    the same characters whole, and `repr` re-escaped what `inert` had already
    made safe, so the tool's own escaping stopped being distinguishable from
    text that merely looks like it.
    """
    act_text = "x" * 400
    lines = review_text.render(
        _delivered_act(
            {
                "assessment": {"state": "assessed", "problem": None},
                "uncertain_spans": [
                    {
                        "start": 0,
                        "end": len(act_text),
                        "alternatives": ["y" * 400],
                        "confidence": "low",
                    }
                ],
                "gaps": [],
            },
            text=act_text,
        )
    )
    span_line = next(line for line in lines if line.strip().startswith("[0, 400)"))

    assert "characters as shown, of a 400-character value" in span_line
    assert "x" * 400 not in span_line
    assert "y" * 400 not in span_line


def test_a_backslash_in_the_doubted_text_is_escaped_once_not_twice():
    """`inert` doubles it so the tool's escaping stays distinguishable; `repr` did it again."""
    lines = review_text.render(
        _delivered_act(
            {
                "assessment": {"state": "assessed", "problem": None},
                "uncertain_spans": [
                    {"start": 0, "end": 2, "alternatives": [], "confidence": "low"}
                ],
                "gaps": [],
            },
            text="\\u001b and more",
        )
    )
    span_line = next(line for line in lines if line.strip().startswith("[0, 2)"))

    assert span_line.endswith("confidence low")
    assert "'\\\\u'" in span_line


def test_a_malformed_alternatives_list_is_refused_rather_than_spelled_out():
    """A bare string iterated character by character and printed three readings."""
    with pytest.raises(review_text.ProjectionShapeError) as flat:
        review_text.render(
            _delivered_act(
                {
                    "assessment": {"state": "assessed", "problem": None},
                    "uncertain_spans": [
                        {"start": 0, "end": 1, "alternatives": "abc", "confidence": "low"}
                    ],
                    "gaps": [],
                }
            )
        )
    assert flat.value.field == "acts[].row.uncertainty.uncertain_spans[0].alternatives"
    assert flat.value.index is None

    with pytest.raises(review_text.ProjectionShapeError) as entry:
        review_text.render(
            _delivered_act(
                {
                    "assessment": {"state": "assessed", "problem": None},
                    "uncertain_spans": [
                        {"start": 0, "end": 1, "alternatives": ["ok", 7], "confidence": "low"}
                    ],
                    "gaps": [],
                }
            )
        )
    assert entry.value.field == "acts[].row.uncertainty.uncertain_spans[0].alternatives"
    assert entry.value.index == 1


def test_offsets_that_cannot_anchor_show_no_ink_at_all():
    """Python slicing never complains: a negative start showed the act's END.

    Ink presented as the doubted region that is not it (GOALS 5), under a line
    printing the offsets that would have told a careful reader something was
    wrong.
    """
    lines = review_text.render(
        _delivered_act(
            {
                "assessment": {"state": "assessed", "problem": None},
                "uncertain_spans": [
                    {"start": -3, "end": 2, "alternatives": [], "confidence": "low"},
                    {"start": 0, "end": 99, "alternatives": [], "confidence": "low"},
                ],
                "gaps": [],
            }
        )
    )
    text = "\n".join(lines)

    assert text.count("(these offsets do not anchor to the text shown)") == 2
    assert "'eta'" not in text


def test_an_unrecognised_state_is_named_as_one_rather_than_echoed():
    """The vocabulary is three words; a tampered tree may hold a fourth."""
    lines = review_text.render(
        _delivered_act(
            {
                "assessment": {"state": "confident", "problem": "why"},
                "uncertain_spans": [],
                "gaps": [],
            }
        )
    )
    text = "\n".join(lines)

    assert "doubts: an unrecognised state (confident) — why" in text

    # And an assessment object with no state at all says so in words, rather
    # than printing this language's `None` as though it were a measurement.
    stateless = "\n".join(
        review_text.render(
            _delivered_act({"assessment": {"problem": None}, "uncertain_spans": [], "gaps": []})
        )
    )
    assert "doubts: no state recorded —" in stateless


def test_a_gap_names_the_chairs_that_corroborate_it_and_the_layer_its_revisions():
    """The record holds more than position and offset, and a person reviewing a
    gap against the ink should see what it holds (goal 4). Naming the chairs an
    absence rests on is not a selection among them: nothing here chooses, and no
    witness reading is shown as text (principle 1)."""
    lines = review_text.render(
        _delivered_act(
            {
                "assessment": {"state": "assessed", "problem": None},
                "uncertain_spans": [],
                "gaps": [
                    {
                        "position": "internal",
                        "start": 6,
                        "end": 6,
                        "witness_evidence": [
                            {"chair": "attestator_1", "variant": "beta"},
                            {"chair": "attestator_2", "variant": ""},
                        ],
                    }
                ],
                "self_revisions": [
                    {"reading_span": {"start": 0, "end": 1}, "prior_span": {"start": 0, "end": 1}}
                ],
            }
        )
    )
    text = "\n".join(lines)

    assert "1 uncertain span(s), 1 gap(s), 1 self-revision(s)" not in text
    assert "0 uncertain span(s), 1 gap(s), 1 self-revision(s)" in text
    assert "gap (internal) at 6; corroborated by attestator_1, attestator_2" in text


@pytest.fixture(scope="module")
def exported_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One run carried through the Armarium, holding at least one act.

    `audit-reproof-cutoff` holds a1 on a re-examination that did not finish, so
    the run is partial, exits 3, and writes an export that delivers one act and
    not the other -- which is the pair this screen has to show after the export.
    """
    run_root = tmp_path_factory.mktemp("exported") / "runs"
    completed = _orchestrate(run_root)
    assert completed.returncode == 3, completed.stderr
    return run_root


def test_a_held_acts_reading_and_doubt_survive_the_export(exported_run: Path):
    """The screen this exists for shows a held act; the export writes it no text.

    F3 closed this asymmetry for crops and witnesses by reading them from the
    run tree; the reading and its doubt report were still on the wrong side of
    it, so a held act showed its doubt line before the export and nothing after.
    """
    projection = dataclasses.asdict(_projection(exported_run))
    assert projection["export"]["present"] is True
    held = [
        act
        for act in projection["acts"]
        if (act.get("row") or {}).get("text") is None and (act.get("row") or {}).get("reading")
    ]
    assert held, "this exported run must carry a non-delivered act with a sealed reading"
    text = "\n".join(review_text.render(projection))

    # Both halves of the same screen: the delivered act's layer, and the held
    # act's reading read back from the run tree.
    assert text.count("doubts:") == len(projection["acts"])
    assert "doubts: not-assessed — the reader reports no doubt assessment" in text


def test_the_pre_export_reading_path_prints_every_state_the_same_way():
    """Four of the five earlier cases drove only the post-export layer.

    The pre-export path reads different keys off a different object, so it needs
    its own cases: it is the path a person meets while a run is still stopped,
    which is what this screen is for.
    """
    absent = "\n".join(
        review_text.render(_reading_act({"outcome": "read", "text": "alpha beta", "audit": {}}))
    )
    assert "doubts: not recorded — this reading was sealed before" in absent

    audited = "\n".join(
        review_text.render(
            _reading_act(
                {
                    "outcome": "read",
                    "text": "alpha beta",
                    "audit": {"unresolved": False, "examination": "complete"},
                    "uncertainty_assessment": {"state": "assessed", "problem": None},
                    "uncertain_spans": [
                        {"start": 0, "end": 5, "alternatives": [], "confidence": "low"}
                    ],
                    "gaps": [],
                }
            )
        )
    )
    assert "assessed by the reader; this view cannot tell which of the span(s) below are" in audited

    with pytest.raises(review_text.ProjectionShapeError) as refused:
        review_text.render(
            _reading_act(
                {
                    "outcome": "read",
                    "text": "alpha beta",
                    "audit": {},
                    "uncertainty_assessment": "assessed",
                }
            )
        )
    assert refused.value.field == "acts[].row.reading.uncertainty_assessment"
    assert refused.value.index is None


def test_a_stopped_runs_own_render_carries_a_doubt_line_for_every_reading(
    witnessed_run: Path, tmp_path: Path
):
    """The projection keys are pinned by a real run, not only by hand-built rows.

    Before this, the keys `_reading_row` carries could have been renamed or
    dropped and no test over a real tree would have noticed: the end-to-end
    render assertions never looked for a doubt line.
    """
    run_root = _writable_copy(witnessed_run, tmp_path / "runs")
    resumed = _orchestrate(run_root, "--from", "perlector", "--to", "recensor")
    assert resumed.returncode == 3, resumed.stderr
    projection = dataclasses.asdict(_projection(run_root))
    assert projection["export"]["present"] is False
    text = "\n".join(review_text.render(projection))

    assert "machine reading:" in text
    assert text.count("doubts:") == len(projection["acts"])
    assert "doubts: not-assessed — the reader reports no doubt assessment" in text


def test_a_malformed_layer_beside_a_missing_assessment_is_still_refused():
    """The absence line used to return before the layers were looked at.

    A record with no assessment and a damaged span list printed one honest
    sentence about the assessment and said nothing at all about the layer --
    the same silence, one field over.
    """
    with pytest.raises(review_text.ProjectionShapeError) as refused:
        review_text.render(
            _delivered_act({"assessment": None, "uncertain_spans": "not a list", "gaps": []})
        )
    assert refused.value.field == "acts[].row.uncertainty.uncertain_spans"
    assert refused.value.index is None


def test_spans_published_without_an_assessment_are_still_shown():
    """And the valid entries such a record does carry are printed, not swallowed."""
    text = "\n".join(
        review_text.render(
            _delivered_act(
                {
                    "assessment": None,
                    "uncertain_spans": [
                        {"start": 0, "end": 5, "alternatives": [], "confidence": "low"}
                    ],
                    "gaps": [],
                }
            )
        )
    )

    assert "doubts: not recorded — this reading was sealed before" in text
    assert "published beside that absence: 1 uncertain span(s), 0 gap(s)" in text
    assert "[0, 5) 'alpha' confidence low" in text


def test_a_gaps_offsets_are_checked_before_it_is_printed_as_a_position():
    """A gap carries no characters of its own, so its two offsets are one position.

    Anything else is a damaged record, and printing it as an anchored position
    points a person at ink the record does not name (GOALS 5).
    """
    text = "\n".join(
        review_text.render(
            _delivered_act(
                {
                    "assessment": {"state": "assessed", "problem": None},
                    "uncertain_spans": [],
                    "gaps": [
                        {"position": "internal", "start": 6, "end": 6},
                        {"position": "internal", "start": 2, "end": 5},
                        {"position": "trailing", "start": 99, "end": 99},
                        {"position": "leading", "start": True, "end": True},
                    ],
                }
            )
        )
    )

    assert "gap (internal) at 6" in text
    assert text.count("does not anchor to the text shown as one zero-width position") == 3


def test_a_falsey_witness_evidence_value_is_refused_rather_than_read_as_none():
    """`or ()` swallowed `""`, `0` and `{}`, each of which is a damaged value."""
    for value in ("", 0, {}):
        with pytest.raises(review_text.ProjectionShapeError) as refused:
            review_text.render(
                _delivered_act(
                    {
                        "assessment": {"state": "assessed", "problem": None},
                        "uncertain_spans": [],
                        "gaps": [
                            {
                                "position": "internal",
                                "start": 6,
                                "end": 6,
                                "witness_evidence": value,
                            }
                        ],
                    }
                )
            )
        assert refused.value.field == "acts[].row.uncertainty.gaps[0].witness_evidence"
        assert refused.value.index is None


def test_a_malformed_audit_on_a_reading_is_refused_rather_than_read_as_absent():
    """Mapped to `None` at the projection, a fault printed as an ordinary absence."""
    with pytest.raises(review_text.ProjectionShapeError) as refused:
        review_text.render(
            _reading_act({"outcome": "read", "text": "alpha beta", "audit": "complete"})
        )
    assert refused.value.field == "acts[].row.reading.audit"
    assert refused.value.index is None


def test_a_delivered_export_row_without_a_witness_basis_is_refused_not_recovered():
    """Recovery is for the rows the Armarium deliberately writes thin.

    A delivered act is described entirely by its export record, so one with no
    witness basis is damaged. Reading its witnesses and its reading out of the
    run tree instead would paper over that and show the delivered text beside a
    reading the export never named.
    """
    export_ref = {"relative_path": "7_armarium/artifacts/export/x.json", "sha256": "0" * 64}
    delivered = {"act_id": "act_0123456789abcdef", "act_key": "a1", "text": "alpha"}

    with pytest.raises(OperatorError) as refused:
        review._normalised_act_row(delivered, export_ref, [], delivered=True)
    assert refused.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    detail = refused.value.detail or ""
    assert "delivers act" in detail and "no witness basis" in detail

    # The same row, not delivered, is the thin shape recovery exists for: no
    # witnesses in the run tree here either, so it comes back unchanged rather
    # than refused.
    assert review._normalised_act_row(delivered, export_ref, [], delivered=False) == delivered


def test_an_act_that_was_never_read_is_not_told_its_reading_predates_a_contract():
    """`not-run` is no reading at all: held before the Perlector, no chair, over capacity.

    Those records carry no text either, which is what separates them from a
    reading sealed before the doubt report was part of the record.
    """
    not_run = "\n".join(
        review_text.render(_reading_act({"outcome": "not-run", "reason": "no chair configured"}))
    )
    assert "doubts: not recorded — this act was not read" in not_run

    sealed = "\n".join(
        review_text.render(_reading_act({"outcome": "read", "text": "alpha beta", "audit": {}}))
    )
    assert "doubts: not recorded — this reading was sealed before" in sealed

    # A reading that ran and carries no text is a damaged record, not an act
    # that was never read: refused by field, never printed as the unread line.
    for text in (None, 7, {"text": "alpha"}, ["alpha"]):
        with pytest.raises(review_text.ProjectionShapeError) as refused:
            review_text.render(_reading_act({"outcome": "read", "text": text, "reason": "damaged"}))
        assert refused.value.field == "acts[].row.reading.text"
        assert refused.value.index is None
