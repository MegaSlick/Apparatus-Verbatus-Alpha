"""The Coniector's rules: the plan, applying departures and the sealed policy."""

import dataclasses
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.reconstruction import (
    DEFAULT_RECONSTRUCTION_CONFIG_PATH,
    DEPARTURE_NO_CHANGE,
    NOT_MADE_CODES,
    apply_departures,
    apply_join,
    load_reconstruction_policy,
    reconstruction_plan,
)
from common.sealed_config import read_sealed_toml

POLICY = load_reconstruction_policy()
BASE = "Le 3 mai Jan fils de Piere [[Roy|Rey]] et [[?]] sa femme"
SHOWN = ["Le 2 mai Jean fils de Pierre Roy", BASE, "parrain Pierre Roy"]


def dep(diplomatic="Jan", reconstruction="Jean", reason="the name recurs"):
    return {"diplomatic": diplomatic, "reconstruction": reconstruction, "reason": reason}


def apply(departures, base=BASE, policy=POLICY):
    return apply_departures(base, departures, SHOWN, policy)


def codes(not_made):
    return sorted(problem["code"] for problem in not_made)


def assert_not_made(result, *expected):
    text, applied, not_made = result
    assert text is None and applied == [] and codes(not_made) == sorted(expected)


# --- policy ------------------------------------------------------------------------------


