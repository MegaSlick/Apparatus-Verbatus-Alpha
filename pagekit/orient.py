"""Orientation: how many quarter turns clockwise make a page upright.

The detector works on a reduced working copy of the source image and writes no file.
It returns the detector answer shape: exactly ``value`` (0, 1, 2 or 3 quarter turns
clockwise), ``confidence`` (0 to 1), ``evidence`` (one plain sentence) and ``flags``
(plain reasons to review; empty when none). When it is not sure it answers 0 turns
and says why in a flag; it never guesses silently.

Method, in two steps:

1. Lines across or down. Lines of writing make the ink profile across rows sharply
   peaked while the profile across columns is flatter. The two are compared (squared
   coefficient of variation) inside square tiles of writing, and the median over the
   tiles decides, so margins, a gutter, page-edge stacks and dark masses, which give
   the whole frame strong column structure, do not pull the score; facing pages
   whose lines are out of step or lean apart are each measured on their own. Rules,
   page edges and the dark border are masked first. After H. S. Baird, "The skew
   angle of printed documents", Proc. SPSE Symposium on Hybrid Imaging Systems, 1987,
   which scores projection profiles by how sharply they vary. Marks wider than tall
   are a second cue, weighted 0 by default (see the settings).
2. Upright or upside down. In narrow vertical strips of writing, each line's core band
   is found, and two cues are measured: the ink above the core band (ascenders)
   against the ink below it (descenders), and how much sharper the baseline edge is
   than the x-line edge. Each strip votes; the votes are combined by how consistently
   they agree (mean over standard error), so a small but steady asymmetry over many
   strips counts. Line starts aligned on the left against a ragged right edge are a
   further cue, weighted 0 by default, since registers justify their lines. After R. S.
   Caprari, "Algorithm for text page up/down orientation determination", Pattern
   Recognition Letters 21(4):311-317, 2000.

A blank or nearly blank page answers 0 turns with confidence 0 and a flag. A frame that looks like light
writing on a dark ground is flagged as possibly negative and its
orientation is not decided.

Limits: the up-down cues are small asymmetries (about 0.1 on old French cursive) and
are decided by their consistency over 20 to 30 strips, so a page with little writing
goes to review; the strip settings were chosen on two real spreads and are
unmeasured beyond them. All settings are unmeasured guesses (``thresholds_split.toml``).
"""

from __future__ import annotations

import math
from statistics import median, pstdev
from typing import Any

from PIL import Image

from pagekit._orient_ink import (
    TOO_LITTLE_INK,
    Mark,
    answer,
    grey_of,
    ink_map,
    is_long_thin,
    levels_of,
    looks_negative,
    marks_of,
    mask_straight_marks,
    paint,
    profile,
    runs_of,
    setting_values,
    strength,
    working_copy,
)

UNCERTAIN_DIRECTION = (
    "orientation uncertain: whether the lines run across or down is too close to call"
)
UNCERTAIN_UPDOWN = "orientation uncertain: whether the page is upright or upside down is too weak"
UNCERTAIN_CORE_BAND = (
    "orientation uncertain: whether the page is upright or upside down depends on where "
    "each line's core band is taken"
)
# An uncertain step has confidence below 0.5; scaled by this, an uncertain answer
# reports below 0.2, because its 0 turns is a default, not a finding.
UNCERTAIN_SCALE = 0.4
DEFAULT_NOTE = "left at 0 turns as a default, not a finding, so its confidence is scaled below 0.2"
UNCERTAIN_UNIFORM = (
    "orientation uncertain: nearly all marks share one size (figures or capitals), with no "
    "ascenders or descenders and no runs of writing to orient by"
)
UNCERTAIN_FIGURE_TILES = (
    "orientation uncertain: a large share of the writing is figures or capitals of one "
    "size, with no ascenders or descenders of their own to orient by"
)
UNCERTAIN_COLUMNS = (
    "orientation uncertain: most of the writing is in narrow columns of items (amounts) "
    "beside a block of writing"
)
POSSIBLY_NEGATIVE = (
    "possibly a negative (light writing on a dark ground); orientation not decided from it"
)


def _trim_dark_border(ink: Image.Image, share: float) -> tuple[int, int, int, int]:
    """Trim border rows and columns that are nearly all dark: backdrop and page edge."""
    columns = profile(ink, along_x=True)
    rows = profile(ink, along_x=False)
    limit = share * 255

    def inner(values: list[int]) -> int:
        for index, level in enumerate(values):
            if level < limit:
                return index
        return len(values)

    x0, x1 = inner(columns), len(columns) - inner(columns[::-1])
    y0, y1 = inner(rows), len(rows) - inner(rows[::-1])
    if x1 <= x0 or y1 <= y0:
        return 0, 0, ink.size[0], ink.size[1]
    return x0, y0, x1, y1


