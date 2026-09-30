"""The page-read denominator: what a run read page by page counts, proven from its records.

The trees are the synthetic fixture's `happy` and `page-review` scenarios read with
`reading_unit = "page"`: happy places and reads every entry; page-review leaves page
2's entry unplaced, so the page accounting holds it. Each forgery rewrites one
Perlector record and rewitnesses the stage's boundary, so the only thing left
to catch it is the denominator's own recomputation. A forgery that changes what
the page accounting measures has its accounting measured again by the
denominator's own measurement (`_reaccount`), never written by hand.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest

from common import dissent, page_path
from common import stage as stage_module
from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of, self_hash
from common.contracts.errors import ContractError, FatalAccounting, IdentityRefusal
from common.contracts.identities import act_id as derive_act_id
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import (
    ATTESTATORES,
    DESIGNATOR,
    EXEMPLAR,
    INK_MAP,
    PERLECTOR,
    RECENSOR,
)
from common.exemplar_boundary import verify_reading_region_lineage
from common.runtree.store import RunTree
from common.stage import (
    COUNTED_READING_CLASSES,
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


def _protocol(directory: Path, unit: str, lines: dict[str, str]) -> Path:
    text = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    assert 'reading_unit = "act"' in text
    text = text.replace('reading_unit = "act"', f'reading_unit = "{unit}"')
    for line, replacement in lines.items():
        assert text.count(line) == 1
        text = text.replace(line, replacement)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "perlector_protocol.toml"
    path.write_text(text, "utf-8")
    return path


def _tree(
    base: Path, scenario: str, unit: str = "page", lines: dict[str, str] | None = None
) -> tuple[Path, Path, str]:
    protocol = _protocol(base / "config", unit, lines or {})
    root = base / "runs"
    for program in programs_through("perlector"):
        result = run_stage(root, RUN_ID, scenario, program, perlector_protocol_config=protocol)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return root, protocol, scenario


@pytest.fixture(scope="module")
def happy_tree(tmp_path_factory) -> tuple[Path, Path, str]:
    return _tree(tmp_path_factory.mktemp("happy"), "happy")


@pytest.fixture(scope="module")
def review_tree(tmp_path_factory) -> tuple[Path, Path, str]:
    return _tree(tmp_path_factory.mktemp("page-review"), "page-review")


@pytest.fixture(scope="module")
def act_tree(tmp_path_factory) -> tuple[Path, Path, str]:
    return _tree(tmp_path_factory.mktemp("act"), "happy", unit="act")


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
        if item[1]["payload"].get("page_ordinal") == ordinal
        and (n is None or item[1]["payload"].get("n") == n)
    ]
    return found


def _write(path: Path, record: dict[str, Any]) -> None:
    record["self_hash"] = self_hash(
        {key: value for key, value in record.items() if key != "self_hash"}
    )
    path.write_bytes(canonical_bytes(record))


def _rewitness(root: Path) -> None:
    rewitness_stage_boundary(RunTree(root, RUN_ID), PERLECTOR)


def _forge(root: Path, kind: str, ordinal: int, n: int | None, change: Callable) -> None:
    path, record = _one(root, kind, ordinal, n)
    forged = copy.deepcopy(record)
    change(forged)
    _write(path, forged)
    _rewitness(root)


def _forge_through(root: Path, kind: str, ordinal: int, n: int | None, change: Callable) -> None:
    """Rewrite one Perlector record and carry its new digest into every record naming it.

    Each record that names a rewritten one is rewritten in turn, so the forgery
    reaches the denominator with every reference and self-hash consistent.
    """
    path, record = _one(root, kind, ordinal, n)
    forged = copy.deepcopy(record)
    change(forged)
    before = digest_bytes(path.read_bytes())
    _write(path, forged)
    swaps = [(before, digest_bytes(path.read_bytes()))]
    artifacts = root / RUN_ID / "4_perlector" / "artifacts"
    while swaps:
        old, new = swaps.pop()
        for other in artifacts.glob("*/*.json"):
            text = other.read_text(encoding="utf-8")
            if old in text:
                was = digest_bytes(other.read_bytes())
                _write(other, json.loads(text.replace(old, new)))
                swaps.append((was, digest_bytes(other.read_bytes())))
    _rewitness(root)


def _with_feed_digest(change: Callable) -> Callable:
    """`change` applied to a page feed, its `feed_digest` then made that of the changed feed."""

    def changed(record):
        feed = record["payload"]
        change(feed)
        feed["feed_digest"] = digest_of({k: v for k, v in feed.items() if k != "feed_digest"})

    return changed


def _fixture_says(monkeypatch, ordinal: int, answer: str, stop_reason: str | None = "stop"):
    """The fixture's declared answer to page `ordinal`, as a forged reading needs it to be.

    The denominator reads a fixture run's reply from the fixture; a test that
    forges what stage 4 was answered forges what the fixture declares too.
    """
    declared = page_path.fixture_page_answer

    def said(context, asked):
        if asked == ordinal:
            return {"answer": answer, "stop_reason": stop_reason}
        return declared(context, asked)

    monkeypatch.setattr(page_path, "fixture_page_answer", said)


def _read_as(root: Path, ordinal: int, content: str, stop_reason: str | None) -> Callable:
    """A forge making page `ordinal`'s reading what stage 4 reads from `content`."""
    feed = _one(root, "page-feed", ordinal)[1]["payload"]
    state, answer, problems = page_path.read_reply(content, stop_reason, feed)
    read = state == "parsed" and not problems

    def forged(record):
        record["outcome"] = "read" if read else "held"
        record["payload"].update(
            parse_state=state,
            answer=answer,
            problems=problems,
            finish_reason=stop_reason,
            stop_reason=stop_reason,
            disposition="read" if read else "held",
        )

    return forged


