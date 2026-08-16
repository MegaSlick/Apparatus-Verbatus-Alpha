"""Attack the R7a gold-record custody boundaries through real JSON records."""

from __future__ import annotations

import errno
import json
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import IncompatibleReuse, SchemaRefusal
from common.contracts.identities import act_id
from gold import cli
from gold.core import (
    ILLEGIBLE,
    LAYOUT_SCHEMA,
    MANUAL_PICK_SCHEMA,
    PADDING_SCHEMA,
    adjudicate,
    bind_instrument,
    build_sample,
    build_sampling_draw,
    ingest_manual_pick,
    sample_stratified,
    set_for_page,
    transcribe,
    validate_adjudication,
    validate_corpus,
    validate_layout,
    validate_measurement,
    validate_padding,
    validate_sample,
    validate_sampling_draw,
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
    assert all(record["set"] == set_for_page(frame, record["page"]["sha256"]) for record in first)
    # Attack the only way a page could cross sets: forge its stated membership.
    forged = dict(first[0])
    forged["set"] = "locked-acceptance" if forged["set"] == "calibration" else "calibration"
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="page-derived partition"):
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
    for rejected in ("", "   ", "parrain [illegible]", "parrain (ILLEGIBLE?)", "ILLEGIBLE"):
        with pytest.raises(SchemaRefusal, match="empty|reserved"):
            transcribe(sample, _act(), "hand-a", rejected, path)


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
    with pytest.raises(SchemaRefusal, match="supplied two different readings"):
        validate_corpus([sample, first, revised], path)


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
    assert cli.main(["verify-sampling", str(output), "--run", str(path)]) == 0
    sample_file = next(
        item for item in written if json.loads(item.read_text())["schema"] == "gold-page-sample.v1"
    )
    sample_file.unlink()
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
