from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash, verify_self_hash
from common.contracts.identities import attempt_id
from common.runtree.store import RunTree
from operations.corpus import CorpusRefusal
from operations.corpus.compare import (
    ReadOnlyRunTree,
    load_exemplar_page_shas,
    load_pipeline_proposal_acts,
)
from operations.corpus.reference import build_reference_page
from operations.corpus.test_evaluate import (
    _fixture_reference_for_page_one,
    _ledger_for,
    _orchestrate,
)
from operations.corpus.witness_evaluate import (
    CHAIRS,
    _attachment_index,
    evaluate_page,
    evaluate_run,
    main,
    page_health_counts,
    witness_reading,
    write_report,
)
from operations.spike_perlector.models import OutputStatus


def _health(*, truncated: bool | None, recordable: bool = True) -> dict:
    return {"recordable": recordable, "truncated": truncated}


def _testimonium(text: str, *, truncated: bool | None = False, outcome: str = "read") -> dict:
    return {
        "outcome": outcome,
        "payload": {"payload": text, "content_health": _health(truncated=truncated)},
    }


def _page_record(chair: str, ordinal: int, *, truncated: bool | None, outcome="read") -> dict:
    subject = f"page-{ordinal}"
    return {
        "artifact_id": f"artifact-{chair}-{ordinal}",
        "subject_id": subject,
        "attempt_id": attempt_id(subject, f"read:{chair}", 1),
        "outcome": outcome,
        "payload": {
            "attempt_ordinal": 1,
            "chair": chair,
            "page_ordinal": ordinal,
            "presented": {"source_page_ordinal": ordinal, "image_sha256": "f" * 64},
            "content_health": _health(truncated=truncated),
        },
    }


def _write_inputs(tmp_path: Path, tree: RunTree, reference: dict) -> tuple[Path, Path]:
    ledger_path = tmp_path / "ledger.json"
    pages_path = tmp_path / "reference-pages.jsonl"
    ledger_path.write_bytes(canonical_bytes(_ledger_for(reference)))
    pages_path.write_bytes(canonical_bytes(reference) + b"\n")
    return ledger_path, pages_path


def _inventory(tree: RunTree) -> dict[str, str]:
    return {
        path.relative_to(tree.root).as_posix(): digest_bytes(path.read_bytes())
        for path in tree.root.rglob("*")
        if path.is_file()
    }


def test_act_scoped_dai_uses_its_sealed_full_crop_span():
    status, text, reason = witness_reading(
        {
            "attached": True,
            "comparable": True,
            "span": {"start": 0, "end": 4},
            "content_health": _health(truncated=False),
        },
        _testimonium("test"),
    )
    assert (status, text, reason) == (OutputStatus.COMPLETE, "test", None)


def test_page_witness_without_alignment_span_is_named_unavailable():
    status, text, reason = witness_reading(
        {
            "attached": True,
            "comparable": False,
            "span": None,
            "content_health": _health(truncated=False),
        },
        _testimonium("page extra text"),
    )
    assert (status, text, reason) == (OutputStatus.UNAVAILABLE, None, "not-comparable")


def test_truncated_sealed_excerpt_retains_its_partial_text():
    status, text, reason = witness_reading(
        {
            "attached": True,
            "comparable": True,
            "span": {"start": 1, "end": 3},
            # This is act-attempt currency, not the page response's completion.
            "content_health": _health(truncated=False),
        },
        _testimonium("abcd", truncated=True),
    )
    assert (status, text, reason) == (OutputStatus.TRUNCATED, "bc", None)


def test_unknown_completion_is_unavailable_instead_of_silently_complete():
    status, text, reason = witness_reading(
        {"attached": True, "comparable": True, "span": {"start": 0, "end": 4}},
        _testimonium("test", truncated=None),
    )
    assert (status, text, reason) == (
        OutputStatus.UNAVAILABLE,
        None,
        "unknown-truncation",
    )


def test_page_health_joins_transformed_presentations_through_source_page_ordinal():
    records = [
        _page_record("attestator_1", 1, truncated=True),
        _page_record("attestator_2", 1, truncated=None, outcome="failed"),
        _page_record("attestator_3", 1, truncated=False),
    ]
    counts = page_health_counts(
        records,
        page_sha256_by_ordinal={1: "a" * 64, 2: "b" * 64},
        chairs=CHAIRS,
    )
    assert counts["attestator_1"] == {
        "missing": 1,
        "truncated_true": 1,
        "truncated_false": 0,
        "truncated_null": 0,
        "failed_model_response": 0,
    }
    assert counts["attestator_2"]["truncated_null"] == 1
    assert counts["attestator_2"]["failed_model_response"] == 1
    assert counts["attestator_3"]["truncated_false"] == 1


@pytest.fixture(scope="module")
def sealed_run(tmp_path_factory) -> RunTree:
    run_root = tmp_path_factory.mktemp("witness-runs")
    completed = _orchestrate(run_root, "happy")
    assert completed.returncode == 0, completed.stderr
    return RunTree(run_root, "r")


