"""Product-level checks for the Armarium's one-text export projection."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import subprocess
import sys
import unicodedata
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest
from armarium_export import (
    CANONICAL_TEXT_FIELD,
    EXPORT_MANIFEST_NAME,
    NOT_MEASURED_BASIS_SCHEMA,
    NOT_MEASURED_INSTRUMENTS,
    NOT_MEASURED_SCHEMA,
    ArmariumProjection,
    _act_json_records,
    _jsonl_act_records,
    _jsonl_literals,
    _not_measured_status,
    _page_ledger_category,
    _terminal_ledger,
    _validate_ink_map_pages,
    _verify_acts_schema,
    _verify_continuation_joins,
    _verify_retained_references_bounded,
    _zip_bytes,
    act_key_sort_key,
    build_armarium_bundle,
    canonical_text_sha256,
    continuation_join_row,
    edge_hold_pages_from_rows,
    verify_delivered_bundle,
    verify_export_bundle,
)
from textnorm import TEXTNORM_REVISION, search_fold

from common.armarium_formats import ArmariumFormats
from common.contracts.approval import real_ingress_record
from common.contracts.canonical import canonical_bytes, canonical_text, digest_bytes, self_hash
from common.contracts.errors import ApprovalRefusal, FatalAccounting, SchemaRefusal
from common.contracts.identities import lot_id
from common.contracts.outcomes import PAGE_READ_SILENT_PAGE_REASON, ArmariumCategory
from common.contracts.outcomes import run_aggregate as _run_aggregate
from common.contracts.stages import ARMARIUM
from common.contracts.uncertainty import validate as validate_uncertainty
from common.imaging import encode_grayscale_png
from common.reading_annotations import read_doubt_marks
from common.residual_ink import (
    load_coverage_audit_config,
)
from common.runtree.store import RunTree
from common.stage import REAL_SCENARIO, StageContext
from conftest import load_stage

TEXT_REGISTER = "text/_source_folder/register/readings.txt"
ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR_CLI = ROOT / "pipeline" / "orchestrator" / "run.py"


# The sealed noise floor and fraction gate (`[coverage_audit.noise_floor]`), read
# the way the stages read them rather than as a module constant.
_NOISE_FLOOR = load_coverage_audit_config()["coverage_audit"]
MINIMUM_INK_PIXELS = _NOISE_FLOOR["minimum_ink_pixels"]
MINIMUM_FRACTION_OUTSIDE_BP = _NOISE_FLOOR["minimum_fraction_outside_bp"]
MINIMUM_FRACTION_OUTSIDE_COVERAGE = MINIMUM_FRACTION_OUTSIDE_BP / 10_000


def _pixels(value: int) -> bytes:
    return encode_grayscale_png(1, 1, [bytearray([value])])


def _source_bytes(path: str) -> dict[str, bytes]:
    return {
        "1_exemplar/blobs/sha256/page": _pixels(80),
        "2_designator/blobs/sha256/crop": _pixels(40),
        "1_exemplar/blobs/sha256/page-2": _pixels(81),
        "2_designator/blobs/sha256/crop-2": _pixels(41),
        "1_exemplar/blobs/sha256/page-3": _pixels(82),
        "2_designator/blobs/sha256/crop-3": _pixels(42),
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
            # The gate the page was measured under, recorded on the row so this
            # verifier can recompute the hold from the counts alone on a clean
            # machine. 2,000 is a fixed value for this hand-built shape, not a
            # measurement of any real page.
            "substantial_ink_pixels": 2_000,
            # The noise floor and fraction gate the row was measured under,
            # at the values the sealed file ships.
            "minimum_ink_pixels": MINIMUM_INK_PIXELS,
            "minimum_fraction_outside_bp": MINIMUM_FRACTION_OUTSIDE_BP,
        },
    }


# The reader's own doubt report, closed into the canonical uncertainty layer
# because the span layers alone cannot say whether an empty list is "no doubt"
# or "no doubt was ever asked for". These projections are hand-built shapes
# with no reader behind them, so the honest state for every layer below is
# `not-assessed`, and the block's uncertainty instrument declares itself
# unproduced over them for that reason. The layers below that actually carry a
# doubt use `_ASSESSED` instead: only a reader's own doubt report mints a span
# or a gap, so a layer holding one is a reader's report and its state says so.
_ASSESSED = {"state": "assessed", "problem": None}
_NOT_ASSESSED = {
    "state": "not-assessed",
    "problem": "a hand-built projection has no reader to report its doubts",
}


def run_aggregate(*args, **kwargs):
    """The run aggregate of a run whose Perlector read whole pages, as the export derives it."""
    kwargs.setdefault("other_categories_by_page", {})
    return _run_aggregate(*args, **kwargs)


_POLICY_SHA256 = "e" * 64
# A synthetic run's lot: tests carry no real run's.
_LOT = lot_id("d" * 64)


def _page_accounting(*ordinals: int) -> tuple[dict, ...]:
    """One text-free page-accounting row per real sealed page."""
    return tuple(
        {
            "ordinal": ordinal,
            "page_id": f"pg-{ordinal}",
            "rules": {"a": "pass"},
            "hold_codes": [],
            "policy_sha256": _POLICY_SHA256,
            "accounting_ref": {
                "relative_path": f"4_perlector/artifacts/page-accounting/pg-{ordinal}.json",
                "sha256": "f" * 64,
            },
        }
        for ordinal in ordinals
    )


def _test_not_measured_basis(**overrides):
    """A minimal, valid not-measured basis for a hand-built projection.

    A test about the block's content overrides the one sub-record it is about.
    """
    basis = {
        "schema": NOT_MEASURED_BASIS_SCHEMA,
        "perlector-uncertain-spans": {
            "sealed_audit_round_cap": 1,
            "acts_delivered": 1,
            "acts_with_uncertain_spans": 0,
            "acts_assessed": 0,
            "acts_not_assessed": 1,
        },
        "designator-geometry-calibration": {
            "configurations": [
                {
                    "configuration": "designator-geometry",
                    "calibrated_for_this_corpus": False,
                    "sample_count": None,
                },
                # The truncation instrument's length floor: the one sealed
                # configuration in this survey that is not Designator geometry.
                {
                    "configuration": "perlector-protocol",
                    "calibrated_for_this_corpus": False,
                    "sample_count": 0,
                },
            ]
        },
        "page-accounting-thresholds": {
            "policy_sha256": _POLICY_SHA256,
            "thresholds": [{"name": "band_slack", "value": 4}],
            "calibrated_for_this_corpus": False,
            "sample_count": None,
        },
        "perlector-pass-c": {
            "pages_read": 1,
            "pages_audit_not_run": 1,
            "sealed_audit_round_cap": 1,
        },
        "comparison-bounds": {
            "sealed_max_comparison_steps": 1_000,
            "max_comparison_character_pairs": 100_000_000,
            "acts_delivered": 1,
            "acts_with_unmeasured_comparison": 0,
            "unmeasured_act_ids": [],
        },
    }
    basis.update(overrides)
    return basis


def _basis_for_acts(acts, *, sealed_pages=1):
    """Keep hand-built projection caveats aligned with their act and page rows."""
    basis = _test_not_measured_basis()
    basis["perlector-pass-c"]["pages_read"] = sealed_pages
    basis["perlector-pass-c"]["pages_audit_not_run"] = sealed_pages
    delivered = [act for act in acts if act["category"] == ArmariumCategory.DELIVERED.value]
    spans = sum(
        isinstance(act.get("uncertainty"), dict) and bool(act["uncertainty"].get("uncertain_spans"))
        for act in delivered
    )
    assessed = sum(
        isinstance(act.get("uncertainty"), dict)
        and isinstance(act["uncertainty"].get("assessment"), dict)
        and act["uncertainty"]["assessment"]["state"] == "assessed"
        for act in delivered
    )
    basis["perlector-uncertain-spans"] = {
        "sealed_audit_round_cap": 0 if spans else 1,
        "acts_delivered": len(delivered),
        "acts_with_uncertain_spans": spans,
        "acts_assessed": assessed,
        # By subtraction here, deliberately: the production basis counts each
        # state, and this helper's job is to hand a projection the basis a
        # producer would have written. A case that delivers an act in a third
        # state therefore reaches the projection's own refusal of that act,
        # rather than the partition rule one step earlier.
        "acts_not_assessed": len(delivered) - assessed,
    }
    basis["comparison-bounds"]["acts_delivered"] = len(delivered)
    return basis


def _projection() -> ArmariumProjection:
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
    projection = ArmariumProjection(
        not_measured_basis=_test_not_measured_basis(),
        fixture_id="armarium-export-test-v1",
        scenario="happy",
        config_digest="a" * 64,
        aggregate={},
        acts=(
            {
                "act_id": "act-1",
                "act_key": "p1:1",
                "category": "delivered",
                "reading": "first reading",
                "canonical_clean_text": "Cǣsar d’Exemple",
                "uncertainty": {
                    "lectio_kind": "page-read",
                    "uncertain_spans": [],
                    "gaps": [],
                    "self_revisions": None,
                    "assessment": _NOT_ASSESSED,
                },
                "text_status": "established",
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
                "act_key": "p1:2",
                "category": "held-for-review",
                "reading": "first reading",
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
                "p1:1": {
                    "configured": 1,
                    "floor": 1,
                    "under_witnessed": False,
                    "unresolved_chairs": 0,
                },
                "p1:2": {
                    "configured": 1,
                    "floor": 1,
                    "under_witnessed": False,
                    "unresolved_chairs": 0,
                },
            },
            "unaddressed_chairs": [],
            "act_pages": {"p1:1": [1], "p1:2": [1]},
            "act_text_status": {"p1:1": "established"},
            "continuation_flags": {},
            "page_witness_chairs": ["attestator_1"],
        },
        ink_map_pages=(_mapped_page(),),
        page_accounting=_page_accounting(1),
        lot=_LOT,
    )
    basis = projection.aggregate_basis
    return replace(
        projection,
        aggregate=run_aggregate(
            {act["act_key"]: ArmariumCategory(act["category"]) for act in projection.acts},
            basis["coverage_records"],
            {page["ordinal"]: page for page in projection.pages},
            unaddressed_chairs=basis["unaddressed_chairs"],
            act_pages=basis["act_pages"],
            act_text_status=basis["act_text_status"],
        ),
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
    return replace(
        projection,
        acts=acts,
        aggregate=aggregate,
        aggregate_basis=basis,
        not_measured_basis=_basis_for_acts(acts),
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
        ("text-bundle", "acts-database", "jsonl", "csv", "review-items"),
        embed_pixels,
    )


def test_act_key_sort_key_is_reading_order_past_ten_pages_and_ten_readings():
    """`p<page>:<n>` sorts as a string by default, so once a page passes ten
    readings -- or a run passes ten pages -- lexicographic order reads reading 10
    before reading 2 and page 10 before page 2. `act_key_sort_key` must restore
    (page, reading) order, put a page's row with no reading after its numbered
    readings, and leave a key it does not describe after every page, in the
    order its own string gives.
    """
    ten_pages = [f"p{page}:1" for page in range(1, 11)]
    twelve_readings = [f"p1:{n}" for n in range(1, 13)]
    keys = ten_pages + twelve_readings[1:]
    reading_order = sorted(keys, key=lambda key: tuple(int(part) for part in key[1:].split(":")))
    assert sorted(keys, key=act_key_sort_key) == reading_order
    # The fixture this test pins is exactly the shape a plain string sort gets
    # wrong -- if it agreed with the lexicographic sort the fixture would prove
    # nothing about the fix.
    assert sorted(keys) != reading_order

    mixed = ["fixture:z", "p10:1", "p2:blank", "p2:1", "fixture:a"]
    assert sorted(mixed, key=act_key_sort_key) == [
        "p2:1",
        "p2:blank",
        "p10:1",
        "fixture:a",
        "fixture:z",
    ]


def test_act_json_records_are_emitted_in_reading_order_past_ten_readings():
    """The production call site (`_act_json_records`, used for the JSONL and
    review-item projections) must order by the parsed key, not the raw string.
    """
    acts = tuple(
        {
            "act_id": f"act-{n}",
            "act_key": f"p1:{n}",
            "category": "delivered",
            "reading": "first reading",
            CANONICAL_TEXT_FIELD: "x",
        }
        for n in (1, 3, 11, 12, 2)
    )
    records = _act_json_records(acts, None)
    assert [record["act_key"] for record in records] == [
        "p1:1",
        "p1:2",
        "p1:3",
        "p1:11",
        "p1:12",
    ]


def test_every_literal_projection_has_the_same_clean_text_and_hash(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)

    with ZipFile(BytesIO(bundle.data)) as archive:
        assert archive.namelist()[0] == EXPORT_MANIFEST_NAME
        assert not [name for name in archive.namelist() if name.startswith("pixels/")]
        text = archive.read(TEXT_REGISTER).decode("utf-8")
        assert "Cǣsar d’Exemple" in text
        # Once as the literal and once as its diplomatic view, which has no doubt to bracket.
        assert text.count(json.dumps("Cǣsar d’Exemple", ensure_ascii=False)) == 2

    manifest = verify_export_bundle(bundle.data, tmp_path / "clean")
    assert manifest["claims"]["status"] == "partial"
    # One line for the one unresolved fact, keyed by the act's key, with its reason.
    assert manifest["claims"]["partial_reasons"] == [
        "act p1:2 is held-for-review: the review remains unresolved"
    ]
    assert manifest["claims"]["pixels"]["resolution_claim"].startswith("reference validity")
    assert _verified_literals(bundle.data, tmp_path / "identity") == {"act-1": "Cǣsar d’Exemple"}


def test_the_manifest_and_every_row_carry_the_runs_lot(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    verify_delivered_bundle(bundle.data, tmp_path / "clean")
    members = _members(bundle.data)
    assert bundle.manifest["run"]["lot"] == _LOT
    for name in ("acts.jsonl", "review-items.jsonl"):
        rows = [json.loads(line) for line in members[name].decode("utf-8").splitlines()]
        assert rows and {row["lot"] for row in rows} == {_LOT}
    with sqlite3.connect(tmp_path / "clean" / "acts.sqlite") as connection:
        assert {lot for (lot,) in connection.execute("SELECT lot FROM acts")} == {_LOT}
    assert f"lot: {_LOT}" in members[TEXT_REGISTER].decode("utf-8").split("\n")[:4]


def test_a_row_naming_another_runs_lot_is_refused(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    rows = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    rows[0]["lot"] = lot_id("f" * 64)
    members["acts.jsonl"] = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    _refresh_manifest_member(members, "acts.jsonl")
    with pytest.raises(SchemaRefusal, match="lot"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_lot_is_written_exactly_when_the_sealed_formats_turn_it_on(tmp_path):
    off = ArmariumFormats(("text-bundle", "acts-database", "jsonl", "review-items"), False, False)
    bundle = build_armarium_bundle(replace(_projection(), lot=None), off, _source_bytes)
    verify_delivered_bundle(bundle.data, tmp_path / "clean")
    members = _members(bundle.data)
    assert bundle.manifest["run"]["lot"] is None
    assert {json.loads(line)["lot"] for line in members["acts.jsonl"].splitlines()} == {None}
    assert not any(line.startswith("lot: ") for line in members[TEXT_REGISTER].decode().split("\n"))
    with pytest.raises(SchemaRefusal, match="lot"):
        build_armarium_bundle(_projection(), off, _source_bytes)
    with pytest.raises(SchemaRefusal, match="lot"):
        build_armarium_bundle(
            replace(_projection(), lot=None), _formats(embed_pixels=False), _source_bytes
        )


def _csv_rows(data: bytes) -> list[dict]:
    text = _members(data)["acts.csv"].decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text, newline="")))


def test_the_csv_is_one_flat_row_per_act_with_the_reading_every_format_gives(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    report = verify_delivered_bundle(bundle.data, tmp_path / "clean")
    assert "csv" in report["verification"]["projection_identity"]["compared_formats"]
    rows = _csv_rows(bundle.data)
    assert [(row["act_key"], row["category"]) for row in rows] == [
        ("p1:1", "delivered"),
        ("p1:2", "held-for-review"),
    ]
    assert rows[0]["canonical_clean_text"] == "Cǣsar d’Exemple"
    assert rows[0]["lot"] == _LOT
    assert rows[1]["canonical_clean_text"] == "" and rows[1]["reason"]


@pytest.mark.parametrize("literal", ['=HYPERLINK("x")', "+1", "-dit", "@SUM(1)", "'quoted", "\tx"])
def test_a_csv_cell_a_spreadsheet_would_run_is_escaped_and_the_reading_kept(tmp_path, literal):
    projection = _projection()
    delivered = {**projection.acts[0], CANONICAL_TEXT_FIELD: literal}
    projection = replace(projection, acts=(delivered, projection.acts[1]))
    bundle = build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)
    assert _csv_rows(bundle.data)[0]["canonical_clean_text"] == "'" + literal
    assert _verified_literals(bundle.data, tmp_path / "clean") == {"act-1": literal}


def test_a_csv_cell_the_writer_would_not_write_is_refused(tmp_path):
    single = ArmariumFormats(("csv",), False)
    bundle = build_armarium_bundle(_projection(), single, _source_bytes)
    verify_delivered_bundle(bundle.data, tmp_path / "clean")
    members = _members(bundle.data)
    members["acts.csv"] = members["acts.csv"].replace(
        b"the review remains unresolved", b"the review was resolved"
    )
    _refresh_manifest_member(members, "acts.csv")
    with pytest.raises(SchemaRefusal, match="acts CSV is not exactly"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "tampered")


def test_the_readers_views_bracket_doubtful_ink_and_the_literal_stays_clean(tmp_path):
    projection = _doubtful_projection()
    bundle = build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)
    shown = "[illegible] Cǣsar [d’Exemple?]"
    lines = _members(bundle.data)[TEXT_REGISTER].decode("utf-8").split("\n")
    assert lines[lines.index("diplomatic:") + 1] == json.dumps(shown, ensure_ascii=False)
    assert _csv_rows(bundle.data)[0]["diplomatic_text"] == shown
    literal = projection.acts[0][CANONICAL_TEXT_FIELD]
    assert _verified_literals(bundle.data, tmp_path / "clean") == {"act-1": literal}


def _doubtful_projection() -> ArmariumProjection:
    """`act-1` read with a leading gap and `d’Exemple` doubtful: 10 of 15 doubtful or unread."""
    text, report = read_doubt_marks("[[?]] Cǣsar [[d’Exemple|d’Example]]")
    layer = {
        "uncertain_spans": report["uncertain_spans"],
        "gaps": report["gaps"],
        "self_revisions": None,
        "assessment": _ASSESSED,
        "lectio_kind": "page-read",
    }
    return _damaged_delivered(
        _projection(), text_status="partial", canonical_clean_text=text, uncertainty=layer
    )


def test_the_doubt_share_of_each_act_and_page_is_recorded_and_recounted(tmp_path):
    bundle = build_armarium_bundle(
        _doubtful_projection(), _formats(embed_pixels=False), _source_bytes
    )
    claim = bundle.manifest["claims"]["doubt_share"]
    assert claim["status"] == "measured"
    assert claim["acts"] == [
        {
            "act_id": "act-1",
            "act_key": "p1:1",
            "page_ordinal": 1,
            "doubtful_or_unread": 10,
            "out_of": 15,
        }
    ]
    assert claim["pages"] == [{"ordinal": 1, "doubtful_or_unread": 10, "out_of": 15}]
    row = _csv_rows(bundle.data)[0]
    assert (row["doubtful_or_unread"], row["out_of"]) == ("10", "15")
    verify_delivered_bundle(bundle.data, tmp_path / "clean")


@pytest.mark.parametrize("count", ["acts", "pages"])
def test_a_doubt_share_count_changed_in_the_manifest_is_refused(tmp_path, count):
    bundle = build_armarium_bundle(
        _doubtful_projection(), _formats(embed_pixels=False), _source_bytes
    )
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["claims"]["doubt_share"][count][0]["doubtful_or_unread"] = 0
    _refresh_manifest(members, manifest)
    with pytest.raises(SchemaRefusal, match="doubt share"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "tampered")


def test_a_reading_over_the_doubt_limit_is_exported_only_after_it_was_held():
    """The export's own check that the Perlector's hold was not skipped."""
    armarium = load_stage("7_armarium")
    projection = _doubtful_projection()
    act = projection.acts[0]
    row = {"act_key": "p1:1", "hold_codes": []}
    text, layer = act[CANONICAL_TEXT_FIELD], act["uncertainty"]
    # 10 of 15 is 6666.67 basis points, compared exactly (10 * 10000 > limit * 15):
    # 6666 is the highest limit it exceeds and 6667 the lowest it does not.
    with pytest.raises(FatalAccounting, match="never held 'doubt-share-high'"):
        armarium.require_doubt_hold(row, text, layer, 6666)
    released = {**row, "hold_codes": ["doubt-share-high"]}
    armarium.require_doubt_hold(released, text, layer, 6666)
    armarium.require_doubt_hold(row, text, layer, 6667)


def test_a_partial_runs_text_bundle_says_it_is_partial_and_names_what_it_lacks(tmp_path):
    """A reader of readings.txt alone sees the run's status and every reading not
    delivered on its pages, text-free, rather than a file that reads as complete."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    lines = _members(bundle.data)[TEXT_REGISTER].decode("utf-8").split("\n")
    assert lines[1:4] == [
        "run-status: partial (EXPORT_MANIFEST.json claims.partial_reasons says why)",
        f"lot: {_LOT}",
        "folder-readings: 1 delivered, 1 not delivered",
    ]
    stub = lines.index("## NOT DELIVERED p1:2 (act-2)")
    assert lines[stub : stub + 4] == [
        "## NOT DELIVERED p1:2 (act-2)",
        "not-delivered: act held-for-review",
        'not-delivered-reason: "the review remains unresolved"',
        "",
    ]


