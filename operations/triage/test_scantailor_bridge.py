"""Focused contracts for the prescribed ScanTailor midpoint seam."""

from __future__ import annotations

import copy
import os
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common.contracts.canonical import canonical_bytes, digest_bytes
from common.imaging import render_triage_derivative
from operations.operator.scantailor_worker import parse
from operations.triage.producer import SubmittedFrame, produce
from operations.triage.scantailor_bridge import (
    ScantailorBridgeRefusal,
    load_imported_geometry,
    transcribe_imported_geometry,
    transcribe_midpoint_splits,
)
from operations.triage.scantailor_project import PrescribedSpread, prescribed_midpoint_project


def _frame(path: str, size: tuple[int, int]) -> SubmittedFrame:
    encoded = BytesIO()
    Image.new("RGB", size, "white").save(encoded, format="PNG")
    return SubmittedFrame(path, encoded.getvalue())


def _document(tmp_path: Path) -> tuple[dict, dict[str, SubmittedFrame], list[str]]:
    sources = [
        str((tmp_path / "pages/018dc88c3b3f47d308d2.jpg").resolve()),
        str((tmp_path / "pages/2adc37ec376f362ad102.jpg").resolve()),
    ]
    project = prescribed_midpoint_project(
        [
            PrescribedSpread("pages/018dc88c3b3f47d308d2.jpg", 3864, 3056),
            PrescribedSpread("pages/2adc37ec376f362ad102.jpg", 3672, 2744),
        ]
    )
    document = parse(project, tmp_path / "recordgold-midpoints.ScanTailor")
    frames = {
        sources[0]: _frame("pages/018dc88c3b3f47d308d2.jpg", (3864, 3056)),
        sources[1]: _frame("pages/2adc37ec376f362ad102.jpg", (3672, 2744)),
    }
    return document, frames, sources


def test_prescribed_real_dimensions_produce_four_door_rows_in_physical_order(tmp_path: Path):
    document, frames, sources = _document(tmp_path)
    translated = transcribe_midpoint_splits(
        document,
        geometry_document_sha256="a" * 64,
        submitted_by_source_path=frames,
        corpus_id="recordgold-pilot",
        mode="manual",
        orientation_degrees_by_source_path={sources[0]: 0, sources[1]: 180},
    )
    first, second = translated.rows_by_submitted_path.values()
    assert [(part["region"]["x"], part["region"]["w"]) for part in first["split"]["parts"]] == [
        (0, 1932),
        (1932, 1932),
    ]
    assert [(part["region"]["x"], part["region"]["w"]) for part in second["split"]["parts"]] == [
        (1836, 1836),
        (0, 1836),
    ]
    assert [part["rotation"]["rotation_millidegrees"] for part in second["split"]["parts"]] == [
        180_000,
        180_000,
    ]
    # The standard producer is the Door-admission boundary; this proves the
    # bridge's rows, not merely their local shape, are acceptable there.
    admitted = produce(
        list(frames.values()),
        corpus_id="recordgold-pilot",
        mode="manual",
        transcribed_rows_by_path=translated.rows_by_submitted_path,
    )
    assert len(admitted.manifest["records"]) == 2
    assert translated.binding["rows"][1]["orientation_degrees"] == 180
    # The separate prepared sources use the same deterministic pixel operation
    # the Exemplar would apply to these triage parts; no bespoke crop routine
    # gets to reinterpret the stored geometry.
    rendered = [
        render_triage_derivative(frames[sources[1]].data, page_index=0, part=part)
        for part in second["split"]["parts"]
    ]
    assert [geometry["width"] for _pixels, geometry in rendered] == [1836, 1836]
    assert [geometry["height"] for _pixels, geometry in rendered] == [2744, 2744]
    assert all(pixels.startswith(b"\x89PNG\r\n\x1a\n") for pixels, _geometry in rendered)


