"""The exactly-once report on synthetic page records and gold rows."""

from __future__ import annotations

import copy
import json
import random

import pytest

from common.contracts.canonical import digest_bytes, is_sha256
from common.contracts.stages import PERLECTOR
from common.page_accounting import (
    feed_candidates,
    load_page_accounting_policy,
    page_accounting,
    validate_answer,
)
from common.page_path import ACT_REGION_SCHEMA, PAGE_READING_SCHEMA
from common.residual_ink import INK_RUNS_SCHEMA
from common.sealed_config import SEAL_METHOD, SEAL_METHOD_FIELD

from . import exactly_once
from .exactly_once import (
    MAX_GOLD_CER_BP,
    Refusal,
    _caught_by,
    _gold_cer_bp,
    exactly_once_report,
    gold_records,
    load_page_records,
    sealed_policy_sha256,
    summary_lines,
)
from .normalization import MAX_TEXT_LENGTH

POLICY = load_page_accounting_policy()
PAGE_SHA = "a" * 64
_NAMES = ["Jean Roy", "Marie Côté", "Louis Morin", "Anne Gagnon", "Pierre Lavoie", "Rose Gauthier"]


def entry_text(k: int) -> str:
    rng = random.Random(k)
    return (
        f"Le {k + 2} mai mil huit cent dix, nous prêtre soussigné avons baptisé "
        f"{rng.choice(_NAMES)}, fils de {rng.choice(_NAMES)} et de {rng.choice(_NAMES)}; "
        f"parrain {rng.choice(_NAMES)}, marraine {rng.choice(_NAMES)}, entrée {k}."
    )


def bx(x0: int, y0: int, x1: int, y1: int) -> dict[str, int]:
    """A box as the page records carry it, `{x, y, w, h}`, from its corners."""
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def band(k: int) -> dict[str, int]:
    return bx(100, 100 + 400 * k, 900, 400 + 400 * k)


def gold(k: int) -> dict:
    return {
        "record_id": f"rec-{k}",
        "page_sha256": PAGE_SHA,
        "box_px": band(k),
        "text": entry_text(k),
    }


COVERAGE_POLICY = {
    "substantial_ink_pixels": 5000,
    "edge_band_px": 10,
    "page_spanning_area_bp": 9000,
    "gap_tolerance_px": 2,
    "minimum_ink_pixels": 500,
    "minimum_fraction_outside_bp": 100,
}


def ink(boxes: list[dict[str, int]]) -> dict:
    """Ink-run evidence on a 1000 x 1400 page, one 400-pixel run per row of each box."""
    rows: list[list[list[int]]] = [[] for _ in range(1400)]
    for box in boxes:
        for y in range(box["y"], box["y"] + box["h"]):
            rows[y].append([box["x"] + 50, 400])
    return {"schema": INK_RUNS_SCHEMA, "width": 1000, "height": 1400, "rows": rows}


INK = {"runs": ink([band(k) for k in range(3)]), "coverage_policy": COVERAGE_POLICY}


def page(
    acts: list[dict],
    detector: list[dict[str, int]] | None = None,
    truncation: dict[int, str] | None = None,
    **reading,
) -> dict:
    """Page records as the stage would publish them, accounted by the real rule set.

    `acts` are answer entries without `n`; the feed has one DAI unit per box in
    `detector` (default: one per gold band), each one of the detector's sealed
    records, and one Surya line and one Surya block per band. Acts are placed
    by the lines: a block places nothing.
    """
    records = 3
    detector = detector if detector is not None else [band(k) for k in range(records)]
    feed = {
        "schema": "perlector-page-feed.v2",
        "page_id": "page-1",
        "page_ordinal": 1,
        "page_size": {"w": 1000, "h": 1400},
        "switches": {"witness_units": "own"},
        "witnesses": [
            {
                "letter": "A",
                "witness_label": "dai",
                "outcome": "read",
                "units": [
                    {
                        "id": f"A{i + 1}",
                        "box_px": box,
                        "text": " ".join(
                            entry_text(k)
                            for k in range(records)
                            if box["y"] <= band(k)["y"]
                            and band(k)["y"] + band(k)["h"] <= box["y"] + box["h"]
                        ),
                    }
                    for i, box in enumerate(detector)
                ],
            }
        ],
        "surya": {
            "lines": [{"id": f"L{k + 1}", "box_px": band(k)} for k in range(records)],
            "blocks": [{"id": f"S{k + 1}", "box_px": band(k)} for k in range(records)],
        },
    }
    sealed = [
        {"letter": w["letter"], "outcome": w["outcome"], "blank": False, "units": w["units"]}
        for w in feed["witnesses"]
    ]
    detections = {
        "surya": {
            "lines": [{**line, "ref": line["id"]} for line in feed["surya"]["lines"]],
            "blocks": [{**block, "ref": block["id"]} for block in feed["surya"]["blocks"]],
        },
        "records": [
            {"id": f"A{i + 1}", "box_px": box, "ref": f"record-{i}"}
            for i, box in enumerate(detector)
        ],
        "record_detector": "configured",
        "record_census": {
            "detection_count": len(detector),
            "max_det": 300,
            "max_det_reached": False,
        },
    }
    answer = {
        "acts": [
            {
                "n": n,
                "kind": "act",
                "continues_from_previous_page": False,
                "continues_to_next_page": False,
                **act,
            }
            for n, act in enumerate(acts, start=1)
        ],
        "set_aside": [],
    }
    reading = {
        "schema": PAGE_READING_SCHEMA,
        "parse_state": "parsed",
        "finish_reason": "stop",
        "answer": answer,
        "disposition": "read",
        "capacity": {"image_prompt_tokens": 3000, "prompt_tokens": 2000, "need": 17000},
        **reading,
    }
    accounting = page_accounting(
        feed=feed,
        witnesses=sealed,
        detections=detections,
        reading=reading,
        entry_truncation=truncation or {n: "complete" for n in range(1, len(acts) + 1)},
        ink=INK,
        policy=POLICY,
        feed_ref=None,
        page_reading_ref=None,
    )
    entries = validate_answer(answer, feed_candidates(feed, POLICY))["entries"]
    return {
        "page_sha256": PAGE_SHA,
        "feed": feed,
        "witnesses": sealed,
        "detections": detections,
        "reading": reading,
        "act_regions": [
            {
                "schema": ACT_REGION_SCHEMA,
                "n": e["n"],
                "kind": e["kind"],
                "region_boxes_px": e["region_boxes_px"],
                "union_box_px": e["union_box_px"],
            }
            for e in entries
        ],
        "perlectios": [{"schema": "perlectio.v3", "n": e["n"], "text": e["text"]} for e in entries],
        "accounting": accounting,
        "usage": {"prompt_tokens": 5100},
    }


def one_act_each() -> list[dict]:
    return [{"cites": [f"A{k + 1}", f"L{k + 1}"], "text": entry_text(k)} for k in range(3)]