def test_a_partial_runs_acts_database_says_whose_run_it_is_and_that_it_is_partial(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    verify_export_bundle(bundle.data, tmp_path / "clean")
    with sqlite3.connect(tmp_path / "clean" / "acts.sqlite") as connection:
        metadata = dict(connection.execute("SELECT key, value FROM export_metadata"))
    assert metadata["run_status"] == "partial"
    assert json.loads(metadata["partial_reasons"]) == bundle.manifest["claims"]["partial_reasons"]
    assert json.loads(metadata["run"]) == bundle.manifest["run"]


@pytest.mark.parametrize(
    "key, value",
    [
        ("run_status", "complete"),
        ("partial_reasons", "[]"),
        ("run", '{"fixture_id":"another"}'),
    ],
)
def test_an_acts_database_that_misstates_its_run_is_refused(tmp_path, key, value):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    database = tmp_path / "tampered.sqlite"
    database.write_bytes(members["acts.sqlite"])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE export_metadata SET value = ? WHERE key = ?", (value, key))
    members["acts.sqlite"] = database.read_bytes()
    _refresh_manifest_member(members, "acts.sqlite")
    with pytest.raises(SchemaRefusal, match="does not name the package's run"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def _edited_text_bundle(data: bytes, edit) -> bytes:
    members = _members(data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    edit(lines)
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)
    return _zip_bytes(members)


def _stub_first(lines: list[str]) -> None:
    """The NOT DELIVERED section moved ahead of the delivered act's, content intact."""
    stub = lines.index("## NOT DELIVERED p1:2 (act-2)")
    section = lines[stub : stub + 4]
    del lines[stub : stub + 4]
    lines[4:4] = section


def _drop_stub(lines: list[str]) -> None:
    stub = lines.index("## NOT DELIVERED p1:2 (act-2)")
    del lines[stub : stub + 4]


@pytest.mark.parametrize(
    "edit, refusal",
    [
        (
            lambda lines: lines.__setitem__(1, "run-status: complete"),
            "is not exactly what this build writes",
        ),
        (
            lambda lines: lines.__setitem__(2, "folder-readings: 1 delivered, 0 not delivered"),
            "is not exactly what this build writes",
        ),
        (_drop_stub, "is not exactly what this build writes"),
        (lambda lines: lines.insert(4, "a line no writer wrote"), "is not exactly what"),
        (
            lambda lines: lines.__setitem__(0, "# Armarium text bundle — source folder: other"),
            "is not exactly what",
        ),
        (_stub_first, "is not exactly what"),
    ],
    ids=[
        "status-made-complete",
        "count-edited",
        "stub-dropped",
        "free-line-inserted",
        "title-edited",
        "section-reordered",
    ],
)
def test_a_text_bundle_that_hides_a_partial_run_is_refused(tmp_path, edit, refusal):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    with pytest.raises(SchemaRefusal, match=refusal):
        verify_export_bundle(_edited_text_bundle(bundle.data, edit), tmp_path / "clean")


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
        "coverage_records": {"p1:1": original.aggregate_basis["coverage_records"]["p1:1"]},
        "act_pages": {"p1:1": [1]},
    }
    projection = replace(
        original,
        acts=acts,
        expected_acts=1,
        aggregate_basis=basis,
        not_measured_basis=_basis_for_acts(acts),
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


def test_a_required_claim_moves_the_manifest_schema_identity(tmp_path):
    """An older identity may not describe a newer closed claim set.

    ``claims.ink_map`` took the manifest from v2 to v3, ``claims.not_measured``
    took it from v3 to v5, the required Ink Map unmeasurable-page census took
    it from v5 to v7, and the page path's claims took it to v9 and v10. Each
    has the same reason: a required claim a stale
    reader has no field for would be presented as a bundle that does not carry
    it. Old and new closed shapes need different identities rather than two
    incompatible meanings of one.
    """
    members = _members(
        build_armarium_bundle(
            _otherwise_complete(), _formats(embed_pixels=False), _source_bytes
        ).data
    )
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    assert manifest["schema"] == "armarium-export-manifest.v13"

    for stale in (
        "armarium-export-manifest.v2",
        "armarium-export-manifest.v3",
        "armarium-export-manifest.v5",
        "armarium-export-manifest.v6",
        "armarium-export-manifest.v7",
        "armarium-export-manifest.v8",
        "armarium-export-manifest.v9",
        "armarium-export-manifest.v12",
    ):
        manifest["schema"] = stale
        _refresh_manifest(members, manifest)
        with pytest.raises(SchemaRefusal, match="no recognized EXPORT_MANIFEST schema"):
            verify_export_bundle(_zip_bytes(members), tmp_path / f"stale-{stale[-2:]}")


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


def test_the_recorded_absolute_gate_decides_below_the_fraction_gate(tmp_path):
    """The row's page-specific gate, including its inclusive endpoint, is used."""
    total = 200_000
    assert 2_000 / total < MINIMUM_FRACTION_OUTSIDE_COVERAGE
    for outside, held in ((1_999, []), (2_000, [1])):
        bundle = build_armarium_bundle(
            _otherwise_complete(ink_map_pages=(_edge_page(outside=outside, total=total),)),
            _formats(embed_pixels=False),
            _source_bytes,
        )
        manifest = verify_export_bundle(bundle.data, tmp_path / f"absolute-{outside}")
        assert manifest["claims"]["ink_map"]["held_pages"] == held, outside
        assert manifest["claims"]["status"] == ("partial" if held else "complete"), outside


@pytest.mark.parametrize(
    "gate", ["substantial_ink_pixels", "minimum_ink_pixels", "minimum_fraction_outside_bp"]
)
def test_a_zero_recorded_gate_is_refused_before_it_can_hold_every_page(gate):
    row = _edge_page(outside=0)
    row["remeasured"][gate] = 0
    with pytest.raises(SchemaRefusal, match="invalid ink-map re-measurement"):
        build_armarium_bundle(
            _otherwise_complete(ink_map_pages=(row,)),
            _formats(embed_pixels=False),
            _source_bytes,
        )


def test_a_mismatched_noise_floor_across_pages_is_refused():
    """`minimum_ink_pixels`/`minimum_fraction_outside_bp` are the run's single
    sealed `[coverage_audit.noise_floor]`, passed through unchanged for every
    page (unlike `substantial_ink_pixels`, which legitimately scales per
    page). A bundle whose flagged pages disagree on either field cannot have
    come from one honest run and must be refused, not silently accepted with
    one page's hold decided by the wrong value."""
    first = _edge_page(1, outside=5_000)
    second = _edge_page(2, outside=5_000)
    second["remeasured"]["minimum_ink_pixels"] += 1
    with pytest.raises(SchemaRefusal, match="more than one sealed noise floor"):
        _validate_ink_map_pages([first, second], "test bundle")


def test_a_mismatched_fraction_gate_across_pages_is_refused():
    """The other half of the same sealed pair, checked independently."""
    first = _edge_page(1, outside=5_000)
    second = _edge_page(2, outside=5_000)
    second["remeasured"]["minimum_fraction_outside_bp"] += 1
    with pytest.raises(SchemaRefusal, match="more than one sealed noise floor"):
        _validate_ink_map_pages([first, second], "test bundle")


def test_matching_noise_floors_across_pages_are_accepted():
    """What an honest run always writes -- the same sealed noise floor on
    every flagged row -- must not be refused."""
    first = _edge_page(1, outside=5_000)
    second = _edge_page(2, outside=0)
    assert _validate_ink_map_pages([first, second], "test bundle") == [first, second]


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
    with pytest.raises(SchemaRefusal, match="ink-map claim does not match"):
        verify_export_bundle(_zip_bytes(forged), tmp_path / "forged-green")


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
    """Absence of a measurement is recorded as absence."""
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
                            "substantial_ink_pixels": 2_000,
                            "minimum_ink_pixels": MINIMUM_INK_PIXELS,
                            "minimum_fraction_outside_bp": MINIMUM_FRACTION_OUTSIDE_BP,
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
    formats = ArmariumFormats(("review-items",), embed_pixels=False)
    bundle = build_armarium_bundle(_projection(), formats, _source_bytes)
    manifest = json.loads(_members(bundle.data)[EXPORT_MANIFEST_NAME])

    assert manifest["claims"]["uncertainty"] == {
        "status": "not-applicable",
        "offset_unit": "unicode-code-point",
        "carried_by": [],
    }


def test_manifest_refuses_available_uncertainty_with_no_literal_carrier(tmp_path):
    formats = ArmariumFormats(("review-items",), embed_pixels=False)
    bundle = build_armarium_bundle(_projection(), formats, _source_bytes)
    members = _members(bundle.data)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["claims"]["uncertainty"]["status"] = "canonical-unicode-codepoint-offsets"
    _refresh_manifest(members, manifest)

    with pytest.raises(SchemaRefusal, match="canonical carriage claim"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_markup_like_characters_in_a_literal_do_not_refuse_or_change_it(tmp_path):
    projection = _projection()
    literal = r"Act ⟨literal⟩, gap glyphs ⟦not markup⟧, and a \\ path"
    delivered = {**projection.acts[0], "canonical_clean_text": literal}
    bundle = build_armarium_bundle(
        replace(projection, acts=(delivered, projection.acts[1])),
        _formats(embed_pixels=False),
        _source_bytes,
    )

    assert _verified_literals(bundle.data, tmp_path) == {"act-1": literal}


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

    assert _verified_literals(bundle.data, tmp_path / name) == {"act-1": literal}


def test_compare_literal_projections_refuses_an_unhandled_literal_format(tmp_path, monkeypatch):
    """A fifth literal format with no comparison branch built for it here must
    refuse by name, not fall silently out of `projections` and out of the
    identity check the branch above it exists to run.
    """
    import armarium_export

    clean_root = tmp_path / "clean"
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    verify_export_bundle(bundle.data, clean_root)

    monkeypatch.setattr(
        armarium_export,
        "_LITERAL_TEXT_FORMATS",
        (*armarium_export._LITERAL_TEXT_FORMATS, "xml"),
    )
    unhandled_formats = SimpleNamespace(
        formats=("text-bundle", "acts-database", "jsonl", "csv", "xml")
    )
    with pytest.raises(SchemaRefusal, match="no comparison built for literal format 'xml'"):
        armarium_export._compare_literal_projections(clean_root, unhandled_formats)


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
        verify_delivered_bundle(tampered, tmp_path / "identity")


def test_projection_identity_refuses_a_self_consistent_package_with_drifted_uncertainty(tmp_path):
    """The same drift class as the sibling test above, one field over.

    A writer that changed only `uncertainty` -- never touching `canonical_clean_text`
    or its hash -- would pass the literal-text identity check by construction: the
    text is untouched. Uncertainty is a projected reading beside that text, not a
    decoration outside the one-reading guarantee, so a format that silently
    drifted on it alone must fail identity exactly as a drifted literal would.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    records = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    records[0]["uncertainty"] = {
        "lectio_kind": "page-read",
        # Cap-projection-shaped on purpose (no alternatives), so this forged
        # copy stays a layer the pipeline could legitimately have written under
        # a reader with no doubt channel; what the test is about is that it
        # differs from the layer every other format carries.
        "uncertain_spans": [{"start": 0, "end": 1, "alternatives": [], "confidence": "low"}],
        "gaps": [],
        "self_revisions": None,
        "assessment": _NOT_ASSESSED,
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
        verify_delivered_bundle(tampered, tmp_path / "identity")


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
        verify_delivered_bundle(_zip_bytes(members), tmp_path)


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
        verify_delivered_bundle(_zip_bytes(members), tmp_path)


def test_text_bundle_refuses_a_second_literal_that_would_orphan_its_uncertainty(tmp_path):
    """The layer's anchor cannot be swapped out from under it after it is checked.

    `uncertainty:` validates against the literal already parsed. A section that
    then declared a *second* `canonical_clean_text:` recorded the new literal
    beside the first literal's layer -- offsets into a text this act no longer
    carries -- and every remaining check passed: the second literal has its own
    valid hash line. Two or more
    literal formats show the drift as a projection-identity mismatch, but a
    package may legally select the text bundle as its one literal format, and
    there this section is the whole reading of the act.
    """
    formats = ArmariumFormats(("text-bundle", "review-items"), False)
    original = _projection()
    literal = original.acts[0]["canonical_clean_text"]
    delivered = {
        **original.acts[0],
        "uncertainty": {
            "lectio_kind": "page-read",
            "uncertain_spans": [
                {"start": 0, "end": len(literal), "alternatives": ["?"], "confidence": "low"}
            ],
            "gaps": [],
            "self_revisions": None,
            "assessment": _ASSESSED,
        },
    }
    bundle = build_armarium_bundle(
        replace(
            original,
            acts=(delivered, original.acts[1]),
            not_measured_basis=_basis_for_acts((delivered, original.acts[1])),
        ),
        formats,
        _source_bytes,
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
        verify_delivered_bundle(_zip_bytes(members), tmp_path)


def test_text_bundle_refuses_uncertainty_valid_only_for_a_different_acts_literal(tmp_path):
    """Valid JSON and valid offsets for some other act do not authorize this act."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    marker = lines.index("uncertainty:")
    own_literal = _projection().acts[0]["canonical_clean_text"]
    other_literal = own_literal + " belongs to a different act"
    other_layer = {
        "lectio_kind": "page-read",
        "uncertain_spans": [
            {
                "start": len(own_literal),
                "end": len(own_literal) + 1,
                "alternatives": ["?"],
                "confidence": "low",
            }
        ],
        "gaps": [],
        "self_revisions": None,
        "assessment": _ASSESSED,
    }
    assert validate_uncertainty(other_layer, other_literal) == other_layer
    lines[marker + 1] = json.dumps(other_layer, ensure_ascii=False, sort_keys=True)
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="does not anchor to its own act's literal"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path)


# Ten thousand levels of nesting around a 4,301-digit integer. CPython 3.12
# refuses the nesting with `RecursionError` before it reaches the integer;
# 3.14's decoder no longer recurses on the C stack and walks all ten thousand
# levels, then refuses the integer with `ValueError` at the interpreter's
# integer-string limit. Either way the reader's widened arm answers, which is
# the invariant these tests pin; a bare nesting bomb pinned only the 3.12 path.
_PATHOLOGICALLY_NESTED_JSON = b"[" * 10_000 + b"9" * 4301 + b"]" * 10_000


def test_a_deeply_nested_acts_jsonl_row_is_refused_by_name_not_a_recursion_error(tmp_path):
    """Every Armarium JSONL reader widens `except json.JSONDecodeError` to
    `(UnicodeDecodeError, ValueError, RecursionError)`; this pins the
    representative one (`_jsonl_act_records`) against a ~10k-deep row, the
    same failure `common/chandra_layout.py` guards.
    """
    path = tmp_path / "acts.jsonl"
    path.write_bytes(_PATHOLOGICALLY_NESTED_JSON)
    with pytest.raises(SchemaRefusal, match="an acts JSONL row is not JSON"):
        _jsonl_act_records(path, [])


def test_a_huge_integer_in_an_acts_jsonl_row_is_refused_by_name(tmp_path):
    """A 4,301-digit integer literal is otherwise well-formed JSON.

    CPython's own integer-string-conversion limit (4,300 digits by default)
    turns the scanner's `int()` call into a bare `ValueError` -- not
    `json.JSONDecodeError` -- once a literal crosses it.
    """
    path = tmp_path / "acts.jsonl"
    path.write_bytes(b'{"extra":' + b"9" * 4301 + b"}")
    with pytest.raises(SchemaRefusal, match="an acts JSONL row is not JSON"):
        _jsonl_act_records(path, [])


def test_a_deeply_nested_retained_reference_is_refused_by_name_not_a_recursion_error():
    """`_verify_retained_references` walks the *already-parsed* Python
    structure with its own separate recursion, so a row shallow enough to
    parse (e.g. under the ~10k-deep JSON decoder limit pinned above) but with
    a deeply nested value inside a field this walker recurses into (any
    dict/list/tuple value, not only 'evidence') must still be refused by name
    rather than reaching callers as a bare `RecursionError`. Testing the
    shared `_verify_retained_references_bounded` wrapper directly, once,
    covers all five call sites that use it (acts JSONL, acts database,
    review-items JSONL, act-citation evidence, and
    `_export_bundle`'s sources.json check)."""
    nested: object = "leaf"
    for _ in range(5000):
        nested = {"nested": nested}
    with pytest.raises(SchemaRefusal, match="nests too deeply for its availability walk"):
        _verify_retained_references_bounded({"field": nested})


def test_jsonl_uncertainty_status_may_not_contradict_the_layer_beside_it(tmp_path):
    """The declaration a recipient reads is checked against the payload it describes.

    Cross-format identity compares layer to layer; it never reads
    `uncertainty_status`, so without this check a delivered JSONL row could carry
    a valid canonical layer while telling every reader of that row there was none.
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
    formats = ArmariumFormats(("jsonl", "review-items"), False)
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
        "uncertainty": {
            "lectio_kind": "page-read",
            "uncertain_spans": [],
            "gaps": [],
            "self_revisions": None,
            "assessment": _NOT_ASSESSED,
        },
    }
    with pytest.raises(SchemaRefusal, match="may not carry an uncertainty layer"):
        build_armarium_bundle(
            replace(original, acts=(original.acts[0], held)),
            _formats(embed_pixels=False),
            _source_bytes,
        )


def test_the_delivered_gate_asks_both_questions_the_manifest_claims_were_asked(tmp_path):
    """One reading per act on the path the product actually leaves by.

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
        "csv",
        "jsonl",
        "text-bundle",
    ]
    with pytest.raises(SchemaRefusal, match="projection differs"):
        verify_delivered_bundle(tampered, tmp_path / "delivered")

    report = verify_delivered_bundle(bundle.data, tmp_path / "intact")["verification"]
    assert report["projection_identity"] == {
        "status": "verified",
        "compared_formats": ["acts-database", "csv", "jsonl", "text-bundle"],
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
    established -- a second reading of the act inside the same package, when
    every format must show one reading per act.
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
                VALUES (1, 'caesar dexemple and fabricated terms');
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
    that was never tampered with. An act refused at the terminal gate for
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
    pipeline or from a third party -- the quarantine rule governs
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
        "lot": _LOT,
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


def test_export_run_identity_never_touches_the_refusing_fixture_accessor_on_a_real_run():
    """The unit's central claim, pinned rather than asserted only in prose and CONTRACT.md.

    `StageContext.fixture` refuses on a real run (`common/stage.py`); if
    `export_run_identity` read it unconditionally instead of deciding the route
    from `submission_identity(context.run)` first, this would raise a
    `ContractError` instead of returning a `submission_id`-shaped identity.
    """
    armarium = load_stage("7_armarium")
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
    armarium = load_stage("7_armarium")
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
    manifest["self_hash"] = self_hash(manifest)
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)

    with pytest.raises(SchemaRefusal, match="selected formats.*unexpected"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_a_reconstruction_member_with_no_recorded_reconstruction_is_refused(tmp_path):
    """The writer adds `coniector.jsonl` only when `sources.json` records a
    reconstruction, so an empty one planted beside none is a member nothing
    promised."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    assert "reconstructions" not in json.loads(members["sources.json"])
    members["coniector.jsonl"] = b""
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["members"] = sorted(
        [
            *manifest["members"],
            {"bytes": 0, "path": "coniector.jsonl", "sha256": digest_bytes(b"")},
        ],
        key=lambda row: row["path"],
    )
    _refresh_manifest(members, manifest)

    with pytest.raises(SchemaRefusal, match=r"selected formats.*unexpected=\['coniector.jsonl'\]"):
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
            provenance=None,
            source_regions=[],
        )
        held.append(record)
    bundle = build_armarium_bundle(
        replace(
            original,
            acts=tuple(held),
            not_measured_basis=_basis_for_acts(tuple(held)),
            aggregate_basis={**original.aggregate_basis, "act_text_status": {}},
            aggregate={
                "status": "partial",
                "reasons": ["act p1:1 is held-for-review", "act p1:2 is held-for-review"],
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
            not_measured_basis=_basis_for_acts(held, sealed_pages=2),
            pages=pages,
            ink_map_pages=ink_map_pages,
            page_accounting=_page_accounting(1, 2),
            source_manifest=source_manifest,
            aggregate={
                "status": "partial",
                "reasons": ["act p1:1 is held-for-review", "act p1:2 is held-for-review"],
                "by_category": {"held-for-review": 2},
                "by_page_outcome": {"sealed": 2},
            },
            aggregate_basis={
                **original.aggregate_basis,
                "act_pages": {"p1:1": [1, 2], "p1:2": [1, 2]},
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

    assert literal == "Cǣsar d’Exemple"
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


def test_bundle_bytes_are_deterministic_for_the_same_sealed_projection():
    first = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    second = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    assert first.data == second.data


def test_the_terminal_ledger_partitions_sources_pages_and_acts_totally(tmp_path):
    """A total partition, not an act-only one.

    One submitted source, one sealed page and two acts is four units, every one of
    them carrying a closed category. The counts are checked to sum because a partition
    that misses a unit loses it silently.
    """
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    ledger = json.loads(_members(bundle.data)[EXPORT_MANIFEST_NAME])["claims"]["terminal_ledger"]

    assert ledger["by_unit_type"] == {"source": 1, "page": 1, "act": 2, "other": 0}
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
    assert ledger["unresolved_reasons"] == [
        "act p1:2 is held-for-review: the review remains unresolved"
    ]
    assert "one unit per page or frame" in ledger["granularity_limit"]


def test_page_ledger_category_inherits_confirmed_blank_and_excluded_when_every_act_agrees():
    """The two page categories a page inherits besides delivered and held.

    Driven against the pure function because nothing upstream emits either outcome
    yet, so a full-projection fixture would be synthetic in exactly the same way this
    call is. Neither category is ever *inferred*: both are inherited only when every
    act cut from the page already carries it.
    """
    assert _page_ledger_category(1, ["confirmed-blank"], other_categories=[]) == (
        ArmariumCategory.CONFIRMED_BLANK.value,
        None,
    )
    assert _page_ledger_category(
        2, ["confirmed-blank", "confirmed-blank"], other_categories=[]
    ) == (
        ArmariumCategory.CONFIRMED_BLANK.value,
        None,
    )
    assert _page_ledger_category(3, ["excluded-with-approval"], other_categories=[]) == (
        ArmariumCategory.EXCLUDED_WITH_APPROVAL.value,
        None,
    )
    # A mix with no delivered act present is neither category on its own -- held
    # for a human, exactly like any other non-unanimous, non-delivered mix.
    category, reason = _page_ledger_category(
        4, ["confirmed-blank", "excluded-with-approval"], other_categories=[]
    )
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
            "p1:1": ArmariumCategory.CONFIRMED_BLANK,
            "p1:2": ArmariumCategory.EXCLUDED_WITH_APPROVAL,
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
            not_measured_basis=_basis_for_acts(acts),
            aggregate=aggregate,
            aggregate_basis={**original.aggregate_basis, "act_text_status": {}},
        ),
        _formats(embed_pixels=False),
        _source_bytes,
    )

    assert bundle.manifest["claims"]["status"] == "partial"
    assert bundle.manifest["claims"]["partial_reasons"] == [
        "page 1 delivered no act; its acts are confirmed-blank, excluded-with-approval"
    ]
    text = _members(bundle.data)[TEXT_REGISTER].decode("utf-8")
    assert "## NOT DELIVERED page 1\nnot-delivered: page held-for-review\n" in text
    assert (
        "## NOT DELIVERED p1:2 (act-2)\nnot-delivered: act excluded-with-approval\n"
        'not-delivered-reason: null\nnot-delivered-approval: "approvals/exclusion-1"\n'
    ) in text
    # Another fact about the same page (a person's clearance of it) does not
    # stand in for this one: each fact is its own string, named once.
    cleared = run_aggregate(
        {
            "p1:1": ArmariumCategory.CONFIRMED_BLANK,
            "p1:2": ArmariumCategory.EXCLUDED_WITH_APPROVAL,
        },
        original.aggregate_basis["coverage_records"],
        {1: dict(original.pages[0])},
        unaddressed_chairs=[],
        act_pages=original.aggregate_basis["act_pages"],
        act_text_status={},
        review_clearances=[
            {
                "scope": "page",
                "subject": 1,
                "page": 1,
                "decision": "no-missed-act",
                "cleared": ["merged-detection"],
            }
        ],
    )
    [clearance] = cleared["reasons"]
    assert clearance.startswith("page 1 ")
    ledger = _terminal_ledger(
        [
            {
                "act_id": act["act_id"],
                "act_key": act["act_key"],
                "category": act["category"],
                "reason": act["reason"],
                "text_status": None,
            }
            for act in acts
        ],
        [dict(original.pages[0])],
        original.aggregate_basis["act_pages"],
        cleared,
        (),
        [],
    )
    assert ledger["unresolved_reasons"] == [
        clearance,
        "page 1 delivered no act; its acts are confirmed-blank, excluded-with-approval",
    ]
    # And the clean-machine verifier recomputes the same disagreement rather than
    # reading the reassuring half of it out of the manifest.
    manifest = verify_export_bundle(bundle.data, tmp_path / "clean")
    assert manifest["claims"]["status"] == "partial"
    assert manifest["aggregate"]["status"] == "complete"


def test_a_refused_source_and_a_silent_page_each_land_in_a_named_set(tmp_path):
    """A door refusal and a sealed page no reading accounts for are the two units
    an act-only partition could not see at all. Neither may be inferred blank."""
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
        page_accounting=_page_accounting(1, 3),
        source_manifest=tuple(
            {
                "ordinal": page["ordinal"],
                "relative_path": page["declared_path"],
                "sha256": page["declared_sha256"],
            }
            for page in pages
        ),
        not_measured_basis=_basis_for_acts(base.acts, sealed_pages=2),
        aggregate=run_aggregate(
            {"p1:1": ArmariumCategory.DELIVERED, "p1:2": ArmariumCategory.HELD_FOR_REVIEW},
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
    assert "counts no reading of it" in units["page:3"]["reason"]
    assert ledger["by_unit_type"] == {"source": 3, "page": 2, "act": 2, "other": 0}
    assert sum(ledger["by_category"].values()) == ledger["unit_count"] == 7
    # The readable text names the refused source and the silent page as well.
    text = _members(bundle.data)[TEXT_REGISTER].decode("utf-8")
    assert (
        "## NOT DELIVERED source 2\nnot-delivered: source refused-with-reason\n"
        'not-delivered-reason: "the submitted bytes were not a readable image"\n'
    ) in text
    assert "## NOT DELIVERED page 3\nnot-delivered: page held-for-review\n" in text
    verify_export_bundle(bundle.data, tmp_path / "clean")
    # Five unresolved units, three facts: each named once, by the act's key or
    # the page's ordinal, never again as the source or page unit beside it.
    assert ledger["unresolved_reasons"] == [
        "act p1:2 is held-for-review: the review remains unresolved",
        "page 2 was refused: the submitted bytes were not a readable image",
        PAGE_READ_SILENT_PAGE_REASON.format(ordinal=3),
    ]


def test_a_section_that_drops_its_last_field_is_refused(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").splitlines()
    last = next(index for index, line in enumerate(lines) if line.startswith("text_status: "))
    del lines[last]
    members[TEXT_REGISTER] = ("\n".join(lines) + "\n").encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="no completed literal record"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


def test_the_manifest_says_whether_projection_identity_was_actually_checked(tmp_path):
    """Below two literal formats, nothing is compared across formats -- say so."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    manifest = json.loads(_members(bundle.data)[EXPORT_MANIFEST_NAME])
    assert manifest["canonical_text"]["identity_verified_across"] == [
        "acts-database",
        "csv",
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


@pytest.mark.hostile_local
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


@pytest.mark.hostile_local
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


@pytest.mark.hostile_local
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


@pytest.mark.hostile_local
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

    assert manifest["schema"] == "armarium-export-manifest.v13"
    assert outside.read_bytes() == b"bytes outside the extraction root"
    assert linked.stat().st_ino != shared_inode


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


def _verified_literals(data: bytes, clean_root: Path) -> dict[str, str]:
    """The delivered literals of a package that verified whole, formats compared."""
    verify_delivered_bundle(data, clean_root)
    return {
        act_id: record[0] for act_id, record in _jsonl_literals(clean_root / "acts.jsonl").items()
    }


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
        "lectio_kind": "page-read",
        "uncertain_spans": [{"start": 0, "end": 1, "alternatives": ["?"], "confidence": "low"}],
        "gaps": [
            {"position": "trailing", "start": len(text), "end": len(text), "witness_evidence": []}
        ],
        "self_revisions": None,
        "assessment": _ASSESSED,
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


# --- The damage record: text_status and the uncertainty layer ---------------------
#
# A delivered act whose reading records unread ink is `partial` in every format, and
# the run's aggregate names it.


def _internal_gap_layer(text: str) -> dict:
    middle = len(text) // 2
    return {
        "lectio_kind": "page-read",
        "uncertain_spans": [],
        "gaps": [{"position": "internal", "start": middle, "end": middle, "witness_evidence": []}],
        "self_revisions": None,
        "assessment": _ASSESSED,
    }


def _partial_projection() -> ArmariumProjection:
    base = _projection()
    literal = base.acts[0][CANONICAL_TEXT_FIELD]
    return _damaged_delivered(base, text_status="partial", uncertainty=_internal_gap_layer(literal))


def test_a_delivered_act_with_a_gap_reaches_every_selected_literal_format(tmp_path):
    """A damaged act is partial in the written product, not only in the projection.

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
    database = tmp_path / "acts.sqlite"
    database.write_bytes(members["acts.sqlite"])
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT text_status FROM acts WHERE act_id = 'act-1'"
        ).fetchone() == ("partial",)

    manifest = verify_export_bundle(bundle.data, tmp_path / "clean")
    assert manifest["aggregate"]["status"] == "partial"
    assert any(
        reason.startswith("act p1:1 was delivered with partial text")
        for reason in manifest["aggregate"]["reasons"]
    ), manifest["aggregate"]["reasons"]
    assert manifest["claims"]["status"] == "partial"


def test_an_empty_reading_is_never_delivered():
    """No text and no gap is `no_readable_text`, which no stage delivers."""
    projection = _damaged_delivered(
        _projection(), text_status="no_readable_text", canonical_clean_text=""
    )
    with pytest.raises(SchemaRefusal, match="no text and no gap"):
        build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)


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
    sources["aggregate_basis"]["act_text_status"] = {"p1:1": "established"}
    members["sources.json"] = canonical_bytes(sources)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["aggregate"] = run_aggregate(
        {"p1:1": ArmariumCategory.DELIVERED, "p1:2": ArmariumCategory.HELD_FOR_REVIEW},
        sources["aggregate_basis"]["coverage_records"],
        {page["ordinal"]: page for page in sources["pages"]},
        unaddressed_chairs=[],
        act_pages=sources["aggregate_basis"]["act_pages"],
        act_text_status={"p1:1": "established"},
    )
    manifest["aggregate_basis"] = sources["aggregate_basis"]
    ledger = _terminal_ledger(
        sources["act_outcomes"],
        sources["pages"],
        sources["aggregate_basis"]["act_pages"],
        manifest["aggregate"],
        (),
        sources["other_outcomes"],
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


def test_projection_identity_refuses_a_package_whose_formats_disagree_about_damage(tmp_path):
    """Two deliverables cannot disagree about the doubt carried with one reading.

    The literal is byte-identical in every format, so the text comparison passes
    by construction; the uncertainty layer is part of the same one reading and
    rides in the same equality check.
    """
    bundle = build_armarium_bundle(
        _partial_projection(), _formats(embed_pixels=False), _source_bytes
    )
    members = _members(bundle.data)
    span = {"start": 0, "end": 1, "alternatives": ["?"], "confidence": "low"}
    rows = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    for row in rows:
        if row["text_status"] is not None:
            row["uncertainty"]["uncertain_spans"] = [span]
    members["acts.jsonl"] = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    _refresh_manifest_member(members, "acts.jsonl")

    tampered = _zip_bytes(members)
    # Package verification alone is green: the edited layer is well-formed, and
    # `partial` is still the honest status for a row that carries its gap.
    verify_export_bundle(tampered, tmp_path / "clean")
    with pytest.raises(SchemaRefusal, match="projection differs"):
        verify_delivered_bundle(tampered, tmp_path / "identity")


def test_the_text_bundle_refuses_a_literal_section_with_no_damage_record(tmp_path):
    """The status is not optional beside a delivered literal, and says so by name."""
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    marker = lines.index("text_status: established")
    del lines[marker]
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="no completed literal record"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path)


def test_the_text_bundle_refuses_two_established_text_statuses_for_one_literal(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    lines = members[TEXT_REGISTER].decode("utf-8").split("\n")
    marker = lines.index("text_status: established")
    lines[marker:marker] = [lines[marker]]
    members[TEXT_REGISTER] = "\n".join(lines).encode("utf-8")
    _refresh_manifest_member(members, TEXT_REGISTER)

    with pytest.raises(SchemaRefusal, match="established-text status has no literal to describe"):
        verify_delivered_bundle(_zip_bytes(members), tmp_path)


# --- `claims.not_measured`: what this run did not measure ---------------------
#
# `DELIVERED` and `aggregate.status == "complete"` are reachable over five
# things nothing measured -- a page whose testimony content coverage was
# recorded unmeasured, a page whose ink was never reconciled, two instruments
# with no producer, and geometry thresholds no sample was taken for. All five
# are recorded somewhere; none of them qualified the word on the deliverable.
# These prove the block is present, closed, and derived rather than constant.


def _manifest_of(projection) -> dict:
    formats = ArmariumFormats(("jsonl",), embed_pixels=False)
    return build_armarium_bundle(projection, formats, lambda _path: b"").manifest


def _block(projection) -> dict:
    return _manifest_of(projection)["claims"]["not_measured"]


def _entry(block: dict, instrument: str) -> dict:
    return next(row for row in block["entries"] if row["instrument"] == instrument)


def test_projection_refuses_a_pass_c_denominator_that_does_not_match_its_sealed_pages():
    projection = _projection()
    basis = _basis_for_acts(projection.acts)
    basis["perlector-pass-c"]["pages_read"] = 2
    basis["perlector-pass-c"]["pages_audit_not_run"] = 2
    with pytest.raises(SchemaRefusal, match="Pass C basis does not count exactly"):
        build_armarium_bundle(
            replace(projection, not_measured_basis=basis),
            _formats(embed_pixels=False),
            _source_bytes,
        )


def test_the_export_names_every_instrument_of_this_build_exactly_once_in_order():
    block = _block(_projection())

    assert block["schema"] == NOT_MEASURED_SCHEMA
    assert [row["instrument"] for row in block["entries"]] == list(NOT_MEASURED_INSTRUMENTS)
    for row in block["entries"]:
        assert set(row) == {"instrument", "status", "detail", "recorded_in"}
        # Where a reader goes to check the row against the evidence.
        assert row["recorded_in"].strip()
    assert (
        _entry(block, "perlector-pass-c")["recorded_in"]
        == "each page's `page-reading` record, field `audit`, and the sealed Perlector audit "
        "policy's `round_cap`, in the retained run"
    )


def test_the_count_is_the_number_of_instruments_that_did_not_measure():
    block = _block(_projection())

    assert block["count"] == sum(1 for row in block["entries"] if row["status"] != "measured")
    assert block["count"] == 4


def test_pass_c_is_declared_unproduced_and_measured_when_it_runs():
    """`declared-unproduced` is the contract's word, not a softer `not-measured`.

    No page-path stage audits a page reading, so every page on a current run
    records `not-run`. A run whose pages were audited is a different status, and
    the block must be able to say so rather than always reporting absence.
    """

    def status(pages_audit_not_run: int, pages_read: int = 2) -> str:
        detail = {
            "pages_read": pages_read,
            "pages_audit_not_run": pages_audit_not_run,
            "sealed_audit_round_cap": 1,
        }
        return _not_measured_status("perlector-pass-c", detail)

    absent = _entry(_block(_projection()), "perlector-pass-c")
    assert absent["status"] == "declared-unproduced"
    assert absent["detail"]["pages_audit_not_run"] == 1
    assert status(2) == "declared-unproduced"
    assert status(0) == "measured"
    assert status(1) == "not-measured"


def test_comparison_bounds_are_measured_only_when_no_delivered_comparison_stopped():
    """A delivered act whose dissent stopped on the step budget or the pair bound
    was delivered without that witness's departures measured; one such act makes
    the instrument `not-measured`."""

    def status(unmeasured: int) -> str:
        detail = {
            "sealed_max_comparison_steps": 1_000,
            "max_comparison_character_pairs": 100_000_000,
            "acts_delivered": 2,
            "acts_with_unmeasured_comparison": unmeasured,
            "unmeasured_act_ids": [f"act-{n}" for n in range(unmeasured)],
        }
        return _not_measured_status("comparison-bounds", detail)

    assert _entry(_block(_projection()), "comparison-bounds")["status"] == "measured"
    assert status(0) == "measured"
    assert status(1) == "not-measured"
    assert status(2) == "not-measured"


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (
            {"acts_with_unmeasured_comparison": 2, "unmeasured_act_ids": ["act-1", "act-2"]},
            "names more unmeasured acts than delivered acts",
        ),
        ({"acts_delivered": 2}, "comparison-bounds basis does not count exactly"),
        (
            {"acts_with_unmeasured_comparison": 1},
            "does not name, in order, exactly the acts it counts",
        ),
        (
            {"acts_with_unmeasured_comparison": 2, "unmeasured_act_ids": ["act-2", "act-1"]},
            "does not name, in order, exactly the acts it counts",
        ),
        (
            {"acts_with_unmeasured_comparison": 1, "unmeasured_act_ids": ["act-elsewhere"]},
            "names an unmeasured act it does not deliver",
        ),
    ],
)
def test_projection_refuses_a_comparison_bounds_basis_that_misstates_its_acts(change, refusal):
    projection = _projection()
    basis = _basis_for_acts(projection.acts)
    basis["comparison-bounds"].update(change)
    with pytest.raises(SchemaRefusal, match=refusal):
        build_armarium_bundle(
            replace(projection, not_measured_basis=basis),
            _formats(embed_pixels=False),
            _source_bytes,
        )


def test_the_uncertainty_instrument_measures_the_readers_that_were_actually_asked():
    """Who was asked decides this instrument's status; the sealed cap is reported beside it.

    The reader's own doubt report is the measurement: `declared-unproduced`
    needs zero assessed readings, and some assessed is a partial measurement
    that may not be reported as a whole one.
    """
    silenced = _entry(_block(_projection()), "perlector-uncertain-spans")
    assert silenced["status"] == "declared-unproduced"
    assert silenced["detail"]["sealed_audit_round_cap"] == 1
    assert silenced["detail"]["acts_assessed"] == 0

    original = _projection()
    assessed = {
        **original.acts[0],
        "uncertainty": {
            **original.acts[0]["uncertainty"],
            "assessment": {"state": "assessed", "problem": None},
        },
    }
    acts = (assessed, *original.acts[1:])
    asked = replace(original, acts=acts, not_measured_basis=_basis_for_acts(acts))
    assert _entry(_block(asked), "perlector-uncertain-spans")["status"] == "measured"

    # The partial readings of this instrument need two delivered acts, or the
    # live configuration's own combination, and every hand-built projection in
    # this file delivers one assessed or unassessed act; the derivation is asked
    # where it lives rather than by inventing a second delivery to reach it.
    assert (
        _not_measured_status(
            "perlector-uncertain-spans",
            {
                "sealed_audit_round_cap": 1,
                "acts_delivered": 2,
                "acts_with_uncertain_spans": 0,
                "acts_assessed": 1,
                "acts_not_assessed": 1,
            },
        )
        == "not-measured"
    )


def test_a_delivered_act_whose_doubt_report_was_broken_is_refused_not_counted():
    """`malformed` is a hold, so a delivered one is a broken tree, never a count.

    `acts_not_assessed` must not count an act whose reader's report could not
    be anchored the same way as one whose reader simply had no doubt channel
    -- two different facts, never folded into one number by subtraction.
    """
    original = _projection()
    broken = {
        **original.acts[0],
        "uncertainty": {
            **original.acts[0]["uncertainty"],
            "assessment": {"state": "malformed", "problem": "the report could not be anchored"},
        },
    }
    acts = (broken, *original.acts[1:])

    with pytest.raises(SchemaRefusal, match="only a reading that was assessed"):
        _manifest_of(replace(original, acts=acts, not_measured_basis=_basis_for_acts(acts)))


def test_the_assessment_counts_must_partition_the_delivered_acts():
    """Two counts of the same population, refused where they do not add up."""
    basis = _test_not_measured_basis()
    basis["perlector-uncertain-spans"]["acts_not_assessed"] = 0

    with pytest.raises(SchemaRefusal, match="do not partition its delivered acts"):
        _manifest_of(replace(_projection(), not_measured_basis=basis))


def test_an_uncalibrated_geometry_configuration_is_a_caveat_on_the_act_boundaries():
    caveat = _entry(_block(_projection()), "designator-geometry-calibration")
    assert caveat["status"] == "not-measured"
    assert caveat["detail"]["configurations"][0]["sample_count"] is None

    calibrated = replace(
        _projection(),
        not_measured_basis=_test_not_measured_basis(
            **{
                "designator-geometry-calibration": {
                    "configurations": [
                        {
                            "configuration": "designator-geometry",
                            "calibrated_for_this_corpus": True,
                            "sample_count": None,
                        },
                        {
                            "configuration": "perlector-protocol",
                            "calibrated_for_this_corpus": True,
                            "sample_count": 7,
                        },
                    ]
                }
            }
        ),
    )
    assert _entry(_block(calibrated), "designator-geometry-calibration")["status"] == "measured"


def test_a_projection_with_no_not_measured_basis_is_refused():
    """A block derived from nothing is the reassuring silence it exists to break."""
    with pytest.raises(SchemaRefusal, match="carries no not-measured basis"):
        build_armarium_bundle(
            replace(_projection(), not_measured_basis=None),
            ArmariumFormats(("jsonl",), embed_pixels=False),
            lambda _path: b"",
        )


def test_a_basis_missing_one_instrument_is_refused_before_a_product_byte_is_written():
    broken = _test_not_measured_basis()
    del broken["perlector-pass-c"]

    with pytest.raises(SchemaRefusal, match="not-measured basis has an unrecognized field set"):
        build_armarium_bundle(
            replace(_projection(), not_measured_basis=broken),
            ArmariumFormats(("jsonl",), embed_pixels=False),
            lambda _path: b"",
        )


def test_a_geometry_basis_cannot_turn_a_string_or_empty_row_set_into_measurement():
    broken = _test_not_measured_basis()
    broken["designator-geometry-calibration"]["configurations"] = []
    with pytest.raises(SchemaRefusal, match="must name 2 configurations"):
        _manifest_of(replace(_projection(), not_measured_basis=broken))

    broken = _test_not_measured_basis()
    broken["designator-geometry-calibration"]["configurations"][0]["calibrated_for_this_corpus"] = (
        "false"
    )
    with pytest.raises(SchemaRefusal, match="untyped values"):
        _manifest_of(replace(_projection(), not_measured_basis=broken))


def test_a_projection_not_measured_basis_refuses_bool_as_an_integer_count():
    broken = _test_not_measured_basis()
    broken["perlector-pass-c"]["pages_read"] = False

    with pytest.raises(SchemaRefusal, match="pages_read.*non-negative integer"):
        _manifest_of(replace(_projection(), not_measured_basis=broken))


@pytest.mark.parametrize(
    ("instrument", "mutate"),
    [
        pytest.param(
            "perlector-pass-c",
            lambda detail: detail.update(pages_read=0, pages_audit_not_run=1),
            id="more-unaudited-pages-than-pages-read",
        ),
        pytest.param(
            "perlector-uncertain-spans",
            lambda detail: detail.update(sealed_audit_round_cap=0, acts_with_uncertain_spans=2),
            id="more-acts-with-spans-than-delivered",
        ),
    ],
)
def test_a_projection_not_measured_basis_refuses_impossible_count_relations(instrument, mutate):
    broken = _test_not_measured_basis()
    mutate(broken[instrument])

    with pytest.raises(SchemaRefusal, match="more .* than"):
        _manifest_of(replace(_projection(), not_measured_basis=broken))


def _resealed_without_not_measured(projection) -> bytes:
    """Rebuild a package whose manifest has had the block removed."""
    formats = ArmariumFormats(("jsonl",), embed_pixels=False)
    bundle = build_armarium_bundle(projection, formats, lambda _path: b"")
    with ZipFile(BytesIO(bundle.data)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members[EXPORT_MANIFEST_NAME].decode("utf-8"))
    del manifest["claims"]["not_measured"]
    manifest["self_hash"] = self_hash({k: v for k, v in manifest.items() if k != "self_hash"})
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)
    return _zip_bytes(members)


def test_the_export_schema_refuses_a_package_that_omits_the_block(tmp_path):
    """Closed, so a bundle cannot quietly stop carrying its own caveats."""
    with pytest.raises(SchemaRefusal, match="unrecognized field set"):
        verify_export_bundle(_resealed_without_not_measured(_projection()), tmp_path / "clean")


def test_a_basis_naming_uncertain_spans_no_reader_assessed_is_refused():
    basis = _test_not_measured_basis()
    basis["perlector-uncertain-spans"]["acts_with_uncertain_spans"] = 1
    with pytest.raises(SchemaRefusal, match="only a reader's own doubt report mints a span"):
        _manifest_of(replace(_projection(), not_measured_basis=basis))


def test_perlector_basis_counts_must_reconcile_with_the_projected_acts():
    basis = _test_not_measured_basis()
    # The partition rule is a separate refusal asked first, so the counts stay
    # coherent with each other and only their reconciliation with the acts moves.
    basis["perlector-uncertain-spans"]["acts_delivered"] = 0
    basis["perlector-uncertain-spans"]["acts_not_assessed"] = 0
    with pytest.raises(SchemaRefusal, match="does not exactly reconcile"):
        _manifest_of(replace(_projection(), not_measured_basis=basis))


_HEAD_TEXT, _TAIL_TEXT = "Cǣsar d’Amo-", "urs fils"
_FORMATS_WITHOUT_TEXT = ArmariumFormats(("review-items",), False)


def _later_page(
    base: ArmariumProjection, ordinal: int, folder: str = "register"
) -> tuple[dict, dict]:
    page = _source_bytes(f"1_exemplar/blobs/sha256/page-{ordinal}")
    crop = _source_bytes(f"2_designator/blobs/sha256/crop-{ordinal}")
    row = {
        **base.pages[0],
        "ordinal": ordinal,
        "declared_path": f"{folder}/folio-{ordinal}.png",
        "declared_sha256": digest_bytes(page),
        "page_id": f"pg-{ordinal}",
        "image_path": f"1_exemplar/blobs/sha256/page-{ordinal}",
        "image_sha256": digest_bytes(page),
    }
    first = base.acts[0]["source_regions"][0]
    page_fields = {"source_page_ordinal": ordinal, "source_page_id": f"pg-{ordinal}"}
    region = {
        **first,
        **page_fields,
        "region_id": f"rgn-{ordinal}",
        "image_path": f"2_designator/blobs/sha256/crop-{ordinal}",
        "image_sha256": digest_bytes(crop),
        "declared_path": row["declared_path"],
        "declared_sha256": row["declared_sha256"],
        "transform": {**first["transform"], **page_fields},
    }
    return row, region


_CANDIDATE_REF = {
    "relative_path": "2_designator/artifacts/continuation-candidate/c.json",
    "sha256": "d" * 64,
}
_SPAN = {"start": 0, "end": 1, "alternatives": ["?"], "confidence": "low"}


def _joined(
    *,
    head_act_ids=("act-1",),
    held_head=False,
    pages=(1, 2),
    formats=None,
    chained=False,
    head_uncertainty=None,
    candidate_ref=_CANDIDATE_REF,
    tail_folder="register",
) -> ArmariumProjection:
    """`one` ends page 1 and `three` opens page 2; `four` is a second act on page 1.

    `chained` adds `five` on page 3, joined to `three` as well.
    """
    base = _otherwise_complete()
    page_two, region_two = _later_page(base, 2, tail_folder)
    page_three, region_three = _later_page(base, 3)
    delivered = base.acts[0]
    head = {**delivered, CANONICAL_TEXT_FIELD: _HEAD_TEXT}
    if head_uncertainty is not None:
        head["uncertainty"] = head_uncertainty
    if held_head:
        head = {
            **{key: head[key] for key in ("act_id", "act_key", "evidence_refs", "reading")},
            "category": "held-for-review",
            CANONICAL_TEXT_FIELD: None,
            "provenance": None,
            "source_regions": [],
            "reason": "the reading was truncated at the page edge",
        }
    acts = [
        head,
        {
            **delivered,
            "act_id": "act-3",
            "act_key": "p2:1",
            CANONICAL_TEXT_FIELD: _TAIL_TEXT,
            "source_regions": [region_two],
        },
        {**delivered, "act_id": "act-4", "act_key": "p1:3", CANONICAL_TEXT_FIELD: "Anno 1690"},
    ]
    act_pages = {"p1:1": [1], "p2:1": [2], "p1:3": [1]}
    all_pages = [*base.pages, page_two]
    candidates = [(head_act_ids, pages, ["act-3"])]
    if chained:
        acts.append(
            {
                **delivered,
                "act_id": "act-5",
                "act_key": "p3:1",
                CANONICAL_TEXT_FIELD: "et sa femme",
                "source_regions": [region_three],
            }
        )
        act_pages["p3:1"] = [3]
        all_pages.append(page_three)
        candidates.append((["act-3"], (2, 3), ["act-5"]))
    acts = tuple(acts)
    coverage = base.aggregate_basis["coverage_records"]["p1:1"]
    basis = {
        **base.aggregate_basis,
        "coverage_records": {act["act_key"]: coverage for act in acts},
        "act_pages": act_pages,
        "act_text_status": {
            act["act_key"]: act["text_status"] for act in acts if act["category"] == "delivered"
        },
    }
    joins = tuple(
        continuation_join_row(
            join_id=f"join-{join_pages[0]}-{join_pages[1]}-{index}",
            candidate_ref=candidate_ref,
            head_page_ordinal=join_pages[0],
            tail_page_ordinal=join_pages[1],
            head_act_ids=list(heads),
            tail_act_ids=tails,
            delivered_texts={
                act["act_id"]: act[CANONICAL_TEXT_FIELD]
                for act in acts
                if act["category"] == "delivered"
            },
            selected_formats=(formats or _formats(embed_pixels=False)).formats,
        )
        for index, (heads, join_pages, tails) in enumerate(candidates)
    )
    aggregate = run_aggregate(
        {act["act_key"]: ArmariumCategory(act["category"]) for act in acts},
        basis["coverage_records"],
        {page["ordinal"]: page for page in all_pages},
        unaddressed_chairs=basis["unaddressed_chairs"],
        act_pages=basis["act_pages"],
        act_text_status=basis["act_text_status"],
        continuation_joins=joins,
    )
    return replace(
        base,
        acts=acts,
        pages=tuple(all_pages),
        source_manifest=tuple(
            {
                "ordinal": page["ordinal"],
                "relative_path": page["declared_path"],
                "sha256": page["declared_sha256"],
            }
            for page in all_pages
        ),
        ink_map_pages=tuple(_mapped_page(page["ordinal"]) for page in all_pages),
        page_accounting=_page_accounting(*(page["ordinal"] for page in all_pages)),
        expected_acts=len(acts),
        aggregate=aggregate,
        aggregate_basis=basis,
        not_measured_basis=_basis_for_acts(acts, sealed_pages=len(all_pages)),
        continuation_joins=joins,
    )


def _joined_members(**fields) -> dict[str, bytes]:
    bundle = build_armarium_bundle(_joined(**fields), _formats(embed_pixels=False), _source_bytes)
    return _members(bundle.data)


def _text(members: dict[str, bytes]) -> str:
    return "".join(
        content.decode("utf-8") for name, content in members.items() if name.startswith("text/")
    )


def test_code_never_joins_two_literals_and_the_join_stays_out_of_the_act_accounting(tmp_path):
    bundle = build_armarium_bundle(_joined(), _formats(embed_pixels=False), _source_bytes)
    manifest = verify_delivered_bundle(bundle.data, tmp_path / "clean")
    members = _members(bundle.data)

    assert "reconstructions.jsonl" not in members
    (join,) = json.loads(members["sources.json"])["continuation_joins"]
    assert (join["status"], join["not_reconstructed_reason"], join["authoritative"]) == (
        "not-reconstructed",
        "no-code-join",
        False,
    )
    assert join["join_rule"] == "verbatus-page-join.v3"
    text = _text(members)
    assert _HEAD_TEXT + "\n" + _TAIL_TEXT not in text
    assert "possible-continuation-on: p2:1 (page 2) [join-1-2-0]" in text
    assert "possible-continuation-from: p1:1 (page 1) [join-1-2-0]" in text

    assert manifest["claims"]["status"] == "partial"
    assert manifest["aggregate"]["by_category"] == {"delivered": 3}
    assert manifest["claims"]["terminal_ledger"]["by_unit_type"]["act"] == 3
    assert len(members["acts.jsonl"].splitlines()) == 3
    assert members["review-items.jsonl"] == b""
    partial = manifest["claims"]["partial_reasons"]
    assert any("no reconstruction was made (no-code-join)" in line for line in partial)


def test_a_note_moved_to_another_act_is_refused(tmp_path):
    members = _joined_members()
    note = "possible-continuation-on: p2:1 (page 2) [join-1-2-0]\n"
    (name,) = [name for name in members if name.startswith("text/")]
    text = members[name].decode("utf-8").replace(note, "", 1)
    anchor = "act-id: act-4\nreading: first reading\n"
    members[name] = text.replace(anchor, anchor + note, 1).encode("utf-8")
    _refresh_manifest_member(members, name)
    with pytest.raises(SchemaRefusal, match="is not exactly what this build writes"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "moved")


@pytest.mark.parametrize("pages", [(1, 3), (2, 3), (1, 1), (True, 2)])
def test_a_join_on_pages_its_acts_were_not_marked_out_on_is_refused(pages):
    with pytest.raises(SchemaRefusal, match="adjacent pages"):
        build_armarium_bundle(_joined(pages=pages), _formats(embed_pixels=False), _source_bytes)


def test_a_text_bundle_only_export_joins_nothing_and_verifies(tmp_path):
    formats = ArmariumFormats(("text-bundle",), False)
    bundle = build_armarium_bundle(_joined(formats=formats), formats, _source_bytes)
    verify_delivered_bundle(bundle.data, tmp_path / "clean")
    members = _members(bundle.data)
    assert "reconstructions.jsonl" not in members


def test_with_no_literal_format_a_join_names_no_text(tmp_path):
    bundle = build_armarium_bundle(
        _joined(formats=_FORMATS_WITHOUT_TEXT), _FORMATS_WITHOUT_TEXT, _source_bytes
    )
    verify_export_bundle(bundle.data, tmp_path / "clean")
    members = _members(bundle.data)
    (join,) = json.loads(members["sources.json"])["continuation_joins"]
    assert join["not_reconstructed_reason"] == "no-code-join"
    assert join["head_canonical_text_sha256"] is join["tail_canonical_text_sha256"] is None
    assert not any(_HEAD_TEXT.encode("utf-8") in content for content in members.values())


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"head_act_ids": ("act-1", "act-4")}, "several-acts-on-a-side"),
        ({"held_head": True}, "head-not-delivered"),
    ],
)
def test_an_unjoinable_candidate_is_not_reconstructed_and_carries_no_text(tmp_path, fields, reason):
    bundle = build_armarium_bundle(_joined(**fields), _formats(embed_pixels=False), _source_bytes)
    verify_delivered_bundle(bundle.data, tmp_path / "clean")
    members = _members(bundle.data)
    (join,) = json.loads(members["sources.json"])["continuation_joins"]
    assert (join["status"], join["not_reconstructed_reason"]) == ("not-reconstructed", reason)
    assert "reconstructions.jsonl" not in members
    partial = json.loads(members[EXPORT_MANIFEST_NAME])["claims"]["partial_reasons"]
    assert any(f"no reconstruction was made ({reason})" in line for line in partial)


def test_a_dropped_join_row_fails_the_aggregate_recompute(tmp_path):
    members = _joined_members()
    sources = json.loads(members["sources.json"])
    del sources["continuation_joins"]
    members["sources.json"] = canonical_bytes(sources)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    del manifest["self_hash"]
    manifest["members"] = [row for row in manifest["members"] if row["path"] in members]
    for row in manifest["members"]:
        row["sha256"] = digest_bytes(members[row["path"]])
        row["bytes"] = len(members[row["path"]])
    _refresh_manifest(members, manifest)
    with pytest.raises(SchemaRefusal, match="aggregate does not match"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "dropped")


def test_chained_joins_join_nothing_and_mirror_both_notes(tmp_path):
    bundle = build_armarium_bundle(
        _joined(chained=True), _formats(embed_pixels=False), _source_bytes
    )
    verify_delivered_bundle(bundle.data, tmp_path / "clean")
    members = _members(bundle.data)
    joins = json.loads(members["sources.json"])["continuation_joins"]
    assert [join["not_reconstructed_reason"] for join in joins] == ["no-code-join"] * 2
    text = _text(members)
    assert "possible-continuation-from: p1:1 (page 1) [join-1-2-0]" in text
    assert "possible-continuation-on: p3:1 (page 3) [join-2-3-1]" in text


def test_an_acts_database_only_export_joins_nothing(tmp_path):
    formats = ArmariumFormats(("acts-database",), False)
    bundle = build_armarium_bundle(_joined(formats=formats), formats, _source_bytes)
    verify_delivered_bundle(bundle.data, tmp_path / "clean")
    members = _members(bundle.data)
    (join,) = json.loads(members["sources.json"])["continuation_joins"]
    assert join["not_reconstructed_reason"] == "no-code-join"
    assert join["head_canonical_text_sha256"] == canonical_text_sha256(_HEAD_TEXT)
    partial = json.loads(members[EXPORT_MANIFEST_NAME])["claims"]["partial_reasons"]
    assert any("no reconstruction was made" in line for line in partial)


_NOTE = "possible-continuation-on: p2:1 (page 2) [join-1-2-0]\n"


@pytest.mark.parametrize(
    ("place", "refusal"),
    [
        (
            lambda text: text.replace(_NOTE, _NOTE + _NOTE, 1),
            "is not exactly what this build writes",
        ),
        (
            lambda text: text.replace("\n\n", "\n" + _NOTE + "\n", 1),
            "is not exactly what this build writes",
        ),
    ],
)
def test_a_duplicated_or_stray_note_is_refused(tmp_path, place, refusal):
    members = _joined_members()
    (name,) = [name for name, content in members.items() if _NOTE.encode("utf-8") in content]
    members[name] = place(members[name].decode("utf-8")).encode("utf-8")
    _refresh_manifest_member(members, name)
    with pytest.raises(SchemaRefusal, match=refusal):
        verify_export_bundle(_zip_bytes(members), tmp_path / "forged")


def test_a_candidate_reference_with_an_extra_key_is_refused():
    with pytest.raises(SchemaRefusal, match="names no valid join, candidate"):
        build_armarium_bundle(
            _joined(candidate_ref={**_CANDIDATE_REF, "note": "x"}),
            _formats(embed_pixels=False),
            _source_bytes,
        )


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (lambda s: s["continuation_joins"][0].update(head_page_ordinal=1.0), "adjacent pages"),
        (lambda s: s["aggregate_basis"]["act_pages"].update(one=["1"]), "not lists of page"),
    ],
)
def test_a_join_over_non_integer_pages_is_refused_by_the_verifier(tmp_path, change, refusal):
    """Floats cannot be written into a package, so the verifier is handed one directly."""
    sources = json.loads(_joined_members()["sources.json"])
    change(sources)
    with pytest.raises(SchemaRefusal, match=refusal):
        _verify_continuation_joins(tmp_path, ArmariumFormats(("review-items",), False), sources)


def test_a_join_across_two_folders_notes_each_side_in_its_own_folder(tmp_path):
    bundle = build_armarium_bundle(
        _joined(tail_folder="other"), _formats(embed_pixels=False), _source_bytes
    )
    verify_delivered_bundle(bundle.data, tmp_path / "clean")
    members = _members(bundle.data)
    head_text = members[TEXT_REGISTER].decode("utf-8")
    tail_text = members["text/_source_folder/other/readings.txt"].decode("utf-8")
    assert "possible-continuation-on: p2:1 (page 2) [join-1-2-0]" in head_text
    assert "possible-continuation-from: p1:1 (page 1) [join-1-2-0]" in tail_text


def test_member_digest_guard_refuses_a_tampered_self_containment_claim(tmp_path):
    bundle = build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    members["sources.json"] += b"tampered"

    with pytest.raises(SchemaRefusal, match="does not match its manifest digest"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


@pytest.fixture(scope="module")
def recipient_happy_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("recipient-refusals")
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR_CLI),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "page-unbroken",
            "--run-id",
            "r",
            "--run-root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return root


def _set_database_run(members: dict[str, bytes], run: dict, tmp_path: Path) -> None:
    """A thorough resealer rewrites the acts database's run binding too."""
    database = tmp_path / "resealed-run.sqlite"
    database.write_bytes(members["acts.sqlite"])
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE export_metadata SET value = ? WHERE key = 'run'", (canonical_text(run),)
        )
        connection.commit()
    finally:
        connection.close()
    members["acts.sqlite"] = database.read_bytes()


@pytest.mark.hostile_local
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("real-run-type", "non-blank submission and scenario identities"),
        ("real-run-blank", "non-blank submission and scenario identities"),
        ("real-run-extra", "unrecognized field set"),
        ("fixture-run-type", "non-blank fixture and scenario identities"),
        ("scenario-run-type", "non-blank fixture and scenario identities"),
        ("run-both-identities", "both a fixture identifier and a submission"),
        ("run-neither-identity", "neither a fixture identifier nor a submission"),
        ("real-run-nonsha", "submission identity is not a lowercase sha256"),
        ("manifest-root-extra", "unrecognized field set"),
        ("manifest-verification-extra", "unrecognized field set"),
        ("manifest-claims-extra", "unrecognized field set"),
        ("manifest-run-extra", "unrecognized field set"),
        ("manifest-pixels-extra", "unrecognized field set"),
        ("manifest-act-denominator", "not this build's fixed claim"),
        ("manifest-page-denominator", "not this build's fixed claim"),
        ("not-measured-status", "status.*disagrees with its detail"),
        ("geometry-configurations", "canonical order"),
        ("geometry-zero-samples", "sample_count is zero"),
        ("untyped-count", "non-negative integer"),
        ("not-measured-count-type", "not_measured count.*non-negative integer"),
        ("evidence-location", "canonical evidence location"),
        ("uncertainty-cap", "more acts with uncertain spans than assessed acts"),
        ("ink-map-row", "ink-map claim does not match"),
        ("sealed-source-reason", "sealed package source page carries a refusal reason"),
        ("member-byte-count", "manifest byte count"),
        ("selected-format-inventory", "selected formats.*missing"),
        ("damaged-act-whole", "may not be projected as a whole one"),
        ("join-reason", "aggregate does not match|does not recompute from its acts' literals"),
        ("publish-aggregate", "aggregate disagrees"),
        ("publish-binding", "run binding"),
        ("publish-submission", "this run's authority names no real submission"),
        ("publish-retained-manifest", "manifest identity or status disagrees"),
        ("publish-search-fold", "search-fold recomputation was not run"),
        ("publish-run-authority", "fails its own self-hash"),
    ],
)
def test_recipient_refuses_resealed_or_damaged_claims(case, expected, tmp_path, request):
    manifest_mutations = {
        "real-run-type": lambda m: m["run"].update(submission_id=["not", "an", "identity"]),
        "real-run-blank": lambda m: m["run"].update(submission_id="   "),
        "real-run-extra": lambda m: m["run"].update(operator="nobody"),
        "fixture-run-type": lambda m: m["run"].update(fixture_id=["not", "an", "identity"]),
        "scenario-run-type": lambda m: m["run"].update(scenario=["not", "an", "identity"]),
        "run-both-identities": lambda m: m["run"].update(submission_id="a" * 64),
        "run-neither-identity": lambda m: m["run"].pop("fixture_id"),
        "real-run-nonsha": lambda m: m["run"].update(submission_id="not-a-lowercase-sha256"),
        "manifest-root-extra": lambda m: m.update(independent_audit="passed"),
        "manifest-verification-extra": lambda m: m.update(
            verification={"search_fold": {"status": "verified"}}
        ),
        "manifest-claims-extra": lambda m: m["claims"].update(accuracy="99.9%"),
        "manifest-run-extra": lambda m: m["run"].update(operator="nobody"),
        "manifest-pixels-extra": lambda m: m["claims"]["pixels"].update(verified_by="nobody"),
        "manifest-act-denominator": lambda m: m["claims"]["act_partition"].update(
            denominator="other"
        ),
        "manifest-page-denominator": lambda m: m["claims"]["page_census"].update(
            denominator="other"
        ),
        "not-measured-status": lambda m: _entry(m["claims"]["not_measured"], "perlector-pass-c")[
            "detail"
        ].update(pages_audit_not_run=0),
        "geometry-configurations": lambda m: _entry(
            m["claims"]["not_measured"], "designator-geometry-calibration"
        )["detail"]["configurations"].clear(),
        "geometry-zero-samples": lambda m: _entry(
            m["claims"]["not_measured"], "designator-geometry-calibration"
        )["detail"]["configurations"][1].update(calibrated_for_this_corpus=True, sample_count=0),
        "untyped-count": lambda m: _entry(m["claims"]["not_measured"], "perlector-pass-c")[
            "detail"
        ].update(pages_read="1"),
        "evidence-location": lambda m: _entry(
            m["claims"]["not_measured"], "perlector-pass-c"
        ).update(recorded_in="somewhere else"),
        "uncertainty-cap": lambda m: _recipient_uncertainty_overflow(m),
        "not-measured-count-type": lambda m: _recipient_boolean_count(m),
    }
    if case in manifest_mutations:
        with pytest.raises(SchemaRefusal, match=expected):
            verify_delivered_bundle(
                (_real_resealed_manifest if case.startswith("real-run-") else _resealed_manifest)(
                    manifest_mutations[case]
                ),
                tmp_path / "delivered",
            )
        return
    if case in {"ink-map-row", "sealed-source-reason"}:
        projection = (
            _otherwise_complete(ink_map_pages=(_edge_page(outside=5_000),))
            if case == "ink-map-row"
            else _projection()
        )
        members = _members(
            build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes).data
        )
        sources = json.loads(members["sources.json"])
        if case == "ink-map-row":
            sources["ink_map_pages"][0]["remeasured"]["outside_ink_pixels"] = 0
        else:
            sources["pages"][0]["reason"] = "forged refusal despite a sealed page"
        members["sources.json"] = canonical_bytes(sources)
        _refresh_manifest_member(members, "sources.json")
        data = _zip_bytes(members)
    elif case in {"member-byte-count", "selected-format-inventory"}:
        members = _members(
            build_armarium_bundle(_projection(), _formats(embed_pixels=False), _source_bytes).data
        )
        manifest = json.loads(members[EXPORT_MANIFEST_NAME])
        if case == "member-byte-count":
            row = next(item for item in manifest["members"] if item["path"] == "sources.json")
            row["bytes"] += 1
        else:
            members.pop(TEXT_REGISTER)
            manifest["members"] = [
                row for row in manifest["members"] if row["path"] != TEXT_REGISTER
            ]
        _refresh_manifest(members, manifest)
        data = _zip_bytes(members)
    elif case == "damaged-act-whole":
        data = _recipient_whole_damaged_act()
    elif case == "join-reason":
        members = _joined_members()
        sources = json.loads(members["sources.json"])
        sources["continuation_joins"][0]["not_reconstructed_reason"] = "head-not-delivered"
        members["sources.json"] = canonical_bytes(sources)
        _refresh_manifest_member(members, "sources.json")
        data = _zip_bytes(members)
    else:
        import shutil

        root = tmp_path / "runs"
        shutil.copytree(request.getfixturevalue("recipient_happy_run") / "r", root / "r")
        tree = RunTree(root, "r")
        if case == "publish-aggregate":
            from common.contracts.identities import artifact_id

            path = tree.resolve(
                tree.artifact_path(
                    ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None)
                )
            )
            record = json.loads(path.read_bytes())
            record["payload"]["aggregate"]["status"] = "fabricated-terminal-status"
            record["self_hash"] = self_hash(record)
            path.write_bytes(canonical_bytes(record))
        elif case in {
            "publish-binding",
            "publish-submission",
            "publish-retained-manifest",
            "publish-search-fold",
        }:
            from common.contracts.identities import artifact_id

            path = tree.resolve(
                tree.artifact_path(
                    ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None)
                )
            )
            before = json.loads(path.read_bytes())
            if case == "publish-search-fold":

                def mutate(members, _manifest):
                    database = tmp_path / "acts.sqlite"
                    database.write_bytes(members["acts.sqlite"])
                    connection = sqlite3.connect(database)
                    try:
                        connection.execute(
                            "UPDATE export_metadata SET value = 'resealed-different-version' WHERE key = 'unidata_version'"
                        )
                        connection.commit()
                    finally:
                        connection.close()
                    members["acts.sqlite"] = database.read_bytes()
            elif case == "publish-retained-manifest":

                def mutate(_members, manifest):
                    row = _entry(manifest["claims"]["not_measured"], "perlector-pass-c")
                    row["detail"]["sealed_audit_round_cap"] += 1
            elif case == "publish-submission":

                def mutate(members, manifest):
                    manifest["run"].pop("fixture_id")
                    manifest["run"]["submission_id"] = "a" * 64
                    _set_database_run(members, manifest["run"], tmp_path)
            else:

                def mutate(members, manifest):
                    manifest["run"]["scenario"] = "a run that never happened"
                    _set_database_run(members, manifest["run"], tmp_path)

            _recipient_reseal_export(tree, mutate)
            if case in {"publish-submission", "publish-retained-manifest"}:
                changed = json.loads(path.read_bytes())
                if case == "publish-submission":
                    changed["payload"].pop("fixture_id")
                    changed["payload"]["submission_id"] = "a" * 64
                else:
                    changed["payload"]["bundle"]["manifest_self_hash"] = before["payload"][
                        "bundle"
                    ]["manifest_self_hash"]
                    changed["payload"]["bundle"]["claims_status"] = before["payload"]["bundle"][
                        "claims_status"
                    ]
                changed["self_hash"] = self_hash(changed)
                path.write_bytes(canonical_bytes(changed))
        else:
            path = root / "r" / "run.json"
            record = json.loads(path.read_bytes())
            record["fixture_id"] = "a-fixture-this-run-never-used"
            path.write_bytes(canonical_bytes(record))
        destination = tmp_path / "delivery"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "pipeline/7_armarium/bundle.py"),
                "--run-root",
                str(root),
                "--run-id",
                "r",
                "--out",
                str(destination),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert expected in result.stderr
        assert not destination.exists()
        return
    with pytest.raises(SchemaRefusal, match=expected):
        verify_export_bundle(data, tmp_path / "clean")


