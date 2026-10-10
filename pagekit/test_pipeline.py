"""The prepare pipeline, the volume-wide checks, the review sheet and `measure`
on synthetic pages drawn here; no real register material."""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import os
import re
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pagekit import _box_synthetic as boxes
from pagekit import _orient_testpages as pages
from pagekit.__main__ import main
from pagekit.answer import Answer
from pagekit.measure import measure
from pagekit.output import MANIFEST_NAME, execute
from pagekit.pipeline import DETECTORS
from pagekit.prepare import PROJECT_NAME, Detector, plan
from pagekit.review import REVIEW_NAME

DPI = (150, 150)
CROP = {"crop": "content"}  # the box steps on, as specs 0004 and 0005 test them
PAPER = pages.PAPER
NAMES = ("a_upright", "b_sideways", "c_upside_down", "d_spread", "e_tilted", "f_blank")


def _batch(folder: Path) -> Path:
    """The synthetic batch: upright (in colour), sideways, upside down, a
    spread with a fold, tilted, and blank."""
    folder.mkdir(parents=True)
    grey = pages.page(seed=1)
    colour = Image.merge("RGB", (grey, grey, grey.point(lambda level: round(level * 0.88))))
    colour.save(folder / "a_upright.png", dpi=DPI)
    pages.turned(pages.page(seed=2), 1).save(folder / "b_sideways.png", dpi=DPI)
    pages.turned(pages.page(seed=3), 2).save(folder / "c_upside_down.png", dpi=DPI)
    spread = pages.spread(seed=4)
    ImageDraw.Draw(spread).line((1000, 0, 1000, 1400), fill=60, width=3)  # the fold
    spread.save(folder / "d_spread.png", dpi=DPI)
    boxes.turned(pages.page(seed=5), 2.0).save(folder / "e_tilted.png", dpi=DPI)
    Image.new("L", (1000, 1400), PAPER).save(folder / "f_blank.png", dpi=DPI)
    return folder


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(folder.iterdir()) if path.is_file()}


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    """The batch prepared twice into fresh folders, and its first output folder."""
    root = tmp_path_factory.mktemp("batch")
    source = _batch(root / "src")
    first, second = root / "out", root / "again"
    crop = ["--crop", "content"]  # these tests pin the boxes of specs 0004 and 0005
    assert main(["prepare", str(source), "--output", str(first), *crop]) == 1  # blank page
    assert main(["prepare", str(source), "--output", str(second), *crop]) == 1
    return {"root": root, "src": source, "out": first, "again": second}


def _by_source(out: Path) -> dict[str, list[dict]]:
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    found: dict[str, list[dict]] = {}
    for page in manifest["pages"]:
        found.setdefault(Path(page["source"]["name"]).stem, []).append(page)
    return found


def test_the_batch_gets_the_right_turns_counts_angles_and_boxes(prepared):
    found = _by_source(prepared["out"])
    assert sorted(found) == list(NAMES)
    turns = {name: found[name][0]["steps"]["orientation"]["value"] for name in NAMES[:5]}
    assert turns == {
        "a_upright": 0,
        "b_sideways": 3,
        "c_upside_down": 2,
        "d_spread": 0,
        "e_tilted": 0,
    }
    for name in NAMES:
        split = found[name][0]["steps"]["split"]["value"]
        assert split["pages"] == (2 if name == "d_spread" else 1)
        assert len(found[name]) == split["pages"]
        for page in found[name]:
            for step in ("orientation", "split", "skew", "page_box", "content_box"):
                assert page["steps"][step]["origin"] == "detected"
    (top, bottom) = found["d_spread"][0]["steps"]["split"]["value"]["cut"]
    assert abs(top[0] - 1000.5) <= 4 and abs(bottom[0] - 1000.5) <= 4
    assert found["e_tilted"][0]["steps"]["skew"]["value"] == pytest.approx(2.0, abs=0.3)
    for name in NAMES[:4]:
        assert found[name][0]["steps"]["skew"]["value"] == 0.0
    # The writing of a single page lies in (110, 120)-(910, 1280) once upright.
    for name in ("a_upright", "b_sideways", "c_upside_down"):
        left, top_, right, bottom_ = found[name][0]["steps"]["content_box"]["value"]
        assert abs(left - 110) <= 4 and abs(top_ - 120) <= 4
        assert 780 <= right <= 914 and 1200 <= bottom_ <= 1284
    for page in found["d_spread"]:
        assert page["steps"]["content_box"]["value"] is not None
    # Only the blank page is flagged, and it is kept as an image of the paper.
    flagged = sorted(name for name in NAMES for page in found[name] if page["flags"])
    assert flagged == ["f_blank"]
    (blank,) = found["f_blank"]
    assert blank["steps"]["content_box"]["value"] is None
    with Image.open(prepared["out"] / blank["output"]["name"]) as image:
        assert image.size == (1000, 1400) and set(image.tobytes()) == {PAPER}


def test_outputs_are_lossless_tiff_in_the_source_mode_at_the_source_resolution(prepared):
    found = _by_source(prepared["out"])
    for name in NAMES:
        for page in found[name]:
            assert page["output"]["name"].endswith(".tif")
            with Image.open(prepared["out"] / page["output"]["name"]) as image:
                assert image.info["compression"] == "tiff_adobe_deflate"
                assert image.info["dpi"] == pytest.approx(DPI, abs=0.05)
                assert image.mode == ("RGB" if name == "a_upright" else "L")
            geometry = page["geometry"]["steps"]
            assert geometry[4]["factor"] == [1.0, 1.0]  # never shrunk by default


