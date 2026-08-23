"""The checked-in fixture bytes, and the two declarations that describe them.

Meta-invariant #91 — drift checks over agreement surfaces: wherever two files must
agree, a test reads BOTH from source and fails on divergence. Three surfaces have
to agree here, and each has drifted in some project before: the rendered pixels,
the ingress declaration the hook enforces, and the pipeline's fixture declaration
that the stage programs read as data.

The pixel comparison rather than a byte comparison is deliberate. zlib's output is
a pure function of its input for a given build, but it is not guaranteed identical
across zlib implementations, so asserting checked-in bytes equal freshly-compressed
bytes would fail on a machine that has done nothing wrong. What must hold
everywhere is that the checked-in files decode to exactly the image this generator
describes — and that is asserted instead.
"""

import hashlib
import tomllib
from pathlib import Path

import pytest

from common.chairs.config import load_models_toml
from common.chairs.models import ChairIdentity
from common.contracts.outcomes import OutcomeClass, classify
from common.contracts.stages import PERLECTOR
from common.imaging import decode_grayscale_png
from proof.build_fixture import (
    ACTS,
    CHURRO_PAGE_RESPONSES,
    RECOVERY_BOUNDS,
    SCENARIO_TESTIMONY,
    TESTIMONY,
    WITNESS_FAILURES,
    WITNESS_MALFORMED,
    WITNESS_NOT_RUN,
    act_descriptor,
    build_ingress_manifest,
    build_skeleton_fixture,
    render_all,
    toml_value,
)
from proof.synthetic_pages import ALL_PAGES, FIXTURE_ID, PAGES, render_page

PROOF_ROOT = Path(__file__).resolve().parent
MODELS_CONFIG = PROOF_ROOT.parent / "config" / "models.toml"


def load(name: str) -> dict:
    with open(PROOF_ROOT / name, "rb") as handle:
        return tomllib.load(handle)


@pytest.fixture(scope="module")
def ingress():
    return load("fixtures.toml")


@pytest.fixture(scope="module")
def skeleton():
    return load("skeleton_fixture.toml")


@pytest.fixture(scope="module")
def models_config():
    return load_models_toml(MODELS_CONFIG)


def configured_witness_chairs(models_config: dict) -> tuple[str, ...]:
    return tuple(
        sorted(
            role
            for role, chair in models_config.chairs.items()
            if role.startswith("attestator_") and isinstance(chair, ChairIdentity)
        )
    )


# --- The bytes are really there, and are really what is declared ---------------


def test_every_declared_fixture_file_exists_with_the_declared_digest(ingress):
    entries = ingress["fixture"]
    assert len(entries) == 3
    for entry in entries:
        path = PROOF_ROOT.parent / entry["path"]
        assert path.exists(), f"{entry['path']} is declared but not present"
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert len(data) == entry["bytes"]
        assert entry["media_type"] == "image/png"
        assert data.startswith(b"\x89PNG\r\n\x1a\n")


def test_the_checked_in_bytes_decode_to_exactly_the_declared_image():
    """The property that holds on every machine, whatever its zlib."""
    checked = 0
    for page in ALL_PAGES:
        stored = (PROOF_ROOT / "fixtures" / FIXTURE_ID / f"page-{page['ordinal']}.png").read_bytes()
        width, height, rows = decode_grayscale_png(stored)
        _, _, expected_rows = decode_grayscale_png(render_page(page))

        assert (width, height) == (page["width"], page["height"])
        assert rows == expected_rows
        checked += 1
    assert checked == 3


def test_no_fixture_file_is_present_that_nothing_declares(ingress):
    """An undeclared image in the proof tree is exactly what the ingress hook
    exists to catch; failing here first says so with a better message."""
    declared = {PROOF_ROOT.parent / entry["path"] for entry in ingress["fixture"]}
    present = set((PROOF_ROOT / "fixtures").rglob("*.png"))
    assert present == declared


# --- The declarations are regenerable, so a stale one is visible ---------------


def test_the_ingress_declaration_is_up_to_date(ingress):
    assert build_ingress_manifest(render_all()) == (PROOF_ROOT / "fixtures.toml").read_text(
        encoding="utf-8"
    )


def test_the_skeleton_declaration_is_up_to_date(skeleton):
    assert build_skeleton_fixture(render_all()) == (PROOF_ROOT / "skeleton_fixture.toml").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("key", ("nested.key", "spaced key"))
