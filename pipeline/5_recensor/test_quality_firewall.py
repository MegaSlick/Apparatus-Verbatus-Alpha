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
RECENSOR_DIRECTORY = ROOT / "pipeline/5_recensor"

# **Every non-test source file in the stage, not just `run.py`.** This firewall
# parsed one file while the stage had grown to two, so a `publish(kind=
# "recovery-request", ...)` that moved into `residual_ink.py` would have left the
# gate unguarded while `_recovery_request_publications`'s own docstring went on
# claiming it found "every call site in the stage". A structural guard that scans
# less than it says it scans is worse than none: it reports a pass for ground it
# never covered. Discovered by the branch's own review and carried until now.
#
# `rglob`, not `glob`: a guard that scans one directory while the stage has
# grown a subpackage would repeat the exact same failure one level deeper.
RECENSOR_SOURCES = sorted(
    path for path in RECENSOR_DIRECTORY.rglob("*.py") if not path.name.startswith("test_")
)

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


def _modules() -> list[tuple[Path, ast.Module]]:
    """Every source file of this stage, parsed, so nothing hides in a sibling."""
    assert RECENSOR_SOURCES, "the stage has no source files; this guard found nothing to guard"
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in RECENSOR_SOURCES]


def _names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _recovery_request_publications(
    modules: list[tuple[Path, ast.Module]],
) -> list[tuple[Path, ast.Module, ast.Call]]:
    """Every `publish(kind="recovery-request", ...)` call site in the stage.

    Across every source file, which is what "in the stage" has to mean. Each hit
    carries the file and tree it came from so the caller can find its enclosing
    conditional in the right module rather than in whichever one it guessed.
    """
    found = []
    for path, tree in modules:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "kind"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == "recovery-request"
                ):
                    found.append((path, tree, node))
    return found


def _enclosing_ifs(tree: ast.Module, target: ast.Call) -> list[ast.If]:
    """Every `if` whose body contains this call, innermost first.

    Not only the innermost: a gate wrapped in a second, outer conditional is
    still a gate on the request, and a forbidden name added to that outer
    `if` would let a reading-quality fact decide whether recovery is even
    considered -- exactly the accidental re-roll this firewall exists to
    catch -- while looking clean to a check that only read the inner one.
    """
    enclosing = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            target is descendant for statement in node.body for descendant in ast.walk(statement)
        )
    ]
    assert enclosing, "the recovery request is not inside any conditional at all"
    # `ast.walk` is breadth-first, so a shallower (more outer) enclosing `if`
    # is found before a deeper one; reversed so callers that want "just the
    # gate" via `enclosing[0]` still get the innermost, as `_enclosing_if`
    # used to return unconditionally.
    return list(reversed(enclosing))


# --- The structural half: the module boundary spec 09 asks for -----------------


def test_exactly_one_place_in_the_stage_can_ask_for_recovery():
    """One gate, so "the gate is coverage-only" is a statement about all of them."""
    sites = _recovery_request_publications(_modules())
    assert len(sites) == 1, (
        f"expected one recovery-request site across the whole stage, found "
        f"{[str(path.relative_to(ROOT)) for path, _, _ in sites]}"
    )


def test_the_recovery_gate_consults_coverage_and_budget_and_nothing_else():
    """The condition guarding the request may not name a reading-quality fact.

    This is the firewall itself. A future edit that added `or reading_class is
    OutcomeClass.FAILED` to this condition -- the single most natural way to
    build an accidental re-roll -- fails here, by name.
    """
    _, tree, site = _recovery_request_publications(_modules())[0]
    gates = _enclosing_ifs(tree, site)
    # Every enclosing conditional, not only the innermost -- a forbidden name in
    # an outer `if` gates the request exactly as one in the inner `if` would.
    consulted = set().union(*(_names(gate.test) for gate in gates))
    # Meta-invariant #88: a subset assertion is satisfied by an empty set, so an
    # `_enclosing_ifs` that found the wrong node(s) would pass silently. The gate
    # is known to consult these two, and saying so is what stops this passing
    # vacuously.
    assert {"wants_recovery", "continuation_shortfall"} <= consulted, (
        f"the conditional(s) found guarding the recovery request consult {sorted(consulted)}, "
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
    assignments = [
        node
        for _, tree in _modules()
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
    # Across every source file of the stage. This scanned `run.py` alone while
    # the stage had two, so a `subprocess` import in `residual_ink.py` would have
    # passed a guard whose whole subject is "this stage cannot invoke anything".
    imported = set()
    for _, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
    # One binding, used by both the assertion and its message. Written out twice
    # they had already drifted: the check refused `multiprocessing` and the message
    # intersected a set without it, so a Recensor that imported `multiprocessing`
    # would have failed reporting an empty list of offending modules — the failure
    # naming nothing it failed on. Found by CodeRabbit.
    # `runpy` is on this list because it needs none of the others: one
    # `runpy.run_path("pipeline/4_perlector/run.py")` re-invokes the reading
    # stage in this very process, importing nothing banned, and the guard would
    # have reported a pass over exactly the re-roll GOVERNANCE 11 forbids and
    # this file exists to make impossible. `pty` reaches a shell the same way
    # `subprocess` does; `asyncio` deliberately is NOT here, because its
    # subprocess routes all land on `subprocess` itself and banning the whole
    # module would refuse ordinary concurrency that invokes nothing.
    # Found by CodeRabbit.
    banned = {"subprocess", "os", "importlib", "multiprocessing", "runpy", "pty"}
    assert not imported & banned, (
        f"the Recensor imports {sorted(imported & banned)}; it appends recovery "
        "requests and never invokes the stage that answers one"
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
