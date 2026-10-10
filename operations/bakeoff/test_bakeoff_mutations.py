"""The trap generator on synthetic feeds and gold (never a real page, witness or text)."""

import json
import sys
from pathlib import Path

import pytest

from operations.bakeoff import fed_arm as F
from operations.bakeoff import fed_score as C
from operations.bakeoff import mutations as M
from operations.bakeoff import score as S
from operations.bakeoff.gold import parse_gold
from operations.bakeoff.test_bakeoff_fed_arm import _row, make_run_tree
from operations.bakeoff.test_bakeoff_fed_score import GOLD
from operations.bakeoff.test_bakeoff_runner import FAKE, _free_port

TEXT = "Le dix mai mil huit cent Richer Lalonde a St Jean ptre"


def _ref(text=TEXT, feed=None, stem="p001", status="fool's gold"):
    gold = parse_gold(GOLD.format(stem=stem, status=status, text=text), stem)
    ref = M.reference_from_gold(gold)
    if feed is not None:
        M.statuses_from_agreement(ref, feed)
    return ref


def _tree(tmp_path, pages=((TEXT, TEXT),)):
    return F.load_run_tree(make_run_tree(tmp_path / "run", pages=pages))


def _three(feed, second=TEXT):
    """The synthetic feed with its empty second witness reading too."""
    feed = json.loads(json.dumps(feed))
    feed["witnesses"][1] = _row("B", "attestator_2", [second])
    return feed


def _texts(feed):
    return {r["witness_label"]: [u["text"] for u in r["units"]] for r in feed["witnesses"]}


def test_reference_tokens_statuses_and_classes(tmp_path):
    tree = _tree(tmp_path)
    feed = tree.pages[1].feed
    gold = parse_gold(GOLD.format(stem="p001", status="x", text=TEXT), "p001")
    ref = M.reference_from_gold(gold)
    assert ref.tokens() == S.tokens(gold.reference_text())
    assert [w.cls for w in ref.words][:8] == [
        "word", "number", "date", "number", "number", "number", "name", "name",
    ]  # fmt: skip
    assert all(w.status == "draft" for w in ref.words)
    M.statuses_from_agreement(ref, feed)
    assert all(w.status == "agreed" for w in ref.words)  # both readers have every word
    # A word one witness misses stays a draft; a doubtful reading is unresolved.
    feed2 = json.loads(json.dumps(feed))
    feed2["witnesses"][2]["units"][0]["text"] = TEXT.replace("Richer", "Richet")
    ref2 = _ref(TEXT.replace("Lalonde", "[[Lalonde|Lalande]]"), feed2)
    by = {w.text: w.status for w in ref2.words}
    assert by["Richer"] == "draft" and by["Lalonde"] == "unresolved" and by["mai"] == "agreed"
    assert M.classify("1841") == "date" and M.classify("23") == "number"
    assert M.classify("Le") == "word" and M.classify("MONTREAL") == "name"


def test_mutations_are_deterministic_and_honest_is_untouched(tmp_path):
    feed = _tree(tmp_path).pages[1].feed
    ref = _ref(feed=feed)
    for scenario in M.SCENARIOS:
        a = M.mutate(feed, ref, scenario, seed=4, turn=1, stem="p001")
        b = M.mutate(feed, ref, scenario, seed=4, turn=1, stem="p001")
        assert a.record() == b.record(), scenario
        assert a.record()["schema"] == M.SCHEMA and a.family == M.FAMILY_OF[scenario]
    honest = M.mutate(feed, ref, "honest")
    assert honest.feed == feed and not honest.planted and not honest.changes
    with pytest.raises(ValueError):
        M.mutate(feed, ref, "no-such-trap")


