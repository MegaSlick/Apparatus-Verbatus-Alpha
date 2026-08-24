"""Attack the R7a gold-record custody boundaries through real JSON records."""

from __future__ import annotations

import errno
import json
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import IncompatibleReuse, SchemaRefusal
from common.contracts.identities import act_id, page_id
from gold import cli
from gold.core import (
    DRAW_SCHEMA,
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
                    row["stratum"] == stratum and set_for_page(frame, row["sha256"]) == gold_set
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
        assert record["set"] == expected == set_for_page(frame, record["page"]["sha256"])
    # Attack the only way a page could cross sets: forge its stated membership.
    forged = dict(first[0])
    forged["set"] = "locked-acceptance" if forged["set"] == "calibration" else "calibration"
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="page-derived partition"):
        validate_sample(forged, path)


def test_same_page_bytes_at_two_ordinals_are_ranked_as_distinct_pages(tmp_path):
    """A repeated byte digest is two scanned pages when its ordinals differ.

    The sampler's rank includes that ordinal, and corpus validation must preserve
    the same identity instead of collapsing it back to byte content alone.
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
    gold_set = set_for_page(frame, pages[0]["sha256"])
    plan = {
        name: {"duplicate-scan": 2 if name == gold_set else 0}
        for name in ("calibration", "locked-acceptance")
    }

    selected = sample_stratified(path, rows, plan)

    assert {sample["page"]["ordinal"] for sample in selected} == {1, 2}
    ranks = [
        digest_bytes(
            canonical_bytes(
                {
                    "seed": frame["seed"],
                    "ordinal": page["ordinal"],
                    "page_sha256": page["sha256"],
                    "stratum": "duplicate-scan",
                    "purpose": "gold-sample",
                }
            )
        )
        for page in pages
    ]
    assert ranks[0] != ranks[1]
    assert validate_corpus(selected, path) == selected

    # Page identity binds the source ordinal as well as the source itself, so the
    # visually identical pages also derive distinct acts. Act-level custody must
    # admit one established reading for each rather than collapsing by page bytes.
    source_sha = _sha("f")
    by_ordinal = {sample["page"]["ordinal"]: sample for sample in selected}
    custody = []
    acts = []
    for ordinal in (1, 2):
        act = act_id(page_id(source_sha, ordinal), 0, {"x": 1})
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


def test_sample_refuses_a_page_or_frame_restated_differently_than_r0_authority(tmp_path):
    path, frame, pages = run_file(tmp_path)
    record = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    with pytest.raises(SchemaRefusal, match="outside the R0"):
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
        "selection_basis": "Tyrel B1 parish/condition stratification",
        "page": page,
        "set": set_for_page(frame, page["sha256"]),
    }
    result = ingest_manual_pick(path, pick)
    assert result["method"] == "manual"
    assert result["page"] == page
    assert result["claimed_set"] == result["set"] == pick["set"]


def test_manual_pick_predating_the_seed_is_still_ingested_with_an_honest_disagreement(tmp_path):
    """Tyrel's B1 picks are made in week one, before the R0 frame/seed exist, so his
    stated set can honestly disagree with the page-derived partition once it is
    known. Ingestion must not refuse and force a re-pick (that would discard real
    annotation hours); it must record the disagreement, never silently resolve it
    either way (GOVERNANCE 2)."""
    path, frame, pages = run_file(tmp_path)
    page = catalog(pages)[0]
    true_set = set_for_page(frame, page["sha256"])
    claimed_set = "locked-acceptance" if true_set == "calibration" else "calibration"
    pick = {
        "schema": MANUAL_PICK_SCHEMA,
        "selection_basis": "Tyrel B1 pick recorded before R0 froze",
        "page": page,
        "set": claimed_set,
    }
    result = ingest_manual_pick(path, pick)
    assert result["set"] == true_set
    assert result["claimed_set"] == claimed_set
    assert result["claimed_set"] != result["set"]
    # Disjointness stays enforced by construction: the persisted set is never the
    # disputed one, whatever the pick claimed.
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
                    "set": set_for_page(frame, page["sha256"]),
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
    it — the silent shortfall U18 and GOVERNANCE 2 forbid. Naming it with quota 0
    is the declared way to skip it."""
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
    # And the same coherence holds on read, not only at construction.
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
    measurement = bind_instrument(sample, act_id("pg_0123456789abcdef", 0, {"x": 1}), _sha("e"))
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
        "set": set_for_page(frame, page["sha256"]),
    }
    with pytest.raises(SchemaRefusal, match="width.*height.*Add the missing fields"):
        ingest_manual_pick(path, pick)

    sample = sample_stratified(path, rows, plan_for(frame, rows))[0]
    sample["page"].pop("height")
    with pytest.raises(SchemaRefusal, match="width.*height.*Regenerate.*preserve"):
        validate_sample(sample)


