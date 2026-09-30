"""The early ink map is evidence over pages, before any proposal exists."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.background import DEFAULT_BACKGROUND_CONFIG_PATH
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ApprovalRefusal, ContractError, FatalAccounting
from common.contracts.outcomes import OutcomeClass, classify, terminal_category
from common.contracts.stages import DESIGNATOR, EXEMPLAR, INK_MAP
from common.imaging import encode_grayscale_png
from common.residual_ink import ink_map_page, residual_ink
from common.runtree.store import RunTree
from conftest import load_stage
from operations.submit import gate, submit

ROOT = Path(__file__).resolve().parents[2]


INK_MAP_RUN = load_stage("1_ink_map")
RECENSOR_RUN = load_stage("5_recensor")


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

    Every measure in this module needs one: the ink predicate is
    taken below a background the shared inference derives, and no call site is
    allowed a default policy -- a stage measuring under a policy nobody sealed is
    what this argument exists to prevent.
    """
    from common.background import load_background_config, resolve_background_policy

    return resolve_background_policy(load_background_config(), width, height)


def _coverage(width: int, height: int):
    """This page's own resolved coverage-audit policy, from the shipped file.

    The companion to `_policy`, and required at the same call sites for the same
    reason: the two outside-coverage gates and the perimeter
    band are fractions of the page sealed in `[coverage_audit]`, and no call site
    is allowed a default.
    """
    from common.residual_ink import load_coverage_audit_config, resolve_coverage_audit_policy

    return resolve_coverage_audit_policy(load_coverage_audit_config(), width, height)


def test_early_map_and_late_reconciliation_import_one_measure():
    import common.residual_ink

    assert INK_MAP_RUN.ink_map_page is common.residual_ink.ink_map_page
    assert RECENSOR_RUN.residual_ink is common.residual_ink.residual_ink


def test_unclaimed_edge_ink_is_named_and_bounded_but_not_held():
    rows = [bytearray([230] * 200) for _ in range(200)]
    for y in range(10):
        rows[y][10:30] = bytes([170] * 20)
    finding = ink_map_page(
        200, 200, rows, background_policy=_policy(200, 200), coverage_policy=_coverage(200, 200)
    )["edge"]
    assert finding["flagged"] is True
    assert finding["named_finding"] == "unclaimed-edge-ink"
    # 2 pixels on a 200-pixel-square page: `edge_band_bp` is a fraction of the
    # page's own shorter side, not a flat count -- a flat count of 64 would make
    # a third of this page its own perimeter.
    assert finding["edge_band_pixels"] == _coverage(200, 200)["edge_band_px"] == 2
    assert classify(INK_MAP, "unclaimed-edge-ink") is OutcomeClass.UNRESOLVED
    assert terminal_category(INK_MAP, "unclaimed-edge-ink") is None


def test_only_the_area_gate_and_the_perimeter_band_carry_a_calibration_claim():
    """The sealed provenance says which audit values were measured and which were not.

    The area gate (`substantial_ink_area_bp`) and the perimeter band
    (`edge_band_bp`) were measured on 44 real pages and sealed under
    `[coverage_audit.provenance]` with `calibrated_for_this_corpus = true`. The
    noise floor and fraction gate are reasoned defaults and sit under a
    provenance block of their own that denies the claim, so the first block's
    claim cannot be read as covering them.
    """
    import tomllib

    config = tomllib.loads((ROOT / "config/designator_grouping.toml").read_bytes().decode("utf-8"))
    provenance = config["coverage_audit"]["provenance"]
    assert provenance["calibrated_for_this_corpus"] is True
    assert provenance["sample_count"] == 44
    # The claim is bounded by its own caveat, which is what keeps "calibrated"
    # from being read as "calibrated for the corpus this pipeline will run on".
    assert "WHAT THE SAMPLE DOES NOT ESTABLISH" in provenance["caveat"]
    noise_floor = config["coverage_audit"]["noise_floor"]["provenance"]
    assert noise_floor["calibrated_for_this_corpus"] is False
    assert noise_floor["sample_count"] == 0


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
        if relative_path not in self._blobs:
            raise FileNotFoundError(relative_path)
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
    ink map for a run it never measured, which is silent loss
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
    with pytest.raises(FatalAccounting, match="changed under a sealed reference"):
        INK_MAP_RUN.measured_page_bytes(swapped, 1, page)
    with pytest.raises(FatalAccounting, match="could not be read"):
        INK_MAP_RUN.measured_page_bytes(_StubTree([page]), 1, page)


