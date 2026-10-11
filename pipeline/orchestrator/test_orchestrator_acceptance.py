"""Spec 01's seven acceptance tests, driven over the real pipeline.

Meta-invariant #86, verbatim: "A fix proven only on a fixture is not proven."
Load-bearing tests drive REAL producers — real CLIs, real argv, real subprocesses —
over REAL sealed artifacts. Nothing here imports a stage and calls its main(); every
run below shells out exactly as the operator would, so a stage that only works when
imported would fail here rather than pass.

Meta-invariant #88: no test reports success over an empty population. Every loop
asserts an exact expected count.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from argparse import Namespace
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_STORED, BadZipFile, ZipFile

import pytest

from common.chairs import ChairIdentity, load_models_toml
from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of, self_hash
from common.contracts.envelope import build_envelope, validate_envelope, verify_input_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import (
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    CONIECTOR,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    INK_MAP,
    PERLECTOR,
    RECENSOR,
    STAGES,
    WRITING_DIRECTORIES,
)
from common.credentials import looks_like_credential_env
from common.hard_failure import load_hard_failure_policy, tally_hard_failures
from common.imaging import PNG_SIGNATURE, decode_grayscale_png
from common.runtree.store import RunTree
from common.sealed_config import read_sealed_toml
from common.stage import (
    DEFAULT_SERVING_RECIPES_CONFIG_PATH,
    EXIT_FATAL,
    EXIT_HELD,
    _validate_decode_environment,
    load_fixture,
    run_config_bindings,
    run_sealed_config_digests,
    verify_final_seal,
)
from conftest import (
    HELD_RECENSOR_STOP,
    advance_held_recensor,
    file_identities,
    is_immutable_evidence,
    load_stage,
    programs_through,
    rewitness_stage_boundary,
)
from conftest import file_digest_snapshot as snapshot
from operations.operator import surface, volume_s3
from operations.operator.surface import credential_free_environment
from operations.submit import gate, submit

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE = "synthetic-two-page-v0"


# Each digest covers a whole run tree's relative-path -> file-digest inventory. A
# change to what a run writes re-pins them in the same commit; they are never
# loosened, because "nothing changed" must not be satisfiable by a run that is
# internally consistent but no longer the run these tests describe.
#
# To re-pin: run the two `test_repeating_*` tests below, confirm the file-count
# and exit-code assertions still pass while only the digest assertions fail, and
# copy the measured values here. A moved file count means an artifact appeared
# or went missing, which is a real change to explain, not a golden update.
#
# Things that move the digests without any change in behaviour:
#   - any value in a config file the run seals, provenance prose included: a
#     TOML file's seal covers what it says (comments and layout move nothing),
#     and a non-TOML sealed file's bytes;
#   - any code change in `common/page_prompt.py`, whose code digest
#     (`builder_sha256`) is sealed into every page feed's prompt record;
#   - any string sealed into a record or the export manifest.
#
# The review pins are the page-read `page-review` scenario's tree, whose page 2
# the committed re-ask budget asks once more. Its Recensor holds, so the run
# stops there, before the Archetypus: the tree has no Archetypus or Armarium
# record. Both trees carry the Coniector's records, since the committed
# reconstruction config runs it: a call per page, a reconstruction per act, and
# the reconstructor's receipt.
HAPPY_SNAPSHOT_FILES = 142
REVIEW_SNAPSHOT_FILES = 133
HAPPY_RUN_TREE_DIGEST = "a41bf70af765952749c11d68614686ead8984527a178ed37649e6c12f27144b7"
REVIEW_RUN_TREE_DIGEST = "06bfef99744ef5a49f4d2b066ca4f042a66c6eedda0316fe1f33d1419eddcea0"


def orchestrate_to_export(
    run_root: Path, run_id: str, scenario: str, **options
) -> subprocess.CompletedProcess:
    """`orchestrate`, and when it stops at a held Recensor, advance that seal and run again.

    For a test about what the export says of a held run, not about the stop.
    """
    result = orchestrate(run_root, run_id, scenario, **options)
    if result.returncode == 3 and HELD_RECENSOR_STOP in result.stdout:
        advance_held_recensor(run_root, run_id)
        result = orchestrate(run_root, run_id, scenario, **options)
    return result


def orchestrate(
    run_root: Path,
    run_id: str,
    scenario: str,
    *,
    models_config: Path | None = None,
    serving_recipes_config: Path | None = None,
    hard_failure_config: Path | None = None,
    submission_folder: Path | None = None,
    submission_manifest: Path | None = None,
    data_gate_policy: Path | None = None,
    placement_tier: str | None = None,
    stage_timing_journal: Path | None = None,
    repository_commit: str | None = None,
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
    if serving_recipes_config is not None:
        command.extend(("--serving-recipes-config", str(serving_recipes_config)))
    if hard_failure_config is not None:
        command.extend(("--hard-failure-config", str(hard_failure_config)))
    if submission_folder is not None:
        command.extend(("--submission-folder", str(submission_folder)))
    if submission_manifest is not None:
        command.extend(("--submission-manifest", str(submission_manifest)))
    if data_gate_policy is not None:
        command.extend(("--data-gate-policy", str(data_gate_policy)))
    if placement_tier is not None:
        command.extend(("--placement-tier", placement_tier))
    if stage_timing_journal is not None:
        command.extend(("--stage-timing-journal", str(stage_timing_journal)))
    if repository_commit is not None:
        command.extend(("--repository-commit", repository_commit))
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _real_submission(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    approved = tmp_path / "approved-storage"
    source = approved / "submitted-pages"
    source.mkdir(parents=True)
    for name in ("page-1.png", "page-2.png"):
        shutil.copyfile(ROOT / "proof" / "fixtures" / FIXTURE / name, source / name)
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(approved)]
    policy_path = tmp_path / "data-gate-policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    manifest = approved / "submission-ledger.json"
    submit.submit(source, manifest, policy_path=policy_path)
    return approved, source, manifest, policy_path


def test_orchestrator_carries_a_real_submission_to_the_door_end_to_end(tmp_path):
    approved, source, manifest, policy = _real_submission(tmp_path)
    result = orchestrate(
        approved / "runs",
        "real-ingress",
        "happy",
        submission_folder=source,
        submission_manifest=manifest,
        data_gate_policy=policy,
    )

    # This unit ends at the Door. This orchestration seals the fixture catalogue,
    # whose detector rows answer only a synthetic run, so the Designator refuses
    # a real submission by name rather than answering it from declared rows.
    assert result.returncode != 0
    assert "a fixture row answers only a synthetic run" in result.stderr
    run_record = RunTree(approved / "runs", "real-ingress").read_run()
    assert run_record["ingress"] == {"mode": "real"}

    # Only the sealed digest proves the gate evaluated the caller's policy.
    assert run_sealed_config_digests(run_record)["data-handling"] == digest_bytes(
        policy.read_bytes()
    )
    assert digest_bytes(policy.read_bytes()) != digest_bytes(
        gate.DEFAULT_POLICY_PATH.read_bytes()
    ), "the caller's policy must differ from the default, or the check above proves nothing"


def test_real_designator_refuses_a_missing_ink_map_boundary(tmp_path):
    """Real ingress must not bypass the producer inserted immediately before it."""
    approved, source, manifest, policy = _real_submission(tmp_path)
    root = approved / "runs"
    first = orchestrate(
        root,
        "real-ink-map-boundary",
        "happy",
        submission_folder=source,
        submission_manifest=manifest,
        data_gate_policy=policy,
    )
    assert "a fixture row answers only a synthetic run" in first.stderr

    tree = RunTree(root, "real-ink-map-boundary")
    _stage_seal_path(tree, INK_MAP).unlink()
    before = snapshot(tree.root)

    result = invoke_stage(
        root,
        "real-ink-map-boundary",
        "happy",
        "pipeline/2_designator/run.py",
    )

    assert result.returncode == EXIT_FATAL
    assert "predecessor ink-map has no stage-seal" in result.stderr
    assert "never re-derived" in result.stderr
    assert snapshot(tree.root) == before


def test_orchestrator_preserves_the_manifest_without_folder_refusal(tmp_path):
    approved, _source, manifest, policy = _real_submission(tmp_path)
    result = orchestrate(
        approved / "runs",
        "manifest-without-folder",
        "happy",
        submission_manifest=manifest,
        data_gate_policy=policy,
    )

    assert result.returncode != 0
    assert (
        "submission filename ledger is meaningful only with a real submission folder"
        in result.stderr
    )


def test_orchestrator_refuses_a_data_gate_policy_without_a_real_folder(tmp_path):
    result = orchestrate(
        tmp_path / "runs",
        "policy-without-folder",
        "happy",
        data_gate_policy=tmp_path / "policy-that-must-not-be-ignored.json",
    )

    assert result.returncode != 0
    assert "--data-gate-policy is meaningful only with --submission-folder" in result.stderr
    assert not (tmp_path / "runs" / "policy-without-folder" / "run.json").exists()


REAL_INGRESS_FLAGS = frozenset(
    {"--submission-folder", "--submission-manifest", "--data-gate-policy"}
)


def _orchestrator_namespace_fields(tmp_path: Path) -> dict:
    return dict(
        run_root=tmp_path / "runs",
        run_id="r",
        scenario="happy",
        fixture_root=ROOT / "proof",
        models_config=ROOT / "config" / "models.toml",
        # The constant, not a second spelling of the path. This stand-in feeds
        # mocked subprocess tests, so a catalogue that moved with only
        # `DEFAULT_SERVING_RECIPES_CONFIG_PATH` updated would leave them passing
        # while handing every stage a path that is not there. The neighbouring
        # literals predate this branch and are left as they are.
        serving_recipes_config=DEFAULT_SERVING_RECIPES_CONFIG_PATH,
        # `invoke` reads this by name like every sibling flag (the getattr
        # fallback that once papered over its absence here is gone), so the
        # stand-in must carry it or it is not the argv surface it mirrors.
        decoding_config=ROOT / "config" / "decoding.toml",
        pdf_render_config=ROOT / "config" / "pdf_render.toml",
        designator_geometry_config=ROOT / "config" / "designator_geometry.toml",
        alignment_config=ROOT / "config" / "alignment.toml",
        page_accounting_config=ROOT / "config" / "page_accounting.toml",
        reconstruction_config=ROOT / "config" / "reconstruction.toml",
        ink_map_config=ROOT / "config" / "ink_map.toml",
        # The Armarium's formats policy is `config/formats.toml`
        # (`common/armarium_formats.DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH`, what
        # `--formats-config` defaults to): a stand-in that mirrors the argv
        # surface names the surface's own path.
        formats_config=ROOT / "config" / "formats.toml",
        recovery_config=ROOT / "config" / "recovery.toml",
        hard_failure_config=ROOT / "config" / "hard_failure.toml",
        review_config=ROOT / "config" / "review.toml",
        pdf_target_dpi=None,
        # `invoke` reads this by name like every sibling flag; a stand-in that
        # omits it is not the argv surface it mirrors (U7p; pr/14 broke CI by
        # missing exactly this for a different flag).
        placement_tier=None,
        witness_context="named",
        perlector_protocol_config=ROOT / "config" / "perlector_protocol.toml",
        perlector_audit_config=ROOT / "config" / "perlector_audit.toml",
        # The corpus-register argv surface, which `invoke` reads by name on every
        # stage. A stand-in that omits it is not the surface it claims to mirror.
        corpus_register=None,
        submission_folder=None,
        submission_manifest=None,
        data_gate_policy=None,
    )


def test_every_stage_receives_the_runs_selected_serving_recipes_catalogue(monkeypatch, tmp_path):
    """The roster's other half has to travel with it, to every child.

    `--models-config` selects which chairs exist; `--serving-recipes-config`
    selects the vLLM profile each one is served under. Both are sealed into
    `config_digest` (`common/stage.py::run_config_bindings`), so a stage left on
    the fixture-only default while its siblings were handed the real catalogue
    refuses the whole run for a reason that has nothing to do with the corpus.
    Unit 17 added the flag to `stage_parser` alone, which made the real
    catalogue unreachable through the only program that invokes the stages.
    """

    orchestrator = load_stage("orchestrator")
    observed: list[list[str]] = []
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        lambda command, **_kwargs: (
            observed.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    selected = ROOT / "config" / "serving_recipes_real.toml"
    args = Namespace(
        **{**_orchestrator_namespace_fields(tmp_path), "serving_recipes_config": selected}
    )

    programs = [program for _name, program in orchestrator.SEQUENCE if program is not None]
    for program in programs:
        orchestrator.invoke(program, args)

    assert len(observed) == len(programs) and programs, "no stage was invoked"
    for command in observed:
        assert "--serving-recipes-config" in command, (
            f"{Path(command[1]).name} was invoked without the run's serving catalogue and "
            "would seal the fixture-only default instead"
        )
        assert command[command.index("--serving-recipes-config") + 1] == str(selected)


def test_real_roster_and_catalogue_reach_the_real_orchestrator_route(monkeypatch, tmp_path):
    """The actual subprocess route seals the selected real pair, not the defaults.

    The real roster carries measured manifest pins, while its serving catalogue
    remains deliberately unproven.  Reaching the Designator's preflight refusal
    proves the real roster passed its native-adapter boundary and that the Door
    sealed the caller-selected catalogue before any model could run.  Catalogue row
    completeness and unproven state are checked against these same literal files
    in ``operations/serving/test_manager.py``.
    """

    models = ROOT / "config" / "models-real.toml"
    recipes = ROOT / "config" / "serving_recipes_real.toml"
    run_root = tmp_path / "runs"

    # The tier selects a live-shaped row. Its deliberately unproven preflight
    # state must refuse before a serving process or model request can begin.
    # Subprocesses inherit this offline guard, so a regression past the
    # preflight boundary cannot turn this acceptance test into a model download.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    result = orchestrate(
        run_root,
        "r",
        "happy",
        models_config=models,
        serving_recipes_config=recipes,
        placement_tier="generic-48gb",
    )

    assert result.returncode == 2
    # The Designator's first refusal is its in-process record detector, checked before
    # anything is published; this host installs none of its pinned packages.
    assert "the record detector is not ready" in result.stderr
    assert "pre-materialization sentinel" not in result.stderr
    assert "has no witness_adapter" not in result.stderr
    run_record = json.loads((run_root / "r" / "run.json").read_text(encoding="utf-8"))
    expected = run_config_bindings(
        load_models_toml(models),
        load_fixture(ROOT / "proof"),
        "happy",
        serving_recipes_config_path=recipes,
    )
    assert run_record["config_digest"] == expected["config_digest"]
    assert (
        expected["serving_config_inputs"]["serving_recipes_sha256"]
        == read_sealed_toml(recipes, "serving recipes")[1]
    )


def test_a_partial_real_configuration_is_refused_before_anything_is_written(tmp_path):
    """The real model configuration is one selection; a partial one is refused up front.

    Otherwise the missing files fall back to their fixture defaults and the run
    gets as far as a stage before a configuration check notices the mismatch.
    """

    run_root = tmp_path / "runs"
    result = orchestrate(run_root, "r", "happy", models_config=ROOT / "config" / "models-real.toml")

    assert result.returncode == 2
    assert "supply both or neither" in result.stderr
    assert "--serving-recipes-config" in result.stderr
    assert not run_root.exists()


def test_real_ingress_changes_only_the_doors_argv(monkeypatch, tmp_path):
    """No stage after the Door receives a second path to source material."""

    orchestrator = load_stage("orchestrator")
    observed: list[list[str]] = []

    def record(command, **_kwargs):  # type: ignore[no-untyped-def]
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(orchestrator.subprocess, "run", record)
    base = _orchestrator_namespace_fields(tmp_path)
    fixture_args = orchestrator.resolve_caller_paths(Namespace(**base))
    real_args = orchestrator.resolve_caller_paths(
        Namespace(
            **{
                **base,
                "submission_folder": tmp_path / "approved" / "source",
                "submission_manifest": tmp_path / "approved" / "ledger.json",
            }
        )
    )

    # STAGE_PROGRAMS is every invocable stage program in sequence order, so the
    # Door is first and the slice below means "every stage after the Door".
    for program in orchestrator.STAGE_PROGRAMS.values():
        orchestrator.invoke(program, fixture_args)
    fixture_commands = observed[:]
    observed.clear()
    for program in orchestrator.STAGE_PROGRAMS.values():
        orchestrator.invoke(program, real_args)

    assert observed[1:] == fixture_commands[1:]
    assert "--submission-folder" in observed[0]
    assert "--submission-manifest" in observed[0]
    assert "--data-gate-policy" in observed[0]
    assert not REAL_INGRESS_FLAGS.intersection(fixture_commands[0])

    # Relative equality stays green if both routes leak, so also prohibit flags absolutely.
    for commands in (observed, fixture_commands):
        for command in commands[1:]:
            leaked = REAL_INGRESS_FLAGS.intersection(command)
            assert not leaked, (
                f"{Path(command[1]).name} received {sorted(leaked)}; every stage after the "
                "Door works from the run tree the Door sealed, and a source path on its argv "
                "is a second, unsealed route back to the submitted material"
            )


def test_orchestrator_stage_children_do_not_receive_upload_only_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    orchestrator = load_stage("orchestrator")
    observed_environment: dict[str, str] = {}
    monkeypatch.setenv("RUNPOD_S3_ACCESS_KEY", "upload-access-secret")
    monkeypatch.setenv("RUNPOD_S3_SECRET_KEY", "upload-secret-secret")
    monkeypatch.setenv("VERBATUS_STAGE_TEST_SENTINEL", "preserved")

    def record(command, **kwargs):  # type: ignore[no-untyped-def]
        observed_environment.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(orchestrator.subprocess, "run", record)
    args = orchestrator.resolve_caller_paths(Namespace(**_orchestrator_namespace_fields(tmp_path)))

    orchestrator.invoke(orchestrator.STAGE_PROGRAMS["door"], args)

    assert "RUNPOD_S3_ACCESS_KEY" not in observed_environment
    assert "RUNPOD_S3_SECRET_KEY" not in observed_environment
    assert observed_environment["VERBATUS_STAGE_TEST_SENTINEL"] == "preserved"


def test_invoke_refuses_a_caller_relative_path_instead_of_resolving_it_late(monkeypatch, tmp_path):
    """Direct invocation must not reinterpret caller paths under the child's cwd."""

    orchestrator = load_stage("orchestrator")
    invoked: list[list[str]] = []
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        lambda command, **_kwargs: (
            invoked.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    base = _orchestrator_namespace_fields(tmp_path)

    for attribute, flag, value in (
        ("run_root", "--run-root", Path("runs")),
        ("submission_folder", "--submission-folder", Path("approved/source")),
        ("submission_manifest", "--submission-manifest", Path("approved/ledger.json")),
        ("data_gate_policy", "--data-gate-policy", Path("policy.json")),
    ):
        overrides = {attribute: value}
        if attribute in {"submission_manifest", "data_gate_policy"}:
            overrides["submission_folder"] = tmp_path / "approved" / "source"
        args = Namespace(**{**base, **overrides})
        with pytest.raises(ContractError) as refusal:
            orchestrator.invoke(orchestrator.STAGE_PROGRAMS["door"], args)
        assert flag in str(refusal.value)
    assert not invoked, "a stage was launched with a caller-relative path on its argv"

    orchestrator.invoke(
        orchestrator.STAGE_PROGRAMS["door"],
        orchestrator.resolve_caller_paths(Namespace(**{**base, "run_root": Path("runs")})),
    )
    assert invoked


def test_orchestrator_default_data_gate_policy_is_the_gates_own(tmp_path):
    """The common-only import boundary requires duplicate constants to reconcile."""

    orchestrator = load_stage("orchestrator")
    assert orchestrator.DEFAULT_DATA_GATE_POLICY_PATH == gate.DEFAULT_POLICY_PATH
    resolved = orchestrator.resolve_caller_paths(
        Namespace(
            **{
                **_orchestrator_namespace_fields(tmp_path),
                "submission_folder": tmp_path / "approved" / "source",
                "data_gate_policy": None,
            }
        )
    )
    assert resolved.data_gate_policy == gate.DEFAULT_POLICY_PATH
    assert resolved.data_gate_policy.is_file()


def test_orchestrator_upload_credentials_are_the_transfers_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both stripping helpers drop the transfer's own names and keep the rest.

    Both this orchestrator and the operator surface strip the upload-only
    credentials from a stage's environment, and the import boundary keeps this
    one a duplicate. Without this reconciliation a third credential added to the
    transfer would go on reaching stages here while the whole suite stayed
    green -- a live secret in the environment of the process that decodes
    caller-supplied material.

    Comparing the sets is not enough on its own: constants can agree
    perfectly while a helper has stopped consulting its own. So each helper is
    run against an environment holding every name, and what it returns is the
    evidence.
    """

    orchestrator = load_stage("orchestrator")
    assert orchestrator._TRANSFER_CREDENTIAL_ENV == volume_s3.TRANSFER_CREDENTIAL_ENV
    # The names are the transfer's own defaults, not a set that merely happens to
    # match them today.
    spec = volume_s3.VolumeSpec(datacenter_id="EU-CZ-1", volume_id="volume")
    assert {spec.access_key_env, spec.secret_key_env} == set(volume_s3.TRANSFER_CREDENTIAL_ENV)

    for name in volume_s3.TRANSFER_CREDENTIAL_ENV:
        monkeypatch.setenv(name, f"upload-secret-for-{name}")
    monkeypatch.setenv("VERBATUS_STAGE_TEST_SENTINEL", "preserved")

    for label, built in (
        ("orchestrator", orchestrator.stage_environment()),
        ("operator surface", surface.credential_free_environment()),
    ):
        leaked = volume_s3.TRANSFER_CREDENTIAL_ENV.intersection(built)
        assert not leaked, f"{label} passed {sorted(leaked)} to a stage"


def test_orchestrator_and_surface_strip_every_provider_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stage subprocess decodes attacker-supplied material, so both
    `stage_environment` builders must refuse every provider credential except
    the transfer's own two S3 keys -- RUNPOD_API_KEY (pod creation, i.e.
    money), HF_TOKEN, AWS_*, and anything else shaped like a secret -- the
    same broad shape `credential_free_environment` already holds the
    operator's confined children to.
    """

    orchestrator = load_stage("orchestrator")
    representative_names = (
        "RUNPOD_API_KEY",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "SOME_VENDOR_BEARER",
        *volume_s3.TRANSFER_CREDENTIAL_ENV,
    )
    for name in representative_names:
        assert looks_like_credential_env(name), f"{name} is not credential-shaped; fix the fixture"
        monkeypatch.setenv(name, f"secret-for-{name}")
    monkeypatch.setenv("VERBATUS_STAGE_TEST_SENTINEL", "preserved")

    reference = credential_free_environment()
    for label, built in (
        ("orchestrator", orchestrator.stage_environment()),
        ("operator surface", surface.credential_free_environment()),
    ):
        leaked = set(representative_names) & set(built)
        assert not leaked, f"{label} passed {sorted(leaked)} to a stage"
        assert built.get("VERBATUS_STAGE_TEST_SENTINEL") == "preserved", (
            f"{label} dropped an ordinary, non-credential variable"
        )
        # Not just the hand-picked names above: the whole process environment,
        # compared key-for-key against the real `credential_free_environment`,
        # so the duplicated predicate cannot drift narrower *or* wider than the
        # original in silence.
        assert set(built) == set(reference), (
            f"{label} disagrees with credential_free_environment on {set(built) ^ set(reference)}"
        )
        # The stripper must remove those names and nothing else: an
        # implementation that returned an empty environment, or one that dropped
        # everything it did not recognise, would satisfy the assertion above
        # while breaking every stage that reads its own settings.
        assert built["VERBATUS_STAGE_TEST_SENTINEL"] == "preserved", label


def test_resuming_a_real_run_without_its_ingress_flags_refuses(tmp_path):
    """A fixture route may not take over a run tree sealed as real ingress."""

    approved, source, manifest, policy = _real_submission(tmp_path)
    first = orchestrate(
        approved / "runs",
        "seam",
        "happy",
        submission_folder=source,
        submission_manifest=manifest,
        data_gate_policy=policy,
    )
    assert "a fixture row answers only a synthetic run" in first.stderr
    sealed = RunTree(approved / "runs", "seam").read_run()

    resumed = orchestrate(approved / "runs", "seam", "happy")

    assert resumed.returncode != 0
    assert "already exists and is bound to different" in resumed.stderr
    assert "ingress" in resumed.stderr
    assert RunTree(approved / "runs", "seam").read_run() == sealed


def test_relative_run_root_from_outside_the_repository_is_one_tree(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        FIXTURE,
        "--scenario",
        "page-unbroken",
        "--run-id",
        "outside",
        "--run-root",
        "runs",
        "--fixture-root",
        str(ROOT / "proof"),
    ]

    result = subprocess.run(command, cwd=outside, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert (outside / "runs" / "outside" / "run.json").is_file()
    assert not (ROOT / "runs" / "outside").exists()


def test_relative_submission_folder_from_outside_the_repository_finds_the_real_files(
    tmp_path: Path,
) -> None:
    """Real-ingress paths bind before child processes change cwd to the repository."""
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "approved-storage" / "submitted-pages"
    source.mkdir(parents=True)
    for name in ("page-1.png", "page-2.png"):
        shutil.copyfile(ROOT / "proof" / "fixtures" / FIXTURE / name, source / name)
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(outside / "approved-storage")]
    policy_path = outside / "data-gate-policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    manifest = outside / "approved-storage" / "submission-ledger.json"
    submit.submit(source, manifest, policy_path=policy_path)

    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        FIXTURE,
        "--scenario",
        "happy",
        "--run-id",
        "relative-real-ingress",
        "--run-root",
        "approved-storage/runs",
        "--submission-folder",
        "approved-storage/submitted-pages",
        "--submission-manifest",
        "approved-storage/submission-ledger.json",
        "--data-gate-policy",
        "data-gate-policy.json",
    ]

    result = subprocess.run(command, cwd=outside, capture_output=True, text=True)

    # A real route must not require its deliberately unused fixture-root default.
    assert "could not be resolved" not in result.stderr
    assert "outside every approved storage root" not in result.stderr
    run_tree_root = outside / "approved-storage" / "runs"
    assert RunTree(run_tree_root, "relative-real-ingress").read_run()["ingress"] == {"mode": "real"}
    assert not (ROOT / "runs" / "relative-real-ingress").exists()


