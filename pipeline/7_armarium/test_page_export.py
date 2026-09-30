"""The Armarium on a page-read run: acts, the other layer, page rows and page accounting.

The trees are the fixture's `happy` and `page-review` scenarios read with
`reading_unit = "page"`, the Recensor stood in for by
`conftest.publish_stand_in_page_reviews`. Every bundle is checked the way a
recipient would, by `verify_delivered_bundle` on a clean directory.
"""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from armarium_export import (
    EXPORT_MANIFEST_NAME,
    PAGE_NOT_MEASURED_INSTRUMENTS,
    _zip_bytes,
    verify_delivered_bundle,
    verify_export_bundle,
)

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ARCHETYPUS, ARMARIUM, PERLECTOR
from common.runtree.store import RunTree
from conftest import (
    build_page_tree,
    publish_stand_in_page_reviews,
    rewitness_stage_boundary,
    rewrite_page_answer_entry,
    run_stage,
)

RUN_ID = "r"


@pytest.fixture(scope="module")
def happy(tmp_path_factory) -> tuple[Path, Path]:
    return build_page_tree(tmp_path_factory.mktemp("happy"), "happy")


@pytest.fixture(scope="module")
def page_review(tmp_path_factory) -> tuple[Path, Path]:
    return build_page_tree(tmp_path_factory.mktemp("page-review"), "page-review")


@pytest.fixture(scope="module")
def complete(tmp_path_factory, happy) -> dict:
    """Happy with p1:1 read as `other` and no continuation: nothing held, one other reading."""
    root, protocol = _copy(happy, tmp_path_factory.mktemp("complete"))
    rewrite_page_answer_entry(root, RUN_ID, 1, 1, kind="other")
    rewrite_page_answer_entry(root, RUN_ID, 1, 2, continues_to_next_page=False)
    rewrite_page_answer_entry(root, RUN_ID, 2, 1, continues_from_previous_page=False)
    result = _export(root, protocol, "happy")
    assert result.returncode == 0, result.stderr
    return _bundle(root, tmp_path_factory.mktemp("complete-clean"))


def _copy(tree: tuple[Path, Path], base: Path) -> tuple[Path, Path]:
    root, protocol = tree
    shutil.copytree(root, base / "runs")
    return base / "runs", protocol


def _export(root: Path, protocol: Path, scenario: str, **outcomes: str):
    publish_stand_in_page_reviews(root, RUN_ID, scenario, protocol, outcomes=outcomes)
    for program in ("pipeline/6_archetypus/run.py", "pipeline/7_armarium/run.py"):
        result = run_stage(root, RUN_ID, scenario, program, perlector_protocol_config=protocol)
        if program.startswith("pipeline/6") and result.returncode != 0:
            return result
    return result


def _bundle(root: Path, clean: Path) -> dict:
    tree = RunTree(root, RUN_ID)
    [export] = [
        tree.read_artifact(ARMARIUM, "export", entry["artifact_id"])
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "export"
    ]
    data = tree.read_bytes(export["payload"]["bundle"]["reference"]["relative_path"])
    with ZipFile(BytesIO(data)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = verify_delivered_bundle(data, clean)
    established = {
        record["payload"]["act_key"]: record["payload"]
        for record in (
            tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])
            for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
            if entry["kind"] == "archetypus"
        )
    }
    return {
        "export": export,
        "data": data,
        "members": members,
        "manifest": manifest,
        "clean": clean,
        "established": established,
    }


def _jsonl(members: dict, name: str) -> dict[str, dict]:
    rows = [json.loads(line) for line in members[name].decode("utf-8").splitlines() if line]
    return {row["act_key"]: row for row in rows}


def _text_bundle(members: dict) -> str:
    [name] = [name for name in members if name.startswith("text/")]
    return members[name].decode("utf-8")


def _section_value(text: str, heading: str, field: str) -> object:
    lines = text.split("\n")
    start = lines.index(heading)
    at = lines.index(field, start)
    return json.loads(lines[at + 1])


def _unit_types(manifest: dict) -> dict:
    return manifest["claims"]["terminal_ledger"]["by_unit_type"]


# --- a complete page-read export --------------------------------------------------


