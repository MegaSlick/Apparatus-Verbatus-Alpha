"""Every scenario's witness declarations, against that scenario's own structure.

Three defects in a row in this fixture shared one shape. A page witness's
declarations were hand-tuned for one scenario against an expectation that lived
only in a test written for a different scenario, and the mismatch surfaced
somewhere else entirely: as a blind dissent record, as an under-witnessed
`confirmed-blank`, and as a fabricated fallback response whose box overflowed the
sealed page. Each was repaired one row at a time, which left the *class* intact —
the next scenario added could repeat it, because nothing walked the declarations
as a whole and asked whether each one is structurally possible.

That is what this module does. It is deliberately **derived, not enumerated**:
every expectation below is computed from the fixture's own page, act, scenario
and chair tables and from `config/models.toml`, so a new scenario, a new chair,
or a new declared response is checked the moment it is added rather than when
somebody remembers to extend a literal list. `test_proof_fixture_build.py` keeps
the enumerated per-scenario assertions, which say what the fixture *is*; these
say what any fixture of this shape must satisfy to be feedable at all.

The five structural claims, and the defect each one closes:

1. **Names resolve.** Every declaring row's scenario, chair and act exist.
2. **Declared geometry is inside the sealed page.** A block that quantizes past
   the page edge is refused by `common/native_witness.py` at publication and
   takes the whole Attestatores pass to `UNKNOWN` — the ink-free-page fallback
   defect, caught here at declaration time instead.
3. **A region outside the observed partition stays geometry-free.** An act key
   the fixture never declared is a Designator-minted fallback over a page with
   no marked-out acts. A recovery crop may not become retroactively witness
   covered, so no declaration may hand one reported geometry.
4. **One attempt declares one reading.** A row's retained response text and its
   declared payload are the same text (GOVERNANCE 5), and no attempt is declared
   twice.
5. **A scenario override keeps its chair's declaration shape.** If a chair's base
   row for an act retains a native response, a scenario that overrides that row
   with text of its own retains one too — the exact omission that made a page
   witness's testimony unattachable and its dissent record blind.

Claims 2, 4 and 5 are checked through the chair's **own configured adapter**,
resolved from `config/models.toml`, never against a hard-coded adapter name: a
declaration is well formed exactly when the adapter that will be asked to read it
can read it. That is also what keeps this module honest for Units 12 and 13 —
whichever adapter a chair is bound to, its own `parse`/`observe` answer here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

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
# Every key a response row may use to declare reported geometry. Claim 3 refuses
# all of them over a minted region, so adding a second geometry channel later
# cannot quietly escape the wall by not being called `raw_response`.
GEOMETRY_BEARING_KEYS = ("raw_response", "blocks", "observed", "x", "y", "w", "h")


def _load_local_adapters():
    """The stage's own runnable registry, loaded the way its siblings load it."""
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


def declared_scenarios(skeleton: dict[str, Any]) -> set[str]:
    return {scenario["name"] for scenario in skeleton["scenario"]}


