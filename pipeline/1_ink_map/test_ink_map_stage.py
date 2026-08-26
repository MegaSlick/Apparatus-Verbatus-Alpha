"""The early ink map is evidence over pages, before any proposal exists."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.canonical import digest_bytes
from common.contracts.errors import FatalAccounting
from common.contracts.outcomes import OutcomeClass, classify, terminal_category
from common.contracts.stages import EXEMPLAR, INK_MAP
from common.imaging import encode_grayscale_png
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def _load(relative_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INK_MAP_RUN = _load("pipeline/1_ink_map/run.py", "ink_map_run_test")
RECENSOR_RUN = _load("pipeline/5_recensor/run.py", "recensor_run_same_measure_test")


def _invoke(root: Path, program: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_every_sealed_page_is_mapped_before_any_detection(tmp_path):
    root = tmp_path / "runs"
    _invoke(root, "pipeline/1_exemplar/door.py")
    _invoke(root, "pipeline/1_exemplar/run.py")
    _invoke(root, "pipeline/1_ink_map/run.py")
    tree = RunTree(root, "r")
    sealed_pages = [
        row
        for row in tree.build_manifest(EXEMPLAR)["artifacts"]
        if row["kind"] == "page" and row["outcome"] == "sealed"
    ]
    maps = [row for row in tree.build_manifest(INK_MAP)["artifacts"] if row["kind"] == "ink-map"]
    assert len(sealed_pages) == 2
    assert len(maps) == len(sealed_pages)
    assert not tree.resolve(tree.manifest_path("designator")).exists()


def test_early_map_and_late_reconciliation_import_one_measure():
    assert INK_MAP_RUN.page_residual_ink is RECENSOR_RUN.page_residual_ink


def test_unclaimed_edge_ink_is_named_and_bounded_but_not_held():
    rows = [bytearray([230] * 200) for _ in range(200)]
    for y in range(10):
        rows[y][10:30] = bytes([170] * 20)
    finding = INK_MAP_RUN.page_edge_ink(encode_grayscale_png(200, 200, rows))
    assert finding["flagged"] is True
    assert finding["named_finding"] == "unclaimed-edge-ink"
    assert finding["edge_band_pixels"] == 64
    assert classify(INK_MAP, "unclaimed-edge-ink") is OutcomeClass.UNRESOLVED
    assert terminal_category(INK_MAP, "unclaimed-edge-ink") is None
    handoff = (ROOT / "pipeline/1_ink_map/HANDOFF.md").read_text(encoding="utf-8")
    assert "Unit 14 owns the explicit hold outcome" in handoff


def test_ink_thresholds_remain_explicitly_unmeasured_without_a_calibration_claim():
    source = (ROOT / "common/residual_ink.py").read_text(encoding="utf-8")
    handoff = (ROOT / "pipeline/1_ink_map/HANDOFF.md").read_text(encoding="utf-8")
    assert "PROPOSED, NOT YET MEASURED" in source
    assert "PROPOSED, NOT YET\nMEASURED" in handoff
    assert "calibrated" not in handoff.lower()


class _StubTree:
    """Minimal malformed evidence that production Exemplar checks reject earlier."""

    def __init__(self, pages, blobs=None):
        self._pages = pages
        self._blobs = blobs or {}
        self.run_id = "r"

    def build_manifest(self, stage, verify_inputs=True):
        return {
            "artifacts": [
                {
                    "kind": "page",
                    "artifact_id": str(index),
                    "relative_path": f"1_exemplar/artifacts/page/{index}.json",
                }
                for index, _ in enumerate(self._pages)
            ]
        }

    def read_artifact(self, stage, kind, artifact_id):
        return self._pages[int(artifact_id)]

    def read_bytes(self, relative_path):
        return self._blobs[relative_path]


class _StubContext:
    def __init__(self, tree, run):
        self.tree = tree
        self.run = run


def _sealed_page(ordinal, *, image_path="blob", digest="0" * 64):
    return {
        "subject_id": f"page-{ordinal}",
        "outcome": "sealed",
        "payload": {"ordinal": ordinal, "image_path": image_path, "source_sha256": digest},
    }


def test_the_ink_map_refuses_a_page_the_submitted_manifest_does_not_name_once(monkeypatch):
    """One sealed source per map, or no map at all.

    A page claiming an ordinal nobody submitted, or two sealed pages claiming
    the same one, would each put a page's evidence into the census under an
    identity the run authority does not carry -- and this census is the
    denominator Unit 14 derives coverage from. The Exemplar boundary proof is
    stubbed out for the duplicate case on purpose: the rule under test is a
    statement about the census as a whole, and it must hold whether or not each
    page individually satisfies its own boundary.
    """
    unsubmitted = _StubContext(_StubTree([_sealed_page(7)]), {"source_manifest": [{"ordinal": 1}]})
    with pytest.raises(FatalAccounting, match="ordinal 7 matches 0 submitted source rows"):
        INK_MAP_RUN.sealed_pages(unsubmitted)

    monkeypatch.setattr(INK_MAP_RUN, "verify_sealed_page_pixels", lambda *args: None)
    duplicated = _StubContext(
        _StubTree([_sealed_page(1), _sealed_page(1)]), {"source_manifest": [{"ordinal": 1}]}
    )
    with pytest.raises(FatalAccounting, match="more than one sealed page names ordinal 1"):
        INK_MAP_RUN.sealed_pages(duplicated)

    malformed = _StubContext(_StubTree([_sealed_page(True)]), {"source_manifest": [{"ordinal": 1}]})
    with pytest.raises(FatalAccounting, match="sealed page has no integer ordinal"):
        INK_MAP_RUN.sealed_pages(malformed)


def test_the_ink_map_refuses_a_run_it_was_handed_no_sealed_page_of():
    """An empty map is not a mapped run: every sealed page has exactly one record.

    A stage that sealed a boundary over zero records would report a completed
    ink map for a run it never measured, which is GOVERNANCE 2's silent loss
    wearing a completion seal.
    """
    refused = {"subject_id": "page-1", "outcome": "refused", "payload": {"ordinal": 1}}
    context = _StubContext(_StubTree([refused]), {"source_manifest": [{"ordinal": 1}]})
    with pytest.raises(FatalAccounting, match="contains no sealed pages"):
        INK_MAP_RUN.sealed_pages(context)


def test_real_ingress_keeps_every_direct_stage_entry_guard(monkeypatch):
    """The real shortcut may omit fixture loading, not run-integrity checks.

    ``open_context`` applies all four checks on fixture ingress. Real ingress
    needs a separate opener because it has no fixture, so this pins the guards
    that shortcut must retain: live-register drift, the immutable snapshot, the
    Exemplar seal, and the run-level hard-failure cap.
    """
    run = {
        "ingress": {"mode": "real"},
        "adapter_recipes": {INK_MAP: "deterministic-residual-ink-v1"},
    }

    class _RealTree:
        def read_run(self):
            return run

    tree = _RealTree()
    calls = []
    monkeypatch.setattr(INK_MAP_RUN, "RunTree", lambda *_args: tree)
    monkeypatch.setattr(
        INK_MAP_RUN,
        "verify_snapshot_is_current",
        lambda observed, path: calls.append(("live-register", observed, path)),
    )
    monkeypatch.setattr(
        INK_MAP_RUN,
        "read_snapshot",
        lambda observed_tree, observed_run: calls.append(
            ("sealed-snapshot", observed_tree, observed_run)
        ),
    )
    monkeypatch.setattr(
        INK_MAP_RUN,
        "verify_predecessor_seal",
        lambda observed_tree, stage: calls.append(("predecessor", observed_tree, stage)),
    )
    monkeypatch.setattr(
        INK_MAP_RUN,
        "_refuse_halted_run",
        lambda observed_tree, stage, path: calls.append(
            ("hard-failure", observed_tree, stage, path)
        ),
    )
    args = SimpleNamespace(
        run_root="unused",
        run_id="r",
        corpus_register="register.json",
        hard_failure_config="hard-failure.toml",
    )

    context = INK_MAP_RUN._open(args, registry_factory=None)

    assert context.tree is tree
    assert calls == [
        ("live-register", run, "register.json"),
        ("sealed-snapshot", tree, run),
        ("predecessor", tree, INK_MAP),
        ("hard-failure", tree, INK_MAP, "hard-failure.toml"),
    ]


def test_the_ink_map_measures_only_pixels_it_digested_itself():
    """The bytes measured are the bytes proved, not a second unchecked read.

    `verify_sealed_page_pixels` proves the sealed blob against a read of its
    own; this stage then reads it again to measure it. The Recensor states this
    same guard for this same measure at the late boundary, and the early map --
    the pre-proposal evidence baseline every later denominator rests on -- may
    not be the weaker of the two.
    """
    page = _sealed_page(1, digest=digest_bytes(b"the sealed pixels"))
    honest = _StubTree([page], {"blob": b"the sealed pixels"})
    assert INK_MAP_RUN.measured_page_bytes(honest, 1, page) == b"the sealed pixels"

    swapped = _StubTree([page], {"blob": b"different pixels under the same name"})
    with pytest.raises(FatalAccounting, match="does not match the pixel digest"):
        INK_MAP_RUN.measured_page_bytes(swapped, 1, page)


def test_the_ink_map_declares_the_decode_route_it_actually_takes():
    """One implementation may not declare two routes.

    The map and the Recensor's late reconciliation call the same
    `page_residual_ink` over the same `common/imaging.py` decoder, so a stage
    seal claiming a different route family for one of them is a false statement
    about its own pass (GOVERNANCE 6) and makes the decode-environment census
    report drift that is not there.
    """
    from common.contracts.stages import RECENSOR
    from common.stage import _decode_environment

    ink_map = _decode_environment(INK_MAP)
    assert ink_map["decode_paths_used"] == _decode_environment(RECENSOR)["decode_paths_used"]
    assert ink_map["decode_paths_used"] == ["project-png"]
    assert ink_map["produced_pixels"] is True


def test_the_edge_band_is_a_bounded_instrument_and_says_it_is_not_calibrated():
    """The edge width must remain visibly proposed until corpus calibration."""
    from common.residual_ink import EDGE_BAND_PIXELS

    source = (ROOT / "common/residual_ink.py").read_text(encoding="utf-8")
    assert EDGE_BAND_PIXELS == 64
    assert "not a\n# claim that 64 pixels is a calibrated cross-page-act threshold" in source


def test_a_page_with_no_ink_at_all_still_measures_clean_rather_than_flagging():
    """A zero-ink page still needs evidence, but must not manufacture an alarm."""
    blank = encode_grayscale_png(200, 200, [bytearray([230] * 200) for _ in range(200)])
    edge = INK_MAP_RUN.page_edge_ink(blank)
    ink = INK_MAP_RUN.page_residual_ink(blank, covered=[])

    assert edge["flagged"] is False
    assert ink["total_ink_pixels"] == 0
    assert classify(INK_MAP, "mapped") is OutcomeClass.COMPLETED


def test_a_page_with_no_ink_is_published_as_mapped(monkeypatch):
    blank = encode_grayscale_png(200, 200, [bytearray([230] * 200) for _ in range(200)])
    page = _sealed_page(1)

    class _PublishingContext:
        def __init__(self):
            self.tree = object()
            self.published = []
            self.sealed = False
            self.finished = False

        def input_ref(self, path):
            return {"relative_path": path, "sha256": "0" * 64}

        def publish(self, **record):
            self.published.append(record)

        def seal_boundary(self):
            self.sealed = True

        def finish(self):
            self.finished = True

    context = _PublishingContext()

    class _Parser:
        @staticmethod
        def parse_args():
            return SimpleNamespace()

    monkeypatch.setattr(INK_MAP_RUN, "stage_parser", lambda *_args: _Parser())
    monkeypatch.setattr(INK_MAP_RUN, "_open", lambda *_args: context)
    monkeypatch.setattr(
        INK_MAP_RUN,
        "sealed_pages",
        lambda _context: [(1, page, "1_exemplar/artifacts/page/page.json")],
    )
    monkeypatch.setattr(INK_MAP_RUN, "measured_page_bytes", lambda *_args: blank)

    assert INK_MAP_RUN.main(registry_factory=None) == 0
    assert [record["outcome"] for record in context.published] == ["mapped"]
    assert context.sealed is True
    assert context.finished is True


def test_a_one_pixel_wide_page_records_its_whole_width_as_edge():
    """The smallest legal width has an edge even though ``width // 2`` is zero."""
    rows = [bytearray([170 if y < 25 else 230]) for y in range(100)]

    edge = INK_MAP_RUN.page_edge_ink(encode_grayscale_png(1, 100, rows))

    assert edge["edge_band_pixels"] == 1
    assert edge["total_ink_pixels"] == 25
    assert edge["outside_ink_pixels"] == 25
    assert edge["flagged"] is True


def test_the_fixture_geometry_is_degenerate_and_every_fixture_page_flags():
    """Name the fixture's own degeneracy so a green run is not over-read.

    A 64-pixel band on a 200x260 page leaves a 72x132 centre, so every page of
    both pinned scenarios flags and no run in the suite produces `mapped` at
    all. That is a fact about the specimen, not about the instrument, and it is
    pinned here because a green suite otherwise says nothing about selectivity.
    If the fixture pages gain a quiet perimeter, the handoff must change too.
    """
    from common.imaging import dimensions
    from common.residual_ink import EDGE_BAND_PIXELS, page_edge_ink
    from proof.synthetic_pages import page_bytes

    width, height = dimensions(page_bytes(1))
    centre = (width - 2 * EDGE_BAND_PIXELS) * (height - 2 * EDGE_BAND_PIXELS)
    assert (width, height) == (200, 260)
    assert centre * 4 < width * height, (
        "the fixture page now has a substantial quiet centre; the ink map handoff "
        "says it does not, and the edge findings on a fixture run mean something else"
    )
    for ordinal in (1, 2):
        assert page_edge_ink(page_bytes(ordinal))["flagged"] is True
