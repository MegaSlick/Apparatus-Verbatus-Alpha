"""Bounded, loss-accounted text alignment for page testimony.

The anchor is Chandra's retained text-plus-geometry view.  This module never
chooses a reading: it only says which bytes of a witness report can be attached
to which anchor characters, or records that it cannot say so.
"""

from __future__ import annotations

import html
import signal
import threading
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
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


class _TimedOut(Exception):
    pass


# The longest HTML5 named entity is `&CounterClockwiseContourIntegral;` at 33
# characters; numeric references are shorter still. The bound matters because
# the terminator is searched for, not assumed: without it a literal ampersand
# in the ink ("Jean & Marie", "&c.") swallowed every character up to the next
# semicolon anywhere later in the document -- tags included -- and handed them
# back as "stripped" text. See `markup_text_view`.
_MAX_ENTITY_CHARACTERS: Final = 40


def _alarm(signum: int, frame: Any) -> None:
    raise _TimedOut()


# Named so a reader of a retained record cannot mistake the instrument giving up
# for a measurement of the witness. `timeout` alone read as a property of the
# chair ("the witness timed out"); what actually happened is that this module's
# own wall-clock backstop fired before it could say anything about coverage, and
# the difference decides whether a shortfall is evidence or an absent
# measurement (principle 8).
DEADLINE_REASON: Final = "alignment-deadline-exceeded"


def _matching_blocks(witness_text: str, anchor_text: str) -> list[tuple[int, int, int]]:
    """Return `(witness_start, anchor_start, size)` for every matched run.

    `difflib.SequenceMatcher`'s Ratcliff-Obershelp blocks, longest common
    contiguous block first then recursively to its left and right, with the
    terminating zero-size block dropped. Blocks are strictly ordered and
    non-overlapping on both sides, which is what lets a page alignment be
    clipped to one act's anchor range.

    `autojunk=False` is deliberate: its heuristic treats any element in over
    1% of the sequence as junk, which in French register prose is most of the
    alphabet. This is also what makes the matcher slow on degenerate input,
    hence the wall-clock backstop below.

    RapidFuzz's LCS opcodes were tried and refused: they are far faster but
    maximize matched characters, which on two acts opening with the same
    formula can attribute a witness's second-act reading to the first act
    instead -- a coverage-maximizing objective is the wrong one for attaching a
    reading to an anchor. "Longest verbatim agreement wins" is the one that is
    load-bearing here, and `common/test_alignment.py` pins that case by name.

    No normalization of its own: the comparison is over the codepoints
    `markup_text_view` produced, so the returned offsets index that same text.
    """
    return [
        (block.a, block.b, block.size)
        for block in SequenceMatcher(
            a=witness_text, b=anchor_text, autojunk=False
        ).get_matching_blocks()
        if block.size
    ]


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


def align_to_anchor(witness_raw: str, anchor_raw: str, limits: AlignmentLimits) -> dict[str, Any]:
    """Align a witness comparison view to an anchor, or explicitly `unaligned`.

    The character and pair bounds always apply before the matcher runs. The
    wall-clock deadline applies only where this call owns the process
    real-time timer (main thread, POSIX `SIGALRM`, no timer already armed);
    elsewhere the comparison runs unbounded under the caller's own deadline.
    The pair bound does not refuse every pathologically slow input -- some
    low-entropy pairs sitting exactly at `max_character_pairs` are still slow
    -- which is what the deadline is for. No input is clipped: a limit or a
    fired deadline produces a retained unaligned result with its reason.

    `deadline_in_force` is `True` only when this call actually armed the
    SIGALRM backstop. Without it a caller cannot tell a genuinely bounded
    alignment from one that ran unbounded and happened to finish, both of
    which otherwise return the same `{"status": "aligned", ...}`.

    A fired deadline (`DEADLINE_REASON`) is a non-verdict: this module made no
    measurement of coverage, and it is `unaligned` rather than a partial map,
    since publishing spans a timed-out comparison never finished would be
    worse than saying nothing.

    The deadline is sized from the legitimate ceiling, not the pathological
    one, and does not clear the pathological one: closing that gap needs a
    different matcher, recorded as a design question in
    `pipeline/3_attestatores/CONTRACT.md` rather than solved here.
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
            # Refused before the matcher ever ran; no timer question arises.
            "deadline_in_force": False,
        }
    if len(witness_text) * len(anchor_text) > limits.max_character_pairs:
        return {
            "status": "unaligned",
            "reason": "character-pair-limit",
            "witness": witness,
            "anchor": anchor,
            "deadline_in_force": False,
        }
    previous = None
    alarm_armed = False
    try:
        if (
            hasattr(signal, "SIGALRM")
            and hasattr(signal, "ITIMER_REAL")
            and threading.current_thread() is threading.main_thread()
            and signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
        ):
            previous = signal.signal(signal.SIGALRM, _alarm)
            try:
                signal.alarm(limits.timeout_seconds)
            except BaseException:
                signal.signal(signal.SIGALRM, previous)
                raise
            alarm_armed = True
        blocks = _matching_blocks(witness_text, anchor_text)
        # Cancelled inside the `try`, not only `finally`: an alarm firing after
        # `_matching_blocks` returns but before `finally` runs would otherwise
        # raise `_TimedOut` past the `except` above, propagating an internal
        # exception from a function whose contract is to return `unaligned`
        # instead. A firing in the remaining instructions is still caught and
        # recorded as the deadline reason -- understating a finished alignment,
        # the safe direction of the two possible mistakes.
        if alarm_armed:
            signal.alarm(0)
    except _TimedOut:
        return {
            "status": "unaligned",
            "reason": DEADLINE_REASON,
            "witness": witness,
            "anchor": anchor,
            # A fired deadline is only reachable with the alarm armed; recorded
            # explicitly rather than left implied by the reason code, so every
            # record in this module answers the same question the same way.
            "deadline_in_force": True,
        }
    finally:
        if alarm_armed:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
    spans = [
        {
            "witness": {"start": witness_start, "end": witness_start + size},
            "anchor": {"start": anchor_start, "end": anchor_start + size},
        }
        for witness_start, anchor_start, size in blocks
    ]
    if not spans:
        return {
            "status": "unaligned",
            "reason": "no-common-anchor-text",
            "witness": witness,
            "anchor": anchor,
            "deadline_in_force": alarm_armed,
        }
    return {
        "status": "aligned",
        "witness": witness,
        "anchor": anchor,
        "spans": spans,
        "deadline_in_force": alarm_armed,
    }
