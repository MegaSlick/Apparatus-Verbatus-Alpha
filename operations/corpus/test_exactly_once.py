"""The exactly-once report on synthetic page records and gold rows."""

from __future__ import annotations

import json
import random

import pytest

from common.contracts.canonical import digest_bytes
from common.contracts.stages import PERLECTOR
from common.page_accounting import (
    feed_candidates,
    load_page_accounting_policy,
    page_accounting,
    validate_answer,
)
from common.residual_ink import INK_RUNS_SCHEMA

from .exactly_once import (
    Refusal,
    exactly_once_report,
    gold_records,
    load_page_records,
    summary_lines,
)

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


def band(k: int) -> list[int]:
    return [100, 100 + 400 * k, 900, 400 + 400 * k]


def gold(k: int) -> dict:
    x0, y0, x1, y1 = band(k)
    return {
        "record_id": f"rec-{k}",
        "page_sha256": PAGE_SHA,
        "box_px": [x0, y0, x1, y1],
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


def ink(boxes: list[list[int]]) -> dict:
    """Ink-run evidence on a 1000 x 1400 page, one 400-pixel run per row of each box."""
    rows: list[list[list[int]]] = [[] for _ in range(1400)]
    for x0, y0, _x1, y1 in boxes:
        for y in range(y0, y1):
            rows[y].append([x0 + 50, 400])
    return {"schema": INK_RUNS_SCHEMA, "width": 1000, "height": 1400, "rows": rows}


INK = {"runs": ink([band(k) for k in range(3)]), "coverage_policy": COVERAGE_POLICY}


def page(acts: list[dict], detector: list[list[int]] | None = None, **reading) -> dict:
    """Page records as the stage would publish them, accounted by the real rule set.

    `acts` are answer entries without `n`; the feed has one detector unit per
    box in `detector` (default: one per gold band) and one Surya block per band.
    """
    records = 3
    detector = detector if detector is not None else [band(k) for k in range(records)]
    feed = {
        "schema": "perlector-page-feed.v1",
        "page_id": "page-1",
        "page_ordinal": 1,
        "reading_unit": "page",
        "witnesses": [
            {
                "letter": "A",
                "witness_label": "dai",
                "units": [
                    {
                        "id": f"A{i + 1}",
                        "box_px": box,
                        "text": " ".join(
                            entry_text(k)
                            for k in range(records)
                            if box[1] <= band(k)[1] and band(k)[3] <= box[3]
                        ),
                        "detector_record": True,
                    }
                    for i, box in enumerate(detector)
                ],
            }
        ],
        "surya": {
            "lines": [],
            "blocks": [{"id": f"S{k + 1}", "box_px": band(k)} for k in range(records)],
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
        "parse_state": "parsed",
        "finish_reason": "stop",
        "answer": answer,
        "disposition": "read",
        "capacity": {"image_prompt_tokens": 3000, "prompt_tokens": 2000, "need": 17000},
        **reading,
    }
    accounting = page_accounting(
        feed=feed,
        reading=reading,
        entry_truncation={n: "complete" for n in range(1, len(acts) + 1)},
        ink=INK,
        policy=POLICY,
        feed_ref=None,
        page_reading_ref=None,
    )
    entries = validate_answer(answer, feed_candidates(feed))["entries"]
    return {
        "page_sha256": PAGE_SHA,
        "feed": feed,
        "reading": reading,
        "act_regions": [
            {"n": e["n"], "kind": e["kind"], "union_box_px": e["union_box_px"]} for e in entries
        ],
        "perlectios": [{"schema": "perlectio.v2", "n": e["n"], "text": e["text"]} for e in entries],
        "accounting": accounting,
        "usage": {"prompt_tokens": 5100},
    }


def one_act_each() -> list[dict]:
    return [{"cites": [f"A{k + 1}", f"S{k + 1}"], "text": entry_text(k)} for k in range(3)]


def report(pages: list[dict], records: list[dict] | None = None) -> dict:
    return exactly_once_report(pages, records or [gold(k) for k in range(3)], policy=POLICY)


def test_every_record_read_once_passes_the_gate():
    result = report([page(one_act_each())])

    assert result["gate"] == {
        "exactly_once_bp": 10_000,
        "required_bp": 9_500,
        "uncaught_losses": 0,
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


def test_two_entries_read_as_one_act_are_caught_by_merged_detection():
    acts = one_act_each()
    merged = {
        "cites": acts[0]["cites"] + acts[1]["cites"],
        "text": acts[0]["text"] + " " + acts[1]["text"],
    }
    result = report([page([merged, acts[2]])])

    rows = {row["record_id"]: row for row in result["rows"]}
    # Records 0 and 1 are each held by one act region: read once, but inside a merge.
    assert rows["rec-0"]["act_regions"] == "1"
    assert result["merged_detection"]["fired_on_true_merge"] == 1
    assert result["merged_detection"]["fired_on_single_record"] == 0
    assert result["pages"]["hold_codes"] == {"merged-detection": 1}


def test_a_detector_record_merging_two_entries_is_a_merge_case():
    """Detector A1 spans records 0 and 1; citing it widens act 1's region over both.

    Record 1 then lies in two act regions: duplicated, not lost. Rule (i) is
    silent (A1 is less than half inside act 2); rule (e) holds the page, since
    A1's text carries record 1 and act 1, the only act citing it, reads only record 0.
    """
    wide = [band(0)[0], band(0)[1], band(1)[2], band(1)[3]]
    acts = [
        {"cites": ["A1", "S1"], "text": entry_text(0)},
        {"cites": ["S2"], "text": entry_text(1)},
        {"cites": ["A2", "S3"], "text": entry_text(2)},
    ]
    result = report([page(acts, detector=[wide, band(2)])])

    assert result["records"]["by_merge_class"] == {
        "detector-record": {"records": 2, "exactly_once": 1},
        "no-merge": {"records": 1, "exactly_once": 1},
    }
    assert result["records"]["by_outcome"] == {"duplicated": 1, "exactly-once": 2}
    assert result["records"]["duplicated_on_pages_not_held"] == 0
    assert result["pages"]["hold_codes"] == {"witness-text-not-read": 1}
    assert result["gate"]["uncaught_losses"] == 0
    assert result["merged_detection"]["fired_on_true_merge"] == 0
    assert result["merged_detection"]["silent_on_true_merge"] == 1


def test_a_record_read_in_no_act_is_lost_and_caught_by_the_holding_rules():
    acts = one_act_each()
    acts[2]["text"] = "Le premier juin, rien."
    result = report([page(acts)])

    [lost] = [row for row in result["rows"] if row["outcome"] != "exactly-once"]
    assert lost["outcome"] == "lost"
    assert lost["record_id"] == "rec-2"
    assert lost["text"] == "not-read"
    assert lost["page_held"] is True
    assert result["records"]["lost_caught_by_rule"] == {"e": 1}
    assert result["gate"]["uncaught_losses"] == 0
    assert result["gate"]["passed"] is False  # 2 of 3 is under 95%


def test_a_loss_on_a_page_nothing_held_is_uncaught():
    acts = one_act_each()
    records = [gold(k) for k in range(3)]
    # A gold record the page's own evidence never saw: no witness, no line, no block.
    records.append(
        {
            "record_id": "rec-x",
            "page_sha256": PAGE_SHA,
            "box_px": [100, 1250, 900, 1390],
            "text": entry_text(9),
        }
    )
    result = report([page(acts)], records)

    assert result["gate"]["uncaught_losses"] == 1
    assert result["records"]["uncaught_record_ids"] == ["rec-x"]
    assert result["records"]["by_act_regions"] == {"0": 1, "1": 3}


def test_a_held_reading_publishes_no_region_and_its_records_are_caught():
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
    assert result["gate"]["uncaught_losses"] == 0
    assert result["pages"]["finish_length_bp"] == 10_000
    assert result["pages"]["by_parse_state"] == {"malformed": 1}


def test_a_page_absent_from_the_run_loses_its_records():
    result = report([], [gold(0)])

    assert result["rows"][0]["text"] == "page-not-read"
    assert result["gate"]["uncaught_losses"] == 1


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


def test_gold_records_join_the_ledger_box_and_the_gold_text():
    ledger = [
        {
            "record_id": "r1",
            "page_sha256": PAGE_SHA,
            "bbox": [10, 20, 30, 40],
            "decision": "admitted",
        },
        {"record_id": "r2", "page_sha256": None, "bbox": None, "decision": "refused"},
    ]
    rows = [{"record_id": "r1", "text": "t"}, {"record_id": "r2", "text": "u"}]

    assert gold_records(rows, ledger) == [
        {"record_id": "r1", "page_sha256": PAGE_SHA, "box_px": [10, 20, 40, 60], "text": "t"}
    ]
    with pytest.raises(Refusal, match="no gold row"):
        gold_records([], ledger)


class _Tree:
    """The read surface `load_page_records` uses, over in-memory records."""

    def __init__(self, records: dict[tuple[str, str, str], dict], blobs: dict[str, bytes]):
        self.records, self.blobs = records, blobs

    def build_manifest(self, stage, *, verify_inputs=True):
        return {
            "artifacts": [
                {"kind": kind, "artifact_id": artifact}
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

    records[(PERLECTOR, "page-feed", "f")]["payload"] = {**built["feed"], "reading_unit": "act"}
    with pytest.raises(Refusal, match="not-page-read"):
        load_page_records(_Tree(records, {"call.json": call}))
