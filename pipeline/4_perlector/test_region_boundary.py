"""The Perlector accepts only crops bound to their actual sealed Exemplar page."""

import copy
import importlib.util
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common.chairs import ChairRegistry
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id, region_id
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR
from common.exemplar_boundary import verify_exemplar_crop_lineage
from common.imaging import crop_png, dimensions
from common.native_witness import partition_disagreement
from common.runtree.store import RunTree
from common.stage import load_fixture

ROOT = Path(__file__).resolve().parents[2]


def _load_perlector():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("perlector_run_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()


class _Context:
    def __init__(self, tree):
        self.tree = tree
        self.run = tree.read_run()
        self.registry = ChairRegistry.from_toml(ROOT / "config/models.toml")
        self.fixture = load_fixture(ROOT / "proof")
        self.witness_chairs = list(self.run["witness_chairs"])

    def input_ref(self, relative_path):
        return {
            "relative_path": relative_path,
            "sha256": digest_bytes(self.tree.read_bytes(relative_path)),
        }

    def artifact_ref(self, stage, kind, artifact_id_):
        return self.input_ref(self.tree.artifact_path(stage, kind, artifact_id_))


@pytest.fixture
def real_region(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "region-boundary",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "region-boundary")
    entry = next(
        entry for entry in tree.build_manifest(DESIGNATOR)["artifacts"] if entry["kind"] == "region"
    )
    return _Context(tree), tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])


def test_a_region_bound_to_its_actual_exemplar_input_verifies(real_region):
    context, region = real_region
    verified = perlector.verify_region(context, region)

    assert verified["source_page_ordinal"] == region["payload"]["transform"]["source_page_ordinal"]
    assert verified["source_page_id"] == region["payload"]["transform"]["source_page_id"]
    assert verified["transform"] == region["payload"]["transform"]
    assert verified["structure_provenance"] == region["payload"]["provenance"]


def test_perlector_refuses_a_tampered_designator_region_provenance(real_region, monkeypatch):
    """The mirror of `test_perlector_refuses_a_tampered_testimonium_model_provenance`
    (pipeline/orchestrator/test_orchestrator_acceptance.py), one join earlier: a
    region's own GOVERNANCE-6 provenance must be validated before the Perlector
    treats it as the basis for a real reading, exactly as
    pipeline/3_attestatores/run.py::proposed_regions already validates the
    identical artifact kind before showing it to a witness."""
    context, region = real_region
    tampered = copy.deepcopy(region)
    tampered["payload"]["provenance"]["resolved_revision"] = {
        "kind": "digest-manifest",
        "value": "0" * 64,
    }
    tampered["self_hash"] = self_hash(tampered)
    entry = next(
        entry
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["artifact_id"] == region["artifact_id"]
    )
    path = context.tree.resolve(entry["relative_path"])
    path.write_bytes(canonical_bytes(tampered))
    monkeypatch.setattr(context.tree, "build_manifest", lambda stage: {"artifacts": [entry]})

    with pytest.raises(SchemaRefusal, match="resolved revision"):
        perlector.regions_of(context, region["subject_id"])


def test_perlector_names_a_designator_region_with_missing_provenance(real_region, monkeypatch):
    context, region = real_region
    missing = copy.deepcopy(region)
    del missing["payload"]["provenance"]
    missing["self_hash"] = self_hash(missing)
    entry = next(
        entry
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["artifact_id"] == region["artifact_id"]
    )
    context.tree.resolve(entry["relative_path"]).write_bytes(canonical_bytes(missing))
    monkeypatch.setattr(context.tree, "build_manifest", lambda stage: {"artifacts": [entry]})

    with pytest.raises(SchemaRefusal, match="model provenance is not an object"):
        perlector.regions_of(context, region["subject_id"])


def test_a_crop_from_page_one_cannot_claim_another_valid_page(real_region):
    context, region = real_region
    other = next(
        page
        for page in (
            context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
            for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]
            if entry["kind"] == "page"
        )
        if page["payload"]["ordinal"] != region["payload"]["transform"]["source_page_ordinal"]
    )
    mismatched = copy.deepcopy(region)
    mismatched["payload"]["transform"]["source_page_ordinal"] = other["payload"]["ordinal"]
    mismatched["payload"]["transform"]["source_page_id"] = other["subject_id"]
    mismatched["payload"]["region_id"] = region_id(
        mismatched["subject_id"], mismatched["payload"]["transform"]
    )

    with pytest.raises(SchemaRefusal, match="does not trace to its Exemplar page"):
        perlector.verify_region(context, mismatched)


