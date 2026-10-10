"""Residual-ink page coverage: the wiring, proven against real run trees.

`test_residual_ink.py` proves the pixel-level arithmetic against hand-built
canvases. This proves that `run.py`'s `sealed_page_images` and
`page_coverage_findings` read the real Exemplar artifacts and page pixels, that
the measure fires on genuine pipeline bytes when a reading region is missing,
and that a flagged or unmeasurable page reaches every review through `main()`.

The fixture's ink is painted to match its declared acts
(`proof/synthetic_pages.py`), so no scenario leaves ink outside every reading
region; the tests that need one drop a region or substitute the finding.
"""

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.background import (
    DEFAULT_INK_MAP_CONFIG_PATH,
)
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.stages import RECENSOR
from common.imaging import encode_grayscale_png
from common.page_review import reviewed_rows
from common.runtree.store import RunTree
from common.stage import reading_denominator
from conftest import build_page_tree, load_stage, page_context, programs_through, run_stage

RUN = load_stage("5_recensor")
page_review = load_stage("5_recensor", "page_review")


def _built_through_designator(tmp_path, scenario="happy"):
    root = tmp_path / "runs"
    for program in programs_through("designator"):
        result = run_stage(root, "r", scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return RunTree(root, "r")


@pytest.fixture(scope="module")
def page_tree(tmp_path_factory) -> Path:
    """A `happy` run read by page through the Perlector; copy it before running a stage."""
    root, options = build_page_tree(tmp_path_factory.mktemp("happy"), "happy", reask=None)
    assert not options
    return root


def _copy(page_tree: Path, tmp_path: Path) -> Path:
    shutil.copytree(page_tree, tmp_path / "runs")
    return tmp_path / "runs"


def _reading_regions(root: Path) -> dict[int, list[dict]]:
    """The regions the page review counts as covering each sealed page's ink."""
    context = page_context(root, "r", "happy", {})
    denominator = reading_denominator(context)
    return page_review.reading_regions_by_page(
        context, denominator["pages"], reviewed_rows(denominator["acts"])
    )


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
        self.args = SimpleNamespace(ink_map_config=str(DEFAULT_INK_MAP_CONFIG_PATH))
        self.required_configs = []

    def require_sealed_config(self, name, observed_sha256):
        if self.run["sealed_config_digests"].get(name) != observed_sha256:
            raise ContractError(f"sealed {name} digest does not match the consumer input")
        self.required_configs.append((name, observed_sha256))


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


def test_the_residual_ink_check_refuses_page_bytes_it_did_not_verify(page_tree):
    """`sealed_page_images` verifies each page's pixels; `page_coverage_findings`
    then reads that path AGAIN to measure it. Two reads of one path is a check
    followed by a use of something else, and only the second read's bytes are
    ever measured -- so those are the bytes that have to carry the page's own
    digest.

    A single-process test cannot land a writer between the two reads, so the
    race is modelled: this tree is honest on every read the verification makes
    and returns a different page on the second read of the same path, which
    must be refused rather than measured."""
    real = RunTree(page_tree, "r")

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
    with pytest.raises(FatalAccounting, match="changed under a sealed reference"):
        RUN.page_coverage_findings(context, regions=_reading_regions(page_tree))


def test_a_page_whose_paper_cannot_be_inferred_is_unmeasurable_and_never_checked(
    page_tree, monkeypatch
):
    """The audit refuses the page rather than reporting zero residual ink on it.

    The page substituted here is an inverted scan -- 80% at 30, 20% at 220 --
    whose mode is darker than its own mean and whose interior is dark, so no
    branch of the shared inference can call anything on it paper. Taking 30 as
    the paper value would find no pixel 40 levels below it and report the page
    clean: a coverage proof over a page nobody measured.

    Page 1's bytes are substituted at the read this check makes, with the page
    record's own declared digest moved to match, so the boundary check passes
    and what is exercised is the measurement rather than the verification. The
    sealed blob on disk is left alone: the run tree verifies every artifact
    input when it builds a manifest, and rewriting it would fail there first.
    """
    tree = RunTree(page_tree, "r")
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
    monkeypatch.setattr(RUN, "sealed_page_images", lambda _context: pages)
    findings = RUN.page_coverage_findings(context, regions=_reading_regions(page_tree))
    assert findings[1]["ink_measurable"] is False
    assert findings[1]["named_finding"] == "ink-not-measurable"
    assert "majority ink" in findings[1]["background_refusal"]
    # No count of any kind: not zero ink, no measurement.
    assert "total_ink_pixels" not in findings[1]
    assert "flagged" not in findings[1]

    # Page 2 was not substituted and still measures normally, so the refusal is
    # about the page rather than about the run.
    assert findings[2]["flagged"] is False
    assert page_review.page_coverage_of(1, findings)["unmeasurable_pages"] == [1]
    assert page_review.page_coverage_of(2, findings)["checked_pages"] == [2]


def test_page_coverage_findings_does_not_flag_the_real_fully_covered_fixture(page_tree):
    """The fixture's ink lies inside its reading regions, so the check does not
    misfire on ordinary, fully accounted-for pages."""
    findings = RUN.page_coverage_findings(
        _FakeContext(RunTree(page_tree, "r")), regions=_reading_regions(page_tree)
    )
    assert set(findings) == {1, 2}
    for ordinal, finding in findings.items():
        assert finding["flagged"] is False, f"page {ordinal}: {finding}"
        assert finding["outside_ink_pixels"] == 0


def test_a_missing_reading_region_flags_real_pipeline_pixels(page_tree):
    """Real sealed page-1 bytes with one reading region left out of the covered
    set: the evidence a reading that missed an act would leave behind."""
    context = _FakeContext(RunTree(page_tree, "r"))
    regions = _reading_regions(page_tree)
    assert len(regions[1]) > 1

    short = RUN.page_coverage_findings(context, regions={**regions, 1: regions[1][1:]})
    assert short[1]["flagged"] is True and short[1]["outside_ink_pixels"] > 0
    assert short[2]["flagged"] is False
    # The full set clears it, on the identical bytes.
    assert RUN.page_coverage_findings(context, regions=regions)[1]["flagged"] is False


def _run_main(root: Path, monkeypatch) -> int:
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--run-root", str(root), "--run-id", "r", "--scenario", "happy"],
    )
    return RUN.main()


