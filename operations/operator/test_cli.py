"""F004: a top-level flag typed after the verb gets a fix, not a bare 'unrecognized'."""

from __future__ import annotations

import pytest

from operations.operator.errors import ErrorCode, OperatorError

from . import cli


@pytest.mark.parametrize(
    "message",
    (
        "argument verb: invalid choice: 'launch'",
        "the following arguments are required: --run-root",
    ),
)
def test_a_non_unrecognized_argparse_message_is_untouched(message):
    assert cli._annotate_unrecognized(message) == message


def test_an_unrecognized_flag_that_is_not_top_level_only_is_untouched():
    message = "unrecognized arguments: --typo-flag value"
    assert cli._annotate_unrecognized(message) == message


@pytest.mark.parametrize("flag", ("--workspace", "--state-dir", "--notify"))
def test_an_unrecognized_top_level_flag_names_the_fix(flag):
    message = f"unrecognized arguments: {flag} value"
    annotated = cli._annotate_unrecognized(message)
    assert annotated.startswith(message)
    assert flag in annotated
    assert "before the word" in annotated
    assert " is accepted only" in annotated


def test_two_unrecognized_top_level_flags_are_named_together_and_pluralized():
    message = "unrecognized arguments: --state-dir /records --notify"
    annotated = cli._annotate_unrecognized(message)
    assert "--state-dir, --notify are accepted only" in annotated


def test_a_flag_value_form_is_still_matched():
    message = "unrecognized arguments: --state-dir=/records"
    annotated = cli._annotate_unrecognized(message)
    assert "--state-dir is accepted only" in annotated


def test_a_state_dir_typed_after_the_verb_is_refused_with_the_fix_named():
    parser = cli.build_parser()

    with pytest.raises(OperatorError) as excinfo:
        parser.parse_args(["review", "--run-root", "/runs", "--run-id", "r", "--state-dir", "/x"])

    assert excinfo.value.code is ErrorCode.INVALID_COMMAND
    assert "--state-dir is accepted only" in excinfo.value.render()
    assert "before the word" in excinfo.value.render()


def test_a_genuine_typo_after_the_verb_still_reads_as_a_plain_refusal():
    parser = cli.build_parser()

    with pytest.raises(OperatorError) as excinfo:
        parser.parse_args(["review", "--run-root", "/runs", "--run-id", "r", "--typo-flag", "x"])

    assert excinfo.value.code is ErrorCode.INVALID_COMMAND
    assert "is accepted only" not in excinfo.value.render()
