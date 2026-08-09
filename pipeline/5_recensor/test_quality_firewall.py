"""Spec 09's fifth test: recovery recovers coverage, and there is no path from a
quality signal to a re-roll.

    5. Quality firewall: a suspected-fabrication flag routes to review; no code
       path exists from a quality flag to a re-roll (module boundary test).

GOVERNANCE 11 states the rule and ARCHITECTURE repeats it: "It recovers coverage,
not quality. A suspected fabrication or a poor reading may be flagged for review.
It may never be re-rolled until it looks better." That is a claim about what code
*cannot* do, so proving it needs the structural half as well as the behavioural
one -- a behavioural test only shows that today's inputs do not reach the branch.

**The fabrication flag does not exist yet, and this test does not pretend it
does.** Spec 09 allows "a vision or text model [to] **flag** where determinism
cannot see (incoherence, suspected gaps)"; nothing in the built pipeline produces
such a flag, and inventing a fake one here would test this file's own fixture
rather than the stage. So the behavioural half drives the quality signals that
genuinely exist -- a `truncated` Perlectio, which is a FAILED-class reading that
still carries text, the exact shape a re-roll would be tempting for -- and the
structural half is what will still hold on the day a real flag is added, because
it constrains the recovery gate itself rather than the inputs reaching it.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.stages import RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
RECENSOR_SOURCE = ROOT / "pipeline/5_recensor/run.py"

# Every name the recovery gate is allowed to consult. All of them are coverage
# and budget facts. None of them can be derived from what a reading SAID.
_COVERAGE_AND_BUDGET_NAMES = {
    "continuation_shortfall",
    "wants_recovery",
    "used_fallback",
    "allowed_fallback",
    "used_total",
    "budget",
}

# Names in this stage that carry a reading's quality rather than its coverage.
# None of them may appear in the gate, or in what the gate is computed from.
_QUALITY_NAMES = {
    "reading_class",
    "latest",
    "latest_payload",
    "basis_regions",
    "reading_ref",
    "blank_evidence",
    "corroborating_chairs",
    "OutcomeClass",
    "classify",
}


def _module() -> ast.Module:
    return ast.parse(RECENSOR_SOURCE.read_text(encoding="utf-8"))


def _names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _recovery_request_publications(tree: ast.Module) -> list[ast.Call]:
    """Every `publish(kind="recovery-request", ...)` call site in the stage."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "kind"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "recovery-request"
            ):
                found.append(node)
    return found


def _enclosing_if(tree: ast.Module, target: ast.Call) -> ast.If:
    """The innermost `if` whose body contains this call."""
    enclosing = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if any(
            target is descendant for statement in node.body for descendant in ast.walk(statement)
        ):
            enclosing = node
    assert enclosing is not None, "the recovery request is not inside any conditional at all"
    return enclosing


# --- The structural half: the module boundary spec 09 asks for -----------------


def test_exactly_one_place_in_the_stage_can_ask_for_recovery():
    """One gate, so "the gate is coverage-only" is a statement about all of them."""
    assert len(_recovery_request_publications(_module())) == 1


def test_the_recovery_gate_consults_coverage_and_budget_and_nothing_else():
    """The condition guarding the request may not name a reading-quality fact.

    This is the firewall itself. A future edit that added `or reading_class is
    OutcomeClass.FAILED` to this condition -- the single most natural way to
    build an accidental re-roll -- fails here, by name.
    """
    tree = _module()
    gate = _enclosing_if(tree, _recovery_request_publications(tree)[0])
    consulted = _names(gate.test)
    # Meta-invariant #88: a subset assertion is satisfied by an empty set, so an
    # `_enclosing_if` that found the wrong node would pass silently. The gate is
    # known to consult these two, and saying so is what stops this passing
    # vacuously.
    assert {"wants_recovery", "continuation_shortfall"} <= consulted, (
        f"the conditional found guarding the recovery request consults {sorted(consulted)}, "
        "which is not the coverage gate; this test located the wrong node"
    )
    assert consulted <= _COVERAGE_AND_BUDGET_NAMES, (
        f"the recovery gate consults {sorted(consulted - _COVERAGE_AND_BUDGET_NAMES)}, which is "
        "outside the coverage and budget facts it is allowed to see"
    )
    assert not consulted & _QUALITY_NAMES


def test_what_the_gate_is_computed_from_is_coverage_only():
    """`wants_recovery` itself must not be derived from a reading's quality.

    Constraining the `if` alone would be satisfied by computing the same forbidden
    thing one line earlier and calling it a coverage name.
    """
    tree = _module()
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "wants_recovery"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    sources = _names(assignments[0].value)
    assert sources <= {"act_key", "scenario", "used_total"}, (
        f"wants_recovery is derived from {sorted(sources)}; recovery is requested on coverage "
        "evidence, never on what a reading said"
    )


def test_the_recensor_cannot_re_invoke_a_reading_stage_at_all():
    """The deeper structural guarantee: this stage has no way to run anything.

    Even a gate that consulted only coverage could re-roll if the stage could
    invoke the Perlector itself. It cannot: recovery is dispatched by the
    orchestrator, from an artifact the Recensor appended, which is what keeps the
    loop countable and stops a stage from recropping its own evidence until it
    likes it. A `subprocess` import here would be the first step of undoing that.
    """
    tree = _module()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"subprocess", "os", "importlib", "multiprocessing"}, (
        f"the Recensor imports {sorted(imported & {'subprocess', 'os', 'importlib'})}; it "
        "appends recovery requests and never invokes the stage that answers one"
    )


# --- The behavioural half: a real quality failure reaches review, not rework ----


def _run_through_recensor(root: Path, run_id: str, scenario: str):
    result = None
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                run_id,
                "--scenario",
                scenario,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"
    return result


def test_a_failed_class_reading_is_reviewed_and_never_re_requested(tmp_path):
    """A `truncated` Perlectio is a reading-quality failure that still carries
    text. It is held for review with the outcome named, and the run appends no
    recovery request for it at all -- not one that is later refused, none."""
    root = tmp_path / "runs"
    result = _run_through_recensor(root, "r", "truncated-reading")
    assert result.returncode == 3, result.stderr

    tree = RunTree(root, "r")
    reviews = {
        record["payload"]["act_key"]: record
        for record in (
            tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            for entry in tree.build_manifest(RECENSOR)["artifacts"]
            if entry["kind"] == "review"
        )
    }
    assert reviews["a1"]["outcome"] == "held-for-review"
    assert "truncated" in reviews["a1"]["payload"]["reason"]

    assert [
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "recovery-request"
    ] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
