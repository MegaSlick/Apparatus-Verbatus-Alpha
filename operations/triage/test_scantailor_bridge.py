"""Focused contracts for the prescribed ScanTailor midpoint seam."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common.contracts.canonical import canonical_bytes
from common.imaging import render_triage_derivative
from operations.operator.scantailor_worker import parse
from operations.triage.producer import SubmittedFrame, produce
from operations.triage.scantailor_bridge import (
    ORIENTATION_SCHEMA,
    ScantailorBridgeRefusal,
    load_orientations,
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
        )


def test_a_declared_removed_half_is_refused_rather_than_published(tmp_path: Path):
    """The operator deleted a half in ScanTailor; this bridge emits both.

    `removed_half` was required by the closed image schema and then read by
    nothing, so an excluded half reached the Door as an ordinary row and could
    be established as an act with nothing downstream able to tell the removal
    had been discarded (CodeRabbit). Refused rather than honoured: emitting
    only the retained half would decide that half's physical ordering silently.
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


def test_a_boolean_orientation_is_refused_not_read_as_zero(tmp_path: Path):
    """`False == 0` in Python, so a JSON boolean passed the membership test.

    It then selected the zero-degree region order, recorded `0` for
    `degrees * 1000`, and sealed `False` into the binding -- a malformed
    canonical document accepted instead of refused (CodeRabbit).
    """
    orientations = {"schema": ORIENTATION_SCHEMA, "orientations": {"pages/a.jpg": False}}
    raw = canonical_bytes(orientations) + b"\n"
    path = tmp_path / "orientations.json"
    path.write_bytes(raw)
    with pytest.raises(ScantailorBridgeRefusal, match="wrong closed schema"):
        load_orientations(path)

    # The honest spellings still load.
    for degrees in (0, 180):
        good = {"schema": ORIENTATION_SCHEMA, "orientations": {"pages/a.jpg": degrees}}
        good_path = tmp_path / f"orientations-{degrees}.json"
        good_path.write_bytes(canonical_bytes(good) + b"\n")
        assert load_orientations(good_path)[0] == {"pages/a.jpg": degrees}
