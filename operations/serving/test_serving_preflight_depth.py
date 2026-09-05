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

import json
from types import MappingProxyType

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


def test_the_bound_is_exact_at_the_level_it_names():
    """Off by one either way and one of these two fails.

    `_nested(n)` puts its deepest mapping at exactly depth `n`, which is the
    number `_plain_mapping` compares against `MAX_AUDIT_DEPTH`. A bound tested
    only at `-1` and `+5` is a bound tested nowhere near its edge: it would pass
    just as well if the walk refused a level early or accepted a level late, and
    a copy that refuses one level early refuses real evidence.
    """
    at_the_bound = _nested(MAX_AUDIT_DEPTH)
    assert _plain_mapping(at_the_bound) == at_the_bound

    with pytest.raises(ServingConfigurationError, match=f"deeper than {MAX_AUDIT_DEPTH} levels"):
        _plain_mapping(_nested(MAX_AUDIT_DEPTH + 1))


def test_a_mapping_under_two_tuple_levels_is_copied_rather_than_handed_back():
    """The copy exists so a receipt carries plain JSON-shaped values.

    A walk that looked one level into a tuple copied `(mapping,)` and left
    `((mapping,),)` alone, so an immutable `MappingProxyType` survived into the
    receipt and `json.dumps` raised `TypeError: Object of type mappingproxy is
    not JSON serializable` -- from inside receipt assembly, naming neither the
    audit nor the field. The result is asserted to serialize, which is the
    property the receipt actually needs and the one that failed.
    """
    inner = MappingProxyType({"port": 8000})
    copied = _plain_mapping({"children": ((inner, "plain"),)})

    assert copied == {"children": [[{"port": 8000}, "plain"]]}
    assert type(copied["children"][0][0]) is dict
    assert json.dumps(copied) == '{"children": [[{"port": 8000}, "plain"]]}'


def test_a_pathological_chain_of_tuples_is_refused_by_name():
    """Depth is counted per sequence level, not only per mapping level.

    Until it was, a tuple chain cost the walk nothing at all: the tuples were
    never entered, so an audit could nest arbitrarily far inside them and the
    bound never saw it. Now the same chain is answered by the same named
    refusal a chain of mappings gets."""
    deep: object = MappingProxyType({"leaf": "audit value"})
    for _ in range(MAX_AUDIT_DEPTH + 5):
        deep = (deep,)

    with pytest.raises(ServingConfigurationError, match=f"deeper than {MAX_AUDIT_DEPTH} levels"):
        _plain_mapping({"launched": deep})


def test_the_copy_is_detached_at_every_level_not_only_at_the_root():
    """A receipt keeps the audit it was handed, whatever the handle does next.

    A root-only copy still shares every nested mapping with the source, so this
    mutates the source after copying and requires the copy not to move. The
    identity assertions say the same thing structurally: no level of the result
    is the object it was copied from.
    """
    source = {"outer": {"inner": {"port": 8000}}, "chairs": [{"role": "perlector"}]}
    copied = _plain_mapping(source)
    assert copied == source

    assert copied is not source
    assert copied["outer"] is not source["outer"]
    assert copied["outer"]["inner"] is not source["outer"]["inner"]
    assert copied["chairs"] is not source["chairs"]
    assert copied["chairs"][0] is not source["chairs"][0]

    source["outer"]["inner"]["port"] = 9001
    source["chairs"][0]["role"] = "attestator_1"
    source["chairs"].append({"role": "designator"})
    assert copied == {"outer": {"inner": {"port": 8000}}, "chairs": [{"role": "perlector"}]}
