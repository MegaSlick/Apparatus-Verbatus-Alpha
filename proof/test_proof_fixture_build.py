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
    RECOVERY_BOUNDS,
    TESTIMONY,
    act_descriptor,
    build_ingress_manifest,
    build_skeleton_fixture,
    render_all,
)
from proof.synthetic_pages import FIXTURE_ID, PAGES, render_page

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
    assert len(entries) == 2
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
    for page in PAGES:
        stored = (PROOF_ROOT / "fixtures" / FIXTURE_ID / f"page-{page['ordinal']}.png").read_bytes()
        width, height, rows = decode_grayscale_png(stored)
        _, _, expected_rows = decode_grayscale_png(render_page(page))

        assert (width, height) == (page["width"], page["height"])
        assert rows == expected_rows
        checked += 1
    assert checked == 2


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


# --- The pipeline's declaration agrees with the rendered geometry --------------


def test_declared_pages_match_the_rendered_pages(skeleton):
    assert len(skeleton["page"]) == 2
    for declared in skeleton["page"]:
        page = next(item for item in PAGES if item["ordinal"] == declared["ordinal"])
        assert declared["width"] == page["width"]
        assert declared["height"] == page["height"]
        stored = PROOF_ROOT / declared["path"]
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == declared["sha256"]


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


def test_the_recovery_region_stays_on_the_page_and_clear_of_the_other_act(skeleton):
    assert len(skeleton["recovery"]) == 1
    recovery = skeleton["recovery"][0]
    page = next(item for item in PAGES if item["ordinal"] == 1)
    assert recovery["x"] >= 0 and recovery["y"] >= 0
    assert recovery["x"] + recovery["w"] <= page["width"]
    assert recovery["y"] + recovery["h"] <= page["height"]

    other = act_descriptor(1, 1)["bounds"]
    assert recovery["y"] + recovery["h"] <= other["y"], (
        "an expanded recrop that reached into the neighbouring act would be "
        "inventing an overlap rather than widening a crop"
    )


def test_the_recovery_region_differs_from_the_original_proposal(skeleton):
    original = act_descriptor(1, 0)["bounds"]
    recovery = skeleton["recovery"][0]
    assert {key: recovery[key] for key in ("x", "y", "w", "h")} != original
    assert RECOVERY_BOUNDS["a1"] == {key: recovery[key] for key in ("x", "y", "w", "h")}


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
    disagreeing = [row for row in skeleton["testimony"] if row["reported"] != texts[row["act_key"]]]
    assert len(disagreeing) == 4


def test_the_review_scenario_exercises_the_repaired_failed_state(skeleton, models_config):
    """Sol B-2 / blocker 4: `failed` is a member of the witness vocabulary. The
    fixture drives it end to end rather than leaving it only unit-tested."""
    failures = skeleton["witness_failure"]
    assert len(failures) == 1
    assert failures[0]["scenario"] == "review"
    assert failures[0]["chair"] in configured_witness_chairs(models_config)


def test_the_scenarios_are_exactly_the_declared_ones(skeleton):
    names = [scenario["name"] for scenario in skeleton["scenario"]]
    assert names == [
        "happy",
        "review",
        "refused-page",
        "refused-first-page",
        "truncated-reading",
        "genuinely-empty-witness",
        "confirmed-blank",
        "blank-with-dissent",
    ]
    by_name = {scenario["name"]: scenario for scenario in skeleton["scenario"]}
    assert by_name["happy"]["recover_acts"] == []
    assert by_name["happy"]["hold_acts"] == []
    assert by_name["review"]["recover_acts"] == ["a1"]
    assert by_name["review"]["hold_acts"] == ["a2"]
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


def test_the_declared_reading_failure_outcomes_are_never_completed_class(skeleton):
    """Every declared reading-failure scenario drives a real hazard: a reading
    that did not succeed. A declaration that named a completed-class outcome
    would exercise nothing, whichever class it actually belongs to."""
    failures = skeleton["reading_failure"]
    assert len(failures) == 3
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
    }
    # `truncated` is FAILED-class and still carries text -- the hazard the
    # Archetypus's own guard (spec 09) exists to refuse.
    assert classify(PERLECTOR, by_scenario["truncated-reading"]) is OutcomeClass.FAILED
    # `no-readable-text` is UNRESOLVED-class: the Perlector's own direct claim
    # of absence, which the Recensor's blank confirmation may or may not be
    # able to corroborate depending on what the witnesses say.
    assert classify(PERLECTOR, by_scenario["confirmed-blank"]) is OutcomeClass.UNRESOLVED
    assert classify(PERLECTOR, by_scenario["blank-with-dissent"]) is OutcomeClass.UNRESOLVED


def test_each_page_refusal_declares_a_digest_the_bytes_cannot_match(skeleton):
    """The door must refuse through its real inspection path — a declared digest
    that happened to match the checked-in bytes would silently turn the refusal
    scenarios back into happy runs."""
    refusals = skeleton["page_refusal"]
    assert len(refusals) == 2
    true_digests = {page["ordinal"]: page["sha256"] for page in skeleton["page"]}
    scenario_names = {scenario["name"] for scenario in skeleton["scenario"]}
    for refusal in refusals:
        assert refusal["scenario"] in scenario_names
        assert refusal["ordinal"] in true_digests
        declared = refusal["declared_sha256"]
        assert len(declared) == 64
        assert declared != true_digests[refusal["ordinal"]]


def test_the_refusal_scenarios_lose_the_declared_pages(skeleton):
    by_scenario = {refusal["scenario"]: refusal["ordinal"] for refusal in skeleton["page_refusal"]}
    assert by_scenario == {"refused-page": 2, "refused-first-page": 1}


def test_the_testimony_table_carries_no_chair_the_run_does_not_configure(skeleton, models_config):
    for row in skeleton["testimony"]:
        assert row["chair"] in configured_witness_chairs(models_config)
    assert set(TESTIMONY) == {act["key"] for act in ACTS}
