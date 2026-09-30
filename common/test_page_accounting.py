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
    best_substring_distance,
    expand_cites,
    feed_candidates,
    load_page_accounting_policy,
    normalized_text,
    page_accounting,
    require_page_accounting_policy,
    validate_answer,
)
from common.residual_ink import INK_RUNS_SCHEMA

POLICY = load_page_accounting_policy()
WIDTH, HEIGHT = 1000, 1400
RULE_NAMES = "abcdefghi"

_FIRST = ["Jean", "Marie", "Joseph", "Louis", "Pierre", "Marguerite", "Angélique", "François"]
_LAST = ["Tremblay", "Gagnon", "Roy", "Côté", "Bouchard", "Gauthier", "Morin", "Lavoie"]
_DAYS = ["premier", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf", "dix"]
_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août"]
BURIAL = "Le deux mai a été inhumé Jean Roy, âgé de trois jours. Morin ptre"


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


def bx(x0: int, y0: int, x1: int, y1: int) -> dict[str, int]:
    """A box as the accounting reads it, `{x, y, w, h}`, from its corners."""
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def band(k: int) -> dict[str, int]:
    """Record k's box: a 300-pixel band per record, 400 pixels apart."""
    return bx(100, 100 + 400 * k, 900, 400 + 400 * k)


def line_box(k: int, row: int) -> dict[str, int]:
    top = 100 + 400 * k + 100 * row
    return bx(100, top, 900, top + 100)


def ink_runs(boxes: list[dict[str, int]]) -> dict:
    """Ink-run evidence with one 400-pixel run on every row of every box."""
    rows: list[list[list[int]]] = [[] for _ in range(HEIGHT)]
    for box in boxes:
        for y in range(box["y"], box["y"] + box["h"]):
            rows[y].append([box["x"] + 50, 400])
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


def page(
    records: int = 3, *, noise: float = 0.0, reading_noise: float = 0.0, seed: int = 0
) -> dict:
    """A clean page: three witnesses reading `records` records and a matching answer.

    A is a boxed layout witness, B the record detector (its units are the
    detector's records, which the sealed detections also carry), C an unboxed
    witness. Surya has three lines and one block per record, shown and sealed.
    The sealed witnesses are the feed's own witness objects, so a test that
    edits a shown witness edits what was sealed; dropping one from the feed
    hides it from the model without unsealing it.
    """
    texts = [record_text(seed * 100 + k) for k in range(records)]

    def unit(letter: str, k: int, text: str, box: dict | None, offset: int) -> dict:
        return {"id": f"{letter}{k + 1}", "box_px": box, "text": noisy(text, noise, offset + k)}

    feed = {
        "page_id": "page-1",
        "page_ordinal": 1,
        "switches": {"witness_units": "own"},
        "witnesses": [
            {
                "letter": "A",
                "outcome": "read",
                "blank": False,
                "units": [unit("A", k, t, band(k), seed) for k, t in enumerate(texts)],
            },
            {
                "letter": "B",
                "outcome": "read",
                "blank": False,
                "units": [unit("B", k, t, band(k), seed + 100) for k, t in enumerate(texts)],
            },
            {
                "letter": "C",
                "outcome": "read",
                "blank": False,
                "units": [unit("C", k, t, None, seed + 200) for k, t in enumerate(texts)],
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
    detections = {
        "surya": {
            "lines": [
                {**line, "ref": f"surya-line-{line['id']}"} for line in feed["surya"]["lines"]
            ],
            "blocks": [
                {**block, "ref": f"surya-block-{block['id']}"} for block in feed["surya"]["blocks"]
            ],
        },
        "records": [
            {"id": f"B{k + 1}", "box_px": band(k), "ref": f"record-{k}"} for k in range(records)
        ],
        "record_detector": "configured",
        "record_census": {"detection_count": records, "max_det": 300, "max_det_reached": False},
    }
    answer = {
        "acts": [
            {
                "n": k + 1,
                "kind": "act",
                "label": "baptism",
                "cites": [f"A{k + 1}", f"B{k + 1}", f"C{k + 1}", f"L{3 * k + 1}-L{3 * k + 3}"],
                "text": noisy(t, reading_noise, seed + 900 + k),
                "continues_from_previous_page": False,
                "continues_to_next_page": False,
            }
            for k, t in enumerate(texts)
        ],
        "set_aside": [],
    }
    return {
        "feed": feed,
        "witnesses": list(feed["witnesses"]),
        "detections": detections,
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


def only_hold(record: dict, rule: str, **others: str) -> None:
    expected = {name: "pass" for name in RULE_NAMES}
    expected[rule] = "hold"
    assert statuses(record) == {**expected, **others}


def codes(record: dict, rule: str) -> list[str]:
    return [finding["code"] for finding in record["rules"][rule]["findings"]]


def acts(case: dict) -> list[dict]:
    return case["reading"]["answer"]["acts"]


def witness(case: dict, letter: str) -> dict:
    [found] = [w for w in case["feed"]["witnesses"] if w["letter"] == letter]
    return found


def add_line(case: dict, identifier: str | None, box: dict, *, shown: bool = True) -> None:
    """A Surya line in the sealed census, and in the feed when `shown`."""
    if shown:
        case["feed"]["surya"]["lines"].append({"id": identifier, "box_px": box})
    case["detections"]["surya"]["lines"].append(
        {"id": identifier if shown else None, "box_px": box, "ref": f"surya-line-{box['y']}"}
    )


def set_block_box(case: dict, identifier: str, box: dict) -> None:
    for block in case["feed"]["surya"]["blocks"] + case["detections"]["surya"]["blocks"]:
        if block["id"] == identifier:
            block["box_px"] = box


def unit_measure(record: dict, identifier: str) -> dict:
    [found] = [m for m in record["rules"]["e"]["measurements"] if m["id"] == identifier]
    return found


# --- the clean page ------------------------------------------------------------------


def test_a_clean_page_passes_every_rule():
    record = account(page())

    assert statuses(record) == {name: "pass" for name in RULE_NAMES}
    assert record["holds"] == []
    assert record["schema"] == "page-accounting.v1"
    assert record["policy_sha256"] == POLICY.sha256
    assert record["units"][0] == {"id": "A1", "disposition": "cited", "by": [1]}
    assert record["lines"][:3] == [
        {"id": f"L{i}", "ref": f"surya-line-L{i}", "inside": [1]} for i in (1, 2, 3)
    ]
    assert record["records"][0] == {
        "id": "B1",
        "ref": "record-0",
        "box_px": band(0),
        "act": [1],
        "other": [],
        "set_aside": False,
    }


# The false-hold side of rule (e): correct readings whose disagreement with the
# witnesses is about 15% and 30% of characters, on either side or split. Each
# case is six pages of three records and three witnesses.
NOISY_BUT_CORRECT = [(0.15, 0.0), (0.0, 0.15), (0.3, 0.0), (0.0, 0.3), (0.15, 0.15)]


@pytest.mark.parametrize(("witness_noise", "reading_noise"), NOISY_BUT_CORRECT)
def test_noisy_but_correct_readings_pass_with_margin(witness_noise, reading_noise):
    measured = []
    for seed in range(6):
        record = account(page(noise=witness_noise, reading_noise=reading_noise, seed=seed))
        assert record["rules"]["e"]["status"] == "pass", record["rules"]["e"]["findings"]
        measured += record["rules"]["e"]["measurements"]

    # Margins, so a threshold change that eats them fails here rather than on a page.
    assert max(m["unread_run"] for m in measured) <= POLICY.max_unread_characters // 2
    assert max(m["unread_share_bp"] for m in measured) <= POLICY.max_unread_share_bp // 2
    shares = [m["distinctive_share_bp"] for m in measured if m["distinctive_share_bp"] is not None]
    assert len(shares) >= len(measured) * 9 // 10
    assert min(shares) >= POLICY.min_distinctive_share_bp + 500


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
    assert [f["problem"]["grammar"] for f in record["rules"]["a"]["findings"]] == [
        "n-not-contiguous"
    ]


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


def test_a_short_unit_set_aside_with_a_reason_is_accounted_for():
    case = page()
    witness(case, "C")["units"].append({"id": "C4", "box_px": None, "text": "Registre 1810, f. 12"})
    case["reading"]["answer"]["set_aside"].append({"id": "C4", "reason": "running header"})

    record = account(case)

    assert statuses(record) == {name: "pass" for name in RULE_NAMES}
    assert {"id": "C4", "disposition": "set-aside", "by": []} in record["units"]


@pytest.mark.parametrize(
    "text",
    [
        "Roy, Jean-Baptiste, baptême, 12 mai 1810 .......... f. 34",  # an index row
        BURIAL,  # a one-line burial
    ],
)
def test_a_set_aside_unit_longer_than_a_folio_or_short_header_holds(text):
    case = page()
    witness(case, "C")["units"].append({"id": "C4", "box_px": None, "text": text})
    case["reading"]["answer"]["set_aside"].append({"id": "C4", "reason": "index"})

    record = account(case)

    assert len(normalized_text(text)) > POLICY.max_set_aside_characters
    assert len(normalized_text(text)) < POLICY.max_unread_characters
    assert codes(record, "c") == ["set-aside-substantial"]
    assert record["rules"]["c"]["findings"][0]["unit_characters"] == len(normalized_text(text))


def test_setting_aside_a_whole_act_holds():
    """Every other witness read record 3; the reader set aside A3 and read nothing there."""
    case = page()
    acts(case)[2]["cites"].remove("A3")
    case["reading"]["answer"]["set_aside"].append({"id": "A3", "reason": "struck through"})

    record = account(case)

    assert codes(record, "c") == ["set-aside-substantial"]
    [finding] = record["rules"]["c"]["findings"]
    assert finding["id"] == "A3"
    assert finding["reason"] == "struck through"
    assert finding["box_px"] == band(2)
    assert record["rules"]["e"]["status"] == "pass"  # set aside, so not compared


def test_setting_aside_every_unit_of_an_act_holds_every_way():
    case = page()
    del acts(case)[2]
    case["reading"]["answer"]["set_aside"].append({"id": "A3", "reason": "not an act"})
    case["reading"]["answer"]["set_aside"].append({"id": "B3", "reason": "not an act"})
    case["reading"]["answer"]["set_aside"].append({"id": "C3", "reason": "not an act"})
    case["reading"]["answer"]["set_aside"].append({"id": "L7-L9", "reason": "not an act"})
    case["reading"]["answer"]["set_aside"].append({"id": "S3", "reason": "not an act"})
    del case["entry_truncation"][3]

    record = account(case)

    assert set(codes(record, "c")) == {"set-aside-substantial"}
    assert record["rules"]["d"]["status"] == "pass"  # set-aside lines stay quiet under (d)
    assert codes(record, "f") == ["unread-ink"]  # but their ink is not read
    assert codes(record, "i") == ["set-aside-record"]


def test_a_unit_set_aside_without_a_reason_is_unaccounted():
    case = page()
    witness(case, "C")["units"].append({"id": "C4", "box_px": None, "text": "x"})
    case["reading"]["answer"]["set_aside"].append({"id": "C4", "reason": "  "})

    record = account(case)

    assert codes(record, "c") == ["unaccounted-witness-unit"]
    assert record["rules"]["a"]["status"] == "hold"


@pytest.mark.parametrize("outcome", ["failed", "refused", None])
def test_a_witness_that_did_not_read_the_page_holds(outcome):
    case = page()
    failed = witness(case, "C")
    failed.update(units=[], outcome=outcome, blank=None)
    for entry in acts(case):
        entry["cites"] = [cite for cite in entry["cites"] if not cite.startswith("C")]

    record = account(case)

    expected = {"code": "witness-not-read", "letter": "C", "outcome": outcome}
    assert record["rules"]["c"]["findings"] == [expected]
    assert expected in record["rules"]["e"]["findings"]
    assert record["holds"] == ["witness-not-read"]


def test_a_witness_that_read_the_page_but_gave_no_unit_holds():
    case = page()
    witness(case, "C")["units"] = []
    for entry in acts(case):
        entry["cites"] = [cite for cite in entry["cites"] if not cite.startswith("C")]

    record = account(case)

    expected = {"code": "witness-read-no-units", "letter": "C", "outcome": "read"}
    assert record["rules"]["c"]["findings"] == [expected]
    assert expected in record["rules"]["e"]["findings"]
    assert record["holds"] == ["witness-read-no-units"]


def test_a_witness_that_reports_a_blank_page_is_recorded_not_held():
    case = page()
    witness(case, "C").update(units=[], blank=True)
    for entry in acts(case):
        entry["cites"] = [cite for cite in entry["cites"] if not cite.startswith("C")]

    record = account(case)

    expected = {"code": "witness-read-blank", "letter": "C", "outcome": "read"}
    assert record["rules"]["c"] == {"status": "pass", "findings": [expected]}
    assert record["holds"] == []

    witness(case, "A")["blank"] = True
    with pytest.raises(ContractError, match="reports a blank page and gives units"):
        account(case)


def hide_witness(case: dict, letter: str) -> None:
    """The feed's `witnesses` switch hides one sealed witness from the model."""
    case["feed"]["witnesses"] = [w for w in case["feed"]["witnesses"] if w["letter"] != letter]
    for entry in acts(case):
        entry["cites"] = [cite for cite in entry["cites"] if not cite.startswith(letter)]


def test_a_witness_hidden_by_the_feed_is_still_measured_against_every_reading():
    case = page()
    hide_witness(case, "C")

    record = account(case)

    assert statuses(record) == {name: "pass" for name in RULE_NAMES}
    assert {"id": "C2", "disposition": "not-shown", "by": []} in record["units"]
    assert {m["id"] for m in record["rules"]["e"]["measurements"]} >= {"C1", "C2", "C3"}


MARRIAGE = (
    "Le douze juin mil huit cent dix, après la publication de trois bans, nous prêtre "
    "soussigné avons reçu le mutuel consentement de mariage de Hyacinthe Desrosiers, "
    "journalier, et de Scholastique Beaulieu, en présence de Narcisse Lapointe. Morin ptre"
)


def test_a_record_only_a_hidden_witness_read_holds_under_rule_e_not_c():
    case = page()
    witness(case, "C")["units"].append({"id": "C4", "box_px": None, "text": MARRIAGE})
    hide_witness(case, "C")

    record = account(case)

    only_hold(record, "e")
    [held] = [f for f in record["rules"]["e"]["findings"] if f["code"] == "witness-text-not-read"]
    assert held["id"] == "C4"
    assert held["compared_with"] == [1, 2, 3]


def test_a_hidden_witness_that_did_not_read_holds_under_rule_e_only():
    case = page()
    witness(case, "C").update(units=[], outcome="failed", blank=None)
    hide_witness(case, "C")

    record = account(case)

    only_hold(record, "e")
    assert codes(record, "e") == ["witness-not-read"]


def test_a_feed_witness_other_than_the_sealed_one_is_refused():
    case = page()
    case["witnesses"] = [
        {**w, "units": [{**u, "text": u["text"] + " x"} for u in w["units"]]}
        if w["letter"] == "A"
        else w
        for w in case["witnesses"]
    ]
    with pytest.raises(ContractError, match="shows witness A other than the sealed"):
        account(case)

    case = page()
    case["witnesses"] = [w for w in case["witnesses"] if w["letter"] != "B"]
    with pytest.raises(ContractError, match="shows witness B other than the sealed"):
        account(case)

    case = page()
    witness(case, "C")["units"][2]["id"] = "C5"
    with pytest.raises(ContractError):
        account(case)


# --- rule (d) -------------------------------------------------------------------------


def test_a_surya_line_outside_every_region_holds():
    case = page()
    add_line(case, "L10", bx(100, 1300, 900, 1350))

    record = account(case)

    only_hold(record, "d")
    assert record["rules"]["d"]["findings"] == [
        {
            "code": "unread-line",
            "id": "L10",
            "ref": "surya-line-1300",
            "box_px": bx(100, 1300, 900, 1350),
        }
    ]
    assert {"id": "L10", "ref": "surya-line-1300", "inside": []} in record["lines"]


def test_a_sealed_line_the_feed_did_not_show_is_still_measured():
    case = page()
    add_line(case, None, bx(100, 1300, 900, 1350), shown=False)

    record = account(case)

    only_hold(record, "d")
    assert record["rules"]["d"]["findings"][0]["id"] is None


def test_surya_switched_off_in_the_feed_is_still_measured_from_the_census():
    case = page()
    case["feed"]["surya"] = {"lines": [], "blocks": []}
    for entry in acts(case):
        entry["cites"] = [cite for cite in entry["cites"] if cite[0] in "ABC"]
    for line in case["detections"]["surya"]["lines"]:
        line["id"] = None
    case["detections"]["surya"]["blocks"] = []
    add_line(case, None, bx(100, 1300, 900, 1350), shown=False)

    record = account(case)

    only_hold(record, "d")
    assert len(record["lines"]) == 10


def test_a_page_without_a_surya_census_is_not_measured_and_held():
    case = page()
    case["feed"]["surya"] = {"lines": [], "blocks": []}
    for entry in acts(case):
        entry["cites"] = [cite for cite in entry["cites"] if cite[0] in "ABC"]
    case["detections"]["surya"] = None

    record = account(case)

    assert record["rules"]["d"] == {
        "status": "not-measured",
        "findings": [{"code": "unread-line-not-measured"}],
    }
    assert record["holds"] == ["unread-line-not-measured"]


def test_a_feed_showing_lines_the_census_lacks_is_refused():
    case = page()
    case["detections"]["surya"]["lines"].pop()
    with pytest.raises(ContractError, match="sealed Surya lines do not hold"):
        account(case)
    case = page()
    case["detections"]["surya"]["lines"][0]["box_px"] = bx(0, 0, 5, 5)
    with pytest.raises(ContractError, match="same box"):
        account(case)


def test_a_surya_line_set_aside_is_quiet_under_d_but_its_ink_is_not_read():
    case = page()
    add_line(case, "L10", bx(100, 1300, 900, 1350))
    case["reading"]["answer"]["set_aside"].append({"id": "L10", "reason": "folio number"})
    case["ink"]["runs"] = ink_runs([band(0), band(1), band(2), bx(100, 1300, 900, 1350)])

    record = account(case)

    only_hold(record, "f")


def test_a_line_half_inside_a_region_is_inside():
    case = page()
    # 50 of this line's 100 rows lie in record 3's band (y 900..1200).
    add_line(case, "L10", bx(100, 1150, 900, 1250))

    record = account(case)

    assert record["rules"]["d"]["status"] == "pass"
    assert {"id": "L10", "ref": "surya-line-1150", "inside": [3]} in record["lines"]


# --- rule (e) -------------------------------------------------------------------------


def merged_and_half_read() -> dict:
    """Witness A's second unit merged two records; the one act citing it read only one.

    Every citation, line and pixel of ink is accounted for: only the text of the
    second record, present in the witness and absent from every reading, shows it.
    """
    case = page()
    merged = record_text(1) + " " + record_text(7)
    witness(case, "A")["units"][1]["text"] = merged
    witness(case, "A")["units"][1]["box_px"] = bx(100, 500, 900, 1100)
    # The second record lies below the first, where no detector record was cut.
    case["feed"]["witnesses"] = [w for w in case["feed"]["witnesses"] if w["letter"] != "B"]
    case["detections"].update(records=None, record_census=None, record_detector="absent")
    for answer_act in acts(case):
        answer_act["cites"] = [cite for cite in answer_act["cites"] if not cite.startswith("B")]
    return case


def test_a_merged_unit_read_only_in_half_holds_under_rule_e_and_nothing_else():
    record = account(merged_and_half_read())

    only_hold(record, "e", i="not-applicable")
    [finding] = [
        f for f in record["rules"]["e"]["findings"] if f["code"] == "witness-text-not-read"
    ]
    assert finding["id"] == "A2"
    assert finding["compared_with"] == [2]
    assert "unread-run" in finding["reasons"]
    assert finding["unread_characters"] > POLICY.max_unread_characters
    assert finding["box_px"] == bx(100, 500, 900, 1100)
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

    assert "witness-text-not-read" in codes(record, "e")


def neighbour_read(seed: int, witness_noise: float) -> dict:
    """Act 2 cites record 2's units and lines but transcribes record 3: same formula."""
    case = page(noise=witness_noise, seed=seed)
    acts(case)[1]["text"] = acts(case)[2]["text"]
    return account(case)


def test_a_reading_of_the_neighbouring_record_holds():
    """Every run of record 2 has a substitute opposite it, so no run is unread: only
    its names, absent where the reading aligns them, show the reading is of other ink."""
    record = neighbour_read(0, 0.0)

    assert record["holds"] == ["witness-text-not-read"]
    for identifier in ("A2", "B2", "C2"):
        [finding] = [f for f in record["rules"]["e"]["findings"] if f.get("id") == identifier]
        assert finding["reasons"] == ["distinctive-share"]
        assert finding["unread_characters"] <= POLICY.max_unread_characters


@pytest.mark.parametrize("witness_noise", [0.0, 0.15, 0.3])
def test_neighbouring_record_readings_are_held_on_most_pages(witness_noise):
    """Measured, not assumed: neighbouring entries that share their names (the pool
    here is eight first and eight last names) look alike to identity too."""
    held = sum(
        neighbour_read(seed, witness_noise)["rules"]["e"]["status"] == "hold" for seed in range(20)
    )

    assert held >= 18


def test_two_readings_swapped_between_their_regions_hold():
    case = page()
    first, second = acts(case)[0], acts(case)[1]
    first["text"], second["text"] = second["text"], first["text"]

    record = account(case)

    held = {f["id"] for f in record["rules"]["e"]["findings"] if "reasons" in f}
    assert {"A1", "A2", "B1", "B2", "C1", "C2"} <= held


def test_a_short_burial_folded_into_a_long_act_holds_twice():
    """A burial cited by the next baptism's act but never transcribed.

    No run of it is unread: the long reading's characters stand opposite every
    letter of the short burial. Its names, which two witnesses read and the
    reading does not hold, show it; and the detector's record for it lies in the
    same act region as the baptism's.
    """
    case = page()
    burial_box = bx(100, 1250, 900, 1300)
    witness(case, "A")["units"].append({"id": "A4", "box_px": burial_box, "text": BURIAL})
    witness(case, "B")["units"].append({"id": "B4", "box_px": burial_box, "text": BURIAL})
    case["detections"]["records"].append({"id": "B4", "box_px": burial_box, "ref": "record-3"})
    acts(case)[2]["cites"] += ["A4", "B4"]
    assert len(normalized_text(BURIAL)) < POLICY.max_unread_characters

    record = account(case)

    held = {f["id"]: f["reasons"] for f in record["rules"]["e"]["findings"] if "reasons" in f}
    assert held == {"A4": ["distinctive-share"], "B4": ["distinctive-share"]}
    assert codes(record, "i") == ["merged-detection"]


def test_a_short_burial_whose_act_reads_nothing_holds():
    case = page()
    witness(case, "A")["units"].append(
        {"id": "A4", "box_px": bx(100, 1250, 900, 1300), "text": BURIAL}
    )
    acts(case).append(
        {
            "n": 4,
            "kind": "act",
            "cites": ["A4"],
            "text": "",
            "continues_from_previous_page": False,
            "continues_to_next_page": False,
        }
    )
    case["entry_truncation"][4] = "complete"

    record = account(case)

    [finding] = [
        f for f in record["rules"]["e"]["findings"] if f["code"] == "witness-text-not-read"
    ]
    assert finding["id"] == "A4"
    assert set(finding["reasons"]) == {"unread-share", "no-match", "short-unit-distance"}


def test_an_unreadable_line_marked_in_the_reading_passes():
    """The reader could not read one line of record 2 and wrote `[[?]]` there."""
    case = page()
    text = acts(case)[1]["text"]
    line = "nous prêtre soussigné avons baptisé"
    start = text.index(line)
    unreadable = text[:start] + "[[?]]" + text[text.index(",", start + len(line)) :]
    acts(case)[1]["text"] = unreadable

    record = account(case)

    assert record["rules"]["e"]["status"] == "pass", record["rules"]["e"]["findings"]
    # The same line simply left out is a gap of text, not flagged ink.
    silent = page()
    silent_text = acts(silent)[1]["text"]
    acts(silent)[1]["text"] = (
        silent_text[:start] + silent_text[text.index(",", start + len(line)) :]
    )
    assert unit_measure(account(silent), "A2")["unread_run"] > 0


def test_a_passage_missed_between_read_parts_holds():
    """The reader skipped the godparents and read the signature: the run between holds."""
    case = page()
    first = record_text(0)
    skipped = first[first.index("Le parrain") : first.index("signer.")]
    acts(case)[0]["text"] = first.replace(skipped, "")

    record = account(case)

    assert len(normalized_text(skipped)) > POLICY.max_unread_characters
    assert unit_measure(record, "A1")["unread_run"] > POLICY.max_unread_characters
    assert "witness-text-not-read" in codes(record, "e")


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


def test_a_marginal_name_that_is_read_passes_and_one_that_is_not_holds():
    """Too few distinctive pieces for identity: the name's distance to its readings decides."""
    case = page()
    witness(case, "C")["units"].append({"id": "C4", "box_px": None, "text": "Jean Roy"})
    acts(case)[2]["cites"].append("C4")
    acts(case)[2]["text"] += " Jean Roy"

    record = account(case)

    [recorded] = [f for f in record["rules"]["e"]["findings"] if f.get("id") == "C4"]
    assert recorded["code"] == "too-few-distinctive-pieces"
    assert recorded["distance_bp"] == 0
    assert record["rules"]["e"]["status"] == "pass"

    acts(case)[2]["text"] = acts(case)[2]["text"].removesuffix(" Jean Roy")
    unread = account(case)
    [held] = [f for f in unread["rules"]["e"]["findings"] if f["code"] == "witness-text-not-read"]
    assert held["id"] == "C4"
    assert held["reasons"] == ["short-unit-distance"]
    assert held["distance_bp"] > POLICY.max_short_unit_distance_bp


def test_a_burial_folded_into_a_merged_unit_and_read_by_one_witness_alone_holds():
    """DAI merged baptism 3 and the burial below it into A3; Chandra (C) read the burial
    as its own unit C4. The baptism's act cites both and transcribes only the baptism.

    A3 leaves the burial's 50 letters as one run under the line and a small share, and
    C4 has too few distinctive pieces for identity: DAI's noisy copy of the burial does
    not corroborate them. Only C4's distance from the reading shows it.
    """
    case = page(noise=0.15, seed=3)
    burial = noisy(BURIAL, 0.3, 11)
    witness(case, "A")["units"][2]["text"] += " " + burial
    witness(case, "C")["units"].append({"id": "C4", "box_px": None, "text": BURIAL})
    acts(case)[2]["cites"].append("C4")

    record = account(case)

    assert record["holds"] == ["witness-text-not-read"]
    [held] = [f for f in record["rules"]["e"]["findings"] if f["code"] == "witness-text-not-read"]
    assert held["id"] == "C4"
    assert held["reasons"] == ["short-unit-distance"]
    assert held["distinctive_pieces"] < POLICY.min_pieces
    a3 = unit_measure(record, "A3")
    assert a3["unread_run"] <= POLICY.max_unread_characters
    assert a3["unread_share_bp"] <= POLICY.max_unread_share_bp


def short_units_read_correctly(seed: int, witness_noise: float, reading_noise: float) -> dict:
    """A burial and a marginal name, each read by one witness and by the reader."""
    case = page(noise=witness_noise, reading_noise=reading_noise, seed=seed)
    witness(case, "C")["units"] += [
        {"id": "C4", "box_px": None, "text": noisy(BURIAL, witness_noise, seed + 40)},
        {"id": "C5", "box_px": None, "text": noisy(MARGINAL_NAME, witness_noise, seed + 50)},
    ]
    acts(case)[2]["cites"] += ["C4", "C5"]
    acts(case)[2]["text"] += " " + noisy(BURIAL, reading_noise, seed + 60)
    acts(case)[2]["text"] += " " + noisy(MARGINAL_NAME, reading_noise, seed + 70)
    return account(case)


MARGINAL_NAME = "Marguerite Gauthier"


@pytest.mark.parametrize(("witness_noise", "reading_noise"), [(0.15, 0.0), (0.0, 0.15)])
def test_short_units_read_correctly_at_fifteen_percent_pass(witness_noise, reading_noise):
    """The false-hold side of the short-unit distance, 15% character error on one side.

    The burial is read, so the reading corroborates its names and identity measures
    it; the name mostly has too few distinctive pieces, and its distance decides.
    Over these 30 pages the largest distance was 2,500 (witness side) and 2,777
    (reading side). Alone, over 200 draws, an 18-letter name at 15% reached 4,117
    and a 7-letter one 6,250: the shorter the unit, the thinner the margin.
    """
    distances = []
    for seed in range(30):
        record = short_units_read_correctly(seed, witness_noise, reading_noise)
        assert record["rules"]["e"]["status"] == "pass", record["rules"]["e"]["findings"]
        distances.append(unit_measure(record, "C5")["distance_bp"])

    measured = [value for value in distances if value is not None]
    assert len(measured) >= 20
    assert max(measured) <= POLICY.max_short_unit_distance_bp - 1000


def test_an_entry_of_only_unreadable_marks_passes_rule_e():
    """Deliberate: the reading's own `[[?]]` routes the gaps to review, not rule (e)."""
    case = page()
    witness(case, "C")["units"].append({"id": "C4", "box_px": None, "text": BURIAL})
    acts(case).append(
        {
            "n": 4,
            "kind": "act",
            "cites": ["C4", "L10"],
            "text": "[[?]]",
            "continues_from_previous_page": False,
            "continues_to_next_page": False,
        }
    )
    add_line(case, "L10", bx(100, 1250, 900, 1300))
    case["entry_truncation"][4] = "complete"

    record = account(case)

    assert record["rules"]["e"]["status"] == "pass", record["rules"]["e"]["findings"]
    assert unit_measure(record, "C4")["distance_bp"] == 0


def test_normalization_ignores_case_accents_markup_punctuation_and_doubt_marks():
    assert normalized_text("<p>Né le <b>DEUX</b> mai,&nbsp;l'an</p>") == "neledeuxmailan"
    assert normalized_text("Jean [[Rov|Roy]] [[?]] ptre") == "jeanrovptre"
    assert normalized_text("Marie [UNCERTAIN] Côté") == "mariecote"


def test_normalization_keeps_text_after_a_stray_angle_bracket():
    assert normalized_text("âgé < 30 jours, <i>Jean</i> Roy") == "age30joursjeanroy"
    assert normalized_text("3 < 4 et 5 > 2") == "34et52"
    assert normalized_text("fils < de Roy & de Côté; <br/>ptre") == "filsderoydecoteptre"


def test_an_unreadable_mark_survives_only_in_a_reading():
    assert normalized_text("Jean [[?]] Roy", unreadable=True) == "jeanroy"
    assert normalized_text("Jean  Roy", unreadable=True) == "jeanroy"


def test_best_substring_distance_matches_the_edit_distance_definition():
    rng = random.Random(3)

    def brute(pattern: str, text: str) -> int:
        previous = list(range(len(pattern) + 1))
        best = len(pattern)
        for character in text:
            current = [0]
            for i in range(1, len(pattern) + 1):
                current.append(
                    min(
                        previous[i] + 1,
                        current[i - 1] + 1,
                        previous[i - 1] + (pattern[i - 1] != character),
                    )
                )
            previous = current
            best = min(best, current[-1])
        return best

    for _ in range(300):
        pattern = "".join(rng.choice("abc") for _ in range(rng.randint(0, 12)))
        text = "".join(rng.choice("abc") for _ in range(rng.randint(0, 20)))
        assert best_substring_distance(pattern, text) == brute(pattern, text)


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
        "switches": {"witness_units": "own"},
        "witnesses": [
            {
                "letter": "A",
                "outcome": "read",
                "units": [
                    {
                        "id": f"A{k + 1}",
                        "box_px": bx(0, 60 * k, 1000, 60 * k + 60),
                        "text": noisy(t, 0.15, k),
                    }
                    for k, t in enumerate(texts)
                ],
            },
            {
                "letter": "C",
                "outcome": "read",
                "units": [{"id": "C1", "box_px": None, "text": noisy(" ".join(texts), 0.15, 99)}],
            },
        ],
        "surya": {"lines": [], "blocks": []},
    }
    assert sum(len(t) for t in texts) > 12_000
    started = time.monotonic()
    record = page_accounting(
        feed=feed,
        witnesses=[{**row, "blank": False} for row in feed["witnesses"]],
        detections={
            "surya": {"lines": [], "blocks": []},
            "records": None,
            "record_detector": "absent",
            "record_census": None,
        },
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

    assert record["rules"]["e"]["status"] == "pass", record["rules"]["e"]["findings"]
    assert elapsed < POLICY.deadline_milliseconds / 1000


# --- rule (f) -------------------------------------------------------------------------


def test_ink_outside_every_region_holds():
    case = page()
    case["ink"]["runs"] = ink_runs([band(0), band(1), band(2), bx(100, 1250, 900, 1300)])

    record = account(case)

    only_hold(record, "f")
    [finding] = record["rules"]["f"]["findings"]
    assert finding["code"] == "unread-ink"
    assert finding["outside_ink_pixels"] == 50 * 400


def test_ink_inside_a_set_aside_box_is_not_read():
    case = page()
    case["feed"]["surya"]["blocks"].append({"id": "S4", "box_px": bx(100, 1250, 900, 1300)})
    case["detections"]["surya"]["blocks"].append(
        {"id": "S4", "box_px": bx(100, 1250, 900, 1300), "ref": "surya-block-S4"}
    )
    case["reading"]["answer"]["set_aside"].append({"id": "S4", "reason": "stamp"})
    case["ink"]["runs"] = ink_runs([band(0), band(1), band(2), bx(100, 1250, 900, 1300)])

    record = account(case)

    only_hold(record, "f")


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


def test_an_unknown_truncation_holds_as_reading_incomplete():
    case = page()
    case["entry_truncation"][2] = "unknown"

    record = account(case)

    only_hold(record, "g")
    assert record["rules"]["g"]["findings"] == [
        {"code": "reading-incomplete", "n": 2, "truncation": "unknown"}
    ]


def test_an_unclassified_entry_is_not_measured_and_holds():
    case = page()
    del case["entry_truncation"][3]

    record = account(case)

    assert record["rules"]["g"] == {
        "status": "not-measured",
        "findings": [{"code": "truncation-not-classified", "n": 3}],
    }
    assert "truncation-not-classified" in record["holds"]


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
    assert {"code": "duplicate-region", "ns": [2, 3], "union_box_px": bx(100, 500, 900, 1200)} in (
        record["rules"]["h"]["findings"]
    )


def test_a_line_inside_two_regions_is_recorded_not_held():
    case = page()
    acts(case)[1]["cites"].append("L3")
    # Record 1's band grows to reach line 4: the line now lies in both regions.
    set_block_box(case, "S1", bx(100, 100, 900, 600))
    acts(case)[0]["cites"].append("S1")

    record = account(case)

    assert record["rules"]["h"]["status"] == "pass"
    assert {"code": "shared-line", "id": "L4", "ref": "surya-line-L4", "inside": [1, 2]} in (
        record["rules"]["h"]["findings"]
    )
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
        {
            "code": "merged-detection",
            "n": 1,
            "kind": "act",
            "records": [{"id": "B1", "ref": "record-0"}, {"id": "B2", "ref": "record-1"}],
        }
    ]
    assert record["holds"] == ["merged-detection"]


def test_a_detector_record_set_aside_holds():
    case = page()
    witness(case, "B")["units"].append({"id": "B4", "box_px": bx(100, 1250, 900, 1300), "text": ""})
    case["detections"]["records"].append(
        {"id": "B4", "box_px": bx(100, 1250, 900, 1300), "ref": "record-3"}
    )
    case["reading"]["answer"]["set_aside"].append({"id": "B4", "reason": "blot"})

    record = account(case)

    assert record["rules"]["i"] == {
        "status": "hold",
        "findings": [
            {
                "code": "set-aside-record",
                "id": "B4",
                "ref": "record-3",
                "box_px": bx(100, 1250, 900, 1300),
            }
        ],
        "records_not_measured": 0,
    }


def test_an_act_labelled_other_holds():
    case = page()
    acts(case)[2]["kind"] = "other"

    record = account(case)

    only_hold(record, "i")
    assert record["rules"]["i"]["findings"] == [
        {
            "code": "record-read-as-other",
            "id": "B3",
            "ref": "record-2",
            "box_px": band(2),
            "inside": [3],
        }
    ]


def test_two_records_read_as_one_other_entry_hold_both_ways():
    case = two_entries_read_as_one()
    acts(case)[0]["kind"] = "other"

    record = account(case)

    assert sorted(codes(record, "i")) == [
        "merged-detection",
        "record-read-as-other",
        "record-read-as-other",
    ]


def test_a_detector_record_in_no_reading_region_holds():
    """Detected, not shown to the model (DAI flat), and inside no region."""
    case = page()
    case["detections"]["records"].append({"box_px": bx(100, 1250, 900, 1300), "ref": "record-3"})

    record = account(case)

    only_hold(record, "i")
    assert record["rules"]["i"]["findings"] == [
        {
            "code": "record-not-read",
            "id": None,
            "ref": "record-3",
            "box_px": bx(100, 1250, 900, 1300),
        }
    ]


def test_no_detector_census_is_not_measured_and_held():
    case = page()
    case["detections"]["records"] = None
    case["detections"]["record_census"] = None

    record = account(case)

    assert record["rules"]["i"] == {
        "status": "not-measured",
        "findings": [{"code": "detector-records-not-measured"}],
        "records_not_measured": None,
    }
    assert record["holds"] == ["detector-records-not-measured"]


def test_a_detector_that_reached_its_cap_is_not_measured_and_held():
    case = page()
    case["detections"]["record_census"] = {
        "detection_count": 300,
        "max_det": 300,
        "max_det_reached": True,
    }

    record = account(case)

    assert record["rules"]["i"] == {
        "status": "not-measured",
        "findings": [{"code": "record-detector-capped"}],
        "records_not_measured": 0,
    }
    assert record["holds"] == ["record-detector-capped"]


def test_a_roster_without_a_record_detector_is_not_applicable():
    case = page()
    case["detections"].update(record_detector="absent", records=None, record_census=None)

    record = account(case)

    assert record["rules"]["i"] == {
        "status": "not-applicable",
        "findings": [{"code": "no-record-detector"}],
        "records_not_measured": 0,
    }
    assert record["holds"] == []


def test_detections_that_contradict_the_feed_or_themselves_are_refused():
    case = page()
    case["detections"]["records"][0]["box_px"] = band(1)
    with pytest.raises(ContractError, match="same box"):
        account(case)
    case = page()
    case["detections"]["record_detector"] = "absent"
    with pytest.raises(ContractError, match="stated absent"):
        account(case)
    case = page()
    case["detections"]["record_census"] = None
    with pytest.raises(ContractError, match="together or neither"):
        account(case)
    case = page()
    case["detections"]["record_census"]["max_det_reached"] = 1
    with pytest.raises(ContractError, match="record_census"):
        account(case)
    case = page()
    del case["detections"]["record_census"]
    with pytest.raises(ContractError, match="detections are not"):
        account(case)


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
        rng.shuffle(shuffled["witnesses"])
        for witness_row in shuffled["feed"]["witnesses"]:
            rng.shuffle(witness_row["units"])
        rng.shuffle(shuffled["feed"]["surya"]["lines"])
        rng.shuffle(shuffled["feed"]["surya"]["blocks"])
        rng.shuffle(shuffled["detections"]["surya"]["lines"])
        if shuffled["detections"]["records"] is not None:
            rng.shuffle(shuffled["detections"]["records"])
        rng.shuffle(shuffled["reading"]["answer"]["set_aside"])
        for act in acts(shuffled):
            rng.shuffle(act["cites"])

        assert account(shuffled) == account(case)


# --- the shared answer reading ------------------------------------------------------------


CANDIDATES = {
    **{f"A{i}": bx(100 * i, 0, 100 * i + 10, 10) for i in range(1, 4)},
    **{f"L{i}": bx(0, 10 * i, 10, 10 * i + 10) for i in range(1, 21)},
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


def grammar_codes(answer) -> list[str]:
    return sorted(
        problem["grammar"]
        for problem in validate_answer(answer, CANDIDATES)["problems"]
        if problem["code"] == "answer-grammar"
    )


def test_an_answer_outside_the_one_grammar_is_named_by_it():
    """The grammar is `common.page_answer`'s, label rule included: no second reading."""
    assert grammar_codes([]) == ["not-object"]
    assert grammar_codes({"acts": [], "set_aside": [], "extra": 1}) == ["top-fields"]
    assert grammar_codes({"acts": [_entry(1, ["A1"]), _entry(3, ["A2"])], "set_aside": []}) == [
        "n-not-contiguous"
    ]
    assert grammar_codes(
        {
            "acts": [
                _entry(1, ["A1"]),
                _entry(2, ["L1"], continues_from_previous_page=True),
                _entry(3, ["L5"], continues_to_next_page=True),
            ],
            "set_aside": [],
        }
    ) == ["continuation-not-at-edge"]
    assert grammar_codes({"acts": [_entry(1, ["A1"], extra=1)], "set_aside": []}) == [
        "act-field-unknown"
    ]
    assert grammar_codes({"acts": [_entry(1, ["A1"], label="x" * 81)], "set_aside": []}) == [
        "label-invalid"
    ]
    # A blank label is outside the grammar here exactly as it is for the page answer.
    assert grammar_codes({"acts": [_entry(1, ["A1"], label="  ")], "set_aside": []}) == [
        "label-invalid"
    ]
    validated = validate_answer({"acts": [_entry(1, ["A1"], extra=1)], "set_aside": []}, CANDIDATES)
    assert validated["entries"] == [] and validated["set_aside"] == {}


def test_validation_names_every_problem():
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
    assert problem_codes({"acts": [_entry(1, ["A1"]), _entry(2, ["A1"])], "set_aside": []}) == [
        "duplicate-region"
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
        bx(100, 0, 110, 10),
        None,
        bx(0, 50, 10, 60),
    ]


def test_the_union_box_bounds_every_cited_box_unpadded():
    validated = validate_answer({"acts": [_entry(1, ["L2-L4", "C1"])], "set_aside": []}, CANDIDATES)

    assert validated["entries"][0]["cited_ids"] == ["L2", "L3", "L4", "C1"]
    assert validated["entries"][0]["union_box_px"] == bx(0, 20, 10, 50)


def test_a_detector_record_with_no_box_is_reported_not_measured_never_dropped():
    case = page()
    case["detections"]["records"].append({"box_px": None, "ref": "record-collapsed"})

    record = account(case)

    rule = record["rules"]["i"]
    assert rule["status"] == "not-measured" and rule["records_not_measured"] == 1
    assert rule["findings"] == [
        {"code": "detector-record-not-measured", "id": None, "ref": "record-collapsed"}
    ]
    assert record["holds"] == ["detector-record-not-measured"]
    assert len(record["records"]) == 3


def test_a_flat_witness_places_nothing_so_the_accounting_reads_the_stage_s_regions():
    """Under `witness_units = "flat"` the regions measured are the ones the stage cuts."""
    case = page()
    case["feed"]["switches"]["witness_units"] = "flat"
    candidates = feed_candidates(case["feed"])
    assert candidates["A1"] is None and candidates["B1"] is None
    assert candidates["L1"] == line_box(0, 0)
    for entry in acts(case):
        entry["cites"] = [cite for cite in entry["cites"] if cite[0] in "ABC"]

    record = account(case)

    # No entry cites a placing id, so every one is unplaced and nothing is inside.
    assert codes(record, "b") == ["reading-unplaced"] * 3
    assert all(row["inside"] == [] for row in record["lines"])
    # The detector records still carry the sealed boxes of the units they are.
    assert record["records"][0]["box_px"] == band(0)


def test_a_feed_with_a_repeated_malformed_or_skipped_id_is_refused():
    feed = page()["feed"]
    feed["surya"]["lines"].append({"id": "L1", "box_px": bx(0, 0, 1, 1)})
    with pytest.raises(ContractError, match="twice"):
        feed_candidates(feed)
    feed = page()["feed"]
    feed["surya"]["lines"][0]["box_px"] = bx(5, 5, 5, 9)
    with pytest.raises(ContractError, match="box_px"):
        feed_candidates(feed)
    feed = page()["feed"]
    feed["surya"]["lines"][4]["id"] = "L12"
    with pytest.raises(ContractError, match="without a gap"):
        feed_candidates(feed)


# --- the sealed policy ------------------------------------------------------------------


def test_the_policy_is_closed(tmp_path: Path):
    text = DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH.read_text(encoding="utf-8")
    extra = tmp_path / "extra.toml"
    extra.write_text(text + "\n[other]\nx = 1\n", encoding="utf-8")
    with pytest.raises(ContractError, match="closed schema"):
        load_page_accounting_policy(extra)
    missing = tmp_path / "missing.toml"
    missing.write_text(text.replace("anchor_neighbours = 3", ""), encoding="utf-8")
    with pytest.raises(ContractError, match="closed schema"):
        load_page_accounting_policy(missing)
    zero = tmp_path / "zero.toml"
    zero.write_text(text.replace("min_area_bp = 5000", "min_area_bp = 0"), encoding="utf-8")
    with pytest.raises(ContractError, match="min_area_bp"):
        load_page_accounting_policy(zero)
    share = tmp_path / "share.toml"
    share.write_text(
        text.replace("min_distinctive_share_bp = 3500", "min_distinctive_share_bp = 10001"),
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="1..10000"):
        load_page_accounting_policy(share)
    boolean = tmp_path / "bool.toml"
    boolean.write_text(
        text.replace("max_unread_characters = 60", "max_unread_characters = true"),
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="integer"):
        load_page_accounting_policy(boolean)


class _Context:
    def __init__(self, sealed: dict[str, str]):
        self.sealed = sealed

    def require_sealed_config(self, name: str, observed_sha256: str) -> None:
        if self.sealed.get(name) != observed_sha256:
            raise ContractError(f"the {name} configuration changed")


def test_the_stage_reads_the_policy_only_under_the_run_seal(tmp_path: Path):
    policy = require_page_accounting_policy(_Context({"page-accounting": POLICY.sha256}))
    assert policy == POLICY

    edited = tmp_path / "edited.toml"
    edited.write_text(
        DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH.read_text(encoding="utf-8").replace(
            "min_pieces = 5", "min_pieces = 6"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="page-accounting configuration changed"):
        require_page_accounting_policy(_Context({"page-accounting": POLICY.sha256}), edited)
