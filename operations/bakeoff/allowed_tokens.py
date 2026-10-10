"""Named output-token sets a reader may be restricted to, off unless an arm asks.

vLLM 0.30.0 accepts `allowed_token_ids` on a chat request
(`vllm/entrypoints/openai/chat_completion/protocol.py`) and masks every other
token at sampling, including the verification step of speculative decoding
(`vllm/v1/sample/rejection_sampler.py`). The point, for a reader that
transcribes French and Latin registers into JSON: a token that cannot be emitted
cannot be silently substituted for a rare name piece. It changes what the model may write, so it is
never on by default; an arm that uses it says so in every request record.

A set is computed from the served snapshot's own `tokenizer.json` by a fixed
rule, never typed in, so it is bound to the exact tokenizer the server loads.
The request record carries the set's name, size and the SHA-256 of its ids,
never the ids themselves.

`latin-json-v1` keeps:

- every one of the 256 single-byte tokens, so any text at all can still be
  spelled byte by byte (nothing becomes unwritable, only unlikely);
- every vocabulary token whose bytes are complete UTF-8 made only of: ASCII,
  letters whose Unicode name begins "LATIN ", Latin-1 punctuation and signs
  (U+00A0-U+00BF, U+00D7, U+00F7), combining diacritics (U+0300-U+036F),
  general punctuation (U+2000-U+206F) and super/subscripts (U+2070-U+209F);
- the two end-of-turn tokens, `<|im_end|>` and `<|endoftext|>`.

Every other token (other scripts, emoji, partial multi-byte pieces of them, and
the remaining special tokens) is masked.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from functools import lru_cache
from pathlib import Path

SETS = ("latin-json-v1",)
_END_TOKENS = frozenset({"<|im_end|>", "<|endoftext|>"})


def _byte_decoder() -> dict[str, int]:
    """The inverse of the byte-level BPE alphabet (GPT-2's `bytes_to_unicode`)."""

    printable = (
        list(range(ord("!"), ord("~") + 1))
        + list(range(ord("¡"), ord("¬") + 1))
        + list(range(ord("®"), ord("ÿ") + 1))
    )
    codes = printable[:]
    extra = 0
    for byte in range(256):
        if byte not in printable:
            printable.append(byte)
            codes.append(256 + extra)
            extra += 1
    return {chr(code): byte for byte, code in zip(printable, codes, strict=True)}


def _latin_char(char: str) -> bool:
    code = ord(char)
    return (
        code < 128
        or 0x00A0 <= code <= 0x00BF
        or code in (0x00D7, 0x00F7)
        or 0x0300 <= code <= 0x036F
        or 0x2000 <= code <= 0x206F
        or 0x2070 <= code <= 0x209F
        or unicodedata.name(char, "").startswith("LATIN ")
    )


def latin_json_v1(tokenizer: dict) -> list[int]:
    """The `latin-json-v1` ids of one parsed byte-level BPE `tokenizer.json`."""

    model = tokenizer.get("model") or {}
    decoder = tokenizer.get("decoder") or {}
    if model.get("type") != "BPE" or decoder.get("type") != "ByteLevel":
        raise ValueError("latin-json-v1 is defined for a byte-level BPE tokenizer only")
    to_byte = _byte_decoder()
    allowed: set[int] = set()
    for token, token_id in model["vocab"].items():
        try:
            data = bytes(to_byte[char] for char in token)
        except KeyError:
            continue
        if len(data) == 1:
            allowed.add(token_id)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if all(_latin_char(char) for char in text):
            allowed.add(token_id)
    allowed.update(
        item["id"] for item in tokenizer.get("added_tokens", []) if item["content"] in _END_TOKENS
    )
    missing = _END_TOKENS - {
        item["content"] for item in tokenizer.get("added_tokens", []) if item["id"] in allowed
    }
    if missing:
        raise ValueError(f"tokenizer has no {sorted(missing)}; the answer could never end")
    return sorted(allowed)


@lru_cache(maxsize=4)
def load(name: str, tokenizer_json: Path) -> tuple[int, ...]:
    """The named set for the tokenizer at `tokenizer_json` (the served snapshot's)."""

    if name not in SETS:
        raise ValueError(f"unknown allowed-token set {name!r}; known: {list(SETS)}")
    return tuple(latin_json_v1(json.loads(Path(tokenizer_json).read_text("utf-8"))))


def describe(name: str, ids: tuple[int, ...] | list[int]) -> dict[str, object]:
    """What a request record says about the set: its name, size and digest, not the ids."""

    encoded = json.dumps(list(ids), separators=(",", ":")).encode("ascii")
    return {"set": name, "count": len(ids), "sha256": hashlib.sha256(encoded).hexdigest()}
