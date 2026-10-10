"""The page accounting on synthetic pages: one pass and one hold per rule."""

from __future__ import annotations

import copy
import dataclasses
import random
from pathlib import Path

import pytest

from common import page_accounting as page_accounting_module
from common import page_path, truncation
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
    region_area,
    require_page_accounting_policy,
    validate_answer,
)
from common.residual_ink import INK_RUNS_SCHEMA

# The committed policy, and the same with no review flag: the rule tests below
# are about what each rule measures, so they run with every finding holding.
SEALED = load_page_accounting_policy()
POLICY = dataclasses.replace(SEALED, flag_codes=frozenset())
WIDTH, HEIGHT = 1000, 1400
RULE_NAMES = "abcdefghi"
# A first reading's accounting: every rule passes, and rule (j), the re-ask's, does not apply.
CLEAN = {**{name: "pass" for name in RULE_NAMES}, "j": "not-applicable"}

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
        "page_size": {"w": WIDTH, "h": HEIGHT},
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
                "cites": [
                    f"A{k + 1}",
                    f"B{k + 1}",
                    f"C{k + 1}",
                    *(f"L{3 * k + row + 1}" for row in range(3)),
                ],
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
    expected = dict(CLEAN)
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


def unit_measure(record: dict, identifier: str) -> dict:
    [found] = [m for m in record["rules"]["e"]["measurements"] if m["id"] == identifier]
    return found


# --- the clean page ------------------------------------------------------------------


def test_a_clean_page_passes_every_rule():
    record = account(page())

    assert statuses(record) == CLEAN
    assert record["holds"] == []
    assert record["schema"] == "page-accounting.v3"
    assert record["flags"] == []
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

    assert statuses(record) == CLEAN
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
    for line in ("L7", "L8", "L9"):
        case["reading"]["answer"]["set_aside"].append({"id": line, "reason": "not an act"})
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


def test_a_witness_that_looked_and_found_the_page_empty_is_recorded_not_held():
    # `genuinely-empty` is a reading, as DAI's page on which its own detector
    # found nothing below its cap is: never `witness-not-read`.
    case = page()
    witness(case, "C").update(units=[], outcome="genuinely-empty", blank=True)
    for entry in acts(case):
        entry["cites"] = [cite for cite in entry["cites"] if not cite.startswith("C")]

    record = account(case)

    expected = {"code": "witness-read-blank", "letter": "C", "outcome": "genuinely-empty"}
    assert record["rules"]["c"] == {"status": "pass", "findings": [expected]}
    assert expected in record["rules"]["e"]["findings"]
    assert record["holds"] == []


def hide_witness(case: dict, letter: str) -> None:
    """The feed's `witnesses` switch hides one sealed witness from the model."""
    case["feed"]["witnesses"] = [w for w in case["feed"]["witnesses"] if w["letter"] != letter]
    for entry in acts(case):
        entry["cites"] = [cite for cite in entry["cites"] if not cite.startswith(letter)]


def test_a_witness_hidden_by_the_feed_is_still_measured_against_every_reading():
    case = page()
    hide_witness(case, "C")

    record = account(case)

    assert statuses(record) == CLEAN
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

    Every citation, line and pixel of ink is accounted for. The text of the
    second record, present in the witness and absent from every reading, shows
    it; so does the merged box, which lays act 2's region over all of act 3's.
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


def test_a_merged_unit_read_only_in_half_holds_under_rules_e_and_h_only():
    record = account(merged_and_half_read())

    only_hold(record, "e", h="hold", i="not-applicable")
    assert codes(record, "h").count("duplicate-region") == 1
    [finding] = [
        f for f in record["rules"]["e"]["findings"] if f["code"] == "witness-text-not-read"
    ]
    assert finding["id"] == "A2"
    assert finding["compared_with"] == [2]
    assert "unread-run" in finding["reasons"]
    assert finding["unread_characters"] > POLICY.max_unread_characters
    assert finding["box_px"] == bx(100, 500, 900, 1100)
    assert record["holds"] == ["duplicate-region", "witness-text-not-read"]


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


def with_steps(steps: int) -> PageAccountingPolicy:
    return PageAccountingPolicy(**{**POLICY.__dict__, "max_alignment_steps": steps})


def steps_spent(case: dict, monkeypatch) -> int:
    """The alignment steps rule (e) spends on `case` under the sealed policy."""
    spent = []
    spend = page_accounting_module._WorkBudget.spend

    def counted(budget, steps):
        spent.append(steps)
        spend(budget, steps)

    with monkeypatch.context() as patch:
        patch.setattr(page_accounting_module._WorkBudget, "spend", counted)
        account(case)
    return sum(spent)


def test_a_work_bound_hit_is_not_measured_and_held():
    record = account(page(), with_steps(1))

    assert record["rules"]["e"]["status"] == "not-measured"
    assert set(codes(record, "e")) == {"witness-text-not-measured"}
    assert {f["reason"] for f in record["rules"]["e"]["findings"]} == {"work-bound"}
    assert "witness-text-not-measured" in record["holds"]


