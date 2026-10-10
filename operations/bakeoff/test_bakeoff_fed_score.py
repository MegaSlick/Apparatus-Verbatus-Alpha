"""The Perlector scorecard on synthetic pages, readings and gold (never real transcriptions)."""

import json
import sys
from collections import Counter

from operations.bakeoff import fed_arm as F
from operations.bakeoff import fed_score as C
from operations.bakeoff.gold import parse_gold
from operations.bakeoff.score import FOOLS_GOLD_LABEL
from operations.bakeoff.test_bakeoff_fed_arm import make_run_tree
from operations.bakeoff.test_bakeoff_runner import FAKE, _free_port

GOLD = """FILE: {stem}.tif
STATUS: {status}
SOURCE: synthetic
CATEGORY: acts-19c
FORM: handwritten
CONDITION: good
VERDICT: acts
PAGES IN IMAGE: 1
TEST PAGE: no

=== ACT 1 | baptism | from previous page: no | to next page: no ===
{text}
"""


def _gold(folder, texts, status="fool's gold"):
    folder.mkdir(parents=True, exist_ok=True)
    for stem, text in texts.items():
        (folder / f"{stem}.txt").write_text(GOLD.format(stem=stem, status=status, text=text))
    return folder


def test_alignment_keeps_the_word_written_in_place():
    right, form, inserted = C.aligned(("le", "dix", "mai"), ("le", "dis", "mai", "x"))
    assert right == [True, False, True] and form == [None, "dis", None] and inserted == 1
    right, form, _ = C.aligned(("le", "dix"), ("le",))
    assert right == [True, False] and form == [None, None]
    # A tie in the edit script does not decide which word stands in for "mai".
    right, form, inserted = C.aligned(("le", "dix", "mai"), ("le", "dix", "mars", "folio", "1"))
    assert right == [True, True, False] and form[2] == "mars" and inserted == 2


def test_follow_resist_copy_and_vote_on_one_page(tmp_path):
    # Gold "Le dix mai mil huit"; Chandra "Le dix mai mil" + "Folio 1"; DAI empty;
    # Churro "Le dix mars mil huit"; the run's reading "Le dix mai mil".
    tree = F.load_run_tree(make_run_tree(tmp_path / "run"))
    answers = C.answers_from_run_tree(tree)
    gold = {
        "p001": parse_gold(GOLD.format(stem="p001", status="x", text="Le dix mai mil huit"), "p001")
    }
    card = C.scorecard(answers, gold, hard={"p001"})
    s = card["groups"]["acts-handwritten"]
    c = s["scepticism"]["counts"]
    assert s["scepticism"]["witnesses"] == ["chandra", "dai", "churro"]
    assert (c["tokens"], c["reader-right"]) == (5, 4)
    assert (c["only-right:chandra"], c["only-right:chandra,followed"]) == (1, 1)  # "mai"
    assert (c["only-right:churro"], c["only-right:churro,followed"]) == (1, 0)  # "huit"
    # DAI read nothing: absent, never "the only one wrong" (C7: that measured absence).
    assert (c["only-wrong:dai"], c["absent:dai"]) == (0, 5)
    assert (c["wrong-form:churro"], c["wrong-form:churro,copied"]) == (1, 0)  # "mars"
    assert c["wrong-form:chandra"] == 1 and c["wrong-form:dai"] == 0  # an empty DAI has no form
    # Only Chandra and Churro read the page: "mai" and "huit" are one-to-one ties that
    # include the right word, reported apart (the reader has "mai", not "huit").
    assert (c["vote-beaten"], c["vote-lost"]) == (0, 0)
    assert (c["vote-tie"], c["vote-tie,reader-right"], c["vote-right"]) == (2, 1, 3)
    assert (c["reader-wrong"], c["reader-wrong,a-witness-error"]) == (1, 0)
    assert s["gold_acts"] == s["acts_matched"] == s["act_entries"] == 1
    assert 0 < s["cer_median_parsed"] < 0.5 and s["inserted"] == 0
    assert card["hard_acts"]["pages"] == 1


