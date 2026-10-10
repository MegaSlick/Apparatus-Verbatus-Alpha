from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash, verify_self_hash
from common.contracts.stages import ATTESTATORES
from common.runtree.store import RunTree
from conftest import dots_models_config, dots_serving_recipes, run_orchestrator
from operations.corpus import CorpusRefusal
from operations.corpus.compare import (
    ReadOnlyRunTree,
    load_exemplar_page_shas,
    load_pipeline_reading_acts,
)
from operations.corpus.normalization import MAX_TEXT_LENGTH
from operations.corpus.reference import build_reference_page
from operations.corpus.scoring import OutputStatus
from operations.corpus.test_evaluate import (
    _fixture_reference_for_page_one,
    _ledger_for,
    _orchestrate,
)
from operations.corpus.witness_evaluate import (
    CHAIRS,
    evaluate_feed_page,
    evaluate_page,
    evaluate_page_feed_run,
    evaluate_run,
    main,
    page_feeds,
    page_health_counts,
    page_rosters,
    page_witness_index,
    sealed_page_bindings,
    witness_reading,
    write_report,
)


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
    # A chair shown one image per page, so the forged `presented` is the whole
    # presentation rather than the first of several.
    chair_records = next(
        rows
        for rows in by_chair.values()
        if {1, 2} <= set(rows) and rows[1]["payload"].get("presentations") is None
    )
    forged = copy.deepcopy(chair_records[1])
    forged["payload"]["presented"] = copy.deepcopy(chair_records[2]["payload"]["presented"])
    # The retained image path/digest, page id and transform all come from page
    # two. Relabel both ordinal fields as page one, which is internally valid
    # to the Testimonium schema but false against the Exemplar page id.
    forged["payload"]["presented"]["source_page_ordinal"] = 1
    forged["payload"]["presented"]["transform"]["source_page_ordinal"] = 1
    return chair_records[1]["artifact_id"], forged


def test_a_completed_page_reading_is_scored_whole():
    status, text, reason = witness_reading(_testimonium("page text"))
    assert (status, text, reason) == (OutputStatus.COMPLETE, "page text", None)


def test_a_non_reading_page_testimonium_is_named_unavailable():
    record = {"outcome": "failed", "payload": {"payload": None, "content_health": {}}}
    assert witness_reading(record) == (OutputStatus.UNAVAILABLE, None, "non-reading-'failed'")


def test_a_truncated_page_reading_retains_its_partial_text():
    status, text, reason = witness_reading(_testimonium("abcd", truncated=True))
    assert (status, text, reason) == (OutputStatus.TRUNCATED, "abcd", None)


def test_unknown_completion_is_unavailable_instead_of_silently_complete():
    status, text, reason = witness_reading(_testimonium("test", truncated=None))
    assert (status, text, reason) == (
        OutputStatus.UNAVAILABLE,
        None,
        "unknown-truncation",
    )


def test_a_structured_page_reading_is_unavailable_rather_than_coerced_to_text():
    record = {
        "outcome": "read",
        "payload": {"payload": {"lines": ["a"]}, "content_health": _health(truncated=False)},
    }
    assert witness_reading(record) == (OutputStatus.UNAVAILABLE, None, "structured-payload")


def test_page_health_joins_valid_transformed_presentations_to_exact_exemplar(
    sealed_run: RunTree,
):
    read_only = ReadOnlyRunTree(sealed_run)
    sealed_pages = sealed_page_bindings(read_only)
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
    sealed_pages = sealed_page_bindings(read_only)
    _artifact_id, forged = _relabel_page_two_pixels_as_page_one(_page_records(read_only))

    with pytest.raises(CorpusRefusal, match="ordinal disagrees with the sealed page"):
        page_health_counts(
            [forged],
            page_sha256_by_ordinal={1: load_exemplar_page_shas(read_only)[1]},
            sealed_pages=sealed_pages,
            read_bytes=read_only.read_bytes,
            chairs=CHAIRS,
        )


