"""The two-step prescribed-midpoint pilot, driven through its command line."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common.contracts.canonical import canonical_bytes
from operations.operator.scantailor_worker import parse
from operations.triage import recordgold_midpoint_pilot as pilot

PROJECT_NAME = "recordgold-prescribed-midpoints.ScanTailor"


def _png(size: tuple[int, int], colour: str) -> bytes:
    encoded = BytesIO()
    Image.new("RGB", size, colour).save(encoded, format="PNG")
    return encoded.getvalue()


def _layout(tmp_path: Path, pages: dict[str, tuple[int, int]]) -> dict[str, Path]:
    base = tmp_path / "work"
    source_root = base / "submission"
    for index, (identifier, size) in enumerate(pages.items()):
        path = source_root / "pages" / f"{identifier}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png(size, ("white", "black", "red", "blue")[index % 4]))
    dirs = {
        "base": base,
        "source_root": source_root,
        "output": base / "out",
        "prepared": base / "prepared",
    }
    dirs["output"].mkdir()
    dirs["prepared"].mkdir()
    return dirs


def _args(dirs: dict[str, Path], pages: list[str], *extra: str) -> list[str]:
    argv = [
        "--source-root",
        str(dirs["source_root"]),
        "--output-dir",
        str(dirs["output"]),
        "--corpus-id",
        "recordgold-pilot",
    ]
    for page in pages:
        argv += ["--page", page]
    return argv + list(extra)


def _import(dirs: dict[str, Path], pages: list[str]) -> Path:
    """Step one, then what `verbatus scantailor` publishes from its project."""
    assert pilot.main(_args(dirs, pages, "--project-dir", str(dirs["base"]))) == 0
    project = dirs["base"] / PROJECT_NAME
    geometry = dirs["base"] / "geometry.json"
    geometry.write_bytes(canonical_bytes(parse(project.read_bytes(), project)) + b"\n")
    return geometry


def _refused(argv: list[str], capsys, message: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        pilot.main(argv)
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_two_steps_write_rows_binding_and_every_prepared_page(tmp_path: Path):
    dirs = _layout(tmp_path, {"one": (40, 20), "two": (30, 20)})
    pages = ["one:0", "two:180"]
    geometry = _import(dirs, pages)
    argv = _args(
        dirs,
        pages,
        "--geometry-document",
        str(geometry),
        "--prepared-source-dir",
        str(dirs["prepared"]),
    )
    assert pilot.main(argv) == 0
    manifest = json.loads((dirs["output"] / "triage-decision-manifest.json").read_bytes())
    binding = json.loads((dirs["output"] / "scantailor-triage-binding.json").read_bytes())
    assert len(manifest["records"]) == 2
    assert sorted(path.name for path in dirs["prepared"].iterdir()) == [
        "one-left.png",
        "one-right.png",
        "two-left.png",
        "two-right.png",
    ]
    assert len(binding["materialized_pages"]) == 4


def test_a_page_identifier_that_leaves_the_source_root_is_refused(tmp_path: Path, capsys):
    dirs = _layout(tmp_path, {"one": (40, 20)})
    (dirs["base"] / "pages").mkdir()
    (dirs["base"] / "pages" / "outside.jpg").write_bytes(_png((40, 20), "white"))
    _refused(
        _args(dirs, ["../../pages/outside:0"], "--project-dir", str(dirs["base"])),
        capsys,
        "leaves --source-root",
    )


@pytest.mark.parametrize(
    ("project_dir", "message"),
    [
        (lambda dirs: None, "must name an existing directory"),
        (lambda dirs: dirs["base"] / "absent", "must name an existing directory"),
        (lambda dirs: dirs["source_root"], "not equal to it"),
        (lambda dirs: dirs["output"], "must be an ancestor of --source-root"),
    ],
)
def test_the_project_is_written_only_into_a_strict_ancestor(
    tmp_path: Path, capsys, project_dir, message: str
):
    dirs = _layout(tmp_path, {"one": (40, 20)})
    chosen = project_dir(dirs)
    extra = () if chosen is None else ("--project-dir", str(chosen))
    _refused(_args(dirs, ["one:0"], *extra), capsys, message)
    assert not list(dirs["source_root"].rglob("*.ScanTailor"))


def test_a_non_empty_prepared_directory_is_refused_before_anything_is_written(
    tmp_path: Path, capsys
):
    dirs = _layout(tmp_path, {"one": (40, 20)})
    geometry = _import(dirs, ["one:0"])
    (dirs["prepared"] / "leftover.png").write_bytes(b"")
    argv = _args(
        dirs,
        ["one:0"],
        "--geometry-document",
        str(geometry),
        "--prepared-source-dir",
        str(dirs["prepared"]),
    )
    _refused(argv, capsys, "must be an existing empty directory")
    assert not list(dirs["output"].iterdir())


def test_page_ids_that_share_a_file_name_are_refused_before_rendering(tmp_path: Path, capsys):
    dirs = _layout(tmp_path, {"a/x": (40, 20), "b/x": (30, 20)})
    pages = ["a/x:0", "b/x:0"]
    geometry = _import(dirs, pages)
    argv = _args(
        dirs,
        pages,
        "--geometry-document",
        str(geometry),
        "--prepared-source-dir",
        str(dirs["prepared"]),
    )
    _refused(argv, capsys, "prepared pages collide")
    assert not list(dirs["prepared"].iterdir())
    assert not list(dirs["output"].iterdir())


def test_a_render_failure_writes_no_prepared_page(tmp_path: Path, capsys, monkeypatch):
    dirs = _layout(tmp_path, {"one": (40, 20), "two": (30, 20)})
    pages = ["one:0", "two:0"]
    geometry = _import(dirs, pages)
    real = pilot.render_triage_derivative
    calls = []

    def fail_late(data, *, page_index, part):
        calls.append(part)
        if len(calls) == 3:
            raise ValueError("synthetic render failure")
        return real(data, page_index=page_index, part=part)

    monkeypatch.setattr(pilot, "render_triage_derivative", fail_late)
    argv = _args(
        dirs,
        pages,
        "--geometry-document",
        str(geometry),
        "--prepared-source-dir",
        str(dirs["prepared"]),
    )
    _refused(argv, capsys, "synthetic render failure")
    assert not list(dirs["prepared"].iterdir())
    assert not list(dirs["output"].iterdir())


def test_two_page_ids_naming_one_source_are_refused(tmp_path: Path, capsys):
    dirs = _layout(tmp_path, {"one": (40, 20)})
    argv = _args(dirs, ["one:0", "./one:180"], "--project-dir", str(dirs["base"]))
    _refused(argv, capsys, "names a source another --page already names")
    assert not (dirs["base"] / PROJECT_NAME).exists()


def test_an_existing_project_is_a_named_refusal_not_a_traceback(tmp_path: Path, capsys):
    dirs = _layout(tmp_path, {"one": (40, 20)})
    argv = _args(dirs, ["one:0"], "--project-dir", str(dirs["base"]))
    assert pilot.main(argv) == 0
    capsys.readouterr()
    _refused(argv, capsys, "could not write")