def _recipient_uncertainty_overflow(manifest):
    block = manifest["claims"]["not_measured"]
    entry = _entry(block, "perlector-uncertain-spans")
    detail = entry["detail"]
    detail["acts_with_uncertain_spans"] = detail["acts_assessed"] + 1
    entry["status"] = "measured"
    block["count"] = sum(row["status"] != "measured" for row in block["entries"])


def _recipient_reseal_export(tree, mutate):
    from common.contracts.identities import artifact_id

    path = tree.resolve(
        tree.artifact_path(ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None))
    )
    record = json.loads(path.read_bytes())
    old_reference = record["payload"]["bundle"]["reference"]
    members = _members(tree.read_bytes(old_reference["relative_path"]))
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    mutate(members, manifest)
    for row in manifest["members"]:
        row["sha256"] = digest_bytes(members[row["path"]])
        row["bytes"] = len(members[row["path"]])
    _refresh_manifest(members, manifest)
    data = _zip_bytes(members)
    digest = digest_bytes(data)
    relative = tree.blob_path(ARMARIUM, digest)
    tree.resolve(relative).write_bytes(data)
    reference = {"relative_path": relative, "sha256": digest}
    record["inputs"] = [reference if item == old_reference else item for item in record["inputs"]]
    record["payload"]["bundle"].update(
        reference=reference, sha256=digest, manifest_self_hash=manifest["self_hash"]
    )
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))


