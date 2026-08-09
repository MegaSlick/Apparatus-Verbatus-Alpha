"""The dossier: deterministic, order-invariant, and leaking nothing under the
blinded regime that a named dossier would show.
"""

import copy
import importlib.util
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common.chairs.registry import ChairRegistry
from common.contracts.canonical import canonical_text, digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import ATTESTATORES, DESIGNATOR, PERLECTOR
from common.imaging import dimensions
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def _load_perlector():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("perlector_dossier_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()
dossier = perlector.dossier_module


class _Context:
    def __init__(self, tree, witness_context="named"):
        self.tree = tree
        self.run = tree.read_run()
        self.registry = ChairRegistry.from_toml(ROOT / "config/models.toml")
        self.witness_context = witness_context

    @property
    def config_digest(self):
        return self.run["config_digest"]

    def input_ref(self, relative_path):
        return {
            "relative_path": relative_path,
            "sha256": digest_bytes(self.tree.read_bytes(relative_path)),
        }


@pytest.fixture(scope="module")
def evidence(tmp_path_factory):
    """A real run through the Attestatores, so the dossier is built over real
    regions and real testimonia rather than hand-built stand-ins."""
    root = tmp_path_factory.mktemp("dossier") / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "dossier-evidence",
                "--scenario",
                "happy",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"

    tree = RunTree(root, "dossier-evidence")
    context = _Context(tree)
    first_region = next(
        tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region"
    )
    act_id = first_region["subject_id"]
    act_key = first_region["payload"]["act_key"]
    regions = [
        tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])["payload"]
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region" and entry["subject_id"] == act_id
    ]
    testimonia = [
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium" and entry["subject_id"] == act_id
    ]
    return context, act_id, act_key, regions, testimonia


def _build(context, act_id, act_key, regions, testimonia, *, regime="named", witness_context=None):
    return dossier.build_dossier(
        context,
        act_id=act_id,
        act_key=act_key,
        regions=regions,
        testimonia=testimonia,
        witnessed_region_ids={region["region_id"] for region in regions},
        regime=regime,
        page_renders=[],
        witness_context=witness_context,
    )


def test_dossier_is_deterministic_and_shuffle_invariant(evidence):
    context, act_id, act_key, regions, testimonia = evidence
    forward = _build(context, act_id, act_key, regions, testimonia)
    shuffled = _build(context, act_id, act_key, list(reversed(regions)), list(reversed(testimonia)))
    assert forward == shuffled
    assert forward["dossier_digest"] == shuffled["dossier_digest"]


def test_dossier_carries_no_order_bearing_field(evidence):
    context, act_id, act_key, regions, testimonia = evidence
    built = _build(context, act_id, act_key, regions, testimonia)
    dossier.assert_no_order_bearing_field(built)


def test_the_sweep_runs_on_every_dossier_the_build_actually_produces(evidence, monkeypatch):
    """A guard the production path does not run is a guard in name only, and
    this one was exactly that until the merge: the tests called it, the handoff
    said it swept every key, and `build_dossier` never invoked it.

    The sweep checks field *names*, and every field name in a dossier comes from
    this module's own code -- so no forged input can trip it, and the only
    honest test of the wiring is that the build really calls it. A future edit
    that drops the call fails here rather than silently removing the one guard
    standing over GOVERNANCE 3."""
    context, act_id, act_key, regions, testimonia = evidence
    swept = []
    monkeypatch.setattr(
        dossier,
        "assert_no_order_bearing_field",
        # A deep copy, because the real call is handed the dossier dict itself
        # and the digest is added to that same object a line later -- keeping
        # the reference would record the value as it looks *after* the step
        # this test exists to order.
        lambda value, path="$": swept.append(copy.deepcopy(value)),
    )
    built = _build(context, act_id, act_key, regions, testimonia)
    assert swept == [{key: value for key, value in built.items() if key != "dossier_digest"}], (
        "build_dossier must sweep the whole dossier, and must do it before the "
        "digest is taken -- a preference-bearing field sealed into the digest is "
        "already in the record by the time anyone could object"
    )


