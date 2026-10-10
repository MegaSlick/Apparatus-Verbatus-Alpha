"""The repetition-loop guard: exact repeats stop a reply, similar rows never do."""

from __future__ import annotations

import json

import pytest

from common.contracts.errors import ContractError
from common.decoding import load_decoding_policy, perlector_loop_guard
from common.repetition_loop import LoopScanner, first_repetition_loop, validate_guard

GUARD = perlector_loop_guard(load_decoding_policy()[0])
GIVEN = ["Jean", "Marie", "Joseph", "Louise", "Pierre", "Angelique", "Francois", "Josephte"]


def _dense_index_rows(count: int) -> list[str]:
    """Synthetic index rows: one surname for long runs, each row its own given name and folio."""
    return [
        f"{'Tremblay' if index < count * 3 // 4 else 'Gagnon'}, {GIVEN[index % len(GIVEN)]} "
        f"f. {12 + index // 3}"
        for index in range(count)
    ]


def _answer(text: str) -> str:
    """A page answer as the reader writes it: pretty-printed JSON, rows as escaped newlines."""
    return json.dumps(
        {
            "entries": [
                {
                    "n": 1,
                    "kind": "other",
                    "label": "index",
                    "cites": [f"L{line}" for line in range(1, 265)],
                    "text": text,
                }
            ],
            "set_aside": [],
        },
        indent=1,
    )


def test_the_shipped_guard_is_the_sealed_one():
    assert GUARD == {"loop_line_repeats": 30, "loop_block_repeats": 10, "loop_block_max_lines": 8}


def test_a_dense_index_of_similar_but_distinct_rows_passes_untouched():
    rows = _dense_index_rows(264)
    for text in ("\n".join(rows) + "\n", _answer("\n".join(rows))):
        assert first_repetition_loop(text, GUARD) is None
    # The same surname on many rows in a row is not a loop, nor are ditto marks between
    # distinct rows, nor 29 identical lines (one short of the sealed 30).
    ditto = [line for row in rows[:60] for line in (row, "do")]
    assert first_repetition_loop("\n".join(ditto) + "\n", GUARD) is None
    assert first_repetition_loop("Tremblay\n" * 29 + "Gagnon\n", GUARD) is None


def test_an_exact_line_loop_is_found_at_the_thirtieth_line():
    rows = _dense_index_rows(40)
    looped = rows + ["Tremblay, Jean f. 12"] * 200
    finding = first_repetition_loop("\n".join(looped), GUARD)
    assert finding == {"kind": "line", "block_lines": 1, "repeats": 30, "line": 40 + 30}
    # Inside a JSON string, rows end at the escaped newline; trimming and blank lines
    # between repeats do not hide the loop.
    assert first_repetition_loop(_answer("\n".join(looped)), GUARD)["kind"] == "line"
    spaced = "\n\n".join(f"  {line} " for line in looped)
    assert first_repetition_loop(spaced, GUARD) == finding


def test_a_repeated_three_line_block_is_found_at_its_tenth_repeat():
    block = ["Tremblay, Jean f. 12", "Gagnon, Marie f. 12", "Roy, Pierre f. 13"]
    rows = _dense_index_rows(20)
    finding = first_repetition_loop("\n".join(rows + block * 50) + "\n", GUARD)
    assert finding == {"kind": "block", "block_lines": 3, "repeats": 10, "line": 20 + 30}
    # Nine repeats are a page; ten are a loop.
    assert first_repetition_loop("\n".join(rows + block * 9) + "\n", GUARD) is None


def test_a_block_that_is_one_line_repeated_is_the_line_rule_not_the_block_rule():
    # Twenty identical lines are ten repeats of a two-line block of that line; only the
    # line rule judges them, so the sealed 30 is never undercut.
    assert first_repetition_loop("same\n" * 29, GUARD) is None


def test_only_complete_lines_are_judged_and_chunking_never_changes_the_finding():
    text = _answer("\n".join(_dense_index_rows(30) + ["Roy, Pierre f. 13"] * 40))
    whole = first_repetition_loop(text, GUARD)
    assert whole is not None
    for size in (1, 2, 3, 7, 64):
        scanner = LoopScanner(GUARD)
        found = None
        for start in range(0, len(text), size):
            found = scanner.feed(text[start : start + size])
            if found is not None:
                break
        assert found == whole
    # The thirtieth repeat without its line end is not yet a complete line.
    assert first_repetition_loop("x\n" * 29 + "x", GUARD) is None


@pytest.mark.parametrize(
    "guard",
    [
        {**GUARD, "loop_line_repeats": 1},
        {**GUARD, "loop_block_max_lines": 1},
        {**GUARD, "loop_block_repeats": True},
        {k: v for k, v in GUARD.items() if k != "loop_block_repeats"},
    ],
)
def test_a_guard_that_is_not_closed_and_sensible_is_refused(guard):
    with pytest.raises(ContractError):
        validate_guard(guard)
