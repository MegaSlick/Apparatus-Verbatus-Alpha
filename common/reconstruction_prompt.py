"""The Coniector's request text: one page's diplomatic readings, asked text only.

    text = build_reconstruction_prompt(call, entries)

`call` is one call of `common.reconstruction.reconstruction_plan`; `entries` maps
every `act_key` the call can show to `{act_key, page_ordinal, n, kind, label,
text}`, `text` being the entry's diplomatic reading with its doubt marks
(`common.reading_annotations.render_doubt_marks`). The prompt shows every entry
of the call's page in answer order, then, when the plan names them, the
neighbouring pages' edge acts and the pieces of each act that crosses a page
break, and asks for one JSON answer in the reconstruction answer grammar
(`common.reconstruction_answer`). It never shows an image: the Coniector reads
text alone.

`shown_texts(call, entries)` is every diplomatic text the prompt shows, which
`common.reconstruction.apply_departures` measures `replacement_in_context`
against.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from common.contracts.errors import ContractError
from common.reconstruction_answer import FINDING_CODES

PROMPT_VERSION: Final = "verbatus-reconstruction-prompt.v1"

_INSTRUCTION: Final = """\
You are the Coniector of a transcription pipeline for handwritten parish and civil \
registers. Each entry below is the diplomatic transcription of one entry of a register \
page: exactly what the ink shows, already established by the reader, which you must not \
change. You see the text only, never the page image.

Your task is to propose, beneath each act, a reconstruction: what the scribe most \
probably meant where the diplomatic text shows a slip of the pen, a lost abbreviation, \
a word cut at the page edge or a formula left unfinished. Work only from the text you \
are shown: the act itself, the other entries of the page, the formulas of the register \
and, where they are given, the neighbouring pages' edge acts. Where nothing needs \
reconstructing, propose no departure. Never invent a name, date or place that no text \
here supports; a reconstruction you are unsure of is better left out.

Doubt marks are part of the diplomatic text: [[?]] is ink the reader could not read, and \
[[word]] or [[word|other]] a reading it was unsure of. A departure may replace a whole \
mark, never part of one.

A departure is {"diplomatic": "<exact span of the diplomatic text>", \
"reconstruction": "<what you read there instead>", "reason": "<optional, short>"}. The \
diplomatic span must occur exactly once in the act's text at or after the end of the \
departure before it, so list departures in the order they occur and quote enough text to \
make each span unique. Keep each departure to a word or two.

A finding reports something about an act without changing its text: \
{"code": "<code>", "reason": "<optional, short>"}, with code one of: \
cut-at-page-break (the act is cut off where the page ends or begins), incomplete (the act \
stops before its formula ends), out-of-sequence (the act does not belong where it stands, \
for example a date far from its neighbours'), inconsistent (the act contradicts itself or \
its neighbours), other."""

_JOIN_INSTRUCTION: Final = """\
Each join below names the pieces of one act the reader saw cross a page break, in page \
order. Say whether they are one act ("continues": true or false). The joined text is the \
pieces joined by one newline; its departures apply to that joined text as an act's apply \
to its own. A join that does not continue carries no departures."""

_ANSWER_INSTRUCTION: Final = """\
Answer with one JSON object and nothing else, no code fence: \
{"acts": [{"act": "<key>", "findings": [...], "departures": [...]}, ...], \
"joins": [{"acts": ["<key>", ...], "continues": true, "departures": [...]}, ...]}. \
List exactly these acts, in this order: %s. List exactly these joins, in this order: %s."""


def _entry(entries: Mapping[str, Mapping[str, Any]], key: str) -> Mapping[str, Any]:
    entry = entries.get(key)
    if entry is None or not isinstance(entry.get("text"), str):
        raise ContractError(f"the reconstruction prompt has no diplomatic text for {key}")
    return entry


def _entry_block(entry: Mapping[str, Any]) -> str:
    label = entry.get("label")
    head = f"[{entry['act_key']}] ({entry['kind']}" + (f"; {label}" if label else "") + ")"
    return f"{head}\n{entry['text']}"


def page_keys(call: Mapping[str, Any], entries: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Every entry of the call's page, `act` and `other`, in answer order."""
    ordinal = call["page_ordinal"]
    on_page = [entry for entry in entries.values() if entry["page_ordinal"] == ordinal]
    return [entry["act_key"] for entry in sorted(on_page, key=lambda entry: entry["n"])]


def shown_keys(call: Mapping[str, Any], entries: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Every key the prompt shows, once each, in the order it shows them."""
    keys: list[str] = []
    for key in [
        *page_keys(call, entries),
        *call["context"],
        *(piece for chain in call["chains"] for piece in chain),
    ]:
        if key not in keys:
            keys.append(key)
    return keys


def shown_texts(call: Mapping[str, Any], entries: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Every diplomatic text the prompt shows, in the order it shows them."""
    return [_entry(entries, key)["text"] for key in shown_keys(call, entries)]


def build_reconstruction_prompt(
    call: Mapping[str, Any], entries: Mapping[str, Mapping[str, Any]]
) -> str:
    """The one text turn the Coniector's chair is sent for `call`."""
    subjects: Sequence[str] = call["subjects"]
    chains: Sequence[Sequence[str]] = call["chains"]
    page = page_keys(call, entries)
    missing = [key for key in subjects if key not in page]
    if missing:
        raise ContractError(f"reconstruction subjects {missing} are not entries of the call's page")
    sections = [
        _INSTRUCTION,
        f"Finding codes: {', '.join(sorted(FINDING_CODES))}.",
        f"## Page {call['page_ordinal']}\n\n"
        + "\n\n".join(_entry_block(_entry(entries, key)) for key in page),
    ]
    if call["context"]:
        sections.append(
            "## Neighbouring pages' edge acts (context only; not to reconstruct)\n\n"
            + "\n\n".join(_entry_block(_entry(entries, key)) for key in call["context"])
        )
    if chains:
        blocks = []
        for index, chain in enumerate(chains, start=1):
            pieces = "\n\n".join(_entry_block(_entry(entries, key)) for key in chain)
            blocks.append(f"Join {index}: {json.dumps(list(chain))}\n\n{pieces}")
        sections.append(_JOIN_INSTRUCTION + "\n\n## Joins\n\n" + "\n\n".join(blocks))
    sections.append(
        _ANSWER_INSTRUCTION
        % (
            json.dumps(list(subjects)),
            json.dumps([list(chain) for chain in chains]),
        )
    )
    return "\n\n".join(sections) + "\n"
