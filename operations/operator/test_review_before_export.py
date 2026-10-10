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
from pathlib import Path

import pytest

from common.contracts.approval import build_review_decision_record
from common.contracts.canonical import digest_bytes
from common.runtree.store import RunTree
from conftest import stage_programs
from operations.operator import advance, cli, review, review_text
from operations.operator.errors import ErrorCode, OperatorError

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
RUN_ID = "r"
SCENARIO = "happy"


def _orchestrate(
    run_root: Path, *extra: str, scenario: str = SCENARIO
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            scenario,
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


@pytest.fixture(scope="module")
def read_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One run carried to the Perlector boundary and stopped there."""
    run_root = tmp_path_factory.mktemp("read") / "runs"
    completed = _orchestrate(run_root, "--from", "door", "--to", "perlector")
    assert completed.returncode == 0, completed.stderr
    return run_root


def test_a_run_stopped_after_the_witnesses_opens_with_its_images_and_names_what_has_not_run(
    witnessed_run: Path,
):
    """A partial run must remain readable with its available images."""
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
        "coniector": "not-run",
        "armarium": "not-run",
    }
    assert projected.next_action["resume_from"] == "perlector"
    # The operator's own word, complete enough to type, and the stage census
    # said rather than inferred from the first stage that is not sealed.
    summary = projected.next_action["summary"]
    assert f"`verbatus run --run-id {RUN_ID}`" in summary
    assert "picks up from perlector" in summary
    assert "door, exemplar, ink-map, designator, attestatores sealed" in summary
    assert (
        "perlector, coniector, recensor, archetypus, armarium left no record or seal here"
        in summary
    )
    assert "--from" not in summary, "an operator surface prints no orchestrator flag"
    assert projected.next_action["held_acts"] == 0
    assert projected.next_action["hold_records"] == 0
    assert projected.review_items is None
    assert projected.holds == ()
    assert projected.pages_declared == 2

    # The exact images, verified against the sealed digests, each row naming
    # the Exemplar record it came from rather than an export.
    assert [page["ordinal"] for page in projected.pages] == [1, 2]
    for page in projected.pages:
        assert page["outcome"] == "sealed"
        assert page["image_path"].startswith("1_exemplar/blobs/sha256/")
        assert digest_bytes(tree.read_bytes(page["image_path"])) == page["image_sha256"]
        assert page["record_ref"]["relative_path"].startswith("1_exemplar/artifacts/page/")
    # Acts are named by the page reading, which has not run: none is listed,
    # and the count says why rather than reading as a run with no acts.
    assert projected.acts == ()
    assert projected.acts_denominator_note == review._NO_READING_NOTE

    text = "\n".join(review_text.render(dataclasses.asdict(projected)))
    assert "perlector: not-run" in text
    assert "Acts (0; the Perlector has not read the pages" in text
    assert "Review queue: not produced (there is no Armarium export record" in text
    assert "Pages (2 of 2 declared)" in text
    assert f"`verbatus run --run-id {RUN_ID}`" in text


def test_a_run_stopped_after_the_perlector_shows_each_entry_it_read_with_its_crop(
    read_run: Path,
):
    """Before export, the acts are the Perlector's own entries, each with its crop."""
    projected = _projection(read_run)
    tree = RunTree(read_run, RUN_ID)

    # The resume point is whatever stage follows the Perlector in the
    # orchestrator's own sequence (the Coniector today), not a name pinned here.
    names = list(stage_programs())
    assert projected.next_action["resume_from"] == names[names.index("perlector") + 1]
    assert projected.acts, "the happy pages carry entries the Perlector read"
    assert projected.acts_denominator_note == review._PRE_EXPORT_ACTS_NOTE
    for act in projected.acts:
        assert act["category"].startswith("read: ")
        assert act["category"].endswith(", awaiting the Recensor")
        assert act["record_ref"]["relative_path"].startswith("4_perlector/artifacts/act-region/")
        assert act["crops"], "every placed entry shows the crop the Perlector cut"
        for crop in act["crops"]:
            assert digest_bytes(tree.read_bytes(crop["image_path"])) == crop["image_sha256"]
        assert act["row"]["reading"] is not None
        assert act["row"]["review"] is None
        assert act["row"]["established"] is None
        assert len(act["row"]["testimonia"]) == 3
        assert all(
            isinstance(witness["attempt_ordinal"], int) for witness in act["row"]["testimonia"]
        ), "each witness row says which attempt it was"

    text = "\n".join(review_text.render(dataclasses.asdict(projected)))
    assert "recensor: not-run" in text
    assert "awaiting the Recensor" in text
    assert "(attempt 1)" in text


@pytest.mark.parametrize(
    ("scenario", "page_two"),
    [("page-unread", "page 2 held (not-json)"), ("page-blank", "page 2 read, no entry")],
)
def test_a_page_read_without_an_entry_is_named_before_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, page_two: str
):
    """A held or blank page reading is named, so a partial result never reads as not started."""
    run_root = tmp_path / "runs"
    completed = _orchestrate(run_root, "--from", "door", "--to", "perlector", scenario=scenario)
    assert completed.returncode in (0, 3), completed.stderr

    projected = _projection(run_root)
    assert {act["row"]["page_ordinal"] for act in projected.acts} == {1}
    note = projected.acts_denominator_note
    assert note.startswith(review._PRE_EXPORT_ACTS_NOTE)
    assert page_two in note and "page 1" not in note

    # Every page read, none with an entry: the note still names each page.
    monkeypatch.setattr(review, "_progressive_acts", lambda *_args: ())
    projected = _projection(run_root)
    assert projected.acts == ()
    note = projected.acts_denominator_note
    assert note != review._NO_READING_NOTE
    assert "page 1 read, no entry" in note and page_two in note
    text = "\n".join(review_text.render(dataclasses.asdict(projected)))
    assert "the Perlector has not read the pages" not in text
    assert "pages read without an entry: page 1" in text


