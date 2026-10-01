"""`verbatus decide` records each kind of review decision against the run's latest review.

The tree is the fixture's `page-review` scenario through its Recensor: p1:1 and
p1:2 are accepted, and p2:1, an entry the reading could not place, is held on
its own reading and its page's. Every decision recorded through the command is
then applied by the real Recensor as current, so the basis the command
computed is the one the stage computes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from common.contracts.approval import validate_approval_record
from common.runtree.store import RunTree
from conftest import build_page_tree, programs_through, run_stage

from . import cli

RUN_ID = "r"
SCENARIO = "page-review"
RECENSOR = "pipeline/5_recensor/run.py"


@pytest.fixture(scope="module")
def recensed(tmp_path_factory) -> tuple[Path, dict]:
    base = tmp_path_factory.mktemp("decide")
    root, options = build_page_tree(base, SCENARIO)
    assert run_stage(root, RUN_ID, SCENARIO, RECENSOR, **options).returncode == 3
    return root, options


def _copy(recensed, tmp_path: Path) -> tuple[Path, dict]:
    root, options = recensed
    shutil.copytree(root, tmp_path / "runs")
    return tmp_path / "runs", options


def _decide(root: Path, tmp_path: Path, monkeypatch, *words: str, confirm=None) -> int:
    """Run `verbatus decide`, typing back exactly the phrase it asks for unless `confirm`."""
    asked: list[str] = []

    def typed(phrase: str) -> str:
        asked.append(phrase)
        return phrase if confirm is None else confirm

    monkeypatch.setattr(cli, "_typed_decide_confirmation", typed)
    return cli.main(
        [
            "--workspace",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / "state"),
            "decide",
            "--run-root",
            str(root),
            "--run-id",
            RUN_ID,
            *words,
        ]
    )


def _stored(root: Path) -> list[dict]:
    return [record for _reference, record in RunTree(root, RUN_ID).review_decision_records()]


def _decisions_record(root: Path) -> dict:
    records = []
    for path in sorted((root / RUN_ID / "5_recensor").rglob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict) and record.get("kind") == "review-decisions":
            records.append(record["payload"])
    return max(records, key=lambda payload: payload["attempt_ordinal"])


@pytest.mark.parametrize(
    "words, scope, decision, finding",
    [
        (("exclude", "--unit", "p2:1"), "unit", "exclude", None),
        (("hold", "--unit", "p1:1", "--finding", "text-misread"), "unit", "hold", "text-misread"),
        (("re-ask", "--unit", "p2:1"), "unit", "re-ask", None),
        (("no-missed-act", "--page", "2"), "page", "no-missed-act", None),
        (("missed-act", "--page", "1"), "page", "missed-act", None),
        (("re-ask", "--page", "2"), "page", "re-ask", None),
        (("re-shoot", "--page", "2"), "page", "re-shoot", None),
        (("hold", "--page", "2", "--finding", "region-wrong"), "page", "hold", "region-wrong"),
    ],
    ids=lambda value: "-".join(value) if isinstance(value, tuple) else None,
)
def test_each_decision_is_recorded_current_and_the_recensor_applies_it(
    recensed, tmp_path, monkeypatch, capsys, words, scope, decision, finding
):
    root, options = _copy(recensed, tmp_path)
    assert _decide(root, tmp_path, monkeypatch, *words, "--reason", "seen on the page") == 0
    out = capsys.readouterr().out
    assert f"Recorded: {decision}" in out and "by project-lead" in out
    assert "Next: resume the run from the recensor" in out
    [record] = _stored(root)
    validate_approval_record(record)
    assert record["review"]["scope"] == scope
    assert (record["review"]["decision"], record["review"]["finding"]) == (decision, finding)

    assert run_stage(root, RUN_ID, SCENARIO, RECENSOR, **options).returncode in (0, 3)
    applied = _decisions_record(root)
    assert [summary["decision_hash"] for summary in applied["applied"]] == [record["self_hash"]]
    assert applied["stale"] == []


def test_a_page_rerun_request_is_recorded_and_says_what_is_missing(
    recensed, tmp_path, monkeypatch, capsys
):
    root, options = _copy(recensed, tmp_path)
    assert _decide(root, tmp_path, monkeypatch, "re-ask", "--page", "2", "--reason", "re-read") == 0
    out = capsys.readouterr().out
    assert "Nothing can start that re-read yet" in out
    assert run_stage(root, RUN_ID, SCENARIO, RECENSOR, **options).returncode == 3
    [request] = _decisions_record(root)["requests"]
    assert (request["scope"], request["page_ordinal"], request["decision"]) == ("page", 2, "re-ask")


@pytest.mark.parametrize(
    "words, refusal",
    [
        (("release", "--unit", "p2:1"), "cannot send it to export"),
        (("release", "--unit", "p1:1"), "no hold of its own to release"),
        (("exclude", "--unit", "p9:9"), "reviewed no unit 'p9:9'"),
        (("no-missed-act", "--page", "9"), "reviewed nothing on page 9"),
        (("hold", "--unit", "p1:1"), "a hold names its finding"),
    ],
    ids=[
        "unexportable-release",
        "nothing-to-release",
        "unknown-unit",
        "unknown-page",
        "no-finding",
    ],
)
def test_a_decision_its_subject_does_not_allow_is_refused_and_nothing_is_written(
    recensed, tmp_path, monkeypatch, capsys, words, refusal
):
    root, _options = _copy(recensed, tmp_path)
    assert _decide(root, tmp_path, monkeypatch, *words, "--reason", "why") == 2
    assert refusal in capsys.readouterr().out
    assert _stored(root) == []


def test_a_mistyped_confirmation_writes_nothing(recensed, tmp_path, monkeypatch, capsys):
    root, _options = _copy(recensed, tmp_path)
    code = _decide(
        root, tmp_path, monkeypatch, "exclude", "--unit", "p2:1", "--reason", "why", confirm="yes"
    )
    assert code == 2
    assert "did not exactly name this decision" in capsys.readouterr().out
    assert _stored(root) == []


def test_a_run_that_published_its_export_is_refused(tmp_path, monkeypatch, capsys):
    root = tmp_path / "runs"
    for program in programs_through("armarium"):
        assert run_stage(root, RUN_ID, "happy", program).returncode in (0, 3), program
    assert (
        _decide(
            root,
            tmp_path,
            monkeypatch,
            "hold",
            "--unit",
            "p1:1",
            "--finding",
            "other",
            "--reason",
            "late",
        )
        == 2
    )
    assert "the Armarium has published its export" in capsys.readouterr().out
    assert _stored(root) == []
