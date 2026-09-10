"""The structure chair's sealed request text: Chandra's own layout prompt (v3).

The chair this pass serves is Chandra, and tonight's ruling (Tyrel, 2026-09-06)
is that each witness runs as its developers intended -- the vendor's
preprocessing, prompt bytes, message shape, generation values and output grammar
adopted verbatim and pinned by digest. So this module no longer *writes* a
prompt. It names one: `common/chandra_layout.py::OCR_LAYOUT_PROMPT`, the
`"ocr_layout"` entry of the vendor's own `PROMPT_MAPPING` and the prompt every
vendor caller uses for a layout read, carried into this tree under its Apache-2.0
citation and sealed at import against the digest recorded beside it.

**Both Chandra chairs send the same bytes.** `designator_structure` here and
`attestator_1` in `pipeline/3_attestatores/chandra.py` are one model in two
roles (GLOSSARY, "chair"), and a fine-tune asked in two different ways answers
in two different grammars. One prompt constant, imported by both, is what makes
that mechanical rather than remembered.

**What v2 was, and why it is gone.** v2 was this repository's own instruction:
it asked for `{"schema": "verbatus-structure-answer.v1", "acts": [...]}`, one
rectangle per act in normalized integer coordinates, the transcription as
written, an optional label, in reading order. It was carefully written and it
was still a prompt the fine-tune had never seen, asking for a JSON envelope the
model was never trained to emit. Nothing about the wording was wrong; the
premise was -- a chair asked outside its own grammar reports what it can
improvise, not what it was trained to see. The JSON acceptance that read those
answers (`common/structure_answer.py`'s `STRUCTURE_ANSWER_SCHEMA`,
`decode_json_body`, `validate_box_1000`) is retired with it; its shared
geometry and page-text rules -- `to_page_bounds` and `join_delivered_texts` --
are unchanged and still the only conversion either Chandra reading uses.

**The prompt is code, not configuration**, exactly as v2 was: sealed by digest
rather than by a config table a run could point somewhere else unnoticed. What
changed is that the seal is now doubled. `STRUCTURE_PROMPT_VERSION` names the
rendered text this pass sends, and `common/chandra_layout.py` refuses to import
at all if the carried bytes no longer render to the sha256 recorded against
`github.com/datalab-to/chandra @ d4f7467435aa4137d9539f000ddf0b7ced3eb43f`. A
byte edited here, by anyone, for any reason, fails at import rather than
quietly changing what a chair is asked.

**GOVERNANCE 10 still binds it, and now binds it differently.** The rule against
an instrument that argues one way -- a severity floor, a confidence budget, a
told direction -- is a rule about *our* instructions, and it is why v2 stated
none of those. The carried prompt states none either: it is an OCR instruction,
a tag list and a label list. What we no longer have is the freedom to tune it,
because tuning it would break the byte-equality that makes it the vendor's
prompt. That is the trade the ruling makes, and it is recorded here rather than
discovered later.

**One `user` turn, no system turn**, unchanged from v2 and for the same
evidence: Chandra's own inference code sends a single `user` message and never
a system message (`chandra/model/vllm.py`, `model/hf.py`), and its
`chat_template.jinja` at the pinned revision does not admit an image in a system
message. `structure_pass.py::page_request` puts the image block before the text
in that turn, which is the order the vendor appends them in.

**Normalized 0-1000 coordinates, not page pixels**, also unchanged, and now
stated by the vendor's own sentence rather than by ours: the carried prompt says
`Bboxes are normalized 0-1000.`, `common/chandra_layout.py::BBOX_SCALE` is
1000, and that module refuses to import if the two ever disagree. They are
resolution-independent, which is why the inference engine's internal resize does
not corrupt geometry, and the conversion to page pixels is this repository's
own, done once at the edge from the sealed page's own dimensions
(`common/structure_answer.py::to_page_bounds`).
"""

from __future__ import annotations

from typing import Final

from common.chandra_layout import (
    OCR_LAYOUT_PROMPT,
    OCR_LAYOUT_PROMPT_SHA256,
    VENDOR_COMMIT,
    VENDOR_LICENCE,
    VENDOR_PARSER_SOURCE,
    VENDOR_PROMPT_SOURCE,
    VENDOR_REPOSITORY,
)
from common.contracts.canonical import digest_bytes

STRUCTURE_PROMPT_VERSION: Final = "verbatus-structure-prompt.v3"

# The grammar the answer to that prompt is read in: Chandra's layout HTML, as
# `common/chandra_layout.py::parse_layout_html` reads it. It replaces the
# retired `verbatus-structure-answer.v1` JSON envelope on the record's
# `answer_schema` field, so a record says which reading produced its blocks
# rather than leaving a reader to infer it from the prompt version.
STRUCTURE_ANSWER_GRAMMAR: Final = "chandra-layout-html.v1"


def vendor_identity() -> dict[str, str]:
    """The vendor code this pass runs on, recorded onto every page's answer.

    GOVERNANCE 6 requires the resolved identity and revision of the *model* on
    every stored reading; the serving provenance block carries that. This is the
    other half the ruling added: the repository, commit and licence of the code
    whose prompt bytes were sent and whose grammar was read, plus the digest of
    the carried prompt itself. A re-parse under a different vendor pin is then
    visibly different rather than silently so.
    """
    # `LAYOUT_TEXT_VIEW` is deliberately not in here. It is the record's own
    # `text_view` field, and it is *ours*: the vendor's text path routes through
    # markdownify and BeautifulSoup and drops what this one keeps. Filing it
    # under the vendor's identity would credit the vendor with a reading it does
    # not perform.
    return {
        "repository": VENDOR_REPOSITORY,
        "commit": VENDOR_COMMIT,
        "licence": VENDOR_LICENCE,
        "prompt_source": VENDOR_PROMPT_SOURCE,
        "parser_source": VENDOR_PARSER_SOURCE,
        "prompt_sha256": OCR_LAYOUT_PROMPT_SHA256,
    }


def messages() -> tuple[dict[str, str], ...]:
    """The one `user` turn; the image block is put before it by the pass."""
    return ({"role": "user", "content": OCR_LAYOUT_PROMPT},)


def prompt_sha256() -> str:
    """The digest of the exact rendered text -- the seal `STRUCTURE_PROMPT_VERSION` names.

    Over the rendered role/content pairs rather than over `OCR_LAYOUT_PROMPT`
    alone, unchanged from v2: this digest seals what *this pass sends*, message
    shape included, so a turn added or a role changed expires it. The carried
    bytes have their own separate digest (`OCR_LAYOUT_PROMPT_SHA256`), which is
    what proves they are the vendor's; these are two different claims and they
    are sealed apart.
    """
    rendered = "\x00".join(f"{message['role']}\x00{message['content']}" for message in messages())
    return digest_bytes(rendered.encode("utf-8"))
