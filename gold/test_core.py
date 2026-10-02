"""Attack the gold-record custody boundaries through real JSON records."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import IncompatibleReuse, SchemaRefusal
from common.contracts.identities import act_id, page_id
from conftest import tree_snapshot
from gold import cli
from gold.core import (
    ILLEGIBLE,
    LAYOUT_SCHEMA,
    MANUAL_PICK_SCHEMA,
    PADDING_SCHEMA,
    SAMPLE_SCHEMA,
    adjudicate,
    bind_instrument,
    build_sample,
    build_sampling_draw,
    ingest_manual_pick,
    load_run_frame,
    read_json,
    read_transcription_text,
    sample_stratified,
    set_for_page,
    transcribe,
    validate_adjudication,
    validate_corpus,
    validate_layout,
    validate_measurement,
    validate_padding,
    validate_record,
    validate_sample,
    validate_sampling_draw,
    verify_stratified_selection,
    write_append_only,
)


def _sha(character: str) -> str:
    return character * 64


def run_file(tmp_path):
    pages = [
        {"ordinal": ordinal, "sha256": _sha(character), "width": 100, "height": 200}
        for ordinal, character in enumerate("12345678", 1)
    ]
    source = [{"ordinal": page["ordinal"], "sha256": page["sha256"]} for page in pages]
    page_digest = digest_bytes(canonical_bytes(source))
    frame = {
        "page_digest": page_digest,
        "frame_digest": digest_bytes(canonical_bytes({"pages": source})),
        "seed": digest_bytes(canonical_bytes({"page_digest": page_digest, "purpose": "frame"})),
    }
    path = tmp_path / "run.json"
    record = {
        "schema": "skeleton.v1",
        "run_id": "gold-fixture",
        "source_manifest": pages,
        "corpus_frame_membership": frame,
    }
    record["self_hash"] = self_hash(record)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path, frame, pages


def catalog(pages):
    return [{**page, "stratum": "adverse" if page["ordinal"] % 2 else "ordinary"} for page in pages]


def plan_for(frame, rows):
    """A plan naming every stratum in both sets: 1 where the partition can fill it,
    0 (a declared skip) where it cannot."""
    result = {"calibration": {}, "locked-acceptance": {}}
    for gold_set in result:
        for stratum in {row["stratum"] for row in rows}:
            result[gold_set][stratum] = int(
                any(
                    row["stratum"] == stratum and set_for_page(row["sha256"]) == gold_set
                    for row in rows
                )
            )
    return result


def test_seeded_stratification_is_reproducible_and_sets_are_disjoint_by_construction(tmp_path):
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    first = sample_stratified(path, rows, plan_for(frame, rows))
    second = sample_stratified(path, rows, plan_for(frame, rows))
    assert first == second
    by_page = {record["page"]["sha256"]: record["set"] for record in first}
    assert len(by_page) == len(first)
    # Derived independently of `set_for_page`, so the test still argues if the
    # implementation and its own oracle drift together.
    for record in first:
        rank = digest_bytes(
            canonical_bytes({"page_sha256": record["page"]["sha256"], "purpose": "gold-set-v1"})
        )
        expected = "calibration" if int(rank[0], 16) < 8 else "locked-acceptance"
        assert record["set"] == expected == set_for_page(record["page"]["sha256"])
    forged = dict(first[0])
    forged["set"] = "locked-acceptance" if forged["set"] == "calibration" else "calibration"
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="page-derived partition"):
        validate_sample(forged, path)


def test_same_page_bytes_at_two_ordinals_are_ranked_as_distinct_pages(tmp_path):
    """A repeated byte digest is two scanned pages when its ordinals differ.

    The sampler selects both, and corpus validation preserves the same identity
    instead of collapsing it back to byte content alone.
    """
    pages = [
        {"ordinal": 1, "sha256": _sha("a"), "width": 100, "height": 200},
        {"ordinal": 2, "sha256": _sha("a"), "width": 100, "height": 200},
    ]
    source = [{"ordinal": page["ordinal"], "sha256": page["sha256"]} for page in pages]
    page_digest = digest_bytes(canonical_bytes(source))
    frame = {
        "page_digest": page_digest,
        "frame_digest": digest_bytes(canonical_bytes({"pages": source})),
        "seed": digest_bytes(canonical_bytes({"page_digest": page_digest, "purpose": "frame"})),
    }
    path = tmp_path / "duplicate-bytes-run.json"
    authority = {
        "schema": "skeleton.v1",
        "run_id": "gold-duplicate-bytes",
        "source_manifest": pages,
        "corpus_frame_membership": frame,
    }
    authority["self_hash"] = self_hash(authority)
    path.write_text(json.dumps(authority), encoding="utf-8")
    rows = [{**page, "stratum": "duplicate-scan"} for page in pages]
    gold_set = set_for_page(pages[0]["sha256"])
    plan = {
        name: {"duplicate-scan": 2 if name == gold_set else 0}
        for name in ("calibration", "locked-acceptance")
    }

    selected = sample_stratified(path, rows, plan)

    assert {sample["page"]["ordinal"] for sample in selected} == {1, 2}
    assert validate_corpus(selected, path) == selected

    # Page identity binds the source ordinal as well as the source itself, so the
    # visually identical pages also derive distinct acts. Act-level custody must
    # admit one established reading for each rather than collapsing by page bytes.
    source_sha = _sha("f")
    by_ordinal = {sample["page"]["ordinal"]: sample for sample in selected}
    custody = []
    acts = []
    for ordinal in (1, 2):
        act = act_id(
            page_id(
                {
                    "kind": "container-page",
                    "container_sha256": source_sha,
                    "container_page_index": ordinal,
                    "render_contract": {"renderer": "gold-test"},
                },
                {"operation": "whole"},
            ),
            "proposal",
            {"x": 1, "y": 0, "w": 10, "h": 10},
        )
        acts.append(act)
        first = transcribe(by_ordinal[ordinal], act, "hand-a", f"reading {ordinal}", path)
        second = transcribe(by_ordinal[ordinal], act, "hand-b", f"reading {ordinal}", path)
        custody.extend([first, second, adjudicate(first, second)])
    assert acts[0] != acts[1]
    assert validate_corpus([*selected, *custody], path)

    # Reusing the first page's act identity on the other ordinal contradicts the
    # identity's page binding even though the two page digests are equal.
    misplaced = bind_instrument(by_ordinal[2], acts[0], _sha("e"), path)
    with pytest.raises(SchemaRefusal, match="contradictory custody.*Verify the act"):
        validate_corpus([*selected, *custody, misplaced], path)


@pytest.mark.parametrize("ordinal", [0, -1])
def test_load_run_frame_refuses_a_non_positive_source_page_ordinal(tmp_path, ordinal):
    """A self-hashed run.json can assert any ordinal; a page is counted from one,
    so load_run_frame refuses a value below it as naming no page."""
    pages = [{"ordinal": ordinal, "sha256": _sha("a"), "width": 100, "height": 200}]
    source = [{"ordinal": page["ordinal"], "sha256": page["sha256"]} for page in pages]
    page_digest = digest_bytes(canonical_bytes(source))
    frame = {
        "page_digest": page_digest,
        "frame_digest": digest_bytes(canonical_bytes({"pages": source})),
        "seed": digest_bytes(canonical_bytes({"page_digest": page_digest, "purpose": "frame"})),
    }
    record = {
        "schema": "skeleton.v1",
        "run_id": "gold-non-positive-ordinal",
        "source_manifest": pages,
        "corpus_frame_membership": frame,
    }
    record["self_hash"] = self_hash(record)
    path = tmp_path / "run.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(SchemaRefusal, match="ordinal.*counted from one"):
        load_run_frame(path)


def test_sample_refuses_a_page_or_frame_restated_differently_than_r0_authority(tmp_path):
    path, frame, pages = run_file(tmp_path)
    record = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    with pytest.raises(SchemaRefusal, match="outside the run authority"):
        validate_sample(_forge_sample_outside_authority(record), path)
    broken_run = json.loads(path.read_text())
    broken_run["corpus_frame_membership"]["page_digest"] = _sha("0")
    broken_run["self_hash"] = self_hash(broken_run)
    path.write_text(json.dumps(broken_run), encoding="utf-8")
    with pytest.raises(SchemaRefusal, match="diverges"):
        validate_sample(record, path)


def test_manual_pick_is_ingested_without_reselection_and_records_claimed_set(tmp_path):
    path, frame, pages = run_file(tmp_path)
    page = catalog(pages)[0]
    pick = {
        "schema": MANUAL_PICK_SCHEMA,
        "selection_basis": "B1 parish/condition stratification",
        "page": page,
        "set": set_for_page(page["sha256"]),
    }
    result = ingest_manual_pick(path, pick)
    assert result["method"] == "manual"
    assert result["page"] == page
    assert result["claimed_set"] == result["set"] == pick["set"]


def test_manual_pick_predating_the_seed_is_still_ingested_with_an_honest_disagreement(tmp_path):
    """A manual pick may predate the corpus frame and its seed, so the
    stated set can honestly disagree with the page-derived partition once it is
    known. Ingestion must not refuse and force a re-pick (that would discard real
    annotation hours); it must record the disagreement, never silently resolve it
    either way."""
    path, frame, pages = run_file(tmp_path)
    page = catalog(pages)[0]
    true_set = set_for_page(page["sha256"])
    claimed_set = "locked-acceptance" if true_set == "calibration" else "calibration"
    pick = {
        "schema": MANUAL_PICK_SCHEMA,
        "selection_basis": "B1 pick recorded before R0 froze",
        "page": page,
        "set": claimed_set,
    }
    result = ingest_manual_pick(path, pick)
    assert result["set"] == true_set
    assert result["claimed_set"] == claimed_set
    assert result["claimed_set"] != result["set"]
    assert validate_sample(result, path) == result


def test_cli_manual_ingest_refuses_one_page_in_two_strata(tmp_path):
    path, frame, pages = run_file(tmp_path)
    page = catalog(pages)[0]
    records = tmp_path / "manual-records"
    picks = []
    for name, stratum in (("first", page["stratum"]), ("second", "second-stratum")):
        pick_path = tmp_path / f"{name}-pick.json"
        pick_path.write_text(
            json.dumps(
                {
                    "schema": MANUAL_PICK_SCHEMA,
                    "selection_basis": name,
                    "page": {**page, "stratum": stratum},
                    "set": set_for_page(page["sha256"]),
                }
            ),
            encoding="utf-8",
        )
        picks.append(pick_path)

    assert (
        cli.main(
            [
                "ingest-manual",
                "--run",
                str(path),
                "--pick",
                str(picks[0]),
                "--output",
                str(records / "first.json"),
            ]
        )
        == 0
    )
    with pytest.raises(SchemaRefusal, match="stratified as"):
        cli.main(
            [
                "ingest-manual",
                "--run",
                str(path),
                "--pick",
                str(picks[1]),
                "--output",
                str(records / "second.json"),
            ]
        )
    assert [item.name for item in records.glob("*.json")] == ["first.json"]


def test_a_plan_that_leaves_a_stratum_unnamed_is_refused(tmp_path):
    """A stratum the plan does not name contributes no gold and says nothing about
    it. Naming it with quota 0 is the declared way to skip it."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    full = plan_for(frame, rows)
    partial = {gold_set: dict(quotas) for gold_set, quotas in full.items()}
    partial["calibration"].pop("ordinary")
    with pytest.raises(SchemaRefusal, match="deliberately left unsampled"):
        sample_stratified(path, rows, partial)
    declared_skip = {gold_set: dict(quotas) for gold_set, quotas in full.items()}
    declared_skip["calibration"]["ordinary"] = 0
    kept = sample_stratified(path, rows, declared_skip)
    assert not [
        record
        for record in kept
        if record["set"] == "calibration" and record["page"]["stratum"] == "ordinary"
    ]
    assert len(kept) == sum(sum(quotas.values()) for quotas in declared_skip.values())


def test_sealed_canaries_have_a_named_zero_quota_and_cannot_be_relabelled(tmp_path):
    path, frame, pages = run_file(tmp_path)
    authority = json.loads(path.read_text())
    authority["sealed_config_digests"] = {"canary-ledger": "c" * 64}
    authority["source_manifest"][-1]["ledger_sha256"] = "c" * 64
    authority["self_hash"] = self_hash(authority)
    path.write_text(json.dumps(authority), encoding="utf-8")
    rows = catalog(pages)
    rows[-1]["stratum"] = "canary"
    plan = plan_for(frame, rows)
    for quotas in plan.values():
        quotas["canary"] = 0
    assert all(record["page"]["ordinal"] != 8 for record in sample_stratified(path, rows, plan))
    bad = {name: dict(quotas) for name, quotas in plan.items()}
    bad["calibration"]["canary"] = 1
    with pytest.raises(SchemaRefusal, match="canary stratum quota must be zero"):
        sample_stratified(path, rows, bad)
    rows[-1]["stratum"] = "ordinary"
    with pytest.raises(SchemaRefusal, match="sealed canary ledger mark"):
        sample_stratified(path, rows, plan)