def test_a_page_read_run_exports_its_acts_and_other_readings_complete(complete):
    manifest, members = complete["manifest"], complete["members"]
    claims = manifest["claims"]
    assert manifest["schema"] == "armarium-export-manifest.v9"
    assert claims["status"] == "complete" and manifest["aggregate"]["status"] == "complete"
    partition = claims["act_partition"]
    assert partition["denominator"] == "page-read reading acts"
    assert (partition["expected_count"], partition["counted"]) == (2, 2)
    assert sorted(partition["act_keys"].values()) == ["p1:2", "p2:1"]
    other = claims["other_readings"]
    assert (other["count"], other["counted_as_acts"], other["by_category"]) == (
        1,
        False,
        {"delivered": 1},
    )
    assert other["carried_by"] == ["jsonl", "text-bundle"]
    assert _unit_types(manifest) == {"source": 2, "page": 2, "act": 2, "other": 1}
    assert sorted(_jsonl(members, "acts.jsonl")) == ["p1:2", "p2:1"]
    assert sorted(_jsonl(members, "other.jsonl")) == ["p1:1"]
    text = _text_bundle(members)
    assert "## OTHER p1:1 (not an act)" in text and "## p1:1 " not in text
    export = complete["export"]["payload"]
    assert export["expected_acts"] == 2
    assert [item["act_key"] for item in export["other_readings"]] == ["p1:1"]


def test_the_page_accounting_and_what_was_not_measured_are_claimed(complete):
    claims = complete["manifest"]["claims"]
    accounting = claims["page_accounting"]
    assert [row["ordinal"] for row in accounting["pages"]] == [1, 2]
    assert accounting["held_pages"] == []
    assert set(accounting["pages"][0]["rules"]) == set("abcdefghi")
    # Rule (i) needs a record detector, which the fixture does not configure.
    assert accounting["pages"][0]["rules"] == {
        **dict.fromkeys("abcdefgh", "pass"),
        "i": "not-applicable",
    }
    assert len(accounting["policy_sha256s"]) == 1
    entries = {entry["instrument"]: entry for entry in claims["not_measured"]["entries"]}
    assert list(entries) == list(PAGE_NOT_MEASURED_INSTRUMENTS)
    assert entries["page-accounting-thresholds"]["status"] == "not-measured"
    assert entries["page-accounting-thresholds"]["detail"]["calibrated_for_this_corpus"] is False
    assert entries["perlector-pass-c"]["status"] == "declared-unproduced"
    assert entries["perlector-pass-c"]["detail"]["pages_audit_not_run"] == 2
    assert entries["lectio-nuda"]["status"] == "declared-unproduced"


def test_the_same_established_reading_appears_identically_in_every_format(complete):
    members, established = complete["members"], complete["established"]
    acts, others = _jsonl(members, "acts.jsonl"), _jsonl(members, "other.jsonl")
    text = _text_bundle(members)
    database = complete["clean"] / "acts.sqlite"
    with sqlite3.connect(database) as connection:
        stored = dict(connection.execute("SELECT act_key, canonical_clean_text FROM acts"))
    for key, record in established.items():
        if record["kind"] == "other":
            assert others[key]["canonical_clean_text"] == record["text"]
            assert others[key]["uncertainty"] == record["uncertainty"]
            heading = f"## OTHER {key} (not an act)"
            assert _section_value(text, heading, "other_text:") == record["text"]
            assert _section_value(text, heading, "other_uncertainty:") == record["uncertainty"]
            assert key not in acts and key not in stored
        else:
            assert acts[key]["canonical_clean_text"] == record["text"]
            assert stored[key] == record["text"]
            heading = f"## {key} ({record['act_id']})"
            assert _section_value(text, heading, "canonical_clean_text:") == record["text"]


# --- held readings and pages ------------------------------------------------------


def test_a_held_reading_is_a_review_item_with_its_reasons(page_review, tmp_path):
    root, protocol = _copy(page_review, tmp_path)
    result = _export(root, protocol, "page-review")
    assert result.returncode == 3, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    claims = bundle["manifest"]["claims"]
    items = _jsonl(bundle["members"], "review-items.jsonl")
    assert list(items) == ["p2:1"]
    assert items["p2:1"]["category"] == "held-for-review"
    assert "reading-unplaced" in items["p2:1"]["reason"]
    assert claims["page_accounting"]["held_pages"] == [2]
    assert "unread-ink" in claims["page_accounting"]["pages"][1]["hold_codes"]
    assert claims["status"] == "partial"
    assert "p2:1" not in bundle["established"]


