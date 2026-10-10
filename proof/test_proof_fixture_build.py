"""The checked-in fixture bytes, and the two declarations that describe them.

Three surfaces must agree: the rendered pixels, the ingress declaration the hook
enforces, and the fixture declaration the stage programs read as data. Both
declarations are regenerated and compared whole, so the tests below check
properties of the fixture rather than restating its contents.

The pixel comparison rather than a byte comparison is deliberate. zlib's output is
a pure function of its input for a given build, but it is not guaranteed identical
across zlib implementations, so asserting checked-in bytes equal freshly-compressed
bytes would fail on a machine that has done nothing wrong. What must hold
everywhere is that the checked-in files decode to exactly the image this generator
describes — and that is asserted instead.
"""

import hashlib
import json
import tomllib
from pathlib import Path

import pytest

from common.chairs.config import load_models_toml
from common.chairs.models import ChairIdentity
from common.imaging import decode_grayscale_png
from proof.build_fixture import (
    _REASK_FIRST_READINGS,
    ACTS,
    EXTERNAL_FIXTURES,
    SCENARIOS,
    TESTIMONY,
    act_descriptor,
    build_ingress_manifest,
    build_skeleton_fixture,
    render_all,
    toml_string,
    toml_value,
)
from proof.synthetic_pages import ALL_PAGES, FIXTURE_ID, render_page

PROOF_ROOT = Path(__file__).resolve().parent
MODELS_CONFIG = PROOF_ROOT.parent / "config" / "models.toml"


def load(name: str) -> dict:
    with open(PROOF_ROOT / name, "rb") as handle:
        return tomllib.load(handle)


def test_toml_string_round_trips_every_forbidden_basic_string_control():
    value = 'quote=" slash=\\ controls=' + "".join(chr(code) for code in range(0x20)) + "\x7f"
    decoded = tomllib.loads(f"value = {toml_string(value)}\n")
    assert decoded["value"] == value


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
    assert len(entries) == 3 + len(EXTERNAL_FIXTURES)
    signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
    for entry in entries:
        path = PROOF_ROOT.parent / entry["path"]
        assert path.exists(), f"{entry['path']} is declared but not present"
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert len(data) == entry["bytes"]
        assert data.startswith(signatures[entry["media_type"]])


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
    present = {
        path
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.tif", "*.tiff")
        for path in (PROOF_ROOT / "fixtures").rglob(pattern)
    }
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
    # Two scenarios, the same page: one declares an empty response from every
    # whole-page chair; the other declares none, so those chairs end `not-run`.
    assert blank["scenarios"] == ["ink-free-page", "ink-free-page-unwitnessed"]
    source = next(page for page in ALL_PAGES if page["ordinal"] == 3)
    assert source["acts"] == ()
    _, _, rows = decode_grayscale_png(render_page(source))
    assert {value for row in rows for value in row} == {rows[0][0]}, (
        "page 3 must be uniform paper: the ink-free scenario proves nothing otherwise"
    )


def test_the_fixture_declares_no_act_for_the_pipeline_to_be_told(skeleton):
    """The page path finds its acts by reading whole pages, so the declaration
    carries none; the builder's own acts only shape the drawn pages and the
    witnesses' answers."""
    assert "act" not in skeleton and "continuation" not in skeleton
    for act in ACTS:
        source = act_descriptor(act["page_ordinal"], act["proposal_ordinal"])
        assert act["text"].startswith("SYNTHETIC ACT"), source


# --- Witness declarations leave no silent gap ----------------------------------


def test_every_whole_page_chair_has_a_response_declared_for_every_base_page(
    skeleton, models_config
):
    """A chair with no declared response to a page would silently become an
    absence the fixture never meant to describe. DAI answers record by record."""
    chairs = set(configured_witness_chairs(models_config)) - {"attestator_2"}
    declared = {
        (row["page_ordinal"], row["chair"])
        for table in ("testimony", "churro_page_response")
        for row in skeleton[table]
        if "scenario" not in row
    }
    assert declared == {(page, chair) for page in (1, 2) for chair in chairs}
    assert {row["chair"] for row in skeleton["dai_record_response"]} == {"attestator_2"}


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
        "ink-map",
        "designator",
        "attestatores",
        "perlector",
        "recensor",
        "archetypus",
        "coniector",
        "armarium",
    }


