"""The Perlector's page path, proven end to end on the synthetic fixture and fake serving.

A run sealed with `reading_unit = "page"` reads every sealed page whole: a
`page-feed`, a `page-reading`, the page's `page-accounting` and, for a parsed
and valid answer, one `act-region` and one `perlectio.v2` per entry, each
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

import page_feed
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

from common.contracts.canonical import digest_of
from common.contracts.errors import ContractError
from common.contracts.identities import act_bindings, region_id, verify
from common.exemplar_boundary import read_sealed_page
from common.imaging import crop_png
from common.page_accounting import is_inside, load_page_accounting_policy
from common.runtree.store import RunTree
from conftest import file_bytes_snapshot, load_stage, programs_through
from operations.serving.config import profile_preflight_digest
from operations.serving.fakes import FakeEndpoint, ScriptedAnswer

ROOT = Path(__file__).resolve().parents[2]
PERLECTOR_PROGRAM = "pipeline/4_perlector/run.py"
CHAIN = programs_through("attestatores")
FIXTURE = tomllib.loads((ROOT / "proof" / "skeleton_fixture.toml").read_text(encoding="utf-8"))
PAGE_ANSWERS = {
    row["page_ordinal"]: row["answer"]
    for row in FIXTURE["page_answer"]
    if row["scenario"] == "happy"
}
TIERS = ("generic-24gb", "generic-48gb", "generic-80gb-plus")
POLICY = load_page_accounting_policy()

perlector = load_stage("4_perlector")


# --- helpers ----------------------------------------------------------------------


def _page_protocol(directory: Path, **feed: Any) -> Path:
    """The shipped protocol sealed to read whole pages, with any `[feed]` switch changed."""
    text = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    text = text.replace('reading_unit = "act"', 'reading_unit = "page"')
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
    """The shipped roster with chair blocks replaced, beside its fixture snapshots."""
    config_root = base / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    text = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in text
        text = text.replace(old, new)
    path = config_root / "models.toml"
    path.write_text(text, encoding="utf-8")
    return path


_STRUCTURE_DIGEST = tomllib.loads((ROOT / "config" / "models.toml").read_text(encoding="utf-8"))[
    "chairs"
]["designator_structure"]["digest_manifest"]


def _configured_block(chair: str, recipe: str) -> str:
    """A chair standing on the structure chair's fixture snapshot, as the stage-2 tests do."""
    return (
        f'[chairs.{chair}]\nstate = "configured"\nsource = "local-repository"\n'
        f'path = "designator_structure"\ndigest_manifest = "{_STRUCTURE_DIGEST}"\n'
        f'manifest = "manifests/designator_structure.json"\nserving_recipe = "{recipe}"\n'
        'license_note = "fixture identity only; no model weights or model license apply"\n'
    )


_ABSENT_SURYA = (
    '[chairs.designator_surya]\nstate = "absent"\n'
    'reason = "no Surya detector is configured for the offline walking skeleton"\n'
)
_ABSENT_DETECTOR = (
    '[chairs.secondary_proposer]\nstate = "absent"\n'
    'reason = "no secondary proposer is configured for the offline walking skeleton"\n'
)
_SURYA = (_ABSENT_SURYA, _configured_block("designator_surya", "fake-surya-v0"))
_DETECTOR = (_ABSENT_DETECTOR, _configured_block("secondary_proposer", "fake-secondary-v0"))


def _fixture_rows(recipe: str, chair: str) -> str:
    return "".join(
        f'\n[[profiles]]\nkind = "fixture"\nrecipe = "{recipe}"\nchair = "{chair}"\n'
        f'tier = "{tier}"\ndescription = "fixture rows under test"\n'
        for tier in TIERS
    )


_SURYA_ROWS = _fixture_rows("fake-surya-v0", "designator_surya")
_DETECTOR_ROWS = _fixture_rows("fake-secondary-v0", "secondary_proposer")


def _fixture_catalogue(base: Path, rows: str) -> Path:
    path = base / "chair-config" / "serving_recipes.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (ROOT / "config" / "serving_recipes.toml").read_text(encoding="utf-8") + rows,
        encoding="utf-8",
    )
    return path


# --- the fixture page path --------------------------------------------------------


