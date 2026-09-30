"""The text part of one whole-page Perlector request, rendered from its page feed.

The request is the page image (when the feed shows one) as the first content
item, as `live_reader.py` sends a page render, then the page overlay when the
feed draws one (`page_overlay.py`), then this text: the feed's
witness units, Surya's lines and blocks, and last the instruction, so the
instruction is never the reader's first framing.

The instruction is rendered from the feed too (`page_reading_instruction`): it
names only the inputs the feed shows, and says to read from the image only
when an image is shown, so a switched-off input is never referred to.

A builder is registered per serving recipe as `prompts.py` registers act
builders, and a recipe with none refuses rather than borrowing another's
template. The rendered text reads only the feed fields that describe what is
shown -- never `prompt`, `feed_digest`, `unit_kind` or `findings` -- so the
same bytes are rebuilt from a sealed feed.

Each unit is one line: its id, `[x0,y0,x1,y1]` (its box_1000) when coordinates
are shown, its label as a JSON string in parentheses when it has one, and its
text as a JSON string. The text is the unit's text as the feed holds it -- for
Chandra, its text view of its blocks: markup removed, character references
resolved and whitespace runs made one space (`chandra-layout-text.v1`) -- with
only JSON's own escapes, so where a label or a unit ends and the next begins is
never in doubt. Under `witness_units = "flat"` a witness is one line: the range
of its unit ids, then its units' texts joined by newlines as one JSON string.

    text = build_page_prompt(serving_recipe, feed)
    evidence = page_prompt_evidence(serving_recipe, feed)
    # {serving_recipe, builder_sha256, rendered_sha256, instruction_sha256}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Final

from common.contracts.canonical import code_digest, digest_bytes

BUILDER_SHA256: Final[str] = code_digest(Path(__file__).resolve().read_text(encoding="utf-8"))

# The doubt-mark sentences are the act instruction's
# (`prompts.TRANSCRIPTION_INSTRUCTION`) word for word, so
# `annotations.read_doubt_marks` reads both the same way.
TRANSCRIBE_SENTENCE: Final = (
    "Transcribe the ink exactly as it is written on the page. Do not modernize spelling, "
    "expand abbreviations, or correct the scribe. "
)
DOUBT_SENTENCE: Final = (
    "Where ink cannot be read, write [[?]] in its place. Where a reading is uncertain, "
    "write it as [[reading]], or as [[reading|other|other]] to add other possible readings. "
)
ANSWER_FORM: Final = (
    '{"acts": [{"n": 1, "kind": "act", "label": "baptism", "cites": ["A2", "B3", "L10-L17"], '
    '"text": "...", "continues_from_previous_page": false, "continues_to_next_page": false}, '
    '{"n": 2, "kind": "other", "label": "page number", "cites": ["A3", "L18"], "text": "12", '
    '"continues_from_previous_page": false, "continues_to_next_page": false}], '
    '"set_aside": [{"id": "L19", "reason": "not text"}]}'
)


def _box(box: list[int] | None) -> str:
    return "" if box is None else " [" + ",".join(str(value) for value in box) + "]"


def _text(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _label(label: str | None) -> str:
    return "" if label is None else f" ({_text(label)})"


def _shown(feed: dict[str, Any]) -> dict[str, bool]:
    """Which inputs this feed shows, the one source for the text and the instruction."""
    rows = [row for row in feed["witnesses"] if row["units"]]
    surya = feed["surya"]
    return {
        "image": feed["page_render"] is not None,
        "overlay": feed["overlay"] is not None,
        "witnesses": bool(rows),
        "flat": feed["switches"]["witness_units"] == "flat",
        "witness_boxes": any(unit["box_1000"] is not None for row in rows for unit in row["units"]),
        "lines": surya is not None and bool(surya["lines"]),
        "blocks": surya is not None and bool(surya["blocks"]),
    }


def _witness_lines(row: dict[str, Any], flat: bool) -> list[str]:
    units = row["units"]
    if flat:
        ids = units[0]["id"] if len(units) == 1 else f"{units[0]['id']}-{units[-1]['id']}"
        return [f"{ids} {_text(chr(10).join(unit['text'] for unit in units if unit['text']))}"]
    return [
        f"{unit['id']}{_box(unit['box_1000'])}{_label(unit['label'])} {_text(unit['text'])}"
        for unit in units
    ]


def _feed_lines(feed: dict[str, Any]) -> list[str]:
    """The shown inputs of one feed, in the shape every page builder shares."""
    shown = _shown(feed)
    lines = [f"page {feed['page_ordinal']}"]
    if shown["image"]:
        lines.append("first image: the page.")
    else:
        lines.append("page image: not shown.")
    if shown["overlay"]:
        lines.append(
            "second image: the same page with each boxed id below outlined and labelled with "
            "its id, to show where each clue lies; read the ink from the first image."
        )
    lines.append(f"witness regime: {feed['witness_regime']}")
    if feed["witnesses"]:
        if shown["flat"]:
            lines.append(
                "witnesses: what other readers transcribed from this page, one line per "
                "witness: the range of its unit ids, then all its text as one JSON string."
            )
        else:
            box = " its box_1000," if shown["witness_boxes"] else ""
            lines.append(
                "witnesses: what other readers transcribed from this page, each in its own "
                f"units. Each line is one unit: its id,{box} its label as a JSON string in "
                "parentheses where it has one, and its text as a JSON string. A unit labelled "
                '"outside units" is text the witness wrote outside its own units.'
            )
    for witness in feed["witnesses"]:
        if witness["outcome"] != "read":
            lines.append(
                f"witness {witness['letter']} ({witness['witness_label']}): "
                f"{witness['outcome']}, no units"
            )
            continue
        lines.append(f"witness {witness['letter']} ({witness['witness_label']})")
        if witness["units"]:
            lines.extend(_witness_lines(witness, shown["flat"]))
    surya = feed["surya"]
    if shown["lines"]:
        lines.append("surya lines: text lines a layout detector found, with their box_1000.")
        lines.extend(f"{line['id']}{_box(line['box_1000'])}" for line in surya["lines"])
    if shown["blocks"]:
        lines.append(
            "surya blocks: layout blocks the same detector found, in its reading order, "
            "with their box_1000 and label."
        )
        lines.extend(
            f"{block['id']}{_box(block['box_1000'])}{_label(block['label'])}"
            for block in surya["blocks"]
        )
    if shown["witness_boxes"] or shown["lines"] or shown["blocks"]:
        lines.append(
            "coordinates: box_1000 = [x0,y0,x1,y1] on a 0-1000 grid of the page, from its "
            "top-left corner."
        )
    return lines


def _listed(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def page_reading_instruction(feed: dict[str, Any]) -> str:
    """What the Perlector is asked to do with this page, naming only what the feed shows."""
    shown = _shown(feed)
    clues = [
        name
        for name, present in (
            ("the witness units", shown["witnesses"]),
            ("the detected lines", shown["lines"]),
            ("the detected blocks", shown["blocks"]),
        )
        if present
    ]
    kinds = [
        name
        for name, present in (
            ("unit", shown["witnesses"]),
            ("line", shown["lines"]),
            ("block", shown["blocks"]),
        )
        if present
    ]
    parts = []
    if shown["image"]:
        parts.append("Read this page from its ink in the page image. ")
        if clues:
            named = _listed(clues)
            parts.append(
                f"{named[0].upper()}{named[1:]} above are clues to help you find and read the "
                "ink; any of them may be wrong, incomplete or in disagreement with the others, "
                "and none of them is an answer. "
            )
    else:
        parts.append(
            "No page image is shown. Read this page from "
            + (_listed(clues) if clues else "what is shown")
            + " above; any of them may be wrong, incomplete or in disagreement with the "
            "others. "
        )
    parts.append(
        "Establish all the text written on the page, entry by entry, in the order it is "
        "written, numbered n = 1, 2, 3 and so on. An act is one register entry, such as a "
        'baptism, a marriage or a burial: give it kind "act". Every other text on the page, '
        "such as a heading, a page number or a marginal note that is not an entry, is read "
        'too, as an entry of kind "other". '
    )
    cite = (
        f"cites, the ids of every {_listed(kinds)} its ink covers, where a range such as "
        "L10-L17 stands for every id of that letter from the first to the last; "
        if kinds
        else "cites, an empty list, since no ids are shown; "
    )
    parts.append(
        f"For each entry give: {cite}label, if you wish, a few words naming the entry, at "
        "most 80 characters; and text, the entry transcribed from the ink. "
    )
    if shown["witnesses"] and shown["flat"]:
        parts.append("A witness shown on one line is cited by the range of ids before its text. ")
    parts.append(TRANSCRIBE_SENTENCE)
    if shown["image"]:
        parts.append(
            "Read only what the page image shows: where an entry's ink runs past the edge of "
            "the image, stop at the edge and write [[?]] there. "
        )
    parts.append(DOUBT_SENTENCE)
    if kinds:
        parts.append(
            "Every id shown above is either cited by an entry or set aside. Set aside only an "
            "id whose ink you do not read at all -- one that is empty, is not text, or repeats "
            'the ink of another id -- with a short reason, such as "empty", "not text" or '
            '"same ink as L12". '
        )
    parts.append(
        "Set continues_from_previous_page to true only on the first entry, when it began on an "
        "earlier page, and continues_to_next_page to true only on the last entry, when it runs "
        "onto the next page; every other value of both is false. "
        "Answer with one JSON object and nothing else, with no code fence, in this form: "
        + ANSWER_FORM
    )
    return "".join(parts)


def _fake_perlector_page_v0(feed: dict[str, Any]) -> str:
    """The fixture recipe's page template: the shown inputs only."""
    return "\n".join(_feed_lines(feed))