def _drop_act_records(root: Path, ordinal: int) -> None:
    for kind in ("act-region", "perlectio"):
        for path, record in _records(root, kind):
            if record["payload"]["page_ordinal"] == ordinal:
                path.unlink()


def _reaccount(tree: tuple[Path, Path, str], ordinal: int) -> list[str]:
    """Measure page `ordinal`'s accounting again, as the denominator does, and carry it on.

    The page's act records take the new holds, as stage 4 would have written
    them after measuring the forged reading. Returns the page's holds.
    """
    root = tree[0]
    _rewitness(root)
    context = _context(tree)
    index = stage_module._PageReadRecords(context)
    _path, reading = _one(root, "page-reading", ordinal)
    payload = reading["payload"]
    feed = context.tree.read_artifact_reference(
        payload["feed_ref"], stage=PERLECTOR, kind="page-feed", subject_id=reading["subject_id"]
    )
    plans = (
        page_path.entry_plans(
            payload["answer"],
            feed["payload"],
            page_id=reading["subject_id"],
            stop_reason=payload["stop_reason"],
            truncation_policy=index.truncation_policy,
        )
        if payload["disposition"] == "read"
        else []
    )
    witnesses = stage_module._verify_feed(
        context, index, "test", ordinal, reading["subject_id"], feed
    )
    measured, inputs = stage_module._measure_page_accounting(
        context,
        index,
        "test",
        feed["payload"],
        payload["feed_ref"],
        witnesses,
        payload,
        index.ref(reading),
        plans,
    )
    holds = measured["holds"]
    path, accounting = _one(root, "page-accounting", ordinal)
    accounting.update(payload=measured, inputs=inputs, outcome="held" if holds else "read")
    _write(path, accounting)
    for kind in ("act-region", "perlectio"):
        for path, record in _records(root, kind):
            if record["payload"]["page_ordinal"] == ordinal:
                record["payload"]["page_holds"] = holds
                own = record["payload"]["holds"]
                record["outcome"] = "held" if own or holds else "read"
                _write(path, record)
    _rewitness(root)
    return holds


def _republish(root: Path, path: Path, record: dict[str, Any]) -> None:
    """Write `record` under its own artifact id in place of `path`, every reference following."""
    moved = path.with_name(f"{record['artifact_id']}.json")
    old = str(path.relative_to(root / RUN_ID))
    _write(moved, record)
    if moved != path:
        path.unlink()
        for other in (root / RUN_ID / "4_perlector" / "artifacts").glob("*/*.json"):
            text = other.read_text(encoding="utf-8")
            if old in text:
                new = str(moved.relative_to(root / RUN_ID))
                _write(other, json.loads(text.replace(old, new)))
    _rewitness(root)


def _second_attempt(root: Path, ordinal: int, keep: bool) -> None:
    """Publish page `ordinal`'s reading again as attempt `page-read:2`, beside or instead."""
    path, record = _one(root, "page-reading", ordinal)
    page = record["subject_id"]
    record["attempt_id"] = attempt_id(page, "page-read", 2)
    record["artifact_id"] = artifact_id(PERLECTOR, "page-reading", page, record["attempt_id"])
    if keep:
        _write(path.with_name(f"{record['artifact_id']}.json"), record)
        _rewitness(root)
    else:
        _republish(root, path, record)


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
    assert [pages[1]["entry_count"], pages[2]["entry_count"]] == [2, 1]
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


def test_one_context_is_verified_once_however_often_it_is_asked(happy_tree, monkeypatch):
    calls = []
    verify = stage_module._verify_page_read_denominator

    def counted(context):
        calls.append(context)
        return verify(context)

    monkeypatch.setattr(stage_module, "_verify_page_read_denominator", counted)
    context = _context(happy_tree)
    first = reading_acts(context)
    first[0]["hold_codes"].append("edited by a caller")
    assert page_readings(context) and reading_acts(context)[0]["hold_codes"] == []
    assert len(calls) == 1
    reading_denominator(_context(happy_tree))
    assert len(calls) == 2