@pytest.fixture(scope="module")
def page_tree(tmp_path_factory) -> tuple[Path, Path]:
    base = tmp_path_factory.mktemp("page-reading")
    protocol = _page_protocol(base / "config")
    _chain(base / "runs", protocol, through_perlector=True)
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
        assert payload["reading_unit"] == "page"
    feed = feeds[0]["payload"]
    assert feed["surya"]["absent"] == "no Surya page census was sealed in this run"
    assert feed["witness_testimony"] == "present"
    assert [(row["letter"], row["witness_label"]) for row in feed["witnesses"]] == [
        ("A", "attestator_1"),
        ("B", "attestator_3"),
    ]
    assert len(_records(root, "act-region")) == len(_records(root, "perlectio")) == 3
    # The act path read nothing: no reading of a Designator act was published.
    assert all(
        record["payload"]["schema"] == "perlectio.v2" for record in _records(root, "perlectio")
    )


def test_each_placed_act_region_is_the_union_of_its_cited_boxes_cut_from_the_ink(page_tree):
    root, _protocol = page_tree
    tree = RunTree(root, "r")
    feeds = {record["subject_id"]: record["payload"] for record in _records(root, "page-feed")}
    readings = {record["subject_id"]: record for record in _records(root, "page-reading")}
    placed = [r for r in _records(root, "act-region") if r["payload"]["union_box_px"]]
    assert sorted(region["payload"]["n"] for region in placed) == [1, 2]
    for region in placed:
        payload = region["payload"]
        boxes = page_feed.placement_boxes(feeds[payload["page_id"]])
        assert set(payload["cited_ids"]) <= set(boxes)
        cited = [boxes[identifier] for identifier in payload["cited_ids"] if boxes[identifier]]
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
    # The fixture has no Surya census, so rule (d) cannot be measured on any page,
    # and every act of the happy run is held for it.
    assert all(r["outcome"] == "held" for r in _records(root, "perlectio"))


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
    # Churro's line reads "... alpha beta": it stops short, so the reading departs.
    assert rows["B"]["cited_units"] == ["B1"] and rows["B"]["departed"] is True
    assert payload["holds"] == [] and payload["page_holds"] == ["unread-line-not-measured"]
    assert first["outcome"] == "held"
    last = next(
        record
        for record in _records(root, "perlectio")
        if record["payload"]["page_ordinal"] == 1 and record["payload"]["n"] == 2
    )
    assert last["payload"]["continues_to_next_page"] is True


def test_an_entry_citing_no_boxed_id_is_held_unplaced_with_no_crop(page_tree):
    root, _protocol = page_tree
    [region] = [r for r in _records(root, "act-region") if r["payload"]["page_ordinal"] == 2]
    payload = region["payload"]
    assert region["outcome"] == "held"
    assert payload["union_box_px"] is None and payload["act_class"] == "reading-unplaced"
    assert payload["holds"] == ["reading-unplaced"]
    assert (payload["region_id"], payload["image_path"], payload["transform"]) == (None,) * 3
    [reading] = [r for r in _records(root, "perlectio") if r["payload"]["page_ordinal"] == 2]
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


def test_each_page_is_accounted_and_holds_only_for_reasons_it_names(page_tree):
    """Page 1 is read whole and placed; it holds only because this tree has no
    Surya census, so rule (d) cannot be measured. Page 2's one entry cites no
    boxed id: it is unplaced, has no region to measure truncation over, and the
    page's ink lies outside every reading region."""
    root, _protocol = page_tree
    accounts = {r["payload"]["page_ordinal"]: r for r in _records(root, "page-accounting")}
    assert set(accounts) == {1, 2}
    first = accounts[1]["payload"]
    assert first["schema"] == "page-accounting.v1" and accounts[1]["outcome"] == "held"
    assert first["holds"] == ["unread-line-not-measured"]
    assert {unit["disposition"] for unit in first["units"]} == {"cited"}
    assert first["rules"]["i"]["status"] == "not-applicable"
    assert accounts[2]["payload"]["holds"] == [
        "reading-incomplete",
        "reading-unplaced",
        "unread-ink",
        "unread-line-not-measured",
    ]


def test_the_recensor_refuses_a_page_read_tree_by_name(page_tree, tmp_path):
    root, protocol = page_tree
    copy = tmp_path / "runs"
    shutil.copytree(root, copy)
    result = _run("pipeline/5_recensor/run.py", copy, protocol)
    assert result.returncode != 0
    assert "page-read trees are not yet counted downstream" in result.stderr
    assert not (copy / "r" / "5_recensor" / "artifacts").exists()


