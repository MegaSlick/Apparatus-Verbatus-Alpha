"""Spec 07 retention and native-Testimonium tests over the real stage program."""

import ast
import copy
import importlib.util
import inspect
import json
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
    first = _testimonium_for(tree, act_key="a1", chair="attestator_2", ordinal=1)
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
        if record["payload"]["act_key"] == "a1" and record["payload"]["chair"] == "attestator_2"
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
        if record["payload"]["chair"] == "attestator_2"
    )
    assert current["payload"]["attempt_ordinal"] == 2
    assert current["outcome"] == "failed"
    assert attestatores.attempt_tally(tree)["count"] == 12


def test_a_whole_pass_resolves_designator_inputs_once_per_act_not_once_per_chair(
    tmp_path, monkeypatch
):
    """Preflight and publication share regions and exact resolved attempts.

    The append/collision history is indexed by one manifest walk, while the
    closing tally remains an independent rebuild.

    A per-pass page-fallback bounds index used to be counted here too. It fed
    the identity branch that minted `genuinely-empty` for a fallback act's
    chairs without asking anything (Sol-S1); the branch and the index went
    together, and this stage no longer reads the Designator's `page-fallback`
    records at all.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    region_calls: list[str] = []
    attempt_calls = 0
    attestatores_manifest_calls = 0
    real_proposed_regions = attestatores.proposed_regions
    real_resolve_attempt = attestatores.resolve_attempt
    real_build_manifest = RunTree.build_manifest

    def counted_regions(context, act_id):
        region_calls.append(act_id)
        return real_proposed_regions(context, act_id)

    def counted_attempt(*args, **kwargs):
        nonlocal attempt_calls
        attempt_calls += 1
        return real_resolve_attempt(*args, **kwargs)

    def counted_manifest(self, stage, **kwargs):
        nonlocal attestatores_manifest_calls
        if self.root == tree.root and stage == ATTESTATORES:
            attestatores_manifest_calls += 1
        return real_build_manifest(self, stage, **kwargs)

    monkeypatch.setattr(attestatores, "proposed_regions", counted_regions)
    monkeypatch.setattr(attestatores, "resolve_attempt", counted_attempt)
    monkeypatch.setattr(RunTree, "build_manifest", counted_manifest)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--run-root",
            str(run_root),
            "--run-id",
            "retention",
            "--scenario",
            "happy",
            "--fixture-root",
            str(ROOT / "proof"),
        ],
    )

    assert attestatores.main() == 0
    # Exactly six scans are required: history index, prior-seal deletion check,
    # the pre-close manifest the tally reconciles, the independent tally, the
    # post-environment seal inventory, and the final manifest. The tally must run
    # before the completion seal, while the seal must still bind environment bytes.
    assert attestatores_manifest_calls == 6
    assert attempt_calls == 6  # one per act/chair, never re-resolved for publication

    act_ids = {record["subject_id"] for record in _testimonia(tree)}
    assert len(act_ids) == 2
    assert {act_id: region_calls.count(act_id) for act_id in act_ids} == {
        act_id: 2 for act_id in act_ids
    }


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
    first = _testimonium_for(tree, act_key="a1", chair="attestator_2", ordinal=1)
    first_path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", first["artifact_id"]))
    first_bytes = first_path.read_bytes()
    untouched = {
        record["artifact_id"]: tree.resolve(
            tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"])
        ).read_bytes()
        for record in _testimonia(tree)
    }

    result = _reread(run_root, "reread-failure", _act_id_for(tree, "a1"), "attestator_2")
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
        if record["payload"]["act_key"] == "a1" and record["payload"]["chair"] == "attestator_2"
    ]
    assert sorted(record["payload"]["attempt_ordinal"] for record in moved) == [1, 2]
    others = [record for record in records if record not in moved]
    assert others and all(record["payload"]["attempt_ordinal"] == 1 for record in others), (
        "a reread of one chair must not append an attempt for any other chair"
    )

    current = next(
        record
        for record in latest_per_chair(moved, "reread Testimonia")
        if record["payload"]["chair"] == "attestator_2"
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
        _reread(run_root, "reread-failure", _act_id_for(tree, "a1"), "attestator_2").returncode == 0
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


def test_a_whole_pass_at_an_ordinal_a_reread_already_sealed_differently_is_refused_before_any_write(
    tmp_path,
):
    """audit-d's F2: a targeted reread and a whole pass can reach the very same
    (act, chair, ordinal) identity with a different honest outcome —
    `resolve_attempt`'s own docstring says silence means `failed` under a reread
    and `not-run` under a whole pass. Before the fix, a whole pass at that ordinal
    wrote every pair up to the collision, then hit `IncompatibleReuse` reactively
    and exited fatal (2) with a half-written attempt layer and a stale stored
    manifest — from that point on, no whole pass at any ordinal could complete in
    that folder again, not even a resume at ordinal 1. The preflight now catches
    this before any pair is published, so the pass holds cleanly (3) and writes
    nothing, and the folder is not stranded: a resume at ordinal 1 still works."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )
    before = {
        record["artifact_id"]: tree.resolve(
            tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"])
        ).read_bytes()
        for record in _testimonia(tree)
    }

    reread = _reread(run_root, "happy", _act_id_for(tree, "a1"), "attestator_2")
    assert reread.returncode == 0, reread.stderr

    colliding = invoke_stage(
        run_root, "retention", "happy", "pipeline/3_attestatores/run.py", attempt_ordinal=2
    )

    assert colliding.returncode == 3
    assert "would record a different attempt" in colliding.stderr
    # Nothing from this refused pass was written: the reread's own record is the
    # only ordinal-2 artifact, and every ordinal-1 artifact is still exactly what
    # it was.
    after_refusal = {
        record["artifact_id"]: tree.resolve(
            tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"])
        ).read_bytes()
        for record in _testimonia(tree)
        if record["payload"]["attempt_ordinal"] == 1
    }
    assert after_refusal == before
    assert sum(1 for r in _testimonia(tree) if r["payload"]["attempt_ordinal"] == 2) == 1

    # The folder is not stranded: a resume at ordinal 1 still completes cleanly.
    resumed = invoke_stage(
        run_root, "retention", "happy", "pipeline/3_attestatores/run.py", attempt_ordinal=1
    )
    assert resumed.returncode == 0, resumed.stderr
    assert attestatores.attempt_tally(tree)["state"] == "KNOWN"


