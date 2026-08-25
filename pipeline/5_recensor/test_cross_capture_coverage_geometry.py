"""Unit 19C: the coverage path is wired, and it claims only what it measured.

`common/cross_capture_coverage.py`'s three functions had zero production
callers (the 19B failure mode exactly -- see the audit at commit `0cdf290`).
This file drives the real production wiring in `pipeline/5_recensor/run.py`:
`act_cross_capture_coverage` reads real Designator region and occlusion
artifacts through the same `artifacts_for`/`_proposal_geometry_by_page`
helpers the rest of the stage already trusts, and feeds them to
`build_cross_capture_coverage` for real. Nothing here hand-builds a
`visible_cells`/`occluded_cells` dict directly, the way every prior 19C test
did (the Sonnet audit's own charge 3 finding) -- geometry comes from real
polygons and real proposal bounds, exactly as a live occlusion detector would
publish them.

The Opus audit round rewrote two of these cases and added six. Round 2's
wiring reached `visible`/`full` two ways the evidence does not support: from
a page with no occlusion artifact at all (consult §4.1, "absence of an
occlusion artifact is not proof of visibility until the producer seals a
complete survey"), and by unioning two captures' cells with no registration
mapping them into one frame (§4.1, "union visible masks only after mapping
each mask through sealed geometric alignment"). Both now reach a named
`unresolved`, and the cases below pin which cause each one records, what the
route does with it, and that the recovery gate consults none of it.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from common.contracts.errors import FatalAccounting

ROOT = Path(__file__).resolve().parents[2]
RECENSOR = ROOT / "pipeline/5_recensor/run.py"

A = "a" * 64
B = "b" * 64


def _recensor():
    spec = importlib.util.spec_from_file_location("recensor_u19c_geometry", RECENSOR)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _region_record(*, region_id, image_path, page_id, ordinal, bounds):
    return {
        "artifact_id": region_id,
        "payload": {
            "origin": "proposal",
            "region_id": region_id,
            "image_path": image_path,
            "image_sha256": "0" * 64,
            "transform": {
                "source_page_ordinal": ordinal,
                "source_page_id": page_id,
                "bounds": bounds,
            },
        },
    }


def _occlusion_record(*, page_id, polygon, z_relationship="above-ink"):
    return {
        "artifact_id": f"occ-{page_id}-{len(polygon)}",
        "payload": {
            "page_id": page_id,
            "polygon": polygon,
            "z_relationship": z_relationship,
        },
    }


def _rectangle(x0, y0, x1, y1):
    return [{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}]


class _FakeTree:
    """Just enough of `RunTree` for the Designator manifest reads this needs."""

    def __init__(self, artifacts):
        # artifacts: dict[artifact_id] -> (kind, subject_id, record)
        self._artifacts = artifacts

    def build_manifest(self, stage):
        return {
            "artifacts": [
                {"kind": kind, "subject_id": subject_id, "artifact_id": artifact_id}
                for artifact_id, (kind, subject_id, _record) in self._artifacts.items()
            ]
        }

    def read_artifact(self, stage, kind, artifact_id):
        return self._artifacts[artifact_id][2]


class _FakeContext:
    def __init__(self, artifacts):
        self.tree = _FakeTree(artifacts)


def _autopsia_dossier(*, logical_act_id, views):
    return {
        "dossier": {"logical_act_id": logical_act_id, "cross_capture_autopsia": {"views": views}}
    }


def _view(*, view_id, physical_page_id, source_sha256, page_id, local_act_id):
    return {
        "view_id": view_id,
        "physical_page_id": physical_page_id,
        "source_sha256": source_sha256,
        "page_ids": [page_id],
        "local_act_ids": [local_act_id],
        "alignment_ref": f"identity-alignment:{page_id}",
    }


def test_no_cross_capture_presentation_means_no_survey():
    module = _recensor()
    context = _FakeContext({})
    assert module.act_cross_capture_coverage(context, "act_x", {"dossier": {}}) is None
    assert module.act_cross_capture_coverage(context, "act_x", {}) is None


def test_occluded_everywhere_reaches_its_named_finding_on_a_real_singleton_survey():
    """One real capture, one real occlusion polygon wholly covering its own
    sealed proposal AABB: the survey must reach the exact `occluded-everywhere`
    finding from real geometry, not a hand-built cells dict."""
    module = _recensor()
    bounds = {"x": 10, "y": 10, "w": 40, "h": 40}
    context = _FakeContext(
        {
            "region_1": (
                "region",
                "act_x",
                _region_record(
                    region_id="region_1",
                    image_path="fixture/act_x.png",
                    page_id="pg_a",
                    ordinal=1,
                    bounds=bounds,
                ),
            ),
            "occ_1": (
                "occlusion",
                "occ_1",
                _occlusion_record(page_id="pg_a", polygon=_rectangle(0, 0, 60, 60)),
            ),
        }
    )
    latest_payload = _autopsia_dossier(
        logical_act_id="act_x",
        views=[
            _view(
                view_id="view_1",
                physical_page_id="ppg_local_pg_a",
                source_sha256=A,
                page_id="pg_a",
                local_act_id="act_x",
            )
        ],
    )
    result = module.act_cross_capture_coverage(context, "act_x", latest_payload)
    assert result["act_state"] == "occluded-everywhere"
    assert result["findings"] == [
        {"code": "occluded-everywhere", "physical_page_id": "ppg_local_pg_a"}
    ]


def test_no_occlusion_survey_is_unresolved_not_visible():
    """The Opus audit round's first finding: round 2 read an absent occlusion
    artifact as proof of visibility, and published `visible`/`full` for every
    act of every real run off geometry nobody surveyed. Consult §4.1 --
    "absence of an occlusion artifact is not proof of visibility until the
    producer seals a complete survey" -- and §11.3, "every such state is
    `unresolved`; it is never inferred visible from absence"."""
    module = _recensor()
    bounds = {"x": 0, "y": 0, "w": 20, "h": 20}
    context = _FakeContext(
        {
            "region_1": (
                "region",
                "act_x",
                _region_record(
                    region_id="region_1",
                    image_path="fixture/act_x.png",
                    page_id="pg_a",
                    ordinal=1,
                    bounds=bounds,
                ),
            )
        }
    )
    latest_payload = _autopsia_dossier(
        logical_act_id="act_x",
        views=[
            _view(
                view_id="view_1",
                physical_page_id="ppg_local_pg_a",
                source_sha256=A,
                page_id="pg_a",
                local_act_id="act_x",
            )
        ],
    )
    result = module.act_cross_capture_coverage(context, "act_x", latest_payload)
    assert result["act_state"] == "unresolved"
    (component,) = result["components"]
    (capture,) = component["captures"]
    assert capture["visibility_state"] == "unresolved"
    assert capture["visible_cells"] == []
    assert capture["finding_codes"] == [module.SURVEY_ABSENT]
    assert result["findings"] == [
        {"code": "capture-visibility-unresolved", "physical_page_id": "ppg_local_pg_a"}
    ]


