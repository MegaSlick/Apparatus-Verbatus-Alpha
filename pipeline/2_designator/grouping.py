"""Act grouping: crops assemble into acts by geometry and structural cues only.

GOVERNANCE 3 is the whole shape of this module. The old pipeline's grouping
file elected a "pivot witness" per act -- a static trust table scored
candidate witnesses and the highest-scoring one became the act's authoritative
structure, with everyone else diff-aligned onto it (`resolve_columns`,
confirmed a GOVERNANCE 3 picker by this project's own audit). Nothing here
scores, ranks, or weights candidate regions *by quality*, and nothing elects
among witnesses. Every grouping decision below is a deterministic partition
or an overlap test over fixed geometry: a component belongs to a column
because of where it sits, a body run splits because a boundary crosses it, an
anchor attaches to a group because their y-ranges overlap. Where a single
group must be picked from several (`find_continuation_candidate`'s trailing
and leading groups), the pick is the extremal one by position -- bottommost
or topmost -- never a score. Two callers handed the same set of components
in different input order get the identical set of groups back -- `test_grouping.py`
proves this directly, because "the same evidence groups the same way regardless
of who is asked first" is the property an election shape cannot have.

Two columns only: a narrow left-hand *margin* column, where marginal names,
numbered markers and the formulaic openings ARCHITECTURE names live, and
everything else as *body*. A margin component is a candidate **anchor**: its
top edge marks where a new act's body text is expected to begin. An anchor
whose own height is unusually large is a **brace** -- the marginal-brace case
named in the spec (`B. 43 }` / `S. 26 }` joining two register rows) -- and is
treated as marking *two* act starts, one at its top and one at its own
vertical midpoint, rather than one act twice as tall. Both resulting acts then
carry the same brace component as shared evidence; neither absorbs the other,
and neither is dropped. That is the concrete shape of "a Designator that
assumes chunks and acts correspond will silently lose the second act of every
braced pair" turned into code that does not do that.

Every raw candidate region ends up somewhere: in exactly one body-anchored
act, in an isolated marginal-note act (a margin anchor with no adjacent body
group at all), or in no group -- and `conservation.py` is what accounts for
that last case, not this module. Grouping only assembles what it is given; it
never decides that something ungrouped may be discarded.
"""

from typing import Any, Final, TypedDict

from geometry import Bounds

from common.contracts.errors import ContractError


class ActGroup(TypedDict):
    bounds: Bounds
    body_members: list[dict]
    anchors: list[dict]
    rationale: str


# These geometric policies used to carry module defaults here. They no longer
# do: `run.py` resolves each one, per page, from a sealed basis-point config
# and that page's own dimensions (SPEC_C section 2, "Where resolution
# happens"), and passes the resolved pixel integers in on every call. A caller
# that forgets one now fails loudly rather than running under a value nobody
# reviewed for this page -- `geometry.load_padding_config`'s own docstring is
# the precedent: "refused loudly rather than defaulted".


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_margin(margin_px: int, page_w: int) -> None:
    """The one margin predicate both `assign_columns` and `group_page` hold.

    A single spelling so the two call sites cannot drift apart: a float
    margin or one outside the page must be refused the same way regardless
    of which caller resolved it first, including when `group_page` short-
    circuits on an empty page and never reaches `assign_columns` at all.
    """
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