def test_a_labelled_other_reading_is_listed_apart_from_the_acts_before_and_after_export(
    tmp_path: Path,
):
    """An `other` entry never counts as an act, in either view of the same run.

    `page-other-unbroken` reads one labelled other entry on page 1 beside two
    acts. Before export it comes from the Perlector's act-region records;
    after export from the export's `other_readings` layer. Both views list it
    apart from the acts, so the act count does not change across the export.
    """
    views = {}
    for view, extra in (("before", ("--from", "door", "--to", "perlector")), ("after", ())):
        run_root = tmp_path / view / "runs"
        completed = _orchestrate(run_root, *extra, scenario="page-other-unbroken")
        assert completed.returncode == 0, completed.stderr
        views[view] = _projection(run_root)

    before, after = views["before"], views["after"]
    assert after.export["present"] is True and before.export["present"] is False
    assert len(before.acts) == len(after.acts) == 2
    assert [act["act_key"] for act in before.acts] == [act["act_key"] for act in after.acts]
    others = [(other["act_id"], other["act_key"]) for other in before.other_readings]
    assert len(others) == 1
    assert [(other["act_id"], other["act_key"]) for other in after.other_readings] == others
    assert others[0][0] not in {act["act_id"] for act in before.acts + after.acts}
    for projected in (before, after):
        assert projected.other_readings[0]["crops"], "the other reading keeps its crop"
        text = "\n".join(review_text.render(dataclasses.asdict(projected)))
        assert f"Acts ({len(projected.acts)}" in text
        assert "Other readings (1; never counted as acts)" in text


def test_opening_an_unfinished_run_changes_no_path_bytes_size_or_mtime(witnessed_run: Path):
    before = _census(witnessed_run / RUN_ID)
    projected = _projection(witnessed_run)
    assert projected.pages
    assert _census(witnessed_run / RUN_ID) == before


@pytest.mark.parametrize("what", ["page", "crop"])
def test_an_image_whose_bytes_moved_is_refused_by_name_before_export(
    read_run: Path, tmp_path: Path, what: str
):
    run_root = _writable_copy(read_run, tmp_path / "runs")
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


def _act(act_id: str) -> dict[str, object]:
    """One entry as `_progressive_acts` hands it to `_act_summary`."""
    return {"act_id": act_id, "act_key": act_id, "page_id": "p1", "page_ordinal": 1}


def test_a_held_review_is_one_labelled_record_and_one_held_act():
    """The Recensor's current review is the hold; its reason is shown beside it."""
    act_id = "act1"
    recensor_review = _review_record(
        act_id,
        "held-for-review",
        {
            "act_key": "a1",
            "reason": "the witnesses disagree",
            "attempt_ordinal": 1,
        },
    )
    holds = review._holds([recensor_review])
    assert [hold["label"] for hold in holds] == ["Recensor review"]
    assert holds[0]["reason"] == "the witnesses disagree"
    action = review._next_action(RUN_ID, (), {"present": False}, holds)
    assert action["held_acts"] == 1
    assert action["hold_records"] == 1

    text = "\n".join(review_text.render({"run_id": "r", "holds": [dict(hold) for hold in holds]}))
    assert "[Recensor review]" in text
    assert "Held or unresolved acts (1)" in text