def test_the_ink_map_declares_the_decode_route_it_actually_takes():
    """One implementation may not declare two routes.

    The map and the Recensor's late reconciliation both decode through
    `common.imaging.grayscale_rows`, so a stage seal claiming a different route
    family for one of them is a false statement about its own pass and makes
    the decode-environment census report drift that is not there.
    """
    import common.imaging
    from common.contracts.stages import RECENSOR
    from common.stage import _decode_environment

    assert INK_MAP_RUN.grayscale_rows is common.imaging.grayscale_rows
    assert RECENSOR_RUN.grayscale_rows is common.imaging.grayscale_rows
    ink_map = _decode_environment(INK_MAP)
    assert ink_map["decode_paths_used"] == _decode_environment(RECENSOR)["decode_paths_used"]
    assert ink_map["decode_paths_used"] == ["project-png"]
    assert ink_map["produced_pixels"] is True


def test_the_edge_band_is_a_fraction_of_the_pages_shorter_side():
    """The edge width is a bounded instrument, and it is a fraction of the page.

    It is `edge_band_bp` in the
    sealed `[coverage_audit]` block, resolved against the page's own shorter
    side, not a flat pixel count: the same sealed value gives 2 pixels on
    this repository's 200x260 fixture and 36 on a 3,600-pixel leaf, where a flat
    count would give 64 on both.
    """
    from common.residual_ink import (
        EDGE_BAND_BP_FIELD,
        load_coverage_audit_config,
        resolve_coverage_audit_policy,
    )

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
    edge = ink_map_page(200, 200, rows, background_policy=policy, coverage_policy=audit)["edge"]
    ink = residual_ink(200, 200, rows, [], background_policy=policy, coverage_policy=audit)

    assert edge["flagged"] is False
    assert ink["total_ink_pixels"] == 0
    assert classify(INK_MAP, "mapped") is OutcomeClass.COMPLETED


# One definition of each stub, used by every test that drives `main`: a copy
# nobody updates keeps testing an old `publish`/`seal_boundary` contract while
# still passing.
class _Parser:
    def __init__(self, args=None):
        self._args = SimpleNamespace() if args is None else args

    def parse_args(self):
        return self._args


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


def _drive_main(monkeypatch, pages, context=None):
    """Run `main` over `pages`, `(ordinal, image bytes)` pairs, with a stub context.

    Returns the context so a test can read what was published and sealed.
    """
    context = _PublishingContext() if context is None else context
    images = dict(pages)
    monkeypatch.setattr(INK_MAP_RUN, "stage_parser", lambda *_args: _Parser())
    monkeypatch.setattr(INK_MAP_RUN, "open_stage_context", lambda *_a, **_k: context)
    monkeypatch.setattr(
        INK_MAP_RUN,
        "sealed_pages",
        lambda _context: [
            (ordinal, _sealed_page(ordinal), f"1_exemplar/artifacts/page/{ordinal}.json")
            for ordinal, _ in pages
        ],
    )
    monkeypatch.setattr(
        INK_MAP_RUN, "measured_page_bytes", lambda _tree, ordinal, _page: images[ordinal]
    )
    return context


def _blank(width=200, height=200):
    return encode_grayscale_png(width, height, [bytearray([230] * width) for _ in range(height)])


