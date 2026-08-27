"""Act-surface visibility accounting for Unit 19C.

This is deliberately separate from Unit 9's page-ink accounting.  Its small
integer cell surface is a lossless fixture/adapter boundary: production
geometry adapters project masks into the physical-page coordinate system before
calling this module.  No OCR, testimony text, page ink, or ranking fact enters
the calculation.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from common.contracts.canonical import is_sha256
from common.contracts.errors import SchemaRefusal

SCHEMA = "physical-act-visible-surface-union-v1"
_STATES = frozenset({"visible", "occluded", "unresolved"})


def _cells(value: object, what: str) -> frozenset[tuple[int, int]]:
    if not isinstance(value, list):
        raise SchemaRefusal(f"{what} is not a cell list")
    result: set[tuple[int, int]] = set()
    for cell in value:
        if (
            not isinstance(cell, list)
            or len(cell) != 2
            or any(not isinstance(axis, int) or isinstance(axis, bool) for axis in cell)
            or any(axis < 0 for axis in cell)
        ):
            raise SchemaRefusal(f"{what} has a malformed cell")
        result.add((cell[0], cell[1]))
    if len(result) != len(value):
        raise SchemaRefusal(f"{what} repeats a cell")
    return frozenset(result)


def _record_cells(cells: frozenset[tuple[int, int]]) -> list[list[int]]:
    return [[x, y] for x, y in sorted(cells)]


def build_cross_capture_coverage(
    *, logical_act_id: str, components: list[dict[str, Any]]
) -> dict[str, Any]:
    """Measure visibility over all registered captures of a logical act.

    A capture survey must classify every expected cell as visible or explicitly
    occluded.  An unavailable transform, decode, or survey is ``unresolved``;
    it cannot be converted into an occlusion finding by absence.  The result is
    an evidence record, not a text or a page-ink measurement.
    """
    if not isinstance(logical_act_id, str) or not logical_act_id:
        raise SchemaRefusal("cross-capture coverage lacks logical_act_id")
    if not isinstance(components, list) or not components:
        raise SchemaRefusal("cross-capture coverage lacks components")
    published = []
    all_findings: list[dict[str, Any]] = []
    for component in components:
        required_fields = {
            "physical_page_id",
            "expected_cells",
            "required_capture_sha256s",
            "captures",
        }
        if not isinstance(component, dict) or set(component) != required_fields:
            raise SchemaRefusal("cross-capture component is not closed")
        physical_page = component["physical_page_id"]
        if not isinstance(physical_page, str) or not physical_page:
            raise SchemaRefusal("cross-capture component lacks physical_page_id")
        expected = _cells(component["expected_cells"], "expected_cells")
        if not expected:
            raise SchemaRefusal("cross-capture component has an empty expected surface")
        required = component["required_capture_sha256s"]
        if (
            not isinstance(required, list)
            or not required
            or any(not is_sha256(value) for value in required)
        ):
            raise SchemaRefusal("cross-capture component has invalid required captures")
        if len(required) != len(set(required)) or required != sorted(required):
            raise SchemaRefusal("cross-capture required captures are not sorted unique")
        captures = component["captures"]
        if not isinstance(captures, list):
            raise SchemaRefusal("cross-capture component captures are not a list")
        by_source: dict[str, dict[str, Any]] = {}
        visible_union: frozenset[tuple[int, int]] = frozenset()
        occluded_surfaces: list[frozenset[tuple[int, int]]] = []
        for row in captures:
            fields = {
                "source_sha256",
                "alignment_ref",
                "visibility_state",
                "visible_cells",
                "occluded_cells",
                "occlusion_refs",
                "finding_codes",
            }
            if not isinstance(row, dict) or set(row) != fields:
                raise SchemaRefusal("cross-capture capture survey is not closed")
            source, state = row["source_sha256"], row["visibility_state"]
            if (
                not is_sha256(source)
                or source in by_source
                or not isinstance(state, str)
                or state not in _STATES
            ):
                raise SchemaRefusal("cross-capture capture survey has invalid source or state")
            if not isinstance(row["alignment_ref"], str) or not row["alignment_ref"]:
                raise SchemaRefusal("cross-capture capture survey lacks alignment")
            visible = _cells(row["visible_cells"], "visible_cells")
            occluded = _cells(row["occluded_cells"], "occluded_cells")
            if not visible <= expected or not occluded <= expected or visible & occluded:
                raise SchemaRefusal("cross-capture survey escapes or overlaps its expected surface")
            occlusion_refs = row["occlusion_refs"]
            finding_codes = row["finding_codes"]
            if any(
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value for value in values)
                or values != sorted(set(values))
                for values in (occlusion_refs, finding_codes)
            ):
                raise SchemaRefusal("cross-capture survey references are malformed")
            if state == "unresolved":
                if visible or occluded:
                    raise SchemaRefusal("unresolved survey cannot claim measured cells")
            elif visible | occluded != expected:
                raise SchemaRefusal(
                    "measured survey does not classify its complete expected surface"
                )
            elif state == "visible" and occluded:
                raise SchemaRefusal("visible survey claims occluded cells")
            elif state == "occluded" and not occluded:
                raise SchemaRefusal("occluded survey claims no occlusion")
            elif state == "occluded" and not occlusion_refs:
                raise SchemaRefusal("occluded survey carries no occlusion evidence")
            by_source[source] = {
                **row,
                "visible_cells": _record_cells(visible),
                "occluded_cells": _record_cells(occluded),
            }
            visible_union |= visible
            occluded_surfaces.append(occluded)
        if set(by_source) != set(required):
            raise SchemaRefusal("cross-capture survey does not account for every required capture")
        ordered = [by_source[source] for source in required]
        unresolved = any(row["visibility_state"] == "unresolved" for row in ordered)
        uncovered = expected - visible_union
        if not uncovered:
            union_state = "full"
        elif (
            not unresolved
            and not visible_union
            and all(surface >= uncovered for surface in occluded_surfaces)
        ):
            union_state = "occluded-everywhere"
        else:
            union_state = "unresolved"
        findings = []
        if union_state == "occluded-everywhere":
            findings.append({"code": "occluded-everywhere", "physical_page_id": physical_page})
        elif union_state == "unresolved":
            findings.append(
                {"code": "capture-visibility-unresolved", "physical_page_id": physical_page}
            )
        all_findings.extend(findings)
        published.append(
            {
                "physical_page_id": physical_page,
                "expected_cells": _record_cells(expected),
                "required_capture_sha256s": required,
                "captures": ordered,
                "union_visible_cells": _record_cells(visible_union),
                "uncovered_cells": _record_cells(uncovered),
                "union_state": union_state,
                "findings": findings,
            }
        )
    if len({row["physical_page_id"] for row in published}) != len(published):
        raise SchemaRefusal("cross-capture coverage repeats a physical-page component")
    act_state = (
        "occluded-everywhere"
        if published and all(row["union_state"] == "occluded-everywhere" for row in published)
        else "full"
        if all(row["union_state"] == "full" for row in published)
        else "unresolved"
    )
    return {
        "basis": SCHEMA,
        "logical_act_id": logical_act_id,
        "components": sorted(published, key=lambda row: row["physical_page_id"]),
        "act_state": act_state,
        "findings": sorted(all_findings, key=lambda row: (row["physical_page_id"], row["code"])),
    }


def same_chair_witness_floor(
    rows: list[dict[str, Any]], *, components: set[str], floor: int
) -> dict[str, Any]:
    """Count a chair once only after its own comparable rows cover every component."""
    if (
        not isinstance(rows, list)
        or not isinstance(components, set)
        or not components
        or any(not isinstance(component, str) or not component for component in components)
        or not isinstance(floor, int)
        or isinstance(floor, bool)
        or floor < 0
    ):
        raise SchemaRefusal("same-chair witness floor has invalid denominator")
    covered: dict[str, set[str]] = defaultdict(set)
    seen_rows: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "chair",
            "capture",
            "attached",
            "comparable",
            "components",
        }:
            raise SchemaRefusal("witness-floor row is not closed")
        if (
            not isinstance(row["chair"], str)
            or not row["chair"]
            or not isinstance(row["capture"], str)
            or not row["capture"]
        ):
            raise SchemaRefusal("witness-floor row lacks chair/capture provenance")
        if not isinstance(row["attached"], bool) or not isinstance(row["comparable"], bool):
            raise SchemaRefusal("witness-floor row lacks attachment/comparability facts")
        if (
            not isinstance(row["components"], list)
            or any(
                not isinstance(component, str) or not component for component in row["components"]
            )
            or len(row["components"]) != len(set(row["components"]))
        ):
            raise SchemaRefusal("witness-floor row has malformed components")
        identity = (row["chair"], row["capture"])
        if identity in seen_rows:
            raise SchemaRefusal("witness-floor repeats a chair/capture fact")
        seen_rows.add(identity)
        row_components = set(row["components"])
        if not row_components <= components:
            raise SchemaRefusal("witness-floor row names an unknown component")
        if row["attached"] and row["comparable"]:
            covered[row["chair"]].update(row_components)
    counted = sorted(chair for chair, union in covered.items() if union == components)
    return {
        "configured_floor": floor,
        "counted_chairs": counted,
        "count": len(counted),
        "under_witnessed": len(counted) < floor,
    }


def capture_specific_recovery(
    *,
    logical_act_id: str,
    source_sha256: str,
    page_ordinal: int,
    ink_confirmed: bool,
    page_observation_grant_available: bool,
    act_budget_available: bool,
) -> dict[str, Any]:
    """Admit a recrop only on the observed capture and only through Unit 14B.

    Visibility can explain why a logical act needs another view; it can never
    fund recovery.  The Unit 14B ink observation on this exact source page is
    the conjunct that funds the bounded request.
    """
    if not isinstance(logical_act_id, str) or not logical_act_id or not is_sha256(source_sha256):
        raise SchemaRefusal("capture-specific recovery lacks logical-act/capture identity")
    if not isinstance(page_ordinal, int) or isinstance(page_ordinal, bool) or page_ordinal < 0:
        raise SchemaRefusal("capture-specific recovery has invalid page ordinal")
    if not all(
        isinstance(value, bool)
        for value in (ink_confirmed, page_observation_grant_available, act_budget_available)
    ):
        raise SchemaRefusal("capture-specific recovery gate is not boolean")
    admitted = ink_confirmed and page_observation_grant_available and act_budget_available
    missing = [
        label
        for available, label in (
            (ink_confirmed, "this capture has no Unit 14B ink-confirmed observation"),
            (page_observation_grant_available, "the page observation grant is unavailable"),
            (act_budget_available, "the act recovery budget is unavailable"),
        )
        if not available
    ]
    return {
        "logical_act_id": logical_act_id,
        "source_sha256": source_sha256,
        "page_ordinal": page_ordinal,
        "origin": "ink-confirmed-observation" if admitted else "not-admitted",
        "admitted": admitted,
        "reason": (
            "this capture's Unit 14B ink-confirmed observation and both bounded grants admit recovery"
            if admitted
            else "cross-capture visibility alone cannot fund recovery: " + "; ".join(missing)
        ),
    }