def test_a_review_outcome_outside_the_recensor_vocabulary_is_refused_not_skipped():
    """A widened vocabulary must not drop an act out of the holds list in silence."""
    from common.contracts.errors import FatalAccounting

    invented = _review_record(
        "act1", "sent-back-for-rework", {"act_key": "a1", "attempt_ordinal": 1}
    )
    with pytest.raises(FatalAccounting) as refused:
        review._holds([invented])
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

    def refuse(stage_records):
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
        review._act_summary(rows, _act("act1"))
    assert refused.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "2 Archetypus records" in (refused.value.detail or "")


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


def test_an_act_count_with_no_denominator_says_so_rather_than_reading_as_no_acts():
    text = "\n".join(
        review_text.render(
            {
                "run_id": "r",
                "acts": [],
                "acts_denominator_note": review._NO_READING_NOTE,
            }
        )
    )
    assert "Acts (0; the Perlector has not read the pages" in text
    assert "Acts (0)\n" not in text


def test_a_newline_becomes_a_separator_and_the_length_notice_names_two_lengths():
    """Escaping first meant the replacement found nothing to replace."""
    assert review_text._one_line("alpha\nbeta") == "alpha / beta"
    cut = review_text._one_line("x" * 500, 300)
    assert cut.endswith("(first 300 characters as shown, of a 500-character value)")
    assert cut.count("x") == 300


def test_an_empty_queue_row_is_named_by_its_line_number():
    text = "\n".join(
        review_text.render(
            {
                "run_id": "r",
                "review_items": [
                    {"row": {}, "line": 4, "member": "review-items.jsonl", "bundle_path": "b.zip"}
                ],
                "review_items_total": 1,
                "review_page_size": 500,
            }
        )
    )
    assert "line 4 of review-items.jsonl: this queue row is empty" in text
    assert "None: None" not in text
    # An entry that is not this projection's wrapper is itself the row, and its
    # content still reaches the screen.
    raw = "\n".join(
        review_text.render(
            {
                "run_id": "r",
                "review_items": [{"reason": "the margin is torn"}],
                "review_items_total": 1,
                "review_page_size": 500,
            }
        )
    )
    assert "the margin is torn" in raw


@pytest.mark.parametrize("missing", ["review_items_total", "review_page_size"])
def test_review_queue_requires_paging_metadata(missing: str):
    projection = {
        "run_id": "r",
        "review_items": [{"reason": "needs review"}],
        "review_items_total": 1,
        "review_page_size": 500,
    }
    del projection[missing]

    with pytest.raises(review_text.ProjectionShapeError) as excinfo:
        review_text.render(projection)
    assert "review page" in str(excinfo.value)


def test_review_renderer_refuses_page_past_nonempty_queue():
    with pytest.raises(review_text.ProjectionShapeError) as excinfo:
        review_text.render(
            {
                "run_id": "r",
                "review_items": (),
                "review_items_total": 2,
                "review_page": 2,
                "review_page_size": 2,
            }
        )
    assert "review page" in str(excinfo.value)


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
    """A mistyped run id must not be reported as damaged evidence."""
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
    """A damaged tree must not be mistaken for a nonexistent run."""
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


@pytest.mark.hostile_local
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
            }
        ],
        "pages": [],
        "acts": [{"act_id": "a1", "act_key": "x\x1b[2Jwiped", "category": "baptism", "crops": []}],
        "review_items": [{"reason": "adversarial\x1b]0;pwned\x07 escape sequence"}],
        "review_items_total": 1,
        "review_page_size": 500,
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
                "row": {
                    "reading": {
                        "outcome": "read",
                        "text": long_text,
                        "uncertainty_assessment": {"state": "assessed", "problem": None},
                    }
                },
            }
        ],
    }
    text = "\n".join(review_text.render(projection))
    assert "(first 300 characters as shown, of a 900-character value)" in text
    assert "x" * 300 in text