def test_a_successful_reread_retains_new_testimony_and_keeps_attempt_one(tmp_path):
    """A reread that succeeds carries its own ordinal's declared response, not
    attempt 1's, and leaves attempt 1 byte-identical."""
    run_root, tree = run_to_designator(tmp_path, "reread-success")
    initial = invoke_stage(
        run_root, "retention", "reread-success", "pipeline/3_attestatores/run.py"
    )
    assert initial.returncode == 0, initial.stderr
    first = _testimonium_for(tree, act_key="a2", chair="attestator_2", ordinal=1)
    first_path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", first["artifact_id"]))
    first_bytes = first_path.read_bytes()

    result = _reread(run_root, "reread-success", first["subject_id"], "attestator_2")

    assert result.returncode == 0, result.stderr
    second = _testimonium_for(tree, act_key="a2", chair="attestator_2", ordinal=2)
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
    assert "neither a rerun of an attempt it holds" in skipped.stderr
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


def test_a_targeted_reread_of_an_absent_chair_is_refused(tmp_path, absent_third_chair_config):
    """A dead chair is dead. Asking it again is not a second attempt, and inventing
    one would put a serving moment on a chair that never served."""
    models_config = absent_third_chair_config
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

    assert _reread(run_root, "happy", act_id, "attestator_2").returncode == 0

    second = _testimonium_for(tree, act_key="a1", chair="attestator_2", ordinal=2)
    assert second["outcome"] == "failed"
    assert second["payload"]["reason"]
    assert (
        _testimonium_for(tree, act_key="a1", chair="attestator_2", ordinal=1)["outcome"] == "read"
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

    overridden = _reread(run_root, "happy", act_id, "attestator_2", attempt_ordinal=9)
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


def test_a_wiped_attempt_layer_holds_rather_than_silently_restarting_history(tmp_path):
    """The stored inventory is evidence that attempts existed, even with none left.

    Losing *some* of a folder's attempts holds it: the stored manifest no longer
    equals the rebuilt one, `attempt_tally` says UNKNOWN, and nothing is written
    until someone re-derives the inventory deliberately. Losing *all* of them did
    not, because the check that would have noticed was reached only when the walk
    found at least one artifact still on disk — so a folder whose whole Testimonium
    layer was gone took the first-run path instead, wrote attempt 1 for every pair,
    rewrote the inventory over the one that said otherwise, and exited 0.

    A reread is what makes the loss material rather than merely re-derivable: the
    fixture's chairs are deterministic, so the ordinal-1 attempts come back
    byte-identical, but the ordinal-2 attempt a reread appended does not come back
    at all. This folder's own manifest recorded seven sealed attempts; the silent
    restart left six and said nothing. GOVERNANCE 2 and GOVERNANCE 4 both refuse
    that, and the inventory needed to notice it was on disk the whole time.
    """
    run_root, tree = run_to_designator(tmp_path, "reread-failure")
    assert (
        invoke_stage(
            run_root, "retention", "reread-failure", "pipeline/3_attestatores/run.py"
        ).returncode
        == 0
    )
    assert (
        _reread(run_root, "reread-failure", _act_id_for(tree, "a1"), "attestator_2").returncode == 0
    )
    manifest = tree.resolve(tree.manifest_path(ATTESTATORES))
    stored = json.loads(manifest.read_text(encoding="utf-8"))
    # R0 also retains page-scoped Testimonia and derived act attachments; the
    # history invariant here is specifically the seven act-scoped attempts.
    assert len([entry for entry in stored["artifacts"] if entry["kind"] == "testimonium"]) == 7
    assert any(record["payload"]["attempt_ordinal"] == 2 for record in _testimonia(tree))

    shutil.rmtree(tree.resolve("3_attestatores/artifacts"))

    wiped = invoke_stage(run_root, "retention", "reread-failure", "pipeline/3_attestatores/run.py")

    assert wiped.returncode == 3, wiped.stderr
    assert "UNKNOWN" in wiped.stderr
    assert _testimonia(tree) == [], "a held pass writes no attempt over a damaged inventory"
    assert json.loads(manifest.read_text(encoding="utf-8")) == stored, (
        "the inventory that recorded the lost attempts must survive the refusal"
    )


def test_a_resealed_testimonium_may_not_rebind_the_regions_its_witness_saw(tmp_path):
    """The tally's own region binding, which nothing else in this suite exercised.

    `pipeline/4_perlector/run.py::validate_testimonium_regions` makes the same
    comparison at the consumer, but this stage checks it again before authorizing
    another immutable append — a resealed Testimonium that renames the crop its
    witness saw must not be counted as known evidence by the producer that would
    then append beside it. Removing the refusal in `validate_tallied_testimonium`
    left this whole file green, which is what this test closes.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    initial = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert initial.returncode == 0, initial.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    changed = copy.deepcopy(record)
    assert changed["payload"]["regions"], "the happy reading must bind a proposal region"
    changed["payload"]["regions"][0]["region_id"] = "rgn_0123456789abcdef"
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    tally = attestatores.attempt_tally(
        tree,
        context=None,
        acts=None,
    )
    assert tally["state"] == "KNOWN", "the context-free tally deliberately checks no regions"

    retry = invoke_stage(
        run_root,
        "retention",
        "happy",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )

    assert retry.returncode == 3
    assert "does not bind exactly the proposal regions and inputs" in retry.stderr
    assert all(item["payload"]["attempt_ordinal"] == 1 for item in _testimonia(tree))


def test_a_reread_holds_when_the_folder_no_longer_accounts_for_every_pair(tmp_path):
    """The closing act/chair denominator, which nothing exercised.

    A whole pass fills its own denominator, so the closing check can only be seen
    through the one write path that cannot: a targeted reread moves exactly the
    chair it names and leaves a pair that is missing entirely still missing. The
    tally then refuses to call the folder known, the reread's own append is
    retained, and the run holds instead of reporting a complete evidence layer
    over a chair whose testimony is not there.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )
    for record in _testimonia(tree):
        if record["payload"]["act_key"] == "a2" and record["payload"]["chair"] == "attestator_2":
            tree.resolve(
                tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"])
            ).unlink()
    # Re-derived deliberately, so the divergent-inventory refusal is not what fires
    # here and the denominator check is the only thing left that can.
    tree.write_manifest(ATTESTATORES)
    assert len(_testimonia(tree)) == 5

    result = _reread(run_root, "happy", _act_id_for(tree, "a1"), "attestator_2")

    assert result.returncode == 3, result.stderr
    assert "does not account for every expected act/chair pair" in result.stderr
    assert len(_testimonia(tree)) == 6, "the reread's own attempt is retained by the hold"


