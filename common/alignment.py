"""Bounded, loss-accounted text alignment for page testimony.

The anchor is Chandra's retained text-plus-geometry view.  This module never
chooses a reading: it only says which bytes of a witness report can be attached
to which anchor characters, or records that it cannot say so.
"""

from __future__ import annotations

import html
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from difflib import Match, SequenceMatcher
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
    max_alignment_steps: int


@dataclass(frozen=True)
class DissentLimits:
    """The Perlector's act-length comparison budget, sealed beside the page limits."""

    max_comparison_steps: int


_CONFIG_SCHEMA: Final = {
    "limits": {"max_characters", "max_character_pairs", "max_alignment_steps"},
    "dissent": {"max_comparison_steps"},
}


# The longest HTML5 named entity is `&CounterClockwiseContourIntegral;` at 33
# characters; numeric references are shorter still. The bound matters because
# the terminator is searched for, not assumed: without it a literal ampersand
# in the ink ("Jean & Marie", "&c.") would swallow every character up to the
# next semicolon anywhere later in the document, tags included. See
# `markup_text_view`.
_MAX_ENTITY_CHARACTERS: Final = 40


# Named so a reader of a retained record cannot mistake the instrument giving up
# for a measurement of the witness: this module stopped before it could say
# anything about coverage, so the shortfall is an absent measurement, not
# evidence about the chair.
STEP_LIMIT_REASON: Final = "alignment-step-limit"
# Every reason this module stops on one of its own bounds rather than on a
# comparison: a page witness unaligned for one of these was never measured.
UNMEASURED_REASONS: Final = frozenset(
    {"character-limit", "character-pair-limit", STEP_LIMIT_REASON}
)


# Fields the aligned act-attachment record no longer carries. A record holding
# one was aligned under a wall-clock bound, so whether it aligned depended on
# the machine; it is refused by name rather than as a generic shape error.
_RETIRED_ALIGNED_FIELDS: Final = ("deadline_in_force",)
# Unaligned reasons no current aligner writes. A wall-clock stop said nothing
# about the witness, and read today it would land in `unaligned`, the bucket of
# comparisons made, so it is refused by name rather than counted there.
_RETIRED_UNALIGNED_REASONS: Final = ("alignment-deadline-exceeded",)


def refuse_retired_alignment_record(alignment: Any, subject: str) -> None:
    """Name a retired field or reason an alignment record still carries, so the remedy is plain."""
    if not isinstance(alignment, dict):
        return
    retired = [field for field in _RETIRED_ALIGNED_FIELDS if field in alignment]
    if retired:
        raise SchemaRefusal(
            f"{subject} carries the retired alignment field(s) {retired}: it was aligned "
            "under a wall-clock deadline, not the sealed step budget, so whether it aligned "
            "depended on the machine; re-run the Attestatores alignment under the current "
            "contract"
        )
    reason = alignment.get("reason")
    if alignment.get("status") == "unaligned" and reason in _RETIRED_UNALIGNED_REASONS:
        raise SchemaRefusal(
            f"{subject} carries the retired unaligned reason {reason!r}: it stopped on a "
            "wall-clock deadline, not the sealed step budget, so it measured nothing and "
            "cannot be counted as a comparison made; re-run the Attestatores alignment "
            "under the current contract"
        )


class AlignmentStepLimit(Exception):
    """The matcher ran out of its step budget before it finished."""


class StepCountedMatcher(SequenceMatcher):
    """`difflib.SequenceMatcher` without autojunk, stopped by a count of its own work.

    `find_longest_match`'s inner loop is the matcher's only super-linear work.
    Each call is charged, before it runs, one step per witness character in its
    range plus one per anchor position of that character below the range's
    end. The loop visits those positions and at most one more, where it stops,
    and the one step per character covers that visit, so the charge is at least
    the work. Running out raises `AlignmentStepLimit` before the work, and
    whether an alignment finishes depends only on its two texts and the budget,
    never on the machine or its load.

    The linear work around the loop -- building the position index, extending a
    match, recursing into the halves -- is not charged: it is bounded by the
    text lengths, which the character bounds already cap. The matching itself
    is the standard library's, unchanged.
    """

    def __init__(self, a: str, b: str, steps: int) -> None:
        super().__init__(None, a, b, autojunk=False)
        self.steps_left = steps

    def find_longest_match(
        self, alo: int = 0, ahi: int | None = None, blo: int = 0, bhi: int | None = None
    ) -> Match:
        ahi = len(self.a) if ahi is None else ahi
        bhi = len(self.b) if bhi is None else bhi
        # With no junk every anchor position of a character is in `b2j`, sorted.
        positions = self.b2j
        self.steps_left -= sum(
            1 + bisect_left(positions.get(char, ()), bhi) for char in self.a[alo:ahi]
        )
        if self.steps_left < 0:
            raise AlignmentStepLimit()
        return super().find_longest_match(alo, ahi, blo, bhi)