def report(
    pages: list[dict], records: list[dict] | None = None, sealed: set[str] | None = None
) -> dict:
    return exactly_once_report(
        pages,
        records or [gold(k) for k in range(3)],
        policy=POLICY,
        sealed_policy_sha256=POLICY.sha256,
        sealed_page_sha256s=sealed if sealed is not None else {PAGE_SHA},
    )


def test_every_record_read_once_passes_the_gate():
    result = report([page(one_act_each())])

    assert result["gate"] == {
        "exactly_once_bp": 10_000,
        "required_bp": 9_500,
        "uncaught_failures": 0,
        "unchecked_pages": 0,
        "unmeasured_records": 0,
        "passed": True,
    }
    assert result["records"]["by_act_regions"] == {"1": 3}
    assert result["records"]["by_text"] == {"read": 3}
    assert result["merged_detection"]["silent_on_single_record"] == 3
    assert result["pages"]["prompt_tokens"] == {
        "compared": 1,
        "admitted_total": 5000,
        "engine_total": 5100,
        "engine_over_admitted": 1,
        "largest_engine_excess": 100,
    }
    assert result["pages"]["fit_65536"] == {"fits": 1}
    assert result["pages"]["finish_length_bp"] == 0


def test_a_reading_beyond_the_scoring_bounds_is_an_unmeasured_failure_the_gate_cannot_pass(
    monkeypatch,
):
    acts = one_act_each()
    acts[0]["text"] = "a" * (MAX_TEXT_LENGTH + 1)
    # With no share required, only the unmeasured record can hold the gate shut.
    monkeypatch.setattr(exactly_once, "GATE_EXACTLY_ONCE_BP", 0)

    result = report([page(acts)])

    rows = {row["record_id"]: row for row in result["rows"]}
    assert rows["rec-0"]["text"] == "text-out-of-bounds"
    assert rows["rec-0"]["outcome"] == "unmeasured"
    assert [rows[f"rec-{k}"]["outcome"] for k in (1, 2)] == ["exactly-once", "exactly-once"]
    assert result["gate"]["unmeasured_records"] == 1
    assert result["gate"]["passed"] is False


def test_two_entries_read_as_one_act_are_merged_and_caught_by_merged_detection():
    acts = one_act_each()
    merged = {
        "cites": acts[0]["cites"] + acts[1]["cites"],
        "text": acts[0]["text"] + " " + acts[1]["text"],
    }
    result = report([page([merged, acts[2]])])

    rows = {row["record_id"]: row for row in result["rows"]}
    assert rows["rec-0"]["act_regions"] == "1"
    assert rows["rec-0"]["text"] == "read"
    assert result["records"]["by_outcome"] == {"exactly-once": 1, "merged": 2}
    assert result["records"]["failures_caught_by_rule"] == {"i": 2}
    assert result["merged_detection"]["fired_on_true_merge"] == 1
    assert result["merged_detection"]["fired_on_single_record"] == 0
    assert result["pages"]["hold_codes"] == {"merged-detection": 1}
    assert result["gate"]["uncaught_failures"] == 0
    assert result["gate"]["passed"] is False


def test_a_detector_record_merging_two_entries_is_a_merge_case():
    """Detector A1 spans records 0 and 1; citing it widens act 1's region over both.

    Act 1's region holds two gold records, so both are merged, whatever act 2
    read. Rule (i) is silent (A1 is one record inside one region, the blind spot
    this report measures); rule (e) holds the page, since A1's text carries
    record 1 and act 1, the only act citing it, reads only record 0; and rule
    (h) holds it, since act 2's whole region lies inside act 1's.
    """
    wide = bx(100, band(0)["y"], 900, band(1)["y"] + band(1)["h"])
    acts = [
        {"cites": ["A1", "L1"], "text": entry_text(0)},
        {"cites": ["L2"], "text": entry_text(1)},
        {"cites": ["A2", "L3"], "text": entry_text(2)},
    ]
    result = report([page(acts, detector=[wide, band(2)])])

    assert result["records"]["by_merge_class"] == {
        "dai": {"records": 2, "exactly_once": 0},
        "detector-record": {"records": 2, "exactly_once": 0},
        "no-merge": {"records": 1, "exactly_once": 1},
    }
    assert result["records"]["by_outcome"] == {"exactly-once": 1, "merged": 2}
    assert result["pages"]["hold_codes"] == {"duplicate-region": 1, "witness-text-not-read": 1}
    assert result["records"]["failures_caught_by_rule"] == {"e": 2, "h": 2}
    assert result["gate"]["uncaught_failures"] == 0
    assert result["merged_detection"]["fired_on_true_merge"] == 0
    assert result["merged_detection"]["silent_on_true_merge"] == 1


def test_a_record_read_in_no_act_is_lost_and_caught_by_the_rule_that_touches_it():
    acts = one_act_each()
    acts[2]["text"] = "Le premier juin, rien."
    result = report([page(acts)])

    [lost] = [row for row in result["rows"] if row["outcome"] != "exactly-once"]
    assert lost["outcome"] == "lost"
    assert lost["record_id"] == "rec-2"
    assert lost["text"] == "not-read"
    assert lost["caught_by"] == ["e"]
    assert result["records"]["failures_caught_by_rule"] == {"e": 1}
    assert result["gate"]["uncaught_failures"] == 0
    assert result["gate"]["passed"] is False  # 2 of 3 is under 95%


def test_a_hold_elsewhere_on_the_page_does_not_catch_a_loss():
    """Act 1 is truncated (rule g, on record 0's region); record 2's text is lost."""
    acts = one_act_each()
    acts[2]["text"] = "Le premier juin, rien."
    held = page(acts, truncation={1: "truncated", 2: "complete", 3: "complete"})

    result = report([held])

    [lost] = [row for row in result["rows"] if row["outcome"] == "lost"]
    assert lost["caught_by"] == ["e"]
    assert result["records"]["failures_caught_by_rule"] == {"e": 1}


def test_a_loss_only_an_unrelated_hold_reaches_is_uncaught():
    acts = one_act_each()
    records = [gold(k) for k in range(3)]
    # A gold record the page's own evidence never saw: no witness, no line, no block.
    records.append(
        {
            "record_id": "rec-x",
            "page_sha256": PAGE_SHA,
            "box_px": bx(100, 1250, 900, 1390),
            "text": entry_text(9),
        }
    )
    held = page(acts, truncation={1: "truncated", 2: "complete", 3: "complete"})
    assert held["accounting"]["holds"] == ["reading-incomplete"]

    result = report([held], records)

    assert result["gate"]["uncaught_failures"] == 1
    assert result["records"]["uncaught_record_ids"] == ["rec-x"]
    assert result["records"]["by_act_regions"] == {"0": 1, "1": 3}


def test_a_loss_on_a_page_nothing_held_is_uncaught():
    acts = one_act_each()
    records = [gold(k) for k in range(3)]
    records.append(
        {
            "record_id": "rec-x",
            "page_sha256": PAGE_SHA,
            "box_px": bx(100, 1250, 900, 1390),
            "text": entry_text(9),
        }
    )
    result = report([page(acts)], records)

    assert result["gate"]["uncaught_failures"] == 1
    assert result["records"]["uncaught_record_ids"] == ["rec-x"]