def test_an_unsurveyed_act_is_recorded_but_does_not_hold_the_act():
    """The other half of the same finding. An unmeasured instrument is named
    in the record (GOVERNANCE 2) and routes like `False`, the way this stage's
    own `testimony_shortfall`/`audit_unresolved` already treat "no measurement
    exists". Holding every act of every run on a producer nobody has built
    would report the state of our tooling as a finding about the ink."""
    module = _recensor()
    coverage = {
        "act_state": "unresolved",
        "findings": [{"code": "capture-visibility-unresolved", "physical_page_id": "ppg_a"}],
        "components": [
            {
                "physical_page_id": "ppg_a",
                "union_state": "unresolved",
                "captures": [
                    {
                        "source_sha256": A,
                        "visibility_state": "unresolved",
                        "finding_codes": [module.SURVEY_ABSENT],
                    }
                ],
            }
        ],
    }
    assert module.cross_capture_review_causes(coverage) == (False, None)
    assert (
        module.review_route_from_findings(
            cross_capture_occluded_everywhere=False,
            cross_capture_unresolved=None,
            testimony_shortfall=False,
            audit_unresolved=False,
            under_witnessed=False,
        )
        is None
    )


def test_a_measured_gap_beside_an_unmeasured_capture_still_holds():
    """The strictness that keeps the clause above from becoming a hatch: one
    capture surveyed and short, one capture unmeasured, is a measured
    shortfall and holds. Only a component where nothing at all was measured
    routes like `False`."""
    module = _recensor()
    coverage = {
        "act_state": "unresolved",
        "findings": [{"code": "capture-visibility-unresolved", "physical_page_id": "ppg_a"}],
        "components": [
            {
                "physical_page_id": "ppg_a",
                "union_state": "unresolved",
                "captures": [
                    {
                        "source_sha256": A,
                        "visibility_state": "occluded",
                        "finding_codes": [],
                    },
                    {
                        "source_sha256": B,
                        "visibility_state": "unresolved",
                        "finding_codes": [module.SURVEY_ABSENT],
                    },
                ],
            }
        ],
    }
    assert module.cross_capture_review_causes(coverage) == (False, True)


