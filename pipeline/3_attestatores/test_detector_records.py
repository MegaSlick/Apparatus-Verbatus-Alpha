"""How DAI's page reading over detector records places each act.

A record belongs to the act whose proposal it overlaps most, and to none on a
tie or when it overlaps no act. The page text joins each act's owned records
together, in act order, then the unowned ones; an act's slice spans exactly
the records it owns. One failed record fails the page.
"""

from conftest import load_stage

attestatores = load_stage("3_attestatores")
Attempt = attestatores.Attempt

PAGE = 1
ACTS = [
    {"act_id": "upper", "page_ordinal": PAGE},
    {"act_id": "lower", "page_ordinal": PAGE},
    {"act_id": "unread", "page_ordinal": PAGE},
]


def _box(y: int, h: int) -> dict[str, int]:
    return {"x": 0, "y": y, "w": 100, "h": h}


def _proposal(y: int, h: int) -> dict:
    return {"payload": {"transform": {"source_page_ordinal": PAGE, "bounds": _box(y, h)}}}


REGIONS_BY_ACT = {
    "upper": ([_proposal(0, 100)], None),
    "lower": ([_proposal(100, 100)], None),
    "unread": ([_proposal(400, 100)], None),
}

# In detector order: inside `lower`; inside `upper`; 40 rows on `upper` against
# 10 on `lower`; 20 rows on each (a tie); on no act at all.
RECORDS = [
    ("inside lower", _box(110, 40)),
    ("inside upper", _box(10, 40)),
    ("mostly upper", _box(60, 50)),
    ("tied", _box(80, 40)),
    ("on no act", _box(300, 50)),
]


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
        for (_name, bounds), attempt in zip(RECORDS, attempts, strict=True)
    ]


def _alignments(outcome: str, observed: list[dict]) -> dict[str, dict]:
    return {
        act["act_id"]: attestatores.detector_record_alignment(
            page_outcome=outcome,
            observed=observed,
            act=act,
            page_acts=ACTS,
            regions_by_act=REGIONS_BY_ACT,
        )
        for act in ACTS
    }


def test_each_record_goes_to_its_largest_overlap_and_to_no_act_on_a_tie_or_no_overlap():
    owners = [
        attestatores._record_owner(bounds, PAGE, ACTS, REGIONS_BY_ACT) for _name, bounds in RECORDS
    ]
    assert dict(zip([name for name, _bounds in RECORDS], owners, strict=True)) == {
        "inside lower": "lower",
        "inside upper": "upper",
        "mostly upper": "upper",
        "tied": None,
        "on no act": None,
    }


def test_an_acts_slice_spans_exactly_the_records_it_owns():
    texts = ["LOWER", "UPPER-A", "UPPER-B", "TIED", "STRAY"]
    served = _served([_attempt("read", text) for text in texts])

    text, observed = attestatores._detector_page_reading(served, PAGE, ACTS, REGIONS_BY_ACT)
    outcome, reason, completed = attestatores._detector_page_outcome(served, text)

    # Act order first, each act's records together, then the records no act owns.
    assert text == "UPPER-A\nUPPER-B\nLOWER\nTIED\nSTRAY"
    assert (outcome, reason, completed) == ("read", None, True)
    assert [item["ordinal"] for item in observed] == [0, 1, 2, 3, 4]
    assert [item["bounds"] for item in observed] == [bounds for _name, bounds in RECORDS]
    alignments = _alignments(outcome, observed)
    assert {
        act_id: (alignment["status"], alignment.get("witness_span"))
        for act_id, alignment in alignments.items()
    } == {
        "upper": ("aligned", {"start": 0, "end": len("UPPER-A\nUPPER-B")}),
        "lower": ("aligned", {"start": 16, "end": 21}),
        "unread": ("unaligned", None),
    }
    assert text[16:21] == "LOWER"
    assert alignments["upper"]["anchor_basis"] == "detector-record"
    assert alignments["upper"]["line_geometry"] == [
        {"bbox": RECORDS[1][1]},
        {"bbox": RECORDS[2][1]},
    ]
    assert alignments["unread"]["reason"] == attestatores.NO_DETECTOR_RECORD_OWNED


def test_one_failed_record_fails_the_page_and_places_no_act():
    attempts = [_attempt("read", "LOWER"), _attempt("failed", None, reason="engine refused")]
    attempts += [_attempt("read", text) for text in ("UPPER-B", "TIED", "STRAY")]
    served = _served(attempts)

    text, observed = attestatores._detector_page_reading(served, PAGE, ACTS, REGIONS_BY_ACT)
    outcome, reason, _completed = attestatores._detector_page_outcome(served, text)

    assert outcome == "failed"
    assert "record 1: engine refused" in reason
    assert observed[1]["span"] is None
    assert {act_id: alignment for act_id, alignment in _alignments(outcome, observed).items()} == {
        act["act_id"]: {"status": "unaligned", "reason": "non-reading-page-testimonium-failed"}
        for act in ACTS
    }
