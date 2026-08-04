"""Spec 01's seven acceptance tests, driven over the real pipeline.

Meta-invariant #86, verbatim: "A fix proven only on a fixture is not proven."
Load-bearing tests drive REAL producers — real CLIs, real argv, real subprocesses —
over REAL sealed artifacts. Nothing here imports a stage and calls its main(); every
run below shells out exactly as the operator would, so a stage that only works when
imported would fail here rather than pass.

Meta-invariant #88: no test reports success over an empty population. Every loop
asserts an exact expected count.
"""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from common.chairs import ChairIdentity, load_models_toml
from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of, self_hash
from common.contracts.envelope import validate_envelope, verify_input_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import (
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    PERLECTOR,
    RECENSOR,
    STAGE_DIRECTORIES,
)
from common.runtree.store import RunTree
from common.stage import load_fixture, run_config_bindings

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE = "synthetic-two-page-v0"

# Re-pinned, in the same commit that changed the provenance block, per spec 02's
# test 9 — never loosened. Each is the digest of the whole relative-path ->
# file-digest inventory, so "nothing changed" cannot be satisfied by a run that
# is internally consistent but no longer the run these tests describe. A change
# here is legitimate exactly when a commit deliberately changes what a run
# writes, and then the new value belongs in that commit and nowhere else.
#
# Re-pinned again for the System 03 rebuild. The file count remains fixed, but door
# admissions, Exemplar pages/census, and Armarium export rows now retain original
# filename/digest linkage; the Designator independently verifies that census before
# it creates proposals. Recomputed against the real orchestrator.
#
# Re-pinned for ruling 14 and the final System 03 check. The run authority now carries
# its PDF target, the fixed decoder route is no longer disguised as configuration,
# and every delivered source region retains its complete crop transform. These are
# deliberate record changes, so the whole-tree pins change with them.
HAPPY_RUN_TREE_DIGEST = "f18ba5ff5a3ba53cc58d91c458c032aa628804329971b1956f8f595b87058feb"
REVIEW_RUN_TREE_DIGEST = "3ff2b39ab6a50562c64ca8de5d682ffefe9f3d00abb59f1f3886c9cb3223e4e0"


def orchestrate(
    run_root: Path,
    run_id: str,
    scenario: str,
    *,
    models_config: Path | None = None,
    recovery_config: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run the pipeline the way a person would, and return the whole result."""
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        FIXTURE,
        "--scenario",
        scenario,
        "--run-id",
        run_id,
        "--run-root",
        str(run_root),
    ]
    if models_config is not None:
        command.extend(("--models-config", str(models_config)))
    if recovery_config is not None:
        command.extend(("--recovery-config", str(recovery_config)))
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def invoke_stage(
    run_root: Path, run_id: str, scenario: str, program: str, **extra
) -> subprocess.CompletedProcess:
    """Run one real stage program against a staged synthetic run tree."""
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(run_root),
        "--run-id",
        run_id,
        "--scenario",
        scenario,
    ]
    for key, value in extra.items():
        command.extend((f"--{key.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def run_through_recensor(
    run_root: Path, run_id: str, scenario: str = "happy", *, allow_held: bool = False
) -> None:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = invoke_stage(run_root, run_id, scenario, program)
        expected = {0, 3} if allow_held else {0}
        assert result.returncode in expected, f"{program}: {result.stderr}"


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def snapshot_digest(root: Path) -> str:
    """The canonical pin for the full relative run-tree inventory."""
    return digest_of(snapshot(root))


def export_of(tree: RunTree) -> dict:
    return tree.read_artifact(ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None))[
        "payload"
    ]


@pytest.fixture(scope="module")
def happy_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("happy")
    result = orchestrate(root, "r", "happy")
    assert result.returncode == 0, result.stderr
    return root, RunTree(root, "r")


@pytest.fixture(scope="module")
def review_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("review")
    result = orchestrate(root, "r", "review")
    # Exit 3 is "accounted, holdable" — the run reached honest terminal states and
    # one act is held. A zero here would be the vacuous green this project exists
    # to notice.
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r")


# --- 1. The happy path runs offline, and every reference resolves --------------


def test_the_happy_path_runs_and_establishes_both_acts(happy_run):
    _, tree = happy_run
    export = export_of(tree)
    assert export["aggregate"]["status"] == "complete"
    assert export["aggregate"]["reasons"] == []
    assert len(export["delivered"]) == 2
    assert export["review"] == []
    assert {item["category"] for item in export["delivered"]} == {"delivered"}


def test_every_input_reference_in_the_run_resolves_and_matches_its_digest(happy_run):
    """The whole traceability claim in one assertion: every artifact names the
    bytes it was derived from, and every one of those references is real."""
    _, tree = happy_run
    checked = 0
    for stage in (DOOR, EXEMPLAR, DESIGNATOR, ATTESTATORES, PERLECTOR, RECENSOR, ARCHETYPUS):
        for entry in tree.build_manifest(stage)["artifacts"]:
            record = tree.read_artifact(stage, entry["kind"], entry["artifact_id"])
            for reference in record["inputs"]:
                verify_input_bytes(reference, tree.read_bytes(reference["relative_path"]))
                checked += 1
    assert checked >= 20, f"only {checked} references checked; the run looks too thin"


def test_every_expected_act_has_exactly_one_terminal_category(happy_run):
    _, tree = happy_run
    export = export_of(tree)
    entries = [
        tree.read_artifact(ARMARIUM, "manifest-entry", entry["artifact_id"])
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "manifest-entry"
    ]
    assert len(entries) == export["expected_acts"] == 2
    assert len({entry["subject_id"] for entry in entries}) == 2


def test_the_final_export_keeps_each_original_filename_and_digest_link(happy_run):
    """Every exported crop names its original source file/frame, not just the pages list."""
    _, tree = happy_run
    export = export_of(tree)
    source_by_ordinal = {row["ordinal"]: row for row in tree.read_run()["source_manifest"]}
    assert len(export["pages"]) == len(source_by_ordinal) == 2
    pages_by_ordinal = {page["ordinal"]: page for page in export["pages"]}
    for page in export["pages"]:
        source = source_by_ordinal[page["ordinal"]]
        assert page["declared_path"] == source["relative_path"]
        assert page["declared_sha256"] == source["sha256"]
        assert page["page_id"]
    for delivered in export["delivered"]:
        assert delivered["source_regions"]
        for region in delivered["source_regions"]:
            source = source_by_ordinal[region["source_page_ordinal"]]
            page = pages_by_ordinal[region["source_page_ordinal"]]
            assert region["source_page_id"] == page["page_id"]
            assert region["declared_path"] == source["relative_path"]
            assert region["declared_sha256"] == source["sha256"]


def test_a_genuinely_empty_testimonium_counts_as_a_witnessed_read(tmp_path):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "genuinely-empty-witness")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    empty = next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
        and entry["outcome"] == "genuinely-empty"
        and entry["subject_id"]
    )
    assert empty["payload"]["chair"] == "attestator_3"
    assert empty["payload"]["content_health"] == {
        "empty": True,
        "truncated": False,
        "characters": 0,
    }
    reading = next(
        tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio" and entry["subject_id"] == empty["subject_id"]
    )
    assert all(region["witness_covered"] for region in reading["payload"]["basis"]["regions"])
    assert export_of(tree)["aggregate"]["status"] == "complete"


def test_a_shortened_resealed_proposal_denominator_stops_the_first_consumer(tmp_path):
    """The fixture's a2 cannot silently disappear from the downstream denominator."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
    ):
        result = invoke_stage(root, "r", "happy", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    tree = RunTree(root, "r")
    seal_id = artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal")
    path = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal-seal", seal_id))
    seal = json.loads(path.read_text(encoding="utf-8"))
    seal["payload"]["expected_acts"] = seal["payload"]["expected_acts"][:1]
    seal["payload"]["count"] = 1
    seal["payload"]["self_hash"] = self_hash(seal["payload"])
    seal["self_hash"] = self_hash(seal)
    path.write_bytes(canonical_bytes(seal))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == 2
    assert "does not reconcile to every synthetic act" in result.stderr
    assert snapshot(root) == before


def test_recensor_refuses_duplicate_witness_attempt_ordinals_instead_of_selecting_one(tmp_path):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        result = invoke_stage(root, "r", "happy", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    tree = RunTree(root, "r")
    original = next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium" and entry["outcome"] == "read"
    )
    act_id = original["subject_id"]
    chair = original["payload"]["chair"]
    forged = json.loads(json.dumps(original))
    forged_attempt = attempt_id(act_id, f"read:{chair}", 2)
    forged["attempt_id"] = forged_attempt
    forged["artifact_id"] = artifact_id(ATTESTATORES, "testimonium", act_id, forged_attempt)
    # A new artifact identity with the old semantic ordinal is an ambiguity, not
    # an attempt 2 that the Recensor is allowed to choose among.
    forged["self_hash"] = self_hash(forged)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", forged["artifact_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(forged))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 2
    assert "duplicate attempt ordinal" in result.stderr
    assert snapshot(root) == before