def test_dimension_bearing_records_use_a_new_schema_identity(tmp_path):
    """Adding dimensions changes self-hashed record meaning. The new reader must
    never reinterpret a dimensionless v1 record as its dimension-bearing format."""
    assert SAMPLE_SCHEMA == "gold-page-sample.v2"
    assert DRAW_SCHEMA == "gold-sampling-draw.v2"
    assert MANUAL_PICK_SCHEMA == "gold-manual-pick.v2"
    assert LAYOUT_SCHEMA == "gold-page-layout.v2"
    assert PADDING_SCHEMA == "gold-padding-rectangles.v2"

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
    with pytest.raises(
        SchemaRefusal, match="predates required page dimensions.*Preserve.*do not edit"
    ):
        validate_sample(legacy)
    with pytest.raises(
        SchemaRefusal, match="predates required page dimensions.*Preserve.*do not edit"
    ):
        validate_record(legacy)


def _act(index=0):
    return act_id("pg_0123456789abcdef", index, {"x": 1})


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
    """Reconciling two readings is not picking between them (hard rule 8): the
    adjudicator reads the ink and records what they read, which need not be either
    transcription, and both transcriptions are retained unaltered (GOVERNANCE 4)."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    first, second = _pair(sample, "Marie Anne Dubois", "Marie Anne Dubais", path)
    with pytest.raises(SchemaRefusal, match="reading they established from the ink"):
        adjudicate(first, second)
    record = adjudicate(first, second, adjudicator="hand-c", text="Marie Anne Duboís")
    assert record["outcome"] == "adjudicated"
    assert record["text"] not in {first["text"], second["text"]}
    assert record["transcriptions"] == [first, second]
    assert validate_adjudication(record) == record
    with pytest.raises(SchemaRefusal, match="cannot be its own reconciliation"):
        adjudicate(first, second, adjudicator="hand-a", text="Marie Anne Dubois")


def test_an_adjudication_cannot_assert_an_outcome_its_transcriptions_deny(tmp_path):
    """`outcome` is derived from the two readings, never taken on trust — the same
    discipline `set` gets. Resealing the self-hash launders neither."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    first, second = _pair(sample, "Jean Baptiste", "Jean Baptiste", path)
    agreed = adjudicate(first, second)
    forged = json.loads(json.dumps(agreed))
    forged["outcome"] = "adjudicated"
    forged["adjudicator"] = "hand-c"
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="the outcome is 'agreed'"):
        validate_adjudication(forged)
    differing = adjudicate(
        *_pair(sample, "Jean Baptiste", "Jean Batiste", path),
        adjudicator="hand-c",
        text="Jean Baptiste",
    )
    forged = json.loads(json.dumps(differing))
    forged["outcome"] = "agreed"
    forged["adjudicator"] = None
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="the outcome is 'adjudicated'"):
        validate_adjudication(forged)
    # A single transcriber cannot be both readings, and the two must be of one act.
    with pytest.raises(SchemaRefusal, match="not independent"):
        adjudicate(first, transcribe(sample, _act(), "hand-a", "Jean Baptiste", path))
    with pytest.raises(SchemaRefusal, match="different acts"):
        adjudicate(first, transcribe(sample, _act(3), "hand-b", "Jean Baptiste", path))


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
    deleting it used to let unrelated fragments on either side splice back together
    into "illegible" by accident, refusing a perfectly correct use of the token
    (`peril` + `[ILLEGIBLE]` + `legible` used to read as `perillegible`)."""
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
    one, were refused by name for a reason that was not true. Refusing a
    transcriber's real hours wrongly is the failure this module exists to avoid."""
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
    immutable, so the ambiguity could never be re-recorded out of Tyrel's hours."""
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
    reading = "Marie Anne"
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
    would make the measurement circular (GOVERNANCE 3, GOALS 2)."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    with pytest.raises(SchemaRefusal, match="pipeline identity, not a person"):
        transcribe(sample, _act(), _act(7), "Jean", path)
    first, second = _pair(sample, "Jean", "Jehan", path)
    with pytest.raises(SchemaRefusal, match="pipeline identity, not a person"):
        adjudicate(first, second, adjudicator=_act(7), text="Jean")