def test_occluded_everywhere_on_one_component_is_named_even_beside_a_full_one():
    """A continuation whose second physical page is wholly occluded: the
    act-level state is `unresolved` (not every component is occluded), so
    routing off `act_state` alone -- round 2's shape -- would hold the act
    without ever saying the exact thing that was measured."""
    module = _recensor()
    coverage = {
        "act_state": "unresolved",
        "findings": [{"code": "occluded-everywhere", "physical_page_id": "ppg_b"}],
        "components": [
            {
                "physical_page_id": "ppg_a",
                "union_state": "full",
                "captures": [
                    {"source_sha256": A, "visibility_state": "visible", "finding_codes": []}
                ],
            },
            {
                "physical_page_id": "ppg_b",
                "union_state": "occluded-everywhere",
                "captures": [
                    {"source_sha256": B, "visibility_state": "occluded", "finding_codes": []}
                ],
            },
        ],
    }
    assert module.cross_capture_review_causes(coverage) == (True, False)
    route = module.review_route_from_findings(
        cross_capture_occluded_everywhere=True,
        cross_capture_unresolved=False,
        testimony_shortfall=False,
        audit_unresolved=False,
        under_witnessed=False,
    )
    assert route is not None and "occluded" in route[1]


def test_two_captures_of_one_physical_page_cannot_union_without_a_registration():
    """The Opus audit round's second wiring finding. Round 2 unioned two
    captures' cells and called the result `full`, but those cells are
    normalized to each capture's own act footprint
    (`common/act_visibility_geometry.py`) and nothing in this repository maps
    them into one frame -- consult §4.1 admits a union only over masks "mapped
    through sealed geometric alignment". The case that exposes it is the very
    case the union exists for: two captures each showing a different half of
    one act would both report their own 16 cells visible and union to `full`
    without either having shown the other half.

    Both captures here are genuinely surveyed -- one occluded, one clean -- so
    the cause recorded is the missing registration and nothing else."""
    module = _recensor()
    context = _FakeContext(
        {
            "region_a": (
                "region",
                "act_a",
                _region_record(
                    region_id="region_a",
                    image_path="fixture/act_a.png",
                    page_id="pg_a",
                    ordinal=1,
                    bounds={"x": 8, "y": 12, "w": 80, "h": 24},
                ),
            ),
            "region_b": (
                "region",
                "act_b",
                _region_record(
                    region_id="region_b",
                    image_path="fixture/act_b.png",
                    page_id="pg_b",
                    ordinal=2,
                    bounds={"x": 10, "y": 11, "w": 80, "h": 24},
                ),
            ),
            "occ_a": (
                "occlusion",
                "occ_a",
                _occlusion_record(page_id="pg_a", polygon=_rectangle(0, 0, 200, 200)),
            ),
            "occ_b": (
                "occlusion",
                "occ_b",
                _occlusion_record(
                    page_id="pg_b", polygon=_rectangle(0, 0, 4, 4), z_relationship="below-ink"
                ),
            ),
        }
    )
    views = [
        _view(
            view_id="view_a",
            physical_page_id="ppg_shared",
            source_sha256=A,
            page_id="pg_a",
            local_act_id="act_a",
        ),
        _view(
            view_id="view_b",
            physical_page_id="ppg_shared",
            source_sha256=B,
            page_id="pg_b",
            local_act_id="act_b",
        ),
    ]
    latest_payload = _autopsia_dossier(logical_act_id="pac_shared", views=views)

    result = module.act_cross_capture_coverage(context, "act_a", latest_payload)
    assert result["act_state"] == "unresolved"
    (component,) = result["components"]
    assert component["union_state"] == "unresolved"
    assert component["union_visible_cells"] == []
    for row in component["captures"]:
        assert row["visibility_state"] == "unresolved"
        assert row["finding_codes"] == [module.REGISTRATION_ABSENT]
    assert result["findings"] == [
        {"code": "capture-visibility-unresolved", "physical_page_id": "ppg_shared"}
    ]
    # Nothing was measured in one frame, so the act holds on a real cause
    # rather than routing like an absent instrument.
    assert module.cross_capture_review_causes(result) == (False, None)


