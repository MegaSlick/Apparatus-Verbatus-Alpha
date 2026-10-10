"""A page's one re-ask, proven end to end on the synthetic fixture and fake serving.

A page whose first reading leaves boxed witness units, detected lines or
detector records unaccounted for is asked once more about exactly those ids
(`common/page_reask.py`). The re-ask is a second `page-reading` of the page
(attempt 2); the accounting of both readings together is the page's last, and
every act record names it; an entry the re-ask read carries
`reading_attempt: 2`. The first reading and its accounting are never changed.
The fixture tests run the real chain, the stage in this process where a test
needs to stop or tamper with it; the live tests run it against
`operations/serving/fakes.py`.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import page_run
import pytest
from test_page_reading import (
    FIXTURE,
    PAGE_ANSWERS,
    PERLECTOR_PROGRAM,
    ROOT,
    _chain,
    _chat_requests,
    _denominator_context,
    _live_chain,
    _page_protocol,
    _read_pages,
    _records,
    _scripted,
    perlector,
)

from common import page_path, page_prompt
from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.identities import act_bindings, artifact_id, attempt_id, verify
from common.contracts.stages import PERLECTOR
from common.hard_failure import load_hard_failure_policy, tally_hard_failures
from common.request_capacity import RequestCapacityRefusal
from common.runtree.store import RunTree
from common.stage import reading_acts
from conftest import file_bytes_snapshot, reask_recovery_config, rewitness_stage_boundary
from operations.serving.fakes import ScriptedAnswer

REASK_ROWS = {(row["scenario"], row["page_ordinal"]): row for row in FIXTURE["page_reask_answer"]}


# --- helpers ----------------------------------------------------------------------


def _tree(base: Path, scenario: str, *, reask: int = 1) -> tuple[Path, Path, Path]:
    """`scenario` read whole through the Perlector with the page re-ask budget `reask`."""
    protocol = _page_protocol(base / "config")
    recovery = reask_recovery_config(base / "config", reask)
    _chain(
        base / "runs",
        protocol,
        "--recovery-config",
        str(recovery),
        through_perlector=True,
        scenario=scenario,
    )
    return base / "runs", protocol, recovery


def _copy(tree: tuple[Path, Path, Path], tmp_path: Path) -> tuple[Path, Path, Path]:
    root, protocol, recovery = tree
    shutil.copytree(root, tmp_path / "runs")
    return tmp_path / "runs", protocol, recovery


def _stage(tree: tuple[Path, Path, Path], scenario: str, monkeypatch) -> int:
    """Run stage 4 in this process over a fixture tree, as the chain would."""
    root, protocol, recovery = tree
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ROOT / PERLECTOR_PROGRAM),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            scenario,
            "--perlector-protocol-config",
            str(protocol),
            "--recovery-config",
            str(recovery),
        ],
    )
    return perlector.main()


def _on_page(root: Path, kind: str, ordinal: int) -> list[dict[str, Any]]:
    return [r for r in _records(root, kind) if r["payload"]["page_ordinal"] == ordinal]


def _reading(root: Path, ordinal: int, attempt: int) -> dict[str, Any] | None:
    found = [
        r
        for r in _on_page(root, "page-reading", ordinal)
        if r["payload"]["attempt_ordinal"] == attempt
    ]
    assert len(found) <= 1
    return found[0] if found else None


def _accounting(root: Path, ordinal: int, attempt: int) -> dict[str, Any] | None:
    basis = "attempt-1" if attempt == 1 else "combined"
    found = [
        r
        for r in _on_page(root, "page-accounting", ordinal)
        if r["payload"]["answer_basis"] == basis
    ]
    assert len(found) <= 1
    return found[0] if found else None


def _names(ref: dict[str, str], record: dict[str, Any]) -> bool:
    return ref["relative_path"].endswith(f"{record['artifact_id']}.json")


def _path_of(root: Path, record: dict[str, Any]) -> Path:
    return (
        root / "r" / "4_perlector" / "artifacts" / record["kind"] / f"{record['artifact_id']}.json"
    )


def _rewrite(root: Path, record: dict[str, Any]) -> None:
    """Write a changed record back, self-hashed, and rewitness the Perlector's boundary."""
    record["self_hash"] = self_hash({k: v for k, v in record.items() if k != "self_hash"})
    _path_of(root, record).write_bytes(canonical_bytes(record))
    rewitness_stage_boundary(RunTree(root, "r"), PERLECTOR)


def _plant(root: Path, source: dict[str, Any], ordinal: int) -> None:
    """A copy of a page reading or accounting planted as another attempt of the same page."""
    planted = json.loads(json.dumps(source))
    planted["attempt_id"] = attempt_id(source["subject_id"], "page-read", ordinal)
    planted["artifact_id"] = artifact_id(
        PERLECTOR, source["kind"], source["subject_id"], planted["attempt_id"]
    )
    if source["kind"] == "page-reading":
        planted["payload"]["attempt_ordinal"] = ordinal
    _rewrite(root, planted)


