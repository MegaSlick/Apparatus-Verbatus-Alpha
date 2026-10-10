"""The fed-witness reader arm on a synthetic run tree, against the fake server (no GPU).

Every page, witness and reading here is made up; no real transcription is used.
"""

import copy
import gzip
import hashlib
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from common import page_prompt
from common.page_path import SURYA_ORDER_HEAD
from operations.bakeoff import fed_arm as F
from operations.bakeoff.test_bakeoff_runner import FAKE, _free_port

SERVED = "perlector-synthetic"
RECIPE = "unproven-real-perlector"


def _png(text: str) -> bytes:
    image = Image.new("L", (400, 300), 255)
    ImageDraw.Draw(image).text((20, 20), text, fill=0)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _row(letter, label, units, outcome="read"):
    return {
        "letter": letter,
        "witness_label": label,
        "chair": label,
        "unit_kind": "layout-block",
        "outcome": outcome,
        "testimonium_ref": {"relative_path": f"t/{label}.json", "sha256": "0" * 64},
        "findings": [],
        "answer_health": {"truncated": False, "repetition": []},
        "units": [
            {
                "id": f"{letter}{n}",
                "ordinal": n - 1,
                "box_px": {"x": 10, "y": 10 * n, "w": 300, "h": 9},
                "box_1000": [25, 33 * n, 775, 33 * n + 30],
                "label": "Text",
                "text": text,
            }
            for n, text in enumerate(units, start=1)
        ],
    }


def _feed(ordinal: int, image_path: str, png: bytes, first: str, third: str) -> dict:
    feed = {
        "schema": "perlector-page-feed.v2",
        "page_id": f"pg_{ordinal}",
        "page_ordinal": ordinal,
        "page_render": {"image_path": image_path, "image_sha256": hashlib.sha256(png).hexdigest()},
        "overlay": None,
        "page_size": {"w": 400, "h": 300},
        "switches": {
            "page_image": "legible",
            "page_overlay": "off",
            "surya_blocks": True,
            "surya_lines": True,
            "witness_coordinates": True,
            "witness_units": "own",
            "witnesses": "all",
        },
        "witness_regime": "named",
        "witness_testimony": "present",
        "witnesses": [
            _row("A", "attestator_1", [first, "Folio 1"]),
            _row("B", "attestator_2", [], outcome="genuinely-empty"),
            _row("C", "attestator_3", [third]),
        ],
        "surya": {
            "block_sequence": SURYA_ORDER_HEAD,
            "lines": [{"id": "L1", "box_1000": [25, 33, 775, 63]}],
            "blocks": [{"id": "S1", "box_1000": [20, 30, 780, 70], "label": "Text"}],
        },
    }
    feed["prompt"] = page_prompt.page_prompt_evidence(RECIPE, feed)
    return feed


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_run_tree(root: Path, pages=(("Le dix mai mil", "Le dix mars mil huit"),)) -> Path:
    """A run tree with one feed, render, reading and call record per page."""
    sealed = F.sealed_sampling()
    for ordinal, (first, third) in enumerate(pages, start=1):
        stem = f"p{ordinal:03d}"
        _write(root / f"1_exemplar/artifacts/page/{stem}.json",
               {"payload": {"ordinal": ordinal, "declared_path": f"{stem}.tif"}})  # fmt: skip
        png = _png(stem)
        image_path = f"4_perlector/blobs/sha256/{hashlib.sha256(png).hexdigest()}"
        (root / image_path).parent.mkdir(parents=True, exist_ok=True)
        (root / image_path).write_bytes(png)
        feed = _feed(ordinal, image_path, png, first, third)
        feed_rel = f"4_perlector/artifacts/page-feed/feed{ordinal}.json"
        _write(root / feed_rel, {"payload": feed})
        body, _ = F.build_body(
            feed, png, model_name=SERVED, sampling=sealed, seed=0, max_tokens=12_288, stream=True
        )
        call = {
            "served_model_id": SERVED,
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "image_sha256s": [hashlib.sha256(png).hexdigest()],
            "generation_sent": {"seed": 0, "max_tokens": 12_288},
            "stream": {"stopped": None},
            "usage": {"completion_tokens": 42, "prompt_tokens": 900},
        }
        call_rel = f"4_perlector/blobs/sha256/call{ordinal}"
        _write(root / call_rel, call)
        reading = {
            "page_ordinal": ordinal,
            "attempt_ordinal": 1,
            "feed_ref": {"relative_path": feed_rel},
            "engine_call": {"call_record_ref": {"relative_path": call_rel}},
            "parse_state": "parsed",
            "problems": [],
            "answer": {"acts": [{"n": 1, "kind": "act", "text": first, "cites": ["A1"]}]},
            "finish_reason": "stop",
            "stop_reason": "stop",
            "failure": None,
        }
        _write(root / f"4_perlector/artifacts/page-reading/r{ordinal}.json", {"payload": reading})
    return root


