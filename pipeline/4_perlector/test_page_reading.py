"""The Perlector's page path, proven end to end on the synthetic fixture and fake serving.

A run reads every sealed page whole: a
`page-feed`, a `page-reading`, the page's `page-accounting` and, for a parsed
and valid answer, one `act-region` and one `perlectio.v3` per entry, each
naming the accounting and held when the page is. The fixture tests run the
real chain as subprocesses; the live tests run the stage in this process
against `operations/serving/fakes.py`, as `test_live_perlector.py` does.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import page_run
import pytest
from test_live_perlector import (
    SERVED_MODEL_ID,
    TIER,
    _live_row,
    _perlector_identity,
    _serving_factory,
    _toml_value,
    _TreeBlobs,
)

from common import page_answer, page_feed, page_path
from common.alignment import load_dissent_limits
from common.background import DEFAULT_INK_MAP_CONFIG_PATH
from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of, self_hash
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal
from common.contracts.identities import act_bindings, artifact_id, region_id, verify
from common.contracts.stages import PERLECTOR, RECENSOR
from common.decoding import (
    chair_decoding,
    engine_effective_sampling,
    load_decoding_policy,
    recorded_wire_decimals,
)
from common.exemplar_boundary import read_sealed_page
from common.hard_failure import load_hard_failure_policy, tally_hard_failures
from common.imaging import crop_png
from common.page_accounting import is_inside, load_page_accounting_policy, placement_boxes
from common.page_witness_units import DAI
from common.runtree.store import RunTree
from common.stage import open_context, reading_acts, stage_parser
from conftest import (
    file_bytes_snapshot,
    floor_models_config,
    load_stage,
    programs_through,
    reask_recovery_config,
    rewitness_stage_boundary,
)
from operations.serving.assembly import SERVING_READER
from operations.serving.config import profile_preflight_digest
from operations.serving.fakes import FakeEndpoint, ScriptedAnswer

ROOT = Path(__file__).resolve().parents[2]
BUDGET = load_dissent_limits()[0].max_comparison_steps
PERLECTOR_PROGRAM = "pipeline/4_perlector/run.py"
MODELS = ROOT / "config" / "models.toml"
CHAIN = programs_through("attestatores")
FIXTURE = tomllib.loads((ROOT / "proof" / "skeleton_fixture.toml").read_text(encoding="utf-8"))
PAGE_ANSWERS = {
    row["page_ordinal"]: row["answer"]
    for row in FIXTURE["page_answer"]
    if row["scenario"] == "happy"
}
POLICY = load_page_accounting_policy()

perlector = load_stage("4_perlector")


# --- helpers ----------------------------------------------------------------------


def _page_protocol(directory: Path, **feed: Any) -> Path:
    """The shipped protocol, which reads whole pages, with any `[feed]` switch changed."""
    text = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    for key, value in feed.items():
        head, table = text.split("[feed]\n")
        lines = [
            f"{key} = {_feed_value(value)}" if line.split(" = ")[0] == key else line
            for line in table.split("\n")
        ]
        text = head + "[feed]\n" + "\n".join(lines)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "perlector_protocol.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _feed_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(f'"{item}"' for item in value) + "]"
    return _toml_value(value)


def _run(
    program: str, root: Path, protocol: Path, *extra: str, scenario: str = "happy"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            scenario,
            "--perlector-protocol-config",
            str(protocol),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _chain(
    root: Path,
    protocol: Path,
    *extra: str,
    through_perlector: bool = False,
    programs: tuple[str, ...] = CHAIN,
    scenario: str = "happy",
) -> None:
    for program in programs + ((PERLECTOR_PROGRAM,) if through_perlector else ()):
        result = _run(program, root, protocol, *extra, scenario=scenario)
        assert result.returncode == 0, f"{program}: {result.stderr}"


def _records(root: Path, kind: str) -> list[dict[str, Any]]:
    """Every published record of one kind, read from the files a stopped pass left too."""
    directory = root / "r" / "4_perlector" / "artifacts" / kind
    if not directory.exists():
        return []
    return sorted(
        (json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")),
        key=lambda record: (record["payload"].get("page_ordinal", 0), record["artifact_id"]),
    )


def _designator_records(root: Path, kind: str) -> list[dict[str, Any]]:
    directory = root / "r" / "2_designator" / "artifacts" / kind
    if not directory.exists():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")]


def _corners(box: dict[str, int]) -> tuple[int, int, int, int]:
    return box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"]


def _union(boxes: list[dict[str, int]]) -> dict[str, int]:
    corners = [_corners(box) for box in boxes]
    x0, y0 = min(c[0] for c in corners), min(c[1] for c in corners)
    x1, y1 = max(c[2] for c in corners), max(c[3] for c in corners)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _roster(base: Path, *replacements: tuple[str, str]) -> Path:
    """The committed roster with chair blocks replaced, beside its fixture snapshots."""
    path = floor_models_config(base / "chair-config", 3)
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in text
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return path


def _chair_block(chair: str) -> str:
    """The shipped roster's block for `chair`, exactly as written."""
    text = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    start = text.index(f"[chairs.{chair}]\n")
    return text[start : text.index("\n\n", start) + 1]


_ABSENT_SURYA = (
    '[chairs.designator_surya]\nstate = "absent"\n'
    'reason = "no Surya detector is configured for this test run"\n'
)
_NO_SURYA = (_chair_block("designator_surya"), _ABSENT_SURYA)


# --- the fixture page path --------------------------------------------------------


@pytest.fixture(scope="module")
def page_tree(tmp_path_factory) -> tuple[Path, Path]:
    base = tmp_path_factory.mktemp("page-reading")
    protocol = _page_protocol(base / "config")
    _chain(base / "runs", protocol, through_perlector=True)
    return base / "runs", protocol


@pytest.fixture(scope="module")
def review_page_tree(tmp_path_factory) -> tuple[Path, Path]:
    """The `page-review` scenario read whole: page 1 as `happy`, page 2's entry citing no box."""
    base = tmp_path_factory.mktemp("page-reading-review")
    protocol = _page_protocol(base / "config")
    _chain(base / "runs", protocol, through_perlector=True, scenario="page-review")
    return base / "runs", protocol


def test_every_sealed_page_is_read_whole_into_a_feed_a_reading_and_its_acts(page_tree):
    root, _protocol = page_tree
    feeds, readings = _records(root, "page-feed"), _records(root, "page-reading")
    assert [feed["payload"]["page_ordinal"] for feed in feeds] == [1, 2]
    assert [reading["payload"]["page_ordinal"] for reading in readings] == [1, 2]
    for reading in readings:
        payload = reading["payload"]
        assert payload["schema"] == page_run.PAGE_READING_SCHEMA
        assert (payload["parse_state"], payload["disposition"], reading["outcome"]) == (
            "parsed",
            "read",
            "read",
        )
        assert payload["answer"] == json.loads(PAGE_ANSWERS[payload["page_ordinal"]])
        assert payload["audit"]["state"] == "not-run"
        assert payload["engine_call"] is None and payload["capacity"] is None
    feed = feeds[0]["payload"]
    # The fixture roster runs Surya in stage 2, so every feed shows its census.
    assert feed["surya"]["census_ref"] is not None and "absent" not in feed["surya"]
    assert feed["surya"]["block_sequence"] == "surya-order-head"
    assert feed["witness_testimony"] == "present"
    assert [(row["letter"], row["witness_label"]) for row in feed["witnesses"]] == [
        ("A", "attestator_1"),
        ("B", "attestator_2"),
        ("C", "attestator_3"),
    ]
    assert len(_records(root, "act-region")) == len(_records(root, "perlectio")) == 3
    # Every Perlectio is a page-path reading.
    assert all(
        record["payload"]["schema"] == "perlectio.v3" for record in _records(root, "perlectio")
    )


def test_each_placed_act_region_is_the_union_of_its_cited_boxes_cut_from_the_ink(page_tree):
    root, _protocol = page_tree
    tree = RunTree(root, "r")
    feeds = {record["subject_id"]: record["payload"] for record in _records(root, "page-feed")}
    readings = {record["subject_id"]: record for record in _records(root, "page-reading")}
    placed = [r for r in _records(root, "act-region") if r["payload"]["union_box_px"]]
    assert sorted((r["payload"]["page_ordinal"], r["payload"]["n"]) for r in placed) == [
        (1, 1),
        (1, 2),
        (2, 1),
    ]
    for region in placed:
        payload = region["payload"]
        boxes = placement_boxes(feeds[payload["page_id"]], load_page_accounting_policy())
        assert set(payload["cited_ids"]) <= set(boxes)
        cited = [boxes[identifier] for identifier in payload["cited_ids"] if boxes[identifier]]
        # The region is each placing box once, in first-cited order; the union crops it.
        assert payload["region_boxes_px"] == [
            box for index, box in enumerate(cited) if box not in cited[:index]
        ]
        assert payload["union_box_px"] == _union(cited)
        attempt = readings[payload["page_id"]]["attempt_id"]
        verify(
            region["subject_id"],
            "act",
            act_bindings(
                payload["page_id"],
                "reading",
                {
                    "page_reading": attempt,
                    "n": payload["n"],
                    "union_box_px": payload["union_box_px"],
                },
            ),
        )
        assert payload["region_id"] == region_id(region["subject_id"], payload["transform"])
        _page, page_bytes = read_sealed_page(tree, payload["page_id"])
        crop = (root / "r" / payload["image_path"]).read_bytes()
        assert crop == crop_png(page_bytes, payload["union_box_px"])
        assert payload["transform"]["bounds"] == payload["union_box_px"]
        assert payload["holds"] == []


def test_every_act_record_names_its_page_accounting_published_before_it(page_tree):
    """The accounting comes first, and an act on a held page is held by the page's codes."""
    root, _protocol = page_tree
    accounts = {r["subject_id"]: r for r in _records(root, "page-accounting")}
    # Published first: the accounting names no act record, and each act record
    # names the accounting as an input, which must exist when it is published.
    for account in accounts.values():
        assert not any(
            "/act-region/" in ref["relative_path"] or "/perlectio/" in ref["relative_path"]
            for ref in account["inputs"]
        )
    for kind in ("act-region", "perlectio"):
        for record in _records(root, kind):
            payload = record["payload"]
            account = accounts[payload["page_id"]]
            assert payload["page_accounting_ref"]["relative_path"].endswith(
                f"{account['artifact_id']}.json"
            )
            assert payload["page_accounting_ref"] in record["inputs"]
            assert payload["page_holds"] == account["payload"]["holds"]
            held = bool(payload["page_holds"] or payload["holds"])
            assert record["outcome"] == ("held" if held else "read")
    # Both pages are read whole and covered, so nothing holds an act.
    outcomes = {(r["payload"]["page_ordinal"], r["outcome"]) for r in _records(root, "perlectio")}
    assert outcomes == {(1, "read"), (2, "read")}


