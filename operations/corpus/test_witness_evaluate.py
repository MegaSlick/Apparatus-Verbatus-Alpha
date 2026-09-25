from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash, verify_self_hash
from common.contracts.stages import ATTESTATORES
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
    _reference_pages,
    _sealed_page_bindings,
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


def _page_records(tree: ReadOnlyRunTree) -> list[dict]:
    return [
        tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
    ]


def _relabel_page_two_pixels_as_page_one(records: list[dict]) -> tuple[str, dict]:
    by_chair: dict[str, dict[int, dict]] = {}
    for record in records:
        payload = record["payload"]
        by_chair.setdefault(payload["chair"], {})[payload["page_ordinal"]] = record
    chair_records = next(rows for rows in by_chair.values() if {1, 2} <= set(rows))
    forged = copy.deepcopy(chair_records[1])
    forged["payload"]["presented"] = copy.deepcopy(chair_records[2]["payload"]["presented"])
    # The retained image path/digest, page id and transform all come from page
    # two. Relabel both ordinal fields as page one, which is internally valid
    # to the Testimonium schema but false against the Exemplar page id.
    forged["payload"]["presented"]["source_page_ordinal"] = 1
    forged["payload"]["presented"]["transform"]["source_page_ordinal"] = 1
    return chair_records[1]["artifact_id"], forged


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


def test_page_health_joins_valid_transformed_presentations_to_exact_exemplar(
    sealed_run: RunTree,
):
    read_only = ReadOnlyRunTree(sealed_run)
    sealed_pages = _sealed_page_bindings(read_only)
    page_sha256_by_ordinal = load_exemplar_page_shas(read_only)
    records = [
        read_only.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        for entry in read_only.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
    ]
    assert any(
        record["payload"]["presented"]["image_sha256"]
        != sealed_pages[record["payload"]["presented"]["source_page_id"]].sha256
        for record in records
    )
    counts = page_health_counts(
        records,
        page_sha256_by_ordinal=page_sha256_by_ordinal,
        sealed_pages=sealed_pages,
        read_bytes=read_only.read_bytes,
        chairs=CHAIRS,
    )
    for chair in CHAIRS:
        assert counts[chair]["missing"] + counts[chair]["truncated_true"] + counts[chair][
            "truncated_false"
        ] + counts[chair]["truncated_null"] == len(page_sha256_by_ordinal)


def test_page_health_refuses_self_consistent_page_relabelled_to_another_ordinal(
    sealed_run: RunTree,
):
    read_only = ReadOnlyRunTree(sealed_run)
    sealed_pages = _sealed_page_bindings(read_only)
    _artifact_id, forged = _relabel_page_two_pixels_as_page_one(_page_records(read_only))

    with pytest.raises(CorpusRefusal, match="ordinal disagrees with the sealed page"):
        page_health_counts(
            [forged],
            page_sha256_by_ordinal={1: load_exemplar_page_shas(read_only)[1]},
            sealed_pages=sealed_pages,
            read_bytes=read_only.read_bytes,
            chairs=CHAIRS,
        )


def test_attachment_index_refuses_self_consistent_page_relabelled_to_another_ordinal(
    sealed_run: RunTree,
):
    read_only = ReadOnlyRunTree(sealed_run)
    artifact_id, forged = _relabel_page_two_pixels_as_page_one(_page_records(read_only))

    class ForgedReadTree:
        def read_artifact(self, stage, kind, candidate):  # type: ignore[no-untyped-def]
            record = read_only.read_artifact(stage, kind, candidate)
            return copy.deepcopy(forged) if candidate == artifact_id else record

        def read_artifact_reference(self, reference, **kwargs):  # type: ignore[no-untyped-def]
            record = read_only.read_artifact_reference(reference, **kwargs)
            return copy.deepcopy(forged) if record.get("artifact_id") == artifact_id else record

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(read_only, name)

    with pytest.raises(CorpusRefusal, match="ordinal disagrees with the sealed page"):
        _attachment_index(ForgedReadTree())  # type: ignore[arg-type]


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


def test_witness_report_retains_only_geometry_facts_from_placeholder_comparison(
    sealed_run: RunTree,
):
    reference = _fixture_reference_for_page_one(sealed_run)
    read_only = ReadOnlyRunTree(sealed_run)
    proposals = [
        proposal
        for proposal in load_pipeline_proposal_acts(read_only)
        if proposal["page_sha256"] == reference["page"]["sha256"]
    ]
    report = evaluate_page(
        reference_page=reference,
        source_page_ordinal=1,
        proposals=proposals,
        attachments=_attachment_index(read_only),
        chairs=CHAIRS,
    )

    expected = {
        "pipeline_act_id",
        "reference_physical_act_id",
        "record_id",
        "intersection_area",
        "union_area",
    }
    assert report["geometry"]["matched_pairs"]
    assert all(set(pair) == expected for pair in report["geometry"]["matched_pairs"])
    assert "normalization_profile_id" not in report["geometry"]
    assert "self_hash" not in report["geometry"]


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


def test_reference_pages_that_are_not_json_are_refused_as_a_malformed_record(tmp_path):
    path = tmp_path / "reference-pages.jsonl"
    path.write_text("{not json\n")
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        _reference_pages(path)