def test_the_policy_loads_its_starting_values_with_its_seal():
    assert dataclasses.asdict(POLICY) | {"sha256": None} == {
        "mode": "off",
        "pages_are_consecutive": False,
        "max_departures_per_act": 5,
        "max_departure_characters": 40,
        "max_changed_share_bp": 1500,
        "changed_floor_characters": 16,
        "max_reason_characters": 160,
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
        lambda text: text + "\nrequire_other_entry_basis = true\n",
        lambda text: text + "\nmax_basis_cites = 6\n",
        lambda text: text.replace("max_departures_per_act = 5\n", ""),
        lambda text: text.replace("max_departures_per_act = 5", "max_departures_per_act = 0"),
        lambda text: text.replace("max_departures_per_act = 5", "max_departures_per_act = true"),
        lambda text: text.replace("max_departures_per_act = 5", "max_departures_per_act = 5.0"),
        lambda text: text.replace("max_changed_share_bp = 1500", "max_changed_share_bp = 10001"),
        lambda text: text.replace('mode = "off"', 'mode = "sometimes"'),
        lambda text: text.replace('mode = "off"', 'mode = ["on"]'),
        lambda text: text.replace('mode = "off"\n', ""),
        lambda text: text.replace("pages_are_consecutive = false", 'pages_are_consecutive = "no"'),
        lambda text: text.replace("pages_are_consecutive = false\n", ""),
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


def test_consecutive_must_be_a_boolean():
    with pytest.raises(ContractError):
        reconstruction_plan(THREE_PAGES, mode="on", pages_are_consecutive=1)


def test_a_three_page_act_is_one_join_asked_on_the_page_of_its_last_piece():
    assert reconstruction_plan(THREE_PAGES, mode="on", pages_are_consecutive=True) == [
        {"page_ordinal": 1, "subjects": ["p1:1"], "chains": [], "context": ["p2:1"]},
        {
            "page_ordinal": 3,
            "subjects": ["p3:2"],
            "chains": [["p1:2", "p2:1", "p3:1"]],
            "context": ["p2:1"],
        },
    ]


def test_without_consecutive_pages_there_are_no_chains_and_no_cross_page_context():
    assert reconstruction_plan(THREE_PAGES, mode="on", pages_are_consecutive=False) == [
        {"page_ordinal": 1, "subjects": ["p1:1", "p1:2"], "chains": [], "context": []},
        {"page_ordinal": 2, "subjects": ["p2:1"], "chains": [], "context": []},
        {"page_ordinal": 3, "subjects": ["p3:1", "p3:2"], "chains": [], "context": []},
    ]


def test_consecutive_pages_show_the_neighbours_edge_acts_even_with_no_break_flagged():
    entries = [entry(1, 1), entry(1, 2), entry(2, 1), entry(2, 2), entry(3, 1), entry(3, 2)]
    assert reconstruction_plan(entries, mode="on", pages_are_consecutive=True) == [
        {"page_ordinal": 1, "subjects": ["p1:1", "p1:2"], "chains": [], "context": ["p2:1"]},
        {
            "page_ordinal": 2,
            "subjects": ["p2:1", "p2:2"],
            "chains": [],
            "context": ["p1:2", "p3:1"],
        },
        {"page_ordinal": 3, "subjects": ["p3:1", "p3:2"], "chains": [], "context": ["p2:2"]},
    ]
    assert all(
        call["context"] == []
        for call in reconstruction_plan(entries, mode="on", pages_are_consecutive=False)
    )


def test_context_skips_a_neighbour_with_no_act_and_never_names_an_other_entry():
    entries = [entry(1, 1), entry(1, 2, kind="other"), entry(2, None), entry(3, 1)]
    assert reconstruction_plan(entries, mode="on", pages_are_consecutive=True) == [
        {"page_ordinal": 1, "subjects": ["p1:1"], "chains": [], "context": []},
        {"page_ordinal": 3, "subjects": ["p3:1"], "chains": [], "context": []},
    ]


def test_a_break_one_side_flags_is_not_planned_as_a_join():
    entries = [entry(1, 1, end=True), entry(2, 1)]
    assert reconstruction_plan(entries, mode="on", pages_are_consecutive=True) == [
        {"page_ordinal": 1, "subjects": ["p1:1"], "chains": [], "context": ["p2:1"]},
        {"page_ordinal": 2, "subjects": ["p2:1"], "chains": [], "context": ["p1:1"]},
    ]


def test_other_entries_unread_pages_and_later_attempts_are_never_subjects():
    entries = [entry(1, 1, kind="other"), entry(2, None), entry(3, 1, attempt=2), entry(4, 1)]
    assert reconstruction_plan(entries, mode="on", pages_are_consecutive=True) == [
        {"page_ordinal": 4, "subjects": ["p4:1"], "chains": [], "context": []}
    ]


# --- applying departures -----------------------------------------------------------------


def test_no_departures_leave_the_diplomatic():
    assert apply([]) == (BASE, [], [])


def test_a_departure_applies_and_records_its_spans():
    text, applied, not_made = apply([dep()])
    assert not_made == []
    assert text == BASE.replace("Jan", "Jean")
    [departure] = applied
    assert departure["raw_span"] == {"start": 9, "end": 12}
    assert departure["clean_span"] == {"start": 9, "end": 12}
    assert departure["notes"] == []
    assert departure["reason"] == "the name recurs"
    assert departure["replacement_in_context"] is True


def test_a_departure_s_reason_is_optional():
    text, applied, not_made = apply([{"diplomatic": "Jan", "reconstruction": "Jean"}])
    assert not_made == [] and text == BASE.replace("Jan", "Jean")
    assert applied[0]["reason"] is None


def test_spans_after_a_mark_are_recorded_in_clean_offsets_too():
    text, applied, not_made = apply([dep("Piere", "Pierre"), dep("[[?]] sa", "sa")])
    assert not_made == []
    assert text == "Le 3 mai Jan fils de Pierre [[Roy|Rey]] et sa femme"
    start = BASE.index("[[?]]")
    assert applied[1]["raw_span"] == {"start": start, "end": start + 8}
    clean = read_clean(BASE)
    assert applied[1]["clean_span"] == {"start": clean.index(" sa"), "end": clean.index(" sa") + 3}
    assert applied[0]["clean_span"] == applied[0]["raw_span"]


def read_clean(raw):
    from common.reading_annotations import read_doubt_marks

    return read_doubt_marks(raw)[0]


def test_a_whole_gap_mark_maps_to_a_zero_width_clean_span():
    _, applied, not_made = apply([dep("[[?]]", "Marie")])
    assert not_made == []
    assert applied[0]["clean_span"]["start"] == applied[0]["clean_span"]["end"]


def test_a_whole_doubt_mark_may_be_departed_from():
    text, _, not_made = apply([dep("[[Roy|Rey]]", "Roy")])
    assert not_made == [] and "Pierre" not in text and "Piere Roy et" in text


def test_a_departure_that_changes_nothing_is_recorded_and_never_refuses():
    text, applied, not_made = apply([dep("Jan", "Jan")])
    assert (text, not_made) == (BASE, [])
    assert applied[0]["notes"] == [DEPARTURE_NO_CHANGE]
    assert DEPARTURE_NO_CHANGE not in NOT_MADE_CODES


@pytest.mark.parametrize(
    ("replacement", "found"),
    [("Jean", True), ("Pierre Roy", True), ("Jacques", False), ("", False), ("--", False)],
)
def test_replacement_in_context_is_measured_against_every_shown_text_and_never_refuses(
    replacement, found
):
    text, applied, not_made = apply([dep("Jan", replacement)])
    assert not_made == [] and text is not None
    assert applied[0]["replacement_in_context"] is found


def test_replacement_in_context_is_false_when_nothing_was_shown():
    _, applied, _ = apply_departures(BASE, [dep()], [], POLICY)
    assert applied[0]["replacement_in_context"] is False


@pytest.mark.parametrize(
    "departure",
    [
        "Jan",
        {**dep(), "note": ""},
        {**dep(), "basis": ["p4:1"]},
        {"diplomatic": "Jan", "reason": "r"},
        {"reconstruction": "Jean"},
        dep(diplomatic=""),
        dep(diplomatic=3),
        dep(reconstruction=None),
        dep(reason=None),
        dep(reason=["r"]),
        dep(reason="x" * 161),
    ],
)
def test_a_departure_off_its_shape_is_not_made(departure):
    assert_not_made(apply([departure]), "departure-invalid")


def test_a_departure_at_its_reason_bound_or_with_an_empty_reason_is_made():
    for reason in ("x" * 160, ""):
        text, _, not_made = apply([dep(reason=reason)])
        assert not_made == [] and text is not None


def test_departures_that_are_not_a_list_are_not_made():
    assert_not_made(apply({"diplomatic": "Jan"}), "departure-invalid")


def test_a_span_not_in_the_diplomatic_is_not_made():
    assert_not_made(apply([dep("Jacques", "Jean")]), "departure-span-not-found")


def test_departures_are_found_in_order_after_the_one_before():
    assert_not_made(apply([dep("Piere", "Pierre"), dep("Jan", "Jean")]), "departure-span-not-found")


def test_a_span_found_twice_is_not_made_but_one_found_once_after_the_cursor_is():
    base = "de Jan et de Jan"
    assert_not_made(apply([dep("Jan", "Jean")], base=base), "departure-span-ambiguous")
    text, _, not_made = apply([dep("de Jan et", "de Jean et"), dep("Jan", "Jean")], base=base)
    assert not_made == [] and text == "de Jean et de Jean"


@pytest.mark.parametrize("diplomatic", ["Piere [[Roy", "Rey]] et", "[[Roy", "Roy|Rey]]", "[?]]"])
def test_a_span_ending_inside_a_doubt_mark_is_not_made(diplomatic):
    assert_not_made(apply([dep(diplomatic, "x")]), "departure-splits-doubt-mark")


@pytest.mark.parametrize(
    "bad",
    [
        dep("Jacques", "Jean"),
        dep(reason="x" * 161),
        dep("[[Roy", "x"),
        {"diplomatic": "", "reconstruction": "x"},
    ],
)
def test_one_failing_departure_refuses_the_whole_act_and_the_good_ones_are_not_applied(bad):
    good = [dep("Le 3", "Le 4"), dep("femme", "épouse")]
    text, applied, not_made = apply([good[0], bad, good[1]])
    assert text is None and applied == [] and len(not_made) == 1
    assert not_made[0]["departure"] == 1


def test_more_departures_than_the_bound_are_not_made_and_the_bound_itself_is_made():
    base = "a b c d e f g h i j k l m n o p q r s t u v w x y z " + "." * 60
    five = [dep(letter, letter.upper()) for letter in "abcde"]
    text, _, not_made = apply(five, base=base)
    assert not_made == []
    assert_not_made(apply([*five, dep("f", "F")], base=base), "reconstruction-too-large")


def test_a_side_longer_than_the_bound_is_not_made_and_one_at_it_is_made():
    base = "x" + "a" * 40 + "y" * 400
    text, _, not_made = apply([dep("x" + "a" * 39, "b" * 40)], base=base)
    assert not_made == []
    assert_not_made(apply([dep("x", "b" * 41)], base=base), "reconstruction-too-large")


def test_changed_characters_are_bounded_by_the_share_or_the_floor():
    base = "abcdefghijklmnopqrstuvwxyz" + "." * 74  # 100 characters: share 15, floor 16
    text, _, not_made = apply([dep("abcdefghijklmnop", "A" * 16)], base=base)
    assert not_made == []
    assert_not_made(
        apply([dep("abcdefghijklmnopq", "A" * 17)], base=base), "reconstruction-too-large"
    )
    long_base = "abcdefghijklmnopqrstuvwxyz" + "." * 174  # 200 characters: share 30
    text, _, not_made = apply([dep("abcdefghijklmnopqrstuvwxyz", "Z" * 26)], base=long_base)
    assert not_made == []


def test_a_departure_that_changes_nothing_counts_no_characters():
    base = "abcdefghijklmnopqrstuvwxyz" + "." * 74
    text, _, not_made = apply(
        [dep("abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz")], base=base
    )
    assert not_made == []


def test_a_reconstruction_whose_marks_do_not_parse_is_not_made_and_a_well_formed_mark_is():
    assert_not_made(apply([dep("Jan", "[[Jean")]), "reconstruction-marks-malformed")
    text, _, not_made = apply([dep("Jan", "[[Jean|Jan]]")])
    assert not_made == [] and "[[Jean|Jan]]" in text


def test_the_not_made_codes_are_closed_and_none_holds_or_rests_on_a_basis():
    assert NOT_MADE_CODES == {
        "departure-invalid",
        "departure-span-not-found",
        "departure-span-ambiguous",
        "departure-splits-doubt-mark",
        "reconstruction-too-large",
        "reconstruction-marks-malformed",
        "join-departures-without-continuation",
    }
    import common.reconstruction as module

    assert not hasattr(module, "HOLD_CODES")
    assert not any("basis" in name.lower() and name != "BASIS_POINTS" for name in dir(module))


# --- joins -------------------------------------------------------------------------------

JOIN_SHOWN = [
    "baptisé par moy Jean Roy curé",
    "Le 9 mai a été bapti",
    "sé Jean fils",
    "parrain Jean Roy",
]


def join(departures, continues=True):
    return {"acts": ["p3:9", "p4:1"], "continues": continues, "departures": departures}


def test_a_join_applies_to_the_pieces_joined_by_one_newline():
    text, applied, not_made = apply_join(
        ["Le 9 mai a été bapti", "sé Jean fils"],
        join([dep("bapti\nsé", "baptisé")]),
        JOIN_SHOWN,
        POLICY,
    )
    assert not_made == [] and text == "Le 9 mai a été baptisé Jean fils"
    assert applied[0]["raw_span"] == {"start": 15, "end": 23}
    assert applied[0]["replacement_in_context"] is True


def test_a_join_with_no_departures_is_the_pieces_joined():
    text, _, not_made = apply_join(["a", "b"], join([]), JOIN_SHOWN, POLICY)
    assert (text, not_made) == ("a\nb", [])


def test_a_failing_join_departure_leaves_the_join_not_made():
    result = apply_join(
        ["Le 9 mai a été bapti", "sé Jean fils"],
        join([dep("bapti\nsé", "baptisé"), dep("fille", "fils")]),
        JOIN_SHOWN,
        POLICY,
    )
    assert_not_made(result, "departure-span-not-found")


def test_a_join_that_does_not_continue_has_no_reconstruction_and_is_not_made_with_departures():
    assert apply_join(["a", "b"], join([], continues=False), JOIN_SHOWN, POLICY) == (None, [], [])
    assert_not_made(
        apply_join(["a", "b"], join([dep("a", "A")], continues=False), JOIN_SHOWN, POLICY),
        "join-departures-without-continuation",
    )
