"""Whether a reply has fallen into a degenerate loop, judged line by line as it arrives.

A dense index page legitimately gives many similar lines in a row (the same surname
over and over, each row with its own given name and folio), so similarity is never
the signal. What is caught is the model emitting the *exact* same line, or the exact
same short block of lines, again and again: the sealed `[perlector_generation]`
thresholds of `config/decoding.toml` say how many times.

A line ends at a newline or at the JSON escape for one (`\\n`, optionally after
`\\r`), since the answer is one JSON object whose texts carry their rows as escaped
newlines inside a string. Each line is compared whitespace-trimmed, blank lines are
skipped, and only complete lines are judged, so the finding depends on the text alone
and never on how the engine chunked it: `LoopScanner` fed a reply piece by piece
stops at exactly the line `first_repetition_loop` finds in the whole reply.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from common.contracts.errors import ContractError

# The guard's sealed fields (`common.decoding.perlector_loop_guard`).
GUARD_FIELDS: Final = ("loop_line_repeats", "loop_block_repeats", "loop_block_max_lines")
# A block is at least two lines; one line repeated is the line rule's.
MIN_BLOCK_LINES: Final = 2
LINE: Final = "line"
BLOCK: Final = "block"
_LINE_END: Final = re.compile(r"\r?\n|(?:\\r)?\\n")


def validate_guard(guard: Any) -> dict[str, int]:
    """The guard as plain integers, or `ContractError`: exactly its fields, each sensible."""
    if not isinstance(guard, Mapping) or set(guard) != set(GUARD_FIELDS):
        raise ContractError(f"a repetition-loop guard must name exactly {list(GUARD_FIELDS)}")
    values = dict(guard)
    if any(type(value) is not int for value in values.values()):
        raise ContractError("a repetition-loop guard's values must be integers")
    if values["loop_line_repeats"] < 2 or values["loop_block_repeats"] < 2:
        raise ContractError("a repetition loop is at least two repeats of a line or a block")
    if values["loop_block_max_lines"] < MIN_BLOCK_LINES:
        raise ContractError(
            f"a repetition-loop guard's largest block must be at least {MIN_BLOCK_LINES} lines"
        )
    return {name: values[name] for name in GUARD_FIELDS}


def _primitive(block: list[str]) -> bool:
    """Whether the block is not itself a shorter block repeated (its shortest period is its length)."""
    size = len(block)
    return not any(
        size % period == 0 and block == block[:period] * (size // period)
        for period in range(1, size)
    )


class LoopScanner:
    """Judges a reply's complete lines as they arrive; `finding` is set at the first loop.

    `feed` takes the next piece of the reply and returns the finding once there is
    one. The finding is `{kind, block_lines, repeats, line}`: `line` (one line) or
    `block`, how many lines the repeated unit has, how many times it ran when the
    threshold was reached, and the number of the non-blank line that reached it.
    """

    def __init__(self, guard: Mapping[str, int]) -> None:
        self._guard = validate_guard(guard)
        self._pending = ""
        self._lines: list[str] = []
        # How many lines at the tail are the same line.
        self._run = 0
        self.finding: dict[str, Any] | None = None

    def feed(self, text: str) -> dict[str, Any] | None:
        if self.finding is not None:
            return self.finding
        self._pending += text
        pieces = _LINE_END.split(self._pending)
        # The last piece has no line end yet. A trailing `\r` or `\\` or `\\r` may be
        # the start of one, so it stays pending with its piece.
        self._pending = pieces.pop()
        for piece in pieces:
            line = piece.strip()
            if line and self._add(line):
                break
        return self.finding

    def _add(self, line: str) -> bool:
        lines = self._lines
        self._run = self._run + 1 if lines and lines[-1] == line else 1
        lines.append(line)
        guard = self._guard
        if self._run >= guard["loop_line_repeats"]:
            self.finding = self._found(LINE, 1, self._run)
            return True
        repeats = guard["loop_block_repeats"]
        for size in range(MIN_BLOCK_LINES, guard["loop_block_max_lines"] + 1):
            span = size * repeats
            if len(lines) < span:
                break
            block = lines[-size:]
            if not _primitive(block):
                continue
            if all(lines[-span + index] == block[index % size] for index in range(span)):
                self.finding = self._found(BLOCK, size, repeats)
                return True
        return False

    def _found(self, kind: str, size: int, repeats: int) -> dict[str, Any]:
        return {"kind": kind, "block_lines": size, "repeats": repeats, "line": len(self._lines)}


def first_repetition_loop(text: str, guard: Mapping[str, int]) -> dict[str, Any] | None:
    """The first loop in the whole reply, exactly as `LoopScanner` finds it piece by piece."""
    return LoopScanner(guard).feed(text)