def test_a_page_layout_or_padding_record_may_not_be_empty(tmp_path):
    """A record whose annotation list is empty says nothing while reading as a
    completed annotation. A page with nothing on it is annotated `true-blank`, and
    a padding record with no rectangles has measured nothing to be calibrated
    against (GOVERNANCE 2)."""
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
    refused, and the file already there is never touched
    (`common/runtree/store.py::_publish_bytes`'s rule)."""
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

    def refuse_link(_source, _target):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(core_module.os, "link", refuse_link)
    with pytest.raises(SchemaRefusal, match="refuses hard links"):
        write_append_only(tmp_path / "records" / "one.json", {"example": "evidence"})


def _forge_sample_outside_authority(sample):
    forged = json.loads(json.dumps(sample))
    forged["page"]["sha256"] = _sha("9")
    forged["set"] = set_for_page(forged["frame"], forged["page"]["sha256"])
    without = {
        key: value for key, value in forged.items() if key not in {"sample_digest", "self_hash"}
    }
    forged["sample_digest"] = digest_bytes(canonical_bytes(without))
    forged["self_hash"] = self_hash(forged)
    return forged


def test_layout_and_padding_can_recheck_their_embedded_sample_against_run_authority(tmp_path):
    """A layout or padding record's embedded sample is only checked for internal
    self-consistency by default — it can restate a page/frame belonging to no real
    R0 run and still validate. Passing --run (validate_layout/validate_padding's
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
    with pytest.raises(SchemaRefusal, match="outside the R0"):
        validate_layout(layout, path)
    padding = {
        "schema": PADDING_SCHEMA,
        "sample": forged,
        "rectangles": [{"x": 0, "y": 0, "w": 1, "h": 1}],
        "calibrated_for_this_corpus": False,
    }
    padding["self_hash"] = self_hash(padding)
    assert validate_padding(padding) == padding
    with pytest.raises(SchemaRefusal, match="outside the R0"):
        validate_padding(padding, path)


def test_bind_instrument_can_recheck_its_sample_against_run_authority(tmp_path):
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    forged = _forge_sample_outside_authority(sample)
    act = act_id("pg_0123456789abcdef", 0, {"x": 1})
    assert bind_instrument(forged, act, _sha("e"))["sample_digest"] == forged["sample_digest"]
    with pytest.raises(SchemaRefusal, match="outside the R0"):
        bind_instrument(forged, act, _sha("e"), path)


def test_a_shared_manual_pick_has_one_set_across_three_frames(tmp_path):
    """The partition itself, not only the corpus validator, prevents F-O2.

    R0 gives each frame a different seed. The same manually picked page must still
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
                "set": set_for_page(bound_frame, page["sha256"]),
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
    for hand, reading in (("hand-a", "Marie Anne"), ("hand-b", "Marie Jeanne")):
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
    established.write_text("Marie Anne\n", encoding="utf-8")
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
        "Marie Anne",
        "Marie Jeanne",
    ]
    assert cli.main(["validate", str(adjudication)]) == 0
    assert cli.main(["validate-corpus", str(records), "--run", str(path)]) == 0


def test_corpus_refuses_orphaned_or_conflicting_adjudication_custody(tmp_path):
    """An adjudication must resolve the exact two independently stored readings.

    Self-hashing one record cannot establish collection custody: without this
    reconciliation an adjudication can embed transcriptions never retained as their
    own evidence, and two different adjudicators can establish two texts for one act
    while every record remains individually valid.
    """
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    first, second = _pair(sample, "Marie Anne", "Marie Jeanne", path)
    established = adjudicate(first, second, adjudicator="hand-c", text="Marie Anne")

    with pytest.raises(SchemaRefusal, match="absent as independent gold records"):
        validate_corpus([sample, established], path)
    assert validate_corpus([sample, first, second, established], path)

    conflict = adjudicate(first, second, adjudicator="hand-d", text="Marie Jeanne")
    with pytest.raises(SchemaRefusal, match="two conflicting adjudications"):
        validate_corpus([sample, first, second, established, conflict], path)

    revised = transcribe(sample, _act(), "hand-a", "Marie Annette", path)
    with pytest.raises(SchemaRefusal, match="supplied two transcription records"):
        validate_corpus([sample, first, revised], path)