def _recipient_boolean_count(manifest):
    block = manifest["claims"]["not_measured"]
    uncertainty = _entry(block, "perlector-uncertain-spans")
    uncertainty["detail"]["sealed_audit_round_cap"] = 0
    uncertainty["detail"]["acts_assessed"] = uncertainty["detail"]["acts_delivered"]
    uncertainty["detail"]["acts_not_assessed"] = 0
    uncertainty["status"] = "measured"
    geometry = _entry(block, "designator-geometry-calibration")
    for row in geometry["detail"]["configurations"]:
        row["calibrated_for_this_corpus"] = True
        if row["sample_count"] == 0:
            row["sample_count"] = 1
    geometry["status"] = "measured"
    thresholds = _entry(block, "page-accounting-thresholds")
    thresholds["detail"].update(calibrated_for_this_corpus=True, sample_count=1)
    thresholds["status"] = "measured"
    assert sum(row["status"] != "measured" for row in block["entries"]) == 1
    block["count"] = True


def _recipient_whole_damaged_act():
    members = _members(
        build_armarium_bundle(
            _partial_projection(), _formats(embed_pixels=False), _source_bytes
        ).data
    )
    rows = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    for row in rows:
        if row["text_status"] == "partial":
            row["text_status"] = "established"
    members["acts.jsonl"] = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    members[TEXT_REGISTER] = (
        members[TEXT_REGISTER]
        .decode("utf-8")
        .replace("text_status: partial", "text_status: established")
        .encode("utf-8")
    )
    sources = json.loads(members["sources.json"])
    for outcome in sources["act_outcomes"]:
        if outcome["text_status"] == "partial":
            outcome["text_status"] = "established"
    sources["aggregate_basis"]["act_text_status"] = {"p1:1": "established"}
    members["sources.json"] = canonical_bytes(sources)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["aggregate"] = run_aggregate(
        {"p1:1": ArmariumCategory.DELIVERED, "p1:2": ArmariumCategory.HELD_FOR_REVIEW},
        sources["aggregate_basis"]["coverage_records"],
        {page["ordinal"]: page for page in sources["pages"]},
        unaddressed_chairs=[],
        act_pages=sources["aggregate_basis"]["act_pages"],
        act_text_status={"p1:1": "established"},
    )
    manifest["aggregate_basis"] = sources["aggregate_basis"]
    ledger = _terminal_ledger(
        sources["act_outcomes"],
        sources["pages"],
        sources["aggregate_basis"]["act_pages"],
        manifest["aggregate"],
        (),
        sources["other_outcomes"],
    )
    manifest["claims"]["terminal_ledger"] = ledger
    manifest["claims"]["status"] = ledger["status"]
    manifest["claims"]["partial_reasons"] = ledger["unresolved_reasons"]
    for member in ("acts.jsonl", TEXT_REGISTER, "sources.json"):
        row = next(item for item in manifest["members"] if item["path"] == member)
        row["sha256"] = digest_bytes(members[member])
        row["bytes"] = len(members[member])
    _refresh_manifest(members, manifest)
    return _zip_bytes(members)


