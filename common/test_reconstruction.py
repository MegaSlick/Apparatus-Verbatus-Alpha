"""The reconstruction rules: the plan, applying departures and the sealed policy."""

import dataclasses
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.reconstruction import (
    DEFAULT_RECONSTRUCTION_CONFIG_PATH,
    DEPARTURE_NO_CHANGE,
    HOLD_CODES,
    apply_departures,
    apply_join,
    entry_cite,
    load_reconstruction_policy,
    reconstruction_plan,
    unit_cite,
)
from common.sealed_config import read_sealed_toml

POLICY = load_reconstruction_policy()
BASE = "Le 3 mai Jan fils de Piere [[Roy|Rey]] et [[?]] sa femme"
CITEABLE = {
    "p4:1": "Le 2 mai Jean fils de Pierre Roy",
    "p4:2": BASE,
    "p4:3": "parrain Pierre Roy",
    "p4:A1": "Jean fils de Pierre",
}


def dep(diplomatic="Jan", reconstruction="Jean", basis=("p4:1",), reason="the name recurs"):
    return {
        "diplomatic": diplomatic,
        "reconstruction": reconstruction,
        "basis": list(basis),
        "reason": reason,
    }


def apply(departures, base=BASE, policy=POLICY, subject="p4:2"):
    return apply_departures(base, departures, CITEABLE, subject, policy)


def codes(holds):
    return sorted(hold["code"] for hold in holds)


# --- policy ------------------------------------------------------------------------------


def test_the_policy_loads_its_starting_values_with_its_seal():
    assert dataclasses.asdict(POLICY) | {"sha256": None} == {
        "max_departures_per_act": 5,
        "max_departure_characters": 40,
        "max_changed_share_bp": 1500,
        "changed_floor_characters": 16,
        "max_basis_cites": 6,
        "max_reason_characters": 160,
        "require_other_entry_basis": True,
        "sha256": None,
    }
    assert POLICY.sha256 == read_sealed_toml(DEFAULT_RECONSTRUCTION_CONFIG_PATH, "x")[1]


def _write_policy(tmp_path, text):
    path = tmp_path / "reconstruction.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _config_text():
    return Path(DEFAULT_RECONSTRUCTION_CONFIG_PATH).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text + "\nunknown = 1\n",
        lambda text: text.replace("max_basis_cites = 6\n", ""),
        lambda text: text.replace("max_basis_cites = 6", "max_basis_cites = 0"),
        lambda text: text.replace("max_basis_cites = 6", "max_basis_cites = true"),
        lambda text: text.replace("max_basis_cites = 6", "max_basis_cites = 6.0"),
        lambda text: text.replace("max_changed_share_bp = 1500", "max_changed_share_bp = 10001"),
        lambda text: text.replace(
            "require_other_entry_basis = true", "require_other_entry_basis = 1"
        ),
    ],
)
def test_the_policy_is_closed_and_typed(tmp_path, change):
    with pytest.raises(ContractError):
        load_reconstruction_policy(_write_policy(tmp_path, change(_config_text())))


def test_the_seal_follows_values_not_comments(tmp_path):
    commented = load_reconstruction_policy(_write_policy(tmp_path, "# note\n" + _config_text()))
    assert commented.sha256 == POLICY.sha256
    changed = load_reconstruction_policy(
        _write_policy(tmp_path, _config_text().replace("= 160", "= 161"))
    )
    assert changed.sha256 != POLICY.sha256


# --- cites -------------------------------------------------------------------------------


def test_cites_name_entries_by_act_key_and_witness_units_but_never_surya():
    assert entry_cite(4, 2) == "p4:2"
    assert unit_cite(4, "A7") == "p4:A7"
    for surya in ("L3", "S1"):
        with pytest.raises(ContractError):
            unit_cite(4, surya)


# --- the plan ----------------------------------------------------------------------------


def entry(page, n, *, kind="act", start=False, end=False, attempt=1):
    return {
        "act_key": f"p{page}:{n}",
        "page_ordinal": page,
        "n": n,
        "kind": kind,
        "continues_from_previous_page": start,
        "continues_to_next_page": end,
        "reading_attempt": attempt,
    }