def test_a_page_read_without_an_accounting_is_unchecked_never_caught():
    unchecked = page(one_act_each())
    unchecked["accounting"] = None

    result = report([unchecked])

    assert result["gate"]["unchecked_pages"] == 1
    assert result["gate"]["passed"] is False
    assert result["pages"]["unchecked_page_ids"] == ["page-1"]
    assert result["records"]["unchecked"] == 3
    assert result["records"]["failures_caught_by_rule"] == {}


def test_the_text_measure_is_stricter_than_the_accounting():
    """A reading at 30% error passes rule (e) and still is not read for the proof."""
    rng = random.Random(4)

    def garble(text: str, rate: float) -> str:
        return "".join(
            rng.choice("abcdefgh") if rng.random() < rate and c.isalpha() else c for c in text
        )

    rough, fair = one_act_each(), one_act_each()
    rough[1]["text"] = garble(rough[1]["text"], 0.3)
    fair[1]["text"] = garble(fair[1]["text"], 0.08)
    rough_page, fair_page = page(rough), page(fair)
    assert rough_page["accounting"]["rules"]["e"]["status"] == "pass"

    rough_rows = {row["record_id"]: row for row in report([rough_page])["rows"]}
    fair_rows = {row["record_id"]: row for row in report([fair_page])["rows"]}

    assert rough_rows["rec-1"]["text"] == "not-read"
    assert rough_rows["rec-1"]["outcome"] == "lost"
    assert rough_rows["rec-1"]["caught_by"] == []
    assert fair_rows["rec-1"]["outcome"] == "exactly-once"
    assert MAX_GOLD_CER_BP < 3_000


def test_a_reading_of_a_closer_gold_record_does_not_read_this_one():
    """Twins: record 1's gold differs from record 2's only in its entry number.

    Act 2, on record 1's region, transcribes record 2. Record 1's text is within
    the error bound of that reading, but record 2's is closer, so record 1 is not
    read there.
    """
    twin = entry_text(2)
    records = [gold(0), {**gold(1), "text": twin.replace("entrée 2", "entrée 1")}, gold(2)]
    acts = one_act_each()
    acts[1]["text"] = twin
    result = report([page(acts)], records)

    rows = {row["record_id"]: row for row in result["rows"]}
    assert _gold_cer_bp(records[1]["text"], twin) <= MAX_GOLD_CER_BP
    assert rows["rec-1"]["text"] == "not-read"
    assert rows["rec-1"]["outcome"] == "lost"
    assert rows["rec-2"]["outcome"] == "exactly-once"


def test_a_catch_through_an_unplaced_region_only_is_reported_not_credited():
    box = band(1)
    held = {
        "act_regions": [
            {"n": 1, "kind": "act", "region_boxes_px": [band(0)], "union_box_px": band(0)},
            {"n": 2, "kind": "act", "region_boxes_px": [], "union_box_px": None},
            {"n": 3, "kind": "act", "region_boxes_px": [band(1)], "union_box_px": band(1)},
        ],
        "accounting": {
            "units": [{"id": "C1", "disposition": "cited", "by": [2]}],
            "rules": {
                "b": {"findings": [{"code": "reading-unplaced", "n": 2}]},
                "e": {"findings": [{"code": "witness-text-not-read", "id": "C1"}]},
                "f": {"findings": [{"code": "unread-ink"}]},
                "g": {"findings": [{"code": "reading-incomplete", "n": 3}]},
                "i": {"findings": [{"code": "record-not-read", "id": "A9", "box_px": band(0)}]},
            },
            "holds": [
                "reading-incomplete",
                "reading-unplaced",
                "record-not-read",
                "unread-ink",
                "witness-text-not-read",
            ],
            "flags": [],
        },
    }

    assert _caught_by(held, box) == (["g"], ["f"], ["b", "e"])


def test_only_findings_listed_in_the_accounting_holds_catch_a_failure():
    """A review flag and a finding left out of `holds` catch nothing."""
    box = band(1)
    page_records = {
        "act_regions": [
            {"n": 1, "kind": "act", "region_boxes_px": [band(1)], "union_box_px": band(1)},
        ],
        "accounting": {
            "units": [{"id": "A2", "disposition": "cited", "by": [1]}],
            "rules": {
                "e": {"findings": [{"code": "witness-short-unit-not-read", "id": "A2"}]},
                "i": {"findings": [{"code": "record-read-as-other", "n": 1, "id": "A2"}]},
            },
            "holds": [],
            "flags": ["witness-short-unit-not-read"],
        },
    }

    assert _caught_by(page_records, box) == ([], [], [])
    assert _caught_by(page_records, box, "flags") == (["e"], [], [])


def test_a_loss_only_a_review_flag_reaches_is_uncaught_and_reported_flagged():
    """Rule (e) finds record 2 read otherwise; under a policy that makes that finding a
    review flag the reading is delivered as read, so the loss is not caught."""
    acts = one_act_each()
    acts[2]["text"] = "Le premier juin, rien."
    flagged = page(acts)
    accounting = flagged["accounting"]
    assert accounting["holds"] == ["witness-text-not-read"]
    flagged["accounting"] = {**accounting, "holds": [], "flags": accounting["holds"]}

    result = report([flagged])

    [lost] = [row for row in result["rows"] if row["outcome"] == "lost"]
    assert (lost["caught_by"], lost["flagged_by"]) == ([], ["e"])
    assert result["records"]["failures_caught_by_rule"] == {}
    assert result["records"]["failures_flagged_by_rule"] == {"e": 1}
    assert result["records"]["uncaught_record_ids"] == ["rec-2"]
    assert result["gate"]["uncaught_failures"] == 1


def test_a_held_reading_publishes_no_region_and_its_records_are_caught_page_wide():
    unread = page(one_act_each())
    unread["reading"] = {
        **unread["reading"],
        "parse_state": "malformed",
        "answer": None,
        "disposition": "held",
        "finish_reason": "length",
    }
    unread["accounting"] = page_accounting(
        feed=unread["feed"],
        witnesses=unread["witnesses"],
        detections=unread["detections"],
        reading=unread["reading"],
        entry_truncation={},
        ink=None,
        policy=POLICY,
        feed_ref=None,
        page_reading_ref=None,
    )
    unread["act_regions"], unread["perlectios"] = [], []
    result = report([unread])

    assert result["records"]["by_act_regions"] == {"0": 3}
    assert result["records"]["failures_caught_page_wide_by_rule"] == {"a": 3, "g": 3}
    # Reported, not credited: a page-wide hold does not say which record it saw.
    assert result["records"]["failures_caught_by_rule"] == {}
    assert result["gate"]["uncaught_failures"] == 3
    assert result["pages"]["finish_length_bp"] == 10_000
    assert result["pages"]["by_parse_state"] == {"malformed": 1}


