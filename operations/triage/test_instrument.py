"""Synthetic checks for Unit 6A's deterministic co-visibility instrument."""

from __future__ import annotations

import json
from base64 import b64decode
from dataclasses import replace
from io import BytesIO

import PIL
import pillow_heif
import pytest
from PIL import Image, ImageDraw

from common.contracts.canonical import digest_bytes, digest_of
from operations.triage import instrument

# A synthetic 64×48 grayscale PNG.  The fixture is base64 so the repository stays
# text-only while still pinning the exact checked-in PNG bytes used by this test.
TINY_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAAAwCAAAAACEICPDAAAAPUlEQVR4nGO8w0AZYKJQ/6gBowYMEgNY0AUsCGg4QW0XjBowasCoAaMGjBowmAxgHG0jjRowagADAwMDAwB1rAMZa9FnFAAAAABJRU5ErkJggg=="
)
TINY_SIGNATURE_DIGEST = "a67b1f3ad64d7d48e57467a5efbdf831bdbe9b98d6519fcf67a6368327e64562"


def synthetic_page(
    *, left_insert: bool = False, right_insert: bool = False, shift: int = 0
) -> Image.Image:
    image = Image.new("L", (256, 192), 230)
    if left_insert:
        image.paste(20, (16 + shift, 24, 112 + shift, 168))
    if right_insert:
        image.paste(20, (144 + shift, 24, 240 + shift, 168))
    return image


def png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_pillow_reduce_is_the_pinned_integer_box_average():
    image = Image.new("L", (2, 2))
    image.putdata([0, 1, 2, 3])
    # Pillow 12.3.0's integer reduce rounds the exact 1.5 block mean to 2.
    assert list(image.reduce(2).get_flattened_data()) == [2]


def test_proxy_bytes_are_idempotent_and_the_checked_in_png_digest_is_pinned():
    config = instrument.load_config()
    first = instrument.build_proxies_from_bytes(TINY_PNG, config)
    second = instrument.build_proxies_from_bytes(TINY_PNG, config)
    assert first.signature_png == second.signature_png
    assert first.review_png == second.review_png
    assert first.signature_png_sha256 == TINY_SIGNATURE_DIGEST
    assert first.signature_png_sha256 == digest_bytes(first.signature_png)


def test_proxy_decode_refuses_before_loading_past_the_declared_pixel_bound(monkeypatch):
    config = instrument.load_config()
    monkeypatch.setattr(instrument, "MAX_PIXELS", 3)
    source = png_bytes(Image.new("L", (2, 2), 220))
    with pytest.raises(instrument.InstrumentRefusal, match="3-pixel bound"):
        instrument.build_proxies_from_bytes(source, config)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (Image.DecompressionBombError("synthetic bomb"), "decoder pixel safety bound"),
        (MemoryError("synthetic exhaustion"), "exhausted memory"),
    ],
)
def test_proxy_decode_names_resource_failures(monkeypatch, failure, reason):
    config = instrument.load_config()

    def fail_open(_source):
        raise failure

    monkeypatch.setattr(instrument.Image, "open", fail_open)
    with pytest.raises(instrument.InstrumentRefusal, match=reason):
        instrument.build_proxies_from_bytes(b"synthetic", config)


@pytest.mark.parametrize(
    ("content", "reason"),
    [(b"\xff", "valid UTF-8"), (b"not = valid = toml", "valid TOML")],
)
def test_configuration_parse_refusals_name_the_actual_format_failure(tmp_path, content, reason):
    config_path = tmp_path / "instrument.toml"
    config_path.write_bytes(content)
    with pytest.raises(instrument.InstrumentRefusal, match=reason):
        instrument.load_config(config_path)


def test_jpeg_recipe_records_versions_and_requires_remeasurement_across_versions():
    config = instrument.load_config()
    jpeg = BytesIO()
    synthetic_page(right_insert=True).save(jpeg, format="JPEG", quality=91)
    proxy = instrument.build_proxies_from_bytes(jpeg.getvalue(), config)
    recipe = instrument.producer_recipe(config)
    assert proxy.signature_png_sha256
    # Against the installed libraries, not against the call that wrote the recipe:
    # comparing the record with its own producer holds even when the versions are
    # empty or wrong, and these versions are the whole reason a JPEG proxy digest can
    # be re-measured after an upgrade.
    versions = recipe["imaging_library_versions"]
    assert set(versions) == {
        "renderer",
        "renderer_version",
        "pillow_heif_version",
        "libheif_version",
    }
    assert versions["renderer"] == "Pillow"
    assert versions["renderer_version"] == PIL.__version__
    assert versions["pillow_heif_version"] == pillow_heif.__version__
    assert versions["libheif_version"] == pillow_heif.libheif_info()["libheif"]
    assert all(value for value in versions.values())
    assert recipe["determinism"]["cross_version_claim"] == "NOT_CLAIMED"
    # The whole declared sentence, so a rewrite that drops the obligation cannot pass
    # on the strength of one surviving word.
    assert recipe["determinism"]["jpeg_remeasure_failure"] == instrument.JPEG_REMEASURE_FAILURE
    assert "re-measure the JPEG case before making any determinism claim" in (
        instrument.JPEG_REMEASURE_FAILURE
    )