def test_the_act_count_conserves_across_the_partition_the_formats_and_the_ledger(
    page_review, tmp_path
):
    root, protocol = _copy(page_review, tmp_path)
    _export(root, protocol, "page-review")
    bundle = _bundle(root, tmp_path / "clean")
    manifest, members = bundle["manifest"], bundle["members"]
    partition = manifest["claims"]["act_partition"]
    by_category = {row["category"]: row["count"] for row in partition["categories"]}
    assert sum(by_category.values()) == partition["expected_count"] == 3
    assert by_category["delivered"] == 2 and by_category["held-for-review"] == 1
    assert len(_jsonl(members, "acts.jsonl")) == 3
    assert _unit_types(manifest)["act"] == 3
    assert manifest["claims"]["other_readings"]["count"] == 0
    assert members["other.jsonl"] == b""


def _drop_page_entries(root: Path, ordinal: int) -> None:
    directory = root / RUN_ID / "4_perlector" / "artifacts"
    for kind in ("act-region", "perlectio"):
        for path in (directory / kind).glob("*.json"):
            if json.loads(path.read_text("utf-8"))["payload"]["page_ordinal"] == ordinal:
                path.unlink()


def _rewrite(root: Path, kind: str, ordinal: int, change) -> None:
    directory = root / RUN_ID / "4_perlector" / "artifacts" / kind
    [path] = [
        path
        for path in directory.glob("*.json")
        if json.loads(path.read_text("utf-8"))["payload"]["page_ordinal"] == ordinal
    ]
    record = json.loads(path.read_text("utf-8"))
    change(record)
    record["self_hash"] = self_hash(
        {key: value for key, value in record.items() if key != "self_hash"}
    )
    path.write_bytes(canonical_bytes(record))


def test_a_page_whose_answer_was_not_read_is_one_held_item_with_its_reasons(happy, tmp_path):
    root, protocol = _copy(happy, tmp_path)
    _drop_page_entries(root, 2)

    def malformed(record):
        record["outcome"] = "held"
        record["payload"].update(
            parse_state="malformed",
            answer=None,
            problems=[{"code": "json-invalid", "detail": "the reply is not JSON"}],
            disposition="held",
        )

    def held(record):
        record["outcome"] = "held"
        record["payload"]["holds"] = ["page-answer-incomplete"]

    _rewrite(root, "page-reading", 2, malformed)
    _rewrite(root, "page-accounting", 2, held)
    rewitness_stage_boundary(RunTree(root, RUN_ID), PERLECTOR)
    result = _export(root, protocol, "happy")
    assert result.returncode == 3, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    items = _jsonl(bundle["members"], "review-items.jsonl")
    assert list(items) == ["p2:unread"]
    assert "page-unread" in items["p2:unread"]["reason"]
    assert "json-invalid" in items["p2:unread"]["reason"]
    assert bundle["manifest"]["claims"]["page_accounting"]["held_pages"] == [2]
    ledger = bundle["manifest"]["claims"]["terminal_ledger"]
    [page_two] = [unit for unit in ledger["units"] if unit["unit_id"] == "page:2"]
    assert page_two["category"] == "held-for-review"


@pytest.mark.parametrize(
    ("outcome", "category", "exit_code"),
    [("confirmed-blank", "confirmed-blank", 0), (None, "held-for-review", 3)],
    ids=["confirmed", "unconfirmed"],
)
def test_a_page_read_as_blank_is_confirmed_blank_only_when_the_recensor_confirms_it(
    happy, tmp_path, outcome, category, exit_code
):
    root, protocol = _copy(happy, tmp_path)
    _drop_page_entries(root, 2)
    feed_dir = root / RUN_ID / "4_perlector" / "artifacts" / "page-feed"
    [feed] = [
        json.loads(path.read_text("utf-8"))["payload"]
        for path in feed_dir.glob("*.json")
        if json.loads(path.read_text("utf-8"))["payload"]["page_ordinal"] == 2
    ]
    ids = [unit["id"] for witness in feed["witnesses"] for unit in witness["units"]]
    ids += [line["id"] for line in feed["surya"]["lines"]]
    ids += [block["id"] for block in feed["surya"]["blocks"]]

    def blank(record):
        record["payload"]["answer"] = {
            "acts": [],
            "set_aside": [{"id": identifier, "reason": "blank paper"} for identifier in ids],
        }

    _rewrite(root, "page-reading", 2, blank)
    rewrite_page_answer_entry(root, RUN_ID, 1, 2, continues_to_next_page=False)
    outcomes = {"p2:blank": outcome} if outcome else {}
    result = _export(root, protocol, "happy", **outcomes)
    assert result.returncode == exit_code, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    partition = bundle["manifest"]["claims"]["act_partition"]
    [blank_row] = [row for row in partition["categories"] if row["category"] == category]
    assert partition["act_keys"][blank_row["act_ids"][0]] == "p2:blank"
    ledger = bundle["manifest"]["claims"]["terminal_ledger"]
    [page_two] = [unit for unit in ledger["units"] if unit["unit_id"] == "page:2"]
    assert page_two["category"] == category


