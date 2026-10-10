"""The text part of one whole-page Perlector request, rendered from its page feed.

The request is the page image (when the feed shows one) as the first content
item, as `operations/serving/chat_request.py` sends a page render, then the page overlay when the
feed draws one (`page_overlay.py`), then this text: the feed's
witness units, Surya's lines and blocks, and last the instruction, so the
instruction is never the reader's first framing.

The instruction is rendered from the feed too (`page_reading_instruction`): it
names only the inputs the feed shows, and says to read from the image only
when an image is shown, so a switched-off input is never referred to. With no
image the reading is made from the witnesses' reports, with [[?]] where they
disagree or none reports the text. Every witness unit whose text is read is
cited, also where another witness's unit reads the same text; only an id with
nothing to read, or a Surya detection repeating another, is set aside, so the
instruction never invites a choice between witnesses.

A builder is registered per serving recipe, and a recipe with none refuses
rather than borrowing another's template; a recipe that only serves the same
model another way (`RECIPE_ALIASES`) is declared to take its base recipe's. The rendered text reads only the feed fields that describe what is
shown -- never `prompt`, `feed_digest`, `unit_kind` or `findings` -- so the
same bytes are rebuilt from a sealed feed.

Each witness has a line of its own before its units: its letter and label,
then what its `answer_health` shows -- that its answer was cut off, or repeats
one passage over and over. A witness that did not read says its outcome, and
one that read but has no unit says "read, no text"; the unit-format header is
printed only when some witness has a unit.

Each unit is one line: its id, `[x0,y0,x1,y1]` (its box_1000) when coordinates
are shown, its label as a JSON string in parentheses when it has one, and its
text as a JSON string. The text is the unit's text as the feed holds it -- for
Chandra, its text view of its blocks: markup removed, character references
resolved and whitespace runs made one space (`chandra-layout-text.v2`) -- with
only JSON's own escapes, so where a label or a unit ends and the next begins is
never in doubt. Under `witness_units = "flat"` a witness is one unit with no
box, on one line: the range of its unit ids, then its units' texts joined by
newlines as one JSON string.

    text = build_page_prompt(serving_recipe, feed)
    parts = prompt_parts(serving_recipe, feed)   # the same text, reported pieces marked
    evidence = page_prompt_evidence(serving_recipe, feed)
    # {serving_recipe, builder_sha256, rendered_sha256, instruction_sha256}

A page's one re-ask (`common/page_reask.py`) is rendered here too, with its
own builder per recipe: the same rendered feed, then the first reading's
entries by number, kind, label and cites -- never their text -- then the ids
it is asked about with their boxes, grouped by what the accounting found, and
last its own instruction.

    text = page_reask_prompt(serving_recipe, feed, reask)
    parts = reask_prompt_parts(serving_recipe, feed, reask)
    evidence = reask_prompt_evidence(serving_recipe, feed, reask)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Final

from common.contracts.canonical import code_digest, digest_bytes
from common.page_path import SURYA_ORDER_HEAD

BUILDER_SHA256: Final[str] = code_digest(Path(__file__).resolve().read_text(encoding="utf-8"))

# What the reader is asked to write, and how it marks ink it cannot read or is
# unsure of; `annotations.read_doubt_marks` reads exactly these marks back.
TRANSCRIBE_SENTENCE: Final = (
    "Transcribe the ink exactly as it is written on the page. Do not modernize spelling, "
    "expand abbreviations, or correct the scribe. "
)
DOUBT_SENTENCE: Final = (
    "Where ink cannot be read, write [[?]] in its place. Where a reading is uncertain, "
    "write it as [[reading]], or as [[reading|other|other]] to add other possible readings. "
)
# The answer's shape with placeholders only, so it suggests no reading
# (`common.page_types`): the page's type and how it is written, then the entries.
ANSWER_FORM: Final = (
    '{"page_type": "<page type>", "writing": "<how the page is written>", '
    '"entries": [{"n": 1, "kind": "<entry kind>", "label": "<a few words>", '
    '"cites": ["<id>", "<first unit id>-<last unit id>"], "text": "<the entry\'s text>", '
    '"continues_from_previous_page": false, "continues_to_next_page": false}], '
    '"set_aside": [{"id": "<id>", "reason": "<short reason>"}]}'
)
# A re-ask reads ids inside a page already typed, so it names no page type.
REASK_ANSWER_FORM: Final = (
    '{"entries": [{"n": 1, "kind": "<entry kind>", "label": "<a few words>", '
    '"cites": ["<id>", "<first unit id>-<last unit id>"], "text": "<the entry\'s text>", '
    '"continues_from_previous_page": false, "continues_to_next_page": false}], '
    '"set_aside": [{"id": "<id>", "reason": "<short reason>"}]}'
)
# What each page type and entry kind is, as the reader is told.
PAGE_TYPE_SENTENCE: Final = (
    "First name the page's type, page_type: "
    '"register-acts" for a parish or civil register of acts (baptisms, marriages, burials '
    "and other registered acts); "
    '"index" for an index or table of names that points to acts elsewhere; '
    '"table" for a list or census-like table of rows, such as a family or wage list; '
    '"ledger" for accounts or a journal of dated entries with amounts; '
    '"instrument" for a notarial act, a contract, an engagement or a filled-in form; '
    '"prose" for a letter, a note, an attestation or other running text; '
    '"blank" for a page with no writing. '
    'Name how the page is written, writing: "handwritten", "typed", "printed" or "mixed". '
)
ENTRY_KIND_SENTENCE: Final = (
    "Give each entry a kind: "
    '"act" for one registered act, such as a baptism, a marriage or a burial, with its '
    "margin note and its signatures; "
    '"index-row" for one row of an index; '
    '"table-row" for one row of a table or list; '
    '"ledger-entry" for one entry of accounts; '
    '"instrument" for one notarial act or contract, whole; '
    '"paragraph" for one paragraph of running text; '
    '"other" for every other text on the page, such as a heading, a page number or a '
    "marginal note that is not an entry. "
)

# The repetition finding that says a witness's answer repeats itself.
REPEATING: Final = "post-hoc-repetition"


# A rendered line is a list of parts, each `(text, reported)`: `reported` marks
# a string a witness or detector wrote -- a unit's text or label, a witness
# label, a block label -- which the request's capacity check charges at one
# token per byte (`prompt_parts`); every other part is this module's own
# fixed wording, ids and numbers.
_Part = tuple[str, bool]


def _box(box: list[int] | None) -> _Part:
    return ("" if box is None else " [" + ",".join(str(value) for value in box) + "]", False)


def _text(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _label(label: str | None) -> list[_Part]:
    return [] if label is None else [(" (", False), (_text(label), True), (")", False)]


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


def _witness_lines(row: dict[str, Any], flat: bool) -> list[list[_Part]]:
    units = row["units"]
    if flat:
        ids = units[0]["id"] if len(units) == 1 else f"{units[0]['id']}-{units[-1]['id']}"
        joined = chr(10).join(unit["text"] for unit in units if unit["text"])
        return [[(f"{ids} ", False), (_text(joined), True)]]
    return [
        [
            (unit["id"], False),
            _box(unit["box_1000"]),
            *_label(unit["label"]),
            (" ", False),
            (_text(unit["text"]), True),
        ]
        for unit in units
    ]


def _health_notes(row: dict[str, Any]) -> str:
    """What the witness's own answer shows about itself: cut off, or repeating."""
    notes = []
    health = row["answer_health"]
    if health["truncated"] is True:
        notes.append("this witness's answer was cut off before it finished")
    if any(finding["kind"] == REPEATING for finding in health["repetition"]):
        notes.append("this witness's answer repeats one passage over and over")
    return "" if not notes else " -- " + "; ".join(notes)