def test_designator_refuses_a_commandless_recovery_recrop(tmp_path):
    root = tmp_path / "runs"
    # This must be a real outstanding request.  An empty tree would fail later
    # for lack of any Recensor record even if the CLI guard disappeared, making
    # the test a check of an error spelling rather than a check that the request
    # argument is required to authorize a crop.
    run_through_recensor(root, "r", "review", allow_held=True)
    tree = RunTree(root, "r")
    act_id = next(
        tree.read_artifact(RECENSOR, "recovery-request", entry["artifact_id"])["subject_id"]
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "recovery-request"
    )
    before = snapshot(root)

    result = invoke_stage(
        root,
        "r",
        "review",
        "pipeline/2_designator/run.py",
        operation="recover",
        act=act_id,
    )
    assert result.returncode == 2
    assert "exact Recensor recovery request" in result.stderr
    assert snapshot(root) == before


def test_designator_refuses_a_standalone_next_recovery_request(tmp_path):
    """Only the latest Recensor review can authorize its exact request.

    First make the legitimate first recrop and reread, then add a syntactically
    sound second request.  It has the right act, next ordinal, policy, and new
    Perlectio input, but no current review names it.  Before the cross-stage
    check, direct invocation of the crop author accepted this unreviewed request.
    """
    root = tmp_path / "runs"
    run_through_recensor(root, "r", "review", allow_held=True)
    tree = RunTree(root, "r")
    original = next(
        tree.read_artifact(RECENSOR, "recovery-request", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "recovery-request"
    )
    act_id = original["subject_id"]
    assert (
        invoke_stage(
            root,
            "r",
            "review",
            "pipeline/2_designator/run.py",
            operation="recover",
            act=act_id,
            recovery_request=original["artifact_id"],
        ).returncode
        == 0
    )
    assert (
        invoke_stage(
            root,
            "r",
            "review",
            "pipeline/4_perlector/run.py",
            act=act_id,
        ).returncode
        == 0
    )

    latest_reading = max(
        (
            tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
            for entry in tree.build_manifest(PERLECTOR)["artifacts"]
            if entry["kind"] == "perlectio" and entry["subject_id"] == act_id
        ),
        key=lambda record: record["payload"]["attempt_ordinal"],
    )
    reading_path = tree.artifact_path(PERLECTOR, "perlectio", latest_reading["artifact_id"])
    reading_ref = {
        "relative_path": reading_path,
        "sha256": digest_bytes(tree.read_bytes(reading_path)),
    }
    forged = json.loads(json.dumps(original))
    forged_attempt = attempt_id(act_id, "recover", 2)
    forged["attempt_id"] = forged_attempt
    forged["artifact_id"] = artifact_id(RECENSOR, "recovery-request", act_id, forged_attempt)
    forged["inputs"] = [reading_ref]
    forged["payload"]["attempt_ordinal"] = 2
    forged["payload"]["budget_used"] = 1
    forged["payload"]["perlectio_ref"] = reading_ref
    forged["self_hash"] = self_hash(forged)
    forged_path = tree.resolve(
        tree.artifact_path(RECENSOR, "recovery-request", forged["artifact_id"])
    )
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_bytes(canonical_bytes(forged))
    before = snapshot(root)

    result = invoke_stage(
        root,
        "r",
        "review",
        "pipeline/2_designator/run.py",
        operation="recover",
        act=act_id,
        recovery_request=forged["artifact_id"],
    )
    assert result.returncode == 2
    assert "not the exact current Recensor request" in result.stderr
    assert snapshot(root) == before


def test_designator_refuses_a_current_recovery_review_with_a_different_policy(tmp_path):
    """The review's policy is evidence, not a decorative copy of the request's."""
    root = tmp_path / "runs"
    run_through_recensor(root, "r", "review", allow_held=True)
    tree = RunTree(root, "r")
    request = next(
        tree.read_artifact(RECENSOR, "recovery-request", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "recovery-request"
    )
    review_entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "recovery-requested"
    )
    review_path = tree.resolve(review_entry["relative_path"])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["payload"]["recovery_policy"] = {"not": "the run-bound policy"}
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))
    before = snapshot(root)

    result = invoke_stage(
        root,
        "r",
        "review",
        "pipeline/2_designator/run.py",
        operation="recover",
        act=request["subject_id"],
        recovery_request=request["artifact_id"],
    )
    assert result.returncode == 2
    assert "run-bound policy" in result.stderr
    assert snapshot(root) == before


