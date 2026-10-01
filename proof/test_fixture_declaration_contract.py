"""Structural constraints for every scenario's witness declarations.

Expectations derive from the fixture and configured chair bindings rather than
from enumerated scenarios. Declared names must resolve; each adapter must parse
its own chair's response; reported geometry must lie inside its page and reach
an act declared there; a page with no declared act stays geometry-free; and one
attempt must retain one reading with the same response shape as its base
declaration.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from common.chairs.config import load_models_toml
from common.chairs.models import ChairIdentity
from common.contracts.errors import SchemaRefusal
from conftest import load_stage

PROOF_ROOT = Path(__file__).resolve().parent
ROOT = PROOF_ROOT.parent
MODELS_CONFIG = ROOT / "config" / "models.toml"

# The declaration tables that name one chair's response to one whole page. Each
# carries the same (scenario?, page_ordinal, chair) identity; they differ only
# in what they say that response was.
RESPONSE_TABLES = (
    "testimony",
    "churro_page_response",
    "witness_empty",
    "witness_not_run",
    "witness_malformed",
)
# Every geometry channel is refused on a page with no declared act; naming all
# channels prevents a new spelling from bypassing that constraint.
GEOMETRY_BEARING_KEYS = ("blocks", "observed", "x", "y", "w", "h")


@pytest.fixture(scope="module")
def skeleton() -> dict[str, Any]:
    with open(PROOF_ROOT / "skeleton_fixture.toml", "rb") as handle:
        return tomllib.load(handle)


@pytest.fixture(scope="module")
def adapters():
    return load_stage("3_attestatores", "witness_adapters", isolate_path=True)


@pytest.fixture(scope="module")
def chairs() -> dict[str, ChairIdentity]:
    """The configured witness chairs, by role, with their bound adapter."""
    config = load_models_toml(MODELS_CONFIG)
    return {
        role: chair
        for role, chair in config.chairs.items()
        if role.startswith("attestator_") and isinstance(chair, ChairIdentity)
    }


# --- Derived views of the fixture's own structural claims ----------------------


def declared_pages(skeleton: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {page["ordinal"]: page for page in skeleton["page"]}


def acts_on_page(skeleton: dict[str, Any], ordinal: int) -> list[dict[str, Any]]:
    return [act for act in skeleton["act"] if act["page_ordinal"] == ordinal]


def pages_without_an_act(skeleton: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """The pages the fixture leaves unmarked: blank paper, with no act declared."""
    return {
        ordinal: page
        for ordinal, page in declared_pages(skeleton).items()
        if not acts_on_page(skeleton, ordinal)
    }


def response_rows(skeleton: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every declaration that says what one chair returned for one page."""
    return [(table, row) for table in RESPONSE_TABLES for row in skeleton.get(table, [])]


def page_presentation(page: dict[str, Any]) -> dict[str, Any]:
    """A whole-page presentation for the page a declared response was read on.

    Only the closed shape matters here: `observe` is being asked what geometry it
    would derive, not being handed a real run's blob. The digest is a placeholder
    and is never compared against anything.
    """
    return {
        "kind": "page",
        "source_page_id": f"page-{page['ordinal']}",
        "source_page_ordinal": page["ordinal"],
        "image_path": "1_exemplar/blobs/sha256/" + "0" * 64,
        "image_sha256": "0" * 64,
        "transform": {
            "operation": "whole",
            "source_page_id": f"page-{page['ordinal']}",
            "source_page_ordinal": page["ordinal"],
            "bounds": {"x": 0, "y": 0, "w": page["width"], "h": page["height"]},
        },
    }


def overlaps(left: dict[str, int], right: dict[str, int]) -> bool:
    return min(left["x"] + left["w"], right["x"] + right["w"]) > max(left["x"], right["x"]) and min(
        left["y"] + left["h"], right["y"] + right["h"]
    ) > max(left["y"], right["y"])