def _fixed(text: str) -> list[_Part]:
    return [(text, False)]


def _feed_parts(feed: dict[str, Any]) -> list[list[_Part]]:
    """The shown inputs of one feed, in the shape every page builder shares."""
    shown = _shown(feed)
    lines = [_fixed(f"page {feed['page_ordinal']}")]
    if shown["image"]:
        lines.append(_fixed("first image: the page."))
    else:
        lines.append(_fixed("page image: not shown."))
    if shown["overlay"]:
        lines.append(
            _fixed(
                "second image: the same page with each boxed id below outlined and labelled "
                "with its id, to show where each clue lies; read the ink from the first image."
            )
        )
    lines.append(_fixed(f"witness regime: {feed['witness_regime']}"))
    if shown["witnesses"]:
        if shown["flat"]:
            lines.append(
                _fixed(
                    "witnesses: what other readers transcribed from this page, one line per "
                    "witness: the range of its unit ids, then all its text as one JSON string."
                )
            )
        else:
            box = " its box_1000," if shown["witness_boxes"] else ""
            lines.append(
                _fixed(
                    "witnesses: what other readers transcribed from this page, each in its own "
                    f"units. Each line is one unit: its id,{box} its label as a JSON string in "
                    "parentheses where it has one, and its text as a JSON string. A unit "
                    'labelled "outside units" is text the witness wrote outside its own units.'
                )
            )
    for witness in feed["witnesses"]:
        head = [
            (f"witness {witness['letter']} (", False),
            (witness["witness_label"], True),
            (")", False),
        ]
        if witness["outcome"] != "read":
            lines.append([*head, (f": {witness['outcome']}, no units", False)])
            continue
        if not witness["units"]:
            lines.append([*head, (": read, no text" + _health_notes(witness), False)])
            continue
        lines.append([*head, (_health_notes(witness), False)])
        lines.extend(_witness_lines(witness, shown["flat"]))
    surya = feed["surya"]
    if shown["lines"]:
        lines.append(
            _fixed("surya lines: text lines a layout detector found, with their box_1000.")
        )
        lines.extend([(line["id"], False), _box(line["box_1000"])] for line in surya["lines"])
    if shown["blocks"]:
        # A raster fallback is Surya's own top-to-bottom sort, not a reading
        # order, and the prompt says so.
        order = (
            "in the reading order that detector predicted"
            if surya["block_sequence"] == SURYA_ORDER_HEAD
            else "in raster order (top to bottom, then left to right), not a reading order"
        )
        lines.append(
            _fixed(
                f"surya blocks: layout blocks the same detector found, {order}, "
                "with their box_1000 and label."
            )
        )
        lines.extend(
            [(block["id"], False), _box(block["box_1000"]), *_label(block["label"])]
            for block in surya["blocks"]
        )
    if shown["witness_boxes"] or shown["lines"] or shown["blocks"]:
        lines.append(
            _fixed(
                "coordinates: box_1000 = [x0,y0,x1,y1] on a 0-1000 grid of the page, from its "
                "top-left corner."
            )
        )
    return lines


