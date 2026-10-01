"""The reconstruction report, on synthetic records and on the fixture's page run."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, verify_self_hash
from common.runtree.store import RunTree

from . import CorpusRefusal
from .compare import ReadOnlyRunTree, load_exemplar_page_shas, load_pipeline_reading_acts
from .reconstruction_evaluate import evaluate_run, main, reconstruction_report
from .reference import build_reference_page
from .test_evaluate import ROOT, _fixture_reference_for_page_one, _orchestrate

HEAD_TEXT = "Le dix mai, baptisé"
TAIL_TEXT = "Jean, fils de Pierre"


def _join(join_id: str = "join-1-2-0", status: str = "reconstructed", reason=None) -> dict:
    return {
        "join_id": join_id,
        "status": status,
        "not_reconstructed_reason": reason,
        "head_act_ids": ["act_h"],
        "tail_act_ids": ["act_t"],
    }


def _reconstruction(text: str = HEAD_TEXT + "\n" + TAIL_TEXT) -> dict:
    return {
        "join_id": "join-1-2-0",
        "head_act_id": "act_h",
        "tail_act_id": "act_t",
        "head_page_ordinal": 1,
        "tail_page_ordinal": 2,
        "reconstructed_text": text,
    }


def _report(reconstruction: dict, references: dict | None = None, joins=None) -> dict:
    return reconstruction_report(
        joins=joins or [_join()],
        reconstructions=[reconstruction],
        delivered_texts={"act_h": HEAD_TEXT, "act_t": TAIL_TEXT},
        references_by_act=references or {},
    )


def test_a_faithful_reconstruction_departs_from_nothing():
    report = _report(_reconstruction())

    assert report["reconstructions"] == {
        "measured": True,
        "total": 1,
        "acts_joined": 2,
        "departing": 0,
        "departures": 0,
        "departures_by_act": {"act_h": 0, "act_t": 0},
    }
    assert report["reference"]["by_sides"] == {"none": 1}
    assert report["rows"][0]["score"] is None


def test_a_reconstruction_that_changes_a_delivered_text_departs_and_is_counted_per_act():
    report = _report(_reconstruction(HEAD_TEXT + " " + TAIL_TEXT + "."))

    assert report["reconstructions"]["departing"] == 1
    assert report["reconstructions"]["departures"] == 2
    assert report["reconstructions"]["departures_by_act"] == {"act_h": 2, "act_t": 2}


def test_a_reconstruction_is_scored_only_where_the_reference_has_both_acts():
    head = {"record_id": "r-head", "text": HEAD_TEXT}
    tail = {"record_id": "r-tail", "text": "Jean, fils de Paul"}

    both = _report(_reconstruction(), {"act_h": head, "act_t": tail})
    head_only = _report(_reconstruction(), {"act_h": head})

    [row] = both["rows"]
    assert row["reference_sides"] == "both"
    assert (row["head_record_id"], row["tail_record_id"]) == ("r-head", "r-tail")
    assert row["score"]["cer_errors"] > 0
    assert both["reference"]["cer"]["units"] == row["score"]["cer_units"]
    assert head_only["reference"] == {
        "by_sides": {"head-only": 1},
        "cer": {"errors": 0, "units": 0},
        "wer": {"errors": 0, "units": 0},
    }


def test_joins_are_counted_by_status_and_reason():
    joins = [_join(), _join("join-2-3-0", "not-reconstructed", "head-not-delivered")]

    report = _report(_reconstruction(), joins=joins)

    assert report["joins"] == {
        "total": 2,
        "by_status": {"not-reconstructed": 1, "reconstructed": 1},
        "not_reconstructed_by_reason": {"head-not-delivered": 1},
    }


def test_reconstructions_that_disagree_with_the_joins_are_refused():
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        _report(_reconstruction(), joins=[_join(status="not-reconstructed")])
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        reconstruction_report(
            joins=[_join()],
            reconstructions=[_reconstruction()],
            delivered_texts={"act_h": HEAD_TEXT},
            references_by_act={},
        )


def test_the_report_carries_no_text():
    head = {"record_id": "r-head", "text": HEAD_TEXT}
    tail = {"record_id": "r-tail", "text": TAIL_TEXT}
    serialized = json.dumps(_report(_reconstruction(), {"act_h": head, "act_t": tail}))

    assert "baptisé" not in serialized and "Pierre" not in serialized


@pytest.fixture(scope="module")
def happy_run(tmp_path_factory) -> RunTree:
    run_root = tmp_path_factory.mktemp("reconstruction-runs")
    completed = _orchestrate(run_root, "happy")
    # A run with a reconstruction is partial by design: exit 3, export sealed.
    assert completed.returncode == 3, completed.stderr
    return RunTree(run_root, "r")


def _page_two_reference(tree: RunTree) -> dict:
    """Reference truth for the tail act on page 2, over the region it was read on."""
    skeleton = tomllib.load((ROOT / "proof" / "skeleton_fixture.toml").open("rb"))
    page = next(row for row in skeleton["page"] if row["ordinal"] == 2)
    sha = load_exemplar_page_shas(ReadOnlyRunTree(tree))[2]
    [tail] = [
        act
        for act in load_pipeline_reading_acts(ReadOnlyRunTree(tree))
        if act["page_sha256"] == sha
    ]
    text = "SYNTHETIC ACT TWO delta epsilon zeta eta"
    return build_reference_page(
        page={"sha256": sha, "width": page["width"], "height": page["height"]},
        source="fixture",
        volume="synthetic-two-page-v0",
        designation="page-2",
        split="val",
        records=[
            {
                "record_id": "a2-tail",
                "region": tail["bounds"],
                "split": "val",
                "text": text,
                "text_sha256": digest_bytes(text.encode("utf-8")),
            }
        ],
    )


def test_the_fixture_reconstruction_is_counted_and_scored_apart(happy_run: RunTree):
    references = [_fixture_reference_for_page_one(happy_run), _page_two_reference(happy_run)]

    report = evaluate_run(happy_run, references)

    assert verify_self_hash(report)
    assert report["joins"]["by_status"] == {"reconstructed": 1}
    assert report["reconstructions"]["total"] == 1
    assert report["reconstructions"]["departing"] == 0
    assert report["reference"]["by_sides"] == {"both": 1}
    [row] = report["rows"]
    assert (row["head_record_id"], row["tail_record_id"]) == ("a2", "a2-tail")
    assert row["score"]["cer_units"] > 0
    assert canonical_bytes(evaluate_run(happy_run, references)) == canonical_bytes(report)


def test_cli_writes_the_report_outside_the_run_tree(happy_run: RunTree, tmp_path: Path):
    pages = tmp_path / "reference-pages.jsonl"
    pages.write_bytes(canonical_bytes(_fixture_reference_for_page_one(happy_run)) + b"\n")
    args = ["--run-root", str(happy_run.root.parent), "--run-id", happy_run.run_id]
    args += ["--reference-pages", str(pages)]
    out = tmp_path / "reconstruction.json"

    assert main([*args, "--out", str(out)]) == 0
    assert json.loads(out.read_bytes())["reference"]["by_sides"] == {"head-only": 1}
    with pytest.raises(CorpusRefusal, match="^output-exists:"):
        main([*args, "--out", str(out)])
    with pytest.raises(CorpusRefusal, match="^output-in-run-tree:"):
        main([*args, "--out", str(happy_run.root / "reconstruction.json")])


def test_reconstructions_not_packaged_as_jsonl_are_counted_as_not_measured():
    report = reconstruction_report(
        joins=[_join()],
        reconstructions=None,
        delivered_texts={"act_h": HEAD_TEXT, "act_t": TAIL_TEXT},
        references_by_act={},
    )

    assert report["joins"]["by_status"] == {"reconstructed": 1}
    assert report["reconstructions"]["measured"] is False
    assert report["reconstructions"]["total"] == 0