@pytest.mark.parametrize(
    ("chair", "field", "value", "message"),
    (
        ("attestator_1", "act_key", "a9", "disagrees with its act key"),
        ("attestator_1", "chair", "attestator_9", "names no configured chair"),
        (
            "attestator_1",
            "format_capabilities",
            None,
            "non-failed Testimonium carries no format_capabilities record",
        ),
        (
            "attestator_3",
            "regions",
            [
                {
                    "region_id": "rgn_0123456789abcdef",
                    "image_path": "2_designator/blobs/sha256/" + "0" * 64,
                    "image_sha256": "0" * 64,
                }
            ],
            "non-attempted Testimonium tally record carries regions",
        ),
    ),
)
def test_a_resealed_testimonium_the_writer_could_not_have_produced_is_unknown(
    tmp_path, chair, field, value, message
):
    """`validate_tallied_testimonium`'s per-record refusals, exercised rather than
    assumed. Each of these passes the envelope, the self-hash and this stage's
    content-health schema, and each is a record this writer could not have
    produced — the tally must not authorize another immutable append beside one.
    Removing any of these four refusals left the whole file green.
    """
    run_root, tree = run_to_designator(tmp_path, "not-run-witness")
    initial = invoke_stage(
        run_root, "retention", "not-run-witness", "pipeline/3_attestatores/run.py"
    )
    assert initial.returncode == 0, initial.stderr
    record = _testimonium_for(tree, act_key="a1", chair=chair, ordinal=1)
    changed = copy.deepcopy(record)
    changed["payload"][field] = value
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    retry = invoke_stage(
        run_root,
        "retention",
        "not-run-witness",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )

    assert retry.returncode == 3, retry.stderr
    assert message in retry.stderr
    assert all(item["payload"]["attempt_ordinal"] == 1 for item in _testimonia(tree))