def test_a_page_absent_from_the_run_loses_its_records():
    result = report([], [gold(0)])

    assert result["rows"][0]["text"] == "page-not-read"
    assert result["gate"]["uncaught_failures"] == 1


def test_the_report_and_summary_carry_no_text():
    result = report([page(one_act_each())])
    serialized = json.dumps(result) + "\n".join(summary_lines(result))

    for k in range(3):
        for fragment in ("baptisé", "parrain", entry_text(k)[:20]):
            assert fragment not in serialized


def test_an_accounting_under_another_policy_is_refused():
    other = page(one_act_each())
    other["accounting"] = {**other["accounting"], "policy_sha256": "b" * 64}

    with pytest.raises(Refusal, match="policy-mismatch"):
        report([other])


def test_a_run_sealed_under_another_policy_is_refused_even_without_accountings():
    unaccounted = page(one_act_each())
    unaccounted["accounting"] = None

    with pytest.raises(Refusal, match="policy-mismatch"):
        exactly_once_report(
            [unaccounted],
            [gold(0)],
            policy=POLICY,
            sealed_policy_sha256="b" * 64,
            sealed_page_sha256s={PAGE_SHA},
        )


def _sealed_ledger(gold_body: bytes, text: str = "t") -> dict:
    """A validated ledger admitting one record, r1, as `text`, whose receipt seals `gold_body`."""
    from common.contracts.canonical import self_hash

    from .reference import build_reference_page
    from .test_evaluate import _ledger_for

    reference = build_reference_page(
        page={"sha256": PAGE_SHA, "width": 100, "height": 100},
        source="fixture",
        volume="v",
        designation="page-1",
        split="val",
        records=[
            {
                "record_id": "r1",
                "region": {"x": 10, "y": 20, "w": 30, "h": 40},
                "split": "val",
                "text": text,
                "text_sha256": digest_bytes(text.encode("utf-8")),
            }
        ],
    )
    ledger = _ledger_for(reference)
    ledger["receipt"]["digests"]["gold.jsonl"] = digest_bytes(gold_body)
    del ledger["self_hash"]
    ledger["self_hash"] = self_hash(ledger)
    return ledger


def test_gold_records_join_the_ledger_box_and_the_gold_text():
    body = b'{"record_id": "r1", "text": "t"}\n{"record_id": "r2", "text": "u"}\n'

    assert gold_records(body, _sealed_ledger(body)) == (
        [{"record_id": "r1", "page_sha256": PAGE_SHA, "box_px": bx(10, 20, 40, 60), "text": "t"}],
        1,
    )
    with pytest.raises(Refusal, match="^reference-mismatch: the gold file"):
        gold_records(body.replace(b'"t"', b'"T"'), _sealed_ledger(body))
    for unscorable, reason in [
        (b"", "no gold row"),
        (b'{"record_id": "r1", "text": "T"}\n', "^reference-mismatch: no gold row for 'r1'"),
    ]:
        with pytest.raises(Refusal, match=reason):
            gold_records(unscorable, _sealed_ledger(unscorable))
    blank = b'{"record_id": "r1", "text": " -- [[?]] "}\n'
    with pytest.raises(Refusal, match="admitted record 'r1' has no text to measure"):
        gold_records(blank, _sealed_ledger(blank, " -- [[?]] "))


class _Tree:
    """The read surface `load_page_records` uses, over in-memory records."""

    def __init__(
        self,
        records: dict[tuple[str, str, str], dict],
        blobs: dict[str, bytes],
        run: dict | None = None,
    ):
        self.records, self.blobs, self.run = records, blobs, run

    def read_run(self):
        return self.run

    def build_manifest(self, stage, *, verify_inputs=True):
        return {
            "artifacts": [
                {
                    "kind": kind,
                    "artifact_id": artifact,
                    "relative_path": f"{stage}/{kind}/{artifact}.json",
                    "sha256": "f" * 64,
                }
                for (s, kind, artifact) in self.records
                if s == stage
            ]
        }

    def read_artifact(self, stage, kind, artifact_id):
        return self.records[(stage, kind, artifact_id)]

    def read_bytes(self, relative_path):
        return self.blobs[relative_path]


def test_page_records_are_read_from_a_tree_and_grouped_by_page(monkeypatch):
    built = page(one_act_each())
    call = json.dumps({"usage": {"prompt_tokens": 5100}}).encode()
    reading = {
        **built["reading"],
        "page_id": "page-1",
        "engine_call": {
            "call_record_ref": {"relative_path": "call.json", "sha256": digest_bytes(call)}
        },
    }
    records = {
        (PERLECTOR, "page-feed", "f"): {"subject_id": "page-1", "payload": built["feed"]},
        (PERLECTOR, "page-reading", "r"): {"subject_id": "page-1", "payload": reading},
        (PERLECTOR, "page-accounting", "p"): {
            "subject_id": "page-1",
            "payload": built["accounting"],
        },
    }
    for region, perlectio in zip(built["act_regions"], built["perlectios"], strict=True):
        act_id = f"act-{region['n']}"
        records[(PERLECTOR, "act-region", act_id)] = {
            "subject_id": act_id,
            "payload": {**region, "page_id": "page-1"},
        }
        records[(PERLECTOR, "perlectio", act_id)] = {"subject_id": act_id, "payload": perlectio}
    monkeypatch.setattr(
        "operations.corpus.exactly_once.load_exemplar_page_shas", lambda tree: {1: PAGE_SHA}
    )

    [loaded] = load_page_records(_Tree(records, {"call.json": call}))

    assert loaded["page_sha256"] == PAGE_SHA
    assert loaded["usage"] == {"prompt_tokens": 5100}
    assert len(loaded["act_regions"]) == len(loaded["perlectios"]) == 3
    assert report([loaded])["gate"]["passed"]

    records[(PERLECTOR, "perlectio", "act-2")]["payload"] = {
        **records[(PERLECTOR, "perlectio", "act-2")]["payload"],
        "schema": "perlectio.v1",
    }
    with pytest.raises(Refusal, match="not-page-read: perlectio 'act-2' is 'perlectio.v1'"):
        load_page_records(_Tree(records, {"call.json": call}))
    records[(PERLECTOR, "perlectio", "act-2")]["payload"]["schema"] = "perlectio.v2"

    records[(PERLECTOR, "page-feed", "f")]["payload"] = {
        **built["feed"],
        "schema": "perlector-page-feed.v1",
    }
    with pytest.raises(Refusal, match="not-page-read"):
        load_page_records(_Tree(records, {"call.json": call}))