def retained_responses(skeleton: dict[str, Any]):
    """Every declared raw response, with the row and the page it was read on."""
    pages = declared_pages(skeleton)
    for table, row in response_rows(skeleton):
        for raw in row.get("raw_responses", []):
            yield table, row, pages[row["page_ordinal"]], raw


def reported_geometry_declarations(
    skeleton: dict[str, Any],
    row: dict[str, Any],
    chairs: dict[str, ChairIdentity],
    adapters: Any,
) -> list[str]:
    """Which row fields actually declare witness-reported geometry."""
    present = [key for key in GEOMETRY_BEARING_KEYS if key in row]
    page = declared_pages(skeleton)[row["page_ordinal"]]
    adapter = adapters.resolve_runnable_adapter(chairs[row["chair"]].witness_adapter)
    for raw in row.get("raw_responses", []):
        observed = adapter.observe(page_presentation(page), raw.encode("utf-8"))
        if any(item["bounds_source"] in {"native", "derived"} for item in observed):
            present.append("raw_responses")
            break
    return present


def test_every_declared_response_names_a_real_scenario_chair_and_page(skeleton, chairs):
    pages = declared_pages(skeleton)
    scenarios = {scenario["name"] for scenario in skeleton["scenario"]}
    rows = response_rows(skeleton) + [
        ("native_observation", row) for row in skeleton.get("native_observation", [])
    ]
    assert rows, "the fixture declares no witness responses at all"
    for table, row in rows:
        where = f"{table} row {row!r}"
        assert row["chair"] in chairs, f"{where} names a chair models.toml does not configure"
        if "scenario" in row:
            assert row["scenario"] in scenarios, f"{where} names an undeclared scenario"
        assert row["page_ordinal"] in pages, f"{where} names a page nothing declares"
        assert "act_key" not in row, f"{where} names an act; a witness answers a whole page"


def test_a_row_on_a_scenario_page_names_a_scenario_that_page_takes_part_in(skeleton):
    """A response on a scenario-restricted page only exists in the scenarios it has.

    A declaration naming the ink-free page from a scenario that never renders
    it describes a witness reading nothing ever showed anyone.
    """
    pages = declared_pages(skeleton)
    checked = 0
    for table, row in response_rows(skeleton):
        page = pages[row["page_ordinal"]]
        if "scenarios" not in page:
            continue
        assert row.get("scenario") in page["scenarios"], (
            f"{table} row {row!r} declares a response over page {page['ordinal']}, which that "
            "scenario never renders"
        )
        checked += 1
    assert checked, "no row on a scenario page was checked; this guard would pass vacuously"


def test_every_declared_response_is_readable_by_its_own_chairs_adapter(skeleton, chairs, adapters):
    """The adapter that will be asked to read this row can actually read it.

    Asked of the chair's *configured* adapter rather than of a named one: a
    response declared in Chandra's JSON for a chair bound to Churro is a
    declaration the stage will refuse at run time, and the reason it is wrong is
    the binding, not the bytes.

    And of the reader that adapter's *fixture* posture actually uses, where it
    has one. Chandra's live grammar is the vendor's HTML layout answer, while
    this fixture's rows declare its own `fixture-chandra-response.v1`
    placeholder. `RunnableAdapter.fixture_parse` is what the registry says
    about that, so this asks the registry rather than the adapter's name
    (`pipeline/3_attestatores/witness_adapters.py`).
    """
    checked = 0
    for table, row, _page, raw in retained_responses(skeleton):
        adapter = adapters.resolve_runnable_adapter(chairs[row["chair"]].witness_adapter)
        read = adapter.fixture_parse or adapter.parse
        # An adapter may refuse in either of its two vocabularies: a named parse
        # outcome or a `SchemaRefusal`. Both are the same finding here.
        try:
            parsed: Any = read(raw.encode("utf-8"))
        except SchemaRefusal as refusal:
            parsed = refusal
        assert isinstance(parsed, str), (
            f"{table} row {row!r} declares a response its chair's configured adapter "
            f"({chairs[row['chair']].witness_adapter}) does not recognize: {parsed!r}"
        )
        checked += 1
    assert checked, "no declared raw response was checked; this guard would pass vacuously"