def test_the_no_order_bearing_sweep_is_not_vacuous(evidence):
    """Prove the guard can go red: a dossier carrying a trust/preference field
    must be caught."""
    context, act_id, act_key, regions, testimonia = evidence
    built = _build(context, act_id, act_key, regions, testimonia)
    tampered = copy.deepcopy(built)
    tampered["testimonia"][0]["trust_score"] = 100
    with pytest.raises(ContractError, match="names a preference"):
        dossier.assert_no_order_bearing_field(tampered)


def test_blinded_regime_carries_no_chair_name_or_training_domain(evidence):
    context, act_id, act_key, regions, testimonia = evidence
    named = _build(context, act_id, act_key, regions, testimonia, regime="named")
    blinded = _build(context, act_id, act_key, regions, testimonia, regime="blinded")

    chairs = {record["payload"]["chair"] for record in testimonia}
    domains = {
        entry["training_domain"] for entry in named["testimonia"] if entry["training_domain"]
    }
    blinded_text = canonical_text(blinded)

    for chair in chairs:
        assert chair not in blinded_text, f"blinded dossier leaks the real chair name {chair!r}"
    for domain in domains:
        assert domain not in blinded_text, (
            f"blinded dossier leaks a training-domain fact {domain!r}"
        )
    assert all(entry["training_domain"] is None for entry in blinded["testimonia"])
    assert all(entry["model_name"] is None for entry in blinded["testimonia"])
    assert all(entry["resolved_provenance"] is None for entry in blinded["testimonia"])
    assert all(entry["witness_label"].startswith("witness-") for entry in blinded["testimonia"])


def test_named_dossier_carries_each_witness_model_and_resolved_provenance(evidence):
    context, act_id, act_key, regions, testimonia = evidence
    named = _build(context, act_id, act_key, regions, testimonia, regime="named")
    by_chair = {record["payload"]["chair"]: record for record in testimonia}
    assert set(by_chair) == {row["witness_label"] for row in named["testimonia"]}
    for row in named["testimonia"]:
        provenance = by_chair[row["witness_label"]]["payload"]["provenance"]
        identity = provenance["resolved_identity"]
        expected_name = (
            identity["repo"] if identity["source"] == "huggingface" else identity["path"]
        )
        assert row["model_name"] == expected_name
        assert row["resolved_provenance"] == provenance


def test_dossier_refuses_an_undeclared_witness_regime_even_without_testimonia(evidence):
    context, act_id, act_key, regions, _ = evidence
    with pytest.raises(SchemaRefusal, match="witness regime"):
        _build(context, act_id, act_key, regions, [], regime="half-blinded")


def test_blinded_pseudonyms_are_stable_and_reversible_without_a_stored_map(evidence):
    """Reversal is recomputing the same deterministic function over the public
    roster in `run.json`, never a second stored copy of it."""
    from regime import pseudonym_for

    context, act_id, act_key, regions, testimonia = evidence
    blinded = _build(context, act_id, act_key, regions, testimonia, regime="blinded")
    labels = {entry["witness_label"] for entry in blinded["testimonia"]}
    chairs = {record["payload"]["chair"] for record in testimonia}
    recomputed = {
        pseudonym_for(chair, run_id=context.tree.run_id, config_digest=context.config_digest)
        for chair in chairs
    }
    assert labels == recomputed


def test_named_and_blinded_sort_orders_can_differ(evidence):
    """Sorting by displayed label, not the true chair name: under blinding the
    order is a function of the pseudonym, not a fixed slot per chair."""
    context, act_id, act_key, regions, testimonia = evidence
    named = _build(context, act_id, act_key, regions, testimonia, regime="named")
    blinded = _build(context, act_id, act_key, regions, testimonia, regime="blinded")
    named_order = [entry["witness_label"] for entry in named["testimonia"]]
    assert named_order == sorted(named_order)
    blinded_order = [entry["witness_label"] for entry in blinded["testimonia"]]
    assert blinded_order == sorted(blinded_order)