def test_a_same_sized_crop_from_another_page_cannot_keep_the_original_transform(real_region):
    """A crop's dimensions and digest do not prove which sealed page created it."""
    context, region = real_region
    other = next(
        page
        for page in (
            context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
            for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]
            if entry["kind"] == "page"
        )
        if page["payload"]["ordinal"] != region["payload"]["transform"]["source_page_ordinal"]
    )
    bounds = region["payload"]["transform"]["bounds"]
    wrong_crop = crop_png(context.tree.read_bytes(other["payload"]["image_path"]), bounds)
    digest, published = context.tree.put_blob(DESIGNATOR, wrong_crop)
    substituted = copy.deepcopy(region)
    substituted["payload"]["image_path"] = published.relative_path
    substituted["payload"]["image_sha256"] = digest

    with pytest.raises(SchemaRefusal, match="does not trace to its Exemplar page"):
        perlector.verify_region(context, substituted)


def test_a_crop_relabelled_onto_a_different_act_cannot_pass_as_that_acts_own(real_region):
    """The page-substitution class closed above, one level down: a region whose
    TRANSFORM genuinely traces to a real sealed page can still be forged onto a
    different act's identity by relabelling `subject_id` and recomputing only the
    self-consistent `region_id` — every pixel/page check above stays green,
    because none of them ever re-derives which act the Designator's own proposal
    seal actually names for this crop."""
    context, region = real_region
    regions = [
        context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region"
    ]
    other_act = next(
        candidate for candidate in regions if candidate["subject_id"] != region["subject_id"]
    )

    forged = copy.deepcopy(other_act)
    forged["subject_id"] = region["subject_id"]
    forged["payload"]["region_id"] = region_id(forged["subject_id"], forged["payload"]["transform"])

    with pytest.raises(ContractError, match="proposal seal's act identity"):
        verify_exemplar_crop_lineage(context.tree, context.run, forged)
    with pytest.raises(SchemaRefusal, match="does not trace to its Exemplar page"):
        perlector.verify_region(context, forged)


def test_a_crop_relabelled_with_another_acts_key_cannot_borrow_its_seal_evidence(real_region):
    """Changing both identity fields must not make one act's crop evidence another's."""
    context, region = real_region
    other_act = next(
        context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region" and entry["subject_id"] != region["subject_id"]
    )
    forged = copy.deepcopy(other_act)
    forged["subject_id"] = region["subject_id"]
    forged["payload"]["act_key"] = region["payload"]["act_key"]
    forged["payload"]["region_id"] = region_id(forged["subject_id"], forged["payload"]["transform"])

    with pytest.raises(ContractError, match="does not name this proposal crop"):
        verify_exemplar_crop_lineage(context.tree, context.run, forged)


def test_malformed_exemplar_locators_all_refuse(real_region):
    """Every case must refuse *as a locator*, not as a stale binding.

    The loop mutated `transform` and left `region_id` naming the original one, so a
    case that survived the transform type checks refused at the `region_id`
    comparison instead — proving only that the binding still works. Six of the seven
    tripped the intended branch; `{"source_page_ordinal": -1}` is a non-boolean int,
    so it passed the type block and never showed that a negative ordinal is rejected
    as a locator at all. `region_id` is re-derived per case here, exactly as
    `test_a_crop_transform_must_fit_inside_its_sealed_exemplar_page` already does.
    """
    context, region = real_region
    changes = [
        {"source_page_ordinal": None},
        {"source_page_ordinal": "1"},
        {"source_page_ordinal": True},
        {"source_page_ordinal": -1},
        {"source_page_id": None},
        {"source_page_id": ""},
        {"source_page_id": 7},
    ]
    for change in changes:
        malformed = copy.deepcopy(region)
        malformed["payload"]["transform"].update(change)
        try:
            malformed["payload"]["region_id"] = region_id(
                malformed["subject_id"], malformed["payload"]["transform"]
            )
        except (TypeError, ValueError):
            # A locator this malformed cannot be hashed into a binding at all, which
            # leaves the original `region_id` in place. The refusal below is then the
            # transform type check, which is the branch these cases are for anyway.
            pass
        with pytest.raises(SchemaRefusal, match="does not trace to its Exemplar page"):
            perlector.verify_region(context, malformed)