@pytest.fixture(scope="module")
def recovers(tmp_path_factory):
    return _tree(tmp_path_factory.mktemp("reask-recovers"), "reask-recovers")


@pytest.fixture(scope="module")
def happy(tmp_path_factory):
    return _tree(tmp_path_factory.mktemp("reask-happy"), "happy")


# --- the fixture scenarios -----------------------------------------------------------


def test_a_page_whose_reading_left_a_record_out_is_re_asked_about_its_boxed_ids(recovers):
    root, _protocol, _recovery = recovers
    first, second = _reading(root, 1, 1), _reading(root, 1, 2)
    assert _reading(root, 2, 2) is None
    reask = second["payload"]["reask"]
    assert [(item["id"], item["code"]) for item in reask["named"]] == [
        ("A2", "unaccounted-witness-unit"),
        ("B2", "record-not-read"),
        ("B2", "unaccounted-witness-unit"),
        *((f"L{n}", "unread-line") for n in range(5, 10)),
    ]
    # Churro's unboxed line is left unaccounted for, and never named.
    assert "C2" not in {item["id"] for item in reask["named"]}
    assert reask["prior_entries"] == [
        {"n": 1, "kind": "act", "label": "synthetic act a1", "cites": ["A1", "B1", "C1"]}
    ]
    assert reask["budget"] == 1
    assert _names(reask["trigger_reading_ref"], first)
    assert _names(reask["trigger_accounting_ref"], _accounting(root, 1, 1))
    for ref in (reask["trigger_reading_ref"], reask["trigger_accounting_ref"]):
        assert ref in second["inputs"]
    assert reask["prompt"]["serving_recipe"] == "fake-perlector-v0"
    assert reask["prompt"]["builder_sha256"] == page_prompt.BUILDER_SHA256
    payload = second["payload"]
    assert (payload["schema"], payload["parse_state"], payload["disposition"]) == (
        "perlector-page-reading.v2",
        "parsed",
        "read",
    )
    assert first["payload"]["attempt_ordinal"] == 1 and first["payload"]["reask"] is None
    assert second["attempt_id"] == page_path.page_reading_attempt(second["subject_id"], 2)


def test_the_re_ask_shows_the_first_entries_without_their_text(recovers):
    root, _protocol, _recovery = recovers
    feed = _on_page(root, "page-feed", 1)[0]["payload"]
    second = _reading(root, 1, 2)["payload"]
    shown = {key: second["reask"][key] for key in ("prior_entries", "named")}
    fixture_text = page_prompt.page_reask_prompt("fake-perlector-v0", feed, shown)
    assert fixture_text.startswith(page_prompt.build_page_prompt("fake-perlector-v0", feed) + "\n")
    assert second["request_digest"] == page_path.request_digest(
        fixture_text, page_path.request_image_sha256s(feed)
    )
    real = page_prompt.page_reask_prompt("unproven-real-perlector", feed, shown)
    assert real == fixture_text + "\n" + page_prompt.page_reask_instruction(feed)
    first = _reading(root, 1, 1)["payload"]["answer"]
    for act in first["entries"]:
        assert act["text"] not in real
    assert "Cite only the ids just above" in real
    order = (
        "entry 1 (act)",
        "witness units no entry above cites or sets aside:\nA2 [",
        "detected lines outside every entry above:\nL5 [",
        "detector records outside every entry above:\nB2 [",
        "The ids just above",
    )
    assert [real.index(part) for part in order] == sorted(real.index(part) for part in order)
    with pytest.raises(ValueError, match="no declared page re-ask builder"):
        page_prompt.page_reask_prompt("another-recipe", feed, shown)


