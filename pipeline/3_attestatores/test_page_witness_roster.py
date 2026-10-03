"""The sealed page-witness roster, read and refused before any page record is written."""

from types import SimpleNamespace

import pytest

from common.chairs.models import ChairIdentity
from common.contracts.errors import SchemaRefusal
from conftest import load_stage

attestatores = load_stage("3_attestatores")


def _identity(role: str, scope: str) -> ChairIdentity:
    return ChairIdentity(
        role=role,
        source="local-repository",
        repo=None,
        path=role,
        revision=None,
        digest_manifest="a" * 64,
        manifest=f"manifests/{role}.json",
        adapter_of=None,
        serving_recipe="fixture",
        license_note="fixture",
        witness_adapter="churro.v1",
        witness_scope=scope,
    )


def _scope_context(chairs=None, *, scopes=None, **fields):
    scopes = scopes or {"attestator_1": "page", "attestator_3": "page"}
    configured = {role: _identity(role, scope) for role, scope in scopes.items()}
    return SimpleNamespace(
        witness_chairs=list(scopes) if chairs is None else chairs,
        registry=SimpleNamespace(config=SimpleNamespace(chairs=configured)),
        **fields,
    )


@pytest.mark.parametrize(
    "bad_chair",
    ([], {}, [[]], {"nested": []}, {"a": {"b": [1, 2]}}, [[[]]], [{"a": [{}]}]),
)
def test_declared_page_witness_chairs_refuses_unhashable_json_values(bad_chair):
    context = _scope_context([bad_chair])

    with pytest.raises(SchemaRefusal, match="unique list of chair names"):
        attestatores.declared_page_witness_chairs(context)


def test_an_unknown_page_witness_chair_is_refused_before_the_pass_writes():
    context = _scope_context(["attestator_33"])

    with pytest.raises(SchemaRefusal, match="absent from the current models configuration"):
        attestatores.preflight(context, [], 1, {}, fixture=False)


def test_an_unknown_page_witness_chair_is_refused_by_the_shared_accessor_itself():
    """Every page-record writer reads `declared_page_witness_chairs` directly, so
    the roster check must live in the accessor itself or a mismatched chair
    silently reaches a write path unrefused."""
    context = _scope_context(["attestator_33"])

    with pytest.raises(SchemaRefusal, match="absent from the current models configuration"):
        attestatores.declared_page_witness_chairs(context)


def test_the_roster_refusal_names_the_roster_and_not_only_the_offender():
    context = _scope_context(["attestator_33"])

    with pytest.raises(SchemaRefusal) as caught:
        attestatores.declared_page_witness_chairs(context)
    message = str(caught.value)
    assert "attestator_33" in message
    assert "attestator_1" in message and "attestator_3" in message
    assert "do not describe the same witness set" in message
    assert "start a new run; do not edit sealed evidence" in message


@pytest.mark.parametrize(
    "bad_chair",
    (
        float("nan"),
        float("inf"),
        1.5,
        True,
        pytest.param(10**5000, id="huge-int"),
        None,
        # Recursive values can reach direct callers; validation must not walk
        # them, hash them, or render them.
        "recursive",
    ),
)
def test_declared_page_witness_chairs_refuses_values_no_chair_name_could_be(bad_chair):
    if bad_chair == "recursive":
        recursive: list = []
        recursive.append(recursive)
        bad_chair = recursive
    context = _scope_context([bad_chair])

    with pytest.raises(SchemaRefusal, match="unique list of chair names"):
        attestatores.declared_page_witness_chairs(context)


def test_a_chair_name_carrying_a_surrogate_is_refused_printably():
    """A chair name is a string, so it clears the shape check and reaches the
    roster refusal — which then puts it in a message an operator's stderr has to
    encode. `repr` escapes the surrogate; an f-string interpolating it raw would
    raise `UnicodeEncodeError` out of the report of the refusal."""
    context = _scope_context(["attestator_\ud800"])

    with pytest.raises(SchemaRefusal) as caught:
        attestatores.declared_page_witness_chairs(context)
    str(caught.value).encode("utf-8")


@pytest.mark.parametrize("chair", ("NaN", "attestator_\0"))
def test_hostile_but_encodable_chair_strings_are_refused_printably(chair):
    context = _scope_context([chair])

    with pytest.raises(SchemaRefusal) as caught:
        attestatores.declared_page_witness_chairs(context)
    str(caught.value).encode("utf-8")


def test_no_testimonium_is_sealed_before_the_declaration_is_validated():
    """The timing guarantee, driven rather than reasoned about.

    `publish_page_testimonium` builds its payload first and publishes last, so
    the constraint is that the roster is read on the near side of the write. A
    recording context answers it: the refusal must arrive with the publish list
    still empty, because a page record sealed for a chair the sealed roster does
    not name is immutable and nothing later can take it back.
    """
    published: list = []
    context = _scope_context(
        ["attestator_33"],
        adapter_revision="fake-attestatores-v0",
        publish=lambda **kwargs: published.append(kwargs),
    )

    with pytest.raises(SchemaRefusal, match="absent from the current models configuration"):
        attestatores.publish_page_testimonium(
            context,
            chair="attestator_1",
            resolved=_identity("attestator_1", "page"),
            page_ordinal=1,
            attempt=attestatores.not_run_attempt("fixture test needs no live chair"),
            ordinal=1,
            page_ids={1: "page-1"},
            live=False,
        )
    assert published == [], "a page record sealed before the declaration was validated"


def test_a_page_record_for_a_chair_outside_the_page_roster_is_refused_before_it_is_built():
    published: list = []
    context = _scope_context(publish=lambda **kwargs: published.append(kwargs))

    with pytest.raises(attestatores.FatalAccounting, match="not a page witness"):
        attestatores.publish_page_testimonium(
            context,
            chair="attestator_2",
            resolved=_identity("attestator_2", "page"),
            page_ordinal=1,
            attempt=attestatores.not_run_attempt("fixture test needs no live chair"),
            ordinal=1,
            page_ids={1: "page-1"},
            live=False,
        )
    assert published == []


def test_a_detector_page_record_for_a_chair_outside_the_page_roster_is_refused_before_it_is_built():
    published: list = []
    context = _scope_context(publish=lambda **kwargs: published.append(kwargs))

    with pytest.raises(attestatores.FatalAccounting, match="not a page witness"):
        attestatores.publish_detector_page_testimonium(
            context,
            chair="attestator_2",
            resolved=_identity("attestator_2", "page"),
            page_ordinal=1,
            ordinal=1,
            served=[],
            receipt_ref=None,
            page_ids={1: "page-1"},
            detection_count=0,
        )
    assert published == []