def _matching_blocks(witness_text: str, anchor_text: str, steps: int) -> list[tuple[int, int, int]]:
    """Return `(witness_start, anchor_start, size)` for every matched run.

    `difflib.SequenceMatcher`'s Ratcliff-Obershelp blocks, longest common
    contiguous block first then recursively to its left and right, with the
    terminating zero-size block dropped. Blocks are strictly ordered and
    non-overlapping on both sides, which is what lets a page alignment be
    clipped to one act's anchor range. Raises `AlignmentStepLimit` past `steps`.

    `autojunk=False` is deliberate: its heuristic treats any element in over
    1% of the sequence as junk, which in French register prose is most of the
    alphabet. This is also what makes the matcher slow on degenerate input,
    hence the step budget.

    Ratcliff-Obershelp rather than a longest-common-subsequence matcher: LCS
    maximizes matched characters, which on two acts opening with the same
    formula can attribute a witness's second-act reading to the first act. A
    coverage-maximizing objective is the wrong one for attaching a reading to
    an anchor; "longest verbatim agreement wins" is the load-bearing one, and
    `common/test_alignment.py` pins that case by name.

    No normalization of its own: the comparison is over the codepoints
    `markup_text_view` produced, so the returned offsets index that same text.
    """
    return [
        (block.a, block.b, block.size)
        for block in StepCountedMatcher(witness_text, anchor_text, steps).get_matching_blocks()
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
    # Lexical rather than `html.parser.HTMLParser`: HTMLParser exposes source
    # offsets only per token, not per character, so its column cannot seed an
    # exact raw-offset map, and it is permissive by design, so it cannot serve
    # as a refusal of malformed markup either. Tags are omitted; entities are
    # one visible character mapped to their opening ampersand.
    plain: list[str] = []
    offsets: list[int] = []
    in_tag = False
    # Only a `<` that actually closes is markup. An unterminated one is
    # ordinary ink ("aged < 30" at the end of a note), and treating it as an
    # opened tag would drop every character after it. One index instead of a
    # per-`<` forward scan: a `<` closes exactly when any `>` exists after it,
    # i.e. when it sits before the last `>` of the whole input.
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
    # post-composition index would mis-point every entry after the first merge.
    # The map is rebuilt through composition instead: the stripped text splits
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


def _read_alignment_config(path: str | Path) -> tuple[dict[str, dict[str, int]], str]:
    """The whole sealed file, closed-schema checked, so either loader refuses it alike."""
    record, digest = read_sealed_toml(path, "alignment configuration")
    if set(record) != set(_CONFIG_SCHEMA) or any(
        not isinstance(record[table], dict) or set(record[table]) != keys
        for table, keys in _CONFIG_SCHEMA.items()
    ):
        raise ContractError("alignment configuration has the wrong closed schema")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for table in record.values()
        for value in table.values()
    ):
        raise ContractError("alignment limits must be positive integers")
    return record, digest


def load_alignment_limits(
    path: str | Path = DEFAULT_ALIGNMENT_CONFIG_PATH,
) -> tuple[AlignmentLimits, str]:
    record, digest = _read_alignment_config(path)
    return AlignmentLimits(**record["limits"]), digest


def load_dissent_limits(
    path: str | Path = DEFAULT_ALIGNMENT_CONFIG_PATH,
) -> tuple[DissentLimits, str]:
    record, digest = _read_alignment_config(path)
    return DissentLimits(**record["dissent"]), digest


def align_to_anchor(witness_raw: str, anchor_raw: str, limits: AlignmentLimits) -> dict[str, Any]:
    """Align a witness comparison view to an anchor, or explicitly `unaligned`.

    The character and pair bounds apply before the matcher runs; the step
    budget (`max_alignment_steps`) bounds the matcher's own work, so a
    low-entropy pair the pair bound admits still stops. No input is clipped: a
    limit produces a retained unaligned result with its reason, and the result
    is a function of the two texts and the limits alone.

    Running out of steps (`STEP_LIMIT_REASON`) is a non-verdict: this module
    made no measurement of coverage, and it is `unaligned` rather than a
    partial map, since publishing spans from a comparison that never finished
    would be worse than saying nothing.
    """
    witness = markup_text_view(witness_raw)
    anchor = markup_text_view(anchor_raw)
    witness_text, anchor_text = witness["text"], anchor["text"]
    if len(witness_text) > limits.max_characters or len(anchor_text) > limits.max_characters:
        reason = "character-limit"
    elif len(witness_text) * len(anchor_text) > limits.max_character_pairs:
        reason = "character-pair-limit"
    else:
        try:
            blocks = _matching_blocks(witness_text, anchor_text, limits.max_alignment_steps)
        except AlignmentStepLimit:
            reason = STEP_LIMIT_REASON
        else:
            if blocks:
                return {
                    "status": "aligned",
                    "witness": witness,
                    "anchor": anchor,
                    "spans": [
                        {
                            "witness": {"start": witness_start, "end": witness_start + size},
                            "anchor": {"start": anchor_start, "end": anchor_start + size},
                        }
                        for witness_start, anchor_start, size in blocks
                    ],
                }
            reason = "no-common-anchor-text"
    return {"status": "unaligned", "reason": reason, "witness": witness, "anchor": anchor}
