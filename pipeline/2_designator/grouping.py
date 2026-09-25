"""Act grouping: crops assemble into acts by geometry and structural cues only.

Principle 1: nothing here scores, ranks or elects among candidate regions by
quality. Every decision is a deterministic partition or overlap test over
fixed geometry -- a component belongs to a column because of where it sits, a
body run splits because a boundary crosses it, an anchor attaches because
y-ranges overlap -- and where one group must be picked from several
(`find_continuation_candidate`), the pick is the extremal one by position,
never a score. `test_grouping.py` proves the whole pass is input-order
invariant, which an election could never be.

Two columns only: a narrow left-hand *margin* column (names, numbered markers,
formulaic openings) and everything else as *body*. A margin component is a
candidate **anchor**, whose top edge marks where a new act's body is expected
to begin. An anchor unusually tall is a **brace** (the `B. 43 }` case joining
two register rows) and marks *two* act starts -- at its top and at its own
vertical midpoint -- so both resulting acts carry it as shared evidence rather
than one act absorbing the other or losing its second half.

Every raw candidate region ends up in exactly one body-anchored act, an
isolated marginal-note act, or no group at all; `conservation.py`, not this
module, accounts for the ungrouped case.

**A component spanning the page is not connective tissue.** A page-wide
connected component (bezel, writing, or another dark population -- the scan
cannot tell which) would otherwise chain every body component on the page to
it via `_chain_body`. `partition_page_spanning` withholds such a component
from column assignment and chaining, but withheld is not excluded: its pixels
remain in `conservation.reconcile`'s `total_ink_pixel_count`, and any
unclaimed remainder still surfaces as held residual evidence.
"""

from typing import Any, TypedDict

from geometry import BP_DENOMINATOR, Bounds

from common.contracts.errors import ContractError


class ActGroup(TypedDict):
    bounds: Bounds
    body_members: list[dict]
    anchors: list[dict]
    rationale: str


# No geometric policy in this module carries a default: run.py resolves every
# threshold per page from sealed config and passes the pixel integer in, so a
# caller that forgets one fails loudly rather than running under an unreviewed
# value.


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_margin(margin_px: int, page_w: int) -> None:
    """The one margin predicate both `assign_columns` and `group_page` hold,
    so the two call sites cannot drift apart."""
    if not _plain_int(margin_px) or not (0 < margin_px < page_w):
        raise ContractError(f"margin {margin_px}px is not between 0 and page width {page_w}")


def _y_range(component: dict) -> tuple[int, int]:
    bounds = component["bounds"]
    return bounds["y"], bounds["y"] + bounds["h"]


def _x_range(component: dict) -> tuple[int, int]:
    bounds = component["bounds"]
    return bounds["x"], bounds["x"] + bounds["w"]


def _intervals_overlap(a: tuple[int, int], b: tuple[int, int], tolerance: int) -> bool:
    a0, a1 = a
    b0, b1 = b
    return not (a1 + tolerance < b0 or b1 + tolerance < a0)


def _union_bounds(components: list[dict]) -> Bounds:
    xs0 = [component["bounds"]["x"] for component in components]
    ys0 = [component["bounds"]["y"] for component in components]
    xs1 = [component["bounds"]["x"] + component["bounds"]["w"] for component in components]
    ys1 = [component["bounds"]["y"] + component["bounds"]["h"] for component in components]
    x0, y0, x1, y1 = min(xs0), min(ys0), max(xs1), max(ys1)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _check_page_spanning_area_bp(page_spanning_area_bp: int) -> None:
    """The one bound both `group_page` and `partition_page_spanning` hold, so
    the two callers refuse an identical set of values.
    """
    if not _plain_int(page_spanning_area_bp) or not (0 < page_spanning_area_bp <= BP_DENOMINATOR):
        raise ContractError(
            f"page-spanning area {page_spanning_area_bp} is not an integer in "
            f"1..{BP_DENOMINATOR} basis points"
        )


def _bbox_area_bp(bounds: Bounds, page_w: int, page_h: int) -> int:
    """A bounding box's area as floor-divided basis points of the page's own area.

    Floor division only ever moves a box's fraction down, toward being
    grouped rather than withheld by the caller's `>=` -- the direction that
    keeps evidence in the pass.
    """
    return (bounds["w"] * bounds["h"] * BP_DENOMINATOR) // (page_w * page_h)


