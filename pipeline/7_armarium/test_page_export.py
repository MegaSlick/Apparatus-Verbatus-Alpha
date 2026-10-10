"""The Armarium on a page-read run: acts, the other layer, page rows and page accounting.

The trees are the fixture's `happy`, `page-review`, `page-other` and
`page-no-act` scenarios read by page and reviewed by the
real Recensor. A reader that answered a page otherwise
is a scenario of its own (`proof/build_fixture.py`, `PAGE_ANSWER_VARIANTS`),
since every later stage reads the answer again from what the reader said. A
test that needs a decision the Recensor does not make on the fixture forges it
(`conftest.forge_page_review`, `conftest.forge_continuation_links`) and says
why. Every bundle is checked the way a recipient would, by
`verify_delivered_bundle` on a clean directory.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import shutil
import sqlite3
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from armarium_export import (
    EXPORT_MANIFEST_NAME,
    NOT_MEASURED_INSTRUMENTS,
    _zip_bytes,
    verify_delivered_bundle,
    verify_export_bundle,
)

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.stages import ARCHETYPUS, ARMARIUM, RECENSOR
from common.page_accounting import load_page_accounting_policy
from common.page_review import held_by_recensor
from common.runtree.store import RunTree
from common.stage import PAGE_BLANK_HOLD
from conftest import (
    ROOT,
    advance_held_recensor,
    build_page_tree,
    forge_continuation_links,
    forge_page_review,
    load_stage,
    programs_through,
    reask_recovery_config,
    run_stage,
)

RUN_ID = "r"


@pytest.fixture(scope="module")
def happy(tmp_path_factory) -> tuple[Path, dict]:
    return build_page_tree(tmp_path_factory.mktemp("happy"), "happy")


@pytest.fixture(scope="module")
def page_review(tmp_path_factory) -> tuple[Path, dict]:
    return build_page_tree(tmp_path_factory.mktemp("page-review"), "page-review")


@pytest.fixture(scope="module")
def page_other(tmp_path_factory) -> tuple[Path, dict]:
    """a1 read as `other`, over which the record detector finds no record."""
    return build_page_tree(tmp_path_factory.mktemp("page-other"), "page-other")


@pytest.fixture(scope="module")
def no_act(tmp_path_factory) -> tuple[Path, dict]:
    """Page 2 read as one `other` entry; the record detector finds nothing there, so DAI saw
    nothing on it."""
    return build_page_tree(tmp_path_factory.mktemp("page-no-act"), "page-no-act")


@pytest.fixture(scope="module")
def page_blank(tmp_path_factory) -> tuple[Path, dict]:
    """Page 2 read as blank paper, with no act running onto it."""
    return build_page_tree(tmp_path_factory.mktemp("page-blank"), "page-blank")


@pytest.fixture(scope="module")
def runs_past_end(tmp_path_factory) -> tuple[Path, dict]:
    """Nothing runs across the page break; page 2's act says it runs on past the last page."""
    return build_page_tree(tmp_path_factory.mktemp("page-runs-past-end"), "page-runs-past-end")


@pytest.fixture(scope="module")
def complete(tmp_path_factory) -> dict:
    """p1:1 read as `other` and no continuation: nothing held, one other reading."""
    root, options = build_page_tree(tmp_path_factory.mktemp("complete"), "page-other-unbroken")
    result = _export(root, options, "page-other-unbroken")
    assert result.returncode == 0, result.stderr
    return _bundle(root, tmp_path_factory.mktemp("complete-clean"))


def _copy(tree: tuple[Path, dict], base: Path) -> tuple[Path, dict]:
    root, options = tree
    shutil.copytree(root, base / "runs")
    return base / "runs", options


def _recense(root: Path, options: dict, scenario: str) -> None:
    result = run_stage(root, RUN_ID, scenario, "pipeline/5_recensor/run.py", **options)
    assert result.returncode in (0, 3), result.stderr


def _after_recensor(root: Path, options: dict, scenario: str):
    """The Archetypus, then (when it completes) the Coniector and the Armarium; the last result.

    A Recensor that holds anything is first advanced, as a person would to
    export with every hold named; neither stage runs past it otherwise.
    """
    if held_by_recensor(RunTree(root, RUN_ID)):
        advance_held_recensor(root, RUN_ID)
    for program in (
        "pipeline/6_archetypus/run.py",
        "pipeline/4b_coniector/run.py",
        "pipeline/7_armarium/run.py",
    ):
        result = run_stage(root, RUN_ID, scenario, program, **options)
        if program.startswith("pipeline/6") and result.returncode != 0:
            return result
    return result


def _export(root: Path, options: dict, scenario: str):
    """The real Recensor, the Archetypus and the Armarium."""
    _recense(root, options, scenario)
    return _after_recensor(root, options, scenario)


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
    assert manifest["schema"] == "armarium-export-manifest.v13"
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
    assert text.split("\n")[1:4] == [
        "run-status: complete",
        f"lot: {manifest['run']['lot']}",
        "folder-readings: 3 delivered, 0 not delivered",
    ]
    assert "## NOT DELIVERED" not in text
    export = complete["export"]["payload"]
    assert export["expected_acts"] == 2
    assert [item["act_key"] for item in export["other_readings"]] == ["p1:1"]


