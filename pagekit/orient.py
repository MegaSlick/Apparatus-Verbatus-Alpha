"""Orientation: how many quarter turns clockwise make a page upright (spec 0003).

The detector works on a reduced working copy of the source image and writes no file.
It returns the answer shape of spec 0002: exactly ``value`` (0, 1, 2 or 3 quarter turns
clockwise), ``confidence`` (0 to 1), ``evidence`` (one plain sentence) and ``flags``
(plain reasons to review; empty when none). When it is not sure it answers 0 turns
and says why in a flag; it never guesses silently.

Method, in two steps:

1. Lines across or down. Lines of writing make the ink profile across rows sharply
   peaked while the profile across columns is flatter; the direction whose profile
   varies more sharply (squared coefficient of variation) is the direction the lines
   run. Marks wider than tall are a second cue. Rules, page edges and the dark border
   are masked first. After H. S. Baird, "The skew angle of printed documents",
   Proc. SPSE Symposium on Hybrid Imaging Systems, 1987, which scores projection
   profiles by how sharply they vary.
2. Upright or upside down. Within each line, the ink above the core band (ascenders)
   is weighed against the ink below it (descenders); Latin-script hands carry more
   above. Line starts are left-aligned, so the ragged edge is normally on the right.
   Each cue votes with a strength. After R. S. Caprari, "Algorithm for text page up/down
   orientation determination", Pattern Recognition Letters 21(4):311-317, 2000.

A blank or nearly blank page (finding 0021, with the correction in clean-room log
entry 0014) answers 0 turns with confidence 0 and a flag. A frame that looks like light
writing on a dark ground (finding 0013) is flagged as possibly negative and its
orientation is not decided.

Limits: the line bands are found from the row profile alone, so lines whose ascenders
and descenders touch are scored as one line; facing pages whose lines are out of step
blur the profile; writing that is not left-aligned weakens the ragged-edge cue. All
settings are unmeasured guesses (``thresholds_split.toml``).
"""

from __future__ import annotations

import math
from statistics import median
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


def _direction(ink: Image.Image, marks: list[Mark], value: dict[str, Any]) -> dict[str, Any]:
    """Positive score: lines run across. Negative: lines run down."""
    rows = _peakedness(profile(ink, along_x=False))
    columns = _peakedness(profile(ink, along_x=True))
    ratio = value["shape_ratio"]
    wide = sum(1 for m in marks if m.width >= ratio * m.height)
    tall = sum(1 for m in marks if m.height >= ratio * m.width)
    tiny = 1e-9
    score = math.log((rows + tiny) / (columns + tiny)) + value["shape_weight"] * math.log(
        (wide + 1) / (tall + 1)
    )
    return {"score": score, "rows": rows, "columns": columns, "wide": wide, "tall": tall}


def _mad(values: list[int]) -> float:
    centre = median(values)
    return median(abs(v - centre) for v in values)


def _updown(ink: Image.Image, value: dict[str, Any]) -> dict[str, Any]:
    """Positive vote: upright. Negative: upside down. `ink` has its lines across."""
    width, _ = ink.size
    rows = profile(ink, along_x=False)
    peak = max(rows) if rows else 0
    bands = [
        (a, b)
        for a, b in runs_of([level > value["line_floor_share"] * peak for level in rows])
        if b - a >= 2
    ]
    cores: list[tuple[int, int]] = []
    for a, b in bands:
        segment = rows[a:b]
        floor = value["core_share"] * max(segment)
        busy = [i for i, level in enumerate(segment) if level >= floor]
        cores.append((a + busy[0], a + busy[-1] + 1))
    above = below = 0.0
    starts: list[int] = []
    ends: list[int] = []
    for index, (low, high) in enumerate(cores):
        reach = round(1.5 * (high - low)) + 1
        upper = low - reach
        if index:
            upper = max(upper, (cores[index - 1][1] + low + 1) // 2)
        lower = high + reach
        if index + 1 < len(cores):
            lower = min(lower, (high + cores[index + 1][0]) // 2)
        upper, lower = max(0, upper), min(len(rows), lower)
        above += sum(rows[upper:low])
        below += sum(rows[high:lower])
        columns = profile(ink.crop((0, upper, width, lower)), along_x=True)
        filled = [i for i, level in enumerate(columns) if level]
        if filled:
            starts.append(filled[0])
            ends.append(filled[-1])
    ascender = (above - below) / (above + below) if above + below else 0.0
    ragged = 0.0
    if len(starts) >= value["min_lines"]:
        spread_start, spread_end = _mad(starts), _mad(ends)
        total = spread_start + spread_end
        if total:
            ragged = (spread_end - spread_start) / total
            ragged *= min(1.0, total / max(1e-9, value["ragged_min_spread_share"] * width))
    weight = value["ascender_weight"]
    if len(starts) >= value["min_lines"]:
        vote = weight * ascender + (1 - weight) * ragged
    else:
        vote = ascender
    return {"vote": vote, "ascender": ascender, "ragged": ragged, "lines": len(bands)}


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
    profiles = (
        f"row profile sharpness {direction['rows']:.2f} against column "
        f"{direction['columns']:.2f}, {direction['wide']} marks wider than tall and "
        f"{direction['tall']} taller than wide"
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
    vote = updown["vote"]
    updown_confidence = strength(abs(vote), value["updown_min_vote"])
    confidence = min(across_confidence, updown_confidence)
    cues = (
        f"up-down vote {vote:+.2f} from ascenders against descenders "
        f"{updown['ascender']:+.2f} and ragged right edge {updown['ragged']:+.2f} "
        f"over {updown['lines']} lines"
    )
    if abs(vote) < value["updown_min_vote"]:
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