def assign_columns(
    components: list[dict], page_w: int, *, margin_px: int
) -> tuple[list[dict], list[dict]]:
    """Split components into (margin, body) by horizontal position only.

    A component's centre-x decides its column: marginal names and numbered
    markers are narrow and left-aligned, so a centre inside the margin band
    puts it there even when its right edge slightly overhangs the boundary.
    `margin_px` is resolved from the page's own width before this is called,
    never a fraction computed here -- the margin is a fixed lane the page's
    layout defines, not a property of what happens to be printed in it. The
    comparison itself stays integer: `x0 + x1 < 2 * margin_px` decides the
    same side as `(x0 + x1) / 2 < margin_px` for integer bounds, without ever
    producing a float (GLOSSARY / canonical-integer rule).
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
    """How many act-start rows are at or before `y`, allowing `reach` pixels of slack.

    A body component's own top edge can land a few pixels before the anchor
    that actually seeds its act -- ordinary detection jitter, not evidence of
    an earlier start -- so a boundary within `reach` pixels of `y` still counts
    as reached. Requiring equality to the pixel merges the second act into the
    first and loses its identity entirely, and the anchor-attachment overlap
    test below already declines to require that of the same geometry: this is
    the same slack, applied where the partition is decided rather than only
    where an anchor attaches to a run already decided.
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
) -> list[ActGroup]:
    """Group one page's raw candidate regions into acts.

    Deterministic and permutation-invariant in its input: every component is
    sorted by geometry before anything is decided, so the order `components`
    arrives in never affects the result. That property is what makes this
    reconciliation rather than election -- an election is exactly a function
    that *can* depend on presentation order or a score, and this one cannot.
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

    # A brace is not "more than one anchor attached to this group" -- it is
    # one anchor shared *across* more than one group. Counting attachments
    # per anchor, over every group at once, is what tells the two apart: a
    # single wide anchor overlapping two runs must mark both as brace-linked,
    # never one of them as merely "single-anchor" because it only checked its
    # own run's attachment count.
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

    # An anchor no body run reached at all: a marginal note with no body text
    # of its own, never dropped -- it becomes its own act, body-empty.
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
    column_overlap_px: int = 0,
) -> dict[str, Any] | None:
    """A page-break continuation candidate, found by geometry alone.

    The trailing group on page A must touch the page's own bottom edge, and
    the leading group on page B must touch its own top edge, carry no anchor
    of its own (an anchored group is a new act, not a continuation) and share
    a column with the trailing group. All four conditions are position tests;
    none reads a character of text, and none exists to guess whether the
    *content* actually continues -- that judgement belongs to the Recensor.
    This function only proposes that the geometry is consistent with one.

    `edge_reach_a_px` and `edge_reach_b_px` are each page's own resolved edge
    reach -- one value serving both pages silently assumed they shared a
    height, which a real corpus does not guarantee.
    """
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
    if not _intervals_overlap(_x_range(trailing), _x_range(leading), column_overlap_px):
        return None
    return {"page_a_group": trailing, "page_b_group": leading}


# The predetermined fallback crop grid, for a page the structure pass found
# nothing on. Tyrel ruled 2026-08-11: "If the designator sees no text it should
# default to predetermined crops with a small margin of overlap and send the
# crops down stream to be read by everything. If all the witnesses and the
# perlector see no text on any of the crops then it's likely a true blank." And,
# settling where a page that cannot be thresholded goes: "Everything gets read
# every time nothing gets pulled out or held."
#
# Horizontal bands rather than a checkerboard because a register page is a
# column of entries: a band spans the full width, so an entry is never split
# down its middle by the grid itself. Declared here as named policy, overridable
# by keyword, for the same reason as every default above -- not a magic number
# buried in a stage program.
DEFAULT_FALLBACK_BANDS: Final = 4
DEFAULT_FALLBACK_OVERLAP_PX: Final = 8


def fallback_tiles(
    page_w: int,
    page_h: int,
    *,
    bands: int = DEFAULT_FALLBACK_BANDS,
    overlap_px: int = DEFAULT_FALLBACK_OVERLAP_PX,
) -> list[ActGroup]:
    """Predetermined overlapping crops covering the whole page, for a page with no found ink.

    Every pixel of the page falls inside at least one band, and adjacent bands
    overlap by `overlap_px`, so a line of writing sitting exactly on a band
    boundary is whole inside one of the two rather than cut in half by both.

    This is not detection and it does not pretend to be: each group carries a
    rationale saying it is a fallback tile, so nothing downstream can mistake a
    grid for something the structure pass found. It elects nothing and ranks
    nothing -- GOVERNANCE 3 is about choosing among witnesses, and a fixed grid
    computed from the page's own dimensions chooses nothing at all.
    """
    if page_w <= 0 or page_h <= 0:
        raise ContractError(f"a {page_w}x{page_h} page has no area to tile")
    if bands <= 0:
        raise ContractError(f"a fallback grid of {bands} bands cuts nothing")
    if overlap_px < 0:
        raise ContractError(f"fallback overlap {overlap_px} is negative")

    bands = min(bands, page_h)
    tiles: list[ActGroup] = []
    for index in range(bands):
        # Integer edges computed from the index so the last band always ends
        # exactly at the page edge -- a rounded band height would leave a strip
        # of the page in no crop at all, which is the one thing this must not do.
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
                    f"fallback tile {index + 1} of {bands}: the structure pass found no ink to "
                    "group on this page, so a predetermined crop is cut and sent to be read "
                    "rather than the page being called blank here"
                ),
            )
        )
    return tiles