def test_near_duplicate_small_insert_move_is_recorded_as_evidence_not_a_link():
    config = instrument.load_config()
    first = instrument.build_proxies(
        synthetic_page(left_insert=True), source_frame_sha256="1" * 64, config=config
    )
    moved = instrument.build_proxies(
        synthetic_page(left_insert=True, shift=12), source_frame_sha256="2" * 64, config=config
    )
    evidence = instrument.compare_signatures(first.signature, moved.signature, config)
    assert set(evidence) == set(instrument.EVIDENCE_FIELDS)
    assert evidence["schema"] == instrument.EVIDENCE_SCHEMA
    assert evidence["verdict"] == "near-duplicate"
    assert "link" not in evidence


def test_complementary_shape_is_flagged_but_never_asserted_as_a_link():
    config = instrument.load_config()
    sixty_three = instrument.build_proxies(
        synthetic_page(right_insert=True), source_frame_sha256="3" * 64, config=config
    )
    sixty_five = instrument.build_proxies(
        synthetic_page(left_insert=True), source_frame_sha256="5" * 64, config=config
    )
    evidence = instrument.compare_signatures(sixty_three.signature, sixty_five.signature, config)
    assert evidence["verdict"] == "complementary-candidate"
    assert evidence["agreeing_cells"] * 1000 < (
        evidence["thresholds"]["link_agreement_per_mille"] * evidence["overlapping_cells"]
    )
    assert "link" not in evidence
    assert (
        evidence["thresholds"]["complementary_candidate_reason"]
        == instrument.COMPLEMENTARY_CANDIDATE_REASON
    )