def test_an_occluded_row_carries_the_occlusion_records_it_rests_on():
    """GOALS 5 and consult §4.1: the finding "records the component, all
    capture digests, all occlusion refs". Round 2 published `occlusion_refs:
    []` beside a real occlusion claim -- a claim with its evidence detached."""
    module = _recensor()
    context = _FakeContext(
        {
            "region_1": (
                "region",
                "act_x",
                _region_record(
                    region_id="region_1",
                    image_path="fixture/act_x.png",
                    page_id="pg_a",
                    ordinal=1,
                    bounds={"x": 10, "y": 10, "w": 40, "h": 40},
                ),
            ),
            "occ_1": (
                "occlusion",
                "occ_1",
                _occlusion_record(page_id="pg_a", polygon=_rectangle(0, 0, 60, 60)),
            ),
        }
    )
    latest_payload = _autopsia_dossier(
        logical_act_id="act_x",
        views=[
            _view(
                view_id="view_1",
                physical_page_id="ppg_local_pg_a",
                source_sha256=A,
                page_id="pg_a",
                local_act_id="act_x",
            )
        ],
    )
    result = module.act_cross_capture_coverage(context, "act_x", latest_payload)
    (component,) = result["components"]
    (capture,) = component["captures"]
    assert capture["occlusion_refs"] == ["occ-pg_a-4"]


def test_a_component_cannot_take_one_members_expected_surface_for_the_others():
    """Consult §7.14/§4.1: the extent of a component is not a choice between
    its members. Round 2's `setdefault` kept whichever view was surveyed first
    and dropped the rest without a collision check; the surfaces agree today
    only because the grid is a constant."""
    module = _recensor()
    context = _FakeContext(
        {
            "region_a": (
                "region",
                "act_a",
                _region_record(
                    region_id="region_a",
                    image_path="fixture/act_a.png",
                    page_id="pg_a",
                    ordinal=1,
                    bounds={"x": 0, "y": 0, "w": 20, "h": 20},
                ),
            ),
            "region_b": (
                "region",
                "act_b",
                _region_record(
                    region_id="region_b",
                    image_path="fixture/act_b.png",
                    page_id="pg_b",
                    ordinal=2,
                    bounds={"x": 0, "y": 0, "w": 20, "h": 20},
                ),
            ),
        }
    )
    views = [
        _view(
            view_id="view_a",
            physical_page_id="ppg_shared",
            source_sha256=A,
            page_id="pg_a",
            local_act_id="act_a",
        ),
        _view(
            view_id="view_b",
            physical_page_id="ppg_shared",
            source_sha256=B,
            page_id="pg_b",
            local_act_id="act_b",
        ),
    ]
    surfaces = iter([[[0, 0]], [[0, 0], [1, 0]]])
    module.expected_surface_cells = lambda *args, **kwargs: next(surfaces)
    with pytest.raises(FatalAccounting, match="two different expected surfaces"):
        module.act_cross_capture_coverage(
            context, "act_a", _autopsia_dossier(logical_act_id="pac_shared", views=views)
        )


