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

_BACKGROUND = {
    "background_level": 220,
    "background_source": "inferred-modal",
    "dark_mode": 0,
    "ink_margin": 73,
    "contrast_below_background": 40,
    "ink_threshold": 180,
    "config_sha256": "0" * 64,
}


def _ink_record(artifact_id: str, ordinal, outcome="mapped", evidence=None) -> dict:
    return {
        "artifact_id": artifact_id,
        "outcome": outcome,
        "payload": {
            "page_ordinal": ordinal,
            "ink_measurable": True,
            "background": dict(_BACKGROUND),
            "ink": {},
            "edge": {},
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


class _Context(SimpleNamespace):
    def require_sealed_config(self, name, observed_sha256):
        expected = self.run["sealed_config_digests"].get(name)
        if expected != observed_sha256:
            raise ContractError(f"sealed {name} digest does not match the Ink Map payload")


def _context(records: dict[str, list[dict]], digest="0" * 64):
    return _Context(
        tree=_Tree(records), run={"sealed_config_digests": {"designator-grouping": digest}}
    )


SEALED_ONE = {1: {"outcome": "sealed"}}


def test_a_mapped_page_records_the_measurement_nobody_took_as_absence():
    """GOVERNANCE 10: `remeasured: None`, never a reassuring row of zeros."""
    armarium = _armarium()
    rows = armarium.ink_map_page_rows(_context({INK_MAP: [_ink_record("a", 1)]}), SEALED_ONE, {})
    assert rows == ({"ordinal": 1, "initial_outcome": "mapped", "remeasured": None},)


@pytest.mark.parametrize(
    "defect", ["base-era", "wrong-seal", "missing-background-field", "array-source"]
)
def test_a_measured_ink_map_payload_must_name_the_current_background_contract(defect):
    armarium = _armarium()
    record = _ink_record("a", 1)
    payload = record["payload"]
    if defect == "base-era":
        del payload["ink_measurable"]
        del payload["background"]
        del payload["ink"]
        del payload["edge"]
    elif defect == "wrong-seal":
        payload["background"] = {**_BACKGROUND, "config_sha256": "1" * 64}
    else:
        payload["background"] = dict(_BACKGROUND)
        if defect == "missing-background-field":
            del payload["background"]["ink_threshold"]
        else:
            payload["background"]["background_source"] = []
    with pytest.raises(FatalAccounting, match="invalid sealed measured payload"):
        armarium.ink_map_page_rows(_context({INK_MAP: [record]}), SEALED_ONE, {})


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


def test_an_unmeasurable_page_stays_in_the_denominator_and_can_never_be_held():
    """`ink-not-measurable`: a census row with no measurement and no evidence.

    The Ink Map publishes this when the shared background inference refuses a
    page's paper value: no threshold was cut, so there are no retained runs to
    re-measure and no counts a hold could be derived from. The row must still
    exist -- the page census is reconciled against these rows, and a missing one
    is refused as a denominator mismatch -- and it must carry `remeasured: None`
    for the same GOVERNANCE 10 reason a `mapped` page does, only more strongly:
    here nothing was measured at all.
    """
    armarium = _armarium()
    record = {
        "artifact_id": "a",
        "outcome": "ink-not-measurable",
        "payload": {
            "page_ordinal": 1,
            "ink_measurable": False,
            "background_refusal": "the page is majority ink",
            "background_config_sha256": "0" * 64,
        },
    }
    rows = armarium.ink_map_page_rows(_context({INK_MAP: [record]}), SEALED_ONE, {})
    assert rows == ({"ordinal": 1, "initial_outcome": "ink-not-measurable", "remeasured": None},)
    assert armarium.edge_hold_pages_from_rows(list(rows)) == ()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "page_ordinal": 1,
            "ink_measurable": False,
            "background_refusal": "",
            "background_config_sha256": "0" * 64,
        },
        {
            "page_ordinal": 1,
            "ink_measurable": False,
            "background_refusal": "refused",
            "background_config_sha256": "A" * 64,
        },
        {
            "page_ordinal": 1,
            "ink_measurable": False,
            "background_refusal": "refused",
            "background_config_sha256": "0" * 64,
            "edge_findings": _RUNS,
        },
        {
            "page_ordinal": True,
            "ink_measurable": False,
            "background_refusal": "refused",
            "background_config_sha256": "0" * 64,
        },
        {
            "page_ordinal": 1,
            "ink_measurable": False,
            "background_config_sha256": "0" * 64,
        },
        {
            "page_ordinal": 1,
            "ink_measurable": True,
            "background_refusal": "refused",
            "background_config_sha256": "0" * 64,
        },
    ],
    ids=[
        "blank-refusal",
        "uppercase-digest",
        "extra-measurement",
        "boolean-ordinal",
        "missing-refusal",
        "measurable-true",
    ],
)
def test_an_unmeasurable_ink_map_payload_must_be_closed_before_export(payload):
    armarium = _armarium()
    record = {"artifact_id": "a", "outcome": "ink-not-measurable", "payload": payload}
    expected = (
        "without an integer page ordinal"
        if payload["page_ordinal"] is True
        else "invalid sealed ink-not-measurable payload"
    )
    with pytest.raises(FatalAccounting, match=expected):
        armarium.ink_map_page_rows(_context({INK_MAP: [record]}), SEALED_ONE, {})


def test_an_unmeasurable_ink_map_payload_must_match_the_run_seal():
    armarium = _armarium()
    record = {
        "artifact_id": "a",
        "outcome": "ink-not-measurable",
        "payload": {
            "page_ordinal": 1,
            "ink_measurable": False,
            "background_refusal": "the page is majority ink",
            "background_config_sha256": "1" * 64,
        },
    }
    with pytest.raises(FatalAccounting, match="invalid sealed ink-not-measurable payload"):
        armarium.ink_map_page_rows(_context({INK_MAP: [record]}), SEALED_ONE, {})


def test_the_export_verifier_accepts_the_unmeasurable_row_and_refuses_a_measured_one():
    """The closed row shape knows the third outcome, and still refuses a claim.

    A row that says the page could not be measured and then carries a
    re-measurement is the same contradiction as a `mapped` page carrying one:
    a measurement nobody took, in the one file a clean-machine verifier reads.
    """
    # Bare, the way this stage's other tests import it: pytest's prepend import
    # mode puts this directory on `sys.path`, and a `spec_from_file_location`
    # load leaves the module unregistered, which its dataclasses cannot resolve.
    from armarium_export import _validate_ink_map_pages

    from common.contracts.errors import SchemaRefusal

    row = {"ordinal": 1, "initial_outcome": "ink-not-measurable", "remeasured": None}
    assert _validate_ink_map_pages([row], "subject") == [row]
    with pytest.raises(SchemaRefusal, match="re-measures an ink-map page its own map never"):
        _validate_ink_map_pages(
            [
                {
                    **row,
                    "remeasured": {
                        "total_ink_pixels": 1,
                        "outside_ink_pixels": 1,
                        "edge_band_pixels": 1,
                    },
                }
            ],
            "subject",
        )
