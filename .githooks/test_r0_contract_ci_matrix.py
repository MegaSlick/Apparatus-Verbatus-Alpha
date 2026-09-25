"""The CI Python matrix contract: the test job runs exactly the ruled bracket.

Switch-off: delete this file; nothing else references it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
# 3.12 is the floor and 3.14 the newest supported; 3.13 was dropped because no CI
# failure was ever unique to it.
REQUIRED_PYTHON_VERSIONS = {"3.12", "3.14"}


def _check_job_python_versions() -> set[str]:
    """Versions the `test` job actually runs (its matrix, else the bare setup step), not
    digits found anywhere in the workflow text."""
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
    """Exactly the ruled bracket: a missing leg and an extra leg both fail; order is the
    companion test's business."""
    versions = _check_job_python_versions()
    assert versions == REQUIRED_PYTHON_VERSIONS, (
        f"the CI test matrix runs Python version(s) {sorted(versions)}; the contract "
        f"(see the note beside REQUIRED_PYTHON_VERSIONS) requires exactly "
        f"{sorted(REQUIRED_PYTHON_VERSIONS)} — a missing leg and an extra leg both fail"
    )


def test_the_3_12_floor_gate_still_runs_outside_the_matrix():
    """3.12 stays distinguished as the floor, not one member among equals. Listed first is
    this test's proxy: workflow syntax cannot mark one matrix entry as privileged."""
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
    # Branch protection requires the context `check`: it must gate on every matrix leg or
    # the requirement is decorative.
    summary = workflow["jobs"].get("check")
    assert summary is not None, "the required `check` summary job is missing"
    needs = summary.get("needs")
    assert needs == "test" or needs == ["test"], (
        f"the `check` summary job needs {needs!r}; it must need the `test` matrix"
    )