def test_page_witness_index_refuses_self_consistent_page_relabelled_to_another_ordinal(
    sealed_run: RunTree,
):
    read_only = ReadOnlyRunTree(sealed_run)
    artifact_id, forged = _relabel_page_two_pixels_as_page_one(_page_records(read_only))

    class ForgedReadTree:
        def read_artifact(self, stage, kind, candidate):  # type: ignore[no-untyped-def]
            record = read_only.read_artifact(stage, kind, candidate)
            return copy.deepcopy(forged) if candidate == artifact_id else record

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(read_only, name)

    with pytest.raises(CorpusRefusal, match="ordinal disagrees with the sealed page"):
        page_witness_index(ForgedReadTree())  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def sealed_run(tmp_path_factory) -> RunTree:
    run_root = tmp_path_factory.mktemp("witness-runs")
    completed = _orchestrate(run_root, "page-unbroken")
    assert completed.returncode == 0, completed.stderr
    return RunTree(run_root, "r")


def _page_one_report(sealed_run: RunTree, reference: dict, witnesses=None) -> dict:
    read_only = ReadOnlyRunTree(sealed_run)
    proposals = [
        proposal
        for proposal in load_pipeline_reading_acts(read_only)
        if proposal["page_sha256"] == reference["page"]["sha256"]
    ]
    if witnesses is None:
        witnesses = page_witness_index(read_only)[1]
    return evaluate_page(
        reference_page=reference,
        source_page_ordinal=1,
        proposals=proposals,
        witnesses=witnesses,
        chairs=CHAIRS,
    )