def test_the_page_accounting_and_what_was_not_measured_are_claimed(complete):
    claims = complete["manifest"]["claims"]
    accounting = claims["page_accounting"]
    assert [row["ordinal"] for row in accounting["pages"]] == [1, 2]
    assert accounting["held_pages"] == []
    assert set(accounting["pages"][0]["rules"]) == set("abcdefghij")
    # DAI's one record on page 1 lies inside a2's act region; no page was re-asked.
    assert accounting["pages"][0]["rules"] == {
        **dict.fromkeys("abcdefghi", "pass"),
        "j": "not-applicable",
    }
    assert len(accounting["policy_sha256s"]) == 1
    entries = {entry["instrument"]: entry for entry in claims["not_measured"]["entries"]}
    assert list(entries) == list(NOT_MEASURED_INSTRUMENTS)
    assert entries["page-accounting-thresholds"]["status"] == "not-measured"
    assert entries["page-accounting-thresholds"]["detail"]["calibrated_for_this_corpus"] is False
    assert entries["perlector-pass-c"]["status"] == "declared-unproduced"
    assert entries["perlector-pass-c"]["detail"]["pages_audit_not_run"] == 2
    assert entries["comparison-bounds"]["status"] == "measured"
    assert entries["comparison-bounds"]["detail"]["acts_with_unmeasured_comparison"] == 0
    assert entries["comparison-bounds"]["detail"]["unmeasured_act_ids"] == []


def test_a_delivered_act_whose_dissent_stopped_on_its_budget_is_disclosed(tmp_path):
    """A run sealed with a one-step dissent budget delivers acts whose comparisons
    stopped (`compared: "unknown"`); the export names each such act and does not
    call the comparison measured."""
    shipped = (ROOT / "config" / "alignment.toml").read_text(encoding="utf-8")
    sealed_line = "max_comparison_steps = 100000000\n"
    assert shipped.count(sealed_line) == 1
    alignment = tmp_path / "config" / "alignment.toml"
    alignment.parent.mkdir()
    alignment.write_text(shipped.replace(sealed_line, "max_comparison_steps = 1\n"))
    root, options = build_page_tree(
        tmp_path / "tree", "page-other-unbroken", alignment_config=alignment
    )
    result = _export(root, options, "page-other-unbroken")
    assert result.returncode == 0, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    delivered = sorted(row["act_id"] for row in _jsonl(bundle["members"], "acts.jsonl").values())

    claims = bundle["manifest"]["claims"]["not_measured"]["entries"]
    bounds = {row["instrument"]: row for row in claims}["comparison-bounds"]
    assert bounds["status"] == "not-measured"
    assert bounds["detail"] == {
        "sealed_max_comparison_steps": 1,
        "max_comparison_character_pairs": 100_000_000,
        "acts_delivered": 2,
        "acts_with_unmeasured_comparison": 2,
        "unmeasured_act_ids": delivered,
    }


def test_the_verifier_refuses_an_unmeasured_comparison_on_an_act_it_does_not_deliver(
    complete, tmp_path
):
    """The comparison-bounds claim may name only delivered acts as unmeasured; a
    manifest that blames an act outside the package, its count and status kept
    consistent with it, is refused."""

    def blame_an_undelivered_act(claims):
        block = claims["not_measured"]
        [entry] = [row for row in block["entries"] if row["instrument"] == "comparison-bounds"]
        entry["detail"].update(
            acts_with_unmeasured_comparison=1, unmeasured_act_ids=["act_not_in_this_package"]
        )
        entry["status"] = "not-measured"
        block["count"] += 1

    data = _tampered(complete, lambda members: _claims(members, blame_an_undelivered_act))
    with pytest.raises(SchemaRefusal, match="names an unmeasured act the package does not"):
        verify_export_bundle(data, tmp_path / "clean")


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
    root, options = _copy(page_review, tmp_path)
    result = _export(root, options, "page-review")
    assert result.returncode == 3, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    claims = bundle["manifest"]["claims"]
    items = _jsonl(bundle["members"], "review-items.jsonl")
    assert list(items) == ["p2:1"]
    assert items["p2:1"]["category"] == "held-for-review"
    assert "reading-unplaced" in items["p2:1"]["reason"]
    assert claims["page_accounting"]["held_pages"] == [2]
    # The ink threshold is a review flag under the committed `[flags]`: rule (f) says
    # `flag`, the page is held by its other codes, and the flagged layer carries the
    # reading's text with both lists.
    page_two = claims["page_accounting"]["pages"][1]
    assert "unread-ink" not in page_two["hold_codes"] and page_two["rules"]["f"] == "flag"
    flagged = _jsonl(bundle["members"], "flagged.jsonl")
    assert list(flagged) == ["p2:1"]
    assert flagged["p2:1"]["status"] == "not-established"
    assert flagged["p2:1"]["flag_codes"] == ["residual-ink", "unread-ink"]
    assert "reading-unplaced" in flagged["p2:1"]["hold_codes"]
    assert flagged["p2:1"]["text_label"] == "model reading, not established"
    assert isinstance(flagged["p2:1"]["text"], str) and flagged["p2:1"]["text"]
    assert flagged["p2:1"]["review_priority"] == 1
    assert claims["status"] == "partial"
    assert "p2:1" not in bundle["established"]