def test_a_crop_transform_must_fit_inside_its_sealed_exemplar_page(real_region):
    context, region = real_region
    page = context.tree.read_artifact(
        EXEMPLAR,
        "page",
        next(
            entry["artifact_id"]
            for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]
            if entry["kind"] == "page"
            and entry["subject_id"] == region["payload"]["transform"]["source_page_id"]
        ),
    )
    page_width, page_height = dimensions(context.tree.read_bytes(page["payload"]["image_path"]))
    original = region["payload"]["transform"]["bounds"]
    bad_bounds = [
        {**original, "x": -1},
        {**original, "y": -1},
        {**original, "x": page_width - original["w"] + 1},
        {**original, "y": page_height - original["h"] + 1},
    ]
    for bounds in bad_bounds:
        malformed = copy.deepcopy(region)
        malformed["payload"]["transform"]["bounds"] = bounds
        malformed["payload"]["region_id"] = region_id(
            malformed["subject_id"], malformed["payload"]["transform"]
        )
        with pytest.raises(SchemaRefusal, match="does not trace to its Exemplar page"):
            perlector.verify_region(context, malformed)


@pytest.mark.parametrize("ordinal", [None, "1", True, {}])
def test_a_region_with_no_integer_attempt_ordinal_refuses_by_name(real_region, ordinal):
    """The sort ran before `verify_region` validated anything, and indexed
    `record["payload"]["attempt_ordinal"]` directly — so a resealed region that lost
    the field produced a `KeyError` traceback rather than a named refusal, at the
    boundary whose sibling test asserts `"Traceback" not in result.stderr`.
    """
    context, region = real_region
    malformed = copy.deepcopy(region)
    if ordinal is None:
        del malformed["payload"]["attempt_ordinal"]
    else:
        malformed["payload"]["attempt_ordinal"] = ordinal

    with pytest.raises(SchemaRefusal, match="no integer attempt ordinal"):
        perlector._region_ordinal(malformed)


def test_a_resealed_testimonium_cannot_retroactively_claim_a_recovery_crop(tmp_path):
    """Witness coverage is about pixels actually shown, never a later recrop."""
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "review",
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "witness-boundary",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, result.stderr
    tree = RunTree(tmp_path / "runs", "witness-boundary")
    context = _Context(tree)
    recovery = next(
        tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region"
        and tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])["payload"]["origin"]
        == "recovery"
    )
    act_id = recovery["subject_id"]
    proposals = [
        tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region"
        and entry["subject_id"] == act_id
        and tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])["payload"]["origin"]
        == "proposal"
    ]
    testimony = next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium" and entry["subject_id"] == act_id
    )
    forged = copy.deepcopy(testimony)
    transform = recovery["payload"]["transform"]
    forged["payload"]["presented"] = {
        "kind": "region",
        "source_page_id": transform["source_page_id"],
        "source_page_ordinal": transform["source_page_ordinal"],
        "image_path": recovery["payload"]["image_path"],
        "image_sha256": recovery["payload"]["image_sha256"],
        "transform": transform,
        "region_ref": {"region_id": recovery["payload"]["region_id"]},
    }
    forged["payload"]["observed"] = [
        {
            "ordinal": 0,
            "bounds": transform["bounds"],
            "bounds_source": "presented",
            "span": None,
        }
    ]
    forged["inputs"] = [context.input_ref(recovery["payload"]["image_path"])]
    forged["self_hash"] = self_hash(forged)

    with pytest.raises(SchemaRefusal, match="recovery region cannot be presented"):
        perlector.validate_testimonium_regions(context, forged, proposals)