def test_a_recovered_entry_is_an_act_of_its_own_read_on_re_ask(recovers):
    root, _protocol, _recovery = recovers
    last = _accounting(root, 1, 2)
    assert last["payload"]["holds"] == ["unaccounted-witness-unit"]
    assert [f["id"] for f in last["payload"]["rules"]["c"]["findings"]] == ["C2"]
    assert last["payload"]["rules"]["j"] == {"status": "pass", "findings": []}
    second = _reading(root, 1, 2)
    assert _names(last["payload"]["page_reading_ref"], second)
    assert any(_names(ref, _reading(root, 1, 1)) for ref in last["inputs"])
    assert any(_names(ref, second) for ref in last["inputs"])
    regions = sorted(
        _on_page(root, "act-region", 1), key=lambda r: "reading_attempt" in r["payload"]
    )
    readings = sorted(
        _on_page(root, "perlectio", 1), key=lambda r: "reading_attempt" in r["payload"]
    )
    assert [r["payload"].get("reading_attempt") for r in regions + readings] == [None, 2, None, 2]
    for record in regions + readings:
        assert _names(record["payload"]["page_accounting_ref"], last)
        assert record["payload"]["page_holds"] == ["unaccounted-witness-unit"]
    recovered_region, recovered = regions[1], readings[1]
    assert _names(recovered["payload"]["page_reading_ref"], second)
    assert recovered["payload"]["text"] == "SYNTHETIC ACT TWO delta epsilon zeta eta"
    # Numbered on after the first reading's one entry, as the accounting names it.
    assert recovered["payload"]["n"] == recovered_region["payload"]["n"] == 2
    assert recovered["payload"]["reading_n"] == recovered_region["payload"]["reading_n"] == 1
    assert [(e["n"], e["reading_attempt"], e["reading_n"]) for e in last["payload"]["entries"]] == [
        (1, 1, 1),
        (2, 2, 1),
    ]
    assert "reading_n" not in regions[0]["payload"] and "reading_n" not in readings[0]["payload"]
    assert recovered["payload"]["continues_from_previous_page"] is False
    assert recovered["payload"]["continues_to_next_page"] is False
    verify(
        recovered_region["subject_id"],
        "act",
        act_bindings(
            recovered_region["payload"]["page_id"],
            "reading",
            {
                "page_reading": second["attempt_id"],
                "n": 1,
                "union_box_px": recovered_region["payload"]["union_box_px"],
            },
        ),
    )
    assert recovered_region["payload"]["page_reading_attempt"] == second["attempt_id"]
    # Page 2 was not re-asked: its act records name its first accounting.
    [page_two] = _on_page(root, "perlectio", 2)
    assert _names(page_two["payload"]["page_accounting_ref"], _accounting(root, 2, 1))


@pytest.mark.parametrize(
    ("scenario", "state", "holds"),
    [
        (
            "reask-sets-aside",
            "parsed",
            {"reask-set-aside", "set-aside-record", "set-aside-substantial"},
        ),
        (
            "reask-malformed",
            "malformed",
            {"reask-unread", "unaccounted-witness-unit", "unread-line"},
        ),
        ("reask-cut-off", "cut-off", {"reask-unread", "unaccounted-witness-unit", "unread-line"}),
    ],
)
def test_a_re_ask_that_recovers_nothing_leaves_the_page_held(tmp_path, scenario, state, holds):
    root = _tree(tmp_path, scenario)[0]
    second = _reading(root, 1, 2)
    assert second["payload"]["parse_state"] == state
    last = _accounting(root, 1, 2)
    assert holds <= set(last["payload"]["holds"])
    # Nothing was recovered: page 1's one act is the first reading's, held by the last accounting.
    [region] = _on_page(root, "act-region", 1)
    assert "reading_attempt" not in region["payload"]
    assert _names(region["payload"]["page_accounting_ref"], last)
    assert region["outcome"] == "held"


def test_a_re_ask_that_reads_a_first_entry_again_is_held_as_its_duplicate(tmp_path):
    root = _tree(tmp_path, "reask-duplicate")[0]
    last = _accounting(root, 1, 2)["payload"]
    assert last["rules"]["j"]["findings"] == [
        {"code": "reask-duplicate", "n": 3, "reading_n": 1, "attempt_1_n": 2}
    ]
    assert "reask-duplicate" in last["holds"]
    assert len(_on_page(root, "perlectio", 1)) == 3


def test_a_unit_inside_an_entry_it_was_not_cited_by_is_held_and_never_re_asked(tmp_path):
    root = _tree(tmp_path, "reask-cited-forgot")[0]
    assert [r["payload"]["attempt_ordinal"] for r in _records(root, "page-reading")] == [1, 1]
    first = _accounting(root, 1, 1)["payload"]
    assert first["holds"] == ["unaccounted-witness-unit"]
    assert [f["id"] for f in first["rules"]["c"]["findings"]] == ["B2"]


def test_a_recovered_act_after_one_that_continues_is_not_an_off_edge_continuation(tmp_path):
    root = _tree(tmp_path, "reask-continuation")[0]
    last = _accounting(root, 1, 2)["payload"]
    assert last["rules"]["a"]["status"] == "pass"
    assert last["holds"] == ["unaccounted-witness-unit"]
    flags = {
        r["payload"].get("reading_attempt", 1): r["payload"]["continues_to_next_page"]
        for r in _on_page(root, "perlectio", 1)
    }
    assert flags == {1: True, 2: False}


def test_a_page_first_read_as_blank_is_recovered_whole(tmp_path):
    root = _tree(tmp_path, "blank-then-recovered")[0]
    assert _reading(root, 1, 1)["payload"]["answer"] == {
        "page_type": "register-acts",
        "writing": "handwritten",
        "entries": [],
        "set_aside": [],
    }
    last = _accounting(root, 1, 2)["payload"]
    assert last["holds"] == ["unaccounted-witness-unit"]
    assert [f["id"] for f in last["rules"]["c"]["findings"]] == ["C1", "C2"]
    recovered = _on_page(root, "perlectio", 1)
    assert sorted(r["payload"]["n"] for r in recovered) == [1, 2]
    assert all(r["payload"]["reading_attempt"] == 2 for r in recovered)