def partition_page_spanning(
    components: list[dict], page_w: int, page_h: int, *, page_spanning_area_bp: int
) -> tuple[list[dict], list[dict]]:
    """Split components into (grouped, withheld) by how much of the page each covers.

    A component covering `page_spanning_area_bp` basis points or more of the
    page is withheld: not offered to `assign_columns`, never entering
    `_chain_body`, so it cannot weld two acts together through a page-wide
    y-range. Withheld, not excluded: `run.py` records the withheld half on the
    conservation record, and `conservation.reconcile` still counts every
    withheld pixel in `total_ink_pixel_count`. Each component is measured only
    against the sealed fraction, independently -- a deterministic partition,
    never a comparison between components (principle 1).
    """
    if page_w <= 0 or page_h <= 0:
        raise ContractError(f"a {page_w}x{page_h} page has no area to measure against")
    _check_page_spanning_area_bp(page_spanning_area_bp)
    grouped: list[dict] = []
    withheld: list[dict] = []
    for component in components:
        target = (
            withheld
            if _bbox_area_bp(component["bounds"], page_w, page_h) >= page_spanning_area_bp
            else grouped
        )
        target.append(component)
    return grouped, withheld


def assign_columns(
    components: list[dict], page_w: int, *, margin_px: int
) -> tuple[list[dict], list[dict]]:
    """Split components into (margin, body) by horizontal position only.

    A component's centre-x decides its column, so a centre inside the margin
    band puts it there even when its right edge overhangs the boundary. The
    comparison `x0 + x1 < 2 * margin_px` avoids a float midpoint.
    """
    if page_w <= 0:
        raise ContractError(f"page width {page_w} is not positive")
    _check_margin(margin_px, page_w)
    margin: list[dict] = []
    body: list[dict] = []
    for component in components:
        x0, x1 = _x_range(component)
        (margin if x0 + x1 < 2 * margin_px else body).append(component)
    return margin, body


def _is_brace(anchor: dict, brace_min_height_px: int) -> bool:
    return anchor["bounds"]["h"] >= brace_min_height_px