def test_cli_scores_all_three_chairs_from_current_sealed_page_testimonia_without_mutating_run(
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
                "--basis",
                "page-testimonium",
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
    # Every chair read the whole page, DAI included, so each is scored on it.
    for chair in CHAIRS:
        total = report["totals"][chair]
        assert total["references"] == 1
        assert total["statuses"]["complete"] == 1
        assert total["cer_units"] > 0 and total["wer_units"] > 0
        assert report["page_health"][chair]["truncated_false"] == 1


def test_every_chair_is_scored_on_its_page_against_every_reference_act(sealed_run: RunTree):
    reference = _fixture_reference_for_page_one(sealed_run)
    report = _page_one_report(sealed_run, reference)
    expected = [act["record_id"] for act in reference["acts"]]
    assert {row["chair"]: row["status"] for row in report["rows"]} == dict.fromkeys(
        CHAIRS, "complete"
    )
    assert all(row["record_ids"] == expected for row in report["rows"])


def test_missing_proposal_is_counted_and_its_ink_stays_in_every_chair_s_reference(
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
    baseline = _page_one_report(sealed_run, _fixture_reference_for_page_one(sealed_run))
    assert report["reference_records"] == 3
    for chair in CHAIRS:
        assert report["totals"][chair]["missing_proposals"] == 1
        # No witness read the unmatched text, so it costs every chair deletions.
        assert report["totals"][chair]["cer_errors"] > baseline["totals"][chair]["cer_errors"]


def test_missing_unavailable_and_truncated_readings_all_remain_in_totals(
    sealed_run: RunTree,
):
    reference = _fixture_reference_for_page_one(sealed_run)
    witnesses = json.loads(json.dumps(page_witness_index(ReadOnlyRunTree(sealed_run))[1]))
    witnesses.pop("attestator_2")
    witnesses["attestator_1"]["outcome"] = "failed"
    witnesses["attestator_3"]["payload"]["content_health"]["truncated"] = True

    report = _page_one_report(sealed_run, reference, witnesses)
    for chair in CHAIRS:
        assert report["totals"][chair]["references"] == 1
        assert report["totals"][chair]["cer_units"] > 0
    assert report["totals"]["attestator_1"]["statuses"]["unavailable"] == 1
    assert report["totals"]["attestator_2"]["statuses"]["missing"] == 1
    assert report["totals"]["attestator_3"]["statuses"]["truncated"] == 1


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
    original = _page_one_report(sealed_run, first)
    altered = _page_one_report(sealed_run, changed)

    def pairing(report):
        return [
            (row["pipeline_act_id"], row["reference_physical_act_id"])
            for row in report["geometry"]["matched_pairs"]
        ]

    assert pairing(original) == pairing(altered)


def test_witness_report_retains_only_geometry_facts_from_placeholder_comparison(
    sealed_run: RunTree,
):
    report = _page_one_report(sealed_run, _fixture_reference_for_page_one(sealed_run))

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


# --- page path -------------------------------------------------------------------------


def _synthetic_reference() -> dict:
    texts = {"r1": "Le premier mai baptisé Jean", "r2": "Le deux mai inhumé Marie"}
    regions = {"r1": (0, 0, 100, 50), "r2": (0, 60, 100, 50)}
    return build_reference_page(
        page={"sha256": "a" * 64, "width": 200, "height": 200},
        source="synthetic",
        volume="v",
        designation="p1",
        split="val",
        records=[
            {
                "record_id": key,
                "region": dict(zip("xywh", regions[key], strict=True)),
                "split": "val",
                "text": text,
                "text_sha256": digest_bytes(text.encode("utf-8")),
            }
            for key, text in texts.items()
        ],
    )


def _unit(text: str, box: tuple[int, int, int, int] | None) -> dict:
    return {
        "id": "A1",
        "ordinal": 1,
        "box_px": None if box is None else dict(zip("xywh", box, strict=True)),
        "label": None,
        "text": text,
    }


def _feed(*witnesses: dict) -> dict:
    return {
        "page_id": "page-1",
        "page_ordinal": 1,
        "reading_unit": "page",
        "witness_testimony": "present",
        "witnesses": list(witnesses),
    }


def _witness(label: str, units: list[dict], *, outcome: str = "read", truncated=False) -> dict:
    return {
        "witness_label": label,
        "chair": None,
        "outcome": outcome,
        "answer_health": {"truncated": truncated, "repetition": []},
        "units": units,
    }


def _rows(report: dict) -> dict[tuple[str, str], dict]:
    return {(row["chair"], row["record_id"]): row for row in report["rows"]}


def test_a_page_witness_is_scored_from_the_units_lying_on_each_record():
    feed = _feed(
        _witness(
            "w-lines",
            [
                _unit("Le premier mai", (0, 0, 100, 20)),
                _unit("baptisé Jean", (0, 25, 100, 20)),
                _unit("Le deux mai inhumé Marie", (0, 60, 100, 50)),
                _unit("Table des baptêmes", (120, 0, 80, 20)),
            ],
        )
    )

    report = evaluate_feed_page(reference_page=_synthetic_reference(), feed=feed)

    rows = _rows(report)
    assert rows[("w-lines", "r1")]["status"] == "complete"
    assert rows[("w-lines", "r1")]["cer"] == 0  # two lines, joined, read the record
    assert rows[("w-lines", "r2")]["cer"] == 0
    assert report["units"]["w-lines"] == {
        "units": 4,
        "boxed": 4,
        "on_a_record": 3,
        "on_no_record": 1,
    }
    assert verify_self_hash(report)


def test_a_record_no_unit_lies_on_is_a_whole_deletion_never_dropped():
    feed = _feed(_witness("w", [_unit("Le premier mai baptisé Jean", (0, 0, 100, 50))]))

    report = evaluate_feed_page(reference_page=_synthetic_reference(), feed=feed)

    missed = _rows(report)[("w", "r2")]
    assert missed["status"] == "missing"
    assert missed["reason"] == "no-unit-on-record"
    assert missed["cer"] == missed["cer_units"] > 0
    assert report["totals"]["w"]["references"] == 2


def test_a_unit_beyond_the_scoring_bounds_is_named_unmeasured_and_the_rest_scored():
    def report_with_r1_text(text):
        feed = _feed(
            _witness(
                "w",
                [_unit(text, (0, 0, 100, 50)), _unit("Le deux mai inhumé Marie", (0, 60, 100, 50))],
            )
        )
        return evaluate_feed_page(reference_page=_synthetic_reference(), feed=feed)

    report = report_with_r1_text("a" * (MAX_TEXT_LENGTH + 1))

    runaway, read = _rows(report)[("w", "r1")], _rows(report)[("w", "r2")]
    assert runaway["unmeasured"] == "text-out-of-bounds" and runaway["cer"] is None
    assert read["unmeasured"] is None and read["cer"] == 0
    totals = report["totals"]["w"]
    assert (totals["references"], totals["unmeasured"]) == (2, 1)
    # Counted as wholly deleted: never a better total than reading nothing.
    assert totals == {**report_with_r1_text("")["totals"]["w"], "unmeasured": 1}


def test_a_page_whose_joined_reference_is_beyond_the_scoring_bounds_is_named_unmeasured():
    half = "a" * (MAX_TEXT_LENGTH // 2 + 1)
    reference = build_reference_page(
        page={"sha256": "a" * 64, "width": 200, "height": 200},
        source="synthetic",
        volume="v",
        designation="p1",
        split="val",
        records=[
            {
                "record_id": key,
                "region": {"x": 0, "y": y, "w": 100, "h": 50},
                "split": "val",
                "text": half,
                "text_sha256": digest_bytes(half.encode("utf-8")),
            }
            for key, y in (("r1", 0), ("r2", 60))
        ],
    )

    report = evaluate_page(
        reference_page=reference, source_page_ordinal=1, proposals=[], witnesses={}, chairs=CHAIRS
    )

    assert {row["unmeasured"] for row in report["rows"]} == {"reference-text-out-of-bounds"}
    # The units left out of the rate: each act's own, plus the space each line
    # break joining two acts normalizes to.
    for chair in CHAIRS:
        total = report["totals"][chair]
        assert total["unmeasured"] == 1
        assert (total["unmeasured_cer_units"], total["unmeasured_wer_units"]) == (
            2 * len(half) + 1,
            2,
        )
        assert (total["cer_units"], total["wer_units"]) == (0, 0)


def test_a_unit_straddling_two_records_belongs_to_the_one_holding_most_of_it():
    # 30 rows on r1 (0..50), 20 on r2 (60..110): r1 holds most of it.
    feed = _feed(_witness("w", [_unit("Le premier mai baptisé Jean", (0, 20, 100, 60))]))
    report = evaluate_feed_page(reference_page=_synthetic_reference(), feed=feed)
    assert _rows(report)[("w", "r1")]["status"] == "complete"
    assert _rows(report)[("w", "r2")]["status"] == "missing"

    # Most of it on neither record: r2 holds 50 of its 140 rows, so it lies on none.
    feed = _feed(_witness("w", [_unit("Le premier mai", (0, 40, 100, 140))]))
    report = evaluate_feed_page(reference_page=_synthetic_reference(), feed=feed)
    assert report["units"]["w"]["on_no_record"] == 1


def test_unread_unboxed_and_truncated_page_witnesses_stay_in_the_denominator():
    feed = _feed(
        _witness("a-unread", [], outcome="failed"),
        _witness("b-flat", [_unit("Le premier mai baptisé Jean", None)]),
        _witness(
            "c-cut",
            [_unit("Le premier mai baptisé Jean", (0, 0, 100, 50))],
            truncated=True,
        ),
        _witness(
            "d-unknown",
            [_unit("Le premier mai baptisé Jean", (0, 0, 100, 50))],
            truncated=None,
        ),
    )

    rows = _rows(evaluate_feed_page(reference_page=_synthetic_reference(), feed=feed))

    assert rows[("a-unread", "r1")]["reason"] == "witness-failed"
    assert rows[("b-flat", "r1")]["reason"] == "witness-units-unboxed"
    assert rows[("b-flat", "r1")]["status"] == "unavailable"
    assert rows[("c-cut", "r1")]["status"] == "truncated"
    assert rows[("d-unknown", "r1")]["status"] == "complete"
    assert rows[("d-unknown", "r1")]["reason"] == "truncation-unknown"
    assert len(rows) == 8


def test_cli_scores_the_page_feed_witnesses_of_a_page_read_run(sealed_run: RunTree, tmp_path: Path):
    reference = _fixture_reference_for_page_one(sealed_run)
    ledger_path, pages_path = _write_inputs(tmp_path, sealed_run, reference)
    output = tmp_path / "external" / "witness-page-report.json"
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
                "--output",
                str(output),
            ]
        )
        == 0
    )

    report = json.loads(output.read_bytes())
    assert verify_self_hash(report)
    assert before == _inventory(sealed_run)
    assert report["schema"] == "recordgold-witness-evaluation.page.v2"
    assert report["basis"] == "page-feed"
    assert report["reference_pages_outside_run"] == []
    assert report["reference_records"] == len(reference["acts"])
    [page] = report["pages"]
    assert page["source_page_ordinal"] == 1
    assert report["witnesses"]
    for name in report["witnesses"]:
        assert report["totals"][name]["references"] == len(reference["acts"])
    # Deterministic: the same inputs give the same bytes.
    again = evaluate_page_feed_run(
        tree=sealed_run, ledger_path=ledger_path, reference_pages_path=pages_path
    )
    assert canonical_bytes(again) == output.read_bytes()