def test_a_resealed_dead_outcome_over_a_configured_chair_is_unknown(tmp_path):
    """`dead` is the one outcome that must retain an absent chair, and the check
    that says so had no test: a `not-run` record resealed as `dead` keeps a valid
    configured provenance and passes every generic check on the way in."""
    run_root, tree = run_to_designator(tmp_path, "not-run-witness")
    initial = invoke_stage(
        run_root, "retention", "not-run-witness", "pipeline/3_attestatores/run.py"
    )
    assert initial.returncode == 0, initial.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    assert record["outcome"] == "not-run"
    changed = copy.deepcopy(record)
    changed["outcome"] = "dead"
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    retry = invoke_stage(
        run_root,
        "retention",
        "not-run-witness",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )

    assert retry.returncode == 3, retry.stderr
    assert "does not retain an absent chair" in retry.stderr
    assert all(item["payload"]["attempt_ordinal"] == 1 for item in _testimonia(tree))


# --- `witness_reported`: kept, and demoted ---------------------------------------


def test_a_self_report_is_retained_verbatim_whatever_its_format_can_express(tmp_path):
    """Spec 07's reason for `format_capabilities`, exercised on both sides at once.

    Chair 1's output format cannot express uncertainty at all and claims high
    confidence anyway; chair 2's can, and reports genuine doubt. Both claims are
    retained exactly as the witness made them, and neither reaches the outcome or
    `content_health` — a witness that cannot say "unsure" must not be read as
    confident merely for having said something.
    """
    run_root, tree = run_to_designator(tmp_path, "witness-capabilities")
    assert (
        invoke_stage(
            run_root,
            "retention",
            "witness-capabilities",
            "pipeline/3_attestatores/run.py",
        ).returncode
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
    assert set(parameters) == {"chair_outcomes", "configured_floor", "attachments"}
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


@pytest.mark.parametrize(
    "fixture_key",
    ("witness_failure", "witness_empty", "witness_not_run", "witness_malformed"),
)
def test_a_witness_declaration_without_a_scenario_names_its_table_and_row(fixture_key):
    row = {"act_key": "a1", "chair": "attestator_1"}
    if fixture_key == "witness_malformed":
        row["reason"] = "provider body was malformed"
    context = SimpleNamespace(scenario="happy", fixture={fixture_key: [row]})

    with pytest.raises(
        SchemaRefusal,
        match=rf"fixture \[\[{fixture_key}\]\] row 1 has no scenario",
    ):
        attestatores.declarations_for(context, 1)


def test_scenario_specific_testimony_overrides_scenario_agnostic_testimony():
    shared = {
        "act_key": "a1",
        "chair": "attestator_1",
        "payload": "shared response",
    }
    scenario_specific = {
        "scenario": "happy",
        "act_key": "a1",
        "chair": "attestator_1",
        "payload": "happy response",
    }
    context = SimpleNamespace(
        scenario="happy",
        fixture={"testimony": [shared, scenario_specific]},
    )

    assert attestatores.testimony_for(context, "a1", "attestator_1", 1) is scenario_specific


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


@pytest.mark.parametrize(
    ("native", "expected_type", "where"),
    ((1.5, "float", "payload"), ({"score": 0.5}, "object", "payload.score")),
)
def test_a_float_in_a_native_payload_is_a_failed_attempt_not_a_coerced_number(
    native, expected_type, where
):
    """HANDOFF names this gap; nothing exercised it.

    The shared canonical writer refuses floating-point numbers, so a witness
    returning one — bare, or buried in an otherwise ordinary object — cannot be
    retained verbatim. What this stage owes that response is a `failed` attempt
    that says so, never a rounded, stringified or dropped number quietly wearing
    `read`. The fixture builder cannot declare a float (`toml_value` refuses one),
    so this is the boundary at which the claim can be checked at all.
    """
    payload, self_report, capabilities, health, problem = attestatores.prepared_response(
        {"payload": native}
    )

    assert payload is None
    assert self_report is None
    assert capabilities is None
    assert health["recordable"] is False
    assert health["native_type"] == expected_type
    assert problem == f"{where} has unsupported native type 'float'"
    # And the record such an attempt lands in is a coherent one: the tally's own
    # validator has to accept this exact shape, or the refusal would itself be
    # unaccountable evidence.
    attestatores.validate_content_health(payload, health)


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


def test_a_crop_broken_after_the_designator_sealed_it_stops_at_that_boundary(tmp_path):
    """Crop bytes that moved after the boundary are tree damage, not a bad crop.

    The Designator's completion seal reads every blob's content back off disk,
    so this is refused before the Attestatores reads an act. The retention
    property the test below proves is a different claim about a different tree
    state, and both are worth holding: this one says the pipeline notices, that
    one says it degrades per act when the crop was bad all along.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    entry = next(
        entry for entry in tree.build_manifest(DESIGNATOR)["artifacts"] if entry["kind"] == "region"
    )
    region = tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
    tree.resolve(region["payload"]["image_path"]).write_bytes(b"broken crop bytes")

    result = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")

    assert result.returncode == 2, result.stderr
    assert "designator stage-seal" in result.stderr
    assert "named inventory no longer matches disk" in result.stderr
    assert _testimonia(tree) == []


def test_one_refused_crop_records_its_chairs_and_leaves_the_other_act_intact(
    tmp_path, rebind_stage_seal
):
    """Spec 07's isolation rule: one unreadable crop never kills the folder.

    The Designator seals the crop it actually wrote, so a crop a real detector
    cut badly reaches the Attestatores *inside* a coherent boundary. The rebind
    models that state; without it the test exercises the corruption refusal above.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    entry = next(
        entry for entry in tree.build_manifest(DESIGNATOR)["artifacts"] if entry["kind"] == "region"
    )
    region = tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
    refused_key = region["payload"]["act_key"]
    intact_key = "a2" if refused_key == "a1" else "a1"
    tree.resolve(region["payload"]["image_path"]).write_bytes(b"broken crop bytes")
    rebind_stage_seal(tree, DESIGNATOR)

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


def test_combined_unrecordable_witness_metadata_fails_one_attempt_without_crashing(
    tmp_path, monkeypatch
):
    """Both metadata defects are retained in one failed attempt's reason.

    The row is injected at the fixture-reader seam so the real preflight,
    canonical writer, manifest, and tally all run without adding a pin-moving
    scenario to the sealed fixture.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    real_testimony_for = attestatores.testimony_for

    def combined_testimony(context, act_key, chair, ordinal):
        if (act_key, chair, ordinal) == ("a1", "attestator_3", 1):
            return {
                "payload": "SYNTHETIC ACT ONE alpha beta",
                "format_capabilities": "not an object",
                "witness_reported": "\ud800",
            }
        return real_testimony_for(context, act_key, chair, ordinal)

    monkeypatch.setattr(attestatores, "testimony_for", combined_testimony)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--run-root",
            str(run_root),
            "--run-id",
            "retention",
            "--scenario",
            "happy",
            "--fixture-root",
            str(ROOT / "proof"),
        ],
    )

    assert attestatores.main() == 0
    records = _testimonia(tree)
    assert len(records) == 6
    malformed = _testimonium_for(tree, act_key="a1", chair="attestator_3", ordinal=1)
    assert malformed["outcome"] == "failed"
    assert malformed["payload"]["payload"] == "SYNTHETIC ACT ONE alpha beta"
    assert malformed["payload"]["format_capabilities"] is None
    assert malformed["payload"]["witness_reported"] is None
    reason = malformed["payload"]["reason"]
    assert "format capabilities could not be retained" in reason
    assert "self-report could not be retained" in reason
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
        deep_text = f'{{"deep": {"[" * nesting}"leaf"{"]" * nesting}}}'
        try:
            json.loads(deep_text)
        except RecursionError:
            pass
        else:
            pytest.skip(
                f"this interpreter's JSON scanner absorbs {nesting} levels, so the "
                "guarded path is unreachable here and this case proves nothing"
            )
        manifest_path.write_bytes(deep_text.encode())
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


def test_resealed_content_health_with_an_unknown_field_makes_the_tally_unknown(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "happy")
    initial = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert initial.returncode == 0, initial.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    changed = copy.deepcopy(record)
    changed["payload"]["content_health"]["confidence"] = 0
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    tally = attestatores.attempt_tally(tree)

    assert tally["state"] == "UNKNOWN"
    assert tally["count"] is None
    assert tally["hold"] is True
    assert "unknown field(s) ['confidence']" in tally["reason"]


def test_resealed_testimonium_payload_with_an_unknown_field_makes_the_tally_unknown(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "happy")
    initial = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert initial.returncode == 0, initial.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    changed = copy.deepcopy(record)
    changed["payload"]["approval_ref"] = "art_0123456789abcdef"
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    tally = attestatores.attempt_tally(tree)

    assert tally["state"] == "UNKNOWN"
    assert tally["count"] is None
    assert tally["hold"] is True
    assert "unknown field(s) ['approval_ref']" in tally["reason"]


# --- Invariant #10 is fatal, and a hold is not the same fact ---------------------


def test_an_accounting_imbalance_is_fatal_and_never_becomes_a_hold(tmp_path, monkeypatch):
    """`FatalAccounting` must not be caught here and turned into UNKNOWN + hold.

    The two states say different things. A hold says *the count is unknown* and a
    caller may wait for it to become known. An accounting imbalance says *the
    partition itself is broken* — a unit in no terminal set, or in more than one —
    and `FatalAccounting`'s own docstring is the rule: "nothing may catch this and
    carry on". `attempt_tally` calls `latest_attempt`, which raises it in five
    places, from inside a block whose broadest handler caught every `ContractError`.
    So the fatal case reported as merely unknown, and a caller that holds on an
    unknown count would have waited politely for a broken partition to resolve
    itself. Found by CodeRabbit reviewing the rebased branch.
    """

    run_root, tree = run_to_designator(tmp_path, "happy")
    result = invoke_stage(
        run_root,
        "retention",
        "happy",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=1,
    )
    assert result.returncode == 0, result.stderr
    assert attestatores.attempt_tally(tree)["state"] == "KNOWN"

    def imbalanced(*_args, **_kwargs):
        raise attestatores.FatalAccounting("a unit is in no terminal set")

    monkeypatch.setattr(attestatores, "latest_attempt", imbalanced)

    with pytest.raises(attestatores.FatalAccounting, match="no terminal set"):
        attestatores.attempt_tally(tree)


def test_an_outer_manifest_accounting_imbalance_is_fatal_and_never_becomes_a_hold(
    tmp_path, monkeypatch
):
    """The tally's outer manifest read must preserve `FatalAccounting` too."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    result = invoke_stage(
        run_root,
        "retention",
        "happy",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=1,
    )
    assert result.returncode == 0, result.stderr
    assert attestatores.attempt_tally(tree)["state"] == "KNOWN"

    def imbalanced(_stage):
        raise attestatores.FatalAccounting("the outer manifest partition is broken")

    monkeypatch.setattr(tree, "build_manifest", imbalanced)

    with pytest.raises(attestatores.FatalAccounting, match="outer manifest partition"):
        attestatores.attempt_tally(tree)


