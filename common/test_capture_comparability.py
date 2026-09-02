"""Pin capture comparability to Unit 5's real triage facts.

The condition must be derived through ``capture_comparability.py`` and must
refuse missing facts; otherwise producer wiring could make the gate permanently
true without detection.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from common.capture_comparability import (
    COMPARABILITY_DIFFERENCE_CODES,
    TRIAGE_FACT_FIELDS,
    comparability_from_triage,
)
from common.contracts.errors import SchemaRefusal

ROOT = Path(__file__).resolve().parent.parent
TRIAGE_MANIFEST = ROOT / "pipeline" / "0_triage" / "manifest.py"

# The condition may be declared, consumed, and derived only at these seams; a
# producer that computes it elsewhere would bypass the Unit 5 reconciliation.
_CONDITION_AUTHORS = {
    Path("common/cross_capture_dissent.py"),
    Path("common/reshoot_delta.py"),
    Path("common/capture_comparability.py"),
}
_CONDITION_NAMES = {"capture_condition", "comparably_captured"}
_SKIP_DIRECTORIES = {
    ".git",
    ".claude",
    ".venv",
    "venv",
    "__pycache__",
    "cleanroom",
    "workbench",
    "private",
    "scriptorium",
    "worktrees",
}


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "mode": "auto",
        "actor": {"kind": "producer", "identity": "verbatus-triage", "revision": "0.0.0"},
        "human_override": False,
    }
    row.update(overrides)
    return row


def _production_sources() -> list[Path]:
    found = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if set(relative.parts) & _SKIP_DIRECTORIES or relative.name.startswith("test_"):
            continue
        found.append(relative)
    if not found:
        raise AssertionError(
            f"no production sources were found under {ROOT}; the capture-condition scan "
            "would pass by inspecting nothing"
        )
    return found


def test_a_hand_cropped_capture_against_an_auto_cropped_one_is_not_comparably_captured():
    hand = _row(
        mode="manual",
        actor={"kind": "human", "identity": "operator", "revision": None},
        human_override=True,
    )
    auto = _row()
    result = comparability_from_triage(hand, auto)
    assert result["comparably_captured"] is False
    assert result["difference_codes"] == [
        "triage-actor-identity-differs",
        "triage-actor-kind-differs",
        "triage-actor-revision-differs",
        "triage-human-override-differs",
        "triage-mode-differs",
    ]
    assert set(result["difference_codes"]) <= COMPARABILITY_DIFFERENCE_CODES


def test_two_captures_triaged_identically_are_comparably_captured_with_nothing_to_name():
    result = comparability_from_triage(_row(), _row())
    assert result == {"comparably_captured": True, "difference_codes": []}


def test_each_triage_fact_alone_is_enough_to_make_a_pair_not_comparably_captured():
    variants = (
        ({"mode": "semi"}, "triage-mode-differs"),
        (
            {"actor": {"kind": "scantailor", "identity": "verbatus-triage", "revision": "0.0.0"}},
            "triage-actor-kind-differs",
        ),
        (
            {"actor": {"kind": "producer", "identity": "other-tool", "revision": "0.0.0"}},
            "triage-actor-identity-differs",
        ),
        (
            {"actor": {"kind": "producer", "identity": "verbatus-triage", "revision": "0.0.1"}},
            "triage-actor-revision-differs",
        ),
        ({"human_override": True}, "triage-human-override-differs"),
    )
    for override, code in variants:
        result = comparability_from_triage(_row(), _row(**override))
        assert result == {"comparably_captured": False, "difference_codes": [code]}


def test_capture_argument_order_cannot_change_comparability_or_its_named_differences():
    hand = _row(
        mode="manual",
        actor={"kind": "human", "identity": "operator", "revision": None},
        human_override=True,
    )
    auto = _row()

    expected = {
        "comparably_captured": False,
        "difference_codes": [
            "triage-actor-identity-differs",
            "triage-actor-kind-differs",
            "triage-actor-revision-differs",
            "triage-human-override-differs",
            "triage-mode-differs",
        ],
    }
    assert comparability_from_triage(hand, auto) == expected
    assert comparability_from_triage(auto, hand) == expected


def test_an_absent_triage_fact_is_refused_rather_than_read_as_comparable():
    for missing in TRIAGE_FACT_FIELDS:
        partial = {key: value for key, value in _row().items() if key != missing}
        with pytest.raises(SchemaRefusal, match="triage decision facts"):
            comparability_from_triage(_row(), partial)
    with pytest.raises(SchemaRefusal, match="triage decision facts"):
        comparability_from_triage(_row(), {})
    with pytest.raises(SchemaRefusal, match="triage actor"):
        comparability_from_triage(_row(), _row(actor={"kind": "human"}))
    with pytest.raises(SchemaRefusal, match="human_override"):
        comparability_from_triage(_row(), _row(human_override="no"))


@pytest.mark.parametrize(
    ("overrides", "cause"),
    (
        ({"mode": None}, "triage mode"),
        ({"mode": "unknown"}, "triage mode"),
        (
            {"actor": {"kind": None, "identity": "verbatus-triage", "revision": "0.0.0"}},
            "actor kind",
        ),
        (
            {"actor": {"kind": "producer", "identity": " ", "revision": "0.0.0"}},
            "actor identity",
        ),
        (
            {"actor": {"kind": "producer", "identity": "verbatus-triage", "revision": None}},
            "actor revision",
        ),
        (
            {"actor": {"kind": "human", "identity": "operator", "revision": "invented"}},
            "actor revision",
        ),
    ),
)
def test_malformed_triage_facts_are_refused_rather_than_compared_equal(overrides, cause):
    malformed = _row(**overrides)

    with pytest.raises(SchemaRefusal, match=cause):
        comparability_from_triage(malformed, malformed)


def test_no_production_module_outside_the_derivation_names_the_capture_condition():
    """Production wiring must derive the condition from Unit 5 at the allowed seam."""
    offenders: dict[str, list[str]] = {}
    for relative in _production_sources():
        if relative in _CONDITION_AUTHORS:
            continue
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        named = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                named |= _CONDITION_NAMES & {node.value}
            elif isinstance(node, ast.Name):
                named |= _CONDITION_NAMES & {node.id}
            elif isinstance(node, ast.Attribute):
                named |= _CONDITION_NAMES & {node.attr}
            elif isinstance(node, ast.keyword) and node.arg:
                named |= _CONDITION_NAMES & {node.arg}
        if named:
            offenders[str(relative)] = sorted(named)
    assert offenders == {}, (
        "these production modules name the capture condition outside "
        f"{sorted(str(path) for path in _CONDITION_AUTHORS)}; a producer must derive it from "
        f"Unit 5's triage rows through comparability_from_triage: {offenders}"
    )


def test_the_three_entitled_modules_still_exist_so_the_scan_cannot_pass_vacuously():
    """A scan whose allow-list has rotted away would pass by finding nothing."""
    scanned = _production_sources()
    assert Path("common/physical_act_partition.py") in scanned
    for relative in _CONDITION_AUTHORS:
        assert (ROOT / relative).is_file(), relative
    assert any(
        "comparably_captured" in (ROOT / relative).read_text(encoding="utf-8")
        for relative in _CONDITION_AUTHORS
    )
    assert TRIAGE_MANIFEST.is_file()