def test_a_perlectio_carries_clean_text_doubt_dissent_truncation_and_autopsia(page_tree):
    root, _protocol = page_tree
    first = next(
        record
        for record in _records(root, "perlectio")
        if record["payload"]["page_ordinal"] == 1 and record["payload"]["n"] == 1
    )
    payload = first["payload"]
    assert payload["text"] == "SYNTHETIC ACT ONE alpha beta gamma"
    assert [span["alternatives"] for span in payload["uncertain_spans"]] == [["gamna"]]
    assert payload["truncation"]["classification"] == "complete"
    assert payload["autopsia"] is True
    rows = {row["letter"]: row for row in payload["dissent"]}
    assert rows["A"]["cited_units"] == ["A1"] and rows["A"]["departed"] is False
    # DAI's record reads "gamna" and Churro's line "... alpha beta": both depart.
    assert rows["B"]["cited_units"] == ["B1"] and rows["B"]["departed"] is True
    assert rows["C"]["cited_units"] == ["C1"] and rows["C"]["departed"] is True
    assert payload["holds"] == [] and payload["page_holds"] == []
    assert first["outcome"] == "read"
    last = next(
        record
        for record in _records(root, "perlectio")
        if record["payload"]["page_ordinal"] == 1 and record["payload"]["n"] == 2
    )
    assert last["payload"]["continues_to_next_page"] is True


def test_an_entry_citing_no_boxed_id_is_held_unplaced_with_no_crop(review_page_tree):
    root, _protocol = review_page_tree
    [region] = [
        r
        for r in _records(root, "act-region")
        if r["payload"]["page_ordinal"] == 2 and "reading_attempt" not in r["payload"]
    ]
    payload = region["payload"]
    assert region["outcome"] == "held"
    assert payload["union_box_px"] is None and payload["act_class"] == "reading-unplaced"
    assert payload["holds"] == ["reading-unplaced"]
    assert (payload["region_id"], payload["image_path"], payload["transform"]) == (None,) * 3
    [reading] = [
        r
        for r in _records(root, "perlectio")
        if r["payload"]["page_ordinal"] == 2 and "reading_attempt" not in r["payload"]
    ]
    assert reading["outcome"] == "held" and reading["payload"]["truncation"] is None
    assert reading["payload"]["continues_from_previous_page"] is True


def test_a_second_pass_and_a_fresh_run_leave_the_same_bytes_and_act_ids(page_tree, tmp_path):
    root, protocol = page_tree
    copy = tmp_path / "runs"
    shutil.copytree(root, copy)
    before = file_bytes_snapshot(copy)
    result = _run(PERLECTOR_PROGRAM, copy, protocol)
    assert result.returncode == 0, result.stderr
    assert file_bytes_snapshot(copy) == before
    fresh = tmp_path / "fresh"
    _chain(fresh, protocol, through_perlector=True)
    assert file_bytes_snapshot(fresh / "r" / "4_perlector") == file_bytes_snapshot(
        root / "r" / "4_perlector"
    )


def test_each_page_is_accounted_and_holds_only_for_reasons_it_names(page_tree, review_page_tree):
    """Each page is read whole and placed, and its readings cover every Surya line:
    nothing holds it. In `page-review`, page 2's one entry cites no boxed id: it is
    unplaced, has no region to measure truncation over, DAI's record goes
    uncited and unread, and the page's ink and Surya's lines lie outside every
    reading region."""
    root, _protocol = page_tree
    accounts = {r["payload"]["page_ordinal"]: r for r in _records(root, "page-accounting")}
    assert set(accounts) == {1, 2}
    for account in accounts.values():
        payload = account["payload"]
        assert payload["schema"] == "page-accounting.v2" and account["outcome"] == "read"
        assert payload["holds"] == []
        assert {unit["disposition"] for unit in payload["units"]} == {"cited"}
        # Every DAI record lies inside exactly one act region.
        assert payload["rules"]["i"]["status"] == "pass"
    root, _protocol = review_page_tree
    [page_one] = _accountings(root, 1)
    first, last = _accountings(root, 2)
    assert page_one["payload"]["holds"] == []
    assert first["payload"]["holds"] == [
        "reading-unplaced",
        "record-not-read",
        "truncation-not-classified",
        "unaccounted-witness-unit",
        "unread-ink",
        "unread-line",
    ]
    # Re-asked about DAI's record and Surya's lines, the reader reads the a2 it
    # already read: the ids are accounted for, and the entry is held as a duplicate
    # of it. The first reading's unplaced entry still holds, as it did.
    assert last["payload"]["holds"] == [
        "reading-unplaced",
        "reask-duplicate",
        "truncation-not-classified",
    ]
    [duplicate] = last["payload"]["rules"]["j"]["findings"]
    assert duplicate == {"code": "reask-duplicate", "n": 2, "reading_n": 1, "attempt_1_n": 1}


# Each switch changes what the page is shown, so it changes the sealed feed and,
# where it changes the text, page 1's prompt (page 2 has one unboxed unit per
# witness, which no witness switch changes). The fixture page is smaller than
# the legible edge, so `full` changes the render's record, not the prompt.
@pytest.mark.parametrize(
    ("feed", "prompt_changes"),
    [
        ({"page_image": "full"}, False),
        ({"page_image": "off"}, True),
        ({"witnesses": ["attestator_3"]}, True),
        ({"witness_units": "flat"}, True),
        ({"witness_coordinates": False}, True),
        ({"page_overlay": "boxes"}, True),
        ({"surya_lines": False}, True),
        ({"surya_blocks": False}, True),
    ],
    ids=lambda value: str(value),
)
def test_each_feed_switch_changes_the_sealed_feed(page_tree, tmp_path, feed, prompt_changes):
    root, _protocol = page_tree
    protocol = _page_protocol(tmp_path / "config", **feed)
    # The re-ask is off: each run shows what one switch changes in a first reading.
    recovery = reask_recovery_config(tmp_path / "config", 0)
    _chain(tmp_path / "runs", protocol, "--recovery-config", str(recovery), through_perlector=True)
    shipped = {r["payload"]["page_ordinal"]: r["payload"] for r in _records(root, "page-feed")}
    switched = {
        r["payload"]["page_ordinal"]: r["payload"] for r in _records(tmp_path / "runs", "page-feed")
    }
    assert set(shipped) == set(switched) == {1, 2}
    for ordinal, payload in switched.items():
        assert payload["feed_digest"] != shipped[ordinal]["feed_digest"]
        assert payload["switches"] != shipped[ordinal]["switches"]
    prompt = switched[1]["prompt"]["rendered_sha256"]
    assert (prompt != shipped[1]["prompt"]["rendered_sha256"]) is prompt_changes
    if feed == {"page_image": "full"}:
        render = switched[1]["page_render"]
        assert (render["reason"], render["transform"]["resampler"]) == ("full-page", "identity")
    if feed == {"witnesses": ["attestator_3"]}:
        # The hidden witness is still measured, so its Testimonium is an input of both.
        [hidden] = [
            record
            for record in _records(tmp_path / "runs", "page-feed")
            if record["payload"]["page_ordinal"] == 1
        ]
        paths = {ref["relative_path"] for ref in hidden["inputs"]}
        shown = {row["testimonium_ref"]["relative_path"] for row in hidden["payload"]["witnesses"]}
        assert len(shown) == 1 and len(paths & _testimonium_paths(tmp_path / "runs", 1)) == 3
        [account] = [
            record
            for record in _records(tmp_path / "runs", "page-accounting")
            if record["payload"]["page_ordinal"] == 1
        ]
        accounted = {ref["relative_path"] for ref in account["inputs"]}
        assert _testimonium_paths(tmp_path / "runs", 1) <= accounted
    if feed == {"page_image": "off"}:
        # A reading made without the page image cannot be established from the ink.
        for record in _records(tmp_path / "runs", "perlectio"):
            assert record["payload"]["autopsia"] is False
            assert "no-autopsia" in record["payload"]["holds"]
            assert record["outcome"] == "held"


def _testimonium_paths(root: Path, ordinal: int) -> set[str]:
    directory = root / "r" / "3_attestatores" / "artifacts" / "page-testimonium"
    feed = next(r for r in _records(root, "page-feed") if r["payload"]["page_ordinal"] == ordinal)
    return {
        f"3_attestatores/artifacts/page-testimonium/{path.name}"
        for path in directory.glob("*.json")
        if json.loads(path.read_text("utf-8"))["subject_id"] == feed["subject_id"]
    }


# --- Surya and the record detector, from real stage-2 records ---------------------


def test_the_feed_shows_the_stage_two_surya_records_as_sealed(page_tree):
    root, _protocol = page_tree
    census = {r["subject_id"]: r["payload"] for r in _designator_records(root, "surya-page")}
    lines = {r["subject_id"]: r["payload"] for r in _designator_records(root, "surya-line")}
    blocks = {r["subject_id"]: r["payload"] for r in _designator_records(root, "surya-block")}
    assert census and lines and blocks
    for record in _records(root, "page-feed"):
        feed = record["payload"]
        page = census[feed["page_id"]]
        surya = feed["surya"]
        assert surya["block_sequence"] == page["reading_order"]
        assert surya["block_sequence_reason"] == page["reading_order_reason"]
        assert surya["census_ref"] in record["inputs"]
        assert [line["box_px"] for line in surya["lines"]] == [
            lines[subject]["bounds"] for subject in page["line_subjects"]
        ]
        assert [line["confidence_bp"] for line in surya["lines"]] == [
            lines[subject]["confidence_bp"] for subject in page["line_subjects"]
        ]
        by_position = sorted(
            (blocks[subject] for subject in page["block_subjects"]),
            key=lambda block: block["reading_order_position"],
        )
        assert [(b["box_px"], b["label"]) for b in surya["blocks"]] == [
            (b["bounds"], b["label"]) for b in by_position
        ]
        assert all(ref["ref"] in record["inputs"] for ref in surya["lines"] + surya["blocks"])


def test_with_surya_sealed_every_detected_line_is_measured(page_tree):
    root, _protocol = page_tree
    for account in _records(root, "page-accounting"):
        payload = account["payload"]
        assert payload["rules"]["d"]["status"] != "not-measured"
        assert "unread-line-not-measured" not in payload["holds"]
        assert len(payload["lines"]) == len(
            next(
                r["payload"]["line_subjects"]
                for r in _designator_records(root, "surya-page")
                if r["subject_id"] == payload["page_id"]
            )
        )
    first = next(r for r in _records(root, "page-accounting") if r["payload"]["page_ordinal"] == 1)
    # Page 1's two readings cover its nine lines: nothing on it is held.
    assert first["payload"]["holds"] == [] and first["outcome"] == "read"
    for kind in ("act-region", "perlectio"):
        for record in _records(root, kind):
            if record["payload"]["page_ordinal"] == 1:
                assert record["payload"]["page_holds"] == [] and record["outcome"] == "read"


def test_a_run_with_surya_absent_states_it_on_the_feed_and_holds_rule_d(tmp_path):
    protocol = _page_protocol(tmp_path / "config")
    models = _roster(tmp_path, _NO_SURYA)
    _chain(tmp_path / "runs", protocol, "--models-config", str(models), through_perlector=True)
    for record in _records(tmp_path / "runs", "page-feed"):
        surya = record["payload"]["surya"]
        assert surya["absent"] == "no Surya page census was sealed in this run"
        assert (surya["census_ref"], surya["block_sequence"], surya["lines"]) == (None, None, [])
    for account in _records(tmp_path / "runs", "page-accounting"):
        assert account["payload"]["rules"]["d"]["status"] == "not-measured"
        assert "unread-line-not-measured" in account["payload"]["holds"]
        assert account["outcome"] == "held"