def test_with_the_re_ask_off_a_page_stands_on_its_first_reading(tmp_path):
    root = _tree(tmp_path / "off", "reask-off", reask=0)[0]
    assert [r["payload"]["attempt_ordinal"] for r in _records(root, "page-reading")] == [1, 1]
    first = _accounting(root, 1, 1)
    assert "unaccounted-witness-unit" in first["payload"]["holds"]
    [region] = _on_page(root, "act-region", 1)
    assert _names(region["payload"]["page_accounting_ref"], first)
    # With the re-ask on, the same first reading plans a re-ask it has no answer for.
    protocol = _page_protocol(tmp_path / "on" / "config")
    with pytest.raises(AssertionError, match="needs exactly one"):
        _chain(tmp_path / "on" / "runs", protocol, through_perlector=True, scenario="reask-off")


def test_a_budget_above_one_is_refused_at_the_door(tmp_path):
    protocol = _page_protocol(tmp_path / "config")
    recovery = reask_recovery_config(tmp_path / "config", 2)
    with pytest.raises(AssertionError, match=r"door\.py: .*page_level_reread 2, above the maximum"):
        _chain(tmp_path / "runs", protocol, "--recovery-config", str(recovery))


def test_a_fixture_re_ask_answer_is_required_for_a_planned_page_and_refused_for_another():
    context = SimpleNamespace(
        scenario="s",
        fixture={
            "page_reask_answer": [
                {"scenario": "s", "page_ordinal": 1, "answer": "{}"},
                {"scenario": "s", "page_ordinal": 2, "answer": "{}"},
                {"scenario": "s", "page_ordinal": 2, "answer": "{}"},
            ]
        },
    )
    assert page_path.fixture_reask_answer(context, 1, planned=True)["answer"] == "{}"
    assert page_path.fixture_reask_answer(context, 3, planned=False) is None
    with pytest.raises(ContractError, match="plans no re-ask; the answer would never be asked"):
        page_path.fixture_reask_answer(context, 1, planned=False)
    with pytest.raises(ContractError, match="declares 2 page re-ask answers"):
        page_path.fixture_reask_answer(context, 2, planned=True)
    with pytest.raises(ContractError, match="declares 0 page re-ask answers"):
        page_path.fixture_reask_answer(context, 3, planned=True)


def test_a_second_pass_leaves_every_byte_and_act_id_as_it_was(recovers, tmp_path, monkeypatch):
    tree = _copy(recovers, tmp_path)
    before = file_bytes_snapshot(tree[0])
    assert _stage(tree, "reask-recovers", monkeypatch) == 0
    assert file_bytes_snapshot(tree[0]) == before


def test_the_first_readings_records_are_the_same_bytes_before_and_after_the_re_ask(
    recovers, tmp_path, monkeypatch
):
    """A pass stopped before its re-ask phase has published page 1's first reading and
    accounting and no act record; the resumed pass re-asks and changes neither."""
    tree = _copy(recovers, tmp_path)
    root = tree[0]
    shutil.rmtree(root / "r" / "4_perlector")
    original = page_run._finish_reask

    def stopped(state, page, result):
        raise KeyboardInterrupt

    monkeypatch.setattr(page_run, "_finish_reask", stopped)
    with pytest.raises(KeyboardInterrupt):
        _stage(tree, "reask-recovers", monkeypatch)
    first = [_path_of(root, _reading(root, 1, 1)), _path_of(root, _accounting(root, 1, 1))]
    before = {path: path.read_bytes() for path in first}
    assert _on_page(root, "act-region", 1) == [] and _reading(root, 1, 2) is None
    monkeypatch.setattr(page_run, "_finish_reask", original)
    assert _stage(tree, "reask-recovers", monkeypatch) == 0
    assert {path: path.read_bytes() for path in first} == before
    assert file_bytes_snapshot(root / "r" / "4_perlector") == file_bytes_snapshot(
        recovers[0] / "r" / "4_perlector"
    )


# --- tampering ---------------------------------------------------------------------


def test_a_deleted_re_ask_is_refused_by_name(recovers, tmp_path, monkeypatch):
    tree = _copy(recovers, tmp_path)
    root = tree[0]
    _path_of(root, _reading(root, 1, 2)).unlink()
    with pytest.raises(FatalAccounting, match="were published without its re-ask reading"):
        _stage(tree, "reask-recovers", monkeypatch)


def test_a_re_ask_planted_on_a_page_its_plan_does_not_re_ask_is_refused(
    happy, tmp_path, monkeypatch
):
    tree = _copy(happy, tmp_path)
    _plant(tree[0], _reading(tree[0], 1, 1), 2)
    with pytest.raises(FatalAccounting, match=r"carries a re-ask's reading \(page-read:2\), but"):
        _stage(tree, "happy", monkeypatch)


def test_a_re_ask_accounting_planted_on_a_page_its_plan_does_not_re_ask_is_refused(
    happy, tmp_path, monkeypatch
):
    tree = _copy(happy, tmp_path)
    _plant(tree[0], _accounting(tree[0], 1, 1), 2)
    with pytest.raises(FatalAccounting, match=r"carries a re-ask's accounting \(page-read:2\)"):
        _stage(tree, "happy", monkeypatch)


