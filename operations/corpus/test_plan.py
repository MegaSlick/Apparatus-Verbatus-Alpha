"""Pure tests for `operations/corpus/plan.py` — no network, no parquet.

Every row is an inline dict shaped like a `recordgold-rows.v1` row; no test reads
a file or opens a socket.
"""

import copy

import pytest

from common.contracts.canonical import digest_bytes
from operations.corpus import CorpusRefusal
from operations.corpus.plan import build_fetch_plan, parse_record_url, validate_plan

SNAPSHOT_HASH = "0" * 64  # a placeholder self-hash; these tests never build a real snapshot


def _row(record_id, split, url, source="Ardennes", text="quelque texte"):
    return {
        "split": split,
        "source": source,
        "record_id": record_id,
        "record_url": url,
        "start_date": None,
        "end_date": None,
        "parish": "Rethel",
        "text": text,
        "text_sha256": digest_bytes(text.encode("utf-8")),
    }


ONE_PAGE_URL = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F380403%2F00026.jpg/"
    "239,208,1232,443/full/0/default.jpg"
)
OTHER_RECORD_ON_SAME_PAGE = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F380403%2F00026.jpg/"
    "10,10,100,100/full/0/default.jpg"
)
OTHER_PAGE_URL = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F383351%2F00143.jpg/"
    "103,139,1278,1566/full/0/default.jpg"
)


# --- parse_record_url: the happy path ------------------------------------------


def test_parse_record_url_decodes_identifier_and_region():
    parsed = parse_record_url(ONE_PAGE_URL)
    assert parsed.identifier == "geneanet/Ardennes_BMS/380403/00026.jpg"
    assert parsed.identifier_encoded == "geneanet%2FArdennes_BMS%2F380403%2F00026.jpg"
    assert parsed.host == "europe.iiif.teklia.com"
    assert parsed.region == {"x": 239, "y": 208, "w": 1232, "h": 443}


def test_parse_record_url_preserves_a_literal_plus_verbatim():
    # Measured: quote(unquote(x)) != x for identifiers carrying a literal "+".
    # The encoded form must be threaded through unchanged, never re-escaped.
    url = (
        "https://europe.iiif.teklia.com/iiif/2/"
        "dai-cretdhi%2FIle_de_re_registres_AD17%2Fimg+LaCouarde%2Ffoo.jpg/"
        "1,1,10,10/full/0/default.jpg"
    )
    parsed = parse_record_url(url)
    assert (
        parsed.identifier_encoded
        == "dai-cretdhi%2FIle_de_re_registres_AD17%2Fimg+LaCouarde%2Ffoo.jpg"
    )
    assert parsed.identifier == "dai-cretdhi/Ile_de_re_registres_AD17/img+LaCouarde/foo.jpg"


# --- parse_record_url: every refusal fires by name -----------------------------


@pytest.mark.parametrize(
    "url,reason",
    [
        ("not a url at all", "unparseable-record-url"),
        (
            "https://evil.example.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,10/full/0/default.jpg",
            "unexpected-host",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,10/max/0/default.jpg",
            "unsupported-size-parameter",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,10/full/180/default.jpg",
            "unsupported-rotation-parameter",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,10/full/0/gray.jpg",
            "unsupported-quality-parameter",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,10/full/0/default.png",
            "unsupported-format-parameter",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,0,10/full/0/default.jpg",
            "non-positive-region",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,0/full/0/default.jpg",
            "non-positive-region",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/-1,1,10,10/full/0/default.jpg",
            "non-positive-region",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/onlyonesegment/1,1,10,10/full/0/default.jpg",
            "unparseable-record-url",
        ),
    ],
)
def test_parse_record_url_refuses_by_name(url, reason):
    with pytest.raises(CorpusRefusal, match=f"^{reason}:"):
        parse_record_url(url)


def test_parse_record_url_refuses_a_non_string():
    with pytest.raises(CorpusRefusal, match="^unparseable-record-url:"):
        parse_record_url(None)


# --- build_fetch_plan: grouping -------------------------------------------------