def test_the_act_count_conserves_across_the_partition_the_formats_and_the_ledger(
    page_review, tmp_path
):
    root, options = _copy(page_review, tmp_path)
    _export(root, options, "page-review")
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


def test_a_page_whose_answer_was_not_read_is_one_held_item_with_its_reasons(tmp_path):
    # Page 2's reply is not JSON.
    root, options = build_page_tree(tmp_path, "page-unread")
    result = _export(root, options, "page-unread")
    assert result.returncode == 3, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    items = _jsonl(bundle["members"], "review-items.jsonl")
    assert list(items) == ["p2:unread"]
    assert "page-unread" in items["p2:unread"]["reason"]
    assert "not-json" in items["p2:unread"]["reason"]
    assert bundle["manifest"]["claims"]["page_accounting"]["held_pages"] == [2]
    ledger = bundle["manifest"]["claims"]["terminal_ledger"]
    [page_two] = [unit for unit in ledger["units"] if unit["unit_id"] == "page:2"]
    assert page_two["category"] == "held-for-review"
    _assert_no_entry_reads_nothing(bundle, "p2:unread")


@pytest.mark.parametrize(
    ("confirmed", "category", "exit_code"),
    [(True, "confirmed-blank", 0), (False, "held-for-review", 3)],
    ids=["confirmed", "unconfirmed"],
)
def test_a_page_read_as_blank_is_confirmed_blank_only_when_the_recensor_confirms_it(
    page_blank, tmp_path, confirmed, category, exit_code
):
    # Page 2 read as blank paper, every id on it set aside.
    root, options = _copy(page_blank, tmp_path)
    _recense(root, options, "page-blank")
    if confirmed:
        # Page 2 carries ink, Surya lines and witness text, so the real Recensor
        # never confirms it blank.
        forge_page_review(
            root,
            RUN_ID,
            "p2:blank",
            "confirmed-blank",
            hold_codes=[],
            release={"hold_codes": [PAGE_BLANK_HOLD], "reason": "confirmed"},
        )
    result = _after_recensor(root, options, "page-blank")
    assert result.returncode == exit_code, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    partition = bundle["manifest"]["claims"]["act_partition"]
    [blank_row] = [row for row in partition["categories"] if row["category"] == category]
    assert partition["act_keys"][blank_row["act_ids"][0]] == "p2:blank"
    ledger = bundle["manifest"]["claims"]["terminal_ledger"]
    [page_two] = [unit for unit in ledger["units"] if unit["unit_id"] == "page:2"]
    assert page_two["category"] == category
    _assert_no_entry_reads_nothing(bundle, "p2:blank")


def _assert_no_entry_reads_nothing(bundle: dict, key: str) -> None:
    """A row standing for no entry names no reading and counts in neither re-ask total."""
    assert _jsonl(bundle["members"], "acts.jsonl")[key]["reading"] is None
    reask = bundle["manifest"]["claims"]["reask"]
    assert reask["pages"][1] == {"ordinal": 2, "first_reading_acts": 0, "read_on_reask_acts": 0}
    assert reask["read_on_reask_acts"] == 0


def test_an_agreed_continuation_is_joined_by_no_code_and_keeps_the_run_partial(happy, tmp_path):
    root, options = _copy(happy, tmp_path)
    result = _export(root, options, "happy")
    assert result.returncode == 3, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    assert "reconstructions.jsonl" not in bundle["members"]
    [join] = json.loads(bundle["members"]["sources.json"])["continuation_joins"]
    assert (join["head_page_ordinal"], join["tail_page_ordinal"]) == (1, 2)
    assert join["not_reconstructed_reason"] == "no-code-join"
    [reason] = bundle["manifest"]["aggregate"]["reasons"]
    assert reason.startswith("continuation join") and "(no-code-join)" in reason


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


def _other_named_as_an_act(members: dict) -> None:
    """The other reading's key made an act's, in its row and ledger unit alike."""
    _sources(members, lambda s: s["other_outcomes"][0].update(act_key="p1:2"))

    def rename(claims):
        for unit in claims["terminal_ledger"]["units"]:
            if unit["unit_id"].startswith("other:"):
                unit["act_key"] = "p1:2"

    _claims(members, rename)


def _other_doubt(members: dict) -> None:
    """other.jsonl's doubt edited, still a valid layer over its unchanged literal."""
    [row] = [json.loads(line) for line in members["other.jsonl"].splitlines()]
    [span] = row["uncertainty"]["uncertain_spans"]
    span["confidence"] = "high" if span["confidence"] != "high" else "low"
    members["other.jsonl"] = canonical_bytes(row) + b"\n"


def _held_page_one(members: dict) -> None:
    """Page 1 held in both the rows and the claim, consistently, over its delivered readings."""
    _sources(members, lambda s: s["page_accounting"][0].update(hold_codes=["unread-ink"]))

    def hold(claims):
        claims["page_accounting"]["pages"][0]["hold_codes"] = ["unread-ink"]
        claims["page_accounting"]["held_pages"] = [1]

    _claims(members, hold)


def _relabel_jsonl_act(members: dict) -> None:
    """acts.jsonl's first act relabelled as read on re-ask, every other carrier unchanged."""
    rows = [json.loads(line) for line in members["acts.jsonl"].splitlines()]
    rows[0]["reading"] = "read on re-ask"
    members["acts.jsonl"] = b"".join(canonical_bytes(row) + b"\n" for row in rows)