def test_a_recovered_act_record_on_a_page_its_plan_does_not_re_ask_is_refused(
    happy, tmp_path, monkeypatch
):
    tree = _copy(happy, tmp_path)
    record = _on_page(tree[0], "perlectio", 1)[0]
    record["payload"].update(reading_attempt=2, reading_n=1)
    _rewrite(tree[0], record)
    with pytest.raises(FatalAccounting, match=r"carries a re-ask's act records \(page-read:2\)"):
        _stage(tree, "happy", monkeypatch)


def test_a_re_ask_planted_with_the_re_ask_off_is_refused(tmp_path, monkeypatch):
    tree = _tree(tmp_path / "off", "happy", reask=0)
    _plant(tree[0], _reading(tree[0], 1, 1), 2)
    with pytest.raises(FatalAccounting, match=r"carries a re-ask's reading \(page-read:2\)"):
        _stage(tree, "happy", monkeypatch)


def test_a_third_page_accounting_is_refused(recovers, tmp_path, monkeypatch):
    tree = _copy(recovers, tmp_path)
    _plant(tree[0], _accounting(tree[0], 1, 2), 3)
    with pytest.raises(FatalAccounting, match="page accounting attempt past its one re-ask"):
        _stage(tree, "reask-recovers", monkeypatch)


def test_a_third_page_reading_is_refused(recovers, tmp_path, monkeypatch):
    tree = _copy(recovers, tmp_path)
    _plant(tree[0], _reading(tree[0], 1, 2), 3)
    with pytest.raises(FatalAccounting, match="attempt past its one re-ask"):
        _stage(tree, "reask-recovers", monkeypatch)


@pytest.mark.parametrize(
    "change",
    [
        lambda reask: reask["named"].pop(),
        lambda reask: reask["prompt"].update(builder_sha256="0" * 64),
        lambda reask: reask.update(budget=2),
        lambda reask: reask["prior_entries"][0].update(label="another act"),
    ],
    ids=["named", "builder", "budget", "prior-entries"],
)
def test_a_re_ask_naming_another_re_ask_than_its_plan_is_refused(
    recovers, tmp_path, monkeypatch, change
):
    tree = _copy(recovers, tmp_path)
    second = _reading(tree[0], 1, 2)
    change(second["payload"]["reask"])
    _rewrite(tree[0], second)
    with pytest.raises(FatalAccounting, match="names another re-ask"):
        _stage(tree, "reask-recovers", monkeypatch)


def test_a_changed_re_ask_builder_refuses_the_sealed_re_ask(recovers, tmp_path, monkeypatch):
    tree = _copy(recovers, tmp_path)
    original = page_prompt.reask_prompt_evidence

    def rebuilt(serving_recipe, feed, reask):
        return {**original(serving_recipe, feed, reask), "builder_sha256": "0" * 64}

    monkeypatch.setattr(page_prompt, "reask_prompt_evidence", rebuilt)
    with pytest.raises(FatalAccounting, match="names another re-ask"):
        _stage(tree, "reask-recovers", monkeypatch)


def test_a_re_ask_accounting_whose_first_entries_were_edited_is_refused(
    recovers, tmp_path, monkeypatch
):
    tree = _copy(recovers, tmp_path)
    last = _accounting(tree[0], 1, 2)
    last["payload"]["entries"][0]["kind"] = "other"
    _rewrite(tree[0], last)
    with pytest.raises(FatalAccounting, match="not what its two readings give"):
        _stage(tree, "reask-recovers", monkeypatch)


def test_a_re_ask_citing_an_id_it_was_not_asked_about_is_held_whole(
    recovers, tmp_path, monkeypatch
):
    tree = _copy(recovers, tmp_path)
    shutil.rmtree(tree[0] / "r" / "4_perlector")
    answer = json.loads(REASK_ROWS[("reask-recovers", 1)]["answer"])
    answer["entries"][0]["cites"] = ["A2", "A1"]
    monkeypatch.setattr(
        page_path,
        "fixture_reask_answer",
        lambda context, ordinal, planned: {"answer": json.dumps(answer)} if planned else None,
    )
    assert _stage(tree, "reask-recovers", monkeypatch) == 0
    second = _reading(tree[0], 1, 2)["payload"]
    assert (second["parse_state"], second["disposition"]) == ("parsed", "held")
    assert [p["code"] for p in second["problems"]] == ["unknown-id"]
    [unread] = _accounting(tree[0], 1, 2)["payload"]["rules"]["j"]["findings"]
    assert unread["code"] == "reask-unread" and unread["problems"] == ["unknown-id"]
    assert all("reading_attempt" not in r["payload"] for r in _records(tree[0], "perlectio"))


# --- live -----------------------------------------------------------------------


def _first_act_only() -> ScriptedAnswer:
    answer = json.loads(PAGE_ANSWERS[1])
    answer["entries"] = [{**answer["entries"][0], "continues_to_next_page": False}]
    return _scripted(answer)