@pytest.mark.parametrize(
    ("key", "schema"),
    [
        ((PERLECTOR, "act-region", "act-1"), "perlector-act-region.v1"),
        ((PERLECTOR, "page-accounting", "p"), "page-accounting.v1"),
        ((PERLECTOR, "page-reading", "r"), "perlector-page-reading.v1"),
        ((PERLECTOR, "page-feed", "f"), "perlector-page-feed.v1"),
    ],
)
def test_a_page_record_of_another_schema_is_refused_by_name(monkeypatch, key, schema):
    """An act-region without `region_boxes_px`, or any page record of another shape, is refused."""
    built = page(one_act_each())
    records = {
        (PERLECTOR, "page-feed", "f"): {"subject_id": "page-1", "payload": built["feed"]},
        (PERLECTOR, "page-reading", "r"): {
            "subject_id": "page-1",
            "payload": {**built["reading"], "page_id": "page-1", "engine_call": None},
        },
        (PERLECTOR, "page-accounting", "p"): {
            "subject_id": "page-1",
            "payload": built["accounting"],
        },
        (PERLECTOR, "act-region", "act-1"): {
            "subject_id": "act-1",
            "payload": {**built["act_regions"][0], "page_id": "page-1"},
        },
    }
    monkeypatch.setattr(
        "operations.corpus.exactly_once.load_exemplar_page_shas", lambda tree: {1: PAGE_SHA}
    )
    assert len(load_page_records(_Tree(records, {}))[0]["act_regions"]) == 1

    payload = {**records[key]["payload"], "schema": schema}
    payload.pop("region_boxes_px", None)
    records[key] = {**records[key], "payload": payload}
    with pytest.raises(Refusal, match=f"not-page-read: {key[1]} '[^']+' is '{schema}', not "):
        load_page_records(_Tree(records, {}))


def test_the_sealed_policy_is_read_from_the_run():
    run = {SEAL_METHOD_FIELD: SEAL_METHOD, "sealed_config_digests": {"page-accounting": "c" * 64}}
    assert sealed_policy_sha256(_Tree({}, {}, run)) == "c" * 64

    run["sealed_config_digests"] = {"decoding": "c" * 64}
    with pytest.raises(Refusal, match="policy-mismatch: the run sealed no page-accounting"):
        sealed_policy_sha256(_Tree({}, {}, run))


def test_a_record_between_an_act_s_cited_lines_is_not_inside_its_region():
    """The region is the boxes the act names: the rectangle around L1 and L3 holds record 1,
    the region does not, so record 1, which no act names, has no region."""
    acts = [
        {"cites": ["A1", "L1", "A3", "L3"], "text": entry_text(0) + " " + entry_text(2)},
        {"cites": ["A2"], "text": "Le premier juin, rien."},
    ]
    built = page(acts, detector=[band(0), bx(0, 0, 10, 10), band(2)])
    [region] = [r for r in built["act_regions"] if r["n"] == 1]
    assert region["region_boxes_px"] == [band(0), band(2)]
    assert region["union_box_px"] == bx(100, 100, 900, 1200)

    rows = {row["record_id"]: row for row in report([built])["rows"]}

    assert rows["rec-1"]["text"] == "no-region"
    assert rows["rec-1"]["outcome"] == "lost"


def reasked_page() -> dict:
    """Page 1 read twice: the first reading found records 0 and 1, the re-ask record 2.

    The final accounting accounts for the three entries combined; the first
    accounting for the first reading's two.
    """
    acts = one_act_each()
    final = page(acts)
    final["first_accounting"] = page(acts[:2])["accounting"]
    final["reask"] = {**final["reading"], "attempt_ordinal": 2}
    final["reask_usage"] = {"prompt_tokens": 5200}
    final["act_regions"][2] = {**final["act_regions"][2], "reading_attempt": 2, "reading_n": 1}
    return final


def test_a_reasked_page_is_judged_on_its_final_accounting_and_reports_what_the_reask_recovered():
    result = report([reasked_page()])

    assert result["gate"]["passed"] is True
    assert result["records"]["by_outcome"] == {"exactly-once": 3}
    reask = result["reask"]
    assert reask["pages_reasked"] == 1
    assert reask["reasked_page_ids"] == ["page-1"]
    assert reask["reask_by_parse_state"] == {"parsed": 1}
    assert reask["acts_recovered_on_reask"] == 1
    assert reask["reask_duplicates"] == 0
    assert reask["entries_added_by_reask"] == {"act": 1}
    assert reask["before_reask"] == {
        "records": 3,
        "exactly_once": 2,
        "exactly_once_bp": 6_666,
        "by_outcome": {"exactly-once": 2, "lost": 1},
    }
    assert reask["after_reask"]["exactly_once"] == 3
    assert reask["on_reasked_pages"]["before_reask"]["exactly_once"] == 2
    assert reask["records_now_exactly_once"] == 1
    assert reask["records_no_longer_exactly_once"] == 0
    # Both calls are compared against the engine's count.
    assert result["pages"]["prompt_tokens"]["compared"] == 2


def test_a_reask_act_held_as_a_duplicate_is_not_counted_as_recovered():
    """The re-ask read record 0 again: rule (j) holds the added entry as a duplicate of
    the first reading's, so the re-ask recovered nothing."""
    reasked = reasked_page()
    reasked["accounting"]["rules"]["j"]["findings"] = [
        {"code": "reask-duplicate", "n": 3, "reading_n": 1, "attempt_1_n": 1}
    ]
    result = report([reasked])

    reask = result["reask"]
    assert reask["entries_added_by_reask"] == {"act": 1}
    assert reask["acts_recovered_on_reask"] == 0
    assert reask["reask_duplicates"] == 1
    assert "0 act(s) recovered, 1 duplicate(s) held" in "\n".join(summary_lines(result))


def test_a_page_never_reasked_reads_the_same_before_and_after():
    result = report([page(one_act_each())])

    assert result["reask"]["pages_reasked"] == 0
    assert result["reask"]["acts_recovered_on_reask"] == 0
    assert result["reask"]["before_reask"] == result["reask"]["after_reask"]
    assert result["reask"]["on_reasked_pages"]["after_reask"]["records"] == 0


def test_a_reasked_page_whose_last_reading_has_no_accounting_is_unchecked():
    reasked = reasked_page()
    reasked["accounting"] = None

    result = report([reasked])

    assert result["gate"]["unchecked_pages"] == 1
    assert result["gate"]["passed"] is False
    # The first reading was accounted, so it is judged before the re-ask.
    assert result["reask"]["before_reask"]["exactly_once"] == 2


def test_gold_on_pages_the_run_did_not_seal_is_left_out_and_counted():
    elsewhere = {**gold(0), "record_id": "rec-elsewhere", "page_sha256": "b" * 64}
    records = [gold(k) for k in range(3)] + [elsewhere]

    result = report([page(one_act_each())], records)

    assert result["gate"]["passed"] is True
    assert result["records"]["total"] == 3
    assert result["scope"] == {
        "sealed_pages": 1,
        "sealed_pages_with_gold": 1,
        "sealed_pages_with_gold_not_read": 0,
        "ledger_pages_outside_run": 1,
        "ledger_records_outside_run": 1,
    }
    assert "rec-elsewhere" not in {row["record_id"] for row in result["rows"]}