def test_an_unplaced_entry_is_counted_and_held_by_the_recomputed_accounting(review_tree):
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


def test_a_page_read_without_its_image_holds_every_entry_no_autopsia(tmp_path):
    tree = _tree(tmp_path, "happy", lines={'page_image = "legible"': 'page_image = "off"'})
    acts = reading_acts(_context(tree))
    assert acts and all("no-autopsia" in act["hold_codes"] for act in acts)
    # The region's own holds are recomputed from the feed: dropping one is refused.
    _forge(
        tree[0],
        "act-region",
        1,
        1,
        lambda record: record["payload"].update(
            holds=[code for code in record["payload"]["holds"] if code != "no-autopsia"]
        ),
    )
    with pytest.raises(FatalAccounting, match=r"act-region does not match .*\(holds\)"):
        reading_acts(_context(tree))


def test_a_hidden_witness_is_measured_again_as_stage_4_measured_it(tmp_path):
    tree = _tree(tmp_path, "happy", lines={'witnesses = "all"': 'witnesses = ["attestator_1"]'})
    root = tree[0]
    _path, feed = _one(root, "page-feed", 1)
    assert [row["witness_label"] for row in feed["payload"]["witnesses"]] == ["attestator_1"]
    _path, accounting = _one(root, "page-accounting", 1)
    assert {unit["id"][0] for unit in accounting["payload"]["units"]} == {"A", "B"}
    # The fixture's answer cites the hidden witness's ids, which this feed does not define.
    acts = reading_acts(_context(tree))
    assert {act["class"] for act in acts} == {"page-unread"}
    assert all("unknown-id" in act["hold_codes"] for act in acts)


def test_a_page_whose_answer_was_not_read_is_one_held_page_unread_row(
    happy_tree, tmp_path, monkeypatch
):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    _drop_act_records(root, 2)
    _fixture_says(monkeypatch, 2, "the reply is not JSON")
    _forge(root, "page-reading", 2, None, _read_as(root, 2, "the reply is not JSON", "stop"))
    reply_codes = [p["code"] for p in _one(root, "page-reading", 2)[1]["payload"]["problems"]]
    assert reply_codes
    page_holds = _reaccount(tree, 2)
    assert "page-answer-incomplete" in page_holds
    context = _context(tree)
    pages = page_readings(context)
    assert (pages[2]["parse_state"], pages[2]["disposition"], pages[2]["entry_count"]) == (
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
        "hold_codes": sorted({*reply_codes, "page-unread", *page_holds}),
        "continues_from_previous_page": None,
        "continues_to_next_page": None,
    }


def test_a_read_answer_naming_no_act_is_one_held_page_blank_row(happy_tree, tmp_path, monkeypatch):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    _drop_act_records(root, 2)
    _path, feed = _one(root, "page-feed", 2)
    ids = [unit["id"] for witness in feed["payload"]["witnesses"] for unit in witness["units"]]
    ids += [line["id"] for line in feed["payload"]["surya"]["lines"]]
    ids += [block["id"] for block in feed["payload"]["surya"]["blocks"]]
    answer = {
        "acts": [],
        "set_aside": [{"id": identifier, "reason": "blank paper"} for identifier in ids],
    }
    _fixture_says(monkeypatch, 2, json.dumps(answer))
    _forge(root, "page-reading", 2, None, _read_as(root, 2, json.dumps(answer), "stop"))
    page_holds = _reaccount(tree, 2)
    context = _context(tree)
    assert page_readings(context)[2]["entry_count"] == 0
    row = reading_acts(context)[-1]
    assert (row["class"], row["act_key"], row["disposition"]) == ("page-blank", "p2:blank", "held")
    assert row["hold_codes"] == sorted({"page-blank-unconfirmed", *page_holds})
    assert row["region_ref"] is None and row["perlectio_ref"] is None


def test_a_read_page_naming_only_other_entries_is_held_until_confirmed(
    happy_tree, tmp_path, monkeypatch
):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]

    def other(record):
        payload = record["payload"]
        for entry in payload["answer"]["acts"] if "answer" in payload else [payload]:
            entry["kind"] = "other"

    for kind in ("page-reading", "act-region", "perlectio"):
        _forge(root, kind, 2, None, other)
    _fixture_says(monkeypatch, 2, json.dumps(_one(root, "page-reading", 2)[1]["payload"]["answer"]))
    _reaccount(tree, 2)
    context = _context(tree)
    assert page_readings(context)[2]["entry_count"] == 1
    acts = reading_acts(context)
    rows = [act for act in acts if act["page_ordinal"] == 2]
    assert [(row["kind"], row["disposition"]) for row in rows] == [("other", "held")]
    assert "no-act-on-page-unconfirmed" in rows[0]["hold_codes"]
    # A page with an act entry is not held for it.
    assert all(
        "no-act-on-page-unconfirmed" not in act["hold_codes"]
        for act in acts
        if act["page_ordinal"] == 1
    )