def _page_two() -> ScriptedAnswer:
    answer = json.loads(PAGE_ANSWERS[2])
    answer["entries"][0]["continues_from_previous_page"] = False
    return _scripted(answer)


def _recovered() -> ScriptedAnswer:
    return ScriptedAnswer(content=REASK_ROWS[("reask-recovers", 1)]["answer"], finish_reason="stop")


@pytest.fixture(scope="module")
def live_reask(tmp_path_factory):
    return _live_chain(tmp_path_factory.mktemp("live-reask"))


@pytest.fixture()
def live(live_reask, tmp_path):
    import dataclasses

    root = tmp_path / "runs"
    shutil.copytree(live_reask.root, root)
    return dataclasses.replace(live_reask, root=root)


def test_a_live_re_ask_is_sent_once_its_first_reading_is_published_with_the_same_images(
    live, tmp_path, monkeypatch
):
    # Page 1's re-ask joins the window as soon as its first reading is published,
    # ahead of page 2's first reading.
    endpoint, exit_code = _read_pages(
        live, tmp_path, monkeypatch, _first_act_only(), _recovered(), _page_two()
    )
    assert exit_code == 0
    requests = _chat_requests(endpoint)
    assert len(requests) == 3
    images = [
        [
            part["image_url"]
            for part in request["messages"][0]["content"]
            if part["type"] == "image_url"
        ]
        for request in requests
    ]
    assert images[1] == images[0] != images[2]
    feed = _on_page(live.root, "page-feed", 1)[0]["payload"]
    reask = _reading(live.root, 1, 2)["payload"]["reask"]
    shown = {key: reask[key] for key in ("prior_entries", "named")}
    recipe = reask["prompt"]["serving_recipe"]
    text = requests[1]["messages"][0]["content"][-1]["text"]
    assert text == page_prompt.page_reask_prompt(recipe, feed, shown)
    sent = _records(live.root, "reader-sent")
    assert sorted((r["payload"]["pass"], r["payload"]["attempt_ordinal"]) for r in sent) == [
        ("page-reading", 1),
        ("page-reading", 1),
        ("page-reask", 2),
    ]
    reask_sent = [r for r in sent if r["payload"]["pass"] == "page-reask"]
    assert [r["payload"]["attempt_ordinal"] for r in reask_sent] == [2]
    second = _reading(live.root, 1, 2)
    payload = second["payload"]
    assert payload["disposition"] == "read" and payload["engine_call"] is not None
    assert payload["sampling"] == _reading(live.root, 1, 1)["payload"]["sampling"]
    assert payload["capacity"]["capacity"]["fits"] is True
    assert requests[1]["max_tokens"] == payload["capacity"]["max_tokens"]
    assert any(_names(ref, reask_sent[0]) for ref in second["inputs"])
    recovered = [
        r for r in _on_page(live.root, "perlectio", 1) if "reading_attempt" in r["payload"]
    ]
    assert [r["payload"]["engine_call"] == payload["engine_call"] for r in recovered] == [True]


def test_a_resumed_live_pass_adopts_its_sealed_re_ask_and_asks_nothing(live, tmp_path, monkeypatch):
    _endpoint, exit_code = _read_pages(
        live, tmp_path, monkeypatch, _first_act_only(), _recovered(), _page_two()
    )
    assert exit_code == 0
    before = file_bytes_snapshot(live.root / "r" / "4_perlector" / "artifacts")
    endpoint, exit_code = _read_pages(live, tmp_path / "again", monkeypatch)
    assert exit_code == 0 and _chat_requests(endpoint) == []
    assert file_bytes_snapshot(live.root / "r" / "4_perlector" / "artifacts") == before


def test_a_re_ask_stopped_after_its_send_with_its_reply_retained_is_refused_by_name(
    live, tmp_path, monkeypatch
):
    original = page_run._publish_reading

    def stopped_before_the_re_ask_is_recorded(state, page, request, result):
        if request.ordinal == 2:
            raise KeyboardInterrupt
        return original(state, page, request, result)

    monkeypatch.setattr(page_run, "_publish_reading", stopped_before_the_re_ask_is_recorded)
    with pytest.raises(KeyboardInterrupt):
        _read_pages(live, tmp_path, monkeypatch, _first_act_only(), _recovered(), _page_two())
    monkeypatch.setattr(page_run, "_publish_reading", original)
    assert _reading(live.root, 1, 2) is None
    # The resumed pass reads page 2, then judges the earlier re-ask send with nothing
    # of its own out, and refuses it.
    with pytest.raises(ContractError, match="asking again would read them twice"):
        _read_pages(live, tmp_path / "again", monkeypatch, _page_two(), _recovered())
    assert _reading(live.root, 1, 2) is None