def _boundaries(anchors: list[dict], brace_min_height_px: int) -> list[int]:
    """Every act-start row an anchor implies, a brace implying two.

    Sorted and de-duplicated: two anchors that happen to start on the exact
    same row would otherwise open a zero-height group between them.
    """
    boundaries: set[int] = set()
    for anchor in anchors:
        y0, y1 = _y_range(anchor)
        boundaries.add(y0)
        if _is_brace(anchor, brace_min_height_px):
            boundaries.add(y0 + (y1 - y0) // 2)
    return sorted(boundaries)


def _boundary_index(y: int, boundaries: list[int], reach: int) -> int:
    """How many act-start rows are at or before `y`, allowing `reach` pixels of
    slack for ordinary detection jitter in a body component's top edge.
    """
    index = 0
    for boundary in boundaries:
        if boundary <= y + reach:
            index += 1
        else:
            break
    return index


def _chain_body(
    body_sorted: list[dict], boundaries: list[int], chain_gap_px: int, anchor_reach_px: int
) -> list[list[dict]]:
    """Partition y-sorted body components into runs.

    A run breaks whenever the vertical gap since the previous component
    exceeds `chain_gap_px`, OR whenever crossing into this component crosses
    one more anchor boundary than the previous component did -- the second
    condition is what splits two acts whose body text has no blank row
    between them at all (the "interleaved margins" case), where a gap alone
    would never be found.
    """
    runs: list[list[dict]] = []
    current: list[dict] = []
    current_index: int | None = None
    previous_bottom: int | None = None
    for component in body_sorted:
        top, bottom = _y_range(component)
        index = _boundary_index(top, boundaries, anchor_reach_px)
        starts_new = (
            not current
            or index != current_index
            or (previous_bottom is not None and top - previous_bottom > chain_gap_px)
        )
        if starts_new and current:
            runs.append(current)
            current = []
            previous_bottom = None
        current.append(component)
        current_index = index
        previous_bottom = bottom if previous_bottom is None else max(previous_bottom, bottom)
    if current:
        runs.append(current)
    return runs


def group_page(
    components: list[dict],
    page_w: int,
    page_h: int,
    *,
    margin_px: int,
    chain_gap_px: int,
    anchor_reach_px: int,
    brace_min_height_px: int,
    page_spanning_area_bp: int,
) -> list[ActGroup]:
    """Group one page's raw candidate regions into acts.

    Every component is sorted by geometry before anything is decided, so input
    order never affects the result (reconciliation, not election).

    Page-spanning components are withheld before anything else, here rather
    than only in `run.py`, so no caller can weld two acts together through one:
    `partition_page_spanning` is pure and idempotent, so `run.py`'s own call to
    publish the withheld half can never disagree with this one.

    A page on which every component is withheld returns no groups at all,
    which `run.py` reads as `fallback-tiles` rather than one accidental
    whole-leaf group.
    """
    if page_w <= 0 or page_h <= 0:
        raise ContractError(f"a {page_w}x{page_h} page has no area to group within")
    for name, value in (
        ("chain gap", chain_gap_px),
        ("anchor reach", anchor_reach_px),
        ("brace minimum height", brace_min_height_px),
    ):
        if not _plain_int(value) or value < 0:
            raise ContractError(f"{name} {value}px is not a non-negative integer")
    _check_margin(margin_px, page_w)
    _check_page_spanning_area_bp(page_spanning_area_bp)

    components, _withheld = partition_page_spanning(
        components, page_w, page_h, page_spanning_area_bp=page_spanning_area_bp
    )
    if not components:
        return []

    margin, body = assign_columns(components, page_w, margin_px=margin_px)
    anchors_sorted = sorted(
        margin, key=lambda component: (component["bounds"]["y"], component["bounds"]["x"])
    )
    body_sorted = sorted(
        body, key=lambda component: (component["bounds"]["y"], component["bounds"]["x"])
    )
    boundaries = _boundaries(anchors_sorted, brace_min_height_px)

    runs = _chain_body(body_sorted, boundaries, chain_gap_px, anchor_reach_px)

    provisional: list[tuple[list[dict], list[dict]]] = []
    claimed_anchor_ids: set[int] = set()
    for run in runs:
        run_range = (min(_y_range(c)[0] for c in run), max(_y_range(c)[1] for c in run))
        attached = [
            anchor
            for anchor in anchors_sorted
            if _intervals_overlap(_y_range(anchor), run_range, anchor_reach_px)
        ]
        claimed_anchor_ids.update(id(anchor) for anchor in attached)
        provisional.append((run, attached))

    # A brace is one anchor shared across more than one group, not "more than
    # one anchor attached to this group" -- so attachments are counted per
    # anchor, over every group at once.
    attachment_counts: dict[int, int] = {}
    for _run, attached in provisional:
        for anchor in attached:
            attachment_counts[id(anchor)] = attachment_counts.get(id(anchor), 0) + 1

    groups: list[ActGroup] = []
    for run, attached in provisional:
        if not attached:
            rationale = "no margin anchor precedes this body run; a candidate leading fragment"
        elif any(attachment_counts[id(anchor)] > 1 for anchor in attached):
            rationale = f"brace-linked: {len(attached)} shared anchor(s) evidence this act"
        else:
            rationale = "single margin anchor seeds one body run"
        groups.append(
            {
                "bounds": _union_bounds(run + attached),
                "body_members": run,
                "anchors": attached,
                "rationale": rationale,
            }
        )

    for anchor in anchors_sorted:
        if id(anchor) not in claimed_anchor_ids:
            groups.append(
                {
                    "bounds": dict(anchor["bounds"]),
                    "body_members": [],
                    "anchors": [anchor],
                    "rationale": "isolated marginal note: no adjacent body run",
                }
            )

    groups.sort(key=lambda group: (group["bounds"]["y"], group["bounds"]["x"]))
    return groups


def find_continuation_candidate(
    page_a_groups: list[ActGroup],
    page_a_h: int,
    page_b_groups: list[ActGroup],
    *,
    edge_reach_a_px: int,
    edge_reach_b_px: int,
) -> dict[str, Any] | None:
    """A page-break continuation candidate, found by geometry alone.

    The trailing group on page A must touch the page's bottom edge, the
    leading group on page B must touch its top edge, carry no anchor of its
    own (an anchored group is a new act) and share a column with the trailing
    group. These are position tests only; whether the content actually
    continues is the Recensor's judgement, not this function's.

    `edge_reach_a_px` and `edge_reach_b_px` are each page's own resolved edge
    reach: one shared value would silently assume the two pages share a height.

    The column-share test takes no slack: two x-ranges share a column only
    when they actually meet, since this check is recorded rather than gating
    (`run.py::_publish_act_group`), so a miss under-corroborates rather than
    losing a continuation outright. Any future slack goes into config in
    basis points, never as a default here.
    """
    for name, value in (
        ("page A edge reach", edge_reach_a_px),
        ("page B edge reach", edge_reach_b_px),
    ):
        if not _plain_int(value) or value < 0:
            raise ContractError(f"{name} {value}px is not a non-negative integer")
    if not page_a_groups or not page_b_groups:
        return None
    trailing = max(page_a_groups, key=lambda group: group["bounds"]["y"] + group["bounds"]["h"])
    leading = min(page_b_groups, key=lambda group: group["bounds"]["y"])
    trailing_bottom = trailing["bounds"]["y"] + trailing["bounds"]["h"]
    if page_a_h - trailing_bottom > edge_reach_a_px:
        return None
    if leading["bounds"]["y"] > edge_reach_b_px:
        return None
    if leading["anchors"]:
        return None
    # Direct non-empty-intersection test, not `_intervals_overlap(..., 0)`:
    # that helper treats touching endpoints as overlap, but two columns that
    # merely touch (`[40,100)` beside `[100,160)`) share no pixel here.
    trailing_x0, trailing_x1 = _x_range(trailing)
    leading_x0, leading_x1 = _x_range(leading)
    if trailing_x0 >= leading_x1 or leading_x0 >= trailing_x1:
        return None
    return {"page_a_group": trailing, "page_b_group": leading}


# The predetermined fallback crop grid, for a page with no eligible structural
# group: every witness reads every crop, so a true blank is decided downstream
# rather than by this module deciding a page has nothing to send. Horizontal
# bands, not a checkerboard, since a register page is a column of entries and
# a band spans the full width.
def fallback_tiles(
    page_w: int,
    page_h: int,
    *,
    bands: int,
    overlap_px: int,
) -> list[ActGroup]:
    """Predetermined overlapping crops covering a page that requires fallback.

    Every pixel falls inside at least one band, and adjacent bands overlap by
    `overlap_px`, so a line sitting exactly on a boundary is whole inside one
    of the two rather than cut in half by both. Each group's rationale says
    it's a fallback tile, so nothing downstream mistakes a grid for a real find.
    """
    if page_w <= 0 or page_h <= 0:
        raise ContractError(f"a {page_w}x{page_h} page has no area to tile")
    if not _plain_int(bands) or bands <= 0:
        raise ContractError(f"a fallback grid of {bands} bands cuts nothing")
    if not _plain_int(overlap_px) or overlap_px < 0:
        raise ContractError(f"fallback overlap {overlap_px} is not a non-negative integer")
    if bands > page_h:
        # Not silently clamped: a zero-height band is not a crop, and the
        # sealed band count must match what this call actually cuts.
        raise ContractError(
            f"a fallback grid of {bands} bands cannot be cut on a {page_h}px-tall page: "
            "at least one band would have zero height"
        )

    tiles: list[ActGroup] = []
    for index in range(bands):
        # Computed from the index, not a rounded band height: a rounded
        # height accumulated over `bands` iterations would leave a strip
        # covered by no crop, and any act inside it would be lost.
        top = (page_h * index) // bands
        bottom = (page_h * (index + 1)) // bands
        grown_top = max(0, top - overlap_px)
        grown_bottom = min(page_h, bottom + overlap_px)
        tiles.append(
            ActGroup(
                bounds={
                    "x": 0,
                    "y": grown_top,
                    "w": page_w,
                    "h": grown_bottom - grown_top,
                },
                body_members=[],
                anchors=[],
                rationale=(
                    f"fallback tile {index + 1} of {bands}: a predetermined crop is cut "
                    "and sent to be read rather than the page being called blank here"
                ),
            )
        )
    return tiles
