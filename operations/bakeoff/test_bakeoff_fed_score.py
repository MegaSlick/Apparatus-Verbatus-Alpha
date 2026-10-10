"""The Perlector scorecard on synthetic pages, readings and gold (never real transcriptions)."""

import json
import sys
from collections import Counter

import pytest

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
    # DAI read nothing: absent, never "the only one wrong".
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


def test_act_entries_are_counted_by_their_act_class(tmp_path):
    # An instrument counts as an act and an index row does not.
    tree = F.load_run_tree(make_run_tree(tmp_path / "run"))
    answers = C.answers_from_run_tree(tree)
    gold = {
        "p001": parse_gold(GOLD.format(stem="p001", status="x", text="Le dix mai mil huit"), "p001")
    }
    assert F.reading_text(answers["p001"].answer) == "Le dix mai mil"
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
        "--model-name", "m", "--accept-new-model-name", "--weights", str(tree), "--vllm-cmd", sys.executable, str(FAKE),
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
    # `Jean Paul` read as `Jeanne`: one emitted word is aligned to one gold word, so it
    # never counts as two copied errors.
    right, form, inserted = C.aligned(("Jean", "Paul"), ("Jeanne",))
    assert right == [False, False] and form == ["Jeanne", None] and inserted == 0
    right, form, _ = C.aligned(("le", "Jean", "Paul", "fils"), ("le", "Paule", "Jeanne", "fils"))
    assert form[1:3] in (["Paule", "Jeanne"], ["Jeanne", None], [None, "Jeanne"])
    assert len([f for f in form if f is not None]) == len(set(f for f in form if f is not None))


def test_absent_witnesses_are_not_the_only_one_wrong():
    # An absent witness is counted apart, never as "only wrong"; a witness that read the page but
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
    # Missing answers count in the "all" metrics, and rates are not read as if every
    # word were independent.
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
    # Only a checked status is labelled lead-checked (not silver, draft or unknown), and
    # a comparison does not inherit the first set's label.
    def page(status):
        return parse_gold(GOLD.format(stem="p", status=status, text="Le dix"), "p")

    assert C.reference_label([page("gold (Tyrel 2026-10-07)"), page("lead-checked")]) == (
        " (lead-checked gold)"
    )
    for statuses in (
        ["silver"],
        ["draft"],
        [""],
        ["gold (Tyrel)", "silver"],
        ["gold (unchecked draft)"],
    ):
        assert C.reference_label([page(s) for s in statuses]) != " (lead-checked gold)"
    assert C.reference_label([page("gold (Tyrel)"), page("fool's gold")]) == f" {FOOLS_GOLD_LABEL}"
    assert not C.is_checked_status("fool's gold")
    assert C.is_checked_status("Gold (Tyrel 2026-10-07)")
    # Only a named checker and a date make gold checked.
    for status in (
        "gold",
        "gold (unchecked draft)",
        "gold (AI draft)",
        "gold (Tyrel 2026-99-99)",
        "gold (Tyrel 2026-02-30)",
        "gold ()",
        "gold (2026-10-07)",
    ):
        assert not C.is_checked_status(status), status


def _fed_run(tree, cache, label, *extra):
    argv = [
        "run", "--run-tree", str(tree), "--out", str(cache), "--label", label,
        "--model-name", "m", "--accept-new-model-name", "--weights", str(tree), "--vllm-cmd", sys.executable, str(FAKE),
        "--port", str(_free_port()), "--startup-timeout", "60", *extra,
    ]  # fmt: skip
    assert F.main(argv) == 0


def _retarget(src, dst, **setup_changes):
    """Copy a cache folder with some of its recorded setup fields changed."""
    dst.mkdir()
    for path in src.glob("*.json"):
        record = json.loads(path.read_text())
        if record.get("schema") == F.SCHEMA:
            record["setup"].update(setup_changes)
        (dst / path.name).write_text(json.dumps(record))
    return dst