def test_armarium_rechecks_a_corpus_seal_tampered_after_designator(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    identity = artifact_id(EXEMPLAR, "seal", "corpus-seal")
    path = tree.resolve(tree.artifact_path(EXEMPLAR, "seal", identity))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["page_count"] = 999
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    before = snapshot(root)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/7_armarium/run.py"),
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
    assert result.returncode == 2
    assert "valid self-hashed census" in result.stderr
    assert snapshot(root) == before


def test_armarium_rechecks_sealed_pixels_tampered_after_designator(tmp_path):
    """The final export has its own pixel boundary, not only a census boundary."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    page = next(
        tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
        and tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])["outcome"] == "sealed"
    )
    tree.resolve(page["payload"]["image_path"]).write_bytes(b"altered after Designator")
    before = snapshot(root)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/7_armarium/run.py"),
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

    assert result.returncode == 2
    assert "changed under a sealed reference" in result.stderr
    assert snapshot(root) == before


def test_the_seal_carries_an_outcome_and_a_derived_continuation_for_every_act(happy_run):
    """The seal entry is the handoff contract: every entry names its Designator
    outcome, and `has_continuation` reports the regions actually cut, never the
    declaration — a claim of a continuation nothing holds is how half an act gets
    delivered as the act."""
    _, tree = happy_run
    seal = tree.read_artifact(
        DESIGNATOR, "proposal-seal", artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)
    )["payload"]
    by_key = {entry["act_key"]: entry for entry in seal["expected_acts"]}
    assert by_key["a1"]["outcome"] == "proposed"
    assert by_key["a2"]["outcome"] == "proposed"
    assert by_key["a1"]["has_continuation"] is False
    assert by_key["a2"]["has_continuation"] is True


def test_the_run_used_no_network_and_no_model(happy_run):
    """The adapters are all fakes, declared as such. A run that had reached a real
    model would carry a resolved identity that was not a `fake-*` recipe."""
    _, tree = happy_run
    run = tree.read_run()
    config = load_models_toml(ROOT / "config" / "models.toml")
    fixture = load_fixture(str(ROOT / "proof"))
    bindings = run_config_bindings(config, fixture, "happy")
    assert run["config_digest"] == bindings["config_digest"]
    assert run["witness_chairs"] == list(config.witness_chairs)
    assert run["adapter_recipes"] == dict(config.adapter_recipes)
    recipes = run["adapter_recipes"]
    assert len(recipes) == 8
    assert all(revision.startswith("fake-") for revision in recipes.values())
    # Every configured chair is a local-repository fixture: nothing here can have
    # reached Hugging Face, because no live chair names a repo at all.
    assert {
        chair.source for chair in config.chairs.values() if isinstance(chair, ChairIdentity)
    } == {"local-repository"}


def test_the_config_digest_still_binds_the_scenario_as_well_as_the_chairs(happy_run):
    """Spec 02 moved the roster into `config/models.toml`; it did not move the
    scenario out of the run's configuration digest. The two are distinct runs,
    and the digest has to say so before spec 01's third test below can refuse
    one under the other's run id *before any write*."""
    _, tree = happy_run
    config = load_models_toml(ROOT / "config" / "models.toml")
    fixture = load_fixture(str(ROOT / "proof"))

    happy = run_config_bindings(config, fixture, "happy")["config_digest"]
    review = run_config_bindings(config, fixture, "review")["config_digest"]

    assert tree.read_run()["config_digest"] == happy
    assert happy != review
    # And the fixture is in there too: same scenario, one changed act, new digest.
    altered = json.loads(json.dumps(fixture))
    altered["act"][0]["text"] = "SOMETHING ELSE ENTIRELY"
    assert run_config_bindings(config, altered, "happy")["config_digest"] != happy


def test_an_explicit_absent_witness_is_a_visible_not_run_and_counts_against_floor(tmp_path):
    """Exercise absence through real stage programs, not only the config parser."""
    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    live = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    configured = """[chairs.attestator_3]
state = \"configured\"
source = \"local-repository\"
path = \"attestator_3\"
digest_manifest = \"b9d6f5b6400e8aa36ecc35bad33cb4c54bb69b207e1ffdea39e1999cfa7e523a\"
manifest = \"manifests/attestator_3.json\"
serving_recipe = \"fake-attestatores-v0\"
license_note = \"fixture identity only; no model weights or model license apply\"
"""
    absent = """[chairs.attestator_3]
state = \"absent\"
reason = \"fixture test removes this witness without replacing it\"
"""
    assert configured in live
    models_config = config_root / "models.toml"
    models_config.write_text(live.replace(configured, absent), encoding="utf-8")

    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy", models_config=models_config)
    assert result.returncode == 3, result.stderr
    tree = RunTree(root, "r")
    testimonia = [
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
    ]
    absent_records = [
        record for record in testimonia if record["payload"]["chair"] == "attestator_3"
    ]
    assert len(absent_records) == 2
    assert all(record["outcome"] == "not-run" for record in absent_records)
    for record in absent_records:
        provenance = record["payload"]["provenance"]
        assert provenance["chair_state"] == "absent"
        assert provenance["absence"] == {
            "role": "attestator_3",
            "state": "absent",
            "reason": "fixture test removes this witness without replacing it",
        }
    export = export_of(tree)
    assert export["aggregate"]["status"] == "partial"
    assert all(item["under_witnessed"] is True for item in export["review"])
    assert tree.read_run()["witness_chairs"] == ["attestator_1", "attestator_2", "attestator_3"]


def test_perlector_refuses_a_tampered_testimonium_model_provenance(tmp_path):
    """#42 at the handoff: a sealed-looking witness cannot change its model pin."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    entry = next(
        entry
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
    )
    path = tree.resolve(entry["relative_path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["provenance"]["resolved_revision"] = {
        "kind": "digest-manifest",
        "value": "0" * 64,
    }
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/4_perlector/run.py"),
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
    assert result.returncode == 2
    assert "SchemaRefusal" in result.stderr
    assert "resolved revision" in result.stderr


def test_a_perlectio_retains_digest_checked_testimonia_it_used(tmp_path):
    """Changing a witness record after reading must stop the next real consumer."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        result = invoke_stage(root, "r", "happy", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    tree = RunTree(root, "r")
    testimony = next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium" and entry["outcome"] == "read"
    )
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", testimony["artifact_id"]))
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["payload"]["reported"] = "changed after Perlectio"
    changed["self_hash"] = self_hash(changed)
    path.write_bytes(canonical_bytes(changed))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 2
    assert "changed under a sealed reference" in result.stderr
    assert snapshot(root) == before