def test_a_fatal_closing_tally_does_not_publish_a_completion_seal(tmp_path, monkeypatch):
    """A fatal close cannot leave the checkpoint that only a closed pass earns."""
    run_root, tree = run_to_designator(tmp_path, "happy")

    def imbalanced(*_args, **_kwargs):
        raise attestatores.FatalAccounting("the closing partition is broken")

    monkeypatch.setattr(attestatores, "attempt_tally", imbalanced)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pipeline/3_attestatores/run.py",
            "--run-root",
            str(run_root),
            "--run-id",
            "retention",
            "--scenario",
            "happy",
            "--fixture-root",
            str(ROOT / "proof"),
        ],
    )

    with pytest.raises(attestatores.FatalAccounting, match="closing partition"):
        attestatores.main()

    assert not any(
        entry["kind"] == "stage-seal" for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
    )


def test_main_does_not_turn_a_fatal_manifest_outcome_into_a_hold(tmp_path):
    """The initial existing-attempt check builds the manifest before the tally.
    Its FatalAccounting must reach the stage boundary as fatal, not be caught by
    the neighbouring ordinary ContractError-to-hold path."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    initial = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert initial.returncode == 0, initial.stderr
    record = _testimonium_for(tree, act_key="a1", chair="attestator_1", ordinal=1)
    damaged = copy.deepcopy(record)
    damaged["outcome"] = "outside-the-closed-vocabulary"
    damaged["self_hash"] = self_hash(damaged)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(damaged))

    retry = invoke_stage(
        run_root,
        "retention",
        "happy",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )

    assert retry.returncode == 2
    assert "outside-the-closed-vocabulary" in retry.stderr
    assert "attempt tally UNKNOWN" not in retry.stderr


# --- Audit-and-repair regression (F-O5) -----------------------------------------
#
# Opus audit-and-repair seat 3, R0. The act-level tally walk filters on
# `kind == "testimonium"`, so the `payload["scope"] == "page"` skip inside the loop
# was unreachable. Had anything reached it, one self-reported field would have
# carried an act-scoped record past every check in `attempt_tally` -- its closed
# field set, its named chair, its content health, and `validate_tallied_testimonium`.
# `scope` and `page_ordinal` were also listed as optional act-level payload fields,
# which is what made the disguise well-formed in the first place.


def test_an_act_scoped_testimonium_cannot_wear_page_scope_to_skip_the_tally(tmp_path):
    """A field nothing validates is a field nothing downstream can trust.

    This is the file's own rule about its closed payload, applied to the two
    fields that belong to the page-scoped kind. A resealed act-scoped record
    claiming `scope: "page"` must make the count UNKNOWN, not disappear from it.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )
    record = _testimonium_for(tree, act_key="a1", chair="attestator_2", ordinal=1)
    changed = copy.deepcopy(record)
    changed["payload"]["scope"] = "page"
    changed["payload"]["page_ordinal"] = 1
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    tally = attestatores.attempt_tally(tree)

    assert tally["state"] == "UNKNOWN"
    assert tally["hold"] is True
    assert "unknown field(s) ['page_ordinal', 'scope']" in tally["reason"], tally["reason"]


