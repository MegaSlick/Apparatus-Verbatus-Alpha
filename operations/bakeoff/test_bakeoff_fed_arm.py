"""The fed-witness reader arm on a synthetic run tree, against the fake server (no GPU).

Every page, witness and reading here is made up; no real transcription is used.
"""

import copy
import gzip
import hashlib
import io
import json
import sys
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from common import page_overlay, page_prompt
from common.page_path import SURYA_ORDER_HEAD, request_digest
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
            "page_types": "named",
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


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wire(value):
    if isinstance(value, float):
        return {"decimal": repr(value), "schema": "wire-decimal.v1"}
    return value


def make_run_tree(
    root: Path, pages=(("Le dix mai mil", "Le dix mars mil huit"),), *, overlay: bool = False
) -> Path:
    """A run tree with one feed, render, reading and call record per page; with
    `overlay`, every feed draws the page overlay and its request sends two images."""
    sealed = F.sealed_sampling()
    for ordinal, (first, third) in enumerate(pages, start=1):
        stem = f"p{ordinal:03d}"
        _write(root / f"1_exemplar/artifacts/page/{stem}.json",
               {"subject_id": f"pg_{ordinal}",
                "payload": {"ordinal": ordinal, "declared_path": f"{stem}.tif"}})  # fmt: skip
        png = _png(stem)
        image_path = f"4_perlector/blobs/sha256/{hashlib.sha256(png).hexdigest()}"
        (root / image_path).parent.mkdir(parents=True, exist_ok=True)
        (root / image_path).write_bytes(png)
        feed = _feed(ordinal, image_path, png, first, third)
        if overlay:
            feed["page_render"]["transform"] = {"target_dimensions": {"w": 400, "h": 300}}
            feed["surya"]["lines"][0]["box_px"] = {"x": 10, "y": 10, "w": 300, "h": 9}
            feed["surya"]["blocks"][0]["box_px"] = {"x": 8, "y": 9, "w": 304, "h": 12}
            feed["switches"]["page_overlay"] = "boxes"
            feed["overlay"] = page_overlay.overlay_record(png, page_overlay.overlay_plan(feed))
            feed["prompt"] = page_prompt.page_prompt_evidence(RECIPE, feed)
        images = [png]
        if overlay:
            images.append(page_overlay.overlay_image(feed, lambda rel: (root / rel).read_bytes()))
        digests = [hashlib.sha256(image).hexdigest() for image in images]
        feed_rel = f"4_perlector/artifacts/page-feed/feed{ordinal}.json"
        _write(root / feed_rel, {"payload": feed})
        body, _ = F.build_body(
            feed, images, model_name=SERVED, sampling=sealed, seed=0, max_tokens=12_288,
            stream=True,
        )  # fmt: skip
        call = {
            "served_model_id": SERVED,
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "image_sha256s": digests,
            "generation_sent": {"seed": 0, "max_tokens": 12_288},
            "sampling_effective": {k: _wire(v) for k, v in sealed.items()},
            "stream": {"stopped": None, "loop_guard": F.loop_guard()},
            "usage": {"completion_tokens": 42, "prompt_tokens": 900},
        }
        call_rel = f"4_perlector/blobs/sha256/call{ordinal}"
        _write(root / call_rel, call)
        reading = {
            "page_ordinal": ordinal,
            "page_id": feed["page_id"],
            "attempt_ordinal": 1,
            "feed_ref": {"relative_path": feed_rel, "sha256": _digest_file(root / feed_rel)},
            "request_digest": request_digest(page_prompt.build_page_prompt(RECIPE, feed), digests),
            "engine_call": {
                "call_record_ref": {
                    "relative_path": call_rel,
                    "sha256": _digest_file(root / call_rel),
                }
            },
            "parse_state": "parsed",
            "problems": [],
            "answer": {
                "page_type": "register-acts",
                "writing": "handwritten",
                "entries": [{"n": 1, "kind": "act", "text": first, "cites": ["A1"]}],
            },
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
    # A feed changed after its reading named it is refused by digest, not rebuilt.
    path = tree / "4_perlector/artifacts/page-feed/feed1.json"
    record = json.loads(path.read_text())
    record["payload"]["witnesses"][0]["units"][0]["text"] = "changed"
    path.write_text(json.dumps(record))
    with pytest.raises(SystemExit, match="names its feed with digest"):
        F.main(["prompts", "--run-tree", str(tree)])


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
        F.main(
            _run_argv(
                tree, out, "sealed-repeat", "--model-name", "other", "--accept-new-model-name"
            )
        )


def test_variant_runs_greedy_and_without_an_image(tree, tmp_path):
    (tree / "config.json").write_text("{}")
    out = tmp_path / "cache"
    argv = _run_argv(
        tree, out, "no-image", "--model-name", "adapter-x", "--accept-new-model-name", "--image", "none",
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


# --- review fixes (C7, 2026-10-09): identity, overlays, seals, config, deadlines ------


def test_a_label_refuses_pages_cached_under_another_setup(tree, tmp_path):
    """C7 P1: seed, token cap, stream mode, checkpoint revision and run tree are part of a
    label's identity; a page cached under any other is refused, never mixed in."""
    (tree / "config.json").write_text("{}")
    out = tmp_path / "cache"
    base = ("--model-name", SERVED, "--pages", "p001")
    assert F.main(_run_argv(tree, out, "x", *base)) == 0
    assert F.main(_run_argv(tree, out, "x", *base)) == 0  # the same setup resumes
    for change in (
        ("--seed", "1"),
        ("--max-tokens", "4096"),
        ("--no-stream",),
        ("--revision", "f" * 40),
        ("--sampling", "sealed"),
    ):
        with pytest.raises(SystemExit, match="another setup"):
            F.main(_run_argv(tree, out, "x", *base, *change))
    # The same tree copied elsewhere is the same tree (identity by content) ...
    copy_ = tmp_path / "elsewhere" / "run"
    import shutil

    shutil.copytree(tree, copy_)
    assert F.main(_run_argv(copy_, out, "x", *base)) == 0
    # ... and another run tree with the same page names is not.
    other = make_run_tree(tmp_path / "other", pages=(("Le vingt mai", "Le vingt mai"),))
    (other / "config.json").write_text("{}")
    with pytest.raises(SystemExit, match="another setup"):
        F.main(_run_argv(other, out, "x", *base))


def test_a_changed_mutation_under_the_same_label_is_refused(tree, tmp_path):
    (tree / "config.json").write_text("{}")
    run = F.load_run_tree(tree)
    folder = tmp_path / "muts"
    folder.mkdir()
    feed = copy.deepcopy(run.pages[1].feed)
    record = {
        "schema": "witness-mutation.v1", "page": "p001", "page_ordinal": 1,
        "page_sha": feed["page_render"]["image_sha256"], "scenario": "honest", "seed": 0,
        "turn": 0, "feed": feed, "planted": [], "changes": [],
        "reference_sha256": "w" * 64, "reference_record_sha256": "r" * 64,
    }  # fmt: skip
    (folder / "p001.json").write_text(json.dumps(record))
    out = tmp_path / "cache"
    argv = _run_argv(tree, out, "m", "--model-name", SERVED, "--pages", "p001",
                     "--mutations", str(folder))  # fmt: skip
    assert F.main(argv) == 0
    record["feed"]["witnesses"][0]["units"][0]["text"] = "Le dix mars mil"
    (folder / "p001.json").write_text(json.dumps(record))
    with pytest.raises(SystemExit, match="another setup"):
        F.main(argv)


def test_a_mutation_from_another_page_or_feed_is_refused(tree):
    """C7: a stale mutation folder cannot supply another run's witness text."""
    run = F.load_run_tree(tree)
    page = run.pages[1]
    good = {
        "schema": "witness-mutation.v1", "page": "p001", "page_ordinal": 1,
        "page_sha": page.feed["page_render"]["image_sha256"], "feed": copy.deepcopy(page.feed),
        "reference_sha256": "w" * 64, "reference_record_sha256": "r" * 64,
    }  # fmt: skip
    variant = F.Variant(mutations=str(tree / "muts"))
    (tree / "muts").mkdir()

    def load(record):
        (tree / "muts" / "p001.json").write_text(json.dumps(record))
        return F.load_mutation(variant, "p001", page.feed)

    assert load(good)["_record_sha256"]
    other = run.pages[2].feed
    for bad, why in (
        ({**good, "page_ordinal": 2}, "page ordinal"),
        ({**good, "page_sha": "0" * 64}, "page digest"),
        ({**good, "feed": copy.deepcopy(other)}, "feed page_id"),
        ({**good, "feed": {**good["feed"], "feed_digest": "f" * 64}}, "feed feed_digest"),
        ({**good, "feed": {**good["feed"], "surya": None}}, "feed surya"),
    ):
        with pytest.raises(SystemExit, match=why):
            load(bad)


def test_render_and_call_record_bytes_are_digest_checked(tree):
    run = F.load_run_tree(tree)
    render = tree / run.pages[1].feed["page_render"]["image_path"]
    render.write_bytes(_png("another page"))
    with pytest.raises(SystemExit, match="does not match its recorded digest"):
        F.page_image(run, run.pages[1], F.Variant())
    call = tree / "4_perlector/blobs/sha256/call2"
    call.write_text(call.read_text().replace(SERVED, "someone-else"))
    with pytest.raises(SystemExit, match="call record"):
        F.load_run_tree(tree)


def test_a_real_run_tree_must_prove_its_stage_seals(tree):
    """C7: a tree carrying a run authority (run.json) is used only once its Exemplar and
    Perlector stage seals verify; this synthetic tree has none."""
    (tree / "run.json").write_text(json.dumps({"run_id": "run"}))
    with pytest.raises(SystemExit, match="stage seals do not verify"):
        F.main(["prompts", "--run-tree", str(tree)])


def test_overlay_feeds_send_the_render_and_the_overlay(tmp_path):
    """C7: the pipeline sends the render and the overlay (common/page_path.py); so does
    the fed arm, byte for byte, and a variant redraws the overlay from its own feed."""
    tree = make_run_tree(tmp_path / "run", overlay=True)
    run = F.load_run_tree(tree)
    report = F.verify_prompts(run)
    assert report["images_identical"] == report["requests_identical"] == 1
    assert report["reading_digests_identical"] == 1
    page = run.pages[1]
    images, shown = F.request_images(run, page, page.feed, F.Variant())
    assert [hashlib.sha256(i).hexdigest() for i in images] == page.call["image_sha256s"]
    assert len(images) == 2 and shown is page.feed
    body, text = F.build_body(shown, images, model_name="m", sampling=F.sampling_for("greedy"),
                              seed=0, max_tokens=10, stream=False)  # fmt: skip
    assert body.decode().count("data:image/png;base64") == 2
    # Dropping a witness changes the boxes drawn: a fresh overlay, recorded in the feed.
    variant = F.Variant(drop=("attestator_1",))
    feed = F.apply_variant(page.feed, variant, {})
    images2, shown2 = F.request_images(run, page, feed, variant)
    assert len(images2) == 2 and images2[1] != images[1]
    assert shown2["overlay"]["image_sha256"] == hashlib.sha256(images2[1]).hexdigest()
    # No image: no overlay either.
    none = F.Variant(image="none")
    images3, shown3 = F.request_images(run, page, F.apply_variant(page.feed, none, {}), none)
    assert images3 == [] and shown3["overlay"] is None
    # The run's recorded request is refused if only the render were sent.
    one, _ = F.build_body(page.feed, images[:1], model_name=SERVED, seed=0, max_tokens=12_288,
                          sampling=F.sealed_sampling(), stream=True)  # fmt: skip
    assert hashlib.sha256(one).hexdigest() != page.call["request_sha256"]


def test_decoding_that_differs_from_the_runs_is_refused(tree, tmp_path, monkeypatch):
    """C7: a sealed repeat is sent under the run's sealed sampling and loop guard, or not
    at all unless --accept-new-config says so (and the setup records it)."""
    (tree / "config.json").write_text("{}")
    assert F.config_differences(F.load_run_tree(tree)) == []
    guard = {**F.loop_guard(), "loop_line_repeats": 99}
    monkeypatch.setattr(F, "loop_guard", lambda: guard)
    run = F.load_run_tree(tree)
    assert any("loop guard" in d for d in F.config_differences(run))
    run.decoding_sha256 = "0" * 64
    assert any("decoding.toml" in d for d in F.config_differences(run))
    out = tmp_path / "cache"
    argv = _run_argv(tree, out, "c", "--model-name", SERVED, "--sampling", "sealed",
                     "--pages", "p001")  # fmt: skip
    with pytest.raises(SystemExit, match="accept-new-config"):
        F.main(argv)
    assert F.main([*argv, "--accept-new-config"]) == 0
    record = json.loads((out / "c" / "p001.json").read_text())
    assert record["setup"]["accepted"]["new_config"] is True
    assert record["setup"]["loop_guard"]["loop_line_repeats"] == 99


def test_a_timed_out_page_is_terminal_and_not_resent_at_the_same_settings(tmp_path):
    tree = make_run_tree(tmp_path / "run", pages=(("Le dix mai", "HANG-TEST"),))
    (tree / "config.json").write_text("{}")
    out = tmp_path / "cache"
    base = ("--model-name", SERVED)
    argv = _run_argv(tree, out, "t", *base, "--request-timeout", "1")
    assert F.main(argv) == 0  # a terminal failure is recorded, as witness_run records one
    record = json.loads((out / "t" / "p001.json").read_text())
    assert record["error"].startswith("request-timeout") and record["failure"]["terminal"]
    assert record["failure"]["settings"]["request_timeout"] == 1
    assert F.main(argv) == 0
    events = [json.loads(x) for x in (out / "events.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events].count("page-not-retried") == 1
    assert [e["event"] for e in events].count("nothing-to-do") == 1
    # A longer timeout is another condition: the page is sent again.
    assert F.main(_run_argv(tree, out, "t", *base, "--request-timeout", "20")) == 0
    assert json.loads((out / "t" / "p001.json").read_text())["error"] is None


class _Trickle:
    """A server that sends a streamed reply's headers and then a byte every 0.2 s."""

    def __enter__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b":\n")
                        self.wfile.flush()
                        time.sleep(0.2)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def test_a_trickling_stream_is_cut_at_the_total_deadline():
    """C7: a reply that keeps sending a little never outlives the request timeout, in the
    fed arm's stream and in witness_run's."""
    from operations.bakeoff import witness_run as W

    body = json.dumps({"stream": True, "messages": []}).encode()
    with _Trickle() as url:
        fed = F._stream(url, body, 1.0, F.loop_guard())
        witness = W.post(url, {"stream": True, "messages": []}, 1.0, None)
    assert fed["stop"] == W.REQUEST_TIMEOUT and fed["error"].startswith("request-timeout")
    assert fed["seconds"] < 1.6
    assert witness["stop"] == W.REQUEST_TIMEOUT and witness["seconds"] < 1.6


def test_a_quantized_recipe_is_refused_on_an_unquantized_snapshot(tree, tmp_path):
    """C7 (arms.py:274): the fed arm launching `--recipe unproven-real-perlector-fp8` on bf16
    weights is refused before vLLM would quantize them at load."""
    (tree / "config.json").write_text(json.dumps({"architectures": ["X"]}))
    argv = _run_argv(tree, tmp_path / "cache", "q", "--model-name", SERVED, "--pages", "p001",
                     "--recipe", "unproven-real-perlector-fp8", "--revision", "a" * 40)  # fmt: skip
    with pytest.raises(SystemExit, match="quantization"):
        F.main(argv)
    with pytest.raises(SystemExit):  # another checkpoint must say its revision
        F.parse_args(["run", "--run-tree", "t", "--out", "o", "--label", "l", "--model-name",
                      "m", "--weights", "w", "--recipe", "unproven-real-perlector-fp8"])  # fmt: skip


# --- bake-off day: the run's served name, and the pipeline's own answer parser -------


def test_the_runs_served_name_is_the_default_and_the_server_answers_under_it(tree, tmp_path):
    (tree / "config.json").write_text("{}")
    assert F.recorded_model_names(F.load_run_tree(tree)) == [SERVED]
    out = tmp_path / "cache"
    # No --model-name: the fake answers only under its --served-model-name, which the
    # arm sets from the resolved name, so a 200 shows the server was started under it.
    argv = _run_argv(tree, out, "default-name", "--sampling", "sealed", "--pages", "p001")
    assert F.main(argv) == 0
    record = json.loads((out / "default-name" / "p001.json").read_text())
    assert record["model_name"] == SERVED and record["error"] is None
    assert record["request"]["matches_run"] is True


def test_another_explicit_name_on_a_run_with_recorded_calls_is_refused(tree, tmp_path):
    (tree / "config.json").write_text("{}")
    out = tmp_path / "cache"
    for name in ("perlector-qwen3.8-27b-fp8", "adapter-x"):
        with pytest.raises(SystemExit, match="part of the request bytes") as refused:
            F.main(_run_argv(tree, out, "x", "--model-name", name))
        assert SERVED in str(refused.value)
    assert not (out / "x").exists()
    assert F.resolve_model_name(F.load_run_tree(tree), SERVED) == SERVED
    # On purpose (an adapter, a merge), and recorded in the cache's setup.
    argv = _run_argv(tree, out, "adapter", "--model-name", "adapter-x", "--pages", "p001",
                     "--accept-new-model-name")  # fmt: skip
    assert F.main(argv) == 0
    run = json.loads((out / "adapter" / "run.json").read_text())
    assert run["setup"]["accepted"]["new_model_name"] is True


def test_a_run_tree_without_recorded_calls_needs_a_name(tree):
    for path in (tree / "4_perlector/artifacts/page-reading").glob("*.json"):
        record = json.loads(path.read_text())
        del record["payload"]["engine_call"]
        path.write_text(json.dumps(record))
    bare = F.load_run_tree(tree)
    assert F.recorded_model_names(bare) == []
    with pytest.raises(SystemExit, match="records no Perlector call"):
        F.resolve_model_name(bare, None)
    assert F.resolve_model_name(bare, "any-name") == "any-name"


def test_a_bare_key_reply_is_parsed_as_the_pipeline_parses_it(tmp_path):
    tree = make_run_tree(tmp_path / "run", pages=(
        ("Le dix mai mil", "Le dix mars mil huit"),
        ("Le onze juin", "BARE-TEST"),
    ))  # fmt: skip
    (tree / "config.json").write_text("{}")
    out = tmp_path / "cache"
    assert F.main(_run_argv(tree, out, "bare", "--sampling", "sealed")) == 0
    clean = json.loads((out / "bare" / "p001.json").read_text())
    assert clean["parse_state"] == "parsed" and clean["repaired"] is False
    assert clean["answer_repairs"] == []
    bare = json.loads((out / "bare" / "p002.json").read_text())
    assert bare["parse_state"] == "parsed" and bare["parse_problems"] == []
    assert bare["repaired"] is True and bare["text"] == "Le dix mai"
    assert [(r["code"], r["keys"]) for r in bare["answer_repairs"]] == [
        ("unquoted-keys-quoted", 11)
    ]
    # The unrepaired grammar would have called it malformed; a loop-stopped or cut-off
    # reply is never repaired.
    from common import page_answer

    content = bare["content"]
    assert page_answer.parse_page_answer(content)[0] == "malformed"
    assert F.parse_reply({"content": content, "loop_stop": None, "finish_reason": "stop"})[0] == (
        "parsed"
    )
    for cut in ({"loop_stop": {"kind": "line"}, "finish_reason": "repetition-loop"},
                {"loop_stop": None, "finish_reason": "length"}):  # fmt: skip
        state, _, _, repairs = F.parse_reply({"content": content, **cut})
        assert state == "malformed" and repairs == []


def test_a_cached_page_with_no_recorded_setup_is_refused_by_name(tmp_path):
    cached = tmp_path / "p001.json"
    cached.write_text(json.dumps({"setup_sha256": "0" * 64}), "utf-8")
    with pytest.raises(SystemExit, match="another setup .*use a new --label"):
        F.cached_state(cached, {"model_name": "m"}, "r" * 64, "s" * 64)
