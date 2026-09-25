"""The structure chair's sealed request text: Chandra's own layout prompt (v3).

This module names, rather than writes, the vendor's own layout prompt
(`common/chandra_layout.py::OCR_LAYOUT_PROMPT`) instead of asking a fine-tuned
model to answer in a grammar it was never trained on. `designator_structure`
here and `attestator_1` in `pipeline/3_attestatores/chandra.py` import the same
constant so both Chandra chairs send identical bytes.

The prompt is sealed by digest, not configuration: `common/chandra_layout.py`
refuses to import if the carried bytes no longer hash to the pinned vendor
commit's sha256, so an edited byte fails loudly at import rather than quietly
changing what the chair is asked. This also means the prompt cannot be tuned.
Principle 8's ban on a steering instrument still binds; the vendor prompt
meets it by asking for no preference, severity floor or confidence budget
(`test_structure_prompt.py`).

Chandra's own inference code sends a single `user` message and never a system
one, and its chat template does not accept an image there -- so this module's
`messages()` returns that one `user` turn; `structure_pass.page_request` puts
the image block before it. Coordinates are normalized 0-1000 (the vendor's own
convention, resolution-independent); `to_page_bounds` converts to page pixels
once, at the edge.
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

# The grammar the answer is read in, recorded on the record's `answer_schema`
# field so a reader doesn't have to infer it from the prompt version.
STRUCTURE_ANSWER_GRAMMAR: Final = "chandra-layout-html.v1"


def vendor_identity() -> dict[str, str]:
    """The vendor code's repository, commit, licence and prompt digest.

    Model identity is recorded separately by the serving provenance block;
    `LAYOUT_TEXT_VIEW` is excluded because it is this repository's own reading,
    not the vendor's.
    """
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
    """Digest of the rendered role/content pairs this pass sends.

    Distinct from `OCR_LAYOUT_PROMPT_SHA256`, which proves the carried bytes
    are the vendor's; this one covers message shape too, so a turn added or a
    role changed expires it.
    """
    rendered = "\x00".join(f"{message['role']}\x00{message['content']}" for message in messages())
    return digest_bytes(rendered.encode("utf-8"))