def test_a_run_that_sealed_none_of_the_ledger_pages_measures_nothing_and_fails():
    result = report([], [gold(0)], sealed={"b" * 64})

    assert result["records"]["total"] == 0
    assert result["gate"]["exactly_once_bp"] is None
    assert result["gate"]["passed"] is False
    assert result["scope"]["ledger_records_outside_run"] == 1


def test_a_sealed_page_with_gold_and_no_feed_is_counted_as_not_read():
    result = report([], [gold(0)])

    assert result["scope"]["sealed_pages_with_gold_not_read"] == 1
    assert result["rows"][0]["outcome"] == "lost"


def _reask_tree_records(built: dict, first: dict) -> dict:
    """Page records in the re-ask's shapes: two readings, two accountings, numbered entries."""
    base = {"page_id": "page-1", "engine_call": None}
    records = {
        (PERLECTOR, "page-feed", "f"): {"subject_id": "page-1", "payload": built["feed"]},
        (PERLECTOR, "page-reading", "r1"): {
            "subject_id": "page-1",
            "payload": {**built["reading"], **base, "attempt_ordinal": 1},
        },
        (PERLECTOR, "page-reading", "r2"): {
            "subject_id": "page-1",
            "payload": {**built["reading"], **base, "attempt_ordinal": 2},
        },
        (PERLECTOR, "page-accounting", "p1"): {
            "subject_id": "page-1",
            "payload": {**first["accounting"], "answer_basis": "attempt-1"},
        },
        (PERLECTOR, "page-accounting", "p2"): {
            "subject_id": "page-1",
            "payload": {**built["accounting"], "answer_basis": "combined"},
        },
    }
    for region, perlectio in zip(built["act_regions"], built["perlectios"], strict=True):
        act_id = f"act-{region['n']}"
        recovered = {"reading_attempt": 2, "reading_n": 1} if region["n"] == 3 else {}
        records[(PERLECTOR, "act-region", act_id)] = {
            "subject_id": act_id,
            "payload": {**region, "page_id": "page-1", **recovered},
        }
        records[(PERLECTOR, "perlectio", act_id)] = {
            "subject_id": act_id,
            "payload": {**perlectio, **recovered},
        }
    return records


def test_reask_records_are_read_by_schema_and_kept_apart(monkeypatch):
    acts = one_act_each()
    built, first = page(acts), page(acts[:2])
    records = _reask_tree_records(built, first)
    monkeypatch.setattr(
        "operations.corpus.exactly_once.load_exemplar_page_shas", lambda tree: {1: PAGE_SHA}
    )

    [loaded] = load_page_records(_Tree(records, {}))

    assert loaded["reading"]["attempt_ordinal"] == 1
    assert loaded["reask"]["attempt_ordinal"] == 2
    assert loaded["accounting"]["answer_basis"] == "combined"
    assert loaded["first_accounting"]["answer_basis"] == "attempt-1"
    result = report([loaded])
    assert result["gate"]["passed"] is True
    assert result["reask"]["acts_recovered_on_reask"] == 1
    assert result["reask"]["before_reask"]["exactly_once"] == 2

    # The order the manifest lists them in does not decide which accounting is final.
    reordered = dict(reversed(list(records.items())))
    [again] = load_page_records(_Tree(reordered, {}))
    assert again["accounting"]["answer_basis"] == "combined"

    twice = {
        **records,
        (PERLECTOR, "page-accounting", "p3"): records[(PERLECTOR, "page-accounting", "p2")],
    }
    with pytest.raises(Refusal, match="two accountings at attempt 2"):
        load_page_records(_Tree(twice, {}))

    no_reask = {key: value for key, value in records.items() if key[2] != "r2"}
    with pytest.raises(Refusal, match="accounting for a reading it does not have"):
        load_page_records(_Tree(no_reask, {}))

    third = dict(records)
    third[(PERLECTOR, "page-reading", "r2")] = {
        "subject_id": "page-1",
        "payload": {**records[(PERLECTOR, "page-reading", "r2")]["payload"], "attempt_ordinal": 3},
    }
    with pytest.raises(Refusal, match="reading attempt 3"):
        load_page_records(_Tree(third, {}))

    two_reasks = {
        **records,
        (PERLECTOR, "page-reading", "r3"): records[(PERLECTOR, "page-reading", "r2")],
    }
    with pytest.raises(Refusal, match="two readings at attempt 2"):
        load_page_records(_Tree(two_reasks, {}))

    no_first = {key: value for key, value in records.items() if key[2] not in {"r1", "p1"}}
    with pytest.raises(Refusal, match="has a re-ask and no first reading"):
        load_page_records(_Tree(no_first, {}))

    # A re-ask's accounting is "combined", and "attempt-<n>" from 3 on names an
    # operator re-read, so "attempt-2" is no basis any accounting has.
    unknown_basis = dict(records)
    unknown_basis[(PERLECTOR, "page-accounting", "p2")] = {
        "subject_id": "page-1",
        "payload": {
            **records[(PERLECTOR, "page-accounting", "p2")]["payload"],
            "answer_basis": "attempt-2",
        },
    }
    with pytest.raises(Refusal, match="answer basis 'attempt-2'"):
        load_page_records(_Tree(unknown_basis, {}))

    region = (PERLECTOR, "act-region", "act-3")
    unknown_region_attempt = dict(records)
    unknown_region_attempt[region] = {
        "subject_id": "act-3",
        "payload": {**records[region]["payload"], "reading_attempt": 5},
    }
    with pytest.raises(Refusal, match="an act region names reading attempt 5"):
        load_page_records(_Tree(unknown_region_attempt, {}))


@pytest.fixture(scope="module")
def proof_set(tmp_path_factory):
    """A real run of `page-unbroken`, and an admission ledger sealed over its gold file."""
    from common.contracts.canonical import self_hash
    from common.runtree.store import RunTree

    from .test_evaluate import _fixture_reference_for_page_one, _orchestrate

    root = tmp_path_factory.mktemp("proof-set")
    completed = _orchestrate(root / "runs", "page-unbroken")
    assert completed.returncode == 0, completed.stderr
    tree = RunTree(root / "runs", "r")
    reference = _fixture_reference_for_page_one(tree)
    rows = [{"record_id": act["record_id"], "text": act["text"]} for act in reference["acts"]]
    return {"root": root, "tree": tree, "reference": reference, "rows": rows, "seal": self_hash}


def _seal_set(tmp_path, proof_set, gold_rows, *, sealed_rows=None):
    """Write `gold_rows` as gold.jsonl and a ledger whose receipt seals `sealed_rows`' bytes."""
    from common.contracts.canonical import canonical_bytes

    from .test_evaluate import _ledger_for

    def body(rows):
        return "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")

    ledger = _ledger_for(proof_set["reference"])
    ledger["receipt"]["digests"]["gold.jsonl"] = digest_bytes(body(sealed_rows or gold_rows))
    del ledger["self_hash"]
    ledger["self_hash"] = proof_set["seal"](ledger)
    (tmp_path / "ledger.json").write_bytes(canonical_bytes(ledger))
    (tmp_path / "gold.jsonl").write_bytes(body(gold_rows))
    args = ["--run-root", str(proof_set["root"] / "runs"), "--run-id", "r"]
    args += ["--gold", str(tmp_path / "gold.jsonl"), "--ledger", str(tmp_path / "ledger.json")]
    return ledger, args