def test_a_pair_at_exactly_tau_link_is_still_only_recorded_candidate_evidence():
    """At the threshold, evidence remains a candidate record with no link shape."""
    config = instrument.load_config()
    grid_cells = config.grid_columns * config.grid_rows
    agree_target = -(-config.link_agreement_per_mille * grid_cells // 1000)  # ceil(9/10 * cells)
    disagree_count = grid_cells - agree_target

    def grid(digest: str, disagreeing: set[tuple[int, int]]) -> instrument.SignatureGrid:
        cells = tuple(
            instrument.Cell(mean_intensity=0, ink_count=0)
            if (x, y) in disagreeing
            else instrument.Cell(mean_intensity=100, ink_count=100)
            for y in range(config.grid_rows)
            for x in range(config.grid_columns)
        )
        return instrument.SignatureGrid(
            source_frame_sha256=digest,
            frame_size=(256, 192),
            proxy_size=(1024, 768),
            cells=cells,
            coarse_cells=(),
        )

    disagreeing = {
        (index % config.grid_columns, index // config.grid_columns)
        for index in range(disagree_count)
    }
    left = grid("1" * 64, set())
    right = grid("2" * 64, disagreeing)
    evidence = instrument.compare_signatures(left, right, config)

    assert (
        evidence["agreeing_cells"] * 1000
        >= config.link_agreement_per_mille * evidence["overlapping_cells"]
    )
    # An independent literal prevents EVIDENCE_FIELDS from hiding link-shaped output.
    assert set(evidence) == {
        "schema",
        "instrument_config_sha256",
        "both_digests",
        "offset",
        "agreeing_cells",
        "overlapping_cells",
        "disagreeing_component_count",
        "largest_component_share",
        "ink_count_total_left",
        "ink_count_total_right",
        "ink_count_distance_per_mille",
        "verdict",
        "thresholds",
    }
    assert "link" not in evidence
    assert "cluster_id" not in evidence
    assert "re_shoot_cluster_id" not in evidence


def test_one_disagreeing_cell_is_negligible_rather_than_unrelated():
    """The verdict must not run backwards against its own evidence.

    The blob share was once a floor the largest disagreeing component had to reach,
    which sent the tightest agreements — one to thirty of 3072 cells at the shipped
    values — past both clauses to "unrelated", while a hundred-cell blob was recorded
    near-duplicate. A re-shoot read as unrelated is how two frames of one physical page
    both enter the corpus as separate pages. The share still keeps a tiny difference
    from being argued about; it no longer disqualifies the pair for being too similar.
    Recorded without tuning against a synthetic corpus: the shipped values stay
    UNMEASURED, and a large diffuse disagreement is still not near-duplicate.
    """
    config = instrument.load_config()
    count = config.grid_columns * config.grid_rows
    common = instrument.Cell(mean_intensity=100, ink_count=100)
    changed = instrument.Cell(mean_intensity=0, ink_count=0)
    left = instrument.SignatureGrid(
        source_frame_sha256="1" * 64,
        frame_size=(256, 192),
        proxy_size=(1024, 768),
        cells=(common,) * count,
        coarse_cells=(),
    )
    right = instrument.SignatureGrid(
        source_frame_sha256="2" * 64,
        frame_size=(256, 192),
        proxy_size=(1024, 768),
        cells=(changed,) + (common,) * (count - 1),
        coarse_cells=(),
    )
    evidence = instrument.compare_signatures(left, right, config)
    assert evidence["agreeing_cells"] * 1000 >= (
        config.link_agreement_per_mille * evidence["overlapping_cells"]
    )
    assert evidence["disagreeing_component_count"] == 1
    assert evidence["largest_component_share"] < config.blob_share_per_mille
    assert evidence["verdict"] == "near-duplicate"


def test_unequal_dimensions_are_a_declared_comparison_refusal():
    config = instrument.load_config()
    one = instrument.build_proxies(
        Image.new("L", (256, 192), 220), source_frame_sha256="a" * 64, config=config
    )
    two = instrument.build_proxies(
        Image.new("L", (320, 192), 220), source_frame_sha256="b" * 64, config=config
    )
    with pytest.raises(instrument.InstrumentRefusal, match="unequal dimensions"):
        instrument.compare_signatures(one.signature, two.signature, config)


def test_malformed_signature_lengths_are_named_refusals_not_index_or_zip_errors():
    config = instrument.load_config()
    one = instrument.build_proxies(synthetic_page(), source_frame_sha256="a" * 64, config=config)
    two = instrument.build_proxies(synthetic_page(), source_frame_sha256="b" * 64, config=config)
    short_cells = instrument.SignatureGrid(
        source_frame_sha256=one.signature.source_frame_sha256,
        frame_size=one.signature.frame_size,
        proxy_size=one.signature.proxy_size,
        cells=one.signature.cells[:-1],
        coarse_cells=one.signature.coarse_cells,
    )
    with pytest.raises(instrument.InstrumentRefusal, match="left signature must carry exactly"):
        instrument.compare_signatures(short_cells, two.signature, config)
    short_coarse = instrument.SignatureGrid(
        source_frame_sha256=one.signature.source_frame_sha256,
        frame_size=one.signature.frame_size,
        proxy_size=one.signature.proxy_size,
        cells=one.signature.cells,
        coarse_cells=one.signature.coarse_cells[:-1],
    )
    with pytest.raises(instrument.InstrumentRefusal, match="coarse signature must carry exactly"):
        instrument.select_candidate_pairs([short_coarse, two.signature], config)


def test_every_produced_record_runs_through_the_shared_preference_refusal(monkeypatch):
    config = instrument.load_config()
    calls: list[dict] = []
    monkeypatch.setattr(instrument, "refuse_capture_preference", calls.append)
    one = instrument.build_proxies(synthetic_page(), source_frame_sha256="a" * 64, config=config)
    two = instrument.build_proxies(synthetic_page(), source_frame_sha256="b" * 64, config=config)
    recipe = instrument.producer_recipe(config)
    evidence = instrument.compare_signatures(one.signature, two.signature, config)
    records, manifest = instrument.candidate_evidence([one, two], config)
    assert recipe in calls
    assert evidence in calls
    assert records[0] in calls
    assert manifest in calls


def test_two_tier_cost_at_the_real_corpus_order_of_magnitude_is_explicit():
    config = instrument.load_config()
    signatures = [
        instrument.SignatureGrid(
            source_frame_sha256=f"{index:064x}",
            frame_size=(256, 192),
            proxy_size=(256, 192),
            cells=(),
            coarse_cells=tuple(instrument.Cell(index * 1000, index * 1000) for _ in range(16)),
        )
        for index in range(1200)
    ]
    selection = instrument.select_candidate_pairs(signatures, config)
    assert selection.cost.to_record() == {
        "submission_window_pairs": 14_322,
        "coarse_pairs_examined": 719_400,
        "global_prefilter_passes": 0,
        "unique_candidate_pairs": 14_322,
        "dimension_refused_pairs": 0,
    }
    assert len(selection.pairs) == 14_322


# Printed form geometry is shared across different openings, so threshold changes must
# preserve visibility into cases where the signature sees too much common structure.


def ruled_form() -> Image.Image:
    """A printed parish-register form: a frame rule, two column rules, 18 ruled rows."""
    image = Image.new("L", (768, 576), 235)
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 728, 536), outline=90, width=2)
    for x in (200, 520):
        draw.line((x, 40, x, 536), fill=90, width=2)
    for index in range(18):
        draw.line((40, 70 + index * 28, 728, 70 + index * 28), fill=110, width=1)
    return image


def entry_block(image: Image.Image, box: tuple[int, int, int, int], ink: int) -> Image.Image:
    """A dense contiguous block of ink: an entry, a pasted label, an insert card."""
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = box
    for y in range(top, bottom, 6):
        draw.line((left, y, right, y), fill=ink, width=3)
    return image


def handwriting(image: Image.Image, seed: int) -> Image.Image:
    """Short thin strokes along each ruled row: one printed form, different ink.

    The deterministic seed keeps the pinned measurements reproducible.
    """
    draw = ImageDraw.Draw(image)
    for row in range(18):
        y = 70 + row * 28
        x = 48
        step = 0
        while x < 720:
            run = 5 + (seed * 7 + row * 11 + step * 13) % 13
            if (seed * 3 + row * 5 + step * 7) % 10 < 7:
                drop = 3 + (seed + row + step) % 7
                draw.line((x, y - drop, x + run, y - drop - 1), fill=40, width=1)
            x += run + 4 + (seed + step * 3) % 6
            step += 1
    return image


def verdict_for(left: Image.Image, right: Image.Image, config) -> dict:
    one = instrument.build_proxies(left, source_frame_sha256="a" * 64, config=config)
    two = instrument.build_proxies(right, source_frame_sha256="b" * 64, config=config)
    return instrument.compare_signatures(one.signature, two.signature, config)