def test_the_ink_map_opens_both_ingress_routes_through_the_shared_constructor(monkeypatch):
    """The stage opens its context only through `common.stage.open_stage_context`.

    `main` hands the parsed argv, its own stage name and the registry factory
    it was given to the shared constructor, which decides the route from one
    read of the run authority and applies the same direct-entry guards --
    register drift, the sealed snapshot, the Exemplar seal, the run-level cap
    -- on both.
    """
    context = _drive_main(monkeypatch, [(1, _blank())])
    args = SimpleNamespace(run_root="unused", run_id="r")
    opened = []

    def registry_factory(_path):
        raise AssertionError("the stage must pass the factory through, not resolve it")

    def open_stage_context(observed_args, stage, *, registry_factory):
        opened.append((observed_args, stage, registry_factory))
        return context

    monkeypatch.setattr(INK_MAP_RUN, "stage_parser", lambda *_args: _Parser(args))
    monkeypatch.setattr(INK_MAP_RUN, "open_stage_context", open_stage_context)

    assert INK_MAP_RUN.main(registry_factory=registry_factory) == INK_MAP_RUN.EXIT_COMPLETE
    assert opened == [(args, INK_MAP, registry_factory)]


def test_the_ink_map_refuses_a_run_whose_ingress_evidence_names_no_route(monkeypatch):
    """A run with no `ingress` record is refused before any page is published.

    `open_stage_context`'s own route test, `is_real_ingress`, treats a missing
    `ingress` key as synthetic by design -- it has to, to decide which route to
    build -- and does not raise. This stage never branches on the route, so
    `main` re-parses the record for its refusal alone.
    """
    context = _drive_main(monkeypatch, [(1, _blank())], _PublishingContext(run={}))

    with pytest.raises(ApprovalRefusal, match="closed fixture-or-real record"):
        INK_MAP_RUN.main(registry_factory=None)
    assert context.published == [], "no record may be published before the route is proved"


def test_a_page_with_no_ink_is_published_as_mapped(monkeypatch):
    context = _drive_main(monkeypatch, [(1, _blank())])

    # `EXIT_COMPLETE`, not a literal 0: a literal that stops matching the
    # constant would let this assertion pass for the wrong reason.
    assert INK_MAP_RUN.main(registry_factory=None) == INK_MAP_RUN.EXIT_COMPLETE
    assert [record["outcome"] for record in context.published] == ["mapped"]
    assert context.sealed is True
    assert context.finished is True


def test_a_page_with_ink_in_its_perimeter_is_published_as_unclaimed_edge_ink(monkeypatch):
    """The record's outcome is the edge finding's own flag, not a constant."""
    rows = [bytearray([230] * 200) for _ in range(200)]
    for y in range(10):
        rows[y][10:30] = bytes([170] * 20)
    context = _drive_main(monkeypatch, [(1, encode_grayscale_png(200, 200, rows))])

    assert INK_MAP_RUN.main(registry_factory=None) == INK_MAP_RUN.EXIT_COMPLETE
    (record,) = context.published
    assert record["outcome"] == "unclaimed-edge-ink"
    assert record["payload"]["edge"]["flagged"] is True
    assert context.sealed is True


def test_the_ink_map_refuses_a_page_whose_verified_pixels_will_not_decode(monkeypatch):
    """A digest-verified page this module's own decoder still cannot read is a
    named refusal, not a bare traceback.

    `measured_page_bytes` proves the bytes match the digest the Exemplar sealed;
    it says nothing about whether `common.imaging.grayscale_rows`, the decode
    `main` runs, can read them. `run_stage` only catches `RunHalted` and
    `ContractError` (`common/stage.py`), so an uncaught decoder `ValueError`
    here would escape as an unhandled traceback with `seal_boundary`/`finish`
    never reached.
    """
    context = _drive_main(monkeypatch, [(1, b"not an image")])

    with pytest.raises(FatalAccounting, match="cannot measure sealed Exemplar page 1"):
        INK_MAP_RUN.main(registry_factory=None)

    assert context.published == []
    assert context.sealed is False
    assert context.finished is False


