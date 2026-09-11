"""The review surface opens an unfinished run and says what happened to it.

Independent audit of 2026-09-10, finding F3, reproduced on `0aa08db7e4`: after the
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
    assert "no completed export" in projected.export["note"]
    assert projected.export["record_ref"] is None
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
    assert "--from perlector --to armarium" in projected.next_action["summary"]
    assert projected.next_action["held_acts"] == 0
    assert projected.review_items is None
    assert projected.holds == ()

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

    text = "\n".join(review_text.render(dataclasses.asdict(projected)))
    assert "perlector: not-run" in text
    assert "witnessed, awaiting the Perlector" in text
    assert "Review queue: not produced" in text
    assert "resume the run from perlector" in text


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
    assert cut["row"]["reading"]["truncation"] == "complete"
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
    assert "held-for-review by the recensor; audit examination incomplete" in text
    assert "did not complete" in text
    assert "machine reading:" in text

    # The export arrives; the same surface now shows its accounting, and the
    # hold neither disappears nor changes its reason.
    for stage, expected_exit in (("archetypus", 0), ("armarium", 3)):
        completed = _orchestrate(run_root, "--stage", stage)
        assert completed.returncode == expected_exit, (stage, completed.stderr)
    after = _projection(run_root)
    assert after.export["present"] is True
    assert after.export["record_ref"]["relative_path"].startswith("7_armarium/artifacts/export/")
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
    assert "resume the run from perlector" in plain
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