def test_the_same_input_gives_byte_identical_outputs_manifest_and_review(prepared):
    first = _snapshot(prepared["out"])
    assert REVIEW_NAME in first and MANIFEST_NAME in first
    again = _snapshot(prepared["again"])
    # The review sheet's correction command names its own output folder; otherwise a
    # second folder holds the same bytes.
    review = _own_folder(first.pop(REVIEW_NAME), prepared["out"])
    assert _own_folder(again.pop(REVIEW_NAME), prepared["again"]) == review
    assert first == again
    first = _snapshot(prepared["again"])
    # A re-run on the project it wrote keeps every value and gives the same bytes.
    again = ["prepare", str(prepared["src"]), "--output", str(prepared["again"])]
    assert main([*again, "--crop", "content"]) == 1
    assert _snapshot(prepared["again"]) == first


def _own_folder(sheet: bytes, out: Path) -> bytes:
    """A review sheet with the names of its own output folder and that folder's stage
    cache (the only things a second folder changes) made the same."""
    sheet = sheet.replace(bytes(out.parent / f"{out.name}.pagekit-cache"), b"CACHE")
    sheet = sheet.replace(f"../{out.name}.pagekit-cache/".encode(), b"CACHE/")
    return sheet.replace(bytes(out), b"OUT")


def _articles(text: str) -> list[tuple[str, int]]:
    found = []
    for name, badge in re.findall(
        r"<h2>([^<]+?) <span class=\"badge [^\"]+\">([^<]+)</span>", text
    ):
        found.append((name, 0 if badge == "no flags" else int(badge.split()[0])))
    return found


def test_the_review_sheet_is_one_offline_file_flagged_first_with_small_previews(prepared):
    text = (prepared["out"] / REVIEW_NAME).read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>")
    lowered = text.lower()
    for outside in ("http:", "https:", "<script", "<link", "@import", "url(", 'src="//'):
        assert outside not in lowered
    articles = _articles(text)
    assert [name for name, _ in articles][0] == "../src/f_blank.png"
    assert sorted(name for name, _ in articles) == sorted(f"../src/{n}.png" for n in NAMES)
    counts = [count for _, count in articles]
    assert counts == sorted(counts, reverse=True) and counts[0] > 0 and counts[-1] == 0
    previews = re.findall(r'src="data:image/jpeg;base64,([^"]+)"', text)
    assert len(previews) == len(NAMES) + 7  # one per source, one per prepared page
    for data in previews:
        with Image.open(io.BytesIO(base64.b64decode(data))) as preview:
            assert max(preview.size) <= 320
    assert "not yet been measured" in text and "no flag is not proof" in text
    assert "too little ink" in text  # the blank page's flags, in plain words


def _override_lines(text: str) -> dict[tuple[str, str], dict]:
    """{(step, page): override} for every ready-to-copy line, per source sha256."""
    lines = {}
    for step, page, body in re.findall(
        r'<pre class="override" data-step="([^"]+)" data-page="([^"]*)">([^<]+)</pre>', text
    ):
        entry = json.loads(html.unescape(body))
        lines[(entry["source"], step, page)] = entry
    return lines


def _values(project: dict) -> dict:
    found = {}
    for source in project["sources"]:
        for step, entry in source["steps"].items():
            found[(source["path"], None, step)] = (entry["value"], entry["origin"])
        for page in source["pages"]:
            for step, entry in page["steps"].items():
                found[(source["path"], page["page"], step)] = (entry["value"], entry["origin"])
    return found


def _command(text: str) -> str:
    (command,) = re.findall(r'<pre class="command">([^<]+)</pre>', text)
    return html.unescape(command)


def test_an_override_line_pasted_into_an_overrides_file_changes_exactly_that_step(
    prepared, tmp_path
):
    import os
    import subprocess

    # A folder beside the first output, prepared from the same sources.
    target = prepared["root"] / "corrected"
    assert (
        main(["prepare", str(prepared["src"]), "--output", str(target), "--crop", "content"]) == 1
    )
    project = json.loads((target / PROJECT_NAME).read_text())
    before = _values(project)
    text = (target / REVIEW_NAME).read_text(encoding="utf-8")
    sha = next(s["sha256"] for s in project["sources"] if s["path"].endswith("a_upright.png"))
    line = _override_lines(text)[(sha, "content_box", "1")]
    assert line == {"source": sha, "step": "content_box", "page": 1, "value": line["value"]}
    line["value"] = [100, 100, 700, 900]
    (target / "overrides.json").write_text(
        '{"schema": "pagekit-overrides.v1", "overrides": [\n' + json.dumps(line) + "\n]}"
    )
    # The command exactly as the sheet prints it, run by a shell in another folder, with
    # no PYTHONPATH and with PYTHONSAFEPATH set (as CI and some shells do): pagekit is
    # not installed, and the current folder is never on the import path.
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONSAFEPATH"] = "1"
    command = _command(text)
    assert command.startswith("PYTHONPATH=") and "\n" not in command  # one line to copy
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = subprocess.run(
        command, shell=True, cwd=elsewhere, env=environment, capture_output=True, text=True
    )
    assert result.returncode == 1, result.stderr
    after = _values(json.loads((target / PROJECT_NAME).read_text()))
    key = ("../src/a_upright.png", 1, "content_box")
    assert after.pop(key) == ([100, 100, 700, 900], "manual")
    before.pop(key)
    assert after == before


def _plain_sources(folder: Path, count: int) -> Path:
    folder.mkdir(parents=True)
    for index in range(count):
        Image.new("L", (240, 320), 220).save(folder / f"p{index:02d}.png", dpi=(300, 300))
    return folder