def test_plant_one_rotates_the_chair_and_touches_only_settled_words(tmp_path):
    feed = _three(_tree(tmp_path).pages[1].feed)
    # "Richer" is a draft word (one witness has it wrong), so it is never planted on.
    feed["witnesses"][1]["units"][0]["text"] = TEXT.replace("Richer", "Richet")
    ref = _ref(feed=feed)
    touched = []
    for turn in range(3):
        m = M.mutate(feed, ref, "plant-1", seed=0, turn=turn)
        changed = [label for label, texts in _texts(m.feed).items() if texts != _texts(feed)[label]]
        assert len(changed) == 1 and m.planted
        touched.append(changed[0])
        for site in m.planted:
            assert site["witnesses"] == changed and site["k"] == 1
            assert site["status"] == "agreed" and site["ref_word"] != "Richer"
            assert site["planted"] != site["ref_word"]
            assert (
                site["planted"]
                in m.feed["witnesses"][["A", "B", "C"].index(site["letters"][0])]["units"][0][
                    "text"
                ]
            )
    assert sorted(touched) == ["attestator_1", "attestator_2", "attestator_3"]


def test_plant_two_and_three_share_one_wrong_word(tmp_path):
    feed = _three(_tree(tmp_path).pages[1].feed)
    ref = _ref(feed=feed)
    two = M.mutate(feed, ref, "plant-2", seed=1, turn=0)
    assert two.planted
    for site in two.planted:
        assert site["k"] == 2 and len(set(site["witnesses"])) == 2
        assert "attestator_1" not in site["witnesses"]  # the chair of turn 0 keeps the right word
    texts = _texts(two.feed)
    assert (
        texts["attestator_1"][0] == TEXT
        and texts["attestator_2"][0] == texts["attestator_3"][0] != TEXT
    )
    three = M.mutate(feed, ref, "plant-3", seed=1, turn=0)
    assert all(site["k"] == 3 for site in three.planted)
    t = _texts(three.feed)
    assert t["attestator_1"][0] == t["attestator_2"][0] == t["attestator_3"][0] != TEXT
    # With two readers, plant-2 makes a wrong majority of both and the sidecar's k says 2.
    pair = M.mutate(_tree(tmp_path).pages[1].feed, ref, "plant-2", seed=1, turn=0)
    assert pair.planted and all(site["k"] == 2 for site in pair.planted) and pair.notes


def test_planted_forms_look_like_reading_errors():
    import random

    rng = random.Random(0)
    pool = {"name": ["Richer", "Richet", "Lalonde", "Lalande", "Brunet"]}
    for _ in range(20):
        assert M.planted_form("mai", "date", rng, pool) in M.MONTHS_FR
        assert M.planted_form("1841", "date", rng, pool) != "1841"
        assert M.planted_form("dix", "number", rng, pool) in M.NUMBERS_FR
        assert M.planted_form("Richer,", "name", rng, pool).endswith(",")
        word = M.planted_form("Richer", "name", rng, pool)
        assert word != "Richer" and word[0].isupper()
        assert M.planted_form("baptisé", "word", rng, pool) != "baptisé"


def test_removed_witnesses(tmp_path):
    feed = _tree(tmp_path).pages[1].feed
    ref = _ref(feed=feed)
    blind = M.mutate(feed, ref, "blind")
    assert blind.feed["witnesses"] == [] and blind.changes == [{"kind": "blind"}]
    drop = M.mutate(feed, ref, "drop-one", turn=0)
    assert [r["witness_label"] for r in drop.feed["witnesses"]] == ["attestator_2", "attestator_3"]
    assert [r["letter"] for r in drop.feed["witnesses"]] == ["A", "B"]  # relettered
    failed = M.mutate(feed, ref, "failed-one", turn=1)  # turn 1 of two readers: attestator_3
    row = failed.feed["witnesses"][2]
    assert row["outcome"] == "failed" and row["units"] == []
    empty = M.mutate(feed, ref, "empty-one", turn=0)
    assert empty.feed["witnesses"][0]["outcome"] == "genuinely-empty"


