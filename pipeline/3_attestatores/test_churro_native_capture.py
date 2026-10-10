"""Exercise Churro capture through a run tree, including retained failures.

The pinned happy scenario must traverse this boundary without moving reading
text; churro-native adds page furniture, transport truncation, and malformed XML.
"""

from __future__ import annotations

import pytest

from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES
from common.runtree.store import RunTree
from conftest import run_through


def _document(text: str) -> str:
    """The fixture's Churro answer for `text`: one `Line` per line of the grammar."""
    lines = "".join(f"<Line>{line}</Line>" for line in text.split("\n"))
    return f"<HistoricalDocument><Page><Body>{lines}</Body></Page></HistoricalDocument>"


HEADER = "[FOLIO RUBRIC 7 -- page furniture, belongs to no entry]"


def _page_testimonia(tree: RunTree) -> dict[tuple[int, str], dict]:
    records = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "page-testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        payload = record["payload"]
        records[(payload["page_ordinal"], payload["chair"])] = record
    return records


@pytest.fixture(scope="module")
def native_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("churro-native") / "runs"
    run_through(root, "r", "churro-native", "attestatores")
    return RunTree(root, "r")


@pytest.fixture(scope="module")
def truncation_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("churro-truncation") / "runs"
    run_through(root, "r", "churro-truncation", "attestatores")
    return RunTree(root, "r")


@pytest.fixture(scope="module")
def happy_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("churro-happy") / "runs"
    run_through(root, "r", "happy", "attestatores")
    return RunTree(root, "r")


def test_a_captured_page_reading_parses_and_keeps_its_raw_bytes(native_run):
    record = _page_testimonia(native_run)[(1, "attestator_3")]
    payload = record["payload"]
    capture = payload["native_capture"]

    assert record["outcome"] == "read"
    assert capture["adapter"] == "churro.v1"
    assert capture["parse"]["state"] == "parsed"
    assert payload["payload"] == capture["parse"]["text"]
    assert payload["payload"].startswith(HEADER)
    raw = native_run.read_bytes(capture["raw_response_ref"]["relative_path"])
    assert raw == _document(payload["payload"]).encode()
    assert capture["findings"] == []
    assert capture["raw_response_ref"] in record["inputs"]
    assert payload["content_health"]["recordable"] is True
    assert payload["content_health"]["truncated"] is False
    assert payload["content_health"]["characters"] == len(payload["payload"])


def test_a_truncated_capture_is_visible_and_is_never_completed_or_retried(truncation_run):
    record = _page_testimonia(truncation_run)[(2, "attestator_3")]
    payload = record["payload"]
    health = payload["content_health"]

    assert record["outcome"] == "read"
    assert health["truncated"] is True
    assert health["truncation_basis"] == "trusted-response-boundary"
    assert payload["native_capture"]["transport_stop_reason"] == "length"
    raw = truncation_run.read_bytes(payload["native_capture"]["raw_response_ref"]["relative_path"])
    assert raw.decode() == _document(payload["payload"])
    assert payload["attempt_ordinal"] == 1


def test_a_captured_response_that_cannot_be_parsed_keeps_its_bytes_and_names_the_cut(native_run):
    record = _page_testimonia(native_run)[(2, "attestator_3")]
    payload = record["payload"]
    health = payload["content_health"]
    capture = payload["native_capture"]

    assert record["outcome"] == "failed"
    assert payload["payload"] is None
    assert health["recordable"] is False
    assert health["encoding"] == "invalid-or-unrecordable"
    assert capture["parse"]["state"] == "failed"
    assert capture["transport_stop_reason"] == "length"
    assert "length" in health["truncation_basis"]
    assert "cut off" in health["truncation_basis"]
    assert "stopped the response at its bound" in payload["reason"]
    raw = native_run.read_bytes(capture["raw_response_ref"]["relative_path"])
    assert raw == (
        b"<HistoricalDocument><Page><Body><Line>"
        + HEADER.encode()
        + b"\nSYNTHETIC ACT TWO delta epsiIon zeta eta"
    )
    # Cut inside the *grammar*: these bytes open `HistoricalDocument` and stop
    # mid-element, so the parser fails rather than reading them. A body that
    # offers no grammar at all is different -- plain reading-order text, which
    # reads (`test_feeding.py`).
    assert not raw.endswith(b"</HistoricalDocument>")


def test_the_pinned_happy_run_captures_through_churro_without_moving_a_reading(happy_run):
    records = _page_testimonia(happy_run)
    assert set(records) == {
        (1, "attestator_1"),
        (1, "attestator_2"),
        (1, "attestator_3"),
        (2, "attestator_1"),
        (2, "attestator_2"),
        (2, "attestator_3"),
    }
    for (page_ordinal, chair), record in records.items():
        payload = record["payload"]
        assert record["outcome"] == "read", (page_ordinal, chair)
        assert payload["content_health"]["truncated"] is False
        if chair == "attestator_2":
            # DAI reads its detector's records (`test_fixture_detector_pages.py`).
            continue
        if chair == "attestator_1":
            # The Chandra chair's fixture page is declared as its own JSON
            # placeholders; a churro capture attributed to it would wear another
            # model boundary's name.
            assert "native_capture" not in payload
            continue
        capture = payload["native_capture"]
        assert capture["adapter"] == "churro.v1"
        assert capture["parse"]["state"] == "parsed"
        assert capture["transport_stop_reason"] == "eos"
        raw = happy_run.read_bytes(capture["raw_response_ref"]["relative_path"])
        assert raw == _document(payload["payload"]).encode()

    assert records[(1, "attestator_1")]["payload"]["payload"] == (
        "SYNTHETIC ACT ONE alpha beta gamma\nSYNTHETIC ACT TWO delta epsilon zeta eta"
    )
    assert records[(2, "attestator_3")]["payload"]["payload"] == (
        "SYNTHETIC ACT TWO delta epsiIon zeta eta"
    )


def test_a_page_testimonium_read_verifies_its_retained_raw_response(tmp_path):
    root = tmp_path / "runs"
    run_through(root, "r", "happy", "attestatores")
    tree = RunTree(root, "r")
    entry, record = next(
        (item, candidate)
        for item in tree.build_manifest(ATTESTATORES)["artifacts"]
        if item["kind"] == "page-testimonium"
        and "native_capture"
        in (candidate := tree.read_artifact(ATTESTATORES, "page-testimonium", item["artifact_id"]))[
            "payload"
        ]
    )
    raw_ref = record["payload"]["native_capture"]["raw_response_ref"]
    tree.resolve(raw_ref["relative_path"]).write_bytes(b"tampered raw response")

    with pytest.raises(SchemaRefusal, match="digest"):
        tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