def test_a_detector_error_flags_that_page_and_the_batch_finishes(tmp_path):
    source = _plain_sources(tmp_path / "src", 3)

    def skew(context):
        if context.source.path.name == "p01.png":
            raise RuntimeError("a deliberate failure")
        return Answer(0.5, 0.9, "Test skew.", ())

    prepared = plan([source], tmp_path / "out", detectors={"skew": Detector("test.skew/1", skew)})
    manifest = execute(prepared)
    assert len(manifest["pages"]) == 3
    good, bad, other = manifest["pages"]
    assert good["steps"]["skew"]["value"] == 0.5 and other["steps"]["skew"]["value"] == 0.5
    assert bad["steps"]["skew"]["value"] == 0.0 and bad["steps"]["skew"]["confidence"] == 0.0
    (reason,) = [flag["reason"] for flag in bad["flags"] if flag["step"] == "skew"]
    assert "skew step could not be measured" in reason and "a deliberate failure" in reason
    assert bad["verdict"] == "review"
    assert (tmp_path / "out" / bad["output"]["name"]).is_file()


def test_a_manual_value_is_not_re_detected_and_a_large_disagreement_is_reported(tmp_path):
    folder = tmp_path / "src"
    folder.mkdir()
    boxes.turned(pages.page(seed=5), 2.0).save(folder / "tilted.png", dpi=DPI)
    calls = []

    def counted(context):
        calls.append(context.step)
        return DETECTORS["skew"].run(context)

    detectors = dict(DETECTORS)
    detectors["skew"] = Detector(DETECTORS["skew"].method, counted, (), DETECTORS["skew"].compare)
    overrides = tmp_path / "fix.json"

    def run(value: float) -> dict:
        overrides.write_text(
            json.dumps(
                {
                    "schema": "pagekit-overrides.v1",
                    "overrides": [
                        {"source": "src/tilted.png", "step": "skew", "page": 1, "value": value}
                    ],
                }
            )
        )
        prepared = plan([folder], tmp_path / "out", overrides_path=overrides, detectors=detectors)
        execute(prepared)
        return json.loads((tmp_path / "out" / MANIFEST_NAME).read_text())["pages"][0]

    far = run(0.0)
    skew = far["steps"]["skew"]
    assert (skew["value"], skew["origin"], skew["flags"]) == (0.0, "manual", [])
    assert "run only to compare" in skew["evidence"] and "+2.0" in skew["evidence"]
    assert calls == ["skew"]  # run to compare, never stored
    project = json.loads((tmp_path / "out" / PROJECT_NAME).read_text())
    stored = project["sources"][0]["pages"][0]["steps"]["skew"]
    assert stored["evidence"] == "Set by hand in fix.json." and stored["origin"] == "manual"
    # The detected value is the levelled page's, so the boxes follow the hand-set value.
    near = run(2.2)
    assert near["steps"]["skew"]["evidence"] == "Set by hand in fix.json."
    assert near["steps"]["skew"]["value"] == 2.2


def _batch_with_skews(tmp_path: Path, skews: list[float]):
    tmp_path = tmp_path / f"run{len(list(tmp_path.iterdir()))}"
    source = _plain_sources(tmp_path / "src", len(skews))
    by_name = {f"p{index:02d}.png": angle for index, angle in enumerate(skews)}

    def skew(context):
        return Answer(by_name[context.source.path.name], 0.9, "Test skew.", ())

    prepared = plan([source], tmp_path / "out", detectors={"skew": Detector("t/1", skew)})
    return [
        [flag["reason"] for flag in page.flags if flag["step"] == "batch"]
        for page in prepared.pages
    ], prepared.batch


def test_a_page_far_from_a_large_batch_is_flagged_and_a_small_batch_is_not_compared(tmp_path):
    skews = [0.4, 0.5, 0.45, 0.55, 0.5, 0.6, 0.4, 0.5, 3.5]
    flags, batch = _batch_with_skews(tmp_path, skews)
    assert all(not reasons for reasons in flags[:-1])
    (reason,) = flags[-1]
    assert "skew (+3.50 degrees)" in reason and "far from the rest" in reason
    assert batch["measurements"]["skew"]["compared"] is True
    assert batch["measurements"]["skew"]["flagged"] == 1
    # Small differences in an even batch stay under the floor.
    flags, _ = _batch_with_skews(tmp_path, [0.5] * 8 + [0.9])
    assert not any(flags)
    # Too few pages: nothing is compared, however far off one is.
    flags, batch = _batch_with_skews(tmp_path, [0.5, 0.5, 0.5, 0.5, 6.0])
    assert not any(flags)
    assert batch["measurements"]["skew"] == {"pages": 5, "compared": False}


def test_a_content_box_far_from_the_batch_is_flagged_by_its_size(tmp_path):
    source = _plain_sources(tmp_path / "src", 9)

    def content(context):
        small = context.source.path.name == "p08.png"
        return Answer([100, 100, 110, 110] if small else [20, 20, 220, 300], 0.9, "Test.", ())

    prepared = plan(
        [source],
        tmp_path / "out",
        detectors={"content_box": Detector("c/1", content)},
        settings_overrides={"crop": "content"},
    )
    reasons = [flag["reason"] for flag in prepared.pages[-1].flags if flag["step"] == "batch"]
    assert any("content width" in reason for reason in reasons)
    assert any("content height" in reason for reason in reasons)
    assert not any(flag["step"] == "batch" for page in prepared.pages[:-1] for flag in page.flags)