def test_inline_toml_objects_refuse_keys_that_are_not_bare_safe(key):
    with pytest.raises(ValueError, match="object keys must be bare-safe"):
        toml_value({key: "value"})


# --- The pipeline's declaration agrees with the rendered geometry --------------


def test_declared_pages_match_the_rendered_pages(skeleton):
    assert len(skeleton["page"]) == 3
    for declared in skeleton["page"]:
        page = next(item for item in ALL_PAGES if item["ordinal"] == declared["ordinal"])
        assert declared["width"] == page["width"]
        assert declared["height"] == page["height"]
        stored = PROOF_ROOT / declared["path"]
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == declared["sha256"]


def test_the_ink_free_page_is_restricted_to_its_integration_scenarios(skeleton):
    blank = next(page for page in skeleton["page"] if page["ordinal"] == 3)
    # Two scenarios, the same page: one declares an empty witness response per
    # chair for the minted fallback act and completes as a proved blank; the
    # other declares none and must hold instead (Sol-S1).
    assert blank["scenarios"] == ["ink-free-page", "ink-free-page-unwitnessed"]
    # Both scenarios declare no recovery and no scenario-level holds: the
    # unwitnessed act holds through the WITNESS shortfall alone, so a stray
    # declaration here would let the red demonstration pass for the wrong
    # reason.
    for name in ("ink-free-page", "ink-free-page-unwitnessed"):
        scenario = next(row for row in skeleton["scenario"] if row["name"] == name)
        assert scenario["recover_acts"] == []
        assert scenario["hold_acts"] == []
    source = next(page for page in ALL_PAGES if page["ordinal"] == 3)
    assert source["acts"] == ()
    _, _, rows = decode_grayscale_png(render_page(source))
    assert {value for row in rows for value in row} == {rows[0][0]}, (
        "page 3 must be uniform paper: the ink-free scenario proves nothing otherwise"
    )


def test_declared_acts_match_the_rendered_act_bounds(skeleton):
    assert len(skeleton["act"]) == len(ACTS) == 2
    for declared in skeleton["act"]:
        source = act_descriptor(declared["page_ordinal"], declared["proposal_ordinal"])
        assert {key: declared[key] for key in ("x", "y", "w", "h")} == source["bounds"]
        assert declared["text"].startswith("SYNTHETIC ACT")


def test_there_are_two_acts_and_exactly_one_cross_page_continuation(skeleton):
    """Spec 01: two acts, one cross-page continuation. The continuation is a
    region of an existing act, never a third act — an act that gained an identity
    by turning a page would break "act identity survives recropping" at the one
    place it is hardest to notice."""
    assert len(skeleton["act"]) == 2
    assert len(skeleton["continuation"]) == 1
    continuation = skeleton["continuation"][0]
    assert continuation["act_key"] in {act["key"] for act in ACTS}
    assert continuation["page_ordinal"] == 2


def test_every_recovery_region_stays_on_the_page_and_clear_of_the_other_act(skeleton):
    """Both declared recrops widen into empty margin, never into a neighbour.

    One rectangle per act, and each is checked against every OTHER act's
    declared bounds rather than against one hard-coded neighbour: a recrop that
    reached into the act next to it would be inventing an overlap rather than
    widening a crop, and the Designator would answer with a coverage refusal
    that named the wrong defect.
    """
    assert len(skeleton["recovery"]) == len(ACTS) == 2
    page = next(item for item in PAGES if item["ordinal"] == 1)
    for recovery in skeleton["recovery"]:
        assert recovery["x"] >= 0 and recovery["y"] >= 0
        assert recovery["x"] + recovery["w"] <= page["width"]
        assert recovery["y"] + recovery["h"] <= page["height"]

        for act in ACTS:
            if act["key"] == recovery["act_key"]:
                continue
            other = act_descriptor(act["page_ordinal"], act["proposal_ordinal"])["bounds"]
            disjoint = (
                recovery["y"] + recovery["h"] <= other["y"]
                or other["y"] + other["h"] <= recovery["y"]
                or recovery["x"] + recovery["w"] <= other["x"]
                or other["x"] + other["w"] <= recovery["x"]
            )
            assert disjoint, f"the {recovery['act_key']} recrop reaches into act {act['key']}"


def test_every_recovery_region_differs_from_its_own_original_proposal(skeleton):
    declared = {
        row["act_key"]: {key: row[key] for key in ("x", "y", "w", "h")}
        for row in skeleton["recovery"]
    }
    assert declared == RECOVERY_BOUNDS
    for act in ACTS:
        original = act_descriptor(act["page_ordinal"], act["proposal_ordinal"])["bounds"]
        assert declared[act["key"]] != original


