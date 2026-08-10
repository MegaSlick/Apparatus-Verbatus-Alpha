"""Tests for the mandatory three-part operator failure contract."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from operations.operator import errors

PACKAGE = Path(__file__).resolve().parent


@pytest.mark.parametrize("code", tuple(errors.ErrorCode), ids=lambda code: code.value)
def test_every_raisable_operator_error_has_three_plain_language_parts(
    code: errors.ErrorCode,
) -> None:
    """The registry, not a hand-picked list of current callers, owns the contract."""

    copy = errors.ERRORS[code]
    assert copy.what_happened.strip()
    assert copy.what_it_means.strip()
    assert copy.next_step.strip()

    rendered = errors.OperatorError(code, detail="saved fixture detail").render()
    assert "What happened:" in rendered
    assert "What it means:" in rendered
    assert "Next step:" in rendered
    assert "Saved detail: saved fixture detail" in rendered


def test_registry_guard_rejects_an_error_code_without_its_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new enum member cannot quietly bypass the recovery-copy table."""

    monkeypatch.delitem(errors.ERRORS, errors.ErrorCode.RUN_FAILED)
    with pytest.raises(RuntimeError, match="operator error registry mismatch"):
        errors.assert_error_registry_complete()


def test_every_declared_error_code_is_one_this_surface_actually_raises() -> None:
    """The completeness proof must not drift from what a verb really raises.

    A table-driven test over the enum proves every *declared* code has its three
    parts. It cannot notice a code nothing reaches — copy written for a state
    that cannot happen reads as coverage and is not. So the enum is checked
    against the code that names it, in the non-test modules only: a code kept
    alive solely by its own test is the same fiction.
    """

    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE.glob("*.py"))
        if not path.name.startswith("test_") and path.name != "errors.py"
    )
    unreached = sorted(
        code.name for code in errors.ErrorCode if not re.search(rf"\b{code.name}\b", sources)
    )

    assert unreached == [], f"declared but never raised: {unreached}"


def test_error_renderer_never_shows_a_raw_traceback_or_old_close_vocabulary() -> None:
    rendered = errors.OperatorError(
        errors.ErrorCode.UNEXPECTED,
        detail="Traceback: provider terminate failed during shutdown",
    ).render()

    assert "Traceback" not in rendered
    assert "terminate" not in rendered.lower()
    assert "shutdown" not in rendered.lower()


def test_old_close_vocabulary_is_replaced_even_without_the_word_traceback() -> None:
    """The substitution table itself, not just the short-circuit around it.

    The detail string above always contains "traceback", which returns a fixed
    generic phrase before the shutdown/terminate/stop substitution table ever
    runs — so that test alone cannot tell the table apart from being deleted.
    This one drives a detail string the table, not the short-circuit, must
    handle.
    """

    rendered = errors.sanitize_detail(
        "provider termination confirmed, shutdown complete, pod stopped"
    )

    assert "termination" not in rendered.lower()
    assert "shutdown" not in rendered.lower()
    assert "stopped" not in rendered.lower()
    assert "close" in rendered
    assert "paused" in rendered


def test_a_path_segment_survives_the_close_vocabulary_rewrite_unmangled() -> None:
    """A receipt path is an identifier, not prose, and must come out byte-true.

    The words this rewrite targets are ordinary English and can legitimately
    appear inside a directory or file name the operator chose, not only inside
    the pipeline's own composed sentences. Rewriting "stop" to "paused" inside
    a path corrupts the exact instruction — "preserve this message and its
    saved receipt path" — this function exists to keep true.
    """

    rendered = errors.sanitize_detail(
        "a technical detail was saved locally at /home/x/stop/terminated-run.log"
    )

    assert "/home/x/stop/terminated-run.log" in rendered


def test_stripping_control_bytes_changes_nothing_else_about_a_line() -> None:
    """The output channel needs the strip without the detail-only rewriting.

    A reconciliation table, a price screen and a close notice must keep their
    spacing, their vocabulary and their full length; only the bytes a terminal
    would act on come out.
    """

    line = "| Delivered acts | 2 |   spaced\x1b[2Jand terminated\x00"

    assert errors.strip_control_bytes(line) == "| Delivered acts | 2 |   spaced[2Jand terminated"
    assert errors.strip_control_bytes("x" * 5000) == "x" * 5000


def test_a_detail_too_long_to_keep_says_it_was_cut() -> None:
    """This text reaches a person through `render`, never a stored receipt.

    A rendered fragment that reads as a whole diagnostic is the partial result
    GOVERNANCE 2 requires to be visibly partial.
    """

    rendered = errors.sanitize_detail("x" * 5000)

    assert rendered.startswith("x" * 2000)
    assert "detail truncated at 2000 characters" in rendered
    assert errors.sanitize_detail("x" * 2000).endswith("x")
    assert "truncated" not in errors.sanitize_detail("x" * 2000)


def test_control_bytes_are_stripped_from_the_operator_facing_detail() -> None:
    """A path an operator did not choose could carry a terminal escape sequence."""

    rendered = errors.sanitize_detail(
        "a file named by the sealed record is not a regular file at "
        "/tmp/src/notes\x1b[2J\x1b[H\x1b[32mFAKE: type CONFIRM to finish\x1b[0m.txt"
    )

    assert "\x1b" not in rendered
    assert "FAKE: type CONFIRM to finish" in rendered