def test_orchestrator_preserves_a_submitted_folder_symlink_for_the_doors_gate(tmp_path: Path):
    """Making caller paths absolute must not silently dereference real ingress."""

    approved, source, manifest, policy = _real_submission(tmp_path)
    submitted_link = approved / "submitted-link"
    submitted_link.symlink_to(source, target_is_directory=True)

    result = orchestrate(
        approved / "runs",
        "symlink-refusal",
        "happy",
        submission_folder=submitted_link,
        submission_manifest=manifest,
        data_gate_policy=policy,
    )

    assert result.returncode != 0
    assert "submitted folder is a symlink" in result.stderr
    assert not (approved / "runs" / "symlink-refusal" / "run.json").exists()


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


def _run_through_designator(root: Path, run_id: str = "r", scenario: str = "happy") -> None:
    """Run Door, Exemplar, and Designator, refusing a partial setup loudly."""
    for program in programs_through("designator"):
        result = invoke_stage(root, run_id, scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"


def run_through_recensor(
    run_root: Path, run_id: str, scenario: str = "happy", *, allow_held: bool = False
) -> None:
    for program in programs_through("recensor"):
        result = invoke_stage(run_root, run_id, scenario, program)
        expected = {0, 3} if allow_held else {0}
        assert result.returncode in expected, f"{program}: {result.stderr}"


def _sqlite_logical_digest(data: bytes) -> str:
    """Bind a SQLite member to its schema and rows, not its library header."""
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise ValueError(
            "pragma_table_list is unavailable: this interpreter reports "
            f"sqlite3.sqlite_version={sqlite3.sqlite_version}; SQLite 3.37.0 or newer is required"
        )
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(data)
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise ValueError(f"SQLite integrity check failed: {integrity!r}")

        schema_sql = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                "ORDER BY type, name, tbl_name, sql"
            )
        ]
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM pragma_table_list "
                "WHERE schema = 'main' AND type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables = {}
        for table_name in table_names:
            escaped_table = table_name.replace('"', '""')
            quoted_table = f'"{escaped_table}"'
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})")]
            if table_name == "act_search":
                excluded = {"derived_search_text", "derived_text_sha256"}
                if not excluded <= set(columns):
                    raise ValueError(
                        "act_search no longer carries the version-local derived columns "
                        "this pin excludes; update the exclusion deliberately"
                    )
                columns = [column for column in columns if column not in excluded]
            quoted_columns = []
            for column in columns:
                escaped_column = column.replace('"', '""')
                quoted_columns.append(f'"{escaped_column}"')
            query = f"SELECT {', '.join(quoted_columns)} FROM {quoted_table}"
            parameters = ()
            if table_name == "export_metadata":
                if "key" not in columns:
                    raise ValueError(
                        "export_metadata has no 'key' column, so the unidata_version "
                        "stamp cannot be excluded from this pin"
                    )
                query += ' WHERE "key" != ?'
                parameters = ("unidata_version",)
            query += " ORDER BY " + ", ".join(str(index) for index in range(1, len(columns) + 1))
            tables[table_name] = {
                "columns": columns,
                "rows": connection.execute(query, parameters).fetchall(),
            }

        return digest_of(
            {
                "schema_sql": schema_sql,
                # `pragma_table_list ... type = 'table'` leaves out the FTS5
                # virtual table and its shadow tables: they are SQLite's own
                # index over `act_search.derived_search_text`, and that column is
                # itself excluded above as Unicode-version-local. Binding the
                # index while excluding what it indexes would put the same
                # version-local bytes back into the pin under another name. Their
                # *schema* stays bound in `schema_sql`, and the fold they encode
                # is checked by the verifier's version-aware recomputation
                # (`armarium_export._verify_search_fold_claim`), not here.
                "tables": tables,
                "application_id": connection.execute("PRAGMA application_id").fetchone()[0],
                "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            }
        )
    except sqlite3.DatabaseError as error:
        raise ValueError("the Armarium SQLite member is unreadable") from error
    finally:
        connection.close()


def _armarium_bundle_semantics(data: bytes) -> tuple[str, dict[str, str]] | None:
    """Return the semantic bundle digest and its derived-hash replacements."""
    if not data.startswith(b"PK\x03\x04"):
        return None
    try:
        with ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or not {"EXPORT_MANIFEST.json", "acts.sqlite"} <= set(
                names
            ):
                return None
            manifest_data = archive.read("EXPORT_MANIFEST.json")
            manifest = json.loads(manifest_data)
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema") != "armarium-export-manifest.v13"
                or canonical_bytes(manifest) != manifest_data
                or manifest.get("self_hash") != self_hash(manifest)
            ):
                return None

            member_names = set(names) - {"EXPORT_MANIFEST.json"}
            listed = manifest.get("members")
            if not isinstance(listed, list):
                return None
            member_rows = {}
            for row in listed:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"path", "sha256", "bytes"}
                    or not isinstance(row.get("path"), str)
                    or row["path"] in member_rows
                ):
                    return None
                member_rows[row["path"]] = row
            if set(member_rows) != member_names:
                return None
            member_data = {name: archive.read(name) for name in member_names}
            if any(
                row.get("sha256") != digest_bytes(member_data[name])
                or row.get("bytes") != len(member_data[name])
                for name, row in member_rows.items()
            ):
                return None

            database_data = member_data["acts.sqlite"]
            logical_database_digest = _sqlite_logical_digest(database_data)
            semantic_manifest = deepcopy(manifest)
            database_rows = [
                row for row in semantic_manifest["members"] if row["path"] == "acts.sqlite"
            ]
            database_rows[0]["sha256"] = logical_database_digest
            semantic_manifest["self_hash"] = self_hash(semantic_manifest)
            semantic_manifest_data = canonical_bytes(semantic_manifest)

            member_inventory = {}
            for name in names:
                if name == "acts.sqlite":
                    member_inventory[name] = logical_database_digest
                elif name == "EXPORT_MANIFEST.json":
                    # This member's SQLite digest and its own self-hash are
                    # consequences of the container bytes. Every other field in
                    # it, and every other member, remains byte-bound.
                    member_inventory[name] = digest_bytes(semantic_manifest_data)
                else:
                    member_inventory[name] = digest_bytes(member_data[name])
    except (BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    return digest_of(member_inventory), {
        digest_bytes(data): digest_of(member_inventory),
        manifest["self_hash"]: semantic_manifest["self_hash"],
    }


def _replace_semantic_digests(value, replacements: dict[str, str]):
    """Replace only exact digest tokens and content-addressed path components."""
    if isinstance(value, str):
        if value in replacements:
            return replacements[value]
        for raw_digest, semantic_digest in replacements.items():
            suffix = f"/{raw_digest}"
            if value.endswith(suffix):
                return f"{value[: -len(raw_digest)]}{semantic_digest}"
        return value
    if isinstance(value, dict):
        return {key: _replace_semantic_digests(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_semantic_digests(item, replacements) for item in value]
    return value


def _semantic_export_artifact(data: bytes, replacements: dict[str, str]) -> bytes | None:
    """Reduce the Armarium export envelope's bundle bindings semantically."""
    try:
        record = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    bundle = payload.get("bundle", {}) if isinstance(payload, dict) else {}
    if (
        record.get("stage") != ARMARIUM
        or record.get("kind") != "export"
        or bundle.get("format") != "zip"
        or bundle.get("sha256") not in replacements
        or canonical_bytes(record) != data
        or record.get("self_hash") != self_hash(record)
    ):
        return None
    semantic = _replace_semantic_digests(record, replacements)
    semantic["self_hash"] = self_hash(semantic)
    return canonical_bytes(semantic)


def _semantic_decode_environment(data: bytes) -> bytes | None:
    """Normalize host probes while retaining the stage's semantic decode role."""
    try:
        record = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("kind") != "decode-environment":
        return None
    try:
        environment = _validate_decode_environment(record.get("payload"), "acceptance pin")
    except SchemaRefusal:
        return None
    if canonical_bytes(record) != data or record.get("self_hash") != self_hash(record):
        return None
    semantic = deepcopy(record)
    semantic["payload"] = deepcopy(environment)
    for decoder in semantic["payload"]["decoders"]:
        decoder["version"] = "platform-normalized"
    semantic["payload"]["platform"] = "platform-normalized"
    semantic["payload"]["machine"] = "platform-normalized"
    semantic["self_hash"] = self_hash(semantic)
    return canonical_bytes(semantic)


def _semantic_armarium_manifest(data: bytes, replacements: dict[str, str]) -> bytes | None:
    """Reduce only derived bundle/export digests in the stage inventory."""
    try:
        manifest = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(manifest, dict)
        or manifest.get("stage") != ARMARIUM
        or manifest.get("schema") != "skeleton.v1"
        or canonical_bytes(manifest) != data
        or not any(blob in replacements for blob in manifest.get("blobs", []))
    ):
        return None
    return canonical_bytes(_replace_semantic_digests(manifest, replacements))


def _semantic_envelope(data: bytes, replacements: dict[str, str]) -> bytes | None:
    """Reduce a valid ordinary envelope when it names semantic blob content."""
    try:
        record = json.loads(data)
        validate_envelope(record)
    except (SchemaRefusal, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        record["kind"] in {"decode-environment", "stage-seal"}
        or canonical_bytes(record) != data
        or record["self_hash"] != self_hash(record)
    ):
        return None
    semantic = _replace_semantic_digests(record, replacements)
    if semantic == record:
        return None
    semantic["self_hash"] = self_hash(semantic)
    return canonical_bytes(semantic)


def _semantic_stage_seal(data: bytes, replacements: dict[str, str]) -> bytes | None:
    """Reduce a witnessed stage inventory only when its complete shape is sound."""
    try:
        record = json.loads(data)
        validate_envelope(record)
    except (SchemaRefusal, UnicodeDecodeError, json.JSONDecodeError):
        return None
    payload = record.get("payload")
    if (
        record.get("kind") != "stage-seal"
        or canonical_bytes(record) != data
        or record.get("self_hash") != self_hash(record)
        or not isinstance(payload, dict)
        or set(payload)
        != {
            "stage",
            "attempt_ordinal",
            "attempt_id",
            "config_digest",
            "register_digest",
            "artifact_inventory",
            "blob_inventory",
            "census",
            "decode_environment_artifact_id",
            "decode_environment_sha256",
        }
        or payload["stage"] not in STAGES
        or record.get("stage") != payload["stage"]
        or record.get("subject_id") != payload["stage"]
        or record.get("attempt_id") != payload["attempt_id"]
        or not isinstance(payload["attempt_ordinal"], int)
        or payload["attempt_ordinal"] < 1
        or not isinstance(payload["census"], list)
        or any(
            not isinstance(row, dict)
            or set(row) != {"kind", "outcome", "count"}
            or not isinstance(row["kind"], str)
            or not isinstance(row["outcome"], str)
            or not isinstance(row["count"], int)
            or row["count"] < 1
            for row in payload["census"]
        )
        or payload["census"]
        != sorted(payload["census"], key=lambda row: (row["kind"], row["outcome"]))
        or len({(row["kind"], row["outcome"]) for row in payload["census"]})
        != len(payload["census"])
        or any(
            not isinstance(payload[field], str)
            or len(payload[field]) != 64
            or any(character not in "0123456789abcdef" for character in payload[field])
            for field in (
                "config_digest",
                "register_digest",
                "artifact_inventory",
                "blob_inventory",
                "decode_environment_sha256",
            )
        )
        or not isinstance(payload["attempt_id"], str)
        or not isinstance(payload["decode_environment_artifact_id"], str)
        or payload["artifact_inventory"] not in replacements
        or payload["blob_inventory"] not in replacements
        or payload["decode_environment_sha256"] not in replacements
    ):
        return None
    semantic = _replace_semantic_digests(record, replacements)
    semantic["payload"]["artifact_inventory"] = replacements[payload["artifact_inventory"]]
    semantic["payload"]["blob_inventory"] = replacements[payload["blob_inventory"]]
    semantic["self_hash"] = self_hash(semantic)
    return canonical_bytes(semantic)


def _semantic_stage_seal_inventory_replacements(
    root: Path, files: list[tuple[Path, bytes]], replacements: dict[str, str]
) -> None:
    """Map each valid seal's raw aggregate values to its semantic inventories.

    `root` holds one directory per run, and a seal witnesses only its own run's
    stage, so records and blobs are grouped by (run directory, stage).
    """
    records_by_stage: dict[tuple[str, str], list[tuple[Path, bytes, dict]]] = {}
    seals: list[tuple[Path, bytes, dict]] = []
    for path, data in files:
        try:
            record = json.loads(data)
            validate_envelope(record)
        except (SchemaRefusal, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if canonical_bytes(record) != data or record.get("self_hash") != self_hash(record):
            continue
        stage = record.get("stage")
        if stage not in STAGES:
            continue
        row = (path, data, record)
        records_by_stage.setdefault((path.relative_to(root).parts[0], stage), []).append(row)
        if record.get("kind") == "stage-seal":
            seals.append(row)

    def inventory_entry(path: Path, data: bytes, record: dict) -> dict[str, str]:
        return {
            "artifact_id": record["artifact_id"],
            "kind": record["kind"],
            "subject_id": record["subject_id"],
            "outcome": record["outcome"],
            "relative_path": str(path.relative_to(path.parents[3])),
            "sha256": digest_bytes(data),
        }

    # A seal witnesses the stage's whole artifact and blob inventory when it was
    # written. Rebuild exactly that from the tree and let the seal's own digests
    # confirm it; a tree that has grown since cannot be reduced and is refused.
    for seal_path, _, seal in seals:
        payload = seal.get("payload")
        stage = payload.get("stage") if isinstance(payload, dict) else None
        if stage not in STAGES:
            continue
        run = seal_path.relative_to(root).parts[0]
        sealed = sorted(
            (
                row
                for row in records_by_stage.get((run, stage), [])
                if row[2]["kind"] not in {"stage-seal", "decode-environment"}
            ),
            key=lambda row: row[2]["artifact_id"],
        )
        artifacts = [inventory_entry(*row) for row in sealed]
        blob_directory = root / run / WRITING_DIRECTORIES[stage] / "blobs" / "sha256"
        blobs = sorted(
            (
                {"name": path.name, "sha256_of_content": digest_bytes(data)}
                for path, data in files
                if path.parent == blob_directory
            ),
            key=lambda row: row["name"],
        )
        if payload.get("artifact_inventory") != digest_of(artifacts) or payload.get(
            "blob_inventory"
        ) != digest_of(blobs):
            raise AssertionError(
                f"{stage} stage seal {seal_path.name}: the stage's artifacts and blobs no "
                "longer match the inventory it sealed, so the semantic snapshot cannot "
                "reduce it"
            )
        semantic_artifacts = _replace_semantic_digests(artifacts, replacements)
        for entry, (_, data, _) in zip(semantic_artifacts, sealed, strict=True):
            entry["sha256"] = replacements.get(digest_bytes(data), digest_bytes(data))
        semantic_blobs = _replace_semantic_digests(blobs, replacements)
        replacements[payload["artifact_inventory"]] = digest_of(semantic_artifacts)
        replacements[payload["blob_inventory"]] = digest_of(semantic_blobs)


def _semantic_manifest(data: bytes, replacements: dict[str, str]) -> bytes | None:
    """Reduce a derived stage manifest only when it has the exact store shape."""
    try:
        manifest = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema", "run_id", "stage", "artifacts", "blobs"}
        or manifest.get("schema") != "skeleton.v1"
        or manifest.get("stage") not in WRITING_DIRECTORIES
        or not isinstance(manifest["run_id"], str)
        or not isinstance(manifest["artifacts"], list)
        or not isinstance(manifest["blobs"], list)
        or canonical_bytes(manifest) != data
    ):
        return None
    semantic = _replace_semantic_digests(manifest, replacements)
    return None if semantic == manifest else canonical_bytes(semantic)


def semantic_snapshot(root: Path) -> dict[str, str]:
    """Run-tree inventory with platform-written containers reduced to data.

    PNG blobs bind decoded pixels. The Armarium bundle binds its named member
    inventory, with ``acts.sqlite`` reduced to a deterministic schema-and-row
    dump; the package manifest and run-tree manifest bind the corresponding
    semantic digest rather than derivative container hashes. Everything else
    remains byte-bound. The ordinary ``snapshot`` stays byte-exact for all resume
    and no-write assertions.
    """
    if (root / "run.json").is_file():
        raise ValueError(
            "semantic_snapshot requires the runs root, not an individual run directory"
        )
    files = [(path, path.read_bytes()) for path in sorted(root.rglob("*")) if path.is_file()]
    bundle_paths = {}
    replacements = {}
    for path, data in files:
        semantics = _armarium_bundle_semantics(data)
        if semantics is None:
            continue
        semantic_digest, bundle_replacements = semantics
        bundle_paths[path] = semantic_digest
        replacements.update(bundle_replacements)

    # A blob's content-addressed filename is an encoding of its container bytes.
    # Establish its pixel value first, so every record that names that component
    # can be reduced before inventories digest the record bytes.
    for _, data in files:
        if not data.startswith(PNG_SIGNATURE):
            continue
        try:
            width, height, rows = decode_grayscale_png(data)
            pixel_digest = digest_bytes(b"".join(rows))
        except ValueError:
            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                grayscale = image.convert("L")
                width, height = grayscale.size
                pixel_digest = digest_bytes(grayscale.tobytes())
        replacements[digest_bytes(data)] = digest_of(
            {"width": width, "height": height, "pixel_sha256": pixel_digest}
        )

    semantic_files = {}
    for path, data in files:
        semantic = _semantic_decode_environment(data)
        if semantic is not None:
            semantic_files[path] = semantic
            replacements[digest_bytes(data)] = digest_bytes(semantic)
    for path, data in files:
        semantic = _semantic_export_artifact(data, replacements)
        if semantic is not None:
            semantic_files[path] = semantic
            replacements[digest_bytes(data)] = digest_bytes(semantic)

    # Ordinary envelopes may bind a content-addressed blob directly. Work to a
    # fixed point because later envelopes can name an earlier envelope's digest.
    for _ in range(len(files)):
        changed = False
        for path, data in files:
            semantic = _semantic_envelope(data, replacements)
            if semantic is None:
                continue
            semantic_files[path] = semantic
            raw_digest, semantic_digest = digest_bytes(data), digest_bytes(semantic)
            if replacements.get(raw_digest) != semantic_digest:
                replacements[raw_digest] = semantic_digest
                changed = True
        if not changed:
            break

    _semantic_stage_seal_inventory_replacements(root, files, replacements)
    for path, data in files:
        semantic = _semantic_stage_seal(data, replacements)
        if semantic is not None:
            semantic_files[path] = semantic
            replacements[digest_bytes(data)] = digest_bytes(semantic)

    for path, data in files:
        semantic = _semantic_armarium_manifest(data, replacements)
        if semantic is None:
            semantic = _semantic_manifest(data, replacements)
        if semantic is not None:
            semantic_files[path] = semantic

    inventory = {}
    for path, data in files:
        relative = str(path.relative_to(root))
        relative = _replace_semantic_digests(relative, replacements)
        if path in bundle_paths:
            raw_digest = digest_bytes(data)
            semantic_digest = bundle_paths[path]
            if relative.endswith(raw_digest):
                relative = f"{relative[: -len(raw_digest)]}{semantic_digest}"
            inventory[relative] = semantic_digest
        elif path in semantic_files:
            inventory[relative] = digest_bytes(semantic_files[path])
        elif data.startswith(PNG_SIGNATURE):
            try:
                width, height, rows = decode_grayscale_png(data)
                pixel_digest = digest_bytes(b"".join(rows))
            except ValueError:
                # The Perlector's page-render blobs are written by Pillow, whose
                # adaptive PNG filters the project's own minimal decoder refuses
                # by design. The pin still binds pixels, not compressor bytes —
                # only the decoder differs.
                from PIL import Image

                with Image.open(BytesIO(data)) as image:
                    grayscale = image.convert("L")
                    width, height = grayscale.size
                    pixel_digest = digest_bytes(grayscale.tobytes())
            inventory[relative] = digest_of(
                {
                    "width": width,
                    "height": height,
                    "pixel_sha256": pixel_digest,
                }
            )
        else:
            inventory[relative] = digest_bytes(data)
    return inventory


def semantic_snapshot_digest(root: Path) -> str:
    """The canonical content pin for the full relative run-tree inventory."""
    return digest_of(semantic_snapshot(root))


def _acceptance_sqlite(
    path: Path,
    text: str,
    *,
    unidata_version: str = "15.1.0",
    derived_search_text: str = "original derived text",
    derived_from_canonical_sha256: str | None = None,
) -> bytes:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version=1")
        connection.executescript(
            """
            CREATE TABLE export_metadata (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE acts (act_id TEXT PRIMARY KEY, text TEXT NOT NULL);
            CREATE TABLE act_search (
                rowid INTEGER PRIMARY KEY,
                act_id TEXT UNIQUE NOT NULL REFERENCES acts(act_id),
                derived_search_text TEXT NOT NULL,
                derived_text_sha256 TEXT NOT NULL,
                derived_from_canonical_sha256 TEXT NOT NULL,
                normalizer_revision TEXT NOT NULL,
                derived_kind TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO export_metadata VALUES (?, ?)",
            (
                ("normalizer_revision", "armarium-textnorm-v1"),
                ("unidata_version", unidata_version),
            ),
        )
        connection.execute("INSERT INTO acts VALUES ('a1', ?)", (text,))
        if derived_from_canonical_sha256 is None:
            derived_from_canonical_sha256 = digest_bytes(text.encode("utf-8"))
        connection.execute(
            "INSERT INTO act_search VALUES (1, 'a1', ?, ?, ?, ?, ?)",
            (
                derived_search_text,
                digest_bytes(derived_search_text.encode("utf-8")),
                derived_from_canonical_sha256,
                "armarium-textnorm-v1",
                "search-fold",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return path.read_bytes()


def _write_acceptance_bundle_tree(root: Path, database_data: bytes, damage=None) -> None:
    """Write a whole run tree around one bundle, optionally damaged from the inside.

    ``damage`` mutates the package manifest *after* it is written and before the tree
    is addressed, so everything outside the archive -- the blob's content-addressed
    name, the export artifact's digests and self-hash, the stage manifest -- is
    rebuilt consistently around the damaged bytes. That is the tree a forger leaves,
    and it is the only one in which the reducer's own integrity guards are the thing
    under test rather than a stale filename.
    """
    members = {"acts.sqlite": database_data, "acts.jsonl": b'{"act_id":"a1"}\n'}
    package_manifest = {
        "schema": "armarium-export-manifest.v13",
        "members": [
            {"path": name, "sha256": digest_bytes(content), "bytes": len(content)}
            for name, content in sorted(members.items())
        ],
    }
    package_manifest["self_hash"] = self_hash(package_manifest)
    if damage is not None:
        damage(package_manifest)
    members["EXPORT_MANIFEST.json"] = canonical_bytes(package_manifest)
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED) as archive:
        for name, content in sorted(members.items()):
            archive.writestr(name, content)
    bundle_data = buffer.getvalue()
    bundle_digest = digest_bytes(bundle_data)
    bundle_relative = f"7_armarium/blobs/sha256/{bundle_digest}"

    bundle_path = root / bundle_relative
    bundle_path.parent.mkdir(parents=True)
    bundle_path.write_bytes(bundle_data)
    export = {
        "schema": "skeleton.v1",
        "stage": ARMARIUM,
        "kind": "export",
        "payload": {
            "bundle": {
                "format": "zip",
                "sha256": bundle_digest,
                "manifest_self_hash": package_manifest["self_hash"],
                "reference": {"relative_path": bundle_relative, "sha256": bundle_digest},
            },
            "unrelated": "remains-byte-bound",
        },
        "inputs": [{"relative_path": bundle_relative, "sha256": bundle_digest}],
    }
    export["self_hash"] = self_hash(export)
    export_data = canonical_bytes(export)
    export_path = root / "7_armarium/artifacts/export/example.json"
    export_path.parent.mkdir(parents=True)
    export_path.write_bytes(export_data)
    stage_manifest = {
        "schema": "skeleton.v1",
        "stage": ARMARIUM,
        "run_id": "r",
        "artifacts": [
            {
                "kind": "export",
                "relative_path": "7_armarium/artifacts/export/example.json",
                "sha256": digest_bytes(export_data),
            }
        ],
        "blobs": [bundle_digest],
    }
    (root / "7_armarium/manifest.json").write_bytes(canonical_bytes(stage_manifest))


def test_semantic_snapshot_digest_binds_sqlite_rows_not_library_header(tmp_path):
    """Version-local database fields cannot rename a run; a literal row can."""
    database = _acceptance_sqlite(tmp_path / "database.sqlite", "original row")
    version_local = _acceptance_sqlite(
        tmp_path / "version-local.sqlite",
        "original row",
        unidata_version="16.0.0",
        derived_search_text="different version-local derived text",
    )
    doctored = version_local[:96] + b"\xff\xff\xff\xff" + version_local[100:]
    original_root = tmp_path / "original"
    doctored_root = tmp_path / "doctored"
    changed_root = tmp_path / "changed"
    _write_acceptance_bundle_tree(original_root, database)
    _write_acceptance_bundle_tree(doctored_root, doctored)
    changed = _acceptance_sqlite(
        tmp_path / "changed.sqlite",
        "changed row",
        derived_from_canonical_sha256=digest_bytes(b"original row"),
    )
    _write_acceptance_bundle_tree(changed_root, changed)

    assert snapshot(original_root) != snapshot(doctored_root)
    assert semantic_snapshot_digest(original_root) == semantic_snapshot_digest(doctored_root)
    assert semantic_snapshot_digest(original_root) != semantic_snapshot_digest(changed_root)


def test_semantic_snapshot_refuses_damaged_persisted_integrity_fields(tmp_path):
    """Integrity damage stays byte-bound instead of being normalized out of the pin.

    The two bundle-internal cases are the ones the reduction would otherwise *erase*:
    it recomputes the package manifest's `self_hash` and overwrites the `acts.sqlite`
    member row's `sha256` with the logical digest, so without the reducer's own
    integrity guards a manifest lying about either would reduce to exactly the same
    pin as an honest one. Both trees are written whole, so the blob's content address,
    the export artifact and the stage manifest all agree with the damaged bytes and
    nothing incidental distinguishes them.
    """
    database = _acceptance_sqlite(tmp_path / "database.sqlite", "original row")
    original_root = tmp_path / "original"
    _write_acceptance_bundle_tree(original_root, database)
    original_semantic = semantic_snapshot_digest(original_root)

    manifest_hash_root = tmp_path / "manifest-self-hash"
    member_digest_root = tmp_path / "member-digest"
    export_hash_root = tmp_path / "export-self-hash"

    def damage_manifest_hash(manifest: dict) -> None:
        manifest["self_hash"] = "b" * 64

    def damage_database_member_digest(manifest: dict) -> None:
        row = next(item for item in manifest["members"] if item["path"] == "acts.sqlite")
        row["sha256"] = "d" * 64
        manifest["self_hash"] = self_hash(
            {key: value for key, value in manifest.items() if key != "self_hash"}
        )

    _write_acceptance_bundle_tree(manifest_hash_root, database, damage=damage_manifest_hash)
    _write_acceptance_bundle_tree(
        member_digest_root,
        database,
        damage=damage_database_member_digest,
    )
    shutil.copytree(original_root, export_hash_root)
    export_path = export_hash_root / "7_armarium/artifacts/export/example.json"
    export = json.loads(export_path.read_bytes())
    export["self_hash"] = "c" * 64
    export_path.write_bytes(canonical_bytes(export))

    for root in (manifest_hash_root, member_digest_root, export_hash_root):
        assert snapshot(root) != snapshot(original_root)
        assert semantic_snapshot_digest(root) != original_semantic


def export_of(tree: RunTree) -> dict:
    return tree.read_artifact(ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None))[
        "payload"
    ]


@pytest.fixture(scope="module")
def happy_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("happy")
    result = orchestrate(root, "r", "happy")
    # Partial by design: an act may cross the page break, and code never joins it.
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r")


# --- Semantic snapshot: stage seals reduce whatever their inventory size --------

_AFTER_ATTESTATORES = ("4_perlector", "4b_coniector", "5_recensor", "6_archetypus", "7_armarium")


def _through_attestatores(happy_run, destination: Path, change=None) -> Path:
    """The happy tree up to the Attestatores, optionally changed and honestly resealed.

    Later stages name the Attestatores seal's bytes, so they are dropped rather
    than rewitnessed; the Attestatores seal is the one under test.
    """
    source, _ = happy_run
    shutil.copytree(source, destination)
    for name in _AFTER_ATTESTATORES:
        shutil.rmtree(destination / "r" / name)
    if change is not None:
        tree = RunTree(destination, "r")
        change(tree)
        rewitness_stage_boundary(tree, ATTESTATORES)
    return destination


def _only_artifact(tree: RunTree, kind: str) -> Path:
    (path,) = (tree.root / "3_attestatores" / "artifacts" / kind).iterdir()
    return path


def _rewrite_record(path: Path, edit) -> None:
    record = json.loads(path.read_bytes())
    edit(record)
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))


def _other_platform(tree: RunTree) -> None:
    def edit(record):
        record["payload"]["platform"] = "Darwin"
        record["payload"]["machine"] = "arm64"

    _rewrite_record(_only_artifact(tree, "decode-environment"), edit)


def _changed_text(tree: RunTree) -> None:
    def edit(record):
        record["payload"]["payload"] = record["payload"]["payload"].replace("alpha", "alpha!")

    directory = tree.root / "3_attestatores" / "artifacts" / "page-testimonium"
    _rewrite_record(sorted(directory.iterdir())[0], edit)


def _added_blob(tree: RunTree) -> None:
    tree.put_blob(ATTESTATORES, b"one more retained response")


def _attestatores_blobs(root: Path) -> list[Path]:
    return sorted((root / "r" / "3_attestatores" / "blobs" / "sha256").iterdir())


def test_a_platform_only_change_leaves_the_semantic_digest_unchanged(happy_run, tmp_path):
    """The Attestatores seal's blob inventory is too large to find by trying subsets."""
    original = _through_attestatores(happy_run, tmp_path / "original")
    moved = _through_attestatores(happy_run, tmp_path / "moved", _other_platform)

    assert len(_attestatores_blobs(original)) > 13
    assert snapshot(original) != snapshot(moved)
    assert semantic_snapshot_digest(moved) == semantic_snapshot_digest(original)


@pytest.mark.parametrize("change", [_added_blob, _changed_text], ids=["blob", "text"])
def test_a_content_change_under_a_stage_seal_moves_the_semantic_digest(happy_run, tmp_path, change):
    original = _through_attestatores(happy_run, tmp_path / "original")
    changed = _through_attestatores(happy_run, tmp_path / "changed", change)

    assert semantic_snapshot_digest(changed) != semantic_snapshot_digest(original)


def test_each_run_under_one_root_reduces_its_own_seals(happy_run, tmp_path):
    root = _through_attestatores(happy_run, tmp_path / "runs")
    shutil.copytree(root / "r", root / "second")

    inventory = semantic_snapshot(root)

    first = {key[len("r/") :]: value for key, value in inventory.items() if key.startswith("r/")}
    second = {
        key[len("second/") :]: value
        for key, value in inventory.items()
        if key.startswith("second/")
    }
    assert (
        first
        == second
        == {
            key[len("r/") :]: value
            for key, value in semantic_snapshot(
                _through_attestatores(happy_run, tmp_path / "one")
            ).items()
        }
    )


def test_a_seal_whose_inventory_cannot_be_rebuilt_is_refused(happy_run, tmp_path):
    root = _through_attestatores(happy_run, tmp_path / "unsealed")
    _added_blob(RunTree(root, "r"))

    with pytest.raises(AssertionError, match="attestatores stage seal"):
        semantic_snapshot_digest(root)


# --- 1. The happy path runs offline, and every reference resolves --------------


def test_the_happy_path_delivers_every_reading_and_is_partial_only_for_its_reconstruction(
    happy_run,
):
    _, tree = happy_run
    export = export_of(tree)
    assert sorted(item["act_key"] for item in export["delivered"]) == ["p1:1", "p1:2", "p2:1"]
    assert {item["category"] for item in export["delivered"]} == {"delivered"}
    assert export["non_delivered"] == []
    assert export["other_readings"] == []
    assert export["aggregate"]["status"] == "partial"
    [reason] = export["aggregate"]["reasons"]
    assert reason.startswith("continuation join join-1-2-0 (not-reconstructed)")
    assert "(no-code-join)" in reason


def test_every_input_reference_in_the_run_resolves_and_matches_its_digest(happy_run):
    """The whole traceability claim in one assertion: every artifact names the
    bytes it was derived from, and every one of those references is real."""
    _, tree = happy_run
    checked = 0
    for stage in (
        DOOR,
        EXEMPLAR,
        INK_MAP,
        DESIGNATOR,
        ATTESTATORES,
        PERLECTOR,
        RECENSOR,
        ARCHETYPUS,
    ):
        for entry in tree.build_manifest(stage)["artifacts"]:
            record = tree.read_artifact(stage, entry["kind"], entry["artifact_id"])
            for reference in record["inputs"]:
                verify_input_bytes(reference, tree.read_bytes(reference["relative_path"]))
                checked += 1
    assert checked >= 20, f"only {checked} references checked; the run looks too thin"


def test_every_counted_reading_has_exactly_one_terminal_category(happy_run):
    _, tree = happy_run
    export = export_of(tree)
    entries = [
        tree.read_artifact(ARMARIUM, "manifest-entry", entry["artifact_id"])
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "manifest-entry"
    ]
    assert len(entries) == export["expected_acts"] == 3
    assert len({entry["subject_id"] for entry in entries}) == 3
    categorised = export["delivered"] + export["non_delivered"] + export["other_readings"]
    assert sorted(item["act_id"] for item in categorised) == sorted(
        entry["subject_id"] for entry in entries
    )


def test_the_final_export_keeps_each_original_filename_and_digest_link(happy_run):
    """Every delivered reading names its original source file and the exact ink it was cut from."""
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
    assert len(export["delivered"]) == 3
    for delivered in export["delivered"]:
        perlectio = tree.read_artifact_reference(
            delivered["perlectio_ref"],
            stage=PERLECTOR,
            kind="perlectio",
            subject_id=delivered["act_id"],
        )
        assert perlectio["payload"]["text"] == delivered["text"]
        assert delivered["dissent_ref"] == delivered["perlectio_ref"]
        assert {witness["chair"] for witness in delivered["witnesses"]} == {
            "attestator_1",
            "attestator_2",
            "attestator_3",
        }
        for witness in delivered["witnesses"]:
            testimony = tree.read_artifact_reference(
                witness["testimonium_ref"], stage=ATTESTATORES, kind="page-testimonium"
            )
            assert testimony["payload"]["chair"] == witness["chair"]
            assert testimony["payload"]["page_ordinal"] == delivered["page_ordinal"]
            assert testimony["payload"]["provenance"] == witness["provenance"]
        [region] = delivered["source_regions"]
        source = source_by_ordinal[region["source_page_ordinal"]]
        page = pages_by_ordinal[region["source_page_ordinal"]]
        assert region["source_page_ordinal"] == delivered["page_ordinal"]
        assert region["source_page_id"] == page["page_id"]
        assert region["declared_path"] == source["relative_path"]
        assert region["declared_sha256"] == source["sha256"]
        assert region["region_id"].startswith("rgn_")
        assert digest_bytes(tree.read_bytes(region["image_path"])) == region["image_sha256"]


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
    assert len(recipes) == 10
    assert recipes[INK_MAP] == "deterministic-residual-ink-v1"
    assert all(
        revision.startswith("fake-") for stage, revision in recipes.items() if stage != INK_MAP
    )
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
    # And the fixture is in there too: same scenario, one changed testimony, new digest.
    altered = json.loads(json.dumps(fixture))
    altered["testimony"][0]["payload"] = "SOMETHING ELSE ENTIRELY"
    assert run_config_bindings(config, altered, "happy")["config_digest"] != happy


# --- the commit and the clock the tree could not carry ------------------------


COMMIT = "a1b2c3d4" * 5


def test_the_run_authority_names_the_commit_the_code_ran_at(tmp_path):
    """A tree handed to a fresh session could prove its configuration bytes by
    digest and still not say which code produced them.

    The commit comes from the caller, not from a lookup here: on a pod the
    bootstrap has already read the running checkout back against the pin, so
    `pod_run` forwards a proven fact rather than paying for a weaker one.
    """

    root = tmp_path / "runs"
    assert orchestrate(root, "r", "page-unbroken", repository_commit=COMMIT).returncode == 0

    assert RunTree(root, "r").read_run()["repository_commit"] == COMMIT


def test_a_run_whose_caller_names_no_commit_records_none_rather_than_a_placeholder(tmp_path):
    """Not measured is recorded as not measured, never invented."""

    root = tmp_path / "runs"
    journal = tmp_path / "timings.json"
    assert orchestrate(root, "r", "page-unbroken", stage_timing_journal=journal).returncode == 0

    assert "repository_commit" not in RunTree(root, "r").read_run()
    entry = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert entry["repository_commit"] is None
    assert "no --repository-commit was named" in entry["repository_commit_detail"]


def test_a_short_or_decorated_revision_is_refused_before_the_door_runs(tmp_path):
    """A revision that names a commit only against the repository that resolved
    it is worthless to a fetched tree."""

    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy", repository_commit="a1b2c3d")

    assert result.returncode == 2
    assert "is not a full lowercase" in result.stderr
    assert not (root / "r").exists()


def test_a_stage_timing_journal_records_every_invocation_outside_the_run_tree(
    tmp_path, monkeypatch
):
    """Outside the tree deliberately: a run tree is pinned byte-identical across a
    rerun, a resume and a restored backup, and a clock is not that. `pod_run` names
    this journal beside its report on the volume, where the transcript and the
    liveness tick already live."""

    # A fake card on PATH: the stages inherit it, so a real reading must land.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "nvidia-smi"
    fake.write_text("#!/bin/sh\necho '97, 1234'\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    root = tmp_path / "runs"
    journal = tmp_path / "timings" / "pod-run-report-timings.json"

    assert (
        orchestrate(
            root, "r", "page-unbroken", stage_timing_journal=journal, repository_commit=COMMIT
        ).returncode
        == 0
    )

    entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert all(entry["schema"] == "stage-timing-journal.v4" for entry in entries)
    assert all(entry["run_id"] == "r" for entry in entries)
    stages = [entry["stage"] for entry in entries]
    # Every program the automatic sequence invokes, the Door and the Exemplar
    # named apart although they share `1_exemplar/`.
    assert stages[:2] == ["door", "exemplar"]
    assert stages[-1] == "armarium"
    for entry in entries:
        assert entry["exit_code"] == 0
        assert entry["duration_ms"] >= 0
        assert entry["started_at"].endswith("Z") and entry["finished_at"].endswith("Z")
        assert entry["repository_commit"] == COMMIT
        assert entry["repository_commit_detail"] is None
        gpu = entry["gpu_utilization"]
        assert entry["gpu_utilization_reason"] is None
        assert gpu["sample_count"] >= 1 and gpu["mean"] == 97 and gpu["max"] == 97
        assert gpu["busy_fraction_over_95"] == 1.0
        assert gpu["samples"][0]["memory_used_mib"] == 1234
        # Unset: the Perlector keeps its served row's bound; no other stage has one.
        assert entry["perlector_concurrency"] is None


def test_a_short_revision_is_refused_on_a_run_that_does_not_start_at_the_door(tmp_path):
    """Every selected sequence validates it, not only the one that opens at the Door.

    A manual or semi run that starts after the Door, with no timing journal
    configured, must not accept a malformed `repository_commit` and go on to
    execute stages.
    """

    root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            "happy",
            "--run-id",
            "r",
            "--run-root",
            str(root),
            "--stage",
            "recensor",
            "--repository-commit",
            "a1b2c3d",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "is not a full lowercase" in result.stderr
    assert not (root / "r").exists()


def test_a_timing_journal_inside_the_run_tree_is_refused_before_anything_runs(tmp_path):
    """The option says "outside the run tree" and now nothing else has to.

    A journal under the run directory would add mutable bytes to an immutable
    tree once per stage invocation and change the byte identity the rerun,
    resume and restore checks all rest on.
    """

    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy", stage_timing_journal=root / "r" / "timings.json")

    assert result.returncode == 2
    assert "is inside this run's own tree" in result.stderr
    assert not (root / "r" / "timings.json").exists()


# --- 2. Repeating the identical command changes nothing ------------------------


def test_repeating_the_identical_command_leaves_every_byte_unchanged(tmp_path):
    root = tmp_path / "runs"
    # `happy` reads its two pages whole and ends partial: the act running across
    # the page break is delivered as each side's literal beside a labelled,
    # unconfirmed reconstruction.
    assert orchestrate(root, "r", "happy").returncode == 3
    before = snapshot(root)

    # The count includes every retained raw witness response and, because the
    # Chandra adapter runs the vendor's own `scale_to_fit`, the page image it was
    # actually shown, one per witnessed page.
    assert len(before) == HAPPY_SNAPSHOT_FILES
    assert semantic_snapshot_digest(root) == HAPPY_RUN_TREE_DIGEST
    assert orchestrate(root, "r", "happy").returncode == 3
    after = snapshot(root)

    assert after == before
    assert semantic_snapshot_digest(root) == HAPPY_RUN_TREE_DIGEST


def test_repeating_the_review_scenario_also_changes_nothing(orchestrated_run, tmp_path):
    """The page-read review scenario holds an entry, so it is the one that could
    most easily append on every run."""
    root = tmp_path / "runs"
    orchestrated_run(root, "r", "page-review", 3)
    before = snapshot(root)

    assert len(before) == REVIEW_SNAPSHOT_FILES
    assert semantic_snapshot_digest(root) == REVIEW_RUN_TREE_DIGEST
    assert orchestrate(root, "r", "page-review").returncode == 3
    assert snapshot(root) == before
    assert semantic_snapshot_digest(root) == REVIEW_RUN_TREE_DIGEST


# --- 3. An incompatible run id fails before writing ----------------------------


def test_reusing_a_run_id_with_a_changed_configuration_fails_before_writing(
    orchestrated_run, tmp_path
):
    root = tmp_path / "runs"
    orchestrated_run(root, "r", "page-unbroken")
    before = snapshot(root)

    # The scenario is part of the run's configuration digest, so the same run id
    # under a different scenario is a different run wearing an old name. Both
    # scenarios declare the *same* two source pages, so the source manifest
    # cannot be what catches this — only the config digest can, and this is the
    # assertion that says so.
    result = orchestrate(root, "r", "page-review")

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


def test_an_interrupted_run_resumes_without_rewriting_what_survived(orchestrated_run, tmp_path):
    """Interrupt for real: delete everything from the Perlector onward, as though
    the process died mid-run, then run the same command again."""
    root = tmp_path / "runs"
    orchestrated_run(root, "r", "page-unbroken")
    complete = snapshot(root)

    for stage_directory in ("4_perlector", "5_recensor", "6_archetypus", "7_armarium"):
        shutil.rmtree(root / "r" / stage_directory)
    survivors = snapshot(root)
    survivor_identities = file_identities(root)
    assert len(survivors) < len(complete)

    assert orchestrate(root, "r", "page-unbroken").returncode == 0
    resumed = snapshot(root)
    resumed_identities = file_identities(root)

    # Everything that survived is byte-identical: resume reused it rather than
    # redoing it. And the finished tree is identical to the uninterrupted one.
    for path, digest in survivors.items():
        # Membership first, or a deleted survivor reports as a bare KeyError
        # that reads like a broken test rather than a page that left the run.
        assert path in resumed, f"resume deleted surviving evidence at {path}"
        assert resumed[path] == digest, f"{path} was rewritten on resume"
    # Byte-identity alone cannot tell reuse from an identical rewrite, which is
    # the whole claim in this test's name. Every publication mints a new inode,
    # so an unchanged one is proof the evidence was never republished.
    checked = 0
    for path, identity in survivor_identities.items():
        if not is_immutable_evidence(path):
            continue
        checked += 1
        assert path in resumed_identities, f"resume deleted surviving evidence at {path}"
        assert resumed_identities[path] == identity, (
            f"{path} kept its bytes but was republished on resume"
        )
    assert checked, "the identity check ran over no evidence at all"
    assert resumed == complete


def test_a_run_interrupted_at_every_boundary_resumes_to_the_same_tree_and_tally(
    orchestrated_run, tmp_path
):
    """Resume must preserve held work and incident-based tallying."""
    policy = load_hard_failure_policy(ROOT / "config" / "hard_failure.toml")

    reference_root = tmp_path / "reference"
    orchestrated_run(reference_root, "r", "page-review", 3)
    reference = snapshot(reference_root)
    reference_tally = tally_hard_failures(RunTree(reference_root, "r"), policy)

    # The `page-review` scenario holds a reading, and the Recensor is what
    # decides that: the whole run ends on the hold at 3 and no stage after the
    # Recensor runs. So every boundary strictly before the Recensor, in the
    # orchestrator's own order, leaves a partial tree that completes cleanly at
    # 0; stopping at the Recensor itself is the whole held run, and anything
    # later is unreachable. The boundaries are read from the sequence rather
    # than listed here so moving a stage (the Coniector sits after the
    # Perlector now) cannot leave this test checking a stale order, and the
    # ones after the Perlector matter most: the held-reading tally lives there.
    sequence = tuple(load_stage("orchestrator").SEQUENCE_NAMES)
    holding_stage = RECENSOR
    partial_boundaries = sequence[: sequence.index(holding_stage)]
    assert PERLECTOR in partial_boundaries
    for stage in sequence[sequence.index(holding_stage) + 1 :]:
        assert not (reference_root / "r" / WRITING_DIRECTORIES[stage]).exists(), (
            f"the reference run was expected to halt at the {holding_stage}, yet {stage} ran"
        )

    def stop_at(stage: str) -> tuple[Path, subprocess.CompletedProcess[str]]:
        root = tmp_path / f"stopped-at-{stage}"
        partial = subprocess.run(
            [
                sys.executable,
                str(ORCHESTRATOR),
                "--fixture",
                FIXTURE,
                "--scenario",
                "page-review",
                "--run-id",
                "r",
                "--run-root",
                str(root),
                "--from",
                "door",
                "--to",
                stage,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        return root, partial

    # Stopping at the holding stage is the whole run, so the premise below
    # (`len(survivors) < len(reference)`) is checked here rather than assumed.
    root, partial = stop_at(holding_stage)
    assert partial.returncode == EXIT_HELD, partial.stderr
    assert snapshot(root) == reference, f"stopping at the {holding_stage} is not the whole run"

    for boundary in partial_boundaries:
        root, partial = stop_at(boundary)
        assert partial.returncode == 0, partial.stderr
        survivors = snapshot(root)
        survivor_identities = file_identities(root)
        assert survivors, f"stopping at {boundary} wrote nothing to resume from"
        assert len(survivors) < len(reference)

        assert orchestrate(root, "r", "page-review").returncode == 3
        resumed = snapshot(root)
        resumed_identities = file_identities(root)
        assert resumed == reference, f"resuming after {boundary} did not land on the same tree"
        # The tree being right does not mean the resume reused what it found; a
        # stage that redid its work and wrote the same bytes lands the same tree.
        # The inode is what separates the two.
        checked = 0
        for path, identity in survivor_identities.items():
            if not is_immutable_evidence(path):
                continue
            checked += 1
            assert resumed_identities[path] == identity, (
                f"resuming after {boundary} republished {path} instead of reusing it"
            )
        assert checked, f"stopping at {boundary} left no evidence to check the identity of"
        assert tally_hard_failures(RunTree(root, "r"), policy) == reference_tally


# --- 5. Witness evidence cannot change under the reading that used it ---------


def artifacts(tree: RunTree, stage: str, kind: str) -> list[dict]:
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind
    ]


def _through_perlector(root: Path, scenario: str = "happy") -> RunTree:
    for program in programs_through("perlector"):
        result = invoke_stage(root, "r", scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return RunTree(root, "r")


def _read_page_testimonium(tree: RunTree, chair: str | None = None) -> dict:
    return next(
        record
        for record in artifacts(tree, ATTESTATORES, "page-testimonium")
        if record["outcome"] == "read" and chair in (None, record["payload"]["chair"])
    )


def _publish_page_testimonium(tree: RunTree, record: dict) -> None:
    record["self_hash"] = self_hash(
        {key: value for key, value in record.items() if key != "self_hash"}
    )
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "page-testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(record))


def test_a_page_reading_retains_digest_checked_page_testimonia_it_used(tmp_path):
    """Changing a page witness after the reading must stop the next real consumer."""
    root = tmp_path / "runs"
    tree = _through_perlector(root)
    changed = _read_page_testimonium(tree)
    changed["payload"]["payload"] = "changed after the page was read"
    _publish_page_testimonium(tree, changed)
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 2
    assert "changed under a sealed reference" in result.stderr
    assert snapshot(root) == before


def test_the_recensor_refuses_a_page_testimonium_ordinal_its_identity_does_not_bind(tmp_path):
    """A new artifact identity over the old ordinal is an ambiguity, never an attempt 2
    the Recensor may choose between."""
    root = tmp_path / "runs"
    tree = _through_perlector(root)
    original = _read_page_testimonium(tree)
    page_id, chair = original["subject_id"], original["payload"]["chair"]
    forged = json.loads(json.dumps(original))
    forged["attempt_id"] = attempt_id(page_id, f"read:{chair}", 2)
    forged["artifact_id"] = artifact_id(
        ATTESTATORES, "page-testimonium", page_id, forged["attempt_id"]
    )
    _publish_page_testimonium(tree, forged)
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 2
    assert "claims attempt ordinal 1 in its payload" in result.stderr
    assert snapshot(root) == before


def test_the_recensor_refuses_a_page_testimonium_from_a_chair_the_run_never_sealed(tmp_path):
    """Two real witnesses and a stranger must never read as three against the floor."""
    root = tmp_path / "runs"
    tree = _through_perlector(root)
    forged = json.loads(json.dumps(_read_page_testimonium(tree, "attestator_1")))
    forged["payload"]["chair"] = "attestator_9"
    forged["attempt_id"] = attempt_id(forged["subject_id"], "read:attestator_9", 1)
    forged["artifact_id"] = artifact_id(
        ATTESTATORES, "page-testimonium", forged["subject_id"], forged["attempt_id"]
    )
    _publish_page_testimonium(tree, forged)

    result = invoke_stage(root, "r", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode != 0, "an unsealed chair was accepted into the coverage count"
    assert "['attestator_9'], which this run did not seal as page witnesses" in result.stderr
    assert not (root / "r" / "5_recensor").exists()


def test_an_unknown_attestatores_tally_holds_an_orchestrated_rerun(orchestrated_run, tmp_path):
    """A damaged independent count cannot hide behind an old complete export."""
    root = tmp_path / "runs"
    orchestrated_run(root, "r", "page-unbroken")
    tree = RunTree(root, "r")
    tree.resolve(tree.manifest_path(ATTESTATORES)).write_bytes(b"{")
    before = snapshot(root)

    result = orchestrate(root, "r", "page-unbroken")

    assert result.returncode == 3
    assert "UNKNOWN" in result.stderr
    assert "run r: held; its reason is on stderr above" in result.stdout
    assert snapshot(root) == before


def test_an_explicitly_absent_witness_counts_against_the_floor_on_every_page(
    tmp_path, absent_third_chair_config
):
    """Absence through the real stage programs, not only the config parser.

    The run still seals the whole roster; the absent chair testifies to no page,
    so every page is read by two witnesses against a floor of three and nothing
    is delivered as fully witnessed.
    """
    root = tmp_path / "runs"
    result = orchestrate_to_export(
        root,
        "r",
        "happy",
        models_config=absent_third_chair_config,
        serving_recipes_config=DEFAULT_SERVING_RECIPES_CONFIG_PATH,
    )
    assert result.returncode == 3, result.stderr
    tree = RunTree(root, "r")
    assert tree.read_run()["witness_chairs"] == ["attestator_1", "attestator_2", "attestator_3"]
    page_testimonia = artifacts(tree, ATTESTATORES, "page-testimonium")
    assert sorted(
        (record["payload"]["page_ordinal"], record["payload"]["chair"])
        for record in page_testimonia
    ) == [(1, "attestator_1"), (1, "attestator_2"), (2, "attestator_1"), (2, "attestator_2")]
    export = export_of(tree)
    assert export["delivered"] == []
    assert export["aggregate"]["status"] == "partial"
    assert len(export["non_delivered"]) == 2
    for item in export["non_delivered"]:
        coverage = item["witness_coverage"]
        assert (coverage["configured"], coverage["floor"]) == (2, 3)
        assert coverage["by_outcome"] == {"read": 2}
        assert coverage["under_witnessed"] is True
        assert "under-witnessed" in item["reason"]


# --- 7. Every contract handoff refuses corruption ------------------------------

# (producer, consumer, the artifact kind that crosses this boundary)
HANDOFF_ARTIFACTS = (
    (DOOR, EXEMPLAR, "admission"),
    (EXEMPLAR, INK_MAP, "page"),
    (INK_MAP, DESIGNATOR, "ink-map"),
    (DESIGNATOR, ATTESTATORES, "detector-region"),
    (ATTESTATORES, PERLECTOR, "page-testimonium"),
    (PERLECTOR, RECENSOR, "perlectio"),
    (PERLECTOR, CONIECTOR, "perlectio"),
    (RECENSOR, ARCHETYPUS, "review"),
    (ARCHETYPUS, ARMARIUM, "archetypus"),
)

# A sibling to HANDOFF_ARTIFACTS: seals prove complete stage boundaries, including
# Armarium's final one, which the orchestrator itself consumes.
SEAL_ARTIFACTS = (
    (DOOR, EXEMPLAR),
    (EXEMPLAR, INK_MAP),
    (INK_MAP, DESIGNATOR),
    (DESIGNATOR, ATTESTATORES),
    (ATTESTATORES, PERLECTOR),
    (PERLECTOR, RECENSOR),
    (RECENSOR, ARCHETYPUS),
    (ARCHETYPUS, ARMARIUM),
    (CONIECTOR, ARMARIUM),
    (ARMARIUM, "orchestrator"),
)

CONSUMER_PROGRAMS = {
    EXEMPLAR: "pipeline/1_exemplar/run.py",
    INK_MAP: "pipeline/1_ink_map/run.py",
    DESIGNATOR: "pipeline/2_designator/run.py",
    ATTESTATORES: "pipeline/3_attestatores/run.py",
    PERLECTOR: "pipeline/4_perlector/run.py",
    RECENSOR: "pipeline/5_recensor/run.py",
    ARCHETYPUS: "pipeline/6_archetypus/run.py",
    CONIECTOR: "pipeline/4b_coniector/run.py",
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


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer", SEAL_ARTIFACTS)
def test_each_stage_seal_corruption_stops_its_named_consumer(
    happy_run, tmp_path, producer, consumer
):
    """Every seal has a downstream reader; Armarium's reader is the orchestrator."""
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    path = _stage_seal_path(tree, producer)
    record = json.loads(path.read_bytes())
    record["schema"] = "skeleton.v99"
    path.write_bytes(canonical_bytes(record))
    before = snapshot(root)

    if consumer == "orchestrator":
        # Do not rerun Armarium: that would let the producer's own scan refuse
        # first and would not exercise the final consumer this row names. The
        # orchestrator now reaches that boundary through `verify_final_seal`,
        # which is the same seal contract plus the export check, so that is what
        # this row drives. `SchemaRefusal` is a `ContractError`, and the message
        # match is kept so the refusal still has to name the forged config.
        with pytest.raises(ContractError, match="skeleton.v99|schema"):
            verify_final_seal(tree)
        assert snapshot(root) == before
        return

    result = invoke_stage(root, "r", "happy", CONSUMER_PROGRAMS[consumer])

    assert result.returncode != 0
    assert "skeleton.v99" in result.stderr or "SchemaRefusal" in result.stderr
    assert snapshot(root) == before


def _further_seal_readers() -> list[tuple[str, str]]:
    from common.contracts.stages import seal_readers

    return [
        (producer, reader)
        for producer in STAGES
        for reader in seal_readers(producer)
        if (producer, reader) not in SEAL_ARTIFACTS
    ]


def test_the_perlector_seal_has_a_further_reader_in_the_coniector():
    """The battery below is parametrized over this list, so an empty list would skip it."""
    assert (PERLECTOR, CONIECTOR) in _further_seal_readers()


@pytest.mark.full
@pytest.mark.parametrize("producer,reader", _further_seal_readers())
def test_every_further_reader_of_a_seal_refuses_it_corrupted(happy_run, tmp_path, producer, reader):
    """A seal read by more than one stage (the Perlector's, by the Recensor and the
    Coniector) is refused by each reader, not only the one the battery above names."""
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    path = _stage_seal_path(tree, producer)
    record = json.loads(path.read_bytes())
    record["schema"] = "skeleton.v99"
    path.write_bytes(canonical_bytes(record))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", CONSUMER_PROGRAMS[reader])

    assert result.returncode != 0
    assert "skeleton.v99" in result.stderr or "SchemaRefusal" in result.stderr
    assert snapshot(root) == before


def _stage_seal_path(tree: RunTree, stage: str) -> Path:
    entry = next(
        entry for entry in tree.build_manifest(stage)["artifacts"] if entry["kind"] == "stage-seal"
    )
    return tree.resolve(entry["relative_path"])


@pytest.mark.full
def test_next_stage_refuses_blob_content_changed_under_the_named_exemplar_seal(happy_run, tmp_path):
    """A blob name is content-addressed only until disk bytes are independently read."""
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    blob = next(
        path
        for path in tree.resolve("1_exemplar/blobs").rglob("*")
        if path.is_file() and path.name != tree.read_run()["register_digest"]
    )
    blob.write_bytes(b"tampered bytes under the same filename")

    result = invoke_stage(root, "r", "happy", "pipeline/1_ink_map/run.py")

    assert result.returncode == EXIT_FATAL
    assert "exemplar stage-seal" in result.stderr
    assert "inventory no longer matches disk" in result.stderr


@pytest.mark.full
def test_next_stage_refuses_artifact_added_after_the_named_boundary(happy_run, tmp_path):
    """The Exemplar-to-Ink-Map addition case. Its sibling below is one link later."""
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    forged = build_envelope(
        run_id="r",
        artifact_id=artifact_id(EXEMPLAR, "added-after-seal", "added", None),
        subject_id="added",
        stage=EXEMPLAR,
        kind="added-after-seal",
        outcome="sealed",
        config_digest=tree.read_run()["config_digest"],
        adapter_revision=tree.read_artifact(
            EXEMPLAR,
            "page",
            next(
                entry["artifact_id"]
                for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
                if entry["kind"] == "page"
            ),
        )["producer"]["adapter_revision"],
        inputs=[],
        payload={"deliberately": "unaccounted"},
    )
    tree.publish_artifact(forged)

    result = invoke_stage(root, "r", "happy", "pipeline/1_ink_map/run.py")

    assert result.returncode == EXIT_FATAL
    assert "exemplar stage-seal" in result.stderr
    assert "inventory no longer matches disk" in result.stderr


@pytest.mark.full
def test_next_stage_refuses_an_ink_map_artifact_added_after_the_named_boundary(happy_run, tmp_path):
    """Each predecessor link needs its own added-artifact corruption proof."""
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    forged = build_envelope(
        run_id="r",
        artifact_id=artifact_id(INK_MAP, "added-after-seal", "added", None),
        subject_id="added",
        stage=INK_MAP,
        kind="added-after-seal",
        # An ordinary stage kind may not wear a boundary outcome; the forged
        # addition uses the stage's own vocabulary and is still refused by the
        # seal's inventory.
        outcome="mapped",
        config_digest=tree.read_run()["config_digest"],
        adapter_revision=tree.read_artifact(
            INK_MAP,
            "ink-map",
            next(
                entry["artifact_id"]
                for entry in tree.build_manifest(INK_MAP)["artifacts"]
                if entry["kind"] == "ink-map"
            ),
        )["producer"]["adapter_revision"],
        inputs=[],
        payload={"deliberately": "unaccounted"},
    )
    tree.publish_artifact(forged)

    result = invoke_stage(root, "r", "happy", "pipeline/2_designator/run.py")

    assert result.returncode == EXIT_FATAL
    assert "ink-map stage-seal" in result.stderr
    assert "inventory no longer matches disk" in result.stderr


@pytest.mark.full
def test_next_stage_refuses_an_exemplar_artifact_removed_after_the_boundary(happy_run, tmp_path):
    """A later boundary can stay green while Exemplar removal checks regress."""
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    page = tree.resolve(
        tree.artifact_path(
            EXEMPLAR,
            "page",
            next(
                entry["artifact_id"]
                for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
                if entry["kind"] == "page"
            ),
        )
    )
    assert page.is_file()
    page.unlink()

    result = invoke_stage(root, "r", "happy", "pipeline/1_ink_map/run.py")

    assert result.returncode == EXIT_FATAL
    assert "exemplar stage-seal" in result.stderr
    assert "inventory no longer matches disk" in result.stderr


@pytest.mark.full
def test_next_stage_refuses_an_artifact_removed_after_the_named_boundary(happy_run, tmp_path):
    """The Ink-Map-to-Designator removal case, one stage later than the test above.

    "Added or removed after the boundary" is the one corruption class a
    manifest-derived seal adds over the envelope self-hash. The Exemplar link
    is proved above; this proves the same recompute one stage later so that
    link is not left covered only for addition, never for removal.
    """
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    ink_map = tree.resolve(
        tree.artifact_path(
            INK_MAP,
            "ink-map",
            next(
                entry["artifact_id"]
                for entry in tree.build_manifest(INK_MAP)["artifacts"]
                if entry["kind"] == "ink-map"
            ),
        )
    )
    assert ink_map.is_file()
    ink_map.unlink()

    result = invoke_stage(root, "r", "happy", "pipeline/2_designator/run.py")

    assert result.returncode == EXIT_FATAL
    assert "ink-map stage-seal" in result.stderr
    assert "inventory no longer matches disk" in result.stderr


@pytest.mark.full
def test_next_stage_refuses_a_deleted_exemplar_seal_without_rederiving(happy_run, tmp_path):
    source_root, _ = happy_run
    missing_root = tmp_path / "missing"
    shutil.copytree(source_root, missing_root)

    missing_tree = RunTree(missing_root, "r")
    _stage_seal_path(missing_tree, EXEMPLAR).unlink()
    missing = invoke_stage(missing_root, "r", "happy", "pipeline/1_ink_map/run.py")
    assert missing.returncode == EXIT_FATAL
    assert "exemplar has no stage-seal" in missing.stderr
    assert "never re-derived" in missing.stderr


@pytest.mark.full
def test_next_stage_refuses_a_deleted_ink_map_seal_without_rederiving(happy_run, tmp_path):
    """The Ink-Map-to-Designator link, one stage later than the test above."""
    source_root, _ = happy_run
    missing_root = tmp_path / "missing"
    shutil.copytree(source_root, missing_root)

    missing_tree = RunTree(missing_root, "r")
    _stage_seal_path(missing_tree, INK_MAP).unlink()
    missing = invoke_stage(missing_root, "r", "happy", "pipeline/2_designator/run.py")
    assert missing.returncode == EXIT_FATAL
    assert "ink-map has no stage-seal" in missing.stderr
    assert "never re-derived" in missing.stderr


@pytest.mark.full
def test_every_handoff_in_the_contract_is_covered_by_this_table():
    """Meta-invariant #91 — a drift check over an agreement surface. If a handoff
    is added to the contracts and not to this table, the boundary test would
    silently cover six of seven."""
    from common.contracts.stages import HANDOFFS

    assert {(producer, consumer) for producer, consumer, _ in HANDOFF_ARTIFACTS} == set(HANDOFFS)
    assert {consumer for _, consumer, _ in HANDOFF_ARTIFACTS} == set(CONSUMER_PROGRAMS)
    assert len(HANDOFF_ARTIFACTS) == 9


def test_every_stage_has_one_seal_battery_row():
    from common.contracts.stages import STAGES

    assert {producer for producer, _ in SEAL_ARTIFACTS} == set(STAGES)
    assert len(SEAL_ARTIFACTS) == 10


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


# --- 8. A refused page cannot vanish -------------------------------------------
#
# A run that lost a whole page once reported `status: complete, reasons: []`.
# `refused-first-page` declares page 1 with a digest it does not have.


@pytest.fixture(scope="module")
def refused_first_page_run(tmp_path_factory):
    """Page 1 is refused at the door; page 2 is sealed and witnessed."""
    root = tmp_path_factory.mktemp("refused_first_page")
    result = orchestrate_to_export(root, "r", "refused-first-page")
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r"), result


def test_the_orchestrator_relays_the_doors_private_refusal_report(refused_first_page_run):
    """A successful Door can still have a named refusal report for an operator."""
    _, tree, result = refused_first_page_run
    assert "1 door refusal(s); private refusal report:" in result.stderr
    [report] = artifacts(tree, DOOR, "refusal-report")
    assert report["artifact_id"] in result.stderr


def test_the_door_really_refused_page_one_through_its_own_inspection(refused_first_page_run):
    _, tree, _ = refused_first_page_run
    refusals = [
        record for record in artifacts(tree, EXEMPLAR, "page") if record["outcome"] == "refused"
    ]
    assert len(refusals) == 1
    assert refusals[0]["payload"]["ordinal"] == 1
    assert "digest" in refusals[0]["payload"]["reason"]


def test_the_page_loss_is_named_and_the_run_is_partial(refused_first_page_run):
    _, tree, _ = refused_first_page_run
    export = export_of(tree)
    assert export["aggregate"]["status"] == "partial"
    assert any(
        reason.startswith("page 1 was refused:") for reason in export["aggregate"]["reasons"]
    )
    assert export["aggregate"]["by_page_outcome"] == {"sealed": 1, "refused": 1}
    [page] = [page for page in export["pages"] if page["ordinal"] == 1]
    assert page["outcome"] == "refused" and page["reason"].startswith("digest-mismatch")


def test_nothing_is_read_or_delivered_from_a_lost_page(refused_first_page_run):
    """The lost page is named where its reading would be, and no reading or witness
    pretends to have seen it; the surviving page is read from its own witnesses."""
    _, tree, _ = refused_first_page_run
    readings = {
        record["payload"]["page_ordinal"]: record
        for record in artifacts(tree, PERLECTOR, "page-reading")
    }
    assert sorted(readings) == [1, 2]
    lost = readings[1]
    assert lost["outcome"] == "held"
    assert [problem["code"] for problem in lost["payload"]["problems"]] == ["page-not-sealed"]
    assert lost["payload"]["answer"] is None
    witnessed = {
        record["payload"]["page_ordinal"]
        for record in artifacts(tree, ATTESTATORES, "page-testimonium")
    }
    assert witnessed == {2}, "no witness is shown a page the Door refused"
    assert readings[2]["outcome"] == "read"

    export = export_of(tree)
    assert all(not item["act_key"].startswith("p1:") for item in export["delivered"])
    entries = [
        entry
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "manifest-entry"
    ]
    assert len(entries) == export["expected_acts"], (
        "every counted unit still has exactly one category"
    )


def test_no_fixture_page_holds_for_edge_ink_now_that_the_band_is_a_fraction(tmp_path):
    """No fixture page holds for edge ink now that the band is a fraction.

    `edge_band_bp` resolves to 2 pixels on the `happy` scenario's 200-pixel
    pages, which carry zero ink in every band up to 20, so the Ink Map maps both
    pages and holds neither. This was the repository's only end-to-end proof of an Ink Map
    page hold reaching the export, and it went away because the retired
    64-pixel band was body text.

    **What this test protects is that the loss stays visible.** The scenario
    still exits held and still exports partial -- for its own cause, the act
    that may cross its page break -- and no page reason mentions edge ink any
    more.
    If a future fixture page gains ink near its edge, this test fails and the
    edge path's end-to-end proof comes back with it, a known gap recorded in
    `pipeline/1_ink_map/CONTRACT.md`.
    """
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy")
    assert result.returncode == EXIT_HELD

    tree = RunTree(root, "r")
    export = export_of(tree)
    bundle = tree.read_bytes(export["bundle"]["reference"]["relative_path"])
    with ZipFile(BytesIO(bundle)) as archive:
        manifest = json.loads(archive.read("EXPORT_MANIFEST.json"))

    assert manifest["claims"]["status"] == "partial"
    assert manifest["claims"]["ink_map"]["held_pages"] == []
    assert not any(
        "unclaimed-edge-ink" in reason for reason in manifest["claims"]["partial_reasons"]
    )
    assert manifest["claims"]["partial_reasons"], (
        "the scenario must still be visibly partial for its own cause; a green "
        "export here would be a far larger finding than the band"
    )


# --- The export boundary rechecks each sealed page -------------------------------


def test_armarium_rechecks_the_filename_a_page_was_sealed_under(orchestrated_run, tmp_path):
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
    orchestrated_run(root, "r", "page-unbroken")
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
            "page-unbroken",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "no longer matches its submitted filename and digest" in result.stderr
    assert snapshot(root) == before


def test_armarium_rechecks_sealed_pixels_tampered_after_designator(orchestrated_run, tmp_path):
    """The final export has its own pixel boundary, not only a census boundary."""
    root = tmp_path / "runs"
    orchestrated_run(root, "r", "page-unbroken")
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
            "page-unbroken",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "changed under a sealed reference" in result.stderr
    assert snapshot(root) == before