def test_compare_refuses_answer_sets_that_were_asked_differently(tmp_path, capsys):
    tree = make_run_tree(tmp_path / "run", pages=(("Le dix mai", "Le dix mai"),))
    (tree / "config.json").write_text("{}")
    cache = tmp_path / "cache"
    _fed_run(tree, cache, "a")
    gold = _gold(tmp_path / "gold", {"p001": "Le dix mai"})
    base = ["--run-tree", str(tree), "--gold", str(gold), "--gold-glob", "*.txt"]

    def compare(other):
        return C.main([*base, "--answers", str(cache / "a"), "--compare", str(other),
                       "--names", "A,B"])  # fmt: skip

    # Another model, checkpoint and recipe is the point of a comparison: allowed, and
    # each set's identity is printed so swapped --names show.
    other = _retarget(
        cache / "a", tmp_path / "b", model_name="other", repo="x/y", revision="r2", recipe="fp8"
    )
    assert compare(other) == 0
    printed = capsys.readouterr().out
    assert "- B: x/y@r2, recipe fp8, served other, sampling greedy seed 0" in printed
    for key, value in (
        ("sampling_name", "sealed"), ("seed", 7), ("max_tokens", 100),
        ("decoding_sha256", "0" * 64), ("variant", {"changed": True}),
        ("run", {"run_sha256": "0" * 64}), ("stream", False),
    ):  # fmt: skip
        with pytest.raises(SystemExit) as refused:
            compare(_retarget(cache / "a", tmp_path / f"b-{key}", **{key: value}))
        assert refused.value.code != 0
        assert "refusing to compare" in capsys.readouterr().err
    # A mutation is part of what was asked, per page.
    mutated = tmp_path / "b-mutation"
    mutated.mkdir()
    for path in (cache / "a").glob("*.json"):
        record = json.loads(path.read_text())
        if record.get("schema") == F.SCHEMA:
            record["setup"]["page"] = {"mutation": {"scenario": "other"}}
        (mutated / path.name).write_text(json.dumps(record))
    with pytest.raises(SystemExit):
        compare(mutated)
    assert "mutation differs" in capsys.readouterr().err
    # The run's own readings carry no setup: only the run tree is checked.
    assert compare(tree) == 0
    elsewhere = make_run_tree(tmp_path / "run2", pages=(("Le dix mai", "Le dix mai"),))
    (elsewhere / "config.json").write_text("{}")
    with pytest.raises(SystemExit):
        compare(elsewhere)


def test_one_cache_folder_may_not_mix_model_identities(tmp_path, capsys):
    tree = make_run_tree(tmp_path / "run", pages=(("Le dix mai", "Le dix mai"),) * 2)
    (tree / "config.json").write_text("{}")
    cache = tmp_path / "cache"
    _fed_run(tree, cache, "a")
    mixed = _retarget(cache / "a", tmp_path / "mixed")
    record = json.loads((mixed / "p002.json").read_text())
    record["setup"]["revision"] = "another"
    (mixed / "p002.json").write_text(json.dumps(record))
    gold = _gold(tmp_path / "gold", {"p001": "Le dix mai", "p002": "Le dix mai"})
    argv = ["--run-tree", str(tree), "--gold", str(gold), "--gold-glob", "*.txt"]
    with pytest.raises(SystemExit):
        C.main([*argv, "--answers", str(cache / "a"), "--compare", str(mixed)])
    assert "mixes setups" in capsys.readouterr().err


def test_caches_of_a_real_run_tree_compare_against_it(tmp_path, monkeypatch):
    # A run tree with run.json is loaded sealed by fed_arm; the scorer must load it alike,
    # or its identity differs from the one the caches recorded and every cache is refused.
    tree = make_run_tree(tmp_path / "run", pages=(("Le dix mai", "Le dix mai"),))
    (tree / "config.json").write_text("{}")
    (tree / "run.json").write_text(json.dumps({"run_id": "run"}))
    monkeypatch.setattr(F, "verify_seals", lambda root: None)
    cache = tmp_path / "cache"
    _fed_run(tree, cache, "a")
    _fed_run(tree, cache, "b")
    gold = _gold(tmp_path / "gold", {"p001": "Le dix mai"})
    argv = ["--run-tree", str(tree), "--gold", str(gold), "--gold-glob", "*.txt"]
    assert C.main([*argv, "--answers", str(cache / "a"), "--compare", str(cache / "b")]) == 0
    assert C.main([*argv, "--answers", str(cache / "a"), "--compare", str(tree)]) == 0


def test_repaired_replies_are_counted_in_answer_health(tmp_path):
    tree = make_run_tree(tmp_path / "run", pages=(
        ("Le dix mai mil", "Le dix mars mil huit"),
        ("Le onze juin", "BARE-TEST"),
    ))  # fmt: skip
    (tree / "config.json").write_text("{}")
    cache = tmp_path / "cache"
    _fed_run(tree, cache, "bare", "--sampling", "sealed")
    answers = C.answers_from_cache(cache / "bare")
    assert [answers[s].repaired for s in ("p001", "p002")] == [False, True]
    gold = _gold(tmp_path / "gold", {"p001": "Le dix mai", "p002": "Le dix mai"})
    out = tmp_path / "card"
    assert C.main(["--run-tree", str(tree), "--answers", str(cache / "bare"), "--gold",
                   str(gold), "--gold-glob", "*.txt", "--out", str(out)]) == 0  # fmt: skip
    card = json.loads((out / "scorecard.json").read_text())
    assert card["answers"]["groups"]["acts-handwritten"]["repaired"] == 1
    assert (
        "- repaired before parsing (bare grammar keys quoted): 1 pages"
        in (out / "scorecard.md").read_text()
    )
    # The run's own readings carry their repairs the same way.
    path = tree / "4_perlector/artifacts/page-reading/r2.json"
    record = json.loads(path.read_text())
    record["payload"]["answer_repairs"] = [{"code": "unquoted-keys-quoted", "keys": 3}]
    path.write_text(json.dumps(record))
    assert C.answers_from_run_tree(F.load_run_tree(tree))["p002"].repaired is True
