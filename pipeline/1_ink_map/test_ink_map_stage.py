"""The early ink map is evidence over pages, before any proposal exists."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.background import DEFAULT_BACKGROUND_CONFIG_PATH
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


def _policy(width: int, height: int):
    """This page's own resolved background policy, from the shipped sealed file.

    Every measure in this module needs one since 2026-09-06: the ink predicate is
    taken below a background the shared inference derives, and no call site is
    allowed a default policy -- a stage measuring under a policy nobody sealed is
    what this argument exists to prevent.
    """
    from common.background import load_background_config, resolve_background_policy

    return resolve_background_policy(load_background_config(), width, height)


def _coverage(width: int, height: int):
    """This page's own resolved coverage-audit policy, from the shipped file.

    The companion to `_policy`, and required at the same call sites for the same
    reason: since 2026-09-06 the two outside-coverage gates and the perimeter
    band are fractions of the page sealed in `[coverage_audit]`, and no call site
    is allowed a default.
    """
    from common.residual_ink import load_coverage_audit_config, resolve_coverage_audit_policy

    return resolve_coverage_audit_policy(load_coverage_audit_config(), width, height)


def test_early_map_and_late_reconciliation_import_one_measure():
    assert INK_MAP_RUN.residual_ink is RECENSOR_RUN.residual_ink


def test_unclaimed_edge_ink_is_named_and_bounded_but_not_held():
    rows = [bytearray([230] * 200) for _ in range(200)]
    for y in range(10):
        rows[y][10:30] = bytes([170] * 20)
    finding = INK_MAP_RUN.edge_ink(
        200, 200, rows, background_policy=_policy(200, 200), coverage_policy=_coverage(200, 200)
    )
    assert finding["flagged"] is True
    assert finding["named_finding"] == "unclaimed-edge-ink"
    # 2 pixels on a 200-pixel-square page: `edge_band_bp` is a fraction of the
    # page's own shorter side since 2026-09-06, where 64 was a flat count that
    # made a third of this page its own perimeter.
    assert finding["edge_band_pixels"] == _coverage(200, 200)["edge_band_px"] == 2
    assert classify(INK_MAP, "unclaimed-edge-ink") is OutcomeClass.UNRESOLVED
    assert terminal_category(INK_MAP, "unclaimed-edge-ink") is None
    handoff = (ROOT / "pipeline/1_ink_map/HANDOFF.md").read_text(encoding="utf-8")
    assert "Unit 14 owns the explicit hold outcome" in handoff


def test_the_measured_and_unmeasured_ink_thresholds_are_told_apart_by_name():
    """Two of this audit's five numbers are calibrated now; three are not.

    The module used to say PROPOSED-NOT-MEASURED of all of them and the handoff
    used to say the stage claimed no calibration, and both were true. On
    2026-09-06 the two that are *lengths* -- the absolute outside-coverage gate
    and the perimeter band -- were measured on 44 real pages and sealed in
    `[coverage_audit]`, with `calibrated_for_this_corpus = true` and their own
    provenance block. The three that are not lengths were not:
    `MINIMUM_INK_PIXELS`, `MINIMUM_CONTRAST_BELOW_BACKGROUND` and
    `MINIMUM_FRACTION_OUTSIDE_COVERAGE` are still reasoned defaults.

    So the claim this test protects has changed shape rather than gone away: the
    module must still carry the unmeasured banner over the three it applies to,
    and the sealed block must still carry the calibration claim over the two it
    applies to. A later edit that widened either would have to move this test.
    """
    import tomllib

    source = " ".join((ROOT / "common/residual_ink.py").read_text(encoding="utf-8").split())
    handoff = " ".join(
        (ROOT / "pipeline/1_ink_map/HANDOFF.md").read_text(encoding="utf-8").split()
    ).lower()

    assert "PROPOSED, NOT YET MEASURED" in source
    assert "proposed, not yet measured" in handoff

    config = tomllib.loads((ROOT / "config/designator_grouping.toml").read_bytes().decode("utf-8"))
    provenance = config["coverage_audit"]["provenance"]
    assert provenance["calibrated_for_this_corpus"] is True
    assert provenance["sample_count"] == 44
    # The claim is bounded by its own caveat, which is what keeps "calibrated"
    # from being read as "calibrated for the corpus this pipeline will run on".
    assert "WHAT THE SAMPLE DOES NOT ESTABLISH" in provenance["caveat"]
    # The handoff must keep saying which two moved and which three did not,
    # so a reader of the stage interface is not left to infer it from the file
    # the gates now live in.
    assert "sample_count = 44" in handoff


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
    """The edge width is a bounded instrument, and it is a fraction of the page.

    It was the flat 64 pixels until 2026-09-06 and is `edge_band_bp` in the
    sealed `[coverage_audit]` block now, resolved against the page's own shorter
    side. Both halves are pinned here: the module still says what the band is
    for and does not claim it is a calibrated cross-page-act threshold, and the
    resolution really is proportional -- the same sealed value gives 2 pixels on
    this repository's 200x260 fixture and 36 on a 3,600-pixel leaf, where the
    retired constant gave 64 on both.
    """
    from common.residual_ink import (
        EDGE_BAND_BP_FIELD,
        load_coverage_audit_config,
        resolve_coverage_audit_policy,
    )

    source = (ROOT / "common/residual_ink.py").read_text(encoding="utf-8")
    normalised = " ".join(source.replace("#", " ").split())
    assert "instrument boundary rather than a calibrated cross-page-act threshold" in normalised

    config = load_coverage_audit_config()
    assert config["coverage_audit"][EDGE_BAND_BP_FIELD] == 100
    assert resolve_coverage_audit_policy(config, 200, 260)["edge_band_px"] == 2
    assert resolve_coverage_audit_policy(config, 3853, 3600)["edge_band_px"] == 36
    # The floor under the smallest legal image, which is why the band can never
    # resolve to zero however small the page is.
    assert resolve_coverage_audit_policy(config, 1, 100)["edge_band_px"] == 1


def test_a_page_with_no_ink_at_all_still_measures_clean_rather_than_flagging():
    """A zero-ink page still needs evidence, but must not manufacture an alarm."""
    rows = [bytearray([230] * 200) for _ in range(200)]
    policy = _policy(200, 200)
    audit = _coverage(200, 200)
    edge = INK_MAP_RUN.edge_ink(200, 200, rows, background_policy=policy, coverage_policy=audit)
    ink = INK_MAP_RUN.residual_ink(
        200, 200, rows, [], background_policy=policy, coverage_policy=audit
    )

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
        # The stage reads its background policy from the path its own parsed
        # argv names and proves the bytes against the run's seal, exactly as the
        # Designator does. A stub without these two would be testing a stage
        # that skipped both, which is the drift this stub's own comment warns
        # about.
        self.args = SimpleNamespace(designator_grouping_config=str(DEFAULT_BACKGROUND_CONFIG_PATH))
        self.required_configs = []

    def require_sealed_config(self, name, observed_sha256):
        self.required_configs.append((name, observed_sha256))

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

    edge = INK_MAP_RUN.edge_ink(
        1, 100, rows, background_policy=_policy(1, 100), coverage_policy=_coverage(1, 100)
    )

    assert edge["edge_band_pixels"] == 1
    assert edge["total_ink_pixels"] == 25
    assert edge["outside_ink_pixels"] == 25
    assert edge["flagged"] is True


def test_the_fixture_pages_stop_flagging_because_the_band_stopped_being_the_page():
    """The retired band's flag on a fixture page was an artefact, and it is gone.

    A 64-pixel band on a 200x260 page left a 72x132 centre: a third of each
    dimension was "perimeter", so both pinned scenarios flagged every page and
    no run in the suite produced `mapped` at all. Measured here, that flag was
    the page's own body text being counted as edge ink -- these pages carry
    **zero** ink in every band from 1 pixel to 20, and only at 64 does the band
    reach the writing. `edge_band_bp` resolves to 2 pixels here, so both pages
    are now `mapped`.

    **The cost is named rather than hidden: no fixture scenario exercises the
    `unclaimed-edge-ink` outcome end to end any more.** That path is covered by
    this module's own unit tests, by `pipeline/5_recensor/test_residual_ink.py`
    and by `pipeline/7_armarium/test_unit14b_edge_release.py`, all of which build
    the flagged shape directly. What no run tree in this repository now proves is
    the release travelling from the Ink Map through the Designator's cuts to the
    Armarium on a real scenario. It is a real gap and it belongs to the fixture,
    which has no page with ink near its edge; it is written into the Ink Map's
    HANDOFF.md beside this test.
    """
    from common.imaging import dimensions
    from common.residual_ink import page_edge_ink
    from proof.synthetic_pages import page_bytes

    width, height = dimensions(page_bytes(1))
    band = _coverage(width, height)["edge_band_px"]
    assert (width, height) == (200, 260)
    assert band == 2
    centre = (width - 2 * band) * (height - 2 * band)
    assert centre * 100 > width * height * 95, (
        "the fixture page's perimeter is no longer a thin strip; the ink map handoff "
        "says it is, and the edge findings on a fixture run mean something else"
    )
    for ordinal in (1, 2):
        finding = page_edge_ink(
            page_bytes(ordinal),
            background_policy=_policy(width, height),
            coverage_policy=_coverage(width, height),
        )
        assert finding["outside_ink_pixels"] == 0
        assert finding["flagged"] is False


def test_the_stage_proves_the_background_policy_bytes_against_the_runs_own_seal():
    """The policy is read from the parsed argv and checked, not just read.

    Three stages now infer a page's paper value under the same sealed block. If
    this one read a file the run never bound, its counts would be taken at a
    threshold no other stage's record could be compared against -- the drift the
    whole point-of-use recheck family exists to catch.
    """
    from common.background import load_background_config
    from common.residual_ink import load_coverage_audit_config

    blank = encode_grayscale_png(200, 200, [bytearray([230] * 200) for _ in range(200)])
    page = _sealed_page(1)
    context = _PublishingContext()

    class _Parser:
        @staticmethod
        def parse_args():
            return SimpleNamespace()

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(INK_MAP_RUN, "stage_parser", lambda *_args: _Parser())
        monkeypatch.setattr(INK_MAP_RUN, "open_stage_context", lambda *_a, **_k: context)
        monkeypatch.setattr(
            INK_MAP_RUN,
            "sealed_pages",
            lambda _context: [(1, page, "1_exemplar/artifacts/page/page.json")],
        )
        monkeypatch.setattr(INK_MAP_RUN, "measured_page_bytes", lambda *_args: blank)
        assert INK_MAP_RUN.main(registry_factory=None) == INK_MAP_RUN.EXIT_COMPLETE

    # Twice, and deliberately: the stage reads `[grouping.background]` and
    # `[coverage_audit]` through two loaders, and each one proves the bytes IT
    # read against the run's seal. One check standing for both would leave the
    # second loader's read unproved on a file that had changed between them.
    assert context.required_configs == [
        ("designator-grouping", load_background_config()["config_sha256"]),
        ("designator-grouping", load_coverage_audit_config()["config_sha256"]),
    ]
    background = context.published[0]["payload"]["background"]
    assert background["config_sha256"] == load_background_config()["config_sha256"]
    # GOVERNANCE 6, as fields rather than as a sentence: the paper value, where
    # it came from, the level this stage measured at, and the derived margin the
    # Designator will measure the same page at.
    assert background["background_level"] == 230
    assert background["background_source"] == "inferred-modal"
    assert background["contrast_below_background"] == 40
    assert background["ink_threshold"] == 190
    assert background["dark_mode"] == 230
    assert background["ink_margin"] == 20
    # Published once, not on each finding: two copies of one page's paper value
    # is how two copies come to disagree.
    assert "background" not in context.published[0]["payload"]["ink"]
    assert "background" not in context.published[0]["payload"]["edge"]


def test_a_page_whose_paper_cannot_be_inferred_is_named_rather_than_mapped(monkeypatch):
    """`ink-not-measurable`: in the census, with no counts and no retained runs.

    The page is the inverted scan `pipeline/2_designator/test_structure.py`
    uses -- 80% at 30, 20% at 220 -- whose mode is darker than its own mean and
    whose interior is dark, so no branch can call anything on it paper. Before
    2026-09-06 this stage would have taken 30 as the paper value, found no pixel
    40 levels below it, and published `mapped` with `total_ink_pixels: 0`: a
    page reported clean because its threshold could not be reached.

    What is asserted here is what the record does *not* carry as much as what it
    does. `ink`, `edge` and `edge_findings` are absent, not zeroed, so the
    Armarium's re-measurement and the Recensor's pointer confirmation both fail
    loudly on a consumer that assumed them rather than reading a zero nobody
    measured.
    """
    rows = [bytearray([30] * 100) for _ in range(100)]
    for y in range(80, 100):
        rows[y] = bytearray([220] * 100)
    page = _sealed_page(1)
    context = _PublishingContext()

    class _Parser:
        @staticmethod
        def parse_args():
            return SimpleNamespace()

    monkeypatch.setattr(INK_MAP_RUN, "stage_parser", lambda *_args: _Parser())
    monkeypatch.setattr(INK_MAP_RUN, "open_stage_context", lambda *_a, **_k: context)
    monkeypatch.setattr(
        INK_MAP_RUN,
        "sealed_pages",
        lambda _context: [(1, page, "1_exemplar/artifacts/page/page.json")],
    )
    monkeypatch.setattr(
        INK_MAP_RUN, "measured_page_bytes", lambda *_args: encode_grayscale_png(100, 100, rows)
    )

    assert INK_MAP_RUN.main(registry_factory=None) == INK_MAP_RUN.EXIT_COMPLETE
    (record,) = context.published
    assert record["outcome"] == "ink-not-measurable"
    payload = record["payload"]
    assert payload["ink_measurable"] is False
    assert payload["page_ordinal"] == 1
    assert "majority ink" in payload["background_refusal"]
    assert set(payload) == {
        "page_ordinal",
        "ink_measurable",
        "background_refusal",
        "background_config_sha256",
    }
    # The census still closes: the page is present, and the boundary is sealed.
    assert context.sealed is True
    assert context.finished is True