def test_the_corpus_api_refuses_an_empty_collection():
    """The CLI already names an empty directory, but the public collection gate
    must not return success when called directly with nothing to establish."""
    with pytest.raises(SchemaRefusal, match="empty collection proves no custody.*Supply"):
        validate_corpus([])


def test_corpus_refuses_a_started_reading_chain_without_its_adjudication(tmp_path):
    """Deleting the established record used to leave one or both independent
    transcriptions in a corpus that still validated. A partial chain is legitimate
    while people work, but collection validation must name it as partial rather than
    let absence wear the same success as completed custody (GOVERNANCE 2)."""
    path, frame, pages = run_file(tmp_path)
    sample = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    first, second = _pair(sample, "Marie Anne", "Marie Jeanne", path)
    for partial in ([sample, first], [sample, first, second]):
        with pytest.raises(SchemaRefusal, match="custody chain is incomplete.*adjudicate"):
            validate_corpus(partial, path)
    established = adjudicate(first, second, adjudicator="hand-c", text="Marie Anne")
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
            "selection_basis": "Tyrel B1 pick of a page the seed did not draw",
            "page": never_drawn,
            "set": set_for_page(frame, never_drawn["sha256"]),
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
            "set": set_for_page(frame, page["sha256"]),
        },
    )


def test_corpus_refuses_a_manual_pick_that_contradicts_the_retained_catalog(tmp_path):
    """A seeded sample is reconciled against the catalog by its membership digest; a
    manual one was reconciled against nothing. The draw retains the *whole*
    normalized catalog, so the predeclared stratum and pixel size of every page a
    pick could name are sitting right beside it — and a pick that contradicted them
    passed every reader. A stratum nobody planned makes the stratification
    unmeasurable (GOVERNANCE 10), and an invented width makes "the rectangles are
    proven on-page" vacuous, because every rectangle fits a page said to be huge."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    draw, selected = build_sampling_draw(path, rows, plan_for(frame, rows))
    drawn = {(record["page"]["ordinal"], record["page"]["sha256"]) for record in selected}
    never_drawn = next(row for row in rows if (row["ordinal"], row["sha256"]) not in drawn)

    honest = _pick(path, frame, never_drawn, "Tyrel B1 pick")
    assert validate_corpus([draw, *selected, honest], path)

    restratified = _pick(path, frame, {**never_drawn, "stratum": "invented"}, "Tyrel B1 pick")
    assert validate_sample(restratified, path) == restratified  # well-formed alone
    with pytest.raises(SchemaRefusal, match="silently restratify.*Regenerate.*preserve"):
        validate_corpus([draw, *selected, restratified], path)

    enlarged = _pick(path, frame, {**never_drawn, "width": 999_999}, "Tyrel B1 pick")
    with pytest.raises(SchemaRefusal, match="rectangle boundary is therefore ambiguous.*preserve"):
        validate_corpus([draw, *selected, enlarged], path)
    # And the enlargement is what would otherwise have let a rectangle off the page.
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
    destination corpus to prevent, and the cross-record stratum check only caught it
    when the second pick also restratified the page.

    A manual record beside the *seeded* record for the same page stays admissible:
    the seed can land on a page Tyrel already picked in week one, and refusing that
    would strand a real corpus with no remedy short of discarding his recorded
    provenance (GOVERNANCE 2, 4)."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    draw, selected = build_sampling_draw(path, rows, plan_for(frame, rows))
    drawn = {(record["page"]["ordinal"], record["page"]["sha256"]) for record in selected}
    never_drawn = next(row for row in rows if (row["ordinal"], row["sha256"]) not in drawn)

    first = _pick(path, frame, never_drawn, "Tyrel B1 pick")
    again = _pick(path, frame, never_drawn, "Tyrel B1 pick, restated")
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
    """Act custody was keyed on `(sample_digest, act_identity)`, which reads as "one
    established reading per act *per sample record*". An act identity binds the page
    it was marked out on, so it names one act once — but a page legitimately carried
    by both a manual and a seeded sample record gave that one act two independent
    custody chains, each internally impeccable, with nothing but file order to choose
    between the two texts they established. That is a picker by omission (hard rule
    8) inside the corpus the pipeline is measured against, and two texts where
    GOVERNANCE 5 allows one."""
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    draw, selected = build_sampling_draw(path, rows, plan_for(frame, rows))
    seeded = selected[0]
    picked = _pick(path, frame, {**seeded["page"]}, "Tyrel B1 pick of a page the seed also drew")
    assert picked["sample_digest"] != seeded["sample_digest"]

    act = _act()
    records = [draw, *selected, picked]
    for sample, readings, established in (
        (picked, ("Jean Dupont", "Jean Dupond"), "Jean Dupont"),
        (seeded, ("Marie Cure", "Marie Curee"), "an entirely different reading"),
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

    # Four distinct transcribers, so no one of them reads the act twice and the
    # refusal has to come from the act's own custody rather than from a repeated
    # name: the act still cannot hold two independently established readings.
    records = [draw, *selected, picked]
    for sample, hands, readings, established in (
        (picked, ("hand-a", "hand-b"), ("Jean Dupont", "Jean Dupond"), "Jean Dupont"),
        (seeded, ("hand-d", "hand-e"), ("Marie Cure", "Marie Curee"), "another reading"),
    ):
        first = transcribe(sample, act, hands[0], readings[0], path)
        second = transcribe(sample, act, hands[1], readings[1], path)
        records += [
            first,
            second,
            adjudicate(first, second, adjudicator="hand-c", text=established),
        ]
    with pytest.raises(SchemaRefusal, match="exactly the two independent transcriptions"):
        validate_corpus(records, path)


def test_corpus_refuses_a_drawn_page_re_minted_under_another_method(tmp_path):
    """The membership check used to run one way only: every `stratified-seed`
    sample had to be a draw member, but nothing required every draw member to
    still be present as one. A page the seed genuinely chose could be re-minted
    as `manual` (with the matching page-derived `set` as its `claimed_set`, so it
    is individually well-formed) and disappear from the seeded accounting while
    `validate_corpus` kept reporting success -- a silent loss GOVERNANCE 2
    forbids."""
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


def test_corpus_lock_failures_are_named_before_any_record_is_written(tmp_path, monkeypatch):
    """Lock failure cannot fall through to an unlocked publication or a traceback;
    the refusal states the risk, the safe remedy, and that no evidence was written."""

    def refuse_lock(_descriptor, _operation):
        raise OSError("locking unavailable")

    monkeypatch.setattr(cli.fcntl, "flock", refuse_lock)
    with pytest.raises(SchemaRefusal, match="supports advisory locks.*no gold record was written"):
        with cli._locked_corpus(tmp_path):
            raise AssertionError("an unheld corpus lock must never yield")

    assert not list(tmp_path.glob("*.json"))


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
    reads that one directory, so drawn samples and picks share it by design — yet
    `verify-sampling` refused the whole directory the moment a pick appeared,
    accusing it of being "a sample that was not seed-selected" when it never said
    it was. The seeded members still reconcile exactly, because `sample_digest`
    binds `method`: a hand-picked page wearing `stratified-seed` is still refused."""
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
                "selection_basis": "Tyrel B1 pick, filed with the drawn corpus",
                "page": picked,
                "set": set_for_page(frame, picked["sha256"]),
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
    """`main()` raising the named refusal is only half of it: run as a program, an
    uncaught `SchemaRefusal` still reached the operator as a stack trace and exit 1.
    `pipeline/orchestrator/run.py`'s entry point settles the convention."""
    bad = tmp_path / "not-json.json"
    bad.write_text("{not valid json", encoding="utf-8")
    finished = subprocess.run(
        [sys.executable, "-m", "gold.cli", "validate", str(bad)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
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


def test_a_deeply_nested_gold_file_is_a_named_refusal_not_a_traceback(tmp_path):
    """json's scanner recurses per nesting level, so a deeply nested file raises
    `RecursionError` rather than `JSONDecodeError` — and `_records_in` reads every
    `*.json` in a directory, so one such file was enough to end `validate-corpus`
    in a traceback instead of a refusal naming the file.
    `common/runtree/store.py::_read_json` settled the convention."""
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
    """gold/cli.py's own JSON reading used to bypass core._read_json's SchemaRefusal
    wrapping, so a malformed catalog/plan/pick/record crashed with a raw parser
    traceback instead of a named refusal."""
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
    """JSON arrays and objects are legal input but unhashable in Python. Testing
    one directly for membership in an enum set used to raise raw `TypeError` before
    the CLI could state a `SchemaRefusal`. Every externally supplied enum checks its
    string shape first."""
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
