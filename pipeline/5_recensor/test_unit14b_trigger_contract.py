"""The request gate admits independently measured ink, never witness preference.

A native or derived witness box is a pointer, not coverage evidence. These
tests bind the live request expression to ink-confirmed observations, exercise
the measurement arithmetic, and require one page-scoped observation to fund at
most one act-scoped recovery request.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from common.contracts.errors import FatalAccounting
from common.residual_ink import MINIMUM_INK_PIXELS

ROOT = Path(__file__).resolve().parents[2]
RECENSOR = ROOT / "pipeline/5_recensor/run.py"


def _recensor():
    spec = importlib.util.spec_from_file_location("recensor_u14b_trigger", RECENSOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _tree(source: str | None = None) -> ast.Module:
    return ast.parse((RECENSOR.read_text(encoding="utf-8") if source is None else source))


def _names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _live_recovery_trigger(source: str | None = None):
    """Compile the one expression that decides whether recovery is wanted.

    This is the real request-path expression, read from ``run.py`` rather than
    a duplicate predicate in the test.  The publication guard below proves
    that its result is what reaches a recovery-request artifact.
    """
    tree = _tree(source)
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "wants_recovery"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1, "the Recensor no longer has one identifiable recovery trigger"
    expression = assignments[0].value

    publications = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if any(
            keyword.arg == "kind"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == "recovery-request"
            for keyword in node.keywords
        ):
            publications.append(node)
    assert len(publications) == 1, "Unit 14B must have exactly one recovery-request publication"

    guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            publications[0] is child for statement in node.body for child in ast.walk(statement)
        )
    ]
    assert any("wants_recovery" in _names(guard.test) for guard in guards), (
        "the live recovery trigger no longer guards the recovery-request publication"
    )

    names = _names(expression)
    assert "outside_ink_requests" in names, (
        "wants_recovery no longer reads ink-confirmed observations -- missing ink evidence check"
    )
    assert "content_coverage" not in names, (
        "wants_recovery reads the raw witness-reported dict directly -- missing ink evidence check"
    )
    assert "funded_pages" in names, (
        "wants_recovery no longer bounds the observation route to one request per page"
    )

    return compile(ast.Expression(expression), str(RECENSOR), "eval")


def _wants_recovery(
    outside_ink_requests: list,
    *,
    source: str | None = None,
    page_ordinal: int = 1,
    funded_pages: set[int] | None = None,
) -> bool:
    return bool(
        eval(  # noqa: S307 -- compile input is this checked-in module's one expression.
            _live_recovery_trigger(source),
            {"bool": bool},
            {
                "act_key": "a",
                "act": {"page_ordinal": page_ordinal},
                "scenario": {"recover_acts": []},
                "outside_ink_requests": outside_ink_requests,
                "funded_pages": set(funded_pages or ()),
                "used_total": 0,
            },
        )
    )


def test_wants_recovery_is_inert_without_an_ink_confirmed_observation():
    """The structural wiring: no ink-confirmed request, no recovery wanted."""
    assert _wants_recovery([]) is False
    assert _wants_recovery([{"page_ordinal": 1, "outside_ink_pixels": 40}]) is True


def test_a_bypass_of_ink_confirmation_is_caught_by_this_files_own_guard():
    """The structural guard rejects a request predicate based on raw witness data."""
    source = RECENSOR.read_text(encoding="utf-8")
    live = "bool(outside_ink_requests)"
    assert source.count(live) == 1, "the test no longer identifies the live coverage origin"
    mixed_source = source.replace(live, 'bool(content_coverage.get("unclaimed_observations"))')
    with pytest.raises(AssertionError, match="missing ink evidence check"):
        _live_recovery_trigger(mixed_source)


class _FakeTree:
    def __init__(self, maps_by_ordinal: dict[int, dict]):
        self._maps = maps_by_ordinal

    def build_manifest(self, stage):
        return {
            "artifacts": [
                {"kind": "ink-map", "artifact_id": f"page-{ordinal}"} for ordinal in self._maps
            ]
        }

    def read_artifact(self, stage, kind, artifact_id):
        ordinal = int(artifact_id.split("-")[1])
        return {"payload": {"page_ordinal": ordinal, "edge_findings": self._maps[ordinal]}}


class _FakeContext:
    def __init__(self, maps_by_ordinal: dict[int, dict]):
        self.tree = _FakeTree(maps_by_ordinal)


def _ink_map(width: int, height: int, ink_boxes: list[dict]) -> dict:
    rows: list[list[list[int]]] = []
    for y in range(height):
        runs = sorted(
            (box["x"], box["w"]) for box in ink_boxes if box["y"] <= y < box["y"] + box["h"]
        )
        rows.append([[x, w] for x, w in runs])
    return {"schema": "ink-runs.v1", "width": width, "height": height, "rows": rows}


@pytest.mark.parametrize(
    "forbidden",
    [
        {"agreement": "3-of-3"},
        {"iou": 1.0, "similarity": 1.0},
        {"delta": 999},
        {"chair_weight": 99},
        {"disagrees_with": 2},
    ],
    ids=["n-of-m", "iou-similarity", "delta-magnitude", "chair-weight", "two-chair"],
)
def test_each_forbidden_witness_trigger_cannot_request_recovery_even_with_ink(forbidden):
    """Every named witness-preference signal is inert -- ink is what decides.

    Each observation carries a forbidden field a picker would key off, sitting
    over a page with NO ink under its own box, and over a page WITH ink under
    its own box. Only the ink presence changes the result; the forbidden field
    never does, whichever way it points.
    """
    recensor = _recensor()
    box = {"x": 0, "y": 0, "w": 5, "h": 5}
    observation = {"kind": "unrouted-observation", "bounds": box, **forbidden}

    empty_maps = recensor.ink_map_by_page(_FakeContext({1: _ink_map(20, 20, [])}))
    assert recensor.unclaimed_ink_observations(empty_maps, [observation], 1, {}) == []

    inked_maps = recensor.ink_map_by_page(_FakeContext({1: _ink_map(20, 20, [box])}))
    result = recensor.unclaimed_ink_observations(inked_maps, [observation], 1, {})
    assert result == [{"page_ordinal": 1, "outside_ink_pixels": 25}]


def test_a_two_chair_disagreement_is_refused_through_the_real_gate_by_hand():
    """Reconstruct the two-chair case end to end: geometry alone never wins.

    Both boxes sit outside every proposal but contain no measured ink, so their
    disagreement cannot authorize recovery.
    """
    recensor = _recensor()
    chair_1_box = {"x": 0, "y": 0, "w": 4, "h": 4}
    chair_2_box = {"x": 6, "y": 0, "w": 4, "h": 4}
    observations = [
        {"kind": "unrouted-observation", "bounds": chair_1_box, "disagrees_with": 2},
        {"kind": "unrouted-observation", "bounds": chair_2_box, "disagrees_with": 1},
    ]
    maps = recensor.ink_map_by_page(_FakeContext({1: _ink_map(20, 20, [])}))
    outside_ink_requests = recensor.unclaimed_ink_observations(maps, observations, 1, {})
    assert outside_ink_requests == []
    assert _wants_recovery(outside_ink_requests) is False


def test_ink_below_the_minimum_pixel_floor_still_refuses():
    """A pointer with a trace of ink, short of `MINIMUM_INK_PIXELS`, is not evidence."""
    recensor = _recensor()
    # One pixel short of the floor, derived rather than written out: a changed
    # floor must move this box with it, not silently invert what the test proves.
    box = {"x": 0, "y": 0, "w": MINIMUM_INK_PIXELS - 1, "h": 1}
    observation = {"kind": "unrouted-observation", "bounds": box}
    maps = recensor.ink_map_by_page(_FakeContext({1: _ink_map(MINIMUM_INK_PIXELS, 20, [box])}))
    assert recensor.unclaimed_ink_observations(maps, [observation], 1, {}) == []


def test_a_box_wholly_above_the_page_cannot_claim_ink_through_a_negative_slice():
    """Out-of-page geometry contains no page pixels and funds no recovery.

    Clipping only the near edge turned y=-10..-5 into ``rows[0:-5]``, which
    Python reads as nearly the whole page. A full ink map then made a witness
    pointer that touches no page pixel clear the recovery threshold.
    """
    recensor = _recensor()
    page = {"x": 0, "y": 0, "w": 40, "h": 40}
    maps = recensor.ink_map_by_page(_FakeContext({1: _ink_map(40, 40, [page])}))
    observation = {
        "kind": "unrouted-observation",
        "bounds": {"x": 0, "y": -10, "w": 40, "h": 5},
    }
    assert recensor.unclaimed_ink_observations(maps, [observation], 1, {}) == []


def test_ink_already_inside_a_cut_region_is_not_an_outside_part():
    """The live mask includes recovery crops, not only original proposals.

    Observations are retained against the proposal set present when they were
    recorded, so one may overlap a later recovery crop. Ink in that overlap is
    already covered and cannot fund another recovery.
    """
    recensor = _recensor()
    box = {"x": 0, "y": 0, "w": 10, "h": 10}  # 100 px, well past MINIMUM_INK_PIXELS
    observation = {"kind": "unrouted-observation", "bounds": box}
    maps = recensor.ink_map_by_page(_FakeContext({1: _ink_map(20, 20, [box])}))

    assert recensor.unclaimed_ink_observations(maps, [observation], 1, {}) == [
        {"page_ordinal": 1, "outside_ink_pixels": 100}
    ]
    cut = {1: [{"x": 0, "y": 0, "w": 20, "h": 20}]}
    assert recensor.unclaimed_ink_observations(maps, [observation], 1, cut) == []
    # These two straddle the floor: 20 px is under it and 30 px is over. The
    # geometry is written out because whole rows of a 10-wide box read more
    # plainly than an expression, so the straddle is asserted against the
    # constant instead -- a changed floor fails here, naming the real cause,
    # rather than quietly turning one of the two cases into the other.
    assert 20 < MINIMUM_INK_PIXELS <= 30, (
        "MINIMUM_INK_PIXELS moved out of the 20/30 px straddle these cut regions "
        "are built to sit either side of; rebuild the geometry around the new floor"
    )
    partial = {1: [{"x": 0, "y": 0, "w": 10, "h": 8}]}  # leaves 2 rows = 20 px
    assert recensor.unclaimed_ink_observations(maps, [observation], 1, partial) == []
    smaller = {1: [{"x": 0, "y": 0, "w": 10, "h": 7}]}  # leaves 3 rows = 30 px
    assert recensor.unclaimed_ink_observations(maps, [observation], 1, smaller) == [
        {"page_ordinal": 1, "outside_ink_pixels": 30}
    ]


def test_two_overlapping_cut_regions_do_not_subtract_their_shared_pixels_twice():
    """Acts cut on one page may overlap; the mask is a union, not a sum."""
    recensor = _recensor()
    box = {"x": 0, "y": 0, "w": 10, "h": 10}
    observation = {"kind": "unrouted-observation", "bounds": box}
    maps = recensor.ink_map_by_page(_FakeContext({1: _ink_map(20, 20, [box])}))
    overlapping = {1: [{"x": 0, "y": 0, "w": 6, "h": 10}, {"x": 3, "y": 0, "w": 4, "h": 10}]}
    # Union covers x 0..7 on every row: 3 columns x 10 rows remain outside.
    assert recensor.unclaimed_ink_observations(maps, [observation], 1, overlapping) == [
        {"page_ordinal": 1, "outside_ink_pixels": 30}
    ]


def test_unordered_ink_runs_are_refused_rather_than_double_counted():
    """The same evidence `edge_ink_from_runs` validates, validated here too.

    Overlapping runs would be counted twice and could manufacture the ink
    confirmation this gate exists to require.
    """
    recensor = _recensor()
    forged = _ink_map(20, 20, [])
    forged["rows"][0] = [[0, 10], [5, 10]]
    maps = recensor.ink_map_by_page(_FakeContext({1: forged}))
    observation = {"kind": "unrouted-observation", "bounds": {"x": 0, "y": 0, "w": 20, "h": 20}}
    with pytest.raises(FatalAccounting, match="unordered or out-of-bounds"):
        recensor.unclaimed_ink_observations(maps, [observation], 1, {})


@pytest.mark.parametrize(
    "evidence",
    [
        {"schema": "ink-runs.v1", "width": 0, "height": 1, "rows": [[]]},
        {"schema": "ink-runs.v1", "width": 1, "height": False, "rows": []},
    ],
)
def test_invalid_ink_map_dimensions_are_refused_instead_of_read_as_empty(evidence):
    """Zero and boolean dimensions cannot turn malformed evidence into no ink."""
    recensor = _recensor()
    maps = recensor.ink_map_by_page(_FakeContext({1: evidence}))
    observation = {"kind": "unrouted-observation", "bounds": {"x": 0, "y": 0, "w": 1, "h": 1}}
    with pytest.raises(
        FatalAccounting,
        match=(
            "invalid dimensions.*cannot be measured against a witness pointer.*"
            "Restore the sealed Ink Map artifact"
        ),
    ):
        recensor.unclaimed_ink_observations(maps, [observation], 1, {})


def test_an_observation_on_a_page_with_no_ink_map_entry_is_refused_by_name():
    """Missing evidence is an accounting refusal, never silently read as zero ink."""
    recensor = _recensor()
    box = {"x": 0, "y": 0, "w": 5, "h": 5}
    observation = {"kind": "unrouted-observation", "bounds": box}
    maps = recensor.ink_map_by_page(_FakeContext({2: _ink_map(20, 20, [box])}))
    with pytest.raises(
        FatalAccounting,
        match=(
            "retained unclaimed witness observations but no ink-map page-space evidence.*"
            "cannot determine whether those pointers cover real ink.*"
            "Restore the page's sealed Ink Map artifact"
        ),
    ):
        recensor.unclaimed_ink_observations(maps, [observation], 1, {})


@pytest.mark.parametrize(
    "observation",
    [
        {"kind": "unrouted-observation"},
        {"kind": "unrouted-observation", "bounds": None},
        {"kind": "unrouted-observation", "bounds": [0, 0, 5, 5]},
        # The two shapes that clear `isinstance(bounds, dict)` and reach the
        # arithmetic: a rectangle missing a side, and one whose side is not a
        # number. Both used to escape as a bare KeyError or TypeError.
        {"kind": "unrouted-observation", "bounds": {"x": 0, "y": 0, "w": 5}},
        {"kind": "unrouted-observation", "bounds": {"x": 0, "y": 0, "w": 5, "h": "5"}},
        "not-even-a-mapping",
    ],
    ids=[
        "missing-bounds",
        "null-bounds",
        "list-bounds",
        "partial-bounds",
        "non-integer-bounds",
        "non-mapping-observation",
    ],
)
def test_a_retained_observation_with_no_readable_bounds_is_refused_not_skipped(observation):
    """A malformed pointer is a fatal accounting gap, exactly like a missing map row.

    Silently skipping it would let a corrupted or mis-shaped witness record
    disappear behind an empty result -- the same silent loss GOVERNANCE 2
    refuses, and the one this gate exists to catch for every other malformed
    shape it reads (dimensions, rows, runs, missing map).
    """
    recensor = _recensor()
    box = {"x": 0, "y": 0, "w": 5, "h": 5}
    maps = recensor.ink_map_by_page(_FakeContext({1: _ink_map(20, 20, [box])}))
    with pytest.raises(
        FatalAccounting,
        match="retained unclaimed witness observation with no .x, y, w, h. bounds",
    ):
        recensor.unclaimed_ink_observations(maps, [observation], 1, {})


def test_one_observation_funds_one_request_on_its_page():
    """Page-scoped evidence funds one act-scoped grant on that page."""
    confirmed = [{"page_ordinal": 1, "outside_ink_pixels": 40}]
    assert _wants_recovery(confirmed, page_ordinal=1, funded_pages=set()) is True
    assert _wants_recovery(confirmed, page_ordinal=1, funded_pages={1}) is False
    assert _wants_recovery(confirmed, page_ordinal=2, funded_pages={1}) is True


def test_the_declared_route_is_not_bounded_by_another_pages_observation():
    """A scenario-declared recrop is not the observation's grant to spend."""
    trigger = _live_recovery_trigger()
    assert bool(
        eval(  # noqa: S307 -- compile input is this checked-in module's one expression.
            trigger,
            {"bool": bool},
            {
                "act_key": "a",
                "act": {"page_ordinal": 1},
                "scenario": {"recover_acts": ["a"]},
                "outside_ink_requests": [],
                "funded_pages": {1},
                "used_total": 0,
            },
        )
    )