def test_a_budget_that_runs_out_midway_holds_the_same_units_every_time(monkeypatch):
    case = page(noise=0.15, seed=3)
    policy = with_steps(steps_spent(case, monkeypatch) // 2)

    first, second = account(case, policy), account(copy.deepcopy(case), policy)

    assert first == second
    assert first["rules"]["e"]["status"] == "not-measured"
    unmeasured = [f["id"] for f in first["rules"]["e"]["findings"]]
    assert {f["reason"] for f in first["rules"]["e"]["findings"]} == {"work-bound"}
    assert unmeasured and first["rules"]["e"]["measurements"]
    assert "witness-text-not-measured" in first["holds"]


def test_the_sealed_budget_measures_a_page_within_it_and_one_step_short_does_not(monkeypatch):
    case = page(noise=0.15, seed=3)
    needed = steps_spent(case, monkeypatch)

    assert needed < POLICY.max_alignment_steps
    assert account(case, with_steps(needed))["rules"]["e"]["status"] != "not-measured"
    assert account(case, with_steps(needed - 1))["rules"]["e"]["status"] == "not-measured"


def test_an_unanchored_unit_runs_out_of_budget_rather_than_running_on():
    """Two-letter noise leaves no anchor and a match at every turn: pure work."""
    rng = random.Random(7)
    case = page(1)
    witness(case, "A")["units"][0]["text"] = "".join(rng.choice("ab") for _ in range(2000))
    acts(case)[0]["text"] = "".join(rng.choice("ab") for _ in range(2000))

    record = account(case, with_steps(1_000_000))

    assert {"id": "A1", "code": "witness-text-not-measured", "reason": "work-bound"} in record[
        "rules"
    ]["e"]["findings"]


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
    # A cited unit of seven letters read differently is a short-unit dissent, with its own code.
    [held] = [
        f for f in unread["rules"]["e"]["findings"] if f["code"] == "witness-short-unit-not-read"
    ]
    assert held["id"] == "C4"
    assert held["reasons"] == ["short-unit-distance"]
    assert held["distance_bp"] > POLICY.max_short_unit_distance_bp
    assert unread["holds"] == ["witness-short-unit-not-read"]


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


def test_a_dense_page_is_measured_within_the_sealed_budget():
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
        "page_size": {"w": 1000, "h": 1200},
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

    assert record["rules"]["e"]["status"] == "pass", record["rules"]["e"]["findings"]


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

    region = 800 * 300 * 2  # bands 1 and 2; lines 4..6 lie inside band 1
    assert record["rules"]["h"]["status"] == "hold"
    assert {
        "code": "duplicate-region",
        "ns": [2, 3],
        "shared_px": region,
        "smaller_region_px": region,
    } in record["rules"]["h"]["findings"]


def test_a_line_inside_two_regions_is_recorded_not_held():
    case = page()
    acts(case)[1]["cites"].append("L3")

    record = account(case)

    assert record["rules"]["h"]["status"] == "pass"
    assert {"code": "shared-line", "id": "L3", "ref": "surya-line-L3", "inside": [1, 2]} in (
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


def _detector_found_nothing(case: dict) -> None:
    """The record detector looked below its cap and found no record; DAI testified blank."""
    case["detections"]["records"] = []
    case["detections"]["record_census"] = {
        "detection_count": 0,
        "max_det": 300,
        "max_det_reached": False,
    }
    witness(case, "B").update(units=[], outcome="genuinely-empty", blank=True)
    for entry in case["reading"]["answer"]["acts"]:
        entry["cites"] = [cite for cite in entry["cites"] if not cite.startswith("B")]


def test_a_detector_that_found_nothing_on_a_page_of_acts_holds():
    """The detector's silence contradicts a reading that establishes acts, so the page holds."""
    case = page()
    _detector_found_nothing(case)

    record = account(case)

    assert record["rules"]["i"] == {
        "status": "hold",
        "findings": [{"code": "no-detector-record-on-act-page", "acts": [1, 2, 3]}],
        "records_not_measured": 0,
    }
    assert "no-detector-record-on-act-page" in record["holds"]


def test_records_with_no_box_are_not_measured_rather_than_absent():
    """A detector that found records it could not box did not find an empty page."""
    case = page()
    case["detections"]["records"] = [
        {"box_px": None, "ref": "record-0"},
        {"box_px": None, "ref": "record-1"},
    ]

    record = account(case)

    assert record["rules"]["i"] == {
        "status": "not-measured",
        "findings": [
            {"code": "detector-record-not-measured", "id": None, "ref": "record-0"},
            {"code": "detector-record-not-measured", "id": None, "ref": "record-1"},
        ],
        "records_not_measured": 2,
    }
    assert "no-detector-record-on-act-page" not in record["holds"]


def test_a_detector_that_found_nothing_on_a_page_of_other_entries_passes_rule_i():
    case = page()
    _detector_found_nothing(case)
    for entry in acts(case):
        entry["kind"] = "other"

    record = account(case)

    assert record["rules"]["i"] == {"status": "pass", "findings": [], "records_not_measured": 0}


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
    assert expand_cites(["A1-A3", "L2"], CANDIDATES) == (["A1", "A2", "A3", "L2"], [])
    assert expand_cites(["A2-A2"], CANDIDATES) == (["A2"], [])


@pytest.mark.parametrize("cite", ["L10-L13", "L4-L4", "S1-S3", "L19-L25"])
def test_a_range_over_detections_is_a_problem_and_names_no_id(cite):
    """Lines and blocks are numbered by the detector, not by column: cited one by one."""
    ids, problems = expand_cites([cite, "A1"], CANDIDATES)

    assert ids == ["A1"]
    assert problems == [{"code": "detection-range", "cite": cite}]


@pytest.mark.parametrize("cite", ["L13-L10", "L1-A3", "L1-", "L01", "l1", "L1 - L3", 7])
def test_a_malformed_citation_is_refused_not_guessed(cite):
    ids, problems = expand_cites([cite], CANDIDATES)

    assert ids == []
    assert [problem["code"] for problem in problems] == ["malformed-range"]


def test_a_range_past_the_last_id_is_unknown():
    ids, problems = expand_cites(["A2-A5"], CANDIDATES)

    assert ids == []
    assert problems == [{"code": "unknown-id", "id": "A5"}]


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
    assert (
        grammar_codes(
            {
                "acts": [
                    _entry(1, ["A1"]),
                    _entry(2, ["L1"], continues_from_previous_page=True),
                    _entry(3, ["L5"], continues_to_next_page=True),
                ],
                "set_aside": [],
            }
        )
        == []
    )
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
            "set_aside": [{"id": "A2-A3", "reason": "r"}, {"id": "A2", "reason": "r"}],
        }
    ) == ["set-aside-twice"]
    assert problem_codes(
        {"acts": [_entry(1, ["A1"])], "set_aside": [{"id": "L1", "reason": ""}]}
    ) == ["set-aside-without-reason"]
    # Two entries on one region are the accounting's rule (h), not an answer problem.
    assert problem_codes({"acts": [_entry(1, ["A1"]), _entry(2, ["A1"])], "set_aside": []}) == []


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
    answer = {"acts": [_entry(1, ["L4", "L2", "L3", "C1", "L2"])], "set_aside": []}
    validated = validate_answer(answer, CANDIDATES)

    [entry] = validated["entries"]
    assert entry["cited_ids"] == ["L4", "L2", "L3", "C1"]
    # The region is each placing box once, in first-cited order; the union only crops.
    assert entry["region_boxes_px"] == [bx(0, 40, 10, 50), bx(0, 20, 10, 30), bx(0, 30, 10, 40)]
    assert entry["union_box_px"] == bx(0, 20, 10, 50)


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
    candidates = feed_candidates(case["feed"], POLICY)
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
        feed_candidates(feed, POLICY)
    feed = page()["feed"]
    feed["surya"]["lines"][0]["box_px"] = bx(5, 5, 5, 9)
    with pytest.raises(ContractError, match="box_px"):
        feed_candidates(feed, POLICY)
    feed = page()["feed"]
    feed["surya"]["lines"][4]["id"] = "L12"
    with pytest.raises(ContractError, match="without a gap"):
        feed_candidates(feed, POLICY)