def _gold(prepared_dir: Path, wrong_sideways: bool) -> dict:
    found = _by_source(prepared_dir)
    sources = []
    for name in NAMES:
        first = found[name][0]
        entry = {
            "source": f"{name}.png",
            "orientation": first["steps"]["orientation"]["value"],
            "pages": first["steps"]["split"]["value"]["pages"],
            "skew": [2.0 if name == "e_tilted" else 0.0 for _ in found[name]],
        }
        if name == "d_spread":
            entry["cut"] = [[1000, 0], [1000, 1400]]
        if name in ("a_upright", "b_sideways", "c_upside_down"):
            box = first["steps"]["content_box"]["value"]
            entry["content_box"] = [box]
        if name == "f_blank":
            entry["content_box"] = [None]
        sources.append(entry)
    if wrong_sideways:
        sources[1]["orientation"] = 1
    return {"schema": "pagekit-gold.v1", "sources": sources}


def test_measure_reports_right_wrong_and_review_counts_against_a_gold_file(
    prepared, tmp_path, capsys
):
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps(_gold(prepared["out"], wrong_sideways=True)))
    report = measure(prepared["out"], gold)
    steps = report["steps"]
    assert {key: steps["orientation"][key] for key in ("right", "wrong", "review")} == {
        "right": 4,
        "wrong": 1,
        "review": 1,
    }
    assert (steps["pages"]["right"], steps["pages"]["review"]) == (5, 1)
    assert (steps["cut"]["right"], steps["cut"]["wrong"]) == (1, 0)
    assert steps["cut"]["error_largest"] < 1
    assert steps["skew"]["right"] == 6 and steps["skew"]["review"] == 1
    assert steps["skew"]["error_largest"] <= 0.3
    assert (steps["content_box"]["right"], steps["content_box"]["wrong"]) == (4, 0)
    assert report["wrong_without_flag"] == ["b_sideways.png: orientation"]
    assert main(["measure", "--prepared", str(prepared["out"]), "--gold", str(gold)]) == 0
    printed = capsys.readouterr().out
    assert "orientation: 4 right, 1 wrong, 1 sent to review" in printed
    assert "b_sideways.png: orientation" in printed
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "pagekit-gold.v1", "sources": [{"source": "x.png"}]}))
    assert main(["measure", "--prepared", str(prepared["out"]), "--gold", str(bad)]) == 2
    assert "is not in the prepared batch" in capsys.readouterr().err


def _written_sources(folder: Path) -> Path:
    """Three small pages with writing: grey, colour, and grey with no resolution."""
    folder.mkdir(parents=True)
    grey = pages.page(size=(400, 560), seed=7, margin=(40, 50, 40, 50))
    grey.save(folder / "g.png", dpi=DPI)
    colour = Image.merge("RGB", (grey, grey, grey.point(lambda level: round(level * 0.85))))
    colour.save(folder / "c.png", dpi=DPI)
    pages.page(size=(400, 560), seed=8, margin=(40, 50, 40, 50)).save(folder / "n.png")
    return folder


def test_prepare_tone_view_writes_one_deterministic_view_beside_each_page(tmp_path, monkeypatch):
    from pagekit.tone import tiff_bytes, tone

    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})
    source = _written_sources(tmp_path / "src")
    first, second = tmp_path / "out", tmp_path / "again"
    for out in (first, second):
        assert main(["prepare", str(source), "--output", str(out), "--tone-view"]) == 1
    manifest = json.loads((first / MANIFEST_NAME).read_text())
    assert len(manifest["pages"]) == 3
    for page in manifest["pages"]:
        view = page["tone_view"]
        name = page["output"]["name"]
        assert view["name"] == name.replace(".tif", "_tone.tif")
        assert view["name"] != name and (first / view["name"]).is_file()
        record = view["record"]
        assert record["schema"] == "pagekit-tone-view.v1"
        assert record["settings"] and len(record["settings_sha256"]) == 64
        # The bytes are tone.py's own writer's, from tone.py's own view of the page.
        with Image.open(first / name) as prepared:
            prepared.load()
            image = prepared.copy()
        if page["output"]["resolution"] is None:
            image.info.pop("dpi", None)
        else:
            image.info["dpi"] = tuple(page["output"]["resolution"])
        expected, _ = tone(image)
        data = (first / view["name"]).read_bytes()
        assert data == tiff_bytes(expected, page["output"]["resolution"])
        assert view["sha256"] == hashlib.sha256(data).hexdigest()
    names = sorted(path.name for path in first.iterdir())
    assert names == sorted(path.name for path in second.iterdir())
    for name in names:  # identical bytes on repeat, views included
        one, two = (first / name).read_bytes(), (second / name).read_bytes()
        if name == REVIEW_NAME:  # its correction command names its own folder
            one, two = _own_folder(one, first), _own_folder(two, second)
        assert one == two, name


def test_a_tone_view_never_takes_the_name_of_a_prepared_page_or_a_source(tmp_path, monkeypatch):
    from pagekit.project import PrepareError

    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})
    source = _plain_sources(tmp_path / "src", 2)
    prepared = plan([source], tmp_path / "out", tone_view=True)
    prepared.pages[1].output_name = "p00_p1_tone.tif"  # where page 1's view would go
    with pytest.raises(PrepareError, match="tone view"):
        execute(prepared)
    assert not (tmp_path / "out").exists()


def test_tone_view_is_refused_where_tone_py_is_missing(tmp_path, monkeypatch, capsys):
    source = _plain_sources(tmp_path / "src", 1)
    out = tmp_path / "out"
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})
    monkeypatch.setattr("pagekit.pipeline.tone_view_available", lambda: False)
    assert main(["prepare", str(source), "--output", str(out), "--tone-view"]) == 2
    assert "tone view is not built" in capsys.readouterr().err
    assert not out.exists()