def test_a_removal_of_the_page_bound_is_caught_by_this_files_own_guard():
    """Mutate-and-observe: dropping the bound trips the structural guard."""
    source = RECENSOR.read_text(encoding="utf-8")
    live = '(bool(outside_ink_requests) and act["page_ordinal"] not in funded_pages)'
    assert source.count(live) == 1, "the test no longer identifies the live page bound"
    unbounded = source.replace(live, "bool(outside_ink_requests)")
    with pytest.raises(AssertionError, match="one request per page"):
        _live_recovery_trigger(unbounded)


class _RequestTree:
    def __init__(self, requests: list[dict]):
        self._requests = {request["artifact_id"]: request for request in requests}

    def build_manifest(self, stage):
        return {
            "artifacts": [
                {"kind": "recovery-request", "artifact_id": artifact_id}
                for artifact_id in self._requests
            ]
        }

    def read_artifact(self, stage, kind, artifact_id):
        return self._requests[artifact_id]


class _RequestContext:
    def __init__(self, requests: list[dict]):
        self.tree = _RequestTree(requests)


def _request(artifact_id: str, act_id: str, origin: str | None) -> dict:
    payload: dict = {"act_key": act_id, "recovery_kind": "fallback-recrop"}
    if origin is not None:
        payload["origin"] = origin
    return {"artifact_id": artifact_id, "subject_id": act_id, "payload": payload}


