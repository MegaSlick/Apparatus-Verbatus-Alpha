"""The bulk (no-witness) exporter on synthetic references and tiny generated images
(never a real page or text)."""

import hashlib
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common import page_prompt
from common.page_answer import parse_page_answer
from common.page_render import _downscale_page
from operations.training import bulk as B
from operations.training import export as E

TEXT = "Le dix mai mil huit cent Richer Lalonde"


def _png(shade: int, size=(300, 400)) -> bytes:
    image = Image.new("L", size, 255)
    for x in range(20, 120):
        image.putpixel((x, 50), shade)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _record(stem, status="agreed", kind="act", **extra):
    return {
        "schema": E.REFERENCE_SCHEMA,
        "stem": stem,
        "status_label": "fool's gold",
        "page_type": "register-acts",
        "writing": "handwritten",
        "entries": [
            {"kind": kind, "label": "baptême", "text": TEXT, **extra},
            {"kind": "other", "label": "  ", "text": "12"},
        ],
        "provenance": {"best_status": status},
    }


# volume -> {stem: status}
PAGES = {
    "VolA": {"A_001": "agreed", "A_002": "close", "A_003": "disputed", "A_004": "single"},
    "VolB": {"B_001": "agreed", "B_002": "majority", "B_003_1L": "agreed", "B_003_2R": "agreed"},
    "VolC": {"C_001": "agreed", "C_002": "agreed"},
}