def test_recensor_refuses_a_completed_perlectio_without_an_object_region_basis(tmp_path):
    """A resealed malformed payload is an accounting refusal, never a traceback."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        result = invoke_stage(root, "r", "happy", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    tree = RunTree(root, "r")
    entry = next(
        entry
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio" and entry["outcome"] == "read"
    )
    path = tree.resolve(entry["relative_path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["basis"] = []
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "no object basis" in result.stderr
    assert snapshot(root) == before


def test_archetypus_refuses_a_resealed_completed_perlectio_without_an_object_basis(tmp_path):
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    review_entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    review_path = tree.resolve(review_entry["relative_path"])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    old_ref = review["payload"]["perlectio_ref"]
    reading_path = tree.resolve(old_ref["relative_path"])
    reading = json.loads(reading_path.read_text(encoding="utf-8"))
    reading["payload"]["basis"] = []
    reading["self_hash"] = self_hash(reading)
    reading_path.write_bytes(canonical_bytes(reading))
    new_ref = {
        "relative_path": old_ref["relative_path"],
        "sha256": digest_bytes(reading_path.read_bytes()),
    }
    review["inputs"] = [
        new_ref if reference == old_ref else reference for reference in review["inputs"]
    ]
    review["payload"]["perlectio_ref"] = new_ref
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "no object basis" in result.stderr
    assert snapshot(root) == before


def test_archetypus_uses_the_perlectio_named_by_the_accepted_review(tmp_path):
    """A newer unreviewed reading may not replace the reading Recensor assessed."""
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    # Choose a1 explicitly: it has an accepted review in the happy trace.
    review = next(
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    act_id = review["subject_id"]
    original = tree.read_artifact(
        PERLECTOR,
        "perlectio",
        review["payload"]["perlectio_ref"]["relative_path"].split("/")[-1].removesuffix(".json"),
    )
    forged = json.loads(json.dumps(original))
    forged_attempt = attempt_id(act_id, "perlegere", 2)
    forged["attempt_id"] = forged_attempt
    forged["artifact_id"] = artifact_id(PERLECTOR, "perlectio", act_id, forged_attempt)
    forged["payload"]["attempt_ordinal"] = 2
    forged["payload"]["text"] = "UNREVIEWED REPLACEMENT"
    forged["self_hash"] = self_hash(forged)
    forged_path = tree.resolve(tree.artifact_path(PERLECTOR, "perlectio", forged["artifact_id"]))
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_bytes(canonical_bytes(forged))

    result = invoke_stage(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 0, result.stderr
    established = tree.read_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", act_id)
    )
    assert established["payload"]["text"] == original["payload"]["text"]
    assert established["payload"]["text"] != forged["payload"]["text"]


def test_armarium_refuses_a_resealed_archetypus_text_that_disagrees_with_its_parent(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    entry = next(
        entry
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    )
    path = tree.resolve(entry["relative_path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["text"] = "ALTERED ESTABLISHED TEXT"
    record["payload"]["self_hash"] = self_hash(record["payload"])
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/7_armarium/run.py")
    assert result.returncode == 2
    assert "does not exactly preserve the Perlectio" in result.stderr
    assert snapshot(root) == before


def test_armarium_refuses_two_established_records_instead_of_selecting_one(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    original = next(
        tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    )
    act_id = original["subject_id"]
    forged = json.loads(json.dumps(original))
    forged_attempt = attempt_id(act_id, "establish", 2)
    forged["attempt_id"] = forged_attempt
    forged["artifact_id"] = artifact_id(ARCHETYPUS, "archetypus", act_id, forged_attempt)
    forged["self_hash"] = self_hash(forged)
    path = tree.resolve(tree.artifact_path(ARCHETYPUS, "archetypus", forged["artifact_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(forged))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/7_armarium/run.py")
    assert result.returncode == 2
    assert "carries 2 Archetypus records" in result.stderr
    assert snapshot(root) == before


# --- 2. Repeating the identical command changes nothing ------------------------


def test_repeating_the_identical_command_leaves_every_byte_unchanged(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    before = snapshot(root)

    assert len(before) == 42
    assert digest_of(before) == HAPPY_RUN_TREE_DIGEST
    assert orchestrate(root, "r", "happy").returncode == 0
    after = snapshot(root)

    assert after == before
    assert snapshot_digest(root) == HAPPY_RUN_TREE_DIGEST


def test_repeating_the_review_scenario_also_changes_nothing(tmp_path):
    """The scenario with a recovery loop in it is the one that can most easily
    append on every run — which it did, until the reading attempt stopped being a
    count of invocations."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "review").returncode == 3
    before = snapshot(root)

    assert len(before) == 46
    assert digest_of(before) == REVIEW_RUN_TREE_DIGEST
    assert orchestrate(root, "r", "review").returncode == 3
    assert snapshot(root) == before
    assert snapshot_digest(root) == REVIEW_RUN_TREE_DIGEST


# --- 3. An incompatible run id fails before writing ----------------------------