def test_structural_traps(tmp_path):
    feed = _three(_tree(tmp_path).pages[1].feed)
    ref = _ref(feed=feed)
    dropped = M.mutate(feed, ref, "dropped-act", seed=0)
    assert dropped.changes[0]["kind"] == "dropped-act"
    for texts in _texts(dropped.feed).values():
        assert not any("Richer" in t for t in texts)
    invented = M.mutate(feed, ref, "invented-act", seed=0, donors=["Le deux juin mil huit cent un"])
    for row in invented.feed["witnesses"]:
        assert row["units"][-1]["text"] == "Le deux juin mil huit cent un"
        assert row["units"][-1]["id"] in invented.set_aside_ids
    merged = M.mutate(feed, ref, "merged-entries", seed=0)
    assert len(merged.feed["witnesses"][0]["units"]) == 1  # "Folio 1" joined onto the act
    assert merged.feed["witnesses"][0]["units"][0]["id"] == "A1"
    norm = M.mutate(feed, ref, "normalised")
    for texts in _texts(norm.feed).values():
        assert texts[0].endswith("a Saint Jean prêtre")
    assert {s["cls"] for s in norm.planted} == {"normalised"} and all(
        s["k"] == 3 for s in norm.planted
    )
    swap = M.mutate(feed, ref, "name-swap", seed=0)
    swapped = {_core for _core in (s["ref_word"] for s in swap.planted)}
    assert len(swapped) == 2 and swapped <= {"Richer", "Lalonde", "Jean"}
    for texts in _texts(swap.feed).values():
        assert texts[0] != TEXT and sorted(texts[0].split()) == sorted(TEXT.split())
    inject = M.mutate(feed, ref, "injection", seed=0, turn=2)
    row = inject.feed["witnesses"][2]
    assert (
        "ignore" in row["units"][-1]["text"].lower()
        or "do not read" in row["units"][-1]["text"].lower()
    )
    assert inject.set_aside_ids == [row["units"][-1]["id"]]
    assert _texts(inject.feed)["attestator_1"] == _texts(feed)["attestator_1"]
    perm = M.mutate(feed, ref, "permute", seed=0)
    assert sorted(_texts(perm.feed).items()) == sorted(_texts(feed).items())
    assert [r["letter"] for r in perm.feed["witnesses"]] != ["A", "B", "C"] or [
        r["witness_label"] for r in perm.feed["witnesses"]
    ] != ["attestator_1", "attestator_2", "attestator_3"]
    for row in perm.feed["witnesses"]:
        assert all(u["id"].startswith(row["letter"]) for u in row["units"])


def test_blank_page_and_blinded_labels(tmp_path):
    feed = _tree(tmp_path).pages[1].feed
    blank = _ref("", feed)
    assert blank.blank
    chatty = M.mutate(feed, blank, "blank-chatty", seed=0)
    assert all(r["outcome"] == "read" and r["units"] for r in chatty.feed["witnesses"])
    assert len(chatty.set_aside_ids) == 3 and not chatty.notes
    import random

    drawn = M.draw_scenarios(
        {"honest": 1, "plant-1": 1, "blind": 1}, 30, random.Random(0), blank=True
    )
    assert set(drawn) <= {"honest", "blind", "blank-chatty"} and "blank-chatty" in drawn
    m = M.mutate(feed, _ref(feed=feed), "plant-1", seed=0)
    M.blind_labels(m, seed=0)
    assert m.feed["witness_regime"] == "blinded"
    assert all(
        r["witness_label"].startswith("witness-") and r["chair"] is None
        for r in m.feed["witnesses"]
    )
    assert m.planted[0]["witnesses"] == ["attestator_1"]  # the sidecar keeps the chair


def test_vote_check_counts_wrong_majorities(tmp_path):
    feed = _three(_tree(tmp_path).pages[1].feed)
    ref = _ref(feed=feed)
    honest = M.vote_check([(ref, feed)])
    assert honest["vote_wrong"] == 0 and honest["verdict"] == "below"
    assert honest["spans"] == sum(1 for w in ref.words if w.cls != "word")
    wrong = _three(feed, second=TEXT.replace("Richer", "Richet").replace("mai", "mars"))
    wrong["witnesses"][2]["units"][0]["text"] = wrong["witnesses"][1]["units"][0]["text"]
    check = M.vote_check([(ref, wrong)])
    assert check["vote_wrong"] == 2
    assert (
        check["by_class"]["name"]["vote_wrong"] == 1
        and check["by_class"]["date"]["vote_wrong"] == 1
    )
    # A 1-1 tie between two readers that includes the right word is reported apart.
    tie = json.loads(json.dumps(feed))
    tie["witnesses"][1]["outcome"], tie["witnesses"][1]["units"] = "genuinely-empty", []
    tie["witnesses"][2]["units"][0]["text"] = TEXT.replace("Richer", "Richet")
    check = M.vote_check([(ref, tie)])
    assert check["vote_wrong"] == 0 and check["vote_tied"] == 1