def _detector_tree(
    base: Path, monkeypatch, detections: list[dict[str, Any]], *, max_det: int | None = 300
):
    """A page-read tree whose stage-2 record detector declared `detections`.

    The fixture detector states its cap, 300, as the in-process detector does;
    `max_det` states another, and None states none, so rule (i) is not
    measured. DAI reads each cut record with the fixture's declared answer for
    its page and ordinal.
    """
    protocol = _page_protocol(base / "config")
    root = base / "runs"
    _chain(root, protocol, programs=programs_through("ink-map"))
    designator = load_stage("2_designator")
    original = designator.fixture_record_detector

    def declared(_rows, identity, details):
        detector = original(detections, identity, details)
        facts = {key: value for key, value in detector.run_facts.items() if key != "max_det"}
        if max_det is not None:
            facts["max_det"] = max_det
        return dataclasses.replace(detector, run_facts=facts)

    monkeypatch.setattr(designator, "fixture_record_detector", declared)
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
            "--perlector-protocol-config",
            str(protocol),
        ],
    )
    assert designator.main() == 0
    _chain(
        root,
        protocol,
        programs=programs_through("attestatores")[len(programs_through("designator")) :],
        through_perlector=True,
    )
    return root


# Page 1 holds a1 at (20, 20, 160, 80) and a2 at (20, 120, 160, 100). Two
# records lie inside a1, as if the detector split one entry; one collapses to
# a pixel at the page corner and encloses no crop.
_SPLIT_AND_COLLAPSED = [
    {"page_ordinal": 1, "corners": [[25, 25], [175, 25], [175, 55], [25, 55]], "score_bp": 9000},
    {"page_ordinal": 1, "corners": [[25, 60], [175, 60], [175, 95], [25, 95]], "score_bp": 9000},
    {
        "page_ordinal": 1,
        "corners": [[199.9, 259.9], [199.9, 259.9], [199.9, 259.9], [199.9, 259.9]],
        "score_bp": 2600,
    },
]


def test_a_page_held_by_rule_i_holds_every_act_record_and_reports_an_unboxed_record(
    tmp_path, monkeypatch
):
    root = _detector_tree(tmp_path, monkeypatch, _SPLIT_AND_COLLAPSED)
    account = next(
        r["payload"] for r in _records(root, "page-accounting") if r["payload"]["page_ordinal"] == 1
    )
    rule = account["rules"]["i"]
    assert rule["records_not_measured"] == 1
    codes = [finding["code"] for finding in rule["findings"]]
    assert codes.count("detector-record-not-measured") == 1
    assert "merged-detection" in codes and rule["status"] == "hold"
    assert {"merged-detection", "detector-record-not-measured"} <= set(account["holds"])
    assert len(account["records"]) == 2
    for kind in ("act-region", "perlectio"):
        for record in _records(root, kind):
            if record["payload"]["page_ordinal"] == 1:
                assert record["outcome"] == "held"
                assert "merged-detection" in record["payload"]["page_holds"]


class _Doctored:
    """A run tree whose records read back edited, for refusals no honest stage would write."""

    def __init__(self, tree: RunTree, edit=None, manifest=None):
        self._tree, self._edit, self._manifest = tree, edit, manifest

    def __getattr__(self, name: str):
        return getattr(self._tree, name)

    def read_artifact(self, stage: str, kind: str, artifact_id: str) -> dict[str, Any]:
        record = json.loads(json.dumps(self._tree.read_artifact(stage, kind, artifact_id)))
        if self._edit is not None:
            self._edit(kind, record["payload"])
        return record

    def build_manifest(self, stage: str) -> dict[str, Any]:
        manifest = self._tree.build_manifest(stage)
        return manifest if self._manifest is None else self._manifest(stage, manifest)


def _reading_context(root: Path, edit=None, manifest=None) -> SimpleNamespace:
    tree = RunTree(root, "r")
    context = SimpleNamespace(tree=_Doctored(tree, edit, manifest))
    context.artifact_ref = lambda stage, kind, artifact_id: {
        "relative_path": tree.artifact_path(stage, kind, artifact_id),
        "sha256": digest_bytes(tree.read_bytes(tree.artifact_path(stage, kind, artifact_id))),
    }
    return context


def _first_page(root: Path) -> SimpleNamespace:
    feed = next(r for r in _records(root, "page-feed") if r["payload"]["page_ordinal"] == 1)
    return SimpleNamespace(
        page_id=feed["subject_id"], ordinal=1, feed=feed["payload"], witnesses=[]
    )


def _record_detections(context, page):
    return page_path._record_detections(
        context,
        context.tree.build_manifest("designator")["artifacts"],
        page.page_id,
        page.ordinal,
        page_path._dai_unit_ids(page.feed, page.witnesses),
    )


def _surya_census(context):
    return page_path.sealed_surya_census(
        context, context.tree.build_manifest("designator")["artifacts"]
    )


def test_each_detector_record_is_named_by_the_dai_unit_with_its_box(tmp_path, monkeypatch):
    """So a DAI unit set aside is a detector record set aside (rule (i), set-aside-record)."""
    root = _detector_tree(tmp_path, monkeypatch, _SPLIT_AND_COLLAPSED)
    page = _first_page(root)
    records, census, references = _record_detections(_reading_context(root), page)
    assert census == {"detection_count": 3, "max_det": 300, "max_det_reached": False}
    # With no DAI witness named, no record is named by a unit.
    assert [record.get("id") for record in records] == [None, None, None]
    assert references[0]["relative_path"].startswith("2_designator/artifacts/detector-page/")
    # DAI (B) read the two cut records; the collapsed one was never a unit.
    page.witnesses = [{"witness_label": "attestator_2", "adapter": DAI}]
    records, _census, _references = _record_detections(_reading_context(root), page)
    assert [record.get("id") for record in records] == ["B1", "B2", None]


@pytest.mark.parametrize(
    ("kind", "edit", "refusal"),
    [
        ("detector-page", {"detection_count": 4}, "not a list of its count"),
        ("detector-page", {"detection_count": "3"}, "not a list of its count"),
        ("detector-page", {"record_subjects": "all"}, "not a list of its count"),
        ("detector-page", {"page_ordinal": 2}, "states page ordinal 2"),
        ("detector-record", {"detector_ordinal": 7}, "ordinal 7"),
    ],
)
def test_a_detector_census_its_records_contradict_is_refused(
    tmp_path, monkeypatch, kind, edit, refusal
):
    root = _detector_tree(tmp_path, monkeypatch, _SPLIT_AND_COLLAPSED)

    def doctor(read_kind, payload):
        if read_kind == kind:
            payload.update(edit)

    with pytest.raises(FatalAccounting, match=refusal):
        _record_detections(_reading_context(root, doctor), _first_page(root))


def test_a_detector_record_no_census_names_is_refused(tmp_path, monkeypatch):
    root = _detector_tree(tmp_path, monkeypatch, _SPLIT_AND_COLLAPSED)

    def doctor(kind, payload):
        if kind == "detector-page":
            payload["record_subjects"] = payload["record_subjects"][:-1]
            payload["detection_count"] -= 1

    with pytest.raises(FatalAccounting, match="that page .* detector-page does not name"):
        _record_detections(_reading_context(root, doctor), _first_page(root))


def test_a_detector_that_reached_its_cap_holds_the_page_end_to_end(tmp_path, monkeypatch):
    root = _detector_tree(tmp_path, monkeypatch, _SPLIT_AND_COLLAPSED, max_det=3)
    account = next(
        r for r in _records(root, "page-accounting") if r["payload"]["page_ordinal"] == 1
    )
    rule = account["payload"]["rules"]["i"]
    assert rule["status"] == "not-measured"
    assert [finding["code"] for finding in rule["findings"]][0] == "record-detector-capped"
    assert "record-detector-capped" in account["payload"]["holds"]
    assert account["outcome"] == "held"


def test_a_detector_that_states_no_cap_is_not_measured_and_its_page_is_still_an_input(
    tmp_path, monkeypatch
):
    root = _detector_tree(tmp_path, monkeypatch, _SPLIT_AND_COLLAPSED, max_det=None)
    account = next(
        r for r in _records(root, "page-accounting") if r["payload"]["page_ordinal"] == 1
    )
    assert account["payload"]["rules"]["i"] == {
        "status": "not-measured",
        "findings": [{"code": "detector-records-not-measured"}],
        "records_not_measured": None,
    }
    assert any(
        ref["relative_path"].startswith("2_designator/artifacts/detector-page/")
        for ref in account["inputs"]
    )


@pytest.mark.parametrize(
    ("kind", "edit", "refusal"),
    [
        ("surya-page", {"page_id": "pg_elsewhere"}, "names another page"),
        ("surya-page", {"reading_order": "by-hand"}, "not one Surya gives"),
        ("surya-page", {"line_count": 99}, "not a list of its count"),
        ("surya-block", {"reading_order": "raster-fallback"}, "but its page census states"),
        ("surya-line", {"n": 99}, "n 99, where its census places it"),
    ],
)
def test_a_surya_census_its_records_contradict_is_refused(page_tree, kind, edit, refusal):
    root, _protocol = page_tree

    def doctor(read_kind, payload):
        if read_kind == kind:
            payload.update(edit)

    with pytest.raises(FatalAccounting, match=refusal):
        _surya_census(_reading_context(root, doctor))


def test_a_surya_detection_no_census_names_is_refused(page_tree):
    root, _protocol = page_tree

    def doctor(kind, payload):
        if kind == "surya-page":
            payload["line_subjects"] = payload["line_subjects"][:-1]
            payload["line_count"] -= 1

    with pytest.raises(FatalAccounting, match="that no page census names"):
        _surya_census(_reading_context(root, doctor))


def test_two_ink_maps_for_one_page_are_refused(page_tree):
    root, _protocol = page_tree

    def twice(stage, manifest):
        if stage != "ink-map":
            return manifest
        maps = [entry for entry in manifest["artifacts"] if entry["kind"] == "ink-map"]
        return {**manifest, "artifacts": manifest["artifacts"] + maps}

    context = _reading_context(root, manifest=twice)
    with pytest.raises(FatalAccounting, match="ink maps for page .*, not one"):
        _accounting_ink(context, _first_page(root))


def _ink_context(root: Path, edit=None) -> SimpleNamespace:
    context = _reading_context(root, edit)
    context.args = SimpleNamespace(ink_map_config=DEFAULT_INK_MAP_CONFIG_PATH)
    context.require_sealed_config = lambda _name, _digest: None
    return context


def _accounting_ink(context, page: SimpleNamespace):
    return page_path._accounting_ink(
        context,
        context.tree.build_manifest("ink-map")["artifacts"],
        page.page_id,
        page.ordinal,
        getattr(page, "page_size", None),
    )


def _sized_first_page(root: Path) -> SimpleNamespace:
    page = _first_page(root)
    [record] = [
        record
        for path in (root / "r" / "1_ink_map" / "artifacts" / "ink-map").glob("*.json")
        if (record := json.loads(path.read_text(encoding="utf-8")))["payload"]["page_ordinal"]
        == page.ordinal
    ]
    runs = record["payload"]["edge_findings"]
    page.page_size = (runs["width"], runs["height"])
    return page


