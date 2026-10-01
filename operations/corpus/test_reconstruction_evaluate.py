"""The reconstruction report, on synthetic rows and on the fixture's page run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, verify_self_hash
from common.runtree.store import RunTree

from . import CorpusRefusal
from .reconstruction_evaluate import evaluate_run, main, reconstruction_report
from .test_evaluate import ROOT, _fixture_reference_for_page_one, _orchestrate

HEAD_TEXT = "Le dix mai, baptisé"
TAIL_TEXT = "Jean, fils de Pierre"
DELIVERED = {"act_h": HEAD_TEXT, "act_t": TAIL_TEXT}


def _row(
    act_ids=("act_h",),
    *,
    text: str | None = "Le dix mai, baptise",
    not_made=(),
    flags=(),
    unit: str = "act",
) -> dict:
    made = text is not None
    return {
        "unit": unit,
        "act_ids": list(act_ids),
        "act_keys": [f"p{n}:1" for n, _ in enumerate(act_ids, start=1)],
        "made": made,
        "maker": {"kind": "model"},
        "diplomatic_raw_pieces": [DELIVERED[act_id] for act_id in act_ids],
        "reconstruction_text": text,
        "departures": [{"diplomatic": "é", "reconstruction": "e"}] if made else [],
        "flags": [{"code": code} for code in flags],
        "not_made": [{"code": code} for code in not_made],
    }


def _join(status: str = "not-reconstructed", reason: str = "no-code-join") -> dict:
    return {"join_id": "join-1-2-0", "status": status, "not_reconstructed_reason": reason}


def _report(rows, references=None, joins=(), shown=None) -> dict:
    return reconstruction_report(
        joins=list(joins),
        shown=[row["act_ids"] for row in rows] if shown is None else shown,
        rows=rows,
        delivered_texts=DELIVERED,
        references_by_act=references or {},
    )


def test_a_made_reconstruction_is_counted_with_its_departures():
    report = _report([_row()])

    made = report["reconstructions"]
    assert made["measured"] is True
    assert (made["total"], made["made"], made["by_unit"], made["by_maker"]) == (
        1,
        1,
        {"act": 1},
        {"model": 1},
    )
    assert (made["departures"], made["departing"], made["departed_characters"]) == (1, 1, 1)
    assert report["reference"]["made_by_sides"] == {"none": 1}
    assert report["rows"][0]["score"] is None


def test_a_reconstruction_is_scored_beside_its_diplomatic_where_the_reference_has_every_act():
    reference = {"record_id": "r-head", "text": "Le dix mai, baptise"}

    report = _report([_row()], {"act_h": reference})

    [row] = report["rows"]
    assert row["reference_sides"] == "all"
    assert row["reference_record_ids"] == ["r-head"]
    assert row["score"]["reconstruction"]["cer_errors"] == 0
    assert row["score"]["diplomatic"]["cer_errors"] > 0
    assert report["reference"]["reconstruction"] == row["score"]["reconstruction"]
    assert report["reference"]["diplomatic"] == row["score"]["diplomatic"]


def test_a_join_with_one_act_in_the_reference_is_counted_and_not_scored():
    join = _row(("act_h", "act_t"), text=HEAD_TEXT + " " + TAIL_TEXT, unit="join")

    report = _report([join], {"act_h": {"record_id": "r-head", "text": HEAD_TEXT}})

    assert report["reference"]["made_by_sides"] == {"some": 1}
    assert report["rows"][0]["score"] is None
    assert report["rows"][0]["departed_characters"] == 1


def test_a_reconstruction_not_made_is_counted_by_why_and_its_flags_change_nothing():
    report = _report(
        [_row(text=None, not_made=["reply-cut-off"], flags=["inconsistent"]), _row(("act_t",))]
    )

    made = report["reconstructions"]
    assert made["made"] == 1 and made["total"] == 2
    assert made["not_made_by_code"] == {"reply-cut-off": 1}
    assert made["flags_by_code"] == {"inconsistent": 1}


def test_joins_are_counted_by_status_and_reason():
    joins = [_join(), _join(reason="head-not-delivered")]

    report = _report([], joins=joins)

    assert report["joins"] == {
        "total": 2,
        "by_status": {"not-reconstructed": 2},
        "by_reason": {"head-not-delivered": 1, "no-code-join": 1},
    }


def test_rows_that_disagree_with_the_sources_or_the_delivered_literals_are_refused():
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        _report([_row()], shown=[["act_t"]])
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        reconstruction_report(
            joins=[],
            shown=[["act_h"]],
            rows=[_row()],
            delivered_texts={"act_t": TAIL_TEXT},
            references_by_act={},
        )
    altered = {**_row(), "diplomatic_raw_pieces": ["Le onze mai, baptisé"]}
    with pytest.raises(CorpusRefusal, match="other than the one delivered"):
        _report([altered])


def test_the_report_carries_no_text():
    reference = {"record_id": "r-head", "text": HEAD_TEXT}
    serialized = json.dumps(_report([_row()], {"act_h": reference}))

    assert "baptis" not in serialized and "Pierre" not in serialized


def test_rows_not_packaged_as_jsonl_are_counted_as_not_measured():
    report = reconstruction_report(
        joins=[_join()],
        shown=[["act_h"]],
        rows=None,
        delivered_texts=DELIVERED,
        references_by_act={},
    )

    assert report["reconstructions"]["measured"] is False
    assert report["reconstructions"]["shown"] == 1
    assert report["reconstructions"]["total"] == 0


@pytest.fixture(scope="module")
def happy_run(tmp_path_factory) -> RunTree:
    """The `happy` run with the Coniector on, its default: one fixture reply per page."""
    run_root = tmp_path_factory.mktemp("reconstruction-runs")
    config = ROOT / "config" / "reconstruction.toml"
    completed = _orchestrate(run_root, "happy", "--reconstruction-config", str(config))
    # The act across the page break keeps the export partial: exit 3, export sealed.
    assert completed.returncode == 3, completed.stderr
    return RunTree(run_root, "r")


def test_the_fixture_reconstructions_are_counted_and_scored_apart(happy_run: RunTree):
    references = [_fixture_reference_for_page_one(happy_run)]

    report = evaluate_run(happy_run, references)

    assert verify_self_hash(report)
    assert report["joins"]["by_status"] == {"not-reconstructed": 1}
    made = report["reconstructions"]
    assert made["measured"] is True
    assert made["total"] == 3 and made["by_unit"] == {"act": 3}
    # p1:1's one departure resolves a doubt mark its clean literal already reads as
    # "gamma"; only p2:1's ("zeta eta" -> "zeta theta") changes delivered characters.
    assert made["made"] == 3 and made["departures"] == 2 and made["departing"] == 1
    # Page 1's two acts are in the reference; page 2's is not.
    assert report["reference"]["made_by_sides"] == {"all": 2, "none": 1}
    assert report["reference"]["reconstruction"]["cer_units"] > 0
    assert canonical_bytes(evaluate_run(happy_run, references)) == canonical_bytes(report)


def test_cli_writes_the_report_outside_the_run_tree(happy_run: RunTree, tmp_path: Path):
    pages = tmp_path / "reference-pages.jsonl"
    pages.write_bytes(canonical_bytes(_fixture_reference_for_page_one(happy_run)) + b"\n")
    args = ["--run-root", str(happy_run.root.parent), "--run-id", happy_run.run_id]
    args += ["--reference-pages", str(pages)]
    out = tmp_path / "reconstruction.json"

    assert main([*args, "--out", str(out)]) == 0
    assert json.loads(out.read_bytes())["reconstructions"]["made"] == 3
    with pytest.raises(CorpusRefusal, match="^output-exists:"):
        main([*args, "--out", str(out)])
    with pytest.raises(CorpusRefusal, match="^output-in-run-tree:"):
        main([*args, "--out", str(happy_run.root / "reconstruction.json")])
