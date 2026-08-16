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
    ingest_manual_pick,
    sample_stratified,
    set_for_page,
    validate_layout,
    validate_measurement,
    validate_padding,
    validate_sample,
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
    result = {"calibration": {}, "locked-acceptance": {}}
    for gold_set in result:
        for stratum in {row["stratum"] for row in rows}:
            if any(
                row["stratum"] == stratum and set_for_page(frame, row["sha256"]) == gold_set
                for row in rows
            ):
                result[gold_set][stratum] = 1
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


def test_stratified_samples_carry_no_claimed_set(tmp_path):
    path, frame, pages = run_file(tmp_path)
    rows = catalog(pages)
    record = sample_stratified(path, rows, plan_for(frame, rows))[0]
    assert record["claimed_set"] is None


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