def test_the_ink_maps_runs_are_read_when_they_span_the_sealed_page(page_tree):
    root, _protocol = page_tree
    context = _ink_context(root)
    ink, references = _accounting_ink(context, _sized_first_page(root))
    assert ink is not None and len(references) == 1


def test_ink_runs_sized_for_another_page_are_refused(page_tree):
    root, _protocol = page_tree
    context = _ink_context(root)
    page = _sized_first_page(root)
    page.page_size = (page.page_size[0] + 1, page.page_size[1])
    with pytest.raises(ContractError, match="not the sealed page"):
        _accounting_ink(context, page)


def test_ink_runs_that_disagree_with_their_edge_finding_are_refused(page_tree):
    root, _protocol = page_tree

    def recount(kind, payload):
        if kind == "ink-map":
            payload["edge"]["total_ink_pixels"] += 1
            payload["edge"]["page_ink_pixels"] += 1

    context = _ink_context(root, recount)
    with pytest.raises(ContractError):
        _accounting_ink(context, _sized_first_page(root))


def test_a_malformed_ink_not_measurable_record_is_refused(page_tree):
    root, _protocol = page_tree

    def refused_without_a_reason(kind, payload):
        if kind == "ink-map" and payload["page_ordinal"] == 1:
            payload.clear()
            payload.update({"page_ordinal": 1, "ink_measurable": False})

    context = _ink_context(root, refused_without_a_reason)
    read = context.tree.read_artifact

    def refused(*key):
        record = read(*key)
        if record["payload"].get("ink_measurable") is False:
            record["outcome"] = "ink-not-measurable"
        return record

    context.tree.read_artifact = refused
    with pytest.raises(ContractError, match="ink-not-measurable payload is not closed"):
        _accounting_ink(context, _first_page(root))


# --- the answer's entries -----------------------------------------------------------


def _one_unit_feed(box: dict[str, int]) -> dict:
    return {
        "page_size": {"w": 1000, "h": 1000},
        "switches": {"witness_units": "own"},
        "witnesses": [{"units": [{"id": "A1", "box_px": box, "text": "x"}]}],
        "surya": None,
    }


def test_entries_on_one_region_are_held_and_keep_their_own_ids():
    box = {"x": 1, "y": 2, "w": 3, "h": 4}
    feed = _one_unit_feed(box)
    entry = {
        "kind": "act",
        "cites": ["A1"],
        "text": "x",
        "continues_from_previous_page": False,
        "continues_to_next_page": False,
    }
    answer = {"acts": [{**entry, "n": 1}, {**entry, "n": 2}], "set_aside": []}
    entries = page_path.answer_entries(answer, feed, load_page_accounting_policy())
    assert [entry["holds"] for entry in entries] == [["duplicate-region"]] * 2
    assert [entry["union_box_px"] for entry in entries] == [box, box]
    attempt = page_path.page_reading_attempt("pg_0000000000000001", 1)
    ids = {
        digest_of(
            act_bindings(
                "pg_0000000000000001",
                "reading",
                {"page_reading": attempt, "n": n, "union_box_px": box},
            )
        )
        for n in (1, 2)
    }
    assert len(ids) == 2


def test_entries_on_one_region_do_not_hold_the_reading_whole_but_an_unknown_id_does():
    feed = _one_unit_feed({"x": 1, "y": 2, "w": 3, "h": 4})
    entry = {
        "kind": "act",
        "cites": ["A1"],
        "text": "x",
        "continues_from_previous_page": False,
        "continues_to_next_page": False,
    }
    answer = {"acts": [{**entry, "n": 1}, {**entry, "n": 2}], "set_aside": []}
    policy = load_page_accounting_policy()
    assert page_path.answer_problems(answer, feed, "stop", policy) == []
    answer["acts"][1]["cites"] = ["Q7"]
    assert [p["code"] for p in page_path.answer_problems(answer, feed, "stop", policy)] == [
        "unknown-id"
    ]


def test_dissent_does_not_count_a_witness_s_own_doubt_markers_as_departure():
    feed = {
        "witnesses": [
            {
                "letter": "A",
                "witness_label": "dai",
                "outcome": "read",
                "units": [{"id": "A1", "text": "Marie [UNCERTAIN] Roy"}],
            }
        ]
    }

    def witness(capable: bool) -> dict[str, Any]:
        capabilities = {"can_express_uncertainty": capable, "can_express_layout": False}
        return {
            "witness_label": "dai",
            "testimonium": {"payload": {"format_capabilities": capabilities}},
        }

    [marked] = page_path.page_dissent("Marie  Roy", feed, ["A1"], [witness(True)], BUDGET)
    assert marked["compared"] is True and marked["departed"] is False
    [plain] = page_path.page_dissent("Marie  Roy", feed, ["A1"], [witness(False)], BUDGET)
    assert plain["departed"] is True


def _three_witness_feed() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A shown witness the entry cites, one it does not, and one that did not read."""
    feed = {
        "witnesses": [
            {
                "letter": "A",
                "witness_label": "dai",
                "outcome": "read",
                "units": [{"id": "A1", "text": "Marie Roy"}],
            },
            {
                "letter": "B",
                "witness_label": "churro",
                "outcome": "read",
                "units": [{"id": "B1", "text": "Jean Roy"}],
            },
            {"letter": "C", "witness_label": "surya", "outcome": "failed", "units": []},
        ]
    }
    witnesses = [
        {"witness_label": label, "testimonium": {"payload": {}}}
        for label in ("dai", "churro", "surya")
    ]
    return feed, witnesses


def test_page_path_dissent_goes_through_the_dissent_validator():
    """A page-path row is a dissent row (`dissent.validate_row`) under a witness letter, with
    the witness head and cited units beside it, so it is refused on the same
    terms: a lost or relabelled witness, misstated units, a comparison claimed
    for a witness with nothing to compare, a compared row carrying a budget,
    and a stopped row naming a budget the run never sealed."""
    feed, witnesses = _three_witness_feed()
    rows = page_path.page_dissent("Marie  Roy", feed, ["A1"], witnesses, BUDGET)
    assert [row["compared"] for row in rows] == [True, False, False]

    def refused(forged, match, budget=BUDGET):
        with pytest.raises(SchemaRefusal, match=match):
            page_path.validate_page_dissent(
                forged, text="Marie  Roy", feed=feed, cited_ids=["A1"], max_comparison_steps=budget
            )

    page_path.validate_page_dissent(
        rows, text="Marie  Roy", feed=feed, cited_ids=["A1"], max_comparison_steps=BUDGET
    )
    refused(rows[:2], "one row per shown witness")
    refused([{**rows[0], "letter": "B"}, *rows[1:]], "another witness than the feed's")
    refused([{**rows[0], "cited_units": []}, *rows[1:]], "misstates the units")
    refused(
        [rows[0], {**rows[0], **rows[1], "compared": True}, rows[2]],
        "claims a comparison for a witness",
    )
    refused([rows[0], {**rows[1], "cited_units": ["B1"]}, rows[2]], "misstates the units")
    refused(
        [rows[0], {**rows[1], "compared": "unknown"}, rows[2]], "claims a comparison for a witness"
    )
    refused([{**rows[0], "max_comparison_steps": BUDGET}, *rows[1:]], "closed compared-row schema")

    stopped = page_path.page_dissent("Marie  Roy", feed, ["A1"], witnesses, 1)
    assert stopped[0]["compared"] == "unknown" and stopped[0]["max_comparison_steps"] == 1
    page_path.validate_page_dissent(
        stopped, text="Marie  Roy", feed=feed, cited_ids=["A1"], max_comparison_steps=1
    )
    refused(stopped, "records a 1-step dissent budget, but this run sealed")


def test_the_page_path_refuses_to_publish_dissent_its_own_validator_refuses(monkeypatch):
    """The producer checks what it built before it is published."""
    feed, witnesses = _three_witness_feed()

    def a_malformed_row(text, reported, *, max_comparison_steps):
        return {"compared": True}

    monkeypatch.setattr(page_path.dissent, "dissent_against", a_malformed_row)
    with pytest.raises(SchemaRefusal, match="page dissent\\[0\\]"):
        page_path.page_dissent("Marie  Roy", feed, ["A1"], witnesses, BUDGET)


# --- live serving, against the fakes ------------------------------------------------


def _catalogue(destination: Path, rows: str = "", **overrides: Any) -> Path:
    """The committed fixture catalogue with its Perlector row live, and any field changed."""
    committed = (ROOT / "config" / "serving_recipes.toml").read_text(encoding="utf-8")
    head = committed.split('[[profiles]]\nkind = "fixture"\nrecipe = "fake-perlector-v0"')[0]
    row = {**_live_row(_perlector_identity()), **overrides}
    row["preflight_digest"] = profile_preflight_digest(row)
    body = "\n".join(f"{key} = {_toml_value(value)}" for key, value in row.items())
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "serving_recipes_live_perlector.toml"
    path.write_text(f"{head}[[profiles]]\n{body}\n{rows}", encoding="utf-8")
    return path


@dataclasses.dataclass
class _Live:
    root: Path
    catalogue: Path
    protocol: Path
    scenario: str = "happy"
    recovery: Path = ROOT / "config" / "recovery.toml"


def _live_chain(
    base: Path,
    *,
    scenario: str = "happy",
    feed: dict[str, Any] | None = None,
    reask: int = 1,
    **row: Any,
) -> _Live:
    """A live tree through the Attestatores, sealed with the page re-ask budget `reask`."""
    protocol = _page_protocol(base / "config", **(feed or {}))
    catalogue = _catalogue(base / "config", **row)
    recovery = reask_recovery_config(base / "config", reask)
    extra = ("--serving-recipes-config", str(catalogue), "--recovery-config", str(recovery))
    _chain(base / "runs", protocol, *extra, scenario=scenario)
    return _Live(base / "runs", catalogue, protocol, scenario, recovery)


@pytest.fixture(scope="module")
def live_chain(tmp_path_factory):
    return _live_chain(tmp_path_factory.mktemp("live-pages"))


@pytest.fixture()
def live_tree(live_chain, tmp_path):
    root = tmp_path / "runs"
    shutil.copytree(live_chain.root, root)
    return dataclasses.replace(live_chain, root=root)


def _read_pages(tree: _Live, tmp_path, monkeypatch, *answers: ScriptedAnswer, extra=()):
    """Run the stage in this process against a scripted endpoint; return it and the exit."""
    endpoint = FakeEndpoint(
        served_model_id=SERVED_MODEL_ID,
        blob_store=_TreeBlobs(tree.root),
        assert_retained_before_next_request=True,
    )
    endpoint.script(*answers)
    factory = _serving_factory(
        endpoint, tree.catalogue, tmp_path / "logs", tmp_path / "pod-gpu.lock"
    )
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ROOT / PERLECTOR_PROGRAM),
            "--run-root",
            str(tree.root),
            "--run-id",
            "r",
            "--scenario",
            tree.scenario,
            "--serving-recipes-config",
            str(tree.catalogue),
            "--placement-tier",
            TIER,
            "--perlector-protocol-config",
            str(tree.protocol),
            "--models-config",
            str(MODELS),
            "--recovery-config",
            str(tree.recovery),
            *extra,
        ],
    )
    return endpoint, perlector.main(serving_factory=factory)


def _answers() -> tuple[ScriptedAnswer, ScriptedAnswer]:
    return tuple(
        ScriptedAnswer(content=PAGE_ANSWERS[ordinal], finish_reason="stop") for ordinal in (1, 2)
    )


def _scripted(answer: dict[str, Any], finish_reason: Any = "stop") -> ScriptedAnswer:
    return ScriptedAnswer(content=json.dumps(answer), finish_reason=finish_reason)


def _accountings(root: Path, ordinal: int) -> list[dict[str, Any]]:
    """A page's accountings in reading order: its first reading's, then its re-ask's."""
    return sorted(
        (r for r in _records(root, "page-accounting") if r["payload"]["page_ordinal"] == ordinal),
        key=lambda record: record["payload"]["answer_basis"] != "attempt-1",
    )


