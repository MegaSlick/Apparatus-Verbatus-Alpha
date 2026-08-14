"""The secondary proposer adds recall, never a verdict (spec 06 test 5).

Three levels, cheapest first: the pure candidate rule with no I/O at all, a
real rescue crop published through a real (but hand-fed) page analysis, and a
full end-to-end orchestrator run proving that configuring the role changes no
authoritative outcome relative to leaving it absent.
"""

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from _test_support import load_designator

from common.contracts.errors import ContractError

ROOT = Path(__file__).resolve().parents[2]


def _load_designator():
    return load_designator("designator_secondary_proposer_under_test")


# --- level 1: the pure candidate rule, no I/O at all ---------------------------


def test_a_candidate_touching_no_claim_is_rescued():
    designator = _load_designator()
    claimed = [{"act_id": "act_a", "bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}]
    candidates = [{"bounds": {"x": 100, "y": 100, "w": 5, "h": 5}, "pixel_count": 25}]
    assert designator._secondary_rescue_candidates(claimed, candidates) == [
        {"candidate": candidates[0], "overlapping_claimed_act_count": 0}
    ]


def test_a_candidate_already_inside_one_claim_is_not_a_rescue():
    designator = _load_designator()
    claimed = [{"act_id": "act_a", "bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}]
    candidates = [{"bounds": {"x": 2, "y": 2, "w": 3, "h": 3}, "pixel_count": 9}]
    assert designator._secondary_rescue_candidates(claimed, candidates) == []


def test_a_candidate_that_only_overlaps_one_claim_keeps_its_additional_coverage():
    designator = _load_designator()
    claimed = [{"act_id": "act_a", "bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}]
    candidate = {"bounds": {"x": 8, "y": 2, "w": 6, "h": 3}, "pixel_count": 18}
    assert designator._secondary_rescue_candidates(claimed, [candidate]) == [
        {"candidate": candidate, "overlapping_claimed_act_count": 1}
    ]


def test_a_candidate_spanning_two_claims_is_held_with_the_ambiguity_counted():
    """The P0-incident-shaped rule, without the proposer gaining a verdict.

    A box reaching two established acts may not decide between them — and it
    may not end the run either. It is carried as a held, review-only rescue
    whose payload states how many claims it touched, so a reviewer sees the
    ambiguity instead of an aborted stage.
    """
    designator = _load_designator()
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
    designator = _load_designator()
    claimed = [{"act_id": "act_a", "bounds": {"x": 0, "y": 0, "w": 10, "h": 10}}]
    rescuable = {"bounds": {"x": 50, "y": 50, "w": 4, "h": 4}, "pixel_count": 16}
    already_covered = {"bounds": {"x": 1, "y": 1, "w": 2, "h": 2}, "pixel_count": 4}
    with_both = designator._secondary_rescue_candidates(claimed, [rescuable, already_covered])
    with_only_rescuable = designator._secondary_rescue_candidates(claimed, [rescuable])
    assert with_both == with_only_rescuable
    assert [row["candidate"] for row in with_both] == [rescuable]


# --- level 2: a real rescue crop, published, flagged non-authoritative ---------


def _run(
    program: str, root: Path, models_config: Path | None = None
) -> subprocess.CompletedProcess:
    extra = [] if models_config is None else ["--models-config", str(models_config)]
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
        ]
        + extra,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _designator_context(
    designator,
    root: Path,
    models_config: Path | None = None,
    description: str = "secondary proposer test",
):
    from common.stage import open_context, stage_parser

    extra = [] if models_config is None else ["--models-config", str(models_config)]
    args = stage_parser(description).parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "happy"] + extra
    )
    return open_context(args, designator.DESIGNATOR)


def _prepared_context(designator, root: Path, models_config: Path | None, description: str):
    """Run the Door and Exemplar, then open the matching Designator context."""
    for program in ("pipeline/1_exemplar/door.py", "pipeline/1_exemplar/run.py"):
        result = _run(program, root, models_config)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return _designator_context(designator, root, models_config, description)


def _populated_context(tmp_path, models_config: Path | None = None):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
    ):
        result = _run(program, root, models_config)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    designator = _load_designator()
    context = _designator_context(designator, root, models_config)
    return designator, context


def _published_secondary_provenance(designator, context):
    return next(
        context.tree.read_artifact(
            designator.DESIGNATOR, "secondary-provenance", entry["artifact_id"]
        )["payload"]
        for entry in context.tree.build_manifest(designator.DESIGNATOR)["artifacts"]
        if entry["kind"] == "secondary-provenance"
    )


def test_a_configured_secondary_proposer_publishes_a_flagged_non_authoritative_rescue_crop(
    tmp_path,
):
    designator, context = _populated_context(tmp_path, _configured_models_config(tmp_path))
    records = designator.page_records(context)
    pages = designator.sealed_pages(records)
    page_record = pages[1]
    width, height, rows, background = designator.page_pixels(context, page_record)

    claimed = designator._claimed_regions_by_page(context)[1]
    # Bottom-right corner of the page: both fixture acts on page 1 (a1, a2,
    # even generously padded) stop well short of the page's own bottom edge,
    # so this pixel is not inside any claimed rectangle.
    stray_x, stray_y = width - 2, height - 2
    assert not any(
        entry["bounds"]["x"] <= stray_x < entry["bounds"]["x"] + entry["bounds"]["w"]
        and entry["bounds"]["y"] <= stray_y < entry["bounds"]["y"] + entry["bounds"]["h"]
        for entry in claimed
    ), "the fixture's own claimed bounds must not already cover the stray pixel this test adds"

    rows = [bytearray(row) for row in rows]
    rows[stray_y][stray_x] = 10  # unambiguous ink, far below any background threshold
    analysis = {"width": width, "height": height, "rows": rows, "background": background}
    secondary = _published_secondary_provenance(designator, context)

    before_kinds = {
        entry["kind"] for entry in context.tree.build_manifest(designator.DESIGNATOR)["artifacts"]
    }
    assert (
        designator._publish_secondary_proposals(
            context, 1, page_record, analysis, claimed, secondary
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
    assert payload["rescue_ref"]["relative_path"] == rescue_entries[0]["relative_path"]
    assert rescues[0]["outcome"] == "held"
    assert rescues[0]["payload"]["authoritative"] is False
    assert rescues[0]["payload"]["authority_effect"] == "review-only"
    assert rescues[0]["payload"]["origin"] == "secondary-proposer"
    assert rescues[0]["payload"]["padding"] is None
    assert payload["bounds"]["x"] <= stray_x < payload["bounds"]["x"] + payload["bounds"]["w"]
    assert payload["bounds"]["y"] <= stray_y < payload["bounds"]["y"] + payload["bounds"]["h"]

    # Nothing that decides authority appeared: no new region, act-group, or
    # proposal-seal -- only the flagged rescue.
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
    designator, context = _populated_context(tmp_path, _configured_models_config(tmp_path))
    records = designator.page_records(context)
    page_record = designator.sealed_pages(records)[1]
    width, height, rows, background = designator.page_pixels(context, page_record)
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
        )


# --- level 3: configuring the real roster changes no authoritative outcome -----


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
serving_recipe = \"fake-designator-v0\"
license_note = \"fixture identity only; no model weights or model license apply\"
"""


def _configured_models_config(tmp_path: Path) -> Path:
    """A `models.toml` identical to the shipped one except a configured
    `secondary_proposer` -- reusing `designator_structure`'s own already-
    verified fixture snapshot as the stand-in identity, exactly the way the
    fixture roster already stands in for a real model elsewhere."""
    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    live = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    assert _ABSENT_BLOCK in live
    digest_manifest = tomllib.loads(live)["chairs"]["designator_structure"]["digest_manifest"]
    (config_root / "models.toml").write_text(
        live.replace(_ABSENT_BLOCK, _CONFIGURED_BLOCK.format(digest_manifest=digest_manifest)),
        encoding="utf-8",
    )
    return config_root / "models.toml"


def test_a_secondary_rescue_makes_the_initial_pass_held_without_changing_act_authority(
    tmp_path, monkeypatch
):
    designator = _load_designator()
    root = tmp_path / "runs"
    models_config = _configured_models_config(tmp_path)
    context = _prepared_context(designator, root, models_config, "secondary held-exit test")

    def one_stray_candidate(width, height, rows, *, background):
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


def test_a_rescue_straddling_two_padded_claims_does_not_abort_the_authoritative_pass(
    tmp_path, monkeypatch
):
    """The optional chair may not cost the run its denominator.

    Act a1's and act a2's *padded* capture rectangles abut exactly at one row of
    the fixture page, so an ordinary pen mark in the blank band between the two
    entries produces a secondary component touching both claims at once. This
    used to raise out of `initial_pass` before the proposal seal was written:
    every act's authoritative work was discarded and every downstream stage lost
    its expected-act denominator, because a review-only box was ambiguous.
    """
    designator = _load_designator()
    root = tmp_path / "runs"
    models_config = _configured_models_config(tmp_path)
    context = _prepared_context(designator, root, models_config, "straddling rescue test")

    # The two claims `initial_pass` is about to cut, computed from the same
    # fixture rectangles and the same padding policy it will use.
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
    # The stand-in scan returns this one candidate for every sealed page; only
    # page 1 carries the two abutting claims that make it ambiguous.
    proposals = [
        record
        for record in (
            context.tree.read_artifact(
                designator.DESIGNATOR, "secondary-proposal", entry["artifact_id"]
            )
            for entry in context.tree.build_manifest(designator.DESIGNATOR)["artifacts"]
            if entry["kind"] == "secondary-proposal"
        )
        if record["payload"]["page_ordinal"] == 1
    ]
    assert len(proposals) == 1
    assert proposals[0]["outcome"] == "held"
    assert proposals[0]["payload"]["authoritative"] is False
    assert proposals[0]["payload"]["overlapping_claimed_act_count"] == 2


def test_an_out_of_page_secondary_candidate_is_refused_as_a_contract_error(tmp_path, monkeypatch):
    """A secondary-scan candidate landing outside the page must be refused with
    this pipeline's own `ContractError` shape, never `crop_png`'s bare
    `ValueError` -- the same defect class `bf6a716` closed for the recovery
    path. Unreachable today (candidates derive from the page's own pixel scan,
    always in-page by construction) but the day a real detector proposes boxes,
    they arrive from outside that guarantee.
    """
    designator = _load_designator()
    root = tmp_path / "runs"
    models_config = _configured_models_config(tmp_path)
    context = _prepared_context(
        designator, root, models_config, "out-of-page secondary candidate test"
    )

    def out_of_page_candidate(width, height, rows, *, background):
        return [{"bounds": {"x": width - 1, "y": height - 1, "w": 5, "h": 5}, "pixel_count": 25}]

    monkeypatch.setattr(designator.structure, "secondary_scan", out_of_page_candidate)
    with pytest.raises(
        ContractError, match=r"secondary candidate bounds .* falls outside its 200x260 pixel space"
    ):
        designator.initial_pass(context)


def _orchestrate(root: Path, models_config: Path | None) -> subprocess.CompletedProcess:
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
    ]
    if models_config is not None:
        command.extend(("--models-config", str(models_config)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def test_configuring_the_real_roster_changes_no_authoritative_outcome(tmp_path):
    from common.contracts.identities import artifact_id
    from common.contracts.stages import ARMARIUM, DESIGNATOR
    from common.runtree.store import RunTree

    absent_root = tmp_path / "absent"
    assert _orchestrate(absent_root, None).returncode == 0
    configured_root = tmp_path / "configured"
    assert _orchestrate(configured_root, _configured_models_config(tmp_path)).returncode == 0

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

    # Nothing visible differs. The secondary chair's provenance record is
    # written on every run, configured or absent, and this fixture has no stray
    # ink, so no rescue crop is cut either -- and never a region, an act-group,
    # or a changed proposal-seal.
    absent_kinds = {entry["kind"] for entry in absent_tree.build_manifest(DESIGNATOR)["artifacts"]}
    configured_kinds = {
        entry["kind"] for entry in configured_tree.build_manifest(DESIGNATOR)["artifacts"]
    }
    assert absent_kinds == configured_kinds