def test_a_page_read_pass_refuses_to_read_one_act_by_name(page_tree, tmp_path):
    root, protocol = page_tree
    copy = tmp_path / "runs"
    shutil.copytree(root, copy)
    before = file_bytes_snapshot(copy)
    result = _run(PERLECTOR_PROGRAM, copy, protocol, "--act", "act_0000000000000001")
    assert result.returncode != 0
    assert "run the pass without --act" in result.stderr
    assert file_bytes_snapshot(copy) == before


def test_the_orchestrator_reads_the_pages_and_stops_at_the_recensor(tmp_path):
    protocol = _page_protocol(tmp_path / "config")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "orchestrator" / "run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-id",
            "r",
            "--run-root",
            str(tmp_path / "runs"),
            "--perlector-protocol-config",
            str(protocol),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "page-read trees are not yet counted downstream" in result.stdout + result.stderr
    assert len(_records(tmp_path / "runs", "page-reading")) == 2
    assert (tmp_path / "runs" / "r" / "4_perlector" / "artifacts" / "stage-seal").exists()


def test_the_page_path_refuses_a_blind_read_or_a_sampled_control_by_name():
    base = {"blind_read": "off", "nuda_per_mille": 0, "perlector_instrument_per_mille": 0}
    page_run.refuse_unsupported_settings(SimpleNamespace(**base))
    for name, value in (
        ("blind_read", "fed"),
        ("blind_read", "saved"),
        ("nuda_per_mille", 5),
        ("perlector_instrument_per_mille", 5),
    ):
        with pytest.raises(ContractError, match=name):
            page_run.refuse_unsupported_settings(SimpleNamespace(**{**base, name: value}))


# Each switch changes what the page is shown, so it changes the sealed feed and,
# where it changes the text, page 1's prompt (page 2 has one unboxed unit per
# witness, which no witness switch changes). The fixture page is smaller than
# the legible edge, so `full` changes the render's record, not the prompt; and
# Surya is absent from this tree, so its two switches change only the switches.
@pytest.mark.parametrize(
    ("feed", "prompt_changes"),
    [
        ({"page_image": "full"}, False),
        ({"page_image": "off"}, True),
        ({"witnesses": ["attestator_3"]}, True),
        ({"witness_units": "flat"}, True),
        ({"witness_coordinates": False}, True),
        ({"page_overlay": "boxes"}, True),
        ({"surya_lines": False}, False),
        ({"surya_blocks": False}, False),
    ],
    ids=lambda value: str(value),
)
def test_each_feed_switch_changes_the_sealed_feed(page_tree, tmp_path, feed, prompt_changes):
    root, _protocol = page_tree
    protocol = _page_protocol(tmp_path / "config", **feed)
    _chain(tmp_path / "runs", protocol, through_perlector=True)
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
        assert len(shown) == 1 and len(paths & _testimonium_paths(tmp_path / "runs", 1)) == 2
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


@pytest.fixture(scope="module")
def surya_tree(tmp_path_factory) -> tuple[Path, Path, tuple[str, ...]]:
    """A page-read tree whose stage 2 ran Surya against the fixture's declared rows."""
    base = tmp_path_factory.mktemp("surya-pages")
    protocol = _page_protocol(base / "config")
    flags = (
        "--models-config",
        str(_roster(base, _SURYA)),
        "--serving-recipes-config",
        str(_fixture_catalogue(base, _SURYA_ROWS)),
    )
    _chain(base / "runs", protocol, *flags, through_perlector=True)
    return base / "runs", protocol, flags


def test_the_feed_shows_the_stage_two_surya_records_as_sealed(surya_tree):
    root, _protocol, _flags = surya_tree
    census = {r["subject_id"]: r["payload"] for r in _designator_records(root, "surya-page")}
    lines = {r["subject_id"]: r["payload"] for r in _designator_records(root, "surya-line")}
    blocks = {r["subject_id"]: r["payload"] for r in _designator_records(root, "surya-block")}
    assert census and lines and blocks
    for record in _records(root, "page-feed"):
        feed = record["payload"]
        page = census[feed["page_id"]]
        surya = feed["surya"]
        assert surya["layout_error"] is page["layout_error"] is False
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


