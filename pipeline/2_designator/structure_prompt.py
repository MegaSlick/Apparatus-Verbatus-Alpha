"""The structure chair's sealed request text (SPEC_D §1.1).

The prompt is code, not configuration, so it is sealed by digest rather than by
a config table a run could point somewhere else unnoticed. It asks for every
act on the page -- GLOSSARY's own definition, deliberately loose: "a unit of
relevant body text. Usually a register entry ... [but] also index rows,
letters, notes, and essays" -- each as one rectangle in normalized integer
coordinates, its transcription as written, an optional short structural label,
in reading order, and nothing else. It states no preference, no severity
floor, and no confidence budget: GOVERNANCE 10's rule against an instrument
that argues one way binds the prompt exactly as it binds any other part of the
pipeline, and this file is where that would first go wrong if it did.

Normalized 0-1000 coordinates, not page pixels, are the reason the internal
inference-engine resize does not corrupt geometry: they are resolution-
independent, and the conversion to page pixels is this repository's own, done
once at the edge from the sealed page's own dimensions
(`common/structure_answer.py::to_page_bounds`).

`STRUCTURE_PROMPT_VERSION` names the exact rendered text this prompt is. It
changes only as a reviewed edit to this file -- rewording the instruction, not
tuning it toward an answer -- and every change bumps the version so a run
produced under one wording is never read as though it came from another.
"""

from __future__ import annotations

from typing import Final

from common.contracts.canonical import digest_bytes

STRUCTURE_PROMPT_VERSION: Final = "verbatus-structure-prompt.v1"

_SYSTEM_TEXT: Final = (
    "You are transcribing one page of a handwritten or printed record for "
    "an archival pipeline. Report exactly what is on the page: nothing "
    "corrected, modernized, summarized, or left out."
)

_USER_TEXT: Final = (
    "Find every act on this page. An act is any unit of relevant body "
    "text -- most often a register entry (a baptism, marriage, or burial), "
    "but this page may instead hold an index row, a marginal note, a "
    "letter, or an essay. Include every one you find; do not skip one "
    "because it looks unusual.\n\n"
    "For each act, report:\n"
    "- box_1000: one rectangle [x0, y0, x1, y1] in normalized integer "
    "coordinates from 0 to 1000, measured against the image exactly as "
    "shown -- x0,y0 the top-left corner and x1,y1 the bottom-right corner.\n"
    "- text: the act's transcription exactly as written, unmodernized, "
    "through to its end.\n"
    "- label (optional): a short structural label for the act, if one is "
    "evident.\n\n"
    "List the acts in reading order.\n\n"
    "Respond with exactly this JSON shape and nothing else -- no "
    "explanation, no markdown fencing, no text outside the JSON object:\n\n"
    '{"schema": "verbatus-structure-answer.v1", "acts": '
    '[{"box_1000": [x0, y0, x1, y1], "text": "...", "label": "..."}]}\n\n'
    "If the page holds no act, report an empty acts list."
)


def messages() -> tuple[dict[str, str], ...]:
    """The system and user turns; the image block is appended by the pass."""
    return (
        {"role": "system", "content": _SYSTEM_TEXT},
        {"role": "user", "content": _USER_TEXT},
    )


def prompt_sha256() -> str:
    """The digest of the exact rendered text -- the seal `STRUCTURE_PROMPT_VERSION` names."""
    rendered = "\x00".join(f"{message['role']}\x00{message['content']}" for message in messages())
    return digest_bytes(rendered.encode("utf-8"))
