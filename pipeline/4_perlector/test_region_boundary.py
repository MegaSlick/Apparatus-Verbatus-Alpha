"""The Perlector accepts only crops bound to their actual sealed Exemplar page."""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.chairs import ChairRegistry
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import region_id
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR
from common.exemplar_boundary import verify_exemplar_crop_lineage
from common.imaging import crop_png, dimensions
from common.runtree.store import RunTree

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

    def input_ref(self, relative_path):
        return {
            "relative_path": relative_path,
            "sha256": digest_bytes(self.tree.read_bytes(relative_path)),
        }


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


def test_malformed_exemplar_locators_all_refuse(real_region):
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
    forged["payload"]["regions"].append(perlector._region_reference(recovery))
    forged["inputs"] = sorted(
        forged["inputs"] + [context.input_ref(recovery["payload"]["image_path"])],
        key=lambda reference: (reference["relative_path"], reference["sha256"]),
    )
    forged["self_hash"] = self_hash(forged)

    with pytest.raises(SchemaRefusal, match="does not bind exactly the original proposal"):
        perlector.validate_testimonium_regions(context, forged, proposals)


def test_a_genuinely_empty_testimonium_marks_its_actual_regions_witness_covered():
    """Completed empty is evidence of inspection, unlike failed or not-run."""
    testimonia = [
        {
            "outcome": "genuinely-empty",
            "payload": {"regions": [{"region_id": "rgn_empty"}]},
        },
        {
            "outcome": "failed",
            "payload": {"regions": [{"region_id": "rgn_failed"}]},
        },
    ]
    assert perlector.witnessed_region_ids(testimonia) == {"rgn_empty"}
