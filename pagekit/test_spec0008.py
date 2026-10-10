"""Split-first defaults (cropping off unless asked) and the stage cache.
Synthetic pages only; no real register material."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pagekit import _orient_testpages as pages
from pagekit.__main__ import main
from pagekit.answer import Answer
from pagekit.geometry import Chain, apply
from pagekit.output import MANIFEST_NAME
from pagekit.prepare import PROJECT_NAME, Detector
from pagekit.review import REVIEW_NAME

DPI = (150, 150)
CUT = {"pages": 2, "cut": [[1000.0, 0.0], [1008.0, 1399.0]]}


@pytest.fixture(autouse=True)
def _detectors(monkeypatch):
    """A given split and skew; the real page-box and content-box detectors, watched."""
    from pagekit import pipeline

    calls: list[str] = []

    def watched(step):
        real = pipeline.DETECTORS[step]

        def run(context):
            calls.append(step)
            return real.run(context)

        return Detector(real.method, run, real.settings, real.compare, real.files)

    detectors = {
        "split": Detector("test.split/1", lambda context: Answer(CUT, 0.9, "Given.", ())),
        "skew": Detector(
            "test.skew/1",
            lambda context: Answer(1.0 if context.page == 1 else -0.5, 0.9, "Given.", ()),
        ),
        "page_box": watched("page_box"),
        "content_box": watched("content_box"),
    }
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", detectors)
    return calls


def spread(folder: Path) -> Path:
    image = pages.spread(seed=31)
    ImageDraw.Draw(image).line((1004, 0, 1004, 1400), fill=60, width=3)
    path = folder / "src" / "spread.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, dpi=DPI)
    return path


def run(folder: Path, *extra: str) -> tuple[dict, Path]:
    out = folder / "out"
    main(["prepare", str(folder / "src"), "--output", str(out), *extra])
    return json.loads((out / MANIFEST_NAME).read_text()), out


# --- 1. Defaults -------------------------------------------------------------------------


def test_a_default_run_keeps_each_whole_side_of_the_cut_levelled(tmp_path, _detectors):
    spread(tmp_path)
    manifest, out = run(tmp_path)
    assert len(manifest["pages"]) == 2
    assert _detectors == []  # the box detectors do not run when cropping is off
    for page in manifest["pages"]:
        chain = Chain.from_dict(page["geometry"])
        grid = chain.levelled_size
        # The canvas holds the whole levelled side: no crop, so no source pixel of the
        # side is cut away.
        assert chain.crop_box == (0, 0, *grid) and page["output"]["size"] == list(grid)
        # Every corner of the side lands on the page.
        for x, y in apply(chain.upright_to_output(), chain.polygon):
            assert -1e-6 <= x <= grid[0] + 1e-6 and -1e-6 <= y <= grid[1] + 1e-6
        applied = page["applied"]
        assert applied == {
            "orientation_tag": False,
            "orientation": True,
            "split": True,
            "skew": True,
            "page_box": False,
            "content_box": False,
            "margin": False,
            "crop": "none",
        }
        assert "cropping is off" in page["steps"]["page_box"]["evidence"].lower()
        assert not [flag for flag in page["flags"] if flag["step"] in ("page_box", "content_box")]
    review = (out / REVIEW_NAME).read_text(encoding="utf-8")
    assert review.count("Cropping: off") == 2


def test_crop_content_crops_as_before_and_a_page_override_crops_one_page(tmp_path, _detectors):
    spread(tmp_path / "a")
    manifest, _ = run(tmp_path / "a", "--crop", "content")
    assert sorted(set(_detectors)) == ["content_box", "page_box"]
    for page in manifest["pages"]:
        assert page["applied"]["crop"] == "content" and page["applied"]["margin"] is True
        chain = Chain.from_dict(page["geometry"])
        assert chain.crop_box != (0, 0, *chain.levelled_size)
    spread(tmp_path / "b")
    fix = tmp_path / "b" / "fix.json"
    entry = {"source": "src/spread.png", "step": "crop", "page": 2, "value": "page"}
    fix.write_text(json.dumps({"schema": "pagekit-overrides.v1", "overrides": [entry]}))
    manifest, out = run(tmp_path / "b", "--overrides", str(fix))
    first, second = manifest["pages"]
    assert first["applied"]["crop"] == "none" and second["applied"]["crop"] == "page"
    assert second["applied"]["page_box"] is True and second["applied"]["content_box"] is False
    review = (out / REVIEW_NAME).read_text(encoding="utf-8")
    assert "Cropping: off" in review and "Cropping: to the page box" in review


# --- 2. The original is preserved ----------------------------------------------------------


def test_a_run_never_changes_the_source_folder(tmp_path):
    path = spread(tmp_path)
    os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    before = {
        entry.name: (entry.read_bytes(), entry.stat().st_mtime_ns)
        for entry in sorted((tmp_path / "src").iterdir())
    }
    run(tmp_path, "--crop", "content", "--output-mode", "grey")
    run(tmp_path)
    after = {
        entry.name: (entry.read_bytes(), entry.stat().st_mtime_ns)
        for entry in sorted((tmp_path / "src").iterdir())
    }
    assert after == before


# --- 3. The stage cache --------------------------------------------------------------------


def _cache(tmp_path: Path) -> Path:
    return tmp_path / "out.pagekit-cache"  # named after the output folder


def _index(tmp_path: Path, sha: str) -> dict:
    return json.loads((_cache(tmp_path) / sha / "index.json").read_text())


def _stamps(folder: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(folder)): (path.stat().st_mtime_ns, path.stat().st_ino)
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def test_the_cache_holds_every_stage_keyed_by_source_and_inputs(tmp_path):
    spread(tmp_path)
    manifest, out = run(tmp_path, "--crop", "content", "--cache-full")
    sha = manifest["pages"][0]["source"]["sha256"]
    index = _index(tmp_path, sha)
    project = json.loads((out / PROJECT_NAME).read_text())["sources"][0]
    stages = {(entry["stage"], entry["page"]): entry for entry in index["entries"]}
    assert sorted(stages) == sorted(
        [("opened", None), ("upright", None)]
        + [(stage, page) for page in (1, 2) for stage in ("side", "levelled", "boxes")]
    )
    # Keys: the source's sha256 and the inputs hash (and value) of the step that made it.
    assert stages[("upright", None)]["step"] == "split"
    assert stages[("upright", None)]["inputs_hash"] == project["steps"]["split"]["inputs_hash"]
    for page in (1, 2):
        record = project["pages"][page - 1]["steps"]
        assert stages[("levelled", page)]["inputs_hash"] == record["skew"]["inputs_hash"]
        assert stages[("boxes", page)]["inputs_hash"] == record["content_box"]["inputs_hash"]
    for entry in index["entries"]:
        assert entry["source_sha256"] == sha
        for name in entry["files"].values():
            assert (_cache(tmp_path) / sha / name).is_file()
        full = entry["files"].get("full")
        if full is not None:  # full resolution is lossless TIFF
            assert full.endswith(".tif")
            with Image.open(_cache(tmp_path) / sha / full) as image:
                assert image.info["compression"] == "tiff_adobe_deflate"
        assert entry["files"]["preview"].endswith(".png")
    for stage in ("opened", "side", "levelled"):
        assert all("full" in e["files"] for (s, _), e in stages.items() if s == stage)
    with Image.open(_cache(tmp_path) / sha / stages[("opened", None)]["files"]["full"]) as image:
        assert image.size == (2000, 1400)
    # The review sheet links to each preview.
    review = (out / REVIEW_NAME).read_text(encoding="utf-8")
    for entry in index["entries"]:
        assert f'href="../out.pagekit-cache/{sha}/{entry["files"]["preview"]}"' in review


def test_a_re_run_rewrites_nothing_and_a_changed_skew_only_that_page(tmp_path):
    spread(tmp_path)
    run(tmp_path)
    first = _stamps(_cache(tmp_path))
    run(tmp_path)
    assert _stamps(_cache(tmp_path)) == first
    fix = tmp_path / "fix.json"
    entry = {"source": "src/spread.png", "step": "skew", "page": 2, "value": 0.75}
    fix.write_text(json.dumps({"schema": "pagekit-overrides.v1", "overrides": [entry]}))
    manifest, _ = run(tmp_path, "--overrides", str(fix))
    sha = manifest["pages"][0]["source"]["sha256"]
    after = _stamps(_cache(tmp_path))
    index = _index(tmp_path, sha)
    changed = {
        (e["stage"], e["page"])
        for e in index["entries"]
        if any(f"{sha}/{name}" not in first for name in e["files"].values())
    }
    assert changed == {("levelled", 2)}
    unchanged = [name for name in first if name in after and first[name] == after[name]]
    assert len(unchanged) >= len(first) - 3  # only page 2's levelled files (and index)


def test_prepared_pages_are_the_same_with_the_cache_stale_deleted_or_off(tmp_path):
    spread(tmp_path)
    manifest, out = run(tmp_path)
    pages_before = {
        p["output"]["name"]: (out / p["output"]["name"]).read_bytes() for p in manifest["pages"]
    }
    sha = manifest["pages"][0]["source"]["sha256"]
    # A stale cache: every cached image replaced by noise of the same size.
    for path in (_cache(tmp_path) / sha).glob("*.png"):
        with Image.open(path) as image:
            size, mode = image.size, image.mode
        Image.new(mode, size, 0).save(path, "PNG")
    shutil.rmtree(out)
    manifest, out = run(tmp_path)
    assert {name: (out / name).read_bytes() for name in pages_before} == pages_before
    # Deleted.
    shutil.rmtree(_cache(tmp_path))
    shutil.rmtree(out)
    run(tmp_path)
    assert {name: (out / name).read_bytes() for name in pages_before} == pages_before
    assert _cache(tmp_path).is_dir()  # rebuilt
    # Off.
    shutil.rmtree(_cache(tmp_path))
    shutil.rmtree(out)
    run(tmp_path, "--no-cache")
    assert {name: (out / name).read_bytes() for name in pages_before} == pages_before
    assert not _cache(tmp_path).exists()


def test_the_cache_folder_can_be_set_and_never_lies_in_a_source_folder(tmp_path, capsys):
    spread(tmp_path)
    elsewhere = tmp_path / "my-cache"
    manifest, _ = run(tmp_path, "--cache", str(elsewhere))
    assert (elsewhere / manifest["pages"][0]["source"]["sha256"] / "index.json").is_file()
    assert (
        main(
            [
                "prepare",
                str(tmp_path / "src"),
                "--output",
                str(tmp_path / "o2"),
                "--cache",
                str(tmp_path / "src" / "c"),
            ]
        )
        == 2
    )
    assert "source folder" in capsys.readouterr().err
    assert not (tmp_path / "src" / "c").exists()


def test_the_box_detectors_may_report_when_off_and_never_change_the_page(tmp_path, _detectors):
    from pagekit.output import execute
    from pagekit.prepare import plan

    spread(tmp_path / "a")
    spread(tmp_path / "b")
    from pagekit import pipeline

    plain = execute(
        plan([tmp_path / "a" / "src"], tmp_path / "a" / "out", detectors=pipeline.DETECTORS)
    )
    assert _detectors == []
    reported = execute(
        plan(
            [tmp_path / "b" / "src"],
            tmp_path / "b" / "out",
            detectors=pipeline.DETECTORS,
            settings_overrides={"crop_detectors_when_off": 1},
        )
    )
    assert sorted(set(_detectors)) == ["content_box", "page_box"]
    for a, b in zip(plain["pages"], reported["pages"], strict=True):
        assert a["output"]["pixels_sha256"] == b["output"]["pixels_sha256"]
        assert a["steps"]["page_box"]["value"] == b["steps"]["page_box"]["value"]
        assert "would have cut to" in b["steps"]["page_box"]["evidence"]
        assert b["applied"]["page_box"] is False


def test_a_default_cache_holds_small_thumbnails_only_and_full_images_on_request(tmp_path):
    spread(tmp_path / "a")
    spread(tmp_path / "b")
    manifest, out_a = run(tmp_path / "a")
    full, out_b = run(tmp_path / "b", "--cache-full")
    sha = manifest["pages"][0]["source"]["sha256"]
    thumbs = tmp_path / "a" / "out.pagekit-cache" / sha
    index = json.loads((thumbs / "index.json").read_text())
    assert all(set(entry["files"]) == {"preview"} for entry in index["entries"])
    assert not list(thumbs.glob("*.tif"))
    total = sum(path.stat().st_size for path in thumbs.iterdir())
    assert total < 400_000  # a 2000 x 1400 spread: under 400 kB of thumbnails
    for path in thumbs.glob("*.png"):
        with Image.open(path) as image:
            assert max(image.size) <= 320
    big = tmp_path / "b" / "out.pagekit-cache" / sha
    full_index = json.loads((big / "index.json").read_text())
    with_full = {(e["stage"], e["page"]) for e in full_index["entries"] if "full" in e["files"]}
    assert with_full == {
        ("opened", None),
        ("side", 1),
        ("side", 2),
        ("levelled", 1),
        ("levelled", 2),
    }
    for a, b in zip(manifest["pages"], full["pages"], strict=True):
        assert (out_a / a["output"]["name"]).read_bytes() == (
            out_b / b["output"]["name"]
        ).read_bytes()
