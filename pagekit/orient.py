"""Orientation: how many quarter turns clockwise make a page upright (spec 0003).

The detector works on a reduced working copy of the source image and writes no file.
It returns the answer shape of spec 0002: exactly ``value`` (0, 1, 2 or 3 quarter turns
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

A blank or nearly blank page (finding 0021, with the correction in clean-room log
entry 0014) answers 0 turns with confidence 0 and a flag. A frame that looks like light
writing on a dark ground (finding 0013) is flagged as possibly negative and its
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
# An uncertain step has confidence below 0.5; scaled by this, an uncertain answer
# reports below 0.2, because its 0 turns is a default, not a finding.
UNCERTAIN_SCALE = 0.4
DEFAULT_NOTE = "left at 0 turns as a default, not a finding, so its confidence is scaled below 0.2"
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


def _peakedness(values: list[int]) -> float:
    """Squared coefficient of variation: how sharply a profile varies."""
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return variance / (mean * mean)


def _tile_scores(ink: Image.Image, value: dict[str, Any]) -> list[float]:
    """ln(row sharpness / column sharpness) for each square tile that holds writing
    (ink share within the tile limits). Inside a tile of writing the row profile
    alternates lines and gaps while the column profile is flat; tiles of margin,
    gutter, page edges or dark masses are left out, so their structure does not pull
    the score."""
    width, height = ink.size
    size = value["orient_tile_px"]
    tiny = 1e-6
    scores = []
    for y in range(0, height - size + 1, size):
        for x in range(0, width - size + 1, size):
            tile = ink.crop((x, y, x + size, y + size))
            share = tile.histogram()[255] / (size * size)
            if not value["tile_min_ink"] <= share <= value["tile_max_ink"]:
                continue
            rows = _peakedness(profile(tile, along_x=False))
            columns = _peakedness(profile(tile, along_x=True))
            scores.append(math.log((rows + tiny) / (columns + tiny)))
    return scores


def _direction(ink: Image.Image, marks: list[Mark], value: dict[str, Any]) -> dict[str, Any]:
    """Positive score: lines run across. Negative: lines run down.

    The profile cue is the median over tiles of writing; when too few tiles hold
    writing it falls back to the profiles of the whole content."""
    tiles = _tile_scores(ink, value)
    rows = _peakedness(profile(ink, along_x=False))
    columns = _peakedness(profile(ink, along_x=True))
    tiny = 1e-9
    if len(tiles) >= value["min_tiles"]:
        profile_score = median(tiles)
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
        "rows": rows,
        "columns": columns,
        "wide": wide,
        "tall": tall,
    }


def _mad(values: list[int]) -> float:
    centre = median(values)
    return median(abs(v - centre) for v in values)


def _strip_votes(ink: Image.Image, value: dict[str, Any]) -> list[tuple[float, float]]:
    """For each vertical strip holding writing: the ascender-against-descender balance
    and the baseline-against-x-line sharpness of its lines, each from -1 to 1, positive
    when upright. A strip is narrow enough that a slight lean or lines out of step
    between facing pages do not blur its lines, and its core bands are found as the
    rows at or above core_share of its busy level, so lines whose ascenders and
    descenders touch still separate."""
    width, height = ink.size
    size = value["orient_strip_px"]
    out = []
    for x in range(0, width - size + 1, size):
        strip = ink.crop((x, 0, x + size, height))
        if strip.histogram()[255] / (size * height) < value["strip_min_ink"]:
            continue
        rows = profile(strip, along_x=False)
        busy = sorted(rows)[int(0.9 * (len(rows) - 1))]
        if busy <= 0:
            continue
        cores = [
            (a, b)
            for a, b in runs_of([level >= value["core_share"] * busy for level in rows])
            if b - a >= 2
        ]
        if len(cores) < 2:
            continue
        above = below = rise = fall = 0.0
        for index, (low, high) in enumerate(cores):
            reach = round(1.5 * (high - low)) + 1
            upper = low - reach if index == 0 else (cores[index - 1][1] + low + 1) // 2
            lower = high + reach if index + 1 == len(cores) else (high + cores[index + 1][0]) // 2
            upper, lower = max(0, upper), min(height, lower)
            above += sum(rows[upper:low])
            below += sum(rows[high:lower])
            rise += max(rows[i] - rows[i - 1] for i in range(max(1, low - 2), min(height, low + 3)))
            fall += max(
                rows[i - 1] - rows[i] for i in range(max(1, high - 2), min(height, high + 3))
            )
        if above + below and rise + fall:
            out.append(((above - below) / (above + below), (fall - rise) / (fall + rise)))
    return out


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


def _updown(ink: Image.Image, value: dict[str, Any]) -> dict[str, Any]:
    """Positive score: upright. Negative: upside down. `ink` has its lines across.

    Each strip votes with its two line cues; the strip votes are combined by how
    consistently they agree (their mean over its standard error), so a small but
    steady asymmetry over many strips counts and a large but erratic one does not.
    The ragged-edge cue adds its weight when enough lines are found."""
    votes = _strip_votes(ink, value)
    weight = value["ascender_weight"]
    ascender = baseline = consistency = 0.0
    if len(votes) >= value["min_strips"]:
        ascender = sum(a for a, _ in votes) / len(votes)
        baseline = sum(b for _, b in votes) / len(votes)
        combined = [weight * a + (1 - weight) * b for a, b in votes]
        mean = sum(combined) / len(combined)
        spread = max(value["strip_vote_sd_floor"], pstdev(combined))
        consistency = mean / (spread / math.sqrt(len(combined)))
    ragged, lines = _ragged(ink, value)
    score = consistency + value["ragged_score_weight"] * ragged
    return {
        "score": score,
        "consistency": consistency,
        "strips": len(votes),
        "ascender": ascender,
        "baseline": baseline,
        "ragged": ragged,
        "lines": lines,
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
    clean = clean.crop(clean.getbbox())
    direction = _direction(clean, marks, value)
    margin = math.log(value["direction_margin"])
    score = direction["score"]
    across_confidence = strength(abs(score), margin)
    if direction["tiles"]:
        basis = f"median over {direction['tiles']} tiles of writing"
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
    if score > 0:
        base, lines, frame = 0, "across", clean
    else:
        base, lines, frame = 1, "down", clean.transpose(Image.Transpose.ROTATE_270)
    updown = _updown(frame, value)
    vote = updown["score"]
    updown_confidence = strength(abs(vote), value["updown_min_score"])
    confidence = min(across_confidence, updown_confidence)
    cues = (
        f"up-down score {vote:+.2f} from {updown['strips']} strips agreeing at "
        f"{updown['consistency']:+.2f} standard errors (ascenders against descenders "
        f"{updown['ascender']:+.2f}, baseline against x-line {updown['baseline']:+.2f}) "
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