def test_a_testimonium_may_not_understate_which_of_its_crops_it_speaks_for(tmp_path):
    """The reader re-derives `unpresented_regions` rather than trusting it, the
    same way it re-derives the presented region itself. A continuation act binds
    two proposal crops and presents one; a record that dropped the name would
    present a derived layer that looks like it speaks for the whole act, which is
    the partial result invariant 6 refuses to let look whole."""
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "continuation-scope",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "continuation-scope")
    context = _Context(tree)
    by_act: dict[str, list] = {}
    for entry in tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "region":
            continue
        record = tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        by_act.setdefault(record["subject_id"], []).append(record)
    act_id, proposals = next(
        (subject, sorted(rows, key=lambda region: region["payload"]["attempt_ordinal"]))
        for subject, rows in by_act.items()
        if len(rows) == 2
    )
    testimony = next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium" and entry["subject_id"] == act_id
    )
    assert testimony["payload"]["unpresented_regions"] == [proposals[1]["payload"]["region_id"]]
    perlector.validate_testimonium_regions(context, testimony, proposals)

    forged = copy.deepcopy(testimony)
    forged["payload"]["unpresented_regions"] = []
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="does not name exactly the bound proposal regions"):
        perlector.validate_testimonium_regions(context, forged, proposals)

    adapter_crop = copy.deepcopy(testimony)
    adapter_crop["payload"]["presented"]["kind"] = "adapter-crop"
    del adapter_crop["payload"]["presented"]["region_ref"]
    adapter_crop["payload"]["unpresented_regions"] = []
    adapter_crop["self_hash"] = self_hash(adapter_crop)
    with pytest.raises(SchemaRefusal, match="does not name exactly the bound proposal regions"):
        perlector.validate_testimonium_regions(context, adapter_crop, proposals)


def test_act_testimonium_consumer_reconciles_outcome_regions_and_inputs(real_region):
    """A resealed record cannot detach an attempted report from its presentation
    or from the exact proposal/input denominator the Attestatores retained."""
    context, region = real_region
    _, proposals = perlector.act_regions(context, region["subject_id"])
    testimony = next(
        context.tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium" and entry["subject_id"] == region["subject_id"]
    )

    unpresented = copy.deepcopy(testimony)
    unpresented["payload"].update({"presented": {}, "observed": [], "unpresented_regions": []})
    unpresented["inputs"] = []
    with pytest.raises(SchemaRefusal, match="attempted Testimonium has no image presentation"):
        perlector.validate_testimonium_regions(context, unpresented, proposals)

    not_attempted = copy.deepcopy(testimony)
    not_attempted["outcome"] = "not-run"
    with pytest.raises(SchemaRefusal, match="non-attempted Testimonium carries"):
        perlector.validate_testimonium_regions(context, not_attempted, proposals)

    shortened = copy.deepcopy(testimony)
    shortened["payload"]["regions"] = []
    with pytest.raises(SchemaRefusal, match="does not bind exactly its original proposal"):
        perlector.validate_testimonium_regions(context, shortened, proposals)

    extra_input = copy.deepcopy(testimony)
    other = next(
        row
        for row in perlector.sealed_proposal_regions(context)
        if row["subject_id"] != testimony["subject_id"]
    )
    extra_input["inputs"].append(context.input_ref(other["payload"]["image_path"]))
    extra_input["inputs"].sort(key=lambda row: (row["relative_path"], row["sha256"]))
    with pytest.raises(SchemaRefusal, match="does not bind exactly its proposal and presentation"):
        perlector.validate_testimonium_regions(context, extra_input, proposals)


