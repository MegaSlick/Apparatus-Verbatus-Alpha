"""Attack the R7a gold-record custody boundaries through real JSON records."""

from __future__ import annotations

import errno
import json

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import act_id
from gold import cli
from gold.core import (
    LAYOUT_SCHEMA,
    MANUAL_PICK_SCHEMA,
    PADDING_SCHEMA,
    bind_instrument,
    build_sample,
    ingest_manual_pick,
    sample_stratified,
    set_for_page,
    validate_corpus,
    validate_layout,
    validate_measurement,
    validate_padding,
    validate_sample,
    verify_stratified_selection,
    write_append_only,
)


def _sha(character: str) -> str:
    return character * 64


def run_file(tmp_path):
    pages = [
        {"ordinal": ordinal, "sha256": _sha(character)}
        for ordinal, character in enumerate("12345678", 1)
    ]
    source = [{"ordinal": page["ordinal"], "sha256": page["sha256"]} for page in pages]
    frame = {
        "page_digest": digest_bytes(canonical_bytes(source)),
        "frame_digest": digest_bytes(canonical_bytes({"pages": source})),
        "seed": _sha("f"),
    }
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps({"source_manifest": pages, "corpus_frame_membership": frame}), encoding="utf-8"
    )
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
    assert all(record["set"] == set_for_page(frame, record["page"]["sha256"]) for record in first)
    # Attack the only way a page could cross sets: forge its stated membership.
    forged = dict(first[0])
    forged["set"] = "locked-acceptance" if forged["set"] == "calibration" else "calibration"
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="seed-derived partition"):
        validate_sample(forged, path)


def test_sample_refuses_a_page_or_frame_restated_differently_than_r0_authority(tmp_path):
    path, frame, pages = run_file(tmp_path)
    record = sample_stratified(path, catalog(pages), plan_for(frame, catalog(pages)))[0]
    forged = json.loads(json.dumps(record))
    forged["page"]["sha256"] = _sha("9")
    forged["set"] = set_for_page(forged["frame"], forged["page"]["sha256"])
    without = {
        key: value for key, value in forged.items() if key not in {"sample_digest", "self_hash"}
    }
    forged["sample_digest"] = digest_bytes(canonical_bytes(without))
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="outside the R0"):
        validate_sample(forged, path)
    broken_run = json.loads(path.read_text())
    broken_run["corpus_frame_membership"]["page_digest"] = _sha("0")
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
    stated set can honestly disagree with the seed-derived partition once it is
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
    set matching the seed-derived partition, self-hash intact. Only replaying the
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


def test_append_only_writer_refuses_overwrite(tmp_path):
    record = {"example": "evidence"}
    target = tmp_path / "records" / "one.json"
    write_append_only(target, record)
    with pytest.raises(ContractError, match="already exists"):
        write_append_only(target, record)


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


def test_a_gold_corpus_assembled_across_two_frames_is_refused(tmp_path):
    """Disjointness by construction holds inside one corpus frame, because `set` is
    a function of that frame's seed — and the seed is derived from the frame's own
    page digest. Add a page to the corpus and roughly half the pages change sides.
    Records from before and after both validate against their own frame, so only a
    corpus-level check can see that one page now sits in both sets."""
    first, frame, pages = run_file(tmp_path)
    wider = tmp_path / "wider.json"
    extra = [*pages, {"ordinal": 9, "sha256": _sha("9")}]
    source = [{"ordinal": page["ordinal"], "sha256": page["sha256"]} for page in extra]
    page_digest = digest_bytes(canonical_bytes(source))
    wider_frame = {
        "page_digest": page_digest,
        "frame_digest": digest_bytes(canonical_bytes({"pages": source})),
        "seed": _sha("e"),
    }
    wider.write_text(
        json.dumps({"source_manifest": extra, "corpus_frame_membership": wider_frame}),
        encoding="utf-8",
    )
    flipped = [
        page
        for page in pages
        if set_for_page(frame, page["sha256"]) != set_for_page(wider_frame, page["sha256"])
    ]
    assert flipped, "the two frames must disagree about some page for this to be a test"
    page = {**flipped[0], "stratum": "adverse"}
    before = ingest_manual_pick(
        first,
        {
            "schema": MANUAL_PICK_SCHEMA,
            "selection_basis": "picked before the corpus grew",
            "page": page,
            "set": set_for_page(frame, page["sha256"]),
        },
    )
    after = ingest_manual_pick(
        wider,
        {
            "schema": MANUAL_PICK_SCHEMA,
            "selection_basis": "picked after the corpus grew",
            "page": page,
            "set": set_for_page(wider_frame, page["sha256"]),
        },
    )
    assert before["set"] != after["set"]
    assert validate_corpus([before]) and validate_corpus([after])
    with pytest.raises(SchemaRefusal, match="different corpus frames"):
        validate_corpus([before, after])


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
    assert len(written) == sum(sum(quotas.values()) for quotas in plan.values())
    assert cli.main(["verify-sampling", str(output), *common]) == 0
    written[0].unlink()
    with pytest.raises(SchemaRefusal, match="does not replay"):
        cli.main(["verify-sampling", str(output), *common])


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