# --- Witness declarations leave no silent gap ----------------------------------


def test_every_chair_has_testimony_declared_for_every_act(skeleton, models_config):
    """A chair with no declared testimony would silently become an absence the
    fixture never meant to describe."""
    declared = {(row["act_key"], row["chair"]) for row in skeleton["testimony"]}
    chairs = configured_witness_chairs(models_config)
    expected = {(act["key"], chair) for act in ACTS for chair in chairs}
    assert declared == expected
    assert len(declared) == 6


def test_models_config_owns_the_live_chairs_floor_and_recipes(skeleton, models_config):
    """Fixture data does not decide which model chairs a run invokes."""
    assert "witness_chairs" not in skeleton
    assert "witness_floor" not in skeleton
    assert "adapter_recipes" not in skeleton
    assert models_config.witness_floor == 3
    assert configured_witness_chairs(models_config) == (
        "attestator_1",
        "attestator_2",
        "attestator_3",
    )
    assert set(models_config.adapter_recipes) == {
        "door",
        "exemplar",
        "designator",
        "attestatores",
        "perlector",
        "recensor",
        "archetypus",
        "armarium",
    }


def test_testimony_differs_from_the_established_text_somewhere(skeleton):
    """Dissent must be exercisable. If every witness agreed with the reading
    everywhere, the Perlectio's dissent record would be structurally untested."""
    texts = {act["key"]: act["text"] for act in ACTS}
    disagreeing = [
        row
        for row in skeleton["testimony"]
        if "scenario" not in row and row["payload"] != texts[row["act_key"]]
    ]
    assert len(disagreeing) == 4


def test_fixture_testimonia_declare_native_payloads_not_the_retired_body_field(skeleton):
    assert all("payload" in row and "reported" not in row for row in skeleton["testimony"])
    scenario_rows = [row for row in skeleton["testimony"] if "scenario" in row]
    assert scenario_rows == list(SCENARIO_TESTIMONY)


def test_the_review_scenario_exercises_the_repaired_failed_state(skeleton, models_config):
    """Validate the fixture's declared `failed` outcomes and configured chairs.

    `test_the_failed_chair_is_visible_in_the_export` in the orchestrator acceptance
    suite carries the end-to-end half by driving `failed` into the export.
    """
    failures = skeleton["witness_failure"]
    assert failures == list(WITNESS_FAILURES)
    assert {failure["scenario"] for failure in failures} == {"review", "reread-failure"}
    assert all(failure["chair"] in configured_witness_chairs(models_config) for failure in failures)
    assert failures[1]["attempt_ordinal"] == 2


def test_the_declared_churro_page_responses_reach_a_page_scoped_chair(skeleton, models_config):
    """Every declared full-page response names a chair configured to be asked one.

    `pipeline/3_attestatores/run.py::churro_page_capture` looks a response up by
    `(page_ordinal, chair)`. A row naming an act-scoped chair, a chair that does
    not exist, or a page that does not exist is not an error there -- it is never
    found, and the whole capture path silently reverts to the synthetic act join
    while every test stays green. The stage refuses such a row by name at run
    time; this refuses it at the generator, where the declaration is written.
    """
    page_chairs = {
        role
        for role, chair in models_config.chairs.items()
        if isinstance(chair, ChairIdentity) and chair.witness_scope == "page"
    }
    assert page_chairs, "the configuration seals no page witness at all"
    declared_pages = {page["ordinal"] for page in skeleton["page"]}
    rows = skeleton["churro_page_response"]
    assert rows == [dict(row) for row in CHURRO_PAGE_RESPONSES]
    assert rows, "no scenario exercises the Churro page capture path"
    for row in rows:
        assert set(row) == {
            "scenario",
            "page_ordinal",
            "chair",
            "raw_xml",
            "transport_stop_reason",
        }
        assert row["chair"] in page_chairs
        assert row["page_ordinal"] in declared_pages
        assert row["transport_stop_reason"]
    keys = [(row["scenario"], row["page_ordinal"], row["chair"]) for row in rows]
    assert len(set(keys)) == len(keys), "two responses declared for one (scenario, page, chair)"


