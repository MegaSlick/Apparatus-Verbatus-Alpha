"""The bounded recovery budget is the run's sealed policy, never a file read twice.

`config/recovery.toml` decides how many times rework may be asked for before an act
goes to review. Until this build it was read once when a run's binding was checked
and again at each point of use, and the two reads are not the same act: a rewrite
landing between them let the Recensor publish reviews and recovery-requests
carrying an allowance the run never sealed.

That failure does not undo. The review and the request are immutable records under
an immutable identity, so restoring the file does not recover the run: the correct
rerun computes different bytes under the same review identity and stops with
`IncompatibleReuse`. Measured before this file existed — a swap to a zero fallback
allowance held both acts for review with `budget_allowed: 0` under a run whose
`config_digest` bound the stock allowance (audit S3).

The policy is now parsed once, at the binding check, and carried in `StageContext`.
Each point of use — the Recensor, the Designator's recovery pass, and the
orchestrator's dispatch, which is not a stage and proves it against the digests
`run.json` recorded — asks for the sealed digest by name.
"""

from __future__ import annotations

import ast
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import ContractError
from common.contracts.stages import RECENSOR
from common.recovery import load_recovery_policy
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
SHIPPED_RECOVERY = ROOT / "config" / "recovery.toml"

# The stages that must run before the Recensor has anything to review.
BEFORE_RECENSOR = (
    "pipeline/1_exemplar/door.py",
    "pipeline/1_exemplar/run.py",
    "pipeline/1_ink_map/run.py",
    "pipeline/2_designator/run.py",
    "pipeline/3_attestatores/run.py",
    "pipeline/4_perlector/run.py",
)


def _invoke(program: str, root: Path, run_id: str, scenario: str, recovery: Path):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
            "--fixture-root",
            str(ROOT / "proof"),
            "--recovery-config",
            str(recovery),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _through_perlector(root: Path, run_id: str, scenario: str, recovery: Path) -> None:
    for program in BEFORE_RECENSOR:
        result = _invoke(program, root, run_id, scenario, recovery)
        assert result.returncode == 0, f"{program}: {result.stderr}"


def _load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spent_policy(text: str) -> str:
    """The shipped policy with every per-act allowance spent, and nothing else moved."""
    assert "fallback_recrop = 1" in text and "page_level_reread = 1" in text, (
        "the shipped recovery config no longer budgets one of each operation"
    )
    return text.replace("fallback_recrop = 1", "fallback_recrop = 0").replace(
        "page_level_reread = 1", "page_level_reread = 0"
    )


def _recensor_records(tree: RunTree) -> list[dict]:
    return [
        tree.read_artifact(RECENSOR, entry["kind"], entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] in {"review", "recovery-request"}
    ]


def test_a_policy_swapped_after_the_binding_check_never_reaches_a_published_review(
    tmp_path, monkeypatch
):
    """The audit's own demonstration, made durable.

    The swap lands exactly where it landed then: after `open_stage_context` has
    checked this run's binding and before the Recensor publishes anything. The stage now
    has nothing left to re-read, so every review and every recovery-request it
    writes carries the allowance the run sealed — and the act the scenario asks
    recovery for still gets its request, rather than being held under a budget of
    zero that no run ever bound.
    """
    root = tmp_path / "runs"
    recovery_path = tmp_path / "recovery.toml"
    shutil.copyfile(SHIPPED_RECOVERY, recovery_path)
    _through_perlector(root, "review", "review", recovery_path)

    sealed = load_recovery_policy(recovery_path)
    assert sealed["allowed"] == 2, "this test's premise is a run sealed with a spendable budget"

    recensor = _load("recensor_recovery_binding_under_test", "pipeline/5_recensor/run.py")
    original = recovery_path.read_text(encoding="utf-8")
    bind = recensor.open_stage_context

    def swapping_open_stage_context(args, stage, **kwargs):
        context = bind(args, stage, **kwargs)
        recovery_path.write_text(_spent_policy(original), encoding="utf-8")
        return context

    monkeypatch.setattr(recensor, "open_stage_context", swapping_open_stage_context)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--run-root",
            str(root),
            "--run-id",
            "review",
            "--scenario",
            "review",
            "--fixture-root",
            str(ROOT / "proof"),
            "--recovery-config",
            str(recovery_path),
        ],
    )
    assert recensor.main() == 3

    assert recovery_path.read_text(encoding="utf-8") != original, (
        "the swap this test is about did not actually happen"
    )
    tree = RunTree(root, "review")
    records = _recensor_records(tree)
    assert records, "the Recensor published nothing to check"
    requests = [record for record in records if record["kind"] == "recovery-request"]
    assert requests, (
        "the review scenario asks one act for a fallback recrop and the sealed budget "
        "allows it; a request missing here means the swapped zero allowance was read"
    )
    for record in records:
        payload = record["payload"]
        if "recovery_policy" in payload:
            assert payload["recovery_policy"] == sealed, (
                f"{record['artifact_id']} names a recovery policy the run never sealed"
            )
        if "budget_allowed" in payload:
            assert payload["budget_allowed"] == sealed["allowed"], (
                f"{record['artifact_id']} was sealed under an allowance of "
                f"{payload['budget_allowed']}, not the run's {sealed['allowed']}"
            )


