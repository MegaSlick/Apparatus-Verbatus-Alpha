"""Spec 07 retention and native-Testimonium tests over the real stage program."""

import ast
import copy
import importlib.util
import inspect
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.envelope import build_envelope
from common.contracts.errors import ApprovalRefusal, IncompatibleReuse, SchemaRefusal
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.outcomes import witness_coverage
from common.contracts.stages import ATTESTATORES, DESIGNATOR
from common.runtree.store import RunTree
from common.stage import latest_per_chair

ROOT = Path(__file__).resolve().parents[2]


def _load_attestatores():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("attestatores_retention_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


attestatores = _load_attestatores()


def invoke_stage(
    run_root: Path,
    run_id: str,
    scenario: str,
    program: str,
    *,
    fixture_root: Path = ROOT / "proof",
    **extra,
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(run_root),
        "--run-id",
        run_id,
        "--scenario",
        scenario,
        "--fixture-root",
        str(fixture_root),
    ]
    for key, value in extra.items():
        command.extend((f"--{key.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def run_to_designator(
    tmp_path: Path,
    scenario: str,
    *,
    fixture_root: Path = ROOT / "proof",
    models_config: Path | None = None,
) -> tuple[Path, RunTree]:
    run_root = tmp_path / "runs"
    extra = {} if models_config is None else {"models_config": models_config}
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
    ):
        result = invoke_stage(
            run_root, "retention", scenario, program, fixture_root=fixture_root, **extra
        )
        expected = (
            3
            if program == "pipeline/2_designator/run.py"
            and scenario in {"refused-page", "refused-first-page", "structure-failure"}
            else 0
        )
        assert result.returncode == expected, f"{program}: {result.stderr}"
    return run_root, RunTree(run_root, "retention")


def absent_third_chair_config(tmp_path: Path) -> Path:
    """A models.toml whose third witness chair is explicitly absent.

    The same substitution `pipeline/orchestrator/test_orchestrator_acceptance.py`
    makes, kept here so this file can exercise the reread guard rails against a
    real `AbsentChair` rather than against a stub of one.
    """
    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    live = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    configured = """[chairs.attestator_3]
state = "configured"
source = "local-repository"
path = "attestator_3"
digest_manifest = "b9d6f5b6400e8aa36ecc35bad33cb4c54bb69b207e1ffdea39e1999cfa7e523a"
manifest = "manifests/attestator_3.json"
serving_recipe = "fake-attestatores-v0"
license_note = "fixture identity only; no model weights or model license apply"
"""
    absent = """[chairs.attestator_3]
state = "absent"
reason = "fixture test removes this witness without replacing it"
"""
    assert configured in live
    path = config_root / "models.toml"
    path.write_text(live.replace(configured, absent), encoding="utf-8")
    return path


def _testimonia(tree: RunTree) -> list[dict]:
    return [
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
    ]


def _testimonium_for(tree: RunTree, *, act_key: str, chair: str, ordinal: int) -> dict:
    return next(
        record
        for record in _testimonia(tree)
        if record["payload"]["act_key"] == act_key
        and record["payload"]["chair"] == chair
        and record["payload"]["attempt_ordinal"] == ordinal
    )


def test_reread_appends_and_current_keeps_the_new_failed_outcome(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "reread-failure")
    first_result = invoke_stage(
        run_root,
        "retention",
        "reread-failure",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=1,
    )
    assert first_result.returncode == 0, first_result.stderr
    first = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    first_path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", first["artifact_id"]))
    first_bytes = first_path.read_bytes()

    # A same-ordinal resume is exact-byte reuse, not an overwrite path.
    resumed = invoke_stage(
        run_root,
        "retention",
        "reread-failure",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=1,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert first_path.read_bytes() == first_bytes

    reread = invoke_stage(
        run_root,
        "retention",
        "reread-failure",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )
    assert reread.returncode == 0, reread.stderr
    records = [
        record
        for record in _testimonia(tree)
        if record["payload"]["act_key"] == "a1" and record["payload"]["chair"] == "attestator_3"
    ]
    assert [
        record["payload"]["attempt_ordinal"]
        for record in sorted(records, key=lambda record: record["payload"]["attempt_ordinal"])
    ] == [1, 2]
    assert first_path.read_bytes() == first_bytes
    assert len({record["artifact_id"] for record in records}) == 2
    current = next(
        record
        for record in latest_per_chair(records, "reread Testimonia")
        if record["payload"]["chair"] == "attestator_3"
    )
    assert current["payload"]["attempt_ordinal"] == 2
    assert current["outcome"] == "failed"
    assert attestatores.attempt_tally(tree)["count"] == 12


# --- The targeted reread: one chair, one act ------------------------------------
#
# The whole-pass reread above re-witnesses every chair on every act to move one of
# them. That is the right instrument for a resume and the wrong one for a reread,
# which happens because one witness failed on one act. These tests drive the
# narrow path: the ordinal comes from that chair's own history, and nothing else
# in the folder moves.


def _act_id_for(tree: RunTree, act_key: str) -> str:
    return next(
        record["subject_id"]
        for record in _testimonia(tree)
        if record["payload"]["act_key"] == act_key
    )


def _reread(run_root: Path, scenario: str, act_id: str, chair: str, **extra):
    return invoke_stage(
        run_root,
        "retention",
        scenario,
        "pipeline/3_attestatores/run.py",
        operation="reread",
        act=act_id,
        chair=chair,
        **extra,
    )


def test_a_targeted_reread_moves_one_chair_and_leaves_every_other_chair_alone(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "reread-failure")
    assert (
        invoke_stage(
            run_root, "retention", "reread-failure", "pipeline/3_attestatores/run.py"
        ).returncode
        == 0
    )
    first = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    first_path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", first["artifact_id"]))
    first_bytes = first_path.read_bytes()
    untouched = {
        record["artifact_id"]: tree.resolve(
            tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"])
        ).read_bytes()
        for record in _testimonia(tree)
    }

    result = _reread(run_root, "reread-failure", _act_id_for(tree, "a1"), "attestator_3")
    assert result.returncode == 0, result.stderr

    records = _testimonia(tree)
    # One new artifact, and every artifact that existed before is byte-identical.
    assert len(records) == len(untouched) + 1
    for record in records:
        if record["artifact_id"] in untouched:
            path = tree.resolve(
                tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"])
            )
            assert path.read_bytes() == untouched[record["artifact_id"]]
    assert first_path.read_bytes() == first_bytes

    moved = [
        record
        for record in records
        if record["payload"]["act_key"] == "a1" and record["payload"]["chair"] == "attestator_3"
    ]
    assert sorted(record["payload"]["attempt_ordinal"] for record in moved) == [1, 2]
    others = [record for record in records if record not in moved]
    assert others and all(record["payload"]["attempt_ordinal"] == 1 for record in others), (
        "a reread of one chair must not append an attempt for any other chair"
    )

    current = next(
        record
        for record in latest_per_chair(moved, "reread Testimonia")
        if record["payload"]["chair"] == "attestator_3"
    )
    assert current["payload"]["attempt_ordinal"] == 2
    assert current["outcome"] == "failed"
    assert attestatores.attempt_tally(tree)["count"] == 7


def test_the_whole_pass_still_resumes_over_a_folder_one_chair_has_been_reread_in(tmp_path):
    """A reread moves one chair's ordinal and no other's, so the whole pass — which
    the orchestrator always invokes at ordinal 1 — has to stay a resume rather than
    become a hold. Every write it makes here is byte-identical to one already on
    disk, and the RunTree would refuse it outright if it were not."""
    run_root, tree = run_to_designator(tmp_path, "reread-failure")
    assert (
        invoke_stage(
            run_root, "retention", "reread-failure", "pipeline/3_attestatores/run.py"
        ).returncode
        == 0
    )
    assert (
        _reread(run_root, "reread-failure", _act_id_for(tree, "a1"), "attestator_3").returncode == 0
    )
    before = {
        record["artifact_id"]: tree.resolve(
            tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"])
        ).read_bytes()
        for record in _testimonia(tree)
    }
    assert len(before) == 7

    resumed = invoke_stage(
        run_root, "retention", "reread-failure", "pipeline/3_attestatores/run.py"
    )

    assert resumed.returncode == 0, resumed.stderr
    after = {
        record["artifact_id"]: tree.resolve(
            tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"])
        ).read_bytes()
        for record in _testimonia(tree)
    }
    assert after == before
    assert attestatores.attempt_tally(tree)["state"] == "KNOWN"


def test_a_successful_reread_retains_new_testimony_and_keeps_attempt_one(tmp_path):
    """A reread that succeeds carries its own ordinal's declared response, not
    attempt 1's, and leaves attempt 1 byte-identical."""
    run_root, tree = run_to_designator(tmp_path, "reread-success")
    initial = invoke_stage(
        run_root, "retention", "reread-success", "pipeline/3_attestatores/run.py"
    )
    assert initial.returncode == 0, initial.stderr
    first = _testimonium_for(tree, act_key="a2", chair="attestator_1", ordinal=1)
    first_path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", first["artifact_id"]))
    first_bytes = first_path.read_bytes()

    result = _reread(run_root, "reread-success", first["subject_id"], "attestator_1")

    assert result.returncode == 0, result.stderr
    second = _testimonium_for(tree, act_key="a2", chair="attestator_1", ordinal=2)
    assert second["outcome"] == "read"
    assert second["payload"]["payload"].endswith(", reread")
    assert second["payload"]["reported"] == second["payload"]["payload"]
    assert second["payload"]["payload"] != first["payload"]["payload"]
    assert first_path.read_bytes() == first_bytes


def test_a_whole_pass_may_not_skip_an_ordinal_over_any_seat(tmp_path):
    """The other half of the same bound: `current + 2` would leave a hole where an
    attempt that existed is no longer here, which is what a gap always means."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )
    before = len(_testimonia(tree))

    skipped = invoke_stage(
        run_root,
        "retention",
        "happy",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=3,
    )

    assert skipped.returncode == 3
    assert "UNKNOWN" in skipped.stderr
    assert len(_testimonia(tree)) == before


def test_a_targeted_reread_names_both_the_act_and_the_chair_or_is_refused(tmp_path):
    """Without both, it is a whole second pass wearing a narrower name."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )
    act_id = _act_id_for(tree, "a1")
    before = len(_testimonia(tree))

    missing_chair = invoke_stage(
        run_root,
        "retention",
        "happy",
        "pipeline/3_attestatores/run.py",
        operation="reread",
        act=act_id,
    )
    assert missing_chair.returncode == 2
    assert "names the one act and the one chair" in missing_chair.stderr

    unknown_act = _reread(run_root, "happy", "act_not_in_this_run", "attestator_1")
    assert unknown_act.returncode == 2
    assert "proposal seal does not" in unknown_act.stderr

    unsealed_chair = _reread(run_root, "happy", act_id, "attestator_9")
    assert unsealed_chair.returncode == 2
    assert "this run is not sealed with" in unsealed_chair.stderr

    assert len(_testimonia(tree)) == before, "a refused reread writes nothing"


def test_a_targeted_reread_of_a_held_act_is_refused(tmp_path):
    """`refused-page` holds a2: its continuation page never sealed, so no witness
    was ever shown a reading there. There is nothing to read a second time."""
    run_root, tree = run_to_designator(tmp_path, "refused-page")
    assert (
        invoke_stage(
            run_root, "retention", "refused-page", "pipeline/3_attestatores/run.py"
        ).returncode
        == 0
    )
    held = _testimonium_for(tree, act_key="a2", chair="attestator_1", ordinal=1)
    assert held["outcome"] == "not-run"

    result = _reread(run_root, "refused-page", held["subject_id"], "attestator_1")

    assert result.returncode == 2
    assert "is held" in result.stderr


def test_a_targeted_reread_of_an_absent_chair_is_refused(tmp_path):
    """A dead chair is dead. Asking it again is not a second attempt, and inventing
    one would put a serving moment on a chair that never served."""
    models_config = absent_third_chair_config(tmp_path)
    run_root, tree = run_to_designator(tmp_path, "happy", models_config=models_config)
    assert (
        invoke_stage(
            run_root,
            "retention",
            "happy",
            "pipeline/3_attestatores/run.py",
            models_config=models_config,
        ).returncode
        == 0
    )
    dead = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    assert dead["outcome"] == "dead"
    before = len(_testimonia(tree))

    result = _reread(
        run_root,
        "happy",
        dead["subject_id"],
        "attestator_3",
        models_config=models_config,
    )

    assert result.returncode == 2
    assert "explicitly absent" in result.stderr
    assert len(_testimonia(tree)) == before


def test_a_reread_that_gets_nothing_back_is_failed_rather_than_never_attempted(tmp_path):
    """Spec 07 separates `failed` — "an attempt was made and produced no usable
    Testimonium" — from `not-run`, "configured, never attempted". A targeted reread
    names one chair on one act, so the invocation is the attempt: no response to it
    is a reading that failed, and filing it as never-attempted would put the reread
    in the unresolved class and report "chair with no outcome yet" over a chair that
    was asked and answered nothing."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )
    act_id = _act_id_for(tree, "a1")

    assert _reread(run_root, "happy", act_id, "attestator_3").returncode == 0

    second = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=2)
    assert second["outcome"] == "failed"
    assert second["payload"]["reason"]
    assert (
        _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)["outcome"] == "read"
    )


def test_an_operation_this_stage_does_not_implement_is_refused(tmp_path):
    """`--operation` carries no argparse `choices` — the same parser serves every
    stage — so an unrecognized one fell through to the whole pass. A mistyped
    reread therefore re-read nothing, ignored the act and chair it was handed, and
    exited 0 over a witness that was never asked again."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )
    before = len(_testimonia(tree))

    result = invoke_stage(
        run_root,
        "retention",
        "happy",
        "pipeline/3_attestatores/run.py",
        operation="reraed",
        act=_act_id_for(tree, "a1"),
        chair="attestator_3",
    )

    assert result.returncode == 2
    assert "has no 'reraed' operation" in result.stderr
    assert len(_testimonia(tree)) == before


def test_neither_write_path_accepts_the_other_path_s_arguments(tmp_path):
    """Each ignored argument was an instruction the operator gave and the stage did
    not carry out: a reread honours its own history's next ordinal, so an
    `--attempt-ordinal` beside it was silently discarded, and a whole pass reads
    every chair regardless of the one it was told to narrow to."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )
    act_id = _act_id_for(tree, "a1")
    before = len(_testimonia(tree))

    overridden = _reread(run_root, "happy", act_id, "attestator_3", attempt_ordinal=9)
    assert overridden.returncode == 2
    assert "names a different attempt" in overridden.stderr

    narrowed = invoke_stage(
        run_root,
        "retention",
        "happy",
        "pipeline/3_attestatores/run.py",
        act=act_id,
        chair="attestator_3",
    )
    assert narrowed.returncode == 2
    assert "cannot narrow to them" in narrowed.stderr

    assert len(_testimonia(tree)) == before


def test_a_pass_interrupted_before_its_manifest_was_written_can_still_be_completed(tmp_path):
    """A pass killed part way through leaves attempts on disk and no stored tally.

    The tally staying UNKNOWN until someone re-derives it is spec 07 test 5 and it
    is kept. What is not kept is the second gate behind it: the act/chair
    denominator was demanded *before* the pass, so the pass that would have
    supplied the missing pairs was refused for their being missing, and the folder
    could never be finished at all — only abandoned and re-run under a new run id,
    taking every earlier stage's work with it. The denominator is a closing check,
    and it still holds the folder after the pass if it does not reconcile.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )
    manifest = tree.resolve(tree.manifest_path(ATTESTATORES))
    manifest.unlink()
    for record in _testimonia(tree)[:2]:
        tree.resolve(
            tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"])
        ).unlink()

    held = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert held.returncode == 3, "an absent stored tally still holds, before anything else"
    assert "UNKNOWN" in held.stderr

    tree.write_manifest(ATTESTATORES)
    assert len(_testimonia(tree)) == 4

    resumed = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")

    assert resumed.returncode == 0, resumed.stderr
    assert len(_testimonia(tree)) == 6
    assert all(record["payload"]["attempt_ordinal"] == 1 for record in _testimonia(tree))
    assert attestatores.attempt_tally(tree)["state"] == "KNOWN"


# --- `witness_reported`: kept, and demoted ---------------------------------------


def test_a_self_report_is_retained_verbatim_whatever_its_format_can_express(tmp_path):
    """Spec 07's reason for `format_capabilities`, exercised on both sides at once.

    Chair 1's output format cannot express uncertainty at all and claims high
    confidence anyway; chair 2's can, and reports genuine doubt. Both claims are
    retained exactly as the witness made them, and neither reaches the outcome or
    `content_health` — a witness that cannot say "unsure" must not be read as
    confident merely for having said something.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )

    cannot_say_unsure = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    assert cannot_say_unsure["payload"]["format_capabilities"]["can_express_uncertainty"] is False
    assert cannot_say_unsure["payload"]["witness_reported"] == {"confidence": "high"}
    assert cannot_say_unsure["outcome"] == "read"

    can_say_unsure = _testimonium_for(tree, act_key="a1", chair="attestator_2", ordinal=1)
    assert can_say_unsure["payload"]["format_capabilities"]["can_express_uncertainty"] is True
    assert can_say_unsure["payload"]["witness_reported"] == {
        "confidence": "low",
        "note": "faded ink",
    }
    # The confident claim and the doubtful one produce the same outcome and the
    # same computed health shape. Nothing here grades a witness by what it says
    # about itself.
    assert can_say_unsure["outcome"] == cannot_say_unsure["outcome"]
    assert set(can_say_unsure["payload"]["content_health"]) == set(
        cannot_say_unsure["payload"]["content_health"]
    )

    silent = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    assert silent["payload"]["witness_reported"] is None, (
        "a witness that said nothing about itself has nothing invented for it"
    )


def test_no_self_report_can_reach_a_coverage_count_or_an_outcome_class():
    """Structural, not incidental: `witness_coverage` takes chair outcomes and a
    floor, and the outcome vocabulary is a closed set of strings. There is no
    parameter anywhere on that boundary through which a witness's claim about
    itself could reach a count or a class."""
    parameters = inspect.signature(witness_coverage).parameters
    assert set(parameters) == {"chair_outcomes", "configured_floor"}
    assert attestatores.content_health.__doc__ is not None
    assert "witness_reported" not in inspect.signature(attestatores.content_health).parameters


def test_conflicting_fixture_outcomes_are_refused_instead_of_selected():
    context = SimpleNamespace(
        scenario="conflict",
        fixture={
            "witness_failure": [{"scenario": "conflict", "act_key": "a1", "chair": "attestator_1"}],
            "witness_empty": [{"scenario": "conflict", "act_key": "a1", "chair": "attestator_1"}],
        },
    )

    with pytest.raises(SchemaRefusal, match="conflicting witness outcomes"):
        attestatores.declarations_for(context, 1)


def test_an_excluded_testimonium_without_an_approval_reference_is_refused_at_the_schema():
    """Spec 07 test 2: `excluded` exists only as a reference to a Tyrel
    approval-record artifact. Absent that artifact, a chair that did not read is
    `not-run`, `dead` or `failed` — all of which force visible partial status —
    and the word `excluded` alone buys nothing.

    The refusal is generic (`common/contracts/envelope.py` calls `require_approval`
    on the way out as well as on the way in), and it is pinned here because spec 07
    names it as this stage's test and a generic guarantee nobody exercises for a
    stage is one a later change can quietly stop covering.
    """
    envelope = dict(
        run_id="r",
        subject_id="act_0123456789abcdef",
        stage=ATTESTATORES,
        kind="testimonium",
        config_digest="0" * 64,
        adapter_revision="test",
        inputs=[],
        payload={"chair": "attestator_1"},
        attempt=attempt_id("act_0123456789abcdef", "read:attestator_1", 1),
    )
    envelope["artifact_id"] = artifact_id(
        ATTESTATORES, "testimonium", envelope["subject_id"], envelope["attempt"]
    )

    with pytest.raises(ApprovalRefusal, match="only Tyrel approves an exclusion"):
        build_envelope(outcome="excluded", **envelope)

    # The generic envelope accepts a non-empty, well-formed-looking identifier.
    # It does not resolve or verify approval-record bytes; this assertion pins
    # only the negative guarantee above and must not be read as proof that the
    # positive exclusion path required by Spec 07 exists.
    approved = build_envelope(outcome="excluded", approval_ref="art_0123456789abcdef", **envelope)
    assert approved["approval_ref"] == "art_0123456789abcdef"


def test_an_actual_testimonium_identity_refuses_replacement_at_the_store_boundary(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "happy")
    result = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == 0, result.stderr
    original = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", original["artifact_id"]))
    before = path.read_bytes()
    changed = copy.deepcopy(original)
    changed["payload"]["payload"] = "different native witness output"
    changed["self_hash"] = self_hash(changed)

    with pytest.raises(IncompatibleReuse, match="immutable"):
        tree.publish_artifact(changed)
    assert path.read_bytes() == before


def test_every_testimonium_outcome_uses_one_identity_bearing_writer():
    """A second constructor can drift without changing either constructor's tests."""
    module = ast.parse(inspect.getsource(attestatores))
    publishers = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "publish":
            continue
        kind = next((item.value for item in node.keywords if item.arg == "kind"), None)
        if isinstance(kind, ast.Constant) and kind.value == "testimonium":
            publishers.append(node.lineno)
    source_lines, start = inspect.getsourcelines(attestatores.publish_attempt)
    assert len(publishers) == 1
    assert start <= publishers[0] < start + len(source_lines)


def test_parseable_native_payload_and_self_report_remain_separate_in_a_real_artifact(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "structured-witness")
    result = invoke_stage(
        run_root,
        "retention",
        "structured-witness",
        "pipeline/3_attestatores/run.py",
    )
    assert result.returncode == 0, result.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    native = {"tokens": ["μ", "beta"], "layout": {"line": 4}, "uncertain": True}
    self_report = {"confidence": "certain", "note": "witness claim only"}
    assert record["payload"]["payload"] == native
    assert record["payload"]["witness_reported"] == self_report
    assert "reported" not in record["payload"]
    assert record["payload"]["content_health"] == attestatores.content_health(
        native, completed=True
    )
    _, _, _, changed_health, problem = attestatores.prepared_response(
        {"payload": native, "witness_reported": {"confidence": "unsure"}}
    )
    assert problem is None
    assert changed_health == record["payload"]["content_health"]


def test_unrecordable_native_output_becomes_failed_without_replacement_text():
    bad_native = "\ud800"
    native, self_report, capabilities, health, problem = attestatores.prepared_response(
        {"payload": bad_native}
    )
    assert native is None
    assert self_report is None
    assert capabilities is None
    assert health["recordable"] is False
    assert health["encoding"] == "invalid-or-unrecordable"
    assert "valid UTF-8" in problem
    record = attestatores.testimonium_payload(
        chair="attestator_1",
        act_key="a1",
        ordinal=1,
        regions=[],
        provenance={},
        format_capabilities=capabilities,
        native_payload=native,
        witness_reported=self_report,
        health=health,
        outcome="failed",
        reason=problem,
    )
    assert record["payload"] is None
    assert "reported" not in record
    assert "�" not in str(record)


def test_a_deeply_nested_native_payload_becomes_failed_not_a_recursion_crash():
    """A witness response nested past Python's recursion limit must become one
    isolated `failed` attempt, the same as any other malformed response -- never
    an uncaught RecursionError that would crash the whole stage process and take
    every other act and chair in the pass down with it (spec 07's isolation
    bullet: "one ... malformed response never kills the folder")."""
    nested = "leaf"
    for _ in range(5000):
        nested = [nested]

    problem = attestatores._native_problem(nested)
    assert problem is not None
    assert "nests deeper" in problem

    health = attestatores.content_health(nested, completed=True)
    assert health["recordable"] is False
    assert health["truncation_basis"] == problem

    native, self_report, capabilities, prepared_health, prepared_problem = (
        attestatores.prepared_response({"payload": nested})
    )
    assert native is None
    assert self_report is None
    assert capabilities is None
    assert prepared_health["recordable"] is False
    assert prepared_problem == problem

    # Reasonable real-world nesting must not be caught by the same guard.
    reasonable = {"tokens": ["a", "b"], "layout": {"line": 4, "spans": [{"a": 1}, {"b": 2}]}}
    assert attestatores._native_problem(reasonable) is None


def test_configured_never_attempted_seat_is_not_run_not_dead(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "not-run-witness")
    result = invoke_stage(
        run_root, "retention", "not-run-witness", "pipeline/3_attestatores/run.py"
    )
    assert result.returncode == 0, result.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    assert record["outcome"] == "not-run"
    assert record["inputs"] == []
    assert record["payload"]["regions"] == []
    assert record["payload"]["payload"] is None
    assert record["payload"]["provenance"]["chair_state"] == "configured"
    assert record["payload"]["provenance"]["receipt_ref"] is None


def test_one_refused_crop_records_its_chairs_and_leaves_the_other_act_intact(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "happy")
    entry = next(
        entry for entry in tree.build_manifest(DESIGNATOR)["artifacts"] if entry["kind"] == "region"
    )
    region = tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
    refused_key = region["payload"]["act_key"]
    intact_key = "a2" if refused_key == "a1" else "a1"
    tree.resolve(region["payload"]["image_path"]).write_bytes(b"broken crop bytes")

    result = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")

    assert result.returncode == 0, result.stderr
    records = _testimonia(tree)
    assert len(records) == 6
    refused = [record for record in records if record["payload"]["act_key"] == refused_key]
    intact = [record for record in records if record["payload"]["act_key"] == intact_key]
    assert len(refused) == len(intact) == 3
    assert all(record["outcome"] == "not-run" for record in refused)
    assert all(record["inputs"] == [] and record["payload"]["regions"] == [] for record in refused)
    assert all(record["outcome"] == "read" for record in intact)


def test_malformed_one_witness_response_is_a_failed_attempt_that_does_not_hold_the_folder(
    tmp_path,
):
    """Spec 07's isolation bullet: "one malformed response never kills the folder".

    The response is refused without repair — no replacement characters, no
    `str()`, no empty reading — and the refusal is one `failed` attempt with its
    reason. That attempt is countable and counted, so the tally stays KNOWN and
    the other two chairs' readings of the same act reach the Perlector. The act
    goes under-witnessed and the run goes visibly partial; that is where this is
    reported, and it is not a reason to stop reading ink nobody doubted.
    """
    run_root, tree = run_to_designator(tmp_path, "malformed-witness")
    result = invoke_stage(
        run_root, "retention", "malformed-witness", "pipeline/3_attestatores/run.py"
    )
    assert result.returncode == 0, result.stderr
    records = _testimonia(tree)
    assert len(records) == 6
    malformed = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    assert malformed["outcome"] == "failed"
    assert malformed["payload"]["payload"] is None
    assert malformed["payload"]["content_health"]["recordable"] is False
    assert malformed["payload"]["reason"]
    assert "reported" not in malformed["payload"]
    assert sum(record["outcome"] == "read" for record in records) == 5
    tally = attestatores.attempt_tally(tree)
    assert tally["state"] == "KNOWN"
    assert tally["count"] == 6
    assert tally["hold"] is False


def test_malformed_capabilities_fail_one_attempt_without_aborting_other_chairs(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "malformed-capabilities")
    result = invoke_stage(
        run_root,
        "retention",
        "malformed-capabilities",
        "pipeline/3_attestatores/run.py",
    )
    assert result.returncode == 0, result.stderr
    records = _testimonia(tree)
    assert len(records) == 6
    malformed = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    assert malformed["outcome"] == "failed"
    assert malformed["payload"]["payload"] == "SYNTHETIC ACT ONE alpha beta"
    assert malformed["payload"]["format_capabilities"] is None
    assert malformed["payload"]["content_health"]["recordable"] is True
    assert "format capabilities could not be retained" in malformed["payload"]["reason"]
    assert malformed["inputs"]
    assert malformed["payload"]["provenance"]["receipt_ref"] is not None
    assert sum(record["outcome"] == "read" for record in records) == 5
    assert attestatores.attempt_tally(tree)["state"] == "KNOWN"


def test_witness_metadata_cannot_rewrite_native_content_health():
    native = "verbatim native response"
    expected = attestatores.content_health(native, completed=True)

    _, _, capabilities, self_report_health, self_report_problem = attestatores.prepared_response(
        {"payload": native, "witness_reported": "\ud800"}
    )
    assert capabilities == attestatores.DEFAULT_FORMAT_CAPABILITIES
    assert self_report_health == expected
    assert "self-report could not be retained" in self_report_problem

    _, _, capabilities, capability_health, capability_problem = attestatores.prepared_response(
        {"payload": native, "format_capabilities": "not an object"}
    )
    assert capabilities is None
    assert capability_health == expected
    assert "format capabilities could not be retained" in capability_problem


def test_a_reading_that_claims_its_own_channel_was_unrecordable_is_unknown(tmp_path):
    """The one `recordable=False` shape that is #23's UNKNOWN rather than an
    accounted failure: a record claiming to be a *reading* while recording that
    nothing could retain what it read. A reading nothing could keep is not a
    reading, and it is the shape a resealed artifact would take to slip a lost
    payload past the tally as though it were counted evidence.

    The resealed health record here is otherwise exactly what this stage writes
    for an unrecordable channel, so the outcome is the only thing wrong with it:
    the tally has to refuse it for claiming to be a reading, not for some other
    damage that happens to be alongside."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    assert record["outcome"] == "read"
    changed = copy.deepcopy(record)
    changed["payload"]["payload"] = None
    changed["payload"].pop("reported")
    changed["payload"]["content_health"] = {
        "native_type": "unrecordable",
        "encoding": "invalid-or-unrecordable",
        "recordable": False,
        "empty": None,
        "blank": None,
        "truncated": None,
        "characters": None,
        "truncation_basis": "the provider body could not be retained",
    }
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    tally = attestatores.attempt_tally(tree)

    assert tally["state"] == "UNKNOWN"
    assert tally["count"] is None
    assert tally["hold"] is True
    assert "is not a reading" in tally["reason"]


def test_an_unrecordable_channel_may_not_assert_facts_nothing_measured(tmp_path):
    """`content_health` is the one field spec 07 requires to be "computed here,
    deterministically ... never self-reported". `recordable=False` used to be a
    door out of every other check in this validator, so a resealed record could
    take that branch and then claim a character count, a truncation state, a valid
    encoding and a full native payload — all of them uncomputable by definition,
    since the premise of the branch is that nothing could be kept. The tally
    counted such a record as KNOWN.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    fabricated = copy.deepcopy(record)
    fabricated["outcome"] = "failed"
    fabricated["payload"]["reason"] = "the provider response was refused without repair"
    fabricated["payload"].pop("reported")
    fabricated["payload"]["content_health"] = {
        "native_type": "string",
        "encoding": "utf-8-json-native",
        "recordable": False,
        "empty": False,
        "blank": False,
        "truncated": False,
        "characters": 9999,
        "truncation_basis": "trusted-response-boundary",
    }
    fabricated["self_hash"] = self_hash(fabricated)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(fabricated))
    tree.write_manifest(ATTESTATORES)

    tally = attestatores.attempt_tally(tree)

    assert tally["state"] == "UNKNOWN"
    assert tally["hold"] is True
    assert "retains a native payload" in tally["reason"]