def test_the_pinned_reference_run_exercises_the_churro_capture_path(skeleton):
    """`happy` is pinned, so a capture path it does not run can rot unnoticed.

    Its four rows also reproduce the previous synthetic join text exactly: the
    boundary becomes real without moving a single act's reading, which is what
    keeps this a change of path rather than a change of evidence.
    """
    happy = [row for row in skeleton["churro_page_response"] if row["scenario"] == "happy"]
    assert {(row["page_ordinal"], row["chair"]) for row in happy} == {
        (1, "attestator_1"),
        (1, "attestator_3"),
        (2, "attestator_1"),
        (2, "attestator_3"),
    }
    page_acts = {1: ("a1", "a2"), 2: ("a2",)}
    for row in happy:
        joined = "\n".join(
            TESTIMONY[act_key][row["chair"]] for act_key in page_acts[row["page_ordinal"]]
        )
        assert row["raw_xml"] == f"<output>{joined}</output>"
        assert row["transport_stop_reason"] == "eos"


def test_the_churro_native_scenario_reaches_all_three_parse_states(skeleton):
    """Parse-success, visible truncation, and a retained unparseable response.

    Reading them off the declaration is what makes the end-to-end assertions in
    `pipeline/3_attestatores/test_churro_native_capture.py` assertions about a
    scenario that genuinely contains all three, rather than about whichever
    states happen to survive an edit here.
    """
    rows = {
        (row["page_ordinal"], row["chair"]): row
        for row in skeleton["churro_page_response"]
        if row["scenario"] == "churro-native"
    }
    assert set(rows) == {
        (1, "attestator_1"),
        (1, "attestator_3"),
        (2, "attestator_1"),
        (2, "attestator_3"),
    }
    # Parse-success, complete, and carrying page furniture no act accounts for.
    header = "[FOLIO RUBRIC 7 -- page furniture, belongs to no entry]"
    complete = rows[(1, "attestator_1")]
    assert complete["raw_xml"].startswith(f"<output>{header}")
    assert complete["raw_xml"].endswith("</output>")
    assert complete["transport_stop_reason"] == "eos"
    assert header not in "".join(act["text"] for act in ACTS)
    # Parse-success cut off at the transport's own bound: kept, marked truncated.
    truncated = rows[(2, "attestator_1")]
    assert truncated["raw_xml"].endswith("</output>")
    assert truncated["transport_stop_reason"] == "length"
    # Never closed: retained raw, refused by the XML validator.
    malformed = rows[(2, "attestator_3")]
    assert not malformed["raw_xml"].endswith("</output>")
    assert malformed["transport_stop_reason"] == "length"


def test_fixture_declares_the_explicit_non_reading_and_malformed_attempts(skeleton):
    assert skeleton["witness_not_run"] == list(WITNESS_NOT_RUN)
    assert skeleton["witness_malformed"] == list(WITNESS_MALFORMED)