def _move_act_reading_to_page_one(members: dict) -> None:
    """Page 2's act counted on page 1, in the source rows and the claim alike."""

    def move(sources):
        [row] = [row for row in sources["act_readings"] if row["act_key"] == "p2:1"]
        row["page_ordinal"] = 1

    _sources(members, move)

    def recount(claims):
        pages = claims["reask"]["pages"]
        pages[0]["first_reading_acts"] += 1
        pages[1]["first_reading_acts"] -= 1

    _claims(members, recount)


def _unlabel_text_bundle_act(members: dict) -> None:
    [name] = [name for name in members if name.startswith("text/")]
    members[name] = members[name].replace(b"reading: first reading\n", b"", 1)


def _page_roster_narrowed(members: dict) -> None:
    """The page witness chairs narrowed by one chair, in the basis and the manifest."""
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    roster = sorted(manifest["witness_chairs"])[:-1]
    manifest["aggregate_basis"]["page_witness_chairs"] = roster
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)
    _sources(members, lambda s: s["aggregate_basis"].update(page_witness_chairs=roster))


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
            # A held other reading is an aggregate reason, so the aggregate refuses first.
            "aggregate does not match its measured accounting basis",
        ),
        (_other_text, "valid literal text hash"),
        (_held_page_one, "held by their page accounting yet delivered"),
        (_other_named_as_an_act, "other reading is counted in the act partition"),
        (_other_doubt, "formats carrying the other layer disagree"),
        (_page_roster_narrowed, "disagrees with the exported roster"),
        (
            lambda m: _claims(m, lambda c: c["reask"].update(read_on_reask_acts=1)),
            "re-ask claim does not follow",
        ),
        (
            lambda m: _sources(m, lambda s: s["act_readings"][0].update(reading="second")),
            "a reading other than",
        ),
        (
            lambda m: _sources(m, lambda s: s["act_readings"].pop()),
            "act readings do not reconcile to the manifest act partition",
        ),
        (
            lambda m: _sources(m, lambda s: s["act_readings"][0].update(reading="read on re-ask")),
            "does not name the reading",
        ),
        (
            lambda m: _sources(m, lambda s: s["act_readings"][0].update(reading=None)),
            "null for a row with no entry",
        ),
        (_move_act_reading_to_page_one, "sealed page its key names"),
        (_relabel_jsonl_act, "acts JSONL does not name the reading"),
        (_unlabel_text_bundle_act, "text-bundle act does not name the reading"),
    ],
    ids=[
        "other-count",
        "held-pages",
        "rule-status",
        "page-holds",
        "other-category",
        "other-text",
        "held-page-delivered",
        "other-as-act",
        "formats-disagree",
        "page-roster",
        "reask-count",
        "unknown-reading",
        "reading-dropped",
        "relabelled-source",
        "entry-without-reading",
        "moved-page",
        "relabelled-jsonl",
        "unlabelled-text",
    ],
)
def test_the_clean_verifier_recomputes_the_page_claims_and_refuses_a_tampered_one(
    complete, tmp_path, change, refusal
):
    with pytest.raises(SchemaRefusal, match=refusal):
        verify_export_bundle(_tampered(complete, change), tmp_path / "clean")


# --- page-path accounting a delivered act alone does not show ------------------------


def _unit(manifest: dict, unit_id: str) -> dict:
    [unit] = [u for u in manifest["claims"]["terminal_ledger"]["units"] if u["unit_id"] == unit_id]
    return unit


def test_a_held_other_reading_keeps_the_run_partial(tmp_path):
    # The page-review scenario's held entry read as `other`.
    root, options = build_page_tree(tmp_path, "page-review-other")
    result = _export(root, options, "page-review-other")
    assert result.returncode == 3, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    claims = bundle["manifest"]["claims"]
    assert claims["other_readings"]["by_category"] == {"held-for-review": 1}
    assert claims["act_partition"]["counted"] == 2
    assert claims["status"] == "partial"
    assert _unit(bundle["manifest"], f"other:{claims['other_readings']['act_ids'][0]}")[
        "category"
    ] == ("held-for-review")
    # The held reading reaches the review queue under its own kind, and the
    # partial reasons name it by key, so a person sees it wherever they look.
    [other] = _jsonl(bundle["members"], "other.jsonl").values()
    review = _jsonl(bundle["members"], "review-items.jsonl")
    assert (review[other["act_key"]]["kind"], review[other["act_key"]]["category"]) == (
        "other",
        "held-for-review",
    )
    assert [
        reason
        for reason in claims["partial_reasons"]
        if reason.startswith(f"other {other['act_key']} is held-for-review: ")
    ]
    assert f"## NOT DELIVERED {other['act_key']} ({other['act_id']})\nnot-delivered: other " in (
        _text_bundle(bundle["members"])
    )

    def drop_other(members: dict) -> None:
        rows = members["review-items.jsonl"].decode("utf-8").splitlines(keepends=True)
        members["review-items.jsonl"] = "".join(
            row for row in rows if json.loads(row)["kind"] != "other"
        ).encode("utf-8")

    with pytest.raises(SchemaRefusal, match="held and refused readings"):
        verify_export_bundle(_tampered(bundle, drop_other), tmp_path / "dropped")