def test_load_witness_context_refuses_a_chair_with_no_declared_entry(tmp_path, evidence):
    context, act_id, act_key, regions, testimonia = evidence
    incomplete = tmp_path / "witness_context.toml"
    incomplete.write_text('[attestator_1]\ntraining_domain = "x"\n', encoding="utf-8")
    table = dossier.load_witness_context(incomplete)
    with pytest.raises(ContractError, match="no declared entry"):
        _build(context, act_id, act_key, regions, testimonia, witness_context=table)


@pytest.mark.parametrize(
    "entry",
    [
        'training_domain = ""',
        'training_domain = "fixture"\ntrust_score = 1',
    ],
)
def test_witness_context_refuses_missing_facts_and_picker_metadata(tmp_path, entry):
    declaration = tmp_path / "witness_context.toml"
    declaration.write_text(f"[attestator_1]\n{entry}\n", encoding="utf-8")
    with pytest.raises(ContractError, match="closed, non-blank"):
        dossier.load_witness_context(declaration)


def test_build_page_render_records_its_whole_transform_not_only_a_factor(evidence):
    """ARCHITECTURE invariant 3 asks that the exact image shown be reproducible
    from the Exemplar plus the recorded transforms. A bare factor is only
    reproducible by someone who also has this module's code, so the record
    names the source size, the target size and the resampler."""
    context, act_id, act_key, regions, testimonia = evidence
    render = dossier.build_page_render(
        context,
        source_page_id=regions[0]["transform"]["source_page_id"],
        source_page_ordinal=regions[0]["transform"]["source_page_ordinal"],
    )
    assert render["transform"] == {
        "operation": "downscale-for-page-context",
        "source_dimensions": {"w": 200, "h": 260},
        # The synthetic fixture page is far inside the bound, so the honest
        # record is that nothing was resampled -- not a decorative resize
        # reported as a downscale.
        "target_dimensions": {"w": 200, "h": 260},
        "maximum_edge": dossier.PAGE_CONTEXT_MAX_EDGE,
        "resampler": "identity",
    }
    assert render["source"]["sha256"]
    assert context.tree.read_bytes(render["image_path"])


def test_a_page_past_the_bound_is_actually_downscaled_to_it():
    """The branch the 200x260 fixture page never reaches. Proved on real bytes
    rather than asserted, because a page-context render that quietly returned
    the full-resolution page would satisfy every other test in this file."""
    big = BytesIO()
    Image.new("L", (4000, 3000), color=200).save(big, format="PNG")
    rendered, transform = dossier._downscale_page(big.getvalue(), maximum_edge=1024)
    assert transform["source_dimensions"] == {"w": 4000, "h": 3000}
    assert transform["target_dimensions"] == {"w": 1024, "h": 768}
    assert transform["resampler"] == "pillow-lanczos"
    assert dimensions(rendered) == (1024, 768)


def test_build_page_render_is_reused_byte_identically_on_a_repeat_call(evidence):
    context, act_id, act_key, regions, testimonia = evidence
    first = dossier.build_page_render(
        context,
        source_page_id=regions[0]["transform"]["source_page_id"],
        source_page_ordinal=regions[0]["transform"]["source_page_ordinal"],
    )
    second = dossier.build_page_render(
        context,
        source_page_id=regions[0]["transform"]["source_page_id"],
        source_page_ordinal=regions[0]["transform"]["source_page_ordinal"],
    )
    assert first == second


def test_published_perlectio_binds_page_context_and_its_source_as_direct_inputs(tmp_path):
    root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--run-root",
            str(root),
            "--run-id",
            "page-context-inputs",
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "page-context-inputs")
    readings = [
        entry
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio"
    ]
    assert readings, "the fixture must publish a Perlectio before input lineage can be tested"
    for entry in readings:
        reading = tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        for page_render in reading["payload"]["dossier"]["page_renders"]:
            assert page_render["source"] in reading["inputs"]
            assert {
                "relative_path": page_render["image_path"],
                "sha256": page_render["image_sha256"],
            } in reading["inputs"]