def test_the_scenarios_are_exactly_the_declared_ones(skeleton):
    names = [scenario["name"] for scenario in skeleton["scenario"]]
    assert names == [
        "happy",
        "witness-capabilities",
        "review",
        "continuation-recovery",
        "coverage-recovery",
        "churro-native",
        "audit-change",
        "refused-page",
        "refused-first-page",
        "truncated-reading",
        "genuinely-empty-witness",
        "confirmed-blank",
        "blank-with-dissent",
        "engine-truncated-reading",
        "no-readable-text-reading",
        "structure-failure",
        "ink-free-page",
        "ink-free-page-unwitnessed",
        "reread-failure",
        "reread-success",
        "not-run-witness",
        "malformed-witness",
        "structured-witness",
        "malformed-capabilities",
    ]
    by_name = {scenario["name"]: scenario for scenario in skeleton["scenario"]}
    assert by_name["happy"]["recover_acts"] == []
    assert by_name["happy"]["hold_acts"] == []
    assert by_name["witness-capabilities"]["recover_acts"] == []
    assert by_name["witness-capabilities"]["hold_acts"] == []
    assert by_name["review"]["recover_acts"] == ["a1"]
    assert by_name["review"]["hold_acts"] == ["a2"]
    # The recrop subject here is a2 -- the act that runs across the page break.
    # `review` only ever recrops a1, which lives on one page, so without this
    # scenario nothing exercised an expanded crop on an act whose evidence
    # spans two pages.
    assert by_name["continuation-recovery"]["recover_acts"] == ["a2"]
    assert by_name["continuation-recovery"]["hold_acts"] == []
    # coverage-recovery declares neither, on purpose: it is the one scenario in
    # which a recovery request or a hold can have come from nowhere but the
    # page witness's own unclaimed observation. A declaration added here would
    # silently re-conflate the two origins
    # (`pipeline/5_recensor/test_coverage_recovery_origin.py`).
    assert by_name["coverage-recovery"]["recover_acts"] == []
    assert by_name["coverage-recovery"]["hold_acts"] == []
    # churro-native declares neither either: what it declares is the Churro page
    # responses themselves, so nothing but the native capture boundary separates
    # it from `happy` (`pipeline/3_attestatores/test_churro_native_capture.py`).
    assert by_name["churro-native"]["recover_acts"] == []
    assert by_name["churro-native"]["hold_acts"] == []
    assert [
        row for row in skeleton["native_observation"] if row.get("scenario") == "coverage-recovery"
    ] == [
        {
            "scenario": "coverage-recovery",
            "chair": "attestator_1",
            "page_ordinal": 1,
            "x": 0,
            "y": 200,
            "w": 10,
            "h": 40,
        }
    ]
    # The scenario's data, not only its presence in the name census: a wrong
    # recover/hold declaration or a missing re-proof row would leave the
    # audit-change path measuring nothing while this file stayed green.
    assert by_name["audit-change"]["recover_acts"] == []
    assert by_name["audit-change"]["hold_acts"] == []
    assert skeleton["audit_reproof"] == [
        {
            "scenario": "audit-change",
            "act_key": "a1",
            "text": "SYNTHETIC ACT ONE alpha beta gamma!",
        }
    ]
    assert by_name["refused-page"]["recover_acts"] == []
    assert by_name["refused-page"]["hold_acts"] == []
    assert by_name["refused-first-page"]["recover_acts"] == []
    assert by_name["refused-first-page"]["hold_acts"] == []
    # Nothing is held or recovered by configuration here: the hold this scenario
    # produces must come from the reading outcome itself, or it would prove
    # nothing about the guard.
    assert by_name["truncated-reading"]["recover_acts"] == []
    assert by_name["truncated-reading"]["hold_acts"] == []
    assert by_name["genuinely-empty-witness"]["recover_acts"] == []
    assert by_name["genuinely-empty-witness"]["hold_acts"] == []
    assert by_name["confirmed-blank"]["recover_acts"] == []
    assert by_name["confirmed-blank"]["hold_acts"] == []
    assert by_name["blank-with-dissent"]["recover_acts"] == []
    assert by_name["blank-with-dissent"]["hold_acts"] == []
    assert by_name["engine-truncated-reading"]["recover_acts"] == []
    assert by_name["engine-truncated-reading"]["hold_acts"] == []
    assert by_name["no-readable-text-reading"]["recover_acts"] == []
    assert by_name["no-readable-text-reading"]["hold_acts"] == []
    # Nothing is held by configuration here either: the hold must come from the
    # recorded structure failure, or the scenario would prove nothing.
    assert by_name["structure-failure"]["recover_acts"] == []
    assert by_name["structure-failure"]["hold_acts"] == []
    for name in (
        "reread-failure",
        "reread-success",
        "not-run-witness",
        "malformed-witness",
        "structured-witness",
        "malformed-capabilities",
    ):
        assert by_name[name]["recover_acts"] == []
        assert by_name[name]["hold_acts"] == []


def test_the_recorded_structure_failure_names_one_page_and_one_closed_reason(skeleton):
    """Spec 06 test 4's fixture: a page the structure chair could not mark out."""
    assert skeleton["structure_failure"] == [
        {
            "scenario": "structure-failure",
            "page_ordinal": 1,
            "reason_code": "recorded-fixture-structure-failure",
        }
    ]