_UNIT_HOLD = "duplicate-region"


def _released_on_its_own_holds() -> ArmariumProjection:
    """`p1:1` delivered over a unit hold of its own reading, on a page with no hold."""
    from operator_layer import released_row

    projection = _projection()
    basis = {
        **projection.aggregate_basis,
        "review_decisions": {
            "clearances": [
                {
                    "scope": "unit",
                    "subject": "p1:1",
                    "page": 1,
                    "decision": "release",
                    "cleared": [_UNIT_HOLD],
                }
            ],
            "page_holds": [],
            "corrections": [],
        },
    }
    act = projection.acts[0]
    row = released_row(
        {"act_id": act["act_id"], "act_key": act["act_key"], "kind": "act"},
        {"codes": [_UNIT_HOLD]},
        [_UNIT_HOLD],
        [
            {
                "decision": "release",
                "scope": "unit",
                "subject_id": act["act_id"],
                "approver": "project-lead",
                "timestamp": "2026-01-01T00:00:00Z",
                "reason": "one act read twice",
                "approval_ref": {
                    "relative_path": "5_recensor/approvals/release.json",
                    "sha256": "d" * 64,
                },
                "decision_hash": "9" * 64,
            }
        ],
    )
    return replace(
        projection,
        aggregate_basis=basis,
        aggregate=run_aggregate(
            {a["act_key"]: ArmariumCategory(a["category"]) for a in projection.acts},
            basis["coverage_records"],
            {page["ordinal"]: page for page in projection.pages},
            unaddressed_chairs=basis["unaddressed_chairs"],
            act_pages=basis["act_pages"],
            act_text_status=basis["act_text_status"],
            review_clearances=basis["review_decisions"]["clearances"],
        ),
        operator_actions=(row,),
        reading_hold_codes={act["act_id"]: [_UNIT_HOLD]},
    )


