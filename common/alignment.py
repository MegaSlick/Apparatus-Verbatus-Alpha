"""Loss-accounted comparison views of witness text, and the sealed alignment limits.

Each view strips what a witness wrapped around its reading and maps every kept
character back to its raw offset, so nothing is lost silently.
"""

from __future__ import annotations

import html
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.uncertainty import UNCERTAINTY_TOKENS
from common.sealed_config import read_sealed_toml

DEFAULT_ALIGNMENT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "alignment.toml"


@dataclass(frozen=True)
class AlignmentLimits:
    max_characters: int
    max_character_pairs: int
    timeout_seconds: int


# The longest HTML5 named entity is `&CounterClockwiseContourIntegral;` at 33
# characters; numeric references are shorter still. The bound matters because
# the terminator is searched for, not assumed: without it a literal ampersand
# in the ink ("Jean & Marie", "&c.") swallowed every character up to the next
# semicolon anywhere later in the document -- tags included -- and handed them
# back as "stripped" text. See `markup_text_view`.
_MAX_ENTITY_CHARACTERS: Final = 40


def markup_text_view(raw: str) -> dict[str, Any]:
    """Return plain text plus a raw-offset map and explicit stripping loss.

    Plain text is NFC-normalized and whitespace-collapsed. `offset_map` indexes
    the input character that supplied each normalized character; `None` marks a
    collapsed separator synthesized from a run of whitespace.  Markup, entity
    spelling, and collapsed whitespace are all visible in `loss`, though not
    separably: `markup_characters` is the whole raw-minus-stripped difference,
    so an entity that collapses to one character (`&amp;` to `&`) is counted
    there beside the tags rather than under a name of its own.
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
    # Only a `<` that actually closes is markup. An unterminated one is
    # ordinary ink ("aged < 30" at the end of a note), and treating it as an
    # opened tag silently dropped every character after it. One index instead
    # of a per-`<` forward scan: a `<` closes exactly when any `>` exists
    # after it, i.e. when it sits before the last `>` of the whole input.
    last_close = raw.rfind(">")
    i = 0
    while i < len(raw):
        char = raw[i]
        if char == "<" and i < last_close:
            in_tag = True
        elif char == ">" and in_tag:
            in_tag = False
        elif not in_tag:
            if char == "&":
                # Both conditions load-bearing: the terminator must be inside
                # `_MAX_ENTITY_CHARACTERS`, and the candidate must actually
                # decode to something else (`html.unescape` returns a
                # non-entity unchanged), or a literal "&" followed eventually
                # by any semicolon would swallow everything between as if it
                # were one entity.
                end = raw.find(";", i + 1, i + _MAX_ENTITY_CHARACTERS)
                candidate = raw[i : end + 1] if end != -1 else ""
                decoded = html.unescape(candidate) if candidate else ""
                if candidate and decoded != candidate:
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
    stripped = "".join(plain)
    composed = unicodedata.normalize("NFC", stripped)
    # NFC can change codepoint count, so indexing pre-composition offsets by a
    # post-composition index mis-points every entry after the first merge. The
    # map is rebuilt through composition instead: the stripped text splits
    # into clusters at combining-class-0 starters, and every composed
    # character maps to its cluster's first raw offset. Where per-cluster
    # composition cannot reproduce the composed text (e.g. Hangul jamo), the
    # map records None throughout rather than publishing offsets that may lie.
    composed_offsets: list[int | None]
    cluster_chars: list[str] = []
    cluster_offsets: list[int | None] = []
    if stripped:
        boundaries = [
            index
            for index, char in enumerate(stripped)
            if index == 0 or unicodedata.combining(char) == 0
        ]
        boundaries.append(len(stripped))
        for start, end in zip(boundaries, boundaries[1:], strict=False):
            piece = unicodedata.normalize("NFC", stripped[start:end])
            cluster_chars.extend(piece)
            cluster_offsets.extend([offsets[start]] * len(piece))
    if "".join(cluster_chars) == composed:
        composed_offsets = cluster_offsets
    else:
        composed_offsets = [None] * len(composed)
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
        normalized_offsets.append(composed_offsets[index])
    normalized = "".join(normalized_chars)
    return {
        "text": normalized,
        "offset_map": normalized_offsets,
        "loss": {
            "markup_characters": len(raw) - len(stripped),
            "whitespace_characters": len(composed) - len(normalized),
            "unicode_reencoded_characters": abs(len(stripped) - len(composed)),
        },
    }


def bracket_marker_view(raw: str) -> dict[str, Any]:
    """Return `raw` with exactly the RecordGold bracket markers removed.

    Act-scoped chairs that can express uncertainty (DAI) embed
    `[UNCERTAIN]`/`[CROSSED_OUT]` inline in otherwise plain reported text --
    not as tag-shaped markup, so `markup_text_view` does not touch them and
    would count each marker's characters as witness disagreement if the raw
    report were diffed against clean established text. This view removes only
    those two exact substrings, verbatim and case-sensitively, and maps every
    surviving character back to its index in `raw` so a span found in the
    stripped text still resolves to the ink it came from.

    Deliberately narrower than `markup_text_view`: no NFC normalization, no
    whitespace collapsing, no entity decoding. A marker's neighbouring
    whitespace is left exactly as reported -- e.g. `"Marie [UNCERTAIN] "`
    loses only the eleven bracketed characters, not the space next to them --
    because folding whitespace here would be a second, unrelated transform
    hiding inside a function whose contract is "remove exactly these tokens."
    Overlap is not a real hazard between these two literals (neither is a
    prefix of the other), but the scan still checks both at every position
    rather than assuming an order, so adding a third marker later cannot
    silently depend on scan order.
    """
    if not isinstance(raw, str):
        raise SchemaRefusal("bracket marker input is not text")
    kept_chars: list[str] = []
    offsets: list[int] = []
    removed_characters = 0
    i = 0
    n = len(raw)
    while i < n:
        matched_length = 0
        for token in UNCERTAINTY_TOKENS:
            if raw.startswith(token, i):
                matched_length = len(token)
                break
        if matched_length:
            removed_characters += matched_length
            i += matched_length
            continue
        kept_chars.append(raw[i])
        offsets.append(i)
        i += 1
    return {
        "text": "".join(kept_chars),
        "offset_map": offsets,
        "loss": {"marker_characters": removed_characters},
    }


def load_alignment_limits(
    path: str | Path = DEFAULT_ALIGNMENT_CONFIG_PATH,
) -> tuple[AlignmentLimits, str]:
    record, digest = read_sealed_toml(path, "alignment configuration")
    if (
        set(record) != {"limits"}
        or not isinstance(record["limits"], dict)
        or set(record["limits"])
        != {
            "max_characters",
            "max_character_pairs",
            "timeout_seconds",
        }
    ):
        raise ContractError("alignment configuration has the wrong closed schema")
    values = record["limits"]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in values.values()
    ):
        raise ContractError("alignment limits must be positive integers")
    return AlignmentLimits(**values), digest
