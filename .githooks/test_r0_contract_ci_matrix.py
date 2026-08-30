"""The CI Python matrix contract: the check job runs exactly the ruled bracket.

Originally an R0 falsification test written blind against a three-version plan
whose source documents (R0_CONTRACT_NOTE.md v2, GAMEPLAN_v3.md) have since left
the tree. The contract now lives in this file alone: `REQUIRED_PYTHON_VERSIONS`
below carries the ruling and its reason, and the assertion holds the workflow to
exactly that set — a missing leg and a stray extra leg both fail it.

Switch-off, in one step: delete this file. Nothing else references it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
# 3.13 was dropped from the contract on 2026-08-29 by Tyrel's in-session
# ruling: 3.12 is the floor and 3.14 the newest supported, and no failure in
# the pr/ queue's CI runs was ever unique to the middle leg. The documents the
# assertion once cited (R0_CONTRACT_NOTE.md, GAMEPLAN_v3.md) are no longer in
# the tree; this comment is the contract's record now.
REQUIRED_PYTHON_VERSIONS = {"3.12", "3.14"}


def _check_job_python_versions() -> set[str]:
    """Every Python version the CI test matrix's `setup-python` step actually runs.

    Reads a `matrix.python-version` strategy if one exists, otherwise the job's
    bare `with: python-version:` value -- so this reports what the workflow
    ACTUALLY executes rather than merely searching workflow text for the digits
    '3.13' or '3.14' somewhere irrelevant (a comment, an unrelated pin).
    """
    workflow = yaml.safe_load(WORKFLOW.read_text())
    check_job = workflow["jobs"]["test"]
    strategy = check_job.get("strategy", {})
    matrix = strategy.get("matrix", {})
    matrix_versions = matrix.get("python-version")
    if isinstance(matrix_versions, list):
        return {str(version) for version in matrix_versions}
    for step in check_job.get("steps", []):
        if step.get("name") == "Set up Python":
            value = step.get("with", {}).get("python-version")
            if value is not None:
                return {str(value)}
    return set()


def test_the_ci_check_job_runs_the_r0_python_matrix():
    """The CI test matrix must run exactly the ruled bracket — membership and
    nothing extra: 3.12 the floor, 3.14 the newest supported. Order carries no
    contract; the floor's distinction is the companion test's business.

    On the base commit the job ran a single hardcoded `python-version: '3.12'`
    with no `strategy.matrix` at all, so this reported exactly one version and
    the assertion below failed red for the contract reason: the R0 CI matrix had
    not landed.
    """
    versions = _check_job_python_versions()
    assert versions == REQUIRED_PYTHON_VERSIONS, (
        f"the CI test matrix runs Python version(s) {sorted(versions)}; the contract "
        f"(see the note beside REQUIRED_PYTHON_VERSIONS) requires exactly "
        f"{sorted(REQUIRED_PYTHON_VERSIONS)} — a missing leg and an extra leg both fail"
    )


def test_the_3_12_floor_gate_still_runs_outside_the_matrix():
    """3.12 must remain distinguished as the floor, not merely be one
    interchangeable member of the bracket — the later legs are cross-version
    coverage, never the floor (the retired plan's phrasing, kept because the
    distinction still binds).

    This is deliberately a companion to the matrix test above rather than a
    restatement of it: a matrix missing 3.12 entirely would fail the first test
    on membership, but a build could misread 'the floor stays distinguished' as
    satisfied by any matrix that merely happens to include 3.12 among equals.
    This asserts 3.12 is listed first, which is this test's own reasonable
    proxy for 'the floor' in a plain
    list-shaped matrix -- flagged in the report as a judgment call, since the
    workflow syntax has no native way to name one matrix entry privileged.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text())
    matrix_versions = (
        workflow["jobs"]["test"].get("strategy", {}).get("matrix", {}).get("python-version")
    )
    assert isinstance(matrix_versions, list) and matrix_versions, (
        "the CI `test` job declares no strategy.matrix.python-version list at all; "
        "the 3.12 floor cannot be distinguished from 3.13/3.14 cross-version "
        "coverage without one"
    )
    assert str(matrix_versions[0]) == "3.12", (
        f"the Python version matrix is {matrix_versions!r}; 3.12 is the floor "
        "and must be listed first, with the later legs as cross-version coverage"
    )
    # Branch protection requires the single context `check`; the summary job is
    # that name, and it must gate on every matrix leg or the requirement is
    # decorative.
    summary = workflow["jobs"].get("check")
    assert summary is not None, "the required `check` summary job is missing"
    needs = summary.get("needs")
    assert needs == "test" or needs == ["test"], (
        f"the `check` summary job needs {needs!r}; it must need the `test` matrix"
    )
