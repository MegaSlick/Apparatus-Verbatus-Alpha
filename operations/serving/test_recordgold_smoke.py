"""The DAI chair's RecordGold smoke record: pins, offline fetch, scoring.

No network is touched. ``record_pin`` builds a record of its own -- a small
JPEG and a short gold text, pinned the way the real one is -- and a fetch that
answers from memory, so the pass, the over-threshold fail, each digest-mismatch
refusal and the fetch-failure refusal are all proven offline. The real pin is
checked for shape only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common.chairs.config import load_models_toml
from operations.corpus.record_url import parse_record_url

from .recordgold_smoke import (
    RECORDGOLD_SMOKE_MAX_CER,
    RECORDGOLD_SMOKE_RECORD,
    RecordGoldSmokeRecord,
    RecordGoldSmokeRefusal,
    fetch_recordgold_smoke_page,
    recordgold_smoke_chairs,
    recordgold_smoke_prompt,
    score_recordgold_answer,
    verify_recordgold_text,
)

ROOT = Path(__file__).resolve().parents[2]
TEST_GOLD_TEXT = "L'an mil sept cent soixante et onze le dix mai fut baptisé Jean fils de Pierre"


def record_jpeg() -> bytes:
    """A small grayscale JPEG standing in for the IIIF crop."""

    image = Image.new("L", (96, 32), color=240)
    for x in range(8, 88, 4):
        image.putpixel((x, 16), 20)
    encoded = BytesIO()
    image.save(encoded, format="JPEG")
    return encoded.getvalue()


def record_pin(
    jpeg: bytes | None = None, text: str = TEST_GOLD_TEXT
) -> tuple[RecordGoldSmokeRecord, Callable[[str], bytes]]:
    """A pinned record and a fetch answering its two URLs from memory."""

    data = record_jpeg() if jpeg is None else jpeg
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
    record = RecordGoldSmokeRecord(
        dataset="example/records",
        revision="0" * 40,
        licence="test fixture",
        split="train",
        row_index=3,
        record_id="00000000-0000-0000-0000-000000000003",
        record_url=(
            "https://europe.iiif.teklia.com/iiif/2/example%2Fvolume%2F0001.jpg/"
            "10,20,96,32/full/0/default.jpg"
        ),
        source="test",
        parish="test",
        start_date=1771,
        image_sha256=hashlib.sha256(data).hexdigest(),
        image_width=width,
        image_height=height,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text_length=len(text),
    )
    answers = {
        record.record_url: data,
        record.rows_url: json.dumps(
            {"rows": [{"row_idx": 3, "row": {"record_id": record.record_id, "text": text}}]}
        ).encode("utf-8"),
    }

    def fetch(url: str) -> bytes:
        return answers[url]

    return record, fetch


def test_the_fetched_record_is_verified_and_re_encoded_as_png() -> None:
    record, fetch = record_pin()

    page = fetch_recordgold_smoke_page(record, fetch=fetch)

    assert page.text == TEST_GOLD_TEXT
    assert page.png.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(BytesIO(page.png)) as png, Image.open(BytesIO(record_jpeg())) as jpeg:
        assert png.size == jpeg.size
        assert list(png.getdata()) == list(jpeg.getdata())


def test_a_crop_whose_bytes_differ_from_the_pin_is_refused_by_name() -> None:
    record, _ = record_pin()
    other_jpeg = record_jpeg()[:-2] + b"\x00\xd9"
    _, fetch = record_pin(jpeg=other_jpeg)
    # The same pin, a different served byte stream.
    with pytest.raises(RecordGoldSmokeRefusal) as refusal:
        fetch_recordgold_smoke_page(record, fetch=fetch)
    assert refusal.value.reason == "recordgold-smoke-image-mismatch"
    assert record.record_url in str(refusal.value)


def test_a_transcription_whose_digest_differs_from_the_pin_is_refused_by_name() -> None:
    record, _ = record_pin()
    _, fetch = record_pin(text=TEST_GOLD_TEXT[:-1] + "s")

    with pytest.raises(RecordGoldSmokeRefusal) as refusal:
        fetch_recordgold_smoke_page(record, fetch=fetch)
    assert refusal.value.reason == "recordgold-smoke-text-mismatch"


def test_a_row_that_names_another_record_is_refused_as_moved_not_as_a_model_failure() -> None:
    record, fetch = record_pin()
    moved = replace(record, record_id="00000000-0000-0000-0000-000000000004")

    with pytest.raises(RecordGoldSmokeRefusal) as refusal:
        fetch_recordgold_smoke_page(moved, fetch=fetch)
    assert refusal.value.reason == "recordgold-smoke-row-malformed"
    assert "moved" in str(refusal.value)


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"rows": []}', b'{"rows": [{}]}'])
def test_a_malformed_rows_answer_is_refused_by_name(body: bytes) -> None:
    record, fetch = record_pin()

    def broken(url: str) -> bytes:
        return body if url == record.rows_url else fetch(url)

    with pytest.raises(RecordGoldSmokeRefusal) as refusal:
        fetch_recordgold_smoke_page(record, fetch=broken)
    assert refusal.value.reason == "recordgold-smoke-row-malformed"


def test_a_fetch_that_fails_is_refused_as_the_network_naming_the_url() -> None:
    record, _ = record_pin()

    def offline(url: str) -> bytes:
        raise RecordGoldSmokeRefusal(
            "recordgold-smoke-fetch-failed", f"{url}: URLError: name resolution failed"
        )

    with pytest.raises(RecordGoldSmokeRefusal) as refusal:
        fetch_recordgold_smoke_page(record, fetch=offline)
    assert refusal.value.reason == "recordgold-smoke-fetch-failed"
    assert record.record_url in str(refusal.value)


def test_verify_text_refuses_a_non_string_and_a_length_drift() -> None:
    record, _ = record_pin()
    assert verify_recordgold_text(TEST_GOLD_TEXT, record) == TEST_GOLD_TEXT
    for bad in (None, TEST_GOLD_TEXT + " ", b"bytes"):
        with pytest.raises(RecordGoldSmokeRefusal, match="recordgold-smoke-text-mismatch"):
            verify_recordgold_text(bad, record)


def test_a_reason_outside_the_closed_set_is_a_programming_error() -> None:
    with pytest.raises(TypeError):
        RecordGoldSmokeRefusal("recordgold-smoke-made-up", "detail")


def test_an_exact_reading_scores_zero_and_passes() -> None:
    score = score_recordgold_answer(TEST_GOLD_TEXT, TEST_GOLD_TEXT)
    assert score is not None
    assert (score.character_error_rate, score.edits, score.passed) == (Decimal("0"), 0, True)
    assert score.reference_units == len(TEST_GOLD_TEXT)


def test_layout_whitespace_and_presentation_forms_are_normalised_before_scoring() -> None:
    folded = TEST_GOLD_TEXT.replace(" ", "\n").replace("'", "’")
    score = score_recordgold_answer(folded, TEST_GOLD_TEXT)
    assert score is not None
    assert score.edits == 0


def test_a_few_slips_pass_and_a_mangled_reading_fails_the_threshold() -> None:
    slipped = TEST_GOLD_TEXT.replace("Pierre", "Piere").replace("onze", "onse")
    passing = score_recordgold_answer(slipped, TEST_GOLD_TEXT)
    assert passing is not None
    assert passing.edits == 2
    assert passing.character_error_rate <= RECORDGOLD_SMOKE_MAX_CER
    assert passing.passed

    mangled = TEST_GOLD_TEXT[:40]
    failing = score_recordgold_answer(mangled, TEST_GOLD_TEXT)
    assert failing is not None
    assert failing.character_error_rate > RECORDGOLD_SMOKE_MAX_CER
    assert not failing.passed


def test_an_empty_or_unmeasurable_answer_never_passes() -> None:
    empty = score_recordgold_answer("", TEST_GOLD_TEXT)
    assert empty is not None
    assert empty.character_error_rate == Decimal("1") and not empty.passed
    assert score_recordgold_answer("x" * 30_000, TEST_GOLD_TEXT) is None


def test_the_fixture_roster_binds_the_dai_adapter_to_attestator_2_only() -> None:
    models = load_models_toml(ROOT / "config" / "models.toml")
    assert recordgold_smoke_chairs(models) == frozenset({"attestator_2"})
    real = load_models_toml(ROOT / "config" / "models-real.toml")
    assert recordgold_smoke_chairs(real) == frozenset({"attestator_2"})


def test_the_smoke_asks_dai_exactly_what_its_run_asks() -> None:
    spec = importlib.util.spec_from_file_location(
        "dai_feeding", ROOT / "pipeline" / "3_attestatores" / "feeding.py"
    )
    assert spec is not None and spec.loader is not None
    feeding = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(feeding)
    prompt = feeding.dai_prompt()
    assert recordgold_smoke_prompt() == (prompt["system"], prompt["user"])


def test_the_real_pin_is_a_recordgold_crop_url_with_well_formed_digests() -> None:
    record = RECORDGOLD_SMOKE_RECORD
    parsed = parse_record_url(record.record_url)
    assert parsed.region["w"] == record.image_width
    assert parsed.region["h"] == record.image_height
    assert record.dataset == "Teklia/DAI-CReTDHI-RecordGold-ATR"
    assert len(record.revision) == 40
    for digest in (record.image_sha256, record.text_sha256):
        assert len(digest) == 64 and int(digest, 16) >= 0
    assert record.text_length > 0
    assert "offset=708" in record.rows_url and "length=1" in record.rows_url
