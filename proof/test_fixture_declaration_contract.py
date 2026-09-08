"""Structural constraints for every scenario's witness declarations.

Expectations derive from the fixture and configured chair bindings rather than
from enumerated scenarios. Declared names must resolve; each adapter must parse
its own chair's response; reported geometry must lie inside and reach the named
act; minted fallback regions must remain geometry-free; and one attempt must
retain one reading with the same response shape as its base declaration.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from common import native_witness
from common.chairs.config import load_models_toml
from common.chairs.models import ChairIdentity
from common.contracts.errors import SchemaRefusal
from common.stage import fallback_page_act_key

PROOF_ROOT = Path(__file__).resolve().parent
ROOT = PROOF_ROOT.parent
STAGE = ROOT / "pipeline" / "3_attestatores"
MODELS_CONFIG = ROOT / "config" / "models.toml"

# The declaration tables that name one chair's response to one act. Each carries
# the same (scenario?, act_key, chair) identity; they differ only in what they
# say that response was.
RESPONSE_TABLES = ("testimony", "witness_empty", "witness_failure", "witness_not_run")
# Every geometry channel is refused over minted regions; naming all channels
# prevents a new spelling from bypassing that constraint.
GEOMETRY_BEARING_KEYS = ("blocks", "observed", "x", "y", "w", "h")


def _load_local_adapters():
    """Load the non-package stage registry with its sibling imports resolvable."""
    path = STAGE / "witness_adapters.py"
    spec = importlib.util.spec_from_file_location("attestatores_witness_adapters", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(STAGE))
    try:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
        sys.path.remove(str(STAGE))
    return module


@pytest.fixture(scope="module")
def skeleton() -> dict[str, Any]:
    with open(PROOF_ROOT / "skeleton_fixture.toml", "rb") as handle:
        return tomllib.load(handle)


@pytest.fixture(scope="module")
def adapters():
    return _load_local_adapters()


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


def declared_acts(skeleton: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {act["key"]: act for act in skeleton["act"]}


def minted_fallback_act_keys(skeleton: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The act keys the Designator mints, for pages the fixture leaves unmarked.

    Derived from the same helper the producer and verifier share so a rename
    cannot make every minted-region assertion pass by vacuity.
    """
    acts = declared_acts(skeleton)
    unmarked = {
        ordinal: page
        for ordinal, page in declared_pages(skeleton).items()
        if not any(act["page_ordinal"] == ordinal for act in acts.values())
    }
    return {fallback_page_act_key(ordinal): page for ordinal, page in unmarked.items()}


def page_for_act_key(skeleton: dict[str, Any], act_key: str) -> dict[str, Any]:
    """Resolve both declared and minted act keys to their sealed page."""
    acts = declared_acts(skeleton)
    pages = declared_pages(skeleton)
    if act_key in acts:
        return pages[acts[act_key]["page_ordinal"]]
    return minted_fallback_act_keys(skeleton)[act_key]