def test_build_fetch_plan_groups_two_records_on_one_page():
    rows = [
        _row("r1", "val", ONE_PAGE_URL),
        _row("r2", "val", OTHER_RECORD_ON_SAME_PAGE),
        _row("r3", "val", OTHER_PAGE_URL),
    ]
    plan = build_fetch_plan(rows, SNAPSHOT_HASH)
    assert plan["schema"] == "recordgold-fetch-plan.v1"
    assert len(plan["pages"]) == 2

    page = next(
        p for p in plan["pages"] if p["identifier"] == "geneanet/Ardennes_BMS/380403/00026.jpg"
    )
    assert page["source"] == "Ardennes"
    assert page["volume"] == "geneanet/Ardennes_BMS/380403"
    assert page["designation"] == "00026.jpg"
    assert page["splits_present"] == ["val"]
    assert [record["record_id"] for record in page["records"]] == ["r1", "r2"]
    assert page["image_url_candidates"]["full"].endswith("/full/full/0/default.jpg")
    assert page["image_url_candidates"]["max"].endswith("/full/max/0/default.jpg")
    assert page["physical_page_id"].startswith("ppg_")
    for record in page["records"]:
        assert record["physical_act_id"].startswith("pac_")

    other = next(p for p in plan["pages"] if p["identifier"] != page["identifier"])
    assert len(other["records"]) == 1


def test_build_fetch_plan_is_deterministic():
    rows = [_row("r1", "val", ONE_PAGE_URL), _row("r2", "val", OTHER_RECORD_ON_SAME_PAGE)]
    plan_a = build_fetch_plan(rows, SNAPSHOT_HASH)
    plan_b = build_fetch_plan(copy.deepcopy(rows), SNAPSHOT_HASH)
    assert plan_a == plan_b
    assert plan_a["self_hash"] == plan_b["self_hash"]


def test_build_fetch_plan_refuses_inconsistent_source_for_one_identifier():
    rows = [
        _row("r1", "val", ONE_PAGE_URL, source="Ardennes"),
        _row("r2", "val", OTHER_RECORD_ON_SAME_PAGE, source="Tours"),
    ]
    with pytest.raises(CorpusRefusal, match="^inconsistent-source-for-identifier:"):
        build_fetch_plan(rows, SNAPSHOT_HASH)


# --- build_fetch_plan: refusals are recorded, not escalated --------------------


def test_build_fetch_plan_records_a_row_refusal_and_keeps_the_rest():
    bad_url = "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,10/full/180/default.jpg"
    rows = [_row("r1", "val", ONE_PAGE_URL), _row("r2", "val", bad_url)]
    plan = build_fetch_plan(rows, SNAPSHOT_HASH)
    assert len(plan["pages"]) == 1
    assert len(plan["refusals"]) == 1
    assert plan["refusals"][0]["record_id"] == "r2"
    assert plan["refusals"][0]["reason"] == "unsupported-rotation-parameter"
    assert plan["measurements"]["refused_row_count"] == 1
    assert plan["measurements"]["refused_count_by_reason"] == {"unsupported-rotation-parameter": 1}


# --- measurements ----------------------------------------------------------------


def test_build_fetch_plan_measurements():
    rows = [
        _row("r1", "val", ONE_PAGE_URL),
        _row("r2", "val", OTHER_RECORD_ON_SAME_PAGE),
        _row("r3", "test", OTHER_PAGE_URL),
    ]
    plan = build_fetch_plan(rows, SNAPSHOT_HASH)
    m = plan["measurements"]
    assert m["distinct_pages_total"] == 2
    assert m["pages_per_split"] == {"test": 1, "train": 0, "val": 1}
    assert m["records_per_page_distribution"] == {"1": 1, "2": 1}
    assert m["cross_split_page_count"] == 0


def test_build_fetch_plan_counts_a_cross_split_page():
    rows = [
        _row("r1", "val", ONE_PAGE_URL),
        _row("r2", "test", OTHER_RECORD_ON_SAME_PAGE),
    ]
    plan = build_fetch_plan(rows, SNAPSHOT_HASH)
    assert plan["measurements"]["cross_split_page_count"] == 1
    page = plan["pages"][0]
    assert page["splits_present"] == ["test", "val"]


# --- validate_plan -----------------------------------------------------------


def test_validate_plan_refuses_a_self_hash_mismatch():
    rows = [_row("r1", "val", ONE_PAGE_URL)]
    plan = build_fetch_plan(rows, SNAPSHOT_HASH)
    tampered = dict(plan)
    tampered["pages"] = []
    with pytest.raises(CorpusRefusal, match="^self-hash-mismatch:"):
        validate_plan(tampered)


def test_validate_plan_refuses_an_open_field_set():
    rows = [_row("r1", "val", ONE_PAGE_URL)]
    plan = build_fetch_plan(rows, SNAPSHOT_HASH)
    tampered = dict(plan)
    tampered["unexpected_field"] = "x"
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        validate_plan(tampered)


def test_validate_plan_refuses_wrong_schema():
    rows = [_row("r1", "val", ONE_PAGE_URL)]
    plan = build_fetch_plan(rows, SNAPSHOT_HASH)
    tampered = dict(plan)
    tampered["schema"] = "some-other.v1"
    with pytest.raises(CorpusRefusal, match="^wrong-schema:"):
        validate_plan(tampered)