def test_page_testimonium_consumer_reconciles_outcome_page_and_inputs(real_region):
    context, _ = real_region
    proposals = perlector.sealed_proposal_regions(context)
    testimony = next(
        context.tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium" and entry["outcome"] == "read"
    )
    perlector.validate_page_testimonium_record(context, testimony, proposals)

    unpresented = copy.deepcopy(testimony)
    unpresented["payload"].update({"presented": {}, "observed": [], "unpresented_regions": []})
    unpresented["inputs"] = []
    with pytest.raises(SchemaRefusal, match="attempted page Testimonium has no image"):
        perlector.validate_page_testimonium_record(context, unpresented, proposals)

    not_attempted = copy.deepcopy(testimony)
    not_attempted["outcome"] = "not-run"
    with pytest.raises(SchemaRefusal, match="non-attempted page Testimonium carries"):
        perlector.validate_page_testimonium_record(context, not_attempted, proposals)

    wrong_subject = copy.deepcopy(testimony)
    other_page = next(
        context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page" and entry["subject_id"] != testimony["subject_id"]
    )
    wrong_subject["payload"]["presented"] = attestatores_presentation = {
        "kind": "page",
        "source_page_id": other_page["subject_id"],
        "source_page_ordinal": other_page["payload"]["ordinal"],
        "image_path": other_page["payload"]["image_path"],
        "image_sha256": other_page["payload"]["source_sha256"],
        "transform": {
            "operation": "whole",
            "source_page_id": other_page["subject_id"],
            "source_page_ordinal": other_page["payload"]["ordinal"],
            "bounds": dict(testimony["payload"]["presented"]["transform"]["bounds"]),
        },
    }
    wrong_subject["payload"]["observed"] = [
        {
            "ordinal": 0,
            "bounds": dict(attestatores_presentation["transform"]["bounds"]),
            "bounds_source": "presented",
            "span": None,
        }
    ]
    wrong_subject["inputs"] = [context.input_ref(other_page["payload"]["image_path"])]
    with pytest.raises(SchemaRefusal, match="presentation names a different page"):
        perlector.validate_page_testimonium_record(context, wrong_subject, proposals)

    extra_input = copy.deepcopy(testimony)
    extra_input["inputs"].append(context.input_ref(proposals[0]["payload"]["image_path"]))
    extra_input["inputs"].sort(key=lambda row: (row["relative_path"], row["sha256"]))
    with pytest.raises(SchemaRefusal, match="does not bind exactly its presented image"):
        perlector.validate_page_testimonium_record(context, extra_input, proposals)


def test_page_adapter_crop_cannot_hide_proposals_outside_its_presentation(real_region):
    """The page consumer re-derives the disclosure for adapter crops too. An
    adapter may narrow its image, but it may not leave the page record looking
    complete while another sealed proposal lies outside that image."""
    context, _ = real_region
    proposals = perlector.sealed_proposal_regions(context)
    testimony = next(
        context.tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
        and entry["outcome"] == "read"
        and sum(
            region["payload"]["transform"]["source_page_id"] == entry["subject_id"]
            for region in proposals
        )
        >= 2
    )
    crop = next(
        region
        for region in proposals
        if region["payload"]["transform"]["source_page_id"] == testimony["subject_id"]
    )
    forged = copy.deepcopy(testimony)
    forged["payload"]["presented"] = {
        "kind": "adapter-crop",
        "source_page_id": testimony["subject_id"],
        "source_page_ordinal": testimony["payload"]["page_ordinal"],
        "image_path": crop["payload"]["image_path"],
        "image_sha256": crop["payload"]["image_sha256"],
        "transform": crop["payload"]["transform"],
    }
    forged["payload"]["observed"] = [
        {
            "ordinal": 0,
            "bounds": dict(crop["payload"]["transform"]["bounds"]),
            "bounds_source": "presented",
            "span": None,
        }
    ]
    forged["payload"]["unpresented_regions"] = []
    # The retained partition snapshot restates reported geometry against the
    # page's sealed proposals; re-derive it for the forged observed block, or
    # the record refuses on that internal contradiction before reaching the
    # disclosure rule under test.
    page_proposals = [
        region
        for region in proposals
        if region["payload"]["transform"]["source_page_id"] == testimony["subject_id"]
    ]
    forged["payload"]["partition_disagreement"] = partition_disagreement(forged, page_proposals)
    forged["inputs"] = [context.input_ref(crop["payload"]["image_path"])]

    with pytest.raises(SchemaRefusal, match="proposal regions outside its presentation"):
        perlector.validate_page_testimonium_record(context, forged, proposals)