def test_vote_check_and_the_scorecard_share_one_vote(tmp_path):
    # C7: gold "mai", witnesses mai / mars / juin. vote_check called the vote right (tie
    # to the first shown) while the scorecard credited a reader of "mai" with beating
    # it. Both now call it a tie, reported apart.
    feed = _three(_tree(tmp_path).pages[1].feed, second="Le dix mars")
    feed["witnesses"][0]["units"] = feed["witnesses"][0]["units"][:1]
    feed["witnesses"][0]["units"][0]["text"] = "Le dix mai"
    feed["witnesses"][2]["units"][0]["text"] = "Le dix juin"
    ref = _ref("Le dix mai", feed)
    check = M.vote_check([(ref, feed)])
    assert (check["vote_wrong"], check["vote_tied"]) == (0, 1)  # "mai"; "dix" is a clear win
    assert C.vote(["mai", "mars", "juin"], "mai") == "tie"
    assert C.vote(["mars", "mars", "mai"], "mai") == "wrong"
    assert C.vote(["mars", "juin"], "mai") == "wrong"  # a tie among wrong words is wrong
    assert C.vote(["mai", "mai", "juin"], "mai") == "right" and C.vote([], "mai") is None
    row = {"reader_right": [True], "reader_form": [None], "ref_words": ("mai",),
           "witnesses": {n: {"right": [n == "chandra"], "form": [None if n == "chandra" else f],
                             "read": True} for n, f in (("chandra", None), ("dai", "mars"),
                                                         ("churro", "juin"))}}  # fmt: skip
    c = C.page_counts(row)
    assert (c["vote-beaten"], c["vote-tie"], c["vote-tie,reader-right"]) == (0, 1, 1)


def test_name_swap_and_normalised_record_one_site_per_position(tmp_path):
    # C7: name-swap recorded each swapped position once per witness (two positions over
    # three witnesses became six sites, each k=3); plant-3 records one site per position.
    feed = _three(_tree(tmp_path).pages[1].feed)
    ref = _ref(feed=feed)
    swap = M.mutate(feed, ref, "name-swap", seed=0)
    assert len(swap.planted) == 2 and len({s["ref_index"] for s in swap.planted}) == 2
    for site in swap.planted:
        assert site["k"] == 3 and sorted(site["witnesses"]) == [
            "attestator_1", "attestator_2", "attestator_3",
        ]  # fmt: skip
        assert len(site["letters"]) == len(site["unit_ids"]) == 3
    norm = M.mutate(feed, ref, "normalised")
    indices = [s["ref_index"] for s in norm.planted]
    assert len(indices) == len(set(indices)) == 2 and all(s["k"] == 3 for s in norm.planted)
    assert swap.reference_sha256 == C.reference_digest(ref.tokens())