def _unproven_real_perlector_page_v0(feed: dict[str, Any]) -> str:
    """`unproven-real-perlector`'s page template: the shown inputs, then the instruction."""
    return "\n".join([*_feed_lines(feed), page_reading_instruction(feed)])


_Render = Callable[[dict[str, Any]], str]
# Each recipe's builder and the instruction it sends, `None` where it sends none.
_BUILDERS: Final[dict[str, tuple[_Render, _Render | None]]] = {
    "fake-perlector-v0": (_fake_perlector_page_v0, None),
    "unproven-real-perlector": (_unproven_real_perlector_page_v0, page_reading_instruction),
}


def _builder_for(serving_recipe: str) -> tuple[_Render, _Render | None]:
    entry = _BUILDERS.get(serving_recipe)
    if entry is None:
        raise ValueError(
            f"no declared page prompt builder is registered for serving recipe "
            f"{serving_recipe!r}; a chair with no registered builder is never silently served "
            "a default template"
        )
    return entry


def build_page_prompt(serving_recipe: str, feed: dict[str, Any]) -> str:
    """The text part of one page request, byte-exact, or a refusal by name."""
    return _builder_for(serving_recipe)[0](feed)


def page_prompt_evidence(serving_recipe: str, feed: dict[str, Any]) -> dict[str, str | None]:
    """The record of the prompt a page reading is produced through.

    `builder_sha256` is this module's code digest (comments and docstrings
    stripped), so an edit to the instruction or the rendering changes it;
    `instruction_sha256` names the instruction the recipe sent for this feed,
    `None` for a recipe that sends none.
    """
    builder, instruction = _builder_for(serving_recipe)
    rendered = builder(feed)
    return {
        "serving_recipe": serving_recipe,
        "builder_sha256": BUILDER_SHA256,
        "rendered_sha256": digest_bytes(rendered.encode("utf-8")),
        "instruction_sha256": None
        if instruction is None
        else digest_bytes(instruction(feed).encode("utf-8")),
    }