def test_a_re_ask_stopped_after_its_send_with_no_reply_retained_is_sent_again(
    live, tmp_path, monkeypatch
):
    original = page_run._call

    def stopped_before_the_call(run, page, request, images):
        if request.ordinal == 2:
            raise KeyboardInterrupt
        return original(run, page, request, images)

    monkeypatch.setattr(page_run, "_call", stopped_before_the_call)
    with pytest.raises(KeyboardInterrupt):
        _read_pages(live, tmp_path, monkeypatch, _first_act_only(), _recovered(), _page_two())
    monkeypatch.setattr(page_run, "_call", original)
    assert _reading(live.root, 1, 2) is None
    reask_sent = [
        r["payload"]["attempt_ordinal"]
        for r in _records(live.root, "reader-sent")
        if r["payload"]["pass"] == "page-reask"
    ]
    assert reask_sent == [2]
    # Its send has no reply that could be its answer, so the resumed pass asks again,
    # after page 2's first reading: a re-ask an earlier pass sent waits for the first
    # readings, so no reply of this pass is out when its earlier send is judged.
    endpoint, exit_code = _read_pages(
        live, tmp_path / "again", monkeypatch, _page_two(), _recovered()
    )
    assert exit_code == 0
    assert len(_chat_requests(endpoint)) == 2
    second = _reading(live.root, 1, 2)["payload"]
    assert second["disposition"] == "read" and second["engine_call"] is not None
    assert len(_on_page(live.root, "perlectio", 1)) == 2


def test_a_re_ask_over_the_rows_capacity_is_held_unread_and_never_sent(live, tmp_path, monkeypatch):
    record = {"fits": False, "reason": "the re-ask does not fit", "max_model_len": 1}

    def refused(*_args, **_kwargs):
        raise RequestCapacityRefusal("the re-ask does not fit the row", capacity=record)

    monkeypatch.setattr(page_path, "reask_request_capacity", refused)
    endpoint, exit_code = _read_pages(live, tmp_path, monkeypatch, _first_act_only(), _page_two())
    assert exit_code == 0
    assert len(_chat_requests(endpoint)) == 2
    assert not [
        r for r in _records(live.root, "reader-sent") if r["payload"]["pass"] == "page-reask"
    ]
    second = _reading(live.root, 1, 2)["payload"]
    assert second["parse_state"] == "refused-capacity" and second["engine_call"] is None
    assert second["capacity"] == {"capacity": record, "answer_reserve": None, "max_tokens": None}
    assert second["request_digest"] is None
    last = _accounting(live.root, 1, 2)["payload"]
    [unread] = last["rules"]["j"]["findings"]
    assert unread["code"] == "reask-unread" and unread["parse_state"] == "refused-capacity"


def test_a_failed_live_re_ask_call_is_failed_and_its_page_stands_on_its_first_reading(
    live, tmp_path, monkeypatch
):
    """A re-ask whose engine call fails is a `failed` page-reading the hard-failure cap
    counts once, and its page keeps exactly its first reading's acts, held under
    `reask-unread` rather than counted as an unread page."""
    root = live.root
    transport = ScriptedAnswer(transport_failure="connection reset after dispatch")
    _endpoint, exit_code = _read_pages(
        live, tmp_path, monkeypatch, _first_act_only(), transport, _page_two()
    )
    assert exit_code == 0
    second = _reading(root, 1, 2)
    payload = second["payload"]
    assert (second["outcome"], payload["parse_state"]) == ("failed", "call-failed")
    failure = payload["failure"]
    assert (failure["phase"], failure["kind"]) == ("page-reask", "transport")
    assert failure["code"] and failure["call_record_ref"] is not None
    assert failure["raw_response_ref"] is None
    assert failure["call_record_ref"] in second["inputs"]
    [unread] = _accounting(root, 1, 2)["payload"]["rules"]["j"]["findings"]
    assert unread["code"] == "reask-unread" and unread["parse_state"] == "call-failed"

    acts = _on_page(root, "perlectio", 1)
    first_regions = _on_page(root, "act-region", 1)
    assert len(acts) == 1 and len(first_regions) == 1
    assert all("reading_attempt" not in r["payload"] for r in acts + first_regions)
    assert (
        first_regions[0]["payload"]["page_reading_attempt"] == (_reading(root, 1, 1)["attempt_id"])
    )

    rows = reading_acts(_denominator_context(live))
    page_one = [row for row in rows if row["act_key"].startswith("p1:")]
    assert [(row["act_key"], row["reading_attempt"]) for row in page_one] == [("p1:1", 1)]
    assert _names(page_one[0]["perlectio_ref"], acts[0])
    assert "reask-unread" in page_one[0]["hold_codes"]

    tally = tally_hard_failures(RunTree(root, "r"), load_hard_failure_policy())
    assert tally["by_kind"]["perlector:failed"] == [second["subject_id"]]
    assert tally["count"] == 1