def test_manual_ingest_and_sample_validation_refuse_a_sealed_canary(tmp_path):
    path, frame, pages = run_file(tmp_path)
    authority = json.loads(path.read_text())
    authority["sealed_config_digests"] = {"canary-ledger": "c" * 64}
    authority["source_manifest"][-1]["ledger_sha256"] = "c" * 64
    authority["self_hash"] = self_hash(authority)
    path.write_text(json.dumps(authority), encoding="utf-8")
    page = {**pages[-1], "stratum": "canary"}
    pick = {
        "schema": MANUAL_PICK_SCHEMA,
        "selection_basis": "synthetic pick",
        "page": page,
        "set": "calibration",
    }
    with pytest.raises(SchemaRefusal, match="manual pick names a canary page"):
        ingest_manual_pick(path, pick)
    sample = build_sample(
        frame,
        page,
        selection_basis="synthetic pick",
        method="manual",
        claimed_set="calibration",
    )
    with pytest.raises(SchemaRefusal, match="sample names a canary page"):
        validate_sample(sample, path)


def test_stratified_samples_carry_no_claimed_set(tmp_path):
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    record = sample_stratified(path, rows, plan_for(frame, rows))[0]
    assert record["claimed_set"] is None


def test_a_tampered_sampling_seed_is_refused_as_an_edited_run_authority(tmp_path):
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    sample_stratified(path, rows, plan_for(frame, rows))
    edited = json.loads(path.read_text())
    edited["corpus_frame_membership"]["seed"] = _sha("0")
    path.write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(SchemaRefusal, match="fails its self-hash"):
        sample_stratified(path, rows, plan_for(frame, rows))
    # Reseal the edited authority so the self-hash passes: only the seed's own
    # derivation over the run's pages can refuse it now.
    edited["self_hash"] = self_hash(edited)
    path.write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(SchemaRefusal, match="seed diverges from its derivation"):
        sample_stratified(path, rows, plan_for(frame, rows))


def test_sampling_draw_refuses_unhashable_catalog_identity_fields_by_name(tmp_path):
    """The retained catalog is untrusted record input. Its identity fields must be
    checked before they become tuple members in a set; JSON arrays and objects are
    legal values but unhashable Python objects."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    draw, _selected = build_sampling_draw(path, rows, plan_for(frame, rows))
    for field, value, message in (
        ("ordinal", [], "sampling draw catalog page ordinal is not an integer"),
        ("sha256", {}, "sampling draw catalog page sha256 is not a lowercase sha256"),
    ):
        forged = json.loads(json.dumps(draw))
        forged["catalog"][0][field] = value
        forged["self_hash"] = self_hash(forged)
        with pytest.raises(SchemaRefusal, match=message):
            validate_sampling_draw(forged)


def test_a_directory_whose_draw_record_vanished_refuses_without_the_full_replay(tmp_path):
    """The draw is published first, so interruption leaves a draw short of its
    samples -- refused by membership divergence. The converse state (samples
    without a draw) now only arises by deletion, and bare verify-sampling
    refuses it by name; the legacy --catalog/--plan path is deliberately still
    open because it REPLAYS the whole selection, which is a full
    re-verification, not a silent accept."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    records = tmp_path / "records"
    (tmp_path / "catalog.json").write_text(json.dumps(rows), encoding="utf-8")
    (tmp_path / "plan.json").write_text(json.dumps(plan_for(frame, rows)), encoding="utf-8")
    assert (
        cli.main(
            [
                "sample",
                "--run",
                str(path),
                "--catalog",
                str(tmp_path / "catalog.json"),
                "--plan",
                str(tmp_path / "plan.json"),
                "--output-dir",
                str(records),
            ]
        )
        == 0
    )
    draw_files = list(records.glob("draw-*.json"))
    assert len(draw_files) == 1
    draw_files[0].unlink()

    with pytest.raises(SchemaRefusal, match="no recorded sampling draw exists"):
        cli.main(["verify-sampling", str(records), "--run", str(path)])


def test_a_method_cannot_carry_another_methods_provenance(tmp_path):
    """A sample claims one origin and must carry that origin's evidence: a seeded
    draw has no human claim and names its catalog and plan; a manual pick states a
    set and names neither."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    page = rows[0]
    sampling = {"catalog_digest": _sha("a"), "plan_digest": _sha("b")}
    with pytest.raises(SchemaRefusal, match="must name the catalog and plan"):
        build_sample(frame, page, selection_basis="basis", method="stratified-seed")
    with pytest.raises(SchemaRefusal, match="no human claimed_set"):
        build_sample(
            frame,
            page,
            selection_basis="basis",
            method="stratified-seed",
            claimed_set="calibration",
            sampling=sampling,
        )
    with pytest.raises(SchemaRefusal, match="must record the set its picker stated"):
        build_sample(frame, page, selection_basis="basis", method="manual")
    with pytest.raises(SchemaRefusal, match="not drawn from a catalog and plan"):
        build_sample(
            frame,
            page,
            selection_basis="basis",
            method="manual",
            claimed_set="calibration",
            sampling=sampling,
        )
    record = sample_stratified(path, rows, plan_for(frame, rows))[0]
    forged = json.loads(json.dumps(record))
    forged["claimed_set"] = "calibration"
    without = {
        key: value for key, value in forged.items() if key not in {"sample_digest", "self_hash"}
    }
    forged["sample_digest"] = digest_bytes(canonical_bytes(without))
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="no human claimed_set"):
        validate_sample(forged, path)


def test_a_selection_that_the_draw_did_not_produce_is_refused_on_replay(tmp_path):
    """Every individual record below validates: right frame, right corpus page,
    set matching the page-derived partition, self-hash intact. Only replaying the
    draw from the bound catalog and plan shows that a page was swapped for one the
    seed did not choose."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    plan = plan_for(frame, rows)
    drawn = sample_stratified(path, rows, plan)
    assert verify_stratified_selection(drawn, path, rows, plan) == sorted(
        drawn, key=lambda record: record["sample_digest"]
    )
    chosen = {record["page"]["sha256"] for record in drawn}
    substitute = next(row for row in rows if row["sha256"] not in chosen)
    hand_picked = build_sample(
        frame,
        substitute,
        selection_basis="seeded-stratified-v1",
        method="stratified-seed",
        sampling=drawn[0]["sampling"],
    )
    validate_sample(hand_picked, path)  # indistinguishable one record at a time
    swapped = [
        record
        for record in drawn
        if record["set"] != hand_picked["set"]
        or record["page"]["stratum"] != hand_picked["page"]["stratum"]
    ] + [hand_picked]
    with pytest.raises(SchemaRefusal, match="does not replay"):
        verify_stratified_selection(swapped, path, rows, plan)
    with pytest.raises(SchemaRefusal, match="does not replay"):
        verify_stratified_selection(drawn[1:], path, rows, plan)
    with pytest.raises(SchemaRefusal, match="appears twice"):
        verify_stratified_selection([*drawn, drawn[0]], path, rows, plan)


def test_a_sample_names_the_catalog_and_plan_it_was_drawn_from(tmp_path):
    """The binding is what makes the replay meaningful: re-describing the catalog
    (here, restratifying one page) changes the digest the records carry, so records
    and stratification cannot be silently mismatched afterwards."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    plan = plan_for(frame, rows)
    drawn = sample_stratified(path, rows, plan)
    assert {record["sampling"]["catalog_digest"] for record in drawn} == {
        digest_bytes(canonical_bytes(sorted(rows, key=lambda r: (r["stratum"], r["ordinal"]))))
    }
    reordered = sample_stratified(path, list(reversed(rows)), plan)
    assert [record["sampling"] for record in reordered] == [record["sampling"] for record in drawn]
    restratified = [dict(row) for row in rows]
    restratified[0]["stratum"] = "ordinary"
    changed = sample_stratified(path, restratified, plan_for(frame, restratified))
    assert changed[0]["sampling"]["catalog_digest"] != drawn[0]["sampling"]["catalog_digest"]


def test_recorded_draw_recomputes_independently_from_seed_membership_and_plan(tmp_path):
    """Replay without calling gold's sampler or its set/rank helper.

    The retained catalog is the complete frame membership plus human strata; the
    retained plan supplies the predeclared quota. Direct SHA-256 ranking over those
    bytes must select exactly the recorded member pages.
    """
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    plan = plan_for(frame, rows)
    draw, selected = build_sampling_draw(path, rows, plan)

    independently_selected = []
    for gold_set in sorted(plan):
        for stratum, quota in sorted(plan[gold_set].items()):
            eligible = []
            for page in rows:
                page_partition = digest_bytes(
                    canonical_bytes({"page_sha256": page["sha256"], "purpose": "gold-set-v1"})
                )
                independently_derived_set = (
                    "calibration" if int(page_partition[0], 16) < 8 else "locked-acceptance"
                )
                if page["stratum"] != stratum or independently_derived_set != gold_set:
                    continue
                rank = digest_bytes(
                    canonical_bytes(
                        {
                            "seed": draw["frame"]["seed"],
                            "ordinal": page["ordinal"],
                            "page_sha256": page["sha256"],
                            "stratum": stratum,
                            "purpose": "gold-sample",
                        }
                    )
                )
                eligible.append((rank, page["sha256"]))
            independently_selected.extend(page_sha for _rank, page_sha in sorted(eligible)[:quota])

    assert sorted(independently_selected) == sorted(sample["page"]["sha256"] for sample in selected)
    assert validate_sampling_draw(draw, path) == draw

    forged = json.loads(json.dumps(draw))
    forged["members"] = forged["members"][:-1]
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="membership diverges"):
        validate_sampling_draw(forged, path)

    with_count = {**draw, "member_count": len(draw["members"])}
    with_count["self_hash"] = self_hash(with_count)
    with pytest.raises(SchemaRefusal, match="wrong closed schema"):
        validate_sampling_draw(with_count, path)


def test_layout_padding_and_instrument_records_are_closed_and_self_hashed(tmp_path):
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    base = {"sample": sample}
    layout = {
        "schema": LAYOUT_SCHEMA,
        **base,
        "regions": [
            {"kind": "act", "rect": {"x": 1, "y": 2, "w": 3, "h": 4}},
            {"kind": "true-blank", "rect": {"x": 5, "y": 6, "w": 7, "h": 8}},
        ],
    }
    layout["self_hash"] = self_hash(layout)
    assert validate_layout(layout) == layout
    padding = {
        "schema": PADDING_SCHEMA,
        **base,
        "rectangles": [{"x": 0, "y": 0, "w": 10, "h": 10}],
        "calibrated_for_this_corpus": False,
    }
    padding["self_hash"] = self_hash(padding)
    assert validate_padding(padding) == padding
    measurement = bind_instrument(sample, _act(), _sha("e"))
    assert validate_measurement(measurement) == measurement
    layout["regions"][0]["kind"] = "free-text-label"
    layout["self_hash"] = self_hash(layout)
    with pytest.raises(SchemaRefusal, match="recognized"):
        validate_layout(layout)
    layout["regions"][0] = {"kind": "act", "rect": {"x": 99, "y": 1, "w": 2, "h": 1}}
    layout["self_hash"] = self_hash(layout)
    with pytest.raises(SchemaRefusal, match="Regenerate an unpublished annotation.*preserve"):
        validate_layout(layout)
    padding["rectangles"] = [{"x": 1, "y": 199, "w": 1, "h": 2}]
    padding["self_hash"] = self_hash(padding)
    with pytest.raises(SchemaRefusal, match="Regenerate an unpublished annotation.*preserve"):
        validate_padding(padding)


def test_page_dimension_refusals_name_the_missing_fact_and_remedy(tmp_path):
    """Dimensions newly make rectangle bounds meaningful, so every input route
    that supplies them must tell the operator both what is absent and how to fix it."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    missing = [dict(row) for row in rows]
    missing[0].pop("width")
    with pytest.raises(SchemaRefusal, match="width.*height.*Add the missing fields"):
        sample_stratified(path, missing, plan_for(frame, rows))

    page = rows[0]
    pick = {
        "schema": MANUAL_PICK_SCHEMA,
        "selection_basis": "basis",
        "page": {key: value for key, value in page.items() if key != "height"},
        "set": set_for_page(page["sha256"]),
    }
    with pytest.raises(SchemaRefusal, match="width.*height.*Add the missing fields"):
        ingest_manual_pick(path, pick)

    sample = sample_stratified(path, rows, plan_for(frame, rows))[0]
    sample["page"].pop("height")
    with pytest.raises(SchemaRefusal, match="width.*height.*Regenerate.*preserve"):
        validate_sample(sample)