def test_the_review_verb_prints_plain_language_by_default_and_json_on_request(
    witnessed_run: Path, capsys: pytest.CaptureFixture[str]
):
    cli._review(witnessed_run, RUN_ID)
    plain = capsys.readouterr().out
    assert "What you can do next" in plain
    assert f"`verbatus run --run-id {RUN_ID}`" in plain
    assert "picks up from perlector" in plain
    assert "the Perlector has not read the pages" in plain
    assert not plain.lstrip().startswith("{")

    cli._review(witnessed_run, RUN_ID, raw=True)
    raw = capsys.readouterr().out
    assert json.loads(raw) == json.loads(
        json.dumps(dataclasses.asdict(_projection(witnessed_run)), sort_keys=True)
    )
    assert json.loads(raw)["export"]["present"] is False


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


def test_a_projection_list_entry_that_is_not_an_object_is_refused_by_field_and_index():
    """Refused, never skipped: a row the renderer passed over is a row nobody sees."""
    with pytest.raises(review_text.ProjectionShapeError) as refused:
        review_text.render({"run_id": "r", "progress": [{"stage": "door"}, "not a row"]})
    assert refused.value.field == "progress" and refused.value.index == 1
    with pytest.raises(review_text.ProjectionShapeError) as nested:
        review_text.render({"run_id": "r", "acts": [{"act_id": "a", "act_key": "a", "crops": [7]}]})
    assert nested.value.field == "acts[].crops"