@pytest.fixture
def tree(tmp_path):
    return make_run_tree(tmp_path / "run", pages=(
        ("Le dix mai mil", "Le dix mars mil huit"),
        ("Le onze juin", "Le onze juin"),
        ("Le douze mai", "LOOP-TEST"),
    ))  # fmt: skip


def test_every_prompt_and_request_is_rebuilt_byte_for_byte(tree, tmp_path):
    report = F.verify_prompts(F.load_run_tree(tree))
    assert report["pages"] == report["prompts_identical"] == 3
    assert report["requests_checked"] == report["requests_identical"] == 3
    assert F.main(["prompts", "--run-tree", str(tree), "--json", str(tmp_path / "c.json")]) == 0
    # A feed whose recorded digest no longer matches its content is reported.
    path = tree / "4_perlector/artifacts/page-feed/feed1.json"
    record = json.loads(path.read_text())
    record["payload"]["witnesses"][0]["units"][0]["text"] = "changed"
    path.write_text(json.dumps(record))
    assert F.main(["prompts", "--run-tree", str(tree)]) == 1


def test_dropping_a_witness_reletters_in_label_order(tree):
    feed = F.load_run_tree(tree).pages[1].feed
    before = copy.deepcopy(feed)
    shown = F.apply_variant(feed, F.Variant(drop=("attestator_2",)), {})
    assert feed == before  # the sealed feed is never changed in place
    assert [(r["letter"], r["witness_label"]) for r in shown["witnesses"]] == [
        ("A", "attestator_1"),
        ("B", "attestator_3"),
    ]
    assert shown["witnesses"][1]["units"][0]["id"] == "B1"
    text = page_prompt.build_page_prompt(RECIPE, shown)
    assert "witness B (attestator_3)" in text and "attestator_2" not in text
    with pytest.raises(SystemExit, match="drop-witness"):
        F.apply_variant(feed, F.Variant(drop=("attestator_9",)), {})


def test_an_added_witness_brings_its_units_and_boxes(tree):
    feed = F.load_run_tree(tree).pages[1].feed
    dots = {
        "arm": "dots-mocr",
        "model": "dots-mocr",
        "finish_reason": "stop",
        "text": "ignored",
        "units": [
            {
                "request": {"loaded_size": [800, 600]},
                "cells": [
                    {"bbox": [80, 60, 720, 120], "category": "Text", "text": "Le dix mai"},
                    {"bbox": [0, 0, 10, 10], "category": "Picture", "text": ""},
                    {"bbox": [80, 200, 720, 400], "category": "Table",
                     "text": "<table><tr><td>Abel</td><td>12</td></tr></table>"},
                ],
            }
        ],
    }  # fmt: skip
    plain = {
        "arm": "party-blla",
        "model": "party-blla",
        "finish_reason": "length",
        "text": "Le dix",
    }
    variant = F.Variant(add=(("attestator_4", "x"), ("attestator_5", "y"), ("attestator_6", "z")))
    shown = F.apply_variant(feed, variant, {"attestator_4": dots, "attestator_5": plain})
    rows = {r["witness_label"]: r for r in shown["witnesses"]}
    assert [r["letter"] for r in shown["witnesses"]] == ["A", "B", "C", "D", "E", "F"]
    d = rows["attestator_4"]
    assert [u["id"] for u in d["units"]] == ["D1", "D2"]
    assert d["units"][0]["box_1000"] == [100, 100, 900, 200] and d["units"][1]["text"] == "Abel 12"
    e = rows["attestator_5"]
    assert e["units"][0]["box_1000"] is None and e["answer_health"]["truncated"] is True
    assert rows["attestator_6"]["outcome"] == "failed"
    text = page_prompt.build_page_prompt(RECIPE, shown)
    assert 'D1 [100,100,900,200] ("Text") "Le dix mai"' in text
    assert "witness F (attestator_6): failed, no units" in text


