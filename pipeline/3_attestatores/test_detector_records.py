"""How DAI's page reading joins the detector records it was shown.

The page text joins every record's reading in the detector's own order; each
observed box is the record crop DAI was shown, never geometry it reported, and
carries the span of that record's text. One failed record fails the page.
"""

from conftest import load_stage

attestatores = load_stage("3_attestatores")
Attempt = attestatores.Attempt


def _box(y: int, h: int) -> dict[str, int]:
    return {"x": 0, "y": y, "w": 100, "h": h}


# In the detector's own order, which need not be reading order down the page.
RECORDS = [_box(110, 40), _box(10, 40), _box(60, 50), _box(80, 40), _box(300, 50)]


def _attempt(outcome: str, text: str | None, reason: str | None = None) -> Attempt:
    return Attempt(
        outcome=outcome,
        native_payload=text,
        witness_reported=None,
        format_capabilities=attestatores.DEFAULT_FORMAT_CAPABILITIES,
        health=attestatores.content_health(text, completed=True)
        if text is not None
        else attestatores.no_response_health(reason=reason),
        reason=reason,
    )


def _served(attempts: list[Attempt]) -> list[tuple[dict, dict, Attempt]]:
    return [
        ({"payload": {"transform": {"bounds": bounds}}}, {}, attempt)
        for bounds, attempt in zip(RECORDS, attempts, strict=True)
    ]


def test_the_page_text_joins_every_record_in_the_detectors_own_order():
    texts = ["FIRST", "SECOND", "", "FOURTH", "FIFTH"]
    served = _served([_attempt("read" if text else "genuinely-empty", text) for text in texts])

    text, observed = attestatores._detector_page_reading(served)
    outcome, reason, completed = attestatores._detector_page_outcome(served, text)

    assert text == "FIRST\nSECOND\nFOURTH\nFIFTH"
    assert (outcome, reason, completed) == ("read", None, True)
    assert [item["ordinal"] for item in observed] == [0, 1, 2, 3, 4]
    assert [item["bounds"] for item in observed] == RECORDS
    # Echoes of the crops DAI was shown, never geometry it reported.
    assert {item["bounds_source"] for item in observed} == {"presented"}
    # Each record's span covers its own text; an empty reading spans nothing.
    assert [text[item["span"]["start"] : item["span"]["end"]] for item in observed] == texts
    assert observed[2]["span"]["start"] == observed[2]["span"]["end"]


def test_one_failed_record_fails_the_page():
    attempts = [_attempt("read", "FIRST"), _attempt("failed", None, reason="engine refused")]
    attempts += [_attempt("read", text) for text in ("THIRD", "FOURTH", "FIFTH")]
    served = _served(attempts)

    text, observed = attestatores._detector_page_reading(served)
    outcome, reason, _completed = attestatores._detector_page_outcome(served, text)

    assert outcome == "failed"
    assert "record 1: engine refused" in reason
    assert observed[1]["span"] is None


def test_a_page_of_empty_records_is_genuinely_empty():
    served = _served([_attempt("genuinely-empty", "") for _bounds in RECORDS])

    text, _observed = attestatores._detector_page_reading(served)

    assert text == ""
    assert attestatores._detector_page_outcome(served, text)[0] == "genuinely-empty"
