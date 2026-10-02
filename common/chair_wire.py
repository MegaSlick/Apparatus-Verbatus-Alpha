"""Request fields a chair carries because of *who occupies it*.

`datalab-to/chandra-ocr-2` fills `attestator_1` (`pipeline/3_attestatores/`), and
the serving client (`operations/serving/client.py`) holds a Chandra request to
the same call shape -- so a fact about how Chandra must be called lives here,
once, rather than as two literals that can drift.

Nothing here decides anything about a *request*: the per-request arithmetic is
`common/request_capacity.py`'s, and what a chair's own adapter carries from its
vendor stays with that adapter (`pipeline/3_attestatores/feeding.py` holds
DAI's and Churro's carried decoding values, under their own digests). This
module is only for a value two stages must agree on.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final, Mapping

# Sent on every Chandra request. The two chat templates that
# ship at the pinned revision disagree on whether a turn opens in thinking
# mode -- read from the release's own template files, never seen running --
# so forcing this off is a no-op under one template and decisive under the
# other, and either way it is free -- a thinking turn would waste a tight page
# budget, and the chair's parser refuses a body that opens with `<think>`.
CHANDRA_CHAT_TEMPLATE_KWARGS: Final[Mapping[str, bool]] = MappingProxyType(
    {"enable_thinking": False}
)


# Checkpoints whose chat template may open a turn in thinking mode, keyed by
# repository rather than by chair: the same checkpoint can fill several chairs
# (Qwen3.8 serves both the Perlector and the reconstructor), and each of them is
# asked for a direct answer. Anything that reads a page from one of these
# checkpoints outside its stage, such as the golden-page smoke, sends the same
# switch so it reads the way the run will.
_DIRECT_RESPONSE_TEMPLATE_KWARGS: Final[Mapping[str, Mapping[str, bool]]] = MappingProxyType(
    {
        "datalab-to/chandra-ocr-2": CHANDRA_CHAT_TEMPLATE_KWARGS,
        "Qwen/Qwen3.8-27B": MappingProxyType({"enable_thinking": False}),
    }
)


def direct_response_template_kwargs(repo: str) -> dict[str, bool] | None:
    """The `chat_template_kwargs` a checkpoint's calls carry, or None when it needs none."""

    kwargs = _DIRECT_RESPONSE_TEMPLATE_KWARGS.get(repo)
    return None if kwargs is None else dict(kwargs)


def chandra_wire_fields() -> dict[str, Any]:
    """The extra request fields every Chandra call carries, as a fresh mapping.

    Returned as plain, mutable containers because it goes onto a
    `ChairRequest.generation_sent` the client seals and records; a shared
    read-only proxy would travel into a retained record as a different type
    than every other value in it.
    """

    return {"chat_template_kwargs": dict(CHANDRA_CHAT_TEMPLATE_KWARGS)}


# `chat_template_content_format` is NOT sendable per request: vLLM sets it
# once at server launch from a CLI argument and never reads it off an incoming
# request, so naming it here would be silently accepted and ignored. It is
# pinned to `openai`, which keeps the caller's part order, at launch in
# `operations/serving/manager.py`; vLLM's `string` format would move images
# ahead of text instead.