@pytest.fixture
def corpus(tmp_path):
    rows = []
    for v, (volume, pages) in enumerate(PAGES.items()):
        (tmp_path / volume / "reference").mkdir(parents=True)
        for n, (stem, status) in enumerate(pages.items()):
            record = _record(stem, status)
            (tmp_path / volume / "reference" / f"{stem}.json").write_text(
                json.dumps(record, ensure_ascii=False)
            )
            data = _png(10 * v + n)
            image = tmp_path / "images" / f"{stem}.png"
            image.parent.mkdir(exist_ok=True)
            image.write_bytes(data)
            rows.append(
                {"stem": stem, "image": f"images/{stem}.png",
                 "sha256": hashlib.sha256(data).hexdigest()}
            )  # fmt: skip
    (tmp_path / "map.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (tmp_path / "held.txt").write_text("# frozen\nB_003_1L.tif\n")
    return tmp_path


def _run(corpus, out="out", **kw):
    refs = B.reference_paths([str(corpus / "*" / "reference")])
    images = B.read_images_map(corpus / "map.jsonl")
    held = E.read_held_out(corpus / "held.txt")
    manifest = B.export_bulk(refs, images, held, corpus / out, **kw)
    examples = []
    for split in ("train", "val"):
        path = corpus / out / f"{split}.jsonl"
        examples += [json.loads(line) for line in path.read_text().splitlines()]
    return manifest, examples


def test_targets_parse_and_the_prompt_is_the_pipelines_blind_prompt(corpus):
    manifest, examples = _run(corpus, val_share=0)
    assert examples and manifest["distinct_prompts"] == 1
    for ex in examples:
        user, assistant = ex["messages"]
        state, answer, problems = parse_page_answer(assistant["content"])
        assert state == "parsed", problems
        assert answer["set_aside"] == [] and all(e["cites"] == [] for e in answer["entries"])
        assert [e["n"] for e in answer["entries"]] == [1, 2]
        assert "label" not in answer["entries"][1]  # a blank label is dropped
        assert user["content"][0] == {"type": "image"}
        prompt = user["content"][1]["text"]
        assert "cites, an empty list, since no ids are shown" in prompt
        assert "witness " not in prompt.split("witness regime")[1].split("\n", 1)[1][:200]
        assert "surya" not in prompt
        assert ex["chat_template_kwargs"] == {"enable_thinking": False}
        spans = ex["loss_spans"]
        assert spans[0][0] == 0 and spans[-1][1] == len(assistant["content"])
        assert all(a[1] == b[0] for a, b in zip(spans, spans[1:], strict=False))
        assert (corpus / "out" / ex["images"][0]).is_file()
    # The prompt is exactly what the page feed builder renders for a no-testimony page.
    switches, _ = B.protocol_settings(None)
    render = B.render_record("x", {"image": "x.png", "sha256": "0" * 64},
                             examples[0]["image_transform"], "0" * 64)  # fmt: skip
    feed = B.blind_feed("x", render, (300, 400), switches)
    assert feed["witness_testimony"] == "none" and feed["prompt"] is not None
    expected = page_prompt.build_page_prompt(B.SERVING_RECIPE, feed)
    assert examples[0]["messages"][0]["content"][1]["text"] == expected


def test_held_out_pages_and_their_other_half_are_excluded(corpus):
    manifest, examples = _run(corpus)
    pages = {e["page"] for e in examples}
    assert "B_003_1L" not in pages and "B_003_2R" not in pages
    assert manifest["held_out_excluded"] == ["B_003_1L", "B_003_2R"]


def test_disputed_is_excluded_and_weights_follow_status(corpus):
    manifest, examples = _run(corpus)
    weight = {e["page"]: e["weight"] for e in examples}
    assert "A_003" not in weight and manifest["excluded_by_status"] == {"disputed": 1}
    assert weight["A_001"] == 0.7 and weight["A_002"] == 0.5
    assert weight["B_002"] == 0.5 and weight["A_004"] == 0.3
    manifest, examples = _run(
        corpus, out="out2", weights=B.parse_weights("disputed=0.1,single=exclude")
    )
    weight = {e["page"]: e["weight"] for e in examples}
    assert weight["A_003"] == 0.1 and "A_004" not in weight


def test_val_split_never_shares_a_volume_with_train(corpus):
    manifest, examples = _run(corpus, val_share=0.3)
    train = {e["volume"] for e in examples if e["split"] == "train"}
    val = {e["volume"] for e in examples if e["split"] == "val"}
    assert val and train and not (train & val)
    assert val == set(manifest["val_volumes"])
    assert {e["volume"] for e in examples} == {"VolA", "VolB", "VolC"}  # from the folders


def test_deterministic_with_seed_and_capped_per_volume(corpus):
    _, first = _run(corpus, out="o1", seed=3, max_per_volume=1)
    _, again = _run(corpus, out="o2", seed=3, max_per_volume=1)
    assert first == again
    assert sorted(e["volume"] for e in first) == ["VolA", "VolB", "VolC"]
    picks = {tuple(sorted(e["page"] for e in _run(corpus, out=f"s{s}", seed=s,
                                                   max_per_volume=1)[1]))
             for s in range(6)}  # fmt: skip
    assert len(picks) > 1  # the seed changes which page stands for a volume
    _, capped = _run(corpus, out="o3", seed=3, max_pages=2)
    assert len(capped) == 2 and len({e["volume"] for e in capped}) == 2


def test_kinds_cites_and_bad_images(corpus):
    path = corpus / "VolC" / "reference" / "C_001.json"
    path.write_text(json.dumps(_record("C_001", kind="baptism", cites=["A1", "A2"])))
    row = json.loads((corpus / "map.jsonl").read_text().splitlines()[-1])
    assert row["stem"] == "C_002"
    lines = (corpus / "map.jsonl").read_text().splitlines()
    row["sha256"] = "f" * 64
    (corpus / "map.jsonl").write_text("\n".join([*lines[:-1], json.dumps(row)]))
    manifest, examples = _run(corpus)
    [c1] = [e for e in examples if e["page"] == "C_001"]
    answer = json.loads(c1["messages"][1]["content"])
    assert answer["entries"][0]["kind"] == "other" and answer["entries"][0]["cites"] == []
    assert manifest["target_notes"]["kind_mapped_to_other"] == 1
    assert {"id": "C_002", "reason": "image sha256 differs from the map"} in manifest["refused"]


def test_bad_reference_files_are_refused_not_fatal(corpus):
    ref_dir = corpus / "VolC" / "reference"
    (ref_dir / "broken.json").write_text("{not json")
    nameless = _record("C_009")
    del nameless["stem"]
    (ref_dir / "nameless.json").write_text(json.dumps(nameless))
    manifest, examples = _run(corpus)
    reasons = {Path(r["id"]).name: r["reason"] for r in manifest["refused"]}
    assert reasons["broken.json"].startswith("reference unreadable")
    assert reasons["nameless.json"] == "reference names no stem"
    assert {"C_001", "C_002"} <= {e["page"] for e in examples}


def test_weights_refuse_an_unknown_status_name():
    with pytest.raises(SystemExit, match="unknown status 'agred'"):
        B.parse_weights("agred=0.9")
    assert B.parse_weights("unknown=exclude")["unknown"] is None


def test_render_is_the_pipelines_pixels(corpus):
    _, examples = _run(corpus, val_share=0)
    for ex in examples:
        data = (corpus / "images" / f"{ex['page']}.png").read_bytes()
        expected, _ = B.render_pixels(data, maximum_edge=2560)
        written = Image.open(corpus / "out" / ex["images"][0])
        assert written.mode == "L" and written.tobytes() == expected.tobytes()
    data = _png(0, size=(3000, 1200))
    pixels, transform = B.render_pixels(data, maximum_edge=2560)
    pipeline, pipeline_transform = _downscale_page(data, maximum_edge=2560)
    assert transform == pipeline_transform
    assert pixels.tobytes() == Image.open(BytesIO(pipeline)).convert("L").tobytes()
    assert B.image_tokens((1978, 2560), B.perlector_geometry()) == 4960  # the protocol's note


def test_catalogue_csv_columns_are_accepted(tmp_path):
    (tmp_path / "catalog.csv").write_text(
        "page_id,volume,collection,page_image,page_sha256\n"
        f"p1,V1,fur,/abs/p1.png,{'a' * 64}\np2,V1,fur,,\n"
    )
    images = B.read_images_map(tmp_path / "catalog.csv")
    assert images == {
        "p1": {"stem": "p1", "image": "/abs/p1.png", "sha256": "a" * 64,
               "volume": "V1", "collection": "fur"}
    }  # fmt: skip


def test_malformed_reference_shapes_are_refused_alone(corpus):
    ref_dir = corpus / "VolC" / "reference"
    bad_prov = _record("C_010")
    bad_prov["provenance"] = "agreed"
    (ref_dir / "C_010.json").write_text(json.dumps(bad_prov))
    bad_entry = _record("C_011")
    bad_entry["entries"] = ["not an object"]
    (ref_dir / "C_011.json").write_text(json.dumps(bad_entry))
    manifest, examples = _run(corpus)
    reasons = {r["id"]: r["reason"] for r in manifest["refused"]}
    assert reasons["C_010"] == "reference malformed: provenance is not an object"
    assert reasons["C_011"] == "reference malformed: entry 1 is not an object"
    assert {"C_001", "C_002"} <= {e["page"] for e in examples}


def test_a_dead_render_worker_refuses_its_pages(monkeypatch):
    class Broken:
        def result(self):
            raise B.BrokenProcessPool("worker died")

    class Pool:
        def __init__(self, max_workers):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, fn, job):
            return Broken()

    monkeypatch.setattr(B, "ProcessPoolExecutor", Pool)
    out = B.render_all([("P_1", "x.png", "0" * 64, 2560, None)], workers=2)
    assert out["P_1"] == {"stem": "P_1", "error": "render worker stopped before this page"}
