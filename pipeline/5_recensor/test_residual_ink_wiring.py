"""Residual-ink page coverage: the wiring, proven against a real run tree.

`test_residual_ink.py` proves the pixel-level arithmetic against hand-built
canvases. This proves the extraction functions in `run.py`
(`regions_by_source_page`, `sealed_page_images`, `page_coverage_findings`,
`page_coverage_for`) read the *real* Designator/Exemplar artifact shapes
correctly, and that `page_residual_ink` fires on genuine pipeline pixel bytes
when handed an incomplete covered set -- not a synthetic canvas standing in
for one.

What this file cannot yet prove end-to-end through `main()`: the walking
skeleton's synthetic Designator derives every act from the declared fixture,
so it never proposes a *short* denominator the way a real structural detector
eventually could -- there is no scenario in which the real pipeline, run
start to finish, actually misses an act on `proof/skeleton_fixture.toml`'s
pages (their ink is painted to exactly match the declared act bounds by
construction; see `proof/synthetic_pages.py`). Forging a missing region
directly is not a shortcut either: `common/stage.py`'s proposal-seal
reconciliation refuses a region set that disagrees with the sealed seal's own
evidence list before Recensor ever runs, which is a working guard, not a gap
to route around. So this exercises every function `main()` actually calls,
against a real tree, with the one input the walking skeleton cannot yet
supply (a genuinely short covered set) provided directly -- exactly the
boundary named in HANDOFF.md.
"""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.background import (
    DEFAULT_BACKGROUND_CONFIG_PATH,
    load_background_config,
    resolve_background_policy,
)
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.stages import DESIGNATOR, RECENSOR
from common.imaging import dimensions, encode_grayscale_png
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def _load_module(relative_path: str, name: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = _load_module("pipeline/5_recensor/run.py", "recensor_run_residual_ink_wiring")
sys.path.insert(0, str((ROOT / "pipeline" / "5_recensor")))
from residual_ink import (  # noqa: E402
    load_coverage_audit_config,
    page_residual_ink,
    resolve_coverage_audit_policy,
)


def _measure_page(image_bytes, covered):
    """`page_residual_ink` under this page's own resolved background policy."""
    return page_residual_ink(
        image_bytes,
        covered,
        background_policy=resolve_background_policy(
            load_background_config(), *dimensions(image_bytes)
        ),
        coverage_policy=resolve_coverage_audit_policy(
            load_coverage_audit_config(), *dimensions(image_bytes)
        ),
    )


def _invoke(root: Path, run_id: str, scenario: str, program: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{program}: {result.stderr}"


def _built_through_designator(tmp_path, scenario="happy"):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
    ):
        _invoke(root, "r", scenario, program)
    return RunTree(root, "r")


class _FakeContext:
    """Just enough of `StageContext` for these free functions: a `.tree` and
    the real sealed `.run`, which `sealed_page_images` verifies every page's
    pixels against before trusting them."""

    def __init__(self, tree):
        self.tree = tree
        self.run = tree.read_run()
        # `page_coverage_findings` reads its background policy from the path its
        # own parsed argv names and proves those bytes against the run's seal,
        # the way the Designator and the Ink Map do. A stub without these two
        # would exercise a stage that did neither.
        self.args = SimpleNamespace(designator_grouping_config=str(DEFAULT_BACKGROUND_CONFIG_PATH))
        self.required_configs = []

    def require_sealed_config(self, name, observed_sha256):
        if self.run["sealed_config_digests"].get(name) != observed_sha256:
            raise ContractError(f"sealed {name} digest does not match the consumer input")
        self.required_configs.append((name, observed_sha256))


def test_regions_by_source_page_reads_every_real_designator_region(tmp_path):
    tree = _built_through_designator(tmp_path)
    by_page = RUN.regions_by_source_page(_FakeContext(tree))

    # The happy-scenario fixture: a1 on page 1, a2 on page 1 with a
    # continuation region on page 2 (proof/skeleton_fixture.toml).
    assert set(by_page) == {1, 2}
    assert len(by_page[1]) == 2  # a1's region and a2's page-1 region
    assert len(by_page[2]) == 1  # a2's continuation region
    for bounds in by_page[1] + by_page[2]:
        assert {"x", "y", "w", "h"} == set(bounds)


def test_sealed_page_images_reads_every_real_sealed_page(tmp_path):
    tree = _built_through_designator(tmp_path)
    pages = RUN.sealed_page_images(_FakeContext(tree))
    assert set(pages) == {1, 2}
    for record in pages.values():
        assert record["outcome"] == "sealed"
        assert isinstance(record["payload"]["image_path"], str)


def test_sealed_page_images_refuses_a_page_whose_pixels_no_longer_verify(tmp_path):
    """`sealed_page_images` reads raw bytes off `payload["image_path"]`, a
    self-declared field `validate_envelope` never relates to a page's own
    digest-checked `inputs`. Every other stage that reads sealed page pixels
    (`pipeline/2_designator/run.py`, `pipeline/7_armarium/run.py`) calls
    `verify_sealed_page_pixels` first; this proves the Recensor's own wiring
    does too, rather than trusting the pixels this stage was told to check.

    `verify_sealed_page_pixels` itself is already proven directly against
    every kind of mismatch in `common/test_exemplar_boundary.py`; this only
    proves this stage's own call site actually reaches it -- corrupting the
    run's submitted filename ledger for page 1 is the simplest mismatch that
    function is already known to refuse.
    """
    tree = _built_through_designator(tmp_path)
    context = _FakeContext(tree)
    context.run = dict(context.run)
    context.run["source_manifest"] = [
        dict(row, relative_path="a-different-file.png") if row["ordinal"] == 1 else row
        for row in context.run["source_manifest"]
    ]
    with pytest.raises(FatalAccounting, match="failed pixel verification"):
        RUN.sealed_page_images(context)


def test_sealed_page_images_refuses_duplicate_ordinals_instead_of_selecting_one():
    class DuplicatePageTree:
        def build_manifest(self, _stage):
            return {
                "artifacts": [
                    {"kind": "page", "artifact_id": "page-a"},
                    {"kind": "page", "artifact_id": "page-b"},
                ]
            }

        def read_artifact(self, _stage, _kind, artifact_id):
            return {
                "artifact_id": artifact_id,
                "outcome": "sealed",
                "payload": {"ordinal": 1, "image_path": f"{artifact_id}.png"},
            }

        def read_run(self):
            # Never reached: the duplicate-ordinal refusal fires in the
            # purely structural first pass, before pixel verification (which
            # would need this to be a real source manifest) ever runs.
            return {"source_manifest": [{"ordinal": 1}]}

    with pytest.raises(FatalAccounting, match="more than one sealed page for ordinal 1"):
        RUN.sealed_page_images(_FakeContext(DuplicatePageTree()))


def test_a_region_whose_bounds_are_missing_a_side_is_refused_not_indexed(tmp_path):
    """`regions_by_source_page` hands `bounds` straight to `residual_ink`, which
    indexes all four sides, so a rectangle that is an object and nothing more
    would reach the pixel arithmetic and leave by `KeyError`. Driven through a
    stand-in tree rather than a tampered artifact on purpose: the proposal seal
    references every region by digest, so a real edited region is refused by
    `build_manifest` long before this function sees it (verified). The shape this
    guards is therefore a Designator regression, not an attacker -- and a
    regression deserves the named refusal, not a traceback."""

    class ShortBoundsTree:
        def build_manifest(self, _stage):
            return {"artifacts": [{"kind": "region", "artifact_id": "region-a"}]}

        def read_artifact(self, _stage, _kind, artifact_id):
            return {
                "artifact_id": artifact_id,
                "payload": {
                    "transform": {"source_page_ordinal": 1, "bounds": {"x": 0, "y": 0}},
                },
            }

        def read_run(self):
            return {}  # never reached: the refusal is on the region, before any page

    with pytest.raises(FatalAccounting, match="invalid transform"):
        RUN.regions_by_source_page(_FakeContext(ShortBoundsTree()))


def test_the_residual_ink_check_refuses_page_bytes_it_did_not_verify(tmp_path):
    """`sealed_page_images` verifies each page's pixels; `page_coverage_findings`
    then reads that path AGAIN to measure it. Two reads of one path is a check
    followed by a use of something else, and only the second read's bytes are
    ever measured -- so those are the bytes that have to carry the page's own
    digest.

    A single-process test cannot land a writer between the two reads, so the
    race is modelled: this tree is honest on every read the verification makes
    and returns a different page on the second read of the same path. Before the
    digest check below, this produced `flagged: False` for both pages of the
    real fixture over pixels nobody verified -- a measurement recorded as a pass
    without having been made (GOVERNANCE 10)."""
    real = _built_through_designator(tmp_path)

    class RacingTree:
        def __init__(self, tree):
            self._tree = tree
            self._read = set()

        def __getattr__(self, name):
            return getattr(self._tree, name)

        def read_bytes(self, relative_path):
            if relative_path in self._read:
                return encode_grayscale_png(40, 40, [bytearray(b"\x00" * 40) for _ in range(40)])
            self._read.add(relative_path)
            return self._tree.read_bytes(relative_path)

    context = _FakeContext(real)
    context.tree = RacingTree(real)
    with pytest.raises(FatalAccounting, match="does not match the pixel digest"):
        RUN.page_coverage_findings(context)


def test_page_coverage_findings_does_not_flag_the_real_fully_covered_fixture(tmp_path):
    """The fixture's own ink is painted to exactly match its declared act
    bounds (`proof/synthetic_pages.py`) -- proof the check does not misfire
    on ordinary, fully-accounted-for pages."""
    tree = _built_through_designator(tmp_path)
    findings = RUN.page_coverage_findings(_FakeContext(tree))
    assert set(findings) == {1, 2}
    for ordinal, finding in findings.items():
        assert finding["flagged"] is False, f"page {ordinal}: {finding}"
        assert finding["outside_ink_pixels"] == 0


def test_a_genuinely_incomplete_covered_set_flags_real_pipeline_pixels(tmp_path):
    """Real sealed page-1 bytes (Designator/Exemplar output, not a hand-built
    canvas), with a1's region deliberately left out of `covered` -- exactly
    the shape of evidence a Designator that missed an act would leave behind.
    Proves detection against genuine pipeline pixels, not synthetic ones."""
    tree = _built_through_designator(tmp_path)
    context = _FakeContext(tree)
    by_page = RUN.regions_by_source_page(context)
    pages = RUN.sealed_page_images(context)
    image_bytes = tree.read_bytes(pages[1]["payload"]["image_path"])

    # a1 is the smaller-x0/y0 region (x=20,y=20); a2 is x=20,y=120. Keep only
    # the region with the larger y as "covered", omitting a1's.
    incomplete_coverage = [max(by_page[1], key=lambda bounds: bounds["y"])]
    assert len(incomplete_coverage) < len(by_page[1])

    finding = _measure_page(image_bytes, incomplete_coverage)
    assert finding["flagged"] is True
    assert finding["outside_ink_pixels"] > 0

    # And the full, real coverage set clears it, on the identical bytes.
    full_finding = _measure_page(image_bytes, by_page[1])
    assert full_finding["flagged"] is False


def test_page_coverage_for_reads_every_page_an_acts_own_regions_touch(tmp_path):
    """Exercises the exact call `main()` makes -- real `state["regions"]`-shaped
    region records against a synthetic findings dict, so the ACT-level
    extraction (which page ordinals an act's regions touch, and whether any of
    them is flagged) is proven independent of the pixel arithmetic."""
    tree = _built_through_designator(tmp_path)
    a2_regions = [
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["payload"]["act_key"] == "a2"
    ]
    assert {region["payload"]["transform"]["source_page_ordinal"] for region in a2_regions} == {
        1,
        2,
    }

    def coverage(findings):
        return RUN.page_coverage_for(a2_regions, findings)

    def flagged(findings):
        return coverage(findings)["flagged_pages"]

    # `checked_pages` is every page the act's regions touch AND findings has an
    # entry for -- a page absent from findings entirely is never reported as
    # checked, so "checked and clear" cannot be confused with "never checked".
    assert coverage({1: {"flagged": False}, 2: {"flagged": False}})["checked_pages"] == [1, 2]
    assert coverage({1: {"flagged": False}})["checked_pages"] == [1]
    assert coverage({})["checked_pages"] == []
    assert flagged({1: {"flagged": False}, 2: {"flagged": False}}) == []
    assert flagged({1: {"flagged": False}, 2: {"flagged": True}}) == [2]
    assert flagged({1: {"flagged": True}, 2: {"flagged": True}}) == [1, 2]
    # A page with no finding at all (never checked) is never treated as flagged.
    assert flagged({}) == []


def test_a_flagged_page_holds_every_act_that_touches_it_through_main(tmp_path, monkeypatch):
    """The one consequence of this whole instrument -- `flagged_pages` routing an
    act to `held-for-review`, and gating `confirmed-blank` -- that no test reached
    through `main()` before this one. The skeleton's synthetic Designator can
    never produce a genuinely short region set (see the module docstring), so
    `page_coverage_findings` itself is substituted with one that flags every
    page it finds, on a real run tree built through the real Perlector -- the
    same technique four other test files in this directory already use to load
    this module, applied here to drive the wiring rather than the arithmetic.
    """
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        _invoke(root, "r", "happy", program)

    # `main` now hands the verified sealed-page map down rather than letting
    # each consumer re-derive it, so the substitute takes it and ignores it:
    # this test drives the flagged-page wiring, not the pixel verification the
    # map carries.
    def flags_every_page(context, unused_sealed_pages=None):
        return {ordinal: {"flagged": True} for ordinal in RUN.regions_by_source_page(context)}

    monkeypatch.setattr(RUN, "page_coverage_findings", flags_every_page)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--run-root", str(root), "--run-id", "r", "--scenario", "happy"],
    )
    exit_code = RUN.main()
    assert exit_code == RUN.EXIT_HELD

    tree = RunTree(root, "r")
    reviews = [
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review"
    ]
    assert {review["payload"]["act_key"] for review in reviews} == {"a1", "a2"}
    for review in reviews:
        assert review["outcome"] == "held-for-review"
        assert review["payload"]["page_coverage"]["flagged_pages"]
        assert "carry ink outside every region currently cut" in review["payload"]["reason"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


def test_an_unmeasurable_page_qualifies_an_otherwise_accepted_reason_through_main(
    tmp_path, monkeypatch
):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        _invoke(root, "r", "happy", program)

    def unmeasurable_pages(context, unused_sealed_pages=None):
        return {
            ordinal: {"ink_measurable": False} for ordinal in RUN.regions_by_source_page(context)
        }

    monkeypatch.setattr(RUN, "page_coverage_findings", unmeasurable_pages)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--run-root", str(root), "--run-id", "r", "--scenario", "happy"],
    )
    assert RUN.main() == RUN.EXIT_COMPLETE
    tree = RunTree(root, "r")
    reviews = [
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review"
    ]
    assert {review["outcome"] for review in reviews} == {"accepted"}
    assert all(review["payload"]["page_coverage"]["unmeasurable_pages"] for review in reviews)
    assert all(
        "page ink could not be measured or reconciled" in review["payload"]["reason"]
        for review in reviews
    )