def _rendered(lines: list[list[_Part]]) -> str:
    return "\n".join("".join(text for text, _reported in line) for line in lines)


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
    detections = shown["lines"] or shown["blocks"]
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
            "No page image is shown. The reading is made from what was reported of this page: "
            f"{_listed(clues)} above. Any of them may be wrong or incomplete. "
        )
    parts.append(
        "Establish all the text written on the page, entry by entry, in the order it is "
        "written, numbered n = 1, 2, 3 and so on. "
    )
    parts.append(PAGE_TYPE_SENTENCE)
    parts.append(ENTRY_KIND_SENTENCE)
    parts.append("Every text on the page is read, whatever its kind. ")
    covers = "its ink covers" if shown["image"] else "it is read from"
    ranges = []
    if shown["witnesses"]:
        letter = next(row["letter"] for row in feed["witnesses"] if row["units"])
        ranges.append(
            f"where a range of witness units such as {letter}2-{letter}5 stands for every "
            "unit of that witness from the first to the last"
        )
    if detections:
        detected = _listed([name for name in ("line", "block") if shown[f"{name}s"]])
        ranges.append(f"each detected {detected} cited by its own id, never by a range")
    cite = (
        f"cites, the ids of every {_listed(kinds)} {covers}, {', and '.join(ranges)}; "
        if kinds
        else "cites, an empty list, since no ids are shown; "
    )
    text = (
        "text, the entry transcribed from the ink"
        if shown["image"]
        else "text, the entry as the witnesses report it"
    )
    parts.append(
        f"For each entry give: {cite}label, if you wish, a few words naming the entry, at "
        f"most 80 characters; and {text}. "
    )
    if shown["witnesses"] and shown["flat"]:
        parts.append(
            "A witness shown on one line is one unit with no box: cite it by the range of ids "
            "before its text. "
        )
    if shown["image"]:
        parts.append(TRANSCRIBE_SENTENCE)
        parts.append(
            "Read only what the page image shows: where an entry's ink runs past the edge of "
            "the image, stop at the edge and write [[?]] there. "
        )
        parts.append(DOUBT_SENTENCE)
    else:
        parts.append(
            "Give each text as the witnesses report it: do not modernize spelling, expand "
            "abbreviations, or correct it. Where the witnesses disagree about a reading, or "
            "none of them reports it, write [[?]] in its place. "
        )
    if kinds:
        parts.append(
            "Every id shown above is either cited by an entry or set aside. "
            + (
                "Cite every witness unit whose text you read, also where another witness's "
                "unit reads the same text. "
                if shown["witnesses"]
                else ""
            )
            + "Set aside only an id with nothing to read -- one that is empty or is not text"
            + (
                " -- or a detected line or block that repeats another detection"
                if detections
                else ""
            )
            + ', with a short reason, such as "empty" or "not text". '
        )
    parts.append(
        "Set continues_from_previous_page to true only on the first act or instrument, when "
        "it began on an earlier page, and continues_to_next_page to true only on the last act "
        "or instrument, when it runs onto the next page; every other value of both is false. "
        "Answer with one JSON object and nothing else, with no code fence, in this form: "
        + ANSWER_FORM
    )
    return "".join(parts)


