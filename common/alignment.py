"""Bounded, loss-accounted text alignment for page testimony.

The anchor is Chandra's retained text-plus-geometry view.  This module never
chooses a reading: it only says which bytes of a witness report can be attached
to which anchor characters, or records that it cannot say so.
"""

from __future__ import annotations

import html
import signal
import tomllib
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal

DEFAULT_ALIGNMENT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "alignment.toml"


@dataclass(frozen=True)
class AlignmentLimits:
    max_characters: int
    max_character_pairs: int
    timeout_seconds: int


class _TimedOut(Exception):
    pass


def _alarm(signum: int, frame: Any) -> None:
    raise _TimedOut()


def markup_text_view(raw: str) -> dict[str, Any]:
    """Return plain text plus a raw-offset map and explicit stripping loss.

    Plain text is NFC-normalized and whitespace-collapsed. `offset_map` indexes
    the input character that supplied each normalized character; `None` marks a
    collapsed separator synthesized from a run of whitespace.  Markup, entity
    spelling, and collapsed whitespace are all visible in `loss`.
    """
    if not isinstance(raw, str):
        raise SchemaRefusal("alignment input is not text")
    # Deliberately lexical rather than `html.parser.HTMLParser`: HTMLParser
    # exposes source offsets only per token, not per character, so its column
    # cannot seed an exact raw-offset map (and, run first only to catch
    # malformed markup as a refusal, it never actually raised on any input in
    # this module's own testing -- HTMLParser is intentionally permissive, so
    # that pass was dead code pretending to be a validation guarantee it did
    # not provide). Tags are omitted; entities are one visible character
    # mapped to their opening ampersand.
    plain: list[str] = []
    offsets: list[int] = []
    in_tag = False
    i = 0
    while i < len(raw):
        char = raw[i]
        if char == "<":
            in_tag = True
        elif char == ">" and in_tag:
            in_tag = False
        elif not in_tag:
            if char == "&":
                end = raw.find(";", i + 1)
                if end != -1:
                    decoded = html.unescape(raw[i : end + 1])
                    plain.extend(decoded)
                    offsets.extend([i] * len(decoded))
                    i = end
                else:
                    plain.append(char)
                    offsets.append(i)
            else:
                plain.append(char)
                offsets.append(i)
        i += 1
    composed = unicodedata.normalize("NFC", "".join(plain))
    # NFC can change codepoint count. The map remains a best-effort trace to the
    # retained raw payload; composition loss is recorded rather than fabricated.
    normalized_chars: list[str] = []
    normalized_offsets: list[int | None] = []
    pending_space = False
    for index, char in enumerate(composed):
        if char.isspace():
            pending_space = bool(normalized_chars)
            continue
        if pending_space:
            normalized_chars.append(" ")
            normalized_offsets.append(None)
            pending_space = False
        normalized_chars.append(char)
        normalized_offsets.append(offsets[index] if index < len(offsets) else None)
    normalized = "".join(normalized_chars)
    return {
        "text": normalized,
        "offset_map": normalized_offsets,
        "loss": {
            "markup_characters": len(raw) - len("".join(plain)),
            "whitespace_characters": len(composed) - len(normalized),
            "unicode_reencoded_characters": abs(len("".join(plain)) - len(composed)),
        },
    }


def load_alignment_limits(
    path: str | Path = DEFAULT_ALIGNMENT_CONFIG_PATH,
) -> tuple[AlignmentLimits, str]:
    try:
        raw = Path(path).read_bytes()
        record = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(f"alignment configuration at {path} could not be read") from error
    if set(record) != {"limits"} or set(record["limits"]) != {
        "max_characters",
        "max_character_pairs",
        "timeout_seconds",
    }:
        raise ContractError("alignment configuration has the wrong closed schema")
    values = record["limits"]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in values.values()
    ):
        raise ContractError("alignment limits must be positive integers")
    return AlignmentLimits(**values), digest_bytes(raw)


def align_to_anchor(witness_raw: str, anchor_raw: str, limits: AlignmentLimits) -> dict[str, Any]:
    """Align a witness comparison view to an anchor, or explicitly `unaligned`.

    Bounds apply before and during SequenceMatcher. No input is clipped: a limit
    or deadline produces a retained unaligned result with its reason.
    """
    witness = markup_text_view(witness_raw)
    anchor = markup_text_view(anchor_raw)
    witness_text, anchor_text = witness["text"], anchor["text"]
    if len(witness_text) > limits.max_characters or len(anchor_text) > limits.max_characters:
        return {
            "status": "unaligned",
            "reason": "character-limit",
            "witness": witness,
            "anchor": anchor,
        }
    if len(witness_text) * len(anchor_text) > limits.max_character_pairs:
        return {
            "status": "unaligned",
            "reason": "character-pair-limit",
            "witness": witness,
            "anchor": anchor,
        }
    previous = None
    try:
        if hasattr(signal, "SIGALRM"):
            previous = signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(limits.timeout_seconds)
        blocks = SequenceMatcher(
            a=witness_text, b=anchor_text, autojunk=False
        ).get_matching_blocks()
    except _TimedOut:
        return {"status": "unaligned", "reason": "timeout", "witness": witness, "anchor": anchor}
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
            if previous is not None:
                signal.signal(signal.SIGALRM, previous)
    spans = [
        {
            "witness": {"start": block.a, "end": block.a + block.size},
            "anchor": {"start": block.b, "end": block.b + block.size},
        }
        for block in blocks
        if block.size
    ]
    if not spans:
        return {
            "status": "unaligned",
            "reason": "no-common-anchor-text",
            "witness": witness,
            "anchor": anchor,
        }
    return {"status": "aligned", "witness": witness, "anchor": anchor, "spans": spans}