def test_png_and_shrinking_are_available_by_setting(tmp_path, monkeypatch):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})
    source = _plain_sources(tmp_path / "src", 1)
    out = tmp_path / "out"
    assert main(["prepare", str(source), "--output", str(out), "--format", "png"]) == 1
    assert (out / "p00_p1.png").is_file()
    shrunk = tmp_path / "shrunk"
    assert main(["prepare", str(source), "--output", str(shrunk), "--max-dpi", "150"]) == 1
    with Image.open(shrunk / "p00_p1.tif") as image:
        assert image.info["dpi"] == pytest.approx((150, 150), abs=0.05)
        assert image.size[0] < 240 and image.size[1] < 320  # shrunk, from a larger crop


def test_every_thresholds_file_ships_with_the_package():
    import tomllib

    here = Path(__file__).parent
    with (here / "pyproject.toml").open("rb") as handle:
        shipped = set(tomllib.load(handle)["tool"]["setuptools"]["package-data"]["pagekit"])
    assert {path.name for path in here.glob("thresholds*.toml")} <= shipped


def _odd_page(mode: str) -> Image.Image:
    """A noisy page whose deflate strips (256 rows each) end on an odd byte in all."""
    import random
    import zlib

    for seed in range(200):
        rng = random.Random(seed)
        width, height = 97 + seed, 300
        bands = len(mode)
        raw = bytes(rng.randrange(120, 136) for _ in range(width * height * bands))
        total = sum(
            len(zlib.compress(raw[row * width * bands : (row + 256) * width * bands], 6))
            for row in range(0, height, 256)
        )
        if total & 1:
            return Image.frombytes(mode, (width, height), raw)
    raise AssertionError("no page gave an odd strip")


_WRITE_IN_A_PROCESS = """
import hashlib, sys
from PIL import Image
from pagekit.output import encode
mode, width, height, path = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
image = Image.frombytes(mode, (width, height), open(path, "rb").read())
dpi = None if sys.argv[5] == "none" else (300.0, 300.0)
if sys.argv[6] == "tone":  # the tone view's own writer
    from pagekit.tone import tiff_bytes
    sys.stdout.write(tiff_bytes(image, None if dpi is None else list(dpi)).hex())
else:
    sys.stdout.write(encode(image, "tiff", dpi).hex())
"""


@pytest.mark.parametrize(("mode", "writer"), [("L", "prepare"), ("RGB", "prepare"), ("L", "tone")])
@pytest.mark.parametrize("dpi", ["300", "none"])
def test_a_tiff_with_an_odd_strip_is_identical_from_two_processes(tmp_path, mode, writer, dpi):
    import os
    import subprocess

    from pagekit.output import encode
    from pagekit.tone import tiff_bytes

    image = _odd_page(mode)
    raw = tmp_path / "pixels.raw"
    raw.write_bytes(image.tobytes())
    root = Path(__file__).resolve().parent.parent
    # The child imports pagekit through PYTHONPATH, whatever PYTHONSAFEPATH says.
    environment = {**os.environ, "PYTHONPATH": str(root)}
    written = []
    for _ in range(2):  # two separate processes
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _WRITE_IN_A_PROCESS,
                mode,
                *map(str, image.size),
                raw,
                dpi,
                writer,
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        written.append(bytes.fromhex(result.stdout))
    assert written[0] == written[1]
    data = written[0]
    # The directory is word-aligned after the odd strip, and the pad byte is zero.
    directory = int.from_bytes(data[4:8], "little")
    assert directory % 2 == 0 and data[directory - 1] == 0
    with Image.open(io.BytesIO(data)) as page:
        assert page.mode == mode and page.tobytes() == image.tobytes()
        assert page.info["compression"] == "tiff_adobe_deflate"
        if dpi == "none":
            assert "dpi" not in page.info
        else:
            assert page.info["dpi"] == (300.0, 300.0)
    expected_dpi = None if dpi == "none" else (300.0, 300.0)
    assert data == encode(image, "tiff", expected_dpi)
    if mode == "L":  # the same writer as the tone view's
        assert data == tiff_bytes(image, None if expected_dpi is None else list(expected_dpi))


def test_each_manifest_entry_carries_the_sha256_of_its_decoded_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})
    source = _written_sources(tmp_path / "src")
    out = tmp_path / "out"
    for fmt in ("tiff", "png"):
        assert main(["prepare", str(source), "--output", str(out), "--format", fmt]) == 1
        for page in json.loads((out / MANIFEST_NAME).read_text())["pages"]:
            with Image.open(out / page["output"]["name"]) as image:
                pixels = hashlib.sha256(image.tobytes()).hexdigest()
                assert page["output"]["pixels_sha256"] == pixels
                assert image.mode == page["output"]["mode"]
                if page["source"]["name"] == "n.png":  # no resolution: none written
                    assert "dpi" not in image.info