def test_testimony_differs_from_the_established_text_somewhere(skeleton):
    """Dissent must be exercisable: each chair's page text departs from the
    established text of some act on that page."""
    texts = {act["key"]: act["text"] for act in ACTS}
    for chair in ("attestator_2", "attestator_3"):
        assert any(TESTIMONY[key][chair] != texts[key] for key in texts)
    assert TESTIMONY["a1"]["attestator_1"] == texts["a1"]


def test_the_page_testimony_is_declared_per_page_and_chair(skeleton):
    for row in skeleton["testimony"]:
        assert "act_key" not in row
        assert {"page_ordinal", "chair", "payload"} <= set(row)


def test_chandra_page_text_is_the_join_of_its_placeholders(skeleton):
    """Each placeholder binds one act's text to native geometry over that act;
    the page text joins them, and a continuation-only page has none."""
    rows = {
        row["page_ordinal"]: row
        for row in skeleton["testimony"]
        if row["chair"] == "attestator_1" and "scenario" not in row
    }
    placeholders = [json.loads(raw) for raw in rows[1]["raw_responses"]]
    assert "\n".join(item["markdown"] for item in placeholders) == rows[1]["payload"]
    assert [item["blocks"] for item in placeholders] == [
        [{"bbox": [20.25, 20.5, 180, 100.1]}],
        [{"bbox": [20.25, 120.5, 180, 220.1]}],
    ]
    assert "raw_responses" not in rows[2]
    assert rows[2]["payload"] == TESTIMONY["a2"]["attestator_1"]


def test_scenario_specific_chandra_text_keeps_its_reported_layout(skeleton):
    """A self-report override must keep the geometry its base row reports."""
    base = next(
        row
        for row in skeleton["testimony"]
        if row["chair"] == "attestator_1" and row["page_ordinal"] == 1 and "scenario" not in row
    )
    override = next(
        row for row in skeleton["testimony"] if row.get("scenario") == "witness-capabilities"
    )
    assert override["raw_responses"] == base["raw_responses"]
    assert override["payload"] == base["payload"]
    assert override["witness_reported"] == {"confidence": "high"}


def test_the_declared_churro_page_responses_reach_a_page_scoped_chair(skeleton, models_config):
    """Every response must name a declared page and page-scoped chair."""
    page_chairs = {
        role
        for role, chair in models_config.chairs.items()
        if isinstance(chair, ChairIdentity) and chair.witness_scope == "page"
    }
    assert page_chairs, "the configuration seals no page witness at all"
    declared_pages = {page["ordinal"] for page in skeleton["page"]}
    rows = skeleton["churro_page_response"]
    for row in rows:
        assert set(row) - {"scenario"} == {
            "page_ordinal",
            "chair",
            "raw_xml",
            "transport_stop_reason",
        }
        assert row["chair"] in page_chairs
        assert row["page_ordinal"] in declared_pages
        assert row["transport_stop_reason"]
    keys = [(row.get("scenario"), row["page_ordinal"], row["chair"]) for row in rows]
    assert len(set(keys)) == len(keys), "two responses declared for one (scenario, page, chair)"


def test_the_base_churro_response_per_page_matches_its_reading(skeleton):
    """A declaration check, not a run: Churro's unscoped answer to each page
    reproduces the text of the acts on it, so the capture moves no reading."""
    base = [row for row in skeleton["churro_page_response"] if "scenario" not in row]
    assert {(row["page_ordinal"], row["chair"]) for row in base} == {
        (1, "attestator_3"),
        (2, "attestator_3"),
    }
    page_acts = {1: ("a1", "a2"), 2: ("a2",)}
    for row in base:
        joined = "\n".join(
            TESTIMONY[act_key][row["chair"]] for act_key in page_acts[row["page_ordinal"]]
        )
        lines = "".join(f"<Line>{line}</Line>" for line in joined.split("\n"))
        assert row["raw_xml"] == (
            f"<HistoricalDocument><Page><Body>{lines}</Body></Page></HistoricalDocument>"
        )
        assert row["transport_stop_reason"] == "eos"


