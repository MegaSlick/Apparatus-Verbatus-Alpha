"""The page accounting on synthetic pages: one pass and one hold per rule."""

from __future__ import annotations

import copy
import random
import time
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.page_accounting import (
    DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH,
    PageAccountingPolicy,
    expand_cites,
    feed_candidates,
    load_page_accounting_policy,
    normalized_text,
    page_accounting,
    validate_answer,
)
from common.residual_ink import INK_RUNS_SCHEMA

POLICY = load_page_accounting_policy()
WIDTH, HEIGHT = 1000, 1400

_FIRST = ["Jean", "Marie", "Joseph", "Louis", "Pierre", "Marguerite", "Angélique", "François"]
_LAST = ["Tremblay", "Gagnon", "Roy", "Côté", "Bouchard", "Gauthier", "Morin", "Lavoie"]
_DAYS = ["premier", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf", "dix"]
_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août"]


def record_text(seed: int) -> str:
    """A synthetic baptism in the register's formula, different names per seed."""
    rng = random.Random(seed)

    def name() -> str:
        return f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"

    return (
        f"Le {rng.choice(_DAYS)} {rng.choice(_MONTHS)} mil huit cent {rng.choice(_DAYS)}, "
        f"nous prêtre soussigné avons baptisé {name()}, né la veille du légitime mariage "
        f"de {name()}, cultivateur de cette paroisse, et de {name()}. Le parrain a été "
        f"{name()}, la marraine {name()}, lesquels ainsi que le père ont déclaré ne savoir "
        f"signer. {name()} ptre"
    )


def noisy(text: str, rate: float, seed: int) -> str:
    """Handwriting-recognition-like errors: deletions, substitutions, insertions."""
    rng = random.Random(seed)
    out = []
    for character in text:
        draw = rng.random()
        if draw < rate / 3:
            continue
        if draw < 2 * rate / 3:
            out.append(rng.choice("abcdefghilmnoprstuv"))
            continue
        out.append(character)
        if draw < rate:
            out.append(rng.choice("aeinrstu"))
    return "".join(out)


def band(k: int) -> list[int]:
    """Record k's box: a 300-pixel band per record, 400 pixels apart."""
    return [100, 100 + 400 * k, 900, 400 + 400 * k]


def line_box(k: int, row: int) -> list[int]:
    top = 100 + 400 * k + 100 * row
    return [100, top, 900, top + 100]


def ink_runs(boxes: list[list[int]]) -> dict:
    """Ink-run evidence with one 400-pixel run on every row of every box."""
    rows: list[list[list[int]]] = [[] for _ in range(HEIGHT)]
    for x0, y0, _x1, y1 in boxes:
        for y in range(y0, y1):
            rows[y].append([x0 + 50, 400])
    for row in rows:
        row.sort()
    return {"schema": INK_RUNS_SCHEMA, "width": WIDTH, "height": HEIGHT, "rows": rows}


COVERAGE_POLICY = {
    "substantial_ink_pixels": 5000,
    "edge_band_px": 10,
    "page_spanning_area_bp": 9000,
    "gap_tolerance_px": 2,
    "minimum_ink_pixels": 500,
    "minimum_fraction_outside_bp": 100,
}


def page(records: int = 3, *, noise: float = 0.0) -> dict:
    """A clean page: three witnesses reading `records` records and a matching answer.

    A is a boxed layout witness, B a record detector (its units are detector
    records), C an unboxed witness. Surya has three lines and one block per record.
    """
    texts = [record_text(k) for k in range(records)]
    feed = {
        "page_id": "page-1",
        "page_ordinal": 1,
        "witnesses": [
            {
                "letter": "A",
                "units": [
                    {"id": f"A{k + 1}", "box_px": band(k), "text": noisy(t, noise, k)}
                    for k, t in enumerate(texts)
                ],
            },
            {
                "letter": "B",
                "units": [
                    {
                        "id": f"B{k + 1}",
                        "box_px": band(k),
                        "text": noisy(t, noise, 100 + k),
                        "detector_record": True,
                    }
                    for k, t in enumerate(texts)
                ],
            },
            {
                "letter": "C",
                "units": [
                    {"id": f"C{k + 1}", "box_px": None, "text": noisy(t, noise, 200 + k)}
                    for k, t in enumerate(texts)
                ],
            },
        ],
        "surya": {
            "lines": [
                {"id": f"L{3 * k + row + 1}", "box_px": line_box(k, row)}
                for k in range(records)
                for row in range(3)
            ],
            "blocks": [{"id": f"S{k + 1}", "box_px": band(k)} for k in range(records)],
        },
    }
    answer = {
        "acts": [
            {
                "n": k + 1,
                "kind": "act",
                "label": "baptism",
                "cites": [f"A{k + 1}", f"B{k + 1}", f"C{k + 1}", f"L{3 * k + 1}-L{3 * k + 3}"],
                "text": t,
                "continues_from_previous_page": False,
                "continues_to_next_page": False,
            }
            for k, t in enumerate(texts)
        ],
        "set_aside": [],
    }
    return {
        "feed": feed,
        "reading": {"parse_state": "parsed", "finish_reason": "stop", "answer": answer},
        "entry_truncation": {k + 1: "complete" for k in range(records)},
        "ink": {
            "runs": ink_runs([band(k) for k in range(records)]),
            "coverage_policy": COVERAGE_POLICY,
        },
    }


def account(case: dict, policy: PageAccountingPolicy = POLICY, **overrides) -> dict:
    arguments = {**case, **overrides}
    return page_accounting(
        **arguments, policy=policy, feed_ref={"ref": "feed"}, page_reading_ref={"ref": "reading"}
    )


def statuses(record: dict) -> dict[str, str]:
    return {name: rule["status"] for name, rule in record["rules"].items()}


def only_hold(record: dict, rule: str) -> None:
    expected = {name: "pass" for name in "abcdefghi"}
    expected[rule] = "hold"
    assert statuses(record) == expected


def codes(record: dict, rule: str) -> list[str]:
    return [finding["code"] for finding in record["rules"][rule]["findings"]]


def acts(case: dict) -> list[dict]:
    return case["reading"]["answer"]["acts"]


# --- the clean page ------------------------------------------------------------------


def test_a_clean_page_passes_every_rule():
    record = account(page())

    assert statuses(record) == {name: "pass" for name in "abcdefghi"}
    assert record["holds"] == []
    assert record["schema"] == "page-accounting.v1"
    assert record["policy_sha256"] == POLICY.sha256
    assert record["units"][0] == {"id": "A1", "disposition": "cited", "by": [1]}
    assert record["lines"][:3] == [{"id": f"L{i}", "inside": [1]} for i in (1, 2, 3)]


def test_noisy_witness_text_still_counts_as_read():
    record = account(page(noise=0.2))

    assert record["rules"]["e"]["status"] == "pass"


# --- rule (a) -------------------------------------------------------------------------


def test_a_answer_cut_at_the_length_limit_holds():
    case = page()
    case["reading"]["finish_reason"] = "length"

    record = account(case)

    assert record["rules"]["a"]["status"] == "hold"
    assert "page-answer-incomplete" in record["holds"]
    assert record["rules"]["g"]["status"] == "hold"


def test_a_malformed_answer_holds_and_nothing_else_is_measured():
    case = page()
    case["reading"] = {"parse_state": "malformed", "finish_reason": "stop", "answer": None}

    record = account(case)

    assert record["rules"]["a"]["status"] == "hold"
    for rule in "bcdefhi":
        assert record["rules"][rule]["status"] == "not-measured"
    assert {row["disposition"] for row in record["units"]} == {"unaccounted"}
    assert record["holds"] == ["page-answer-incomplete"]


def test_an_invalid_answer_holds_under_rule_a():
    case = page()
    acts(case)[1]["n"] = 5

    record = account(case)

    assert record["rules"]["a"]["status"] == "hold"
    assert {"code": "non-contiguous-n"} in [f["problem"] for f in record["rules"]["a"]["findings"]]


# --- rule (b) -------------------------------------------------------------------------


def test_an_unknown_cited_id_holds_under_rule_b():
    case = page()
    acts(case)[0]["cites"].append("A9")

    record = account(case)

    only_hold(record, "b")
    assert record["rules"]["b"]["findings"] == [{"code": "unknown-id", "id": "A9", "n": 1}]


def test_an_entry_citing_no_boxed_id_is_unplaced():
    case = page()
    acts(case).append(
        {
            "n": 4,
            "kind": "other",
            "cites": [],
            "text": "",
            "continues_from_previous_page": False,
            "continues_to_next_page": False,
        }
    )
    case["entry_truncation"][4] = "complete"

    record = account(case)

    only_hold(record, "b")
    assert record["rules"]["b"]["findings"] == [{"code": "reading-unplaced", "n": 4}]


# --- rule (c) -------------------------------------------------------------------------


def test_a_witness_unit_neither_cited_nor_set_aside_holds():
    case = page()
    acts(case)[2]["cites"].remove("C3")

    record = account(case)

    only_hold(record, "c")
    assert record["rules"]["c"]["findings"] == [{"code": "unaccounted-witness-unit", "id": "C3"}]


def test_a_unit_set_aside_with_a_reason_is_accounted_for():
    case = page()
    feed_units = case["feed"]["witnesses"][2]["units"]
    feed_units.append({"id": "C4", "box_px": None, "text": "Registre des baptêmes 1810"})
    case["reading"]["answer"]["set_aside"].append({"id": "C4", "reason": "running header"})

    record = account(case)

    assert statuses(record) == {name: "pass" for name in "abcdefghi"}
    assert {"id": "C4", "disposition": "set-aside", "by": []} in record["units"]


def test_a_unit_set_aside_without_a_reason_is_unaccounted():
    case = page()
    case["feed"]["witnesses"][2]["units"].append({"id": "C4", "box_px": None, "text": "x"})
    case["reading"]["answer"]["set_aside"].append({"id": "C4", "reason": "  "})

    record = account(case)

    assert codes(record, "c") == ["unaccounted-witness-unit"]
    assert record["rules"]["a"]["status"] == "hold"


# --- rule (d) -------------------------------------------------------------------------


def test_a_surya_line_outside_every_region_holds():
    case = page()
    case["feed"]["surya"]["lines"].append({"id": "L10", "box_px": [100, 1300, 900, 1350]})

    record = account(case)

    only_hold(record, "d")
    assert record["rules"]["d"]["findings"] == [{"code": "unread-line", "id": "L10"}]
    assert {"id": "L10", "inside": []} in record["lines"]


def test_a_surya_line_set_aside_is_accounted_for():
    case = page()
    case["feed"]["surya"]["lines"].append({"id": "L10", "box_px": [100, 1300, 900, 1350]})
    case["reading"]["answer"]["set_aside"].append({"id": "L10", "reason": "folio number"})
    case["ink"]["runs"] = ink_runs([band(0), band(1), band(2), [100, 1300, 900, 1350]])

    assert statuses(account(case)) == {name: "pass" for name in "abcdefghi"}


def test_a_line_half_inside_a_region_is_inside():
    case = page()
    # 50 of this line's 100 rows lie in record 3's band (y 900..1200).
    case["feed"]["surya"]["lines"].append({"id": "L10", "box_px": [100, 1150, 900, 1250]})

    record = account(case)

    assert record["rules"]["d"]["status"] == "pass"
    assert {"id": "L10", "inside": [3]} in record["lines"]


# --- rule (e) -------------------------------------------------------------------------


def merged_and_half_read() -> dict:
    """Witness A's second unit merged two records; the one act citing it read only one.

    Every citation, line and pixel of ink is accounted for: only the text of the
    second record, present in the witness and absent from every reading, shows it.
    """
    case = page()
    merged = record_text(1) + " " + record_text(7)
    for witness in case["feed"]["witnesses"]:
        if witness["letter"] == "A":
            witness["units"][1]["text"] = merged
            witness["units"][1]["box_px"] = [100, 500, 900, 1100]
        # The second record lies below the first, where no detector record was cut.
    case["feed"]["witnesses"] = [w for w in case["feed"]["witnesses"] if w["letter"] != "B"]
    for answer_act in acts(case):
        answer_act["cites"] = [cite for cite in answer_act["cites"] if not cite.startswith("B")]
    return case


def test_a_merged_unit_read_only_in_half_holds_under_rule_e_and_nothing_else():
    record = account(merged_and_half_read())

    only_hold(record, "e")
    [finding] = record["rules"]["e"]["findings"]
    assert finding["code"] == "witness-text-not-read"
    assert finding["id"] == "A2"
    assert finding["compared_with"] == [2]
    assert finding["unread_characters"] > POLICY.max_unread_characters
    assert record["holds"] == ["witness-text-not-read"]


def test_the_merged_unit_passes_once_both_records_are_read():
    case = merged_and_half_read()
    acts(case)[1]["text"] = record_text(1) + " " + record_text(7)

    assert account(case)["rules"]["e"]["status"] == "pass"


def test_the_formula_of_a_neighbouring_entry_does_not_cover_an_unread_record():
    """Scoped to the citing entries: record 7 is not found in acts 1 and 3's formula."""
    case = merged_and_half_read()
    acts(case)[0]["cites"].append("A2")

    record = account(case)

    assert codes(record, "e") == ["witness-text-not-read"]


def test_a_deadline_hit_is_not_measured_and_held():
    expired = PageAccountingPolicy(**{**POLICY.__dict__, "deadline_milliseconds": 0})

    record = account(page(), expired)

    assert record["rules"]["e"]["status"] == "not-measured"
    assert set(codes(record, "e")) == {"witness-text-not-measured"}
    assert record["rules"]["e"]["findings"][0]["reason"] == "deadline"
    assert "witness-text-not-measured" in record["holds"]


def test_a_deadline_that_expires_midway_is_not_measured():
    ticks = iter(range(10_000))
    policy = PageAccountingPolicy(**{**POLICY.__dict__, "deadline_milliseconds": 5_000})

    record = account(page(), policy, clock=lambda: float(next(ticks)))

    assert record["rules"]["e"]["status"] == "not-measured"
    assert "witness-text-not-measured" in record["holds"]


def test_a_size_bound_hit_is_not_measured_and_held():
    bounded = PageAccountingPolicy(**{**POLICY.__dict__, "max_characters": 100})

    record = account(page(), bounded)

    assert record["rules"]["e"]["status"] == "not-measured"
    assert {f["reason"] for f in record["rules"]["e"]["findings"]} == {"size-bound"}
    assert "witness-text-not-measured" in record["holds"]


def test_set_aside_units_are_not_compared():
    case = page()
    case["feed"]["witnesses"][2]["units"].append(
        {"id": "C4", "box_px": None, "text": record_text(9)}
    )
    case["reading"]["answer"]["set_aside"].append({"id": "C4", "reason": "struck through"})

    assert account(case)["rules"]["e"]["status"] == "pass"


def test_normalization_ignores_case_accents_markup_punctuation_and_doubt_marks():
    assert normalized_text("<p>Né le <b>DEUX</b> mai,&nbsp;l'an</p>") == "neledeuxmailan"
    assert normalized_text("Jean [[Rov|Roy]] [[?]] ptre") == "jeanrovptre"
    assert normalized_text("Marie [UNCERTAIN] Côté") == "mariecote"


def test_a_dense_page_is_measured_within_the_deadline():
    """Twenty entries, about 12,000 characters per witness, as own units and flat."""
    texts = [record_text(k) + " " + record_text(50 + k) for k in range(20)]
    readings = [
        {
            "n": k + 1,
            "kind": "act",
            "cites": [f"A{k + 1}", "C1"],
            "text": t,
            "continues_from_previous_page": False,
            "continues_to_next_page": False,
        }
        for k, t in enumerate(texts)
    ]
    feed = {
        "page_id": "dense",
        "page_ordinal": 1,
        "witnesses": [
            {
                "letter": "A",
                "units": [
                    {
                        "id": f"A{k + 1}",
                        "box_px": [0, 60 * k, 1000, 60 * k + 60],
                        "text": noisy(t, 0.15, k),
                    }
                    for k, t in enumerate(texts)
                ],
            },
            {
                "letter": "C",
                "units": [{"id": "C1", "box_px": None, "text": noisy(" ".join(texts), 0.15, 99)}],
            },
        ],
        "surya": {"lines": [], "blocks": []},
    }
    assert sum(len(t) for t in texts) > 12_000
    started = time.monotonic()
    record = page_accounting(
        feed=feed,
        reading={
            "parse_state": "parsed",
            "finish_reason": "stop",
            "answer": {"acts": readings, "set_aside": []},
        },
        entry_truncation={k + 1: "complete" for k in range(20)},
        ink=None,
        policy=POLICY,
        feed_ref=None,
        page_reading_ref=None,
    )
    elapsed = time.monotonic() - started

    assert record["rules"]["e"]["status"] == "pass"
    assert elapsed < POLICY.deadline_milliseconds / 1000


# --- rule (f) -------------------------------------------------------------------------


def test_ink_outside_every_region_holds():
    case = page()
    case["ink"]["runs"] = ink_runs([band(0), band(1), band(2), [100, 1250, 900, 1300]])

    record = account(case)

    only_hold(record, "f")
    [finding] = record["rules"]["f"]["findings"]
    assert finding["code"] == "unread-ink"
    assert finding["outside_ink_pixels"] == 50 * 400


def test_ink_inside_a_set_aside_box_is_accounted_for():
    case = page()
    case["feed"]["surya"]["blocks"].append({"id": "S4", "box_px": [100, 1250, 900, 1300]})
    case["reading"]["answer"]["set_aside"].append({"id": "S4", "reason": "stamp"})
    case["ink"]["runs"] = ink_runs([band(0), band(1), band(2), [100, 1250, 900, 1300]])

    assert account(case)["rules"]["f"]["status"] == "pass"


def test_ink_without_evidence_is_not_measured_and_held():
    record = account(page(), ink=None)

    assert record["rules"]["f"]["status"] == "not-measured"
    assert "unread-ink-not-measured" in record["holds"]


# --- rule (g) -------------------------------------------------------------------------


def test_a_truncated_entry_holds():
    case = page()
    case["entry_truncation"][2] = "truncated"

    record = account(case)

    only_hold(record, "g")
    assert record["rules"]["g"]["findings"] == [
        {"code": "reading-incomplete", "n": 2, "truncation": "truncated"}
    ]


def test_an_unclassified_entry_holds():
    case = page()
    del case["entry_truncation"][3]

    only_hold(account(case), "g")


def test_a_failed_call_holds_under_rule_g():
    case = page()
    case["reading"] = {"parse_state": "call-failed", "finish_reason": None, "answer": None}

    record = account(case)

    assert codes(record, "g") == ["reading-incomplete"]
    assert record["rules"]["a"]["status"] == "hold"


# --- rule (h) -------------------------------------------------------------------------


def test_two_entries_on_one_region_hold_both():
    case = page()
    acts(case)[2]["cites"] = list(acts(case)[1]["cites"]) + ["A3", "B3", "C3"]
    acts(case)[1]["cites"] = list(acts(case)[2]["cites"])

    record = account(case)

    assert record["rules"]["h"]["status"] == "hold"
    assert {"code": "duplicate-region", "ns": [2, 3], "union_box_px": [100, 500, 900, 1200]} in (
        record["rules"]["h"]["findings"]
    )


def test_a_line_inside_two_regions_is_recorded_not_held():
    case = page()
    acts(case)[1]["cites"].append("L3")
    # Record 1's band grows to reach line 4: the line now lies in both regions.
    case["feed"]["surya"]["blocks"][0]["box_px"] = [100, 100, 900, 600]
    acts(case)[0]["cites"].append("S1")

    record = account(case)

    assert record["rules"]["h"]["status"] == "pass"
    assert {"code": "shared-line", "id": "L4", "inside": [1, 2]} in record["rules"]["h"]["findings"]
    assert "shared-line" not in record["holds"]


# --- rule (i) -------------------------------------------------------------------------


def two_entries_read_as_one() -> dict:
    """The Perlector read records 1 and 2 as one act, citing and transcribing both."""
    case = page()
    first, second = acts(case)[0], acts(case)[1]
    first["cites"] = first["cites"] + second["cites"]
    first["text"] = first["text"] + " " + second["text"]
    del acts(case)[1]
    acts(case)[1]["n"] = 2
    case["entry_truncation"] = {1: "complete", 2: "complete"}
    return case


def test_two_entries_read_as_one_act_hold_only_under_rule_i():
    record = account(two_entries_read_as_one())

    only_hold(record, "i")
    assert record["rules"]["i"]["findings"] == [
        {"code": "merged-detection", "n": 1, "ids": ["B1", "B2"]}
    ]
    assert record["holds"] == ["merged-detection"]


def test_a_detector_record_in_no_act_region_is_recorded_not_held():
    case = page()
    case["feed"]["witnesses"][1]["units"].append(
        {"id": "B4", "box_px": [100, 1250, 900, 1300], "text": "", "detector_record": True}
    )
    case["reading"]["answer"]["set_aside"].append({"id": "B4", "reason": "blot"})
    case["ink"]["runs"] = ink_runs([band(0), band(1), band(2), [100, 1250, 900, 1300]])

    record = account(case)

    assert record["rules"]["i"] == {
        "status": "pass",
        "findings": [{"code": "undetected-read", "id": "B4"}],
    }
    assert record["holds"] == []


def test_a_detector_record_inside_only_an_other_region_is_undetected_read():
    case = page()
    acts(case)[2]["kind"] = "other"

    record = account(case)

    assert record["rules"]["i"]["findings"] == [{"code": "undetected-read", "id": "B3"}]
    assert record["rules"]["i"]["status"] == "pass"


# --- order does not matter --------------------------------------------------------------


@pytest.mark.parametrize("seed", range(5))
def test_candidate_order_does_not_change_the_record(seed):
    for case in (page(), merged_and_half_read(), two_entries_read_as_one()):
        case["reading"]["answer"]["set_aside"] = [
            {"id": "L2", "reason": "a"},
            {"id": "C1", "reason": "b"},
        ]
        for act in acts(case):
            act["cites"] = [c for c in act["cites"] if c not in {"C1"}]
        shuffled = copy.deepcopy(case)
        rng = random.Random(seed)
        rng.shuffle(shuffled["feed"]["witnesses"])
        for witness in shuffled["feed"]["witnesses"]:
            rng.shuffle(witness["units"])
        rng.shuffle(shuffled["feed"]["surya"]["lines"])
        rng.shuffle(shuffled["feed"]["surya"]["blocks"])
        rng.shuffle(shuffled["reading"]["answer"]["set_aside"])
        for act in acts(shuffled):
            rng.shuffle(act["cites"])

        assert account(shuffled) == account(case)


# --- the shared answer reading ------------------------------------------------------------


CANDIDATES = {
    **{f"A{i}": [100 * i, 0, 100 * i + 10, 10] for i in range(1, 4)},
    **{f"L{i}": [0, 10 * i, 10, 10 * i + 10] for i in range(1, 21)},
    "C1": None,
}


def test_a_range_expands_to_every_id_between_inclusive():
    assert expand_cites(["L10-L13", "A2"], CANDIDATES) == (["L10", "L11", "L12", "L13", "A2"], [])
    assert expand_cites(["L4-L4"], CANDIDATES) == (["L4"], [])


@pytest.mark.parametrize("cite", ["L13-L10", "L1-A3", "L1-", "L01", "l1", "L1 - L3", 7])
def test_a_malformed_citation_is_refused_not_guessed(cite):
    ids, problems = expand_cites([cite], CANDIDATES)

    assert ids == []
    assert [problem["code"] for problem in problems] == ["malformed-range"]


def test_a_range_past_the_last_id_is_unknown():
    ids, problems = expand_cites(["L19-L25"], CANDIDATES)

    assert ids == []
    assert problems == [{"code": "unknown-id", "id": "L25"}]


def _entry(n: int, cites: list, **extra) -> dict:
    return {
        "n": n,
        "kind": "act",
        "cites": cites,
        "text": "x",
        "continues_from_previous_page": False,
        "continues_to_next_page": False,
        **extra,
    }


def problem_codes(answer) -> list[str]:
    return sorted(problem["code"] for problem in validate_answer(answer, CANDIDATES)["problems"])


def test_validation_names_every_problem():
    assert problem_codes([]) == ["answer-not-an-object"]
    assert problem_codes({"acts": [], "set_aside": [], "extra": 1}) == ["answer-not-an-object"]
    assert problem_codes({"acts": [_entry(1, ["A1"]), _entry(3, ["A2"])], "set_aside": []}) == [
        "non-contiguous-n"
    ]
    assert problem_codes(
        {"acts": [_entry(1, ["A1"])], "set_aside": [{"id": "A1", "reason": "r"}]}
    ) == ["cited-and-set-aside"]
    assert problem_codes(
        {
            "acts": [_entry(1, ["A1"])],
            "set_aside": [{"id": "L1-L2", "reason": "r"}, {"id": "L2", "reason": "r"}],
        }
    ) == ["set-aside-twice"]
    assert problem_codes(
        {"acts": [_entry(1, ["A1"])], "set_aside": [{"id": "L1", "reason": ""}]}
    ) == ["set-aside-without-reason"]
    assert problem_codes(
        {
            "acts": [
                _entry(1, ["A1"]),
                _entry(2, ["L1"], continues_from_previous_page=True),
                _entry(3, ["L5"], continues_to_next_page=True),
            ],
            "set_aside": [],
        }
    ) == ["continuation-flag-on-non-edge-act"]
    assert problem_codes({"acts": [_entry(1, ["A1"]), _entry(2, ["A1"])], "set_aside": []}) == [
        "duplicate-region"
    ]
    assert problem_codes({"acts": [_entry(1, ["A1"], extra=1)], "set_aside": []}) == [
        "malformed-entry"
    ]
    assert problem_codes({"acts": [_entry(1, ["A1"], label="x" * 81)], "set_aside": []}) == [
        "malformed-entry"
    ]


def test_edge_continuation_flags_and_an_unplaced_entry_are_valid():
    answer = {
        "acts": [
            _entry(1, ["A1"], continues_from_previous_page=True),
            _entry(2, ["C1"]),
            _entry(3, ["L5"], continues_to_next_page=True),
        ],
        "set_aside": [],
    }

    validated = validate_answer(answer, CANDIDATES)

    assert validated["problems"] == []
    assert [entry["union_box_px"] for entry in validated["entries"]] == [
        [100, 0, 110, 10],
        None,
        [0, 50, 10, 60],
    ]


def test_the_union_box_bounds_every_cited_box_unpadded():
    validated = validate_answer({"acts": [_entry(1, ["L2-L4", "C1"])], "set_aside": []}, CANDIDATES)

    assert validated["entries"][0]["cited_ids"] == ["L2", "L3", "L4", "C1"]
    assert validated["entries"][0]["union_box_px"] == [0, 20, 10, 50]


def test_a_feed_with_a_repeated_or_malformed_id_is_refused():
    feed = page()["feed"]
    feed["surya"]["lines"].append({"id": "L1", "box_px": [0, 0, 1, 1]})
    with pytest.raises(ContractError, match="twice"):
        feed_candidates(feed)
    feed = page()["feed"]
    feed["surya"]["lines"][0]["box_px"] = [5, 5, 5, 9]
    with pytest.raises(ContractError, match="box_px"):
        feed_candidates(feed)


# --- the sealed policy ------------------------------------------------------------------


def test_the_policy_is_closed(tmp_path: Path):
    text = DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH.read_text(encoding="utf-8")
    extra = tmp_path / "extra.toml"
    extra.write_text(text + "\n[other]\nx = 1\n", encoding="utf-8")
    with pytest.raises(ContractError, match="closed schema"):
        load_page_accounting_policy(extra)
    missing = tmp_path / "missing.toml"
    missing.write_text(text.replace("anchor_characters = 6", ""), encoding="utf-8")
    with pytest.raises(ContractError, match="closed schema"):
        load_page_accounting_policy(missing)
    zero = tmp_path / "zero.toml"
    zero.write_text(text.replace("min_area_bp = 5000", "min_area_bp = 0"), encoding="utf-8")
    with pytest.raises(ContractError, match="min_area_bp"):
        load_page_accounting_policy(zero)
    boolean = tmp_path / "bool.toml"
    boolean.write_text(
        text.replace("max_unread_characters = 60", "max_unread_characters = true"),
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="integer"):
        load_page_accounting_policy(boolean)