def test_a_confirmed_no_act_page_is_delivered_while_its_one_sided_break_holds_the_run(
    no_act, tmp_path
):
    # The fixture's page-no-act scenario: page 2's answer names one `other`
    # entry, which says it runs on from page 1, and DAI saw nothing on the page.
    root, options = _copy(no_act, tmp_path)
    result = _export(root, options, "page-no-act")
    assert result.returncode == 3, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    manifest = bundle["manifest"]
    page_two = _unit(manifest, "page:2")
    assert page_two["category"] == "delivered"
    assert "confirmed it carries no act" in page_two["reason"]
    assert manifest["claims"]["other_readings"]["by_category"] == {"delivered": 1}
    assert bundle["established"]["p2:1"]["kind"] == "other"
    # Page 1's last act says it runs on and page 2 has no act to join.
    [join] = json.loads(bundle["members"]["sources.json"])["continuation_joins"]
    assert join["status"] == "not-reconstructed"
    # The Recensor's note on the `other` entry's flag reaches its manifest entry.
    tree = RunTree(root, RUN_ID)
    entries = [
        tree.read_artifact(ARMARIUM, "manifest-entry", item["artifact_id"])["payload"]
        for item in tree.build_manifest(ARMARIUM)["artifacts"]
        if item["kind"] == "manifest-entry"
    ]
    [entry] = [entry for entry in entries if entry["act_key"] == "p2:1"]
    assert entry["review_notes"] == [
        {"code": "continuation-flag-on-other", "flags": ["continues_from_previous_page"]}
    ]


def test_a_confirmed_no_act_page_delivers_its_other_readings_and_completes(tmp_path):
    # `page-no-act` with nothing running across the break; the real Recensor
    # confirms the page holds no act.
    root, options = build_page_tree(tmp_path, "page-no-act-unbroken")
    result = _export(root, options, "page-no-act-unbroken")
    assert result.returncode == 0, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    manifest = bundle["manifest"]
    assert manifest["claims"]["status"] == "complete"
    assert manifest["aggregate"]["reasons"] == []
    page_two = _unit(manifest, "page:2")
    assert page_two["category"] == "delivered"
    assert "confirmed it carries no act" in page_two["reason"]
    assert manifest["claims"]["other_readings"]["by_category"] == {"delivered": 1}
    assert bundle["established"]["p2:1"]["kind"] == "other"
    assert sorted(_jsonl(bundle["members"], "other.jsonl")) == ["p2:1"]


def test_a_typed_index_page_holding_a_detector_record_is_confirmed_and_counts_no_act(tmp_path):
    # Page 1 handwritten register acts read as an act and an instrument; page 2 a
    # typed index whose one row holds the record detector's record there. Rule (i)
    # does not apply to an index page, so the row is confirmed, not held on it.
    root, options = build_page_tree(tmp_path, "page-typed-index")
    _recense(root, options, "page-typed-index")
    tree = RunTree(root, RUN_ID)
    reviews = {
        record["payload"]["act_key"]: record
        for record in (
            tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            for entry in tree.build_manifest(RECENSOR)["artifacts"]
            if entry["kind"] == "review"
        )
    }
    row = reviews["p2:1"]
    assert row["outcome"] == "accepted", row["payload"]["reason"]
    confirmation = row["payload"]["confirmation"]
    assert confirmation["confirmed"] is True and confirmation["rules"]["i"] == "hold"
    assert (
        "rule (i) does not apply to the page's stated type" in (row["payload"]["release"]["reason"])
    )
    result = _after_recensor(root, options, "page-typed-index")
    assert result.returncode == 0, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    manifest, members = bundle["manifest"], bundle["members"]
    assert manifest["claims"]["status"] == "complete"
    partition = manifest["claims"]["act_partition"]
    assert (partition["expected_count"], partition["counted"]) == (2, 2)
    assert sorted(_jsonl(members, "acts.jsonl")) == ["p1:1", "p1:2"]
    assert sorted(_jsonl(members, "other.jsonl")) == ["p2:1"]
    established = bundle["established"]
    assert (established["p1:2"]["kind"], established["p2:1"]["kind"]) == ("act", "other")
    [page_two] = [
        page for page in manifest["claims"]["page_accounting"]["pages"] if page["ordinal"] == 2
    ]
    assert page_two["hold_codes"] == [] and page_two["rules"]["i"] == "hold"


def test_a_link_whose_flags_disagree_is_a_join_that_reconstructs_nothing(tmp_path):
    root, options = build_page_tree(tmp_path, "page-flags-disagree")
    result = _export(root, options, "page-flags-disagree")
    assert result.returncode == 3, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    [join] = json.loads(bundle["members"]["sources.json"])["continuation_joins"]
    assert (join["status"], join["not_reconstructed_reason"]) == (
        "not-reconstructed",
        "flags-disagree",
    )
    assert "reconstructions.jsonl" not in bundle["members"]
    [reason] = bundle["manifest"]["aggregate"]["reasons"]
    assert "(not-reconstructed)" in reason and "flags-disagree" in reason