def test_a_page_with_one_act_and_one_other_entry_is_not_held_for_naming_no_act(
    happy_tree, tmp_path, monkeypatch
):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]

    def second_is_other(record):
        payload = record["payload"]
        if "answer" in payload:
            payload["answer"]["acts"][1]["kind"] = "other"
        elif payload["n"] == 2:
            payload["kind"] = "other"

    _forge(root, "page-reading", 1, None, second_is_other)
    for kind in ("act-region", "perlectio"):
        _forge(root, kind, 1, 2, second_is_other)
    _fixture_says(monkeypatch, 1, json.dumps(_one(root, "page-reading", 1)[1]["payload"]["answer"]))
    _reaccount(tree, 1)
    rows = [act for act in reading_acts(_context(tree)) if act["page_ordinal"] == 1]
    assert [row["kind"] for row in rows] == ["act", "other"]
    assert all("no-act-on-page-unconfirmed" not in row["hold_codes"] for row in rows)


def test_a_reading_with_no_stop_reason_is_held_whole_by_that_tail(
    happy_tree, tmp_path, monkeypatch
):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    _drop_act_records(root, 2)
    content = json.dumps(_one(root, "page-reading", 2)[1]["payload"]["answer"])
    _fixture_says(monkeypatch, 2, content, stop_reason=None)
    _forge(root, "page-reading", 2, None, _read_as(root, 2, content, None))
    problems = _one(root, "page-reading", 2)[1]["payload"]["problems"]
    assert [problem["code"] for problem in problems] == ["no-stop-reason"]
    _reaccount(tree, 2)
    row = reading_acts(_context(tree))[-1]
    assert row["class"] == "page-unread" and "no-stop-reason" in row["hold_codes"]
    # The tail is recomputed: a reading that drops it and calls itself read is refused.
    _forge(
        root,
        "page-reading",
        2,
        None,
        lambda record: (
            record.update(outcome="read"),
            record["payload"].update(problems=[], disposition="read"),
        ),
    )
    with pytest.raises(FatalAccounting, match=r"not what its reply gives \(problems\)"):
        reading_acts(_context(tree))


# --- the page the Exemplar refused -------------------------------------------------


def _refuse_page_two(tree: tuple[Path, Path, str]) -> str:
    """Make page 2 one the Exemplar refused, with the not-run reading stage 4 writes for it."""
    root = tree[0]
    run = RunTree(root, RUN_ID)
    page_id = _one(root, "page-reading", 2)[1]["subject_id"]
    relative = run.artifact_path(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    exemplar = root / RUN_ID / relative
    record = json.loads(exemplar.read_text(encoding="utf-8"))
    record["outcome"] = "refused"
    _write(exemplar, record)
    for stage in (EXEMPLAR, INK_MAP, DESIGNATOR, ATTESTATORES):
        rewitness_stage_boundary(run, stage)
    _drop_act_records(root, 2)
    for kind in ("page-accounting", "page-feed"):
        _one(root, kind, 2)[0].unlink()
    path, reading = _one(root, "page-reading", 2)
    reading["outcome"] = "held"
    reading["inputs"] = [{"relative_path": relative, "sha256": digest_bytes(exemplar.read_bytes())}]
    reading["payload"].update(
        feed_ref=None,
        request_digest=None,
        finish_reason=None,
        stop_reason=None,
        parse_state="not-run",
        answer=None,
        problems=[{"code": "page-not-sealed", "detail": "the Exemplar refused this page"}],
        disposition="held",
    )
    _write(path, reading)
    rebind_stage_seal_artifact(run, PERLECTOR)
    return page_id


def test_a_page_the_exemplar_refused_is_a_row_that_is_not_counted(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    page_id = _refuse_page_two(tree)
    denominator = reading_denominator(_context(tree))
    assert sorted(denominator["pages"]) == [1]
    row = denominator["acts"][-1]
    assert set(row) == READING_ACT_FIELDS
    assert (row["class"], row["act_key"], row["disposition"]) == (
        "page-refused",
        "p2:refused",
        "refused",
    )
    assert (row["page_id"], row["act_id"], row["kind"], row["hold_codes"]) == (
        page_id,
        None,
        None,
        [],
    )
    assert row["reading_ref"]["relative_path"].startswith("4_perlector/artifacts/page-reading/")
    assert "page-refused" not in COUNTED_READING_CLASSES
    assert [act["class"] for act in denominator["acts"][:-1]] == ["reading", "reading"]


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (lambda r: r["payload"].update(schema="perlector-page-reading.v0"), "not the not-run"),
        (lambda r: r["payload"].update(parse_state="parsed"), "not the not-run"),
        (
            lambda r: r["payload"]["problems"].append({"code": "chair-absent", "detail": "x"}),
            "not the not-run",
        ),
        (lambda r: r["payload"]["problems"][0].update(code=7), "records a problem with no code"),
        *(
            (lambda r, name=name: r["payload"].update({name: {"forged": True}}), "not the not-run")
            for name in (
                "engine_call",
                "request_digest",
                "finish_reason",
                "stop_reason",
                "sampling",
                "capacity",
            )
        ),
    ],
    ids=[
        "schema",
        "parse-state",
        "problems",
        "code-type",
        "engine-call",
        "request-digest",
        "finish-reason",
        "stop-reason",
        "sampling",
        "capacity",
    ],
)
def test_a_refused_page_s_reading_that_is_not_its_not_run_reading_is_refused(
    happy_tree, tmp_path, change, refusal
):
    tree = _copy(happy_tree, tmp_path)
    _refuse_page_two(tree)
    _forge(tree[0], "page-reading", 2, None, change)
    with pytest.raises(FatalAccounting, match=refusal):
        reading_acts(_context(tree))