def _uniform_share(marks: list[Mark], value: dict[str, Any]) -> float:
    """The larger of the shares of marks whose height, or whose width, is within
    same_size_tolerance of the median. Writing mixes ascenders, descenders and letters
    of many widths; a page of lining figures or capitals does not."""
    sized = [m for m in marks if m.count >= 2 * value["speck_px"]]
    if not sized:
        return 0.0
    best = 0.0
    for sizes in ([m.height for m in sized], [m.width for m in sized]):
        middle = median(sizes)
        near = sum(
            1 for size in sizes if abs(size - middle) <= value["same_size_tolerance"] * middle
        )
        best = max(best, near / len(sizes))
    return best


def _layout(marks: list[Mark], box: tuple[int, int, int, int], value: dict[str, Any]):
    """Columns of items beside a block of writing. Along each axis the mark boxes are
    projected into runs of content; a run no wider than narrow_run_share of the
    content's extent is narrow (a column of amounts, or a line of writing seen
    sideways). An axis is mixed when narrow runs hold at least mixed_narrow_share of the
    ink and wide runs at least mixed_wide_share: a block of writing beside columns of
    items. Text whose lines are seen sideways has only narrow runs, and upright text
    only wide ones, so neither is mixed. Returns the largest narrow share on a mixed
    axis and the marks in that axis's wide runs (None when no axis is mixed)."""
    total = sum(m.count for m in marks) or 1
    best = (0.0, None)
    for axis in (0, 1):
        low, high = (box[0], box[2]) if axis == 0 else (box[1], box[3])
        extent = high - low
        covered = [False] * extent
        for m in marks:
            a, b = (m.x0, m.x1) if axis == 0 else (m.y0, m.y1)
            for i in range(max(a, low), min(b, high)):
                covered[i - low] = True
        narrow = 0
        narrow_runs = 0
        wide_marks: list[Mark] = []
        for a, b in runs_of(covered):
            inside = [
                m
                for m in marks
                if a <= ((m.x0 + m.x1) / 2 if axis == 0 else (m.y0 + m.y1) / 2) - low < b
            ]
            if b - a <= value["narrow_run_share"] * extent:
                narrow += sum(m.count for m in inside)
                narrow_runs += 1
            else:
                wide_marks += inside
        narrow_share = narrow / total
        wide_share = sum(m.count for m in wide_marks) / total
        # Many narrow runs are lines of writing (a seal or lines that merge making the
        # wide run), not a few columns of items beside a block.
        mixed = (
            narrow_share >= value["mixed_narrow_share"]
            and wide_share >= value["mixed_wide_share"]
            and narrow_runs <= value["max_item_columns"]
        )
        if mixed and narrow_share > best[0]:
            best = (narrow_share, wide_marks)
    return best


def _peakedness(values: list[int]) -> float:
    """Squared coefficient of variation: how sharply a profile varies."""
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return variance / (mean * mean)


