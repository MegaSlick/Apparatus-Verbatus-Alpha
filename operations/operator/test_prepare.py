"""`verbatus prepare`: pagekit's pages and a triage manifest the Door accepts, from a
folder of synthetic scans. No real register material."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from common.contracts import triage as triage_manifest
from operations.operator import cli
from operations.operator import prepare as prepare_module
from operations.submit import submit

ROOT = Path(__file__).resolve().parents[2]


def _scans(folder: Path) -> Path:
    folder.mkdir(parents=True)
    page = Image.new("L", (600, 800), 215)
    draw = ImageDraw.Draw(page)
    for y in range(120, 700, 50):
        draw.rectangle((90, y, 510, y + 12), fill=30)
    page.save(folder / "leaf one.png", dpi=(300, 300))
    Image.new("RGB", (900, 600), (230, 220, 200)).save(folder / "spread.png", dpi=(300, 300))
    return folder


def _fixes(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "pagekit-overrides.v1",
                "overrides": [
                    {"source": "my scans/leaf one.png", "step": "orientation", "value": 1},
                    {"source": "my scans/leaf one.png", "step": "skew", "page": 1, "value": 1.25},
                    {
                        "source": "my scans/spread.png",
                        "step": "split",
                        "value": {"pages": 2, "cut": [[450, 0], [450, 600]]},
                    },
                ],
            }
        )
    )
    return path


def _run(tmp_path: Path, capsys, *extra: str) -> tuple[int, str]:
    code = cli.main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "prepare",
            "--scans",
            str(tmp_path / "my scans"),
            "--out",
            str(tmp_path / "out dir"),
            *extra,
        ]
    )
    return code, capsys.readouterr().out


def _rows(out: Path) -> dict[str, dict]:
    manifest = json.loads((out / prepare_module.TRIAGE_MANIFEST_NAME).read_text())
    triage_manifest.validate_manifest(manifest)
    return {row["source_frame_sha256"]: row for row in manifest["records"]}


@pytest.fixture
def door(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(ROOT / "pipeline" / "1_exemplar"))
    import door as door_module

    return door_module


def test_prepare_writes_pages_and_a_triage_manifest_the_door_accepts(tmp_path, capsys, door):
    scans = _scans(tmp_path / "my scans")
    fixes = _fixes(tmp_path / "fixes.json")

    code, printed = _run(tmp_path, capsys, "--overrides", str(fixes), "--corpus-id", "parish-a")

    assert code == 0, printed
    out = tmp_path / "out dir"
    names = sorted(path.name for path in out.iterdir())
    assert names == [
        "leaf one_p1.tif",
        "pagekit-prepare.json",
        "pagekit-project.json",
        "review.html",
        "spread_p1.tif",
        "spread_p2.tif",
        "triage-decision-manifest.json",
        "triage-notes.txt",
        "triage-producer-recipe.json",
    ]
    assert "Prepared 3 page(s) from 2 scan(s)" in printed
    assert "Pages to review: " in printed
    assert f"Review sheet: {out / 'review.html'}" in printed
    assert "The Door will cut 3 page(s) from the original scans, 1 as pagekit cut them" in printed
    assert "2 page(s): the frame is split along a straight line" in printed
    assert f"--triage-decision-manifest '{out / 'triage-decision-manifest.json'}'" in printed
    assert f"--submission-folder '{scans}'" in printed
    assert f"verbatus --state-dir {tmp_path / 'state'} run --run-id" in printed

    rows = _rows(out)
    for row in rows.values():
        assert row["actor"]["kind"] == "producer" and row["actor"]["identity"] == "pagekit"
        assert row["human_override"] is True and row["mode"] == "semi"
        assert {part["colour_mode"] for part in row["split"]["parts"]} == {"keep"}
    leaf = rows[submit.walk_folder(scans)[0]["sha256"]]
    assert leaf["split"]["operation_order"] == "region-crop-rotate-crop"
    (part,) = leaf["split"]["parts"]
    assert part["rotation"]["rotation_millidegrees"] == 88_750
    # The fill is the paper colour pagekit filled its own page with.
    pagekit_pages = json.loads((out / "pagekit-prepare.json").read_text())["pages"]
    leaf_fill = next(
        page["geometry"]["fill"]["colour"]
        for page in pagekit_pages
        if page["source"]["name"] == "leaf one.png"
    )
    assert part["fill"] == {"levels": [leaf_fill]}
    with Image.open(out / "leaf one_p1.tif") as prepared:
        assert (part["post_crop_box"]["w"], part["post_crop_box"]["h"]) == prepared.size

    # The Door reads the two documents as written, and fans each scan out to its pages.
    decided, _clusters, _digests = door.load_triage_decisions(
        out / "triage-decision-manifest.json", None, out / "triage-producer-recipe.json"
    )
    entries = submit.walk_folder(scans)
    sources = door.expand_sources(
        entries,
        lambda path: (scans / path).read_bytes(),
        triage_rows=decided,
    )
    assert sorted((source.declared_path, source.triage_part_index) for source in sources) == [
        ("leaf one.png", 0),
        ("spread.png", 0),
        ("spread.png", 1),
    ]
    with pytest.raises(Exception, match="no triage producer recipe"):
        door.load_triage_decisions(out / "triage-decision-manifest.json", None, None)

    # pagekit's rows are declared by pagekit's own recipe, not the instrument's.
    recipe = json.loads((out / "triage-producer-recipe.json").read_text())
    assert recipe["schema"] == "pagekit-producer-recipe.v1"
    assert recipe["producer"]["identity"] == "pagekit"
    from operations.triage import instrument

    instrument_recipe = tmp_path / "instrument-recipe.json"
    instrument_recipe.write_text(json.dumps(instrument.producer_recipe(instrument.load_config())))
    with pytest.raises(Exception, match="duplicate-detection instrument's"):
        door.load_triage_decisions(out / "triage-decision-manifest.json", None, instrument_recipe)


def test_a_second_run_reuses_the_project_and_keeps_the_corrections(tmp_path, capsys):
    _scans(tmp_path / "my scans")
    fixes = _fixes(tmp_path / "fixes.json")
    assert _run(tmp_path, capsys, "--overrides", str(fixes))[0] == 0
    out = tmp_path / "out dir"
    first = {name: (out / name).read_bytes() for name in ("triage-decision-manifest.json",)}

    code, printed = _run(tmp_path, capsys)

    assert code == 0, printed
    assert (out / "triage-decision-manifest.json").read_bytes() == first[
        "triage-decision-manifest.json"
    ]
    project = json.loads((out / "pagekit-project.json").read_text())
    origins = {
        source["path"]: source["steps"]["orientation"]["origin"] for source in project["sources"]
    }
    assert set(origins.values()) == {"manual", "detected"}


def test_the_triage_geometry_is_what_pagekits_own_detectors_decide(tmp_path, monkeypatch):
    """`verbatus prepare` runs pagekit's detector pipeline, the one pagekit's own
    prepare runs, so the Door's geometry is the detected turn, cut, skew and boxes."""
    from pagekit.answer import Answer
    from pagekit.pipeline import DETECTORS
    from pagekit.prepare import Detector

    scans = _scans(tmp_path / "my scans")
    monkeypatch.setitem(
        DETECTORS,
        "skew",
        Detector("test.skew/1", lambda context: Answer(-2.0, 0.9, "A test answer.", ())),
    )
    prepared = prepare_module._plan(
        {"scans": str(scans), "out": str(tmp_path / "out"), "overrides": None, "corpus_id": "c"}
    )
    rotations = {
        part["rotation"]["rotation_millidegrees"]
        for row in prepared.manifest["records"]
        for part in row["split"]["parts"]
    }
    assert rotations == {2_000}
    methods = prepared.recipe["detector_methods"]
    assert not any(
        method.startswith("pagekit.neutral-default.")
        for names in methods.values()
        for method in names
    ), methods


