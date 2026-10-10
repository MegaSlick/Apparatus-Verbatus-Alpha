"""`prepare`: project file, overrides, outputs and the command, on synthetic pages drawn
here; no real register material."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pagekit.__main__ import main
from pagekit.answer import Answer
from pagekit.geometry import Chain, render
from pagekit.output import MANIFEST_NAME, execute
from pagekit.prepare import PROJECT_NAME, Detector, plan
from pagekit.project import PrepareError, canonical_json

MARK = (600.0, 750.0)  # in the source; upright (250, 600) after one quarter turn


@pytest.fixture(autouse=True)
def _neutral_detectors(monkeypatch):
    """These tests pin the preparation core with its neutral defaults, through the
    command as well; the connected detectors are tested in test_pipeline.py."""
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})


def _source(
    folder: Path,
    name="spread.png",
    size=(1400, 1000),
    dpi=(300, 300),
    mode="L",
    paper=None,
    draw=None,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if paper is None:
        paper = 222 if mode == "L" else (226, 214, 192)
    image = Image.new(mode, size, paper)
    if draw is not None:
        draw(ImageDraw.Draw(image))
    path = folder / name
    if dpi:
        image.save(path, dpi=dpi)
    else:
        image.save(path)
    return path


def _marked(draw: ImageDraw.ImageDraw) -> None:
    x, y = MARK
    draw.rectangle((x - 6, y - 6, x + 5, y + 5), fill=12)  # pixels 594..605: centre 600


def _overrides(folder: Path, entries: list[dict], name="overrides.json") -> Path:
    path = folder / name
    path.write_text(json.dumps({"schema": "pagekit-overrides.v1", "overrides": entries}))
    return path


def _snapshot(folder: Path) -> dict[str, bytes]:
    if not folder.exists():
        return {}
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(out: Path) -> dict:
    return json.loads((out / MANIFEST_NAME).read_text())


def _project(out: Path) -> dict:
    return json.loads((out / PROJECT_NAME).read_text())


def _mark_centre(image: Image.Image, below: int = 100) -> tuple[float, float]:
    grey = image.convert("L")
    width = grey.size[0]
    total = sx = sy = 0
    for index, level in enumerate(grey.tobytes()):
        if level < below:
            weight = below - level
            total += weight
            sx += weight * (index % width + 0.5)
            sy += weight * (index // width + 0.5)
    return sx / total, sy / total


HAND_VALUES = [
    {"source": "src/spread.png", "step": "orientation", "value": 1},
    {
        "source": "src/spread.png",
        "step": "split",
        "value": {"pages": 2, "cut": [[500, 0], [520, 1400]]},
    },
    {"source": "src/spread.png", "step": "skew", "page": 1, "value": 2.0},
    {"source": "src/spread.png", "step": "page_box", "page": 1, "value": [20, 20, 500, 1410]},
    {"source": "src/spread.png", "step": "content_box", "page": 1, "value": [60, 80, 460, 1300]},
    {"source": "src/spread.png", "step": "margin", "page": 1, "value": 4},
    {"source": "src/spread.png", "step": "skew", "page": 2, "value": 0},
    {"source": "src/spread.png", "step": "page_box", "page": 2, "value": [0, 0, 500, 1400]},
    {"source": "src/spread.png", "step": "content_box", "page": 2, "value": None},
    {"source": "src/spread.png", "step": "margin", "page": 2, "value": 0, "lock": True},
]


def test_hand_values_place_a_mark_where_the_chain_says_from_one_resampling(tmp_path):
    source = _source(tmp_path / "src", draw=_marked)
    before = _digest(source)
    overrides = _overrides(tmp_path, HAND_VALUES)
    out = tmp_path / "out"
    assert main(["prepare", str(source), "--output", str(out), "--overrides", str(overrides)]) == 0

    manifest = _manifest(out)
    first = manifest["pages"][0]
    assert first["verdict"] == "no_flags" and first["flags"] == []
    assert first["steps"]["skew"] == {
        "value": 2.0,
        "origin": "manual",
        "confidence": None,
        "evidence": "Set by hand in overrides.json.",
        "flags": [],
    }
    chain = Chain.from_dict(first["geometry"])
    page = Image.open(out / first["output"]["name"])
    expected = chain.forward([MARK])[0]
    assert math.dist(_mark_centre(page), expected) < 0.75
    assert math.dist(chain.inverse([expected])[0], MARK) < 1e-6
    # The affine stored in the manifest maps the point without pagekit's code.
    a, b, c, d, e, f = first["geometry"]["affine_output_to_source"]
    x, y = expected
    assert math.dist((a * x + b * y + c, d * x + e * y + f), MARK) < 1e-6
    # The page is exactly the original source put through the chain once.
    with Image.open(source) as original:
        remade = render(original.copy(), chain, first["geometry"]["fill"]["colour"])
    assert remade.tobytes() == page.tobytes()
    assert first["output"]["sha256"] == _digest(out / first["output"]["name"])
    assert _digest(source) == before


def test_a_leaning_cut_keeps_the_neighbours_wedge_out_of_each_page(tmp_path):
    def ink_beside_the_cut(draw):
        # The cut runs from (600, 0) to (660, 1000). Ink fills each side from 40 px away.
        draw.polygon([(640, 0), (1400, 0), (1400, 1000), (700, 1000)], fill=20)
        draw.polygon([(0, 0), (560, 0), (620, 1000), (0, 1000)], fill=20)

    source = _source(tmp_path / "src", paper=210, draw=ink_beside_the_cut)
    cut = {"pages": 2, "cut": [[600, 0], [660, 1000]]}
    overrides = _overrides(tmp_path, [{"source": "src/spread.png", "step": "split", "value": cut}])
    out = tmp_path / "out"
    assert main(["prepare", str(source), "--output", str(out), "--overrides", str(overrides)]) == 1
    left, right = _manifest(out)["pages"]
    with Image.open(source) as image:
        assert image.getpixel((660, 10)) == 20 and image.getpixel((570, 990)) == 20
    for entry, wedge in ((left, (660.0, 10.5)), (right, (570.5, 990.5))):
        chain = Chain.from_dict(entry["geometry"])
        x, y = chain.forward([wedge])[0]
        page = Image.open(out / entry["output"]["name"])
        assert 0 <= x < page.size[0] and 0 <= y < page.size[1]  # inside the page's frame
        assert page.getpixel((int(x), int(y))) == 210  # filled, not the neighbour's ink
        assert entry["geometry"]["fill"]["colour"] == 210


def test_no_output_is_upsampled_and_a_shrunk_page_says_so(tmp_path):
    source = _source(tmp_path / "src", size=(800, 600), dpi=(300, 300))
    out = tmp_path / "out"
    prepared = plan([source], out, settings_overrides={"max_output_dpi": 600})
    page = prepared.pages[0]
    assert page.chain.scale == (1.0, 1.0) and page.output_dpi == (300.0, 300.0)

    smaller = plan([source], tmp_path / "small", settings_overrides={"max_output_dpi": 150})
    manifest = execute(smaller)
    entry = manifest["pages"][0]
    crop = entry["geometry"]["steps"][3]["size_after"]
    assert entry["output"]["size"] == [round(crop[0] / 2), round(crop[1] / 2)]
    assert entry["output"]["resolution"] == pytest.approx([150.0, 150.0])
    with Image.open(tmp_path / "small" / entry["output"]["name"]) as written:
        assert written.info["dpi"] == pytest.approx((150, 150), abs=0.05)


def test_a_re_run_with_unchanged_inputs_is_byte_identical(tmp_path):
    source = _source(tmp_path / "src", draw=_marked)
    _source(tmp_path / "src", name="colour.png", size=(500, 700), mode="RGB")
    overrides = _overrides(tmp_path, HAND_VALUES[:3])
    out = tmp_path / "out"
    command = ["prepare", str(tmp_path / "src"), "--output", str(out)]
    assert main([*command, "--overrides", str(overrides)]) == 1
    first = _snapshot(out)
    assert main(command) == 1
    assert _snapshot(out) == first
    assert main([*command, "--overrides", str(overrides)]) == 1
    assert _snapshot(out) == first
    assert sorted(first) == [
        "colour_p1.tif",
        MANIFEST_NAME,
        PROJECT_NAME,
        "review.html",
        "spread_p1.tif",
        "spread_p2.tif",
    ]
    assert (
        _digest(source)
        == hashlib.sha256((tmp_path / "src" / "spread.png").read_bytes()).hexdigest()
    )
    with Image.open(out / "colour_p1.tif") as colour, Image.open(out / "spread_p1.tif") as grey:
        assert colour.mode == "RGB" and grey.mode == "L"


def test_overrides_are_kept_manual_ones_flagged_when_inputs_move_locked_ones_not(tmp_path):
    source = _source(tmp_path / "src", draw=_marked)
    out = tmp_path / "out"
    command = ["prepare", str(source), "--output", str(out)]
    first = _overrides(
        tmp_path,
        [
            {"source": "src/spread.png", "step": "skew", "page": 1, "value": 1.5},
            {
                "source": "src/spread.png",
                "step": "page_box",
                "page": 1,
                "value": [10, 10, 1300, 900],
                "lock": True,
            },
        ],
    )
    assert main([*command, "--overrides", str(first)]) == 1
    assert main(command) == 1  # no overrides file: the corrections stay
    steps = _project(out)["sources"][0]["pages"][0]["steps"]
    assert (steps["skew"]["value"], steps["skew"]["origin"], steps["skew"]["flags"]) == (
        1.5,
        "manual",
        [],
    )
    assert steps["page_box"]["origin"] == "locked"

    # Turning the source moves what both were set on.
    turn = _overrides(
        tmp_path,
        [{"source": "src/spread.png", "step": "orientation", "value": 2}],
        name="turn.json",
    )
    assert main([*command, "--overrides", str(turn)]) == 1
    steps = _project(out)["sources"][0]["pages"][0]["steps"]
    assert steps["skew"]["value"] == 1.5 and steps["skew"]["origin"] == "manual"
    assert len(steps["skew"]["flags"]) == 1
    assert "earlier step orientation changed" in steps["skew"]["flags"][0]
    assert steps["page_box"]["value"] == [10, 10, 1300, 900]
    assert steps["page_box"]["flags"] == []
    # The detected values downstream were recomputed on the new inputs.
    assert steps["content_box"]["value"] == [10, 10, 1300, 900]
    page_flags = [flag["reason"] for flag in _manifest(out)["pages"][0]["flags"]]
    assert steps["skew"]["flags"][0] in page_flags

    # Applying the same correction again does not make it fresh: it stays flagged.
    assert main([*command, "--overrides", str(first)]) == 1
    skew = _project(out)["sources"][0]["pages"][0]["steps"]["skew"]
    assert skew["flags"] == steps["skew"]["flags"]
    # Locking it, after a check, accepts it on the new inputs.
    lock = _overrides(
        tmp_path,
        [{"source": "src/spread.png", "step": "skew", "page": 1, "value": 1.5, "lock": True}],
        name="lock.json",
    )
    assert main([*command, "--overrides", str(lock)]) == 1
    skew = _project(out)["sources"][0]["pages"][0]["steps"]["skew"]
    assert (skew["origin"], skew["flags"]) == ("locked", [])


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"source": "src/absent.png", "step": "skew", "page": 1, "value": 1}, "no source image"),
        ({"source": "0" * 64, "step": "skew", "page": 1, "value": 1}, "no source image"),
        ({"source": "src/spread.png", "step": "dewarp", "value": 1}, "no step"),
        ({"source": "src/spread.png", "step": "skew", "page": 2, "value": 1}, "no page 2"),
        ({"source": "src/spread.png", "step": "skew", "value": 1}, "name a page"),
        ({"source": "src/spread.png", "step": "orientation", "value": 5}, "0, 1, 2 or 3"),
    ],
)
def test_an_override_naming_something_absent_is_refused_and_changes_nothing(
    tmp_path, capsys, entry, message
):
    source = _source(tmp_path / "src")
    out = tmp_path / "out"
    assert main(["prepare", str(source), "--output", str(out)]) == 1
    before = _snapshot(out)
    bad = _overrides(tmp_path, [entry])
    capsys.readouterr()
    assert main(["prepare", str(source), "--output", str(out), "--overrides", str(bad)]) == 2
    assert message in capsys.readouterr().err
    assert _snapshot(out) == before


def test_an_override_may_name_the_source_by_sha256(tmp_path):
    source = _source(tmp_path / "src")
    overrides = _overrides(
        tmp_path, [{"source": _digest(source), "step": "skew", "page": 1, "value": -1.25}]
    )
    prepared = plan([source], tmp_path / "out", overrides_path=overrides)
    assert prepared.pages[0].steps["skew"]["value"] == -1.25


def test_report_stale_lists_stale_steps_and_writes_nothing(tmp_path, capsys):
    source = _source(tmp_path / "src")
    out = tmp_path / "out"
    stale = ["prepare", str(source), "--output", str(out), "--report-stale"]
    assert main(stale) == 1
    assert "orientation: not computed yet" in capsys.readouterr().out
    assert not out.exists()

    assert main(["prepare", str(source), "--output", str(out)]) == 1
    before = _snapshot(out)
    capsys.readouterr()
    assert main(stale) == 0
    assert capsys.readouterr().out == "nothing is stale\n"

    split = _overrides(
        tmp_path,
        [
            {
                "source": "src/spread.png",
                "step": "split",
                "value": {"pages": 2, "cut": [[700, 0], [700, 1000]]},
            },
        ],
    )
    assert main([*stale, "--overrides", str(split)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "../src/spread.png split: override 1 in overrides.json sets it (manual)"
    assert (
        "../src/spread.png page 1 skew: will be recomputed: the earlier step split changed" in lines
    )
    assert "../src/spread.png page 2 skew: not computed yet" in lines
    assert _snapshot(out) == before


def test_a_missing_resolution_is_a_flag_not_a_default(tmp_path):
    source = _source(tmp_path / "src", dpi=None)
    prepared = plan([source], tmp_path / "out")
    page = prepared.pages[0]
    assert page.resolution == {"value": None, "origin": "missing", "file_value": None}
    assert page.output_dpi is None
    reasons = [flag["reason"] for flag in page.flags if flag["step"] == "resolution"]
    assert len(reasons) == 1 and "no resolution" in reasons[0]
    # No margin or overlap could be measured, so none was added.
    assert page.chain.crop_box == (0, 0, 1400, 1000)

    fixed = _overrides(
        tmp_path, [{"source": "src/spread.png", "step": "resolution", "value": [400, 400]}]
    )
    page = plan([source], tmp_path / "out", overrides_path=fixed).pages[0]
    assert page.resolution["origin"] == "override" and page.output_dpi == (400.0, 400.0)
    assert not [flag for flag in page.flags if flag["step"] == "resolution"]


def test_an_implausible_resolution_is_a_flag(tmp_path):
    source = _source(tmp_path / "src", dpi=(72, 72))
    page = plan([source], tmp_path / "out").pages[0]
    reasons = [flag["reason"] for flag in page.flags if flag["step"] == "resolution"]
    assert len(reasons) == 1 and "outside the plausible range" in reasons[0]


def test_a_blank_page_is_written_as_an_image_of_the_paper(tmp_path):
    source = _source(tmp_path / "src", size=(500, 700), paper=233)
    blank = _overrides(
        tmp_path, [{"source": "src/spread.png", "step": "content_box", "page": 1, "value": None}]
    )
    out = tmp_path / "out"
    assert main(["prepare", str(source), "--output", str(out), "--overrides", str(blank)]) == 1
    entry = _manifest(out)["pages"][0]
    with Image.open(out / entry["output"]["name"]) as page:
        assert page.size == (500, 700)  # the whole page box
        assert set(page.tobytes()) == {233}
    assert entry["geometry"]["fill"]["colour"] == 233


@pytest.mark.parametrize(
    "case",
    [
        "only corrupt",
        "empty folder",
        "inside source",
        "only 16-bit",
        "missing project",
        "bad project",
        "bad overrides",
    ],
)
def test_unusable_input_is_exit_2_and_writes_nothing(tmp_path, capsys, case):
    good = _source(tmp_path / "src")
    out = tmp_path / "out"
    arguments = [str(good), "--output", str(out)]
    if case == "only corrupt":  # no usable source at all (one among good ones is skipped)
        (tmp_path / "src" / "broken.png").write_bytes(b"not a png")
        arguments = [str(tmp_path / "src" / "broken.png"), "--output", str(out)]
    elif case == "empty folder":
        (tmp_path / "empty").mkdir()
        arguments = [str(tmp_path / "empty"), "--output", str(out)]
    elif case == "inside source":
        arguments = [str(good), "--output", str(tmp_path / "src" / "out")]
    elif case == "only 16-bit":
        Image.new("I;16", (300, 300), 4000).save(tmp_path / "src" / "deep.png")
        arguments = [str(tmp_path / "src" / "deep.png"), "--output", str(out)]
    elif case == "bad overrides":
        (tmp_path / "fix.json").write_text("{not json")
        arguments += ["--overrides", str(tmp_path / "fix.json")]
    elif case == "missing project":
        arguments += ["--project", str(tmp_path / "nope.json")]
    elif case == "bad project":
        assert main(["prepare", *arguments]) == 1
        project = _project(out)
        project["sources"][0]["note"] = "unknown key"
        (out / PROJECT_NAME).write_text(json.dumps(project))
    before_sources = _snapshot(tmp_path / "src")
    before_out = _snapshot(out)
    assert main(["prepare", *arguments]) == 2
    assert capsys.readouterr().err.startswith("pagekit: ")
    assert _snapshot(tmp_path / "src") == before_sources
    assert _snapshot(out) == before_out


def test_a_tampered_inputs_hash_is_refused(tmp_path):
    source = _source(tmp_path / "src")
    out = tmp_path / "out"
    assert main(["prepare", str(source), "--output", str(out)]) == 1
    project = _project(out)
    project["sources"][0]["steps"]["orientation"]["inputs"]["settings"] = {"x": 1}
    (out / PROJECT_NAME).write_text(canonical_json(project))
    with pytest.raises(PrepareError, match="inputs hash"):
        plan([source], out)


def test_the_project_file_is_written_atomically(tmp_path, monkeypatch):
    source = _source(tmp_path / "src")
    out = tmp_path / "out"
    assert main(["prepare", str(source), "--output", str(out)]) == 1
    before = (out / PROJECT_NAME).read_bytes()
    overrides = _overrides(
        tmp_path, [{"source": "src/spread.png", "step": "orientation", "value": 2}]
    )
    prepared = plan([source], out, overrides_path=overrides)

    def crash(*_args):
        raise OSError("disk pulled out")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        execute(prepared)
    assert (out / PROJECT_NAME).read_bytes() == before
    assert not [path for path in out.iterdir() if path.name.startswith(".")]


def test_tiff_output_is_lossless_and_carries_its_resolution(tmp_path):
    source = _source(tmp_path / "src", mode="RGB", size=(600, 500), dpi=(400, 400))
    prepared = plan([source], tmp_path / "out", settings_overrides={"output_format": "tiff"})
    manifest = execute(prepared)
    entry = manifest["pages"][0]
    assert entry["output"]["name"] == "spread_p1.tif"
    with Image.open(tmp_path / "out" / "spread_p1.tif") as page, Image.open(source) as original:
        assert page.info["compression"] == "tiff_adobe_deflate"
        assert page.info["dpi"] == pytest.approx((400, 400))
        remade = render(
            original.copy(), prepared.pages[0].chain, tuple(entry["geometry"]["fill"]["colour"])
        )
        assert page.tobytes() == remade.tobytes()


def test_a_detector_is_called_once_its_answer_stored_and_reused(tmp_path):
    source = _source(tmp_path / "src", draw=_marked)
    calls = []

    def skew(context):
        calls.append(context.page)
        image, chain = context.working_copy(200)
        assert max(image.size) <= 200 and chain.scale[0] < 1
        assert context.values == {"orientation": 0, "split": {"pages": 1}}
        return Answer(0.75, 0.9, "Test detector.", ())

    detector = {"skew": Detector("test.skew/1", skew)}
    out = tmp_path / "out"
    execute(plan([source], out, detectors=detector))
    assert calls == [1]
    record = _project(out)["sources"][0]["pages"][0]["steps"]["skew"]
    assert (record["value"], record["origin"], record["confidence"], record["method"]) == (
        0.75,
        "detected",
        0.9,
        "test.skew/1",
    )
    execute(plan([source], out, detectors=detector))
    assert calls == [1]  # inputs unchanged: kept as it is
    execute(plan([source], out, detectors={"skew": Detector("test.skew/2", skew)}))
    assert calls == [1, 1]  # a new method recomputes

    def broken(context):
        return {"value": 0.5, "confidence": 2, "evidence": "Too sure.", "flags": []}

    # A refused answer fails that page alone: neutral default and a flag.
    page = plan([source], tmp_path / "other", detectors={"skew": Detector("bad/1", broken)})
    (refused,) = page.pages
    assert refused.steps["skew"]["value"] == 0.0 and refused.steps["skew"]["confidence"] == 0
    assert any("refuses" in flag["reason"] for flag in refused.flags if flag["step"] == "skew")


def test_continuing_needs_every_source_the_project_holds(tmp_path):
    first = _source(tmp_path / "src", name="a.png", size=(300, 300))
    _source(tmp_path / "src", name="b.png", size=(300, 300))
    out = tmp_path / "out"
    assert main(["prepare", str(tmp_path / "src"), "--output", str(out)]) == 1
    with pytest.raises(PrepareError, match="b.png"):
        plan([first], out)
    # With no sources named, the project's own are used.
    assert [page.output_name for page in plan(None, out).pages] == ["a_p1.tif", "b_p1.tif"]


# Hand-set values across a moved cut or a dropped page


def _spread_with_page_two_skew(tmp_path: Path, cut_x: int, name="overrides.json") -> Path:
    return _overrides(
        tmp_path,
        [
            {
                "source": "src/spread.png",
                "step": "split",
                "value": {"pages": 2, "cut": [[cut_x, 0], [cut_x, 1000]]},
            },
            {"source": "src/spread.png", "step": "skew", "page": 2, "value": 1.0},
        ],
        name=name,
    )


def test_a_moved_cut_flags_the_unchanged_manual_skew_of_page_two(tmp_path):
    """The spec's example: the overrides file moves the cut and keeps page 2's skew."""
    source = _source(tmp_path / "src")
    out = tmp_path / "out"
    command = ["prepare", str(source), "--output", str(out)]
    first = _spread_with_page_two_skew(tmp_path, 700)
    assert main([*command, "--overrides", str(first)]) == 1
    skew = _project(out)["sources"][0]["pages"][1]["steps"]["skew"]
    assert skew["flags"] == []
    moved = _spread_with_page_two_skew(tmp_path, 720)
    assert main([*command, "--overrides", str(moved)]) == 1
    after = _project(out)["sources"][0]["pages"][1]["steps"]["skew"]
    assert after["value"] == 1.0 and after["origin"] == "manual"
    assert after["inputs_hash"] == skew["inputs_hash"]  # still the inputs it was set on
    assert len(after["flags"]) == 1 and "earlier step split changed" in after["flags"][0]


def test_hand_set_values_of_a_dropped_page_are_kept_flagged_and_restored(tmp_path):
    source = _source(tmp_path / "src")
    out = tmp_path / "out"
    command = ["prepare", str(source), "--output", str(out)]
    spread = _spread_with_page_two_skew(tmp_path, 700)
    assert main([*command, "--overrides", str(spread)]) == 1
    one = _overrides(
        tmp_path,
        [{"source": "src/spread.png", "step": "split", "value": {"pages": 1}}],
        name="one.json",
    )
    assert main([*command, "--overrides", str(one)]) == 1
    for _ in range(2):  # flagged on every run, not only the first
        entry = _project(out)["sources"][0]
        assert [page["page"] for page in entry["pages"]] == [1]
        (dropped,) = entry["dropped_pages"]
        assert dropped["page"] == 2 and dropped["output"] == "spread_p2.tif"
        assert dropped["steps"]["skew"]["value"] == 1.0
        assert set(dropped["steps"]) == {"skew"}  # detected values are not kept
        manifest = _manifest(out)
        reasons = [flag["reason"] for flag in manifest["pages"][0]["flags"]]
        assert any("Page 2" in reason and "dropped_pages" in reason for reason in reasons)
        # The left-over output is listed as stale and not deleted.
        assert manifest["stale_outputs"] == ["spread_p2.tif"]
        assert (out / "spread_p2.tif").is_file()
        assert main(command) == 1
    # Splitting again restores the hand-set value, on the inputs it was set on.
    assert (
        main([*command, "--overrides", str(_overrides(tmp_path, spread_entries(), "two.json"))])
        == 1
    )
    entry = _project(out)["sources"][0]
    assert entry["dropped_pages"] == []
    skew = entry["pages"][1]["steps"]["skew"]
    assert (skew["value"], skew["origin"], skew["flags"]) == (1.0, "manual", [])
    assert _manifest(out)["stale_outputs"] == []


def spread_entries() -> list[dict]:
    return [
        {
            "source": "src/spread.png",
            "step": "split",
            "value": {"pages": 2, "cut": [[700, 0], [700, 1000]]},
        }
    ]


def test_a_dropped_page_is_cleared_by_removing_it_from_the_project(tmp_path):
    source = _source(tmp_path / "src")
    out = tmp_path / "out"
    command = ["prepare", str(source), "--output", str(out)]
    assert main([*command, "--overrides", str(_spread_with_page_two_skew(tmp_path, 700))]) == 1
    one = _overrides(
        tmp_path,
        [{"source": "src/spread.png", "step": "split", "value": {"pages": 1}}],
        name="one.json",
    )
    assert main([*command, "--overrides", str(one)]) == 1
    project = _project(out)
    project["sources"][0]["dropped_pages"] = []
    (out / PROJECT_NAME).write_text(canonical_json(project))
    assert main(command) == 1
    assert not [
        flag for flag in _manifest(out)["pages"][0]["flags"] if "dropped_pages" in flag["reason"]
    ]


def test_an_output_path_that_cannot_be_replaced_changes_nothing(tmp_path, capsys):
    source = _source(tmp_path / "src")
    out = tmp_path / "out"
    command = ["prepare", str(source), "--output", str(out)]
    assert main(command) == 1
    (out / "spread_p2.tif").mkdir()  # in the way of the second page
    before = _snapshot(out)
    names = sorted(path.name for path in out.iterdir())
    split = _overrides(tmp_path, spread_entries())
    assert main([*command, "--overrides", str(split)]) == 2
    assert _snapshot(out) == before
    assert sorted(path.name for path in out.iterdir()) == names


@pytest.mark.parametrize("failing", ["render", "rename"])
def test_a_failure_part_way_leaves_the_earlier_run_untouched(tmp_path, monkeypatch, failing):
    source = _source(tmp_path / "src")
    out = tmp_path / "out"
    assert main(["prepare", str(source), "--output", str(out)]) == 1
    before = _snapshot(out)
    names = sorted(path.name for path in out.iterdir())
    prepared = plan([source], out, overrides_path=_overrides(tmp_path, spread_entries()))
    import pagekit.output as output

    if failing == "render":
        real = output.render
        calls = []

        def render_then_fail(*args):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("out of memory")
            return real(*args)

        monkeypatch.setattr(output, "render", render_then_fail)
    else:
        real_replace = os.replace

        failed = []

        def replace(source_path, target):
            # Fails once, when the pages are already in place: on the manifest.
            if Path(target).name == MANIFEST_NAME and not failed:
                failed.append(1)
                raise OSError("disk full")
            return real_replace(source_path, target)

        monkeypatch.setattr(os, "replace", replace)
    with pytest.raises((RuntimeError, OSError)):
        execute(prepared)
    assert _snapshot(out) == before
    assert sorted(path.name for path in out.iterdir()) == names


def test_outputs_are_readable_by_others(tmp_path):
    source = _source(tmp_path / "src")
    out = tmp_path / "out"
    assert main(["prepare", str(source), "--output", str(out)]) == 1
    for path in out.iterdir():
        assert path.stat().st_mode & 0o777 == 0o644


def test_a_resolution_override_carries_forward_and_enters_the_page_inputs(tmp_path):
    source = _source(tmp_path / "src", dpi=None)
    out = tmp_path / "out"
    command = ["prepare", str(source), "--output", str(out)]

    def resolution(dpi, name):
        entry = {"source": "src/spread.png", "step": "resolution", "value": [dpi, dpi]}
        return _overrides(tmp_path, [entry], name=name)

    assert main([*command, "--overrides", str(resolution(400, "r400.json"))]) == 1
    assert main(command) == 1  # no overrides file: the override is kept
    entry = _project(out)["sources"][0]
    assert entry["resolution"]["origin"] == "override"
    assert entry["resolution"]["value"] == [400.0, 400.0]
    assert not [
        flag for flag in _manifest(out)["pages"][0]["flags"] if flag["step"] == "resolution"
    ]
    skew = entry["pages"][0]["steps"]["skew"]
    assert skew["inputs"]["settings"]["resolution"] == [400.0, 400.0]

    assert main([*command, "--overrides", str(resolution(500, "r500.json"))]) == 1
    again = _project(out)["sources"][0]["pages"][0]["steps"]["skew"]
    assert again["inputs"]["settings"]["resolution"] == [500.0, 500.0]
    assert again["inputs_hash"] != skew["inputs_hash"]


@pytest.mark.parametrize(
    "document",
    [
        {
            "schema": "pagekit-overrides.v1",
            "overrides": [
                {"source": "src/spread.png", "step": "skew", "page": 1, "value": 1, "note": "x"}
            ],
        },
        {"schema": "pagekit-overrides.v1", "overrides": [], "author": "someone"},
    ],
)
def test_unknown_keys_in_an_overrides_file_are_refused(tmp_path, capsys, document):
    source = _source(tmp_path / "src")
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(document))
    out = tmp_path / "out"
    assert main(["prepare", str(source), "--output", str(out), "--overrides", str(path)]) == 2
    assert "unknown keys" in capsys.readouterr().err
    assert not out.exists()


def test_a_source_changed_while_running_is_refused_and_nothing_written(tmp_path):
    source = _source(tmp_path / "src")
    out = tmp_path / "out"
    prepared = plan([source], out)
    _source(tmp_path / "src", paper=200)  # the same file, rewritten
    with pytest.raises(PrepareError, match="changed while"):
        execute(prepared)
    assert not out.exists() or not list(out.iterdir())


def test_the_overlap_uses_the_upright_frames_own_axis(tmp_path):
    source = _source(tmp_path / "src", size=(1000, 1400), dpi=(300, 600))
    entries = [
        {"source": "src/spread.png", "step": "orientation", "value": 1},
        {
            "source": "src/spread.png",
            "step": "split",
            "value": {"pages": 2, "cut": [[700, 0], [700, 1000]]},
        },
    ]
    page = plan([source], tmp_path / "out", overrides_path=_overrides(tmp_path, entries)).pages[0]
    # After one quarter turn the upright frame's x axis is the source's y axis: 600 dpi.
    assert max(x for x, _ in page.chain.polygon) == pytest.approx(700 + 3.0 * 600 / 25.4)


def test_a_stored_resolution_is_checked_when_the_project_is_read(tmp_path):
    source = _source(tmp_path / "src")
    out = tmp_path / "out"
    assert main(["prepare", str(source), "--output", str(out)]) == 1
    project = _project(out)
    project["sources"][0]["resolution"]["value"] = [-300, 300]
    (out / PROJECT_NAME).write_text(canonical_json(project))
    with pytest.raises(PrepareError, match="resolution"):
        plan([source], out)


def test_the_margin_comes_from_its_setting_sure_and_unflagged(tmp_path):
    """The margin is a setting, not a detection: confidence 1, no flag."""
    source = _source(tmp_path / "src")
    hand = [entry for entry in HAND_VALUES if entry["step"] != "margin"]
    out = tmp_path / "out"
    command = ["prepare", str(source), "--output", str(out), "--overrides"]
    assert main([*command, str(_overrides(tmp_path, hand))]) == 0
    margin = _project(out)["sources"][0]["pages"][0]["steps"]["margin"]
    assert (margin["value"], margin["origin"], margin["confidence"], margin["flags"]) == (
        5.0,
        "detected",
        1.0,
        [],
    )
    assert "margin_mm" in margin["evidence"]
    assert margin["inputs"]["settings"]["margin_mm"] == 5.0