def test_dpi_gives_a_resolution_to_sources_that_carry_none(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})
    folder = tmp_path / "src"
    folder.mkdir()
    Image.new("L", (600, 800), 220).save(folder / "none.png")
    Image.new("L", (600, 800), 220).save(folder / "own.png", dpi=(200, 200))
    out = tmp_path / "out"

    def resolution_of(name: str) -> tuple[dict, list[str], dict]:
        manifest = json.loads((out / MANIFEST_NAME).read_text())
        (page,) = [p for p in manifest["pages"] if p["source"]["name"] == name]
        flags = [flag["reason"] for flag in page["flags"] if flag["step"] == "resolution"]
        return page["source_resolution"], flags, page

    # Without it the flag says how to give one.
    assert main(["prepare", str(folder), "--output", str(out)]) == 1
    stored, flags, page = resolution_of("none.png")
    assert stored["origin"] == "missing"
    (reason,) = flags
    assert "--dpi" in reason and "resolution" in reason
    assert page["geometry"]["steps"][3]["size_after"] == [600, 800]  # margins were 0 px

    assert main(["prepare", str(folder), "--output", str(out), "--dpi", "300"]) == 1
    stored, flags, page = resolution_of("none.png")
    assert stored == {"value": [300.0, 300.0], "origin": "override", "file_value": None}
    assert flags == []
    assert page["output"]["resolution"] == [300.0, 300.0]
    with Image.open(out / page["output"]["name"]) as image:
        assert image.info["dpi"] == (300.0, 300.0)
    # The file's own resolution is kept; --dpi is only for sources with none.
    stored, _, _ = resolution_of("own.png")
    assert stored == {"value": [200.0, 200.0], "origin": "file", "file_value": [200.0, 200.0]}
    # It is kept in the project like any override, so a later run keeps it.
    assert main(["prepare", str(folder), "--output", str(out)]) == 1
    assert resolution_of("none.png")[0]["origin"] == "override"
    # A value that cannot be a scan resolution is refused, and nothing changes.
    before = {path.name: path.read_bytes() for path in out.iterdir()}
    assert main(["prepare", str(folder), "--output", str(out), "--dpi", "5"]) == 2
    assert "--dpi" in capsys.readouterr().err
    assert {path.name: path.read_bytes() for path in out.iterdir()} == before


def _stale_after(monkeypatch, out: Path, source: Path, changed: str) -> dict:
    """{(step, page): why} for a dry run in which file `changed` reads as edited."""
    import pagekit.prepare as prepare_module

    real = prepare_module.file_digest

    def edited(name: str) -> str:
        return "0" * 64 if name == changed else real(name)

    monkeypatch.setattr(prepare_module, "file_digest", edited)
    stale = plan([source], out, detectors=DETECTORS, dry=True, settings_overrides=CROP).stale
    monkeypatch.setattr(prepare_module, "file_digest", real)
    return {(item["step"], item["page"]): item["why"] for item in stale}


def test_a_changed_settings_file_or_detector_code_recomputes_exactly_what_reads_it(
    tmp_path, monkeypatch
):
    folder = tmp_path / "src"
    folder.mkdir()
    pages.page(size=(400, 560), seed=9, margin=(40, 50, 40, 50)).save(folder / "p.png", dpi=DPI)
    out = tmp_path / "out"
    execute(plan([folder], out, detectors=DETECTORS, settings_overrides=CROP))
    record = json.loads((out / PROJECT_NAME).read_text())["sources"][0]
    inputs = record["pages"][0]["steps"]["content_box"]["inputs"]["settings"]
    here = Path(__file__).parent
    for name in ("thresholds.toml", "thresholds_skew.toml", "content.py", "_box_common.py"):
        digest = hashlib.sha256((here / name).read_bytes()).hexdigest()
        assert inputs[f"file {name}"] == digest
    assert plan([folder], out, detectors=DETECTORS, dry=True, settings_overrides=CROP).stale == []

    # The crop check's thresholds feed only the content box.
    stale = _stale_after(monkeypatch, out, folder, "thresholds.toml")
    assert "thresholds.toml" in stale[("content_box", 1)]
    assert not any(step in ("orientation", "split", "skew", "page_box") for step, _ in stale)
    # Code: the content box's own module, then the skew detector's and what follows.
    stale = _stale_after(monkeypatch, out, folder, "content.py")
    assert "content.py" in stale[("content_box", 1)]
    assert ("page_box", 1) not in stale and ("skew", 1) not in stale
    stale = _stale_after(monkeypatch, out, folder, "skew.py")
    assert "skew.py" in stale[("skew", 1)]
    assert ("orientation", None) not in stale and ("split", None) not in stale
    stale = _stale_after(monkeypatch, out, folder, "thresholds_split.toml")
    assert ("orientation", None) in stale and ("split", None) in stale


def test_measure_scores_a_right_content_box_on_a_tilted_page_and_a_spreads_page_two(
    prepared, tmp_path
):
    # The hand-checked boxes: the writing once the page is turned by its true skew about
    # the image's centre, keeping its size, which for the tilted page is the level page
    # it was drawn from; for the spread, the right page's writing (past the fold).
    level = pages.page(seed=5)
    tilted_box = list(level.point(lambda v: 255 if v < 128 else 0).getbbox())
    spread = pages.spread(seed=4)
    right = spread.crop((1010, 0, 2000, 1400)).point(lambda v: 255 if v < 128 else 0)
    x0, y0, x1, y1 = right.getbbox()
    gold = {
        "schema": "pagekit-gold.v1",
        "sources": [
            {"source": "e_tilted.png", "skew": [2.0], "content_box": [tilted_box]},
            {
                "source": "d_spread.png",
                "content_box": [None, [x0 + 1010, y0, x1 + 1010, y1]],
                "skew": [0.0, 0.0],
            },
        ],
    }
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(gold))
    report = measure(prepared["out"], path)
    boxes_step = report["steps"]["content_box"]
    # Page one of the spread is not blank, so it is wrong; the other two are right.
    assert (boxes_step["right"], boxes_step["wrong"]) == (2, 1)
    assert report["wrong_without_flag"] == ["d_spread.png page 1: content box"]
    errors = report["errors"]["content_box"]
    assert errors["e_tilted.png page 1"] < 1.0  # mm
    assert errors["d_spread.png page 2"] < 1.0


def _fake_batch(tmp_path: Path, image: Image.Image, dpi, detectors) -> Path:
    folder = tmp_path / "src"
    folder.mkdir(parents=True)
    image.save(folder / "s.png", dpi=dpi)
    out = tmp_path / "out"
    execute(plan([folder], out, detectors=detectors))
    return out


def _gold_file(tmp_path: Path, entry: dict) -> Path:
    path = tmp_path / "gold.json"
    path.write_text(json.dumps({"schema": "pagekit-gold.v1", "sources": [entry]}))
    return path


