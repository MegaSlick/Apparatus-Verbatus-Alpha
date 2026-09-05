"""Product-level checks for the Armarium's one-text export projection."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import unicodedata
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest
from armarium_export import (
    CANONICAL_TEXT_FIELD,
    EXPORT_MANIFEST_NAME,
    ArmariumProjection,
    _page_ledger_category,
    _terminal_ledger,
    _verify_acts_schema,
    _zip_bytes,
    build_armarium_bundle,
    canonical_text_sha256,
    edge_hold_pages_from_rows,
    verify_delivered_bundle,
    verify_export_bundle,
    verify_projection_identity,
)
from display import DISPLAY_CONVENTION, render_display
from textnorm import TEXTNORM_REVISION, search_fold

from common.armarium_formats import ArmariumFormats
from common.contracts.approval import real_ingress_record
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ApprovalRefusal, SchemaRefusal
from common.contracts.outcomes import ArmariumCategory, run_aggregate
from common.contracts.stages import ARMARIUM
from common.contracts.uncertainty import validate as validate_uncertainty
from common.imaging import encode_grayscale_png
from common.residual_ink import MINIMUM_FRACTION_OUTSIDE_COVERAGE, MINIMUM_INK_PIXELS
from common.stage import REAL_SCENARIO, StageContext

TEXT_REGISTER = "text/_source_folder/register/readings.txt"
ROOT = Path(__file__).resolve().parents[2]
ARMARIUM_CLI = ROOT / "pipeline" / "7_armarium" / "run.py"


def _pixels(value: int) -> bytes:
    return encode_grayscale_png(1, 1, [bytearray([value])])


def _source_bytes(path: str) -> dict[str, bytes]:
    return {
        "1_exemplar/blobs/sha256/page": _pixels(80),
        "2_designator/blobs/sha256/crop": _pixels(40),
    }[path]


def _mapped_page(ordinal: int = 1) -> dict:
    return {"ordinal": ordinal, "initial_outcome": "mapped", "remeasured": None}


def _edge_page(ordinal: int = 1, *, outside: int, total: int = 10_000) -> dict:
    return {
        "ordinal": ordinal,
        "initial_outcome": "unclaimed-edge-ink",
        "remeasured": {
            "total_ink_pixels": total,
            "outside_ink_pixels": outside,
            "edge_band_pixels": 64,
        },
    }


def _projection(*, salvage_items=()) -> ArmariumProjection:
    page = _source_bytes("1_exemplar/blobs/sha256/page")
    crop = _source_bytes("2_designator/blobs/sha256/crop")
    region = {
        "region_id": "rgn-1",
        "image_path": "2_designator/blobs/sha256/crop",
        "image_sha256": digest_bytes(crop),
        "source_page_ordinal": 1,
        "source_page_id": "pg-1",
        "declared_path": "register/folio-1.png",
        "declared_sha256": digest_bytes(page),
        "transform": {
            "operation": "crop",
            "source_page_ordinal": 1,
            "source_page_id": "pg-1",
            "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
        },
    }
    return ArmariumProjection(
        fixture_id="armarium-export-test-v1",
        scenario="happy",
        config_digest="a" * 64,
        aggregate={
            "status": "partial",
            "reasons": ["act two is held-for-review"],
            "by_category": {"delivered": 1, "held-for-review": 1},
            "by_page_outcome": {"sealed": 1},
        },
        acts=(
            {
                "act_id": "act-1",
                "act_key": "one",
                "category": "delivered",
                "canonical_clean_text": "Cǣsar d’Amours",
                "uncertainty": {"uncertain_spans": [], "gaps": [], "self_revisions": []},
                "text_status": "established",
                "transcription_annotations": [],
                "provenance": {"chair": "perlector"},
                "source_regions": [region],
                "reason": None,
                "evidence_refs": [
                    {
                        "relative_path": "5_recensor/artifacts/review/act-1.json",
                        "sha256": "b" * 64,
                    }
                ],
                "witnesses": [{"chair": "attestator_1"}],
            },
            {
                "act_id": "act-2",
                "act_key": "two",
                "category": "held-for-review",
                "canonical_clean_text": None,
                "provenance": None,
                "source_regions": [],
                "reason": "the review remains unresolved",
                "evidence_refs": [
                    {
                        "relative_path": "5_recensor/artifacts/review/act-2.json",
                        "sha256": "c" * 64,
                    }
                ],
            },
        ),
        pages=(
            {
                "ordinal": 1,
                "outcome": "sealed",
                "reason": "",
                "declared_path": "register/folio-1.png",
                "declared_sha256": digest_bytes(page),
                "page_id": "pg-1",
                "image_path": "1_exemplar/blobs/sha256/page",
                "image_sha256": digest_bytes(page),
            },
        ),
        source_manifest=(
            {
                "ordinal": 1,
                "relative_path": "register/folio-1.png",
                "sha256": digest_bytes(page),
            },
        ),
        expected_acts=2,
        witness_chairs=("attestator_1",),
        witness_floor=1,
        aggregate_basis={
            "coverage_records": {
                "one": {
                    "configured": 1,
                    "floor": 1,
                    "under_witnessed": False,
                    "unresolved_chairs": 0,
                },
                "two": {
                    "configured": 1,
                    "floor": 1,
                    "under_witnessed": False,
                    "unresolved_chairs": 0,
                },
            },
            "unaddressed_chairs": [],
            "act_pages": {"one": [1], "two": [1]},
            "act_text_status": {"one": "established"},
        },
        salvage_items=tuple(salvage_items),
        ink_map_pages=(_mapped_page(),),
    )


def _damaged_delivered(
    projection: ArmariumProjection, *, text_status: str, **act_fields
) -> ArmariumProjection:
    """A projection whose delivered act is damaged, said the same way everywhere.

    The act's own `text_status`, the aggregate basis's `act_text_status`, and the
    aggregate measured from that basis are one statement, and the export now
    refuses a projection where they disagree. Building them by hand per test is
    how they would come to disagree for a reason nobody meant.
    """
    delivered = {**projection.acts[0], "text_status": text_status, **act_fields}
    acts = (delivered, *projection.acts[1:])
    basis = {
        **projection.aggregate_basis,
        "act_text_status": {delivered["act_key"]: text_status},
    }
    aggregate = run_aggregate(
        {act["act_key"]: ArmariumCategory(act["category"]) for act in acts},
        basis["coverage_records"],
        {page["ordinal"]: page for page in projection.pages},
        unaddressed_chairs=basis["unaddressed_chairs"],
        act_pages=basis["act_pages"],
        act_text_status=basis["act_text_status"],
    )
    return replace(projection, acts=acts, aggregate=aggregate, aggregate_basis=basis)


def _two_region_projection() -> ArmariumProjection:
    """A delivered continuation used to prove no format may drop its second citation."""
    original = _projection()
    second = {
        **original.acts[0]["source_regions"][0],
        "region_id": "rgn-2",
        "transform": {
            **original.acts[0]["source_regions"][0]["transform"],
            "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
        },
    }
    delivered = {
        **original.acts[0],
        "source_regions": [original.acts[0]["source_regions"][0], second],
    }
    return replace(original, acts=(delivered, original.acts[1]))


def _formats(*, embed_pixels: bool) -> ArmariumFormats:
    return ArmariumFormats(
        ("text-bundle", "acts-database", "jsonl", "review-items", "salvage-tier"),
        embed_pixels,
    )


def _salvage_item(content: str) -> dict:
    region = dict(_projection().acts[0]["source_regions"][0])
    region["region_id"] = "salvage-region-1"
    return {
        "salvage_id": "salvage-1",
        "content": content,
        "source_regions": [region],
        "provenance": {"collection": "separate tier"},
    }


def test_every_literal_projection_has_the_same_clean_text_and_hash(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)

    with ZipFile(BytesIO(bundle.data)) as archive:
        assert archive.namelist()[0] == EXPORT_MANIFEST_NAME
        assert not [name for name in archive.namelist() if name.startswith("pixels/")]
        text = archive.read(TEXT_REGISTER).decode("utf-8")
        assert "Cǣsar d’Amours" in text
        assert f"display_convention: {DISPLAY_CONVENTION}" in text
        # Twice: the canonical field, and the rendering beside it, which with no
        # uncertainty layer in the Archetypus record is the same text unchanged.
        assert text.count(json.dumps("Cǣsar d’Amours", ensure_ascii=False)) == 2

    manifest = verify_export_bundle(bundle.data, tmp_path / "clean")
    assert manifest["claims"]["status"] == "partial"
    assert manifest["claims"]["partial_reasons"][0].startswith("act act-2 is held-for-review")
    assert manifest["claims"]["pixels"]["resolution_claim"].startswith("reference validity")
    assert verify_projection_identity(bundle.data, tmp_path / "identity") == {
        "act-1": "Cǣsar d’Amours"
    }


def _otherwise_complete(**fields) -> ArmariumProjection:
    """A projection with nothing else wrong with it.

    `_projection()` carries a held act, so every status assertion made over it
    is already true before an edge hold is added; a test that used it could not
    tell "the edge hold forced partial" from "this projection was always
    partial". This one delivers every act it expects, so `complete` is the
    honest baseline and any partial result has exactly one cause.
    """
    original = _projection()
    acts = (original.acts[0],)
    basis = {
        **original.aggregate_basis,
        "coverage_records": {"one": original.aggregate_basis["coverage_records"]["one"]},
        "act_pages": {"one": [1]},
    }
    projection = replace(
        original,
        acts=acts,
        expected_acts=1,
        aggregate_basis=basis,
        **fields,
    )
    return replace(
        projection,
        aggregate=run_aggregate(
            {act["act_key"]: ArmariumCategory(act["category"]) for act in acts},
            basis["coverage_records"],
            {page["ordinal"]: page for page in projection.pages},
            unaddressed_chairs=basis["unaddressed_chairs"],
            act_pages=basis["act_pages"],
            act_text_status=basis["act_text_status"],
            # Page-scoped edge holds must enter the aggregate even when every
            # act category is complete.
            edge_hold_pages=edge_hold_pages_from_rows(list(projection.ink_map_pages)),
        ),
    )


def test_an_otherwise_complete_export_is_complete_without_an_edge_hold():
    """The control the hold test needs: this projection's baseline is green."""
    bundle = build_armarium_bundle(
        _otherwise_complete(), _formats(embed_pixels=False), _source_bytes
    )
    manifest = json.loads(_members(bundle.data)[EXPORT_MANIFEST_NAME])
    assert manifest["claims"]["status"] == "complete"
    assert manifest["claims"]["partial_reasons"] == []
    assert manifest["claims"]["ink_map"]["held_pages"] == []


def test_the_required_ink_map_claim_moves_the_manifest_schema_to_v3(tmp_path):
    """A v2 identity may not describe the new closed claim set.

    ``claims.ink_map`` is required, so old and new closed shapes need different
    identities rather than two incompatible meanings of v2.
    """
    members = _members(
        build_armarium_bundle(
            _otherwise_complete(), _formats(embed_pixels=False), _source_bytes
        ).data
    )
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    assert manifest["schema"] == "armarium-export-manifest.v3"

    manifest["schema"] = "armarium-export-manifest.v2"
    _refresh_manifest(members, manifest)
    with pytest.raises(SchemaRefusal, match="no recognized EXPORT_MANIFEST schema"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "stale-v2")


def test_an_unreleased_edge_finding_forces_a_partial_export_and_rejects_complete(tmp_path):
    """A page-level hold is not erased because its acts happen to be complete.

    Measured against the green control above, so the partial verdict, the named
    reason and the refusal all have exactly one cause: page 1's re-measure
    still leaves ink outside every cut.
    """
    bundle = build_armarium_bundle(
        _otherwise_complete(ink_map_pages=(_edge_page(outside=5_000),)),
        _formats(embed_pixels=False),
        _source_bytes,
    )
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])

    assert manifest["claims"]["status"] == "partial"
    assert manifest["claims"]["ink_map"]["held_pages"] == [1]
    assert any("unclaimed-edge-ink" in reason for reason in manifest["claims"]["partial_reasons"])

    manifest["claims"]["status"] = "complete"
    manifest["claims"]["partial_reasons"] = []
    _refresh_manifest(members, manifest)
    with pytest.raises(SchemaRefusal, match="does not match its own terminal ledger"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "complete-refusal")


def test_a_release_is_by_ink_and_a_partial_claim_does_not_make_one():
    """Release is derived from measured ink, not a separate decision.

    The same flagged page, judged only on how much of its own edge ink the
    Designator's cuts actually reached. A clear re-measure releases; a crop
    that claims all but a trace still releases only if that trace is under the
    ink map's own floor; a partial claim that leaves real ink outside holds.
    """
    # Total chosen so the ink map's *fraction* gate is the one deciding: 24 of
    # 1,000 is 2.4%, over `MINIMUM_FRACTION_OUTSIDE_COVERAGE`, while 23 is under
    # `MINIMUM_INK_PIXELS` and 24 of 10,000 would be under the fraction. Both
    # halves of the shared gate are exercised, neither is assumed.
    # The pixel counts move with `MINIMUM_INK_PIXELS`, but the 1,000 denominator
    # is a literal, so the relationship the third case depends on is stated
    # rather than left in the comment. A fraction gate raised past 2.4% would
    # otherwise turn the held case into a released one and fail while naming
    # ink counts instead of the gate that actually moved.
    assert MINIMUM_INK_PIXELS / 1_000 >= MINIMUM_FRACTION_OUTSIDE_COVERAGE, (
        "the 1,000-pixel total no longer puts MINIMUM_INK_PIXELS over the fraction "
        "gate; rebuild these cases around the new gate"
    )
    for outside, held in ((0, []), (MINIMUM_INK_PIXELS - 1, []), (MINIMUM_INK_PIXELS, [1])):
        bundle = build_armarium_bundle(
            _otherwise_complete(ink_map_pages=(_edge_page(outside=outside, total=1_000),)),
            _formats(embed_pixels=False),
            _source_bytes,
        )
        manifest = json.loads(_members(bundle.data)[EXPORT_MANIFEST_NAME])
        assert manifest["claims"]["ink_map"]["held_pages"] == held, outside
        assert manifest["claims"]["status"] == ("partial" if held else "complete"), outside


def test_a_dropped_edge_hold_cannot_be_verified_away_on_a_clean_machine(tmp_path):
    """The hold is derived from the source graph, never read out of its claim.

    The verifier cannot use `claims.ink_map.held_pages` to prove itself; the
    recorded counts in `sources.json` are its independent derivation basis.
    """
    held = _members(
        build_armarium_bundle(
            _otherwise_complete(ink_map_pages=(_edge_page(outside=5_000),)),
            _formats(embed_pixels=False),
            _source_bytes,
        ).data
    )
    green = _members(
        build_armarium_bundle(
            _otherwise_complete(ink_map_pages=(_edge_page(outside=0),)),
            _formats(embed_pixels=False),
            _source_bytes,
        ).data
    )
    # A held and released page must differ in source evidence, not only in the
    # manifest claim derived from it.
    assert held["sources.json"] != green["sources.json"]

    # Repair member digests so only the false derivation can refuse this green
    # manifest over a held source graph.
    forged = dict(held)
    green_manifest = json.loads(green[EXPORT_MANIFEST_NAME])
    for row in green_manifest["members"]:
        row["sha256"] = digest_bytes(forged[row["path"]])
        row["bytes"] = len(forged[row["path"]])
    _refresh_manifest(forged, green_manifest)
    with pytest.raises(SchemaRefusal, match="ink-map hold claim does not match"):
        verify_export_bundle(_zip_bytes(forged), tmp_path / "forged-green")