def test_every_declared_response_geometry_lies_inside_its_own_sealed_page(
    skeleton, chairs, adapters
):
    """Reject unfeedable geometry before it can hold an Attestatores tally."""
    checked = 0
    for table, row, page, raw in retained_responses(skeleton):
        adapter = adapters.resolve_runnable_adapter(chairs[row["chair"]].witness_adapter)
        observed = adapter.observe(page_presentation(page), raw.encode("utf-8"))
        assert observed, f"{table} row {row!r} retains a response that derives no geometry at all"
        for item in observed:
            bounds = item["bounds"]
            assert (
                bounds["x"] >= 0
                and bounds["y"] >= 0
                and bounds["x"] + bounds["w"] <= page["width"]
                and bounds["y"] + bounds["h"] <= page["height"]
            ), (
                f"{table} row {row!r} derives {bounds} on page {page['ordinal']} "
                f"({page['width']}x{page['height']}); the sealed page cannot hold it"
            )
            checked += 1
    assert checked, "no declared geometry was checked; this guard would pass vacuously"


def test_every_declared_response_geometry_reaches_an_act_on_its_page(skeleton, chairs, adapters):
    """Each declared placeholder overlaps an act the fixture declares on its page."""
    checked = 0
    for table, row, page, raw in retained_responses(skeleton):
        crops = [
            {key: act[key] for key in ("x", "y", "w", "h")}
            for act in acts_on_page(skeleton, page["ordinal"])
        ]
        adapter = adapters.resolve_runnable_adapter(chairs[row["chair"]].witness_adapter)
        observed = adapter.observe(page_presentation(page), raw.encode("utf-8"))
        assert any(overlaps(item["bounds"], crop) for item in observed for crop in crops), (
            f"{table} row {row!r} derives {[item['bounds'] for item in observed]}, none of which "
            f"overlaps an act declared on page {page['ordinal']}"
        )
        checked += 1
    assert checked, "no declared response geometry was checked; this guard would pass vacuously"


def test_every_declared_native_observation_lies_inside_its_own_sealed_page(skeleton):
    """The same wall for the fixture's directly declared observation boxes.

    These bypass every adapter — they are page geometry the fixture states
    outright — so they need the containment check stated separately rather than
    inherited from a `parse`/`observe` pair that never sees them.
    """
    pages = declared_pages(skeleton)
    rows = skeleton.get("native_observation", [])
    assert rows, "no declared native observation was checked; this guard would pass vacuously"
    for row in rows:
        page = pages[row["page_ordinal"]]
        assert (
            row["x"] >= 0
            and row["y"] >= 0
            and row["w"] > 0
            and row["h"] > 0
            and row["x"] + row["w"] <= page["width"]
            and row["y"] + row["h"] <= page["height"]
        ), f"native_observation {row!r} falls outside page {page['ordinal']}"


def test_the_declared_rows_no_live_chair_could_produce_are_named_here(skeleton, chairs, adapters):
    """The offline posture may declare geometry; it may not do so unnoticed.

    A `[[native_observation]]` row bypasses the adapter entirely --
    `run.py::_fixture_native_observations` publishes it as `bounds_source
    "native"` without asking whether the chair's adapter could have reported a
    box at all -- so the fixture can state page geometry for a chair whose live
    `observe` never produces any. Exactly one row does: `attestator_3` is the
    Churro chair, whose `HistoricalDocument` grammar has no coordinate
    vocabulary (`can_express_layout` false, no quantization rule, no
    `takes_page_size`, and an `observe` that returns a `bounds_source
    "presented"` echo routing and coverage exclude). What this test refuses is
    the silence: the moment a second chair is given a declared box its adapter
    says it cannot express, this list is wrong and says so by name.
    """
    incapable = sorted(
        {
            row["chair"]
            for row in skeleton.get("native_observation", [])
            if not adapters.resolve_runnable_adapter(
                chairs[row["chair"]].witness_adapter
            ).format_capabilities["can_express_layout"]
        }
    )
    assert incapable == ["attestator_3"], incapable