def _given(step: str, value):
    return Detector(f"test.{step}/1", lambda context: Answer(value, 0.9, "Given.", ()))


def test_measure_wraps_orientation_errors_and_measures_the_cut_across_the_page(tmp_path):
    # Stored sideways (1400 x 2000) at 300 x 150 dpi: upright it is 2000 x 1400 at
    # 150 dpi across, so a cut 15 px off is 2.54 mm off.
    image = Image.new("L", (1400, 2000), 220)
    cut = [[1000.0, 0.0], [1000.0, 1399.0]]
    out = _fake_batch(
        tmp_path,
        image,
        (300, 150),
        {
            "orientation": _given("orientation", 1),
            "split": _given("split", {"pages": 2, "cut": cut}),
        },
    )
    gold = _gold_file(
        tmp_path, {"source": "s.png", "orientation": 1, "cut": [[1015, 0], [1015, 1400]]}
    )
    steps = measure(out, gold)["steps"]
    assert steps["orientation"]["right"] == 1
    assert steps["cut"]["error_largest"] == pytest.approx(15 / 150 * 25.4, abs=0.01)
    assert steps["cut"]["wrong"] == 1  # 2.54 mm is past the 2 mm tolerance
    # A leaning true cut: the error is the larger gap, at the bottom.
    gold = _gold_file(tmp_path, {"source": "s.png", "cut": [[1000, 0], [1006, 1399]]})
    steps = measure(out, gold)["steps"]
    assert steps["cut"]["error_largest"] == pytest.approx(6 / 150 * 25.4, abs=0.01)
    assert steps["cut"]["right"] == 1
    # Three quarter turns from one is two quarter turns off; from zero it is one.
    gold = _gold_file(tmp_path, {"source": "s.png", "orientation": 3})
    assert measure(out, gold)["steps"]["orientation"]["error_largest"] == 2
    zero = _fake_batch(tmp_path / "zero", Image.new("L", (300, 400), 220), (150, 150), {})
    gold = _gold_file(tmp_path / "zero", {"source": "s.png", "orientation": 3})
    assert measure(zero, gold)["steps"]["orientation"]["error_largest"] == 1


BAD_FILES = {
    "broken.png": "not an image",
    "cut.jpg": "cut short",
    "deep.png": "16-bit",
    "cmyk.tif": "CMYK",
}


def _mixed_folder(folder: Path, good: bool = True) -> Path:
    """Two good pages (if `good`), a text file named .png, a truncated JPEG, a 16-bit
    grey page and a CMYK page."""
    folder.mkdir(parents=True)
    if good:
        for index in (1, 2):
            page = pages.page(size=(300, 400), seed=20 + index, margin=(30, 40, 30, 40))
            page.save(folder / f"good{index}.png", dpi=DPI)
    (folder / "broken.png").write_bytes(b"this is not an image")
    buffer = io.BytesIO()
    pages.page(size=(300, 400), seed=30).convert("RGB").save(buffer, "JPEG", quality=90)
    (folder / "cut.jpg").write_bytes(buffer.getvalue()[: len(buffer.getvalue()) // 2])
    Image.new("I;16", (200, 300), 4000).save(folder / "deep.png")
    Image.new("CMYK", (200, 300), (0, 10, 30, 5)).save(folder / "cmyk.tif", dpi=DPI)
    return folder


def test_unusable_sources_are_skipped_named_everywhere_and_the_rest_prepared(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})
    folder = _mixed_folder(tmp_path / "src")
    out = tmp_path / "out"
    assert main(["prepare", str(folder), "--output", str(out)]) == 1
    printed = capsys.readouterr().out
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    assert [page["output"]["name"] for page in manifest["pages"]] == [
        "good1_p1.tif",
        "good2_p1.tif",
    ]
    skipped = {entry["name"]: entry for entry in manifest["skipped"]}
    assert sorted(skipped) == sorted(BAD_FILES)
    review = (out / REVIEW_NAME).read_text(encoding="utf-8")
    top = review[: review.index("<h2>Sources</h2>")]
    for name, word in BAD_FILES.items():
        reason = skipped[name]["reason"]
        assert word in reason and "0x" not in reason and "BytesIO" not in reason
        assert skipped[name]["path"] == f"../src/{name}"
        assert html.escape(reason) in top and name in top  # before the list of sources
        assert f"{name}: {reason}" in printed
    project = json.loads((out / PROJECT_NAME).read_text())
    assert [source["path"] for source in project["sources"]] == [
        "../src/good1.png",
        "../src/good2.png",
    ]
    assert not any(name.startswith(("broken", "cut", "deep", "cmyk")) for name in os.listdir(out))


def test_a_folder_of_only_unusable_sources_is_exit_2_and_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})
    folder = _mixed_folder(tmp_path / "src", good=False)
    out = tmp_path / "out"
    assert main(["prepare", str(folder), "--output", str(out)]) == 2
    error = capsys.readouterr().err
    assert error.count("pagekit:") == 1 and "no source image can be used" in error
    for name in BAD_FILES:
        assert name in error
    assert not out.exists()


