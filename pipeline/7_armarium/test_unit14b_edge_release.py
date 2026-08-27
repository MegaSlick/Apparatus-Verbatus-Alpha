"""The by-ink release boundary: what may release, and what refuses.

Release is derived from measured ink, so every re-measurement input must be
sealed run evidence. A crop that no longer verifies against its Exemplar page
cannot release anything, and duplicate page findings are refused because row
order cannot decide the hold.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.stages import DESIGNATOR, INK_MAP

ROOT = Path(__file__).resolve().parents[2]


def _armarium():
    spec = importlib.util.spec_from_file_location(
        "armarium_u14b_edge_release", ROOT / "pipeline/7_armarium/run.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


_RUNS = {"schema": "ink-runs.v1", "width": 40, "height": 2, "rows": [[], []]}
# 80 ink pixels, all of them outside any crop: past `MINIMUM_INK_PIXELS`
# and past the fraction gate, so the shared `coverage_flag` holds the page.
_FLAGGED_RUNS = {
    "schema": "ink-runs.v1",
    "width": 40,
    "height": 2,
    "rows": [[[0, 40]], [[0, 40]]],
}


def _ink_record(artifact_id: str, ordinal, outcome="mapped", evidence=None) -> dict:
    return {
        "artifact_id": artifact_id,
        "outcome": outcome,
        "payload": {
            "page_ordinal": ordinal,
            "edge_findings": _RUNS if evidence is None else evidence,
        },
    }


class _Tree:
    def __init__(self, records: dict[str, list[dict]]):
        self._records = records

    def build_manifest(self, stage):
        return {
            "artifacts": [
                {
                    "kind": "ink-map" if stage == INK_MAP else "region",
                    "artifact_id": record["artifact_id"],
                }
                for record in self._records.get(stage, [])
            ]
        }

    def read_artifact(self, stage, kind, artifact_id):
        return next(
            record for record in self._records[stage] if record["artifact_id"] == artifact_id
        )


def _context(records: dict[str, list[dict]]):
    return SimpleNamespace(tree=_Tree(records), run={})


SEALED_ONE = {1: {"outcome": "sealed"}}


def test_a_mapped_page_records_the_measurement_nobody_took_as_absence():
    """GOVERNANCE 10: `remeasured: None`, never a reassuring row of zeros."""
    armarium = _armarium()
    rows = armarium.ink_map_page_rows(_context({INK_MAP: [_ink_record("a", 1)]}), SEALED_ONE, {})
    assert rows == ({"ordinal": 1, "initial_outcome": "mapped", "remeasured": None},)


def test_a_flagged_page_is_re_measured_against_the_crops_actually_cut():
    """The release is the same measure the map made, over the real crop set."""
    armarium = _armarium()
    context = _context({INK_MAP: [_ink_record("a", 1, "unclaimed-edge-ink", _FLAGGED_RUNS)]})
    held = armarium.ink_map_page_rows(context, SEALED_ONE, {})
    assert held[0]["remeasured"]["outside_ink_pixels"] == 80
    assert armarium.edge_hold_pages_from_rows(list(held)) == (1,)

    released = armarium.ink_map_page_rows(
        context, SEALED_ONE, {1: [{"x": 0, "y": 0, "w": 40, "h": 2}]}
    )
    assert released[0]["remeasured"]["outside_ink_pixels"] == 0
    assert armarium.edge_hold_pages_from_rows(list(released)) == ()


def test_a_partial_claim_releases_nothing_it_did_not_actually_cover():
    """A crop over half the flagged ink leaves the rest outside, and held."""
    armarium = _armarium()
    context = _context({INK_MAP: [_ink_record("a", 1, "unclaimed-edge-ink", _FLAGGED_RUNS)]})
    rows = armarium.ink_map_page_rows(context, SEALED_ONE, {1: [{"x": 0, "y": 0, "w": 20, "h": 2}]})
    assert rows[0]["remeasured"]["outside_ink_pixels"] == 40
    assert armarium.edge_hold_pages_from_rows(list(rows)) == (1,)


def test_two_ink_map_records_for_one_page_are_refused_rather_than_resolved():
    """A duplicate in the settled inventory cannot make walk order decide the hold."""
    armarium = _armarium()
    context = _context({INK_MAP: [_ink_record("a", 1), _ink_record("b", 1)]})
    with pytest.raises(FatalAccounting, match="repeats page ordinal 1"):
        armarium.ink_map_page_rows(context, SEALED_ONE, {})


def test_an_ink_map_page_outside_the_sealed_census_is_refused():
    """The Ink Map and sealed page census must name the same page set."""
    armarium = _armarium()
    context = _context({INK_MAP: [_ink_record("a", 1), _ink_record("b", 2)]})
    with pytest.raises(FatalAccounting, match="denominator does not match"):
        armarium.ink_map_page_rows(context, SEALED_ONE, {})


def test_an_unknown_ink_map_outcome_is_refused_rather_than_read_as_mapped():
    armarium = _armarium()
    context = _context({INK_MAP: [_ink_record("a", 1, "some-later-outcome")]})
    with pytest.raises(FatalAccounting, match="unknown page finding outcome"):
        armarium.ink_map_page_rows(context, SEALED_ONE, {})


@pytest.mark.parametrize(
    ("outcome", "evidence", "measured"),
    [
        ("mapped", _FLAGGED_RUNS, "unclaimed-edge-ink"),
        ("unclaimed-edge-ink", _RUNS, "mapped"),
    ],
)
def test_an_ink_map_outcome_must_match_its_retained_ink_runs(outcome, evidence, measured):
    """A contradictory outcome cannot release or hold a page by assertion.

    The retained runs are the immutable page-space evidence introduced by this
    unit. Trusting ``mapped`` without reading them made malformed evidence and
    real flagging ink equally disappear from the export boundary.
    """
    armarium = _armarium()
    context = _context({INK_MAP: [_ink_record("a", 1, outcome, evidence)]})
    with pytest.raises(
        FatalAccounting,
        match=rf"records outcome {outcome!r}, but its retained.*measures {measured!r}",
    ):
        armarium.ink_map_page_rows(context, SEALED_ONE, {})


def test_a_mapped_page_with_unreadable_retained_runs_is_refused_by_name():
    """The clear outcome has the same evidence-validation duty as a hold."""
    armarium = _armarium()
    malformed = {"schema": "ink-runs.v1", "width": 40, "height": 2, "rows": [[]]}
    context = _context({INK_MAP: [_ink_record("a", 1, "mapped", malformed)]})
    with pytest.raises(
        FatalAccounting,
        match=(
            "unreadable retained page-space edge evidence.*"
            "cannot verify the page finding.*"
            "Restore the sealed Ink Map artifact"
        ),
    ):
        armarium.ink_map_page_rows(context, SEALED_ONE, {})


def test_a_page_ordinal_that_is_not_an_integer_is_refused():
    armarium = _armarium()
    context = _context({INK_MAP: [_ink_record("a", True)]})
    with pytest.raises(FatalAccounting, match="without an integer page ordinal"):
        armarium.ink_map_page_rows(context, SEALED_ONE, {})


def test_a_crop_that_no_longer_verifies_cannot_release_an_edge_finding(monkeypatch):
    """A stale crop is not evidence of coverage, whatever its recorded bounds.

    `claimed_bounds_by_page` is the ONLY source of the rectangles a release is
    measured against, and it re-verifies each one against the Exemplar page it
    claims to be a crop of. A region whose lineage no longer checks out would
    otherwise release a page on pixels nobody can prove were ever cut.
    """
    armarium = _armarium()
    region = {
        "artifact_id": "region-1",
        "subject_id": "act-1",
        "payload": {
            "transform": {"source_page_ordinal": 1, "bounds": {"x": 0, "y": 0, "w": 40, "h": 2}}
        },
    }
    context = _context({DESIGNATOR: [region]})

    monkeypatch.setattr(
        armarium,
        "verify_exemplar_crop_lineage",
        lambda *_args: {"source_page_ordinal": 1},
    )
    assert armarium.claimed_bounds_by_page(context, {}) == {1: [{"x": 0, "y": 0, "w": 40, "h": 2}]}

    def stale(*_args):
        raise ContractError("the crop bytes do not match the sealed page region")

    monkeypatch.setattr(armarium, "verify_exemplar_crop_lineage", stale)
    with pytest.raises(FatalAccounting, match="cannot be verified as a crop"):
        armarium.claimed_bounds_by_page(context, {})


def test_a_verified_region_with_no_bounds_is_refused_not_skipped(monkeypatch):
    """A region that verifies but states no rectangle releases nothing silently."""
    armarium = _armarium()
    region = {
        "artifact_id": "region-1",
        "subject_id": "act-1",
        "payload": {"transform": {"source_page_ordinal": 1}},
    }
    monkeypatch.setattr(
        armarium, "verify_exemplar_crop_lineage", lambda *_args: {"source_page_ordinal": 1}
    )
    with pytest.raises(FatalAccounting, match="no crop bounds"):
        armarium.claimed_bounds_by_page(_context({DESIGNATOR: [region]}), {})