def test_two_different_blank_frames_of_one_ruled_form_are_recorded_near_duplicate():
    """Different blank ruled frames are indistinguishable at this signature grid.

    The verdict therefore needs a record-local reason that prevents agreement from
    being interpreted as page identity.
    """
    config = instrument.load_config()
    evidence = verdict_for(ruled_form(), ruled_form(), config)
    assert evidence["verdict"] == "near-duplicate"
    assert evidence["agreeing_cells"] == evidence["overlapping_cells"] == 3072
    assert evidence["disagreeing_component_count"] == 0
    assert evidence["ink_count_distance_per_mille"] == 0
    assert evidence["thresholds"]["near_duplicate_reason"] == instrument.NEAR_DUPLICATE_REASON
    assert evidence["thresholds"]["near_duplicate_reason"] != "identity"
    assert "link" not in evidence and "cluster_id" not in evidence


def test_ink_magnitude_separates_a_written_page_from_the_same_blank_form():
    """Cell agreement is a boolean; ink magnitude is the number a human needs.

    A written page and its own blank verso form agree wherever the print dominates.
    Agreement alone (841‰ here) puts this pair in the same neighbourhood as a genuine
    re-shoot; the ink distance (427‰ against 0‰ for one frame compared with itself)
    is what tells them apart, and it is why the record carries both ink totals.
    """
    config = instrument.load_config()
    written = entry_block(ruled_form(), (60, 60, 708, 150), ink=40)
    evidence = verdict_for(written, ruled_form(), config)
    assert evidence["agreeing_cells"] == 2586
    assert evidence["ink_count_total_left"] == 47298
    assert evidence["ink_count_total_right"] == 18966
    assert evidence["ink_count_distance_per_mille"] == 427
    identical = verdict_for(ruled_form(), ruled_form(), config)
    assert identical["ink_count_distance_per_mille"] == 0


def test_two_different_openings_with_co_located_ink_agree_in_every_cell():
    """Same printed form, same layout of entries, different openings → near-duplicate.

    The pages differ in what is written, not where. Nothing at this grid separates
    them, so the instrument may record a candidate but never assert a link.
    """
    config = instrument.load_config()
    one = entry_block(ruled_form(), (60, 60, 708, 150), ink=40)
    two = entry_block(ruled_form(), (60, 60, 708, 150), ink=45)
    evidence = verdict_for(one, two, config)
    assert evidence["verdict"] == "near-duplicate"
    assert evidence["ink_count_distance_per_mille"] == 0
    assert evidence["thresholds"]["near_duplicate_reason"] == instrument.NEAR_DUPLICATE_REASON


def test_different_openings_of_one_form_with_ordinary_handwriting_are_unrelated():
    """The component gate separates scattered handwriting despite high agreement.

    The pair clears the agreement threshold, so loosening the component condition
    without measurement would admit different written openings as near-duplicates.
    """
    config = instrument.load_config()
    evidence = verdict_for(handwriting(ruled_form(), 2), handwriting(ruled_form(), 9), config)
    assert evidence["verdict"] == "unrelated"
    assert evidence["disagreeing_component_count"] == 125
    assert evidence["agreeing_cells"] == 2928
    assert (
        evidence["agreeing_cells"] * 1000
        >= config.link_agreement_per_mille * evidence["overlapping_cells"]
    )


def test_the_sealed_recipe_states_what_the_signature_cannot_separate():
    config = instrument.load_config()
    recipe = instrument.producer_recipe(config)
    assert recipe["comparison_recipe"]["known_blindness"] == list(instrument.SIGNATURE_BLINDNESS)
    assert recipe["comparison_recipe"]["evidence_fields"] == list(instrument.EVIDENCE_FIELDS)
    stripped = json.loads(json.dumps(recipe))
    stripped["comparison_recipe"]["known_blindness"] = []
    with pytest.raises(instrument.InstrumentRefusal, match="blindness"):
        instrument.validate_producer_recipe(stripped)
    dropped = json.loads(json.dumps(recipe))
    del dropped["comparison_recipe"]["known_blindness"]
    with pytest.raises(instrument.InstrumentRefusal, match="closed schema"):
        instrument.validate_producer_recipe(dropped)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("signature_recipe", "cell_mean_rounding"),
        ("comparison_recipe", "offset_selection"),
        ("comparison_recipe", "disagreement_components"),
        ("comparison_recipe", "per_mille_rounding"),
        ("comparison_recipe", "verdict_rule"),
        ("candidate_selection_recipe", "submission_window_rule"),
        ("candidate_selection_recipe", "global_prefilter_scope"),
        ("candidate_selection_recipe", "deduplication"),
    ],
)
def test_the_sealed_recipe_refuses_changed_reproduction_semantics(section, field):
    config = instrument.load_config()
    recipe = json.loads(json.dumps(instrument.producer_recipe(config)))
    recipe[section][field] = "different algorithm"
    with pytest.raises(instrument.InstrumentRefusal, match="semantics|integer grid"):
        instrument.validate_producer_recipe(recipe)


def test_evidence_itself_declares_that_its_thresholds_are_unmeasured():
    config = instrument.load_config()
    evidence = verdict_for(ruled_form(), ruled_form(), config)
    assert evidence["thresholds"]["measurement_status"] == "UNMEASURED"


