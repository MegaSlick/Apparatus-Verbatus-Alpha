"""What the dissent comparison runs on: a step-counted matcher and its sealed budget.

Also the one view a witness's text is compared through before alignment: a
witness that writes its doubt inline has those markers removed, since they are
its doubt, not characters the reading departed from.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from difflib import Match, SequenceMatcher
from pathlib import Path
from typing import Final

from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.uncertainty import UNCERTAINTY_TOKENS
from common.sealed_config import read_sealed_toml

DEFAULT_ALIGNMENT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "alignment.toml"


@dataclass(frozen=True)
class DissentLimits:
    """The Perlector's dissent comparison budget, sealed as `alignment`."""

    max_comparison_steps: int


_CONFIG_SCHEMA: Final = {"dissent": {"max_comparison_steps"}}


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
    text lengths. The matching itself is the standard library's, unchanged.
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


def bracket_marker_view(raw: str) -> str:
    """`raw` with exactly the RecordGold uncertainty markers removed.

    A witness that can express uncertainty (DAI) writes `[UNCERTAIN]` and
    `[CROSSED_OUT]` inline in otherwise plain text; compared raw, each marker's
    characters would count as disagreement with the reading. Only those two
    exact, case-sensitive substrings are removed: no normalization, no
    whitespace folding, so the view does one thing and the comparison's own
    normalization does the rest.
    """
    if not isinstance(raw, str):
        raise SchemaRefusal("bracket marker input is not text")
    kept: list[str] = []
    i = 0
    while i < len(raw):
        token = next((token for token in UNCERTAINTY_TOKENS if raw.startswith(token, i)), None)
        if token is not None:
            i += len(token)
            continue
        kept.append(raw[i])
        i += 1
    return "".join(kept)


def _read_alignment_config(path: str | Path) -> tuple[dict[str, dict[str, int]], str]:
    """The whole sealed file, closed-schema checked."""
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


def load_dissent_limits(
    path: str | Path = DEFAULT_ALIGNMENT_CONFIG_PATH,
) -> tuple[DissentLimits, str]:
    record, digest = _read_alignment_config(path)
    return DissentLimits(**record["dissent"]), digest


def sealed_dissent_budget(context) -> int:
    """The dissent budget this run sealed, read from the configuration the stage was given."""
    limits, digest = load_dissent_limits(context.args.alignment_config)
    context.require_sealed_config("alignment", digest)
    return limits.max_comparison_steps