THREE_PAGES = [
    entry(1, 1),
    entry(1, 2, end=True),
    entry(2, 1, start=True, end=True),
    entry(3, 1, start=True),
    entry(3, 2),
    entry(3, 3, kind="other"),
]


def test_mode_off_plans_nothing():
    assert reconstruction_plan(THREE_PAGES, mode="off", pages_are_consecutive=True) == []


def test_an_unknown_mode_is_refused():
    with pytest.raises(ContractError):
        reconstruction_plan(THREE_PAGES, mode="maybe", pages_are_consecutive=True)


def test_a_three_page_act_is_one_join_asked_on_the_page_of_its_last_piece():
    assert reconstruction_plan(THREE_PAGES, mode="on", pages_are_consecutive=True) == [
        {"page_ordinal": 1, "subjects": ["p1:1"], "chains": []},
        {"page_ordinal": 3, "subjects": ["p3:2"], "chains": [["p1:2", "p2:1", "p3:1"]]},
    ]


def test_without_consecutive_pages_there_are_no_chains_and_every_act_is_a_subject():
    assert reconstruction_plan(THREE_PAGES, mode="on", pages_are_consecutive=False) == [
        {"page_ordinal": 1, "subjects": ["p1:1", "p1:2"], "chains": []},
        {"page_ordinal": 2, "subjects": ["p2:1"], "chains": []},
        {"page_ordinal": 3, "subjects": ["p3:1", "p3:2"], "chains": []},
    ]


def test_a_break_one_side_flags_is_not_planned_as_a_join():
    entries = [entry(1, 1, end=True), entry(2, 1)]
    assert reconstruction_plan(entries, mode="on", pages_are_consecutive=True) == [
        {"page_ordinal": 1, "subjects": ["p1:1"], "chains": []},
        {"page_ordinal": 2, "subjects": ["p2:1"], "chains": []},
    ]


def test_other_entries_unread_pages_and_later_attempts_are_never_subjects():
    entries = [entry(1, 1, kind="other"), entry(2, None), entry(3, 1, attempt=2), entry(4, 1)]
    assert reconstruction_plan(entries, mode="on", pages_are_consecutive=True) == [
        {"page_ordinal": 4, "subjects": ["p4:1"], "chains": []}
    ]


# --- applying departures -----------------------------------------------------------------


def test_no_departures_leave_the_diplomatic():
    assert apply([]) == (BASE, [], [])


def test_a_departure_applies_and_records_its_spans():
    text, findings, holds = apply([dep()])
    assert holds == []
    assert text == BASE.replace("Jan", "Jean")
    [finding] = findings
    assert finding["raw_span"] == {"start": 9, "end": 12}
    assert finding["clean_span"] == {"start": 9, "end": 12}
    assert finding["notes"] == []
    assert finding["basis_text_found"] is True


def test_spans_after_a_mark_are_recorded_in_clean_offsets_too():
    text, findings, holds = apply([dep("Piere", "Pierre"), dep("[[?]] sa", "sa")])
    assert holds == []
    assert text == "Le 3 mai Jan fils de Pierre [[Roy|Rey]] et sa femme"
    start = BASE.index("[[?]]")
    assert findings[1]["raw_span"] == {"start": start, "end": start + 8}
    clean = read_clean(BASE)
    assert findings[1]["clean_span"] == {"start": clean.index(" sa"), "end": clean.index(" sa") + 3}
    assert findings[0]["clean_span"] == findings[0]["raw_span"]


def read_clean(raw):
    from common.reading_annotations import read_doubt_marks

    return read_doubt_marks(raw)[0]


def test_a_whole_gap_mark_maps_to_a_zero_width_clean_span():
    _, findings, holds = apply([dep("[[?]]", "Marie")])
    assert holds == []
    assert findings[0]["clean_span"]["start"] == findings[0]["clean_span"]["end"]


def test_a_whole_doubt_mark_may_be_departed_from():
    text, _, holds = apply([dep("[[Roy|Rey]]", "Roy")])
    assert holds == [] and "Pierre" not in text and "Piere Roy et" in text


def test_a_departure_that_changes_nothing_is_recorded_and_does_not_hold():
    text, findings, holds = apply([dep("Jan", "Jan")])
    assert (text, holds) == (BASE, [])
    assert findings[0]["notes"] == [DEPARTURE_NO_CHANGE]