def minted_fallback_act_keys(skeleton: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The act keys the Designator mints, for pages the fixture leaves unmarked.

    Derived from the same helper the producer and the verifier share
    (`common/stage.py::fallback_page_act_key`), so a rename cannot leave this
    module quietly matching nothing and passing every claim below by vacuity.
    """
    acts = declared_acts(skeleton)
    unmarked = {
        ordinal: page
        for ordinal, page in declared_pages(skeleton).items()
        if not any(act["page_ordinal"] == ordinal for act in acts.values())
    }
    return {fallback_page_act_key(ordinal): page for ordinal, page in unmarked.items()}


def page_for_act_key(skeleton: dict[str, Any], act_key: str) -> dict[str, Any]:
    """The sealed page a declared response was read on, marked out or minted.

    Resolved for both act kinds rather than only the declared ones. Claim 3
    forbids geometry over a minted region, so today no minted row reaches the
    containment check — but a claim that would raise `KeyError` if it ever did is
    a guard that reports a crash where the fixture wants a finding, and it would
    stop reporting anything at all the moment claim 3 is relaxed.
    """
    acts = declared_acts(skeleton)
    pages = declared_pages(skeleton)
    if act_key in acts:
        return pages[acts[act_key]["page_ordinal"]]
    return minted_fallback_act_keys(skeleton)[act_key]


def response_rows(skeleton: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every declaration that says what one chair returned for one act."""
    return [(table, row) for table in RESPONSE_TABLES for row in skeleton.get(table, [])]


def row_identity(row: dict[str, Any]) -> tuple[str | None, str, str, int]:
    """The immutable attempt a row speaks for. Ordinal one unless it says."""
    return (
        row.get("scenario"),
        row["act_key"],
        row["chair"],
        int(row.get("attempt_ordinal", 1)),
    )


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


# --- Claim 1: every declaring row names something that exists ------------------


def test_every_declared_response_names_a_real_scenario_chair_and_act(skeleton, chairs):
    known_acts = set(declared_acts(skeleton)) | set(minted_fallback_act_keys(skeleton))
    scenarios = declared_scenarios(skeleton)
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


# --- Claim 2: declared geometry lands inside the sealed page -------------------


def test_every_declared_response_is_readable_by_its_own_chairs_adapter(skeleton, chairs, adapters):
    """The adapter that will be asked to read this row can actually read it.

    Asked of the chair's *configured* adapter rather than of a named one: a
    response declared in Chandra's JSON for a chair bound to Churro is a
    declaration the stage will refuse at run time, and the reason it is wrong is
    the binding, not the bytes.
    """
    checked = 0
    for table, row in response_rows(skeleton):
        raw = row.get("raw_response")
        if raw is None:
            continue
        adapter = adapters.resolve_runnable_adapter(chairs[row["chair"]].witness_adapter)
        # An adapter may refuse in either of its two vocabularies: a named parse
        # outcome or a `SchemaRefusal`. Both are the same finding here, and
        # letting the second escape would report a bare exception where this
        # module is supposed to name the declaration that caused it.
        try:
            parsed: Any = adapter.parse(raw.encode("utf-8"))
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
    """The defect that held a whole pass, caught one layer earlier.

    `common/native_witness.py` refuses an observed box outside the sealed page,
    and that refusal takes the Attestatores attempt tally to `UNKNOWN`, holding
    every act on the page. A fixture declaration that quantizes past the page
    edge is therefore not a bad test expectation; it is an unfeedable fixture,
    and it should fail here rather than four stages downstream.
    """
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
    """Geometry that misses its own act attaches to nothing.

    A page witness reaches an act only by overlap of its reported geometry
    against that act's sealed proposal. A declared response whose blocks miss the
    act they are declared under is testimony the join cannot carry — which is how
    `witness-capabilities` lost a chair's comparison view and `confirmed-blank`
    reached two attached witnesses out of three completed responses.
    """
    acts = declared_acts(skeleton)
    for table, row in response_rows(skeleton):
        raw = row.get("raw_response")
        if raw is None or row["act_key"] not in acts:
            # A minted region has no marked-out crop to reach. Claim 3 owns it,
            # and owns it more strictly than this claim would.
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


# --- Claim 3: a minted region stays visibly under-witnessed --------------------


def test_no_declaration_hands_a_minted_fallback_region_reported_geometry(skeleton):
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
        present = [key for key in GEOMETRY_BEARING_KEYS if key in row]
        assert not present, (
            f"{table} row {row!r} declares geometry ({present}) over minted region "
            f"{row['act_key']}; a recovery crop stays visibly under-witnessed"
        )


def test_a_minted_fallback_region_is_still_allowed_a_response(skeleton):
    """The wall above forbids geometry, never testimony.

    Stated as its own claim so a later tightening that deletes the fallback rows
    outright — reading the wall as "no witness may answer for this region" — is a
    red test rather than a quiet return to the state where three chairs were
    recorded as having read a page none of them was asked about.
    """
    minted = set(minted_fallback_act_keys(skeleton))
    declared = {row["act_key"] for _, row in response_rows(skeleton)}
    assert minted & declared, (
        "no minted fallback region has a declared response; blankness is proved by the "
        "witnesses and the Perlector, which only get a say if they are asked"
    )


# --- Claim 4: one attempt declares one reading --------------------------------


def test_no_attempt_is_declared_twice(skeleton):
    """One (scenario, act, chair, ordinal) has at most one declared response.

    The stage refuses a double declaration at run time, and only for the two
    tables that happen to collide there. Asked of every response table at once,
    this also catches the pair that never meet at run time — a `witness_empty`
    and a `witness_not_run` for the same attempt, which is a fixture that cannot
    say whether the chair was asked.
    """
    seen: dict[tuple[str | None, str, str, int], str] = {}
    for table, row in response_rows(skeleton):
        identity = row_identity(row)
        assert identity not in seen, (
            f"{table} redeclares attempt {identity}, already declared by {seen[identity]}"
        )
        seen[identity] = table


def test_a_retained_response_and_its_declared_payload_are_the_same_text(skeleton):
    """One reading per attempt (GOVERNANCE 5), stated where both halves exist.

    A row carries the payload the stage records and, for a native adapter, the
    raw bytes that payload was parsed out of. When a scenario rewrote one and not
    the other, the fixture declared two different readings for one attempt and
    the geometry belonged to neither.
    """
    checked = 0
    for row in skeleton["testimony"]:
        if "raw_response" not in row or not isinstance(row["payload"], str):
            continue
        retained = json.loads(row["raw_response"])
        text = retained.get("markdown", retained.get("text"))
        assert text == row["payload"], (
            f"testimony row {row!r} retains {text!r} but declares payload {row['payload']!r}"
        )
        checked += 1
    for row in skeleton.get("witness_empty", []):
        if "raw_response" not in row:
            continue
        retained = json.loads(row["raw_response"])
        text = retained.get("markdown", retained.get("text"))
        assert text == "", (
            f"witness_empty row {row!r} declares a completed empty response but retains {text!r}"
        )
        checked += 1
    assert checked, "no retained response was compared; this guard would pass vacuously"


# --- Claim 5: a scenario override keeps its chair's declaration shape ----------


def test_a_scenario_override_retains_a_response_wherever_its_base_row_does(skeleton):
    """The omission that made a page witness unattachable, stated as a rule.

    A scenario row replaces the base row for one (act, chair). If the base row
    retains native bytes and the override does not, the chair keeps its text and
    loses its geometry — it reports a reading the page join cannot attach to any
    act, and its dissent record goes blind. The exemption is derived rather than
    listed: an override whose payload is not text is a native-payload boundary
    case with no textual page read to retain, and `structured-witness` is exactly
    that.
    """
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
    """Claim 5 for the other completed outcome, minus the minted regions.

    A chair reporting a genuinely empty response for a *marked-out* act looked at
    that act's real crop and found nothing there, and the Recensor's blank
    corroboration needs that geometry as evidence the chair actually examined the
    region. Claim 3 has already excluded the minted regions, where the opposite
    holds.
    """
    retaining = base_rows_that_retain_a_response(skeleton)
    minted = set(minted_fallback_act_keys(skeleton))
    for row in skeleton.get("witness_empty", []):
        if row["act_key"] in minted:
            continue
        if (row["act_key"], row["chair"]) not in retaining:
            continue
        assert "raw_response" in row, (
            f"witness_empty row {row!r} reports an empty reading of a marked-out act for a chair "
            "whose base row retains a native response, with no response to attach it through"
        )