def _fake_perlector_page_v0(feed: dict[str, Any]) -> list[list[_Part]]:
    """The fixture recipe's page template: the shown inputs only."""
    return _feed_parts(feed)


def _unproven_real_perlector_page_v0(feed: dict[str, Any]) -> list[list[_Part]]:
    """`unproven-real-perlector`'s page template: the shown inputs, then the instruction."""
    return [*_feed_parts(feed), _fixed(page_reading_instruction(feed))]


# Serving recipes that serve the Perlector's own model another way -- smaller weights,
# speculative decoding, an FP8 KV cache (config/serving_recipes_real_variants.toml) --
# and deliberately send the same prompt: each takes the page and re-ask builders of the
# recipe it varies, so a Perlector chair can point at it. The evidence still names the
# recipe asked for (`serving_recipe`); its rendered text is the base recipe's, byte for
# byte.
RECIPE_ALIASES: Final[dict[str, str]] = {
    "unproven-real-perlector-fp8": "unproven-real-perlector",
    "unproven-real-perlector-fp8-mtp3": "unproven-real-perlector",
    "unproven-real-perlector-fp8-mtp1": "unproven-real-perlector",
    "unproven-real-perlector-fp8-kvfp8": "unproven-real-perlector",
    "unproven-real-perlector-nvfp4": "unproven-real-perlector",
    "unproven-real-perlector-nvfp4-mtp3": "unproven-real-perlector",
}

_Build = Callable[[dict[str, Any]], list[list[_Part]]]
_Render = Callable[[dict[str, Any]], str]
# Each recipe's builder and the instruction it sends, `None` where it sends none.
_BUILDERS: Final[dict[str, tuple[_Build, _Render | None]]] = {
    "fake-perlector-v0": (_fake_perlector_page_v0, None),
    "unproven-real-perlector": (_unproven_real_perlector_page_v0, page_reading_instruction),
}


def _builder_for(serving_recipe: str) -> tuple[_Build, _Render | None]:
    entry = _BUILDERS.get(RECIPE_ALIASES.get(serving_recipe, serving_recipe))
    if entry is None:
        raise ValueError(
            f"no declared page prompt builder is registered for serving recipe "
            f"{serving_recipe!r}; a chair with no registered builder is never silently served "
            "a default template"
        )
    return entry