def _drop_operator_layer(members: dict[str, bytes], *, with_codes: bool) -> None:
    """Remove the operator label from every place it is written, keeping the reading."""
    sources = json.loads(members["sources.json"])
    del sources["operator_actions"]
    if with_codes:
        del sources["reading_hold_codes"]
    members["sources.json"] = canonical_bytes(sources)
    del members["operator.jsonl"]
    for name in [name for name in members if name.startswith("text/")]:
        kept, skip = [], 0
        for line in members[name].decode().split("\n"):
            if line.startswith("operator_label: "):
                skip = 3
            if skip:
                skip -= 1
                continue
            kept.append(line)
        members[name] = "\n".join(kept).encode()
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["members"] = [row for row in manifest["members"] if row["path"] in members]
    for row in manifest["members"]:
        row["sha256"] = digest_bytes(members[row["path"]])
        row["bytes"] = len(members[row["path"]])
    _refresh_manifest(members, {k: v for k, v in manifest.items() if k != "self_hash"})


def test_a_reading_released_on_its_own_holds_carries_them_and_verifies(tmp_path):
    bundle = build_armarium_bundle(
        _released_on_its_own_holds(), _formats(embed_pixels=False), _source_bytes
    )
    sources = json.loads(_members(bundle.data)["sources.json"])
    assert sources["reading_hold_codes"] == {"act-1": [_UNIT_HOLD]}
    assert [row["reading_hold_codes"] for row in sources["operator_actions"]] == [[_UNIT_HOLD]]
    assert all(not row["hold_codes"] for row in sources["page_accounting"])
    verify_export_bundle(bundle.data, tmp_path / "clean")
    # A database-only reader sees the release too.
    with sqlite3.connect(tmp_path / "clean" / "acts.sqlite") as connection:
        assert dict(connection.execute("SELECT act_id, operator_label FROM acts")) == {
            "act-1": "released by operator",
            "act-2": None,
        }


