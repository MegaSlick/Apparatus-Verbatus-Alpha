"""Request fields a chair carries because of *who occupies it*, shared by stages.

One model can fill more than one chair, and when it does, both chairs owe it
the same call shape. `datalab-to/chandra-ocr-2` fills `designator_structure`
(`pipeline/2_designator/`) and `attestator_1` (`pipeline/3_attestatores/`), and
neither of those packages may import the other -- so a fact about how Chandra
must be called lives here, once, rather than as two literals that can drift.

Nothing here decides anything about a *request*: the per-request arithmetic is
`common/request_capacity.py`'s, and what a chair's own adapter carries from its
vendor stays with that adapter (`pipeline/3_attestatores/feeding.py` holds
DAI's and Churro's carried decoding values, under their own digests). This
module is only for a value two stages must agree on.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final, Mapping

# Sent on every Chandra request, at both chairs.
#
# **Why it is sent at all.** Two chat templates ship at the pinned revision and
# they disagree. `chat_template.jinja` line 149 emits `<think>\n\n</think>\n\n`
# unconditionally -- thinking closed before the answer starts. The template
# embedded in `tokenizer_config.json` emits `<think>\n` **unless
# `enable_thinking` is false**, which opens the assistant turn in thinking
# mode. Which of the two vLLM resolves is a fact about vLLM's version, not
# about our request: `vllm/renderers/hf.py::resolve_chat_template` prefers the
# processor's template (the `.jinja`) for a multimodal repository, but that is
# a reading of one tagged release and nothing in this tree has observed it
# running.
#
# **Why sending it is safe either way.** `resolve_chat_template_kwargs` keeps a
# kwarg only when the tokenizer method or the resolved template declares it and
# drops the rest without error. The `.jinja` declares no `enable_thinking`, so
# under it this flag is dropped and changes nothing; the `tokenizer_config`
# template does declare it, so under that one the flag is decisive in exactly
# the branch that would otherwise open a thinking turn. Upstream never passes
# it because upstream is pinned to vLLM 0.17.0, where the question does not
# arise.
#
# **What it costs if it were wrong.** Nothing we can lose: a thinking turn
# would spend context on reasoning tokens inside a budget already tight enough
# to truncate a page, and both Chandra chairs' parsers refuse a body that opens
# with `<think>` -- so the failure this closes is a whole page's reading, not a
# formatting blemish.
CHANDRA_CHAT_TEMPLATE_KWARGS: Final[Mapping[str, bool]] = MappingProxyType(
    {"enable_thinking": False}
)


def chandra_wire_fields() -> dict[str, Any]:
    """The extra request fields every Chandra call carries, as a fresh mapping.

    Returned as plain, mutable containers because it goes onto a
    `ChairRequest.generation_sent` the client seals and records; a shared
    read-only proxy would travel into a retained record as a different type
    than every other value in it.
    """

    return {"chat_template_kwargs": dict(CHANDRA_CHAT_TEMPLATE_KWARGS)}
