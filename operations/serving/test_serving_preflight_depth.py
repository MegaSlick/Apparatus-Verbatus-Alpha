"""The frozen-JSON thaw is bounded, so a pathological value is named.

`thawed_json` copies nested mappings and tuples out of frozen values such as
the immutable launch audit a `ServiceHandle` carries, and it recurses. What it
walks is assembled by this package, so the depth really is small -- but the
walk that trusts that has no way to say so if a later edit or a damaged handle
made it wrong. It would answer with a `RecursionError` from inside receipt
assembly, naming neither the value nor the field.

A bound is the proportionate answer here rather than an explicit-stack rewrite:
this is repository-internal operational evidence of known shape, not untrusted
input, and the refusal shape is the one `operations/submit/inventory.py::_walk`
already uses for a submission's directory tree.
"""

from __future__ import annotations

import json
from types import MappingProxyType

import pytest

from operations.serving.config import MAX_JSON_DEPTH, thawed_json
from operations.serving.errors import ServingConfigurationError


def _nested(levels: int) -> dict[str, object]:
    value: dict[str, object] = {"leaf": "audit value"}
    for _ in range(levels):
        value = {"nested": value}
    return value


def test_an_audit_nested_past_the_bound_is_refused_by_name():
    with pytest.raises(ServingConfigurationError, match=f"deeper than {MAX_JSON_DEPTH} levels"):
        thawed_json(_nested(MAX_JSON_DEPTH + 5))


def test_an_audit_inside_the_bound_is_copied_exactly_as_before():
    """The bound is a bound, not a ceiling ordinary evidence trips."""
    inside = _nested(MAX_JSON_DEPTH - 1)
    assert thawed_json(inside) == inside


def test_the_copy_still_detaches_tuples_into_lists():
    """The bound is threaded through the tuple branch too, so a mapping inside a
    tuple keeps being copied rather than being handed back by reference."""
    entry = {"port": 8000}
    copied = thawed_json({"children": (entry, "plain")})
    assert copied == {"children": [{"port": 8000}, "plain"]}
    assert copied["children"][0] is not entry


def test_the_bound_is_exact_at_the_level_it_names():
    """Off by one either way and one of these two fails.

    `_nested(n)` puts its deepest mapping at exactly depth `n`, which is the
    number `thawed_json` compares against `MAX_JSON_DEPTH`. A bound tested
    only at `-1` and `+5` is a bound tested nowhere near its edge: it would pass
    just as well if the walk refused a level early or accepted a level late, and
    a copy that refuses one level early refuses real evidence.
    """
    at_the_bound = _nested(MAX_JSON_DEPTH)
    assert thawed_json(at_the_bound) == at_the_bound

    with pytest.raises(ServingConfigurationError, match=f"deeper than {MAX_JSON_DEPTH} levels"):
        thawed_json(_nested(MAX_JSON_DEPTH + 1))


def test_a_mapping_under_two_tuple_levels_is_copied_rather_than_handed_back():
    """The copy exists so a receipt carries plain JSON-shaped values.

    A walk that looked only one level into a tuple would leave an immutable
    `MappingProxyType` under `((mapping,),)`, and `json.dumps` would raise
    inside receipt assembly. The result is asserted to serialize, which is the
    property the receipt actually needs.
    """
    inner = MappingProxyType({"port": 8000})
    copied = thawed_json({"children": ((inner, "plain"),)})

    assert copied == {"children": [[{"port": 8000}, "plain"]]}
    assert type(copied["children"][0][0]) is dict
    assert json.dumps(copied) == '{"children": [[{"port": 8000}, "plain"]]}'


def test_a_pathological_chain_of_tuples_is_refused_by_name():
    """Depth is counted per sequence level, not only per mapping level.

    Otherwise a value could nest arbitrarily far inside tuples and the bound
    would never see it; the chain gets the same named refusal a chain of
    mappings gets."""
    deep: object = MappingProxyType({"leaf": "audit value"})
    for _ in range(MAX_JSON_DEPTH + 5):
        deep = (deep,)

    with pytest.raises(ServingConfigurationError, match=f"deeper than {MAX_JSON_DEPTH} levels"):
        thawed_json({"launched": deep})


def test_the_copy_is_detached_at_every_level_not_only_at_the_root():
    """A receipt keeps the audit it was handed, whatever the handle does next.

    A root-only copy still shares every nested mapping with the source, so this
    mutates the source after copying and requires the copy not to move. The
    identity assertions say the same thing structurally: no level of the result
    is the object it was copied from.
    """
    source = {"outer": {"inner": {"port": 8000}}, "chairs": [{"role": "perlector"}]}
    copied = thawed_json(source)
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