def test_cli_writes_records_and_the_scorer_counts_copies(tmp_path):
    tree = make_run_tree(tmp_path / "run", pages=((TEXT, TEXT), ("Le onze juin", "Le onze juin")))
    gold = tmp_path / "gold"
    gold.mkdir()
    for stem, text in (("p001", TEXT), ("p002", "Le onze juin")):
        (gold / f"{stem}.txt").write_text(GOLD.format(stem=stem, status="fool's gold", text=text))
    out = tmp_path / "plant"
    argv = ["--run-tree", str(tree), "--gold", str(gold), "--gold-glob", "*.txt",
            "--scenario", "plant-1", "--seed", "2", "--out", str(out)]  # fmt: skip
    assert M.main(argv) == 0
    summary = json.loads((out / "mutations.json").read_text())
    assert summary["pages"] == 2 and summary["planted_sites"] >= 2
    record = json.loads((out / "p001.json").read_text())
    assert record["schema"] == M.SCHEMA and record["planted"] and "feed" in record
    # fed_arm sends the mutated feed and keeps the sidecar; the scorecard reports it.
    (tree / "config.json").write_text("{}")
    cache = tmp_path / "cache"
    run = [
        "run", "--run-tree", str(tree), "--out", str(cache), "--label", "trap",
        "--model-name", "m", "--weights", str(tree), "--vllm-cmd", sys.executable, str(FAKE),
        "--port", str(_free_port()), "--startup-timeout", "60", "--mutations", str(out),
    ]  # fmt: skip
    assert F.main(run) == 0
    cached = json.loads((cache / "trap" / "p001.json").read_text())
    assert cached["mutation"]["scenario"] == "plant-1" and "feed" not in cached["mutation"]
    assert cached["feed"] == record["feed"] and cached["variant"]["mutations"] == str(out)
    card = tmp_path / "card"
    assert C.main(["--run-tree", str(tree), "--answers", str(cache / "trap"), "--gold", str(gold),
                   "--gold-glob", "*.txt", "--out", str(card)]) == 0  # fmt: skip
    text = (card / "scorecard.md").read_text()
    assert "### Planted errors" in text and "| scenario:plant-1 |" in text and "| k:1 |" in text
    data = json.loads((card / "scorecard.json").read_text())
    planted = data["answers"]["all"]["planted"]
    assert planted["all,sites"] == summary["planted_sites"]
    assert planted["all,sites"] == planted.get("all,resisted", 0) + planted.get(
        "all,copied", 0
    ) + planted.get("all,other-wrong", 0)


BOUND = {"reference_sha256": "w" * 64, "reference_record_sha256": "r" * 64}


def test_planted_copies_judges_each_site():
    rows = [
        {
            "page": "p001",
            "parsed": True,
            "reader_right": [True, False, False, True],
            "reader_form": [None, "mars", "huir", None],
            "ref_digest": BOUND["reference_sha256"],
            "reference": BOUND,
        }
    ]
    sidecar = {
        **BOUND,
        "scenario": "plant-1",
        "planted": [
            {"ref_index": 0, "planted": "La", "cls": "word", "k": 1, "witnesses": ["attestator_1"]},
            {
                "ref_index": 1,
                "planted": "mars",
                "cls": "date",
                "k": 1,
                "witnesses": ["attestator_3"],
            },
            {
                "ref_index": 2,
                "planted": "huif",
                "cls": "number",
                "k": 1,
                "witnesses": ["attestator_2"],
            },
            {"ref_index": 9, "planted": "x", "cls": "word", "k": 1, "witnesses": ["attestator_1"]},
        ],
    }
    answer = C.Answer("p001", "parsed", [], [], None, "stop", False, 1, None, None, [], sidecar)
    p = C.planted_copies(rows, {"p001": answer})
    assert (p["all,sites"], p["all,resisted"], p["all,copied"], p["all,other-wrong"]) == (
        3,
        1,
        1,
        1,
    )
    assert p["chair:churro,copied"] == 1 and p["chair:chandra,resisted"] == 1
    assert p["class:date,copied"] == 1 and p["k:1,sites"] == 3 and p["misaligned"] == 1
    assert p["misaligned:site"] == 1
    assert (
        C.planted_copies(
            rows,
            {"p001": C.Answer("p001", "parsed", [], [], None, "stop", False, 1, None, None, [])},
        )
        == {}
    )
    assert Path(FAKE).is_file()