def test_a_ledger_page_the_run_did_not_seal_is_counted_not_scored(
    sealed_run: RunTree, tmp_path: Path
):
    reference = _fixture_reference_for_page_one(sealed_run)
    elsewhere = build_reference_page(
        page={"sha256": "b" * 64, "width": reference["page"]["width"], "height": 900},
        source="fixture",
        volume="synthetic-two-page-v0",
        designation="page-elsewhere",
        split="val",
        records=[
            {
                "record_id": "elsewhere-1",
                "region": {"x": 0, "y": 0, "w": 10, "h": 10},
                "split": "val",
                "text": "ailleurs",
                "text_sha256": digest_bytes(b"ailleurs"),
            }
        ],
    )
    ledger_path = tmp_path / "ledger.json"
    pages_path = tmp_path / "reference-pages.jsonl"
    ledger_path.write_bytes(canonical_bytes(_ledger_for(reference, elsewhere)))
    # The proof subset's reference pages beside the whole set's ledger.
    pages_path.write_bytes(canonical_bytes(reference) + b"\n")

    report = evaluate_page_feed_run(
        tree=sealed_run, ledger_path=ledger_path, reference_pages_path=pages_path
    )

    assert report["reference_pages_outside_run"] == ["page-elsewhere"]
    assert report["reference_records"] == len(reference["acts"])
    with pytest.raises(CorpusRefusal, match="^reference-page-not-in-run:"):
        evaluate_page_feed_run(
            tree=sealed_run,
            ledger_path=ledger_path,
            reference_pages_path=pages_path,
            page_ids=["page-elsewhere"],
        )