def test_a_failed_attempt_with_an_unrecordable_channel_and_no_reason_is_unknown(tmp_path):
    """An absence with no reason is the silent loss this stage exists to refuse,
    so it cannot buy its way into the count by wearing `failed`."""
    run_root, tree = run_to_designator(tmp_path, "malformed-witness")
    assert (
        invoke_stage(
            run_root, "retention", "malformed-witness", "pipeline/3_attestatores/run.py"
        ).returncode
        == 0
    )
    record = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    changed = copy.deepcopy(record)
    del changed["payload"]["reason"]
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    tally = attestatores.attempt_tally(tree)

    assert tally["state"] == "UNKNOWN"
    assert tally["hold"] is True
    assert "records no reason" in tally["reason"]


def test_a_failed_attempt_with_recordable_native_evidence_still_requires_a_reason(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "malformed-capabilities")
    initial = invoke_stage(
        run_root,
        "retention",
        "malformed-capabilities",
        "pipeline/3_attestatores/run.py",
    )
    assert initial.returncode == 0, initial.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    assert record["payload"]["content_health"]["recordable"] is True
    changed = copy.deepcopy(record)
    del changed["payload"]["reason"]
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    retry = invoke_stage(
        run_root,
        "retention",
        "malformed-capabilities",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )

    assert retry.returncode == 3
    assert "records no reason" in retry.stderr
    assert all(item["payload"]["attempt_ordinal"] == 1 for item in _testimonia(tree))