def test_witness_coverage_requires_reported_geometry_to_contain_the_region():
    """Completed empty counts only when native/derived geometry contains the crop."""
    testimonia = [
        {
            "outcome": "genuinely-empty",
            "payload": {
                "presented": {"source_page_id": "page-1"},
                "observed": [
                    {
                        "bounds": {"x": 10, "y": 10, "w": 20, "h": 20},
                        "bounds_source": "native",
                    }
                ],
                "unpresented_regions": [],
            },
        },
        {
            "outcome": "failed",
            "payload": {"presented": {"source_page_id": "page-1"}, "observed": []},
        },
    ]
    regions = [
        {
            "region_id": "rgn_empty",
            "transform": {
                "source_page_id": "page-1",
                "bounds": {"x": 12, "y": 12, "w": 10, "h": 10},
            },
        },
        {
            "region_id": "rgn_recovery",
            "transform": {
                "source_page_id": "page-1",
                "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
            },
        },
    ]
    assert perlector.witnessed_region_ids(testimonia, regions) == {"rgn_empty"}


def test_unpresented_geometry_is_not_misreported_as_uncovered_when_another_witness_saw_it():
    region = {
        "region_id": "rgn_far_side",
        "transform": {
            "source_page_id": "page-2",
            "bounds": {"x": 20, "y": 20, "w": 10, "h": 10},
        },
    }
    testimonia = [
        {
            "outcome": "read",
            "payload": {
                "presented": {"source_page_id": "page-2"},
                "unpresented_regions": ["rgn_far_side"],
                "observed": [
                    {
                        "bounds": {"x": 0, "y": 0, "w": 100, "h": 100},
                        "bounds_source": "native",
                    }
                ],
            },
        },
        {
            "outcome": "read",
            "payload": {
                "presented": {"source_page_id": "page-2"},
                "unpresented_regions": [],
                "observed": [
                    {
                        "bounds": {"x": 20, "y": 20, "w": 10, "h": 10},
                        "bounds_source": "derived",
                    }
                ],
            },
        },
    ]
    assert perlector.witnessed_region_ids(testimonia, [region]) == {"rgn_far_side"}


def test_the_refusal_names_the_cause_it_used_to_swallow(real_region):
    """Opus-F3(c). Every distinct fault the shared boundary can find — a missing
    blob, a transform outside the page, a crop relabelled onto another act,
    pixels that are not the crop — reached the operator as the same nine words,
    because the `ContractError` carrying the specific cause was left on
    `__cause__` and `run_stage` prints only the refusal it catches.

    Two causes rather than one, so this proves the message is *derived* from the
    fault rather than merely longer. Both are checked against the exact sentence
    the shared boundary raises, so a wording change there cannot leave this test
    green with the cause gone.
    """
    context, region = real_region

    outside = copy.deepcopy(region)
    outside["payload"]["transform"]["bounds"] = dict(
        outside["payload"]["transform"]["bounds"], w=100_000
    )
    outside["payload"]["region_id"] = region_id(
        outside["subject_id"], outside["payload"]["transform"]
    )
    with pytest.raises(SchemaRefusal) as refused:
        perlector.verify_region(context, outside)
    assert str(refused.value) == (
        "a Designator region does not trace to its Exemplar page: "
        "a crop region's transform falls outside its Exemplar page"
    )

    relabelled = copy.deepcopy(region)
    relabelled["payload"]["act_key"] = "an act key the proposal seal never named"
    relabelled["payload"]["region_id"] = region_id(
        relabelled["subject_id"], relabelled["payload"]["transform"]
    )
    with pytest.raises(SchemaRefusal) as refused:
        perlector.verify_region(context, relabelled)
    assert str(refused.value).startswith(
        "a Designator region does not trace to its Exemplar page: a crop region names act_key"
    )
    assert "the proposal seal does not name exactly once" in str(refused.value)


def test_a_crop_written_by_another_encoder_is_not_refused_as_untraceable(real_region):
    """The composed half of Opus-F3, at the stage that reported it. The audit's
    demonstration ended `exit=2, SchemaRefusal: a Designator region does not
    trace to its Exemplar page. Every crop in the run is refused.` — with every
    pixel reproducing exactly from the Exemplar and the recorded transform."""
    context, region = real_region
    crop = context.tree.read_bytes(region["payload"]["image_path"])
    with Image.open(BytesIO(crop)) as image:
        image.load()
        output = BytesIO()
        image.save(output, format="PNG", optimize=False, compress_level=1)
    assert output.getvalue() != crop
    digest, published = context.tree.put_blob(DESIGNATOR, output.getvalue())
    reframed = copy.deepcopy(region)
    reframed["payload"]["image_path"] = published.relative_path
    reframed["payload"]["image_sha256"] = digest

    verified = perlector.verify_region(context, reframed)

    assert verified["region_id"] == region["payload"]["region_id"]


