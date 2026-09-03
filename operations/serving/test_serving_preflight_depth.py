"""The launch-audit copy is bounded, so a pathological audit is named.

`_plain_mapping` copies nested mappings and tuples out of the immutable launch
audit a `ServiceHandle` carries, and it recurses. What it walks is assembled by
this package, so the depth really is small -- but that is a claim about today's
code, and the walk that trusts it had no way to say so if a later edit or a
damaged handle made it wrong. It would have answered with a `RecursionError`
from inside receipt assembly, naming neither the audit nor the field.

A bound is the proportionate answer here rather than an explicit-stack rewrite:
this is repository-internal operational evidence of known shape, not untrusted
input, and the refusal shape is the one `operations/submit/inventory.py::_walk`
already uses for a submission's directory tree.
"""

from __future__ import annotations

import pytest

from operations.serving.errors import ServingConfigurationError
from operations.serving.preflight import MAX_AUDIT_DEPTH, _plain_mapping


def _nested(levels: int) -> dict[str, object]:
    value: dict[str, object] = {"leaf": "audit value"}
    for _ in range(levels):
        value = {"nested": value}
    return value


def test_an_audit_nested_past_the_bound_is_refused_by_name():
    with pytest.raises(ServingConfigurationError, match=f"deeper than {MAX_AUDIT_DEPTH} levels"):
        _plain_mapping(_nested(MAX_AUDIT_DEPTH + 5))


def test_an_audit_inside_the_bound_is_copied_exactly_as_before():
    """The bound is a bound, not a ceiling ordinary evidence trips."""
    inside = _nested(MAX_AUDIT_DEPTH - 1)
    assert _plain_mapping(inside) == inside


def test_the_copy_still_detaches_tuples_into_lists():
    """The bound is threaded through the tuple branch too, so a mapping inside a
    tuple keeps being copied rather than being handed back by reference."""
    entry = {"port": 8000}
    copied = _plain_mapping({"children": (entry, "plain")})
    assert copied == {"children": [{"port": 8000}, "plain"]}
    assert copied["children"][0] is not entry