def test_an_edited_ink_map_row_cannot_release_a_page_it_still_flags(tmp_path):
    """Editing only the counts is refused by the claim they no longer support."""
    members = _members(
        build_armarium_bundle(
            _otherwise_complete(ink_map_pages=(_edge_page(outside=5_000),)),
            _formats(embed_pixels=False),
            _source_bytes,
        ).data
    )
    sources = json.loads(members["sources.json"])
    sources["ink_map_pages"][0]["remeasured"]["outside_ink_pixels"] = 0
    members["sources.json"] = canonical_bytes(sources)
    _refresh_manifest_member(members, "sources.json")
    with pytest.raises(SchemaRefusal, match="ink-map hold claim does not match"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "edited-row")


def test_the_ink_map_denominator_must_be_exactly_the_sealed_page_census():
    """Ink-map rows and the sealed page census must have identical identities."""
    with pytest.raises(SchemaRefusal, match="ink-map denominator is not exactly"):
        build_armarium_bundle(
            _otherwise_complete(ink_map_pages=()), _formats(embed_pixels=False), _source_bytes
        )
    with pytest.raises(SchemaRefusal, match="ink-map denominator is not exactly"):
        build_armarium_bundle(
            _otherwise_complete(ink_map_pages=(_mapped_page(), _mapped_page(2))),
            _formats(embed_pixels=False),
            _source_bytes,
        )


def test_a_page_the_map_never_flagged_may_not_carry_a_re_measurement():
    """GOVERNANCE 10: absence of a measurement is recorded as absence."""
    with pytest.raises(SchemaRefusal, match="re-measures an ink-map page its own map never"):
        build_armarium_bundle(
            _otherwise_complete(
                ink_map_pages=(
                    {
                        "ordinal": 1,
                        "initial_outcome": "mapped",
                        "remeasured": {
                            "total_ink_pixels": 0,
                            "outside_ink_pixels": 0,
                            "edge_band_pixels": 64,
                        },
                    },
                )
            ),
            _formats(embed_pixels=False),
            _source_bytes,
        )


def test_a_flagged_page_with_no_re_measurement_cannot_reach_an_export():
    """A hold may not be released by a row that never says it was re-measured."""
    with pytest.raises(SchemaRefusal, match="no re-measurement to resolve it"):
        build_armarium_bundle(
            replace(
                _otherwise_complete(),
                ink_map_pages=(
                    {"ordinal": 1, "initial_outcome": "unclaimed-edge-ink", "remeasured": None},
                ),
            ),
            _formats(embed_pixels=False),
            _source_bytes,
        )


def test_manifest_uncertainty_status_reflects_no_literal_format_carriage(tmp_path):
    formats = ArmariumFormats(("review-items", "salvage-tier"), embed_pixels=False)
    bundle = build_armarium_bundle(_projection(), formats, _source_bytes)
    manifest = json.loads(_members(bundle.data)[EXPORT_MANIFEST_NAME])

    assert manifest["claims"]["uncertainty"] == {
        "status": "not-applicable",
        "offset_unit": "unicode-code-point",
        "carried_by": [],
    }