def test_slanted_cutter_refuses_without_rounding_geometry(tmp_path: Path):
    document, frames, sources = _document(tmp_path)
    document["geometry"][0]["cutters"][0]["p2"]["x"] = "1933"
    with pytest.raises(ScantailorBridgeRefusal, match="vertical line"):
        transcribe_midpoint_splits(
            document,
            geometry_document_sha256="a" * 64,
            submitted_by_source_path=frames,
            corpus_id="recordgold-pilot",
            mode="manual",
            orientation_degrees_by_source_path={sources[0]: 0, sources[1]: 180},
        )


def test_foreign_submitted_source_refuses_exact_coverage(tmp_path: Path):
    document, frames, sources = _document(tmp_path)
    foreign = dict(frames)
    foreign.pop(sources[1])
    foreign["/workspace/private/trial-source-public/pages/foreign.jpg"] = _frame(
        "pages/foreign.jpg", (3672, 2744)
    )
    with pytest.raises(ScantailorBridgeRefusal, match="exact coverage"):
        transcribe_midpoint_splits(
            document,
            geometry_document_sha256="a" * 64,
            submitted_by_source_path=foreign,
            corpus_id="recordgold-pilot",
            mode="manual",
            orientation_degrees_by_source_path={sources[0]: 0},
        )


def test_a_declared_removed_half_is_refused_rather_than_published(tmp_path: Path):
    """The operator deleted a half in ScanTailor; this bridge emits both.

    Publishing both halves would let the excluded half reach the Door as an ordinary
    row, and emitting only the retained half would decide its ordering silently.
    """
    document, frames, sources = _document(tmp_path)
    removed = {
        **document,
        "geometry": [
            {
                **document["geometry"][0],
                "image": {**document["geometry"][0]["image"], "removed_half": "left"},
            },
            *document["geometry"][1:],
        ],
    }
    with pytest.raises(ScantailorBridgeRefusal, match="declares a removed half"):
        transcribe_midpoint_splits(
            removed,
            geometry_document_sha256="a" * 64,
            submitted_by_source_path=frames,
            corpus_id="recordgold-pilot",
            mode="manual",
            orientation_degrees_by_source_path={sources[0]: 0, sources[1]: 180},
        )


def _small(tmp_path: Path) -> tuple[dict, dict[str, SubmittedFrame], list[str]]:
    sources = [str((tmp_path / "pages/a.png").resolve()), str((tmp_path / "pages/b.png").resolve())]
    project = prescribed_midpoint_project(
        [PrescribedSpread("pages/a.png", 40, 20), PrescribedSpread("pages/b.png", 30, 20)]
    )
    document = parse(project, tmp_path / "small.ScanTailor")
    frames = {
        sources[0]: _frame("pages/a.png", (40, 20)),
        sources[1]: _frame("pages/b.png", (30, 20)),
    }
    return document, frames, sources


def _translate(document, frames, orientations):
    return transcribe_midpoint_splits(
        document,
        geometry_document_sha256="a" * 64,
        submitted_by_source_path=frames,
        corpus_id="recordgold-pilot",
        mode="manual",
        orientation_degrees_by_source_path=orientations,
    )


def _entry(document: dict, index: int = 0) -> dict:
    return document["geometry"][index]