def test_an_answer_naming_page_types_scores_as_its_acts_shape(tmp_path):
    # The same reading in the `entries` grammar the protocol's `page_types = "named"`
    # asks for: its text and its act entries score exactly as the `acts` shape's do,
    # an instrument counting as an act and an index row not.
    tree = F.load_run_tree(make_run_tree(tmp_path / "run"))
    answers = C.answers_from_run_tree(tree)
    gold = {
        "p001": parse_gold(GOLD.format(stem="p001", status="x", text="Le dix mai mil huit"), "p001")
    }
    before = C.scorecard(answers, gold, hard={"p001"})
    for answer in answers.values():
        acts = answer.answer.pop("acts")
        answer.answer.update(page_type="register-acts", writing="handwritten", entries=acts)
    assert F.reading_text(answers["p001"].answer) == "Le dix mai mil"
    assert C.scorecard(answers, gold, hard={"p001"}) == before
    for kind, counted in (("instrument", 1), ("index-row", 0)):
        for answer in answers.values():
            answer.answer["entries"][0]["kind"] = kind
        s = C.scorecard(answers, gold, hard={"p001"})["groups"]["acts-handwritten"]
        assert s["act_entries"] == counted


def test_a_copied_wrong_word_is_counted(tmp_path):
    tree = F.load_run_tree(make_run_tree(tmp_path / "run", pages=(("Le dix mars", "Le dix mars"),)))
    answers = C.answers_from_run_tree(tree)
    gold = {"p001": parse_gold(GOLD.format(stem="p001", status="x", text="Le dix mai"), "p001")}
    c = C.scorecard(answers, gold, set())["groups"]["acts-handwritten"]["scepticism"]["counts"]
    assert c["all-wrong"] == 1 and c["all-wrong,recovered"] == 0
    assert (c["wrong-form:chandra"], c["wrong-form:chandra,copied"]) == (1, 1)
    assert (c["reader-wrong"], c["reader-wrong,a-witness-error"]) == (1, 1)


def test_a_fed_cache_scored_and_compared_end_to_end(tmp_path):
    tree = make_run_tree(tmp_path / "run", pages=(
        ("Le dix mai mil", "Le dix mars mil huit"),
        ("Le onze juin", "Le onze juin"),
        ("Le douze mai", "LOOP-TEST"),
    ))  # fmt: skip
    (tree / "config.json").write_text("{}")
    cache = tmp_path / "cache"
    argv = [
        "run", "--run-tree", str(tree), "--out", str(cache), "--label", "swap",
        "--model-name", "m", "--weights", str(tree), "--vllm-cmd", sys.executable, str(FAKE),
        "--port", str(_free_port()), "--startup-timeout", "60",
        "--letter-map", "attestator_1=C,attestator_3=A",
        "--witness-order", "attestator_3,attestator_2,attestator_1",
    ]  # fmt: skip
    assert F.main(argv) == 0
    texts = {"p001": "Le dix mai mil huit", "p002": "Le onze juin", "p003": "Le douze mai"}
    gold = _gold(tmp_path / "gold", texts)
    out = tmp_path / "card"
    common = ["--run-tree", str(tree), "--gold", str(gold), "--gold-glob", "*.txt"]
    assert C.main([*common, "--answers", str(cache / "swap"), "--compare", str(tree),
                   "--names", "swap,run", "--out", str(out)]) == 0  # fmt: skip
    card = (out / "scorecard.md").read_text()
    assert card.startswith(f"# Perlector scorecard {FOOLS_GOLD_LABEL}")
    assert "## swap vs run" in card and "loop-guard stops 1" in card
    assert "| chandra |" in card and "repetition-loop 1" in card
    data = json.loads((out / "scorecard.json").read_text())
    assert data["invariance"]["pages"] == 3
    swap = data["answers"]["groups"]["acts-handwritten"]
    assert swap["pages"] == 3 and swap["parsed"] == 2
    # Witnesses are named by chair, whatever letter or place the swap gave them.
    assert swap["scepticism"]["witnesses"] == ["churro", "dai", "chandra"]
    # Lead-checked gold drops the label.
    gold = _gold(tmp_path / "checked", texts, status="lead-checked")
    common[3] = str(gold)
    assert C.main([*common, "--out", str(out)]) == 0
    assert FOOLS_GOLD_LABEL not in (out / "scorecard.md").read_text()