def test_reusing_a_run_id_with_a_changed_configuration_fails_before_writing(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    before = snapshot(root)

    # The scenario is part of the run's configuration digest, so the same run id
    # under a different scenario is a different run wearing an old name. Both
    # scenarios declare the *same* two source pages, so the source manifest
    # cannot be what catches this — only the config digest can, and this is the
    # assertion that says so.
    result = orchestrate(root, "r", "review")

    assert result.returncode != 0
    assert "IncompatibleReuse" in result.stderr
    assert "config_digest" in result.stderr, (
        "the refusal must name the changed binding; catching this later, on "
        "artifact immutability, is a refusal several stages after the first write"
    )
    assert snapshot(root) == before, "a refused reuse must leave the tree untouched"
    # And refused by the door — the stage that binds the run id — rather than by
    # some later stage discovering it cannot overwrite an artifact.
    assert "1_exemplar/door.py" in result.stderr


# --- 4. Resume reuses valid artifacts without rewriting them -------------------


def test_an_interrupted_run_resumes_without_rewriting_what_survived(tmp_path):
    """Interrupt for real: delete everything from the Perlector onward, as though
    the process died mid-run, then run the same command again."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    complete = snapshot(root)

    for stage_directory in ("4_perlector", "5_recensor", "6_archetypus", "7_armarium"):
        shutil.rmtree(root / "r" / stage_directory)
    survivors = snapshot(root)
    assert len(survivors) < len(complete)

    assert orchestrate(root, "r", "happy").returncode == 0
    resumed = snapshot(root)

    # Everything that survived is byte-identical: resume reused it rather than
    # redoing it. And the finished tree is identical to the uninterrupted one.
    for path, digest in survivors.items():
        assert resumed[path] == digest, f"{path} was rewritten on resume"
    assert resumed == complete


# --- 5. The review scenario preserves the whole history ------------------------


def artifacts(tree: RunTree, stage: str, kind: str) -> list[dict]:
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind
    ]


def test_the_recovered_act_keeps_one_identity_across_two_regions(review_run):
    """ARCHITECTURE invariant 1, driven end to end rather than unit-tested: act
    identity survives recropping, and the region identity does not."""
    _, tree = review_run
    regions = [
        record
        for record in artifacts(tree, DESIGNATOR, "region")
        if record["payload"]["act_key"] == "a1"
    ]
    assert len(regions) == 2
    assert len({record["subject_id"] for record in regions}) == 1
    assert len({record["payload"]["region_id"] for record in regions}) == 2
    assert {record["payload"]["origin"] for record in regions} == {"proposal", "recovery"}


def test_the_recovery_request_and_both_reading_attempts_survive(review_run):
    _, tree = review_run
    requests = artifacts(tree, RECENSOR, "recovery-request")
    assert len(requests) == 1
    assert requests[0]["payload"]["act_key"] == "a1"

    readings = [
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a1"
    ]
    assert sorted(record["payload"]["attempt_ordinal"] for record in readings) == [1, 2]


def test_both_recensor_outcomes_for_the_recovered_act_survive(review_run):
    """Nothing is lost inside a recovery loop: the request and the acceptance are
    both still there, in order."""
    _, tree = review_run
    reviews = [
        record
        for record in artifacts(tree, RECENSOR, "review")
        if record["payload"]["act_key"] == "a1"
    ]
    assert len(reviews) == 2
    by_ordinal = {record["payload"]["attempt_ordinal"]: record["outcome"] for record in reviews}
    assert by_ordinal == {1: "recovery-requested", 2: "accepted"}


def test_recovery_ink_is_recorded_as_witness_uncovered(review_run):
    """The recrop uncovered ink no witness ever saw. Saying so is the difference
    between a gap in the record and a gap nobody can see."""
    _, tree = review_run
    latest = max(
        (
            record
            for record in artifacts(tree, PERLECTOR, "perlectio")
            if record["payload"]["act_key"] == "a1"
        ),
        key=lambda record: record["payload"]["attempt_ordinal"],
    )
    coverage = [basis["witness_covered"] for basis in latest["payload"]["basis"]["regions"]]
    assert coverage == [True, False]


def test_the_cross_page_act_is_witnessed_on_both_sides_of_the_break(review_run):
    """A continuation is part of the original proposal, not a later attempt. A
    witness shown only the near side would have read half an act while the record
    said it read the act."""
    _, tree = review_run
    reading = next(
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a2"
    )
    regions = reading["payload"]["basis"]["regions"]
    assert len(regions) == 2
    assert all(basis["witness_covered"] for basis in regions)


def test_recovery_stayed_inside_its_budget(review_run):
    _, tree = review_run
    requests = artifacts(tree, RECENSOR, "recovery-request")
    assert len(requests) == 1
    assert requests[0]["payload"]["budget_allowed"] <= 3, "the absolute cap is a ruling"


# --- 6. The held act cannot look complete --------------------------------------


def test_the_held_act_has_no_archetypus_at_all(review_run):
    """The absence is the evidence. An export that showed a held act as delivered
    would have to invent a record that does not exist."""
    _, tree = review_run
    established = artifacts(tree, ARCHETYPUS, "archetypus")
    assert len(established) == 1
    assert established[0]["payload"]["act_key"] == "a1"


def test_the_held_act_appears_in_the_review_output_and_forces_partial(review_run):
    _, tree = review_run
    export = export_of(tree)
    assert export["aggregate"]["status"] == "partial"
    assert len(export["review"]) == 1
    assert export["review"][0]["act_key"] == "a2"
    assert export["review"][0]["category"] == "held-for-review"
    assert len(export["delivered"]) == 1
    assert "act a2 is held-for-review" in export["aggregate"]["reasons"]


def test_no_delivered_entry_carries_a_witness_reading_as_its_text(review_run):
    """GOALS 3: a witness reading is never itself an output. The established text
    must not equal any witness's reported words *by accident of the fixture*
    either, so the fixture deliberately makes two chairs disagree."""
    _, tree = review_run
    export = export_of(tree)
    testimony = {
        record["payload"]["reported"]
        for record in artifacts(tree, ATTESTATORES, "testimonium")
        if record["outcome"] == "read" and record["payload"]["act_key"] == "a1"
    }
    delivered = export["delivered"][0]["text"]
    disagreeing = [reported for reported in testimony if reported != delivered]
    assert len(disagreeing) == 2, "the fixture must keep dissent exercisable"


def test_the_failed_chair_is_visible_in_the_export(review_run):
    """Sol B-2 / blocker 4, driven end to end: `failed` is a real member of the
    closed vocabulary and reaches the export as a named shortfall."""
    _, tree = review_run
    export = export_of(tree)
    held = export["review"][0]
    assert held["under_witnessed"] is True
    assert held["witness_coverage"]["by_outcome"]["failed"] == 1
    assert held["witness_coverage"]["by_class"] == {"completed": 2, "unresolved": 0, "failed": 1}
    assert any("under-witnessed" in reason for reason in export["aggregate"]["reasons"])


def test_a_delivered_act_still_links_back_to_the_exact_ink(review_run):
    _, tree = review_run
    delivered = export_of(tree)["delivered"][0]
    source_by_ordinal = {row["ordinal"]: row for row in tree.read_run()["source_manifest"]}
    assert len(delivered["source_regions"]) == 2
    for region in delivered["source_regions"]:
        assert region["image_sha256"]
        assert region["region_id"].startswith("rgn_")
        assert tree.read_bytes(region["image_path"])
        source = source_by_ordinal[region["source_page_ordinal"]]
        assert region["declared_path"] == source["relative_path"]
        assert region["declared_sha256"] == source["sha256"]


# --- 7. Every one of the seven handoffs refuses corruption ---------------------

# (producer, consumer, the artifact kind that crosses this boundary)
HANDOFF_ARTIFACTS = (
    (DOOR, EXEMPLAR, "admission"),
    (EXEMPLAR, DESIGNATOR, "page"),
    (DESIGNATOR, ATTESTATORES, "region"),
    (ATTESTATORES, PERLECTOR, "testimonium"),
    (PERLECTOR, RECENSOR, "perlectio"),
    (RECENSOR, ARCHETYPUS, "review"),
    (ARCHETYPUS, ARMARIUM, "archetypus"),
)

CONSUMER_PROGRAMS = {
    EXEMPLAR: "pipeline/1_exemplar/run.py",
    DESIGNATOR: "pipeline/2_designator/run.py",
    ATTESTATORES: "pipeline/3_attestatores/run.py",
    PERLECTOR: "pipeline/4_perlector/run.py",
    RECENSOR: "pipeline/5_recensor/run.py",
    ARCHETYPUS: "pipeline/6_archetypus/run.py",
    ARMARIUM: "pipeline/7_armarium/run.py",
}


def one_artifact(tree: RunTree, stage: str, kind: str) -> tuple[Path, dict]:
    entries = [entry for entry in tree.build_manifest(stage)["artifacts"] if entry["kind"] == kind]
    assert entries, f"{stage} produced no {kind} to corrupt"
    entry = entries[0]
    return tree.resolve(entry["relative_path"]), tree.read_artifact(
        stage, kind, entry["artifact_id"]
    )


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_validator_refuses_a_corrupted_schema(happy_run, producer, consumer, kind):
    _, tree = happy_run
    _, record = one_artifact(tree, producer, kind)
    record["schema"] = "skeleton.v99"
    with pytest.raises(SchemaRefusal):
        validate_envelope(record)


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_validator_refuses_a_malformed_identity(happy_run, producer, consumer, kind):
    _, tree = happy_run
    _, record = one_artifact(tree, producer, kind)
    record["artifact_id"] = "art_not_a_real_identity"
    with pytest.raises(SchemaRefusal):
        validate_envelope(record)


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_validator_refuses_duplicate_accounting(happy_run, producer, consumer, kind):
    """A duplicate reference is how one page gets counted twice and a conservation
    check passes over something nobody read."""
    _, tree = happy_run
    _, record = one_artifact(tree, producer, kind)
    assert record["inputs"], (
        f"{producer} {kind} references no input, so this boundary carries nothing "
        "verifiable. Skipping here would be a skip-list, which is how a gap goes "
        "unnoticed (#87) — the producer should name the bytes it acted on"
    )
    record["inputs"] = record["inputs"] + [dict(record["inputs"][0])]
    with pytest.raises(SchemaRefusal):
        validate_envelope(record)


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_validator_refuses_bytes_that_changed_under_a_sealed_reference(
    happy_run, producer, consumer, kind
):
    _, tree = happy_run
    _, record = one_artifact(tree, producer, kind)
    assert record["inputs"], f"{producer} {kind} names no bytes to tamper with"
    reference = record["inputs"][0]
    with pytest.raises(SchemaRefusal):
        verify_input_bytes(reference, b"tampered")


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_corruption_stops_its_named_real_consumer(
    happy_run, tmp_path, producer, consumer, kind
):
    """The validator matrix above is not evidence that a consumer calls it.

    Give every contract edge a fresh complete tree, damage its producer record on
    disk, and invoke the particular downstream program named by the handoff.  A
    generic test that merely calls ``validate_envelope`` can stay green while a
    stage bypasses the boundary entirely; this one cannot.
    """
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    path, record = one_artifact(tree, producer, kind)
    record["schema"] = "skeleton.v99"
    path.write_bytes(canonical_bytes(record))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", CONSUMER_PROGRAMS[consumer])
    assert result.returncode != 0
    assert "skeleton.v99" in result.stderr or "SchemaRefusal" in result.stderr
    assert snapshot(root) == before


def test_recovery_policy_is_a_run_bound_configuration_not_a_late_local_default(tmp_path):
    """A policy change must refuse the old run before any stage can reinterpret it."""
    root = tmp_path / "runs"
    policy = tmp_path / "recovery.toml"
    policy.write_text((ROOT / "config/recovery.toml").read_text(encoding="utf-8"), encoding="utf-8")
    assert orchestrate(root, "r", "happy", recovery_config=policy).returncode == 0
    before = snapshot(root)

    policy.write_text(
        "absolute_cap = 3\n\n[budget]\nfallback_recrop = 0\npage_level_reread = 1\n",
        encoding="utf-8",
    )
    result = orchestrate(root, "r", "happy", recovery_config=policy)
    assert result.returncode == 2
    assert "different config_digest" in result.stderr
    assert snapshot(root) == before


@pytest.mark.full
def test_every_handoff_in_the_contract_is_covered_by_this_table():
    """Meta-invariant #91 — a drift check over an agreement surface. If a handoff
    is added to the contracts and not to this table, the boundary test would
    silently cover six of seven."""
    from common.contracts.stages import HANDOFFS

    assert {(producer, consumer) for producer, consumer, _ in HANDOFF_ARTIFACTS} == set(HANDOFFS)
    assert {consumer for _, consumer, _ in HANDOFF_ARTIFACTS} == set(CONSUMER_PROGRAMS)
    assert len(HANDOFF_ARTIFACTS) == 7


def test_the_run_authority_is_never_rewritten_by_any_stage(happy_run):
    root, tree = happy_run
    stored = json.loads((root / "r" / "run.json").read_text(encoding="utf-8"))
    assert stored == tree.read_run()


def test_a_stage_invoked_before_its_producer_refuses_rather_than_inventing(tmp_path):
    """Order is not a convention here. A stage run out of sequence has nothing to
    read, and must say so instead of producing an empty success."""
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "2_designator" / "run.py"),
            "--run-root",
            str(tmp_path),
            "--run-id",
            "never-created",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "IncompatibleReuse" in result.stderr or "ContractError" in result.stderr


def test_contract_error_is_the_only_way_a_stage_reports_refusal():
    """A stage that crashed with a traceback and exited zero would be the vacuous
    green this project exists to notice."""
    assert issubclass(SchemaRefusal, ContractError)


# --- 8. A refused page cannot vanish, and no act rides out over one -------------
#
# The defect all four reviewers filed first: the Designator skipped any act whose
# page was not sealed, wrote nothing for it anywhere, and sealed a shorter
# expected-act list — so the one conservation check in the pipeline reconciled
# perfectly against a record of the loss's absence. A run that lost a whole page
# reported `status: complete, reasons: []`.


@pytest.fixture(scope="module")
def refused_page_run(tmp_path_factory):
    """Page 2 is refused at the door, so a2's continuation cannot be cut."""
    root = tmp_path_factory.mktemp("refused_page")
    result = orchestrate(root, "r", "refused-page")
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r")


@pytest.fixture(scope="module")
def refused_first_page_run(tmp_path_factory):
    """Page 1 — the page both acts live on — is refused at the door."""
    root = tmp_path_factory.mktemp("refused_first_page")
    result = orchestrate(root, "r", "refused-first-page")
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r")


def proposal_seal(tree: RunTree) -> dict:
    return tree.read_artifact(
        DESIGNATOR, "proposal-seal", artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)
    )["payload"]