@pytest.mark.parametrize("keep", [False, True], ids=["page-read-2-alone", "beside-page-read-1"])
def test_a_refused_page_s_second_reading_attempt_is_refused(happy_tree, tmp_path, keep):
    tree = _copy(happy_tree, tmp_path)
    _refuse_page_two(tree)
    _second_attempt(tree[0], 2, keep=keep)
    with pytest.raises(FatalAccounting, match="has 2 records|not the page path's attempt"):
        reading_acts(_context(tree))


def test_a_refused_page_with_an_act_record_is_refused(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    region, record = _one(root, "act-region", 2, 1)
    _refuse_page_two(tree)
    # The page's feed, reading and accounting it was cut from are gone with the page.
    record["inputs"] = []
    _write(region, record)
    rebind_stage_seal_artifact(RunTree(root, RUN_ID), PERLECTOR)
    with pytest.raises(FatalAccounting, match="no sealed pixels, yet the Perlector published"):
        reading_acts(_context(tree))


def test_every_submitted_page_must_have_an_exemplar_page(happy_tree):
    context = _context(happy_tree)
    context.run["source_manifest"].append({**context.run["source_manifest"][-1], "ordinal": 3})
    with pytest.raises(FatalAccounting, match=r"ordinal\(s\) \[3\] have no Exemplar page"):
        reading_acts(context)
    with pytest.raises(FatalAccounting, match=r"ordinal\(s\) \[3\] have no Exemplar page"):
        stage_module.exemplar_page_ids(context)


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


@pytest.mark.parametrize("keep", [False, True], ids=["page-read-2-alone", "beside-page-read-1"])
def test_a_second_page_reading_attempt_is_refused(happy_tree, tmp_path, keep):
    tree = _copy(happy_tree, tmp_path)
    _second_attempt(tree[0], 1, keep=keep)
    match = "has 2 records" if keep else "is attempt .*, not the page path's attempt"
    with pytest.raises(FatalAccounting, match=match):
        reading_acts(_context(tree))


def test_a_reading_the_engine_cut_at_its_cap_cannot_be_parsed(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    _forge(
        tree[0],
        "page-reading",
        1,
        None,
        lambda record: record["payload"].update(stop_reason="length", finish_reason="length"),
    )
    with pytest.raises(
        FatalAccounting, match=r"not what its reply gives \(finish_reason, stop_reason\)"
    ):
        reading_acts(_context(tree))


def test_a_problem_without_a_string_code_is_refused(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    _drop_act_records(root, 2)

    def malformed(record):
        record["outcome"] = "held"
        record["payload"].update(
            parse_state="malformed",
            answer=None,
            problems=[{"code": ["json-invalid"], "detail": "x"}],
            disposition="held",
        )

    _forge(root, "page-reading", 2, None, malformed)
    with pytest.raises(FatalAccounting, match="records a problem with no code"):
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


@pytest.mark.parametrize(
    ("kind", "what"), [("act-region", "act-region"), ("perlectio", "Perlectio")]
)
def test_an_act_record_carrying_a_field_beyond_its_schema_is_refused(
    happy_tree, tmp_path, kind, what
):
    tree = _copy(happy_tree, tmp_path)
    _forge(tree[0], kind, 1, 1, lambda record: record["payload"].update(note="added later"))
    with pytest.raises(FatalAccounting, match=f"{what} carries fields other than its closed"):
        reading_acts(_context(tree))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("holds", ["reading-incomplete"]),
        ("text", "SYNTHETIC ACT ONE alpha beta"),
        ("uncertainty_assessment", None),
        ("autopsia", False),
        ("truncation", None),
        ("engine_call", {"forged": True}),
        ("provenance", {"forged": True}),
        ("continues_to_next_page", True),
    ],
)
def test_a_perlectio_departing_from_its_answer_entry_is_refused(happy_tree, tmp_path, field, value):
    tree = _copy(happy_tree, tmp_path)
    _forge(tree[0], "perlectio", 1, 1, lambda record: record["payload"].update({field: value}))
    with pytest.raises(FatalAccounting, match=rf"Perlectio does not match .*{field}"):
        reading_acts(_context(tree))


def _compared_row(record) -> dict:
    return next(row for row in record["payload"]["dissent"] if row["compared"] is True)


@pytest.mark.parametrize(
    "change",
    [
        lambda row: row.update(departed=not row["departed"]),
        lambda row: row.update(
            departures=[
                *row["departures"],
                {
                    "reading_span": {"start": 0, "end": 1},
                    "testimonium_span": {"start": 0, "end": 0},
                },
            ]
        ),
        lambda row: row.update(cited_units=["A9"]),
        lambda row: row.update(compared="unknown"),
    ],
    ids=["departed", "departures", "cited-units", "not-compared-without-reason"],
)
def test_a_perlectio_whose_dissent_is_not_its_entrys_is_refused(happy_tree, tmp_path, change):
    tree = _copy(happy_tree, tmp_path)
    _forge(tree[0], "perlectio", 1, 1, lambda record: change(_compared_row(record)))
    with pytest.raises(FatalAccounting, match="dissent is not where its reading departs"):
        reading_acts(_context(tree))


def test_a_dissent_row_whose_alignment_ran_out_of_time_where_it_was_sealed_is_counted(
    happy_tree, tmp_path
):
    """The not-compared row a clock gave claims no comparison, so it stands as sealed."""
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    _path, perlectio = _one(root, "perlectio", 1, 1)
    row = _compared_row(perlectio)
    feed = _one(root, "page-feed", 1)[1]["payload"]
    [witness] = [w for w in feed["witnesses"] if w["letter"] == row["letter"]]
    reported = "\n".join(u["text"] for u in witness["units"] if u["id"] in row["cited_units"])
    unaligned = dissent.unaligned_row(row["letter"], perlectio["payload"]["text"], reported)
    unaligned.pop("chair")

    def ran_out(record):
        sealed = _compared_row(record)
        kept = {name: sealed[name] for name in ("letter", "witness_label", "cited_units")}
        sealed.clear()
        sealed.update(kept, **unaligned)

    _forge(root, "perlectio", 1, 1, ran_out)
    acts = reading_acts(_context(tree))
    assert [act["act_key"] for act in acts] == ["p1:1", "p1:2", "p2:1"]


@pytest.mark.parametrize(
    ("kind", "n"),
    [("page-reading", None), ("page-accounting", None), ("act-region", 1), ("perlectio", 1)],
)
def test_the_run_tree_refuses_a_page_record_of_another_configuration(happy_tree, tmp_path, kind, n):
    """A store test: the run tree refuses such a record on every read, before any counting.

    The denominator reads its records only through the run tree, so it needs
    no configuration check of its own.
    """
    tree = _copy(happy_tree, tmp_path)
    path, record = _one(tree[0], kind, 1, n)
    record["config_digest"] = "0" * 64
    _write(path, record)
    with pytest.raises(ContractError, match="produced under configuration '0000"):
        RunTree(tree[0], RUN_ID).read_artifact(PERLECTOR, kind, record["artifact_id"])


@pytest.mark.parametrize(
    "change",
    [
        lambda record, _other: record["payload"].update(holds=[]),
        lambda record, _other: record["payload"].update(holds=["no-such-hold"]),
        lambda record, _other: record.update(outcome="read"),
        lambda record, _other: record["inputs"].pop(),
        lambda record, other: record["payload"].update(feed_ref=other),
    ],
    ids=["no-holds", "invented-hold", "outcome", "input-dropped", "other-feed"],
)
def test_a_page_accounting_its_inputs_do_not_measure_is_refused(review_tree, tmp_path, change):
    tree = _copy(review_tree, tmp_path)
    root = tree[0]
    other = _one(root, "page-reading", 1)[1]["payload"]["feed_ref"]
    _forge(root, "page-accounting", 2, None, lambda record: change(record, other))
    with pytest.raises(FatalAccounting, match="page accounting is not what its sealed inputs"):
        reading_acts(_context(tree))


@pytest.mark.parametrize("answer", ["not an object", ["acts"], None], ids=["text", "list", "none"])
def test_a_parsed_reading_whose_answer_is_not_an_object_is_refused(happy_tree, tmp_path, answer):
    tree = _copy(happy_tree, tmp_path)
    _forge(tree[0], "page-reading", 1, None, lambda record: record["payload"].update(answer=answer))
    with pytest.raises(FatalAccounting, match=r"not what its reply gives \(answer"):
        reading_acts(_context(tree))


def test_answer_entries_that_cannot_be_planned_refuse_as_accounting(happy_tree, monkeypatch):
    def malformed(*_args, **_kwargs):
        raise KeyError("page_size")

    monkeypatch.setattr(page_path, "entry_plans", malformed)
    with pytest.raises(FatalAccounting, match="answer entries cannot be planned"):
        reading_acts(_context(happy_tree))


@pytest.mark.parametrize(
    "inputs",
    [None, "refs", [{"sha256": "0" * 64}], [["relative_path"]]],
    ids=["none", "text", "no-path", "not-a-mapping"],
)
def test_malformed_accounting_inputs_refuse_as_accounting(inputs):
    with pytest.raises(FatalAccounting, match="not a list of path references"):
        stage_module._refs_by_path(inputs, "page 1's page accounting")


def _swap_render_for_a_crop(root: Path) -> Callable:
    _path, region = _one(root, "act-region", 1, 1)
    crop = region["payload"]

    def swap(feed):
        feed["page_render"].update(image_path=crop["image_path"], image_sha256=crop["image_sha256"])

    return swap


@pytest.mark.parametrize(
    "forgery",
    [
        lambda root: (
            lambda feed: feed["witnesses"][0]["units"][0].update(
                text=feed["witnesses"][0]["units"][0]["text"] + " and a line no witness wrote"
            )
        ),
        lambda root: lambda feed: feed["witnesses"][0]["units"].pop(),
        _swap_render_for_a_crop,
        lambda root: lambda feed: feed["page_render"]["transform"].update(maximum_edge=1),
        lambda root: lambda feed: feed.update(prompt=None),
    ],
    ids=[
        "shown-unit-text",
        "shown-unit-dropped",
        "page-render-image",
        "render-transform",
        "prompt",
    ],
)
def test_a_forged_page_feed_is_refused_against_the_feed_its_inputs_build(
    happy_tree, tmp_path, forgery
):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    _forge_through(root, "page-feed", 1, None, _with_feed_digest(forgery(root)))
    with pytest.raises(FatalAccounting, match="page feed is not the feed its sealed inputs build"):
        reading_acts(_context(tree))


def test_a_reading_whose_answer_drops_an_entry_its_reply_gave_is_refused(happy_tree, tmp_path):
    """Stage 4's records agree with each other; only the reply read again shows the gap."""
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    for kind in ("perlectio", "act-region"):
        _one(root, kind, 1, 2)[0].unlink()
    _forge(root, "page-reading", 1, None, lambda r: r["payload"]["answer"]["acts"].pop())
    _reaccount(tree, 1)
    with pytest.raises(FatalAccounting, match=r"not what its reply gives \(answer\)"):
        reading_acts(_context(tree))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("parse_state", "malformed"),
        ("finish_reason", None),
        ("problems", [{"code": "no-stop-reason", "detail": "forged"}]),
    ],
)
def test_a_reading_departing_from_its_reply_is_refused(happy_tree, tmp_path, field, value):
    tree = _copy(happy_tree, tmp_path)
    _forge(tree[0], "page-reading", 1, None, lambda r: r["payload"].update({field: value}))
    with pytest.raises(FatalAccounting, match=rf"not what its reply gives \(.*{field}"):
        reading_acts(_context(tree))