def test_a_page_whose_paper_cannot_be_inferred_is_unmeasurable_and_never_checked(tmp_path):
    """The audit refuses the page rather than reporting zero residual ink on it.

    The page substituted here is the inverted scan
    `pipeline/2_designator/test_structure.py` uses -- 80% at 30, 20% at 220 --
    whose mode is darker than its own mean and whose interior is dark, so no
    branch of the shared inference can call anything on it paper. Before
    2026-09-06 this check would have taken 30 as the paper value, found no pixel
    40 levels below it, and reported the page as carrying no ink outside
    coverage at all: a green coverage proof over a page nobody measured.

    Page 1's bytes are substituted at the read this check makes, with the page
    record's own declared digest moved to match, so the boundary check passes
    and what is exercised is the measurement rather than the verification. The
    sealed blob on disk is left alone: the run tree verifies every artifact
    input when it builds a manifest, and rewriting it would fail there first.
    """
    tree = _built_through_designator(tmp_path)
    context = _FakeContext(tree)
    pages = RUN.sealed_page_images(context)
    relative_path = pages[1]["payload"]["image_path"]

    rows = [bytearray([30] * 100) for _ in range(100)]
    for y in range(80, 100):
        rows[y] = bytearray([220] * 100)
    substituted = encode_grayscale_png(100, 100, rows)
    pages[1]["payload"]["source_sha256"] = digest_bytes(substituted)

    class SubstitutingTree:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def read_bytes(self, path):
            return substituted if path == relative_path else self._inner.read_bytes(path)

    context.tree = SubstitutingTree(tree)
    findings = RUN.page_coverage_findings(context, sealed_pages=pages)
    assert findings[1]["ink_measurable"] is False
    assert findings[1]["named_finding"] == "ink-not-measurable"
    assert "majority ink" in findings[1]["background_refusal"]
    # No count of any kind: not zero ink, no measurement.
    assert "total_ink_pixels" not in findings[1]
    assert "flagged" not in findings[1]

    # Page 2 was not substituted and still measures normally, so the refusal is
    # about the page rather than about the run.
    assert findings[2]["flagged"] is False

    regions = [
        {"payload": {"transform": {"source_page_ordinal": 1}}},
        {"payload": {"transform": {"source_page_ordinal": 2}}},
    ]
    coverage = RUN.page_coverage_for(regions, findings)
    assert coverage == {
        "checked_pages": [2],
        "flagged_pages": [],
        "unmeasurable_pages": [1],
    }