def test_recipe_validation_refuses_a_strengthened_determinism_claim():
    config = instrument.load_config()
    recipe = instrument.producer_recipe(config)
    overstated = json.loads(json.dumps(recipe))
    overstated["determinism"]["claim"] = "identical forever across every library version"
    with pytest.raises(instrument.InstrumentRefusal, match="determinism claim"):
        instrument.validate_producer_recipe(overstated)


@pytest.mark.parametrize(
    ("section", "field", "value", "reason"),
    [
        ("proxy_recipe", "signature_max_edge", 4096, "signature proxy edge"),
        ("comparison_recipe", "link_agreement_per_mille", 1001, "per-mille"),
        (
            "candidate_selection_recipe",
            "global_prefilter_agreeing_cells",
            17,
            "more than its 16 cells",
        ),
    ],
)
def test_recipe_validation_refuses_values_its_source_configuration_cannot_load(
    section, field, value, reason
):
    config = instrument.load_config()
    recipe = json.loads(json.dumps(instrument.producer_recipe(config)))
    recipe[section][field] = value
    with pytest.raises(instrument.InstrumentRefusal, match=reason):
        instrument.validate_producer_recipe(recipe)


def test_persisted_evidence_refuses_non_integer_measures_and_non_enum_verdicts():
    config = instrument.load_config()
    evidence = verdict_for(ruled_form(), ruled_form(), config)
    floated = json.loads(json.dumps(evidence))
    floated["agreeing_cells"] = 3072.0
    with pytest.raises(instrument.InstrumentRefusal, match="non-negative integers"):
        instrument.validate_candidate_evidence(floated, config)
    linked = json.loads(json.dumps(evidence))
    linked["verdict"] = "same-page-link"
    with pytest.raises(instrument.InstrumentRefusal, match="named enum"):
        instrument.validate_candidate_evidence(linked, config)


def test_persisted_evidence_refuses_a_valid_enum_that_contradicts_its_measures():
    config = instrument.load_config()
    evidence = verdict_for(ruled_form(), ruled_form(), config)
    evidence["verdict"] = "unrelated"
    with pytest.raises(instrument.InstrumentRefusal, match="contradicts its recorded"):
        instrument.validate_candidate_evidence(evidence, config)


def test_persisted_evidence_refuses_an_overlap_impossible_for_its_offset():
    config = instrument.load_config()
    evidence = verdict_for(ruled_form(), ruled_form(), config)
    evidence["agreeing_cells"] -= 1
    evidence["overlapping_cells"] -= 1
    with pytest.raises(instrument.InstrumentRefusal, match="internally inconsistent"):
        instrument.validate_candidate_evidence(evidence, config)


def test_persisted_evidence_cannot_drop_its_reason_or_unmeasured_status():
    config = instrument.load_config()
    evidence = verdict_for(ruled_form(), ruled_form(), config)
    for field in ("near_duplicate_reason", "measurement_status"):
        stripped = json.loads(json.dumps(evidence))
        del stripped["thresholds"][field]
        with pytest.raises(instrument.InstrumentRefusal, match="thresholds, reasons"):
            instrument.validate_candidate_evidence(stripped, config)


def test_evidence_carries_the_configuration_digest_that_produced_it():
    """Evidence and its sealed recipe join through the configuration digest.

    The threshold snapshot is a copy, not an identity — two configurations can share
    every threshold and differ in grid, proxy edge, or candidate window.  Without the
    configuration digest, a confirmation could not name the instrument whose evidence
    it acted on, and the sealed recipe the Door binds would join to nothing.
    """
    config = instrument.load_config()
    evidence = verdict_for(ruled_form(), ruled_form(), config)
    assert evidence["instrument_config_sha256"] == config.source_sha256
    assert (
        evidence["instrument_config_sha256"]
        == instrument.producer_recipe(config)["instrument_config_sha256"]
    )
    assert tuple(evidence) == instrument.EVIDENCE_FIELDS


def frames(*sizes: tuple[int, int]) -> list:
    """One proxy set per declared frame size, with distinguishable stable digests."""
    config = instrument.load_config()
    return [
        instrument.build_proxies(
            Image.new("L", size, 220 + index),
            source_frame_sha256=f"{index:064x}",
            config=config,
        )
        for index, size in enumerate(sizes)
    ]


def test_every_selected_pair_yields_exactly_one_evidence_record():
    """The candidate rule and the emitted set close over each other.

    A verdict is only as honest as the set it belongs to: an instrument that quietly
    emitted fewer records than it selected pairs would report a corpus with fewer
    candidates than it actually had, and nothing downstream could tell (GOVERNANCE 2).
    """
    config = instrument.load_config()
    proxies = frames(*[(256, 192)] * 5)
    evidence, manifest = instrument.candidate_evidence(proxies, config)
    cost = manifest["candidate_cost"]
    assert len(evidence) == cost["unique_candidate_pairs"] == 10
    assert manifest["emitted_evidence_records"] == 10
    assert cost["dimension_refused_pairs"] == 0
    assert manifest["frame_count"] == 5
    assert manifest["frame_digests"] == [proxy.source_frame_sha256 for proxy in proxies]
    # The pair digest is what makes a lost record findable: a reader holding the frame
    # digests and the recipe recomputes the selection and compares this one value.
    assert manifest["emitted_pairs_sha256"] == digest_of(
        sorted(record["both_digests"] for record in evidence)
    )