def test_basis_text_not_found_is_recorded_and_does_not_hold():
    text, findings, holds = apply([dep("Jan", "Jacques")])
    assert holds == [] and text is not None
    assert findings[0]["basis_text_found"] is False


@pytest.mark.parametrize(
    "departure",
    [
        {k: v for k, v in dep().items() if k != "basis"},
        dep(basis=()),
    ],
)
def test_a_departure_with_no_basis_holds(departure):
    text, _, holds = apply([departure])
    assert text is None and codes(holds) == ["departure-no-basis"]


@pytest.mark.parametrize(
    "departure",
    [
        "Jan",
        {**dep(), "note": ""},
        {k: v for k, v in dep().items() if k != "reason"},
        dep(diplomatic=""),
        dep(diplomatic=3),
        dep(reconstruction=None),
        {**dep(), "basis": "p4:1"},
        dep(basis=("p4:1", 2)),
        dep(reason=" "),
        dep(reason="x" * 161),
        dep(basis=("p4:1",) * 7),
    ],
)
def test_a_departure_off_its_shape_holds(departure):
    text, _, holds = apply([departure])
    assert text is None and codes(holds) == ["departure-invalid"]


def test_a_departure_at_its_bounds_does_not_hold():
    text, _, holds = apply([dep(reason="x" * 160, basis=("p4:1",) * 6)])
    assert holds == [] and text is not None


def test_departures_that_are_not_a_list_hold():
    text, _, holds = apply({"diplomatic": "Jan"})
    assert text is None and codes(holds) == ["departure-invalid"]


def test_a_span_not_in_the_diplomatic_holds():
    text, _, holds = apply([dep("Jacques", "Jean")])
    assert text is None and codes(holds) == ["departure-span-not-found"]


def test_departures_are_found_in_order_after_the_one_before():
    text, _, holds = apply([dep("Piere", "Pierre"), dep("Jan", "Jean")])
    assert text is None and codes(holds) == ["departure-span-not-found"]


def test_a_span_found_twice_holds_but_one_found_once_after_the_cursor_does_not():
    base = "de Jan et de Jan"
    text, _, holds = apply([dep("Jan", "Jean")], base=base)
    assert text is None and codes(holds) == ["departure-span-ambiguous"]
    text, _, holds = apply([dep("de Jan et", "de Jean et"), dep("Jan", "Jean")], base=base)
    assert holds == [] and text == "de Jean et de Jean"


@pytest.mark.parametrize("diplomatic", ["Piere [[Roy", "Rey]] et", "[[Roy", "Roy|Rey]]", "[?]]"])
def test_a_span_ending_inside_a_doubt_mark_holds(diplomatic):
    text, _, holds = apply([dep(diplomatic, "x")])
    assert text is None and codes(holds) == ["departure-splits-doubt-mark"]


def test_a_cite_not_shown_holds():
    text, _, holds = apply([dep(basis=("p4:1", "p9:1"))])
    assert text is None and codes(holds) == ["basis-not-shown"]


def test_a_surya_cite_is_never_shown():
    text, _, holds = apply([dep(basis=("p4:1", "p4:L3"))])
    assert text is None and codes(holds) == ["basis-not-shown"]


@pytest.mark.parametrize("basis", [("p4:2",), ("p4:A1",), ("p4:2", "p4:A1")])
def test_a_basis_citing_no_other_entry_holds(basis):
    text, _, holds = apply([dep(basis=basis)])
    assert text is None and codes(holds) == ["basis-no-reading"]


def test_the_policy_may_let_a_departure_rest_on_witness_text_alone():
    relaxed = dataclasses.replace(POLICY, require_other_entry_basis=False)
    text, _, holds = apply([dep(basis=("p4:A1",))], policy=relaxed)
    assert holds == [] and text is not None


def test_more_departures_than_the_bound_hold_and_the_bound_itself_does_not():
    base = "a b c d e f g h i j k l m n o p q r s t u v w x y z " + "." * 60
    five = [dep(letter, letter.upper()) for letter in "abcde"]
    text, _, holds = apply(five, base=base)
    assert holds == []
    text, _, holds = apply([*five, dep("f", "F")], base=base)
    assert text is None and codes(holds) == ["reconstruction-too-large"]