@pytest.fixture(scope="module")
def routed_run(tmp_path_factory) -> RunTree:
    """dots.mocr (`attestator_4`) routed to page 2, a table page; page 1 is an act page."""
    base = tmp_path_factory.mktemp("routed-runs")
    completed = run_orchestrator(
        base / "runs",
        "r",
        "dots-table",
        models_config=dots_models_config(base / "models"),
        serving_recipes_config=dots_serving_recipes(base),
    )
    # The fixture's pages carry an act across their break, so the run is partial.
    assert completed.returncode == 3, completed.stderr
    return RunTree(base / "runs", "r")


def test_a_routed_witness_is_scored_only_on_the_pages_routed_to_it(
    routed_run: RunTree, tmp_path: Path
):
    read_only = ReadOnlyRunTree(routed_run)
    base = ["attestator_1", "attestator_2", "attestator_3"]
    assert page_rosters(read_only, page_feeds(read_only)) == {
        1: base,
        2: [*base, "attestator_4"],
    }
    reference = _fixture_reference_for_page_one(routed_run)
    ledger_path, pages_path = _write_inputs(tmp_path, routed_run, reference)

    report = evaluate_page_feed_run(
        tree=routed_run, ledger_path=ledger_path, reference_pages_path=pages_path
    )

    assert report["witnesses"] == base
    assert not any(row["reason"] == "witness-not-in-feed" for row in report["pages"][0]["rows"])


def test_a_witness_the_feed_does_not_show_is_charged_on_that_page():
    feed = _feed(_witness("w", [_unit("Le premier mai baptisé Jean", (0, 0, 100, 50))]))
    feed["witnesses"] = []
    feed["witness_testimony"] = "none"

    report = evaluate_feed_page(reference_page=_synthetic_reference(), feed=feed, roster=["w"])

    rows = _rows(report)
    assert {row["reason"] for row in rows.values()} == {"witness-not-in-feed"}
    assert report["totals"]["w"]["references"] == 2
    assert report["units"]["w"]["units"] == 0