def test_proxy_bytes_must_match_the_digests_the_pass_carries():
    """A carried proxy digest is evidence only when it is compared before use."""
    config = instrument.load_config()
    proxies = frames((256, 192), (256, 192))
    proxies[0] = replace(proxies[0], signature_png=proxies[0].signature_png + b"changed")
    with pytest.raises(instrument.InstrumentRefusal, match="proxy bytes.*recorded digest"):
        instrument.candidate_evidence(proxies, config)


def test_an_oversized_evidence_sequence_refuses_before_traversing_its_records():
    config = instrument.load_config()
    proxies = frames((256, 192), (256, 192))
    selection = instrument.select_candidate_pairs([proxy.signature for proxy in proxies], config)
    with pytest.raises(instrument.InstrumentRefusal, match="2 records for 1 selected pairs"):
        instrument.evidence_manifest(proxies, selection, [object(), object()], config)


def test_a_dropped_evidence_record_is_refused_by_name_not_silently_shorter():
    config = instrument.load_config()
    proxies = frames(*[(256, 192)] * 4)
    selection = instrument.select_candidate_pairs([proxy.signature for proxy in proxies], config)
    evidence = [
        instrument.compare_signatures(proxies[left].signature, proxies[right].signature, config)
        for left, right in selection.pairs
    ]
    assert len(evidence) == 6
    with pytest.raises(instrument.InstrumentRefusal, match="does not account for every selected"):
        instrument.evidence_manifest(proxies, selection, evidence[:-1], config)
    # A duplicated record is the same failure from the other side: six records that
    # cover five pairs would satisfy a bare count and must not satisfy this one.
    with pytest.raises(instrument.InstrumentRefusal, match="does not account for every selected"):
        instrument.evidence_manifest(proxies, selection, evidence[:-1] + [evidence[0]], config)
    assert (
        instrument.evidence_manifest(proxies, selection, evidence, config)[
            "emitted_evidence_records"
        ]
        == 6
    )


def test_a_caller_supplied_subset_cannot_redefine_the_pass_denominator():
    config = instrument.load_config()
    proxies = frames(*[(256, 192)] * 4)
    complete = instrument.select_candidate_pairs([proxy.signature for proxy in proxies], config)
    subset = instrument.CandidateSelection(
        pairs=complete.pairs[:1],
        dimension_refused=(),
        cost=instrument.CandidateCost(
            submission_window_pairs=1,
            coarse_pairs_examined=0,
            global_prefilter_passes=0,
            unique_candidate_pairs=1,
            dimension_refused_pairs=0,
        ),
    )
    evidence = [
        instrument.compare_signatures(proxies[left].signature, proxies[right].signature, config)
        for left, right in subset.pairs
    ]
    with pytest.raises(instrument.InstrumentRefusal, match="complete selection recomputed"):
        instrument.evidence_manifest(proxies, subset, evidence, config)


def test_the_pass_manifest_binds_evidence_contents_not_only_pair_identities():
    config = instrument.load_config()
    proxies = frames((256, 192), (256, 192))
    selection = instrument.select_candidate_pairs([proxy.signature for proxy in proxies], config)
    evidence = [
        instrument.compare_signatures(proxies[left].signature, proxies[right].signature, config)
        for left, right in selection.pairs
    ]
    original = instrument.evidence_manifest(proxies, selection, evidence, config)
    assert original["evidence_records_sha256"] == digest_of(evidence)
    changed = json.loads(json.dumps(evidence))
    changed[0]["ink_count_total_left"] += 1
    assert original["evidence_records_sha256"] != digest_of(changed)
    with pytest.raises(instrument.InstrumentRefusal, match="records recomputed from this pass"):
        instrument.evidence_manifest(proxies, selection, changed, config)


@pytest.mark.parametrize("digest_field", ["emitted_pairs_sha256", "evidence_records_sha256"])
def test_persisted_pass_manifest_digests_are_compared_before_consumption(digest_field):
    config = instrument.load_config()
    proxies = frames((256, 192), (256, 192), (256, 192))
    evidence, manifest = instrument.candidate_evidence(proxies, config)
    assert instrument.validate_evidence_manifest(manifest, proxies, evidence, config) == manifest

    altered = json.loads(json.dumps(manifest))
    altered[digest_field] = "f" * 64
    with pytest.raises(instrument.InstrumentRefusal, match="does not match.*evidence it seals"):
        instrument.validate_evidence_manifest(altered, proxies, evidence, config)


def test_persisted_pass_manifest_cannot_use_a_boolean_as_an_integer_count():
    config = instrument.load_config()
    proxies = frames((256, 192))
    evidence, manifest = instrument.candidate_evidence(proxies, config)
    assert manifest["frame_count"] == 1
    altered = json.loads(json.dumps(manifest))
    altered["frame_count"] = True
    with pytest.raises(instrument.InstrumentRefusal, match="does not match.*evidence it seals"):
        instrument.validate_evidence_manifest(altered, proxies, evidence, config)