@pytest.mark.parametrize(
    ("scenario", "replacement", "message"),
    (
        ("happy", "different text", "differs from its verbatim native payload"),
        ("structured-witness", "coerced text", "carries a compatibility projection"),
    ),
)
def test_a_resealed_compatibility_projection_cannot_change_native_testimony(
    tmp_path, scenario, replacement, message
):
    run_root, tree = run_to_designator(tmp_path, scenario)
    initial = invoke_stage(run_root, "retention", scenario, "pipeline/3_attestatores/run.py")
    assert initial.returncode == 0, initial.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    changed = copy.deepcopy(record)
    changed["payload"]["reported"] = replacement
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    retry = invoke_stage(
        run_root,
        "retention",
        scenario,
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )

    assert retry.returncode == 3
    assert message in retry.stderr
    assert all(item["payload"]["attempt_ordinal"] == 1 for item in _testimonia(tree))


@pytest.mark.parametrize("damage", ("absent", "garbled", "truncated", "recursion"))
def test_damaged_attempt_tally_is_unknown_and_refuses_to_add_a_replacement(tmp_path, damage):
    """`recursion` pins the gap two blind audits both found: `attempt_tally`
    reads the stored manifest through its own bare `json.loads`, the one JSON
    reader in this stage that `common/runtree/store.py::_read_json`'s
    `RecursionError` guard does not cover. A stored manifest replaced with deep
    enough nesting used to escape as an uncaught traceback (exit 1) instead of
    the UNKNOWN + hold #23 promises."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    initial = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert initial.returncode == 0, initial.stderr
    manifest_path = tree.resolve(tree.manifest_path(ATTESTATORES))
    original = manifest_path.read_bytes()
    if damage == "absent":
        manifest_path.unlink()
    elif damage == "garbled":
        manifest_path.write_bytes(b"{")
    elif damage == "recursion":
        nesting = 30_000
        manifest_path.write_bytes(
            f'{{"deep": {"[" * nesting}"leaf"{"]" * nesting}}}'.encode()
        )
    else:
        manifest_path.write_bytes(original[:-1])

    tally = attestatores.attempt_tally(tree)
    assert tally["state"] == "UNKNOWN"
    assert tally["count"] is None
    assert tally["hold"] is True
    retry = invoke_stage(
        run_root,
        "retention",
        "happy",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )
    assert retry.returncode == 3
    assert "UNKNOWN" in retry.stderr
    assert all(record["payload"]["attempt_ordinal"] == 1 for record in _testimonia(tree))


def test_resealed_missing_testimonium_provenance_makes_the_next_tally_unknown(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "happy")
    initial = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert initial.returncode == 0, initial.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    changed = copy.deepcopy(record)
    del changed["payload"]["provenance"]
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    tally = attestatores.attempt_tally(tree)
    assert tally["state"] == "UNKNOWN"
    assert tally["count"] is None
    assert tally["hold"] is True

    retry = invoke_stage(
        run_root,
        "retention",
        "happy",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )

    assert retry.returncode == 3
    assert "UNKNOWN" in retry.stderr
    assert all(record["payload"]["attempt_ordinal"] == 1 for record in _testimonia(tree))


def test_resealed_malformed_content_health_makes_the_tally_unknown(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "happy")
    initial = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert initial.returncode == 0, initial.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    changed = copy.deepcopy(record)
    changed["payload"]["content_health"]["truncated"] = "not-a-truncation-state"
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    tally = attestatores.attempt_tally(tree)

    assert tally["state"] == "UNKNOWN"
    assert tally["count"] is None
    assert tally["hold"] is True