def test_reference_keeps_doubt_marks_and_tracks_doubt_by_position(tmp_path):
    # C7 P1/P2: the entry text is the diplomatic target (marks kept); a gap-only act is
    # still an entry; the certain first "Marie" stays certain and the doubtful second one
    # is the unresolved word, so agreement can never make it plantable.
    ref = _ref("Le [[?]] [[juin|juillet]] mil huit")
    assert ref.entries[0]["text"] == "Le [[?]] [[juin|juillet]] mil huit"
    assert [(w.text, w.status) for w in ref.words] == [
        ("Le", "draft"), ("juin", "unresolved"), ("mil", "draft"), ("huit", "draft"),
    ]  # fmt: skip
    gaps = _ref("[[?]] [[?]]")
    assert [e["text"] for e in gaps.entries] == ["[[?]] [[?]]"] and gaps.words == []
    text = "Marie épouse [[Marie|Maria]]"
    feed = _three(_tree(tmp_path).pages[1].feed, second="Marie épouse Marie")
    feed["witnesses"][0]["units"][0]["text"] = "Marie épouse Marie"
    feed["witnesses"][2]["units"][0]["text"] = "Marie épouse Marie"
    ref = _ref(text, feed)
    assert [w.status for w in ref.words] == ["agreed", "agreed", "unresolved"]
    for turn in range(3):
        m = M.mutate(feed, ref, "plant-1", seed=0, turn=turn)
        assert all(site["ref_index"] != 2 for site in m.planted)
    # Donor acts are witness-like text: the marks are reduced.
    donor = _ref("Le [[?]] dix [[mai|mars]] mil huit cent Richer Lalonde fils de Pierre")
    assert M.donor_acts({"p009": donor}, "p001") == [
        "Le dix mai mil huit cent Richer Lalonde fils de Pierre"
    ]


def test_planted_sites_are_bound_to_the_scored_reference():
    # C7: any in-range index was judged, so a revised gold scored another word.
    digest = C.reference_digest(("le", "dix", "mai"))
    held = {"reference_sha256": digest, "reference_record_sha256": "r" * 64}
    rows = [{"page": "p001", "parsed": True, "reader_right": [True, True, False],
             "reader_form": [None, None, "mars"], "ref_words": ("le", "dix", "mai"),
             "ref_digest": digest, "reference": held}]  # fmt: skip
    site = {"ref_index": 2, "ref_word": "mai", "planted": "mars", "cls": "date", "k": 1,
            "witnesses": ["attestator_1"]}  # fmt: skip

    def judge(sidecar):
        answer = C.Answer("p001", "parsed", [], [], None, "stop", False, 1, None, None, [], sidecar)
        return C.planted_copies(rows, {"p001": answer})

    good = {"scenario": "plant-1", **held, "planted": [site]}
    assert judge(good)["all,copied"] == 1 and "misaligned" not in judge(good)
    moved = {**good, "planted": [{**site, "ref_index": 1}]}  # the word at 1 is "dix"
    assert judge(moved) == {"misaligned": 1, "misaligned:site": 1}
    stale = {**good, "reference_sha256": C.reference_digest(("le", "dix", "juin"))}
    assert judge(stale) == {"misaligned": 1, "misaligned:words-differ": 1}


def test_planted_sites_are_bound_to_the_whole_reference(tmp_path):
    # R3: R1 bound sites to the scored words; two references with the same words but
    # another doubt mark (so other statuses, other plantable words) now differ too.
    tree = _tree(tmp_path)
    feed = tree.pages[1].feed
    plain = _ref(TEXT, feed)
    marked = _ref(TEXT.replace("Lalonde", "[[Lalonde]]"), feed)
    assert plain.tokens() == marked.tokens()  # R1's digest cannot tell them apart
    assert C.reference_digest(plain.tokens()) == C.reference_digest(marked.tokens())
    assert plain.digest() != marked.digest()
    m = M.mutate(feed, marked, "plant-1", seed=0, turn=0, stem="p001")
    assert m.reference_record_sha256 == marked.digest() and m.sidecar()["reference_record_sha256"]
    # The scorer, holding the plain reference, judges none of the sites.
    rows = [{"page": "p001", "parsed": True, "reader_right": [True] * len(plain.words),
             "reader_form": [None] * len(plain.words), "ref_words": plain.tokens(),
             "ref_digest": C.reference_digest(plain.tokens()),
             "reference": M.reference_identities({"p001": plain})["p001"]}]  # fmt: skip
    answer = C.Answer("p001", "parsed", [], [], None, "stop", False, 1, None, None, [], m.sidecar())
    assert m.planted
    assert C.planted_copies(rows, {"p001": answer}) == {
        "misaligned": len(m.planted), "misaligned:reference-differs": len(m.planted),
    }  # fmt: skip
    rows[0]["reference"] = M.reference_identities({"p001": marked})["p001"]
    assert C.planted_copies(rows, {"p001": answer})["all,sites"] == len(m.planted)
    # A sidecar that names no whole reference, or a scorer that holds none, judges nothing.
    unbound = {k: v for k, v in m.sidecar().items() if k != "reference_record_sha256"}
    answer.mutation = unbound
    assert C.planted_copies(rows, {"p001": answer})["misaligned:reference-unbound"] == len(
        m.planted
    )
    answer.mutation = m.sidecar()
    rows[0]["reference"] = None
    assert C.planted_copies(rows, {"p001": answer})["misaligned:reference-unchecked"] == len(
        m.planted
    )