def test_a_page_with_no_settled_grey_reading_is_refused_as_policy_not_damage(monkeypatch):
    """A transparent page decodes; the pipeline has no settled way to read it as grey.

    `common.imaging` raises its policy refusal for it, which is not a decode
    failure, and the stage's refusal must say which one it is so the operator
    does not go looking for damaged bytes.
    """
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGBA", (20, 20), (230, 230, 230, 128)).save(buffer, format="PNG")
    context = _drive_main(monkeypatch, [(1, buffer.getvalue())])

    with pytest.raises(FatalAccounting, match="no settled policy for reading its pixels") as caught:
        INK_MAP_RUN.main(registry_factory=None)

    assert "do not decode" not in str(caught.value)
    assert context.published == []
    assert context.sealed is False


def test_the_undecodable_page_refusal_does_not_claim_an_empty_run_tree(monkeypatch):
    """The sibling above fails on page 1; here page 1 decodes and page 2 does not.

    Publication is inside the page loop, so a decode failure part-way through
    a shard leaves the earlier pages' records on disk. The refusal must not
    claim "no ink-map record was written" -- an operator would read that as a
    clean tree and act on a false picture of what is there. The boundary is
    still unsealed, so nothing downstream proceeds either way; this pins the
    refusal's wording against the records that actually exist.
    """
    context = _drive_main(monkeypatch, [(1, _blank(20, 20)), (2, b"not an image")])

    with pytest.raises(FatalAccounting, match="cannot measure sealed Exemplar page 2") as caught:
        INK_MAP_RUN.main(registry_factory=None)

    message = str(caught.value)
    assert "incomplete map" in message
    assert "no ink-map record was written" not in message, (
        "the refusal claimed an empty tree while page 1's record was already published"
    )
    assert [record["subject_id"] for record in context.published] == ["page-1"]
    assert context.sealed is False
    assert context.finished is False