def test_cli_scores_all_three_chairs_from_current_sealed_attachments_without_mutating_run(
    sealed_run: RunTree, tmp_path: Path
):
    reference = _fixture_reference_for_page_one(sealed_run)
    ledger_path, pages_path = _write_inputs(tmp_path, sealed_run, reference)
    output = tmp_path / "external" / "witness-report.json"
    before = _inventory(sealed_run)

    assert (
        main(
            [
                "--run-root",
                str(sealed_run.root.parent),
                "--run-id",
                sealed_run.run_id,
                "--ledger",
                str(ledger_path),
                "--reference-pages",
                str(pages_path),
                "--page-id",
                reference["designation"],
                "--output",
                str(output),
            ]
        )
        == 0
    )

    report = json.loads(output.read_bytes())
    assert verify_self_hash(report)
    assert before == _inventory(sealed_run)
    assert report["reference_records"] == len(reference["acts"])
    assert set(report["totals"]) == set(CHAIRS)
    for chair in CHAIRS:
        total = report["totals"][chair]
        assert total["references"] == len(reference["acts"])
        assert total["cer_units"] > 0 and total["wer_units"] > 0

    # Attestator 2 is the act-scoped DAI chair. Each scored row came from its
    # whole sealed crop response, while both page chairs supplied aligned slices.
    assert report["totals"]["attestator_2"]["statuses"]["complete"] == 2
    assert report["totals"]["attestator_1"]["statuses"]["complete"] == 2
    assert report["totals"]["attestator_3"]["statuses"]["complete"] == 2
    assert report["page_health"]["attestator_2"]["missing"] == 1
    assert report["page_health"]["attestator_1"]["truncated_false"] == 1
    assert report["page_health"]["attestator_3"]["truncated_false"] == 1


def test_two_page_act_selects_primary_source_attachment_not_transformed_image_digest(
    sealed_run: RunTree,
):
    reference = _fixture_reference_for_page_one(sealed_run)
    read_only = ReadOnlyRunTree(sealed_run)
    source_sha = load_exemplar_page_shas(read_only)[1]
    attachments = _attachment_index(read_only)
    proposals = load_pipeline_proposal_acts(read_only)
    spanning = next(
        (act_id, by_chair)
        for act_id, by_chair in attachments.items()
        if any(
            len(
                {
                    item["attachment"]["page_ordinal"]
                    for item in rows
                    if item["attachment"]["page_witness"]
                }
            )
            > 1
            for rows in by_chair.values()
        )
    )
    act_id, by_chair = spanning
    for chair in ("attestator_1", "attestator_3"):
        page_one = next(item for item in by_chair[chair] if item["attachment"]["page_ordinal"] == 1)
        assert page_one["testimonium"]["payload"]["presented"]["image_sha256"] != source_sha
        span = page_one["attachment"]["span"]
        page_text = page_one["testimonium"]["payload"]["payload"]
        assert 0 <= span["start"] < span["end"] <= len(page_text)
        assert span["end"] - span["start"] < len(page_text)

    for candidate in attachments.values():
        dai = candidate["attestator_2"][0]
        assert dai["attachment"]["page_witness"] is False
        assert dai["attachment"]["span"] == {
            "start": 0,
            "end": len(dai["testimonium"]["payload"]["payload"]),
        }

    report = evaluate_page(
        reference_page=reference,
        source_page_ordinal=1,
        proposals=[proposal for proposal in proposals if proposal["page_sha256"] == source_sha],
        attachments=attachments,
        chairs=CHAIRS,
    )
    rows = [row for row in report["rows"] if row["pipeline_act_id"] == act_id]
    assert {row["chair"]: row["status"] for row in rows} == {chair: "complete" for chair in CHAIRS}


def test_missing_proposal_is_a_full_deletion_in_every_chair_aggregate(
    sealed_run: RunTree, tmp_path: Path
):
    extra_text = "synthetic unmatched reference"
    reference = _fixture_reference_for_page_one(
        sealed_run,
        extra=[
            {
                "record_id": "synthetic-extra",
                "region": {"x": 175, "y": 225, "w": 20, "h": 20},
                "split": "val",
                "text": extra_text,
                "text_sha256": digest_bytes(extra_text.encode()),
            }
        ],
    )
    ledger_path, pages_path = _write_inputs(tmp_path, sealed_run, reference)
    report = evaluate_run(
        tree=sealed_run,
        ledger_path=ledger_path,
        reference_pages_path=pages_path,
        page_ids=[reference["designation"]],
    )
    assert report["reference_records"] == 3
    for chair in CHAIRS:
        assert report["totals"][chair]["references"] == 3
        assert report["totals"][chair]["missing_proposals"] == 1
        missing = next(
            row
            for row in report["pages"][0]["rows"]
            if row["chair"] == chair and row["reason"] == "missing-proposal"
        )
        assert missing["status"] == "missing"
        assert missing["cer"] == missing["cer_units"]
        assert missing["wer"] == missing["wer_units"]