@pytest.mark.parametrize(
    ("edit", "tail"),
    [
        (
            lambda d, f, o, s: f.__setitem__(s[1], SubmittedFrame(f[s[0]].path, f[s[1]].data)),
            "two ScanTailor sources map to one submitted path",
        ),
        (
            lambda d, f, o, s: _entry(d)["image"].__setitem__("width", 41),
            "imported ScanTailor dimensions disagree with submitted bytes",
        ),
        (
            lambda d, f, o, s: o.__setitem__(s[0], 90),
            "source orientation must be 0 or 180 degrees",
        ),
        (
            lambda d, f, o, s: o.__setitem__(s[0], False),
            "source orientation must be 0 or 180 degrees",
        ),
        (
            lambda d, f, o, s: _entry(d)["outline"][1].__setitem__("x", "39"),
            "ScanTailor outline is not the exact submitted frame",
        ),
        (
            lambda d, f, o, s: _entry(d)["cutters"].append(copy.deepcopy(_entry(d)["cutters"][0])),
            "ScanTailor two-pages geometry needs exactly one cutter",
        ),
        (
            lambda d, f, o, s: _entry(d)["cutters"][0].__setitem__("name", "cutter2"),
            "ScanTailor cutter is not the supported primary split",
        ),
        (
            lambda d, f, o, s: [
                _entry(d)["cutters"][0][point].__setitem__("x", "20.5") for point in ("p1", "p2")
            ],
            "ScanTailor geometry cannot be represented exactly by triage pixels",
        ),
        (
            lambda d, f, o, s: _entry(d)["image"].__setitem__("file_image", 1),
            "names a page other than the first of its file",
        ),
        (
            lambda d, f, o, s: o.pop(s[1]),
            "every imported source needs exactly one declared orientation",
        ),
    ],
)
def test_bridge_refusals_name_their_reason(tmp_path: Path, edit, tail: str):
    document, frames, sources = _small(tmp_path)
    orientations = {sources[0]: 0, sources[1]: 180}
    _translate(document, dict(frames), dict(orientations))
    edit(document, frames, orientations, sources)
    with pytest.raises(ScantailorBridgeRefusal, match=f"{tail}$"):
        _translate(document, frames, orientations)


def _published(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "geometry.json"
    path.write_bytes(canonical_bytes(document) + b"\n")
    return path


def test_published_geometry_is_read_once_and_bound_by_digest(tmp_path: Path):
    document, frames, sources = _small(tmp_path)
    path = _published(tmp_path, document)
    loaded, digest = load_imported_geometry(path)
    assert loaded == document
    assert digest == digest_bytes(path.read_bytes())
    translated = transcribe_imported_geometry(
        path,
        submitted_by_source_path=frames,
        corpus_id="recordgold-pilot",
        mode="manual",
        orientation_degrees_by_source_path={sources[0]: 0, sources[1]: 0},
    )
    assert translated.binding["geometry_document_sha256"] == digest


@pytest.mark.parametrize(
    ("raw", "tail"),
    [
        (b"{not json\n", "imported ScanTailor geometry is not JSON"),
        (b"[" * 100_000 + b"]" * 100_000, "imported ScanTailor geometry is not JSON"),
        (
            b"[" * 300 + b"]" * 300 + b"\n",
            "imported ScanTailor geometry cannot be represented as canonical JSON",
        ),
        (b"1.5\n", "imported ScanTailor geometry cannot be represented as canonical JSON"),
        (b'"\\ud800"\n', "imported ScanTailor geometry cannot be represented as canonical JSON"),
        (b'{"a": 1}\n', "imported ScanTailor geometry is not canonical bytes"),
    ],
)
def test_published_geometry_refuses_unreadable_json(tmp_path: Path, raw: bytes, tail: str):
    path = tmp_path / "geometry.json"
    path.write_bytes(raw)
    with pytest.raises(ScantailorBridgeRefusal, match=f"{tail}$"):
        load_imported_geometry(path)


def test_published_geometry_must_be_one_direct_regular_file(tmp_path: Path):
    document, _frames, _sources = _small(tmp_path)
    target = _published(tmp_path, document)
    link = tmp_path / "link.json"
    os.symlink(target, link)
    tail = "imported ScanTailor geometry could not be read as one direct regular file$"
    for path in (link, tmp_path / "missing.json", tmp_path):
        with pytest.raises(ScantailorBridgeRefusal, match=tail):
            load_imported_geometry(path)


@pytest.mark.parametrize("size", [(1, 20), (40, 1), (True, 20), (40.0, 20)])
def test_a_prescribed_source_needs_two_pixels_on_each_axis(size):
    with pytest.raises(ValueError, match="integers of at least 2 pixels$"):
        prescribed_midpoint_project([PrescribedSpread("pages/a.png", *size)])
