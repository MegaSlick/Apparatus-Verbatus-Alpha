"""The Perlector's page path, proven end to end on the synthetic fixture and fake serving.

A run sealed with `reading_unit = "page"` reads every sealed page whole: a
`page-feed`, a `page-reading` and, for a parsed and valid answer, one
`act-region` and one `perlectio.v2` per entry. The fixture tests run the real
chain as subprocesses; the live tests run the stage in this process against
`operations/serving/fakes.py`, as `test_live_perlector.py` does.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
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


def _run(program: str, root: Path, protocol: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
            "--perlector-protocol-config",
            str(protocol),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _chain(root: Path, protocol: Path, *extra: str, through_perlector: bool = False) -> None:
    for program in CHAIN + ((PERLECTOR_PROGRAM,) if through_perlector else ()):
        result = _run(program, root, protocol, *extra)
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


def _feed_ids(feed: dict[str, Any]) -> dict[str, Any]:
    return page_feed.placement_boxes(feed)


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
        feed = feeds[payload["page_id"]]
        boxes = _feed_ids(feed)
        assert set(payload["cited_ids"]) <= set(boxes)
        cited_boxes = [
            boxes[identifier] for identifier in payload["cited_ids"] if boxes[identifier]
        ]
        assert page_run.corners(payload["union_box_px"]) == [
            min(box["x"] for box in cited_boxes),
            min(box["y"] for box in cited_boxes),
            max(box["x"] + box["w"] for box in cited_boxes),
            max(box["y"] + box["h"] for box in cited_boxes),
        ]
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
        assert region["outcome"] == "read" and payload["holds"] == []


def test_a_perlectio_carries_clean_text_doubt_dissent_and_truncation(page_tree):
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
    rows = {row["letter"]: row for row in payload["dissent"]}
    assert rows["A"]["cited_units"] == ["A1"] and rows["A"]["departed"] is False
    # Churro's line reads "... alpha beta": it stops short, so the reading departs.
    assert rows["B"]["cited_units"] == ["B1"] and rows["B"]["departed"] is True
    assert first["outcome"] == "read"
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


# --- live serving, against the fakes ------------------------------------------------


def _catalogue(destination: Path, **overrides: Any) -> Path:
    """The committed fixture catalogue with its Perlector row live, and any field changed."""
    source = (ROOT / "config" / "serving_recipes.toml").read_text(encoding="utf-8")
    head = source.split('[[profiles]]\nkind = "fixture"\nrecipe = "fake-perlector-v0"')[0]
    row = {**_live_row(_perlector_identity()), **overrides}
    row["preflight_digest"] = profile_preflight_digest(row)
    body = "\n".join(f"{key} = {_toml_value(value)}" for key, value in row.items())
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "serving_recipes_live_perlector.toml"
    path.write_text(f"{head}[[profiles]]\n{body}\n", encoding="utf-8")
    return path


def _live_chain(base: Path, **row: Any) -> tuple[Path, Path, Path]:
    catalogue = _catalogue(base / "config", **row)
    protocol = _page_protocol(base / "config")
    _chain(base / "runs", protocol, "--serving-recipes-config", str(catalogue))
    return base / "runs", catalogue, protocol


@pytest.fixture(scope="module")
def live_chain(tmp_path_factory):
    return _live_chain(tmp_path_factory.mktemp("live-pages"))


@pytest.fixture()
def live_tree(live_chain, tmp_path):
    template, catalogue, protocol = live_chain
    root = tmp_path / "runs"
    shutil.copytree(template, root)
    return root, catalogue, protocol


def _read_pages(tree, tmp_path, monkeypatch, *answers: ScriptedAnswer):
    """Run the stage in this process against a scripted endpoint; return it and the exit."""
    root, catalogue, protocol = tree
    endpoint = FakeEndpoint(
        served_model_id=SERVED_MODEL_ID,
        blob_store=_TreeBlobs(root),
        assert_retained_before_next_request=True,
    )
    endpoint.script(*answers)
    factory = _serving_factory(endpoint, catalogue, tmp_path / "logs", tmp_path / "pod-gpu.lock")
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
            "happy",
            "--serving-recipes-config",
            str(catalogue),
            "--placement-tier",
            TIER,
            "--perlector-protocol-config",
            str(protocol),
        ],
    )
    return endpoint, perlector.main(serving_factory=factory)


def _answers() -> tuple[ScriptedAnswer, ScriptedAnswer]:
    return tuple(
        ScriptedAnswer(content=PAGE_ANSWERS[ordinal], finish_reason="stop") for ordinal in (1, 2)
    )


def _chat_requests(endpoint: FakeEndpoint) -> list[dict[str, Any]]:
    return [request for request in endpoint.requests if "messages" in request]


def test_a_live_page_is_sent_once_with_its_images_first_and_recorded_with_its_call(
    live_tree, tmp_path, monkeypatch
):
    root = live_tree[0]
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
    root = live_tree[0]
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


def test_an_answer_citing_an_id_the_feed_never_showed_is_held_with_its_answer(
    live_tree, tmp_path, monkeypatch
):
    root = live_tree[0]
    answer = json.loads(PAGE_ANSWERS[1])
    answer["acts"][0]["cites"] = ["A1", "Q7"]
    scripted = ScriptedAnswer(content=json.dumps(answer), finish_reason="stop")
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, scripted, scripted)
    assert exit_code == 0
    reading = _records(root, "page-reading")[0]["payload"]
    assert (reading["parse_state"], reading["disposition"]) == ("parsed", "held")
    assert reading["answer"] == answer
    assert {problem["code"] for problem in reading["problems"]} == {"unknown-id"}


def test_a_real_act_set_aside_is_published_but_its_page_holds(live_tree, tmp_path, monkeypatch):
    root = live_tree[0]
    answer = json.loads(PAGE_ANSWERS[1])
    answer["acts"] = answer["acts"][:1]
    answer["set_aside"] = [
        {"id": "A2", "reason": "not an entry"},
        {"id": "B2", "reason": "not an entry"},
    ]
    scripted = ScriptedAnswer(content=json.dumps(answer), finish_reason="stop")
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, scripted, _answers()[1])
    assert exit_code == 0
    reading = _records(root, "page-reading")[0]
    assert reading["payload"]["disposition"] == "read"
    account = next(
        r["payload"] for r in _records(root, "page-accounting") if r["payload"]["page_ordinal"] == 1
    )
    dispositions = {unit["id"]: unit["disposition"] for unit in account["units"]}
    assert dispositions["A2"] == dispositions["B2"] == "set-aside"
    assert "unread-ink" in account["holds"]


def test_a_held_page_reading_is_still_accounted(live_tree, tmp_path, monkeypatch):
    root = live_tree[0]
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
    readings = _records(tree[0], "page-reading")
    assert [r["payload"]["parse_state"] for r in readings] == ["refused-capacity"] * 2
    capacity = readings[0]["payload"]["capacity"]["capacity"]
    assert capacity["fits"] is False and capacity["max_model_len"] == 400
    assert readings[0]["payload"]["provenance"]["receipt_ref"] is None
    assert _records(tree[0], "reader-sent") == []


def test_a_resumed_pass_never_asks_a_read_page_again(live_tree, tmp_path, monkeypatch):
    root = live_tree[0]
    _endpoint, exit_code = _read_pages(live_tree, tmp_path, monkeypatch, *_answers())
    assert exit_code == 0
    before = file_bytes_snapshot(root / "r" / "4_perlector")
    endpoint, exit_code = _read_pages(live_tree, tmp_path / "again", monkeypatch)
    assert exit_code == 0
    assert _chat_requests(endpoint) == []
    assert file_bytes_snapshot(root / "r" / "4_perlector") == before


def test_a_page_sent_but_never_answered_is_sent_again_naming_the_first_send(
    live_tree, tmp_path, monkeypatch
):
    root = live_tree[0]
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
    root = live_tree[0]
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