def test_a_record_under_another_schema_version_is_refused(tmp_path):
    """A self-hashed record is read only under the schema it names."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    legacy = json.loads(json.dumps(sample))
    legacy["schema"] = "gold-page-sample.v1"
    legacy["sample_digest"] = digest_bytes(
        canonical_bytes(
            {
                key: value
                for key, value in legacy.items()
                if key not in {"sample_digest", "self_hash"}
            }
        )
    )
    legacy["self_hash"] = self_hash(legacy)
    with pytest.raises(SchemaRefusal, match="sample schema is not recognized"):
        validate_sample(legacy)
    with pytest.raises(SchemaRefusal, match="'gold-page-sample.v1' is not a gold record schema"):
        validate_record(legacy)


def _act(index=0):
    return act_id(
        "pg_0123456789abcdef",
        "proposal",
        {"x": index, "y": 2, "w": 3, "h": 4},
    )


def _pair(sample, first_text, second_text, path=None):
    return (
        transcribe(sample, _act(), "hand-a", first_text, path),
        transcribe(sample, _act(), "hand-b", second_text, path),
    )


def test_two_agreeing_transcribers_need_no_adjudicator(tmp_path):
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    first, second = _pair(sample, "L'an mil sept cent quatre", "L'an mil sept cent quatre", path)
    record = adjudicate(first, second)
    assert record["outcome"] == "agreed"
    assert record["adjudicator"] is None
    assert record["text"] == "L'an mil sept cent quatre"
    assert record["transcriptions"] == [first, second]
    assert validate_adjudication(record) == record
    # Argument order is not a fact about the act: the record is the same either way.
    assert adjudicate(second, first) == record
    with pytest.raises(SchemaRefusal, match="nothing for an adjudicator"):
        adjudicate(first, second, adjudicator="hand-c", text="something else")


def test_a_disagreement_records_the_adjudicators_own_reading_and_keeps_both(tmp_path):
    """Reconciling two readings is not picking between them: the
    adjudicator reads the ink and records what they read, which need not be either
    transcription, and both transcriptions are retained unaltered."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    first, second = _pair(sample, "alpha beta gamma", "alpha beta gamna", path)
    with pytest.raises(SchemaRefusal, match="reading they established from the ink"):
        adjudicate(first, second)
    record = adjudicate(first, second, adjudicator="hand-c", text="alpha beta gamná")
    assert record["outcome"] == "adjudicated"
    assert record["text"] not in {first["text"], second["text"]}
    assert record["transcriptions"] == [first, second]
    assert validate_adjudication(record) == record
    with pytest.raises(SchemaRefusal, match="cannot be its own reconciliation"):
        adjudicate(first, second, adjudicator="hand-a", text="alpha beta gamma")


def test_an_adjudication_cannot_assert_an_outcome_its_transcriptions_deny(tmp_path):
    """`outcome` is derived from the two readings, never taken on trust — the same
    discipline `set` gets. Resealing the self-hash launders neither."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    first, second = _pair(sample, "delta epsilon", "delta epsilon", path)
    agreed = adjudicate(first, second)
    forged = json.loads(json.dumps(agreed))
    forged["outcome"] = "adjudicated"
    forged["adjudicator"] = "hand-c"
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="the outcome is 'agreed'"):
        validate_adjudication(forged)
    differing = adjudicate(
        *_pair(sample, "delta epsilon", "delta epsilom", path),
        adjudicator="hand-c",
        text="delta epsilon",
    )
    forged = json.loads(json.dumps(differing))
    forged["outcome"] = "agreed"
    forged["adjudicator"] = None
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="the outcome is 'adjudicated'"):
        validate_adjudication(forged)
    with pytest.raises(SchemaRefusal, match="not independent"):
        adjudicate(first, transcribe(sample, _act(), "hand-a", "delta epsilon", path))
    with pytest.raises(SchemaRefusal, match="different acts"):
        adjudicate(first, transcribe(sample, _act(3), "hand-b", "delta epsilon", path))


def test_illegible_is_the_one_spelling_and_a_transcription_is_never_blank(tmp_path):
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    assert transcribe(sample, _act(), "hand-a", ILLEGIBLE, path)["text"] == ILLEGIBLE
    assert (
        "parrain " + ILLEGIBLE
        in transcribe(sample, _act(), "hand-a", "parrain " + ILLEGIBLE, path)["text"]
    )
    literal = r"le mot \illegible est écrit dans la marge"
    assert transcribe(sample, _act(), "hand-a", literal, path)["text"] == literal
    for rejected, reason in (
        ("", "empty"),
        ("   ", "empty"),
        ("parrain [illegible]", "reserved"),
        ("parrain (ILLEGIBLE?)", "reserved"),
        ("ILLEGIBLE", "reserved"),
    ):
        with pytest.raises(SchemaRefusal, match=reason):
            transcribe(sample, _act(), "hand-a", rejected, path)


def test_a_reserved_token_between_two_words_is_not_mistaken_for_a_bad_spelling(tmp_path):
    """The reserved token is carved out by position, not deleted before rescanning:
    deleting it would let unrelated fragments on either side splice back together
    into "illegible" by accident (`peril` + `[ILLEGIBLE]` + `legible` reading as
    `perillegible`), refusing a perfectly correct use of the token."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    spliced = "peril" + ILLEGIBLE + "legible"
    assert transcribe(sample, _act(), "hand-a", spliced, path)["text"] == spliced
    doubled = ILLEGIBLE + ILLEGIBLE
    assert transcribe(sample, _act(), "hand-a", doubled, path)["text"] == doubled
    escaped_and_reserved = r"un mot \illegible et un autre " + ILLEGIBLE + " ici"
    assert (
        transcribe(sample, _act(), "hand-a", escaped_and_reserved, path)["text"]
        == escaped_and_reserved
    )
    # A near-miss that only coincidentally borders a real token is still refused:
    # the token here is not the exact reserved spelling, so nothing protects it.
    with pytest.raises(SchemaRefusal, match="reserved"):
        transcribe(sample, _act(), "hand-a", "peril[illegible]legible", path)


def test_a_casefold_expanding_character_does_not_fake_a_bad_illegibility_spelling(tmp_path):
    """`str.casefold` is not length-preserving. Folding the whole reading and then
    indexing back into the unfolded one desynchronizes from the first `ß` or `ﬁ`
    onward — and both survive NFC, so a border-parish register reaches this. A
    correct `[ILLEGIBLE]` after two of them, and a correct `\\illegible` escape after
    one, are accepted."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    for accepted in (
        "Straßburg, Straßberg " + ILLEGIBLE,
        "ﬁ ﬁ " + ILLEGIBLE,
        "ß " + r"\illegible",
        "ß ß ß " + r"\illegible et " + ILLEGIBLE,
    ):
        assert unicodedata.normalize("NFC", accepted) == accepted
        assert transcribe(sample, _act(), "hand-a", accepted, path)["text"] == accepted
    # The expansion must not hide a real bad spelling either, in either direction.
    for refused in ("Straße illegible", "ß ß [illegible]"):
        with pytest.raises(SchemaRefusal, match="reserved"):
            transcribe(sample, _act(), "hand-a", refused, path)


def test_the_only_two_escapes_are_the_literal_word_and_a_literal_backslash(tmp_path):
    """`\\illegible` is the literal source word and `\\\\` is a literal backslash;
    a backslash before anything else escapes nothing and is refused.

    Asking the two questions separately is what left the convention without an
    inverse: read `\\\\illegible` as a literal backslash, then ask whether the word
    behind it is escaped by looking at the character in front of it, and it is at
    once a backslash followed by an unescaped illegibility *and* an escaped literal
    word. Nothing in the record decided between them, and these records are
    immutable, so the ambiguity could never be re-recorded out of the
    transcriber's hours."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    for accepted in (r"le mot \illegible", r"le mot \ILLEGIBLE", r"un \\ trait", r"\\\illegible"):
        assert transcribe(sample, _act(), "hand-a", accepted, path)["text"] == accepted
    # `\\` consumes both marks left to right, so the word after it is unescaped.
    with pytest.raises(SchemaRefusal, match="reserved"):
        transcribe(sample, _act(), "hand-a", r"\\illegible", path)
    for orphan in (r"le mot \marge", r"\ illegible", "fin \\"):
        with pytest.raises(SchemaRefusal, match="escapes nothing"):
            transcribe(sample, _act(), "hand-a", orphan, path)


def test_gold_text_is_stored_so_two_identical_readings_compare_equal(tmp_path):
    """Agreement is decided by equality, and the disagreement rate is a measure this
    corpus reports. An invisible difference — surrounding space, a CRLF, an NFD
    composition — would summon an adjudicator for two identical readings, so each is
    refused by name rather than silently repaired."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    composed = "Année"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    assert transcribe(sample, _act(), "hand-a", composed, path)["text"] == composed
    for rejected, reason in (
        (" " + composed, "whitespace"),
        (composed + "\n", "whitespace"),
        ("first\r\nsecond", "CR"),
        (decomposed, "NFC"),
    ):
        with pytest.raises(SchemaRefusal, match=reason):
            transcribe(sample, _act(), "hand-a", rejected, path)
    # A multi-line act is ordinary and must still be accepted.
    assert transcribe(sample, _act(), "hand-a", "first\nsecond", path)["text"] == "first\nsecond"


def test_a_byte_order_mark_may_not_fake_a_disagreement(tmp_path):
    """The commonest invisible difference of all, and the one `_gold_text`'s other
    rules exist to stop: a Windows editor saving "UTF-8 with signature" prefixes
    U+FEFF, which `str.strip` does not remove and no reviewer can see. Two
    transcribers reading the same words would then compare unequal, summon an
    adjudicator for an act nobody disagreed about, and inflate the disagreement
    rate this corpus reports. Refused by name like every other one, rather than
    stripped where nobody would see it."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    reading = "alpha beta"
    with_mark = tmp_path / "hand-a.txt"
    with_mark.write_bytes((reading + "\n").encode("utf-8-sig"))
    marked = read_transcription_text(with_mark)
    assert marked != reading and marked.strip() == marked
    with pytest.raises(SchemaRefusal, match="byte-order mark"):
        transcribe(sample, _act(), "hand-a", marked, path)
    plain = tmp_path / "hand-b.txt"
    plain.write_bytes((reading + "\n").encode("utf-8"))
    assert transcribe(sample, _act(), "hand-b", read_transcription_text(plain), path)["text"] == (
        reading
    )


def test_gold_may_not_be_made_of_the_pipelines_own_output(tmp_path):
    """These records are what the pipeline is measured against, so a chair's
    identity in place of a person's name is refused: gold made of pipeline output
    would make the measurement circular."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    with pytest.raises(SchemaRefusal, match="pipeline identity, not a person"):
        transcribe(sample, _act(), _act(7), "zeta", path)
    first, second = _pair(sample, "zeta", "zita", path)
    with pytest.raises(SchemaRefusal, match="pipeline identity, not a person"):
        adjudicate(first, second, adjudicator=_act(7), text="zeta")


def test_a_page_layout_or_padding_record_may_not_be_empty(tmp_path):
    """A record whose annotation list is empty says nothing while reading as a
    completed annotation. A page with nothing on it is annotated `true-blank`, and
    a padding record with no rectangles has measured nothing to be calibrated
    against."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    layout = {"schema": LAYOUT_SCHEMA, "sample": sample, "regions": []}
    layout["self_hash"] = self_hash(layout)
    with pytest.raises(SchemaRefusal, match="annotated as true-blank"):
        validate_layout(layout, path)
    padding = {
        "schema": PADDING_SCHEMA,
        "sample": sample,
        "rectangles": [],
        "calibrated_for_this_corpus": True,
    }
    padding["self_hash"] = self_hash(padding)
    with pytest.raises(SchemaRefusal, match="measured nothing"):
        validate_padding(padding, path)


