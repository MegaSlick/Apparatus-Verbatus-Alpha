"""The prepare pipeline, the volume-wide checks, the review sheet and `measure`
(spec 0005), on synthetic pages drawn here; no real register material."""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import re
import shutil
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
PAPER = pages.PAPER
NAMES = ("a_upright", "b_sideways", "c_upside_down", "d_spread", "e_tilted", "f_blank")


def _batch(folder: Path) -> Path:
    """The synthetic batch of spec 0005: upright (in colour), sideways, upside down, a
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
    assert main(["prepare", str(source), "--output", str(first)]) == 1  # the blank page
    assert main(["prepare", str(source), "--output", str(second)]) == 1
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
    assert first == _snapshot(prepared["again"])
    # A re-run on the project it wrote keeps every value and gives the same bytes.
    assert main(["prepare", str(prepared["src"]), "--output", str(prepared["again"])]) == 1
    assert _snapshot(prepared["again"]) == first


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
            assert max(preview.size) <= 480
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


def test_an_override_line_pasted_into_an_overrides_file_changes_exactly_that_step(
    prepared, monkeypatch
):
    # A copy beside the output folder, so the project's "../src" paths still hold.
    target = prepared["root"] / "corrected"
    shutil.copytree(prepared["out"], target)
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
    monkeypatch.chdir(target)  # the command the sheet gives, run where it says
    assert main(["prepare", "--output", ".", "--overrides", "overrides.json"]) == 1
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

    prepared = plan([source], tmp_path / "out", detectors={"content_box": Detector("c/1", content)})
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
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


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
    import subprocess

    from pagekit.output import encode
    from pagekit.tone import tiff_bytes

    image = _odd_page(mode)
    raw = tmp_path / "pixels.raw"
    raw.write_bytes(image.tobytes())
    root = Path(__file__).resolve().parent.parent
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
    stale = plan([source], out, detectors=DETECTORS, dry=True).stale
    monkeypatch.setattr(prepare_module, "file_digest", real)
    return {(item["step"], item["page"]): item["why"] for item in stale}


def test_a_changed_settings_file_or_detector_code_recomputes_exactly_what_reads_it(
    tmp_path, monkeypatch
):
    folder = tmp_path / "src"
    folder.mkdir()
    pages.page(size=(400, 560), seed=9, margin=(40, 50, 40, 50)).save(folder / "p.png", dpi=DPI)
    out = tmp_path / "out"
    execute(plan([folder], out, detectors=DETECTORS))
    record = json.loads((out / PROJECT_NAME).read_text())["sources"][0]
    inputs = record["pages"][0]["steps"]["content_box"]["inputs"]["settings"]
    here = Path(__file__).parent
    for name in ("thresholds.toml", "thresholds_skew.toml", "content.py", "_box_common.py"):
        digest = hashlib.sha256((here / name).read_bytes()).hexdigest()
        assert inputs[f"file {name}"] == digest
    assert plan([folder], out, detectors=DETECTORS, dry=True).stale == []

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