def test_an_agreed_continuation_is_a_labelled_reconstruction_and_keeps_the_run_partial(
    happy, tmp_path
):
    root, protocol = _copy(happy, tmp_path)
    result = _export(root, protocol, "happy")
    assert result.returncode == 3, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    [reconstruction] = [
        json.loads(line) for line in bundle["members"]["reconstructions.jsonl"].splitlines()
    ]
    assert reconstruction["head_page_ordinal"] == 1 and reconstruction["tail_page_ordinal"] == 2
    [reason] = bundle["manifest"]["aggregate"]["reasons"]
    assert reason.startswith("continuation join") and "(reconstructed)" in reason


# --- the verifier recomputes every claim from the package's own sources ------------


def _tampered(complete: dict, change) -> bytes:
    members = copy.deepcopy(complete["members"])
    change(members)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    for row in manifest["members"]:
        row["sha256"] = digest_bytes(members[row["path"]])
        row["bytes"] = len(members[row["path"]])
    manifest["self_hash"] = self_hash(
        {key: value for key, value in manifest.items() if key != "self_hash"}
    )
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)
    return _zip_bytes(members)


def _claims(members: dict, change) -> None:
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    change(manifest["claims"])
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)


def _sources(members: dict, change) -> None:
    sources = json.loads(members["sources.json"])
    change(sources)
    members["sources.json"] = canonical_bytes(sources)


def _other_text(members: dict) -> None:
    [row] = [json.loads(line) for line in members["other.jsonl"].splitlines()]
    row["canonical_clean_text"] += " (edited)"
    members["other.jsonl"] = canonical_bytes(row) + b"\n"


def _held_page_one(members: dict) -> None:
    """Page 1 held in both the rows and the claim, consistently, over its delivered readings."""
    _sources(members, lambda s: s["page_accounting"][0].update(hold_codes=["unread-ink"]))

    def hold(claims):
        claims["page_accounting"]["pages"][0]["hold_codes"] = ["unread-ink"]
        claims["page_accounting"]["held_pages"] = [1]

    _claims(members, hold)


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (
            lambda m: _claims(m, lambda c: c["other_readings"].update(count=0)),
            "other-readings claim",
        ),
        (
            lambda m: _claims(m, lambda c: c["page_accounting"].update(held_pages=[1])),
            "page-accounting claim",
        ),
        (
            lambda m: _claims(
                m, lambda c: c["page_accounting"]["pages"][0]["rules"].update(e="fail")
            ),
            "page-accounting claim",
        ),
        (
            lambda m: _sources(
                m, lambda s: s["page_accounting"][0].update(hold_codes=["unread-ink"])
            ),
            "page-accounting claim",
        ),
        (
            lambda m: _sources(
                m,
                lambda s: s["other_outcomes"][0].update(
                    category="held-for-review", reason="edited", text_status=None
                ),
            ),
            "terminal ledger",
        ),
        (_other_text, "valid literal text hash"),
        (_held_page_one, "held by their page accounting yet delivered"),
    ],
    ids=[
        "other-count",
        "held-pages",
        "rule-status",
        "page-holds",
        "other-category",
        "other-text",
        "held-page-delivered",
    ],
)
def test_the_clean_verifier_recomputes_the_page_claims_and_refuses_a_tampered_one(
    complete, tmp_path, change, refusal
):
    with pytest.raises(SchemaRefusal, match=refusal):
        verify_export_bundle(_tampered(complete, change), tmp_path / "clean")