def test_a_measure_that_omits_its_fraction_is_refused_by_name_not_by_key_error():
    """A missing key and a wrong type are the same contract break, reported alike:

    a shared measure that stops emitting `fraction_outside` must not surface as
    a bare `KeyError` with no stage, page, or contract named, while the same
    measure emitting a string gets a clean refusal.
    """
    complete = {
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

    edge = ink_map_page(
        1, 100, rows, background_policy=_policy(1, 100), coverage_policy=_coverage(1, 100)
    )["edge"]

    assert edge["edge_band_pixels"] == 1
    assert edge["total_ink_pixels"] == 25
    assert edge["outside_ink_pixels"] == 25
    assert edge["flagged"] is True


def test_the_fixture_pages_carry_no_ink_in_the_sealed_perimeter_band():
    """The sealed band is a thin strip on the fixture, and the fixture's ink is inside it.

    `edge_band_bp` resolves to 2 pixels on the 200x260 fixture pages, and their
    painted acts start 20 pixels in, so both pages map as `mapped`. The
    `unclaimed-edge-ink` outcome is exercised by
    `test_a_real_submission_names_edge_ink_and_an_unmeasurable_page` below and
    by the pages the Recensor and Armarium tests build for it.
    """
    from common.imaging import dimensions, grayscale_rows
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
        finding = ink_map_page(
            *grayscale_rows(page_bytes(ordinal)),
            background_policy=_policy(width, height),
            coverage_policy=_coverage(width, height),
        )["edge"]
        assert finding["outside_ink_pixels"] == 0
        assert finding["flagged"] is False


def test_the_stage_proves_the_background_policy_bytes_against_the_runs_own_seal(monkeypatch):
    """The policy is read from the parsed argv and checked, not just read.

    Three stages infer a page's paper value under the same sealed block. If
    this one read a file the run never bound, its counts would be taken at a
    threshold no other stage's record could be compared against.
    """
    from common.background import load_background_config
    from common.residual_ink import load_coverage_audit_config

    context = _drive_main(monkeypatch, [(1, _blank())])
    assert INK_MAP_RUN.main(registry_factory=None) == INK_MAP_RUN.EXIT_COMPLETE

    # Twice, and deliberately: the stage reads `[grouping.background]` and
    # `[coverage_audit]` through two loaders, and each one proves the bytes IT
    # read against the run's seal. One check standing for both would leave the
    # second loader's read unproved on a file that had changed between them.
    assert context.required_configs == [
        ("designator-grouping", load_background_config()["config_sha256"]),
        ("designator-grouping", load_coverage_audit_config()["config_sha256"]),
    ]
    payload = context.published[0]["payload"]
    background = payload["background"]
    assert background["config_sha256"] == load_background_config()["config_sha256"]
    # Provenance, as fields rather than as a sentence: the paper value, where
    # it came from, the level this stage measured at, and the derived margin the
    # Designator will measure the same page at.
    assert background["background_level"] == 230
    assert background["background_source"] == "inferred-modal"
    assert background["contrast_below_background"] == 40
    assert background["ink_threshold"] == 190
    assert background["dark_mode"] == 230
    assert background["ink_margin"] == 20
    # Published once, beside one edge finding that carries the page's audited
    # ink total: two copies of one number is how two copies come to disagree.
    assert set(payload) == {"page_ordinal", "ink_measurable", "background", "edge", "edge_findings"}
    assert "background" not in payload["edge"]


def _measured_payload(monkeypatch):
    rows = [bytearray([230] * 200) for _ in range(200)]
    for y in range(10):
        rows[y][10:30] = bytes([170] * 20)
    context = _drive_main(monkeypatch, [(1, encode_grayscale_png(200, 200, rows))])
    assert INK_MAP_RUN.main(registry_factory=None) == INK_MAP_RUN.EXIT_COMPLETE
    return context.published[0]["payload"]


def test_every_consumer_check_accepts_what_the_ink_map_publishes(monkeypatch):
    """The shared consumer validators pass the producer's own record unchanged."""
    from common.background import validate_measured_ink_map_payload
    from common.residual_ink import (
        MINIMUM_CONTRAST_BELOW_BACKGROUND,
        load_coverage_audit_config,
        reconcile_edge_finding_with_runs,
    )

    payload = _measured_payload(monkeypatch)
    sealed = load_coverage_audit_config()
    validate_measured_ink_map_payload(
        payload,
        audit_contrast=MINIMUM_CONTRAST_BELOW_BACKGROUND,
        ink_margin_bp=sealed["ink_margin_bp"],
    )
    measured = reconcile_edge_finding_with_runs(
        payload["edge"],
        payload["edge_findings"],
        coverage_policy=_coverage(200, 200),
        expected_dimensions=(200, 200),
    )
    assert measured["flagged"] is True


def test_a_recorded_margin_the_sealed_fraction_does_not_derive_is_refused(monkeypatch):
    """`ink_margin` decides the page-spanning split, so it must be the sealed one.

    A value the producer could never have measured with -- above the paper
    level, or merely different from what the sealed `ink_margin_bp` derives
    from the record's own paper and dark modes -- is refused, as is a payload
    carrying a field the contract no longer has.
    """
    from common.background import validate_measured_ink_map_payload
    from common.contracts.errors import ContractError
    from common.residual_ink import MINIMUM_CONTRAST_BELOW_BACKGROUND, load_coverage_audit_config

    payload = _measured_payload(monkeypatch)
    ink_margin_bp = load_coverage_audit_config()["ink_margin_bp"]

    def validate(candidate):
        return validate_measured_ink_map_payload(
            candidate, audit_contrast=MINIMUM_CONTRAST_BELOW_BACKGROUND, ink_margin_bp=ink_margin_bp
        )

    validate(payload)
    for margin in (payload["background"]["ink_margin"] + 1, 250):
        tampered = {**payload, "background": {**payload["background"], "ink_margin": margin}}
        with pytest.raises(ContractError, match="ink_margin"):
            validate(tampered)
    with pytest.raises(ContractError, match="not closed"):
        validate({**payload, "ink": dict(payload["edge"])})


def test_retained_runs_for_another_page_size_are_refused_by_the_shared_reconciler(monkeypatch):
    """Runs and finding that agree with each other still have to fit the sealed page."""
    from common.contracts.errors import ContractError
    from common.residual_ink import reconcile_edge_finding_with_runs

    payload = _measured_payload(monkeypatch)
    with pytest.raises(ContractError, match="not the sealed page's 200x201"):
        reconcile_edge_finding_with_runs(
            payload["edge"],
            payload["edge_findings"],
            coverage_policy=_coverage(200, 200),
            expected_dimensions=(200, 201),
        )


def test_a_page_whose_paper_cannot_be_inferred_is_named_rather_than_mapped(monkeypatch):
    """`ink-not-measurable`: in the census, with no counts and no retained runs.

    The page is the inverted scan `pipeline/2_designator/test_structure.py`
    uses -- 80% at 30, 20% at 220 -- whose mode is darker than its own mean and
    whose interior is dark, so no branch can call anything on it paper. Taking
    the raw mode (30) as the paper value would find no pixel 40 levels below
    it, and would publish `mapped` with `total_ink_pixels: 0`: a
    page reported clean because its threshold could not be reached.

    What is asserted here is what the record does *not* carry as much as what it
    does. `background`, `edge` and `edge_findings` are absent, not zeroed, so the
    Armarium's re-measurement and the Recensor's pointer confirmation both fail
    loudly on a consumer that assumed them rather than reading a zero nobody
    measured.
    """
    rows = [bytearray([30] * 100) for _ in range(100)]
    for y in range(80, 100):
        rows[y] = bytearray([220] * 100)
    context = _drive_main(monkeypatch, [(1, encode_grayscale_png(100, 100, rows))])

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


def test_paper_too_dark_for_this_audits_contrast_is_not_measurable_here(monkeypatch):
    """The second cause of `ink-not-measurable`: the shared inference accepts the paper.

    A uniform page at 30 infers paper 30 on the Designator's own terms (its
    floor margin is 20), but 30 levels leave no room for this audit's 40, so
    no pixel could count as ink and the page is named rather than mapped clean.
    """
    from common.background import infer_background_evidence

    rows = [bytearray([30] * 100) for _ in range(100)]
    assert (
        infer_background_evidence(100, 100, rows, background_policy=_policy(100, 100))["background"]
        == 30
    )
    context = _drive_main(monkeypatch, [(1, encode_grayscale_png(100, 100, rows))])

    assert INK_MAP_RUN.main(registry_factory=None) == INK_MAP_RUN.EXIT_COMPLETE
    (record,) = context.published
    assert record["outcome"] == "ink-not-measurable"
    assert "a background of 30 at a 40-point margin" in record["payload"]["background_refusal"]


def test_a_real_submission_names_edge_ink_and_an_unmeasurable_page(tmp_path):
    """Door, Exemplar and Ink Map as programs over pages the fixture does not carry.

    One page has writing inside its 2-pixel perimeter band and one is paper too
    dark to measure. The Ink Map's sealed run tree names each outcome.
    """
    edge_rows = [bytearray([230] * 200) for _ in range(260)]
    for y in range(12):
        edge_rows[y][40:160] = bytes([40] * 120)
    dark_rows = [bytearray([30] * 200) for _ in range(260)]
    approved = tmp_path / "approved-storage"
    source = approved / "submitted-pages"
    source.mkdir(parents=True)
    (source / "page-1.png").write_bytes(encode_grayscale_png(200, 260, edge_rows))
    (source / "page-2.png").write_bytes(encode_grayscale_png(200, 260, dark_rows))
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(approved)]
    policy_path = tmp_path / "data-gate-policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    ledger = approved / "submission-ledger.json"
    submit.submit(source, ledger, policy_path=policy_path)
    root = approved / "runs"
    door_argv = [
        "--submission-folder",
        str(source),
        "--submission-manifest",
        str(ledger),
        "--data-gate-policy",
        str(policy_path),
    ]
    for program, extra in (
        ("pipeline/1_exemplar/door.py", door_argv),
        ("pipeline/1_exemplar/run.py", []),
        ("pipeline/1_ink_map/run.py", []),
    ):
        result = subprocess.run(
            [sys.executable, str(ROOT / program), "--run-root", str(root), "--run-id", "r", *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"

    tree = RunTree(root, "r")
    outcomes = {}
    for entry in tree.build_manifest(INK_MAP)["artifacts"]:
        if entry["kind"] == "ink-map":
            record = tree.read_artifact(INK_MAP, "ink-map", entry["artifact_id"])
            outcomes[record["payload"]["page_ordinal"]] = record["outcome"]
    assert outcomes == {1: "unclaimed-edge-ink", 2: "ink-not-measurable"}


@pytest.mark.parametrize(
    ("row", "defect"),
    [
        ("not a row", "malformed row"),
        ([[0, 1, 2]], "malformed run"),
        ([[0, True]], "malformed run"),
        ([[2, 1], [0, 1]], "unordered or out-of-bounds"),
        ([[3, 2]], "unordered or out-of-bounds"),
        ([[1, 0]], "unordered or out-of-bounds"),
    ],
)
def test_every_reader_of_the_retained_runs_refuses_the_same_row(row, defect):
    """The Recensor and the Designator check a retained row through one validator."""
    designator = load_stage("2_designator")
    evidence = {"schema": "ink-runs.v2", "width": 4, "height": 1, "rows": [row]}
    box = {"x": 0, "y": 0, "w": 4, "h": 1}

    with pytest.raises(FatalAccounting, match=defect):
        RECENSOR_RUN._ink_outside_cuts_in_box(evidence, box, [])
    with pytest.raises(ContractError, match=defect):
        designator._ink_outside_cut_union(evidence, box, [])
    assert (
        RECENSOR_RUN._ink_outside_cuts_in_box({**evidence, "rows": [[[0, 1], [2, 2]]]}, box, [])
        == designator._ink_outside_cut_union({**evidence, "rows": [[[0, 1], [2, 2]]]}, box, [])
        == 3
    )


_RUNS = {"schema": "ink-runs.v2", "width": 4, "height": 1, "rows": [[[0, 1]]]}


@pytest.mark.parametrize(
    ("evidence", "defect"),
    [
        ("not a record", "wrong schema"),
        ({**_RUNS, "schema": "ink-runs.v1"}, "wrong schema"),
        ({**_RUNS, "extra": 1}, "not a closed record"),
        ({**_RUNS, "width": 0}, "invalid dimensions"),
        ({**_RUNS, "height": True}, "invalid dimensions"),
        ({**_RUNS, "height": 2}, "invalid dimensions"),
        ({**_RUNS, "rows": "rows"}, "invalid dimensions"),
    ],
)
def test_every_reader_of_the_retained_runs_refuses_the_same_envelope(evidence, defect):
    """The Recensor and the Designator check the run record through one validator."""
    designator = load_stage("2_designator")
    box = {"x": 0, "y": 0, "w": 4, "h": 1}

    with pytest.raises(FatalAccounting, match=defect):
        RECENSOR_RUN._ink_outside_cuts_in_box(evidence, box, [])
    with pytest.raises(ContractError, match=defect):
        designator._ink_outside_cut_union(evidence, box, [])
