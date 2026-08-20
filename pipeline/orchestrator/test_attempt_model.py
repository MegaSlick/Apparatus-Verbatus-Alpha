"""The one attempt model, driven through the real stage programs.

The reading attempt ordinal is a function of the act's *crop* history alone —
one reading of the proposal, plus one for each recovery crop cut since. Witness
testimony never moves it. Four stages derive that same number and every one of
them now derives it through `common/stage.py::recovery_region_count`.

The consequence the Attestatores reread path had to be made to obey: a targeted
reread has a window, and the window closes when the Perlector reads the act. In
the window a reread is a complete evidence layer that runs green to export;
after it, the reread is refused at entry by name. Both halves are asserted here,
because the audits' finding was precisely that neither was true — the reread
wrote a Testimonium no later stage could ever consume, and the wedge that made
was discovered as a schema refusal three stages later rather than named at the
door.

Every test drives the real programs as subprocesses over the real synthetic
fixture; meta-invariant #86 ("a fix proven only on a fixture is not proven")
applies, and this suite is where the two audits' red demonstrations live as
executable checks rather than prose.

Findings: Opus-F2 (2a wedge, 2b whole-pass refusal, 2c currency loss, 2d stale
`complete` export) and Sol-S5 (the lax recovery counter at the Perlector).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import ATTESTATORES, DESIGNATOR, PERLECTOR
from common.runtree.store import RunTree
from common.stage import (
    act_by_key,
    act_identity,
    current_recovery_request,
    load_fixture,
    load_recovery_policy,
)

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE_ROOT = ROOT / "proof"
FIXTURE = "synthetic-two-page-v0"

DOOR = "pipeline/1_exemplar/door.py"
EXEMPLAR = "pipeline/1_exemplar/run.py"
DESIGNATOR_PROGRAM = "pipeline/2_designator/run.py"
ATTESTATORES_PROGRAM = "pipeline/3_attestatores/run.py"
PERLECTOR_PROGRAM = "pipeline/4_perlector/run.py"
RECENSOR_PROGRAM = "pipeline/5_recensor/run.py"
ARCHETYPUS_PROGRAM = "pipeline/6_archetypus/run.py"
ARMARIUM_PROGRAM = "pipeline/7_armarium/run.py"

TO_ATTESTATORES = (DOOR, EXEMPLAR, DESIGNATOR_PROGRAM, ATTESTATORES_PROGRAM)


def invoke(run_root: Path, run_id: str, scenario: str, program: str, *extra: str):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def orchestrate(run_root: Path, run_id: str, scenario: str):
    return subprocess.run(
        [
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
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def through_attestatores(run_root: Path, run_id: str, scenario: str) -> RunTree:
    for program in TO_ATTESTATORES:
        result = invoke(run_root, run_id, scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return RunTree(run_root, run_id)


def reread(run_root: Path, run_id: str, scenario: str, act_id: str, chair: str):
    return invoke(
        run_root,
        run_id,
        scenario,
        ATTESTATORES_PROGRAM,
        "--operation",
        "reread",
        "--act",
        act_id,
        "--chair",
        chair,
    )


def act_id_for(key: str) -> str:
    fixture = load_fixture(str(FIXTURE_ROOT))
    return act_identity(fixture, act_by_key(fixture, key))


def artifacts(tree: RunTree, stage: str, kind: str, subject: str) -> list[dict]:
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind and entry["subject_id"] == subject
    ]


def snapshot(run_root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(run_root)): path.read_bytes()
        for path in sorted(run_root.rglob("*"))
        if path.is_file()
    }


def reseal(path: Path, record: dict) -> None:
    record["self_hash"] = self_hash(
        {key: value for key, value in record.items() if key != "self_hash"}
    )
    path.write_bytes(canonical_bytes(record))


# --- Sol-S5: one reader for the recovery denominator, at the Perlector ---------
#
# `_next_attempt` counted `origin == "recovery"` itself and scored every other
# value — including an unknown or malformed one — as zero, while the Recensor,
# Archetypus and Armarium all asked `recovery_region_count`, which refuses an
# origin outside the closed `{proposal, recovery}` vocabulary. Two derivations of
# one accounting fact, in the one stage that publishes an immutable record from
# it. The readable path also happens to be caught downstream of here by T5's
# image-based lineage check, which refuses the same forged origin with a message
# about Exemplar lineage; that is a second check, not this one, and it does not
# make the ordinal correct at the moment it is derived.


def _recovered_tree(run_root: Path, run_id: str) -> tuple[RunTree, str]:
    """A real `review` tree carried through one Designator recovery recrop."""
    tree = through_attestatores(run_root, run_id, "review")
    assert invoke(run_root, run_id, "review", PERLECTOR_PROGRAM).returncode == 0
    # The Recensor holds this scenario asking for the recrop; EXIT_HELD is 3.
    assert invoke(run_root, run_id, "review", RECENSOR_PROGRAM).returncode == 3
    act = act_id_for("a1")
    request = current_recovery_request(
        tree, act, load_recovery_policy(ROOT / "config" / "recovery.toml")
    )
    result = invoke(
        run_root,
        run_id,
        "review",
        DESIGNATOR_PROGRAM,
        "--operation",
        "recover",
        "--act",
        act,
        "--recovery-request",
        request["artifact_id"],
    )
    assert result.returncode == 0, result.stderr
    return tree, act


def _forge_recovery_origin(tree: RunTree, act: str, origin: str) -> None:
    forged = 0
    for entry in tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "region" or entry["subject_id"] != act:
            continue
        record = tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        if record["payload"]["origin"] != "recovery":
            continue
        record["payload"]["origin"] = origin
        reseal(
            tree.resolve(tree.artifact_path(DESIGNATOR, "region", entry["artifact_id"])),
            record,
        )
        forged += 1
    assert forged == 1, "the review scenario cuts exactly one recovery crop for act a1"


def test_a_forged_region_origin_is_refused_at_the_perlector_naming_the_denominator(tmp_path):
    """Sol-S5, driven Perlector→Recensor rather than only through the helper.

    The Perlector must refuse the resealed tree *itself*, before it publishes a
    second Perlectio, and it must refuse it as what it is: a region whose place
    in the recovery denominator is unknown. A Perlectio published here is
    immutable, so a refusal that arrives at the next stage arrives after the
    only record that could have been corrected is already sealed.
    """
    root = tmp_path / "runs"
    tree, act = _recovered_tree(root, "r")
    _forge_recovery_origin(tree, act, "mystery")
    before = snapshot(root)

    result = invoke(root, "r", "review", PERLECTOR_PROGRAM, "--act", act)

    assert result.returncode != 0, "the Perlector read on over an unplaceable region origin"
    assert "unrecognized origin 'mystery'" in result.stderr, result.stderr
    assert "recovery denominator" in result.stderr, result.stderr
    readings = artifacts(tree, PERLECTOR, "perlectio", act)
    assert len(readings) == 1, "a second Perlectio was published over a forged denominator"
    # The Recensor is reached with nothing new to reconcile, and refuses for its
    # own copy of the same shared rule rather than establishing anything.
    recensor = invoke(root, "r", "review", RECENSOR_PROGRAM)
    assert recensor.returncode != 0, "the Recensor accepted a tree the Perlector had refused"
    assert "unrecognized origin 'mystery'" in recensor.stderr, recensor.stderr
    unchanged = {path: body for path, body in snapshot(root).items() if path in before}
    assert unchanged == before, "a refused pass rewrote bytes that were already sealed"


def test_the_perlector_derives_its_ordinal_from_the_shared_recovery_reader(tmp_path):
    """The helper-level half of S5: one derivation, not two.

    Read as a pair with the drive above. This one pins that the Perlector's
    ordinal comes out of `recovery_region_count` — so a future origin added to
    that closed vocabulary moves all four stages at once — rather than out of a
    private `== "recovery"` comparison that scores every unknown value zero.
    """
    import importlib.util

    from common.contracts.errors import FatalAccounting

    spec = importlib.util.spec_from_file_location(
        "perlector_attempt_model_under_test", ROOT / "pipeline" / "4_perlector" / "run.py"
    )
    perlector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(perlector)

    def region(origin):
        return {"payload": {"origin": origin}}

    assert perlector._next_attempt(None, "a1", [region("proposal")]) == 1
    assert perlector._next_attempt(None, "a1", [region("proposal"), region("recovery")]) == 2
    with pytest.raises(FatalAccounting, match="recovery denominator"):
        perlector._next_attempt(None, "a1", [region("proposal"), region("mystery")])


# --- Opus-F2 (2a): the reread has a window, and it runs green inside it --------
#
# The audit reported the reread as producing a payload the run tree then refuses
# (IncompatibleReuse). Driving it showed something stronger and simpler: the
# reread had no working window at all. `reread_pass` appended a Testimonium and
# wrote no act-attachment, so the very next Perlector invocation refused the
# stale derived record — in the reread's own intended order, whole pass then
# targeted reread then read. The write path existed and its product was
# unconsumable, which is the "worse than no path" the audit's own disposition
# names.


def test_a_reread_inside_its_window_runs_green_to_a_delivered_export(tmp_path):
    """2a's documented remedy, end to end: reread, then the rest of the run.

    `reread-success` declares a second, longer attestator_2 response for act a2.
    A reread taken before the Perlector reads is the whole of the remedy — no
    whole second pass, no hand-repair — and the run must then reach a delivered
    export whose act a2 witness basis cites the *second* attempt.
    """
    root = tmp_path / "runs"
    act = act_id_for("a2")
    tree = through_attestatores(root, "r", "reread-success")

    result = reread(root, "r", "reread-success", act, "attestator_2")
    assert result.returncode == 0, result.stderr

    for program in (
        PERLECTOR_PROGRAM,
        RECENSOR_PROGRAM,
        ARCHETYPUS_PROGRAM,
        ARMARIUM_PROGRAM,
    ):
        stage_result = invoke(root, "r", "reread-success", program)
        assert stage_result.returncode == 0, f"{program}: {stage_result.stderr}"

    export = tree.read_artifact("armarium", "export", artifact_id("armarium", "export", "export"))
    assert export["outcome"] == "delivered", export["outcome"]
    assert export["payload"]["aggregate"]["status"] == "complete"

    testimonia = artifacts(tree, ATTESTATORES, "testimonium", act)
    current = max(
        (record for record in testimonia if record["payload"]["chair"] == "attestator_2"),
        key=lambda record: record["payload"]["attempt_ordinal"],
    )
    assert current["payload"]["attempt_ordinal"] == 2, "the reread never appended"
    reading = artifacts(tree, PERLECTOR, "perlectio", act)[0]
    assert reading["payload"]["attempt_ordinal"] == 1, (
        "a witness reread moved the reading attempt ordinal; testimony is a clue, not a crop"
    )
    cited = {item["artifact_id"] for item in reading["payload"]["basis"]["testimonia"]}
    assert current["artifact_id"] in cited, (
        "the reading was established over the superseded first attempt"
    )


def test_a_reread_after_the_act_is_read_is_refused_at_entry_by_name(tmp_path):
    """2a's wedge, closed at the door rather than three stages downstream.

    Once a Perlectio exists for the act, its witness basis is fixed and the
    reading ordinal cannot move to acknowledge new testimony — that is the model,
    not an accident of derivation. So the reread is refused here, before it
    writes anything, and the refusal says which act, which Perlectio, and what
    the operator's window was.
    """
    root = tmp_path / "runs"
    act = act_id_for("a2")
    tree = through_attestatores(root, "r", "reread-success")
    assert invoke(root, "r", "reread-success", PERLECTOR_PROGRAM).returncode == 0
    before = snapshot(root)

    result = reread(root, "r", "reread-success", act, "attestator_2")

    assert result.returncode != 0, "a reread was accepted over an act the Perlector had read"
    assert "already carries a Perlectio" in result.stderr, result.stderr
    assert snapshot(root) == before, "a refused reread wrote to the run tree"
    testimonia = artifacts(tree, ATTESTATORES, "testimonium", act)
    assert {record["payload"]["attempt_ordinal"] for record in testimonia} == {1}


def test_the_refused_reread_leaves_the_run_able_to_finish(tmp_path):
    """The refusal is a refusal, not a second wedge.

    A run that asked for a reread too late must still complete on the evidence
    it already has. The audit's finding was "there is no forward path"; this is
    the assertion that there is one.
    """
    root = tmp_path / "runs"
    act = act_id_for("a2")
    through_attestatores(root, "r", "reread-success")
    assert invoke(root, "r", "reread-success", PERLECTOR_PROGRAM).returncode == 0
    assert reread(root, "r", "reread-success", act, "attestator_2").returncode != 0

    for program in (RECENSOR_PROGRAM, ARCHETYPUS_PROGRAM, ARMARIUM_PROGRAM):
        result = invoke(root, "r", "reread-success", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    tree = RunTree(root, "r")
    export = tree.read_artifact("armarium", "export", artifact_id("armarium", "export", "export"))
    assert export["outcome"] == "delivered"


def test_a_reread_of_a_failed_witness_is_retained_and_the_act_still_holds(tmp_path):
    """`reread-failure` means what its name claims, driven rather than declared.

    attestator_2's second attempt on act a1 is a declared failure. The reread is
    inside its window, so it is accepted and retained; the act then carries a
    live `failed` witness, and what the run does with that is the ordinary
    witness-floor accounting — visible, never silently absorbed.
    """
    root = tmp_path / "runs"
    act = act_id_for("a1")
    tree = through_attestatores(root, "r", "reread-failure")

    result = reread(root, "r", "reread-failure", act, "attestator_2")
    assert result.returncode == 0, result.stderr

    assert invoke(root, "r", "reread-failure", PERLECTOR_PROGRAM).returncode == 0
    testimonia = artifacts(tree, ATTESTATORES, "testimonium", act)
    current = max(
        (record for record in testimonia if record["payload"]["chair"] == "attestator_2"),
        key=lambda record: record["payload"]["attempt_ordinal"],
    )
    assert (current["payload"]["attempt_ordinal"], current["outcome"]) == (2, "failed")
    reading = artifacts(tree, PERLECTOR, "perlectio", act)[0]
    outcomes = {
        item["chair"]: item["outcome"] for item in reading["payload"]["basis"]["testimonia"]
    }
    assert outcomes["attestator_2"] == "failed", (
        "the reading was primed from the superseded successful attempt"
    )
    recensor = invoke(root, "r", "reread-failure", RECENSOR_PROGRAM)
    assert recensor.returncode in (0, 3), recensor.stderr


# --- Opus-F2 (2b, 2c): the whole pass is not the remedy, and says so ----------


def test_a_whole_pass_at_the_next_ordinal_after_a_reread_is_refused(tmp_path):
    """2b, reproduced and left refused — with the reason now naming the model.

    On a tree where a reread sealed `failed` at ordinal 2, the whole pass at
    ordinal 2 would write `not-run` over it, and `_refuse_write_collision`
    refuses before writing anything, naming the chair and the two outcomes. That
    refusal is correct and stays. What was missing is that it was also, until now,
    the *prescribed* way forward out of a wedge — so the operator met it as a dead
    end. It is no longer prescribed: the reread's own window is the remedy.
    """
    root = tmp_path / "runs"
    act = act_id_for("a1")
    through_attestatores(root, "r", "happy")
    assert reread(root, "r", "happy", act, "attestator_2").returncode == 0
    before = snapshot(root)

    result = invoke(root, "r", "happy", ATTESTATORES_PROGRAM, "--attempt-ordinal", "2")

    assert result.returncode == 3, result.stderr
    assert "sealed outcome 'failed', this pass would write 'not-run'" in result.stderr
    assert snapshot(root) == before, "a refused whole pass wrote to the run tree"


def test_a_whole_pass_may_not_append_over_an_act_that_was_reread(tmp_path):
    """2c, closed rather than documented.

    The audit's 2c was that a whole pass at ordinal 2 "cost the currency of all
    the others" — five chairs that were current at `read` becoming current at
    `not-run`, because a whole pass asks every configured chair at one ordinal and
    silence there means the chair was not attempted. That was the *prescribed*
    escape from the wedge, which is what made it a defect rather than an operator
    choice. It is now refused: a targeted reread takes its act off the shared
    whole-pass ordinal, and the refusal says so before anything is written.

    Driven on `reread-failure`, where the pair the reread moved would be written
    byte-identically by the whole pass, so no attempt collision fires and this
    rule is the only thing standing between the operator and five superseded
    chairs.
    """
    root = tmp_path / "runs"
    act = act_id_for("a1")
    tree = through_attestatores(root, "r", "reread-failure")
    assert reread(root, "r", "reread-failure", act, "attestator_2").returncode == 0
    before = snapshot(root)

    result = invoke(root, "r", "reread-failure", ATTESTATORES_PROGRAM, "--attempt-ordinal", "2")

    assert result.returncode == 3, result.stderr
    assert "takes it off the shared whole-pass ordinal" in result.stderr, result.stderr
    assert snapshot(root) == before, "a refused whole pass wrote to the run tree"
    superseded = [
        record
        for key in ("a1", "a2")
        for record in artifacts(tree, ATTESTATORES, "testimonium", act_id_for(key))
        if record["outcome"] == "not-run"
    ]
    assert superseded == [], "re-witnessing one chair still cost another chair its currency"


def test_a_whole_second_pass_is_still_available_on_a_run_that_was_not_reread(tmp_path):
    """The rule above bounds the whole pass; it does not remove it.

    A run where nobody ran a targeted reread can still take every configured chair
    through a second attempt — the expensive instrument GOVERNANCE 1 says is an
    acceptable cost — and this is the assertion that closing the reread's
    interaction with it did not close the instrument.
    """
    root = tmp_path / "runs"
    tree = through_attestatores(root, "r", "reread-failure")

    result = invoke(root, "r", "reread-failure", ATTESTATORES_PROGRAM, "--attempt-ordinal", "2")

    assert result.returncode == 0, result.stderr
    ordinals = {
        record["payload"]["attempt_ordinal"]
        for record in artifacts(tree, ATTESTATORES, "testimonium", act_id_for("a1"))
    }
    assert ordinals == {1, 2}


def test_an_act_targeted_reread_of_a_page_witness_is_refused_by_name(tmp_path):
    """A page witness has no act-scoped attempt to repeat.

    Its act-level view is *derived* — the page join, then that join aligned
    against the page anchor — so an act-targeted reread would re-derive one act's
    view from an attempt the page Testimonium does not describe, leaving the two
    records disagreeing about the same chair at the same moment. The honest
    operation is a page-level reread, which the recovery vocabulary already names
    and which nothing has built; the refusal says that rather than half-performing
    the act-scoped one.
    """
    root = tmp_path / "runs"
    act = act_id_for("a2")
    tree = through_attestatores(root, "r", "reread-success")
    before = snapshot(root)

    result = reread(root, "r", "reread-success", act, "attestator_1")

    assert result.returncode != 0, "an act-targeted reread of a page witness was accepted"
    assert "is a page witness" in result.stderr, result.stderr
    assert "page-level-reread" in result.stderr, result.stderr
    assert snapshot(root) == before, "a refused reread wrote to the run tree"
    assert {
        record["payload"]["attempt_ordinal"]
        for record in artifacts(tree, ATTESTATORES, "testimonium", act)
    } == {1}


# --- Opus-F2 (2d): the export may not say `complete` over superseded evidence --
#
# Everything the Armarium says about an act is derived from the latest Recensor
# review and from the reading's own basis references; neither route passes back
# through `latest_per_chair`. So a Testimonium appended after the reading was
# established was structurally invisible exactly where the export decides whether
# to say `complete`. With the Recensor refusing the tree and no new review
# published, stages 6 and 7 run by hand reproduced `delivered`/`complete` — and
# the export sealed by an earlier successful orchestrated run stayed on disk
# saying `complete` after a later reread contradicted it, with nothing marking it
# stale.


def _supersede_a_witness_basis(tree: RunTree, act: str, chair: str) -> None:
    """Append a Testimonium the established reading's basis does not cite.

    Written directly rather than through `reread_pass`, which now refuses this by
    name at entry. The point of the tests below is that the refusal at the door is
    not the only thing standing between a superseded basis and a `complete`
    export: a folder assembled, resumed or resealed some other way must be refused
    on its own structure.
    """
    records = [
        record
        for record in artifacts(tree, ATTESTATORES, "testimonium", act)
        if record["payload"]["chair"] == chair
    ]
    current = max(records, key=lambda record: record["payload"]["attempt_ordinal"])
    ordinal = current["payload"]["attempt_ordinal"] + 1
    appended = json.loads(json.dumps(current))
    appended["payload"]["attempt_ordinal"] = ordinal
    appended["attempt_id"] = attempt_id(act, f"read:{chair}", ordinal)
    appended["artifact_id"] = artifact_id(ATTESTATORES, "testimonium", act, appended["attempt_id"])
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", appended["artifact_id"]))
    reseal(path, appended)
    tree.write_manifest(ATTESTATORES)


def test_the_export_refuses_to_complete_over_a_superseded_witness_basis(tmp_path):
    """2d, on a wedged tree, with stages 6 and 7 run by hand.

    The audit's exact route: a green orchestrated run, a Testimonium appended
    after it, the Recensor refusing, and then the Archetypus and Armarium invoked
    directly. Each of the three must refuse rather than re-derive `complete` from
    a review and a basis that no longer describe the current evidence.
    """
    root = tmp_path / "runs"
    act = act_id_for("a2")
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    export = tree.read_artifact("armarium", "export", artifact_id("armarium", "export", "export"))
    assert export["outcome"] == "delivered", "the run must be green before it is contradicted"

    _supersede_a_witness_basis(tree, act, "attestator_2")

    for program in (RECENSOR_PROGRAM, ARCHETYPUS_PROGRAM, ARMARIUM_PROGRAM):
        result = invoke(root, "r", "happy", program)
        assert result.returncode != 0, f"{program} accepted a superseded witness basis"
        assert "since superseded" in result.stderr, f"{program}: {result.stderr}"


def test_the_armarium_alone_refuses_a_superseded_basis_at_the_export_boundary(tmp_path):
    """The export boundary carries the refusal on its own.

    The Recensor and the Archetypus each hold the same rule, so an operator who
    ran only the last stage would otherwise meet no check at all — which is the
    shape 2d actually took: `categorize` derives everything from the latest
    Recensor review, and the reading's basis is read only through its own
    references. This drives the Armarium directly over an already-established act.
    """
    root = tmp_path / "runs"
    act = act_id_for("a2")
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")

    _supersede_a_witness_basis(tree, act, "attestator_2")

    result = invoke(root, "r", "happy", ARMARIUM_PROGRAM)

    assert result.returncode != 0, "the export completed over a superseded witness basis"
    assert "since superseded" in result.stderr, result.stderr
    assert "attestator_2" in result.stderr, result.stderr