def test_no_declaration_hands_a_page_without_an_act_reported_geometry(skeleton, chairs, adapters):
    """A page the fixture leaves unmarked is blank paper; a box there would claim ink.

    A chair may still *report* on it; what it may not do is arrive carrying a box.
    """
    blank = set(pages_without_an_act(skeleton))
    assert blank, "no page without an act was found; this guard would pass vacuously"
    for table, row in response_rows(skeleton):
        if row["page_ordinal"] not in blank:
            continue
        present = reported_geometry_declarations(skeleton, row, chairs, adapters)
        assert not present, (
            f"{table} row {row!r} declares geometry ({present}) on page {row['page_ordinal']}, "
            "which carries no ink"
        )


def test_a_geometry_free_raw_response_is_not_mislabeled_as_reported_geometry(
    skeleton, chairs, adapters
):
    """Retained bytes are custody; only their adapter can say they contain boxes."""
    row = {
        "scenario": "ink-free-page",
        "page_ordinal": 3,
        "chair": "attestator_2",
        "raw_responses": ["<output></output>"],
    }
    assert reported_geometry_declarations(skeleton, row, chairs, adapters) == []


def test_a_page_without_an_act_is_still_allowed_a_response(skeleton):
    """The blank-paper wall forbids reported geometry, not testimony."""
    blank = set(pages_without_an_act(skeleton))
    declared = {row["page_ordinal"] for _, row in response_rows(skeleton)}
    assert blank & declared, (
        "no page without an act has a declared response; blankness is proved by the "
        "witnesses and the Perlector, which only get a say if they are asked"
    )


def test_no_attempt_is_declared_twice(skeleton):
    """One (scenario, page, chair, ordinal) has at most one response declaration."""
    seen: dict[tuple[str | None, int, str, int], str] = {}
    for table, row in response_rows(skeleton):
        identity = (
            row.get("scenario"),
            row["page_ordinal"],
            row["chair"],
            int(row.get("attempt_ordinal", 1)),
        )
        assert identity not in seen, (
            f"{table} redeclares attempt {identity}, already declared by {seen[identity]}"
        )
        seen[identity] = table


def test_a_retained_response_and_its_declared_payload_are_the_same_text(skeleton, chairs, adapters):
    """One reading per attempt, stated where both halves exist.

    A row carries the page text the stage records and the raw placeholders that
    text was parsed out of. When a scenario rewrote one and not the other, the
    fixture declared two different readings for one attempt and the geometry
    belonged to neither. Read through the registry's `fixture_parse`, for the
    reason the readability guard above gives.
    """
    checked = 0
    for row in skeleton["testimony"]:
        if "raw_responses" not in row:
            continue
        adapter = adapters.resolve_runnable_adapter(chairs[row["chair"]].witness_adapter)
        read = adapter.fixture_parse or adapter.parse
        texts = [read(raw.encode("utf-8")) for raw in row["raw_responses"]]
        joined = "\n".join(text for text in texts if text)
        assert joined == row["payload"], (
            f"testimony row {row!r} retains {joined!r} but declares payload {row['payload']!r}"
        )
        checked += 1
    assert checked, "no retained response was compared; this guard would pass vacuously"


def test_a_scenario_override_retains_a_response_wherever_its_base_row_does(skeleton):
    """A textual override cannot discard the native response shape of its base."""
    retaining = {
        (row["page_ordinal"], row["chair"])
        for row in skeleton["testimony"]
        if "scenario" not in row and "raw_responses" in row
    }
    assert retaining, "no base row retains a response; this guard would pass vacuously"
    for row in skeleton["testimony"]:
        if "scenario" not in row or (row["page_ordinal"], row["chair"]) not in retaining:
            continue
        assert "raw_responses" in row, (
            f"testimony row {row!r} overrides a chair whose base row retains a native response, "
            "with text of its own and no response to attach it through"
        )