def test_append_only_writer_reuses_identical_bytes_and_refuses_different_ones(tmp_path):
    """Republishing the same record is reuse, not a rewrite — `sample` writes one
    file per page, so an interruption partway through must not leave a directory
    the same command can never finish. Different bytes under one name are still
    refused, and the file already there is never touched."""
    record = {"example": "evidence"}
    target = tmp_path / "records" / "one.json"
    write_append_only(target, record)
    original = target.read_bytes()
    assert write_append_only(target, record) == target
    assert target.read_bytes() == original
    with pytest.raises(IncompatibleReuse, match="already holds different bytes"):
        write_append_only(target, {"example": "a different record"})
    assert target.read_bytes() == original
    assert not [path for path in target.parent.iterdir() if path.name.startswith(".gold-")]


def test_append_only_writer_names_a_no_hard_link_filesystem(tmp_path, monkeypatch):
    import gold.core as core_module

    def refuse_link(*_arguments, **_keywords):
        raise OSError(errno.EPERM, "Operation not permitted")

    def refuse_cleanup(*_arguments, **_keywords):
        raise OSError(errno.EACCES, "cleanup denied")

    # A failed best-effort cleanup must not mask the security refusal that stopped
    # publication. Restore unlink before pytest removes the deliberately stranded
    # unpredictable temporary.
    with monkeypatch.context() as patch:
        patch.setattr(core_module.os, "link", refuse_link)
        patch.setattr(core_module.os, "unlink", refuse_cleanup)
        with pytest.raises(SchemaRefusal, match="refuses hard links"):
            write_append_only(tmp_path / "records" / "one.json", {"example": "evidence"})


def test_append_only_writer_syncs_the_published_directory(tmp_path, monkeypatch):
    import gold.core as core_module

    real_fsync = core_module.os.fsync
    directory_syncs = 0

    def observe_fsync(descriptor):
        nonlocal directory_syncs
        if stat.S_ISDIR(core_module.os.fstat(descriptor).st_mode):
            directory_syncs += 1
        return real_fsync(descriptor)

    import stat

    monkeypatch.setattr(core_module.os, "fsync", observe_fsync)
    write_append_only(tmp_path / "records" / "one.json", {"example": "evidence"})
    assert directory_syncs == 1


def test_append_only_writer_refuses_symlink_and_portable_name_collisions(tmp_path):
    """An existing symlink is not byte-identical reuse: its target can change after
    the comparison. Names that default APFS identifies as one name are likewise one
    publication slot even when this Linux test filesystem can store both."""
    records = tmp_path / "records"
    records.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(canonical_bytes({"example": "evidence"}) + b"\n")
    redirected = records / "redirected.json"
    redirected.symlink_to(outside)

    with pytest.raises(IncompatibleReuse, match="regular non-symlink"):
        write_append_only(redirected, {"example": "evidence"})
    assert redirected.is_symlink()
    assert outside.read_bytes() == canonical_bytes({"example": "evidence"}) + b"\n"

    write_append_only(records / "Evidence.json", {"example": "first"})
    with pytest.raises(SchemaRefusal, match="collides by case or Unicode normalization"):
        write_append_only(records / "evidence.json", {"example": "second"})
    # On a case-insensitive host filesystem the two spellings are one file, so
    # existence of the lowercase name proves nothing; the slot's listing and
    # bytes staying exactly the first publication is the invariant.
    assert sorted(entry.name for entry in records.iterdir()) == [
        "Evidence.json",
        "redirected.json",
    ]
    assert (records / "Evidence.json").read_bytes() == canonical_bytes({"example": "first"}) + b"\n"