def _chat_requests(endpoint: FakeEndpoint) -> list[dict[str, Any]]:
    return [request for request in endpoint.requests if "messages" in request]


def test_a_live_page_is_sent_once_with_its_images_first_and_recorded_with_its_call(
    live_tree, tmp_path, monkeypatch
):
    root = live_tree.root
    endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert exit_code == 0
    requests = _chat_requests(endpoint)
    assert len(requests) == 2
    content = requests[0]["messages"][0]["content"]
    assert [part["type"] for part in content] == ["image_url", "text"]
    assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    sent = _records(root, "reader-sent")
    assert [record["payload"]["pass"] for record in sent] == ["page-reading"] * 2
    readings = _records(root, "page-reading")
    for reading, request in zip(readings, requests, strict=True):
        payload = reading["payload"]
        assert payload["disposition"] == "read" and payload["finish_reason"] == "stop"
        assert payload["engine_call"]["served_model_id"] == SERVED_MODEL_ID
        assert payload["capacity"]["capacity"]["fits"] is True
        assert request["max_tokens"] == payload["capacity"]["max_tokens"]
        assert payload["provenance"]["receipt_ref"] is not None
    assert len(_records(root, "perlectio")) == 3
    assert all(r["payload"]["engine_call"] for r in _records(root, "perlectio"))


def test_a_live_page_is_sent_the_perlectors_sealed_row_and_names_it(
    live_tree, tmp_path, monkeypatch
):
    root = live_tree.root
    endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert exit_code == 0
    policy, _digest = load_decoding_policy()
    row = chair_decoding(policy, "perlector")
    # Qwen's non-thinking values: the page reading samples, it is not greedy.
    assert row["temperature"] == 0.7
    readings = _records(root, "page-reading")
    receipts = {
        reading["payload"]["provenance"]["receipt_ref"]["relative_path"] for reading in readings
    }
    [receipt] = receipts
    seed = json.loads((root / "r" / receipt).read_text("utf-8"))["seed"]
    for request in _chat_requests(endpoint):
        assert {field: request[field] for field in row} == row
        assert request["seed"] == seed
    for reading in readings:
        assert reading["payload"]["sampling"] == {
            "chair": "perlector",
            "sent": recorded_wire_decimals(row),
            "effective": recorded_wire_decimals(engine_effective_sampling(row)),
        }
        call = json.loads(
            (
                root / "r" / reading["payload"]["engine_call"]["call_record_ref"]["relative_path"]
            ).read_text("utf-8")
        )
        assert call["sampling_effective"] == reading["payload"]["sampling"]["effective"]


def test_a_resumed_pass_refuses_a_reading_that_names_other_sampling(
    live_tree, tmp_path, monkeypatch
):
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert exit_code == 0
    original = page_path.chair_decoding

    def another_row(policy, chair):
        return {**original(policy, chair), "temperature": 0.0}

    monkeypatch.setattr(page_path, "chair_decoding", another_row)
    with pytest.raises(ContractError, match="names sampling other than the sealed Perlector row"):
        _read_pages(live_tree, tmp_path / "again", monkeypatch)


def test_two_pages_in_flight_publish_what_one_at_a_time_does(live_chain, tmp_path, monkeypatch):
    serial = dataclasses.replace(live_chain, root=tmp_path / "serial")
    wide = dataclasses.replace(live_chain, root=tmp_path / "wide")
    for tree in (serial, wide):
        shutil.copytree(live_chain.root, tree.root)
    _endpoint, exit_code = _read_pages(serial, tmp_path / "one", monkeypatch, *_answers())
    assert exit_code == 0
    endpoint, exit_code = _read_pages(
        wide, tmp_path / "two", monkeypatch, *_answers(), extra=("--perlector-concurrency", "2")
    )
    assert exit_code == 0 and len(_chat_requests(endpoint)) == 2
    # Each pass has its own serving receipt, so records naming a call differ; what
    # the pages were shown, what was measured and which acts were made do not.
    assert [r["payload"] for r in _records(wide.root, "page-feed")] == [
        r["payload"] for r in _records(serial.root, "page-feed")
    ]
    for kind, fields in (
        ("page-accounting", ("holds", "units", "lines")),
        ("act-region", ("n", "union_box_px", "cited_ids", "holds", "page_holds")),
        ("perlectio", ("n", "text", "holds", "page_holds")),
    ):
        assert [
            (r["subject_id"], [r["payload"][name] for name in fields])
            for r in _records(wide.root, kind)
        ] == [
            (r["subject_id"], [r["payload"][name] for name in fields])
            for r in _records(serial.root, kind)
        ]


@pytest.mark.parametrize(
    ("answer", "parse_state"),
    [
        (
            ScriptedAnswer(
                content="The page reads: nothing I can put as JSON.", finish_reason="stop"
            ),
            "malformed",
        ),
        (ScriptedAnswer(content='{"acts": [{"n": 1', finish_reason="length"), "cut-off"),
        (ScriptedAnswer(content="{}", finish_reason="eos_token"), "call-failed"),
    ],
    ids=["malformed", "cut-off", "unrecognized-stop"],
)
def test_an_answer_that_cannot_stand_is_held_whole_with_no_act_record(
    live_tree, tmp_path, monkeypatch, answer, parse_state
):
    root = live_tree.root
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, answer, answer)
    assert exit_code == 0
    readings = _records(root, "page-reading")
    assert [r["payload"]["parse_state"] for r in readings] == [parse_state] * 2
    outcome = "failed" if parse_state == "call-failed" else "held"
    for reading in readings:
        assert reading["outcome"] == outcome and reading["payload"]["disposition"] == "held"
        assert reading["payload"]["answer"] is None and reading["payload"]["problems"]
    assert _records(root, "act-region") == [] and _records(root, "perlectio") == []
    if parse_state == "call-failed":
        failure = readings[0]["payload"]["failure"]
        assert failure["code"] == "ENGINE_FINISH_REASON_UNRECOGNIZED"
        assert failure["raw_response_ref"] in readings[0]["inputs"]


def _bare_keys(answer: str) -> str:
    """`answer` with every grammar key written bare, as Qwen3.8 once replied (`{\nacts: [`)."""
    for key in (
        "acts",
        "set_aside",
        "n",
        "kind",
        "label",
        "cites",
        "text",
        "continues_from_previous_page",
        "continues_to_next_page",
    ):
        answer = answer.replace(f'"{key}":', f"{key}:")
    return answer


def test_a_stray_flag_keeps_the_page_and_bare_keys_are_quoted_and_recorded(
    live_tree, tmp_path, monkeypatch
):
    """Page 1 says its first act runs on from the page before; page 2's reply writes its
    keys bare. Both keep every entry: the flag stays on its entry for the Recensor, the
    repair is recorded on the reading, the response bytes stay as sent, and a later
    stage reading both pages again from those bytes finds the same. Synthetic text."""
    root = live_tree.root
    stray = json.loads(PAGE_ANSWERS[1])
    stray["acts"][1]["continues_from_previous_page"] = True
    bare = _bare_keys(PAGE_ANSWERS[2])
    assert page_answer.parse_page_answer(bare)[0] == "malformed"
    _endpoint, exit_code = _read_pages(
        live_tree,
        tmp_path,
        monkeypatch,
        _scripted(stray),
        ScriptedAnswer(content=bare, finish_reason="stop"),
    )
    assert exit_code == 0
    first, second = (r["payload"] for r in _records(root, "page-reading"))
    assert (first["parse_state"], first["problems"], first["answer"]) == ("parsed", [], stray)
    assert "answer_repairs" not in first
    assert (second["parse_state"], second["problems"]) == ("parsed", [])
    assert second["answer"] == json.loads(PAGE_ANSWERS[2])
    assert [repair["code"] for repair in second["answer_repairs"]] == ["unquoted-keys-quoted"]
    raw = (root / "r" / second["engine_call"]["raw_response_ref"]["relative_path"]).read_bytes()
    assert b"acts:" in raw and b'"acts":' not in raw
    perlectios = {
        (r["payload"]["page_ordinal"], r["payload"]["n"]): r["payload"]
        for r in _records(root, "perlectio")
    }
    assert sorted(perlectios) == [(1, 1), (1, 2), (2, 1)]
    assert perlectios[(1, 2)]["continues_from_previous_page"] is True
    rows = reading_acts(_denominator_context(live_tree))
    assert not [row for row in rows if row["class"] == "page-unread"]


def test_a_looping_reply_is_stopped_and_held_whole_and_read_again_from_its_bytes(
    live_tree, tmp_path, monkeypatch
):
    """A reply that repeats one line over and over is abandoned at the sealed thirtieth
    repeat, held `repetition-loop` (never parsed, never a cut-off), and a later stage
    reading the page again finds the same loop in the retained bytes. Synthetic text."""
    root = live_tree.root
    head = '{"acts": [{"n": 1, "kind": "other", "label": "index", "cites": ["A1"], "text": "\n'
    looped = ScriptedAnswer(
        content=head + "Tremblay, Jean f. 12\n" * 300 + '"}], "set_aside": []}',
        finish_reason="length",
    )
    endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, looped, _answers()[1])
    assert exit_code == 0
    assert endpoint.streams_stopped == 1
    first, second = _records(root, "page-reading")
    reading = first["payload"]
    assert (reading["parse_state"], reading["disposition"], first["outcome"]) == (
        "repetition-loop",
        "held",
        "held",
    )
    assert (reading["stop_reason"], reading["finish_reason"], reading["answer"]) == (
        "repetition-loop",
        None,
        None,
    )
    assert [problem["code"] for problem in reading["problems"]] == ["repetition-loop"]
    # Sent the page cap or the context left, never a bound from the answer's estimate.
    assert reading["capacity"]["max_tokens"] == _chat_requests(endpoint)[0]["max_tokens"]
    call = json.loads(
        (root / "r" / reading["engine_call"]["call_record_ref"]["relative_path"]).read_text("utf-8")
    )
    assert call["stream"]["stopped"] == {
        "kind": "line",
        "block_lines": 1,
        "repeats": 30,
        "line": 31,
    }
    raw = (root / "r" / reading["engine_call"]["raw_response_ref"]["relative_path"]).read_bytes()
    assert digest_bytes(raw) == reading["engine_call"]["response_sha256"]
    assert b"[DONE]" not in raw
    assert second["payload"]["parse_state"] == "parsed"
    assert not [r for r in _records(root, "act-region") if r["payload"]["page_ordinal"] == 1]
    rows = reading_acts(_denominator_context(live_tree))
    assert ("p1:unread", "page-unread", "held") in [
        (row["act_key"], row["class"], row["disposition"]) for row in rows
    ]


