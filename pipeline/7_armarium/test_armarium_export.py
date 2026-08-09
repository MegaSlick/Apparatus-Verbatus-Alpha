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
    _zip_bytes,
    build_armarium_bundle,
    canonical_text_sha256,
    verify_export_bundle,
    verify_projection_identity,
)
from textnorm import TEXTNORM_REVISION, search_fold

from common.armarium_formats import ArmariumFormats
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ApprovalRefusal, SchemaRefusal
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
                "one": {"under_witnessed": False, "unresolved_chairs": 0},
                "two": {"under_witnessed": False, "unresolved_chairs": 0},
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
        assert "[" not in text, "no unapproved uncertainty/gap display convention is emitted"

    manifest = verify_export_bundle(bundle.data, tmp_path / "clean")
    assert manifest["claims"]["status"] == "partial"
    assert "submission-file inventory" in manifest["claims"]["partial_reasons"][0]
    assert manifest["claims"]["pixels"]["resolution_claim"].startswith("reference validity")
    assert verify_projection_identity(bundle.data, tmp_path / "identity") == {
        "act-1": "Cǣsar d’Amours"
    }


def test_projection_identity_refuses_a_self_consistent_package_with_one_drifted_format(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    records = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    records[0]["canonical_clean_text"] = "a different purported reading"
    records[0]["canonical_text_sha256"] = canonical_text_sha256(
        records[0]["canonical_clean_text"]
    )
    members["acts.jsonl"] = b"".join(
        canonical_bytes(record) + b"\n" for record in records
    )
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
    manifest["members"] = [
        item for item in manifest["members"] if item["path"] != TEXT_REGISTER
    ]
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
        lambda manifest: manifest["claims"]["submission_inventory"].update(
            status="reconciled"
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
        match="claims.*reconciliation|aggregate claims complete|aggregate does not match",
    ):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_text_bundle_cannot_lose_its_page_and_hash_citation(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").splitlines()
    members[TEXT_REGISTER] = (
        "\n".join(
            line
            for line in lines
            if not line.startswith(("source-page: ", "source-sha256: "))
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
    members["review-items.jsonl"] = b"".join(
        canonical_bytes(record) + b"\n" for record in review
    )
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
        build_armarium_bundle(projection(_projection()), _formats(embed_pixels=False), _source_bytes)


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
    raw_reference = {"relative_path": "4_perlector/artifacts/perlectio/art_123.json", "sha256": "a" * 64}
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


def test_embedded_page_and_crop_pixels_open_on_a_clean_machine(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=True), _source_bytes)

    with ZipFile(BytesIO(bundle.data)) as archive:
        assert {"pixels/pages/1.img", "pixels/crops/rgn-1.img"} <= set(archive.namelist())
    manifest = verify_export_bundle(bundle.data, tmp_path / "clean")
    assert manifest["claims"]["pixels"]["embedded"] is True


def test_salvage_stays_out_of_every_act_projection(tmp_path):
    salvage_content = "marginal material, not an established act"
    projection = _projection(
        salvage_items=(_salvage_item(salvage_content),)
    )
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