def _replace_capture_projection(payload):
    original = payload["payload"]
    forged = ("X" if original[0] != "X" else "Y") + original[1:]
    payload["payload"] = forged
    # `reported` is a retired projection; the closed schema now refuses the key
    # itself rather than checking its value.
    payload["native_capture"]["parse"]["text"] = forged
    payload["content_health"]["characters"] = len(forged)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update({"untrusted": True}), "closed schema"),
        (
            lambda payload: payload["provenance"].update({"receipt_ref": None}),
            "serving receipt",
        ),
        (
            lambda payload: payload["native_capture"].update(adapter="another-adapter.v1"),
            "configured boundary",
        ),
        (_replace_capture_projection, "parse.*retained raw response"),
    ],
)
def test_page_testimonium_consumer_closes_payload_and_provenance(
    real_region, monkeypatch, mutate, message
):
    """The writer's page closure is also enforced where the Perlector reads it."""
    context, region = real_region
    proposal_seal = context.tree.read_artifact(
        DESIGNATOR,
        "proposal-seal",
        artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None),
    )
    act = next(
        row
        for row in proposal_seal["payload"]["expected_acts"]
        if row["act_id"] == region["subject_id"]
    )
    regions, proposals = perlector.act_regions(context, act["act_id"])
    testimonia = perlector.testimonia_of(context, act["act_id"], proposals)
    bases = [perlector.verify_region(context, row) for row in regions]
    original = context.tree.read_artifact_reference

    def forged_reference(reference, *, stage, kind, subject_id):
        record = original(reference, stage=stage, kind=kind, subject_id=subject_id)
        # Only the churro chair's page records carry a native capture in the
        # composed roster; the forgery targets exactly those.
        if kind == "page-testimonium" and "native_capture" in record["payload"]:
            record = copy.deepcopy(record)
            mutate(record["payload"])
        return record

    monkeypatch.setattr(context.tree, "read_artifact_reference", forged_reference)
    proposal_ids = {region["payload"]["region_id"] for region in proposals}
    with pytest.raises(SchemaRefusal, match=message):
        perlector.act_attachment_view(context, act, testimonia, bases, proposal_ids)


def test_page_attachment_uses_the_page_attempt_outcome_not_the_compatibility_act_outcome(
    real_region, monkeypatch
):
    """A failed native page response cannot be laundered by a successful act row."""
    context, region = real_region
    proposal_seal = context.tree.read_artifact(
        DESIGNATOR,
        "proposal-seal",
        artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None),
    )
    act = next(
        row
        for row in proposal_seal["payload"]["expected_acts"]
        if row["act_id"] == region["subject_id"]
    )
    regions, proposals = perlector.act_regions(context, act["act_id"])
    testimonia = perlector.testimonia_of(context, act["act_id"], proposals)
    bases = [perlector.verify_region(context, row) for row in regions]
    proposal_ids = {row["payload"]["region_id"] for row in proposals}
    original_view = perlector.act_attachment_view(context, act, testimonia, bases, proposal_ids)

    original_artifact = context.tree.read_artifact
    original_reference = context.tree.read_artifact_reference

    def failed_attachment(stage, kind, artifact_id_):
        record = original_artifact(stage, kind, artifact_id_)
        if (
            stage == ATTESTATORES
            and kind == "act-attachment"
            and record["subject_id"] == act["act_id"]
        ):
            record = copy.deepcopy(record)
            entry = next(
                item
                for item in record["payload"]["attachments"]
                if item["chair"] == "attestator_3" and item["page_ordinal"] == 1
            )
            assert entry["attached"] is True
            # Comparability implies attachment; a coherent forgery drops both
            # or the comparable seam names it before the outcome check.
            entry.update(attached=False, attachment_basis="unattached", span=None, comparable=False)
        return record

    def failed_page(reference, *, stage, kind, subject_id):
        record = original_reference(reference, stage=stage, kind=kind, subject_id=subject_id)
        if kind == "page-testimonium" and record["payload"]["chair"] == "attestator_3":
            record = copy.deepcopy(record)
            record["outcome"] = "failed"
        return record

    monkeypatch.setattr(context.tree, "read_artifact", failed_attachment)
    monkeypatch.setattr(context.tree, "read_artifact_reference", failed_page)

    view = perlector.act_attachment_view(context, act, testimonia, bases, proposal_ids)
    assert "attestator_3" in original_view["comparison_views"]
    assert "attestator_3" not in view["comparison_views"]