def test_corpus_directory_refuses_links_case_collisions_and_inode_replacement(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(real, target_is_directory=True)
    with pytest.raises(SchemaRefusal, match="without following links"):
        with cli._locked_corpus(redirected):
            raise AssertionError("a symlinked corpus directory must never be locked")

    (real / "One.json").write_text("{}", encoding="utf-8")
    (real / "one.json").write_text("{}", encoding="utf-8")
    if sorted(entry.name for entry in real.iterdir()) == ["One.json"]:
        # This host filesystem itself collapses the two spellings, so the plant
        # cannot exist on disk; drive the same production listing walk with the
        # names a case-sensitive corpus would deliver.
        import unittest.mock

        with unittest.mock.patch.object(cli.os, "listdir", return_value=["One.json", "one.json"]):
            with pytest.raises(SchemaRefusal, match="not portable to default APFS"):
                cli._records_in(real)
        (real / "One.json").unlink()
    else:
        with pytest.raises(SchemaRefusal, match="not portable to default APFS"):
            cli._records_in(real)

    corpus_path = tmp_path / "corpus"
    corpus_path.mkdir()
    moved = tmp_path / "moved"
    with cli._locked_corpus(corpus_path) as corpus:
        corpus_path.rename(moved)
        corpus_path.mkdir()
        with pytest.raises(SchemaRefusal, match="replaced after it was locked"):
            write_append_only(
                corpus_path / "one.json",
                {"example": "evidence"},
                directory_descriptor=corpus.descriptor,
            )
    assert not list(corpus_path.iterdir())
    assert not list(moved.iterdir())


def _forge_sample_outside_authority(sample):
    forged = json.loads(json.dumps(sample))
    forged["page"]["sha256"] = _sha("9")
    forged["set"] = set_for_page(forged["page"]["sha256"])
    without = {
        key: value for key, value in forged.items() if key not in {"sample_digest", "self_hash"}
    }
    forged["sample_digest"] = digest_bytes(canonical_bytes(without))
    forged["self_hash"] = self_hash(forged)
    return forged


def test_layout_and_padding_can_recheck_their_embedded_sample_against_run_authority(tmp_path):
    """A layout or padding record's embedded sample is only checked for internal
    self-consistency by default — it can restate a page/frame belonging to no real
    run and still validate. Passing --run (validate_layout/validate_padding's
    run_path) closes that derived-record gap the same way it already does for a
    bare sample."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    forged = _forge_sample_outside_authority(sample)
    layout = {
        "schema": LAYOUT_SCHEMA,
        "sample": forged,
        "regions": [{"kind": "true-blank", "rect": {"x": 0, "y": 0, "w": 1, "h": 1}}],
    }
    layout["self_hash"] = self_hash(layout)
    assert validate_layout(layout) == layout
    with pytest.raises(SchemaRefusal, match="outside the run authority"):
        validate_layout(layout, path)
    padding = {
        "schema": PADDING_SCHEMA,
        "sample": forged,
        "rectangles": [{"x": 0, "y": 0, "w": 1, "h": 1}],
        "calibrated_for_this_corpus": False,
    }
    padding["self_hash"] = self_hash(padding)
    assert validate_padding(padding) == padding
    with pytest.raises(SchemaRefusal, match="outside the run authority"):
        validate_padding(padding, path)


def test_bind_instrument_can_recheck_its_sample_against_run_authority(tmp_path):
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    forged = _forge_sample_outside_authority(sample)
    act = _act()
    assert bind_instrument(forged, act, _sha("e"))["sample_digest"] == forged["sample_digest"]
    with pytest.raises(SchemaRefusal, match="outside the run authority"):
        bind_instrument(forged, act, _sha("e"), path)


def test_a_shared_manual_pick_has_one_set_across_three_frames(tmp_path):
    """The partition itself, not only the corpus validator, keeps membership stable.

    The run gives each frame a different seed. The same manually picked page must still
    have one set before the validator refuses combining the three distinct ranked
    sampling universes.
    """
    first, frame, pages = run_file(tmp_path)
    paths_and_frames = [(first, frame)]
    for size in (9, 10):
        path = tmp_path / f"frame-{size}.json"
        source_pages = [
            *pages,
            *(
                {"ordinal": ordinal, "sha256": str(ordinal)[-1] * 64}
                for ordinal in range(9, size + 1)
            ),
        ]
        source = [
            {"ordinal": source_page["ordinal"], "sha256": source_page["sha256"]}
            for source_page in source_pages
        ]
        page_digest = digest_bytes(canonical_bytes(source))
        later_frame = {
            "page_digest": page_digest,
            "frame_digest": digest_bytes(canonical_bytes({"pages": source})),
            "seed": digest_bytes(canonical_bytes({"page_digest": page_digest, "purpose": "frame"})),
        }
        authority = {
            "schema": "skeleton.v1",
            "run_id": f"gold-frame-{size}",
            "source_manifest": source_pages,
            "corpus_frame_membership": later_frame,
        }
        authority["self_hash"] = self_hash(authority)
        path.write_text(json.dumps(authority), encoding="utf-8")
        paths_and_frames.append((path, later_frame))

    assert len({item[1]["seed"] for item in paths_and_frames}) == 3
    page = {**pages[0], "stratum": "adverse"}
    records = [
        ingest_manual_pick(
            path,
            {
                "schema": MANUAL_PICK_SCHEMA,
                "selection_basis": f"shared page under frame {index}",
                "page": page,
                "set": set_for_page(page["sha256"]),
            },
        )
        for index, (path, bound_frame) in enumerate(paths_and_frames, 1)
    ]
    assert len({record["set"] for record in records}) == 1
    assert all(validate_corpus([record]) for record in records)
    with pytest.raises(SchemaRefusal, match="different corpus frames"):
        validate_corpus(records)


def test_one_frame_digest_cannot_carry_contradictory_frame_facts(tmp_path):
    """A self-hashed sample cannot rederive a frame from pages it does not carry.

    Offline validation therefore accepts one internally consistent restatement, but
    corpus validation must not accept two different page-digest/seed pairs wearing
    the same frame identity. That would make file order decide which frame the gold
    records claim to inhabit.
    """
    path, frame, pages = run_file(tmp_path)
    samples = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))
    forged = json.loads(json.dumps(samples[0]))
    forged["frame"]["page_digest"] = _sha("e")
    forged["frame"]["seed"] = digest_bytes(
        canonical_bytes({"page_digest": _sha("e"), "purpose": "frame"})
    )
    without = {
        key: value for key, value in forged.items() if key not in {"sample_digest", "self_hash"}
    }
    forged["sample_digest"] = digest_bytes(canonical_bytes(without))
    forged["self_hash"] = self_hash(forged)
    assert validate_sample(forged) == forged
    with pytest.raises(SchemaRefusal, match="different authorities.*Keep immutable records"):
        validate_corpus([samples[1], forged])


def test_a_page_restratified_between_records_is_refused(tmp_path):
    """The catalog is human-supplied and bound to no authority, so a page can be
    described one way in the sample and another in a record embedding a
    differently-drawn sample. Per-record validation cannot see it."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    drawn = sample_stratified(path, rows, plan_for(frame, rows))[0]
    restratified = [
        {**row, "stratum": "occluded"} if row["sha256"] == drawn["page"]["sha256"] else row
        for row in rows
    ]
    other = next(
        record
        for record in sample_stratified(path, restratified, plan_for(frame, restratified))
        if record["page"]["sha256"] == drawn["page"]["sha256"]
    )
    layout = {
        "schema": LAYOUT_SCHEMA,
        "sample": other,
        "regions": [{"kind": "occlusion", "rect": {"x": 1, "y": 1, "w": 2, "h": 2}}],
    }
    layout["self_hash"] = self_hash(layout)
    assert validate_sample(drawn, path) and validate_layout(layout, path)
    with pytest.raises(SchemaRefusal, match="stratified as"):
        validate_corpus([drawn, layout], path)


def test_cli_walks_one_act_from_two_transcriptions_to_an_adjudication(tmp_path):
    """The operator-facing flow end to end, through real files: two transcribers,
    a disagreement, an adjudicator's own reading, and every record validating
    afterwards as part of one gold corpus."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    records = tmp_path / "records"
    records.mkdir()
    sample_file = records / "sample.json"
    sample_file.write_text(json.dumps(sample), encoding="utf-8")
    outputs = {}
    for hand, reading in (("hand-a", "alpha beta"), ("hand-b", "alpha delta")):
        text_file = tmp_path / f"{hand}.txt"
        # As a text editor writes it: one trailing newline, which is the file's.
        text_file.write_text(reading + "\n", encoding="utf-8")
        outputs[hand] = records / f"{hand}.json"
        assert (
            cli.main(
                [
                    "transcribe",
                    "--sample",
                    str(sample_file),
                    "--act-identity",
                    _act(),
                    "--transcriber",
                    hand,
                    "--text-file",
                    str(text_file),
                    "--output",
                    str(outputs[hand]),
                    "--run",
                    str(path),
                ]
            )
            == 0
        )
        assert json.loads(outputs[hand].read_text())["text"] == reading
    established = tmp_path / "established.txt"
    established.write_text("alpha beta\n", encoding="utf-8")
    adjudication = records / "adjudication.json"
    with pytest.raises(SchemaRefusal, match="reading they established"):
        cli.main(
            [
                "adjudicate",
                "--first",
                str(outputs["hand-a"]),
                "--second",
                str(outputs["hand-b"]),
                "--output",
                str(adjudication),
            ]
        )
    assert (
        cli.main(
            [
                "adjudicate",
                "--first",
                str(outputs["hand-a"]),
                "--second",
                str(outputs["hand-b"]),
                "--adjudicator",
                "hand-c",
                "--text-file",
                str(established),
                "--output",
                str(adjudication),
            ]
        )
        == 0
    )
    written = json.loads(adjudication.read_text())
    assert written["outcome"] == "adjudicated"
    assert [record["text"] for record in written["transcriptions"]] == [
        "alpha beta",
        "alpha delta",
    ]
    assert cli.main(["validate", str(adjudication)]) == 0
    assert cli.main(["validate-corpus", str(records), "--run", str(path)]) == 0


def test_cli_refuses_a_contradicting_record_before_it_becomes_immutable(tmp_path):
    """Publication reconciles against the corpus the record joins.

    Once `write_append_only` lands a byte it cannot be withdrawn, so a second
    reading from one transcriber, or a second adjudication of one act, is refused
    before it is written, under the same lock as the write."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    records = tmp_path / "records"
    records.mkdir()
    (records / "sample.json").write_text(json.dumps(sample), encoding="utf-8")

    def transcription(hand, reading, name):
        text_file = tmp_path / f"{name}.txt"
        text_file.write_text(reading + "\n", encoding="utf-8")
        return [
            "transcribe",
            "--sample",
            str(records / "sample.json"),
            "--act-identity",
            _act(),
            "--transcriber",
            hand,
            "--text-file",
            str(text_file),
            "--output",
            str(records / f"{name}.json"),
            "--run",
            str(path),
        ]

    assert cli.main(transcription("hand-a", "alpha beta", "first")) == 0
    # The same hand reading the same act twice: the act no longer has one
    # independent reading from that person, and the corpus cannot say which.
    with pytest.raises(SchemaRefusal, match="two transcription records"):
        cli.main(transcription("hand-a", "alpha delta", "restated"))
    assert not (records / "restated.json").exists()

    # An open custody chain is still publishable -- that is the whole reason
    # closure is waived here rather than the reconciliation being skipped.
    assert cli.main(transcription("hand-b", "alpha delta", "second")) == 0

    def adjudication(name, reading):
        established = tmp_path / f"{name}.txt"
        established.write_text(reading + "\n", encoding="utf-8")
        return [
            "adjudicate",
            "--first",
            str(records / "first.json"),
            "--second",
            str(records / "second.json"),
            "--adjudicator",
            "hand-c",
            "--text-file",
            str(established),
            "--output",
            str(records / f"{name}.json"),
        ]

    assert cli.main(adjudication("adjudication", "alpha beta")) == 0
    # Gold has one established reading per act. A second adjudication that
    # establishes a *different* reading is the contradiction: it is refused at
    # the door rather than published and named by a later validate-corpus.
    # (An identical one would carry the same self-hash and be reuse, not
    # conflict, exactly as a repeated sample record is.)
    with pytest.raises(SchemaRefusal, match="two conflicting adjudications"):
        cli.main(adjudication("second-adjudication", "alpha delta"))
    assert not (records / "second-adjudication.json").exists()
    assert cli.main(["validate-corpus", str(records), "--run", str(path)]) == 0


@pytest.mark.parametrize("payload", ([1, 2, 3], "a string", None, 42))
def test_a_gold_record_that_is_not_an_object_is_refused_by_name(tmp_path, payload):
    """`read_json` returns any JSON value; every reader then reads `schema` off it.

    A file holding a list, a string, a number or null is a named refusal, not an
    AttributeError, from `_records_in`, the one reader every command goes through.
    """
    records = tmp_path / "records"
    records.mkdir()
    (records / "rogue.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SchemaRefusal, match="is not a JSON object"):
        cli.main(["verify-sampling", str(records), "--run", str(tmp_path / "absent")])
    with pytest.raises(SchemaRefusal, match="is not a JSON object"):
        cli.main(["validate-corpus", str(records)])


def test_cli_publishes_into_a_corpus_whose_custody_chain_is_still_open(tmp_path):
    """A partial chain is legitimate while people work, so it blocks no publication.

    `validate-corpus` names an act with a transcription and no adjudication --
    that is the collection gate doing its job. Publication is a different
    question: adding a sample, a manual pick, or the *second* of two readings
    neither closes that act nor threatens it. If publication demanded a closed
    corpus, the first transcription would wedge every later write in the
    directory, including the second reading that is the only thing which can
    close it."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    sample = sample_stratified(path, rows, plan_for(frame, rows))[0]
    records = tmp_path / "records"
    records.mkdir()
    (records / "sample.json").write_text(json.dumps(sample), encoding="utf-8")

    text_file = tmp_path / "hand-a.txt"
    text_file.write_text("alpha beta\n", encoding="utf-8")
    assert (
        cli.main(
            [
                "transcribe",
                "--sample",
                str(records / "sample.json"),
                "--act-identity",
                _act(),
                "--transcriber",
                "hand-a",
                "--text-file",
                str(text_file),
                "--output",
                str(records / "first.json"),
                "--run",
                str(path),
            ]
        )
        == 0
    )
    # The corpus is now exactly what the collection gate refuses...
    with pytest.raises(SchemaRefusal, match="custody chain is incomplete"):
        cli.main(["validate-corpus", str(records), "--run", str(path)])

    # ...and every publication path still writes into it.
    page = rows[0]
    pick = tmp_path / "pick.json"
    pick.write_text(
        json.dumps(
            {
                "schema": MANUAL_PICK_SCHEMA,
                "selection_basis": "hand-picked beside an open chain",
                "page": page,
                "set": set_for_page(page["sha256"]),
            }
        ),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "ingest-manual",
                "--run",
                str(path),
                "--pick",
                str(pick),
                "--output",
                str(records / "manual.json"),
            ]
        )
        == 0
    )
    assert (records / "manual.json").exists()

    second_text = tmp_path / "hand-b.txt"
    second_text.write_text("alpha delta\n", encoding="utf-8")
    assert (
        cli.main(
            [
                "transcribe",
                "--sample",
                str(records / "sample.json"),
                "--act-identity",
                _act(),
                "--transcriber",
                "hand-b",
                "--text-file",
                str(second_text),
                "--output",
                str(records / "second.json"),
                "--run",
                str(path),
            ]
        )
        == 0
    )
    established = tmp_path / "established.txt"
    established.write_text("alpha beta\n", encoding="utf-8")
    assert (
        cli.main(
            [
                "adjudicate",
                "--first",
                str(records / "first.json"),
                "--second",
                str(records / "second.json"),
                "--adjudicator",
                "hand-c",
                "--text-file",
                str(established),
                "--output",
                str(records / "adjudication.json"),
            ]
        )
        == 0
    )
    # Closed again, and the gate agrees.
    assert cli.main(["validate-corpus", str(records), "--run", str(path)]) == 0


def test_cli_bind_instrument_refuses_a_membership_whose_sample_is_absent(tmp_path):
    """Instrument membership names a sample; publishing it beside no such sample
    records a measurement of something the corpus cannot show it measured."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    records = tmp_path / "records"
    records.mkdir()
    sample_file = tmp_path / "sample-outside-the-corpus.json"
    sample_file.write_text(json.dumps(sample), encoding="utf-8")
    with pytest.raises(SchemaRefusal, match="absent from the gold corpus"):
        cli.main(
            [
                "bind-instrument",
                "--sample",
                str(sample_file),
                "--act-identity",
                _act(),
                "--protocol-digest",
                _sha("e"),
                "--output",
                str(records / "membership.json"),
                "--run",
                str(path),
            ]
        )
    assert not (records / "membership.json").exists()


def test_corpus_refuses_orphaned_or_conflicting_adjudication_custody(tmp_path):
    """An adjudication must resolve the exact two independently stored readings.

    Self-hashing one record cannot establish collection custody: without this
    reconciliation an adjudication can embed transcriptions never retained as their
    own evidence, and two different adjudicators can establish two texts for one act
    while every record remains individually valid.
    """
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    first, second = _pair(sample, "alpha beta", "alpha delta", path)
    established = adjudicate(first, second, adjudicator="hand-c", text="alpha beta")

    with pytest.raises(SchemaRefusal, match="absent as independent gold records"):
        validate_corpus([sample, established], path)
    assert validate_corpus([sample, first, second, established], path)

    conflict = adjudicate(first, second, adjudicator="hand-d", text="alpha delta")
    with pytest.raises(SchemaRefusal, match="two conflicting adjudications"):
        validate_corpus([sample, first, second, established, conflict], path)

    revised = transcribe(sample, _act(), "hand-a", "alpha betta", path)
    with pytest.raises(SchemaRefusal, match="supplied two transcription records"):
        validate_corpus([sample, first, revised], path)


def test_the_corpus_api_refuses_an_empty_collection():
    """The CLI already names an empty directory, but the public collection gate
    must not return success when called directly with nothing to establish."""
    with pytest.raises(SchemaRefusal, match="empty collection proves no custody.*Supply"):
        validate_corpus([])


def test_corpus_refuses_a_started_reading_chain_without_its_adjudication(tmp_path):
    """Deleting the established record can leave one or both independent
    transcriptions in a corpus. A partial chain is legitimate while people work,
    but collection validation must name it as partial rather than let absence
    wear the same success as completed custody."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    first, second = _pair(sample, "alpha beta", "alpha delta", path)
    for partial in ([sample, first], [sample, first, second]):
        with pytest.raises(SchemaRefusal, match="custody chain is incomplete.*adjudicate"):
            validate_corpus(partial, path)
    established = adjudicate(first, second, adjudicator="hand-c", text="alpha beta")
    assert validate_corpus([sample, first, second, established], path)


def test_corpus_refuses_a_never_drawn_page_smuggled_inside_an_annotation(tmp_path):
    """`verify-sampling` reconciles the *sample records* in a directory. A layout or
    padding record carries its own copy of a sample inside it, so a page the sampler
    never chose could enter gold as an annotation and be replayed by nothing — every
    per-record check passes, and the draw's membership list is never consulted.
    Collection validation holds every seeded sample it can reach, embedded or
    standing alone, to the draw the corpus retains."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    plan = plan_for(frame, rows)
    draw, selected = build_sampling_draw(path, rows, plan)
    drawn_pages = {record["page"]["sha256"] for record in selected}
    never_drawn = next(row for row in rows if row["sha256"] not in drawn_pages)
    smuggled = build_sample(
        frame,
        never_drawn,
        selection_basis="seeded-stratified-v1",
        method="stratified-seed",
        sampling=selected[0]["sampling"],
    )
    assert validate_sample(smuggled, path)  # indistinguishable one record at a time
    layout = {
        "schema": LAYOUT_SCHEMA,
        "sample": smuggled,
        "regions": [{"kind": "act", "rect": {"x": 1, "y": 1, "w": 2, "h": 2}}],
    }
    layout["self_hash"] = self_hash(layout)
    assert validate_layout(layout, path) == layout
    assert validate_corpus([draw, *selected], path)
    with pytest.raises(SchemaRefusal, match="the retained sampling draw did not produce it"):
        validate_corpus([draw, *selected, layout], path)
    # A bare sample record the draw did not produce is refused the same way, and a
    # manual pick — which never claimed the seed chose it — is not.
    with pytest.raises(SchemaRefusal, match="the retained sampling draw did not produce it"):
        validate_corpus([draw, *selected, smuggled], path)
    picked = ingest_manual_pick(
        path,
        {
            "schema": MANUAL_PICK_SCHEMA,
            "selection_basis": "B1 pick of a page the seed did not draw",
            "page": never_drawn,
            "set": set_for_page(never_drawn["sha256"]),
        },
    )
    assert validate_corpus([draw, *selected, picked], path)


def _pick(path, frame, page, basis):
    return ingest_manual_pick(
        path,
        {
            "schema": MANUAL_PICK_SCHEMA,
            "selection_basis": basis,
            "page": page,
            "set": set_for_page(page["sha256"]),
        },
    )


def test_corpus_refuses_a_manual_pick_that_contradicts_the_retained_catalog(tmp_path):
    """A seeded sample is reconciled against the catalog by its membership digest; a
    manual one is reconciled against the *whole* normalized catalog the draw
    retains. A stratum nobody planned would make the stratification unmeasurable,
    and an invented width would make "the rectangles are proven on-page" vacuous,
    because every rectangle fits a page said to be huge."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    draw, selected = build_sampling_draw(path, rows, plan_for(frame, rows))
    drawn = {(record["page"]["ordinal"], record["page"]["sha256"]) for record in selected}
    never_drawn = next(row for row in rows if (row["ordinal"], row["sha256"]) not in drawn)

    honest = _pick(path, frame, never_drawn, "B1 pick")
    assert validate_corpus([draw, *selected, honest], path)

    restratified = _pick(path, frame, {**never_drawn, "stratum": "invented"}, "B1 pick")
    assert validate_sample(restratified, path) == restratified  # well-formed alone
    with pytest.raises(SchemaRefusal, match="silently restratify.*Regenerate.*preserve"):
        validate_corpus([draw, *selected, restratified], path)

    enlarged = _pick(path, frame, {**never_drawn, "width": 999_999}, "B1 pick")
    with pytest.raises(SchemaRefusal, match="rectangle boundary is therefore ambiguous.*preserve"):
        validate_corpus([draw, *selected, enlarged], path)
    layout = {
        "schema": LAYOUT_SCHEMA,
        "sample": enlarged,
        "regions": [{"kind": "act", "rect": {"x": 0, "y": 0, "w": 999_999, "h": 1}}],
    }
    layout["self_hash"] = self_hash(layout)
    assert validate_layout(layout, path) == layout
    with pytest.raises(SchemaRefusal, match="rectangle boundary is therefore ambiguous.*preserve"):
        validate_corpus([draw, *selected, layout], path)


def test_corpus_refuses_one_page_carried_by_two_manual_records(tmp_path):
    """`sample_digest` binds `selection_basis`, so the same page picked twice under
    two wordings mints two distinct, individually valid samples. That is the "second
    spelling of the same page ... counted twice" `ingest-manual` reconciles the
    destination corpus to prevent, whether or not the second pick also
    restratifies the page.

    A manual record beside the *seeded* record for the same page stays admissible:
    the seed can land on a page picked by hand before it existed, and refusing that
    would strand a real corpus with no remedy short of discarding that recorded
    provenance."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    draw, selected = build_sampling_draw(path, rows, plan_for(frame, rows))
    drawn = {(record["page"]["ordinal"], record["page"]["sha256"]) for record in selected}
    never_drawn = next(row for row in rows if (row["ordinal"], row["sha256"]) not in drawn)

    first = _pick(path, frame, never_drawn, "B1 pick")
    again = _pick(path, frame, never_drawn, "B1 pick, restated")
    assert first["sample_digest"] != again["sample_digest"]
    assert first["page"] == again["page"]
    with pytest.raises(SchemaRefusal, match="count one corpus page twice.*hold the corpus"):
        validate_corpus([draw, *selected, first, again], path)

    already_drawn = next(row for row in rows if (row["ordinal"], row["sha256"]) in drawn)
    assert validate_corpus([draw, *selected, _pick(path, frame, already_drawn, "week one")], path)


def test_corpus_refuses_duplicate_seeded_samples_and_page_annotations(tmp_path):
    """A retained draw already prevents two seeded records for one page, but a
    legacy corpus without its draw did not. Layout and padding had the same quiet
    multiplicity through either sample method: two self-consistent annotations of
    one page left a later reader to select by file order."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    seeded = sample_stratified(path, rows, plan_for(frame, rows))[0]
    duplicate = build_sample(
        frame,
        seeded["page"],
        selection_basis="a second seeded record for the same page",
        method="stratified-seed",
        sampling=seeded["sampling"],
    )
    assert duplicate["sample_digest"] != seeded["sample_digest"]
    with pytest.raises(SchemaRefusal, match="two different stratified-seed sample records"):
        validate_corpus([seeded, duplicate], path)

    for schema, field, first_value, second_value in (
        (
            LAYOUT_SCHEMA,
            "regions",
            [{"kind": "act", "rect": {"x": 1, "y": 1, "w": 2, "h": 2}}],
            [{"kind": "non-act-text", "rect": {"x": 1, "y": 1, "w": 2, "h": 2}}],
        ),
        (
            PADDING_SCHEMA,
            "rectangles",
            [{"x": 1, "y": 1, "w": 2, "h": 2}],
            [{"x": 2, "y": 2, "w": 3, "h": 3}],
        ),
    ):
        records = []
        for value in (first_value, second_value):
            record = {"schema": schema, "sample": seeded, field: value}
            if schema == PADDING_SCHEMA:
                record["calibrated_for_this_corpus"] = True
            record["self_hash"] = self_hash(record)
            records.append(record)
        with pytest.raises(SchemaRefusal, match="no unique gold annotation.*hold the corpus"):
            validate_corpus([seeded, *records], path)

        manual = _pick(path, frame, seeded["page"], "the same page selected manually")
        same_facts = {"schema": schema, "sample": manual, field: first_value}
        if schema == PADDING_SCHEMA:
            same_facts["calibrated_for_this_corpus"] = True
        same_facts["self_hash"] = self_hash(same_facts)
        assert validate_corpus([seeded, manual, records[0], same_facts], path)


def test_corpus_refuses_two_established_readings_for_one_act(tmp_path):
    """Custody is keyed by act identity, not by sample record. A page carried by
    both a manual and a seeded sample still holds each act once, so two custody
    chains for one act, each internally sound, are refused rather than left for
    file order to choose between."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    draw, selected = build_sampling_draw(path, rows, plan_for(frame, rows))
    seeded = selected[0]
    picked = _pick(path, frame, {**seeded["page"]}, "B1 pick of a page the seed also drew")
    assert picked["sample_digest"] != seeded["sample_digest"]

    act = _act()
    records = [draw, *selected, picked]
    for sample, readings, established in (
        (picked, ("zeta eta", "zeta eda"), "zeta eta"),
        (seeded, ("theta iota", "theta iotta"), "an entirely different reading"),
    ):
        first = transcribe(sample, act, "hand-a", readings[0], path)
        second = transcribe(sample, act, "hand-b", readings[1], path)
        records += [
            first,
            second,
            adjudicate(first, second, adjudicator="hand-c", text=established),
        ]
    with pytest.raises(SchemaRefusal, match="two transcription records for act"):
        validate_corpus(records, path)

    # Four distinct transcribers, so no one of them reads the act twice: the
    # refusal comes from the act's own custody, which admits two readings only.
    records = [draw, *selected, picked]
    for sample, hands, readings, established in (
        (picked, ("hand-a", "hand-b"), ("zeta eta", "zeta eda"), "zeta eta"),
        (seeded, ("hand-d", "hand-e"), ("theta iota", "theta iotta"), "another reading"),
    ):
        first = transcribe(sample, act, hands[0], readings[0], path)
        second = transcribe(sample, act, hands[1], readings[1], path)
        records += [
            first,
            second,
            adjudicate(first, second, adjudicator="hand-c", text=established),
        ]
    with pytest.raises(SchemaRefusal, match="already has two independent transcriptions"):
        validate_corpus(records, path)


def test_corpus_refuses_a_drawn_page_re_minted_under_another_method(tmp_path):
    """The membership check must run both ways: every `stratified-seed` sample
    must be a draw member, and every draw member must still be present as one.
    A page the seed genuinely chose could otherwise be re-minted as `manual`
    (with the matching page-derived `set` as its `claimed_set`, so it is
    individually well-formed) and disappear from the seeded accounting while
    `validate_corpus` keeps reporting success -- a silent loss."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    plan = plan_for(frame, rows)
    draw, selected = build_sampling_draw(path, rows, plan)
    real = selected[0]
    relabeled = dict(real)
    relabeled["method"] = "manual"
    relabeled["claimed_set"] = relabeled["set"]
    relabeled["sampling"] = None
    without = {
        key: value for key, value in relabeled.items() if key not in {"sample_digest", "self_hash"}
    }
    relabeled["sample_digest"] = digest_bytes(canonical_bytes(without))
    relabeled["self_hash"] = self_hash(relabeled)
    assert validate_sample(relabeled, path) == relabeled  # well-formed alone
    others = [record for record in selected if record is not real]
    with pytest.raises(SchemaRefusal, match="has vanished.*byte-identical original.*hold"):
        validate_corpus([draw, relabeled, *others], path)


def test_corpus_refuses_two_recorded_draws_in_one_gold_corpus(tmp_path):
    """Two draws are two predeclared designs. Neither can speak for the records
    beside it, and combining them is the same defect the frame-mixing refusal
    exists for one level up."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    plan = plan_for(frame, rows)
    first, _selected = build_sampling_draw(path, rows, plan)
    narrower = {gold_set: dict(quotas) for gold_set, quotas in plan.items()}
    narrower["calibration"][next(name for name, q in narrower["calibration"].items() if q)] = 0
    second, _also = build_sampling_draw(path, rows, narrower)
    assert first["self_hash"] != second["self_hash"]
    with pytest.raises(SchemaRefusal, match="different sampling draws"):
        validate_corpus([first, second], path)


def test_sample_refuses_a_second_draw_before_publishing_any_of_it(tmp_path):
    """A draw is an immutable collection authority, so discovering the conflict
    after publishing its first file leaves a corpus no later command can repair.
    The writer validates the prospective union before any second-draw byte lands."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    first_plan = plan_for(frame, rows)
    second_plan = {gold_set: dict(quotas) for gold_set, quotas in first_plan.items()}
    sampled_stratum = next(
        stratum for stratum, quota in second_plan["calibration"].items() if quota
    )
    second_plan["calibration"][sampled_stratum] = 0
    for name, payload in (("catalog", rows), ("first", first_plan), ("second", second_plan)):
        (tmp_path / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "records"
    common = [
        "--run",
        str(path),
        "--catalog",
        str(tmp_path / "catalog.json"),
        "--output-dir",
        str(output),
    ]
    assert cli.main(["sample", *common, "--plan", str(tmp_path / "first.json")]) == 0
    before = {item.name: item.read_bytes() for item in output.glob("*.json")}

    with pytest.raises(SchemaRefusal, match="separate gold-record directory"):
        cli.main(["sample", *common, "--plan", str(tmp_path / "second.json")])

    assert {item.name: item.read_bytes() for item in output.glob("*.json")} == before
    assert cli.main(["validate-corpus", str(output), "--run", str(path)]) == 0


def test_corpus_transaction_lock_serializes_check_and_publish(tmp_path):
    """Two corpus writers must not both validate the same stale directory state.
    The second transaction cannot enter until the first has finished publishing."""
    entered = threading.Event()
    acquired = threading.Event()

    with cli._locked_corpus(tmp_path):

        def contend() -> None:
            entered.set()
            with cli._locked_corpus(tmp_path):
                acquired.set()

        contender = threading.Thread(target=contend)
        contender.start()
        assert entered.wait(timeout=1)
        assert not acquired.wait(timeout=0.1)

    contender.join(timeout=1)
    assert not contender.is_alive()
    assert acquired.is_set()


def _stray_writes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """The paths a refusal moved, named -- a count would not say which file to look at."""
    return sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )


def _unopenable_corpus(tmp_path, _monkeypatch):
    """A name already held by a regular file: the lock's own `mkdir` refuses it."""
    blocked = tmp_path / "corpus.json"
    blocked.write_bytes(b"{}\n")
    return blocked, "could not be opened for a publication"


def _unlockable_corpus(tmp_path, monkeypatch):
    def refuse_lock(_descriptor, _operation):
        raise OSError("locking unavailable")

    monkeypatch.setattr(cli.fcntl, "flock", refuse_lock)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    write_append_only(corpus / "held.json", {"example": "evidence"})
    return corpus, "supports advisory locks"


@pytest.mark.parametrize(
    "build",
    (
        pytest.param(_unopenable_corpus, id="unopenable"),
        pytest.param(_unlockable_corpus, id="unlockable"),
    ),
)
def test_corpus_lock_failures_are_named_before_any_record_is_written(tmp_path, monkeypatch, build):
    """Lock failure cannot fall through to an unlocked publication or a traceback;
    the refusal states the risk, the safe remedy, and that no evidence was written.

    That last clause is the one an operator acts on -- the remedy is to correct the
    directory and retry, and a retry is only safe while the refused attempt published
    nothing. A gold record is immutable once its name exists, so a record laid down
    under a lock that was never held is not something the retry can take back.

    So it is measured rather than read. The unlockable case starts from a corpus that
    already holds a record, because a glob for JSON names cannot see a record that was
    overwritten rather than created, and the comparison is over content. The assertion
    names the paths that moved, because "one file changed" does not say which.
    """
    directory, named = build(tmp_path, monkeypatch)
    before = tree_snapshot(tmp_path)

    with pytest.raises(SchemaRefusal) as refusal:
        with cli._locked_corpus(directory):
            raise AssertionError("an unheld corpus lock must never yield")

    assert named in str(refusal.value)
    assert "no gold record was written" in str(refusal.value)
    assert _stray_writes(before, tree_snapshot(tmp_path)) == [], (
        "the refusal wrote to the corpus it disowned"
    )


def test_a_publication_refused_for_an_unlistable_directory_wrote_no_record(tmp_path, monkeypatch):
    """The collision scan's own failure is a refusal, and it claims no record.

    `write_append_only` lists the held descriptor to catch a name that collides by
    case or Unicode normalization. When that listing fails it cannot prove the
    absence of a collision, so it refuses -- and tells the operator no record was
    written. The temporary and the link that publish the record come after, which is
    exactly the ordering this measures rather than reads.
    """
    records = tmp_path / "records"
    records.mkdir()
    write_append_only(records / "first.json", {"example": "evidence"})
    real_listdir = os.listdir

    def refuse_descriptor_listing(target):
        # Scoped to the held descriptor: a path-shaped listing is somebody else's.
        if isinstance(target, int):
            raise OSError(errno.EIO, "simulated directory listing failure")
        return real_listdir(target)

    monkeypatch.setattr(os, "listdir", refuse_descriptor_listing)
    before = tree_snapshot(tmp_path)

    with pytest.raises(SchemaRefusal) as refusal:
        write_append_only(records / "second.json", {"example": "another"})

    assert "could not be listed through" in str(refusal.value)
    assert "no record was written" in str(refusal.value)
    assert _stray_writes(before, tree_snapshot(tmp_path)) == [], (
        "the refusal wrote to the directory it disowned"
    )


def test_cli_empty_corpus_refusal_names_the_next_step(tmp_path):
    with pytest.raises(SchemaRefusal, match="Put one corpus's JSON records.*retry"):
        cli.main(["validate-corpus", str(tmp_path)])


def test_corpus_refuses_an_instrument_membership_without_its_sample(tmp_path):
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    membership = bind_instrument(sample, _act(), _sha("e"), path)
    with pytest.raises(SchemaRefusal, match="sample is absent"):
        validate_corpus([membership], path)
    assert validate_corpus([sample, membership], path)


def test_cli_verify_sampling_replays_what_the_sampler_wrote(tmp_path):
    """The operator-facing half of the replay: point `verify-sampling` at the
    directory `sample` wrote and it re-derives the draw from the same run, catalog,
    and plan. A record removed from the directory — the quiet failure, since each
    remaining record still validates on its own — is refused."""
    path, frame, pages = run_file(tmp_path)
    rows, output = catalog(pages), tmp_path / "records"
    plan = plan_for(frame, rows)
    files = {"catalog": rows, "plan": plan}
    for name, payload in files.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    common = [
        "--run",
        str(path),
        "--catalog",
        str(tmp_path / "catalog.json"),
        "--plan",
        str(tmp_path / "plan.json"),
    ]
    assert cli.main(["sample", *common, "--output-dir", str(output)]) == 0
    written = sorted(output.glob("*.json"))
    assert len(written) == sum(sum(quotas.values()) for quotas in plan.values()) + 1
    assert (
        cli.main(
            [
                "verify-sampling",
                str(output),
                "--run",
                str(path),
                "--catalog",
                str(tmp_path / "catalog.json"),
                "--plan",
                str(tmp_path / "plan.json"),
            ]
        )
        == 0
    )
    sample_file = next(
        item for item in written if json.loads(item.read_text())["schema"] == SAMPLE_SCHEMA
    )
    sample_file.unlink()
    with pytest.raises(SchemaRefusal, match="diverge.*membership|membership.*diverge"):
        cli.main(["verify-sampling", str(output), "--run", str(path)])


def test_verify_sampling_survives_a_manual_pick_beside_the_drawn_records(tmp_path):
    """A manual pick is not a claim about the draw. `ingest-manual` reconciles a
    pick against the gold records beside its output path and `validate-corpus`
    reads that one directory, so drawn samples and picks share it by design, and
    `verify-sampling` accepts the directory with a pick in it. The seeded members
    still reconcile exactly, because `sample_digest` binds `method`: a hand-picked
    page wearing `stratified-seed` is refused."""
    path, frame, pages = run_file(tmp_path)
    rows, output = catalog(pages), tmp_path / "records"
    plan = plan_for(frame, rows)
    for name, payload in (("catalog", rows), ("plan", plan)):
        (tmp_path / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    assert (
        cli.main(
            [
                "sample",
                "--run",
                str(path),
                "--catalog",
                str(tmp_path / "catalog.json"),
                "--plan",
                str(tmp_path / "plan.json"),
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )
    drawn_pages = {
        json.loads(item.read_text()).get("page", {}).get("sha256") for item in output.glob("*.json")
    }
    picked = next(row for row in rows if row["sha256"] not in drawn_pages)
    (tmp_path / "pick.json").write_text(
        json.dumps(
            {
                "schema": MANUAL_PICK_SCHEMA,
                "selection_basis": "B1 pick, filed with the drawn corpus",
                "page": picked,
                "set": set_for_page(picked["sha256"]),
            }
        ),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "ingest-manual",
                "--run",
                str(path),
                "--pick",
                str(tmp_path / "pick.json"),
                "--output",
                str(output / "manual.json"),
            ]
        )
        == 0
    )
    assert cli.main(["validate-corpus", str(output), "--run", str(path)]) == 0
    assert (
        cli.main(
            [
                "verify-sampling",
                str(output),
                "--run",
                str(path),
                "--catalog",
                str(tmp_path / "catalog.json"),
                "--plan",
                str(tmp_path / "plan.json"),
            ]
        )
        == 0
    )
    # The seeded half is still reconciled exactly: a page the draw did not choose,
    # minted as a seeded sample, is refused even with the pick sitting beside it.
    smuggled = build_sample(
        frame,
        picked,
        selection_basis="seeded-stratified-v1",
        method="stratified-seed",
        sampling=next(
            record["sampling"]
            for record in (json.loads(item.read_text()) for item in output.glob("*.json"))
            if record.get("sampling")
        ),
    )
    write_append_only(output / f"{smuggled['sample_digest']}.json", smuggled)
    with pytest.raises(SchemaRefusal, match="diverge.*membership|membership.*diverge"):
        cli.main(["verify-sampling", str(output), "--run", str(path)])


def test_cli_entry_point_states_the_refusal_instead_of_printing_a_traceback(tmp_path):
    """Run as a program, a `SchemaRefusal` reaches the operator as one stderr line
    and exit 2, not as a stack trace."""
    bad = tmp_path / "not-json.json"
    bad.write_text("{not valid json", encoding="utf-8")
    finished = subprocess.run(
        [sys.executable, "-m", "gold.cli", "validate", str(bad)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        # PYTHONSAFEPATH (the gate exports it) drops the cwd from sys.path, so
        # `-m gold.cli` needs the repository root supplied explicitly.
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert finished.returncode == 2
    assert finished.stderr.strip() == f"SchemaRefusal: {bad} is not readable JSON"
    assert "Traceback" not in finished.stderr


def test_a_float_in_a_gold_file_is_a_named_refusal_not_a_traceback(tmp_path):
    """`canonical_bytes` refuses floats, but as a `TypeError` from inside the
    self-hash — and a layout/padding record is self-hashed before its rectangles
    are read. A pixel bound typed `1.5` therefore escaped `validate` as a traceback
    and exit 1 instead of a named refusal and exit 2. Refused where the file is
    read, so the refusal can name it."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    layout = {
        "schema": LAYOUT_SCHEMA,
        "sample": sample,
        "regions": [{"kind": "act", "rect": {"x": 1.5, "y": 2, "w": 3, "h": 4}}],
    }
    layout["self_hash"] = _sha("0")
    record = tmp_path / "layout.json"
    record.write_text(json.dumps(layout), encoding="utf-8")
    finished = subprocess.run(
        [sys.executable, "-m", "gold.cli", "validate", str(record)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert finished.returncode == 2
    assert "Traceback" not in finished.stderr
    assert "carries integers, not the float 1.5" in finished.stderr
    # NaN and Infinity are floats under another spelling, and json accepts both.
    for literal in ("NaN", "Infinity"):
        spelled = tmp_path / f"{literal}.json"
        spelled.write_text('{"quota": %s}' % literal, encoding="utf-8")
        with pytest.raises(SchemaRefusal, match="carries integers, not the float"):
            cli.main(["validate", str(spelled)])


def test_gold_inputs_are_bounded_regular_files_and_never_follow_symlinks(tmp_path, monkeypatch):
    import gold.core as core_module

    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    redirected = tmp_path / "redirected.json"
    redirected.symlink_to(real)
    with pytest.raises(SchemaRefusal, match="without following links"):
        read_json(redirected)

    monkeypatch.setattr(core_module, "_MAX_INPUT_BYTES", 8)
    oversized = tmp_path / "oversized.json"
    oversized.write_text('{"value":1}', encoding="utf-8")
    with pytest.raises(SchemaRefusal, match="8-byte gold JSON input limit"):
        read_json(oversized)


def test_an_oversized_integer_literal_is_a_named_refusal_not_a_traceback(tmp_path):
    """CPython bounds decimal-to-int conversion. A literal beyond that bound raises
    ValueError rather than JSONDecodeError, but it is still untrusted JSON input and
    must reach the operator as the same security refusal."""
    record = tmp_path / "huge-integer.json"
    record.write_text('{"ordinal":' + "9" * 5000 + "}", encoding="utf-8")
    with pytest.raises(SchemaRefusal, match="not readable JSON"):
        read_json(record)


def test_a_deeply_nested_gold_file_is_a_named_refusal_not_a_traceback(tmp_path):
    """json's scanner recurses per nesting level, so a deeply nested file raises
    `RecursionError` rather than `JSONDecodeError` — and `_records_in` reads every
    `*.json` in a directory, so one such file must end `validate-corpus` with a
    refusal naming the file, not a traceback."""
    nested = tmp_path / "records" / "nested.json"
    nested.parent.mkdir()
    # The exhaustion depth is the interpreter's own, not a portable constant:
    # 30,000 levels exhaust the scanner on this repo's Linux container but not
    # on a macOS CPython 3.14, whose C-stack allowance is larger. Probe upward
    # for the depth this interpreter actually refuses at, so the test proves
    # the refusal wherever it runs instead of proving it on exactly one build.
    for depth in (30_000, 100_000, 300_000, 1_000_000):
        document = "[" * depth + "]" * depth
        try:
            json.loads(document)
        except RecursionError:
            break
    else:
        pytest.fail(
            "1,000,000 levels did not exhaust this interpreter's scanner, so there is no "
            "RecursionError for the named refusal to catch and this test proves nothing"
        )
    nested.write_text(document, encoding="utf-8")
    with pytest.raises(SchemaRefusal, match="not readable JSON"):
        cli.main(["validate-corpus", str(nested.parent)])


def test_cli_malformed_json_input_is_a_named_refusal_not_a_traceback(tmp_path):
    """Every CLI input route must wrap malformed JSON in a named refusal."""
    path, _frame, _pages = run_file(tmp_path)
    bad = tmp_path / "not-json.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SchemaRefusal, match="not readable JSON"):
        cli.main(
            [
                "sample",
                "--run",
                str(path),
                "--catalog",
                str(bad),
                "--plan",
                str(bad),
                "--output-dir",
                str(tmp_path / "out"),
            ]
        )
    with pytest.raises(SchemaRefusal, match="not readable JSON"):
        cli.main(["validate", str(bad)])


def test_unhashable_enum_spellings_are_named_refusals_not_type_errors(tmp_path):
    """JSON arrays and objects are legal input but unhashable in Python, so every
    externally supplied enum checks its string shape before set membership and
    refuses by name rather than raising `TypeError`."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    for field, expected in (
        ("method", "Use 'stratified-seed' or 'manual' and retry"),
        ("claimed_set", "Use 'calibration' or 'locked-acceptance' and retry"),
        ("set", "Regenerate an unpublished sample from the page sha256"),
    ):
        forged = json.loads(json.dumps(sample))
        forged[field] = []
        without = {
            key: value for key, value in forged.items() if key not in {"sample_digest", "self_hash"}
        }
        forged["sample_digest"] = digest_bytes(canonical_bytes(without))
        forged["self_hash"] = self_hash(forged)
        with pytest.raises(SchemaRefusal, match=expected):
            validate_sample(forged)

    pick = {
        "schema": MANUAL_PICK_SCHEMA,
        "selection_basis": "basis",
        "page": catalog(pages)[0],
        "set": [],
    }
    with pytest.raises(SchemaRefusal, match="Use 'calibration' or 'locked-acceptance' and retry"):
        ingest_manual_pick(path, pick)

    layout = {
        "schema": LAYOUT_SCHEMA,
        "sample": sample,
        "regions": [{"kind": [], "rect": {"x": 1, "y": 1, "w": 2, "h": 2}}],
    }
    layout["self_hash"] = self_hash(layout)
    with pytest.raises(SchemaRefusal, match="Regenerate.*act, non-act-text.*preserve"):
        validate_layout(layout)


def test_the_gold_frame_is_derived_from_the_field_the_run_authority_binds(tmp_path):
    """Gold must re-derive container membership from the run-bound computed digest."""
    from common.contracts.canonical import digest_of
    from common.runtree.store import RunTree

    container = _sha("a")

    def page(index: int) -> dict:
        return {
            "relative_path": "register.pdf",
            "sha256": container,
            "computed_sha256": digest_of(
                {"container_sha256": container, "container_page_index": index}
            ),
            "ordinal": index + 1,
        }

    common = dict(
        config_digest=_sha("c"),
        adapter_recipes={"designator": "fake-designator-v0"},
        witness_chairs=["attestator_1"],
    )
    tree = RunTree.create(tmp_path / "runs", "r1", source_manifest=[page(0), page(1)], **common)
    frame, source, _canaries = load_run_frame(tmp_path / "runs" / "r1" / "run.json")

    assert frame == tree.read_run()["corpus_frame_membership"]
    assert [row["sha256"] for row in source] == [
        page(0)["computed_sha256"],
        page(1)["computed_sha256"],
    ]

    single = RunTree.create(tmp_path / "single", "r1", source_manifest=[page(0)], **common)
    assert single.read_run()["corpus_frame_membership"] != frame


def test_two_runs_agreeing_only_on_their_declarations_are_two_frames(tmp_path):
    """An untrusted shared declaration must not unify different inspected bytes."""
    from common.runtree.store import RunTree

    common = dict(
        config_digest=_sha("c"),
        adapter_recipes={"designator": "fake-designator-v0"},
        witness_chairs=["attestator_1"],
    )

    def frame_of(root: str, computed: str) -> dict:
        RunTree.create(
            tmp_path / root,
            "r1",
            source_manifest=[
                {
                    "relative_path": "page.png",
                    "sha256": _sha("a"),
                    "computed_sha256": computed,
                    "ordinal": 1,
                }
            ],
            **common,
        )
        return load_run_frame(tmp_path / root / "r1" / "run.json")[0]

    assert frame_of("shard-one", _sha("b")) != frame_of("shard-two", _sha("d"))
    # Incidental run identity must not split identical inspected content.
    assert frame_of("shard-three", _sha("b")) == frame_of("shard-four", _sha("b"))


def test_gold_rederivation_honours_an_explicitly_absent_computed_digest(tmp_path):
    """An unreadable page stores None, which means use its retained declaration."""
    from common.runtree.store import RunTree

    tree = RunTree.create(
        tmp_path / "runs",
        "r1",
        source_manifest=[
            {
                "relative_path": "unreadable.png",
                "sha256": _sha("a"),
                "computed_sha256": None,
                "ordinal": 1,
            }
        ],
        config_digest=_sha("c"),
        adapter_recipes={"designator": "fake-designator-v0"},
        witness_chairs=["attestator_1"],
    )

    frame, source, _canaries = load_run_frame(tmp_path / "runs" / "r1" / "run.json")
    assert frame == tree.read_run()["corpus_frame_membership"]
    assert source == [{"ordinal": 1, "sha256": _sha("a")}]


def _cli_transcription(tmp_path, records, run_path, hand, name):
    text_file = tmp_path / f"{name}.txt"
    text_file.write_text("alpha\n", encoding="utf-8")
    return [
        "transcribe",
        "--sample",
        str(records / "sample.json"),
        "--act-identity",
        _act(),
        "--transcriber",
        hand,
        "--text-file",
        str(text_file),
        "--output",
        str(records / f"{name}.json"),
        "--run",
        str(run_path),
    ]


def test_a_third_transcriber_is_refused_before_the_act_becomes_unclosable(tmp_path):
    """An adjudication reconciles exactly two readings and records are immutable,
    so a third transcription of one act is refused at publication, before and
    after the act is adjudicated, and the act stays closable."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    records = tmp_path / "records"
    records.mkdir()
    (records / "sample.json").write_text(json.dumps(sample), encoding="utf-8")

    assert cli.main(_cli_transcription(tmp_path, records, path, "hand-a", "t0")) == 0
    assert cli.main(_cli_transcription(tmp_path, records, path, "hand-b", "t1")) == 0
    with pytest.raises(SchemaRefusal, match="already has two independent transcriptions"):
        cli.main(_cli_transcription(tmp_path, records, path, "hand-c", "t2"))
    assert not (records / "t2.json").exists()

    adjudication = [
        "adjudicate",
        "--first",
        str(records / "t0.json"),
        "--second",
        str(records / "t1.json"),
        "--output",
        str(records / "adjudication.json"),
    ]
    assert cli.main(adjudication) == 0
    with pytest.raises(SchemaRefusal, match="already has two independent transcriptions"):
        cli.main(_cli_transcription(tmp_path, records, path, "hand-c", "t2"))
    assert not (records / "t2.json").exists()
    assert cli.main(["validate-corpus", str(records), "--run", str(path)]) == 0

    three = [transcribe(sample, _act(), hand, "alpha") for hand in ("hand-a", "hand-b", "hand-c")]
    with pytest.raises(SchemaRefusal, match="already has two independent transcriptions"):
        validate_corpus([sample, *three], require_closure=False)


def test_a_person_has_one_spelling_and_is_compared_ignoring_case(tmp_path):
    """A name must be in NFC, and two names that differ only in case name one
    person, so neither can pass as a second, independent reader."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    decomposed = unicodedata.normalize("NFD", "hand-é")
    with pytest.raises(SchemaRefusal, match="transcriber is not in Unicode NFC"):
        transcribe(sample, _act(), decomposed, "alpha")
    first = transcribe(sample, _act(), "hand-a", "alpha")
    second = transcribe(sample, _act(), "hand-b", "alpha beta")
    with pytest.raises(SchemaRefusal, match="adjudicator is not in Unicode NFC"):
        adjudicate(first, second, adjudicator=decomposed, text="alpha")
    with pytest.raises(SchemaRefusal, match="cannot be its own reconciliation"):
        adjudicate(first, second, adjudicator="HAND-A", text="alpha")

    upper = transcribe(sample, _act(), "HAND-É", "alpha")
    lower = transcribe(sample, _act(), "hand-é", "alpha")
    with pytest.raises(SchemaRefusal, match="not independent"):
        adjudicate(upper, lower)
    with pytest.raises(SchemaRefusal, match="supplied two transcription records"):
        validate_corpus([sample, upper, lower], require_closure=False)


def _self_consistent_draw(rows):
    """A draw whose frame, catalog and empty membership agree with each other."""
    source = sorted(
        ({"ordinal": row["ordinal"], "sha256": row["sha256"]} for row in rows),
        key=lambda page: page["ordinal"],
    )
    page_digest = digest_bytes(canonical_bytes(source))
    record = {
        "schema": "gold-sampling-draw.v2",
        "frame": {
            "page_digest": page_digest,
            "frame_digest": digest_bytes(canonical_bytes({"pages": source})),
            "seed": digest_bytes(canonical_bytes({"page_digest": page_digest, "purpose": "frame"})),
        },
        "catalog": sorted(rows, key=lambda row: (row["stratum"], row["ordinal"])),
        "plan": {"calibration": {"s": 0}, "locked-acceptance": {"s": 0}},
        "members": [],
    }
    record["self_hash"] = self_hash(record)
    return record


@pytest.mark.parametrize(
    ("ordinals", "message"),
    [
        ((1, 1, 2), "sampling draw catalog page ordinals repeat"),
        ((0, 1), "sampling draw catalog page ordinal is not a page number"),
        ((-3, 1), "sampling draw catalog page ordinal is not a page number"),
    ],
)
def test_a_draw_checked_offline_refuses_ordinals_that_name_no_one_page(ordinals, message):
    """Without a run authority the retained catalog is the only membership, so
    it is held to the same page numbering the run itself is."""
    rows = [
        {"ordinal": ordinal, "sha256": _sha("abc"[index]), "stratum": "s", "width": 1, "height": 1}
        for index, ordinal in enumerate(ordinals)
    ]
    with pytest.raises(SchemaRefusal, match=message):
        validate_sampling_draw(_self_consistent_draw(rows))
    valid = [{**row, "ordinal": index} for index, row in enumerate(rows, 1)]
    assert validate_sampling_draw(_self_consistent_draw(valid))


def test_validate_refuses_run_for_a_record_that_names_its_sample_by_digest(tmp_path):
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    first, second = _pair(sample, "alpha", "alpha")
    for name, record in (
        ("transcription", first),
        ("adjudication", adjudicate(first, second)),
        ("measurement", bind_instrument(sample, _act(), _sha("e"))),
    ):
        target = tmp_path / f"{name}.json"
        target.write_text(json.dumps(record), encoding="utf-8")
        assert cli.main(["validate", str(target)]) == 0
        with pytest.raises(SchemaRefusal, match="--run cannot check a gold-"):
            cli.main(["validate", str(target), "--run", str(path)])


def _reseal_sample(sample):
    without = {
        key: value for key, value in sample.items() if key not in {"sample_digest", "self_hash"}
    }
    sample["sample_digest"] = digest_bytes(canonical_bytes(without))
    sample["self_hash"] = self_hash(sample)
    return sample


def test_a_replaced_seed_is_refused_offline_in_a_draw_and_a_sample(tmp_path):
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    draw, selected = build_sampling_draw(path, rows, plan_for(frame, rows))
    forged_draw = json.loads(json.dumps(draw))
    forged_draw["frame"]["seed"] = _sha("0")
    forged_draw["self_hash"] = self_hash(forged_draw)
    with pytest.raises(
        SchemaRefusal, match="sampling draw frame seed diverges from its derivation"
    ):
        validate_sampling_draw(forged_draw)

    forged_sample = json.loads(json.dumps(selected[0]))
    forged_sample["frame"]["seed"] = _sha("0")
    with pytest.raises(SchemaRefusal, match="sample frame seed diverges from its derivation"):
        validate_sample(_reseal_sample(forged_sample))


def test_a_draw_or_transcription_with_a_broken_self_hash_is_refused(tmp_path):
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    draw, selected = build_sampling_draw(path, rows, plan_for(frame, rows))
    with pytest.raises(SchemaRefusal, match="sampling draw fails its self-hash"):
        validate_sampling_draw({**draw, "self_hash": _sha("0")})
    first, _second = _pair(selected[0], "alpha", "alpha")
    with pytest.raises(SchemaRefusal, match="transcription fails its self-hash"):
        validate_record({**first, "self_hash": _sha("0")})


def test_custody_records_naming_an_absent_sample_are_refused(tmp_path):
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    first, second = _pair(sample, "alpha", "alpha")
    with pytest.raises(
        SchemaRefusal, match=r"transcription \w+ names sample \w+, but that sample is absent"
    ):
        validate_corpus([first], require_closure=False)
    with pytest.raises(
        SchemaRefusal, match=r"adjudication \w+ names sample \w+, but that sample is absent"
    ):
        validate_corpus([adjudicate(first, second)], require_closure=False)


def test_an_adjudication_refuses_transcriptions_of_different_samples(tmp_path):
    path, frame, pages = run_file(tmp_path)
    samples = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))
    first = transcribe(samples[0], _act(), "hand-a", "alpha")
    second = transcribe(samples[1], _act(), "hand-b", "alpha")
    with pytest.raises(SchemaRefusal, match="different gold samples"):
        adjudicate(first, second)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_is_refused_as_not_a_regular_file(tmp_path):
    fifo = tmp_path / "record.json"
    os.mkfifo(fifo)
    with pytest.raises(SchemaRefusal, match="is not a regular JSON file"):
        read_json(fifo)