@pytest.mark.parametrize("text", ["", "SYNTHETIC ACT ONE alpha beta"])
@pytest.mark.parametrize("units", ["own", "flat"])
def test_a_malformed_unit_box_is_refused_whether_the_unit_places_or_not(text, units):
    case = page()
    case["feed"]["switches"]["witness_units"] = units
    witness(case, "A")["units"][0].update(text=text, box_px=bx(5, 5, 5, 9))
    with pytest.raises(ContractError, match="A1 box_px"):
        feed_candidates(case["feed"], POLICY)


# --- two-column pages: an entry's region is the ink it names, id by id -------------------
#
# Surya numbers lines roughly in raster order, so on a two-column page its ids
# interleave the columns: here the odd lines are column 1 and the even lines
# column 2, row by row. Its blocks interleave the same way.

COLUMN_X = ((100, 450), (550, 900))


def column_line(line: int) -> dict[str, int]:
    """Line `L<line>`: odd lines down column 1, even lines down column 2, 100 px tall."""
    x0, x1 = COLUMN_X[(line - 1) % 2]
    top = 100 + 150 * ((line - 1) // 2)
    return bx(x0, top, x1, top + 100)


def column_block(block: int) -> dict[str, int]:
    """Block `S<block>`: two rows of one column, alternating columns as the lines do."""
    x0, x1 = COLUMN_X[(block - 1) % 2]
    top = 100 + 300 * ((block - 1) // 2)
    return bx(x0, top, x1, top + 250)


def column_ink(boxes: list[dict[str, int]]) -> dict:
    """Ink-run evidence with ink on every row of every box, edge to edge."""
    rows: list[list[list[int]]] = [[] for _ in range(HEIGHT)]
    for box in boxes:
        for y in range(box["y"], box["y"] + box["h"]):
            rows[y].append([box["x"], box["w"]])
    for row in rows:
        row.sort()
    return {"schema": INK_RUNS_SCHEMA, "width": WIDTH, "height": HEIGHT, "rows": rows}


def two_columns(
    cites: list[list[str]],
    *,
    lines: int = 8,
    blocks: list[dict[str, int]] | None = None,
    set_aside: list[str] = (),
) -> dict:
    """A two-column page with Surya's lines and blocks only, and an entry per `cites` row.

    No witness read the page, so no witness text covers any line: only the
    geometry of rules (d), (f) and (h) can see a line left unread. The record
    detector is absent, so rule (i) does not apply.
    """
    line_boxes = [column_line(k) for k in range(1, lines + 1)]
    block_boxes = [column_block(k) for k in range(1, 5)] if blocks is None else blocks
    feed = {
        "page_id": "page-1",
        "page_ordinal": 1,
        "page_size": {"w": WIDTH, "h": HEIGHT},
        "switches": {"witness_units": "own"},
        "witnesses": [],
        "surya": {
            "lines": [{"id": f"L{k}", "box_px": box} for k, box in enumerate(line_boxes, 1)],
            "blocks": [{"id": f"S{k}", "box_px": box} for k, box in enumerate(block_boxes, 1)],
        },
    }
    answer = {
        "acts": [
            {
                "n": n,
                "kind": "act",
                "cites": list(cited),
                "text": "Le deux mai a été baptisé Jean Roy",
                "continues_from_previous_page": False,
                "continues_to_next_page": False,
            }
            for n, cited in enumerate(cites, 1)
        ],
        "set_aside": [{"id": identifier, "reason": "not an act"} for identifier in set_aside],
    }
    return {
        "feed": feed,
        "witnesses": feed["witnesses"],
        "detections": {
            "surya": {
                kind: [{**item, "ref": f"surya-{item['id']}"} for item in feed["surya"][kind]]
                for kind in ("lines", "blocks")
            },
            "records": None,
            "record_detector": "absent",
            "record_census": None,
        },
        "reading": {"parse_state": "parsed", "finish_reason": "stop", "answer": answer},
        "entry_truncation": {n: "complete" for n in range(1, len(cites) + 1)},
        "ink": {"runs": column_ink(line_boxes), "coverage_policy": COVERAGE_POLICY},
    }


COLUMN_ONE = ["L1", "L3", "L5", "L7"]
COLUMN_TWO = ["L2", "L4", "L6", "L8"]


def unread_lines(record: dict) -> list[str]:
    """The ids rule (d) found unread, odd (column 1) then even (column 2), each in order."""
    found = [f["id"] for f in record["rules"]["d"]["findings"] if f["code"] == "unread-line"]
    return sorted(found, key=lambda line: (1 - int(line[1:]) % 2, int(line[1:])))


def problem_codes_of(record: dict) -> list[str]:
    return [finding["problem"]["code"] for finding in record["rules"]["a"]["findings"]]


def test_a_line_range_on_two_columns_holds_the_reading_whole():
    case = two_columns([["L1-L7"]])

    record = account(case)

    assert problem_codes_of(record) == ["detection-range"]
    assert record["rules"]["a"]["findings"][0]["problem"] == {
        "code": "detection-range",
        "cite": "L1-L7",
        "n": 1,
    }
    assert "page-answer-incomplete" in record["holds"]


def test_one_column_cited_line_by_line_leaves_the_other_column_unread():
    record = account(two_columns([COLUMN_ONE]))

    assert unread_lines(record) == COLUMN_TWO
    assert codes(record, "f") == ["unread-ink"]
    assert record["rules"]["a"]["status"] == "pass"


def test_both_columns_cited_line_by_line_read_the_page():
    record = account(two_columns([COLUMN_ONE, COLUMN_TWO]))

    assert {name: record["rules"][name]["status"] for name in "dfh"} == {
        "d": "pass",
        "f": "pass",
        "h": "pass",
    }
    assert codes(record, "h") == []
    assert {row["id"]: row["inside"] for row in record["lines"]} == {
        **{line: [1] for line in COLUMN_ONE},
        **{line: [2] for line in COLUMN_TWO},
    }


def test_an_act_wrapping_columns_claims_only_the_lines_it_names():
    case = two_columns([["L5", "L7", "L2"], ["L1", "L3"]])

    record = account(case)

    assert unread_lines(record) == ["L4", "L6", "L8"]
    entry = validate_answer(case["reading"]["answer"], feed_candidates(case["feed"], POLICY))[
        "entries"
    ][0]
    assert entry["region_boxes_px"] == [column_line(5), column_line(7), column_line(2)]
    assert region_area(entry["region_boxes_px"]) == 3 * 350 * 100
    assert entry["union_box_px"] == bx(100, 100, 900, 650)


def test_an_act_wrapping_columns_is_classified_over_its_lines_area_only():
    """The truncation length signal's denominator is the lines named, not their rectangle."""
    case = two_columns([["L5", "L7", "L2"], ["L1", "L3"]])
    feed = {**case["feed"], "page_size": {"w": WIDTH, "h": HEIGHT}, "page_render": None}

    plans = page_path.entry_plans(
        case["reading"]["answer"],
        feed,
        page_id="pg_0123456789abcdef",
        stop_reason="stop",
        truncation_policy={
            truncation.LENGTH_FLOOR_FIELD: 1,
            truncation.LEGIBLE_PAGE_FIELD: 1,
        },
        accounting_policy=POLICY,
    )

    assert plans[0]["region_boxes_px"] == [column_line(5), column_line(7), column_line(2)]
    assert plans[0]["truncation"]["measure"]["region_pixels"] == 3 * 350 * 100
    assert plans[0]["union_box_px"] == bx(100, 100, 900, 650)


def _doubt_plans(*texts: str) -> list[dict]:
    """The entry plans of a two-column page read as `texts`, one line each."""
    case = two_columns([[f"L{n}"] for n in range(1, len(texts) + 1)])
    answer = copy.deepcopy(case["reading"]["answer"])
    for act, text in zip(answer["acts"], texts, strict=True):
        act["text"] = text
    return page_path.entry_plans(
        answer,
        {**case["feed"], "page_size": {"w": WIDTH, "h": HEIGHT}, "page_render": None},
        page_id="pg_0123456789abcdef",
        stop_reason="stop",
        truncation_policy={truncation.LENGTH_FLOOR_FIELD: 1, truncation.LEGIBLE_PAGE_FIELD: 1},
        accounting_policy=POLICY,
    )


def _doubt_codes(plans: list[dict]) -> list[list[str]]:
    doubt = {page_path.DOUBT_SHARE_HIGH, page_path.PAGE_DOUBT_SHARE_HIGH}
    return [sorted(doubt & set(plan["reading_holds"])) for plan in plans]


def _doubt_holds(*texts: str) -> list[list[str]]:
    """Each entry's doubt holds when the page publishes exactly these entries."""
    plans = _doubt_plans(*texts)
    page_path.hold_doubtful_page(plans, POLICY)
    return _doubt_codes(plans)


def test_an_entry_or_a_page_mostly_doubtful_or_unread_is_held():
    """More than the sealed share doubtful or unread holds; exactly the share does not.

    "Jean Roy fils" with "Roy" doubtful is 3 of 11 characters, over 20%; "Jean"
    and one gap is 1 of 5, exactly 20%. Two entries mostly doubtful take the page
    over 30%, and then every entry on it holds.
    """
    assert (POLICY.max_act_doubt_share_bp, POLICY.max_page_doubt_share_bp) == (2000, 3000)
    assert _doubt_holds("Jean [[Roy]] fils", "Jean [[?]]", "Le deux mai a été baptisé") == [
        ["doubt-share-high"],
        [],
        [],
    ]
    assert _doubt_holds("[[Jean Roy]]", "Le deux [[mai]]", "baptisé") == [
        ["doubt-share-high", "page-doubt-share-high"],
        ["doubt-share-high", "page-doubt-share-high"],
        ["page-doubt-share-high"],
    ]


def test_the_page_share_is_over_every_entry_the_page_publishes_re_ask_included():
    """A first reading at 19% stays clear alone; its re-ask's 80% entry takes the page to 42%.

    The page hold is decided once over the entries the page publishes, so a
    first-reading entry cannot escape it because the doubt arrived on re-ask.
    """
    clean, half, mostly = (
        "a" * 100,
        "[[" + "b" * 30 + "]]" + "c" * 30,
        "[[" + "d" * 80 + "]]" + "e" * 20,
    )
    plans = _doubt_plans(clean, half, mostly)
    assert _doubt_codes(plans) == [[], ["doubt-share-high"], ["doubt-share-high"]]
    first = copy.deepcopy(plans[:2])
    page_path.hold_doubtful_page(first, POLICY)
    assert _doubt_codes(first) == [[], ["doubt-share-high"]]
    page_path.hold_doubtful_page(plans, POLICY)
    assert all("page-doubt-share-high" in codes for codes in _doubt_codes(plans))


def test_unanchorable_or_wholly_unread_entries_count_as_unread_on_the_page():
    """Marks that do not parse, or an entry that is only `[[?]]`, are no certainty.

    "Jean [[Roy" is 9 characters whose doubt cannot be anchored: all unread, so with
    "Le deux mai" the page is 9 of 18. Four `[[?]]` entries are one unread
    character each, 4 of 11 beside "Jean Roy".
    """
    assert _doubt_holds("Jean [[Roy", "Le deux mai")[1] == ["page-doubt-share-high"]
    assert _doubt_holds("[[?]]", "[[?]]", "[[?]]", "[[?]]", "Jean Roy")[4] == [
        "page-doubt-share-high"
    ]
    # A page of nothing but unread entries is wholly unread, and no count is ever empty.
    assert _doubt_holds("[[?]]", " ") == [["page-doubt-share-high"]] * 2
    with pytest.raises(ContractError, match="no characters"):
        page_path.annotations.doubt_exceeds((0, 0), POLICY.max_page_doubt_share_bp)


def test_interleaved_blocks_are_cited_one_by_one_and_lend_no_area():
    # A block range is read (a block places nothing), and lends no area: the
    # entry is unplaced and every line stays unread.
    record = account(two_columns([["S1-S3"]]))
    assert problem_codes_of(record) == []
    assert codes(record, "b") == ["reading-unplaced"]
    assert unread_lines(record) == COLUMN_ONE + COLUMN_TWO

    # Blocks S1 and S3 hold column 1; cited one by one they place nothing, so
    # column 1 is read by its lines and column 2 stays unread.
    record = account(two_columns([["S1", "S3", *COLUMN_ONE]], set_aside=["S2", "S4"]))
    assert record["rules"]["a"]["status"] == "pass"
    assert unread_lines(record) == COLUMN_TWO


def test_a_whole_page_block_alone_places_nothing():
    record = account(two_columns([["S1"]], blocks=[bx(0, 0, WIDTH, HEIGHT)]))

    assert codes(record, "b") == ["reading-unplaced"]
    assert unread_lines(record) == COLUMN_ONE + COLUMN_TWO


def test_a_whole_page_block_shared_by_two_entries_lends_them_nothing():
    """Two acts cite the page's one block beside column 1's lines; column 2 is uncited."""
    case = two_columns([["S1", "L1", "L3"], ["S1", "L5", "L7"]], blocks=[bx(0, 0, WIDTH, HEIGHT)])

    record = account(case)

    assert unread_lines(record) == COLUMN_TWO
    assert codes(record, "f") == ["unread-ink"]
    assert "duplicate-region" not in codes(record, "h")
    assert record["rules"]["a"]["status"] == "pass"


def test_a_blank_page_with_its_block_set_aside_is_read():
    case = two_columns([], lines=0, blocks=[bx(0, 0, WIDTH, HEIGHT)], set_aside=["S1"])

    record = account(case)

    assert record["rules"]["d"]["status"] == "pass"
    assert record["rules"]["f"]["status"] == "pass"
    assert record["holds"] == []


def add_witness(case: dict, units: list[dict]) -> None:
    """Witness A, read, with `units`, shown and sealed."""
    case["feed"]["witnesses"].append(
        {"letter": "A", "outcome": "read", "blank": False, "units": units}
    )


def test_a_textless_witness_unit_with_a_page_sized_box_places_nothing():
    case = two_columns([["A1", *COLUMN_ONE], ["A1"]])
    add_witness(case, [{"id": "A1", "box_px": bx(0, 0, WIDTH, HEIGHT), "text": " [[?]] "}])

    assert feed_candidates(case["feed"], POLICY)["A1"] is None
    record = account(case)

    assert unread_lines(record) == COLUMN_TWO
    assert codes(record, "b") == ["reading-unplaced"]
    assert codes(record, "f") == ["unread-ink"]


def test_a_witness_range_still_expands_and_places():
    case = two_columns([["A1-A3", "L1", "L3", "L5"], ["L7", *COLUMN_TWO]])
    add_witness(
        case,
        [
            {"id": f"A{k}", "box_px": column_line(2 * k - 1), "text": f"ligne {k}"}
            for k in (1, 2, 3)
        ],
    )

    entry = validate_answer(case["reading"]["answer"], feed_candidates(case["feed"], POLICY))[
        "entries"
    ][0]
    assert entry["cited_ids"] == ["A1", "A2", "A3", "L1", "L3", "L5"]
    # A1..A3 lie on L1, L3 and L5, so each box is placed once.
    assert entry["region_boxes_px"] == [column_line(1), column_line(3), column_line(5)]
    record = account(case)
    assert record["rules"]["d"]["status"] == "pass"
    assert record["rules"]["b"]["status"] == "pass"


def test_a_set_aside_line_range_holds_the_reading_whole():
    record = account(two_columns([["L3", "L4", "L5", "L6", "L7", "L8"]], set_aside=["L1-L2"]))

    assert problem_codes_of(record) == ["detection-range"]
    [finding] = record["rules"]["a"]["findings"]
    assert finding["problem"] == {"code": "detection-range", "cite": "L1-L2", "set_aside_index": 0}


def duplicates(record: dict) -> list[list[int]]:
    return [f["ns"] for f in record["rules"]["h"]["findings"] if f["code"] == "duplicate-region"]


def test_an_entry_whose_region_lies_inside_another_s_holds_both():
    every_line = [f"L{k}" for k in range(1, 9)]
    record = account(two_columns([every_line, every_line[:7]]))
    assert duplicates(record) == [[1, 2]]
    assert record["rules"]["h"]["status"] == "hold"

    # The same by a subset: two of column 1's lines inside all four.
    record = account(two_columns([COLUMN_ONE, ["L1", "L3"], COLUMN_TWO]))
    [finding] = [f for f in record["rules"]["h"]["findings"] if f["code"] == "duplicate-region"]
    assert finding == {
        "code": "duplicate-region",
        "ns": [1, 2],
        "shared_px": 2 * 350 * 100,
        "smaller_region_px": 2 * 350 * 100,
    }


def test_one_region_named_by_other_witness_units_holds_both():
    """A1..A2 are column 1's first two lines; B1, one box over both, names the same ink."""
    case = two_columns([["A1-A2", *COLUMN_ONE[2:]], ["B1"], COLUMN_TWO])
    add_witness(
        case,
        [
            {"id": "A1", "box_px": column_line(1), "text": "le deux mai"},
            {"id": "A2", "box_px": column_line(3), "text": "a été baptisé"},
        ],
    )
    case["feed"]["witnesses"].append(
        {
            "letter": "B",
            "outcome": "read",
            "blank": False,
            "units": [
                {"id": "B1", "box_px": bx(100, 100, 450, 350), "text": "le deux mai a été baptisé"}
            ],
        }
    )

    record = account(case)

    assert duplicates(record) == [[1, 2]]


def test_an_act_sharing_one_line_with_a_two_line_neighbour_is_recorded_not_held():
    record = account(two_columns([["L1", "L3", "L5"], ["L5", "L7"], COLUMN_TWO]))

    assert duplicates(record) == []
    assert codes(record, "h") == ["shared-line"]
    assert record["rules"]["h"]["status"] == "pass"


def test_one_block_shared_beside_different_lines_is_not_one_region():
    record = account(two_columns([["S1", *COLUMN_ONE], ["S1", *COLUMN_TWO]]))

    assert duplicates(record) == []
    assert record["rules"]["h"]["status"] == "pass"


def column_unit(text: str) -> list[dict]:
    """Witness A's one unit: `text` on a box as tall as column 1."""
    return [{"id": "A1", "box_px": bx(100, 100, 450, 1100), "text": text}]


def test_a_short_text_on_a_column_sized_box_places_nothing():
    case = two_columns([["A1", "L1"], ["L3", "L5", "L7"], COLUMN_TWO])
    add_witness(case, column_unit("12"))

    assert feed_candidates(case["feed"], POLICY)["A1"] is None
    record = account(case)
    entry = validate_answer(case["reading"]["answer"], feed_candidates(case["feed"], POLICY))
    assert entry["entries"][0]["region_boxes_px"] == [column_line(1)]
    assert duplicates(record) == []


def test_a_unit_places_its_box_from_the_sealed_characters_per_area():
    """Column 1's box is 2,500 basis points of the page: at 100 a character, 25 characters."""
    assert POLICY.max_unit_area_per_character_bp == 100
    assert 350 * 1000 * 10_000 == 2_500 * WIDTH * HEIGHT
    box = column_unit("")[0]["box_px"]
    for characters, places in ((24, False), (25, True)):
        case = two_columns([["A1"]])
        add_witness(case, column_unit("x" * characters))
        assert (feed_candidates(case["feed"], POLICY)["A1"] == box) is places


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
    policy = require_page_accounting_policy(_Context({"page-accounting": SEALED.sha256}))
    assert policy == SEALED

    edited = tmp_path / "edited.toml"
    edited.write_text(
        DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH.read_text(encoding="utf-8").replace(
            "min_pieces = 5", "min_pieces = 6"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="page-accounting configuration changed"):
        require_page_accounting_policy(_Context({"page-accounting": POLICY.sha256}), edited)


def test_a_line_range_inside_the_entrys_own_units_is_read():
    """The lines of record 1 lie inside its units A1 and B1, so citing them as a range
    names no ink the entry's units do not already claim."""
    case = page()
    acts(case)[0]["cites"] = ["A1", "B1", "C1", "L1-L3"]

    record = account(case)

    assert problem_codes_of(record) == []
    assert record["rules"]["d"]["status"] == "pass"
    assert record["entries"][0]["cited_ids"] == ["A1", "B1", "C1", "L1", "L2", "L3"]


def test_a_line_range_reaching_past_the_entrys_own_units_holds_the_reading_whole():
    case = page()
    acts(case)[0]["cites"] = ["A1", "B1", "C1", "L1-L4"]

    record = account(case)

    assert problem_codes_of(record) == ["detection-range"]
    assert "page-answer-incomplete" in record["holds"]


def test_a_line_range_with_no_unit_of_its_own_holds_the_reading_whole():
    case = page()
    acts(case)[0]["cites"] = ["L1-L3"]

    assert problem_codes_of(account(case)) == ["detection-range"]


def test_a_unit_holding_several_entries_places_none_of_those_reading_a_part_of_it():
    """One witness unit over two records (a whole index table): each entry cites it
    beside its own lines and units, all inside it, and is placed by those alone."""
    case = page()
    table = bx(100, 100, 900, 800)
    case["feed"]["witnesses"][0]["units"][0]["box_px"] = table
    acts(case)[0]["cites"] = ["A1", "B1", "C1", "L1", "L2", "L3"]
    acts(case)[1]["cites"] = ["A1", "B2", "C2", "L4", "L5", "L6"]

    record = account(case)

    assert duplicates(record) == []
    entries = {entry["n"]: entry for entry in record["entries"]}
    assert entries[1]["union_box_px"] == band(0)
    assert entries[2]["union_box_px"] == band(1)


def test_an_entry_citing_another_entrys_units_beside_its_own_ink_is_still_a_duplicate():
    """Entry 2 claims record 1's units as well as its own: two claims on the same ink."""
    case = page()
    acts(case)[1]["cites"] = ["A1", "B1", "C1", "A2", "B2", "C2"]

    assert duplicates(account(case)) == [[1, 2]]


# --- review flags ----------------------------------------------------------------------


def test_the_committed_file_seals_the_lead_s_review_flags(tmp_path: Path):
    """The committed `[flags]` table is the lead's four codes, and a file with no table seals
    the same four (the code's defaults), so a run sealed before the table existed replays
    under them."""
    assert SEALED.flag_codes == page_accounting_module.DEFAULT_FLAG_CODES
    silent = tmp_path / "silent.toml"
    silent.write_text(_without_flags_table(), encoding="utf-8")
    assert load_page_accounting_policy(silent).flag_codes == SEALED.flag_codes
    assert load_page_accounting_policy(silent).sha256 != SEALED.sha256
    assert SEALED.flag_codes == {
        "no-detector-record-on-act-page",
        "unread-ink",
        "residual-ink",
        "witness-short-unit-not-read",
    }
    assert SEALED.short_unit_characters == page_accounting_module.DEFAULT_SHORT_UNIT_CHARACTERS


def _without_flags_table() -> str:
    """The committed file with its `[flags]` table (the last table) cut off."""
    text = DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH.read_text(encoding="utf-8")
    head, _flags = text.split("\n[flags]\n", 1)
    return head + "\n"


def _with_flags(tmp_path: Path, table: str) -> Path:
    edited = tmp_path / "flags.toml"
    edited.write_text(_without_flags_table() + "\n" + table, encoding="utf-8")
    return edited


def test_the_flags_table_is_read_and_checked(tmp_path: Path):
    policy = load_page_accounting_policy(
        _with_flags(tmp_path, '[flags]\ncodes = ["unread-ink"]\nshort_unit_characters = 9\n')
    )
    assert policy.flag_codes == {"unread-ink"} and policy.short_unit_characters == 9
    assert policy.sha256 != SEALED.sha256
    # Every code may be taken back: an empty list holds everything, as before the flags.
    strict = load_page_accounting_policy(
        _with_flags(tmp_path, "[flags]\ncodes = []\nshort_unit_characters = 15\n")
    )
    assert strict.flag_codes == frozenset()
    with pytest.raises(ContractError, match="no page-level hold code"):
        load_page_accounting_policy(
            _with_flags(
                tmp_path, '[flags]\ncodes = ["duplicate-region"]\nshort_unit_characters = 15\n'
            )
        )
    # Each code that is also an entry hold is refused, not half-applied.
    for entry_hold in ("duplicate-region", "reading-incomplete", "reading-unplaced"):
        with pytest.raises(ContractError, match="no page-level hold code"):
            load_page_accounting_policy(
                _with_flags(
                    tmp_path, f'[flags]\ncodes = ["{entry_hold}"]\nshort_unit_characters = 15\n'
                )
            )
    with pytest.raises(ContractError, match="exactly codes and short_unit_characters"):
        load_page_accounting_policy(_with_flags(tmp_path, '[flags]\ncodes = ["unread-ink"]\n'))
    with pytest.raises(ContractError, match="twice"):
        load_page_accounting_policy(
            _with_flags(
                tmp_path,
                '[flags]\ncodes = ["unread-ink", "unread-ink"]\nshort_unit_characters = 1\n',
            )
        )


def test_a_flagged_finding_is_recorded_and_holds_nothing():
    """Under the sealed flags the detector's silence is measured, named, and holds no one."""
    case = page()
    _detector_found_nothing(case)

    record = account(case, SEALED)

    assert record["rules"]["i"]["status"] == "flag"
    assert record["rules"]["i"]["findings"] == [
        {"code": "no-detector-record-on-act-page", "acts": [1, 2, 3]}
    ]
    assert record["holds"] == []
    assert record["flags"] == ["no-detector-record-on-act-page"]
    # The same page under a policy with no flags holds, as it always did.
    assert account(case)["holds"] == ["no-detector-record-on-act-page"]


def test_a_rule_with_a_held_finding_beside_a_flagged_one_still_holds():
    case = page()
    _detector_found_nothing(case)
    acts(case)[2]["text"] = acts(case)[2]["text"].removesuffix(" Jean Roy")
    witness(case, "C")["units"].append({"id": "C4", "box_px": None, "text": "Jean Roy"})
    acts(case)[2]["cites"].append("C4")
    # An uncited short unit nobody read is unread text, not a dissent, whatever its length.
    witness(case, "C")["units"].append({"id": "C5", "box_px": None, "text": "Zqxwv Kjyq"})

    record = account(case, SEALED)

    codes = {f["id"]: f["code"] for f in record["rules"]["e"]["findings"] if "id" in f}
    assert codes["C4"] == "witness-short-unit-not-read"
    assert codes["C5"] == "witness-text-not-read"
    assert record["rules"]["e"]["status"] == "hold"
    # The uncited unit is also unaccounted for under rule (c); both hold.
    assert record["holds"] == ["unaccounted-witness-unit", "witness-text-not-read"]
    assert record["flags"] == ["no-detector-record-on-act-page", "witness-short-unit-not-read"]


def test_a_long_unit_read_differently_is_never_a_short_unit():
    """The burial folded into a merged unit (50 letters) keeps the hold the lead did not relax."""
    case = page(noise=0.15, seed=3)
    burial = noisy(BURIAL, 0.3, 11)
    witness(case, "A")["units"][2]["text"] += " " + burial
    witness(case, "C")["units"].append({"id": "C4", "box_px": None, "text": BURIAL})
    acts(case)[2]["cites"].append("C4")

    record = account(case, SEALED)

    assert record["holds"] == ["witness-text-not-read"]
    assert record["flags"] == []
