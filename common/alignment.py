"""Bounded, loss-accounted text alignment for page testimony.

The anchor is Chandra's retained text-plus-geometry view.  This module never
chooses a reading: it only says which bytes of a witness report can be attached
to which anchor characters, or records that it cannot say so.
"""

from __future__ import annotations

import html
import signal
import threading
import tomllib
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Final

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
# measurement (GOVERNANCE 10).
DEADLINE_REASON: Final = "alignment-deadline-exceeded"


def _matching_blocks(witness_text: str, anchor_text: str) -> list[tuple[int, int, int]]:
    """Return `(witness_start, anchor_start, size)` for every matched run.

    `difflib.SequenceMatcher`'s Ratcliff-Obershelp blocks -- longest common
    contiguous block first, then the same search recursively to its left and to
    its right -- with the terminating zero-size block dropped. Blocks are
    strictly ordered and non-overlapping on both sides, which is the property
    `pipeline/3_attestatores/run.py` relies on when it clips a page alignment to
    one act's anchor range: a witness offset can only be attributed to an act
    whose anchor range surrounds it in the same order.

    `autojunk=False` is deliberate. The heuristic it disables treats any element
    appearing in more than 1% of the second sequence as junk, and in French
    register prose that is most of the alphabet, so leaving it on would refuse
    to match ordinary ink. It is also what makes the matcher slow on degenerate
    input; the wall-clock backstop below exists because of it.

    **RapidFuzz's Indel/LCS opcodes were tried here and refused, on measurement
    (hostile review C, 2026-09-07).** They are four orders of magnitude faster
    -- the slowest input the sealed pair bound admits goes from 283.9 s to
    0.011 s -- and on identical or near-identical page text they return exactly
    these blocks. But LCS maximizes matched *characters*, and where that ties, it
    breaks the tie towards the earliest match. Register acts open with the same
    formula, so a witness that read only the second of two acts ties: the whole
    reading against the second act's anchor range (what this returns), or the
    shared opening against the FIRST act plus the remainder against the second
    (what LCS returns). Both attach 40 of 40 characters; only one of them says
    what the witness actually read. The pipeline's own `confirmed-blank`
    scenario failed on exactly that -- the witness's act-two opening was
    attributed to act one, twelve characters of the page fell outside every act
    attachment, and both acts were held instead of the blank being sealed. A
    coverage-maximizing objective is the wrong objective for attaching a reading
    to an anchor; "longest verbatim agreement wins" is the right one, and it is
    the one that is load-bearing here. `common/test_alignment.py` pins that case
    by name so the swap is not retried blind.

    No normalization of its own: the comparison is over the Python `str`
    codepoints `markup_text_view` produced, so case, NFC/NFD distinctions and
    astral characters survive, and the returned offsets index the same
    normalized text the offset map was built against.
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
                # Two conditions, and both are load-bearing. The terminator must
                # be inside `_MAX_ENTITY_CHARACTERS`, and the candidate must
                # actually decode to something else -- `html.unescape` returns a
                # non-entity unchanged, so that equality is the test for "this
                # ampersand began an entity" rather than "a semicolon exists
                # somewhere ahead". Without them `<p>Jean & Marie</p><p>born
                # 1688</p><i>note; here</i>` normalized to
                # `Jean & Marie</p><p>born 1688</p><i>note; here`: raw markup
                # inside the markup-stripped view, `loss.markup_characters`
                # under-reporting it, and every intervening tag then read as
                # witness disagreement by dissent. Found in audit; F-X1.
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
    # NFC can change codepoint count, so indexing the pre-composition offsets
    # with a post-composition index mis-points every entry after the first
    # merge (an NFD French line was measured at 7 of 14 offsets wrong). The
    # map is rebuilt through composition instead: the stripped text splits
    # into clusters at combining-class-0 starters, NFC composes only within
    # such a cluster for this corpus's canonical text, and every composed
    # character maps to its cluster's first raw offset. Where per-cluster
    # composition cannot reproduce the composed text (starter-starter
    # composition, e.g. Hangul jamo), the map records None for every entry
    # rather than publishing offsets that may lie -- an absent measurement,
    # never a fabricated one (GOVERNANCE 10).
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


def load_alignment_limits(
    path: str | Path = DEFAULT_ALIGNMENT_CONFIG_PATH,
) -> tuple[AlignmentLimits, str]:
    try:
        raw = Path(path).read_bytes()
        record = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(f"alignment configuration at {path} could not be read") from error
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
    return AlignmentLimits(**values), digest_bytes(raw)


def align_to_anchor(witness_raw: str, anchor_raw: str, limits: AlignmentLimits) -> dict[str, Any]:
    """Align a witness comparison view to an anchor, or explicitly `unaligned`.

    The character and pair bounds always apply before the matcher runs. The
    wall-clock deadline applies only where this call owns the process real-time
    timer (main thread, POSIX `SIGALRM`, no timer already armed); elsewhere the
    comparison runs unbounded under the caller's own deadline, with the pair
    bound still refusing the pathological case. No input is clipped: a limit or
    a fired deadline produces a retained unaligned result with its reason.

    A fired deadline is `DEADLINE_REASON`, and it is a non-verdict: this module
    made no measurement of coverage, and nothing downstream may read it as one.
    It is still `unaligned` rather than a partial map, because publishing spans
    a timed-out comparison never finished would be worse than saying nothing
    (GOVERNANCE 2/10).

    **The deadline is sized from the legitimate ceiling, not the pathological
    one, and it does not clear the pathological one.** An unaligned page witness
    is not `comparable`, so it leaves the act's witness floor: a deadline short
    enough to fire on real work records a slow comparison as coverage that is
    missing (GOALS 1, hostile review C). A 7,500-character page whose acts
    repeat one formula verbatim -- a scribe copying one form -- measures 10.1 s,
    already past the five seconds this config used to carry, so 25 s is what it
    now carries. Two *different* low-entropy chair responses at exactly
    `max_character_pairs` measure 283.9 s and still reach the deadline; no value
    closes that without costing minutes per (page, chair). Closing it needs the
    matcher, and `pipeline/3_attestatores/HANDOFF.md` records both the
    measurements and the design that would.
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
        # Cancelled inside the `try`, not only in the `finally` -- the same
        # window `pipeline/4_perlector/dissent.py::_aligned_within_deadline`
        # already closes for its own SIGALRM, still open here. An alarm firing
        # after `_matching_blocks` returned but before the `finally` ran raised
        # `_TimedOut` from inside the `finally`, past the `except` above, so a
        # *successful* alignment propagated an internal exception out of a
        # function whose whole contract is to return an `unaligned` record
        # instead. Cancelling here does not close the window completely: a
        # firing in the remaining instructions is caught by the `except` and
        # recorded as the deadline reason, which understates a finished
        # alignment rather than crashing the stage. That is the safe direction
        # of the two.
        if alarm_armed:
            signal.alarm(0)
    except _TimedOut:
        return {
            "status": "unaligned",
            "reason": DEADLINE_REASON,
            "witness": witness,
            "anchor": anchor,
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
        }
    return {"status": "aligned", "witness": witness, "anchor": anchor, "spans": spans}
