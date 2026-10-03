"""Request fields a chair carries because of *who occupies it*.

The stages that build a chair's requests, the serving client that checks a
Chandra request, and the golden-page smoke all need the same answer to "which
template switch does this chair's call carry", so it lives here once rather than
as literals that can drift.

Nothing here decides anything about a *request*: the per-request arithmetic is
`common/request_capacity.py`'s, and what a chair's own adapter carries from its
vendor stays with that adapter (`pipeline/3_attestatores/feeding.py` holds
DAI's and Churro's carried decoding values, under their own digests). This
module is only for a value two stages must agree on.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final, Mapping

# Chairs whose occupant's chat template may open a turn in thinking mode, and
# which every call therefore asks for a direct answer: Chandra (its two shipped
# templates disagree on the default, and its parser refuses a body that opens
# with `<think>`) and Qwen3.8 in the Perlector and reconstructor chairs (thinking
# by default). Keyed by chair, as every run builder and the golden-page smoke
# know the chair they call, so the smoke reads the way the run will whatever
# source the checkpoint was resolved from.
_DIRECT_RESPONSE_CHAIRS: Final = frozenset({"attestator_1", "perlector", "reconstructor"})
_THINKING_OFF: Final[Mapping[str, bool]] = MappingProxyType({"enable_thinking": False})


def chat_template_kwargs_for(chair: str) -> dict[str, bool] | None:
    """The `chat_template_kwargs` every call to this chair carries, or None for none.

    A fresh plain dict, since it goes onto a request record the client seals.
    """

    return dict(_THINKING_OFF) if chair in _DIRECT_RESPONSE_CHAIRS else None


def chandra_wire_fields() -> dict[str, Any]:
    """The extra request fields every Chandra call carries, as a fresh mapping."""

    return {"chat_template_kwargs": chat_template_kwargs_for("attestator_1")}


# `chat_template_content_format` is NOT sendable per request: vLLM sets it
# once at server launch from a CLI argument and never reads it off an incoming
# request, so naming it here would be silently accepted and ignored. It is
# pinned to `openai`, which keeps the caller's part order, at launch in
# `operations/serving/manager.py`; vLLM's `string` format would move images
# ahead of text instead.