def test_a_reading_naming_another_feed_than_its_inputs_is_refused(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    other = _one(root, "page-reading", 2)[1]["payload"]["feed_ref"]
    _forge(root, "page-reading", 1, None, lambda record: record["payload"].update(feed_ref=other))
    with pytest.raises(FatalAccounting, match="names no feed among its inputs"):
        reading_acts(_context(tree))


def test_a_reading_that_calls_a_valid_answer_held_is_refused(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)

    def held(record):
        record["outcome"] = "held"
        record["payload"]["disposition"] = "held"

    _forge(tree[0], "page-reading", 1, None, held)
    with pytest.raises(FatalAccounting, match="but its answer against its feed is 'read'"):
        reading_acts(_context(tree))


@pytest.mark.parametrize("kind", ["act-region", "perlectio"])
def test_a_missing_act_record_is_refused(happy_tree, tmp_path, kind):
    tree = _copy(happy_tree, tmp_path)
    # A Perlectio names its act-region, so a missing region takes its Perlectio with it.
    for dropped in sorted({"perlectio", kind}):
        _one(tree[0], dropped, 1, 2)[0].unlink()
    rebind_stage_seal_artifact(RunTree(tree[0], RUN_ID), PERLECTOR)
    with pytest.raises(FatalAccounting, match=f"{kind}|Perlectio is missing"):
        reading_acts(_context(tree))


@pytest.mark.parametrize("kind", ["act-region", "perlectio"])
def test_an_act_record_beyond_the_answer_s_entries_is_refused(happy_tree, tmp_path, kind):
    tree = _copy(happy_tree, tmp_path)
    root = tree[0]
    path, record = _one(root, kind, 1, 2)
    record["subject_id"] = "act_" + "f" * 16
    record["artifact_id"] = artifact_id(PERLECTOR, kind, record["subject_id"], record["attempt_id"])
    _write(path.with_name(f"{record['artifact_id']}.json"), record)
    rebind_stage_seal_artifact(RunTree(root, RUN_ID), PERLECTOR)
    with pytest.raises(FatalAccounting, match=f"{kind} records do not match its answer's 2"):
        reading_acts(_context(tree))


@pytest.mark.parametrize("kind", ["act-region", "perlectio", "page-accounting"])
def test_a_record_naming_a_page_the_exemplar_never_published_is_refused(happy_tree, tmp_path, kind):
    tree = _copy(happy_tree, tmp_path)
    stray = "pg_" + "f" * 16

    def elsewhere(record):
        if kind == "page-accounting":
            record["subject_id"] = stray
        else:
            record["payload"]["page_id"] = stray

    path, record = _one(tree[0], kind, 2, None if kind == "page-accounting" else 1)
    elsewhere(record)
    record["artifact_id"] = artifact_id(PERLECTOR, kind, record["subject_id"], record["attempt_id"])
    _republish(tree[0], path, record)
    with pytest.raises(FatalAccounting, match="which this run's Exemplar never published"):
        reading_acts(_context(tree))


def test_two_pages_counting_one_act_identity_is_refused(happy_tree, monkeypatch):
    verify = stage_module._verify_page_reading

    def same_identity(context, index, ordinal, page_id):
        row, acts = verify(context, index, ordinal, page_id)
        return row, [{**act, "act_id": "act_shared"} for act in acts[:1]]

    monkeypatch.setattr(stage_module, "_verify_page_reading", same_identity)
    with pytest.raises(FatalAccounting, match="share one act identity"):
        reading_acts(_context(happy_tree))


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


def test_a_record_changed_after_the_perlector_sealed_is_refused(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    context = _context(tree)
    path, record = _one(tree[0], "perlectio", 1, 1)
    record["payload"]["text"] = "rewritten after the seal"
    _write(path, record)
    with pytest.raises(ContractError, match="stage-seal"):
        reading_acts(context)


# --- one run, one way of counting ---------------------------------------------------


def test_a_page_read_tree_holding_an_act_reading_is_refused_as_mixed(happy_tree, tmp_path):
    tree = _copy(happy_tree, tmp_path)
    _forge(
        tree[0], "perlectio", 1, 1, lambda record: record["payload"].update(schema="perlectio.v1")
    )
    with pytest.raises(FatalAccounting, match="counted one way, never both"):
        reading_denominator(_context(tree))


@pytest.mark.parametrize("kind", ["audit-draft", "audit-finding"])
def test_a_page_read_tree_holding_any_act_path_record_is_refused_as_mixed(
    act_tree, happy_tree, tmp_path, kind
):
    tree = _copy(happy_tree, tmp_path)
    source = sorted((act_tree[0] / RUN_ID / "4_perlector" / "artifacts" / kind).glob("*.json"))[0]
    run = RunTree(tree[0], RUN_ID)
    record = json.loads(source.read_text(encoding="utf-8"))
    # Written by this page-read run, with no input it could not have read.
    record.update(config_digest=run.read_run()["config_digest"], inputs=[])
    directory = tree[0] / RUN_ID / "4_perlector" / "artifacts" / kind
    directory.mkdir(exist_ok=True)
    _write(directory / source.name, record)
    rebind_stage_seal_artifact(run, PERLECTOR)
    with pytest.raises(FatalAccounting, match=f"published an act-path {kind}"):
        reading_denominator(_context(tree))


@pytest.mark.parametrize(
    "kind", ["page-feed", "page-reading", "page-accounting", "act-region", "perlectio"]
)
def test_an_act_read_tree_holding_a_page_record_is_refused_as_mixed(
    act_tree, happy_tree, tmp_path, kind
):
    tree = _copy(act_tree, tmp_path)
    source, _record = _one(happy_tree[0], kind, 1, None if kind.startswith("page-") else 1)
    run = RunTree(tree[0], RUN_ID)
    record = json.loads(source.read_text(encoding="utf-8"))
    # Written by this act-read run, with no input it could not have read.
    record.update(config_digest=run.read_run()["config_digest"], inputs=[])
    directory = tree[0] / RUN_ID / "4_perlector" / "artifacts" / kind
    directory.mkdir(exist_ok=True)
    _write(directory / source.name, record)
    rebind_stage_seal_artifact(run, PERLECTOR)
    with pytest.raises(FatalAccounting, match=f"published a page-path {kind}"):
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