@pytest.mark.parametrize("label", [None, "corrected by a person"])
def test_an_acts_database_that_misstates_an_operator_label_is_refused(tmp_path, label):
    bundle = build_armarium_bundle(
        _released_on_its_own_holds(), _formats(embed_pixels=False), _source_bytes
    )
    members = _members(bundle.data)
    database = tmp_path / "tampered.sqlite"
    database.write_bytes(members["acts.sqlite"])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE acts SET operator_label = ? WHERE act_id = 'act-1'", (label,))
    members["acts.sqlite"] = database.read_bytes()
    _refresh_manifest_member(members, "acts.sqlite")
    with pytest.raises(SchemaRefusal, match="does not label exactly the acts an operator"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")


@pytest.mark.parametrize(
    "with_codes, refusal",
    [
        (False, "delivered over its own holds"),
        (True, "records review decisions but not its delivered readings' own holds"),
    ],
    ids=["label-dropped", "label-and-codes-dropped"],
)
def test_a_label_dropped_everywhere_is_refused_when_no_page_is_held(tmp_path, with_codes, refusal):
    """No page hold shows the release, so the reading's own codes must."""
    bundle = build_armarium_bundle(
        _released_on_its_own_holds(), _formats(embed_pixels=False), _source_bytes
    )
    members = _members(bundle.data)
    _drop_operator_layer(members, with_codes=with_codes)
    with pytest.raises(SchemaRefusal, match=refusal):
        verify_export_bundle(_zip_bytes(members), tmp_path / "forged")


def test_a_held_reading_without_its_operator_row_is_refused_at_build():
    projection = replace(_released_on_its_own_holds(), operator_actions=())
    with pytest.raises(SchemaRefusal, match="delivered over its own holds"):
        build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)