def test_copy_attribution_is_one_to_one():
    # C7: `Jean Paul` read as `Jeanne` gave `Jeanne` for both gold words, so one
    # emitted word could count as two copied errors.
    right, form, inserted = C.aligned(("Jean", "Paul"), ("Jeanne",))
    assert right == [False, False] and form == ["Jeanne", None] and inserted == 0
    right, form, _ = C.aligned(("le", "Jean", "Paul", "fils"), ("le", "Paule", "Jeanne", "fils"))
    assert form[1:3] in (["Paule", "Jeanne"], ["Jeanne", None], [None, "Jeanne"])
    assert len([f for f in form if f is not None]) == len(set(f for f in form if f is not None))


def test_absent_witnesses_are_not_the_only_one_wrong():
    # C7: with DAI empty, every word Chandra and Churro agreed on was "only DAI wrong,
    # resisted". An absent witness is counted apart; a witness that read the page but
    # wrote nothing in the place is "only-omitted", not "only-wrong".
    def w(right, form, read=True):
        return {"right": right, "form": form, "read": read}

    row = {"reader_right": [True, True], "reader_form": [None, None], "ref_words": ("a", "b"),
           "witnesses": {"chandra": w([True, True], [None, None]),
                         "dai": w([False, False], [None, None], read=False),
                         "churro": w([True, False], [None, None])}}  # fmt: skip
    c = C.page_counts(row)
    assert c["only-wrong:dai"] == 0 and c["absent:dai"] == 2
    assert c["all-right"] == 1 and (c["only-omitted:churro"], c["only-wrong:churro"]) == (1, 0)
    row["witnesses"]["churro"]["form"] = [None, "x"]
    assert C.page_counts(row)["only-wrong:churro,resisted"] == 1


def test_failures_count_and_intervals_resample_pages(tmp_path):
    # C7: missing answers were left out of even the "all" metrics, and rates were read
    # as if every word were independent.
    tree = F.load_run_tree(
        make_run_tree(tmp_path / "run", pages=(("Le dix mai", "Le dix mai"),) * 3)
    )
    answers = C.answers_from_run_tree(tree)
    gold = {s: parse_gold(GOLD.format(stem=s, status="x", text="Le dix mai"), s)
            for s in ("p001", "p002", "p003")}  # fmt: skip
    del answers["p003"]
    card = C.scorecard(answers, gold, set(), expected={"p001", "p002", "p003"})
    s = card["all"]
    assert (s["pages"], s["parsed"], s["failed"], s["errors"]) == (3, 2, 1, 1)
    assert max(s["cer_all_pages"]) == 1.0 and card["missing"] == ["p003"]
    assert C.scorecard(answers, gold, set())["all"]["pages"] == 2  # nothing expected
    pages = [Counter({"n": 1, "d": 2}), Counter({"n": 2, "d": 2}), Counter({"n": 0, "d": 2})] * 10
    lo, hi = C.boot_ratio(pages, "n", "d")
    assert lo < 0.5 < hi and C.boot_ratio(pages, "n", "d") == (lo, hi)  # seeded
    diff, interval = C.boot_paired(pages, pages, "n", "d")
    assert diff == 0 and interval == (0.0, 0.0)
    lines = C.paired_report(card, card, ("A", "B"))
    assert any("both parsed 2" in line for line in lines)


def test_lead_checked_only_for_an_explicit_checked_status():
    # C7: any status without "fool" (silver, draft, unknown) was labelled lead-checked,
    # and a comparison inherited the first set's label.
    def page(status):
        return parse_gold(GOLD.format(stem="p", status=status, text="Le dix"), "p")

    assert C.reference_label([page("gold (Tyrel 2026-10-07)"), page("lead-checked")]) == (
        " (lead-checked gold)"
    )
    for statuses in (["silver"], ["draft"], [""], ["gold (Tyrel)", "silver"]):
        assert C.reference_label([page(s) for s in statuses]) != " (lead-checked gold)"
    assert C.reference_label([page("gold (Tyrel)"), page("fool's gold")]) == f" {FOOLS_GOLD_LABEL}"
    assert not C.is_checked_status("fool's gold") and C.is_checked_status("Gold")