# --- Fresh-context review (P2): the same disguise, in the sibling walk -----------
#
# `_attempt_history` carried the identical `payload["scope"] == "page"` skip behind
# the identical `kind == "testimonium"` filter, and F-O5 removed only the one in
# `attempt_tally`. Unreachable for any record this stage writes -- page testimony is
# a kind of its own -- its one reachable effect was on a record that lied, and it
# was the wrong effect: the disguised record left the append/collision history
# entirely, so `require_appendable_ordinal` would judge that act/chair pair against
# no history at all. The tally is what refuses such a record (the sibling test
# above); the history's job is to see it.


def test_a_page_scope_claim_cannot_hide_an_act_scoped_attempt_from_the_history(tmp_path):
    """One self-reported field may not remove an attempt from its own history."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    assert (
        invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py").returncode
        == 0
    )
    record = _testimonium_for(tree, act_key="a1", chair="attestator_2", ordinal=1)
    changed = copy.deepcopy(record)
    changed["payload"]["scope"] = "page"
    changed["payload"]["page_ordinal"] = 1
    changed["self_hash"] = self_hash(changed)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", record["artifact_id"]))
    path.write_bytes(canonical_bytes(changed))
    tree.write_manifest(ATTESTATORES)

    history = attestatores._attempt_history(SimpleNamespace(tree=tree)).by_pair

    surviving = [
        item["artifact_id"] for item in history.get((record["subject_id"], "attestator_2"), [])
    ]
    assert record["artifact_id"] in surviving, (
        "an act-scoped Testimonium claiming page scope vanished from the attempt history; "
        "the append/collision bound would then be derived for that pair without the "
        "disguised record it exists to see"
    )


def test_a_normalized_match_with_no_raw_counterpart_is_retained_as_unaligned(tmp_path, monkeypatch):
    """A synthesized separator is not a raw span at the normalized offset.

    The caller used to publish `(start, start)` in raw coordinates when every
    normalized character in the clipped match mapped to ``None``. That asserted
    alignment to an empty raw slice the witness never supplied. Exercise the
    caller, not only the translator: both page chairs must retain an explicit
    unaligned fact, with no zero-length aligned attachment surviving.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    loss = {
        "markup_characters": 0,
        "whitespace_characters": 1,
        "unicode_reencoded_characters": 0,
    }

    def separator_only_alignment(_witness, _anchor, _limits):
        return {
            "status": "aligned",
            "witness": {"text": " ", "offset_map": [None], "loss": loss},
            "anchor": {"text": "S", "offset_map": [0], "loss": loss},
            "spans": [
                {
                    "witness": {"start": 0, "end": 1},
                    "anchor": {"start": 0, "end": 1},
                }
            ],
        }

    monkeypatch.setattr(attestatores, "align_to_anchor", separator_only_alignment)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--run-root",
            str(run_root),
            "--run-id",
            "retention",
            "--scenario",
            "happy",
            "--fixture-root",
            str(ROOT / "proof"),
        ],
    )

    assert attestatores.main() == 0
    a1 = next(
        tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "act-attachment"
        and tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])["payload"][
            "act_key"
        ]
        == "a1"
    )
    page_attachments = [row for row in a1["payload"]["attachments"] if row["page_witness"]]
    assert page_attachments
    assert all(
        row["alignment"] == {"status": "unaligned", "reason": "no-raw-counterpart-for-aligned-span"}
        for row in page_attachments
    )
    assert all(row["attached"] is False and row["span"] is None for row in page_attachments)
