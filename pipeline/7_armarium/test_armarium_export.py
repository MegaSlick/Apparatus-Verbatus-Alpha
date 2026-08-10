"""Product-level checks for the Armarium's one-text export projection."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from io import BytesIO
from zipfile import ZipFile

import pytest
from armarium_export import (
    EXPORT_MANIFEST_NAME,
    ArmariumProjection,
    _page_ledger_category,
    _zip_bytes,
    build_armarium_bundle,
    canonical_text_sha256,
    verify_export_bundle,
    verify_projection_identity,
)
from display import DISPLAY_CONVENTION
from textnorm import TEXTNORM_REVISION, search_fold

from common.armarium_formats import ArmariumFormats
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ApprovalRefusal, SchemaRefusal
from common.contracts.outcomes import ArmariumCategory, run_aggregate
from common.imaging import encode_grayscale_png

TEXT_REGISTER = "text/_source_folder/register/readings.txt"


def _pixels(value: int) -> bytes:
    return encode_grayscale_png(1, 1, [bytearray([value])])


def _source_bytes(path: str) -> dict[str, bytes]:
    return {
        "1_exemplar/blobs/sha256/page": _pixels(80),
        "2_designator/blobs/sha256/crop": _pixels(40),
    }[path]


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
                "provenance": {"chair": "perlector"},
                "source_regions": [region],
                "reason": None,
                "evidence_refs": [],
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
                "evidence_refs": [],
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
        },
        salvage_items=tuple(salvage_items),
    )


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
    "mutate",
    [
        lambda manifest: manifest["claims"].update(status="complete"),
        lambda manifest: manifest["claims"].update(partial_reasons=[]),
        lambda manifest: manifest["claims"]["submission_inventory"].update(status="reconciled"),
        lambda manifest: manifest["claims"]["terminal_ledger"].update(status="complete"),
        lambda manifest: manifest["claims"]["terminal_ledger"]["units"].pop(),
        lambda manifest: manifest["claims"]["terminal_ledger"]["by_category"].update(
            {"held-for-review": 0}
        ),
        lambda manifest: manifest["aggregate"].update(status="complete", reasons=[]),
        lambda manifest: manifest["aggregate"].update(reasons=["a different partial reason"]),
    ],
)
def test_self_hashed_bundle_cannot_claim_unmeasured_completeness(mutate, tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    mutate(manifest)
    _refresh_manifest(members, manifest)

    with pytest.raises(
        SchemaRefusal,
        match="submission denominator|terminal ledger does not match|status does not match"
        "|aggregate claims complete|aggregate does not match",
    ):
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
            provenance=None,
            source_regions=[],
        )
        held.append(record)
    bundle = build_armarium_bundle(
        replace(
            original,
            acts=tuple(held),
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
            "provenance": None,
            "source_regions": [],
        }
        for act in original.acts
    )
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
    rows = [json.loads(line) for line in (root / "acts.jsonl").read_text().splitlines()]
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
    verify_export_bundle(bundle.data, root)
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


def test_a_self_consistent_but_falsified_search_fold_column_is_refused(tmp_path):
    """A digest and a self-hash prove the package was not edited after sealing. Neither
    proves `act_search.derived_search_text` was ever a fold of its own act's literal."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    scratch = tmp_path / "acts.sqlite"
    scratch.write_bytes(members["acts.sqlite"])
    connection = sqlite3.connect(scratch)
    try:
        connection.execute("UPDATE act_search SET derived_search_text = 'not a fold of anything'")
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
        "exercised_against_real_spans": False,
        "reason": (
            "the Archetypus record carries no uncertainty or gap layer yet, so "
            "every rendering in this build is the established text unchanged"
        ),
    }
    members = _members(bundle.data)
    manifest["claims"]["display"]["status"] = "chosen"
    _refresh_manifest(members, manifest)
    with pytest.raises(SchemaRefusal, match="display claim is not the verified claim"):
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