def test_a_pair_refused_for_unequal_dimensions_is_named_not_dropped():
    """The declared precondition is a recorded refusal, not an absence.

    Three reels need not share frame dimensions, and the pair the window reached but
    could not compare is exactly the fact an operator needs in order to know that a
    reel boundary, not the corpus, bounded the instrument's reach.
    """
    config = instrument.load_config()
    proxies = frames((256, 192), (256, 192), (320, 192))
    evidence, manifest = instrument.candidate_evidence(proxies, config)
    assert len(evidence) == 1
    assert manifest["candidate_cost"]["unique_candidate_pairs"] == 1
    assert manifest["candidate_cost"]["dimension_refused_pairs"] == 2
    assert manifest["dimension_refused_pairs"] == [
        [proxies[0].source_frame_sha256, proxies[2].source_frame_sha256],
        [proxies[1].source_frame_sha256, proxies[2].source_frame_sha256],
    ]
    assert (
        manifest["candidate_cost"]["submission_window_pairs"]
        == manifest["candidate_cost"]["unique_candidate_pairs"]
        + manifest["candidate_cost"]["dimension_refused_pairs"]
        == 3
    )


def test_the_conservation_record_carries_the_configuration_that_produced_it():
    config = instrument.load_config()
    _evidence, manifest = instrument.candidate_evidence(frames((256, 192), (256, 192)), config)
    assert manifest["schema"] == instrument.EVIDENCE_MANIFEST_SCHEMA
    assert manifest["instrument_config_sha256"] == config.source_sha256


def test_conservation_refuses_evidence_from_a_different_instrument_configuration():
    config = instrument.load_config()
    proxies = frames((256, 192), (256, 192))
    selection = instrument.select_candidate_pairs([proxy.signature for proxy in proxies], config)
    evidence = [
        instrument.compare_signatures(proxies[left].signature, proxies[right].signature, config)
        for left, right in selection.pairs
    ]
    evidence[0]["instrument_config_sha256"] = "f" * 64
    with pytest.raises(instrument.InstrumentRefusal, match="supplied instrument configuration"):
        instrument.evidence_manifest(proxies, selection, evidence, config)


def test_an_ink_count_is_within_cell_contrast_and_not_an_amount_of_ink():
    """A solid dark cell counts zero ink, exactly as blank paper does.

    The cell measure thresholds against that cell's *own* mean, so uniform tone —
    a solid insert card, a blown highlight, a gutter shadow — produces no count at
    all. A reader who took the recorded totals for ink volume would draw the wrong
    conclusion in precisely the case they exist to expose, so the sealed recipe
    names this among the blindnesses and the fields are named for the measure they
    total rather than for ink.
    """
    config = instrument.load_config()
    blank = instrument.build_proxies(
        Image.new("L", (768, 576), 235), source_frame_sha256="a" * 64, config=config
    )
    solid = instrument.build_proxies(
        Image.new("L", (768, 576), 0), source_frame_sha256="b" * 64, config=config
    )
    assert sum(cell.ink_count for cell in blank.signature.cells) == 0
    assert sum(cell.ink_count for cell in solid.signature.cells) == 0
    evidence = instrument.compare_signatures(blank.signature, solid.signature, config)
    assert evidence["ink_count_total_left"] == evidence["ink_count_total_right"] == 0
    assert evidence["ink_count_distance_per_mille"] == 0
    assert evidence["verdict"] == "complementary-candidate"
    assert evidence["agreeing_cells"] == 0
    assert evidence["disagreeing_component_count"] == 1
    assert any("within-cell contrast" in statement for statement in instrument.SIGNATURE_BLINDNESS)


def test_a_swapped_evidence_record_is_refused_even_though_the_count_is_right():
    """Conservation is set equality, not a tally.

    Two records for one pair and none for another satisfies any count while leaving a
    candidate pair unexamined — and an unexamined pair is exactly the thing GOVERNANCE 2
    refuses to let a successful status hide.
    """
    config = instrument.load_config()
    proxies = frames(*[(256, 192)] * 4)
    selection = instrument.select_candidate_pairs([proxy.signature for proxy in proxies], config)
    evidence = [
        instrument.compare_signatures(proxies[left].signature, proxies[right].signature, config)
        for left, right in selection.pairs
    ]
    swapped = evidence[:-1] + [evidence[0]]
    assert len(swapped) == len(evidence)
    with pytest.raises(instrument.InstrumentRefusal, match="does not account for every selected"):
        instrument.evidence_manifest(proxies, selection, swapped, config)


def test_repeated_frame_identity_cannot_turn_one_digest_pair_into_many_records():
    """Index uniqueness is not evidence-identity uniqueness.

    A pass must name each submitted master once before pair conservation can
    distinguish multiple index pairs from multiple evidence identities.
    """
    config = instrument.load_config()
    proxies = [
        instrument.build_proxies(
            Image.new("L", (256, 192), 220 + index),
            source_frame_sha256="a" * 64,
            config=config,
        )
        for index in range(3)
    ]
    with pytest.raises(instrument.InstrumentRefusal, match="repeats source frame digest"):
        instrument.candidate_evidence(proxies, config)


def test_pair_identity_and_directional_measures_are_canonical_by_digest():
    config = instrument.load_config()
    first = instrument.build_proxies(
        synthetic_page(left_insert=True), source_frame_sha256="a" * 64, config=config
    )
    second = instrument.build_proxies(
        synthetic_page(left_insert=True, shift=12),
        source_frame_sha256="b" * 64,
        config=config,
    )
    forward = instrument.compare_signatures(first.signature, second.signature, config)
    reverse = instrument.compare_signatures(second.signature, first.signature, config)
    assert forward == reverse
    assert forward["both_digests"] == ["a" * 64, "b" * 64]