def test_a_re_ask_the_accounting_does_not_count_adds_no_act(recovers, tmp_path, monkeypatch):
    """A re-ask whose reading is read but which the accounting does not count as standing
    publishes no act: the page's acts are its first reading's alone."""
    from common import page_accounting

    tree = _copy(recovers, tmp_path)
    shutil.rmtree(tree[0] / "r" / "4_perlector")
    original = page_accounting._combined

    def not_standing(first, reask, candidates):
        return {**original(first, reask, candidates), "stands": False, "entries": []}

    monkeypatch.setattr(page_accounting, "_combined", not_standing)
    assert _stage(tree, "reask-recovers", monkeypatch) == 0
    assert _reading(tree[0], 1, 2)["payload"]["disposition"] == "read"
    assert "reask-unread" in _accounting(tree[0], 1, 2)["payload"]["holds"]
    assert all("reading_attempt" not in r["payload"] for r in _records(tree[0], "act-region"))
    assert len(_on_page(tree[0], "perlectio", 1)) == 1


def test_the_page_doubt_hold_counts_the_first_reading_and_its_re_ask_together(
    recovers, tmp_path, monkeypatch
):
    """Page 1's first reading alone is 5 of 29 doubtful, under the page limit; its
    re-ask read with 20 of 33 doubtful takes the page to 25 of 62, over it. Every
    Perlectio the page publishes, first reading and re-ask alike, holds the page."""
    tree = _copy(recovers, tmp_path)
    shutil.rmtree(tree[0] / "r" / "4_perlector")
    original = page_path.fixture_reask_answer

    def doubtful(context, ordinal, *, planned):
        row = original(context, ordinal, planned=planned)
        if row is None:
            return row
        text = "SYNTHETIC ACT TWO delta epsilon zeta eta"
        return {
            **row,
            "answer": row["answer"].replace(text, "[[SYNTHETIC ACT TWO delta]]" + text[23:]),
        }

    monkeypatch.setattr(page_path, "fixture_reask_answer", doubtful)
    assert _stage(tree, "reask-recovers", monkeypatch) == 0
    readings = _on_page(tree[0], "perlectio", 1)
    assert sorted(r["payload"].get("reading_attempt", 1) for r in readings) == [1, 2]
    assert all(page_path.PAGE_DOUBT_SHARE_HIGH in r["payload"]["holds"] for r in readings), [
        r["payload"]["holds"] for r in readings
    ]


# ========================= early re-asks and the reading deadline =========================


def _early_reask_pass(monkeypatch, *, deadline_seconds: int | None):
    """Drive the first readings' job source over two pages, page 1 planning a re-ask.

    Every first reading and the re-ask would be sent; each planned call takes 100 s
    and the chair is already up.
    """
    from datetime import datetime, timedelta, timezone

    deadline = (
        None
        if deadline_seconds is None
        else datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)
    )
    state = SimpleNamespace(
        live=True,
        run=SimpleNamespace(args=SimpleNamespace(reading_deadline=deadline), page_max_tokens=1),
    )
    pages = [page_run._Page(page_id=f"p{n}", ordinal=n, page_record={}) for n in (1, 2)]

    def finish(_state, page, _result):
        if page.ordinal == 1:
            page.reask = page_run._Request(page_run.REASK_READING, page_run.PAGE_REASK_PASS)

    monkeypatch.setattr(page_run, "_sends", lambda *_args: True)
    monkeypatch.setattr(page_run, "_joins_early", lambda *_args: True)
    monkeypatch.setattr(page_run, "_startup_left", lambda _state: 0)
    monkeypatch.setattr(page_run, "planned_seconds_per_page", lambda _cap: 100)
    monkeypatch.setattr(page_run, "_finish", finish)
    monkeypatch.setattr(
        page_run, "_job", lambda _state, page, request, done: ((page.ordinal, request), done)
    )
    joined: set[str] = set()
    source = page_run._first_and_early_reask_jobs(state, pages, joined)
    drawn, finishes = [], []
    # Width 2: both first readings are drawn, then page 1's reply finishes while page 2's
    # call is still out, and the source is asked for more.
    for _ in range(2):
        (ordinal, request), done = next(source)
        drawn.append((ordinal, request.ordinal))
        finishes.append(done)
    finishes[0](None)
    drawn += [(ordinal, request.ordinal) for (ordinal, request), _done in source]
    return drawn, joined


def test_a_deadline_that_holds_only_the_first_readings_left_keeps_the_re_ask_for_its_phase(
    monkeypatch,
):
    # Page 2's first reading is still out (100 s); the re-ask would need 100 s more.
    drawn, joined = _early_reask_pass(monkeypatch, deadline_seconds=150)
    assert drawn == [(1, 1), (2, 1)]
    # Left for the re-ask window, which judges it against the deadline as before.
    assert joined == set()


def test_a_deadline_that_holds_the_re_ask_too_lets_it_join_the_first_readings(monkeypatch):
    drawn, joined = _early_reask_pass(monkeypatch, deadline_seconds=250)
    assert drawn == [(1, 1), (2, 1), (1, 2)]
    assert joined == {"p1"}
    drawn, joined = _early_reask_pass(monkeypatch, deadline_seconds=None)
    assert joined == {"p1"}