def test_the_recovery_gate_consults_no_cross_capture_fact():
    """Charge 2, structurally. Consult §4.2 rule 3 keeps an ink-confirmed
    recovery gated by the §4.5 conjuncts "exactly as Unit 14B landed"; round 2
    added `and not cross_capture_occluded_everywhere` to the gate, which let
    union geometry veto a recovery Unit 9 had already funded from ink measured
    in the page's own pixels. §7.20 forbids occlusion *funding* a reroll --
    which it never could, `wants_recovery` is a declared recrop or measured
    ink -- not occlusion vetoing one."""
    source = RECENSOR.read_text(encoding="utf-8")
    tree = ast.parse(source)
    gates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(call, ast.Call)
            and any(
                keyword.arg == "kind"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "recovery-request"
                for keyword in call.keywords
            )
            for call in ast.walk(node)
        )
    ]
    assert gates, "the recovery-request gate was not found; this guard scans nothing"
    innermost = min(gates, key=lambda node: len(list(ast.walk(node))))
    names = {child.id for child in ast.walk(innermost.test) if isinstance(child, ast.Name)}
    assert not {name for name in names if "cross_capture" in name or "occlu" in name}, (
        "union geometry is back in the recovery gate"
    )
    # And the live gate itself, driven the way `test_unit14b_trigger_contract.
    # py` drives `wants_recovery`: with every §4.5 conjunct satisfied it
    # admits, so no conjunct beyond them can be quietly holding it shut.
    admitted = eval(  # noqa: S307 -- the compiled input is this repository's own gate.
        compile(ast.Expression(innermost.test), str(RECENSOR), "eval"),
        {},
        {
            "continuation_shortfall": False,
            "wants_recovery": True,
            "used_fallback": 0,
            "allowed_fallback": 1,
            "used_total": 0,
            "budget": {"allowed": 1, "absolute_cap": 3},
        },
    )
    assert admitted is True


def test_a_below_ink_occlusion_does_not_occlude_the_real_survey():
    """z_relationship positively proving the occluder sits behind the ink:
    the one relationship the adapter must NOT treat as occluding."""
    module = _recensor()
    context = _FakeContext(
        {
            "region_1": (
                "region",
                "act_x",
                _region_record(
                    region_id="region_1",
                    image_path="fixture/act_x.png",
                    page_id="pg_a",
                    ordinal=1,
                    bounds={"x": 0, "y": 0, "w": 20, "h": 20},
                ),
            ),
            "occ_1": (
                "occlusion",
                "occ_1",
                _occlusion_record(
                    page_id="pg_a", polygon=_rectangle(0, 0, 40, 40), z_relationship="below-ink"
                ),
            ),
        }
    )
    latest_payload = _autopsia_dossier(
        logical_act_id="act_x",
        views=[
            _view(
                view_id="view_1",
                physical_page_id="ppg_local_pg_a",
                source_sha256=A,
                page_id="pg_a",
                local_act_id="act_x",
            )
        ],
    )
    result = module.act_cross_capture_coverage(context, "act_x", latest_payload)
    assert result["act_state"] == "full"


def test_an_act_absent_from_its_own_readings_autopsia_refuses():
    module = _recensor()
    context = _FakeContext({})
    latest_payload = _autopsia_dossier(
        logical_act_id="act_x",
        views=[
            _view(
                view_id="view_1",
                physical_page_id="ppg_local_pg_a",
                source_sha256=A,
                page_id="pg_a",
                local_act_id="act_other",
            )
        ],
    )
    with pytest.raises(FatalAccounting, match="does not name it"):
        module.act_cross_capture_coverage(context, "act_x", latest_payload)


def test_the_coverage_and_witness_floor_functions_have_real_production_callers():
    """Pins the audit's own charge: zero production callers was the 19B
    failure mode repeated. Both must now be called from `run.py` itself, not
    only from a test file."""
    source = RECENSOR.read_text(encoding="utf-8")
    for name in (
        "build_cross_capture_coverage",
        "same_chair_witness_floor",
        "capture_specific_recovery",
    ):
        # More than the one import-time mention: a real call site exists.
        assert source.count(name) >= 2, f"{name} has no production caller in run.py"