def test_manifest_refuses_available_uncertainty_with_no_literal_carrier(tmp_path):
    formats = ArmariumFormats(("review-items", "salvage-tier"), embed_pixels=False)
    bundle = build_armarium_bundle(_projection(), formats, _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["claims"]["uncertainty"]["status"] = "canonical-unicode-codepoint-offsets"
    _refresh_manifest(members, manifest)

    with pytest.raises(SchemaRefusal, match="canonical carriage claim"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_literal_display_markers_do_not_refuse_or_change_an_established_text(tmp_path):
    projection = _projection()
    literal = r"Act ⟨literal⟩, gap glyphs ⟦not markup⟧, and a \\ path"
    delivered = {**projection.acts[0], "canonical_clean_text": literal}
    bundle = build_armarium_bundle(
        replace(projection, acts=(delivered, projection.acts[1])),
        _formats(embed_pixels=False),
        _source_bytes,
    )

    assert verify_projection_identity(bundle.data, tmp_path) == {"act-1": literal}


def test_an_act_missing_the_canonical_text_field_entirely_is_refused(tmp_path):
    """A dropped key must refuse like every other malformed field, not raise a bare KeyError."""
    projection = _projection()
    held = dict(projection.acts[1])
    del held["canonical_clean_text"]

    with pytest.raises(SchemaRefusal, match="no canonical-text field"):
        build_armarium_bundle(
            replace(projection, acts=(projection.acts[0], held)),
            _formats(embed_pixels=False),
            _source_bytes,
        )


@pytest.mark.parametrize("field", ["act_id", "act_key"])
def test_a_newline_in_an_act_identity_is_refused_at_the_boundary_that_owns_it(field, tmp_path):
    """The identity fields are spliced unescaped into a line-oriented format.

    Downstream cross-checks (aggregate-basis reconciliation, source-citation parsing)
    happen to catch a forged line today, but that is not the boundary that claims to
    own the question -- `_validate_projection` should refuse it directly.
    """
    projection = _projection()
    forged = {**projection.acts[0], field: "one\nact-id: forged"}

    with pytest.raises(SchemaRefusal, match="line-safe act identity"):
        build_armarium_bundle(
            replace(projection, acts=(forged, projection.acts[1])),
            _formats(embed_pixels=False),
            _source_bytes,
        )


@pytest.mark.parametrize(
    ("name", "separator"),
    # Written as code points rather than as glyphs: all three are invisible, and a
    # reader of this file has to be able to see which character is under test.
    [("U+0085", chr(0x85)), ("U+2028", chr(0x2028)), ("U+2029", chr(0x2029))],
)
def test_a_unicode_line_separator_in_a_reading_does_not_stop_the_whole_export(
    name, separator, tmp_path
):
    """Every line-oriented member must split only where its own writer joined.

    `ensure_ascii=False` puts these three into `acts.jsonl` and the text bundle raw,
    and `str.splitlines` breaks on all three -- so one of them in one act's reading
    refused the entire run's product, every act, not only the one carrying it.
    """
    projection = _projection()
    literal = f"Marie{separator}Anne"
    delivered = {**projection.acts[0], "canonical_clean_text": literal}
    bundle = build_armarium_bundle(
        replace(projection, acts=(delivered, projection.acts[1])),
        _formats(embed_pixels=False),
        _source_bytes,
    )

    assert verify_projection_identity(bundle.data, tmp_path / name) == {"act-1": literal}


def test_projection_identity_refuses_a_self_consistent_package_with_one_drifted_format(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    records = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    records[0]["canonical_clean_text"] = "a different purported reading"
    records[0]["canonical_text_sha256"] = canonical_text_sha256(records[0]["canonical_clean_text"])
    members["acts.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in records)
    _refresh_manifest_member(members, "acts.jsonl")

    # The member digests now agree, so package verification alone is green. The
    # identity guard is the independent assertion that catches a writer which
    # changes one literal projection while leaving the other formats intact.
    tampered = _zip_bytes(members)
    verify_export_bundle(tampered, tmp_path / "clean")
    with pytest.raises(SchemaRefusal, match="projection differs"):
        verify_projection_identity(tampered, tmp_path / "identity")


def test_projection_identity_refuses_a_self_consistent_package_with_drifted_uncertainty(tmp_path):
    """The same drift class as the sibling test above, one field over.

    A writer that changed only `uncertainty` -- never touching `canonical_clean_text`
    or its hash -- would pass the literal-text identity check by construction: the
    text is untouched. Uncertainty is a projected reading beside that text, not a
    decoration outside GOVERNANCE 5's reach, so a format that silently drifted on it
    alone must fail identity exactly as a drifted literal would (U3).
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    records = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    records[0]["uncertainty"] = {
        "uncertain_spans": [{"start": 0, "end": 1, "alternatives": ["X"], "confidence": "low"}],
        "gaps": [],
        "self_revisions": [],
    }
    members["acts.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in records)
    _refresh_manifest_member(members, "acts.jsonl")

    # The member digests now agree and every format's own uncertainty layer is
    # independently well-formed against its own literal text, so package
    # verification alone is green. The identity guard is the independent
    # assertion that catches a writer which changes one format's uncertainty
    # while leaving the other formats' at their original (also valid) value.
    tampered = _zip_bytes(members)
    verify_export_bundle(tampered, tmp_path / "clean")
    with pytest.raises(SchemaRefusal, match="projection differs"):
        verify_projection_identity(tampered, tmp_path / "identity")


def test_text_bundle_refuses_two_uncertainty_lines_for_one_literal(tmp_path):
    """A second layer cannot overwrite the first while the parser walks the section."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    marker = lines.index("uncertainty:")
    lines[marker:marker] = lines[marker : marker + 2]
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="more than one uncertainty layer"):
        verify_projection_identity(_zip_bytes(members), tmp_path)


def test_text_bundle_refuses_a_literal_section_with_no_uncertainty_layer(tmp_path):
    """The layer is not optional beside a delivered literal, and says so by name."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    marker = lines.index("uncertainty:")
    del lines[marker : marker + 2]
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="literal with no uncertainty layer"):
        verify_projection_identity(_zip_bytes(members), tmp_path)


def test_text_bundle_refuses_a_second_literal_that_would_orphan_its_uncertainty(tmp_path):
    """The layer's anchor cannot be swapped out from under it after it is checked.

    `uncertainty:` validates against the literal already parsed. A section that
    then declared a *second* `canonical_clean_text:` recorded the new literal
    beside the first literal's layer -- offsets into a text this act no longer
    carries -- and every remaining check passed: the second literal has its own
    valid hash line and its own display that strips back to it. Two or more
    literal formats show the drift as a projection-identity mismatch, but a
    package may legally select the text bundle as its one literal format, and
    there this section is the whole reading of the act.
    """
    formats = ArmariumFormats(("text-bundle", "review-items", "salvage-tier"), False)
    original = _projection()
    literal = original.acts[0]["canonical_clean_text"]
    delivered = {
        **original.acts[0],
        "uncertainty": {
            "uncertain_spans": [
                {"start": 0, "end": len(literal), "alternatives": ["?"], "confidence": "low"}
            ],
            "gaps": [],
            "self_revisions": [],
        },
    }
    bundle = build_armarium_bundle(
        replace(original, acts=(delivered, original.acts[1])), formats, _source_bytes
    )
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    replacement = "X"
    assert len(replacement) < len(literal)
    marker = lines.index("uncertainty:")
    lines[marker + 2 : marker + 2] = [
        f"canonical_text_sha256: {canonical_text_sha256(replacement)}",
        "canonical_clean_text:",
        json.dumps(replacement, ensure_ascii=False),
    ]
    lines[lines.index("display:") + 1] = json.dumps(render_display(replacement), ensure_ascii=False)
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="more than one literal"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path / "single")


def test_text_bundle_refuses_an_uncertainty_line_before_its_literal(tmp_path):
    """Line order binds an uncertainty layer to an already parsed act literal."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    marker = lines.index("uncertainty:")
    uncertainty_lines = lines[marker : marker + 2]
    del lines[marker : marker + 2]
    literal_marker = lines.index("canonical_clean_text:")
    lines[literal_marker:literal_marker] = uncertainty_lines
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="has no literal to anchor to"):
        verify_projection_identity(_zip_bytes(members), tmp_path)


def test_text_bundle_refuses_uncertainty_valid_only_for_a_different_acts_literal(tmp_path):
    """Valid JSON and valid offsets for some other act do not authorize this act."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    marker = lines.index("uncertainty:")
    own_literal = _projection().acts[0]["canonical_clean_text"]
    other_literal = own_literal + " belongs to a different act"
    other_layer = {
        "uncertain_spans": [
            {
                "start": len(own_literal),
                "end": len(own_literal) + 1,
                "alternatives": ["?"],
                "confidence": "low",
            }
        ],
        "gaps": [],
        "self_revisions": [],
    }
    assert validate_uncertainty(other_layer, other_literal) == other_layer
    lines[marker + 1] = json.dumps(other_layer, ensure_ascii=False, sort_keys=True)
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="does not anchor to its own act's literal"):
        verify_projection_identity(_zip_bytes(members), tmp_path)


def test_jsonl_uncertainty_status_may_not_contradict_the_layer_beside_it(tmp_path):
    """The declaration a recipient reads is checked against the payload it describes.

    Cross-format identity compares layer to layer; it never reads
    `uncertainty_status`, so before this guard a delivered JSONL row could carry a
    valid canonical layer while telling every reader of that row there was none.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    records = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    assert records[0]["uncertainty_status"] == "canonical-unicode-codepoint-offsets"
    records[0]["uncertainty_status"] = "not-applicable"
    members["acts.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in records)
    _refresh_manifest_member(members, "acts.jsonl")

    with pytest.raises(SchemaRefusal, match="does not declare the canonical uncertainty carriage"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_acts_database_uncertainty_status_may_not_contradict_the_layer_beside_it(tmp_path):
    """The same declaration, in the format whose column a search tool reads first."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    database = tmp_path / "tampered.sqlite"
    database.write_bytes(members["acts.sqlite"])
    connection = sqlite3.connect(database)
    connection.execute("UPDATE acts SET uncertainty_status = 'not-applicable'")
    connection.commit()
    connection.close()
    members["acts.sqlite"] = database.read_bytes()
    _refresh_manifest_member(members, "acts.sqlite")

    with pytest.raises(SchemaRefusal, match="does not declare the canonical uncertainty carriage"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_single_literal_format_package_still_reads_back_its_uncertainty(tmp_path):
    """One selected literal format has nothing to compare against -- and is still read.

    `_compare_literal_projections` needs two formats to say anything, so a package
    that selects only `jsonl` was leaving `claims.uncertainty` asserted and never
    verified: the same defect `verify_delivered_bundle` exists to refuse for the
    one text.
    """
    formats = ArmariumFormats(("jsonl", "review-items", "salvage-tier"), False)
    bundle = build_armarium_bundle(_projection(), formats, _source_bytes)
    members = _members(bundle.data)
    records = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    records[0]["uncertainty"] = {"nonsense": True}
    members["acts.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in records)
    _refresh_manifest_member(members, "acts.jsonl")

    with pytest.raises(SchemaRefusal, match="does not anchor to its own act's literal"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path / "single")


def test_a_non_delivered_act_may_not_carry_an_uncertainty_layer(tmp_path):
    """Offsets into a text this act does not have are not a reading to export."""
    original = _projection()
    held = {
        **original.acts[1],
        "uncertainty": {"uncertain_spans": [], "gaps": [], "self_revisions": []},
    }
    with pytest.raises(SchemaRefusal, match="may not carry an uncertainty layer"):
        build_armarium_bundle(
            replace(original, acts=(original.acts[0], held)),
            _formats(embed_pixels=False),
            _source_bytes,
        )


def test_the_delivered_gate_asks_both_questions_the_manifest_claims_were_asked(tmp_path):
    """GOVERNANCE 5 on the path the product actually leaves by.

    The package above is internally whole and carries two different readings of one
    act, and its own manifest says `identity_verified_across` all three literal
    formats. `verify_export_bundle` is entitled to pass it -- integrity is its whole
    question -- but the publish gate is the last reader before a recipient who has
    only these bytes, and it published this package.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    records = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    records[0]["canonical_clean_text"] = "a different purported reading"
    records[0]["canonical_text_sha256"] = canonical_text_sha256(records[0]["canonical_clean_text"])
    members["acts.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in records)
    _refresh_manifest_member(members, "acts.jsonl")
    tampered = _zip_bytes(members)

    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    assert manifest["canonical_text"]["identity_verified_across"] == [
        "acts-database",
        "jsonl",
        "text-bundle",
    ]
    with pytest.raises(SchemaRefusal, match="projection differs"):
        verify_delivered_bundle(tampered, tmp_path / "delivered")

    report = verify_delivered_bundle(bundle.data, tmp_path / "intact")["verification"]
    assert report["projection_identity"] == {
        "status": "verified",
        "compared_formats": ["acts-database", "jsonl", "text-bundle"],
    }
    # Both questions in one extraction, and the fold report survives the second one.
    assert report["search_fold"]["status"] == "verified"


def test_the_delivered_gate_says_when_there_was_nothing_to_compare(tmp_path):
    """One literal format is not a silent pass of a comparison that never ran."""
    single = build_armarium_bundle(_projection(), ArmariumFormats(("jsonl",), False), _source_bytes)

    report = verify_delivered_bundle(single.data, tmp_path / "single")["verification"]

    assert report["projection_identity"] == {
        "status": "not-applicable-fewer-than-two-literal-formats",
        "compared_formats": ["jsonl"],
    }


def test_delivered_gate_requires_review_evidence_references(tmp_path):
    """A resealed review row may not erase the evidence it promised to retain."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    rows = [json.loads(line) for line in members["review-items.jsonl"].decode("utf-8").splitlines()]
    assert rows[0]["evidence_refs"]
    # Emptied, not deleted. Deleting the key trips the field-set guard first --
    # already covered for delivered bundles by the retired-fields test below --
    # and the gate's own evidence check never runs. Emptying is also the shape a
    # resealer would actually produce: the promised field, keeping nothing.
    rows[0]["evidence_refs"] = []
    members["review-items.jsonl"] = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    _refresh_manifest_member(members, "review-items.jsonl")

    with pytest.raises(SchemaRefusal, match="must retain at least the Recensor review"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path / "delivered")


def test_an_evidence_reference_list_may_not_be_empty(tmp_path):
    """Every act has a Recensor review citation, so empty means evidence was lost."""
    projection = _projection()
    uncited = {**projection.acts[0], "evidence_refs": []}

    with pytest.raises(SchemaRefusal, match="must retain at least the Recensor review"):
        build_armarium_bundle(
            replace(projection, acts=(uncited, *projection.acts[1:])),
            _formats(embed_pixels=False),
            _source_bytes,
        )


def test_delivered_gate_refuses_retired_fields_on_an_act_v2_row(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    rows = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    rows[0]["retired_v1_evidence"] = []
    members["acts.jsonl"] = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    _refresh_manifest_member(members, "acts.jsonl")

    with pytest.raises(SchemaRefusal, match="field set"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path / "delivered")


def test_delivered_gate_requires_sqlite_product_identity(tmp_path):
    """Three ordinary tables are not proof of the requested SQLite product."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    database = tmp_path / "stripped.sqlite"
    database.write_bytes(members["acts.sqlite"])
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA user_version=0")
        connection.execute("DROP TABLE acts_fts")
        connection.execute("DELETE FROM export_metadata WHERE key = 'schema'")
        connection.commit()
    finally:
        connection.close()
    members["acts.sqlite"] = database.read_bytes()
    _refresh_manifest_member(members, "acts.sqlite")

    with pytest.raises(SchemaRefusal, match="SQLite product identity"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path / "delivered")


def test_a_verifier_without_fts5_names_why_sqlite_identity_cannot_be_checked(monkeypatch):
    """A missing verifier capability is a refusal, not a raw sqlite traceback."""

    def no_fts5():
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr("armarium_export._expected_acts_schema", no_fts5)
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(SchemaRefusal, match="requires FTS5 support"):
            _verify_acts_schema(connection)
    finally:
        connection.close()


def test_delivered_gate_requires_the_fts5_index_to_actually_carry_the_fold(tmp_path):
    """A present, correctly-typed `acts_fts` table is not a populated one.

    `INSERT INTO acts_fts(acts_fts) VALUES ('delete-all')` empties the FTS5
    shadow index while leaving the table itself, `act_search`, and every
    digest-checked column untouched -- `SELECT count(*)`/bare `SELECT rowid`
    on an external-content FTS5 table still answer from the content table, so
    only a `MATCH` query actually reads the index a client's full-text search
    would use.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    database = tmp_path / "emptied.sqlite"
    database.write_bytes(members["acts.sqlite"])
    connection = sqlite3.connect(database)
    try:
        connection.execute("INSERT INTO acts_fts(acts_fts) VALUES ('delete-all')")
        connection.commit()
    finally:
        connection.close()
    members["acts.sqlite"] = database.read_bytes()
    _refresh_manifest_member(members, "acts.sqlite")

    with pytest.raises(SchemaRefusal, match="full-text index"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path / "delivered")


def _resealed_acts_database(tmp_path, mutate, *, name="resealed.sqlite"):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    database = tmp_path / name
    database.write_bytes(members["acts.sqlite"])
    connection = sqlite3.connect(database)
    try:
        mutate(connection)
        connection.commit()
    finally:
        connection.close()
    members["acts.sqlite"] = database.read_bytes()
    _refresh_manifest_member(members, "acts.sqlite")
    return _zip_bytes(members)


def test_a_full_text_index_poisoned_with_terms_no_act_carries_is_refused(tmp_path):
    """A `MATCH` probe proves presence, and presence is only half the question.

    Appending a document to an external-content FTS5 index leaves every digest,
    every `act_search` column and the fold's own terms exactly as sealed, so a
    per-row phrase probe still finds what it went looking for. The recipient's
    search, meanwhile, now returns this act for words the Archetypus never
    established -- a second reading of the act inside the same package, which is
    what GOVERNANCE 5 forbids.
    """
    tampered = _resealed_acts_database(
        tmp_path,
        lambda connection: connection.execute(
            "INSERT INTO acts_fts(rowid, derived_search_text) VALUES (1, 'fabricated terms')"
        ),
    )

    with pytest.raises(SchemaRefusal, match="full-text index"):
        verify_delivered_bundle(tampered, tmp_path / "delivered")


def test_a_ghost_act_injected_into_the_full_text_index_is_refused(tmp_path):
    """An indexed rowid that exists in no table the verifier reads at all.

    Every projection check in this file enumerates `acts` or `act_search`, so a
    document indexed under a rowid neither table holds was invisible to all of
    them -- and perfectly visible to a client's `MATCH`.
    """
    tampered = _resealed_acts_database(
        tmp_path,
        lambda connection: connection.execute(
            "INSERT INTO acts_fts(rowid, derived_search_text) VALUES (9001, 'an act never read')"
        ),
    )

    with pytest.raises(SchemaRefusal, match="full-text index"):
        verify_delivered_bundle(tampered, tmp_path / "delivered")


def test_a_full_text_index_repointed_at_a_decoy_content_table_is_refused(tmp_path):
    """FTS5 records what it indexes in its own declaration, so the declaration is checked.

    Dropping `acts_fts` and recreating it over a table the resealer also added
    leaves an index that is perfectly self-consistent and consistent with *its*
    content table: an integrity check sees nothing wrong, because from inside
    FTS5 nothing is. The decoy deliberately carries the true fold as a prefix, so
    a phrase probe finds it too. Only the schema says which table `acts_fts` is
    an index of.
    """

    def repoint(connection):
        connection.executescript(
            """
            CREATE TABLE decoy(rowid INTEGER PRIMARY KEY, derived_search_text TEXT);
            INSERT INTO decoy(rowid, derived_search_text)
                VALUES (1, 'caesar damours and fabricated terms');
            DROP TABLE acts_fts;
            CREATE VIRTUAL TABLE acts_fts USING fts5(
                derived_search_text,
                content='decoy',
                content_rowid='rowid',
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO acts_fts(acts_fts) VALUES ('rebuild');
            """
        )

    with pytest.raises(SchemaRefusal, match="a definition this build never wrote"):
        verify_delivered_bundle(_resealed_acts_database(tmp_path, repoint), tmp_path / "delivered")


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE side_channel(payload TEXT)",
        "CREATE VIEW acts_shadow AS SELECT * FROM acts",
    ],
)
def test_an_acts_database_carrying_an_unaccounted_schema_object_is_refused(statement, tmp_path):
    """The database is a closed product, exactly as every JSONL row is a closed record."""
    tampered = _resealed_acts_database(tmp_path, lambda connection: connection.execute(statement))

    with pytest.raises(SchemaRefusal, match="unaccounted schema object"):
        verify_delivered_bundle(tampered, tmp_path / "delivered")


def test_an_established_reading_that_folds_to_no_search_token_still_publishes(tmp_path):
    """The index check may not refuse a good package for a defect it does not have.

    `search_fold` keeps every alphanumeric character Python knows about; FTS5's
    `unicode61` tokenizer classifies from its own table, and the two do not agree
    on every code point. A reading made only of characters in that gap folds to a
    non-empty key that tokenizes to nothing, which a per-row phrase probe reads as
    a missing index entry -- and the whole export died, naming a tampered index
    that was never tampered with. GOALS 1: an act refused at the terminal gate for
    an instrument's own disagreement is an act that does not leave the pipeline.
    """
    projection = _projection()
    delivered = {**projection.acts[0], CANONICAL_TEXT_FIELD: "\u19b1\u19b2"}
    tokenless = replace(projection, acts=(delivered, *projection.acts[1:]))
    assert search_fold(delivered[CANONICAL_TEXT_FIELD]) != ""

    bundle = build_armarium_bundle(tokenless, _formats(embed_pixels=False), _source_bytes)

    assert (
        verify_delivered_bundle(bundle.data, tmp_path / "delivered")["verification"]["search_fold"][
            "status"
        ]
        == "verified"
    )


def _resealed_manifest(mutate):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    del manifest["self_hash"]
    mutate(manifest)
    _refresh_manifest(members, manifest)
    return _zip_bytes(members)


def _real_resealed_manifest(mutate):
    """`_resealed_manifest`'s twin over the `submission_id`-shaped real projection.

    **Adapted from `_resealed_manifest` directly above, and deliberately still a
    second copy of it.** The two bodies are the same four steps -- build, read
    the manifest out of the members, drop the self-hash so `mutate` can rewrite
    the binding, reseal -- and differ in one line: the projection this one
    builds from carries `fixture_id=None` and a `submission_id`, because
    `_projection()` is fixture-shaped and every case run through the original
    therefore exercises `_verify_manifest_field_closure`'s fixture branch and
    never its real one. Naming that here is what keeps the duplication visible
    to whoever next changes either: a change to the resealing steps belongs in
    both.

    Nothing crossed a boundary to get here. This is a sibling helper in this
    same module, adapted within the repository, not code carried from the old
    pipeline or from a third party -- the quarantine rule (CLAUDE.md) governs
    that crossing and has nothing to say about this one.
    """
    projection = replace(_projection(), fixture_id=None, submission_id="a" * 64)
    bundle = build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    del manifest["self_hash"]
    mutate(manifest)
    _refresh_manifest(members, manifest)
    return _zip_bytes(members)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda manifest: manifest["run"].update(submission_id=["not", "an", "identity"]),
            id="a non-string submission identity",
        ),
        pytest.param(
            lambda manifest: manifest["run"].update(submission_id="   "),
            id="a blank submission identity",
        ),
        pytest.param(
            lambda manifest: manifest["run"].update(operator="nobody"),
            id="an unexpected extra key on the real run binding",
        ),
    ],
)
def test_a_real_manifest_run_binding_is_closed_and_non_blank(mutate, tmp_path):
    """The real shape's field closure and blank-identity refusal, not only the fixture's.

    Every existing manifest-run-binding refusal test above is built from
    `_resealed_manifest`, which is always fixture-shaped: only the one green
    `submission_id` test (`test_a_projection_may_carry_a_submission_identity...`)
    ever reached `_MANIFEST_RUN_FIELDS_REAL`'s exact-field closure or the
    `subject = "submission"` blank-identity refusal, so a mutation deleting
    either would have survived undetected.
    """
    with pytest.raises(
        SchemaRefusal,
        match="non-blank submission and scenario identities|unrecognized field set",
    ):
        verify_delivered_bundle(_real_resealed_manifest(mutate), tmp_path / "delivered")


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda manifest: manifest.update(independent_audit="passed"),
            id="a claim nothing in this package measures",
        ),
        pytest.param(
            lambda manifest: manifest.update(verification={"search_fold": {"status": "verified"}}),
            id="a forged report of checks nobody ran",
        ),
        pytest.param(
            lambda manifest: manifest["claims"].update(accuracy="99.9% against the ink"),
            id="a fabricated claims entry",
        ),
        pytest.param(
            lambda manifest: manifest["run"].update(operator="nobody"),
            id="an extra field on the run binding",
        ),
        pytest.param(
            lambda manifest: manifest["claims"]["pixels"].update(verified_by="nobody"),
            id="an extra field on a claim read field by field",
        ),
        pytest.param(
            lambda manifest: manifest["claims"]["act_partition"].update(
                denominator="whatever the editor chose to count"
            ),
            id="a substituted act denominator",
        ),
        pytest.param(
            lambda manifest: manifest["claims"]["page_census"].update(
                denominator="whatever the editor chose to count"
            ),
            id="a substituted page denominator",
        ),
        pytest.param(
            lambda manifest: manifest["claims"]["display"].update(
                reason="the rendering was exercised and approved"
            ),
            id="a substituted display rationale",
        ),
        pytest.param(
            lambda manifest: manifest["claims"]["salvage"].update(
                promotion="automatic export-time act promotion"
            ),
            id="a substituted salvage promotion claim",
        ),
    ],
)
def test_a_resealed_manifest_may_not_carry_a_field_this_build_never_writes(mutate, tmp_path):
    """The package's claims document cannot carry a field nothing measured."""
    with pytest.raises(
        SchemaRefusal,
        match="unrecognized field set|not this build's fixed claim|display claim",
    ):
        verify_delivered_bundle(_resealed_manifest(mutate), tmp_path / "delivered")


def test_an_unhashable_salvage_status_is_a_named_refusal(tmp_path):
    """Package-supplied JSON values may not escape the verifier as Python errors."""

    def replace_status(manifest):
        manifest["claims"]["salvage"]["status"] = ["accounted"]

    with pytest.raises(SchemaRefusal, match="invalid salvage-tier status"):
        verify_delivered_bundle(_resealed_manifest(replace_status), tmp_path / "delivered")


@pytest.mark.parametrize("field", ["fixture_id", "scenario"])
def test_a_manifest_run_binding_requires_string_identities(field, tmp_path):
    """An exact key set is not a schema if package-supplied identity types are unchecked."""

    def replace_identity(manifest):
        manifest["run"][field] = ["not", "an", "identity"]

    with pytest.raises(SchemaRefusal, match="non-blank fixture and scenario identities"):
        verify_delivered_bundle(_resealed_manifest(replace_identity), tmp_path / "delivered")


def test_a_projection_may_carry_a_submission_identity_instead_of_a_fixture_one(tmp_path):
    """A real submission's identity projects and verifies exactly like a fixture's.

    Never the same field: `fixture_id` is `None` on this projection, and the
    manifest's `run` block names `submission_id` instead, never both.
    """
    projection = replace(_projection(), fixture_id=None, submission_id="a" * 64)
    bundle = build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)
    manifest = json.loads(_members(bundle.data)[EXPORT_MANIFEST_NAME])

    assert manifest["run"] == {
        "submission_id": "a" * 64,
        "scenario": projection.scenario,
        "config_digest": projection.config_digest,
    }
    verify_delivered_bundle(bundle.data, tmp_path / "delivered")


def test_a_projection_with_no_run_identity_at_all_is_refused(tmp_path):
    """Neither identity is not a legal projection, whatever else it carries."""
    projection = replace(_projection(), fixture_id=None)
    with pytest.raises(SchemaRefusal, match="neither a fixture identifier nor a submission"):
        build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)


def test_a_projection_with_both_run_identities_is_refused(tmp_path):
    """One field per concept: a projection may not name a fixture and a submission."""
    projection = replace(_projection(), submission_id="a" * 64)
    with pytest.raises(SchemaRefusal, match="both a fixture identifier and a submission"):
        build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)


def test_a_projection_with_a_non_sha256_submission_id_is_refused(tmp_path):
    """The projection boundary is at least as strict as `submission_identity` itself.

    The manifest-boundary twin of this check is
    `test_a_manifest_run_binding_naming_a_non_sha256_submission_is_refused`; this
    pins the other end -- `_validate_projection`'s own `_require_sha256` call --
    which nothing had reached before, since every other test's `submission_id`
    is a well-formed `"a" * 64`.
    """
    projection = replace(_projection(), fixture_id=None, submission_id="not-a-lowercase-sha256")
    with pytest.raises(
        SchemaRefusal, match="projection submission identity is not a lowercase sha256"
    ):
        build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)


def test_a_manifest_run_binding_naming_both_identities_is_refused(tmp_path):
    """A resealed manifest cannot smuggle both identity fields past the closure check."""

    def add_submission(manifest):
        manifest["run"]["submission_id"] = "a" * 64

    with pytest.raises(SchemaRefusal, match="both a fixture identifier and a submission"):
        verify_delivered_bundle(_resealed_manifest(add_submission), tmp_path / "delivered")


def test_a_manifest_run_binding_naming_neither_identity_is_refused(tmp_path):
    """A resealed manifest cannot drop its run identity entirely and still verify."""

    def drop_fixture(manifest):
        del manifest["run"]["fixture_id"]

    with pytest.raises(SchemaRefusal, match="neither a fixture identifier nor a submission"):
        verify_delivered_bundle(_resealed_manifest(drop_fixture), tmp_path / "delivered")


def test_a_manifest_run_binding_naming_a_non_sha256_submission_is_refused(tmp_path):
    """The manifest boundary is at least as strict as the projection boundary.

    Built under the real shape rather than mutating a fixture-shaped manifest --
    the corrupted field only exists on the `submission_id` branch -- then
    corrupted to a well-formed-but-not-sha256 string.
    """
    projection = replace(_projection(), fixture_id=None, submission_id="a" * 64)
    bundle = build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    del manifest["self_hash"]
    manifest["run"]["submission_id"] = "not-a-lowercase-sha256"
    _refresh_manifest(members, manifest)

    with pytest.raises(SchemaRefusal, match="submission identity is not a lowercase sha256"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path / "delivered")


def _armarium_run_module():
    """Load `run.py` under a private name, mirroring `test_export.py`'s idiom.

    Never a bare ``import run``: several stage directories define a module by
    that name, and the import cache would decide which one this test got.
    """
    spec = importlib.util.spec_from_file_location("armarium_run_under_test_identity", ARMARIUM_CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_run_identity_never_touches_the_refusing_fixture_accessor_on_a_real_run():
    """The unit's central claim, pinned rather than asserted only in prose and HANDOFF.md.

    `StageContext.fixture` refuses on a real run (`common/stage.py`); if
    `export_run_identity` read it unconditionally instead of deciding the route
    from `submission_identity(context.run)` first, this would raise a
    `ContractError` instead of returning a `submission_id`-shaped identity.
    """
    armarium = _armarium_run_module()
    run = {
        "ingress": real_ingress_record(),
        "source_manifest": [{"ledger_sha256": "a" * 64}],
    }
    context = StageContext(
        tree=None,
        run=run,
        fixture=None,
        scenario=REAL_SCENARIO,
        stage=ARMARIUM,
        adapter_revision="adapter-under-test",
        args=None,
        registry=None,
    )

    submission_id, fixture_id, run_identity = armarium.export_run_identity(context)

    assert submission_id == "a" * 64
    assert fixture_id is None
    assert run_identity == {"submission_id": "a" * 64}


def test_export_run_identity_reads_the_declared_fixture_id_on_a_fixture_run():
    """The fixture route is unchanged: the identity is the loaded declaration's own."""
    armarium = _armarium_run_module()
    context = StageContext(
        tree=None,
        run={},
        fixture={"fixture_id": "armarium-export-test-v1"},
        scenario="happy",
        stage=ARMARIUM,
        adapter_revision="adapter-under-test",
        args=None,
        registry=None,
    )

    submission_id, fixture_id, run_identity = armarium.export_run_identity(context)

    assert submission_id is None
    assert fixture_id == "armarium-export-test-v1"
    assert run_identity == {"fixture_id": "armarium-export-test-v1"}


def test_a_source_graph_evidence_ref_that_cites_nothing_is_refused_in_every_format_set(tmp_path):
    """sources.json is the citation carrier shared by every format selection."""
    projection = _projection()
    decoyed = {**projection.acts[0], "evidence_refs": [{"note": "evidence exists somewhere"}]}

    with pytest.raises(SchemaRefusal, match="evidence_refs entry cites nothing"):
        build_armarium_bundle(
            replace(projection, acts=(decoyed, *projection.acts[1:])),
            ArmariumFormats(("text-bundle",), False),
            _source_bytes,
        )


@pytest.mark.parametrize("member", ["review-items.jsonl", "acts.jsonl"])
def test_an_evidence_ref_that_cites_nothing_is_refused(member, tmp_path):
    """Every entry in the required evidence list must cite a retained-run path."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    rows = [json.loads(line) for line in members[member].decode("utf-8").splitlines()]
    rows[0]["evidence_refs"] = [{"note": "trust me, evidence exists somewhere"}]
    members[member] = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    _refresh_manifest_member(members, member)

    with pytest.raises(SchemaRefusal, match="evidence_refs entry cites nothing"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path / "delivered")


def test_an_acts_database_evidence_ref_that_cites_nothing_is_refused(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    database = tmp_path / "decoyed.sqlite"
    database.write_bytes(members["acts.sqlite"])
    connection = sqlite3.connect(database)
    try:
        evidence = json.loads(
            connection.execute("SELECT evidence_json FROM acts WHERE act_id = 'act-1'").fetchone()[
                0
            ]
        )
        evidence["evidence_refs"] = [{"note": "trust me, evidence exists somewhere"}]
        connection.execute(
            "UPDATE acts SET evidence_json = ? WHERE act_id = 'act-1'",
            (json.dumps(evidence),),
        )
        connection.commit()
    finally:
        connection.close()
    members["acts.sqlite"] = database.read_bytes()
    _refresh_manifest_member(members, "acts.sqlite")

    with pytest.raises(SchemaRefusal, match="evidence_refs entry cites nothing"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path / "delivered")


def test_text_bundle_human_heading_must_authenticate_the_machine_act_identity(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    lines[lines.index("act-id: act-1") - 1] = "## forged key (act-1)"
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="human heading"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path / "delivered")


def test_member_digest_guard_refuses_a_tampered_self_containment_claim(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    members["sources.json"] += b"tampered"

    with pytest.raises(SchemaRefusal, match="does not match its manifest digest"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_member_byte_count_guard_refuses_a_self_consistent_false_size_claim(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    row = next(item for item in manifest["members"] if item["path"] == "sources.json")
    row["bytes"] += 1
    manifest["self_hash"] = self_hash(manifest)
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)

    with pytest.raises(SchemaRefusal, match="manifest byte count"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_selected_format_cannot_omit_a_self_consistent_member_inventory(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    members.pop(TEXT_REGISTER)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["members"] = [item for item in manifest["members"] if item["path"] != TEXT_REGISTER]
    manifest["self_hash"] = self_hash(manifest)
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)

    with pytest.raises(SchemaRefusal, match="selected formats.*missing"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_unselected_format_members_cannot_hide_inside_a_self_consistent_bundle(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["formats"]["formats"] = ["jsonl"]
    # Kept consistent with the tamper above so this test isolates the format-hiding
    # check under test rather than tripping the (correct) canonical-text identity
    # mismatch a single selected literal format now produces.
    manifest["canonical_text"]["identity_verified_across"] = []
    manifest["claims"]["uncertainty"]["carried_by"] = ["jsonl"]
    manifest["claims"]["transcription_annotations"]["carried_by"] = ["jsonl"]
    manifest["self_hash"] = self_hash(manifest)
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)

    with pytest.raises(SchemaRefusal, match="selected formats.*unexpected"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


@pytest.mark.parametrize("embed_pixels", [False, True])
def test_sealed_source_page_cannot_lose_its_pixel_reference(embed_pixels, tmp_path):
    bundle = build_armarium_bundle(
        _projection(), _formats(embed_pixels=embed_pixels), _source_bytes
    )
    members = _members(bundle.data)
    sources = json.loads(members["sources.json"])
    sources["pages"][0].pop("page_image")
    members["sources.json"] = canonical_bytes(sources)
    _refresh_manifest_member(members, "sources.json")

    with pytest.raises(SchemaRefusal, match="sealed package source page has no pixel reference"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_sealed_source_page_cannot_carry_a_resealed_refusal_reason(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    sources = json.loads(members["sources.json"])
    sources["pages"][0]["reason"] = "forged refusal despite a sealed page"
    members["sources.json"] = canonical_bytes(sources)
    _refresh_manifest_member(members, "sources.json")

    with pytest.raises(SchemaRefusal, match="sealed package source page carries a refusal reason"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_refused_source_page_requires_a_nonblank_terminal_reason(tmp_path):
    """Whitespace does not name why a source failed to seal or what remains unresolved."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    sources = json.loads(members["sources.json"])
    page = sources["pages"][0]
    page["outcome"] = "refused"
    page["reason"] = "   "
    page.pop("page_image")
    members["sources.json"] = canonical_bytes(sources)
    _refresh_manifest_member(members, "sources.json")

    with pytest.raises(SchemaRefusal, match="refused package source page has no terminal reason"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_selected_products_cannot_omit_an_act_the_manifest_claims(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    held = next(
        row
        for row in manifest["claims"]["act_partition"]["categories"]
        if row["category"] == "held-for-review"
    )
    held["act_ids"].append("act-not-in-products")
    held["count"] += 1
    manifest["claims"]["act_partition"]["expected_count"] += 1
    manifest["claims"]["act_partition"]["counted"] += 1
    manifest["aggregate"]["by_category"]["held-for-review"] += 1
    manifest["self_hash"] = self_hash(manifest)
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)

    with pytest.raises(SchemaRefusal, match="act-key partition|acts database does not reconcile"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda manifest: manifest["claims"].update(status="complete"), "status does not match"),
        (lambda manifest: manifest["claims"].update(partial_reasons=[]), "status does not match"),
        (
            lambda manifest: manifest["claims"]["submission_inventory"].update(status="reconciled"),
            "misstates what its submission denominator covers",
        ),
        (
            lambda manifest: manifest["claims"]["terminal_ledger"].update(status="complete"),
            "terminal ledger does not match",
        ),
        (
            lambda manifest: manifest["claims"]["terminal_ledger"]["units"].pop(),
            "terminal ledger does not match",
        ),
        (
            lambda manifest: manifest["claims"]["terminal_ledger"]["by_category"].update(
                {"held-for-review": 0}
            ),
            "terminal ledger does not match",
        ),
        (
            lambda manifest: manifest["aggregate"].update(status="complete", reasons=[]),
            "aggregate claims complete",
        ),
        (
            lambda manifest: manifest["aggregate"].update(reasons=["a different partial reason"]),
            "aggregate does not match",
        ),
    ],
)
def test_self_hashed_bundle_cannot_claim_unmeasured_completeness(mutate, match, tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    mutate(manifest)
    _refresh_manifest(members, manifest)

    with pytest.raises(SchemaRefusal, match=match):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.update(witness_chairs=["invented-witness"]),
        lambda manifest: manifest.update(witness_floor=0),
    ],
)
def test_manifest_witness_claims_cannot_drift_from_the_accounting_source(mutate, tmp_path):
    members = _members(
        build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes).data
    )
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    mutate(manifest)
    _refresh_manifest(members, manifest)

    with pytest.raises(SchemaRefusal, match="witness roster disagrees"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_text_bundle_cannot_lose_its_page_and_hash_citation(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").splitlines()
    members[TEXT_REGISTER] = (
        "\n".join(
            line for line in lines if not line.startswith(("source-page: ", "source-sha256: "))
        )
        + "\n"
    ).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="literal identity or hash"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("provenance", None, "delivered acts JSONL row has no provenance"),
        ("source_regions", [], "delivered acts JSONL row has no source-region provenance"),
    ],
)
def test_jsonl_cannot_silently_drop_delivered_provenance(field, value, message, tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    records = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    records[0][field] = value
    members["acts.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in records)
    _refresh_manifest_member(members, "acts.jsonl")

    with pytest.raises(SchemaRefusal, match=message):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_jsonl_cannot_silently_drop_delivered_witness_evidence(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    records = [json.loads(line) for line in members["acts.jsonl"].decode().splitlines()]
    records[0]["witnesses"] = []
    members["acts.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in records)
    _refresh_manifest_member(members, "acts.jsonl")

    with pytest.raises(SchemaRefusal, match="exact delivered provenance"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_selected_formats_retain_every_delivered_region_and_exact_provenance(tmp_path):
    bundle = build_armarium_bundle(
        _two_region_projection(), _formats(embed_pixels=False), _source_bytes
    )
    members = _members(bundle.data)
    records = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    records[0]["source_regions"] = records[0]["source_regions"][:1]
    members["acts.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in records)
    _refresh_manifest_member(members, "acts.jsonl")
    with pytest.raises(SchemaRefusal, match="exact delivered provenance"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "jsonl")

    members = _members(bundle.data)
    records = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    records[0]["provenance"] = {"chair": "a different nonempty provenance"}
    members["acts.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in records)
    _refresh_manifest_member(members, "acts.jsonl")
    with pytest.raises(SchemaRefusal, match="exact delivered provenance"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "provenance")

    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").splitlines()
    removed_second = False
    retained: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].startswith("source-page: ") and not removed_second:
            removed_second = True
            index += 2
            continue
        retained.append(lines[index])
        index += 1
    members[TEXT_REGISTER] = ("\n".join(retained) + "\n").encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)
    with pytest.raises(SchemaRefusal, match="every delivered source citation"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "text")


def test_review_items_cannot_replace_a_recorded_terminal_reason(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    review = [
        json.loads(line) for line in members["review-items.jsonl"].decode("utf-8").splitlines()
    ]
    review[0]["reason"] = "a fabricated but nonempty review reason"
    members["review-items.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in review)
    _refresh_manifest_member(members, "review-items.jsonl")

    with pytest.raises(SchemaRefusal, match="exact terminal reason"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_manifest_cannot_replace_the_source_accounting_basis(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["aggregate_basis"]["unaddressed_chairs"] = ["invented-chair"]
    _refresh_manifest(members, manifest)

    with pytest.raises(SchemaRefusal, match="basis disagrees with its source accounting"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_manifest_member_inventory_cannot_repeat_a_self_hashed_row(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["members"].append(dict(manifest["members"][0]))
    _refresh_manifest(members, manifest)

    with pytest.raises(SchemaRefusal, match="repeats or inventories"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_text_bundle_keeps_every_cited_source_folder_when_no_act_is_delivered(tmp_path):
    original = _projection()
    held = []
    for act in original.acts:
        record = dict(act)
        record.update(
            category="held-for-review",
            canonical_clean_text=None,
            uncertainty=None,
            text_status=None,
            transcription_annotations=None,
            provenance=None,
            source_regions=[],
        )
        held.append(record)
    bundle = build_armarium_bundle(
        replace(
            original,
            acts=tuple(held),
            aggregate_basis={**original.aggregate_basis, "act_text_status": {}},
            aggregate={
                "status": "partial",
                "reasons": ["act one is held-for-review", "act two is held-for-review"],
                "by_category": {"held-for-review": 2},
                "by_page_outcome": {"sealed": 1},
            },
        ),
        _formats(embed_pixels=False),
        _source_bytes,
    )

    with ZipFile(BytesIO(bundle.data)) as archive:
        text = archive.read(TEXT_REGISTER).decode("utf-8")
    assert "source folder: register" in text
    assert "canonical_clean_text:" not in text
    verify_export_bundle(bundle.data, tmp_path / "clean")


def test_source_root_and_a_named_source_root_folder_cannot_collide(tmp_path):
    original = _projection()
    source = _source_bytes("1_exemplar/blobs/sha256/page")
    pages = (
        {
            "ordinal": 1,
            "outcome": "sealed",
            "reason": "",
            "declared_path": "root-folio.png",
            "declared_sha256": digest_bytes(source),
            "page_id": "pg-root",
            "image_path": "1_exemplar/blobs/sha256/page",
            "image_sha256": digest_bytes(source),
        },
        {
            "ordinal": 2,
            "outcome": "sealed",
            "reason": "",
            "declared_path": "_source_root/named-folio.png",
            "declared_sha256": digest_bytes(source),
            "page_id": "pg-named",
            "image_path": "1_exemplar/blobs/sha256/page",
            "image_sha256": digest_bytes(source),
        },
    )
    held = tuple(
        {
            **act,
            "category": "held-for-review",
            "canonical_clean_text": None,
            "uncertainty": None,
            "text_status": None,
            "transcription_annotations": None,
            "provenance": None,
            "source_regions": [],
        }
        for act in original.acts
    )
    ink_map_pages = (_mapped_page(1), _mapped_page(2))
    source_manifest = (
        {"ordinal": 1, "relative_path": "root-folio.png", "sha256": digest_bytes(source)},
        {
            "ordinal": 2,
            "relative_path": "_source_root/named-folio.png",
            "sha256": digest_bytes(source),
        },
    )
    bundle = build_armarium_bundle(
        replace(
            original,
            acts=held,
            pages=pages,
            ink_map_pages=ink_map_pages,
            source_manifest=source_manifest,
            aggregate={
                "status": "partial",
                "reasons": ["act one is held-for-review", "act two is held-for-review"],
                "by_category": {"held-for-review": 2},
                "by_page_outcome": {"sealed": 2},
            },
            aggregate_basis={
                **original.aggregate_basis,
                "act_pages": {"one": [1, 2], "two": [1, 2]},
                "act_text_status": {},
            },
        ),
        _formats(embed_pixels=False),
        _source_bytes,
    )

    with ZipFile(BytesIO(bundle.data)) as archive:
        assert {
            "text/_source_root/readings.txt",
            "text/_source_folder/_source_root/readings.txt",
        } <= set(archive.namelist())
    verify_export_bundle(bundle.data, tmp_path / "clean")


def test_pixel_claim_cannot_overstate_reference_only_clean_machine_verification(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["claims"]["pixels"]["resolution_claim"] = "pixels resolve everywhere"
    manifest["self_hash"] = self_hash(manifest)
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)

    with pytest.raises(SchemaRefusal, match="pixel-resolution claim"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


@pytest.mark.parametrize(
    "projection",
    [
        lambda original: replace(original, config_digest="g" * 64),
        lambda original: replace(
            original,
            pages=({**original.pages[0], "image_sha256": "g" * 64},),
        ),
        lambda original: replace(
            original,
            source_manifest=({**original.source_manifest[0], "sha256": "g" * 64},),
        ),
    ],
)
def test_export_refuses_non_sha256_citations_before_packaging(projection):
    with pytest.raises(SchemaRefusal, match="lowercase sha256"):
        build_armarium_bundle(
            projection(_projection()), _formats(embed_pixels=False), _source_bytes
        )


def test_clean_verifier_refuses_a_self_consistent_non_sha256_source_reference(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    sources = json.loads(members["sources.json"])
    sources["pages"][0]["page_image"]["sha256"] = "g" * 64
    members["sources.json"] = canonical_bytes(sources)
    _refresh_manifest_member(members, "sources.json")

    with pytest.raises(SchemaRefusal, match="lowercase sha256"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_product_marks_retained_run_evidence_references_and_refuses_an_unmarked_one(tmp_path):
    original = _projection()
    raw_reference = {
        "relative_path": "4_perlector/artifacts/perlectio/art_123.json",
        "sha256": "a" * 64,
    }
    decorated_reference = {**raw_reference, "artifact_id": "perlectio-art-123"}
    delivered = {
        **original.acts[0],
        "perlectio_ref": decorated_reference,
        "evidence_refs": [raw_reference],
        "witnesses": [
            {
                "chair": "attestator_1",
                "outcome": "read",
                "testimonium_ref": raw_reference,
                "provenance": {"receipt_ref": raw_reference},
            }
        ],
    }
    bundle = build_armarium_bundle(
        replace(original, acts=(delivered, original.acts[1])),
        _formats(embed_pixels=False),
        _source_bytes,
    )
    members = _members(bundle.data)
    rows = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    row = next(record for record in rows if record["act_id"] == "act-1")
    assert row["perlectio_ref"] == {
        "availability": "requires-retained-run-access",
        "run_relative_path": raw_reference["relative_path"],
        "sha256": raw_reference["sha256"],
        "artifact_id": "perlectio-art-123",
    }
    assert row["witnesses"][0]["testimonium_ref"]["availability"] == (
        "requires-retained-run-access"
    )

    row["perlectio_ref"] = decorated_reference
    members["acts.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in rows)
    _refresh_manifest_member(members, "acts.jsonl")
    with pytest.raises(SchemaRefusal, match="retained-run availability"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")

    row["perlectio_ref"] = {
        "run_relative_path": raw_reference["relative_path"],
        "sha256": raw_reference["sha256"],
    }
    members["acts.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in rows)
    _refresh_manifest_member(members, "acts.jsonl")
    with pytest.raises(SchemaRefusal, match="honest availability"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_excluded_act_requires_and_carries_its_approval_reference(tmp_path):
    original = _projection()
    missing_approval = {
        **original.acts[1],
        "category": "excluded-with-approval",
        "canonical_clean_text": None,
    }
    with pytest.raises(ApprovalRefusal, match="approval-record reference"):
        build_armarium_bundle(
            replace(original, acts=(original.acts[0], missing_approval)),
            _formats(embed_pixels=False),
            _source_bytes,
        )

    excluded = {**missing_approval, "approval_ref": "art_0123456789abcdef"}
    bundle = build_armarium_bundle(
        replace(
            original,
            acts=(original.acts[0], excluded),
            aggregate={
                "status": "complete",
                "reasons": [],
                "by_category": {"delivered": 1, "excluded-with-approval": 1},
                "by_page_outcome": {"sealed": 1},
            },
        ),
        _formats(embed_pixels=False),
        _source_bytes,
    )
    root = tmp_path / "clean"
    verify_export_bundle(bundle.data, root)
    # `encoding="utf-8"` explicitly: `read_text()` without it decodes under the
    # locale, and the bundle is written as UTF-8 by `_jsonl_bytes`. A machine
    # whose locale is not UTF-8 would decode a published product's own bytes
    # differently from the machine that wrote them — the same environment
    # dependence this branch already carries in its sealed bundle identity.
    # Found by CodeRabbit.
    rows = [
        json.loads(line) for line in (root / "acts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert next(row for row in rows if row["act_id"] == "act-2")["approval_ref"] == (
        "art_0123456789abcdef"
    )
    connection = sqlite3.connect(root / "acts.sqlite")
    try:
        assert connection.execute(
            "SELECT approval_ref FROM acts WHERE act_id='act-2'"
        ).fetchone() == ("art_0123456789abcdef",)
    finally:
        connection.close()


def test_database_keeps_literal_and_derived_search_layers_separate(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    root = tmp_path / "clean"
    manifest = verify_export_bundle(bundle.data, root)
    connection = sqlite3.connect(root / "acts.sqlite")
    try:
        literal, literal_hash = connection.execute(
            "SELECT canonical_clean_text, canonical_text_sha256 FROM acts WHERE act_id='act-1'"
        ).fetchone()
        derived, revision, derived_from = connection.execute(
            """
            SELECT derived_search_text, normalizer_revision, derived_from_canonical_sha256
            FROM act_search WHERE act_id='act-1'
            """
        ).fetchone()
        matched = connection.execute(
            "SELECT act_id FROM acts_fts JOIN act_search ON acts_fts.rowid=act_search.rowid "
            "WHERE acts_fts MATCH 'caesar'"
        ).fetchall()
    finally:
        connection.close()

    assert literal == "Cǣsar d’Amours"
    assert literal_hash == canonical_text_sha256(literal)
    assert derived == search_fold(literal)
    assert revision == TEXTNORM_REVISION
    assert derived_from == literal_hash
    assert matched == [("act-1",)]
    assert manifest["verification"]["search_fold"] == {
        "status": "verified",
        "recorded_unidata_version": unicodedata.unidata_version,
        "verifier_unidata_version": unicodedata.unidata_version,
        "statement": "search folds recomputed with the recorded Unicode database version",
    }


def test_a_different_unicode_database_records_an_honest_search_fold_skip(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    scratch = tmp_path / "acts.sqlite"
    scratch.write_bytes(members["acts.sqlite"])
    connection = sqlite3.connect(scratch)
    try:
        connection.execute(
            "UPDATE export_metadata SET value = 'different-test-version' "
            "WHERE key = 'unidata_version'"
        )
        connection.commit()
    finally:
        connection.close()
    members["acts.sqlite"] = scratch.read_bytes()
    _refresh_manifest_member(members, "acts.sqlite")

    manifest = verify_export_bundle(_zip_bytes(members), tmp_path / "clean")

    assert manifest["verification"]["search_fold"] == {
        "status": "not-run-unicode-database-mismatch",
        "recorded_unidata_version": "different-test-version",
        "verifier_unidata_version": unicodedata.unidata_version,
        "statement": (
            "search-fold recomputation was not run because the package and verifier "
            "use different Unicode database versions"
        ),
    }


def test_a_self_consistent_but_falsified_search_fold_column_is_refused(tmp_path):
    """A digest and a self-hash prove the package was not edited after sealing. Neither
    proves `act_search.derived_search_text` was ever a fold of its own act's literal.
    The package records this interpreter's own Unicode database version, so this
    exercises the same-version recomputation path rather than the honest skip.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    scratch = tmp_path / "acts.sqlite"
    scratch.write_bytes(members["acts.sqlite"])
    connection = sqlite3.connect(scratch)
    try:
        falsified = "not a fold of anything"
        connection.execute(
            "UPDATE act_search SET derived_search_text = ?, derived_text_sha256 = ?",
            (falsified, canonical_text_sha256(falsified)),
        )
        connection.commit()
    finally:
        connection.close()
    members["acts.sqlite"] = scratch.read_bytes()
    _refresh_manifest_member(members, "acts.sqlite")

    with pytest.raises(SchemaRefusal, match="not a fold of its act's literal"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_search_fold_row_dropped_for_a_delivered_act_is_refused(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    scratch = tmp_path / "acts.sqlite"
    scratch.write_bytes(members["acts.sqlite"])
    connection = sqlite3.connect(scratch)
    try:
        connection.execute("DELETE FROM act_search WHERE act_id='act-1'")
        connection.commit()
    finally:
        connection.close()
    members["acts.sqlite"] = scratch.read_bytes()
    _refresh_manifest_member(members, "acts.sqlite")

    with pytest.raises(SchemaRefusal, match="does not cover exactly the delivered literals"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_embedded_page_and_crop_pixels_open_on_a_clean_machine(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=True), _source_bytes)

    with ZipFile(BytesIO(bundle.data)) as archive:
        assert {"pixels/pages/1.img", "pixels/crops/rgn-1.img"} <= set(archive.namelist())
    manifest = verify_export_bundle(bundle.data, tmp_path / "clean")
    assert manifest["claims"]["pixels"]["embedded"] is True


def test_salvage_stays_out_of_every_act_projection(tmp_path):
    salvage_content = "marginal material, not an established act"
    projection = _projection(salvage_items=(_salvage_item(salvage_content),))
    bundle = build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)
    with ZipFile(BytesIO(bundle.data)) as archive:
        assert salvage_content in archive.read("salvage/items.jsonl").decode("utf-8")
        assert salvage_content not in archive.read("acts.jsonl").decode("utf-8")
        assert salvage_content not in archive.read(TEXT_REGISTER).decode("utf-8")
        assert salvage_content.encode("utf-8") not in archive.read("acts.sqlite")

    leaked = replace(
        projection,
        salvage_items=(
            {
                **_salvage_item(salvage_content),
                "act_id": "act-1",
            },
        ),
    )
    with pytest.raises(SchemaRefusal, match="acts namespace"):
        build_armarium_bundle(leaked, _formats(embed_pixels=False), _source_bytes)

    region_leak = replace(
        projection,
        salvage_items=(
            {
                **_salvage_item(salvage_content),
                "source_regions": [
                    {
                        **_salvage_item(salvage_content)["source_regions"][0],
                        "canonical_clean_text": "not-an-act",
                    }
                ],
            },
        ),
    )
    with pytest.raises(SchemaRefusal, match="salvage-tier .*reaches into the acts namespace"):
        build_armarium_bundle(region_leak, _formats(embed_pixels=False), _source_bytes)

    false_page_binding = replace(
        projection,
        salvage_items=(
            {
                **_salvage_item(salvage_content),
                "source_regions": [
                    {
                        **_salvage_item(salvage_content)["source_regions"][0],
                        "declared_path": "other-folio.png",
                    }
                ],
            },
        ),
    )
    with pytest.raises(SchemaRefusal, match="disagrees with its cited source page"):
        build_armarium_bundle(false_page_binding, _formats(embed_pixels=False), _source_bytes)

    nested_provenance_leak = replace(
        projection,
        salvage_items=(
            {
                **_salvage_item(salvage_content),
                "provenance": {
                    "collection": "separate tier",
                    "act_id": "act-1",
                    "canonical_clean_text": "purported act text",
                },
            },
        ),
    )
    with pytest.raises(SchemaRefusal, match="salvage-tier item reaches into the acts namespace"):
        build_armarium_bundle(nested_provenance_leak, _formats(embed_pixels=False), _source_bytes)


def test_nonempty_salvage_inventory_requires_the_salvage_tier_format():
    projection = _projection(salvage_items=(_salvage_item("marginal material"),))
    formats = ArmariumFormats(
        ("text-bundle", "acts-database", "jsonl", "review-items"),
        False,
    )

    with pytest.raises(SchemaRefusal, match="non-empty sealed salvage inventory requires"):
        build_armarium_bundle(projection, formats, _source_bytes)


def test_salvage_requires_cited_ink_and_collection_provenance():
    content = "marginal material, not an established act"
    missing_regions = {**_salvage_item(content), "source_regions": []}
    missing_provenance = {**_salvage_item(content), "provenance": {}}
    for item, message in (
        (missing_regions, "source-region provenance"),
        (missing_provenance, "collection provenance"),
    ):
        with pytest.raises(SchemaRefusal, match=message):
            build_armarium_bundle(
                _projection(salvage_items=(item,)), _formats(embed_pixels=False), _source_bytes
            )


def test_missing_sealed_salvage_inventory_is_visible_not_an_invented_zero(tmp_path):
    bundle = build_armarium_bundle(
        replace(_projection(), salvage_items=None), _formats(embed_pixels=False), _source_bytes
    )
    manifest = verify_export_bundle(bundle.data, tmp_path / "clean")
    assert manifest["claims"]["salvage"] == {
        "namespace": "salvage",
        "status": "not-produced-no-sealed-salvage-inventory",
        "count": None,
        "reason": "this run has no sealed salvage inventory to account for",
        "promotion": "recorded approval then pipeline re-entry; never export-time act promotion",
    }


def test_bundle_bytes_are_deterministic_for_the_same_sealed_projection():
    first = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    second = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    assert first.data == second.data


def test_the_terminal_ledger_partitions_sources_pages_and_acts_totally(tmp_path):
    """Spec 11 test 1: a total partition, not an act-only one.

    One submitted source, one sealed page and two acts is four units, every one of
    them carrying a closed category. The counts are checked to sum because a partition
    that misses a unit is invariant #10's imbalance and its failure mode is silence.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    ledger = json.loads(_members(bundle.data)[EXPORT_MANIFEST_NAME])["claims"]["terminal_ledger"]

    assert ledger["by_unit_type"] == {"source": 1, "page": 1, "act": 2}
    assert ledger["unit_count"] == 4 == sum(ledger["by_category"].values())
    assert {unit["unit_id"] for unit in ledger["units"]} == {
        "source:1",
        "page:1",
        "act:act-1",
        "act:act-2",
    }
    # The page delivered an act, so its source did too; the held act is the only
    # unresolved unit and it is named rather than counted.
    assert {unit["unit_id"]: unit["category"] for unit in ledger["units"]} == {
        "source:1": "delivered",
        "page:1": "delivered",
        "act:act-1": "delivered",
        "act:act-2": "held-for-review",
    }
    assert ledger["status"] == "partial"
    assert ledger["unresolved_reasons"][0].startswith("act act-2 is held-for-review")
    assert "one unit per page or frame" in ledger["granularity_limit"]


def test_page_ledger_category_inherits_confirmed_blank_and_excluded_when_every_act_agrees():
    """The two page-category branches spec 11 test 1 names besides delivered/held.

    Driven against the pure function because nothing upstream emits either outcome
    yet, so a full-projection fixture would be synthetic in exactly the same way this
    call is. Neither category is ever *inferred*: both are inherited only when every
    act cut from the page already carries it.
    """
    assert _page_ledger_category(1, ["confirmed-blank"]) == (
        ArmariumCategory.CONFIRMED_BLANK.value,
        None,
    )
    assert _page_ledger_category(2, ["confirmed-blank", "confirmed-blank"]) == (
        ArmariumCategory.CONFIRMED_BLANK.value,
        None,
    )
    assert _page_ledger_category(3, ["excluded-with-approval"]) == (
        ArmariumCategory.EXCLUDED_WITH_APPROVAL.value,
        None,
    )
    # A mix with no delivered act present is neither category on its own -- held
    # for a human, exactly like any other non-unanimous, non-delivered mix.
    category, reason = _page_ledger_category(4, ["confirmed-blank", "excluded-with-approval"])
    assert category == ArmariumCategory.HELD_FOR_REVIEW.value
    assert "delivered no act" in reason


def test_a_held_page_makes_the_bundle_partial_where_the_run_aggregate_reconciles(tmp_path):
    """The one state where the ledger and the run aggregate disagree, built whole.

    Every act reaches a completed category, so `run_aggregate` -- which counts acts,
    chairs and page outcomes -- reconciles to `complete` with no reason at all. The
    ledger also accounts the *page*, and a sealed page whose acts agree on nothing is
    held for a human. `run.py` reports the ledger's status for exactly this case:
    reporting the aggregate's would exit 0 and record an `export` outcome of
    `delivered` over a bundle whose own face said `partial` and named the held page.
    """
    original = _projection()
    acts = (
        {
            **original.acts[0],
            "category": ArmariumCategory.CONFIRMED_BLANK.value,
            "canonical_clean_text": None,
            "uncertainty": None,
            "text_status": None,
            "transcription_annotations": None,
            "provenance": None,
            "source_regions": [],
            "reason": None,
        },
        {
            **original.acts[1],
            "category": ArmariumCategory.EXCLUDED_WITH_APPROVAL.value,
            "reason": None,
            "approval_ref": "approvals/exclusion-1",
        },
    )
    aggregate = run_aggregate(
        {
            "one": ArmariumCategory.CONFIRMED_BLANK,
            "two": ArmariumCategory.EXCLUDED_WITH_APPROVAL,
        },
        original.aggregate_basis["coverage_records"],
        {1: dict(original.pages[0])},
        unaddressed_chairs=[],
        act_pages=original.aggregate_basis["act_pages"],
        act_text_status={},
    )
    assert aggregate == {**aggregate, "status": "complete", "reasons": []}

    bundle = build_armarium_bundle(
        replace(
            original,
            acts=acts,
            aggregate=aggregate,
            aggregate_basis={**original.aggregate_basis, "act_text_status": {}},
        ),
        _formats(embed_pixels=False),
        _source_bytes,
    )

    assert bundle.manifest["claims"]["status"] == "partial"
    assert [
        reason
        for reason in bundle.manifest["claims"]["partial_reasons"]
        if reason.startswith("page 1 is held-for-review")
    ]
    # And the clean-machine verifier recomputes the same disagreement rather than
    # reading the reassuring half of it out of the manifest.
    manifest = verify_export_bundle(bundle.data, tmp_path / "clean")
    assert manifest["claims"]["status"] == "partial"
    assert manifest["aggregate"]["status"] == "complete"


def test_a_refused_source_and_a_silent_page_each_land_in_a_named_set(tmp_path):
    """A door refusal and a sealed page nobody marked out are the two units the
    act-only partition could not see at all. Neither may be inferred blank."""
    base = _projection()
    pages = (
        *base.pages,
        {
            "ordinal": 2,
            "outcome": "refused",
            "reason": "the submitted bytes were not a readable image",
            "declared_path": "register/folio-2.png",
            "declared_sha256": "b" * 64,
            "page_id": None,
        },
        {
            "ordinal": 3,
            "outcome": "sealed",
            "reason": "",
            "declared_path": "register/folio-3.png",
            "declared_sha256": "c" * 64,
            "page_id": "pg-3",
            "image_path": "1_exemplar/blobs/sha256/page",
            "image_sha256": digest_bytes(_source_bytes("1_exemplar/blobs/sha256/page")),
        },
    )
    projection = replace(
        base,
        pages=pages,
        # One ink-map row per *sealed* page: page 2 was refused and never
        # reached the map at all.
        ink_map_pages=(_mapped_page(1), _mapped_page(3)),
        source_manifest=tuple(
            {
                "ordinal": page["ordinal"],
                "relative_path": page["declared_path"],
                "sha256": page["declared_sha256"],
            }
            for page in pages
        ),
        aggregate=run_aggregate(
            {"one": ArmariumCategory.DELIVERED, "two": ArmariumCategory.HELD_FOR_REVIEW},
            base.aggregate_basis["coverage_records"],
            {page["ordinal"]: page for page in pages},
            unaddressed_chairs=[],
            act_pages=base.aggregate_basis["act_pages"],
            act_text_status=base.aggregate_basis["act_text_status"],
        ),
    )
    bundle = build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)
    ledger = json.loads(_members(bundle.data)[EXPORT_MANIFEST_NAME])["claims"]["terminal_ledger"]

    units = {unit["unit_id"]: unit for unit in ledger["units"]}
    assert units["source:2"]["category"] == "refused-with-reason"
    assert units["source:2"]["reason"] == "the submitted bytes were not a readable image"
    assert "page:2" not in units, "a refused source sealed no page to account for"
    assert units["page:3"]["category"] == "held-for-review"
    assert units["source:3"]["category"] == "held-for-review"
    assert "blank page is proved" in units["page:3"]["reason"]
    assert ledger["by_unit_type"] == {"source": 3, "page": 2, "act": 2}
    assert sum(ledger["by_category"].values()) == ledger["unit_count"] == 7


def test_a_display_that_does_not_strip_back_to_the_canonical_field_is_refused(tmp_path):
    """Spec 11 test 2's rendered half, on the written product.

    A display convention that changed the reading -- rather than annotating it --
    would be a second text leaving the pipeline under GOVERNANCE 5's nose. The
    verifier strips the rendering and requires the canonical field back exactly.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").splitlines()
    display_at = lines.index("display:") + 1
    lines[display_at] = json.dumps("Caesar d'Amours", ensure_ascii=False)
    members[TEXT_REGISTER] = ("\n".join(lines) + "\n").encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="does not strip back"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_bundle_that_drops_its_rendering_is_refused(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").splitlines()
    convention_at = next(
        index for index, line in enumerate(lines) if line.startswith("display_convention: ")
    )
    del lines[convention_at : convention_at + 3]
    members[TEXT_REGISTER] = ("\n".join(lines) + "\n").encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="no completed literal record"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_the_manifest_says_the_display_convention_is_only_proposed(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    manifest = json.loads(_members(bundle.data)[EXPORT_MANIFEST_NAME])

    assert manifest["claims"]["display"] == {
        "convention": DISPLAY_CONVENTION,
        "status": "proposed-pending-tyrels-choice",
        "alters_stored_text": False,
        "renders_canonical_uncertainty": False,
        "exercised_against_real_spans": False,
        "reason": (
            "the rendering is not fed this package's canonical uncertainty "
            "layer, which travels beside each literal instead; marking spans "
            "inside a displayed reading would exercise a convention that "
            "remains Tyrel's choice at this gate"
        ),
    }
    # The same package says, two claims above, that it carries the canonical
    # layer. Both statements are about this build's uncertainty; a manifest whose
    # display claim contradicted its uncertainty claim would be a package arguing
    # with itself about what it contains.
    assert manifest["claims"]["uncertainty"]["status"] == "canonical-unicode-codepoint-offsets"
    members = _members(bundle.data)
    manifest["claims"]["display"]["status"] = "chosen"
    _refresh_manifest(members, manifest)
    with pytest.raises(SchemaRefusal, match="display claim is not the verified claim"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_the_manifest_may_not_claim_the_rendering_carries_the_canonical_layer(tmp_path):
    """The non-carriage declaration is verified, not merely written.

    A package whose display claim said the rendering carried the layer would be
    describing a `display:` line this build does not produce -- the failure mode
    the claim exists to prevent, one field over from the convention itself.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["claims"]["display"]["renders_canonical_uncertainty"] = True
    _refresh_manifest(members, manifest)

    with pytest.raises(SchemaRefusal, match="display claim is not the verified claim"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_the_manifest_says_whether_projection_identity_was_actually_checked(tmp_path):
    """Below two literal formats, `verify_projection_identity` never runs -- say so."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    manifest = json.loads(_members(bundle.data)[EXPORT_MANIFEST_NAME])
    assert manifest["canonical_text"]["identity_verified_across"] == [
        "acts-database",
        "jsonl",
        "text-bundle",
    ]

    single_format = ArmariumFormats(("jsonl",), False)
    single = build_armarium_bundle(_projection(), single_format, _source_bytes)
    single_manifest = json.loads(_members(single.data)[EXPORT_MANIFEST_NAME])
    assert single_manifest["canonical_text"]["identity_verified_across"] == []

    members = _members(bundle.data)
    manifest["canonical_text"]["identity_verified_across"] = []
    _refresh_manifest(members, manifest)
    with pytest.raises(SchemaRefusal, match="canonical-text claim is not this build's fixed claim"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


@pytest.mark.parametrize(
    "field", ["salvage_id", "harvested_content", "harvest_kind", "content", "promotion"]
)
def test_a_salvage_shaped_record_cannot_enter_the_acts_namespace(field, tmp_path):
    """Spec 11 test 4, in the direction the reserved-field guard does not cover.

    A salvage item that resembles an act must be refused by name, not left to fail on
    a missing key somewhere downstream. Promotion re-enters the pipeline under Tyrel's
    recorded approval; there is no export-time promotion.
    """
    base = _projection()
    smuggled = {**base.acts[0], field: "a grid tiling nobody established"}
    with pytest.raises(SchemaRefusal, match="carries salvage-tier field"):
        build_armarium_bundle(
            replace(base, acts=(smuggled, base.acts[1])),
            _formats(embed_pixels=False),
            _source_bytes,
        )


def test_a_deeply_nested_salvage_item_becomes_a_refusal_not_a_recursion_crash(tmp_path):
    """`_reject_salvage_act_namespace` walks a harvested salvage item whole, before
    any of its own field checks (`_validate_salvage_items`), so unvalidated
    provenance can nest past Python's recursion limit. That must become a
    `SchemaRefusal`, never an uncaught `RecursionError` that would crash the
    whole export and take every other act down with it."""
    nested: object = "leaf"
    for _ in range(5000):
        nested = {"nested": nested}
    item = {**_salvage_item("scrap"), "provenance": {"collection": "tier", "detail": nested}}
    base = _projection()
    # The salvage walk's own wording, not the shared "nests too deeply" prefix:
    # the retained-reference walk and the sources.json parser raise refusals
    # carrying that prefix too, so a bare match could not prove which guard held.
    with pytest.raises(SchemaRefusal, match="salvage-tier .* nests too deeply for this machine"):
        build_armarium_bundle(
            replace(base, salvage_items=(item,)),
            _formats(embed_pixels=False),
            _source_bytes,
        )


def test_bytes_that_are_not_an_archive_are_refused_rather_than_raising_out_of_the_verifier(
    tmp_path,
):
    """ "These bytes are not an archive" is one of the refusals `verify_export_bundle`
    exists to make, not an exception for its caller to interpret."""
    with pytest.raises(SchemaRefusal, match="not a readable ZIP archive"):
        verify_export_bundle(b"PK\x03\x04 but not really a zip", tmp_path / "clean")


def test_a_member_named_as_both_file_and_directory_is_refused_before_extraction(tmp_path):
    """Each name is safe alone; together they are unextractable. Extraction surfaces
    that only after writing part of the package to the clean machine."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    members["sources.json/child"] = b"x"
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["members"].append(
        {"path": "sources.json/child", "sha256": digest_bytes(b"x"), "bytes": 1}
    )
    _refresh_manifest(members, manifest)

    clean = tmp_path / "clean"
    with pytest.raises(SchemaRefusal, match="both a file and a directory"):
        verify_export_bundle(_zip_bytes(members), clean)
    assert not [path for path in clean.rglob("*") if path.is_file()]


@pytest.mark.parametrize(
    ("alias", "message"),
    [
        ("SOURCES.JSON", "collide after filesystem case"),
        ("./sources.json", "not in canonical POSIX spelling"),
    ],
)
def test_member_names_that_alias_on_a_recipient_filesystem_are_refused_before_extraction(
    alias, message, tmp_path
):
    """Distinct ZIP spellings can still name one extracted filesystem object."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    members[alias] = members["sources.json"]
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED) as archive:
        for name in [EXPORT_MANIFEST_NAME] + sorted(
            name for name in members if name != EXPORT_MANIFEST_NAME
        ):
            archive.writestr(name, members[name])

    clean = tmp_path / "clean"
    with pytest.raises(SchemaRefusal, match=message):
        verify_export_bundle(buffer.getvalue(), clean)
    assert not [path for path in clean.rglob("*") if path.is_file()]


def test_a_compressed_member_is_refused_before_a_byte_is_decompressed(tmp_path):
    """The decompression bound is the refusal, so the refusal needs its own witness.

    Nothing else in this file caps an extracted member's size: `verify_export_bundle`
    is safe from a decompression bomb only because a stored member cannot be larger
    than the archive that carries it. Removing the check left every test in the
    repository green.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name in [EXPORT_MANIFEST_NAME] + sorted(
            name for name in members if name != EXPORT_MANIFEST_NAME
        ):
            archive.writestr(name, members[name])

    clean = tmp_path / "clean"
    with pytest.raises(SchemaRefusal, match="is compressed"):
        verify_export_bundle(buffer.getvalue(), clean)
    assert not [path for path in clean.rglob("*") if path.is_file()]


def test_a_directory_swapped_to_a_symlink_after_preflight_cannot_redirect_extraction(
    tmp_path, monkeypatch
):
    """The no-link check and member write must be one descriptor-relative operation."""
    import armarium_export as export_module

    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    clean = tmp_path / "clean"
    clean.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    real_extract = export_module._extract_archive_members

    def swap_then_extract(archive, root_fd, names):
        (clean / "text").symlink_to(outside, target_is_directory=True)
        return real_extract(archive, root_fd, names)

    monkeypatch.setattr(export_module, "_extract_archive_members", swap_then_extract)

    with pytest.raises(SchemaRefusal, match="package member parent is not an ordinary directory"):
        verify_export_bundle(bundle.data, clean)

    assert not list(outside.rglob("*")), "no package byte may cross the clean-root boundary"


def test_a_preexisting_file_symlink_is_refused_before_archive_extraction(tmp_path):
    """A linked ambient entry is refused even when it occupies an expected path.

    Ordinary files are safely replaced (including hard links, tested below), so the
    refusal is specifically about a symlink that extraction would otherwise follow.
    Refusing it before opening the archive proves neither member accounting nor
    text-bundle reads can be redirected through the two former ``rglob`` walks.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"bytes outside the extraction root")
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / EXPORT_MANIFEST_NAME).symlink_to(outside)

    with pytest.raises(SchemaRefusal, match="contains a link"):
        verify_export_bundle(bundle.data, clean)

    assert outside.read_bytes() == b"bytes outside the extraction root"


def test_a_preexisting_hard_link_is_replaced_without_writing_outside_the_clean_root(tmp_path):
    """A hard link reports as a regular file, so link rejection alone is not containment."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"bytes outside the extraction root")
    clean = tmp_path / "clean"
    clean.mkdir()
    linked = clean / EXPORT_MANIFEST_NAME
    linked.hardlink_to(outside)
    shared_inode = linked.stat().st_ino

    manifest = verify_export_bundle(bundle.data, clean)

    assert manifest["schema"] == "armarium-export-manifest.v3"
    assert outside.read_bytes() == b"bytes outside the extraction root"
    assert linked.stat().st_ino != shared_inode


def test_an_extraction_cleanup_failure_names_the_leftover_temporary_file(tmp_path, monkeypatch):
    """A failed cleanup is part of the refusal, never a silently retained member."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    clean = tmp_path / "clean"
    import armarium_export as export_module

    real_unlink = export_module._unlink_at

    monkeypatch.setattr(
        "armarium_export._atomic_replace",
        lambda _source, _target, **_kwargs: (_ for _ in ()).throw(
            OSError("simulated replace failure")
        ),
    )

    def fail_temporary_unlink(path, *args, **kwargs):
        if ".extracting-" in Path(path).name:
            raise OSError("simulated cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(export_module, "_unlink_at", fail_temporary_unlink)

    with pytest.raises(SchemaRefusal, match="temporary file .* could not be removed"):
        verify_export_bundle(bundle.data, clean)

    assert list(clean.rglob("*.extracting-*")), "the test must exercise a real leftover file"


def test_a_member_path_deeper_than_the_bound_is_refused_by_name():
    """A hostile archive gets a refusal, not a RecursionError.

    Extraction builds parents in a loop, so the deep path lands; the inventory
    that follows walks it with a recursive helper, and nothing converts
    `RecursionError`. The bound is checked at validation, before any of it runs.
    """
    import armarium_export as export_module

    legitimate = "media/pages/page-0001/crop-0001.png"
    export_module._validate_member_name(legitimate)

    too_deep = "/".join(f"d{index}" for index in range(64)) + "/acts.jsonl"
    with pytest.raises(SchemaRefusal, match="package member bound"):
        export_module._validate_member_name(too_deep)


@pytest.mark.parametrize(
    ("name", "member"),
    # `PurePosixPath` splits on `/` alone, so neither of these is absolute and
    # neither has `".."` among its parts: only the raw-character rejection sees
    # them. The set's other member, NUL, cannot be driven through this vector --
    # Python's ZIP reader truncates an entry name at the first NUL, so it never
    # reaches `_validate_member_name` as written -- and is covered below on a
    # declared path, which is JSON and survives intact.
    [
        ("backslash traversal", "acts\\..\\..\\evil.txt"),
        ("windows drive absolute", "C:\\evil.txt"),
    ],
)
def test_a_path_no_posix_check_recognizes_as_traversal_is_still_refused(name, member, tmp_path):
    """The Zip-Slip variant `_reject_unsafe_relative_path` exists for, with a witness.

    A bundle is opened by whatever tool its recipient has, and Windows-native tooling
    does treat a backslash in a ZIP entry name as a separator. The commit that closed
    this changed no test file, so removing the raw-character rejection left the whole
    suite green.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    members[member] = b"x"
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["members"].append({"path": member, "sha256": digest_bytes(b"x"), "bytes": 1})
    _refresh_manifest(members, manifest)
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED) as archive:
        for entry in [EXPORT_MANIFEST_NAME] + sorted(
            entry for entry in members if entry != EXPORT_MANIFEST_NAME
        ):
            archive.writestr(entry, members[entry])

    clean = tmp_path / "clean"
    with pytest.raises(SchemaRefusal, match="is unsafe"):
        verify_export_bundle(buffer.getvalue(), clean)
    assert not [path for path in clean.rglob("*") if path.is_file()]


def test_a_nul_in_a_declared_source_path_is_refused(tmp_path):
    """The other half of the raw-character set, on a field that survives as JSON."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    sources = json.loads(members["sources.json"])
    sources["pages"][0]["declared_path"] = "register/folio\x00-1.png"
    members["sources.json"] = canonical_bytes(sources)
    _refresh_manifest_member(members, "sources.json")

    with pytest.raises(SchemaRefusal, match="is unsafe"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_display_rendering_that_cannot_be_parsed_is_refused_not_raised(tmp_path):
    """`strip_display` raises `ValueError` on markup it cannot parse. Every one of
    those is reachable from a package a recipient was handed."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    lines[lines.index("display:") + 1] = json.dumps("⟨never closed", ensure_ascii=False)
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="not a renderable display convention"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_coverage_record_missing_the_fields_the_aggregate_reads_is_refused(tmp_path):
    """Recomputing the basis makes `run_aggregate` reach inside a coverage record for
    `by_class['completed']` whenever it claims `under_witnessed` -- a field nothing
    above it proves is present."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    sources = json.loads(members["sources.json"])
    for record in sources["aggregate_basis"]["coverage_records"].values():
        record["under_witnessed"] = True
    members["sources.json"] = canonical_bytes(sources)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["aggregate_basis"] = sources["aggregate_basis"]
    row = next(item for item in manifest["members"] if item["path"] == "sources.json")
    row["sha256"] = digest_bytes(members["sources.json"])
    row["bytes"] = len(members["sources.json"])
    _refresh_manifest(members, manifest)

    with pytest.raises(SchemaRefusal, match="basis cannot be reconciled"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_an_acts_database_whose_acts_are_a_view_is_refused_before_it_is_queried(tmp_path):
    """A few kilobytes of package member, an unbounded result set.

    SQLite is happy for `acts` to be a view, and a view over a recursive CTE is a
    program rather than stored rows: verifying the package below allocated until the
    kernel killed the process. Refused by construction, because a stored table's row
    count is bounded by the member's own physical bytes and a view's is bounded by
    nothing.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    scratch = tmp_path / "acts.sqlite"
    scratch.write_bytes(members["acts.sqlite"])
    connection = sqlite3.connect(scratch)
    try:
        connection.execute("ALTER TABLE acts RENAME TO acts_real")
        connection.execute(
            """
            CREATE VIEW acts AS
            WITH RECURSIVE forever(act_id, act_key, category, canonical_clean_text,
                                   canonical_text_sha256, provenance_json,
                                   source_regions_json, evidence_json, reason) AS (
                SELECT 'a', 'k', 'delivered', 'x', 'y', NULL, NULL, NULL, NULL
                UNION ALL
                SELECT act_id || 'a', act_key, category, canonical_clean_text,
                       canonical_text_sha256, provenance_json, source_regions_json,
                       evidence_json, reason
                FROM forever
            )
            SELECT * FROM forever
            """
        )
        connection.commit()
    finally:
        connection.close()
    members["acts.sqlite"] = scratch.read_bytes()
    _refresh_manifest_member(members, "acts.sqlite")

    with pytest.raises(SchemaRefusal, match="stored tables"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_clean_root_whose_path_carries_uri_syntax_still_verifies(tmp_path):
    """`bundle.py` derives its staging directory from the operator's own `--out` name,
    so splicing that path into `file:{path}?mode=ro` let an ordinary destination make a
    good package fail with a message blaming the package."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    manifest = verify_export_bundle(bundle.data, tmp_path / "deliver?run#7" / "bundle")
    assert manifest["claims"]["status"] == "partial"


def _members(data: bytes) -> dict[str, bytes]:
    with ZipFile(BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _refresh_manifest_member(members: dict[str, bytes], changed_member: str) -> None:
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    row = next(item for item in manifest["members"] if item["path"] == changed_member)
    row["sha256"] = digest_bytes(members[changed_member])
    row["bytes"] = len(members[changed_member])
    _refresh_manifest(members, manifest)


def _refresh_manifest(members: dict[str, bytes], manifest: dict) -> None:
    manifest["self_hash"] = self_hash(manifest)
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)


@pytest.mark.parametrize("text", ["e\u0301", "é", "𐐷\u0301", "A\u030a𐐷"])
def test_unicode_uncertainty_offsets_survive_every_literal_projection(tmp_path, text):
    """Offsets count Unicode code points, never UTF-8 bytes or UTF-16 units."""
    layer = {
        "uncertain_spans": [{"start": 0, "end": 1, "alternatives": ["?"], "confidence": "low"}],
        "gaps": [
            {"position": "trailing", "start": len(text), "end": len(text), "witness_evidence": []}
        ],
        "self_revisions": [
            {
                "reading_span": {"start": len(text), "end": len(text)},
                "prior_span": {"start": 0, "end": 0},
            }
        ],
    }
    # A trailing gap is unread ink, so the act's own status is `partial` and the
    # run that delivered it says so; this test is about the offsets surviving,
    # and the damage record travelling with them is the rest of the same claim.
    projection = _damaged_delivered(
        _projection(), text_status="partial", canonical_clean_text=text, uncertainty=layer
    )
    bundle = build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    assert json.loads(members["acts.jsonl"].splitlines()[0])["uncertainty"] == layer
    assert (
        json.dumps(layer, ensure_ascii=False, sort_keys=True).encode("utf-8")
        in members[TEXT_REGISTER]
    )
    database = tmp_path / "acts.sqlite"
    database.write_bytes(members["acts.sqlite"])
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT uncertainty_json FROM acts WHERE act_id = 'act-1'"
        ).fetchone()[0]
    assert json.loads(stored) == layer


# --- The damage record: text_status and the transcription annotation layer ------
#
# Opus-F1 / Sol-S4 (T0 export honesty). The Archetypus knew an act was damaged; nothing here read
# the field, so a partial act was exported and aggregated exactly like a whole one
# and the run said `complete` with an empty reason list. These are the projection-
# layer half of that repair; the end-to-end demonstration through the real CLIs is
# `pipeline/6_archetypus/test_annotations.py`.


def _internal_gap_layer(text: str) -> dict:
    middle = len(text) // 2
    return {
        "uncertain_spans": [],
        "gaps": [{"position": "internal", "start": middle, "end": middle, "witness_evidence": []}],
        "self_revisions": [],
    }


def _partial_projection() -> ArmariumProjection:
    base = _projection()
    literal = base.acts[0][CANONICAL_TEXT_FIELD]
    return _damaged_delivered(base, text_status="partial", uncertainty=_internal_gap_layer(literal))


def test_a_delivered_act_with_a_gap_reaches_every_selected_literal_format(tmp_path):
    """Spec 11's honesty, measured on the written product rather than asserted.

    One schema-legal internal gap: the status says `partial` in the readable
    bundle, the JSONL hand-off and the acts database, the run aggregate names the
    act, and the terminal ledger folds that reason in.
    """
    bundle = build_armarium_bundle(
        _partial_projection(), _formats(embed_pixels=False), _source_bytes
    )
    members = _members(bundle.data)

    assert "text_status: partial" in members[TEXT_REGISTER].decode("utf-8")
    row = json.loads(members["acts.jsonl"].splitlines()[0])
    assert row["text_status"] == "partial"
    assert row["transcription_annotations"] == []
    database = tmp_path / "acts.sqlite"
    database.write_bytes(members["acts.sqlite"])
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT text_status FROM acts WHERE act_id = 'act-1'"
        ).fetchone() == ("partial",)

    manifest = verify_export_bundle(bundle.data, tmp_path / "clean")
    assert manifest["aggregate"]["status"] == "partial"
    assert any(
        reason.startswith("act one was delivered with partial text")
        for reason in manifest["aggregate"]["reasons"]
    ), manifest["aggregate"]["reasons"]
    assert manifest["claims"]["status"] == "partial"


def test_a_projection_claiming_established_over_its_own_gap_is_refused():
    """The status is recomputed, never carried: the whole finding in one assertion."""
    projection = _partial_projection()
    dishonest = {**projection.acts[0], "text_status": "established"}
    with pytest.raises(SchemaRefusal, match="may not be projected as a whole one"):
        build_armarium_bundle(
            replace(projection, acts=(dishonest, *projection.acts[1:])),
            _formats(embed_pixels=False),
            _source_bytes,
        )


def test_a_package_edited_to_call_a_damaged_act_whole_is_refused_on_a_clean_machine(tmp_path):
    """A self-hash proves the manifest was not edited afterwards, not that it was true.

    Every row, the source graph, the aggregate and its basis are rewritten here to
    the reassuring value, so nothing inside the package disagrees with anything
    else. What refuses it is the layer the damage actually lives in: the gap is
    still carried beside the literal, and the verifier derives the status from it.
    """
    bundle = build_armarium_bundle(
        _partial_projection(), _formats(embed_pixels=False), _source_bytes
    )
    members = _members(bundle.data)

    rows = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    for row in rows:
        if row["text_status"] == "partial":
            row["text_status"] = "established"
    members["acts.jsonl"] = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    text = (
        members[TEXT_REGISTER]
        .decode("utf-8")
        .replace("text_status: partial", "text_status: established")
    )
    members[TEXT_REGISTER] = text.encode("utf-8")
    sources = json.loads(members["sources.json"])
    for outcome in sources["act_outcomes"]:
        if outcome["text_status"] == "partial":
            outcome["text_status"] = "established"
    sources["aggregate_basis"]["act_text_status"] = {"one": "established"}
    members["sources.json"] = canonical_bytes(sources)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["aggregate"] = run_aggregate(
        {"one": ArmariumCategory.DELIVERED, "two": ArmariumCategory.HELD_FOR_REVIEW},
        sources["aggregate_basis"]["coverage_records"],
        {page["ordinal"]: page for page in sources["pages"]},
        unaddressed_chairs=[],
        act_pages=sources["aggregate_basis"]["act_pages"],
        act_text_status={"one": "established"},
    )
    manifest["aggregate_basis"] = sources["aggregate_basis"]
    # And the ledger the manifest's own top-level status is read from, so the
    # package is green everywhere and nothing inside it disagrees with anything
    # else. Without this the (correct) ledger mismatch refuses first and the
    # row-level derivation this test is about is never reached.
    ledger = _terminal_ledger(
        sources["act_outcomes"],
        sources["pages"],
        sources["aggregate_basis"]["act_pages"],
        manifest["aggregate"],
    )
    manifest["claims"]["terminal_ledger"] = ledger
    manifest["claims"]["status"] = ledger["status"]
    manifest["claims"]["partial_reasons"] = ledger["unresolved_reasons"]
    for member in ("acts.jsonl", TEXT_REGISTER, "sources.json"):
        row = next(item for item in manifest["members"] if item["path"] == member)
        row["sha256"] = digest_bytes(members[member])
        row["bytes"] = len(members[member])
    _refresh_manifest(members, manifest)

    with pytest.raises(SchemaRefusal, match="may not be projected as a whole one"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_row_claiming_produced_semantic_annotations_is_refused(tmp_path):
    """The fixed claim is checked from the product side, not only asserted.

    Nothing in this repository produces a semantic annotation, so a packaged row
    saying one was produced must be refused by the verifier that knows that —
    not accepted because nothing disproves it.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    rows = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    for row in rows:
        if row["category"] == "delivered":
            row["semantic_annotations"] = [{"kind": "person", "value": "Jean"}]
    members["acts.jsonl"] = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    _refresh_manifest_member(members, "acts.jsonl")

    with pytest.raises(SchemaRefusal, match="semantic annotation claim"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_non_delivered_row_carrying_a_text_status_is_refused(tmp_path):
    """An act with no Archetypus record has no status for a row to describe.

    The projection boundary already refuses this shape at build time; this pins
    the same refusal on a clean machine reading the packaged product, where a
    rebuilt-around package would otherwise be the only carrier.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    rows = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    for row in rows:
        if row["category"] != "delivered":
            row["text_status"] = "established"
    members["acts.jsonl"] = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    _refresh_manifest_member(members, "acts.jsonl")

    with pytest.raises(SchemaRefusal, match="non-delivered acts JSONL row"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_package_whose_basis_alone_calls_a_damaged_act_whole_is_refused(tmp_path):
    """The smaller, easier edit than the full rewrite above: ONLY the aggregate
    basis — the copy the run's verdict is computed from — is edited to
    `established`, while every row honestly still says `partial`. The row-level
    derivation cannot see this one; the basis-vs-rows comparison is what
    refuses it, so the verdict can never rest on an unchecked copy of the
    damage record.
    """
    bundle = build_armarium_bundle(
        _partial_projection(), _formats(embed_pixels=False), _source_bytes
    )
    members = _members(bundle.data)

    sources = json.loads(members["sources.json"])
    sources["aggregate_basis"]["act_text_status"] = {"one": "established"}
    members["sources.json"] = canonical_bytes(sources)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["aggregate"] = run_aggregate(
        {"one": ArmariumCategory.DELIVERED, "two": ArmariumCategory.HELD_FOR_REVIEW},
        sources["aggregate_basis"]["coverage_records"],
        {page["ordinal"]: page for page in sources["pages"]},
        unaddressed_chairs=[],
        act_pages=sources["aggregate_basis"]["act_pages"],
        act_text_status={"one": "established"},
    )
    manifest["aggregate_basis"] = sources["aggregate_basis"]
    ledger = _terminal_ledger(
        sources["act_outcomes"],
        sources["pages"],
        sources["aggregate_basis"]["act_pages"],
        manifest["aggregate"],
    )
    manifest["claims"]["terminal_ledger"] = ledger
    manifest["claims"]["status"] = ledger["status"]
    manifest["claims"]["partial_reasons"] = ledger["unresolved_reasons"]
    row = next(item for item in manifest["members"] if item["path"] == "sources.json")
    row["sha256"] = digest_bytes(members["sources.json"])
    row["bytes"] = len(members["sources.json"])
    _refresh_manifest(members, manifest)

    with pytest.raises(SchemaRefusal, match="does not carry exactly the delivered acts"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_sealed_transcription_annotation_is_never_replaced_by_the_semantic_claim(tmp_path):
    """Sol-S4's second field failure, at the layer that wrote the replacement.

    Every row used to carry `annotations: []` and `annotation_status:
    "not-produced"` — a true statement about the unbuilt *semantic* layer, written
    over an act whose Archetypus record had sealed a real `illegible` mark. Both
    layers now travel under their own names and both are asserted here.
    """
    literal = _projection().acts[0][CANONICAL_TEXT_FIELD]
    mark = {"kind": "illegible", "start": 3, "end": 3, "witness_evidence": []}
    projection = _damaged_delivered(
        _projection(), text_status="partial", transcription_annotations=[mark]
    )
    bundle = build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)

    row = json.loads(members["acts.jsonl"].splitlines()[0])
    assert row["transcription_annotations"] == [mark]
    assert row["semantic_annotations"] == []
    assert row["semantic_annotation_status"] == "not-produced-pending-architecture-approval"
    assert json.dumps([mark], ensure_ascii=False, sort_keys=True) in members[TEXT_REGISTER].decode(
        "utf-8"
    )
    database = tmp_path / "acts.sqlite"
    database.write_bytes(members["acts.sqlite"])
    with sqlite3.connect(database) as connection:
        stored, semantic, status = connection.execute(
            "SELECT transcription_annotations_json, semantic_annotations_json, "
            "semantic_annotation_status FROM acts WHERE act_id = 'act-1'"
        ).fetchone()
    assert json.loads(stored) == [mark]
    assert json.loads(semantic) == []
    assert status == "not-produced-pending-architecture-approval"

    manifest = verify_export_bundle(bundle.data, tmp_path / "clean")
    # The package says both things about annotations, and says which is which.
    assert manifest["claims"]["semantic_annotations"] == {
        "status": "semantic-annotations-not-produced",
        "text_writable": False,
    }
    assert manifest["claims"]["transcription_annotations"]["carried_by"] == [
        "acts-database",
        "jsonl",
        "text-bundle",
    ]
    # The literal that the mark anchors to is untouched by carrying it.
    assert verify_projection_identity(bundle.data, tmp_path / "identity") == {"act-1": literal}


def test_projection_identity_refuses_a_package_whose_formats_disagree_about_damage(tmp_path):
    """Two deliverables cannot disagree about whether the same act is damaged.

    The literal is byte-identical in every format, so the text comparison passes
    by construction; the damage record is part of the same one reading and rides
    in the same equality check (GOVERNANCE 5 does not stop at the characters).
    """
    bundle = build_armarium_bundle(
        _partial_projection(), _formats(embed_pixels=False), _source_bytes
    )
    members = _members(bundle.data)
    mark = {"kind": "illegible", "start": 1, "end": 1, "witness_evidence": []}
    rows = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    for row in rows:
        if row["text_status"] is not None:
            row["transcription_annotations"] = [mark]
    members["acts.jsonl"] = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    _refresh_manifest_member(members, "acts.jsonl")

    tampered = _zip_bytes(members)
    # Package verification alone is green: the edited layer is well-formed, and
    # `partial` is still the honest status for a row that carries a gap either way.
    verify_export_bundle(tampered, tmp_path / "clean")
    with pytest.raises(SchemaRefusal, match="projection differs"):
        verify_projection_identity(tampered, tmp_path / "identity")


def test_the_text_bundle_refuses_a_literal_section_with_no_damage_record(tmp_path):
    """The status is not optional beside a delivered literal, and says so by name."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    marker = lines.index("text_status: established")
    del lines[marker]
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="no established-text status"):
        verify_projection_identity(_zip_bytes(members), tmp_path)


def test_the_text_bundle_refuses_two_established_text_statuses_for_one_literal(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    marker = lines.index("text_status: established")
    lines[marker:marker] = [lines[marker]]
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="more than one established-text status"):
        verify_projection_identity(_zip_bytes(members), tmp_path)


def test_the_clean_machine_check_refuses_a_member_key_shared_across_logical_acts():
    """The verifier's twin of the producer's key screen, at the unit seam.

    Ids are the counted unit, so a rebuilt package repeating one
    `member_act_keys` entry under two logical acts -- every id still unique --
    balances the row arithmetic while asserting that two logical acts descend
    from one proposal row. The clean-machine recompute must refuse it exactly
    as the producer does.
    """
    import armarium_export as _module  # noqa: PLC0415

    logical_denominator = "physical-act-partition logical acts"
    memberships = {
        "pac_aaaaaaaaaaaaaaaa": {
            "member_local_act_ids": ["act_1111111111111111"],
            "member_act_keys": ["shared-key"],
            "member_source_page_ordinals": [1],
        },
        "pac_bbbbbbbbbbbbbbbb": {
            "member_local_act_ids": ["act_2222222222222222"],
            "member_act_keys": ["shared-key"],
            "member_source_page_ordinals": [2],
        },
    }
    manifest = {
        "claims": {
            "act_partition": {
                "denominator": logical_denominator,
                "local_proposal_rows": 2,
                "logical_membership": memberships,
            }
        }
    }
    categories = {
        "pac_aaaaaaaaaaaaaaaa": "delivered",
        "pac_bbbbbbbbbbbbbbbb": "delivered",
    }
    act_keys = {
        "pac_aaaaaaaaaaaaaaaa": "logical:pac_aaaaaaaaaaaaaaaa",
        "pac_bbbbbbbbbbbbbbbb": "logical:pac_bbbbbbbbbbbbbbbb",
    }
    sources = {
        "logical_accounting": {"local_proposal_rows": 2, "memberships": memberships},
        "aggregate_basis": {
            "act_pages": {
                "logical:pac_aaaaaaaaaaaaaaaa": [1],
                "logical:pac_bbbbbbbbbbbbbbbb": [2],
            }
        },
    }
    with pytest.raises(SchemaRefusal, match="repeats a member"):
        _module._verify_logical_partition_claim(manifest, categories, act_keys, sources)

    # One proposal row exported twice under two identities -- a member key that
    # also names an exported act row -- balances the arithmetic and must still
    # refuse.
    disguised = {
        "pac_aaaaaaaaaaaaaaaa": {
            "member_local_act_ids": ["act_1111111111111111"],
            "member_act_keys": ["logical:pac_bbbbbbbbbbbbbbbb"],
            "member_source_page_ordinals": [1],
        }
    }
    manifest_disguised = {
        "claims": {
            "act_partition": {
                "denominator": logical_denominator,
                "local_proposal_rows": 2,
                "logical_membership": disguised,
            }
        }
    }
    sources_disguised = {
        "logical_accounting": {"local_proposal_rows": 2, "memberships": disguised},
        "aggregate_basis": {"act_pages": {"logical:pac_aaaaaaaaaaaaaaaa": [1]}},
    }
    with pytest.raises(SchemaRefusal, match="repeats a member"):
        _module._verify_logical_partition_claim(
            manifest_disguised, categories, act_keys, sources_disguised
        )

    # And a member page ordinal missing from the act's own attribution is a
    # dropped page, not an accepted package.
    unattributed = {
        "pac_aaaaaaaaaaaaaaaa": {
            "member_local_act_ids": ["act_1111111111111111", "act_3333333333333333"],
            "member_act_keys": ["key-one", "key-two"],
            "member_source_page_ordinals": [1, 7],
        }
    }
    manifest_unattributed = {
        "claims": {
            "act_partition": {
                "denominator": logical_denominator,
                "local_proposal_rows": 3,
                "logical_membership": unattributed,
            }
        }
    }
    sources_unattributed = {
        "logical_accounting": {"local_proposal_rows": 3, "memberships": unattributed},
        "aggregate_basis": {"act_pages": {"logical:pac_aaaaaaaaaaaaaaaa": [1]}},
    }
    with pytest.raises(SchemaRefusal, match="page attribution does not carry"):
        _module._verify_logical_partition_claim(
            manifest_unattributed, categories, act_keys, sources_unattributed
        )


def _logical_conservation_projection(attribution) -> ArmariumProjection:
    """One logical act row, with `act_pages` set to whatever is under test."""
    entry = {
        "act_id": "pac_aaaaaaaaaaaaaaaa",
        "act_key": "logical:pac_aaaaaaaaaaaaaaaa",
        "category": "delivered",
        "canonical_clean_text": "one text",
        "source_regions": [],
        "reason": None,
        "logical_membership": {
            "member_local_act_ids": ["act_1111111111111111"],
            "member_act_keys": ["member-key"],
            "member_source_page_ordinals": [1],
            "physical_page_components": [
                {
                    "physical_page_id": "ppg_0123456789abcdef",
                    "required_capture_sha256s": ["a" * 64],
                }
            ],
        },
    }
    return ArmariumProjection(
        fixture_id="armarium-logical-attribution-v1",
        scenario="adversarial",
        config_digest="a" * 64,
        aggregate={},
        acts=(entry,),
        pages=(),
        source_manifest=(),
        expected_acts=1,
        witness_chairs=(),
        witness_floor=0,
        aggregate_basis={"act_pages": {entry["act_key"]: attribution}},
        local_proposal_rows=1,
    )


def test_a_malformed_page_attribution_refuses_before_the_page_accounting_reads_it():
    """`_validate_logical_act_conservation` runs before the basis is validated.

    `_aggregate_from_basis` is what proves `act_pages` is well formed, and it
    runs afterwards (`_validate_projection` calls the conservation check first).
    So this check reads a caller's assertion nothing has shaped yet: a number
    raises `TypeError` out of `set(...)` and ends the export with no named
    refusal at all, and a string dedupes into its own characters and reports a
    member page missing that was never missing -- a guard failing for a reason
    other than the one it names.
    """
    import armarium_export as _module  # noqa: PLC0415

    for attribution in (1, "1", ["1"], [True], [1, None]):
        with pytest.raises(SchemaRefusal, match="not a list of page ordinals"):
            _module._validate_logical_act_conservation(
                _logical_conservation_projection(attribution),
                {"pac_aaaaaaaaaaaaaaaa"},
                {"logical:pac_aaaaaaaaaaaaaaaa"},
            )

    # The well-formed attribution still passes, and a genuinely missing page is
    # still reported as the missing page it is, not as a malformed attribution.
    _module._validate_logical_act_conservation(
        _logical_conservation_projection([1]),
        {"pac_aaaaaaaaaaaaaaaa"},
        {"logical:pac_aaaaaaaaaaaaaaaa"},
    )
    with pytest.raises(SchemaRefusal, match="page attribution does not name"):
        _module._validate_logical_act_conservation(
            _logical_conservation_projection([7]),
            {"pac_aaaaaaaaaaaaaaaa"},
            {"logical:pac_aaaaaaaaaaaaaaaa"},
        )