def test_a_failed_page_call_is_counted_unread_and_tallied_as_a_hard_failure(
    live_tree, tmp_path, monkeypatch
):
    """A page whose call failed stays in the denominator as a held `page-unread`
    unit, never zero acts, and its reading's `failed` outcome is what the
    run-level hard-failure cap counts, so a page-read run whose calls keep
    failing trips it."""
    root = live_tree.root
    transport = ScriptedAnswer(transport_failure="connection reset after dispatch")
    unrecognized = ScriptedAnswer(content="{}", finish_reason="eos_token")
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, transport, unrecognized)
    assert exit_code == 0
    readings = _records(root, "page-reading")
    assert [
        (r["outcome"], r["payload"]["parse_state"], r["payload"]["disposition"]) for r in readings
    ] == [("failed", "call-failed", "held")] * 2
    codes = [r["payload"]["failure"]["code"] for r in readings]
    assert codes[1] == "ENGINE_FINISH_REASON_UNRECOGNIZED"

    rows = reading_acts(_denominator_context(live_tree))
    assert [(row["act_key"], row["class"], row["disposition"]) for row in rows] == [
        ("p1:unread", "page-unread", "held"),
        ("p2:unread", "page-unread", "held"),
    ]
    for row, code in zip(rows, codes, strict=True):
        assert {"page-unread", code} <= set(row["hold_codes"])

    tally = tally_hard_failures(RunTree(root, "r"), load_hard_failure_policy())
    assert tally["by_kind"]["perlector:failed"] == sorted(r["subject_id"] for r in readings)
    assert tally["count"] == 2 and tally["breached"] is False


def test_a_failed_page_call_recorded_as_held_is_refused_by_the_denominator(
    live_tree, tmp_path, monkeypatch
):
    """A failed call recorded under the held outcome would hide it from the hard-failure cap."""
    failing = ScriptedAnswer(content="{}", finish_reason="eos_token")
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, failing, failing)
    assert exit_code == 0
    _forge_reading(live_tree, 1, lambda payload: None, outcome="held")
    with pytest.raises(FatalAccounting, match="not a page-path reading of this page"):
        reading_acts(_denominator_context(live_tree))


def test_a_parsed_answer_with_no_finish_reason_is_kept_and_held_whole(
    live_tree, tmp_path, monkeypatch
):
    root = live_tree.root
    unfinished = _scripted(json.loads(PAGE_ANSWERS[1]), finish_reason=None)
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, unfinished, _answers()[1])
    assert exit_code == 0
    reading = _records(root, "page-reading")[0]["payload"]
    assert (reading["parse_state"], reading["disposition"]) == ("parsed", "held")
    assert reading["stop_reason"] is None and reading["answer"] == json.loads(PAGE_ANSWERS[1])
    assert [problem["code"] for problem in reading["problems"]] == ["no-stop-reason"]
    assert not [r for r in _records(root, "act-region") if r["payload"]["page_ordinal"] == 1]


def test_an_answer_citing_an_id_the_feed_never_showed_is_held_with_its_answer(
    live_tree, tmp_path, monkeypatch
):
    root = live_tree.root
    answer = json.loads(PAGE_ANSWERS[1])
    answer["acts"][0]["cites"] = ["A1", "Q7"]
    scripted = _scripted(answer)
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, scripted, scripted)
    assert exit_code == 0
    reading = _records(root, "page-reading")[0]["payload"]
    assert (reading["parse_state"], reading["disposition"]) == ("parsed", "held")
    assert reading["answer"] == answer
    assert {problem["code"] for problem in reading["problems"]} == {"unknown-id"}


def test_a_real_act_set_aside_is_published_but_its_page_holds(live_tree, tmp_path, monkeypatch):
    root = live_tree.root
    answer = json.loads(PAGE_ANSWERS[1])
    answer["acts"] = answer["acts"][:1]
    answer["set_aside"] = [
        {"id": "A2", "reason": "not an entry"},
        {"id": "B2", "reason": "not an entry"},
    ]
    # a2's lines are left outside every entry, so the page is re-asked about them;
    # the re-ask sets them aside too. It is sent as soon as page 1's first reading is
    # published, before page 2's.
    reask = _scripted(
        {
            "acts": [],
            "set_aside": [{"id": f"L{line}", "reason": "not an entry"} for line in range(5, 10)],
        }
    )
    _endpoint, exit_code = _read_pages(
        live_tree, tmp_path, monkeypatch, _scripted(answer), reask, _answers()[1]
    )
    assert exit_code == 0
    reading = _records(root, "page-reading")[0]
    assert reading["payload"]["disposition"] == "read"
    first, last = (r["payload"] for r in _accountings(root, 1))
    for account in (first, last):
        dispositions = {unit["id"]: unit["disposition"] for unit in account["units"]}
        assert dispositions["A2"] == dispositions["B2"] == "set-aside"
        assert "unread-ink" in account["holds"]
    assert "reask-set-aside" in last["holds"]


def test_a_page_held_by_rule_e_holds_every_act_record_on_it(live_tree, tmp_path, monkeypatch):
    """Act 1's text is another record's: the witnesses' own text is not read, so rule (e)
    holds the page, and both its act records carry that code and are held."""
    root = live_tree.root
    answer = json.loads(PAGE_ANSWERS[1])
    answer["acts"][0]["text"] = "Le deux mai a été inhumé Jean Roy, âgé de trois jours"
    _endpoint, exit_code = _read_pages(
        live_tree, tmp_path, monkeypatch, _scripted(answer), _answers()[1]
    )
    assert exit_code == 0
    account = next(
        r["payload"] for r in _records(root, "page-accounting") if r["payload"]["page_ordinal"] == 1
    )
    assert account["rules"]["e"]["status"] == "hold"
    assert "witness-text-not-read" in account["holds"]
    page_one = [
        record
        for kind in ("act-region", "perlectio")
        for record in _records(root, kind)
        if record["payload"]["page_ordinal"] == 1
    ]
    assert len(page_one) == 4
    for record in page_one:
        assert record["outcome"] == "held"
        assert "witness-text-not-read" in record["payload"]["page_holds"]


def test_an_entry_with_no_readable_text_is_held_by_its_own_code(live_tree, tmp_path, monkeypatch):
    root = live_tree.root
    answer = json.loads(PAGE_ANSWERS[1])
    answer["acts"][1]["text"] = "  [[?]]  "
    _endpoint, exit_code = _read_pages(
        live_tree, tmp_path, monkeypatch, _scripted(answer), _answers()[1]
    )
    assert exit_code == 0
    reading = next(
        r
        for r in _records(root, "perlectio")
        if r["payload"]["page_ordinal"] == 1 and r["payload"]["n"] == 2
    )
    assert reading["outcome"] == "held"
    assert reading["payload"]["text"].strip() == ""
    assert "entry-no-readable-text" in reading["payload"]["holds"]


def test_a_held_page_reading_is_still_accounted(live_tree, tmp_path, monkeypatch):
    root = live_tree.root
    scripted = ScriptedAnswer(content="not json", finish_reason="stop")
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, scripted, scripted)
    assert exit_code == 0
    accounts = _records(root, "page-accounting")
    assert len(accounts) == 2
    for account in accounts:
        assert account["outcome"] == "held"
        assert account["payload"]["rules"]["a"]["status"] == "hold"


def test_a_page_over_the_rows_capacity_is_held_whole_and_never_sent(tmp_path, monkeypatch):
    tree = _live_chain(tmp_path / "small", max_model_len=400)
    endpoint, exit_code = _read_pages(tree, tmp_path, monkeypatch)
    assert exit_code == 0
    assert _chat_requests(endpoint) == []
    readings = _records(tree.root, "page-reading")
    assert [r["payload"]["parse_state"] for r in readings] == ["refused-capacity"] * 2
    capacity = readings[0]["payload"]["capacity"]["capacity"]
    assert capacity["fits"] is False and capacity["max_model_len"] == 400
    assert readings[0]["payload"]["provenance"]["receipt_ref"] is None
    assert _records(tree.root, "reader-sent") == []


def _without_testimony(monkeypatch, ordinal: int) -> None:
    """Stage 3 serving no page Testimonium for one page."""
    original = page_run.current_page_testimonia

    def dropped(context):
        current = original(context)
        page_id = page_run.exemplar_page_ids(context)[ordinal]
        return {page: rows for page, rows in current.items() if page != page_id}

    monkeypatch.setattr(page_run, "current_page_testimonia", dropped)


def test_a_page_no_witness_testified_to_is_held_by_name_and_the_pass_goes_on(
    live_tree, tmp_path, monkeypatch
):
    root = live_tree.root
    _without_testimony(monkeypatch, 2)
    endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, _answers()[0])
    assert exit_code == 0
    assert len(_chat_requests(endpoint)) == 1
    reading = next(r for r in _records(root, "page-reading") if r["payload"]["page_ordinal"] == 2)
    assert reading["outcome"] == "held"
    assert reading["payload"]["parse_state"] == "not-run"
    assert [p["code"] for p in reading["payload"]["problems"]] == ["no-witness-testimony"]
    feed = next(r for r in _records(root, "page-feed") if r["payload"]["page_ordinal"] == 2)
    assert feed["payload"]["witness_testimony"] == "none" and feed["payload"]["witnesses"] == []
    account = next(
        r for r in _records(root, "page-accounting") if r["payload"]["page_ordinal"] == 2
    )
    assert account["outcome"] == "held"
    assert account["payload"]["rules"]["a"]["status"] == "hold"


def test_a_live_pass_counts_only_the_pages_it_will_send(live_tree, tmp_path, monkeypatch):
    """The deadline holds one page's planned time and the chair's start, not two."""
    _without_testimony(monkeypatch, 2)
    deadline = datetime.now(timezone.utc) + timedelta(
        seconds=3 + page_run.planned_seconds_per_page(12288) + 600
    )
    endpoint, exit_code = _read_pages(
        live_tree,
        tmp_path,
        monkeypatch,
        _answers()[0],
        extra=("--reading-deadline", deadline.isoformat()),
    )
    assert exit_code == 0 and len(_chat_requests(endpoint)) == 1


def test_a_resumed_pass_never_asks_a_read_page_again(live_tree, tmp_path, monkeypatch):
    root = live_tree.root
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert exit_code == 0
    before = file_bytes_snapshot(root / "r" / "4_perlector")
    endpoint, exit_code = _read_pages(live_tree, tmp_path / "again", monkeypatch)
    assert exit_code == 0
    assert _chat_requests(endpoint) == []
    assert file_bytes_snapshot(root / "r" / "4_perlector") == before


def test_a_resumed_pass_adopts_its_sealed_measures_rather_than_measuring_again(
    live_tree, tmp_path, monkeypatch
):
    """A resume reads back the sealed accounting and dissent rather than measuring again."""
    root = live_tree.root
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert exit_code == 0
    before = file_bytes_snapshot(root / "r" / "4_perlector")

    def never(*_args, **_kwargs):
        raise AssertionError("a sealed measure was computed again")

    monkeypatch.setattr(page_run.page_accounting, "page_accounting", never)
    monkeypatch.setattr(page_path, "page_dissent", never)
    _endpoint, exit_code = _read_pages(live_tree, tmp_path / "again", monkeypatch)
    assert exit_code == 0
    assert file_bytes_snapshot(root / "r" / "4_perlector") == before