def _calls_named(source: str, name: str) -> list[str]:
    """Every call in one module whose callee ends in `name`, as written."""
    return [
        ast.unparse(node.func)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] == name
    ]


def test_neither_point_of_use_reopens_the_recovery_policy_for_itself():
    """One read is the fix; a second read is the defect, wherever it reappears.

    Asserted against the syntax rather than through behaviour because behaviour
    cannot see the difference until something rewrites the file mid-run — which is
    precisely the state this discipline exists to make unreachable. Against the
    syntax rather than the text, so a comment that names the loader is prose about
    the defect rather than a failure.
    """
    for relative in ("pipeline/5_recensor/run.py", "pipeline/2_designator/run.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert not _calls_named(source, "load_recovery_policy"), (
            f"{relative} reads config/recovery.toml itself; the budget it acts on must be "
            "the one `open_context` parsed and sealed (StageContext.recovery_policy)"
        )
        # Through the AST like the check above it, per this test's own stated
        # principle — and specifically for the "recovery" name: the Designator
        # also rechecks padding and geometry, so a nameless call count would
        # pass with the recovery recheck deleted.
        recovery_rechecks = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and ast.unparse(node.func).split(".")[-1] == "require_sealed_config"
            and any(isinstance(arg, ast.Constant) and arg.value == "recovery" for arg in node.args)
        ]
        assert recovery_rechecks, (
            f"{relative} does not prove its recovery budget against the run's sealed digest"
        )


def test_a_context_without_a_sealed_recovery_policy_refuses_rather_than_reading_as_zero():
    """A missing budget must not read as a zero budget.

    `StageContext.recovery_policy` refuses rather than handing back a `None` for a
    caller to index — zero is exactly the value the swap produced in published
    reviews, and it is the one wrong answer that looks like an answer.
    """
    from common.stage import StageContext

    context = StageContext(
        tree=None,
        run={},
        fixture={},
        scenario="happy",
        stage=RECENSOR,
        adapter_revision="unused",
        args=SimpleNamespace(),
        registry=None,
    )
    with pytest.raises(ContractError, match="no run-sealed recovery policy"):
        assert context.recovery_policy


def test_the_run_authority_names_the_recovery_policy_it_was_sealed_under(tmp_path):
    """Recorded, not merely hashed: a reader holding the tree can name the file."""
    root = tmp_path / "runs"
    recovery_path = tmp_path / "recovery.toml"
    shutil.copyfile(SHIPPED_RECOVERY, recovery_path)
    result = _invoke("pipeline/1_exemplar/door.py", root, "named", "happy", recovery_path)
    assert result.returncode == 0, result.stderr

    run = RunTree(root, "named").read_run()
    assert (
        run["sealed_config_digests"]["recovery"]
        == load_recovery_policy(recovery_path)["config_sha256"]
    )


def test_the_orchestrator_refuses_to_dispatch_recovery_under_an_unsealed_policy(tmp_path):
    """The third point of use. It is not a stage, so it asks the run authority.

    Before this, the dispatcher bounded the whole loop — the round ceiling and
    every request it checked — on whatever the file said at that moment. A run
    with nothing outstanding took no round at all, so the swap was invisible and
    the next stage discovered it as an `IncompatibleReuse` about a configuration
    nobody had been told changed.
    """
    root = tmp_path / "runs"
    recovery_path = tmp_path / "recovery.toml"
    shutil.copyfile(SHIPPED_RECOVERY, recovery_path)
    _through_perlector(root, "happy", "happy", recovery_path)
    assert (
        _invoke("pipeline/5_recensor/run.py", root, "happy", "happy", recovery_path).returncode == 0
    )

    orchestrator = _load("orchestrator_recovery_binding_under_test", "pipeline/orchestrator/run.py")
    args = SimpleNamespace(run_root=str(root), run_id="happy", recovery_config=str(recovery_path))
    assert orchestrator.drive_recovery(args, hard_failure_policy={}) is None

    recovery_path.write_text(
        _spent_policy(recovery_path.read_text(encoding="utf-8")), encoding="utf-8"
    )
    with pytest.raises(ContractError, match="recovery configuration changed between"):
        orchestrator.drive_recovery(args, hard_failure_policy={})