def test_the_completed_empty_witness_is_declared_for_a_known_scenario_and_chair(
    skeleton, models_config
):
    rows = skeleton["witness_empty"]
    assert rows == [
        {
            "scenario": "genuinely-empty-witness",
            "act_key": "a1",
            "chair": "attestator_3",
        },
        # The minted fallback act over the ink-free page. These three rows are
        # what `ink-free-page` used to get for free from the act's identity,
        # with no response boundary consulted at all: three chairs recorded as
        # having independently read a page none of them was asked about
        # (Sol-S1). `ink-free-page-unwitnessed` is deliberately absent from
        # this table, and its act must therefore hold.
        {"scenario": "ink-free-page", "act_key": "page-fallback:3", "chair": "attestator_1"},
        {"scenario": "ink-free-page", "act_key": "page-fallback:3", "chair": "attestator_2"},
        {"scenario": "ink-free-page", "act_key": "page-fallback:3", "chair": "attestator_3"},
        # (Held below to exactly the configured roster, derived rather than
        # listed, so adding a fourth chair to models.toml turns this red.)
        # Every configured chair, so `confirmed-blank` has a genuine unanimous
        # absence for the Recensor's blank corroboration to confirm.
        {"scenario": "confirmed-blank", "act_key": "a1", "chair": "attestator_1"},
        {"scenario": "confirmed-blank", "act_key": "a1", "chair": "attestator_2"},
        {"scenario": "confirmed-blank", "act_key": "a1", "chair": "attestator_3"},
        # Two of three: the third dissents by reporting its ordinary declared
        # (non-empty) testimony instead.
        {"scenario": "blank-with-dissent", "act_key": "a1", "chair": "attestator_1"},
        {"scenario": "blank-with-dissent", "act_key": "a1", "chair": "attestator_2"},
    ]
    for row in rows:
        assert row["chair"] in configured_witness_chairs(models_config)
    # attestator_3 is absent from blank-with-dissent's witness_empty rows above,
    # which is what makes it the dissenting chair -- but absence alone would
    # equally describe a chair whose declared testimony was itself blank. The
    # dissent this scenario exists to exercise requires real, non-empty text.
    assert TESTIMONY["a1"]["attestator_3"].strip()

    # Derived, not listed: the fallback act's empty responses must cover
    # exactly the configured witness roster, one row per chair, so a roster
    # change turns this red instead of silently under-witnessing the blank.
    fallback_rows = [row for row in rows if row["scenario"] == "ink-free-page"]
    assert sorted(row["chair"] for row in fallback_rows) == sorted(
        configured_witness_chairs(models_config)
    )
    assert {row["act_key"] for row in fallback_rows} == {"page-fallback:3"}


def test_the_declared_reading_failure_outcomes_are_never_completed_class(skeleton):
    """Every declared reading-failure scenario drives a real hazard: a reading
    that did not succeed. A declaration that named a completed-class outcome
    would exercise nothing, whichever class it actually belongs to."""
    failures = skeleton["reading_failure"]
    assert len(failures) == 4
    for row in failures:
        assert row["act_key"] in {act["key"] for act in skeleton["act"]}
        assert classify(PERLECTOR, row["outcome"]) is not OutcomeClass.COMPLETED

    by_scenario = {row["scenario"]: row["outcome"] for row in failures}
    # The exact scenario-to-outcome mapping, before classifying it: a fixture
    # that quietly swapped which scenario carries which outcome could still
    # pass the classification asserts below by accident.
    assert by_scenario == {
        "truncated-reading": "truncated",
        "confirmed-blank": "no-readable-text",
        "blank-with-dissent": "no-readable-text",
        "no-readable-text-reading": "no-readable-text",
    }
    # `truncated` is FAILED-class and still carries text -- the hazard the
    # Archetypus's own guard (spec 09) exists to refuse.
    assert classify(PERLECTOR, by_scenario["truncated-reading"]) is OutcomeClass.FAILED
    # `no-readable-text` is UNRESOLVED-class: the Perlector's own direct claim
    # of absence, which the Recensor's blank confirmation may or may not be
    # able to corroborate depending on what the witnesses say.
    assert classify(PERLECTOR, by_scenario["confirmed-blank"]) is OutcomeClass.UNRESOLVED
    assert classify(PERLECTOR, by_scenario["blank-with-dissent"]) is OutcomeClass.UNRESOLVED
    # The sibling hazard: an act nothing could be read from at all is
    # unresolved, not failed -- G2's "held until proved," never a refusal.
    assert classify(PERLECTOR, by_scenario["no-readable-text-reading"]) is OutcomeClass.UNRESOLVED


def test_the_declared_stop_reason_is_the_length_signal_for_a_known_scenario(skeleton):
    """The one declared, fixture-only truncation signal
    (`pipeline/4_perlector/truncation.py`): a stand-in for a real engine's own
    stop-reason, authoritative for `truncated` when it says `length`."""
    rows = skeleton["stop_reason"]
    assert rows == [
        {"scenario": "engine-truncated-reading", "act_key": "a1", "stop_reason": "length"}
    ]
    scenario_names = {scenario["name"] for scenario in skeleton["scenario"]}
    for row in rows:
        assert row["scenario"] in scenario_names
        assert row["act_key"] in {act["key"] for act in skeleton["act"]}
        assert row["stop_reason"] in {"stop", "length"}