def test_the_denominator_reads_a_live_reading_again_from_its_retained_reply(
    live_tree, tmp_path, monkeypatch
):
    """The page-read denominator re-derives each live answer from the engine's own bytes."""
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert exit_code == 0
    for reading in _records(live_tree.root, "page-reading"):
        payload = reading["payload"]
        reply = page_path.retained_reply(
            lambda path: (live_tree.root / "r" / path).read_bytes(),
            payload["engine_call"],
            SERVING_READER,
        )
        assert reply == {
            "content": PAGE_ANSWERS[payload["page_ordinal"]],
            "finish_reason": "stop",
            "stop_reason": "stop",
        }
    args = stage_parser("page-read denominator").parse_args(
        [
            "--run-root",
            str(live_tree.root),
            "--run-id",
            "r",
            "--scenario",
            live_tree.scenario,
            "--serving-recipes-config",
            str(live_tree.catalogue),
            "--perlector-protocol-config",
            str(live_tree.protocol),
            "--models-config",
            str(MODELS),
        ]
    )
    acts = reading_acts(open_context(args, RECENSOR, serving_reader=SERVING_READER))
    assert [act["act_key"] for act in acts] == ["p1:1", "p1:2", "p2:1"]


def _denominator_context(tree: _Live, serving_reader=SERVING_READER):
    args = stage_parser("page-read denominator").parse_args(
        [
            "--run-root",
            str(tree.root),
            "--run-id",
            "r",
            "--scenario",
            tree.scenario,
            "--serving-recipes-config",
            str(tree.catalogue),
            "--perlector-protocol-config",
            str(tree.protocol),
            "--models-config",
            str(MODELS),
        ]
    )
    return open_context(args, RECENSOR, serving_reader=serving_reader)


def _forge_reading(tree: _Live, ordinal: int, change, outcome: str | None = None) -> None:
    """Rewrite one page reading (and its outcome, when given) and rewitness the
    Perlector's boundary, so only the denominator's own recomputation is left to
    catch it."""
    directory = tree.root / "r" / "4_perlector" / "artifacts" / "page-reading"
    [path] = [
        path
        for path in directory.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["payload"]["page_ordinal"] == ordinal
    ]
    record = json.loads(path.read_text(encoding="utf-8"))
    change(record["payload"])
    if outcome is not None:
        record["outcome"] = outcome
    record["self_hash"] = self_hash({k: v for k, v in record.items() if k != "self_hash"})
    path.write_bytes(canonical_bytes(record))
    rewitness_stage_boundary(RunTree(tree.root, "r"), PERLECTOR)


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (
            lambda payload: payload.update(request_digest="0" * 64),
            "records a request digest its feed's prompt and images do not give",
        ),
        (
            lambda payload: payload["capacity"].update(max_tokens=1),
            "records a capacity its sealed serving row does not give",
        ),
        (
            lambda payload: payload["sampling"]["sent"].update(seed_note="x"),
            "engine_call is not this page's request",
        ),
    ],
    ids=["request-digest", "capacity", "sampling"],
)
def test_the_denominator_binds_a_live_reading_to_its_pages_request(
    live_tree, tmp_path, monkeypatch, change, refusal
):
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert exit_code == 0
    _forge_reading(live_tree, 1, change)
    with pytest.raises(FatalAccounting, match=refusal):
        reading_acts(_denominator_context(live_tree))


def test_a_context_without_a_serving_reader_refuses_a_live_reading(
    live_tree, tmp_path, monkeypatch
):
    """A live call is read again or the run is refused; it is never taken from the record."""
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert exit_code == 0
    with pytest.raises(FatalAccounting, match="no serving reader"):
        reading_acts(_denominator_context(live_tree, serving_reader=None))


def test_the_denominator_measures_a_capacity_refusal_again(tmp_path, monkeypatch):
    tree = _live_chain(tmp_path / "small", max_model_len=400)
    _endpoint, exit_code = _read_pages(tree, tmp_path, monkeypatch)
    assert exit_code == 0
    rows = reading_acts(_denominator_context(tree))
    assert [row["act_key"] for row in rows] == ["p1:unread", "p2:unread"]
    _forge_reading(
        tree, 1, lambda payload: payload["capacity"]["capacity"].update(max_model_len=401)
    )
    with pytest.raises(FatalAccounting, match="records a capacity its sealed serving row"):
        reading_acts(_denominator_context(tree))


def test_a_resumed_pass_refuses_a_sealed_perlectio_with_invalid_dissent(
    live_tree, tmp_path, monkeypatch
):
    """A sealed reading whose dissent loses a shown witness is not adopted on resume."""
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert exit_code == 0
    original = page_run._sealed

    def one_witness_lost(context, kind, subject, attempt):
        record = original(context, kind, subject, attempt)
        if record is not None and kind == page_run.PERLECTIO_KIND:
            record = json.loads(json.dumps(record))
            record["payload"]["dissent"] = record["payload"]["dissent"][:-1]
        return record

    monkeypatch.setattr(page_run, "_sealed", one_witness_lost)
    with pytest.raises(ContractError, match="cannot stand behind .*one row per shown witness"):
        _read_pages(live_tree, tmp_path / "again", monkeypatch)


def test_a_page_sent_but_never_answered_is_sent_again_naming_the_first_send(
    live_tree, tmp_path, monkeypatch
):
    root = live_tree.root
    calls = []
    original = page_run.send_page_request

    def interrupted_on_page_two(client, **request):
        calls.append(request["what"])
        if len(calls) == 2:
            raise KeyboardInterrupt
        return original(client, **request)

    monkeypatch.setattr(page_run, "send_page_request", interrupted_on_page_two)
    with pytest.raises(KeyboardInterrupt):
        _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert len(_records(root, "page-reading")) == 1
    assert len(_records(root, "reader-sent")) == 2
    monkeypatch.setattr(page_run, "send_page_request", original)
    endpoint, exit_code = _read_pages(live_tree, tmp_path / "again", monkeypatch, _answers()[1])
    assert exit_code == 0
    assert len(_chat_requests(endpoint)) == 1
    sends = [r for r in _records(root, "reader-sent") if r["payload"]["act_key"] == "page-2"]
    assert sorted(r["payload"]["send"] for r in sends) == [1, 2]
    second = next(r for r in sends if r["payload"]["send"] == 2)
    first = next(r for r in sends if r["payload"]["send"] == 1)
    assert any(
        ref["relative_path"].endswith(f"{first['artifact_id']}.json") for ref in second["inputs"]
    )
    assert len(_records(root, "page-reading")) == 2


def test_a_retained_reply_no_record_names_stops_the_resume(live_tree, tmp_path, monkeypatch):
    root = live_tree.root
    original = page_run._publish_reading

    def stopped_before_page_two_is_recorded(state, page, request, result):
        if page.ordinal == 2:
            raise KeyboardInterrupt
        return original(state, page, request, result)

    monkeypatch.setattr(page_run, "_publish_reading", stopped_before_page_two_is_recorded)
    with pytest.raises(KeyboardInterrupt):
        _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    monkeypatch.setattr(page_run, "_publish_reading", original)
    with pytest.raises(ContractError, match="asking again would read them twice"):
        _read_pages(live_tree, tmp_path / "again", monkeypatch, _answers()[1])
    assert len(_records(root, "page-reading")) == 1


def _serial(tree: _Live, tmp_path, monkeypatch, *answers: ScriptedAnswer):
    return _read_pages(
        tree, tmp_path, monkeypatch, *answers, extra=("--perlector-concurrency", "1")
    )


