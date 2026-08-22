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


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update({"untrusted": True}), "closed schema"),
        (
            lambda payload: payload["provenance"].update({"receipt_ref": None}),
            "serving receipt",
        ),
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
        if kind == "page-testimonium":
            record = copy.deepcopy(record)
            mutate(record["payload"])
        return record

    monkeypatch.setattr(context.tree, "read_artifact_reference", forged_reference)
    with pytest.raises(SchemaRefusal, match=message):
        perlector.act_attachment_view(context, act, testimonia, bases)