def test_the_door_really_refused_page_two_through_its_own_inspection(refused_page_run):
    _, tree = refused_page_run
    refusals = [
        record for record in artifacts(tree, EXEMPLAR, "page") if record["outcome"] == "refused"
    ]
    assert len(refusals) == 1
    assert refusals[0]["payload"]["ordinal"] == 2
    assert "digest" in refusals[0]["payload"]["reason"]


def test_the_page_loss_is_named_and_the_run_is_partial(refused_page_run):
    _, tree = refused_page_run
    export = export_of(tree)
    assert export["aggregate"]["status"] == "partial"
    assert any(
        reason.startswith("page 2 was refused:") for reason in export["aggregate"]["reasons"]
    )
    assert export["aggregate"]["by_page_outcome"] == {"sealed": 1, "refused": 1}


def test_the_act_with_the_lost_continuation_is_held_not_delivered(refused_page_run):
    """`has_continuation` is derived from the regions actually cut, so it may not
    claim a continuation nothing holds — and the act whose far side is on the
    lost page is held rather than delivered as a complete reading of half its ink."""
    _, tree = refused_page_run
    seal = proposal_seal(tree)
    by_key = {entry["act_key"]: entry for entry in seal["expected_acts"]}
    assert set(by_key) == {"a1", "a2"}, "the seal must still name every declared act"
    assert by_key["a1"]["outcome"] == "proposed"
    assert by_key["a2"]["outcome"] == "held"
    assert by_key["a2"]["has_continuation"] is False

    export = export_of(tree)
    assert [item["act_key"] for item in export["delivered"]] == ["a1"]
    assert [item["act_key"] for item in export["review"]] == ["a2"]
    assert export["review"][0]["category"] == "held-for-review"


