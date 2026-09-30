"""The text part of one whole-page Perlector request, rendered from its page feed.

The request is the page image (when the feed shows one) as the first content
item, as `live_reader.py` sends a page render, then the page overlay when the
feed draws one (`page_overlay.py`), then this text: the feed's
witness units, Surya's lines and blocks, and last the pinned instruction, so
the instruction is never the reader's first framing.

A builder is registered per serving recipe as `prompts.py` registers act
builders, and a recipe with none refuses rather than borrowing another's
template. The rendered text reads only the feed fields that describe what is
shown -- never `prompt` or `feed_digest` -- so the same bytes are rebuilt from
a sealed feed.

Each unit is one line: its id, `[x0,y0,x1,y1]` (its box_1000) when coordinates
are shown, its label in parentheses when it has one, and its text as a JSON
string, exactly as the witness gave it with only JSON's own escapes, so where
one unit ends and the next begins is never in doubt.

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

# What the Perlector is asked to do with a page. The doubt-mark sentences are
# the act instruction's (`prompts.TRANSCRIPTION_INSTRUCTION`) word for word, so
# `annotations.read_doubt_marks` reads both the same way.
PAGE_READING_INSTRUCTION: Final = (
    "Read this page from its ink. The witness units and the detected lines and blocks above "
    "are clues to help you find and read the ink; any of them may be wrong, incomplete or in "
    "disagreement with the others, and none of them is an answer. "
    "Establish every act written on the page. An act is one register entry, such as a "
    'baptism, a marriage or a burial: give it kind "act". Give any other text on the page, '
    'such as a heading, a page number or a note that is not an entry, kind "other". List the '
    "entries in the order they are written on the page, numbered n = 1, 2, 3 and so on. "
    "For each entry give: cites, the ids of every unit, line and block its ink covers, where "
    "a range such as L10-L17 stands for every id of that letter from the first to the last; "
    "label, if you wish, a few words naming the entry, at most 80 characters; and text, the "
    "entry transcribed from the ink. "
    "Transcribe the ink exactly as it is written on the page. Do not modernize spelling, "
    "expand abbreviations, or correct the scribe. "
    "Read only what the page image shows: where an entry's ink runs past the edge of the "
    "image, stop at the edge and write [[?]] there. "
    "Where ink cannot be read, write [[?]] in its place. Where a reading is uncertain, "
    "write it as [[reading]], or as [[reading|other|other]] to add other possible readings. "
    "Every id shown above is either cited by an entry or set aside: put each id you do not "
    'cite in set_aside with a short reason, such as "printed page number". '
    "Set continues_from_previous_page to true only on the first entry, when it began on an "
    "earlier page, and continues_to_next_page to true only on the last entry, when it runs "
    "onto the next page; every other value of both is false. "
    "Answer with one JSON object and nothing else, with no code fence, in this form: "
    '{"acts": [{"n": 1, "kind": "act", "label": "baptism", "cites": ["A2", "B3", "L10-L17"], '
    '"text": "...", "continues_from_previous_page": false, "continues_to_next_page": false}], '
    '"set_aside": [{"id": "C9", "reason": "printed page number"}]}'
)


def _box(box: list[int] | None) -> str:
    return "" if box is None else " [" + ",".join(str(value) for value in box) + "]"


def _label(label: str | None) -> str:
    return "" if label is None else f" ({label})"


def _text(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _feed_lines(feed: dict[str, Any]) -> list[str]:
    """The shown inputs of one feed, in the shape every page builder shares."""
    lines = [
        f"page {feed['page_ordinal']}",
        "page image: " + ("shown" if feed["page_render"] is not None else "not shown"),
    ]
    if feed["overlay"] is not None:
        lines.append(
            "second image: the same page with each boxed id below outlined and labelled with "
            "its id, to show where each clue lies; read the ink from the first image."
        )
    lines.append(f"witness regime: {feed['witness_regime']}")
    if feed["witnesses"]:
        lines.append(
            "witnesses: what other readers transcribed from this page, each in its own units. "
            "Each line is one unit: its id, its box_1000 where given, its label where given, "
            "and its text as the witness gave it."
        )
    for witness in feed["witnesses"]:
        if witness["outcome"] != "read":
            lines.append(
                f"witness {witness['letter']} ({witness['witness_label']}): "
                f"{witness['outcome']}, no units"
            )
            continue
        lines.append(f"witness {witness['letter']} ({witness['witness_label']})")
        lines.extend(
            f"{unit['id']}{_box(unit['box_1000'])}{_label(unit['label'])} {_text(unit['text'])}"
            for unit in witness["units"]
        )
    surya = feed["surya"]
    if surya is not None and surya["lines"]:
        lines.append("surya lines: text lines a layout detector found, with their box_1000.")
        lines.extend(f"{line['id']}{_box(line['box_1000'])}" for line in surya["lines"])
    if surya is not None and surya["blocks"]:
        lines.append(
            "surya blocks: layout blocks the same detector found, in its reading order, "
            "with their box_1000 and label."
        )
        lines.extend(
            f"{block['id']}{_box(block['box_1000'])}{_label(block['label'])}"
            for block in surya["blocks"]
        )
    lines.append(
        "coordinates: box_1000 = [x0,y0,x1,y1] on a 0-1000 grid of the page, from its "
        "top-left corner."
    )
    return lines


def _fake_perlector_page_v0(feed: dict[str, Any]) -> str:
    """The fixture recipe's page template: the shown inputs only."""
    return "\n".join(_feed_lines(feed))


def _unproven_real_perlector_page_v0(feed: dict[str, Any]) -> str:
    """`unproven-real-perlector`'s page template: the shown inputs, then the instruction."""
    return "\n".join([*_feed_lines(feed), PAGE_READING_INSTRUCTION])


# Each recipe's builder and the instruction it sends, `None` where it sends none.
_BUILDERS: Final[dict[str, tuple[Callable[[dict[str, Any]], str], str | None]]] = {
    "fake-perlector-v0": (_fake_perlector_page_v0, None),
    "unproven-real-perlector": (_unproven_real_perlector_page_v0, PAGE_READING_INSTRUCTION),
}


def _builder_for(serving_recipe: str) -> tuple[Callable[[dict[str, Any]], str], str | None]:
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
    `instruction_sha256` names the instruction the recipe sent, `None` for a
    recipe that sends none.
    """
    builder, instruction = _builder_for(serving_recipe)
    rendered = builder(feed)
    return {
        "serving_recipe": serving_recipe,
        "builder_sha256": BUILDER_SHA256,
        "rendered_sha256": digest_bytes(rendered.encode("utf-8")),
        "instruction_sha256": None
        if instruction is None
        else digest_bytes(instruction.encode("utf-8")),
    }