def response_rows(skeleton: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every declaration that says what one chair returned for one act."""
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


def base_rows_that_retain_a_response(skeleton: dict[str, Any]) -> set[tuple[str, str]]:
    """The (act, chair) pairs whose scenario-independent row retains raw bytes."""
    return {
        (row["act_key"], row["chair"])
        for row in skeleton["testimony"]
        if "scenario" not in row and "raw_response" in row
    }


def reported_geometry_declarations(
    skeleton: dict[str, Any],
    row: dict[str, Any],
    chairs: dict[str, ChairIdentity],
    adapters: Any,
) -> list[str]:
    """Which row fields actually declare witness-reported geometry."""
    present = [key for key in GEOMETRY_BEARING_KEYS if key in row]
    raw = row.get("raw_response")
    if raw is None:
        return present
    page = page_for_act_key(skeleton, row["act_key"])
    adapter = adapters.resolve_runnable_adapter(chairs[row["chair"]].witness_adapter)
    observed = adapter.observe(page_presentation(page), raw.encode("utf-8"))
    if any(item["bounds_source"] in {"native", "derived"} for item in observed):
        present.append("raw_response")
    return present


def test_every_declared_response_names_a_real_scenario_chair_and_act(skeleton, chairs):
    known_acts = set(declared_acts(skeleton)) | set(minted_fallback_act_keys(skeleton))
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
        if "act_key" in row:
            assert row["act_key"] in known_acts, f"{where} names an act nothing declares or mints"


def test_a_minted_fallback_row_names_a_scenario_its_page_takes_part_in(skeleton):
    """A response over a minted region only exists in the scenarios that page has.

    The ink-free page is scenario-restricted, so a declaration naming its
    fallback act from a scenario that never renders the page describes a witness
    reading nothing ever showed anyone.
    """
    fallback_pages = minted_fallback_act_keys(skeleton)
    for table, row in response_rows(skeleton):
        page = fallback_pages.get(row["act_key"])
        if page is None or "scenarios" not in page:
            continue
        assert row.get("scenario") in page["scenarios"], (
            f"{table} row {row!r} declares a response over page {page['ordinal']}, which that "
            "scenario never renders"
        )


def test_every_declared_response_is_readable_by_its_own_chairs_adapter(skeleton, chairs, adapters):
    """The adapter that will be asked to read this row can actually read it.

    Asked of the chair's *configured* adapter rather than of a named one: a
    response declared in Chandra's JSON for a chair bound to Churro is a
    declaration the stage will refuse at run time, and the reason it is wrong is
    the binding, not the bytes.

    And of the reader that adapter's *fixture* posture actually uses, where it
    has one. Chandra's live grammar is the vendor's HTML layout answer, while
    this fixture's rows declare its own `fixture-chandra-response.v1`
    placeholder -- retained history whose bytes are pinned into the fixture's
    digests until U16 re-declares them. `RunnableAdapter.fixture_parse` is what
    the registry says about that, so this asks the registry rather than the
    adapter's name (`pipeline/3_attestatores/witness_adapters.py`).
    """
    checked = 0
    for table, row in response_rows(skeleton):
        raw = row.get("raw_response")
        if raw is None:
            continue
        adapter = adapters.resolve_runnable_adapter(chairs[row["chair"]].witness_adapter)
        read = adapter.fixture_parse or adapter.parse
        # An adapter may refuse in either of its two vocabularies: a named parse
        # outcome or a `SchemaRefusal`. Both are the same finding here, and
        # letting the second escape would report a bare exception where this
        # module is supposed to name the declaration that caused it.
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
    for table, row in response_rows(skeleton):
        raw = row.get("raw_response")
        if raw is None:
            continue
        page = page_for_act_key(skeleton, row["act_key"])
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


def test_every_declared_response_geometry_reaches_the_act_it_is_declared_for(
    skeleton, chairs, adapters
):
    """A page response must overlap the sealed act it claims to report."""
    acts = declared_acts(skeleton)
    checked = 0
    for table, row in response_rows(skeleton):
        raw = row.get("raw_response")
        if raw is None or row["act_key"] not in acts:
            # Minted regions have no marked-out crop; their separate wall
            # forbids reported geometry entirely.
            continue
        act = acts[row["act_key"]]
        page = page_for_act_key(skeleton, row["act_key"])
        crop = {key: act[key] for key in ("x", "y", "w", "h")}
        adapter = adapters.resolve_runnable_adapter(chairs[row["chair"]].witness_adapter)
        observed = adapter.observe(page_presentation(page), raw.encode("utf-8"))
        assert any(overlaps(item["bounds"], crop) for item in observed), (
            f"{table} row {row!r} derives {[item['bounds'] for item in observed]}, none of which "
            f"overlaps act {row['act_key']}'s own crop {crop}"
        )
        checked += 1
    # The counter its siblings already carry. Both `continue` branches above are
    # ordinary, so a fixture edit that left no row with both a `raw_response`
    # and a declared act would have retired this wall in silence -- and the rule
    # it holds is the one keeping a witness reading attached to the act it read.
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
    `observe` never produces any. Exactly one row does, and it is load-bearing:
    `attestator_3` is the Churro chair, whose `HistoricalDocument` grammar has
    no coordinate vocabulary (`can_express_layout` false, no quantization rule,
    no `takes_page_size`, and an `observe` that returns a `bounds_source
    "presented"` echo routing and coverage exclude). That declared box is what
    attaches this chair offline, so the fixture happy scenario counts three
    witnesses of a floor of three and reaches a delivered export.

    U12 has since landed: the live seam over the same chair now also reaches
    a delivered export with three of three
    (`pipeline/test_live_reading_seam_e2e.py`), but through the Perlector's
    own `anchor-line` derivation rather than a reported page-geometry box --
    this chair's grammar carries none live, so no live response could ever
    produce the declared row this test names. `proof/build_fixture.py` carries
    the fuller account. What this test refuses is the silence: the moment a
    second chair is given a declared box its adapter says it cannot express,
    this list is wrong and says so by name, rather than a fixture quietly
    asking a live capability of a chair that has none.
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


def test_a_page_scoped_chairs_declared_observation_fits_its_page_exactly(
    skeleton, chairs, adapters
):
    """A declared row that overshoots is moved out of the act view, not refused.

    The split runs for a chair whose adapter reports geometry normalized against
    the whole page (`takes_page_size`), which today is Chandra alone: Churro's
    `HistoricalDocument` grammar carries no coordinate vocabulary, so its
    adapter declares neither a quantization rule nor a page size, and its
    declared `[[native_observation]]` row reaches the act view untouched -- held
    only by the sibling containment test above, exactly as it was before Unit 12
    asked that chair for rectangles.

    For the rows that do pass through the split, the fit is pinned rather than
    assumed: an overshooting row would be moved out of the act view's `observed`
    list instead of refused, which changes the published tree rather than
    failing. The sibling test permits any row inside the page; this one pins the
    exact edge arithmetic with the one-pixel counterfactual beside it, so that
    widening a committed row by a pixel fails here by name instead of moving
    `HAPPY_RUN_TREE_DIGEST` in silence.
    """
    pages = declared_pages(skeleton)
    clearances = []
    for row in skeleton.get("native_observation", []):
        chair = chairs[row["chair"]]
        if not adapters.resolve_runnable_adapter(chair.witness_adapter).takes_page_size:
            continue
        page = pages[row["page_ordinal"]]
        page_size = (page["width"], page["height"])
        observed = [
            {
                "ordinal": 0,
                "bounds": {key: row[key] for key in ("x", "y", "w", "h")},
                "bounds_source": "native",
                "span": None,
            }
        ]
        observed_snapshot = copy.deepcopy(observed)
        survivors, overshoots = native_witness.split_page_edge_overshoots(
            observed, page_size=page_size
        )
        assert (observed, survivors, overshoots) == (observed_snapshot, observed_snapshot, []), (
            f"declared observation {row!r} no longer survives page {page['ordinal']}'s edge split"
        )
        spare_x = page["width"] - (row["x"] + row["w"])
        spare_y = page["height"] - (row["y"] + row["h"])
        clearances.append(
            (row.get("scenario"), row["chair"], row["page_ordinal"], spare_x, spare_y)
        )
        # The counterfactual, one pixel past this row's own far edge: the box is
        # split out of the act view rather than refused, which is the failure
        # this pin exists to make visible instead of silent. Both edges, because
        # a page has two of them: an implementation that measured only the far x
        # edge would pass the first case and let a box hanging off the bottom of
        # the page into the act view, which is the same silent loss on the other
        # axis. Neither edge is the one this fixture's rows are near -- the flush
        # row has `spare_x == 0` -- so the pair is what says the check is about
        # the page, not about this corpus.
        for axis, spare in (("w", spare_x), ("h", spare_y)):
            over = [
                {**observed[0], "bounds": {**observed[0]["bounds"], axis: row[axis] + spare + 1}}
            ]
            kept, rejected = native_witness.split_page_edge_overshoots(over, page_size=page_size)
            assert kept == [], axis
            assert [item["bounds"] for item in rejected] == [over[0]["bounds"]], axis
    # Every such row's exact distance to its page's far edges, written out. A
    # one-pixel edit to any of them fails here, naming the row, rather than only
    # as a moved whole-tree digest nobody can attribute. Churro's own default
    # row is deliberately absent from this list: its adapter reports no geometry
    # and takes no page size, so nothing splits it, and the sibling containment
    # test is what holds it inside its page.
    assert sorted(clearances, key=repr) == [
        ("coverage-recovery", "attestator_1", 1, 190, 20),
        ("review", "attestator_1", 1, 190, 20),
    ]
    assert clearances, "no declared row reached the split; this guard would pass vacuously"


def test_no_declaration_hands_a_minted_fallback_region_reported_geometry(
    skeleton, chairs, adapters
):
    """The retroactive-coverage wall, enforced at the declaration.

    A page the fixture leaves unmarked reaches the witnesses as a Designator
    fallback crop. Reported geometry over that crop — even an honest empty
    report — makes the Perlector's `witnessed_region_ids` find the recovery
    region wholly contained and mark it `witness_covered`, which is precisely the
    retrospective coverage a recovery region may never acquire. A chair may still
    *report* on it; what it may not do is arrive carrying a box.
    """
    minted = set(minted_fallback_act_keys(skeleton))
    assert minted, "no minted fallback act was found; this guard would pass vacuously"
    for table, row in response_rows(skeleton):
        if row["act_key"] not in minted:
            continue
        present = reported_geometry_declarations(skeleton, row, chairs, adapters)
        assert not present, (
            f"{table} row {row!r} declares geometry ({present}) over minted region "
            f"{row['act_key']}; a recovery crop stays visibly under-witnessed"
        )


def test_a_geometry_free_raw_response_is_not_mislabeled_as_reported_geometry(
    skeleton, chairs, adapters
):
    """Retained bytes are custody; only their adapter can say they contain boxes."""
    row = {
        "scenario": "ink-free-page",
        "act_key": "page-fallback:3",
        "chair": "attestator_2",
        "raw_response": "<output></output>",
    }
    assert reported_geometry_declarations(skeleton, row, chairs, adapters) == []


def test_a_minted_fallback_region_is_still_allowed_a_response(skeleton):
    """The minted-region wall forbids reported geometry, not testimony."""
    minted = set(minted_fallback_act_keys(skeleton))
    declared = {row["act_key"] for _, row in response_rows(skeleton)}
    assert minted & declared, (
        "no minted fallback region has a declared response; blankness is proved by the "
        "witnesses and the Perlector, which only get a say if they are asked"
    )


def test_no_attempt_is_declared_twice(skeleton):
    """One (scenario, act, chair, ordinal) has at most one response declaration."""
    seen: dict[tuple[str | None, str, str, int], str] = {}
    for table, row in response_rows(skeleton):
        identity = (
            row.get("scenario"),
            row["act_key"],
            row["chair"],
            int(row.get("attempt_ordinal", 1)),
        )
        assert identity not in seen, (
            f"{table} redeclares attempt {identity}, already declared by {seen[identity]}"
        )
        seen[identity] = table


def test_a_retained_response_and_its_declared_payload_are_the_same_text(skeleton, chairs, adapters):
    """One reading per attempt (GOVERNANCE 5), stated where both halves exist.

    A row carries the payload the stage records and, for a native adapter, the
    raw bytes that payload was parsed out of. When a scenario rewrote one and not
    the other, the fixture declared two different readings for one attempt and
    the geometry belonged to neither.

    Read through the reader the fixture posture itself uses -- the registry's
    `fixture_parse` where an adapter has one -- for the reason the readability
    guard above gives: Chandra's live grammar is the vendor's HTML layout
    answer, and these rows declare its own placeholder.
    """
    checked = 0
    for row in skeleton["testimony"]:
        if "raw_response" not in row or not isinstance(row["payload"], str):
            continue
        adapter = adapters.resolve_runnable_adapter(chairs[row["chair"]].witness_adapter)
        read = adapter.fixture_parse or adapter.parse
        text = read(row["raw_response"].encode("utf-8"))
        assert text == row["payload"], (
            f"testimony row {row!r} retains {text!r} but declares payload {row['payload']!r}"
        )
        checked += 1
    for row in skeleton.get("witness_empty", []):
        if "raw_response" not in row:
            continue
        adapter = adapters.resolve_runnable_adapter(chairs[row["chair"]].witness_adapter)
        read = adapter.fixture_parse or adapter.parse
        text = read(row["raw_response"].encode("utf-8"))
        assert text == "", (
            f"witness_empty row {row!r} declares a completed empty response but retains {text!r}"
        )
        checked += 1
    assert checked, "no retained response was compared; this guard would pass vacuously"


def test_a_scenario_override_retains_a_response_wherever_its_base_row_does(skeleton):
    """A textual override cannot discard the native response shape of its base."""
    retaining = base_rows_that_retain_a_response(skeleton)
    assert retaining, "no base row retains a response; this guard would pass vacuously"
    for row in skeleton["testimony"]:
        if "scenario" not in row:
            continue
        if (row["act_key"], row["chair"]) not in retaining:
            continue
        if not isinstance(row["payload"], str):
            continue
        assert "raw_response" in row, (
            f"testimony row {row!r} overrides a chair whose base row retains a native response, "
            "with text of its own and no response to attach it through"
        )


def test_a_completed_empty_row_retains_a_response_wherever_its_base_row_does(skeleton):
    """A marked-out empty override retains the response shape of its base.

    A chair reporting a genuinely empty response for a *marked-out* act looked at
    that act's real crop and found nothing there, and the Recensor's blank
    corroboration needs that geometry as evidence the chair actually examined the
    region. Minted regions are excluded because they must remain under-witnessed.
    """
    retaining = base_rows_that_retain_a_response(skeleton)
    assert retaining, "no base row retains a response; this guard would pass vacuously"
    minted = set(minted_fallback_act_keys(skeleton))
    checked = 0
    for row in skeleton.get("witness_empty", []):
        if row["act_key"] in minted:
            continue
        if (row["act_key"], row["chair"]) not in retaining:
            continue
        assert "raw_response" in row, (
            f"witness_empty row {row!r} reports an empty reading of a marked-out act for a chair "
            "whose base row retains a native response, with no response to attach it through"
        )
        checked += 1
    # Both skips above can empty this loop while the declaration still contains
    # the rows the rule is about: drop `raw_response` from the base testimony and
    # every row is skipped, and the rule -- a chair reporting a blank over a
    # marked-out act must carry the geometry proving it examined that crop --
    # would stop being enforced with nothing turning red.
    assert checked, "no marked-out empty row was compared; this guard would pass vacuously"