def _reviews(root: Path) -> list[dict]:
    tree = RunTree(root, "r")
    return [
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review"
    ]


def _substituted(monkeypatch, change) -> None:
    """Replace `page_coverage_findings` with the real measure, each finding changed."""
    measure = RUN.page_coverage_findings

    def substitute(context, *, regions):
        return {
            ordinal: change(finding)
            for ordinal, finding in measure(context, regions=regions).items()
        }

    monkeypatch.setattr(RUN, "page_coverage_findings", substitute)


def _flagged(finding: dict) -> dict:
    return {**finding, "flagged": True}


def test_a_flagged_page_flags_every_unit_on_it_through_main(page_tree, tmp_path, monkeypatch):
    """Residual ink is a review flag under the committed `[flags]`: named on every unit of
    the page, placed in the queue, holding nothing (`common.page_accounting`)."""
    root = _copy(page_tree, tmp_path)
    _substituted(monkeypatch, _flagged)
    assert _run_main(root, monkeypatch) == RUN.EXIT_COMPLETE

    reviews = _reviews(root)
    assert reviews
    for review in reviews:
        assert review["outcome"] == "accepted"
        assert review["payload"]["page_coverage"]["flagged_pages"]
        assert page_review.RESIDUAL_INK in review["payload"]["flag_codes"]
        assert page_review.RESIDUAL_INK not in review["payload"]["hold_codes"]
        assert review["payload"]["review_priority"] == 1
        assert "flagged for review, not held" in review["payload"]["reason"]


def test_a_second_pass_that_clears_a_flag_does_not_collide_with_the_first(
    page_tree, tmp_path, monkeypatch
):
    """A review's content can change between passes while its unit does not.

    Pass 1 flags every page's ink (a review flag, so the units are accepted
    with it); pass 2 measures honestly. Each unit's second review is minted at
    a fresh ordinal rather than republished under the first's identity, which
    the immutable writer would refuse; a third, identical pass mints nothing.
    """
    root = _copy(page_tree, tmp_path)
    _substituted(monkeypatch, _flagged)
    assert _run_main(root, monkeypatch) == RUN.EXIT_COMPLETE
    monkeypatch.undo()
    assert _run_main(root, monkeypatch) == RUN.EXIT_COMPLETE

    by_unit: dict[str, dict[int, dict]] = {}
    for review in _reviews(root):
        by_unit.setdefault(review["subject_id"], {})[review["payload"]["attempt_ordinal"]] = review
    assert by_unit
    for unit, reviews in by_unit.items():
        assert sorted(reviews) == [1, 2], f"unit {unit}: {sorted(reviews)}"
        assert reviews[1]["outcome"] == "accepted"
        assert reviews[1]["payload"]["flag_codes"] == [page_review.RESIDUAL_INK]
        assert reviews[2]["outcome"] == "accepted"
        assert reviews[2]["payload"]["flag_codes"] == []
        assert reviews[2]["payload"]["page_coverage"]["flagged_pages"] == []

    before = {review["artifact_id"] for review in _reviews(root)}
    assert _run_main(root, monkeypatch) == RUN.EXIT_COMPLETE
    assert {review["artifact_id"] for review in _reviews(root)} == before


def test_an_unmeasurable_page_holds_every_unit_on_it_through_main(page_tree, tmp_path, monkeypatch):
    root = _copy(page_tree, tmp_path)
    _substituted(
        monkeypatch,
        lambda finding: {
            "ink_measurable": False,
            "named_finding": "ink-not-measurable",
            "background_refusal": "no paper value",
            "background_config_sha256": finding["background_config_sha256"],
        },
    )
    assert _run_main(root, monkeypatch) == RUN.EXIT_HELD

    reviews = _reviews(root)
    assert reviews
    for review in reviews:
        assert review["outcome"] == "held-for-review"
        assert review["payload"]["page_coverage"]["unmeasurable_pages"]
        assert page_review.RESIDUAL_INK_NOT_MEASURABLE in review["payload"]["hold_codes"]