ACTS = [
    {"act_id": "act-1", "page_ordinal": 1},
    {"act_id": "act-2", "page_ordinal": 1},
    {"act_id": "act-3", "page_ordinal": 2},
]


def test_the_page_bound_is_counted_from_the_tree_not_from_this_pass():
    """The bound has to survive the Recensor pass that follows a recrop.

    A per-run variable would reset on the next pass and let the page's second
    act spend the grant one round later, which is the same two requests with a
    round between them.
    """
    recensor = _recensor()
    context = _RequestContext(
        [
            _request("r1", "act-1", recensor.COVERAGE_OBSERVATION_ORIGIN),
            _request("r2", "act-3", recensor.DECLARED_CROP_ORIGIN),
        ]
    )
    assert recensor.observation_funded_pages(context, ACTS) == {1}


def test_two_observation_funded_requests_on_one_page_are_refused_not_collapsed():
    """The read-side bound is an invariant, not a lossy set conversion."""
    recensor = _recensor()
    context = _RequestContext(
        [
            _request("r1", "act-1", recensor.COVERAGE_OBSERVATION_ORIGIN),
            _request("r2", "act-2", recensor.COVERAGE_OBSERVATION_ORIGIN),
        ]
    )
    with pytest.raises(FatalAccounting, match="more than one observation-funded"):
        recensor.observation_funded_pages(context, ACTS)


