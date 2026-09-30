"""The page-read denominator: what a run read page by page counts, proven from its records.

The trees are the synthetic fixture's `happy` and `page-review` scenarios read with
`reading_unit = "page"`: happy places and reads every entry; page-review leaves page
2's entry unplaced, so the page accounting holds it. Each forgery rewrites one
Perlector record and rewitnesses the stage's boundary, so the only thing left
to catch it is the denominator's own recomputation.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import ContractError, FatalAccounting, IdentityRefusal
from common.contracts.identities import act_id as derive_act_id
from common.contracts.stages import PERLECTOR, RECENSOR
from common.exemplar_boundary import verify_reading_region_lineage
from common.runtree.store import RunTree
from common.stage import (
    PAGE_READING_ROW_FIELDS,
    READING_ACT_FIELDS,
    open_context,
    page_readings,
    reading_acts,
    reading_denominator,
    stage_parser,
)
from conftest import (
    programs_through,
    rebind_stage_seal_artifact,
    rewitness_stage_boundary,
    run_stage,
)

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "r"


def _page_protocol(directory: Path) -> Path:
    text = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    assert 'reading_unit = "act"' in text
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "perlector_protocol.toml"
    path.write_text(text.replace('reading_unit = "act"', 'reading_unit = "page"'), "utf-8")
    return path


def _page_tree(base: Path, scenario: str) -> tuple[Path, Path, str]:
    protocol = _page_protocol(base / "config")
    root = base / "runs"
    for program in programs_through("perlector"):
        result = run_stage(root, RUN_ID, scenario, program, perlector_protocol_config=protocol)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return root, protocol, scenario


@pytest.fixture(scope="module")
def happy_tree(tmp_path_factory) -> tuple[Path, Path, str]:
    return _page_tree(tmp_path_factory.mktemp("happy"), "happy")


@pytest.fixture(scope="module")
def review_tree(tmp_path_factory) -> tuple[Path, Path, str]:
    return _page_tree(tmp_path_factory.mktemp("page-review"), "page-review")


def _copy(tree: tuple[Path, Path, str], tmp_path: Path) -> tuple[Path, Path, str]:
    root, protocol, scenario = tree
    shutil.copytree(root, tmp_path / "runs")
    return tmp_path / "runs", protocol, scenario


def _context(tree: tuple[Path, Path, str]):
    root, protocol, scenario = tree
    args = stage_parser("page-read denominator").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            RUN_ID,
            "--scenario",
            scenario,
            "--perlector-protocol-config",
            str(protocol),
        ]
    )
    return open_context(args, RECENSOR)


def _records(root: Path, kind: str) -> list[tuple[Path, dict[str, Any]]]:
    directory = root / RUN_ID / "4_perlector" / "artifacts" / kind
    return sorted(
        ((path, json.loads(path.read_text(encoding="utf-8"))) for path in directory.glob("*.json")),
        key=lambda item: (item[1]["payload"].get("page_ordinal"), item[1]["payload"].get("n", 0)),
    )


def _one(root: Path, kind: str, ordinal: int, n: int | None = None):
    [found] = [
        item
        for item in _records(root, kind)
        if item[1]["payload"]["page_ordinal"] == ordinal
        and (n is None or item[1]["payload"].get("n") == n)
    ]
    return found


def _write(path: Path, record: dict[str, Any]) -> None:
    record["self_hash"] = self_hash(
        {key: value for key, value in record.items() if key != "self_hash"}
    )
    path.write_bytes(canonical_bytes(record))


def _forge(root: Path, kind: str, ordinal: int, n: int | None, change: Callable) -> None:
    path, record = _one(root, kind, ordinal, n)
    forged = copy.deepcopy(record)
    change(forged)
    _write(path, forged)
    rewitness_stage_boundary(RunTree(root, RUN_ID), PERLECTOR)


def _drop_act_records(root: Path, ordinal: int) -> None:
    for kind in ("act-region", "perlectio"):
        for path, record in _records(root, kind):
            if record["payload"]["page_ordinal"] == ordinal:
                path.unlink()


# --- the rows ---------------------------------------------------------------------


def test_a_happy_page_tree_counts_every_entry_it_read(happy_tree):
    context = _context(happy_tree)
    denominator = reading_denominator(context)
    assert denominator["reading_unit"] == "page"
    pages, acts = denominator["pages"], denominator["acts"]
    assert pages == page_readings(context) and acts == reading_acts(context)
    assert sorted(pages) == [1, 2]
    for row in pages.values():
        assert set(row) == PAGE_READING_ROW_FIELDS
        assert (row["parse_state"], row["disposition"], row["finish_reason"]) == (
            "parsed",
            "read",
            "stop",
        )
    assert [pages[1]["act_count"], pages[2]["act_count"]] == [2, 1]
    assert [act["act_key"] for act in acts] == ["p1:1", "p1:2", "p2:1"]
    root = happy_tree[0]
    for act in acts:
        assert set(act) == READING_ACT_FIELDS
        assert (act["class"], act["kind"], act["disposition"], act["hold_codes"]) == (
            "reading",
            "act",
            "read",
            [],
        )
        _path, perlectio = _one(root, "perlectio", act["page_ordinal"], act["n"])
        assert perlectio["subject_id"] == act["act_id"]
        assert act["perlectio_ref"]["relative_path"].endswith(f"{perlectio['artifact_id']}.json")
        _path, region = _one(root, "act-region", act["page_ordinal"], act["n"])
        assert act["region_ref"]["relative_path"].endswith(f"{region['artifact_id']}.json")
        assert act["reading_ref"] == pages[act["page_ordinal"]]["reading_ref"]
        assert act["accounting_ref"] == pages[act["page_ordinal"]]["accounting_ref"]
    # The continuation is the answer's own flag, carried as given.
    flags = [(a["continues_from_previous_page"], a["continues_to_next_page"]) for a in acts]
    assert flags == [(False, False), (False, True), (True, False)]


def test_an_unplaced_entry_is_counted_and_held_by_the_page_accounting(review_tree):
    acts = reading_acts(_context(review_tree))
    assert [act["act_key"] for act in acts] == ["p1:1", "p1:2", "p2:1"]
    unplaced = acts[2]
    assert (unplaced["class"], unplaced["disposition"]) == ("reading-unplaced", "held")
    assert unplaced["region_ref"] is not None and unplaced["perlectio_ref"] is not None
    assert unplaced["hold_codes"] == [
        "reading-unplaced",
        "truncation-not-classified",
        "unread-ink",
        "unread-line",
    ]
    assert {act["disposition"] for act in acts[:2]} == {"read"}


def test_a_page_whose_answer_was_not_read_is_one_held_page_unread_row(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    _drop_act_records(root, 2)

    def malformed(record):
        record["outcome"] = "held"
        record["payload"].update(
            parse_state="malformed",
            answer=None,
            problems=[{"code": "json-invalid", "detail": "the reply is not JSON"}],
            disposition="held",
        )

    _forge(root, "page-reading", 2, None, malformed)
    _forge(
        root,
        "page-accounting",
        2,
        None,
        lambda record: (
            record.update(outcome="held"),
            record["payload"].update(holds=["page-answer-incomplete"]),
        ),
    )
    context = _context(tree)
    pages = page_readings(context)
    assert (pages[2]["parse_state"], pages[2]["disposition"], pages[2]["act_count"]) == (
        "malformed",
        "held",
        None,
    )
    row = reading_acts(context)[-1]
    page_id = pages[2]["page_id"]
    assert row == {
        "act_id": derive_act_id(page_id, "page-unread", {"x": 0, "y": 0, "w": 200, "h": 260}),
        "act_key": "p2:unread",
        "page_id": page_id,
        "page_ordinal": 2,
        "n": None,
        "kind": "act",
        "class": "page-unread",
        "disposition": "held",
        "region_ref": None,
        "reading_ref": pages[2]["reading_ref"],
        "perlectio_ref": None,
        "accounting_ref": pages[2]["accounting_ref"],
        "hold_codes": ["json-invalid", "page-answer-incomplete", "page-unread"],
        "continues_from_previous_page": None,
        "continues_to_next_page": None,
    }


def test_a_read_answer_naming_no_act_is_one_held_page_blank_row(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    _drop_act_records(root, 2)
    _path, feed = _one(root, "page-feed", 2)
    ids = [unit["id"] for witness in feed["payload"]["witnesses"] for unit in witness["units"]]
    ids += [line["id"] for line in feed["payload"]["surya"]["lines"]]
    ids += [block["id"] for block in feed["payload"]["surya"]["blocks"]]

    def blank(record):
        record["payload"]["answer"] = {
            "acts": [],
            "set_aside": [{"id": identifier, "reason": "blank paper"} for identifier in ids],
        }

    _forge(root, "page-reading", 2, None, blank)
    context = _context(tree)
    assert page_readings(context)[2]["act_count"] == 0
    row = reading_acts(context)[-1]
    assert (row["class"], row["act_key"], row["disposition"]) == ("page-blank", "p2:blank", "held")
    assert row["hold_codes"] == ["page-blank-unconfirmed"]
    assert row["region_ref"] is None and row["perlectio_ref"] is None


# --- refusals ---------------------------------------------------------------------


def test_a_sealed_page_with_no_page_reading_is_refused_never_zero(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    _drop_act_records(root, 2)
    for kind in ("page-accounting", "page-reading"):
        path, _record = _one(root, kind, 2)
        path.unlink()
    rebind_stage_seal_artifact(RunTree(root, RUN_ID), PERLECTOR)
    with pytest.raises(FatalAccounting, match="has no page reading"):
        reading_acts(_context(tree))


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (
            lambda p: p.update(union_box_px={**p["union_box_px"], "w": p["union_box_px"]["w"] + 1}),
            "union_box_px",
        ),
        (lambda p: p.update(n=2), "n"),
        (lambda p: p.update(cites=["A2", "B2"]), "cites"),
        (lambda p: p.update(cited_ids=["A1"]), "cited_ids"),
        (lambda p: p.update(page_holds=["unread-ink"]), "page_holds"),
        (lambda p: p.update(holds=[]), None),
    ],
    ids=["union", "n", "cites", "cited-ids", "page-holds", "own-holds"],
)
def test_a_forged_act_region_is_refused(happy_tree, review_tree, tmp_path, change, field):
    # Own holds are recomputed too: page-review's unplaced entry must say so.
    tree = _copy(review_tree if field is None else happy_tree, tmp_path)
    ordinal = 2 if field is None else 1
    _forge(tree[0], "act-region", ordinal, 1, lambda record: change(record["payload"]))
    with pytest.raises(FatalAccounting, match="act-region does not match") as refusal:
        reading_acts(_context(tree))
    if field is not None:
        assert field in str(refusal.value)


def test_a_reading_that_calls_a_valid_answer_held_is_refused(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)

    def held(record):
        record["outcome"] = "held"
        record["payload"]["disposition"] = "held"

    _forge(tree[0], "page-reading", 1, None, held)
    with pytest.raises(FatalAccounting, match="but its answer against its feed is 'read'"):
        reading_acts(_context(tree))


def test_a_perlectio_whose_continuation_departs_from_the_answer_is_refused(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    _forge(
        tree[0],
        "perlectio",
        1,
        2,
        lambda record: record["payload"].update(continues_to_next_page=False),
    )
    with pytest.raises(FatalAccounting, match="continues_to_next_page"):
        reading_acts(_context(tree))


def test_a_region_whose_crop_is_another_entrys_pixels_fails_its_lineage(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    _path, other = _one(root, "act-region", 1, 2)
    other_crop = {
        "relative_path": other["payload"]["image_path"],
        "sha256": other["payload"]["image_sha256"],
    }

    def swap(record):
        payload = record["payload"]
        own = {"relative_path": payload["image_path"], "sha256": payload["image_sha256"]}
        record["inputs"] = [other_crop if ref == own else ref for ref in record["inputs"]]
        payload["image_path"], payload["image_sha256"] = (
            other_crop["relative_path"],
            other_crop["sha256"],
        )

    _forge(root, "act-region", 1, 1, swap)
    with pytest.raises(FatalAccounting, match="does not trace to the Exemplar"):
        reading_acts(_context(tree))
    context = _context(tree)
    _path, forged = _one(root, "act-region", 1, 1)
    with pytest.raises(ContractError, match="crop region's pixels"):
        verify_reading_region_lineage(context.tree, context.run, forged)
    _path, genuine = _one(root, "act-region", 1, 2)
    verified = verify_reading_region_lineage(context.tree, context.run, genuine)
    assert verified["transform"]["bounds"] == genuine["payload"]["union_box_px"]


def test_a_page_read_tree_holding_an_act_reading_is_refused_as_mixed(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    _forge(
        tree[0], "perlectio", 1, 1, lambda record: record["payload"].update(schema="perlectio.v1")
    )
    with pytest.raises(FatalAccounting, match="counted one way, never both"):
        reading_denominator(_context(tree))


def test_the_page_row_classes_bind_the_page_rectangle_and_nothing_else():
    page = "pg_" + "0" * 16
    rectangle = {"x": 0, "y": 0, "w": 10, "h": 20}
    assert derive_act_id(page, "page-unread", rectangle) != derive_act_id(
        page, "page-blank", rectangle
    )
    with pytest.raises(IdentityRefusal):
        derive_act_id(page, "page-unread", {"page_reading": "att_" + "0" * 16, "n": 1})
    with pytest.raises(IdentityRefusal, match="act class must be one of"):
        derive_act_id(page, "page-missing", rectangle)