def test_the_hold_is_a_real_artifact_naming_the_lost_page(refused_page_run):
    _, tree = refused_page_run
    holds = artifacts(tree, DESIGNATOR, "hold")
    assert len(holds) == 1
    assert holds[0]["outcome"] == "held"
    assert holds[0]["payload"]["act_key"] == "a2"
    assert "page 2" in holds[0]["payload"]["reason"]
    assert holds[0]["inputs"], "the hold must reference the refusal it rests on"


def test_no_witness_and_no_reading_pretends_to_have_seen_the_held_act(refused_page_run):
    """The held act is not silently skipped: every configured chair records an
    explicit not-run, and the Perlector acknowledges the act without reading it —
    a reading of the near side alone would be a truncation delivered as an output."""
    _, tree = refused_page_run
    testimonia = [
        record
        for record in artifacts(tree, ATTESTATORES, "testimonium")
        if record["payload"]["act_key"] == "a2"
    ]
    assert len(testimonia) == 3
    assert {record["outcome"] for record in testimonia} == {"not-run"}

    readings = [
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a2"
    ]
    assert len(readings) == 1
    assert readings[0]["outcome"] == "not-run"
    assert "text" not in readings[0]["payload"]

    established = artifacts(tree, ARCHETYPUS, "archetypus")
    assert [record["payload"]["act_key"] for record in established] == ["a1"]


def test_the_refused_page_scenario_is_deterministic_on_rerun(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "refused-page").returncode == 3
    before = snapshot(root)
    assert orchestrate(root, "r", "refused-page").returncode == 3
    assert snapshot(root) == before


def test_losing_the_first_page_holds_every_act_and_delivers_nothing(refused_first_page_run):
    """Half one of the defect, driven end to end: an act whose own page was never
    sealed used to disappear from the seal entirely. Now it appears, held, with a
    hold artifact each, and the run is partial with the page loss named."""
    _, tree = refused_first_page_run
    seal = proposal_seal(tree)
    assert {entry["act_key"]: entry["outcome"] for entry in seal["expected_acts"]} == {
        "a1": "held",
        "a2": "held",
    }
    assert artifacts(tree, DESIGNATOR, "region") == [], (
        "no region may be cut for an act that cannot be fully marked out — an "
        "orphan continuation crop would be evidence of an act nothing accounts for"
    )
    assert len(artifacts(tree, DESIGNATOR, "hold")) == 2

    export = export_of(tree)
    assert export["aggregate"]["status"] == "partial"
    assert any(
        reason.startswith("page 1 was refused:") for reason in export["aggregate"]["reasons"]
    )
    assert export["delivered"] == []
    assert [item["category"] for item in export["review"]] == [
        "held-for-review",
        "held-for-review",
    ]
    entries = [
        entry
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "manifest-entry"
    ]
    assert len(entries) == 2, "conservation: every expected act still has exactly one category"


def test_the_recensor_refuses_a_continuation_claim_with_one_region(tmp_path):
    """Defence in depth for half two: if the seal claims a continuation and the
    tree holds only one proposal region — drift, tampering, or a future bug —
    the Recensor holds the act rather than accepting a half reading."""
    root = tmp_path / "runs"
    for name, program in (
        ("door", "pipeline/1_exemplar/door.py"),
        ("exemplar", "pipeline/1_exemplar/run.py"),
        ("designator", "pipeline/2_designator/run.py"),
    ):
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
        assert result.returncode == 0, f"{name}: {result.stderr}"

    tree = RunTree(root, "r")
    continuations = [
        entry
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region"
        and tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])["payload"]["transform"][
            "source_page_ordinal"
        ]
        == 2
    ]
    assert len(continuations) == 1
    continuation = continuations[0]
    seal_path = tree.resolve(
        tree.artifact_path(
            DESIGNATOR,
            "proposal-seal",
            artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal"),
        )
    )
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    # Model an internally resealed bad producer, not a bare deleted file: all
    # direct references are coherent, but the proposal still asserts that a2
    # continues while only its near-side region remains.  This reaches
    # Recensor's own defence rather than the earlier store-integrity guard.
    seal["inputs"] = [
        reference
        for reference in seal["inputs"]
        if reference["relative_path"] != continuation["relative_path"]
    ]
    for act in seal["payload"]["expected_acts"]:
        if act["act_id"] == continuation["subject_id"]:
            act["evidence"] = [
                reference
                for reference in act["evidence"]
                if reference["relative_path"] != continuation["relative_path"]
            ]
    seal["payload"]["self_hash"] = self_hash(seal["payload"])
    seal["self_hash"] = self_hash(seal)
    seal_path.write_bytes(canonical_bytes(seal))
    tree.resolve(continuations[0]["relative_path"]).unlink()
    tree.write_manifest(DESIGNATOR)

    for name, program in (
        ("attestatores", "pipeline/3_attestatores/run.py"),
        ("perlector", "pipeline/4_perlector/run.py"),
    ):
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
        assert result.returncode == 0, f"{name}: {result.stderr}"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "5_recensor" / "run.py"),
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
    assert result.returncode == 3, result.stderr

    reviews = [
        record
        for record in artifacts(tree, RECENSOR, "review")
        if record["payload"]["act_key"] == "a2"
    ]
    assert len(reviews) == 1
    assert reviews[0]["outcome"] == "held-for-review"
    assert "continuation" in reviews[0]["payload"]["reason"]