def test_an_unmeasurable_page_confirms_no_witness_pointer_and_authorizes_no_recovery():
    """`None` in the ink map is not `0` and is not a missing artifact.

    Recovery requires independently measured ink outside every current cut. A
    page the Ink Map published as `ink-not-measurable` has no measurement at
    all, so no pointer at it can be confirmed -- and the absence must not be
    read as a missing artifact either, which is a fatal accounting gap with a
    different repair. The page is not lost: every act touching it carries it in
    `page_coverage.unmeasurable_pages`.
    """
    observation = {"bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}

    assert RUN.unclaimed_ink_observations({1: None}, [observation], 1, {}) == []
    with pytest.raises(FatalAccounting, match="no ink-map page-space evidence"):
        RUN.unclaimed_ink_observations({}, [observation], 1, {})


def test_ink_map_by_page_accepts_the_actual_refusal_record_from_the_ink_map(monkeypatch):
    """A real refused PNG crosses the producer/consumer boundary unchanged."""
    ink_map = _load_module("pipeline/1_ink_map/run.py", "ink_map_real_refusal_for_recensor")
    rows = [bytearray([30] * 100) for _ in range(100)]
    for y in range(80, 100):
        rows[y] = bytearray([220] * 100)
    page = {
        "subject_id": "page-1",
        "payload": {"image_path": "page.png", "source_sha256": "0" * 64},
    }
    expected_digest = digest_bytes(DEFAULT_BACKGROUND_CONFIG_PATH.read_bytes())

    class ProducerContext:
        def __init__(self):
            self.tree = object()  # The checked-byte storage boundary is supplied below.
            self.run = {"ingress": {"mode": "synthetic-fixture"}}
            self.args = SimpleNamespace(
                designator_grouping_config=str(DEFAULT_BACKGROUND_CONFIG_PATH)
            )
            self.published = []
            self.sealed_config_digests = {"designator-grouping": expected_digest}
            self.required_configs = []

        def require_sealed_config(self, name, observed_sha256):
            if self.sealed_config_digests.get(name) != observed_sha256:
                raise ContractError(f"sealed {name} digest does not match the producer input")
            self.required_configs.append((name, observed_sha256))

        def input_ref(self, path):
            return {"relative_path": path, "sha256": "0" * 64}

        def publish(self, **record):
            self.published.append(record)

        def seal_boundary(self):
            pass

        def finish(self):
            pass

    producer = ProducerContext()

    class Parser:
        @staticmethod
        def parse_args():
            return SimpleNamespace()

    monkeypatch.setattr(ink_map, "stage_parser", lambda *_args: Parser())
    monkeypatch.setattr(ink_map, "open_stage_context", lambda *_args, **_kwargs: producer)
    monkeypatch.setattr(ink_map, "sealed_pages", lambda _context: [(1, page, "page.json")])
    monkeypatch.setattr(
        ink_map, "measured_page_bytes", lambda *_args: encode_grayscale_png(100, 100, rows)
    )
    assert ink_map.main(registry_factory=None) == ink_map.EXIT_COMPLETE
    (record,) = producer.published
    assert record["outcome"] == "ink-not-measurable"
    assert record["payload"]["background_config_sha256"] == expected_digest
    assert producer.required_configs == [("designator-grouping", expected_digest)]

    class ConsumerTree:
        def build_manifest(self, _stage):
            return {"artifacts": [{"kind": "ink-map", "artifact_id": "page-1"}]}

        def read_artifact(self, _stage, _kind, _artifact_id):
            return record

    context = _FakeContext.__new__(_FakeContext)
    context.tree = ConsumerTree()
    context.run = {"sealed_config_digests": {"designator-grouping": expected_digest}}
    context.args = SimpleNamespace(designator_grouping_config=str(DEFAULT_BACKGROUND_CONFIG_PATH))
    context.required_configs = []
    assert RUN.ink_map_by_page(context) == {1: None}
    assert context.required_configs == [("designator-grouping", expected_digest)]


def test_ink_map_by_page_accepts_the_actual_measured_record_from_the_ink_map(monkeypatch):
    """A producer record proves the measured envelope before the consumer reads runs."""
    ink_map = _load_module("pipeline/1_ink_map/run.py", "ink_map_real_measurement_for_recensor")
    rows = [bytearray([220] * 100) for _ in range(100)]
    for y in range(80, 100):
        rows[y] = bytearray([0] * 100)
    page = {
        "subject_id": "page-1",
        "payload": {"image_path": "page.png", "source_sha256": "0" * 64},
    }
    expected_digest = digest_bytes(DEFAULT_BACKGROUND_CONFIG_PATH.read_bytes())

    class ProducerContext:
        def __init__(self):
            self.tree = object()
            self.run = {"ingress": {"mode": "synthetic-fixture"}}
            self.args = SimpleNamespace(
                designator_grouping_config=str(DEFAULT_BACKGROUND_CONFIG_PATH)
            )
            self.published = []
            self.sealed_config_digests = {"designator-grouping": expected_digest}
            self.required_configs = []

        def require_sealed_config(self, name, observed_sha256):
            if self.sealed_config_digests.get(name) != observed_sha256:
                raise ContractError(f"sealed {name} digest does not match the producer input")
            self.required_configs.append((name, observed_sha256))

        def input_ref(self, path):
            return {"relative_path": path, "sha256": "0" * 64}

        def publish(self, **record):
            self.published.append(record)

        def seal_boundary(self):
            pass

        def finish(self):
            pass

    producer = ProducerContext()

    class Parser:
        @staticmethod
        def parse_args():
            return SimpleNamespace()

    monkeypatch.setattr(ink_map, "stage_parser", lambda *_args: Parser())
    monkeypatch.setattr(ink_map, "open_stage_context", lambda *_args, **_kwargs: producer)
    monkeypatch.setattr(ink_map, "sealed_pages", lambda _context: [(1, page, "page.json")])
    monkeypatch.setattr(
        ink_map, "measured_page_bytes", lambda *_args: encode_grayscale_png(100, 100, rows)
    )
    assert ink_map.main(registry_factory=None) == ink_map.EXIT_COMPLETE
    (record,) = producer.published
    assert record["outcome"] in {"mapped", "unclaimed-edge-ink"}
    assert record["payload"]["ink_measurable"] is True
    assert record["payload"]["background"]["config_sha256"] == expected_digest

    class ConsumerTree:
        def build_manifest(self, _stage):
            return {"artifacts": [{"kind": "ink-map", "artifact_id": "page-1"}]}

        def read_artifact(self, _stage, _kind, _artifact_id):
            return record

    context = _FakeContext.__new__(_FakeContext)
    context.tree = ConsumerTree()
    context.run = {"sealed_config_digests": {"designator-grouping": expected_digest}}
    context.args = SimpleNamespace(designator_grouping_config=str(DEFAULT_BACKGROUND_CONFIG_PATH))
    context.required_configs = []
    assert RUN.ink_map_by_page(context) == {1: record["payload"]["edge_findings"]}
    assert context.required_configs == [("designator-grouping", expected_digest)]

    # The same outcome is not enough: shorten one retained edge run while the
    # producer's published count remains intact. Both versions still flag, but
    # only the original is the measurement the producer made.
    assert record["outcome"] == "unclaimed-edge-ink"
    record = copy.deepcopy(record)
    record["payload"]["edge_findings"]["rows"][-1][0][1] = 40
    with pytest.raises(FatalAccounting, match="does not reconcile with its retained"):
        RUN.ink_map_by_page(context)


def test_ink_map_by_page_refuses_an_unmeasurable_payload_with_a_wrong_seal():
    class RefusalTree:
        def build_manifest(self, _stage):
            return {"artifacts": [{"kind": "ink-map", "artifact_id": "page-1"}]}

        def read_artifact(self, _stage, _kind, _artifact_id):
            return {
                "outcome": "ink-not-measurable",
                "payload": {
                    "page_ordinal": 1,
                    "ink_measurable": False,
                    "background_refusal": "the page is majority ink",
                    "background_config_sha256": "1" * 64,
                },
            }

    context = _FakeContext.__new__(_FakeContext)
    context.tree = RefusalTree()
    context.run = {"sealed_config_digests": {"designator-grouping": "0" * 64}}
    context.required_configs = []
    with pytest.raises(FatalAccounting, match="invalid sealed ink-not-measurable payload"):
        RUN.ink_map_by_page(context)


@pytest.mark.parametrize(
    "defect", ["base-era", "wrong-seal", "missing-background-field", "array-source"]
)
def test_ink_map_by_page_refuses_a_measured_payload_without_current_background_provenance(defect):
    payload = {
        "page_ordinal": 1,
        "ink_measurable": True,
        "background": {
            "background_level": 220,
            "background_source": "inferred-modal",
            "dark_mode": 0,
            "ink_margin": 73,
            "contrast_below_background": 40,
            "ink_threshold": 180,
            "config_sha256": "0" * 64,
        },
        "ink": {},
        "edge": {},
        "edge_findings": {"schema": "ink-runs.v2", "width": 1, "height": 1, "rows": [[]]},
    }
    if defect == "base-era":
        del payload["ink_measurable"]
        del payload["background"]
        del payload["ink"]
        del payload["edge"]
    elif defect == "wrong-seal":
        payload["background"]["config_sha256"] = "1" * 64
    elif defect == "missing-background-field":
        del payload["background"]["ink_threshold"]
    else:
        payload["background"]["background_source"] = []

    class MeasuredTree:
        def build_manifest(self, _stage):
            return {"artifacts": [{"kind": "ink-map", "artifact_id": "page-1"}]}

        def read_artifact(self, _stage, _kind, _artifact_id):
            return {"outcome": "mapped", "payload": payload}

    context = _FakeContext.__new__(_FakeContext)
    context.tree = MeasuredTree()
    context.run = {"sealed_config_digests": {"designator-grouping": "0" * 64}}
    context.required_configs = []
    with pytest.raises(FatalAccounting, match="invalid sealed measured payload"):
        RUN.ink_map_by_page(context)