def test_a_break_with_no_act_on_one_side_is_a_join_that_names_no_act(runs_past_end, tmp_path):
    # The last page's act runs on past the run's last page.
    root, options = _copy(runs_past_end, tmp_path)
    result = _export(root, options, "page-runs-past-end")
    assert result.returncode == 3, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    [join] = json.loads(bundle["members"]["sources.json"])["continuation_joins"]
    assert (join["head_page_ordinal"], join["tail_page_ordinal"]) == (2, 3)
    assert (join["tail_act_ids"], join["not_reconstructed_reason"]) == ([], "side-names-no-act")
    [reason] = bundle["manifest"]["aggregate"]["reasons"]
    assert "side-names-no-act" in reason


def test_a_continuation_flag_no_link_pairs_is_named_and_keeps_the_run_partial(
    runs_past_end, tmp_path
):
    root, options = _copy(runs_past_end, tmp_path)
    _recense(root, options, "page-runs-past-end")
    # The Recensor records every page break a flag names.
    forge_continuation_links(root, RUN_ID, "page-runs-past-end", options, [])
    result = _after_recensor(root, options, "page-runs-past-end")
    assert result.returncode == 3, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    [reason] = bundle["manifest"]["aggregate"]["reasons"]
    assert reason.startswith("act p2:1 says it continues onto the next page")
    basis = bundle["manifest"]["aggregate_basis"]
    assert basis["continuation_flags"] == {"p2:1": ["continues_to_next_page"]}


@pytest.mark.parametrize(
    ("links", "refusal"),
    [
        ([("p1:1", "p2:1", False)], "is not the page break the counted rows' flags derive"),
        ([("p2:1", "p1:2", True)], "does not name one flagged page break"),
    ],
    ids=["other-reading", "non-adjacent"],
)
def test_a_continuation_link_the_recensor_never_makes_is_refused(
    page_other, tmp_path, links, refusal
):
    root, options = _copy(page_other, tmp_path)
    _recense(root, options, "page-other")
    forge_continuation_links(root, RUN_ID, "page-other", options, links)
    result = _after_recensor(root, options, "page-other")
    assert result.returncode == 2
    assert refusal in result.stderr


def test_a_continuation_link_whose_side_is_not_its_pages_act_edge_is_refused(happy, tmp_path):
    """Page 1's break runs from its last act, p1:2; a link from p1:1 joins the wrong pair."""
    root, options = _copy(happy, tmp_path)
    _recense(root, options, "happy")
    forge_continuation_links(root, RUN_ID, "happy", options, [("p1:1", "p2:1", False)])
    result = _after_recensor(root, options, "happy")
    assert result.returncode == 2
    assert "is not the page break the counted rows' flags derive" in result.stderr


def test_confirmed_blank_on_a_row_that_is_not_a_blank_page_is_refused(happy, tmp_path):
    root, options = _copy(happy, tmp_path)
    _recense(root, options, "happy")
    # The Recensor confirms blank only a page-blank row.
    forge_page_review(root, RUN_ID, "p1:2", "confirmed-blank")
    result = _after_recensor(root, options, "happy")
    assert result.returncode == 2
    assert "only a page read as blank can be confirmed blank" in result.stderr


def test_a_page_refused_row_must_stand_for_a_page_the_census_refused():
    armarium = load_stage("7_armarium")
    assert armarium.PAGE_REFUSED_CLASS == "page-refused"
    row = {"act_key": "p2:refused", "page_ordinal": 2, "class": "page-refused"}
    armarium._require_refused_in_census(row, {2: {"outcome": "refused", "reason": "door: x"}})
    for census in ({2: {"outcome": "sealed"}}, {}):
        with pytest.raises(FatalAccounting, match="stands for a refused page"):
            armarium._require_refused_in_census(row, census)
    kept = armarium.reviewed_rows([row, {**row, "class": "reading", "act_key": "p1:1"}])
    assert [item["act_key"] for item in kept] == ["p1:1"]


def test_a_blinded_run_exports_each_witness_by_chair_and_by_the_label_its_reader_saw(
    tmp_path_factory,
):
    root, options = build_page_tree(
        tmp_path_factory.mktemp("blinded"), "page-unbroken", witness_context="blinded"
    )
    result = _export(root, options, "page-unbroken")
    assert result.returncode == 0, result.stderr
    bundle = _bundle(root, tmp_path_factory.mktemp("blinded-clean"))
    roster = set(bundle["export"]["payload"]["witness_chairs"])
    for row in _jsonl(bundle["members"], "acts.jsonl").values():
        assert row["witnesses"]
        for witness in row["witnesses"]:
            assert witness["chair"] in roster
            assert witness["witness_label"].startswith("witness-")
            assert witness["witness_label"] != witness["chair"]


def test_page_rows_carry_the_page_read_lectio_kind_under_their_own_ids(complete):
    members = complete["members"]
    for row in _jsonl(members, "acts.jsonl").values():
        assert row["schema"] == "armarium-act.v7"
        assert row["uncertainty"]["lectio_kind"] == "page-read"
        assert row["uncertainty"]["self_revisions"] is None
        assert row["reading"] == "first reading"
    with sqlite3.connect(complete["clean"] / "acts.sqlite") as connection:
        assert connection.execute(
            "SELECT value FROM export_metadata WHERE key = 'schema'"
        ).fetchone() == ("armarium-acts-sqlite.v7",)
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        assert set(connection.execute("SELECT reading FROM acts")) == {("first reading",)}


