"""The sealed Pass-C audit declaration, loaded and checked.

`audit.py` loads it and every `page-reading` records it as not run.
"""

from __future__ import annotations

from pathlib import Path

import audit
import pytest

from common.contracts.errors import ContractError
from common.perlector_audit import SCHEMA

ROOT = Path(__file__).resolve().parents[2]


def _declaration(
    directory: Path, *, schema: str = SCHEMA, cap: int = 1, ceiling: int = 1, approval: str = ""
) -> Path:
    path = directory / "audit.toml"
    path.write_text(
        f'schema = "{schema}"\n'
        "default_round_cap = 1\n"
        f"absolute_round_cap = {ceiling}\n"
        f"round_cap = {cap}\n"
        f'approval_ref = "{approval}"\n'
    )
    return path


def test_the_shipped_declaration_loads():
    policy, digest = audit.load(ROOT / "config" / "perlector_audit.toml")
    assert policy["schema"] == SCHEMA
    assert len(digest) == 64


def test_an_audit_round_cap_above_one_is_refused_because_no_second_round_exists(tmp_path):
    """A sealed cap of 2 with an approval reference would be recorded but never run."""
    approved = _declaration(tmp_path, cap=2, ceiling=2, approval="project-lead-raised-audit-cap")
    with pytest.raises(ContractError, match="runs no second audit round"):
        audit.load(approved)


def test_a_declaration_of_another_schema_is_refused(tmp_path):
    with pytest.raises(
        ContractError, match=r"schema 'perlector-audit.v2'\) is not its closed schema"
    ):
        audit.load(_declaration(tmp_path, schema="perlector-audit.v2"))


def test_a_raised_cap_needs_the_project_leads_reference(tmp_path):
    raised = _declaration(tmp_path, cap=2, ceiling=2)
    with pytest.raises(ContractError, match="the project lead's approval reference"):
        audit.load(raised)
