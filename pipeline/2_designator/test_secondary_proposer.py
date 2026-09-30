"""The secondary proposer adds recall and DAI's own records, never a verdict.

The `secondary_proposer` chair is DAI's own project's record detector. The
Designator runs it and publishes what it found as page evidence that decides
nothing; the pixel-scan rescue runs only beside the fixture detector, as its
offline stand-in. Three levels,
cheapest first: the pure rules with no I/O, records and rescue crops published
over a real run tree, and a full orchestrator run proving configuring the chair
changes no authoritative outcome relative to leaving it absent.
"""

import hashlib
import shutil
import subprocess
import sys
import tomllib
import types
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from conftest import load_stage, programs_through

ROOT = Path(__file__).resolve().parents[2]


# --- level 1: the pure candidate rule, no I/O at all ---------------------------


def test_a_candidate_touching_no_claim_is_rescued():
    designator = load_stage("2_designator")
    claimed = [{"act_id": "act_a", "bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}]
    candidates = [{"bounds": {"x": 100, "y": 100, "w": 5, "h": 5}, "pixel_count": 25}]
    assert designator._secondary_rescue_candidates(claimed, candidates) == [
        {"candidate": candidates[0], "overlapping_claimed_act_count": 0}
    ]


def test_a_candidate_already_inside_one_claim_is_not_a_rescue():
    designator = load_stage("2_designator")
    claimed = [{"act_id": "act_a", "bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}]
    candidates = [{"bounds": {"x": 2, "y": 2, "w": 3, "h": 3}, "pixel_count": 9}]
    assert designator._secondary_rescue_candidates(claimed, candidates) == []


def test_a_candidate_that_only_overlaps_one_claim_keeps_its_additional_coverage():
    designator = load_stage("2_designator")
    claimed = [{"act_id": "act_a", "bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}]
    candidate = {"bounds": {"x": 8, "y": 2, "w": 6, "h": 3}, "pixel_count": 18}
    assert designator._secondary_rescue_candidates(claimed, [candidate]) == [
        {"candidate": candidate, "overlapping_claimed_act_count": 1}
    ]


def test_a_candidate_spanning_two_claims_is_held_with_the_ambiguity_counted():
    """A box reaching two established acts may not decide between them, and it
    may not end the run either: it is a held, review-only rescue whose payload
    states how many claims it touched, so a reviewer sees the ambiguity
    instead of an aborted stage.
    """
    designator = load_stage("2_designator")
    claimed = [
        {"act_id": "act_a", "bounds": {"x": 0, "y": 0, "w": 10, "h": 10}},
        {"act_id": "act_b", "bounds": {"x": 12, "y": 0, "w": 10, "h": 10}},
    ]
    candidates = [{"bounds": {"x": 8, "y": 0, "w": 6, "h": 10}, "pixel_count": 60}]
    assert designator._secondary_rescue_candidates(claimed, candidates) == [
        {"candidate": candidates[0], "overlapping_claimed_act_count": 2}
    ]


def test_removing_the_proposer_from_a_candidate_set_never_changes_the_rescue_set_of_the_rest():
    """Recall added by one candidate never depends on whether another exists."""
    designator = load_stage("2_designator")
    claimed = [{"act_id": "act_a", "bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}]
    rescuable = {"bounds": {"x": 50, "y": 50, "w": 4, "h": 4}, "pixel_count": 16}
    already_covered = {"bounds": {"x": 1, "y": 1, "w": 2, "h": 2}, "pixel_count": 4}
    with_both = designator._secondary_rescue_candidates(claimed, [rescuable, already_covered])
    with_only_rescuable = designator._secondary_rescue_candidates(claimed, [rescuable])
    assert with_both == with_only_rescuable
    assert [row["candidate"] for row in with_both] == [rescuable]


def test_detector_corners_are_floored_into_the_page_before_the_hull():
    designator = load_stage("2_designator")
    corners = [[30.9, -3.2], [205.0, 30.1], [170.4, 261.8], [0.0, 61.3]]
    assert designator._quantized_corners(corners, 200, 260) == [
        {"x": 30, "y": 0},
        {"x": 199, "y": 30},
        {"x": 170, "y": 259},
        {"x": 0, "y": 61},
    ]


# --- level 2: records and rescue crops over a real run tree ---------------------

_ABSENT_BLOCK = """[chairs.secondary_proposer]
state = \"absent\"
reason = \"no secondary proposer is configured for the offline walking skeleton\"
"""

_CONFIGURED_BLOCK = """[chairs.secondary_proposer]
state = \"configured\"
source = \"local-repository\"
path = \"designator_structure\"
digest_manifest = \"{digest_manifest}\"
manifest = \"manifests/designator_structure.json\"
serving_recipe = \"fake-secondary-proposer-v0\"
license_note = \"fixture identity only; no model weights or model license apply\"
"""

TIERS = ("generic-24gb", "generic-48gb", "generic-80gb-plus")

# Page 1 of the synthetic fixture holds a1 at (20, 20, 160, 80) and a2 at
# (20, 120, 160, 100). One record lies inside a1, slightly rotated; one
# straddles both acts; one collapses to a single pixel at the page corner.
DECLARED_DETECTIONS = (
    {
        "page_ordinal": 1,
        "corners": [[30.2, 30.7], [170.9, 30.1], [170.4, 60.8], [30.0, 61.3]],
        "score_bp": 9731,
    },
    {"page_ordinal": 1, "corners": [[25, 95], [175, 95], [175, 150], [25, 150]], "score_bp": 8120},
    {
        "page_ordinal": 1,
        "corners": [[199.9, 259.9], [250, 300], [210, 270], [199.5, 259.2]],
        "score_bp": 2600,
    },
)


def _configured(tmp_path: Path) -> list[str]:
    """Stage flags for a run whose `secondary_proposer` is a fixture detector.

    A copy of the shipped roster with the chair configured (reusing the
    structure chair's fixture snapshot as its stand-in identity) and the
    shipped catalogue with the chair's fixture rows added. The fixture detector
    answers with the fixture's `[[detector_record]]` rows; the shipped fixture
    declares none, so a test that needs records declares them on the context.
    """
    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    live = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    assert _ABSENT_BLOCK in live
    digest_manifest = tomllib.loads(live)["chairs"]["designator_structure"]["digest_manifest"]
    models = config_root / "models.toml"
    models.write_text(
        live.replace(_ABSENT_BLOCK, _CONFIGURED_BLOCK.format(digest_manifest=digest_manifest)),
        encoding="utf-8",
    )
    catalogue = config_root / "serving_recipes.toml"
    catalogue.write_text(
        (ROOT / "config" / "serving_recipes.toml").read_text(encoding="utf-8")
        + "".join(
            '\n[[profiles]]\nkind = "fixture"\nrecipe = "fake-secondary-proposer-v0"\n'
            f'chair = "secondary_proposer"\ntier = "{tier}"\n'
            'description = "offline walking-skeleton fixture for the record detector"\n'
            for tier in TIERS
        ),
        encoding="utf-8",
    )
    return [
        "--models-config",
        str(models),
        "--serving-recipes-config",
        str(catalogue),
    ]


def _run(program: str, root: Path, extra: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _designator_context(designator, root: Path, extra: list[str], description: str):
    from common.stage import open_context, stage_parser

    args = stage_parser(description).parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "happy", *extra]
    )
    return open_context(args, designator.DESIGNATOR)


def _prepared_context(designator, root: Path, extra: list[str], description: str):
    """Run the Door through the Ink Map, then open the matching Designator context."""
    for program in programs_through("ink-map"):
        result = _run(program, root, extra)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return _designator_context(designator, root, extra, description)


def _populated_context(tmp_path, extra: list[str]):
    root = tmp_path / "runs"
    for program in programs_through("designator"):
        result = _run(program, root, extra)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    designator = load_stage("2_designator")
    return designator, _designator_context(designator, root, extra, "secondary proposer test")


def _records(context, designator, kind: str) -> list[dict]:
    return [
        context.tree.read_artifact(designator.DESIGNATOR, kind, entry["artifact_id"])
        for entry in context.tree.build_manifest(designator.DESIGNATOR)["artifacts"]
        if entry["kind"] == kind
    ]


def _published_secondary_provenance(designator, context):
    return _records(context, designator, "secondary-provenance")[0]["payload"]


def test_declared_detections_become_page_evidence_that_decides_nothing(tmp_path):
    """Every record is kept with its oriented box, hull, score, class and act
    overlaps; none holds, rescues or enters an act; a collapsed box is kept uncut.
    """
    designator = load_stage("2_designator")
    context = _prepared_context(
        designator, tmp_path / "runs", _configured(tmp_path), "detector records test"
    )
    context.fixture["detector_record"] = [dict(row) for row in DECLARED_DETECTIONS]
    assert designator.initial_pass(context) is False
    context.finish()
    pages = _records(context, designator, "detector-page")
    by_page = {record["payload"]["page_ordinal"]: record["payload"] for record in pages}
    assert {ordinal: page["detection_count"] for ordinal, page in by_page.items()} == {1: 3, 2: 0}
    page_id = by_page[1]["record_subjects"][0].rsplit("-detector-", 1)[0]
    assert by_page[1]["record_subjects"] == [f"{page_id}-detector-{index}" for index in range(3)]

    records = {
        record["payload"]["detector_ordinal"]: record
        for record in _records(context, designator, "detector-record")
    }
    claimed = designator._claimed_regions_by_page(context)[1]
    inside, straddling, collapsed = (records[index]["payload"] for index in range(3))
    # Corners floored to their pixels, the hull reaching one pixel past the last centre.
    assert inside["bounds"] == {"x": 30, "y": 30, "w": 141, "h": 32}
    assert straddling["bounds"] == {"x": 25, "y": 95, "w": 151, "h": 56}
    assert inside["raw_proposal"]["geometry_kind"] == "obb"
    assert inside["raw_proposal"]["geometry"] == [
        {"x": 30, "y": 30},
        {"x": 170, "y": 30},
        {"x": 170, "y": 60},
        {"x": 30, "y": 61},
    ]
    assert inside["raw_proposal"]["crop_policy"] == {"mode": "aabb-enclose", "loss_recorded": False}
    assert (inside["score_bp"], inside["class_name"]) == (9731, "record")
    for payload in (inside, straddling, collapsed):
        assert payload["authoritative"] is False
        assert payload["authority_effect"] == "none"
        assert payload["quantization"] == designator.DETECTOR_QUANTIZATION
    # Overlap with every act proposal is recorded, never acted on.
    expected_overlaps = {
        name: sorted(
            (
                {"act_id": entry["act_id"], "overlap_px": area}
                for entry in claimed
                if (area := designator._overlap_area(entry["bounds"], payload["bounds"])) > 0
            ),
            key=lambda row: row["act_id"],
        )
        for name, payload in (("inside", inside), ("straddling", straddling))
    }
    assert len(expected_overlaps["inside"]) == 1
    assert len(expected_overlaps["straddling"]) == 2
    assert inside["act_overlaps"] == expected_overlaps["inside"]
    assert straddling["act_overlaps"] == expected_overlaps["straddling"]
    assert collapsed["cut"] is False
    assert collapsed["bounds"] is None and collapsed["region_ref"] is None

    # One crop per cut record, of its hull, cut by the stage's one crop path.
    regions = {
        record["subject_id"]: record for record in _records(context, designator, "detector-region")
    }
    assert set(regions) == {records[0]["subject_id"], records[1]["subject_id"]}
    region = regions[records[0]["subject_id"]]["payload"]
    assert (region["origin"], region["padding"]) == ("detector", None)
    assert region["transform"]["bounds"] == region["raw_bounds"] == inside["bounds"]
    blob = context.tree.read_bytes(region["image_path"])
    assert hashlib.sha256(blob).hexdigest() == region["image_sha256"]

    # Records enter no act: the seal, the regions and the rescue path never see them.
    kinds = {
        entry["kind"] for entry in context.tree.build_manifest(designator.DESIGNATOR)["artifacts"]
    }
    assert "rescue-crop" not in kinds and "secondary-proposal" not in kinds
    seal = context.tree.read_artifact(
        designator.DESIGNATOR, "proposal-seal", designator._seal_artifact_id()
    )
    assert {row["act_key"] for row in seal["payload"]["expected_acts"]} == {"a1", "a2"}
    assert all(
        record["payload"]["origin"] != "detector"
        for record in _records(context, designator, "region")
    )


def test_a_configured_secondary_proposer_publishes_a_flagged_non_authoritative_rescue_crop(
    tmp_path,
):
    designator, context = _populated_context(tmp_path, _configured(tmp_path))
    records = designator.page_records(context)
    pages = designator.sealed_pages(records)
    page_record = pages[1]
    width, height, rows, evidence = designator.page_pixels(
        context,
        page_record,
        grouping_policy=designator.grouping_config.load_grouping_config(
            ROOT / "config" / "designator_grouping.toml"
        ),
    )
    background = evidence["background"]

    claimed = designator._claimed_regions_by_page(context)[1]
    # Bottom-right corner: both fixture acts, even padded, stop well short of
    # the page's bottom edge, so this pixel is inside no claimed rectangle.
    stray_x, stray_y = width - 2, height - 2
    assert not any(
        entry["bounds"]["x"] <= stray_x < entry["bounds"]["x"] + entry["bounds"]["w"]
        and entry["bounds"]["y"] <= stray_y < entry["bounds"]["y"] + entry["bounds"]["h"]
        for entry in claimed
    ), "the fixture's own claimed bounds must not already cover the stray pixel this test adds"

    rows = [bytearray(row) for row in rows]
    rows[stray_y][stray_x] = 10  # unambiguous ink, far below any background threshold
    analysis = {
        "width": width,
        "height": height,
        "rows": rows,
        "background": background,
        "thresholds": designator.grouping_config.resolve_thresholds(
            designator.grouping_config.load_grouping_config(), width, height
        ),
    }
    secondary = _published_secondary_provenance(designator, context)

    before_kinds = {
        entry["kind"] for entry in context.tree.build_manifest(designator.DESIGNATOR)["artifacts"]
    }
    policy = designator.grouping_config.load_grouping_config()
    assert (
        designator._publish_secondary_proposals(
            context, 1, page_record, analysis, claimed, secondary, policy
        )
        is True
    )
    context.finish()

    manifest = context.tree.build_manifest(designator.DESIGNATOR)["artifacts"]
    proposals = [
        context.tree.read_artifact(
            designator.DESIGNATOR, "secondary-proposal", entry["artifact_id"]
        )
        for entry in manifest
        if entry["kind"] == "secondary-proposal"
    ]
    assert len(proposals) == 1
    rescue_entries = [entry for entry in manifest if entry["kind"] == "rescue-crop"]
    rescues = [
        context.tree.read_artifact(designator.DESIGNATOR, "rescue-crop", entry["artifact_id"])
        for entry in rescue_entries
    ]
    assert len(rescues) == 1
    payload = proposals[0]["payload"]
    assert payload["authoritative"] is False
    assert proposals[0]["outcome"] == "held"
    assert payload["terminal_disposition"] == "held-for-review"
    assert payload["secondary_enumeration"] == designator.SECONDARY_ENUMERATION_COMPLETE
    assert payload["rescue_ref"]["relative_path"] == rescue_entries[0]["relative_path"]
    assert rescues[0]["outcome"] == "held"
    assert rescues[0]["payload"]["authoritative"] is False
    assert rescues[0]["payload"]["authority_effect"] == "review-only"
    assert rescues[0]["payload"]["origin"] == "secondary-proposer"
    assert rescues[0]["payload"]["padding"] is None
    assert payload["bounds"]["x"] <= stray_x < payload["bounds"]["x"] + payload["bounds"]["w"]
    assert payload["bounds"]["y"] <= stray_y < payload["bounds"]["y"] + payload["bounds"]["h"]

    # Nothing that decides authority appeared -- only the flagged rescue.
    after_kinds = {entry["kind"] for entry in manifest}
    assert after_kinds - before_kinds == {"secondary-proposal", "rescue-crop"}


@pytest.mark.parametrize(
    ("missing", "refusal"),
    [
        ("resolved_identity", r"resolved identity"),
        ("resolved_revision", r"resolved revision"),
        ("receipt_ref", r"serving receipt"),
    ],
)
def test_a_configured_secondary_proposer_refuses_incomplete_provenance(tmp_path, missing, refusal):
    designator, context = _populated_context(tmp_path, _configured(tmp_path))
    records = designator.page_records(context)
    page_record = designator.sealed_pages(records)[1]
    width, height, rows, evidence = designator.page_pixels(
        context,
        page_record,
        grouping_policy=designator.grouping_config.load_grouping_config(
            ROOT / "config" / "designator_grouping.toml"
        ),
    )
    background = evidence["background"]
    rows = [bytearray(row) for row in rows]
    rows[height - 2][width - 2] = 10
    secondary = _published_secondary_provenance(designator, context)
    secondary[missing] = None

    with pytest.raises(ContractError, match=refusal):
        designator._publish_secondary_proposals(
            context,
            1,
            page_record,
            {"width": width, "height": height, "rows": rows, "background": background},
            designator._claimed_regions_by_page(context)[1],
            secondary,
            designator.grouping_config.load_grouping_config(),
        )


def test_a_page_with_more_rescue_candidates_than_the_bound_is_held_as_one_item(tmp_path):
    """The other per-page enumeration, bounded the same way the residual one is.

    Past `max_secondary_proposals` the pass is one held record instead of a
    rescue crop, PNG blob and proposal per candidate -- naming the count, the
    bound, and the sealed policy digest it was judged against.

    Exercised at zero rather than by drawing two thousand specks: a threshold
    is crossed identically whichever side of it the page is on.
    """
    import dataclasses

    designator, context = _populated_context(tmp_path, _configured(tmp_path))
    records = designator.page_records(context)
    page_record = designator.sealed_pages(records)[1]
    width, height, rows, evidence = designator.page_pixels(
        context,
        page_record,
        grouping_policy=designator.grouping_config.load_grouping_config(
            ROOT / "config" / "designator_grouping.toml"
        ),
    )
    background = evidence["background"]
    rows = [bytearray(row) for row in rows]
    rows[height - 2][width - 2] = 10
    policy = designator.grouping_config.load_grouping_config()
    analysis = {
        "width": width,
        "height": height,
        "rows": rows,
        "background": background,
        "thresholds": dataclasses.replace(
            designator.grouping_config.resolve_thresholds(policy, width, height),
            max_secondary_proposals=0,
        ),
    }
    secondary = _published_secondary_provenance(designator, context)
    claimed = designator._claimed_regions_by_page(context)[1]

    assert (
        designator._publish_secondary_proposals(
            context, 1, page_record, analysis, claimed, secondary, policy
        )
        is True
    )
    context.finish()

    manifest = context.tree.build_manifest(designator.DESIGNATOR)["artifacts"]
    assert [entry for entry in manifest if entry["kind"] == "rescue-crop"] == [], (
        "a withheld secondary pass cuts no crop; cutting them is what the bound prevents"
    )
    proposals = [
        context.tree.read_artifact(
            designator.DESIGNATOR, "secondary-proposal", entry["artifact_id"]
        )
        for entry in manifest
        if entry["kind"] == "secondary-proposal"
    ]
    assert len(proposals) == 1
    assert proposals[0]["outcome"] == "held"
    payload = proposals[0]["payload"]
    assert payload["secondary_enumeration"] == designator.SECONDARY_ENUMERATION_WITHHELD
    assert payload["secondary_candidate_count"] == 1
    assert payload["max_secondary_proposals"] == 0
    assert payload["grouping_config_sha256"] == policy["config_sha256"]
    assert payload["page_bounds"] == {"x": 0, "y": 0, "w": width, "h": height}
    assert "bounds" not in payload and "rescue_ref" not in payload, (
        "the withheld record names no single candidate rectangle and cites no crop"
    )


def test_a_secondary_rescue_makes_the_initial_pass_held_without_changing_act_authority(
    tmp_path, monkeypatch
):
    designator = load_stage("2_designator")
    root = tmp_path / "runs"
    context = _prepared_context(designator, root, _configured(tmp_path), "secondary held-exit test")

    # The stub must accept gap_tolerance_px too, or it silently wouldn't be
    # called the way the stage actually calls secondary_scan.
    def one_stray_candidate(width, height, rows, *, background, gap_tolerance_px):
        return [{"bounds": {"x": width - 2, "y": height - 2, "w": 1, "h": 1}, "pixel_count": 1}]

    monkeypatch.setattr(designator.structure, "secondary_scan", one_stray_candidate)
    assert designator.initial_pass(context) is True
    context.finish()

    manifest = context.tree.build_manifest(designator.DESIGNATOR)["artifacts"]
    assert any(entry["kind"] == "secondary-proposal" for entry in manifest)
    seal = context.tree.read_artifact(
        designator.DESIGNATOR,
        "proposal-seal",
        designator._seal_artifact_id(),
    )
    assert {row["outcome"] for row in seal["payload"]["expected_acts"]} == {"proposed"}


def test_an_in_process_detector_never_switches_on_the_pixel_rescue(tmp_path, monkeypatch):
    """The rescue is the fixture's stand-in: a real detector neither runs it nor lends
    it provenance, so a stray mark that would be rescued under the fixture is not.
    """
    import dataclasses

    designator = load_stage("2_designator")
    root = tmp_path / "runs"
    context = _prepared_context(designator, root, _configured(tmp_path), "rescue decoupling test")
    real_secondary = designator.secondary_provenance

    def as_in_process(context):
        record, detector = real_secondary(context)
        return record, dataclasses.replace(
            detector, run_facts={**detector.run_facts, "engine": "ultralytics"}
        )

    def one_stray_candidate(width, height, rows, *, background, gap_tolerance_px):
        return [{"bounds": {"x": width - 2, "y": height - 2, "w": 1, "h": 1}, "pixel_count": 1}]

    monkeypatch.setattr(designator, "secondary_provenance", as_in_process)
    monkeypatch.setattr(designator.structure, "secondary_scan", one_stray_candidate)
    assert designator.initial_pass(context) is False
    context.finish()
    kinds = {
        entry["kind"] for entry in context.tree.build_manifest(designator.DESIGNATOR)["artifacts"]
    }
    assert "detector-page" in kinds
    assert not kinds & {"secondary-proposal", "rescue-crop"}


def test_a_detector_score_enters_its_record_and_proposal_rounded_half_to_even(
    tmp_path, monkeypatch
):
    """0.00015 is 2 bp on its decimal text, where float arithmetic gives 1.4999... and 1."""
    import dataclasses

    designator = load_stage("2_designator")
    context = _prepared_context(
        designator, tmp_path / "runs", _configured(tmp_path), "score rounding test"
    )
    real_secondary = designator.secondary_provenance
    corners = DECLARED_DETECTIONS[0]["corners"]

    def scoring(context):
        record, detector = real_secondary(context)
        return record, dataclasses.replace(
            detector,
            _detect=lambda _png, ordinal: (
                [{"corners": corners, "score": 0.00015, "class_id": 0}] if ordinal == 1 else []
            ),
        )

    monkeypatch.setattr(designator, "secondary_provenance", scoring)
    assert round(0.00015 * 10_000) == 1
    assert designator.initial_pass(context) is False
    context.finish()
    [record] = _records(context, designator, "detector-record")
    assert record["payload"]["score_bp"] == 2
    assert record["payload"]["raw_proposal"]["score_bp"] == 2


def test_a_retried_fixture_pass_reuses_the_in_process_detector_s_sealed_receipt(
    tmp_path, monkeypatch
):
    """Each in-process load starts at its own time, so a retry that wrote a fresh
    receipt would change the sealed secondary provenance; it reuses the sealed one."""
    import dataclasses
    import itertools

    designator = load_stage("2_designator")
    root = tmp_path / "runs"
    extra = _configured(tmp_path)
    context = _prepared_context(designator, root, extra, "secondary retry test")
    loads = itertools.count()

    def load(identity, _profile, _root):
        detector = designator.fixture_record_detector(
            [], identity, designator.fixture_serving_details(identity)
        )
        started = f"2026-01-01T00:00:{next(loads):02d}Z"
        return dataclasses.replace(
            detector,
            run_facts={**detector.run_facts, "engine": "ultralytics"},
            serving_details=dataclasses.replace(detector.serving_details, started_at=started),
        )

    monkeypatch.setattr(designator, "_record_detector_mode", lambda *_a, **_k: "in-process")
    monkeypatch.setattr(designator, "load_ultralytics_record_detector", load)
    monkeypatch.setattr(
        designator,
        "bound_serving_recipes",
        lambda *_args: types.SimpleNamespace(for_identity=lambda *_a: None),
    )
    first, _detector = designator.secondary_provenance(context)
    designator._publish_secondary_provenance(context, first)
    context.finish()

    retry = _designator_context(designator, root, extra, "secondary retry test")
    again, _detector = designator.secondary_provenance(retry)
    assert next(loads) == 2
    assert again == first == _published_secondary_provenance(designator, retry)


def test_detector_records_alone_never_hold_the_designator(tmp_path):
    """Records are evidence, not holds: a clean page with records still exits complete."""
    designator = load_stage("2_designator")
    root = tmp_path / "runs"
    context = _prepared_context(designator, root, _configured(tmp_path), "records exit test")
    context.fixture["detector_record"] = [dict(row) for row in DECLARED_DETECTIONS]
    assert designator.initial_pass(context) is False
    context.finish()
    records = _records(context, designator, "detector-record")
    assert sorted(record["payload"]["detector_ordinal"] for record in records) == [0, 1, 2]
    [page_one] = [
        record["payload"]
        for record in _records(context, designator, "detector-page")
        if record["payload"]["page_ordinal"] == 1
    ]
    assert page_one["detection_count"] == len(DECLARED_DETECTIONS)


def test_a_rescue_straddling_two_padded_claims_does_not_abort_the_authoritative_pass(
    tmp_path, monkeypatch
):
    """The optional chair may not cost the run its denominator.

    Act a1's and a2's padded capture rectangles abut exactly at one row of the
    fixture page, so a mark in the blank band between them touches both claims
    at once. This used to raise out of `initial_pass` before the proposal seal
    was written, discarding every act's authoritative work over one ambiguous
    review-only box.
    """
    designator = load_stage("2_designator")
    root = tmp_path / "runs"
    context = _prepared_context(designator, root, _configured(tmp_path), "straddling rescue test")

    padding = designator.geometry.load_padding_config(context.args.designator_padding_config)
    page = next(row for row in context.fixture["page"] if row["ordinal"] == 1)
    will_claim = [
        designator.geometry.apply_padding(
            designator.act_bounds(act), page["width"], page["height"], padding
        )["bounds"]
        for act in context.fixture["act"]
        if act["page_ordinal"] == 1
    ]
    assert len(will_claim) == 2
    seam = max(bounds["y"] for bounds in will_claim)
    straddle = {"bounds": {"x": 100, "y": seam - 4, "w": 1, "h": 8}, "pixel_count": 8}
    touched = [
        bounds for bounds in will_claim if designator._overlap_area(bounds, straddle["bounds"]) > 0
    ]
    assert len(touched) == 2, "the mark must genuinely reach both claims for this to be the case"

    monkeypatch.setattr(
        designator.structure, "secondary_scan", lambda *a, **k: [dict(straddle)], raising=True
    )
    assert designator.initial_pass(context) is True
    context.finish()

    seal = context.tree.read_artifact(
        designator.DESIGNATOR, "proposal-seal", designator._seal_artifact_id()
    )
    assert {row["outcome"] for row in seal["payload"]["expected_acts"]} == {"proposed"}
    # The stand-in scan returns this candidate for every page; only page 1's
    # abutting claims make it ambiguous.
    proposals = [
        record
        for record in _records(context, designator, "secondary-proposal")
        if record["payload"]["page_ordinal"] == 1
    ]
    assert len(proposals) == 1
    assert proposals[0]["outcome"] == "held"
    assert proposals[0]["payload"]["authoritative"] is False
    assert proposals[0]["payload"]["overlapping_claimed_act_count"] == 2


def test_an_out_of_page_secondary_candidate_is_refused_as_a_contract_error(tmp_path, monkeypatch):
    """A secondary-scan candidate landing outside the page must be refused with
    this pipeline's own `ContractError`, never `crop_png`'s bare `ValueError`.
    """
    designator = load_stage("2_designator")
    root = tmp_path / "runs"
    context = _prepared_context(
        designator, root, _configured(tmp_path), "out-of-page secondary candidate test"
    )

    def out_of_page_candidate(width, height, rows, *, background, gap_tolerance_px):
        return [{"bounds": {"x": width - 1, "y": height - 1, "w": 5, "h": 5}, "pixel_count": 25}]

    monkeypatch.setattr(designator.structure, "secondary_scan", out_of_page_candidate)
    with pytest.raises(
        ContractError, match=r"secondary candidate bounds .* falls outside its 200x260 pixel space"
    ):
        designator.initial_pass(context)


def test_a_detector_row_the_stage_cannot_run_is_refused_before_anything_is_cut(tmp_path):
    """The record detector is run in-process or answered by the fixture, never served."""
    designator = load_stage("2_designator")
    root = tmp_path / "runs"
    extra = _configured(tmp_path)
    catalogue = Path(extra[extra.index("--serving-recipes-config") + 1])
    source = catalogue.read_text(encoding="utf-8")
    catalogue.write_text(
        source.replace('recipe = "fake-secondary-proposer-v0"', 'recipe = "nothing-serves-this"'),
        encoding="utf-8",
    )
    context = _prepared_context(designator, root, extra, "unrunnable detector test")
    with pytest.raises(ContractError, match="serving posture of the record detector"):
        designator.initial_pass(context)
    assert _records(context, designator, "region") == []


# --- level 3: configuring the detector changes no authoritative outcome ---------


def _orchestrate(root: Path, extra: list[str]) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ROOT / "pipeline" / "orchestrator" / "run.py"),
        "--fixture",
        "synthetic-two-page-v0",
        "--scenario",
        "happy",
        "--run-id",
        "r",
        "--run-root",
        str(root),
        *extra,
    ]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def test_configuring_the_detector_changes_no_authoritative_outcome(tmp_path):
    from common.contracts.identities import artifact_id
    from common.contracts.stages import ARMARIUM, DESIGNATOR
    from common.runtree.store import RunTree

    absent_root = tmp_path / "absent"
    absent = _orchestrate(absent_root, [])
    assert absent.returncode == 0, absent.stderr
    configured_root = tmp_path / "configured"
    configured = _orchestrate(configured_root, _configured(tmp_path))
    assert configured.returncode == 0, configured.stderr

    absent_tree = RunTree(absent_root, "r")
    configured_tree = RunTree(configured_root, "r")

    def export_of(tree):
        return tree.read_artifact(
            ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None)
        )["payload"]

    absent_export = export_of(absent_tree)
    configured_export = export_of(configured_tree)
    assert (
        absent_export["aggregate"]["status"]
        == configured_export["aggregate"]["status"]
        == "complete"
    )
    assert {item["act_key"]: item["text"] for item in absent_export["delivered"]} == {
        item["act_key"]: item["text"] for item in configured_export["delivered"]
    }

    def seal_outcomes(tree):
        seal = tree.read_artifact(
            DESIGNATOR,
            "proposal-seal",
            artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None),
        )
        return {
            row["act_key"]: (row["outcome"], row["has_continuation"])
            for row in seal["payload"]["expected_acts"]
        }

    assert seal_outcomes(absent_tree) == seal_outcomes(configured_tree)

    # This fixture has no stray ink, so no rescue crop is cut either way, and it
    # declares no detections, so the detector adds only its per-page census.
    absent_kinds = {entry["kind"] for entry in absent_tree.build_manifest(DESIGNATOR)["artifacts"]}
    configured_kinds = {
        entry["kind"] for entry in configured_tree.build_manifest(DESIGNATOR)["artifacts"]
    }
    assert configured_kinds - absent_kinds == {"detector-page"}
    assert absent_kinds <= configured_kinds