def test_a_declared_request_does_not_spend_the_observation_grant_beside_it():
    """The causal origin is the declared route when both routes are present."""
    recensor = _recensor()
    confirmed = [{"page_ordinal": 1, "outside_ink_pixels": 40}]
    assert (
        recensor.recovery_request_origin(declared=True, outside_ink_requests=confirmed)
        == recensor.DECLARED_CROP_ORIGIN
    )
    assert (
        recensor.recovery_request_origin(declared=False, outside_ink_requests=confirmed)
        == recensor.COVERAGE_OBSERVATION_ORIGIN
    )


def test_a_request_origin_with_no_cause_refuses_instead_of_guessing():
    recensor = _recensor()
    with pytest.raises(FatalAccounting, match="neither a declared nor an ink-confirmed origin"):
        recensor.recovery_request_origin(declared=False, outside_ink_requests=[])


def test_a_second_request_is_replaced_by_a_loud_hold_not_an_acceptance():
    """The one-grant bound preserves the unresolved pointer as a live hold."""
    recensor = _recensor()
    confirmed = [{"page_ordinal": 1, "outside_ink_pixels": 40}]
    outcome, reason = recensor.unresolved_observation_hold(confirmed, 1, {1})
    assert outcome == "held-for-review"
    assert "one observation-funded recovery request is already recorded" in reason
    exhausted_outcome, exhausted_reason = recensor.unresolved_observation_hold(confirmed, 1, set())
    assert exhausted_outcome == "held-for-review"
    assert "bounded recovery policy cannot admit another request" in exhausted_reason
    assert recensor.unresolved_observation_hold([], 1, {1}) is None

    source = RECENSOR.read_text(encoding="utf-8")
    assert source.count("observation_hold = unresolved_observation_hold(") == 1
    assert (
        source.count(
            "elif observation_hold is not None:\n            outcome, reason = observation_hold"
        )
        == 1
    ), "the live terminal route no longer turns the unresolved pointer into a loud hold"