def test_the_swap_moves_letters_and_positions(tree):
    feed = F.load_run_tree(tree).pages[1].feed
    letters = F.Variant(letter_map=(("attestator_1", "C"), ("attestator_3", "A")))
    shown = F.apply_variant(feed, letters, {})
    assert [(r["letter"], r["witness_label"]) for r in shown["witnesses"]] == [
        ("C", "attestator_1"), ("B", "attestator_2"), ("A", "attestator_3"),
    ]  # fmt: skip
    assert shown["witnesses"][0]["units"][1]["id"] == "C2"
    both = F.Variant(
        letter_map=letters.letter_map, order=("attestator_3", "attestator_2", "attestator_1")
    )
    text = page_prompt.build_page_prompt(RECIPE, F.apply_variant(feed, both, {}))
    assert text.index("witness A (attestator_3)") < text.index("witness C (attestator_1)")
    assert "such as A2-A5" in text  # the instruction's example follows the first shown
    with pytest.raises(SystemExit, match="distinct"):
        F.apply_variant(feed, F.Variant(letter_map=(("attestator_1", "B"),)), {})
    with pytest.raises(SystemExit, match="witness-order"):
        F.apply_variant(feed, F.Variant(order=("attestator_1",)), {})


def test_image_variants(tree):
    run = F.load_run_tree(tree)
    page = run.pages[1]
    clear = F.page_image(run, page, F.Variant())
    assert clear == run.blob(page.feed["page_render"]["image_path"])
    for mode in ("blur", "blank", "swap"):
        changed = F.page_image(run, page, F.Variant(image=mode))
        assert changed is not None and changed != clear
        assert not F.Variant(image=mode).feed_changed  # the prompt stays the run's
    assert F.page_image(run, page, F.Variant(image="swap")) == run.blob(
        run.pages[2].feed["page_render"]["image_path"]
    )
    none = F.Variant(image="none")
    shown = F.apply_variant(page.feed, none, {})
    assert F.page_image(run, page, none) is None
    body, text = F.build_body(
        shown, None, model_name="m", sampling=F.sampling_for("greedy"), seed=0,
        max_tokens=10, stream=False,
    )  # fmt: skip
    assert "page image: not shown." in text and "image_url" not in body.decode()


def _run_argv(tree, out, label, *extra):
    return [
        "run", "--run-tree", str(tree), "--out", str(out), "--label", label,
        "--weights", str(tree), "--vllm-cmd", sys.executable, str(FAKE),
        "--port", str(_free_port()), "--startup-timeout", "60", "--concurrency", "2", *extra,
    ]  # fmt: skip


def test_one_run_end_to_end_against_the_fake_server(tree, tmp_path):
    (tree / "config.json").write_text("{}")  # a "snapshot" for --weights
    out = tmp_path / "cache"
    argv = _run_argv(tree, out, "sealed-repeat", "--model-name", SERVED, "--sampling", "sealed")
    assert F.main(argv) == 0
    record = json.loads((out / "sealed-repeat" / "p001.json").read_text())
    assert record["schema"] == F.SCHEMA and record["error"] is None
    assert record["prompt"]["matches_run"] is True
    assert record["request"]["matches_run"] is True  # the run's own request, byte for byte
    assert record["parse_state"] == "parsed" and record["text"] == "Le dix mai"
    assert record["finish_reason"] == "stop" and record["usage"]["prompt_tokens"] == 100
    assert gzip.decompress((out / "sealed-repeat" / "p001.sse.gz").read_bytes()).endswith(
        b"data: [DONE]\n\n"
    )
    loop = json.loads((out / "sealed-repeat" / "p003.json").read_text())
    assert loop["finish_reason"] == "repetition-loop" and loop["loop_stop"]["kind"] == "line"
    assert loop["parse_state"] == "malformed"
    # Resumes: nothing is sent again.
    assert F.main(argv) == 0
    events = [json.loads(x)["event"] for x in (out / "events.jsonl").read_text().splitlines()]
    assert events.count("nothing-to-do") == 1
    # The same label with another setup is refused.
    with pytest.raises(SystemExit, match="another setup"):
        F.main(_run_argv(tree, out, "sealed-repeat", "--model-name", "other"))


def test_variant_runs_greedy_and_without_an_image(tree, tmp_path):
    (tree / "config.json").write_text("{}")
    out = tmp_path / "cache"
    argv = _run_argv(
        tree, out, "no-image", "--model-name", "adapter-x", "--image", "none",
        "--drop-witness", "attestator_2", "--no-stream", "--pages", "p001,2",
    )  # fmt: skip
    assert F.main(argv) == 0
    record = json.loads((out / "no-image" / "p001.json").read_text())
    assert record["request"]["sampling"]["temperature"] == 0.0
    assert record["request"]["image_sha256"] is None and record["request"]["matches_run"] is False
    assert record["prompt"]["feed_changed"] is True and record["prompt"]["matches_run"] is False
    assert [r["letter"] for r in record["feed"]["witnesses"]] == ["A", "B"]
    assert record["model_name"] == "adapter-x" and record["text"] == "Le dix mai"
    assert not (out / "no-image" / "p003.json").exists()