def test_ctrl_c_while_writing_leaves_the_output_folder_as_it_was(tmp_path, capsys, monkeypatch):
    scans = _scans(tmp_path / "my scans")
    assert _run(tmp_path, capsys)[0] == 0
    out = tmp_path / "out dir"
    before = {path.name: path.read_bytes() for path in out.iterdir()}

    import pagekit.output

    real_render = pagekit.output.render
    calls = []

    def interrupted(*arguments, **keywords):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real_render(*arguments, **keywords)

    monkeypatch.setattr(pagekit.output, "render", interrupted)
    fixes = _fixes(tmp_path / "fixes.json")
    request = {
        "scans": str(scans),
        "out": str(out),
        "overrides": str(fixes),
        "corpus_id": "my scans",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(prepare_module.signal, "signal", lambda *_arguments: None)

    assert prepare_module.main() == prepare_module.INTERRUPTED_EXIT

    assert len(calls) == 2
    assert {path.name: path.read_bytes() for path in out.iterdir()} == before


def test_a_scans_folder_the_door_would_refuse_is_named_before_submitting(tmp_path, capsys):
    scans = _scans(tmp_path / "my scans")
    (scans / "notes.txt").write_text("not a page")

    code, printed = _run(tmp_path, capsys)

    assert code == 0
    assert "Warning: the Door will not accept the scans folder as it is" in printed
    assert "notes.txt" in printed
    assert "Warning: the Door reads scans and triage documents only from approved storage" in (
        printed
    )


def test_a_missing_scans_folder_is_refused_plainly_and_writes_nothing(tmp_path, capsys):
    code, printed = _run(tmp_path, capsys)

    assert code == 2
    assert "What happened: The pages could not be prepared." in printed
    assert "is not a folder" in printed
    assert not (tmp_path / "out dir").exists()


def test_the_double_click_route_asks_for_both_folders_and_takes_dragged_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(("prepare", "'/Users/me/My Scans'", "/Users/me/Prepared\\ Pages ", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli._interactive_arguments() == [
        "prepare",
        "--scans",
        "/Users/me/My Scans",
        "--out",
        "/Users/me/Prepared Pages",
    ]

    answers = iter(("prepare", "/scans", "/out", "/fixes.json"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert cli._interactive_arguments()[-2:] == ["--overrides", "/fixes.json"]

    answers = iter(("prepare", "/scans", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert cli._interactive_arguments() == []


def test_identical_scans_share_one_triage_row(tmp_path, capsys):
    scans = _scans(tmp_path / "my scans")
    (scans / "leaf copy.png").write_bytes((scans / "leaf one.png").read_bytes())

    code, printed = _run(tmp_path, capsys)

    assert code == 0, printed
    assert "Prepared 3 page(s) from 3 scan(s)" in printed
    assert len(_rows(tmp_path / "out dir")) == 2