def _retuned_config_path(tmp_path, columns: int, rows: int):
    """The shipped declaration with only its prefilter geometry retuned."""
    text = instrument.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    text = text.replace("global_prefilter_columns = 4", f"global_prefilter_columns = {columns}")
    text = text.replace("global_prefilter_rows = 4", f"global_prefilter_rows = {rows}")
    path = tmp_path / "instrument.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_retuned_prefilter_grid_produces_a_recipe_this_module_still_accepts(tmp_path):
    """The loader and the recipe validator must not disagree about the same grid.

    Both name 16 cells, and the columns and rows are declared UNMEASURED, so a
    2 × 8 retune has to survive the round trip. When the validator demanded the
    literal shipped [4, 4], the pass finished its comparisons and could not
    publish, and the refusal named 16 cells for a grid that had 16.
    """
    config = instrument.load_config(_retuned_config_path(tmp_path, 2, 8))
    assert (config.global_prefilter_columns, config.global_prefilter_rows) == (2, 8)
    recipe = json.loads(json.dumps(instrument.producer_recipe(config)))
    assert recipe["candidate_selection_recipe"]["global_prefilter_grid"] == [2, 8]
    assert instrument.validate_producer_recipe(recipe)


@pytest.mark.parametrize(
    ("grid", "reason"),
    [
        ([4, 5], "not 16 cells"),
        ([16], "columns by rows"),
        ("4x4", "columns by rows"),
        ([4, True], "positive integer"),
        ([0, 16], "positive integer"),
    ],
)
def test_a_recipe_prefilter_grid_outside_sixteen_cells_is_refused(grid, reason):
    config = instrument.load_config()
    recipe = json.loads(json.dumps(instrument.producer_recipe(config)))
    recipe["candidate_selection_recipe"]["global_prefilter_grid"] = grid
    with pytest.raises(instrument.InstrumentRefusal, match=reason):
        instrument.validate_producer_recipe(recipe)


def test_no_agreeing_pair_is_recorded_unrelated_for_disagreeing_less():
    """Inside the agreement regime the verdict may not run backwards.

    The three verdicts are not one scale — a complementary candidate disagrees a lot,
    in one region, by design. But while agreement still reaches the link threshold, a
    localized disagreement is the same kind of evidence however it is divided, and a
    *smaller* one may never earn "unrelated" when a larger one earns "near-duplicate".
    Walked across the whole band and over one and two regions, because both were bands:
    at the shipped values one to thirty cells in a single region scored unrelated while
    a hundred scored near-duplicate, and then forty cells split across two regions of
    twenty scored unrelated while thirty-one in one region scored near-duplicate.
    """
    config = instrument.load_config()
    overlapping = config.grid_columns * config.grid_rows
    reached = 0
    for components in (1, 2):
        for disagreements in range(components, overlapping + 1):
            if (overlapping - disagreements) * 1000 < config.link_agreement_per_mille * overlapping:
                continue
            reached += 1
            largest = disagreements - (components - 1)
            assert (
                instrument._verdict_for_metrics(
                    overlapping - disagreements,
                    overlapping,
                    components,
                    largest * 1000 // overlapping,
                    config,
                )
                == "near-duplicate"
            ), f"{disagreements} disagreeing cells in {components} region(s) is not near-duplicate"
    assert reached > 600, "the agreement band the shipped thresholds admit was not walked"


def test_a_large_disagreement_scattered_across_many_regions_stays_unrelated():
    """The component bound is what the blob-share floor was mistaken for.

    Admitting the tightly-agreeing pairs must not admit two different pages that happen
    to agree in most cells: a disagreement past the negligible share and spread over
    more than two regions is still no evidence of a re-shoot.
    """
    config = instrument.load_config()
    overlapping = config.grid_columns * config.grid_rows
    scattered = 200
    assert (
        instrument._verdict_for_metrics(
            overlapping - scattered,
            overlapping,
            40,
            5 * 1000 // overlapping,
            config,
        )
        == "unrelated"
    )


def test_the_shipped_recipe_validates_against_the_validator_that_guards_it():
    """The producer's two halves must agree, sentence for sentence.

    Every declared rule is written once as a module constant now, but the round trip
    is what proves it: if either half ever grows its own copy of a sentence, or the
    validator's closed key set drifts from `imaging_library_versions()`, the producer
    would refuse the only recipe it can build and could commit no confirmation at all.
    """
    recipe = json.loads(json.dumps(instrument.producer_recipe(instrument.load_config())))
    assert instrument.validate_producer_recipe(recipe) == recipe
    comparison = recipe["comparison_recipe"]
    assert comparison["verdict_rule"] == instrument.VERDICT_RULE
    assert comparison["offset_selection"] == instrument.OFFSET_SELECTION
    assert comparison["known_blindness"] == list(instrument.SIGNATURE_BLINDNESS)
    assert len(instrument.SIGNATURE_BLINDNESS) == 5, (
        "a dropped comma merges two blindness statements into one, and the recipe would "
        "then validate against the shortened tuple"
    )
