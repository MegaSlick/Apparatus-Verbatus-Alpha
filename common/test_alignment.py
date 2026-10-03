"""The dissent comparison's marker view, step-counted matcher and sealed budget loader."""

from difflib import SequenceMatcher

import pytest

from common.alignment import (
    DEFAULT_ALIGNMENT_CONFIG_PATH,
    StepCountedMatcher,
    bracket_marker_view,
    load_dissent_limits,
)
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.uncertainty import UNCERTAINTY_TOKENS
from common.sealed_config import read_sealed_toml
from conftest import load_stage

# --- bracket_marker_view: removes exactly the RecordGold uncertainty markers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Marie [UNCERTAIN] Dubois [CROSSED_OUT]", "Marie  Dubois "),
        ("plain established text, no markers here", "plain established text, no markers here"),
        # Neighbouring whitespace and NFD accents survive exactly as reported.
        ("Genevie\u0300ve [UNCERTAIN]  ne\u0301e", "Genevie\u0300ve   ne\u0301e"),
        ("[UNCERTAIN][CROSSED_OUT]tail", "tail"),
        # Only an exact token counts, never a prefix or a loose bracket scan.
        (
            "[UNCERTAIN unterminated] and [CROSSED_OUT_EXTRA]",
            "[UNCERTAIN unterminated] and [CROSSED_OUT_EXTRA]",
        ),
        ("[UNCERTAIN][CROSSED_OUT][UNCERTAIN]", ""),
    ],
)
def test_bracket_marker_view_removes_exactly_the_markers(raw, expected):
    assert bracket_marker_view(raw) == expected


def test_bracket_marker_view_refuses_non_text_input():
    with pytest.raises(SchemaRefusal, match="not text"):
        bracket_marker_view(b"[UNCERTAIN] not a str")


def test_feedings_uncertainty_tokens_are_the_contracts():
    feeding_module = load_stage("3_attestatores", "feeding")

    assert UNCERTAINTY_TOKENS == feeding_module._UNCERTAINTY_TOKENS


# --- The step-counted matcher dissent runs on


class _VisitCounter(dict):
    """`b2j` with every position list counting the times `difflib` iterates it.

    `find_longest_match`'s inner loop is `for j in b2j.get(a[i], nothing)`, so
    each yielded position is one visit of the loop the step budget charges for,
    counted by the loop itself rather than by the charge's own formula.
    """

    visits = 0

    def __init__(self, b2j: dict) -> None:
        super().__init__(
            {char: _CountedPositions(self, positions) for char, positions in b2j.items()}
        )


class _CountedPositions(list):
    def __init__(self, counter: _VisitCounter, positions: list[int]) -> None:
        super().__init__(positions)
        self.counter = counter

    def __iter__(self):
        for position in super().__iter__():
            self.counter.visits += 1
            yield position


def _charge_and_visits(witness: str, anchor: str) -> tuple[int, int]:
    matcher = StepCountedMatcher(witness, anchor, 10**12)
    counter = matcher.b2j = _VisitCounter(matcher.b2j)
    matcher.get_matching_blocks()
    return 10**12 - matcher.steps_left, counter.visits


def test_the_charge_is_a_hand_counted_number_and_covers_every_inner_loop_visit():
    """ "ab" against "abab" is one search: two witness characters scanned, each
    with two anchor positions below the range's end, so 2 + 2 + 2 = 6 steps
    for 4 visits. The longest match is the whole witness, so neither side
    recurses. Over other pairs the charge is never below the visits `difflib`
    actually makes: the budget counts at least the work."""
    assert _charge_and_visits("ab", "abab") == (6, 4)
    pairs = [
        ("alpha beta gamma", "alpha beta gamna"),
        ("abcabcabcabc", "cbacbacba"),
        ("a" * 300, "a" * 200 + "b" + "a" * 50),
        ("et de Marie Bernard, laboureur", "et de Marie Bernart, laboreur de ceste paroisse"),
    ]
    for witness, anchor in pairs:
        charged, visits = _charge_and_visits(witness, anchor)
        assert 0 < visits <= charged, (witness, anchor)


def test_the_step_counted_matcher_returns_exactly_the_standard_library_blocks():
    """The budget counts the work and changes none of it: every block and
    opcode is `difflib`'s own, so no dissent row moves because it is counted."""
    pairs = [
        ("alpha beta gamma", "alpha beta gamna"),
        ("abcabcabcabc", "cbacbacba"),
        (
            "et de Marie Bernard, laboureur de cette paroisse, en presence de Jean Moreau",
            "et de Marie Bernart, laboureur de ceste parroisse, en presence de Jan Moreau",
        ),
        ("", "alpha"),
        ("a" * 300, "a" * 200 + "b" + "a" * 50),
    ]
    for witness, anchor in pairs:
        reference = SequenceMatcher(None, witness, anchor, autojunk=False)
        counted = StepCountedMatcher(witness, anchor, 10**9)
        assert counted.get_matching_blocks() == reference.get_matching_blocks()
        assert counted.get_opcodes() == reference.get_opcodes()


# --- The dissent budget loader: the only gate between config/alignment.toml and every run


def test_the_loader_returns_the_sealed_dissent_budget_and_the_file_seal():
    record, digest = read_sealed_toml(DEFAULT_ALIGNMENT_CONFIG_PATH, "alignment")
    dissent, dissent_digest = load_dissent_limits()
    assert dissent_digest == digest
    assert dissent.max_comparison_steps == record["dissent"]["max_comparison_steps"] > 0


def test_the_loader_reads_a_valid_file(tmp_path):
    path = tmp_path / "limits.toml"
    path.write_text("[dissent]\nmax_comparison_steps = 7\n")
    assert load_dissent_limits(path)[0].max_comparison_steps == 7


def test_the_loader_refuses_an_unreadable_file(tmp_path):
    with pytest.raises(ContractError, match="could not be read"):
        load_dissent_limits(tmp_path / "absent.toml")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[dissent]\nmax_comparison_step = 1\n",
        "[dissent]\nmax_comparison_steps = 1\nextra = 1\n",
        "[dissent]\nmax_comparison_steps = 1\n[limits]\nmax_characters = 1\n",
    ],
)
def test_the_loader_refuses_an_unknown_or_missing_key(tmp_path, text):
    path = tmp_path / "limits.toml"
    path.write_text(text)
    with pytest.raises(ContractError, match="closed schema"):
        load_dissent_limits(path)


@pytest.mark.parametrize("bad", ['"3"', "true", "0", "-1", "1.5"])
def test_the_loader_refuses_a_value_that_is_not_a_positive_integer(tmp_path, bad):
    """`true` would parse as 1 and quietly cut every comparison to one step; a
    float or string would reach the matcher's budget at run time. The loader is
    where those stop."""
    path = tmp_path / "limits.toml"
    path.write_text(f"[dissent]\nmax_comparison_steps = {bad}\n")
    with pytest.raises(ContractError, match="positive integers"):
        load_dissent_limits(path)
