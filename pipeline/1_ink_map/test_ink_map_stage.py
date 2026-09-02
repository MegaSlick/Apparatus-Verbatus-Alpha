"""The early ink map is evidence over pages, before any proposal exists."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ApprovalRefusal, FatalAccounting
from common.contracts.outcomes import OutcomeClass, classify, terminal_category
from common.contracts.stages import DESIGNATOR, EXEMPLAR, INK_MAP
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
    # The constant, not the literal: a bare string that stops matching the
    # stage identifier points the path at somewhere nothing writes, and the
    # assertion then passes for the wrong reason forever.
    assert not tree.resolve(tree.manifest_path(DESIGNATOR)).exists()


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
    # Matched on normalised whitespace. The claim is "no calibration is
    # asserted", not "this sentence occupies these two lines": pinned to the
    # exact wrap, a reflow of either file turned the suite red over no change
    # in meaning.
    assert "PROPOSED, NOT YET MEASURED" in " ".join(source.split())
    normalised_handoff = " ".join(handoff.split()).lower()
    assert "proposed, not yet measured" in normalised_handoff
    # The claim, not the word. Banning "calibrated" outright also failed
    # "not calibrated" and "uncalibrated" -- so strengthening the handoff to
    # say plainly that the band is not calibrated turned the suite red, and the
    # cheapest way out of that is to delete the sentence.
    assert "this stage claims no calibration" in normalised_handoff
    for claim in ("is calibrated", "was calibrated", "has been calibrated"):
        assert claim not in normalised_handoff


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
    # Comment markers and wrapping normalised away, for the same reason as above.
    normalised = " ".join(source.replace("#", " ").split())
    assert "not a claim that 64 pixels is a calibrated cross-page-act threshold" in normalised


def test_a_page_with_no_ink_at_all_still_measures_clean_rather_than_flagging():
    """A zero-ink page still needs evidence, but must not manufacture an alarm."""
    blank = encode_grayscale_png(200, 200, [bytearray([230] * 200) for _ in range(200)])
    edge = INK_MAP_RUN.page_edge_ink(blank)
    ink = INK_MAP_RUN.page_residual_ink(blank, covered=[])

    assert edge["flagged"] is False
    assert ink["total_ink_pixels"] == 0
    assert classify(INK_MAP, "mapped") is OutcomeClass.COMPLETED


# One definition, used by both publishing tests below. Kept at module level
# because two byte-identical copies of a stage-context stub drift: the copy
# nobody updates keeps testing the old `publish`/`seal_boundary` contract
# while still passing.
class _PublishingContext:
    def __init__(self, run=None):
        self.tree = object()
        self.run = run if run is not None else {"ingress": {"mode": "synthetic-fixture"}}
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


def test_the_ink_map_opens_both_ingress_routes_through_the_shared_constructor(monkeypatch):
    """The stage has no opener of its own any more, on either route.

    It used to hand-build its real-ingress context here and re-list the
    direct-entry guards itself -- register drift, the sealed snapshot, the
    Exemplar seal, the run-level cap -- so a guard added to the shared
    constructor would have missed this stage silently, and the three stages
    that copied that shape already disagreed with each other. What is pinned
    now is narrower and stronger: `main` hands the parsed argv, its own stage
    name and the registry factory it was given to `common.stage.open_stage_context`,
    which decides the route from one read of the run authority and applies the
    same guards on both.
    """
    blank = encode_grayscale_png(200, 200, [bytearray([230] * 200) for _ in range(200)])
    page = _sealed_page(1)
    context = _PublishingContext()
    args = SimpleNamespace(run_root="unused", run_id="r")
    opened = []

    def registry_factory(_path):
        raise AssertionError("the stage must pass the factory through, not resolve it")

    def open_stage_context(observed_args, stage, *, registry_factory):
        opened.append((observed_args, stage, registry_factory))
        return context

    class _Parser:
        @staticmethod
        def parse_args():
            return args

    monkeypatch.setattr(INK_MAP_RUN, "stage_parser", lambda *_args: _Parser())
    monkeypatch.setattr(INK_MAP_RUN, "open_stage_context", open_stage_context)
    monkeypatch.setattr(
        INK_MAP_RUN,
        "sealed_pages",
        lambda _context: [(1, page, "1_exemplar/artifacts/page/page.json")],
    )
    monkeypatch.setattr(INK_MAP_RUN, "measured_page_bytes", lambda *_args: blank)

    assert INK_MAP_RUN.main(registry_factory=registry_factory) == INK_MAP_RUN.EXIT_COMPLETE
    assert opened == [(args, INK_MAP, registry_factory)]
    assert not hasattr(INK_MAP_RUN, "_open"), "a stage-private opener is the drift this closes"


def test_the_ink_map_refuses_a_run_whose_ingress_evidence_names_no_route(monkeypatch):
    """An absent `ingress` key must still stop this stage, as it did before the
    shared constructor.

    `open_stage_context`'s own route test, `is_real_ingress`, treats a missing
    `ingress` key as synthetic by design -- it has to, to decide which route to
    build -- and does not raise. This stage never branches on the route, so
    nothing else in `main` re-parses the record; before both routes shared one
    constructor, this stage's own opener re-parsed it unconditionally and
    refused exactly this run. That refusal is re-created here, deliberately,
    rather than dropped as a side effect of sharing the constructor.
    """
    context = _PublishingContext(run={})

    class _Parser:
        @staticmethod
        def parse_args():
            return SimpleNamespace()

    monkeypatch.setattr(INK_MAP_RUN, "stage_parser", lambda *_args: _Parser())
    monkeypatch.setattr(INK_MAP_RUN, "open_stage_context", lambda *_args, **_kwargs: context)

    with pytest.raises(ApprovalRefusal, match="closed fixture-or-real record"):
        INK_MAP_RUN.main(registry_factory=None)
    assert context.published == [], "no record may be published before the route is proved"


def test_a_page_with_no_ink_is_published_as_mapped(monkeypatch):
    blank = encode_grayscale_png(200, 200, [bytearray([230] * 200) for _ in range(200)])
    page = _sealed_page(1)

    context = _PublishingContext()

    class _Parser:
        @staticmethod
        def parse_args():
            return SimpleNamespace()

    monkeypatch.setattr(INK_MAP_RUN, "stage_parser", lambda *_args: _Parser())
    monkeypatch.setattr(INK_MAP_RUN, "open_stage_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        INK_MAP_RUN,
        "sealed_pages",
        lambda _context: [(1, page, "1_exemplar/artifacts/page/page.json")],
    )
    monkeypatch.setattr(INK_MAP_RUN, "measured_page_bytes", lambda *_args: blank)

    # The constant, not 0, for the reason line 65 already gives about literals.
    assert INK_MAP_RUN.main(registry_factory=None) == INK_MAP_RUN.EXIT_COMPLETE
    assert [record["outcome"] for record in context.published] == ["mapped"]
    assert context.sealed is True
    assert context.finished is True


def test_the_ink_map_refuses_a_page_whose_verified_pixels_will_not_decode(monkeypatch):
    """A digest-verified page this module's own decoder still cannot read is a
    named refusal, not a bare traceback.

    `measured_page_bytes` proves the bytes match the digest the Exemplar sealed;
    it says nothing about whether `page_residual_ink`/`page_edge_ink` can decode
    them. `run_stage` only catches `RunHalted` and `ContractError`
    (`common/stage.py`), so an uncaught decoder `ValueError` here would escape as
    an unhandled traceback with `seal_boundary`/`finish` never reached -- GOVERNANCE
    2's silent loss with extra steps.
    """
    page = _sealed_page(1)

    context = _PublishingContext()

    class _Parser:
        @staticmethod
        def parse_args():
            return SimpleNamespace()

    monkeypatch.setattr(INK_MAP_RUN, "stage_parser", lambda *_args: _Parser())
    monkeypatch.setattr(INK_MAP_RUN, "open_stage_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        INK_MAP_RUN,
        "sealed_pages",
        lambda _context: [(1, page, "1_exemplar/artifacts/page/page.json")],
    )
    monkeypatch.setattr(INK_MAP_RUN, "measured_page_bytes", lambda *_args: b"not an image")

    with pytest.raises(FatalAccounting, match="cannot measure sealed Exemplar page 1"):
        INK_MAP_RUN.main(registry_factory=None)

    assert context.published == []
    assert context.sealed is False
    assert context.finished is False


def test_the_undecodable_page_refusal_does_not_claim_an_empty_run_tree(monkeypatch):
    """The sibling above fails on page 1, so nothing was published and the old
    wording happened to be true. Here page 1 decodes and page 2 does not.

    Publication is inside the page loop, so a decode failure part-way through a
    shard leaves the earlier pages' records on disk. The refusal used to say "no
    ink-map record was written", which an operator reads as a clean tree and
    acts on -- retrying or clearing up against a false picture of what is there.
    The boundary is still unsealed, so nothing downstream proceeds; what was
    wrong was the sentence, and this pins it against the records that exist.
    """
    good, bad = _sealed_page(1), _sealed_page(2)
    blank = encode_grayscale_png(20, 20, [bytearray([230] * 20) for _ in range(20)])

    context = _PublishingContext()

    class _Parser:
        @staticmethod
        def parse_args():
            return SimpleNamespace()

    monkeypatch.setattr(INK_MAP_RUN, "stage_parser", lambda *_args: _Parser())
    monkeypatch.setattr(INK_MAP_RUN, "open_stage_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        INK_MAP_RUN,
        "sealed_pages",
        lambda _context: [
            (1, good, "1_exemplar/artifacts/page/one.json"),
            (2, bad, "1_exemplar/artifacts/page/two.json"),
        ],
    )
    monkeypatch.setattr(
        INK_MAP_RUN,
        "measured_page_bytes",
        lambda _tree, ordinal, _page: blank if ordinal == 1 else b"not an image",
    )

    with pytest.raises(FatalAccounting, match="cannot measure sealed Exemplar page 2") as caught:
        INK_MAP_RUN.main(registry_factory=None)

    message = str(caught.value)
    assert "incomplete map" in message
    assert "no ink-map record was written" not in message, (
        "the refusal claimed an empty tree while page 1's record was already published"
    )
    assert [record["subject_id"] for record in context.published] == [good["subject_id"]]
    assert context.sealed is False
    assert context.finished is False


def test_a_measure_that_omits_its_fraction_is_refused_by_name_not_by_key_error():
    """A missing key and a wrong type are the same contract break, reported alike.

    `artifact_finding` used to index `fraction_outside` directly, so a shared
    measure that stopped emitting it produced a bare `KeyError` with no stage,
    page, or contract named -- while the same measure emitting a string got a
    clean refusal. The weaker input got the worse report.
    """
    complete = {
        "background_level": 230,
        "total_ink_pixels": 10,
        "outside_ink_pixels": 4,
        "fraction_outside": 0.4,
        "flagged": False,
    }
    assert INK_MAP_RUN.artifact_finding(complete)["fraction_outside_per_million"] == 400_000

    without_fraction = {key: value for key, value in complete.items() if key != "fraction_outside"}
    with pytest.raises(FatalAccounting, match="no float `fraction_outside`"):
        INK_MAP_RUN.artifact_finding(without_fraction)
    with pytest.raises(FatalAccounting, match="no float `fraction_outside`"):
        INK_MAP_RUN.artifact_finding({**complete, "fraction_outside": "0.4"})


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