def test_fed_arm_and_scorer_refuse_a_mutation_planted_from_another_reference(tmp_path):
    # R3: the gold changed (a doubt mark added) after the mutations were planted; the
    # scored words are the same. fed_arm --gold refuses before sending; the scorer
    # judges no site.
    tree = make_run_tree(tmp_path / "run", pages=((TEXT, TEXT),))
    gold = tmp_path / "gold"
    gold.mkdir()
    page = gold / "p001.txt"
    page.write_text(GOLD.format(stem="p001", status="fool's gold", text=TEXT))
    out = tmp_path / "plant"
    assert M.main(["--run-tree", str(tree), "--gold", str(gold), "--gold-glob", "*.txt",
                   "--scenario", "plant-1", "--seed", "2", "--out", str(out)]) == 0  # fmt: skip
    record = json.loads((out / "p001.json").read_text())
    assert record["reference_sha256"] and record["reference_record_sha256"] and record["planted"]
    (tree / "config.json").write_text("{}")
    cache = tmp_path / "cache"

    def run(label):
        return F.main([
            "run", "--run-tree", str(tree), "--out", str(cache), "--label", label,
            "--model-name", "m", "--weights", str(tree), "--vllm-cmd", sys.executable,
            str(FAKE), "--port", str(_free_port()), "--startup-timeout", "60",
            "--mutations", str(out), "--gold", str(gold), "--gold-glob", "*.txt",
        ])  # fmt: skip

    assert run("same") == 0
    cached = json.loads((cache / "same" / "p001.json").read_text())
    assert (
        cached["setup"]["page"]["mutation"]["reference_record_sha256"]
        == (record["reference_record_sha256"])
    )

    def card():
        folder = tmp_path / "card"
        assert C.main(["--run-tree", str(tree), "--answers", str(cache / "same"),
                       "--gold", str(gold), "--gold-glob", "*.txt", "--out", str(folder)]) == 0  # fmt: skip
        return json.loads((folder / "scorecard.json").read_text())["answers"]["all"]["planted"]

    assert card()["all,sites"] == len(record["planted"]) and "misaligned" not in card()
    page.write_text(GOLD.format(stem="p001", status="fool's gold",
                                text=TEXT.replace("Richer", "[[Richer]]")))  # fmt: skip
    with pytest.raises(SystemExit, match="another reference"):
        run("changed")
    planted = card()
    assert "all,sites" not in planted
    assert planted["misaligned:reference-differs"] == len(record["planted"])


def test_fed_arm_refuses_a_mutation_that_names_no_reference(tmp_path):
    run = _tree(tmp_path)
    page = run.pages[1]
    m = M.mutate(page.feed, _ref(TEXT, page.feed), "honest", stem="p001")
    folder = tmp_path / "muts"
    folder.mkdir()
    variant = F.Variant(mutations=str(folder))
    record = m.record()
    (folder / "p001.json").write_text(json.dumps(record))
    assert F.load_mutation(variant, "p001", page.feed)["reference_record_sha256"]
    record.pop("reference_record_sha256")
    (folder / "p001.json").write_text(json.dumps(record))
    with pytest.raises(SystemExit, match="does not name the reference"):
        F.load_mutation(variant, "p001", page.feed)