def test_a_pass_stopped_between_the_accounting_and_the_act_records_resumes_them(
    live_tree, tmp_path, monkeypatch
):
    root = live_tree.root
    original = page_run.publish_act_records

    def stopped(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(page_run, "publish_act_records", stopped)
    with pytest.raises(KeyboardInterrupt):
        _serial(live_tree, tmp_path, monkeypatch, *_answers())
    [account] = _records(root, "page-accounting")
    assert _records(root, "act-region") == [] and _records(root, "perlectio") == []
    before = (root / "r" / "4_perlector" / "artifacts" / "page-accounting").glob("*.json")
    sealed = {path.name: path.read_bytes() for path in before}
    monkeypatch.setattr(page_run, "publish_act_records", original)
    endpoint, exit_code = _serial(live_tree, tmp_path / "again", monkeypatch, _answers()[1])
    assert exit_code == 0
    # Page 1 is not asked again: only page 2 is sent.
    assert len(_chat_requests(endpoint)) == 1
    directory = root / "r" / "4_perlector" / "artifacts" / "page-accounting"
    assert {name: (directory / name).read_bytes() for name in sealed} == sealed
    assert len(_records(root, "perlectio")) == 3
    for record in _records(root, "perlectio"):
        if record["payload"]["page_ordinal"] == 1:
            assert record["payload"]["page_accounting_ref"]["relative_path"].endswith(
                f"{account['artifact_id']}.json"
            )


def test_a_pass_stopped_between_an_act_region_and_its_perlectio_resumes_the_rest(
    live_tree, tmp_path, monkeypatch
):
    root = live_tree.root
    original = page_path.page_dissent
    calls = []

    def stopped_at_the_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return original(*args, **kwargs)

    monkeypatch.setattr(page_path, "page_dissent", stopped_at_the_second)
    with pytest.raises(KeyboardInterrupt):
        _serial(live_tree, tmp_path, monkeypatch, *_answers())
    regions = [r for r in _records(root, "act-region") if r["payload"]["page_ordinal"] == 1]
    [kept] = _records(root, "perlectio")
    assert len(regions) == 2
    [kept_path] = (root / "r" / "4_perlector" / "artifacts" / "perlectio").glob("*.json")
    kept_bytes = kept_path.read_bytes()
    monkeypatch.setattr(page_path, "page_dissent", original)
    endpoint, exit_code = _serial(live_tree, tmp_path / "again", monkeypatch, _answers()[1])
    assert exit_code == 0 and len(_chat_requests(endpoint)) == 1
    assert kept_path.read_bytes() == kept_bytes
    assert sorted(
        r["payload"]["n"] for r in _records(root, "perlectio") if r["payload"]["page_ordinal"] == 1
    ) == [1, 2]
    assert kept["subject_id"] in {r["subject_id"] for r in regions}


def test_a_retained_accounting_measured_from_other_inputs_is_not_adopted(
    live_tree, tmp_path, monkeypatch
):
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert exit_code == 0
    original = page_path.accounting_inputs

    def one_more_input(context, **kwargs):
        measured, references = original(context, **kwargs)
        page = artifact_id("exemplar", "page", kwargs["feed"]["page_id"])
        return measured, [*references, context.artifact_ref("exemplar", "page", page)]

    monkeypatch.setattr(page_path, "accounting_inputs", one_more_input)
    with pytest.raises(ContractError, match="retained page accounting was measured from other"):
        _read_pages(live_tree, tmp_path / "again", monkeypatch)


def test_a_retained_page_reading_or_perlectio_from_other_inputs_is_not_adopted():
    state = SimpleNamespace(context=SimpleNamespace())
    page = SimpleNamespace(page_id="pg_0000000000000001", feed_ref={"relative_path": "f"})
    request = page_run._Request(1, page_run.PAGE_READING_PASS)
    reading = {
        "payload": {
            "schema": page_run.PAGE_READING_SCHEMA,
            "feed_ref": {"relative_path": "f"},
            "attempt_ordinal": 1,
            "disposition": "read",
            "reask": None,
            "engine_call": None,
        },
    }
    page_run._check_adopted(state, page, request, reading)
    for changed in (
        {"payload": {**reading["payload"], "feed_ref": {}}},
        {"payload": {**reading["payload"], "schema": "perlector-page-reading.v0"}},
        {"payload": {**reading["payload"], "attempt_ordinal": 2}},
    ):
        with pytest.raises(ContractError, match="retained page reading .* not adopted"):
            page_run._check_adopted(state, page, request, {**reading, **changed})
    old = {"payload": {**reading["payload"], "schema": "perlector-page-reading.v0"}}
    with pytest.raises(ContractError, match="schema 'perlector-page-reading.v0'"):
        page_run._check_adopted(state, page, request, old)
    asked = {"payload": {**reading["payload"], "reask": {"named": []}}}
    with pytest.raises(FatalAccounting, match="names another re-ask"):
        page_run._check_adopted(state, page, request, asked)
    expected = {"page_accounting_ref": {"relative_path": "a"}, "text": "Marie Roy"}
    sealed = {**expected, "dissent": []}
    page_run._check_adopted_perlectio({"payload": sealed}, expected, "act_1")
    for changed in (
        {"page_accounting_ref": {"relative_path": "b"}},
        {"text": "Marie Roi"},
        {"added": True},
    ):
        with pytest.raises(ContractError, match="retained perlectio .* not adopted"):
            page_run._check_adopted_perlectio({"payload": {**sealed, **changed}}, expected, "act_1")


def test_the_page_deadline_refusal_speaks_in_pages(live_tree, tmp_path, monkeypatch):
    deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
    with pytest.raises(ContractError) as refused:
        _read_pages(
            live_tree,
            tmp_path,
            monkeypatch,
            *_answers(),
            extra=("--reading-deadline", deadline.isoformat()),
        )
    message = str(refused.value)
    per_page = page_run.planned_seconds_per_page(12288)
    assert f"at {per_page}s a page" in message and "reading 2 pages" in message
    assert "a call" not in message
    assert _records(live_tree.root, "reader-sent") == []


# --- flat witnesses, an ink-free page, an absent chair -----------------------------


def test_under_flat_witnesses_the_accounting_measures_the_regions_the_stage_cut(
    tmp_path, monkeypatch
):
    """Flat witnesses place nothing: act 1's region is its one cited Surya line, in the
    published act-region and in the accounting both, so the lines it leaves out are
    unread by both readings of the page."""
    tree = _live_chain(tmp_path / "flat", feed={"witness_units": "flat"}, reask=0)
    answer = json.loads(PAGE_ANSWERS[1])
    answer["acts"][0]["cites"] = ["A1", "B1", "L1"]
    answer["acts"][1]["cites"] = ["A2", "B2", "L5", "L6", "L7", "L8", "L9"]
    _endpoint, exit_code = _read_pages(
        tree, tmp_path, monkeypatch, _scripted(answer), _answers()[1]
    )
    assert exit_code == 0
    feed = next(
        r["payload"] for r in _records(tree.root, "page-feed") if r["payload"]["page_ordinal"] == 1
    )
    lines = {line["id"]: line["box_px"] for line in feed["surya"]["lines"]}
    units = {unit["id"]: unit["box_px"] for row in feed["witnesses"] for unit in row["units"]}
    published = {
        r["payload"]["n"]: r["payload"]
        for r in _records(tree.root, "act-region")
        if r["payload"]["page_ordinal"] == 1
    }
    regions = {n: payload["region_boxes_px"] for n, payload in published.items()}
    assert regions[1] == [lines["L1"]]
    assert regions[2] == [lines[f"L{i}"] for i in range(5, 10)]
    assert published[2]["union_box_px"] == _union(regions[2])
    # Shown in its own units, the witness would have added its box to the region.
    assert units["A1"] not in regions[1]
    account = next(
        r["payload"]
        for r in _records(tree.root, "page-accounting")
        if r["payload"]["page_ordinal"] == 1
    )
    for row in account["lines"]:
        expected = sorted(
            n for n, boxes in regions.items() if is_inside(lines[row["id"]], boxes, POLICY)
        )
        assert row["inside"] == expected
    unread = sorted(
        f["id"] for f in account["rules"]["d"]["findings"] if f["code"] == "unread-line"
    )
    assert unread == ["L2", "L3", "L4"]


def test_a_page_with_the_image_off_and_nothing_else_shown_is_held_by_name(tmp_path, monkeypatch):
    """An ink-free page with the image off shows nothing, as does any page whose every
    input is switched off: no request is made, and each page is held by name, fed and
    accounted. (The fixture's own ink-free page cannot be page-read: its Churro page
    Testimonium keeps no native capture to break into units.)"""
    tree = _live_chain(
        tmp_path / "nothing",
        feed={"page_image": "off", "witnesses": [], "surya_lines": False, "surya_blocks": False},
    )
    endpoint, exit_code = _read_pages(tree, tmp_path, monkeypatch)
    assert exit_code == 0
    assert _chat_requests(endpoint) == []
    for reading in _records(tree.root, "page-reading"):
        assert reading["outcome"] == "held" and reading["payload"]["parse_state"] == "not-run"
        assert [p["code"] for p in reading["payload"]["problems"]] == ["nothing-to-show"]
    for feed in _records(tree.root, "page-feed"):
        assert page_feed.shows_nothing(feed["payload"]) and feed["payload"]["prompt"] is None
    assert [r["payload"]["page_ordinal"] for r in _records(tree.root, "page-accounting")] == [1, 2]


def test_with_the_perlector_chair_absent_every_page_is_fed_and_accounted_not_read(tmp_path):
    shipped = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    start = shipped.index("[chairs.perlector]\n")
    configured = shipped[start : shipped.index("\n\n", start) + 1]
    absent = '[chairs.perlector]\nstate = "absent"\nreason = "no Perlector in this test run"\n'
    models = _roster(tmp_path, (configured, absent))
    protocol = _page_protocol(tmp_path / "config")
    result = None
    for program in CHAIN + (PERLECTOR_PROGRAM,):
        result = _run(program, tmp_path / "runs", protocol, "--models-config", str(models))
        if result.returncode != 0:
            break
    assert result is not None and result.returncode == 0, result.stderr
    root = tmp_path / "runs"
    feeds, accounts = _records(root, "page-feed"), _records(root, "page-accounting")
    assert [r["payload"]["page_ordinal"] for r in feeds] == [1, 2]
    assert [r["payload"]["page_ordinal"] for r in accounts] == [1, 2]
    assert all(r["payload"]["prompt"] is None for r in feeds)
    for reading in _records(root, "page-reading"):
        assert reading["payload"]["parse_state"] == "not-run"
        assert [p["code"] for p in reading["payload"]["problems"]] == ["chair-absent"]
    assert _records(root, "act-region") == []


def test_the_deadline_count_takes_every_operator_re_read_the_window_may_send(monkeypatch):
    """The live deadline count takes every operator re-read request of a page."""
    sends = []
    monkeypatch.setattr(page_run.live_calls, "sent_records", lambda *args: sends.append(args) or [])
    state = SimpleNamespace(context=SimpleNamespace(), live=True)
    page = SimpleNamespace(
        page_id="pg_0000000000000001",
        ordinal=1,
        not_run=[],
        rereads=[
            page_run._Request(3, page_run.PAGE_REREAD_PASS),
            page_run._Request(4, page_run.PAGE_REREAD_PASS),
        ],
    )
    assert page_run._left_to_send(state, [page], page_run._reread_requests) == 2
    assert [args[3] for args in sends] == [3, 4]


def _entries(*acts: tuple[str, list[str]]) -> list[dict]:
    return [{"act": {"kind": kind}, "cited_ids": ids} for kind, ids in acts]


@pytest.mark.parametrize(
    ("reread", "kept"),
    [
        # The same acts, read again with other or more ids: kept.
        ((("act", ["A1"]), ("act", ["A2", "L3"])), True),
        ((("act", ["A1"]), ("act", ["A2"]), ("other", ["S1"])), True),
        ((("act", ["A1"]), ("act", ["A2"]), ("act", [])), True),
        # Merged into one entry, whatever its text: not kept.
        ((("act", ["A1", "A2"]),), False),
        # Read as something other than an act: not kept.
        ((("act", ["A1"]), ("other", ["A2"])), False),
        # Left out, or split across two entries: not kept.
        ((("act", ["A1"]),), False),
        ((("act", ["A1"]), ("act", ["A2"]), ("act", ["A2"])), False),
        # Two acts kept, but one entry also reads the other's ink: not kept.
        ((("act", ["A1", "A2"]), ("act", ["A2"])), False),
    ],
)
def test_a_re_read_keeps_each_act_it_replaces_as_one_act_of_its_own(reread, kept):
    """The rule reads only cited ids and kinds, so it holds the same with or without a
    record detector."""
    replaced = _entries(("act", ["A1"]), ("act", ["A2"]))
    assert page_path.superseded_acts_kept(replaced, _entries(*reread)) is kept


def test_an_act_whose_ids_another_act_also_cites_is_followed_by_the_count():
    """A replaced reading whose second act also cited the first act's ids: a re-read
    that reads the two apart keeps both; one that merges them does not."""
    replaced = _entries(("act", ["A1"]), ("act", ["A1", "A2"]))
    assert page_path.superseded_acts_kept(replaced, _entries(("act", ["A1"]), ("act", ["A2"])))
    assert not page_path.superseded_acts_kept(replaced, _entries(("act", ["A1", "A2"])))


@pytest.mark.parametrize(
    ("replaced", "reread"),
    [
        # B has no id of its own; a new act Y stands where B was.
        (
            (("act", ["1", "2"]), ("act", ["2"])),
            (("act", ["1", "2"]), ("act", ["9"])),
        ),
        # A cites nothing; a new act Z stands where A was.
        ((("act", []), ("act", ["5"])), (("act", ["5"]), ("act", ["7"]))),
        # An act entry citing nothing makes up the count for B, or for an A citing nothing.
        ((("act", ["1", "2"]), ("act", ["2"])), (("act", ["1", "2"]), ("act", []))),
        ((("act", []), ("act", ["5"])), (("act", ["5"]), ("act", []))),
        # B has no id of its own and is read as `other`; a new act Z makes the count.
        (
            (("act", ["1", "2"]), ("act", ["2"])),
            (("act", ["1", "2"]), ("other", ["2"]), ("act", ["9"])),
        ),
    ],
)
def test_an_act_with_no_id_of_its_own_cannot_be_replaced_by_a_new_act(replaced, reread):
    assert not page_path.superseded_acts_kept(_entries(*replaced), _entries(*reread))


def test_only_a_re_read_that_kept_its_acts_becomes_the_next_ones_baseline():
    kept = [{"reading_holds": []}]
    dropped = [{"reading_holds": [page_path.SUPERSEDED_ACT_NOT_READ]}]
    assert page_path.keeps_counted(kept)
    assert not page_path.keeps_counted(dropped)
    assert not page_path.keeps_counted([])