def test_with_surya_sealed_every_detected_line_is_measured(surya_tree):
    root, _protocol, _flags = surya_tree
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


def _detector_tree(base: Path, monkeypatch, detections: list[dict[str, Any]]):
    """A page-read tree whose stage-2 record detector declared `detections`.

    The fixture detector states no detection cap, so its records would leave
    rule (i) unmeasured; here it states one (`max_det`), as the in-process
    detector does, so the rule is measured over real stage-2 records.
    """
    protocol = _page_protocol(base / "config")
    flags = (
        "--models-config",
        str(_roster(base, _DETECTOR)),
        "--serving-recipes-config",
        str(_fixture_catalogue(base, _DETECTOR_ROWS)),
    )
    root = base / "runs"
    _chain(root, protocol, *flags, programs=programs_through("ink-map"))
    designator = load_stage("2_designator")
    original = designator.fixture_record_detector

    def declared(_rows, identity, details):
        detector = original(detections, identity, details)
        return dataclasses.replace(detector, run_facts={**detector.run_facts, "max_det": 300})

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
            *flags,
        ],
    )
    assert designator.main() == 0
    _chain(
        root,
        protocol,
        *flags,
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


# --- the answer's entries -----------------------------------------------------------


def test_entries_with_one_union_box_are_held_and_keep_their_own_ids():
    box = {"x": 1, "y": 2, "w": 3, "h": 4}
    feed = {
        "switches": {"witness_units": "own"},
        "witnesses": [{"units": [{"id": "A1", "box_px": box, "text": "x"}]}],
        "surya": None,
    }
    entry = {
        "kind": "act",
        "cites": ["A1"],
        "text": "x",
        "continues_from_previous_page": False,
        "continues_to_next_page": False,
    }
    answer = {"acts": [{**entry, "n": 1}, {**entry, "n": 2}], "set_aside": []}
    entries = page_run.answer_entries(answer, feed)
    assert [entry["holds"] for entry in entries] == [["duplicate-region"]] * 2
    assert [entry["union_box_px"] for entry in entries] == [box, box]
    attempt = page_run.page_reading_attempt("pg_0000000000000001")
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


def test_a_shared_union_box_does_not_hold_the_page_but_an_unknown_id_does():
    validated = {
        "problems": [
            {"code": "duplicate-region", "ns": [1, 2]},
            {"code": "unknown-id", "id": "Q7"},
        ]
    }
    assert [p["code"] for p in page_run.page_problems(validated)] == ["unknown-id"]


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

    [marked] = page_run._dissent("Marie  Roy", feed, ["A1"], [witness(True)])
    assert marked["compared"] is True and marked["departed"] is False
    [plain] = page_run._dissent("Marie  Roy", feed, ["A1"], [witness(False)])
    assert plain["departed"] is True


# --- live serving, against the fakes ------------------------------------------------


def _catalogue(destination: Path, rows: str = "", **overrides: Any) -> Path:
    """The committed fixture catalogue with its Perlector row live, and any field changed."""
    source = (ROOT / "config" / "serving_recipes.toml").read_text(encoding="utf-8")
    head = source.split('[[profiles]]\nkind = "fixture"\nrecipe = "fake-perlector-v0"')[0]
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
    extra: tuple[str, ...] = ()
    scenario: str = "happy"


def _live_chain(
    base: Path,
    *,
    surya: bool = False,
    scenario: str = "happy",
    feed: dict[str, Any] | None = None,
    **row: Any,
) -> _Live:
    catalogue = _catalogue(base / "config", _SURYA_ROWS if surya else "", **row)
    protocol = _page_protocol(base / "config", **(feed or {}))
    extra = ("--models-config", str(_roster(base, _SURYA))) if surya else ()
    _chain(
        base / "runs",
        protocol,
        "--serving-recipes-config",
        str(catalogue),
        *extra,
        scenario=scenario,
    )
    return _Live(base / "runs", catalogue, protocol, extra, scenario)


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
            *tree.extra,
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
    for reading in readings:
        assert reading["outcome"] == "held" and reading["payload"]["disposition"] == "held"
        assert reading["payload"]["answer"] is None and reading["payload"]["problems"]
    assert _records(root, "act-region") == [] and _records(root, "perlectio") == []
    if parse_state == "call-failed":
        failure = readings[0]["payload"]["failure"]
        assert failure["code"] == "ENGINE_FINISH_REASON_UNRECOGNIZED"
        assert failure["raw_response_ref"] in readings[0]["inputs"]


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
    _endpoint, exit_code = _read_pages(
        live_tree, tmp_path, monkeypatch, _scripted(answer), _answers()[1]
    )
    assert exit_code == 0
    reading = _records(root, "page-reading")[0]
    assert reading["payload"]["disposition"] == "read"
    account = next(
        r["payload"] for r in _records(root, "page-accounting") if r["payload"]["page_ordinal"] == 1
    )
    dispositions = {unit["id"]: unit["disposition"] for unit in account["units"]}
    assert dispositions["A2"] == dispositions["B2"] == "set-aside"
    assert "unread-ink" in account["holds"]


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
    """Stage 3 serving no page Testimonium for one page, as for a page with no proposed act."""
    original = page_run.current_page_testimonia

    def dropped(context, hooks, proposal_regions):
        current = original(context, hooks, proposal_regions)
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
    """Rule (e) and dissent are bounded by a clock: a resume reads back what was sealed."""
    root = live_tree.root
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert exit_code == 0
    before = file_bytes_snapshot(root / "r" / "4_perlector")

    def never(*_args, **_kwargs):
        raise AssertionError("a sealed measure was computed again")

    monkeypatch.setattr(page_run.page_accounting, "page_accounting", never)
    monkeypatch.setattr(page_run, "dissent_against", never)
    _endpoint, exit_code = _read_pages(live_tree, tmp_path / "again", monkeypatch)
    assert exit_code == 0
    assert file_bytes_snapshot(root / "r" / "4_perlector") == before


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

    def stopped_before_page_two_is_recorded(state, page, result):
        if page.ordinal == 2:
            raise KeyboardInterrupt
        return original(state, page, result)

    monkeypatch.setattr(page_run, "_publish_reading", stopped_before_page_two_is_recorded)
    with pytest.raises(KeyboardInterrupt):
        _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    monkeypatch.setattr(page_run, "_publish_reading", original)
    with pytest.raises(ContractError, match="asking again would read them twice"):
        _read_pages(live_tree, tmp_path / "again", monkeypatch, _answers()[1])
    assert len(_records(root, "page-reading")) == 1


# --- flat witnesses, an ink-free page, an absent chair -----------------------------


def test_under_flat_witnesses_the_accounting_measures_the_regions_the_stage_cut(
    tmp_path, monkeypatch
):
    """Flat witnesses place nothing: act 1's region is its one cited Surya line, in the
    published act-region and in the accounting both, so the lines it leaves out are
    unread by both readings of the page."""
    tree = _live_chain(tmp_path / "flat", surya=True, feed={"witness_units": "flat"})
    answer = json.loads(PAGE_ANSWERS[1])
    answer["acts"][0]["cites"] = ["A1", "B1", "L1"]
    answer["acts"][1]["cites"] = ["A2", "B2", "L5-L9"]
    _endpoint, exit_code = _read_pages(
        tree, tmp_path, monkeypatch, _scripted(answer), _answers()[1]
    )
    assert exit_code == 0
    feed = next(
        r["payload"] for r in _records(tree.root, "page-feed") if r["payload"]["page_ordinal"] == 1
    )
    lines = {line["id"]: line["box_px"] for line in feed["surya"]["lines"]}
    units = {unit["id"]: unit["box_px"] for row in feed["witnesses"] for unit in row["units"]}
    regions = {
        r["payload"]["n"]: r["payload"]["union_box_px"]
        for r in _records(tree.root, "act-region")
        if r["payload"]["page_ordinal"] == 1
    }
    assert regions[1] == lines["L1"]
    assert regions[2] == _union([lines[f"L{i}"] for i in range(5, 10)])
    # Shown in its own units, the witness would have widened the region by its box.
    assert regions[1] != _union([lines["L1"], units["A1"]])
    account = next(
        r["payload"]
        for r in _records(tree.root, "page-accounting")
        if r["payload"]["page_ordinal"] == 1
    )
    for row in account["lines"]:
        expected = sorted(
            n for n, box in regions.items() if is_inside(lines[row["id"]], [box], POLICY)
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