def test_a_side_longer_than_the_bound_holds_and_one_at_it_does_not():
    base = "x" + "a" * 40 + "y" * 400
    text, _, holds = apply([dep("x" + "a" * 39, "b" * 40)], base=base)
    assert holds == []
    text, _, holds = apply([dep("x", "b" * 41)], base=base)
    assert text is None and codes(holds) == ["reconstruction-too-large"]


def test_changed_characters_are_bounded_by_the_share_or_the_floor():
    base = "abcdefghijklmnopqrstuvwxyz" + "." * 74  # 100 characters: share 15, floor 16
    text, _, holds = apply([dep("abcdefghijklmnop", "A" * 16)], base=base)
    assert holds == []
    text, _, holds = apply([dep("abcdefghijklmnopq", "A" * 17)], base=base)
    assert text is None and codes(holds) == ["reconstruction-too-large"]
    long_base = "abcdefghijklmnopqrstuvwxyz" + "." * 174  # 200 characters: share 30
    text, _, holds = apply([dep("abcdefghijklmnopqrstuvwxyz", "Z" * 26)], base=long_base)
    assert holds == []


def test_a_departure_that_changes_nothing_counts_no_characters():
    base = "abcdefghijklmnopqrstuvwxyz" + "." * 74
    text, _, holds = apply(
        [dep("abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz")], base=base
    )
    assert holds == []


def test_a_reconstruction_whose_marks_do_not_parse_holds_and_a_well_formed_mark_does_not():
    text, _, holds = apply([dep("Jan", "[[Jean")])
    assert text is None and codes(holds) == ["reconstruction-marks-malformed"]
    text, _, holds = apply([dep("Jan", "[[Jean|Jan]]")])
    assert holds == [] and "[[Jean|Jan]]" in text


def test_every_hold_code_is_exercised_here():
    assert HOLD_CODES == {
        "departure-no-basis",
        "departure-invalid",
        "departure-span-not-found",
        "departure-span-ambiguous",
        "departure-splits-doubt-mark",
        "basis-not-shown",
        "basis-no-reading",
        "reconstruction-too-large",
        "reconstruction-marks-malformed",
        "join-departures-without-continuation",
    }


# --- joins -------------------------------------------------------------------------------

JOIN_CITEABLE = {
    "p3:8": "baptisé par moy Jean Roy curé",
    "p3:9": "Le 9 mai a été bapti",
    "p4:1": "sé Jean fils",
    "p4:2": "parrain Jean Roy",
    "p3:A4": "Le 9 mai a ete baptise",
}


def join(departures, continues=True):
    return {"acts": ["p3:9", "p4:1"], "continues": continues, "departures": departures}


def test_a_join_applies_to_the_pieces_joined_by_one_newline():
    text, findings, holds = apply_join(
        ["Le 9 mai a été bapti", "sé Jean fils"],
        join([dep("bapti\nsé", "baptisé", basis=("p4:2", "p3:A4"))]),
        JOIN_CITEABLE,
        POLICY,
    )
    assert holds == [] and text == "Le 9 mai a été baptisé Jean fils"
    assert findings[0]["raw_span"] == {"start": 15, "end": 23}


def test_a_join_with_no_departures_is_the_pieces_joined():
    text, _, holds = apply_join(["a", "b"], join([]), JOIN_CITEABLE, POLICY)
    assert (text, holds) == ("a\nb", [])


def test_a_join_basis_must_cite_an_entry_outside_the_chain():
    text, _, holds = apply_join(
        ["Le 9 mai a été bapti", "sé Jean fils"],
        join([dep("bapti\nsé", "baptisé", basis=("p4:1", "p3:A4"))]),
        JOIN_CITEABLE,
        POLICY,
    )
    assert text is None and codes(holds) == ["basis-no-reading"]


def test_a_join_that_does_not_continue_has_no_reconstruction_and_holds_only_with_departures():
    assert apply_join(["a", "b"], join([], continues=False), JOIN_CITEABLE, POLICY) == (
        None,
        [],
        [],
    )
    text, _, holds = apply_join(
        ["a", "b"], join([dep("a", "A", basis=("p4:2",))], continues=False), JOIN_CITEABLE, POLICY
    )
    assert text is None and codes(holds) == ["join-departures-without-continuation"]
