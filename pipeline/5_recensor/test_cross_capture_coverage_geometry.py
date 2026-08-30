"""Exercise Unit 19C through real proposal bounds and occlusion polygons.

Artifact absence cannot prove visibility, and capture-local grids cannot be
unioned without a sealed registration into one coordinate frame. These tests
pin both refusals, their review routing, and their exclusion from the recovery
gate.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from common.contracts.errors import FatalAccounting
from common.contracts.stages import DESIGNATOR
from common.cross_capture_autopsia import build_autopsia

ROOT = Path(__file__).resolve().parents[2]
RECENSOR = ROOT / "pipeline/5_recensor/run.py"
# How many recovery-request gates run.py contains. Pinned so a new origin
# cannot appear without a seat reading it against the ink-evidence rule.
EXPECTED_RECOVERY_GATES = 1

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
        # Keyed by the polygon itself, not its point count. Two occluders over
        # one page with the same number of points collapsed into one entry in
        # the artifacts dict, so a pooled-occlusion case would silently lose a
        # polygon and record coverage the geometry never supported.
        "artifact_id": f"occ-{page_id}-{'-'.join(f'{p["x"]}.{p["y"]}' for p in polygon)}",
        "payload": {
            "page_id": page_id,
            "polygon": polygon,
            "z_relationship": z_relationship,
        },
    }


def _rectangle(x0, y0, x1, y1):
    return [{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}]


# Which stage actually holds each kind in a run. The survey reads both from the
# Designator, and a fake that answered every stage would let the survey start
# asking the Perlector without a single test noticing.
_STAGE_OF = {"region": DESIGNATOR, "occlusion": DESIGNATOR}


class _FakeTree:
    """A stand-in that answers only the stage and kind it was actually given.

    The permissive version ignored both arguments and resolved on artifact_id
    alone. That made the whole suite blind to the one regression it exists to
    catch: had the survey asked the Perlector for regions, or read an occlusion
    record under kind "region", every test here would still have passed while a
    real parish run found no expected surface and measured the act against a
    denominator that does not exist.
    """

    def __init__(self, artifacts):
        self._artifacts = artifacts

    def build_manifest(self, stage):
        return {
            "artifacts": [
                {"kind": kind, "subject_id": subject_id, "artifact_id": artifact_id}
                for artifact_id, (kind, subject_id, _record) in self._artifacts.items()
                if _STAGE_OF[kind] == stage
            ]
        }

    def read_artifact(self, stage, kind, artifact_id):
        held_kind, _subject, record = self._artifacts[artifact_id]
        if kind != held_kind:
            raise AssertionError(
                f"the survey read {artifact_id!r} as kind {kind!r}; it is a {held_kind!r}"
            )
        if stage != _STAGE_OF[held_kind]:
            raise AssertionError(
                f"the survey read {artifact_id!r} from stage {stage!r}; it lives in "
                f"{_STAGE_OF[held_kind]!r}"
            )
        return record


class _FakeContext:
    def __init__(self, artifacts):
        self.tree = _FakeTree(artifacts)


def _autopsia_dossier(*, logical_act_id, views):
    autopsia = build_autopsia(
        logical_act_id=logical_act_id,
        partition_ref={"relative_path": "partition.json", "sha256": "0" * 64},
        required_capture_sha256s=[view["source_sha256"] for view in views],
        views=views,
    )
    return {"dossier": {"logical_act_id": logical_act_id, "cross_capture_autopsia": autopsia}}


def _view(*, view_id, physical_page_id, source_sha256, page_id, local_act_id):
    image_ref = {"relative_path": f"fixture/{view_id}.png", "sha256": "0" * 64}
    return {
        "view_id": view_id,
        "physical_page_id": physical_page_id,
        "source_sha256": source_sha256,
        "page_ids": [page_id],
        "local_act_ids": [local_act_id],
        "region_refs": [image_ref],
        "page_render_refs": [image_ref],
        "alignment_ref": f"identity-alignment:{page_id}",
        "visibility_evidence_refs": [image_ref],
    }


def test_no_cross_capture_presentation_means_no_survey():
    module = _recensor()
    context = _FakeContext({})
    assert module.act_cross_capture_coverage(context, "act_x", {"dossier": {}}) is None
    assert module.act_cross_capture_coverage(context, "act_x", {}) is None


def test_occluded_everywhere_reaches_its_named_finding_on_a_real_singleton_survey():
    """The finding must be derived from sealed geometry, not fixture cells."""
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
    """An absent survey is no evidence of a visible surface."""
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
    """Instrument absence is recorded without becoming a finding about ink."""
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
    """One measured gap makes the component a real shortfall."""
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
    """A component finding must survive a less-specific aggregate state."""
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
    """Capture-local cells are incomparable without a sealed registration."""
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
    # Nothing was measured in one shared frame, so the missing registration is
    # recorded as an absent instrument rather than restated as a measured gap.
    assert module.cross_capture_review_causes(result) == (False, None)


def test_an_occluded_row_carries_the_occlusion_records_it_rests_on():
    """An occlusion claim must retain every supporting artifact reference."""
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
    # Taken from the record the fake actually holds, so the assertion pins that
    # the row carries its occluder's own identity rather than the fake's id
    # spelling. The row must name the polygon it rests on; which string that
    # polygon is keyed by here is this file's business, not the contract's.
    expected_ref = _occlusion_record(page_id="pg_a", polygon=_rectangle(0, 0, 60, 60))[
        "artifact_id"
    ]
    assert capture["occlusion_refs"] == [expected_ref]


def test_a_component_cannot_take_one_members_expected_surface_for_the_others(monkeypatch):
    """No member may silently supply another member's denominator.

    The stand-in surface goes on through monkeypatch so pytest owns its
    removal. Assigning onto the module is safe only while `_recensor()` reloads
    run.py for every test; the moment anyone makes it a module- or
    session-scoped fixture to cut the repeated loads, every later test in this
    file would run against a survey whose expected surface comes from an
    exhausted two-item iterator, failing with StopIteration rather than
    reporting anything about coverage.
    """
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
    monkeypatch.setattr(module, "expected_surface_cells", lambda *args, **kwargs: next(surfaces))
    with pytest.raises(FatalAccounting, match="two different expected surfaces"):
        module.act_cross_capture_coverage(
            context, "act_a", _autopsia_dossier(logical_act_id="pac_shared", views=views)
        )


def test_the_recovery_gate_consults_no_cross_capture_fact():
    """Only Unit 14B ink evidence and bounded grants gate recovery."""
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
    # Screen every gate, not the innermost one. run.py carries one today, and
    # the count is pinned so a second cannot arrive unscreened: reducing the set
    # to one node by size let the assertion pass while another origin funded a
    # bounded recrop from union geometry, spending a recovery round because some
    # other capture happened to see the surface with no Unit 14B ink evidence
    # behind it. That waste has been recorded in this repository once already.
    assert len(gates) == EXPECTED_RECOVERY_GATES, (
        f"run.py now has {len(gates)} recovery-request gates, not "
        f"{EXPECTED_RECOVERY_GATES}; screen the new one here before raising this"
    )
    for gate in gates:
        # Bare names are not the only way in. `ast.Name` misses an attribute
        # read like `coverage.cross_capture_state` and misses a dict key like
        # `result["cross_capture_coverage"]` -- and the subscript idiom is used
        # inside this very gate, whose eval environment below supplies `budget`
        # as a dict. A regression reaching union geometry that way would have
        # passed the screen.
        names = set()
        for child in ast.walk(gate.test):
            if isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Attribute):
                names.add(child.attr)
            elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                names.add(child.value)
        assert not {name for name in names if "cross_capture" in name or "occlu" in name}, (
            f"union geometry is back in the recovery gate at line {gate.lineno}"
        )
    innermost = min(gates, key=lambda node: len(list(ast.walk(node))))
    # Satisfying every permitted conjunct must admit the live gate.
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


@pytest.mark.parametrize(
    ("polygon", "z_relationship", "message"),
    [
        ([{"x": 0, "y": 0}] * 3, "above-ink", "malformed polygon"),
        (_rectangle(0, 0, 40, 40), "in-front-ish", "unknown z_relationship"),
        (
            _rectangle(0, 0, 40, 40) * 300,  # far past MAX_POLYGON_POINTS
            "above-ink",
            "malformed polygon",
        ),
    ],
)
def test_malformed_sealed_occlusion_facts_are_named_accounting_refusals(
    polygon, z_relationship, message
):
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
                _occlusion_record(page_id="pg_a", polygon=polygon, z_relationship=z_relationship),
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
    with pytest.raises(FatalAccounting, match=message):
        module.act_cross_capture_coverage(context, "act_x", latest_payload)


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


def test_dossier_and_autopsia_cannot_name_different_logical_acts():
    module = _recensor()
    context = _FakeContext({})
    latest_payload = _autopsia_dossier(
        logical_act_id="pac_sealed",
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
    latest_payload["dossier"]["logical_act_id"] = "pac_other"
    with pytest.raises(FatalAccounting, match="refuses to attribute"):
        module.act_cross_capture_coverage(context, "act_x", latest_payload)


def test_the_coverage_and_witness_floor_functions_have_real_production_callers():
    """A tested contract with no production caller does not prove the pipeline.

    Counted as raw text, the import statement alone was one occurrence and any
    mention in a nearby comment was the second, so the assertion passed with no
    call site in the file at all. The failure that permitted is the one this
    test is named for: `build_cross_capture_coverage` stops being invoked during
    a real run, every Recensor review records the instrument as absent rather
    than measured, and an act occluded in one capture and readable in another is
    never reported as recoverable -- while the rest of this suite stays green,
    because it calls the function directly instead of through run.py.
    """
    tree = ast.parse(RECENSOR.read_text(encoding="utf-8"))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    for name in (
        "build_cross_capture_coverage",
        "same_chair_witness_floor",
        "capture_specific_recovery",
    ):
        assert name in called, f"{name} has no production call site in run.py"


def test_a_view_spanning_two_pages_is_unmeasured_not_measured_on_a_merged_grid():
    """A continuation act's verdict may not come from a rectangle no page has.

    One capture may render two pages: `logical_reading.act_autopsia` groups
    every touched page by capture, and its own suite proves a single view with
    `page_ids == ["pg_1", "pg_2"]`. The survey used to take one bounding box
    over both pages' proposal geometry and pool both pages' occlusion polygons
    against it, so an occlusion on page two was classified into cells whose
    coordinates belong to page one -- and that verdict is read by
    `cross_capture_review_causes`, which can hold an act with a stated reason
    drawn from a measurement of a surface no camera saw. Continuation acts are
    ordinary in these registers.

    The honest record is that the instrument did not measure this view, which
    is the shape the unresolved state already carries.
    """
    module = _recensor()
    bounds = {"x": 0, "y": 0, "w": 20, "h": 20}
    view = _view(
        view_id="view_1",
        physical_page_id="ppg_local_pg_a",
        source_sha256=A,
        page_id="pg_a",
        local_act_id="act_x",
    )
    view["page_ids"] = ["pg_a", "pg_b"]
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
            # Both pages are genuinely surveyed, so this is not the
            # absent-survey path: page one carries a proven below-ink polygon
            # that occludes nothing, and page two a real occlusion. Pooled onto
            # one grid, page two's polygon would have been reported as covering
            # page one's cells and the act called occluded-everywhere.
            "occ_a": (
                "occlusion",
                "occ_a",
                _occlusion_record(
                    page_id="pg_a", polygon=_rectangle(0, 0, 2, 2), z_relationship="below-ink"
                ),
            ),
            "occ_b": (
                "occlusion",
                "occ_b",
                _occlusion_record(page_id="pg_b", polygon=_rectangle(0, 0, 40, 40)),
            ),
        }
    )
    latest_payload = _autopsia_dossier(logical_act_id="act_x", views=[view])
    result = module.act_cross_capture_coverage(context, "act_x", latest_payload)

    (component,) = result["components"]
    (capture,) = component["captures"]
    assert capture["visibility_state"] == "unresolved"
    assert capture["finding_codes"] == [module.SURVEY_SPANS_TWO_PAGES]
    # Unmeasured means unmeasured: no cell may be claimed either way.
    assert capture["visible_cells"] == []
    assert capture["occluded_cells"] == []
    # And it is an absent instrument, not a measured shortfall, so it does not
    # become an occluded-everywhere hold on evidence that was never taken.
    assert set(capture["finding_codes"]) <= module.INSTRUMENT_ABSENT_CODES
    # Pinned to the honest state, not merely away from the wrong one. An
    # inequality here would also admit "full", which is the worse outcome: the
    # act would be recorded as completely covered, no review raised, and the
    # occluded half of a continuation act never recovered.
    (component_row,) = result["components"]
    assert component_row["union_state"] == "unresolved"
    assert result["act_state"] == "unresolved"