def test_a_skipped_source_is_tried_again_and_the_others_keep_their_values(tmp_path, monkeypatch):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})
    folder = _mixed_folder(tmp_path / "src")
    out = tmp_path / "out"
    fix = tmp_path / "fix.json"
    entry = {"source": "src/good1.png", "step": "skew", "page": 1, "value": 1.5}
    fix.write_text(json.dumps({"schema": "pagekit-overrides.v1", "overrides": [entry]}))
    assert main(["prepare", str(folder), "--output", str(out), "--overrides", str(fix)]) == 1
    before = {s["path"]: s for s in json.loads((out / PROJECT_NAME).read_text())["sources"]}
    pages.page(size=(300, 400), seed=40).save(folder / "broken.png", dpi=DPI)  # replaced
    assert main(["prepare", str(folder), "--output", str(out)]) == 1  # three still skipped
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    assert sorted(entry["name"] for entry in manifest["skipped"]) == [
        "cmyk.tif",
        "cut.jpg",
        "deep.png",
    ]
    assert "broken_p1.tif" in [page["output"]["name"] for page in manifest["pages"]]
    after = {s["path"]: s for s in json.loads((out / PROJECT_NAME).read_text())["sources"]}
    assert sorted(after) == ["../src/broken.png", "../src/good1.png", "../src/good2.png"]
    for path in ("../src/good1.png", "../src/good2.png"):
        assert after[path] == before[path]  # values, origins and inputs all kept
    skew = after["../src/good1.png"]["pages"][0]["steps"]["skew"]
    assert (skew["value"], skew["origin"]) == (1.5, "manual")


def test_the_manifest_stays_closed_with_its_documented_skipped_field(tmp_path, monkeypatch):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})
    folder = _mixed_folder(tmp_path / "src")
    out = tmp_path / "out"
    assert main(["prepare", str(folder), "--output", str(out)]) == 1
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    assert set(manifest) == {
        "schema",
        "tool",
        "pages",
        "skipped",
        "stale_outputs",
        "batch",
        "review",
        "thresholds",
        "thresholds_measured",
        "thresholds_note",
    }
    for entry in manifest["skipped"]:
        assert set(entry) == {"name", "path", "sha256", "reason"}
    readme = (Path(__file__).parent / "README.md").read_text(encoding="utf-8")
    assert "`skipped`" in readme


def test_a_grey_palette_source_gives_a_grey_page_and_a_colour_one_a_colour_page(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})
    folder = tmp_path / "src"
    folder.mkdir()
    grey = pages.page(size=(300, 400), seed=11, margin=(30, 40, 30, 40))
    grey.convert("P").save(folder / "grey_palette.png", dpi=DPI)  # r = g = b throughout
    colour = Image.merge("RGB", (grey, grey, grey.point(lambda v: round(v * 0.8))))
    colour.convert("P", palette=Image.Palette.ADAPTIVE).save(folder / "colour_palette.png", dpi=DPI)
    with Image.open(folder / "grey_palette.png") as check:
        assert check.mode == "P"
    out = tmp_path / "out"
    assert main(["prepare", str(folder), "--output", str(out)]) == 1
    with Image.open(out / "grey_palette_p1.tif") as page:
        assert page.mode == "L"
    with Image.open(out / "colour_palette_p1.tif") as page:
        assert page.mode == "RGB"


def test_the_review_sheet_is_light_to_open_and_says_what_lock_and_the_margin_mean(prepared):
    text = (prepared["out"] / REVIEW_NAME).read_text(encoding="utf-8")
    images = re.findall(r"<img [^>]+>", text)
    assert images and all('loading="lazy"' in image for image in images)
    for data in re.findall(r'src="data:image/jpeg;base64,([^"]+)"', text):
        with Image.open(io.BytesIO(base64.b64decode(data))) as preview:
            assert max(preview.size) <= 320
    # A short table at the top links to every source, flagged first.
    table = text[: text.index("<article")]
    links = re.findall(r'<a href="#(source-\d+)">([^<]+)</a>', table)
    assert [name for _, name in links] == [name for name, _ in _articles(text)]
    for anchor, _ in links:
        assert f'id="{anchor}"' in text
    # About 500 sources must stay well under 30 MB: under 36 KB a source on these
    # dense synthetic pages (18 MB for 500); it was 74 KB.
    assert len(text.encode("utf-8")) / len(NAMES) < 36_000
    # Hand-set values are kept on every run; lock only stops the "inputs changed" flag.
    assert "to keep it on every re-run" not in text
    assert "kept on every run" in text and "lock" in text
    # The margin comes from a setting, not from a detector.
    margins = re.findall(r"<h4>Margin</h4><dl>.*?<dt>From</dt><dd>([^<]+)</dd>", text)
    assert margins and set(margins) == {"the margin_mm setting"}


@pytest.mark.parametrize(
    ("page_box", "content_box", "flagged"),
    [
        ([-50, -40, 2000, 3000], [20, 20, 200, 300], {"page_box": "partly"}),
        (
            [1000, 1000, 1200, 1300],
            [1010, 1010, 1100, 1100],
            {"page_box": "wholly", "content_box": "wholly"},
        ),
        ([0, 0, 240, 320], [200, 280, 260, 340], {"content_box": "partly"}),
        ([0, 0, 240, 320], [20, 20, 200, 300], {}),
    ],
)
def test_a_hand_set_box_outside_the_levelled_page_is_flagged(
    tmp_path, page_box, content_box, flagged
):
    source = _plain_sources(tmp_path / "src", 1)
    overrides = tmp_path / "fix.json"
    entries = [
        {"source": "src/p00.png", "step": "page_box", "page": 1, "value": page_box},
        {"source": "src/p00.png", "step": "content_box", "page": 1, "value": content_box},
    ]
    overrides.write_text(json.dumps({"schema": "pagekit-overrides.v1", "overrides": entries}))
    (page,) = plan([source], tmp_path / "out", overrides_path=overrides).pages
    found = {
        flag["step"]: flag["reason"]
        for flag in page.flags
        if "outside the levelled page" in flag["reason"]
    }
    assert set(found) == set(flagged)
    for step, extent in flagged.items():
        assert f"lies {extent} outside the levelled page (240 by 320 pixels)" in found[step]
