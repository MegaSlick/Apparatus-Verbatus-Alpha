"""The structure chair's sealed request text: Chandra's own layout prompt (SPEC_D §1.1).

**v3 sends the vendor's bytes.** Tyrel's ruling of 2026-09-06 is that each
witness runs as its developers intended -- the vendor's preprocessing, prompt
bytes, message shape, generation values and output grammar adopted verbatim and
pinned by digest. This chair's occupant is Chandra, and the prompt every vendor
caller uses for a layout read is `chandra/prompts.py::OCR_LAYOUT_PROMPT`
(`prompt_type="ocr_layout"`). It is carried, cited and sealed in
`common/chandra_layout.py`, and this module sends exactly those bytes in
exactly the one `user` turn `chandra/model/vllm.py` builds. Nothing is
appended, prefixed, or reworded: a sentence of our own in front of the vendor's
would make the sealed digest ours rather than the vendor's, and the whole point
of the ruling is that what the chair receives is provable against the vendor's
own file.

Both Chandra chairs -- `designator_structure` here and `attestator_1` in
`pipeline/3_attestatores/chandra.py` -- send the same bytes, because they are
the same model asked for the same thing. What differs is what each stage does
with the answer, not what it asks.

**What was retired, and why it is not a loss.** v2 was this repository's own
instruction: it asked for every act as one rectangle in normalized 0-1000
coordinates, its transcription as written, an optional label, in reading order,
and a closed JSON object (`verbatus-structure-answer.v1`). Every one of those
asks survives in the vendor prompt, in the vendor's own words and its own
grammar: `data-bbox` is "x0 y0 x1 y1", the prompt states "Bboxes are normalized
0-1000" (checked against `BBOX_SCALE` at import in `common/chandra_layout.py`),
`data-label` is the block's label, "Reading order should be correct and
natural" is the ordering, and the HTML layout-block grammar is the shape.
The one ask that does *not* survive is our own JSON envelope, which was never a
shape Chandra was trained to answer in -- and the design records that the
comparison between the vendor grammar and the retired JSON contract is
deliberately not run (SPEC_FINDINGS, U16), rather than claimed either way.

**GOVERNANCE 10 still binds the text.** An instrument may state no preference,
no severity floor and no confidence budget. The carried prompt states none:
`test_structure_prompt.py` checks the rendered vendor bytes for those words
directly, so this is a measurement of what the chair receives rather than a
claim about it. Had the vendor's prompt carried one, it would have been a
finding to hand to Tyrel under hard rule 9 -- a carried-verbatim ruling and
GOVERNANCE 10 pulling apart -- not a word for a session to quietly edit out.

Normalized 0-1000 coordinates, not page pixels, are still the reason the
internal inference-engine resize does not corrupt geometry: they are
resolution-independent, and the conversion to page pixels is this repository's
own, done once at the edge from the sealed page's own dimensions
(`common/structure_answer.py::to_page_bounds`, reached through
`common/chandra_layout.py::block_page_bounds`).

`STRUCTURE_PROMPT_VERSION` names the exact rendered text this prompt is. It
changes only as a reviewed edit -- which, from v3 onward, means a change of
vendor pin rather than a rewording of ours -- and every change bumps the
version so a run produced under one wording is never read as though it came
from another.
"""

from __future__ import annotations

from typing import Final

from common.chandra_layout import OCR_LAYOUT_PROMPT
from common.contracts.canonical import digest_bytes

STRUCTURE_PROMPT_VERSION: Final = "verbatus-structure-prompt.v3"


def messages() -> tuple[dict[str, str], ...]:
    """The one `user` turn; the image block is put before it by the pass.

    The content is `OCR_LAYOUT_PROMPT` and nothing else. Referenced rather than
    copied, so there is exactly one place in the tree the vendor's bytes live
    and exactly one digest sealing them (`common/chandra_layout.py` refuses to
    import if that digest drifts).
    """
    return ({"role": "user", "content": OCR_LAYOUT_PROMPT},)


def prompt_sha256() -> str:
    """The digest of the exact rendered text -- the seal `STRUCTURE_PROMPT_VERSION` names."""
    rendered = "\x00".join(f"{message['role']}\x00{message['content']}" for message in messages())
    return digest_bytes(rendered.encode("utf-8"))