# --- A reading that did not succeed ---------------------------------------------


@pytest.fixture(scope="module")
def truncated_reading_run(tmp_path_factory):
    """Act a1's reading is `truncated`: a failed-class Perlector outcome that still
    carries the text it managed. That combination is the dangerous one — the
    Recensor used to ask only whether a reading *existed*, and the Archetypus
    copied the text out of whichever reading was latest."""
    root = tmp_path_factory.mktemp("truncated_reading")
    result = orchestrate(root, "r", "truncated-reading")
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r")


def test_a_reading_that_did_not_succeed_is_held_and_says_why(truncated_reading_run):
    """GOALS 2 is accuracy against the ink, and GOVERNANCE 2 refuses a loss hidden
    behind a successful status. Text nobody successfully read is neither, so it is
    held — visibly, with the outcome that caused it named in the reason."""
    _, tree = truncated_reading_run
    reviews = [
        record
        for record in artifacts(tree, RECENSOR, "review")
        if record["payload"]["act_key"] == "a1"
    ]
    assert len(reviews) == 1
    assert reviews[0]["outcome"] == "held-for-review"
    assert "truncated" in reviews[0]["payload"]["reason"]


def test_the_truncated_reading_never_becomes_established_text(truncated_reading_run):
    """The whole point: the reading exists and carries text, and none of it reaches
    the one place that turns a reading into the established text."""
    _, tree = truncated_reading_run
    readings = [
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a1"
    ]
    assert len(readings) == 1
    assert readings[0]["outcome"] == "truncated"
    assert readings[0]["payload"]["text"], "the reading must really carry text to be a hazard"

    established = [
        record
        for record in artifacts(tree, ARCHETYPUS, "archetypus")
        if record["payload"]["act_key"] == "a1"
    ]
    assert established == [], "a failed reading may not be established"

    export = export_of(tree)
    assert [item["act_key"] for item in export["review"]] == ["a1"]
    assert export["aggregate"]["status"] == "partial"


def test_the_act_whose_reading_succeeded_is_still_delivered(truncated_reading_run):
    """Invariant #14 — the refusal must not have bought its strictness by refusing
    good readings too. a2 read cleanly in the same run and is established."""
    _, tree = truncated_reading_run
    export = export_of(tree)
    assert [item["act_key"] for item in export["delivered"]] == ["a2"]
    established = [
        record
        for record in artifacts(tree, ARCHETYPUS, "archetypus")
        if record["payload"]["act_key"] == "a2"
    ]
    assert len(established) == 1
    assert established[0]["outcome"] == "established"


def test_the_recensor_refuses_a_testimonium_from_a_chair_the_run_never_sealed(tmp_path):
    """The mirror of the vanished-chair hole, and the one that counts toward the floor.

    `chair_outcomes` reports every role it finds a testimonium for, and
    `witness_coverage` counts completed-class outcomes without asking where they
    came from. So a testimonium under a role `run.json` never named raised the
    completed count: two real witnesses and one stranger read as three, and
    `under_witnessed` came back False on a run that was genuinely short a witness.
    Found by CodeRabbit on pull request 16.
    """
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
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
        assert result.returncode == 0, f"{program}: {result.stderr}"

    # Forge one: an existing testimonium re-published under a chair name the run
    # was never sealed with. Written straight into the tree, because no stage
    # would produce it — that is the point of checking rather than trusting.
    tree = RunTree(root, "r")
    entry = next(
        record
        for record in artifacts(tree, ATTESTATORES, "testimonium")
        if record["payload"]["chair"] == "attestator_1"
    )
    forged = json.loads(json.dumps(entry))
    forged["payload"]["chair"] = "attestator_9"
    forged["attempt_id"] = attempt_id(forged["subject_id"], "read:attestator_9", 1)
    forged["artifact_id"] = artifact_id(
        ATTESTATORES,
        "testimonium",
        forged["subject_id"],
        forged["attempt_id"],
    )
    forged["self_hash"] = self_hash(forged)
    path = tree.resolve(
        f"{STAGE_DIRECTORIES[ATTESTATORES]}/artifacts/testimonium/{forged['artifact_id']}.json"
    )
    path.write_text(json.dumps(forged), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "5_recensor" / "run.py"),
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
    assert result.returncode != 0, "an unsealed chair was accepted into the coverage count"
    assert "attestator_9" in result.stderr
    assert "not sealed with" in result.stderr


def test_armarium_rechecks_the_filename_a_page_was_sealed_under(tmp_path):
    """The last boundary compares each page against `run.json`'s ledger row itself.

    Distinct from the pixel recheck above, and from the corpus-seal recheck: those
    two cover a sealed page's bytes and the census as a whole. This covers the
    filename and digest a page artifact says it came from, which is the link ruling
    1 is about — "we literally need the file name. That is how we link it." A page
    that reaches the export naming a different source than the one submitted is an
    export nobody can trace back, and it is the *refused* pages that nothing else
    would catch: they carry no pixels for the pixel boundary to check.
    """
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    entry = next(
        entry for entry in tree.build_manifest(EXEMPLAR)["artifacts"] if entry["kind"] == "page"
    )
    path = tree.resolve(entry["relative_path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["declared_path"] = "some-other-scan.png"
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    seal_path = tree.resolve(
        tree.artifact_path(
            EXEMPLAR,
            "seal",
            artifact_id(EXEMPLAR, "seal", "corpus-seal"),
        )
    )
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    for reference in seal["inputs"]:
        if reference["relative_path"] == entry["relative_path"]:
            reference["sha256"] = digest_bytes(path.read_bytes())
    seal["self_hash"] = self_hash(seal)
    seal_path.write_bytes(canonical_bytes(seal))
    tree.write_manifest(EXEMPLAR)
    before = snapshot(root)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/7_armarium/run.py"),
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

    assert result.returncode == 2
    assert "no longer matches its submitted filename and digest" in result.stderr
    assert snapshot(root) == before