def test_a_recovery_crop_cannot_retroactively_attach_a_page_witness(real_region, monkeypatch):
    """Recovery geometry cannot enter the earlier testimony denominator."""
    context, _ = real_region
    proposal_seal = context.tree.read_artifact(
        DESIGNATOR,
        "proposal-seal",
        artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None),
    )
    act = next(
        row
        for row in proposal_seal["payload"]["expected_acts"]
        if len(
            {
                entry["artifact_id"]
                for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
                if entry["kind"] == "region" and entry["subject_id"] == row["act_id"]
            }
        )
        > 1
    )
    regions, proposals = perlector.act_regions(context, act["act_id"])
    bases = [perlector.verify_region(context, row) for row in regions]
    testimonia = perlector.testimonia_of(context, act["act_id"], proposals)
    far_side = next(basis for basis in bases if basis["source_page_ordinal"] == 2)
    # The observation must miss the sealed continuation crop.
    reported = {"x": 0, "y": 200, "w": 10, "h": 40}
    original = context.tree.read_artifact_reference

    def marginal_page_geometry(reference, *, stage, kind, subject_id):
        record = original(reference, stage=stage, kind=kind, subject_id=subject_id)
        if kind == "page-testimonium" and record["payload"]["page_ordinal"] == 2:
            record = copy.deepcopy(record)
            record["payload"]["observed"] = [
                {"ordinal": 0, "bounds": dict(reported), "bounds_source": "native", "span": None}
            ]
            proposal_boxes = record["payload"]["partition_disagreement"]["proposal_boxes"]
            partition_proposals = [
                {
                    "payload": {
                        "origin": "proposal",
                        "transform": {
                            "source_page_id": record["payload"]["presented"]["source_page_id"],
                            "bounds": box,
                        },
                    }
                }
                for box in proposal_boxes
            ]
            record["payload"]["partition_disagreement"] = partition_disagreement(
                record, partition_proposals
            )
        return record

    monkeypatch.setattr(context.tree, "read_artifact_reference", marginal_page_geometry)

    recovery = copy.deepcopy(far_side)
    recovery["region_id"] = "rgn_recovery_probe"
    recovery["transform"] = copy.deepcopy(far_side["transform"])
    recovery["transform"]["bounds"] = {"x": 0, "y": 0, "w": 200, "h": 260}
    proposal_ids = {region["payload"]["region_id"] for region in proposals}
    assert recovery["transform"]["bounds"] != far_side["transform"]["bounds"]
    # The observation sits inside the later crop but does not contain that crop,
    # so it supplies neither an act attachment nor complete region coverage.
    assert (
        perlector.witnessed_region_ids(
            [
                {
                    "outcome": "read",
                    "payload": {
                        "presented": {"source_page_id": far_side["source_page_id"]},
                        "observed": [
                            {
                                "ordinal": 0,
                                "bounds": dict(reported),
                                "bounds_source": "native",
                                "span": None,
                            }
                        ],
                        "unpresented_regions": [],
                    },
                }
            ],
            [recovery],
        )
        == set()
    )

    view = perlector.act_attachment_view(context, act, testimonia, [*bases, recovery], proposal_ids)
    assert view["page_witness_count"] >= 1

    with pytest.raises(SchemaRefusal, match="does not derive from that witness's reported"):
        perlector.act_attachment_view(
            context, act, testimonia, [*bases, recovery], proposal_ids | {recovery["region_id"]}
        )