def test_a_recovery_request_with_no_recorded_origin_is_refused():
    """The bound counts a recorded fact; an unrecorded one is not resolved."""
    recensor = _recensor()
    context = _RequestContext([_request("r1", "act-1", None)])
    with pytest.raises(FatalAccounting, match="names no recorded origin"):
        recensor.observation_funded_pages(context, ACTS)


def test_a_recovery_request_for_an_unexpected_act_is_refused():
    """A request whose act is outside the seal cannot be counted onto a page."""
    recensor = _recensor()
    context = _RequestContext([_request("r1", "act-9", recensor.COVERAGE_OBSERVATION_ORIGIN)])
    with pytest.raises(FatalAccounting, match="outside the proposal seal"):
        recensor.observation_funded_pages(context, ACTS)


def test_the_mask_argument_has_no_fail_open_default():
    """Omitting the cut mask restores the pre-fix over-count; it must not be optional."""
    recensor = _recensor()
    box = {"x": 0, "y": 0, "w": 10, "h": 10}
    maps = recensor.ink_map_by_page(_FakeContext({1: _ink_map(20, 20, [box])}))
    # Bound to the signature. A bare `TypeError` also matches one raised while
    # measuring the observation, so a later change that gave the mask a default
    # and failed in the arithmetic would keep this green and let the over-count
    # back in unnoticed.
    with pytest.raises(TypeError, match="unclaimed_ink_observations"):
        recensor.unclaimed_ink_observations(maps, [{"bounds": box}], 1)