# --- the export's own checks of a page reading's category ---------------------------


class _Tree:
    def __init__(self, record: dict):
        self.record = record

    def read_artifact(self, _stage, _kind, _identity):
        return self.record


def test_an_accepted_review_over_a_held_row_is_refused_without_a_release():
    armarium = load_stage("7_armarium")
    established = {"artifact_id": "a", "outcome": "established", "payload": {}}
    context = SimpleNamespace(tree=_Tree(established))
    cache = {
        ARCHETYPUS: {"artifacts": [{"kind": "archetypus", "subject_id": "x", "artifact_id": "a"}]}
    }
    row = {
        "act_id": "x",
        "act_key": "p2:1",
        "class": "reading",
        "kind": "act",
        "disposition": "held",
        "hold_codes": ["unread-ink"],
        "perlectio_ref": {"relative_path": "p", "sha256": "0" * 64},
    }
    review = {"outcome": "accepted", "payload": {"release": None}}
    with pytest.raises(FatalAccounting, match="may not resurrect a held reading"):
        armarium._page_category(context, row, review, cache)
    released = {**review, "payload": {"release": {"hold_codes": ["unread-ink"]}}}
    with pytest.raises(FatalAccounting, match="may not resurrect a held reading"):
        armarium._page_category(context, row, released, cache)
    no_act = {**row, "kind": "other", "hold_codes": ["no-act-on-page-unconfirmed"]}
    with pytest.raises(FatalAccounting, match="may not resurrect a held reading"):
        armarium._page_category(context, no_act, review, cache)
    category, record = armarium._page_category(
        context,
        no_act,
        {**review, "payload": {"release": {"hold_codes": ["no-act-on-page-unconfirmed"]}}},
        cache,
    )
    assert (category.value, record) == ("delivered", established)


def test_the_exported_threshold_list_refuses_a_threshold_that_is_not_an_integer(monkeypatch):
    armarium = load_stage("7_armarium")
    policy = dataclasses.replace(load_page_accounting_policy(), band_slack=True)
    monkeypatch.setattr(armarium, "require_page_accounting_policy", lambda _context, _path: policy)
    context = SimpleNamespace(page_accounting_config_path=None)
    with pytest.raises(FatalAccounting, match="band_slack is True, not an integer"):
        armarium.page_not_measured_basis(context, {}, [])


def test_a_re_asked_page_exports_its_recovered_act_with_the_rest(tmp_path):
    """reask-recovers with the re-ask on: page 1's recovered act is a counted unit that
    the Archetypus and the Armarium accept and export, held or delivered on its own."""
    root, options = build_page_tree(tmp_path, "reask-recovers", reask=1)
    result = _export(root, options, "reask-recovers")
    assert result.returncode == 3, result.stderr
    bundle = _bundle(root, tmp_path / "clean")
    claims = bundle["manifest"]["claims"]
    partition = claims["act_partition"]
    assert partition["expected_count"] == 3
    exported = {**_jsonl(bundle["members"], "acts.jsonl")}
    assert sorted(exported) == ["p1:1", "p1:2", "p2:1"]
    # The recovered act is in the main act layer, labelled apart from the first reading's.
    assert {key: row["reading"] for key, row in exported.items()} == {
        "p1:1": "first reading",
        "p1:2": "read on re-ask",
        "p2:1": "first reading",
    }
    with sqlite3.connect(bundle["clean"] / "acts.sqlite") as connection:
        assert dict(connection.execute("SELECT act_key, reading FROM acts")) == {
            key: row["reading"] for key, row in exported.items()
        }
    assert claims["reask"] == {
        "label": "read on re-ask",
        "first_reading_acts": 2,
        "read_on_reask_acts": 1,
        "read_on_reask_act_ids": [exported["p1:2"]["act_id"]],
        "pages": [
            {"ordinal": 1, "first_reading_acts": 1, "read_on_reask_acts": 1},
            {"ordinal": 2, "first_reading_acts": 1, "read_on_reask_acts": 0},
        ],
    }
    # Page 1 stays held by Churro's unboxed line, which no re-ask may name, so both
    # of its acts are held; page 2's is delivered.
    assert {key: row["category"] for key, row in exported.items()} == {
        "p1:1": "held-for-review",
        "p1:2": "held-for-review",
        "p2:1": "delivered",
    }