def test_the_command_scores_a_selection_and_never_overwrites_or_writes_into_the_tree(
    tmp_path, proof_set
):
    from common.contracts.canonical import canonical_bytes, self_hash

    from .exactly_once import main

    ledger, args = _seal_set(tmp_path, proof_set, proof_set["rows"])
    selection = {
        "ledger_self_hash": ledger["self_hash"],
        "pages": [
            {"page_sha256": proof_set["reference"]["page"]["sha256"]},
            {"page_sha256": "b" * 64},
        ],
    }
    selection["self_hash"] = self_hash(selection)
    (tmp_path / "selection.json").write_bytes(canonical_bytes(selection))
    out = tmp_path / "exactly-once.json"

    main([*args, "--selection", str(tmp_path / "selection.json"), "--out", str(out)])

    written = json.loads(out.read_text())
    assert written["scope"]["basis"] == "selection"
    assert written["scope"]["sealed_pages"] == 2
    assert written["scope"]["selection_self_hash"] == selection["self_hash"]
    assert written["reference"] == {
        "ledger_self_hash": ledger["self_hash"],
        "gold_jsonl_sha256": ledger["receipt"]["digests"]["gold.jsonl"],
        "split": "val",
        "rows_not_scored": 0,
    }
    assert written["schema"] == "exactly-once-report.v5"
    assert written["run"]["run_id"] == "r"
    assert is_sha256(written["run"]["export_sha256"])
    with pytest.raises(Refusal, match="^output-exists:"):
        main([*args, "--out", str(out)])
    with pytest.raises(Refusal, match="^output-in-run-tree:"):
        main([*args, "--out", str(proof_set["tree"].root / "exactly-once.json")])
    selection["ledger_self_hash"] = "c" * 64
    (tmp_path / "selection.json").write_bytes(canonical_bytes(selection))
    with pytest.raises(Refusal, match="^malformed-record:"):
        main([*args, "--selection", str(tmp_path / "selection.json"), "--out", str(tmp_path / "x")])


def test_a_run_without_its_final_export_seal_is_refused_and_no_report_written(tmp_path, proof_set):
    import shutil

    from .exactly_once import main

    _, args = _seal_set(tmp_path, proof_set, proof_set["rows"])
    runs = tmp_path / "runs"
    shutil.copytree(proof_set["root"] / "runs", runs)
    shutil.rmtree(runs / "r" / "7_armarium" / "artifacts" / "stage-seal")
    args[args.index("--run-root") + 1] = str(runs)
    out = tmp_path / "exactly-once.json"

    with pytest.raises(Refusal, match="^no-export:"):
        main([*args, "--out", str(out)])
    assert not out.exists()


def test_gold_text_other_than_the_admitted_file_is_refused_before_any_gate(tmp_path, proof_set):
    """The same record ids with other text -- here, text that would agree with any
    reading -- are not the reference admission sealed, so no report is written."""
    from .exactly_once import main

    substituted = [{**row, "text": row["text"] + " corrigé"} for row in proof_set["rows"]]
    _, args = _seal_set(tmp_path, proof_set, substituted, sealed_rows=proof_set["rows"])
    out = tmp_path / "exactly-once.json"

    with pytest.raises(Refusal, match="^reference-mismatch: .*gold.jsonl"):
        main([*args, "--out", str(out)])
    assert not out.exists()


def test_a_real_reasked_run_is_read_with_its_reask_as_the_receipt_binds_it(tmp_path):
    """`reask-recovers` under the committed re-ask budget: page 1's first reading
    reads a1 alone and the re-ask recovers a2. The loader reads the re-ask's real
    records -- attempt 2, its combined accounting, its recovered region -- and the
    page it re-asked is the one the Recensor's receipt binds a re-ask to."""
    from common.recensor_receipt import RECENSOR_PARTITION_RECEIPT_SCHEMA
    from common.runtree.store import RunTree

    from .compare import ReadOnlyRunTree
    from .test_evaluate import _orchestrate

    completed = _orchestrate(tmp_path / "runs", "reask-recovers")
    assert completed.returncode in (0, 3), completed.stderr
    tree = RunTree(tmp_path / "runs", "r")

    pages = load_page_records(ReadOnlyRunTree(tree))
    reasked = [page for page in pages if page["reask"] is not None]
    [page] = reasked
    assert page["reask"]["attempt_ordinal"] == 2
    assert page["accounting"]["answer_basis"] == "combined"
    assert page["first_accounting"]["answer_basis"] == "attempt-1"
    recovered = [r for r in page["act_regions"] if r.get("reading_attempt") == 2]
    assert [region["kind"] for region in recovered] == ["act"]

    receipt = tree.read_recensor_partition_receipt()
    assert receipt["schema"] == RECENSOR_PARTITION_RECEIPT_SCHEMA
    bound = [row["page_ordinal"] for row in receipt["pages"] if row["reask_ref"] is not None]
    assert bound == [page["feed"]["page_ordinal"]]


class _PathTree(_Tree):
    """`_Tree` whose Recensor receipt binds page 1 to `bound`."""

    bound = f"{PERLECTOR}/page-reading/r3.json"
    bound_sha256 = "f" * 64
    receipt_pages = (1,)

    def read_recensor_partition_receipt(self):
        return {
            "pages": [
                {
                    "page_ordinal": ordinal,
                    "reading_ref": {"relative_path": self.bound, "sha256": self.bound_sha256},
                }
                for ordinal in self.receipt_pages
            ]
        }