def build_page_prompt(serving_recipe: str, feed: dict[str, Any]) -> str:
    """The text part of one page request, byte-exact, or a refusal by name."""
    return _rendered(_builder_for(serving_recipe)[0](feed))


def prompt_parts(serving_recipe: str, feed: dict[str, Any]) -> list[tuple[str, bool]]:
    """The page prompt in its pieces, `(text, reported)`, joining to `build_page_prompt`'s text.

    `reported` marks each string a witness or detector wrote -- a unit's text
    or label, a witness label, a Surya block label -- which the capacity check
    charges at one token per byte (`request_capacity.page_request_capacity`).
    """
    parts: list[tuple[str, bool]] = []
    for index, line in enumerate(_builder_for(serving_recipe)[0](feed)):
        if index:
            parts.append(("\n", False))
        parts.extend(part for part in line if part[0])
    return parts


def page_prompt_evidence(serving_recipe: str, feed: dict[str, Any]) -> dict[str, str | None]:
    """The record of the prompt a page reading is produced through.

    `builder_sha256` is this module's code digest (comments and docstrings
    stripped), so an edit to the instruction or the rendering changes it;
    `instruction_sha256` names the instruction the recipe sent for this feed,
    `None` for a recipe that sends none.
    """
    builder, instruction = _builder_for(serving_recipe)
    rendered = _rendered(builder(feed))
    return {
        "serving_recipe": serving_recipe,
        "builder_sha256": BUILDER_SHA256,
        "rendered_sha256": digest_bytes(rendered.encode("utf-8")),
        "instruction_sha256": None
        if instruction is None
        else digest_bytes(instruction(feed).encode("utf-8")),
    }


# --- the re-ask ---------------------------------------------------------------------

# How the re-ask introduces each finding it names, in the order shown.
_REASK_FINDINGS: Final = (
    ("unaccounted-witness-unit", "witness units no entry above cites or sets aside"),
    ("unread-line", "detected lines outside every entry above"),
    ("record-not-read", "detector records outside every entry above"),
)


def _reask_parts(reask: dict[str, Any]) -> list[list[_Part]]:
    """The first reading's entries without their text, then the named ids by finding."""
    lines = [_fixed("entries already read on this page, which stand as they are:")]
    for entry in reask["prior_entries"]:
        lines.append(
            [
                (f"entry {entry['n']} ({entry['kind']})", False),
                *_label(entry["label"]),
                (" cites ", False),
                (_text(", ".join(entry["cites"])), True),
            ]
        )
    if not reask["prior_entries"]:
        lines.append(_fixed("(none)"))
    for code, heading in _REASK_FINDINGS:
        named = [item for item in reask["named"] if item["code"] == code]
        if named:
            lines.append(_fixed(f"{heading}:"))
            lines.extend([(item["id"], False), _box(item["box_1000"])] for item in named)
    return lines