def _tile_scores(
    ink: Image.Image,
    value: dict[str, Any],
    marks: list[Mark] | None = None,
    offset: tuple[int, int] = (0, 0),
) -> tuple[list[float], int, set[int]]:
    """ln(row sharpness / column sharpness) for each square tile that holds writing
    (ink share within the tile limits), how many tiles of uniform marks were left out,
    and the ids of the marks in them. Inside a tile of writing the row profile alternates lines and gaps while the
    column profile is flat; tiles of margin, gutter, page edges or dark masses are left
    out, so their structure does not pull the score. Tiles whose marks nearly all share
    one size (figures in columns, which stack as exactly along a column as along a
    line) do not vote either. The grid is centred on the frame, so a half turn gives
    the same tiles. `marks` are in the coordinates of the frame `ink` was cropped from,
    at `offset`."""
    width, height = ink.size
    size = value["orient_tile_px"]
    across, down = width // size, height // size
    left, top = (width - across * size) // 2, (height - down * size) // 2
    by_tile: dict[tuple[int, int], list[Mark]] = {}
    for mark in marks or []:
        cx = (mark.x0 + mark.x1) / 2 - offset[0] - left
        cy = (mark.y0 + mark.y1) / 2 - offset[1] - top
        if 0 <= cx < across * size and 0 <= cy < down * size:
            by_tile.setdefault((int(cx // size), int(cy // size)), []).append(mark)
    tiny = 1e-6
    scores = []
    uniform = 0
    left_out: set[int] = set()
    for j in range(down):
        for i in range(across):
            x, y = left + i * size, top + j * size
            tile = ink.crop((x, y, x + size, y + size))
            share = tile.histogram()[255] / (size * size)
            if not value["tile_min_ink"] <= share <= value["tile_max_ink"]:
                continue
            inside = by_tile.get((i, j), [])
            if (
                len(inside) >= value["tile_min_marks"]
                and _uniform_share(inside, value) >= value["same_size_share"]
            ):
                uniform += 1
                left_out.update(id(m) for m in inside)
                continue
            rows = _peakedness(profile(tile, along_x=False))
            columns = _peakedness(profile(tile, along_x=True))
            scores.append(math.log((rows + tiny) / (columns + tiny)))
    return scores, uniform, left_out


def _direction(
    ink: Image.Image,
    marks: list[Mark],
    value: dict[str, Any],
    offset: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    """Positive score: lines run across. Negative: lines run down.

    The profile cue is the median over tiles of writing; when too few tiles hold
    writing it falls back to the profiles of the whole content. `dissent` is the share
    of voting tiles whose own vote is against the median."""
    tiles, uniform, left_out = _tile_scores(ink, value, marks, offset)
    rows = _peakedness(profile(ink, along_x=False))
    columns = _peakedness(profile(ink, along_x=True))
    tiny = 1e-9
    dissent = 0.0
    if len(tiles) >= value["min_tiles"]:
        profile_score = median(tiles)
        dissent = sum(1 for t in tiles if t * profile_score < 0) / len(tiles)
    else:
        profile_score = math.log((rows + tiny) / (columns + tiny))
    ratio = value["shape_ratio"]
    wide = sum(1 for m in marks if m.width >= ratio * m.height)
    tall = sum(1 for m in marks if m.height >= ratio * m.width)
    score = profile_score + value["shape_weight"] * math.log((wide + 1) / (tall + 1))
    return {
        "score": score,
        "profile": profile_score,
        "tiles": len(tiles) if len(tiles) >= value["min_tiles"] else 0,
        "uniform_tiles": uniform,
        "voting_tiles": len(tiles),
        "left_out": left_out,
        "dissent": dissent,
        "rows": rows,
        "columns": columns,
        "wide": wide,
        "tall": tall,
    }


def _mad(values: list[int]) -> float:
    centre = median(values)
    return median(abs(v - centre) for v in values)


def _line_segments(rows: list[int], busy: float, value: dict[str, Any]) -> list[tuple[int, int]]:
    """The lines of a strip: runs of rows above a low floor, split at any inner minimum
    that falls below line_split_share of the lower of the peaks on its two sides (so
    lines whose ascenders and descenders touch still separate)."""
    floor = value["strip_line_floor_share"] * busy
    split = value["line_split_share"]
    found = []
    stack = [(a, b) for a, b in runs_of([level > floor for level in rows]) if b - a >= 3]
    while stack:
        a, b = stack.pop()
        segment = rows[a:b]
        cut = None
        for i in range(2, len(segment) - 2):
            if segment[i] <= segment[i - 1] and segment[i] <= segment[i + 1]:
                lower_peak = min(max(segment[:i]), max(segment[i + 1 :]))
                if segment[i] < split * lower_peak and (cut is None or segment[i] < segment[cut]):
                    cut = i
        if cut is None:
            found.append((a, b))
        else:
            stack += [(a, a + cut), (a + cut, b)]
    return sorted(found)


def _strip_votes(
    ink: Image.Image,
    size: int,
    share: float,
    value: dict[str, Any],
    cores: list[int] | None = None,
) -> list[tuple[float, float]]:
    """For each vertical strip of `size` px holding writing: the ascender-against-
    descender balance and the baseline-against-x-line sharpness of its lines, each from
    -1 to 1, positive when upright. Each line's core band is found from that line's own
    profile: its rows from the first to the last at or above `share` of the line's
    busiest row. The strips are laid out centred on the frame, so a half turn of the
    frame gives the same strips."""
    width, height = ink.size
    count = width // size
    offset = (width - count * size) // 2
    out = []
    for i in range(count):
        x = offset + i * size
        strip = ink.crop((x, 0, x + size, height))
        if strip.histogram()[255] / (size * height) < value["strip_min_ink"]:
            continue
        rows = profile(strip, along_x=False)
        busy = sorted(rows)[int(0.9 * (len(rows) - 1))]
        if busy <= 0:
            continue
        lines = _line_segments(rows, busy, value)
        if len(lines) < 2:
            continue
        bands = []
        for a, b in lines:
            segment = rows[a:b]
            floor = share * max(segment)
            busy_rows = [j for j, level in enumerate(segment) if level >= floor]
            bands.append((a + busy_rows[0], a + busy_rows[-1] + 1))
        above = below = rise = fall = 0.0
        for index, (low, high) in enumerate(bands):
            if cores is not None:
                cores.append(high - low)
            # Ascender and descender zones reach halfway to the neighbouring lines'
            # core bands (sparse ascender rows fall below the line floor), and at the
            # first and last line one and a half core heights.
            reach = round(1.5 * (high - low)) + 1
            upper = low - reach if index == 0 else (bands[index - 1][1] + low + 1) // 2
            lower = high + reach if index + 1 == len(bands) else (high + bands[index + 1][0]) // 2
            upper, lower = max(0, upper), min(height, lower)
            above += sum(rows[upper:low])
            below += sum(rows[high:lower])
            rise += max(rows[j] - rows[j - 1] for j in range(max(1, low - 2), min(height, low + 3)))
            fall += max(
                rows[j - 1] - rows[j] for j in range(max(1, high - 2), min(height, high + 3))
            )
        if above + below and rise + fall:
            out.append(((above - below) / (above + below), (fall - rise) / (fall + rise)))
    return out


def _consistency(votes: list[tuple[float, float]], value: dict[str, Any]) -> float:
    """Mean of the strips' combined votes over its standard error; 0 with too few."""
    if len(votes) < value["min_strips"]:
        return 0.0
    weight = value["ascender_weight"]
    combined = [weight * a + (1 - weight) * b for a, b in votes]
    mean = sum(combined) / len(combined)
    spread = max(value["strip_vote_sd_floor"], pstdev(combined))
    return mean / (spread / math.sqrt(len(combined)))


def _ragged(ink: Image.Image, value: dict[str, Any]) -> tuple[float, int]:
    """Left-aligned starts against a ragged right edge, over lines separated by gaps
    across the whole width; 0 when fewer than min_lines such lines are found."""
    width, _ = ink.size
    rows = profile(ink, along_x=False)
    peak = max(rows) if rows else 0
    bands = [
        (a, b)
        for a, b in runs_of([level > value["line_floor_share"] * peak for level in rows])
        if b - a >= 2
    ]
    starts: list[int] = []
    ends: list[int] = []
    for a, b in bands:
        columns = profile(ink.crop((0, a, width, b)), along_x=True)
        filled = [i for i, level in enumerate(columns) if level]
        if filled:
            starts.append(filled[0])
            ends.append(filled[-1])
    if len(starts) < value["min_lines"]:
        return 0.0, len(bands)
    spread_start, spread_end = _mad(starts), _mad(ends)
    total = spread_start + spread_end
    if not total:
        return 0.0, len(bands)
    ragged = (spread_end - spread_start) / total
    ragged *= min(1.0, total / max(1e-9, value["ragged_min_spread_share"] * width))
    return ragged, len(bands)


def _guarded(estimates: list[float], value: dict[str, Any]) -> tuple[float, bool]:
    """The median estimate, and whether any estimate of the opposite sign reaches
    updown_guard_score standard errors (the vote then depends on where the core band
    is put)."""
    middle = median(estimates)
    guard = value["updown_guard_score"]
    return middle, any(e * middle < 0 and abs(e) >= guard for e in estimates)


def _updown(ink: Image.Image, value: dict[str, Any]) -> dict[str, Any]:
    """Positive score: upright. Negative: upside down. `ink` has its lines across.

    Each strip votes with its two line cues, and the votes are combined by how
    consistently they agree (mean over standard error). That measures agreement, not
    correctness: a core band misplaced the same way on every line would read as
    confidence. So the score is estimated several times, with the line core taken at
    three shares of each line's busiest row and at two strip widths, and every
    estimate is made antisymmetric by scoring the frame and its half turn and taking
    half the difference. The score is the median estimate; when an estimate of the
    opposite sign reaches updown_guard_score, the cues depend on where the core band is
    put, and the answer is uncertain."""
    flipped = ink.transpose(Image.Transpose.ROTATE_180)
    shares = (value["core_share_low"], value["core_share"], value["core_share_high"])
    sizes = (value["orient_strip_px"], value["orient_strip_px_wide"])
    estimates = []
    for size in sizes:
        for share in shares:
            forward = _consistency(_strip_votes(ink, size, share, value), value)
            backward = _consistency(_strip_votes(flipped, size, share, value), value)
            estimates.append((forward - backward) / 2)
    ragged_forward, lines = _ragged(ink, value)
    ragged_backward, _ = _ragged(flipped, value)
    ragged = (ragged_forward - ragged_backward) / 2
    estimates = [e + value["ragged_score_weight"] * ragged for e in estimates]
    middle, disagree = _guarded(estimates, value)
    core_heights: list[int] = []
    votes = _strip_votes(ink, sizes[0], shares[1], value, core_heights)
    back_votes = _strip_votes(flipped, sizes[0], shares[1], value)

    def mean_cue(index: int) -> float:
        forward = sum(v[index] for v in votes) / len(votes) if votes else 0.0
        backward = sum(v[index] for v in back_votes) / len(back_votes) if back_votes else 0.0
        return (forward - backward) / 2

    score = middle
    return {
        "score": score,
        "consistency": middle - value["ragged_score_weight"] * ragged,
        "low": min(estimates),
        "high": max(estimates),
        "disagree": disagree,
        "strips": len(votes),
        "ascender": mean_cue(0),
        "baseline": mean_cue(1),
        "ragged": ragged,
        "lines": lines,
        "core_px": median(core_heights) if core_heights else 0,
    }


def detect_orientation(
    image: Image.Image, settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Quarter turns clockwise that make `image` upright, as a detector answer."""
    value = setting_values(settings)
    work = working_copy(grey_of(image), value["orient_long_side_px"])
    levels = levels_of(work)
    if levels.contrast < value["min_ink_contrast"]:
        return answer(
            0,
            0.0,
            f"The page's dark and light levels differ by {levels.contrast:.0f} grey levels, "
            f"below {value['min_ink_contrast']}; there is no writing to orient by.",
            [TOO_LITTLE_INK],
        )
    negative, survival = looks_negative(work, levels, value)
    if negative:
        return answer(
            0,
            0.0,
            f"{levels.dark_share:.0%} of the frame is in the dark class and only "
            f"{survival:.0%} of the light class survives an erosion wider than a stroke, "
            "so the light class looks like writing on a dark ground.",
            [POSSIBLY_NEGATIVE],
        )
    ink = ink_map(work, levels.threshold)
    ink = ink.crop(_trim_dark_border(ink, value["edge_dark_share"]))
    ink = mask_straight_marks(ink, value["long_mark_share"])
    marks = [
        m
        for m in marks_of(ink)
        if m.count >= value["speck_px"] and not is_long_thin(m, ink.size, value)
    ]
    kept = sum(m.count for m in marks)
    area = ink.size[0] * ink.size[1]
    if len(marks) < value["min_marks"] or kept < value["min_ink_share"] * area:
        return answer(
            0,
            0.0,
            f"Only {len(marks)} marks ({kept / max(1, area):.2%} of the page) remain after "
            "specks and straight lines are removed; there is too little writing to orient by.",
            [TOO_LITTLE_INK],
        )
    clean = paint(marks, ink.size)
    box = clean.getbbox()
    clean = clean.crop(box)
    columns_share, block_marks = _layout(marks, box, value)
    if columns_share >= value["columns_ink_share"]:
        return answer(
            0,
            UNCERTAIN_SCALE * 0.25,
            f"{columns_share:.0%} of the ink lies in narrow columns of items beside a block "
            "of writing (amounts in columns, as on an account page); such columns read as "
            f"lines either way, so orientation is not decided from them; {DEFAULT_NOTE}.",
            [UNCERTAIN_COLUMNS],
        )
    voting = marks
    if block_marks is not None:
        # Only the block of writing votes; the columns of items beside it do not.
        voting = block_marks
        clean = paint(voting, ink.size).crop(box)
    direction = _direction(clean, voting, value, (box[0], box[1]))
    margin = math.log(value["direction_margin"])
    score = direction["score"]
    across_confidence = strength(abs(score), margin)
    if direction["tiles"]:
        basis = (
            f"median over {direction['tiles']} tiles of writing, "
            f"{direction['dissent']:.0%} of them voting the other way"
        )
        if direction["uniform_tiles"]:
            basis += f"; {direction['uniform_tiles']} tiles of one-size marks left out"
    else:
        basis = "whole content, too few tiles of writing"
    profiles = (
        f"row against column sharpness {direction['profile']:+.2f} ({basis}), "
        f"{direction['wide']} marks wider than tall and {direction['tall']} taller than wide"
    )
    if abs(score) < margin:
        return answer(
            0,
            UNCERTAIN_SCALE * across_confidence,
            f"Lines across or down too close to call ({profiles}); {DEFAULT_NOTE}.",
            [UNCERTAIN_DIRECTION],
        )
    if direction["dissent"] > value["tile_dissent_share"]:
        return answer(
            0,
            UNCERTAIN_SCALE * min(across_confidence, 0.49),
            f"Lines across or down not decided ({profiles}): more than "
            f"{value['tile_dissent_share']:.0%} of the tiles of writing vote against the "
            f"median, as on a page mixing writing with columns of figures; {DEFAULT_NOTE}.",
            [UNCERTAIN_DIRECTION],
        )
    if score > 0:
        base, lines, frame = 0, "across", clean
    else:
        base, lines, frame = 1, "down", clean.transpose(Image.Transpose.ROTATE_270)
    uniform = _uniform_share(marks, value)
    if uniform >= value["same_size_share"]:
        return answer(
            0,
            UNCERTAIN_SCALE * 0.25,
            f"Lines seem to run {lines} the frame ({profiles}), but {uniform:.0%} of the "
            f"marks share one height (or one width) to within "
            f"{value['same_size_tolerance']:.0%}: figures or capitals in aligned columns, "
            "with no ascenders or descenders; columns of such marks look like lines either "
            f"way, so neither the line direction nor up and down is decided; {DEFAULT_NOTE}.",
            [UNCERTAIN_UNIFORM],
        )
    uniform_tiles = direction["uniform_tiles"]
    tile_share = uniform_tiles / max(1, uniform_tiles + direction["voting_tiles"])
    if tile_share >= value["uniform_tile_share"]:
        return answer(
            0,
            UNCERTAIN_SCALE * 0.25,
            f"Lines run {lines} the frame ({profiles}), but {tile_share:.0%} of the tiles "
            "holding writing are figures or capitals of one size, which carry their own lean "
            f"between upright and upside down; {DEFAULT_NOTE}.",
            [UNCERTAIN_FIGURE_TILES],
        )
    if direction["left_out"]:
        # The marks of the one-size tiles vote neither on the line direction nor here.
        kept = [m for m in voting if id(m) not in direction["left_out"]]
        frame = paint(kept, ink.size).crop(box)
        if base == 1:
            frame = frame.transpose(Image.Transpose.ROTATE_270)
    updown = _updown(frame, value)
    vote = updown["score"]
    updown_confidence = strength(abs(vote), value["updown_min_score"])
    confidence = min(across_confidence, updown_confidence)
    cues = (
        f"up-down score {vote:+.2f}: strips agreeing at a median of "
        f"{updown['consistency']:+.2f} standard errors over {updown['strips']} strips "
        f"(from {updown['low']:+.2f} to {updown['high']:+.2f} as the line core band and "
        f"strip width vary; ascenders against descenders {updown['ascender']:+.2f}, "
        f"baseline against x-line {updown['baseline']:+.2f}) "
        f"and ragged right edge {updown['ragged']:+.2f} over {updown['lines']} lines"
        + ("" if value["ragged_score_weight"] else " (not counted, weight 0)")
    )
    if abs(vote) < value["updown_min_score"]:
        return answer(
            0,
            UNCERTAIN_SCALE * confidence,
            f"Lines run {lines} the frame ({profiles}), but the {cues} is too weak to tell "
            f"upright from upside down; {DEFAULT_NOTE}.",
            [UNCERTAIN_UPDOWN],
        )
    if updown["disagree"]:
        return answer(
            0,
            UNCERTAIN_SCALE * min(confidence, 0.49),
            f"Lines run {lines} the frame ({profiles}), but the {cues} changes sign with "
            f"where the core band is put, so it cannot tell upright from upside down; "
            f"{DEFAULT_NOTE}.",
            [UNCERTAIN_CORE_BAND],
        )
    turns = base if vote > 0 else (base + 2) % 4
    verdict = {
        0: "it is upright",
        1: "one quarter turn clockwise makes it upright",
        2: "a half turn makes it upright",
        3: "three quarter turns clockwise make it upright",
    }[turns]
    return answer(
        turns, confidence, f"Lines run {lines} the frame ({profiles}); {cues}; {verdict}.", []
    )