def _systemic(share: str = "1/50") -> ArmariumProjection:
    """The projection of a run a person advanced past a systemic held share."""
    projection = _projection()
    review = {"held_pages": [1], "pages": 1, "max_held_page_share": share}
    basis = {**projection.aggregate_basis, "systemic_review": review}
    return replace(
        projection,
        aggregate_basis=basis,
        aggregate=run_aggregate(
            {a["act_key"]: ArmariumCategory(a["category"]) for a in projection.acts},
            basis["coverage_records"],
            {page["ordinal"]: page for page in projection.pages},
            unaddressed_chairs=basis["unaddressed_chairs"],
            act_pages=basis["act_pages"],
            act_text_status=basis["act_text_status"],
            systemic_review=review,
        ),
    )


def test_a_systemic_share_is_a_reason_the_package_carries_and_cannot_drop(tmp_path):
    from common.contracts.outcomes import systemic_reason

    bundle = build_armarium_bundle(_systemic(), _formats(embed_pixels=False), _source_bytes)
    reasons = verify_export_bundle(bundle.data, tmp_path / "clean")["aggregate"]["reasons"]
    assert systemic_reason([1], 1, "1/50") in reasons

    members = _members(bundle.data)
    sources = json.loads(members["sources.json"])
    del sources["aggregate_basis"]["systemic_review"]
    members["sources.json"] = canonical_bytes(sources)
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["aggregate_basis"] = sources["aggregate_basis"]
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)
    _refresh_manifest_member(members, "sources.json")
    # The aggregate, recomputed from the basis without it, no longer matches.
    with pytest.raises(SchemaRefusal, match="does not match its measured accounting basis"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "forged")


def test_a_systemic_reason_within_its_limit_is_refused():
    with pytest.raises(SchemaRefusal, match="held share within its limit"):
        build_armarium_bundle(_systemic("1/1"), _formats(embed_pixels=False), _source_bytes)


# --- the flagged layer -------------------------------------------------------------------


def _flagged_rows(projection: ArmariumProjection) -> tuple[dict, ...]:
    """A held act's row with the model's text, and a delivered act's with a review flag."""
    held, delivered = projection.acts[1], projection.acts[0]
    refs = {
        "perlectio_ref": {
            "relative_path": "4_perlector/artifacts/perlectio/x.json",
            "sha256": "1" * 64,
        },
        "page_reading_ref": {
            "relative_path": "4_perlector/artifacts/page-reading/p1.json",
            "sha256": "2" * 64,
        },
        "recensor_ref": {"relative_path": "5_recensor/artifacts/review/x.json", "sha256": "3" * 64},
    }
    return (
        {
            "schema": "armarium-flagged-reading.v1",
            "act_id": held["act_id"],
            "act_key": held["act_key"],
            "lot": _LOT,
            "kind": "act",
            "page_ordinal": 1,
            "status": "not-established",
            "category": "held-for-review",
            "review_priority": 1,
            "hold_codes": ["reading-unplaced"],
            "flag_codes": ["unread-ink"],
            "text": "Cesar d'Exemple, as the model read it",
            "text_label": "model reading, not established",
            "reason": held["reason"],
            **refs,
            "evidence_refs": held["evidence_refs"],
        },
        {
            "schema": "armarium-flagged-reading.v1",
            "act_id": delivered["act_id"],
            "act_key": delivered["act_key"],
            "lot": _LOT,
            "kind": "act",
            "page_ordinal": 1,
            "status": "established-with-flags",
            "category": "delivered",
            "review_priority": 3,
            "hold_codes": [],
            "flag_codes": ["no-detector-record-on-act-page"],
            "text": delivered["canonical_clean_text"],
            "text_label": "established",
            "reason": None,
            **refs,
            "evidence_refs": delivered["evidence_refs"],
        },
    )


def test_the_flagged_layer_carries_held_and_flagged_readings_with_their_text(tmp_path):
    projection = replace(_projection(), flagged_readings=_flagged_rows(_projection()))
    bundle = build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)

    rows = [json.loads(line) for line in members["flagged.jsonl"].decode("utf-8").splitlines()]
    assert [(row["act_key"], row["status"]) for row in rows] == [
        ("p1:2", "not-established"),
        ("p1:1", "established-with-flags"),
    ]
    assert rows[0]["text"] == "Cesar d'Exemple, as the model read it"
    assert rows[0]["text_label"] == "model reading, not established"
    assert rows[1]["text"] == "Cǣsar d’Exemple" and rows[1]["text_label"] == "established"
    # Run-tree citations travel marked, never as bare paths.
    assert rows[0]["perlectio_ref"]["availability"] == "requires-retained-run-access"
    sources = json.loads(members["sources.json"])
    assert sources["flagged_readings"] == rows
    # The strict formats are untouched: one held act, no text for it anywhere else.
    acts = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
    assert [act["canonical_clean_text"] for act in acts] == ["Cǣsar d’Exemple", None]
    manifest = verify_export_bundle(bundle.data, tmp_path / "clean")
    assert manifest["claims"]["partial_reasons"] == [
        "act p1:2 is held-for-review: the review remains unresolved"
    ]


def test_a_flagged_row_that_contradicts_the_package_is_refused(tmp_path):
    base = _projection()
    held, delivered = _flagged_rows(base)
    for row, match in (
        ({**delivered, "text": "another text"}, "other than the delivered literal"),
        ({**held, "act_id": "act-9"}, "no counted reading"),
        ({**held, "hold_codes": [], "flag_codes": []}, "neither held nor flagged"),
        ({**held, "review_priority": 3}, "priority its codes do not give"),
        ({**held, "status": "established-with-flags"}, "says 'established-with-flags'"),
        (
            {**delivered, "hold_codes": ["unread-ink"], "review_priority": 1},
            "says a delivered reading is held",
        ),
        ({**held, "text_label": "established"}, "labels its text against its category"),
        ({**held, "category": "delivered"}, "another key, kind, lot, category or reason"),
    ):
        with pytest.raises(SchemaRefusal, match=match):
            build_armarium_bundle(
                replace(base, flagged_readings=(row,)), _formats(embed_pixels=False), _source_bytes
            )


def test_a_flagged_member_that_differs_from_the_sources_is_refused(tmp_path):
    projection = replace(_projection(), flagged_readings=_flagged_rows(_projection()))
    bundle = build_armarium_bundle(projection, _formats(embed_pixels=False), _source_bytes)
    members = _members(bundle.data)
    rows = members["flagged.jsonl"].decode("utf-8").splitlines()
    members["flagged.jsonl"] = (rows[0] + "\n").encode("utf-8")
    _refresh_manifest_member(members, "flagged.jsonl")
    with pytest.raises(SchemaRefusal, match="other flagged readings than sources.json"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")
    # And a package that drops the member while its sources record the rows.
    members = _members(bundle.data)
    del members["flagged.jsonl"]
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["members"] = [row for row in manifest["members"] if row["path"] != "flagged.jsonl"]
    _refresh_manifest(members, manifest)
    with pytest.raises(SchemaRefusal, match="members disagree"):
        verify_export_bundle(_zip_bytes(members), tmp_path / "clean")