@pytest.mark.parametrize(
    "projection, field",
    [
        ({"export": "present"}, "export"),
        ({"next_action": 7}, "next_action"),
        ({"holds": [{"act_id": "a", "record_ref": "somewhere"}]}, "holds[].record_ref"),
        ({"acts": [{"act_id": "a", "row": "a row"}]}, "acts[].row"),
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
    reading's `truncation` and an act's `row` each crashed the renderer instead
    of refusing.
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
    """Spans published under a `not-assessed` state are not credited to the reader."""
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


def test_an_assessed_layer_is_credited_to_the_reader():
    """No audit runs, so every published span of an assessed layer is the reader's own."""
    first = {"start": 0, "end": 5, "alternatives": [], "confidence": "low"}
    second = {"start": 6, "end": 10, "alternatives": ["beta"], "confidence": "high"}
    delivered = "\n".join(
        review_text.render(
            _delivered_act(
                {
                    "assessment": {"state": "assessed", "problem": None},
                    "uncertain_spans": [first, second],
                    "gaps": [],
                }
            )
        )
    )
    assert "doubts: assessed by the reader; 2 uncertain span(s), 0 gap(s)" in delivered

    read = "\n".join(
        review_text.render(
            _reading_act(
                {
                    "outcome": "read",
                    "text": "alpha beta",
                    "uncertainty_assessment": {"state": "assessed", "problem": None},
                    "uncertain_spans": [second],
                    "gaps": [],
                }
            )
        )
    )
    assert "doubts: assessed by the reader; 1 uncertain span(s), 0 gap(s)" in read


def test_an_identical_pair_is_one_line_with_the_count_and_no_claim_about_its_source():
    """The layer records that the characters were doubted twice, and nothing else.

    Dropping the repeat in the producer would have erased that the layer carried
    it twice, and printing it twice would say two doubts were found where one
    entry appears twice. A fold is evidence of a repeat and of nothing else.
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


def test_a_reading_with_no_doubt_report_is_refused_like_a_malformed_one():
    """Every reading that ran carries the reader's doubt report: absent or malformed is a
    fault of the record, refused by field, never printed as no doubt at all."""
    with pytest.raises(review_text.ProjectionShapeError) as absent:
        review_text.render(_delivered_act({"uncertain_spans": [], "gaps": [], "assessment": None}))
    assert absent.value.field == "acts[].row.uncertainty.assessment"

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


def test_a_delivered_act_with_no_uncertainty_layer_is_refused():
    """An absent or damaged layer is refused, never printed as a reader that found no doubt."""
    with pytest.raises(review_text.ProjectionShapeError) as absent:
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
    assert absent.value.field == "acts[].row.uncertainty.assessment"

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

    Ink presented as the doubted region that is not it, under a line
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


def test_a_gap_names_the_chairs_that_corroborate_it():
    """The record holds more than position and offset, and a person reviewing a
    gap against the ink should see what it holds, not take it on faith. Naming the chairs an
    absence rests on is not a selection among them: nothing here chooses, and no
    witness reading is shown as text."""
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
            }
        )
    )
    text = "\n".join(lines)

    assert "0 uncertain span(s), 1 gap(s)" in text
    assert "gap (internal) at 6; corroborated by attestator_1, attestator_2" in text


def _advance_the_recensor(run_root: Path) -> None:
    """A person's advance of the Recensor's current seal, recorded as `advance` records it."""
    tree = RunTree(run_root, RUN_ID)
    _seal, digest = advance.stored_boundary(tree, "recensor")
    advance.record_advance(
        tree,
        "recensor",
        reason="export with the held readings named",
        expected_digest=digest,
        timestamp="2026-10-01T12:00:00Z",
    )


@pytest.fixture(scope="module")
def exported_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One run carried through the Armarium, holding at least one reading.

    `page-review` holds a reading at the Recensor, so the run stops there,
    before the Archetypus. An advance of the Recensor's seal lets the resumed
    run export: partial, exit 3, delivering some readings and not others --
    the pair this screen has to show after the export.
    """
    run_root = tmp_path_factory.mktemp("exported") / "runs"
    completed = _orchestrate(run_root, scenario="page-review")
    assert completed.returncode == 3, completed.stderr
    assert "stopped at a held recensor, before the archetypus" in completed.stdout
    _advance_the_recensor(run_root)
    resumed = _orchestrate(
        run_root, "--from", "recensor", "--to", "armarium", scenario="page-review"
    )
    assert resumed.returncode == 3, resumed.stderr
    assert "an advance record passes its current seal" in resumed.stdout
    return run_root


@pytest.fixture(scope="module")
def held_recensor_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`page-review` run unattended: it stops at its held Recensor, before the Archetypus."""
    run_root = tmp_path_factory.mktemp("held-recensor") / "runs"
    completed = _orchestrate(run_root, scenario="page-review")
    assert completed.returncode == 3, completed.stderr
    return run_root


def test_a_run_stopped_at_a_held_recensor_says_how_a_decision_resolves_it(
    held_recensor_run: Path,
):
    projected = _projection(held_recensor_run)
    action = projected.next_action
    assert action["resume_from"] == "recensor"
    assert "stopped at a held Recensor, before the Archetypus" in action["summary"]
    assert "--from recensor --to armarium" in action["summary"]
    # A decision the Recensor applies resolves a hold; no new run is required.
    assert "resolved only by a new" not in action["summary"]
    assert "operator review decision recorded in this run" in action["summary"]
    assert projected.review_decisions["stored"] == 0
    text = "\n".join(review_text.render(dataclasses.asdict(projected)))
    assert "Operator review decisions (0 stored)" in text
    assert "none recorded in this run" in text


def test_the_surface_shows_a_decision_no_pass_applied_then_its_state(
    held_recensor_run: Path, tmp_path: Path
):
    """A decision recorded after the Recensor's pass is named, then shown stale once applied."""
    run_root = tmp_path / "runs"
    shutil.copytree(held_recensor_run, run_root)
    tree = RunTree(run_root, RUN_ID)
    held = next(hold for hold in _projection(run_root).holds if hold["act_key"] == "p2:1")
    review_record = json.loads(tree.read_bytes(held["record_ref"]["relative_path"]))
    page_reading = json.loads(
        tree.read_bytes(review_record["payload"]["page_reading_ref"]["relative_path"])
    )
    tree.write_approval_record(
        build_review_decision_record(
            run_id=RUN_ID,
            scope="unit",
            subject_id=held["act_id"],
            page_id=page_reading["subject_id"],
            decision="hold",
            finding="text-misread",
            basis_digest=digest_bytes(b"a review this unit no longer has"),
            reason="held after the Recensor's pass",
            timestamp="2026-10-01T12:00:00Z",
        )
    )

    stale = _projection(run_root)
    assert stale.review_decisions["stored"] == 1
    assert stale.review_decisions["current"] is False
    assert "did not apply as a set" in stale.next_action["summary"]

    resumed = _orchestrate(run_root, "--stage", "recensor", scenario="page-review")
    assert resumed.returncode == 3, resumed.stderr
    applied = _projection(run_root)
    assert applied.review_decisions["current"] is True
    assert [row["stale_because"] for row in applied.review_decisions["stale"]] == ["basis-changed"]
    assert [row["decision"] for row in applied.review_decisions["carried"]] == ["hold"]
    text = "\n".join(review_text.render(dataclasses.asdict(applied)))
    assert "applied exactly the decisions stored now" in text
    assert "stale (1)" in text
    assert "hold of unit" in text


def test_a_held_readings_text_and_doubt_survive_the_export(exported_run: Path):
    """The screen this exists for shows a held reading; the export writes it no text."""
    projection = dataclasses.asdict(_projection(exported_run))
    assert projection["export"]["present"] is True
    held = [
        act
        for act in projection["acts"]
        if (act.get("row") or {}).get("text") is None and (act.get("row") or {}).get("reading")
    ]
    assert held, "this exported run must carry a non-delivered act with a sealed reading"
    assert all(isinstance(act["row"]["reading"]["text"], str) for act in held)
    assert all(act["crops"] for act in projection["acts"] if act["category"] == "delivered")
    text = "\n".join(review_text.render(projection))

    # Both halves of the same screen: the delivered acts' layers, and the held
    # act's reading read back from the run tree.
    assert text.count("doubts:") == len(projection["acts"])


def test_the_pre_export_reading_path_prints_every_state_the_same_way():
    """Four of the five earlier cases drove only the post-export layer.

    The pre-export path reads different keys off a different object, so it needs
    its own cases: it is the path a person meets while a run is still stopped,
    which is what this screen is for.
    """
    with pytest.raises(review_text.ProjectionShapeError) as absent:
        review_text.render(_reading_act({"outcome": "read", "text": "alpha beta"}))
    assert absent.value.field == "acts[].row.reading.uncertainty_assessment"

    assessed = "\n".join(
        review_text.render(
            _reading_act(
                {
                    "outcome": "read",
                    "text": "alpha beta",
                    "uncertainty_assessment": {"state": "assessed", "problem": None},
                    "uncertain_spans": [
                        {"start": 0, "end": 5, "alternatives": [], "confidence": "low"}
                    ],
                    "gaps": [],
                }
            )
        )
    )
    assert "doubts: assessed by the reader; 1 uncertain span(s), 0 gap(s)" in assessed

    with pytest.raises(review_text.ProjectionShapeError) as refused:
        review_text.render(
            _reading_act(
                {
                    "outcome": "read",
                    "text": "alpha beta",
                    "uncertainty_assessment": "assessed",
                }
            )
        )
    assert refused.value.field == "acts[].row.reading.uncertainty_assessment"
    assert refused.value.index is None


def test_a_malformed_layer_beside_a_missing_assessment_is_still_refused():
    """The layers are checked even when the assessment is missing."""
    with pytest.raises(review_text.ProjectionShapeError) as refused:
        review_text.render(
            _delivered_act({"assessment": None, "uncertain_spans": "not a list", "gaps": []})
        )
    assert refused.value.field == "acts[].row.uncertainty.uncertain_spans"
    assert refused.value.index is None


def test_spans_published_without_an_assessment_are_refused():
    """A reading that publishes spans but no doubt report is a damaged record."""
    with pytest.raises(review_text.ProjectionShapeError) as refused:
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
    assert refused.value.field == "acts[].row.uncertainty.assessment"


def test_a_gaps_offsets_are_checked_before_it_is_printed_as_a_position():
    """A gap carries no characters of its own, so its two offsets are one position.

    Anything else is a damaged record, and printing it as an anchored position
    points a person at ink the record does not name.
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


def test_an_act_that_was_never_read_says_so_and_a_reading_without_doubts_is_refused():
    """`not-run` is no reading at all: held before the Perlector, no chair, over capacity.

    Those records carry no text and no doubt report; a reading that ran carries both.
    """
    not_run = "\n".join(
        review_text.render(_reading_act({"outcome": "not-run", "reason": "no chair configured"}))
    )
    assert "doubts: not recorded — this act was not read" in not_run

    with pytest.raises(review_text.ProjectionShapeError) as refused:
        review_text.render(_reading_act({"outcome": "read", "text": "alpha beta"}))
    assert refused.value.field == "acts[].row.reading.uncertainty_assessment"

    # A reading that ran and carries no text is a damaged record, not an act
    # that was never read: refused by field, never printed as the unread line.
    for text in (None, 7, {"text": "alpha"}, ["alpha"]):
        with pytest.raises(review_text.ProjectionShapeError) as refused:
            review_text.render(_reading_act({"outcome": "read", "text": text, "reason": "damaged"}))
        assert refused.value.field == "acts[].row.reading.text"
        assert refused.value.index is None


def test_a_view_review_cannot_read_out_is_its_own_refusal(
    witnessed_run: Path, monkeypatch: pytest.MonkeyPatch
):
    def malformed(_projection):
        raise review_text.ProjectionShapeError("holds", 0, 1)

    monkeypatch.setattr(review_text, "render", malformed)
    with pytest.raises(OperatorError) as refused:
        cli._review(witnessed_run, RUN_ID)
    assert refused.value.code is ErrorCode.CONSOLE_PROJECTION_UNREADABLE
    assert "'holds' entry 0" in (refused.value.detail or "")