def test_missing_unavailable_and_truncated_readings_all_remain_in_totals(
    sealed_run: RunTree,
):
    reference = _fixture_reference_for_page_one(sealed_run)
    read_only = ReadOnlyRunTree(sealed_run)
    proposals = [
        proposal
        for proposal in load_pipeline_proposal_acts(read_only)
        if proposal["page_sha256"] == reference["page"]["sha256"]
    ]
    attachments = json.loads(json.dumps(_attachment_index(read_only)))
    for by_chair in attachments.values():
        by_chair.pop("attestator_2", None)
        for item in by_chair.get("attestator_1", []):
            if item["attachment"]["page_ordinal"] == 1:
                item["attachment"]["comparable"] = False
        for item in by_chair.get("attestator_3", []):
            if item["attachment"]["page_ordinal"] == 1:
                item["testimonium"]["payload"]["content_health"]["truncated"] = True

    report = evaluate_page(
        reference_page=reference,
        source_page_ordinal=1,
        proposals=proposals,
        attachments=attachments,
        chairs=CHAIRS,
    )
    count = len(reference["acts"])
    for chair in CHAIRS:
        assert report["totals"][chair]["references"] == count
        assert report["totals"][chair]["cer_units"] > 0
    assert report["totals"]["attestator_1"]["statuses"]["unavailable"] == count
    assert report["totals"]["attestator_2"]["statuses"]["missing"] == count
    assert report["totals"]["attestator_3"]["statuses"]["truncated"] == count


def test_reference_text_cannot_change_the_geometry_assignment(sealed_run: RunTree):
    first = _fixture_reference_for_page_one(sealed_run)
    changed = build_reference_page(
        page=first["page"],
        source=first["source"],
        volume=first["volume"],
        designation=first["designation"],
        split=first["split"],
        records=[
            {
                "record_id": act["record_id"],
                "region": act["region"],
                "split": first["split"],
                "text": "changed synthetic reading",
                "text_sha256": digest_bytes(b"changed synthetic reading"),
            }
            for act in first["acts"]
        ],
    )
    read_only = ReadOnlyRunTree(sealed_run)
    proposals = [
        proposal
        for proposal in load_pipeline_proposal_acts(read_only)
        if proposal["page_sha256"] == first["page"]["sha256"]
    ]
    attachments = _attachment_index(read_only)
    original = evaluate_page(
        reference_page=first,
        source_page_ordinal=1,
        proposals=proposals,
        attachments=attachments,
        chairs=CHAIRS,
    )
    altered = evaluate_page(
        reference_page=changed,
        source_page_ordinal=1,
        proposals=proposals,
        attachments=attachments,
        chairs=CHAIRS,
    )

    def pairing(report):
        return [
            (row["pipeline_act_id"], row["reference_physical_act_id"])
            for row in report["geometry"]["matched_pairs"]
        ]

    assert pairing(original) == pairing(altered)


def test_duplicate_attachment_for_the_matched_source_page_is_refused(sealed_run: RunTree):
    reference = _fixture_reference_for_page_one(sealed_run)
    read_only = ReadOnlyRunTree(sealed_run)
    proposals = load_pipeline_proposal_acts(read_only)
    attachments = _attachment_index(read_only)
    act_id = proposals[0]["act_id"]
    duplicate = dict(attachments)
    duplicate[act_id] = dict(attachments[act_id])
    duplicate[act_id]["attestator_2"] = list(attachments[act_id]["attestator_2"]) * 2
    with pytest.raises(CorpusRefusal, match="^malformed-record: duplicate attachments"):
        evaluate_page(
            reference_page=reference,
            source_page_ordinal=1,
            proposals=[
                proposal
                for proposal in proposals
                if proposal["page_sha256"] == reference["page"]["sha256"]
            ],
            attachments=duplicate,
            chairs=CHAIRS,
        )


def test_duplicate_selected_page_and_output_inside_run_tree_are_refused(
    sealed_run: RunTree, tmp_path: Path
):
    reference = _fixture_reference_for_page_one(sealed_run)
    ledger_path, pages_path = _write_inputs(tmp_path, sealed_run, reference)
    with pytest.raises(CorpusRefusal, match="^malformed-record: duplicate selected page id"):
        evaluate_run(
            tree=sealed_run,
            ledger_path=ledger_path,
            reference_pages_path=pages_path,
            page_ids=[reference["designation"], reference["designation"]],
        )
    minimal_report = {"schema": "synthetic"}
    minimal_report["self_hash"] = self_hash(minimal_report)
    with pytest.raises(CorpusRefusal, match="^output-in-run-tree:"):
        write_report(minimal_report, sealed_run.root / "report.json", run_root=sealed_run.root)
    assert not (sealed_run.root / "report.json").exists()


def test_report_is_immutable_once_created(tmp_path: Path):
    output = tmp_path / "report.json"
    report = {"schema": "synthetic"}
    report["self_hash"] = self_hash(report)
    write_report(report, output, run_root=tmp_path / "run")
    before = output.read_bytes()
    with pytest.raises(CorpusRefusal, match="^output-exists:"):
        write_report(report, output, run_root=tmp_path / "run")
    assert output.read_bytes() == before