# Runs one stage program with `common.stage.load_fixture` extending the committed
# fixture by the rows in the JSON file named first; every stage of the run reads
# the same extended fixture, so the run seals it like any other.
_EXTENDED_FIXTURE_RUNNER = """
import json, runpy, sys
from pathlib import Path

sys.path.insert(0, {root!r})
import common.stage as stage

committed = stage.load_fixture
extra = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))


def load_fixture(fixture_root):
    fixture = committed(fixture_root)
    for table, rows in extra.items():
        fixture[table] = [*fixture.get(table, []), *rows]
    return fixture


stage.load_fixture = load_fixture
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _reask_recovers_clean_rows() -> dict[str, list[dict]]:
    """`reask-recovers` with Churro's page 1 response empty and page 1's first reading
    citing no Churro unit, so no unboxed line is left that a re-ask may not name:
    the act the re-ask recovers is the page's only open question.

    Built here rather than in `proof/build_fixture.py` because the run seals the whole
    fixture declaration, so a committed scenario would move every pinned run tree.
    """
    import tomllib

    fixture = tomllib.loads((ROOT / "proof" / "skeleton_fixture.toml").read_text(encoding="utf-8"))
    name = "reask-recovers-clean"

    def renamed(row: dict) -> dict:
        return {**row, "scenario": name}

    answers = []
    for row in fixture["page_answer"]:
        if row["scenario"] != "reask-recovers":
            continue
        if row["page_ordinal"] == 1:
            answer = json.loads(row["answer"])
            for entry in answer["entries"]:
                entry["cites"] = [cite for cite in entry["cites"] if not cite.startswith("C")]
            row = {**row, "answer": json.dumps(answer, separators=(",", ":"))}
        answers.append(renamed(row))
    return {
        "scenario": [{"name": name}],
        "page_answer": answers,
        "page_reask_answer": [
            renamed(row)
            for row in fixture["page_reask_answer"]
            if row["scenario"] == "reask-recovers"
        ],
        "witness_empty": [{"scenario": name, "page_ordinal": 1, "chair": "attestator_3"}],
    }


def test_an_act_the_re_ask_recovers_is_accepted_and_delivered_read_on_re_ask(tmp_path):
    """The re-ask recovers page 1's a2 and nothing else holds the page, so the real
    Recensor accepts the recovered act, the Archetypus establishes it and the
    Armarium delivers it labelled "read on re-ask" in every format, in a package
    the clean verifier accepts."""
    scenario = "reask-recovers-clean"
    extra = tmp_path / "extra-fixture.json"
    extra.write_text(json.dumps(_reask_recovers_clean_rows()), encoding="utf-8")
    runner = tmp_path / "run_extended.py"
    runner.write_text(_EXTENDED_FIXTURE_RUNNER.format(root=str(ROOT)), encoding="utf-8")
    options = {"recovery_config": reask_recovery_config(tmp_path / "config", 1)}
    root = tmp_path / "runs"
    for program in programs_through("armarium"):
        command = [sys.executable, str(runner), str(extra), program]
        command += ["--run-root", str(root), "--run-id", RUN_ID, "--scenario", scenario]
        for name, value in options.items():
            command += [f"--{name.replace('_', '-')}", str(value)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    assert not held_by_recensor(RunTree(root, RUN_ID))
    bundle = _bundle(root, tmp_path / "clean")
    verify_export_bundle(bundle["data"], tmp_path / "export-clean")

    exported = _jsonl(bundle["members"], "acts.jsonl")
    assert {key: (row["category"], row["reading"]) for key, row in exported.items()} == {
        "p1:1": ("delivered", "first reading"),
        "p1:2": ("delivered", "read on re-ask"),
        "p2:1": ("delivered", "first reading"),
    }
    recovered = exported["p1:2"]
    assert recovered["canonical_clean_text"] == bundle["established"]["p1:2"]["text"]
    with sqlite3.connect(bundle["clean"] / "acts.sqlite") as connection:
        stored = dict(connection.execute("SELECT act_key, reading FROM acts"))
    assert stored["p1:2"] == "read on re-ask"
    lines = _text_bundle(bundle["members"]).split("\n")
    assert lines[lines.index(f"act-id: {recovered['act_id']}") + 1] == "reading: read on re-ask"
    reask = bundle["manifest"]["claims"]["reask"]
    assert reask["read_on_reask_act_ids"] == [recovered["act_id"]]


def _released_review(record_sha256: str, decision_hash: str) -> tuple[dict, dict]:
    """A row and its accepted review, cleared by one current release of the unit."""
    from common.review_decisions import REVIEW_FIELD

    row = {"act_id": "act-1", "act_key": "p1:1", "page_id": "pg-1", "kind": "act"}
    summary = {
        "state": "current",
        "decision_hash": decision_hash,
        "record_sha256": record_sha256,
        "scope": "unit",
        "subject_id": "act-1",
        "decision": "release",
    }
    block = {"cleared": {"unit": ["duplicate-region"], "page": []}, "decisions": [summary]}
    return row, {"outcome": "accepted", "payload": {REVIEW_FIELD: block}}


@pytest.mark.parametrize(
    "approvals, refusal",
    [
        ({}, "whose stored approval s is not in the run"),
        (
            {
                "s": (
                    SimpleNamespace(relative_path="receipts/sha256/s.json"),
                    {
                        "self_hash": "h",
                        "subject_ids": ["act-1"],
                        "review": {"decision": "exclude", "scope": "unit"},
                    },
                )
            },
            "receipts/sha256/s.json records another decision",
        ),
        (
            {
                "s": (
                    SimpleNamespace(relative_path="receipts/sha256/s.json"),
                    {
                        "self_hash": "h",
                        "subject_ids": ["act-2"],
                        "review": {"decision": "release", "scope": "unit"},
                    },
                )
            },
            "records another decision",
        ),
    ],
    ids=["missing", "other-decision", "other-subject"],
)
def test_a_label_needs_the_stored_decision_its_review_names(approvals, refusal):
    armarium = load_stage("7_armarium", isolate_path=True)
    row, review = _released_review("s", "h")
    with pytest.raises(FatalAccounting, match=refusal):
        armarium.operator_action(row, review, frozenset({"h"}), approvals)