def test_the_churro_scenarios_declare_success_visible_truncation_and_parse_failure(skeleton):
    """A declaration check, not a run: the three shapes are read from the fixture.

    `churro-native` declares a parseable response and one the provider cut
    mid-element; `churro-truncation` declares the visibly cut but still closed
    one. That the parser then classifies each as parsed, failed, and truncated
    is `test_feeding.py`'s to prove, not this file's.
    """
    rows = {
        (row["page_ordinal"], row["chair"]): row
        for row in skeleton["churro_page_response"]
        if row.get("scenario") == "churro-native"
    }
    assert set(rows) == {(1, "attestator_3"), (2, "attestator_3")}
    header = "[FOLIO RUBRIC 7 -- page furniture, belongs to no entry]"
    complete = rows[(1, "attestator_3")]
    assert complete["raw_xml"].startswith(f"<HistoricalDocument><Page><Body><Line>{header}</Line>")
    assert complete["raw_xml"].endswith("</Line></Body></Page></HistoricalDocument>")
    assert complete["transport_stop_reason"] == "eos"
    assert header not in "".join(act["text"] for act in ACTS)
    malformed = rows[(2, "attestator_3")]
    assert not malformed["raw_xml"].endswith("</HistoricalDocument>")
    assert malformed["transport_stop_reason"] == "length"
    truncation_rows = {
        (row["page_ordinal"], row["chair"]): row
        for row in skeleton["churro_page_response"]
        if row.get("scenario") == "churro-truncation"
    }
    assert set(truncation_rows) == {(1, "attestator_3"), (2, "attestator_3")}
    truncated = truncation_rows[(2, "attestator_3")]
    assert truncated["raw_xml"].endswith("</HistoricalDocument>")
    assert truncated["transport_stop_reason"] == "length"


def test_a_scenario_is_only_its_name(skeleton):
    """What a scenario departs in lives in the tables that name it."""
    names = [scenario["name"] for scenario in skeleton["scenario"]]
    assert len(set(names)) == len(names)
    assert all(set(scenario) == {"name"} for scenario in skeleton["scenario"])


def test_every_scenario_declares_the_reader_s_answer_to_every_page_it_reads(skeleton):
    """The page path reads every sealed page, so a scenario missing an answer
    would refuse at the Perlector rather than say anything about its departure."""
    answers = {(row["scenario"], row["page_ordinal"]) for row in skeleton["page_answer"]}
    refused = {(row["scenario"], row["ordinal"]) for row in skeleton["page_refusal"]}
    for name, _departure in SCENARIOS:
        pages = [
            page["ordinal"] for page in skeleton["page"] if name in page.get("scenarios", [name])
        ]
        for ordinal in pages:
            if (name, ordinal) not in refused:
                assert (name, ordinal) in answers, f"{name} has no answer for page {ordinal}"


def test_every_reask_and_reconstruction_answer_is_read_by_a_declared_scenario(skeleton):
    """These rows are read only for the running scenario, so a misspelt scenario
    or a missing or repeated row would sit in the fixture unread."""
    names = {name for name, _departure in SCENARIOS}
    reask = [(row["scenario"], row["page_ordinal"]) for row in skeleton["page_reask_answer"]]
    reconstruction = [
        (row["scenario"], row["page_ordinal"], row["pages_are_consecutive"])
        for row in skeleton["reconstruction_answer"]
    ]
    assert {scenario for scenario, _page in reask} <= names
    assert {scenario for scenario, _page, _consecutive in reconstruction} <= names
    assert len(set(reask)) == len(reask)
    assert len(set(reconstruction)) == len(reconstruction)
    # `reask-off` reads with the re-ask off, and `reask-cited-forgot` leaves nothing
    # unaccounted for, so neither asks again; every other one re-asks page 1.
    asked = set(_REASK_FIRST_READINGS) - {"reask-off", "reask-cited-forgot"}
    assert {(scenario, 1) for scenario in asked} <= set(reask)


def test_the_completed_empty_witness_is_declared_for_a_known_scenario_and_chair(
    skeleton, models_config
):
    rows = skeleton["witness_empty"]
    names = {scenario["name"] for scenario in skeleton["scenario"]}
    witness_chairs = set(configured_witness_chairs(models_config))
    assert {
        "scenario": "genuinely-empty-witness",
        "page_ordinal": 1,
        "chair": "attestator_3",
    } in rows
    assert all(row["scenario"] in names and row["chair"] in witness_chairs for row in rows)
    # The ink-free page's empty responses cover every
    # whole-page chair of the roster; DAI's detector census speaks for it there.
    whole_page_chairs = set(configured_witness_chairs(models_config)) - {"attestator_2"}
    fallback_rows = [row for row in rows if row["scenario"] == "ink-free-page"]
    assert {row["chair"] for row in fallback_rows} == whole_page_chairs