def page_reask_instruction(feed: dict[str, Any]) -> str:
    """What the re-ask asks: read the ink at the named ids, adding to the entries above.

    It stands on its own, since the re-ask is a request of its own: what an
    entry is, what to give for it and how to cite, as the page's instruction
    says them, limited to the ids the re-ask names.
    """
    shown = _shown(feed)
    image = shown["image"]
    parts = ["The ids just above were not accounted for by the entries already read. "]
    if image:
        parts.append(
            "Read the ink at these ids in the page image. The witness units and detections "
            "above are clues to help you find and read the ink; any of them may be wrong, and "
            "none of them is an answer. "
        )
    else:
        parts.append(
            "No page image is shown: read what was reported at these ids. Any report may be "
            "wrong or incomplete. "
        )
    parts.append(
        ENTRY_KIND_SENTENCE + "For each entry you find at these "
        "ids give: n, numbered 1, 2, 3 and so on; kind; label, if you wish, a few words "
        "naming the entry, at most 80 characters; cites, the ids just above that "
        + ("its ink covers" if image else "it is read from")
        + ", where a range of witness units such as A2-A5 stands for every unit of that "
        "witness from the first to the last, and each detected line is cited by its own id, "
        "never by a range; and "
        + (
            "text, the entry transcribed from the ink. "
            if image
            else "text, the entry as the witnesses report it. "
        )
        + "Cite only the ids just above, and set continues_from_previous_page and "
        "continues_to_next_page to false. Set aside an id where you find no entry, with the "
        "reason the "
        + ("ink shows" if image else "reports show")
        + ". Do not repeat or change the entries already read. "
    )
    if image:
        parts += [TRANSCRIBE_SENTENCE, DOUBT_SENTENCE]
    else:
        parts.append(
            "Give each text as the witnesses report it: do not modernize spelling, expand "
            "abbreviations, or correct it. Where the witnesses disagree about a reading, or "
            "none of them reports it, write [[?]] in its place. "
        )
    parts.append(
        "Answer with one JSON object and nothing else, with no code fence, in this form: "
        + REASK_ANSWER_FORM
    )
    return "".join(parts)


_ReaskBuild = Callable[[dict[str, Any], dict[str, Any]], list[list[_Part]]]


def _fake_perlector_reask_v0(feed: dict[str, Any], reask: dict[str, Any]) -> list[list[_Part]]:
    """The fixture recipe's re-ask template: the shown inputs and the re-ask's data."""
    return [*_feed_parts(feed), *_reask_parts(reask)]


def _unproven_real_perlector_reask_v0(
    feed: dict[str, Any], reask: dict[str, Any]
) -> list[list[_Part]]:
    """`unproven-real-perlector`'s re-ask template: the same, then the re-ask's instruction."""
    return [*_feed_parts(feed), *_reask_parts(reask), _fixed(page_reask_instruction(feed))]


_REASK_BUILDERS: Final[dict[str, tuple[_ReaskBuild, _Render | None]]] = {
    "fake-perlector-v0": (_fake_perlector_reask_v0, None),
    "unproven-real-perlector": (_unproven_real_perlector_reask_v0, page_reask_instruction),
}


def _reask_builder_for(serving_recipe: str) -> tuple[_ReaskBuild, _Render | None]:
    entry = _REASK_BUILDERS.get(RECIPE_ALIASES.get(serving_recipe, serving_recipe))
    if entry is None:
        raise ValueError(
            f"no declared page re-ask builder is registered for serving recipe "
            f"{serving_recipe!r}; a chair with no registered builder is never silently served "
            "a default template"
        )
    return entry


def page_reask_prompt(serving_recipe: str, feed: dict[str, Any], reask: dict[str, Any]) -> str:
    """The text part of one page re-ask, byte-exact; `reask` is `page_reask.render_reask`'s."""
    return _rendered(_reask_builder_for(serving_recipe)[0](feed, reask))


def reask_prompt_parts(
    serving_recipe: str, feed: dict[str, Any], reask: dict[str, Any]
) -> list[tuple[str, bool]]:
    """The re-ask prompt in its pieces, `(text, reported)`, as `prompt_parts` gives a page's."""
    parts: list[tuple[str, bool]] = []
    for index, line in enumerate(_reask_builder_for(serving_recipe)[0](feed, reask)):
        if index:
            parts.append(("\n", False))
        parts.extend(part for part in line if part[0])
    return parts


def reask_prompt_evidence(
    serving_recipe: str, feed: dict[str, Any], reask: dict[str, Any]
) -> dict[str, str | None]:
    """The record of the prompt a page re-ask is produced through, as `page_prompt_evidence`."""
    builder, instruction = _reask_builder_for(serving_recipe)
    rendered = _rendered(builder(feed, reask))
    return {
        "serving_recipe": serving_recipe,
        "builder_sha256": BUILDER_SHA256,
        "rendered_sha256": digest_bytes(rendered.encode("utf-8")),
        "instruction_sha256": None
        if instruction is None
        else digest_bytes(instruction(feed).encode("utf-8")),
    }
