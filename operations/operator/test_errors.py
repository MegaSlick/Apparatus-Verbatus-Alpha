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