def test_a_page_a_person_had_read_again_is_judged_on_its_current_reading(monkeypatch):
    """The re-read supersedes the first reading: only its accounting and regions are judged."""
    acts = one_act_each()
    built, first = page(acts), page(acts[:2])
    path = f"{PERLECTOR}/page-reading/r1.json"
    reread_path = f"{PERLECTOR}/page-reading/r3.json"
    base = {"page_id": "page-1", "engine_call": None}
    records = {
        (PERLECTOR, "page-feed", "f"): {"subject_id": "page-1", "payload": built["feed"]},
        (PERLECTOR, "page-reading", "r1"): {
            "subject_id": "page-1",
            "payload": {**first["reading"], **base, "attempt_ordinal": 1},
        },
        (PERLECTOR, "page-reading", "r3"): {
            "subject_id": "page-1",
            "payload": {
                **built["reading"],
                **base,
                "attempt_ordinal": 3,
                "operator_reread": {
                    "decisions": [
                        {
                            "decision_hash": "d" * 64,
                            "approval_ref": {
                                "relative_path": f"receipts/sha256/{'e' * 64}.json",
                                "sha256": "e" * 64,
                            },
                        }
                    ],
                    "supersedes": [{"relative_path": path, "sha256": "0" * 64}],
                },
            },
        },
        (PERLECTOR, "page-accounting", "p1"): {
            "subject_id": "page-1",
            "payload": {**first["accounting"], "answer_basis": "attempt-1"},
        },
        (PERLECTOR, "page-accounting", "p3"): {
            "subject_id": "page-1",
            "payload": {**built["accounting"], "answer_basis": "attempt-3"},
        },
    }
    for reading, at, built_page in (("r1", path, first), ("r3", reread_path, built)):
        for region, perlectio in zip(
            built_page["act_regions"], built_page["perlectios"], strict=True
        ):
            act_id = f"{reading}-act-{region['n']}"
            ref = {"page_reading_ref": {"relative_path": at, "sha256": "0" * 64}}
            records[(PERLECTOR, "act-region", act_id)] = {
                "subject_id": act_id,
                "payload": {**region, "page_id": "page-1", **ref},
            }
            records[(PERLECTOR, "perlectio", act_id)] = {
                "subject_id": act_id,
                "payload": {**perlectio, **ref},
            }
    monkeypatch.setattr(
        "operations.corpus.exactly_once.load_exemplar_page_shas", lambda tree: {1: PAGE_SHA}
    )

    [loaded] = load_page_records(_PathTree(records, {}))

    assert loaded["reading"]["attempt_ordinal"] == 3
    assert loaded["reask"] is None
    assert loaded["accounting"]["answer_basis"] == "attempt-3"
    assert len(loaded["act_regions"]) == len(built["act_regions"])
    result = report([loaded])
    assert result["gate"]["passed"] is True
    # The person's retry is reported apart: the first reading read two of the three.
    reread = result["operator_reread"]
    assert reread["page_ordinals"] == [1]
    assert reread["earlier_reread_act_records_left_out"] == 0
    assert reread["before_reread"]["exactly_once"] == 2
    assert reread["after_reread"]["exactly_once"] == 3
    assert (reread["records_now_exactly_once"], reread["records_no_longer_exactly_once"]) == (1, 0)
    # The machine never re-asked the page, so the re-ask is credited with nothing:
    # its before and after are the machine's first reading, not the person's retry.
    reask = result["reask"]
    assert (reask["pages_reasked"], reask["acts_recovered_on_reask"]) == (0, 0)
    assert reask["before_reask"]["exactly_once"] == reask["after_reask"]["exactly_once"] == 2
    # A run with no re-read says nothing of one.
    assert "operator_reread" not in report(
        load_page_records(_Tree(_reask_tree_records(built, first), {}))
    )

    # The re-read judged must be the reading the Recensor's receipt binds, path and digest.
    other = _PathTree(records, {})
    other.bound = path
    with pytest.raises(Refusal, match="not the reading the Recensor's receipt binds"):
        load_page_records(other)
    other = _PathTree(records, {})
    other.bound_sha256 = "0" * 64
    with pytest.raises(Refusal, match="not the reading the Recensor's receipt binds"):
        load_page_records(other)
    other = _PathTree(records, {})
    other.receipt_pages = ()
    with pytest.raises(Refusal, match="receipt binds no one reading for page"):
        load_page_records(other)
    # A receipt reference without a path or digest binds nothing, never None == None.
    for missing in ("bound", "bound_sha256"):
        other = _PathTree(records, {})
        setattr(other, missing, None)
        with pytest.raises(Refusal, match="without a path and digest"):
            load_page_records(other)

    # The machine's superseded accountings are scored too, so their policy is checked.
    loaded_twice = load_page_records(_PathTree(records, {}))
    for key in ("accounting", "first_accounting"):
        [stale] = copy.deepcopy(loaded_twice)
        stale["superseded"][key]["policy_sha256"] = "0" * 64
        with pytest.raises(Refusal, match="another page-accounting policy"):
            report([stale])

    # A later re-read supersedes an earlier one: the earlier one's act records are
    # scored by neither judgement, and the report counts them as left out.
    later = dict(records)
    r3 = records[(PERLECTOR, "page-reading", "r3")]
    later[(PERLECTOR, "page-reading", "r4")] = {
        **r3,
        "payload": {**r3["payload"], "attempt_ordinal": 4},
    }
    later[(PERLECTOR, "page-accounting", "p4")] = {
        "subject_id": "page-1",
        "payload": {**built["accounting"], "answer_basis": "attempt-4"},
    }
    bound_later = _PathTree(later, {})
    bound_later.bound = f"{PERLECTOR}/page-reading/r4.json"
    [loaded_later] = load_page_records(bound_later)
    assert loaded_later["act_regions"] == []
    left_out = report([loaded_later])["operator_reread"]["earlier_reread_act_records_left_out"]
    assert left_out == 2 * len(built["act_regions"]) == 6

    # An act record naming no reading of the page is refused, never dropped.
    stray = dict(records)
    region_key = next(key for key in records if key[1] == "act-region")
    region = records[region_key]
    stray[region_key] = {
        **region,
        "payload": {
            **region["payload"],
            "page_reading_ref": {"relative_path": "elsewhere.json", "sha256": "0" * 64},
        },
    }
    with pytest.raises(Refusal, match="naming no reading of the page"):
        load_page_records(_PathTree(stray, {}))


@pytest.mark.parametrize(
    ("extra", "reasons"),
    [
        ("repeat", ["duplicate-record-id"]),
        ("identical-repeat", ["duplicate-record-id"]),
        ("malformed", ["malformed-record"]),
    ],
)
def test_every_record_admission_admitted_is_scored_from_its_sealed_file(tmp_path, extra, reasons):
    """Admission seals rows it refuses beside the rows it admits: a second row naming
    a record, a row with no record id, a blank line. Each admitted record is scored
    from the row whose text the ledger hashed, and the rest are counted as not scored."""
    from .local_admission import admit_local_set
    from .test_local_admission import _two_page_set

    root = _two_page_set(tmp_path / "set")
    body = (root / "gold.jsonl").read_bytes()
    first = json.loads(body.splitlines()[0])
    added = {
        "repeat": {**first, "text": "une autre lecture"},
        "identical-repeat": first,
        "malformed": {**first, "record_id": None},
    }[extra]
    body += b"   \n" + (json.dumps(added, ensure_ascii=False) + "\n").encode()
    (root / "gold.jsonl").write_bytes(body)
    receipt = json.loads((root / "fetch_receipt.json").read_text())
    receipt["artifacts"]["gold_jsonl_sha256"] = digest_bytes(body)
    (root / "fetch_receipt.json").write_text(json.dumps(receipt))
    ledger = admit_local_set(root, split="val")
    refused = [row["reason"] for row in ledger["rows"] if row["decision"] == "refused"]
    assert refused == reasons

    records, not_scored = gold_records(body, ledger)

    assert not_scored == 1
    assert len(records) == ledger["summary"]["admitted"]
    [kept] = [record for record in records if record["record_id"] == first["record_id"]]
    assert kept["text"] == first["text"]
