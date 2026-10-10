"""The training exporter on synthetic pages, feeds and gold (never a real page or text)."""

import json

import pytest

from common import page_prompt
from common.page_answer import parse_page_answer
from operations.bakeoff import fed_arm as F
from operations.bakeoff import mutations as M
from operations.bakeoff.gold import parse_gold
from operations.bakeoff.test_bakeoff_fed_arm import make_run_tree
from operations.bakeoff.test_bakeoff_fed_score import GOLD
from operations.training import export as E

TEXT = "Le dix mai mil huit cent Richer Lalonde"
PAGES = ((TEXT, TEXT), ("Le onze juin", "Le onze juin"), ("Le douze mai", "Le douze mai"))
TEXTS = {"p001": TEXT, "p002": "Le onze [[juin|juillet]]", "p003": "Le douze mai"}


def _refs(tree, texts=TEXTS, status="fool's gold"):
    refs = {}
    for stem, text in texts.items():
        ref = M.reference_from_gold(
            parse_gold(GOLD.format(stem=stem, status=status, text=text), stem)
        )
        M.statuses_from_agreement(ref, tree.by_stem()[stem].feed)
        refs[stem] = ref
    return refs


def _examples(out):
    return [json.loads(line) for line in (out / "train.jsonl").read_text().splitlines()]


def test_held_out_list_parsing_and_sibling_halves(tmp_path):
    listing = tmp_path / "held.txt"
    listing.write_text("# frozen\nVolume_1_00030_1L.tif  # one half\n\np002\n")
    held = E.read_held_out(listing)
    assert held == {"Volume_1_00030_1L", "p002"}
    assert E.is_held_out("Volume_1_00030_2R", held)  # the other half of the same original
    assert E.is_held_out("p002", held) and not E.is_held_out("p003", held)
    assert E.original_of("X_00030_2R") == "X_00030" and E.original_of("p002") == "p002"


def test_export_honours_the_held_out_list_and_reuses_the_perlector_prompt(tmp_path):
    tree = F.load_run_tree(make_run_tree(tmp_path / "run", pages=PAGES))
    refs = _refs(tree)
    out = tmp_path / "out"
    manifest = E.export(tree, refs, {"p002"}, out, seed=1, variants_per_page=5, blinded_share=0.0)
    assert manifest["pages"] == 2 and manifest["held_out_excluded"] == ["p002"]
    examples = _examples(out)
    assert len(examples) == 10 and {e["page"] for e in examples} == {"p001", "p003"}
    sidecars = [json.loads(line) for line in (out / "planted.jsonl").read_text().splitlines()]
    assert [s["id"] for s in sidecars] == [e["id"] for e in examples]
    for example, sidecar in zip(examples, sidecars, strict=True):
        assert example["schema"] == E.SCHEMA and example["images"] == [
            f"images/{example['page']}.png"
        ]
        assert (out / example["images"][0]).is_file()
        user, assistant = example["messages"]
        assert user["role"] == "user" and user["content"][0] == {"type": "image"}
        # The prompt is the Perlector's own rendering of the shown feed, byte for byte.
        feed = sidecar_feed(out, example, sidecar)
        expected = page_prompt.build_page_prompt(feed["prompt"]["serving_recipe"], feed)
        assert user["content"][1]["text"] == expected
        body, text = F.build_body(feed, b"png", model_name="x", sampling=F.GREEDY, seed=0,
                                  max_tokens=1, stream=False)  # fmt: skip
        assert text == expected
        if example["scenario"] == "honest":
            assert example["prompt_matches_run"] is True
        else:
            assert example["prompt_matches_run"] is None
        assert example["chat_template_kwargs"] == {"enable_thinking": False}
        state, answer, problems = parse_page_answer(assistant["content"])
        assert state == "parsed", problems
        assert example["witness_regime"] == "named"
    assert manifest["honest_prompts_identical_to_run"] == manifest["honest_prompts_checked"] > 0
    assert manifest["tokens"]["prompt_tokens_bound"]["max"] > 0
    assert manifest["vote_check"]["all"]["spans"] >= manifest["vote_check"]["honest"]["spans"]


def sidecar_feed(out, example, sidecar, held=frozenset({"p002"})):
    """Rebuild the shown feed from the honest run feed and the sidecar is not possible for
    every scenario; the exporter does not store feeds, so the test regenerates it."""
    tree = F.load_run_tree(out.parent / "run")
    page = tree.by_stem()[example["page"]]
    refs = _refs(tree)
    m = M.mutate(page.feed, refs[page.stem], sidecar["scenario"], seed=sidecar["seed"],
                 turn=sidecar["turn"], stem=page.stem,
                 donors=M.donor_acts({k: v for k, v in refs.items() if not E.is_held_out(k, held)},
                                     page.stem))  # fmt: skip
    if any(c["kind"] == "blinded" for c in sidecar["changes"]):
        M.blind_labels(m, sidecar["seed"])
    return m.feed


def test_loss_spans_follow_word_status_and_tile_the_answer(tmp_path):
    tree = F.load_run_tree(make_run_tree(tmp_path / "run", pages=PAGES))
    refs = _refs(tree)
    ref = refs["p001"]
    ref.words[6].status = "checked"  # Richer
    ref.words[7].status = "draft"  # Lalonde
    feed = tree.pages[1].feed
    answer = E.build_answer(ref, feed, [])
    answer_json = json.dumps(answer, ensure_ascii=False)
    spans = E.loss_spans(answer_json, answer, ref, E.CITES_WEIGHT)
    assert spans[0][0] == 0 and spans[-1][1] == len(answer_json)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:], strict=False))
    weighted = {answer_json[s:e]: w for s, e, w in spans if w != 1.0}
    assert "Richer" not in weighted  # a checked word carries 1.0, like the scaffold
    assert weighted["Lalonde"] == 0.3 and weighted["dix"] == 0.7
    assert weighted[json.dumps(answer["acts"][0]["cites"])] == E.CITES_WEIGHT
    # A doubtful reading is unresolved: weight 0 (p002 has [[juin|juillet]]).
    ref2 = refs["p002"]
    answer2 = E.build_answer(ref2, tree.pages[2].feed, [])
    j2 = json.dumps(answer2, ensure_ascii=False)
    weights2 = {j2[s:e]: w for s, e, w in E.loss_spans(j2, answer2, ref2, 1.0)}
    assert weights2["juin"] == 0.0 and weights2["onze"] == 0.7
    # Escapes inside the JSON string do not shift the word offsets.
    ref3 = M.Reference("x", "silver", [{"kind": "act", "label": None, "text": 'dit "Le Roi"\nfils',
                                        "continues_from_previous_page": False,
                                        "continues_to_next_page": False}])  # fmt: skip
    for t in ("dit", '"Le', 'Roi"', "fils"):
        ref3.words.append(M.RefWord(t, "checked", "word", 0))
    answer3 = {"acts": [{"n": 1, "kind": "act", "cites": [], "text": ref3.entries[0]["text"],
                         "continues_from_previous_page": False, "continues_to_next_page": False}],
               "set_aside": []}  # fmt: skip
    j3 = json.dumps(answer3, ensure_ascii=False)
    words = [
        j3[s:e]
        for s, e, w in E.loss_spans(j3, answer3, ref3, 1.0)
        if w == 1.0 and j3[s:e] in ("dit", '\\"Le', 'Roi\\"', "fils")
    ]
    assert words == ["dit", '\\"Le', 'Roi\\"', "fils"]


def test_build_answer_cites_every_shown_id_or_sets_it_aside(tmp_path):
    tree = F.load_run_tree(make_run_tree(tmp_path / "run", pages=PAGES))
    refs = _refs(tree)
    page = tree.pages[1]
    m = M.mutate(page.feed, refs["p001"], "invented-act", seed=0, donors=["Le deux juin mil huit"])
    answer = E.build_answer(refs["p001"], m.feed, m.set_aside_ids)
    shown = {u["id"] for r in m.feed["witnesses"] for u in r["units"]}
    shown |= {line["id"] for line in m.feed["surya"]["lines"]} | {
        b["id"] for b in m.feed["surya"]["blocks"]
    }
    cited = {c for e in answer["acts"] for c in e["cites"]}
    aside = {s["id"] for s in answer["set_aside"]}
    assert cited | aside == shown and not cited & aside
    assert set(m.set_aside_ids) <= aside
    assert {s["reason"] for s in answer["set_aside"] if s["id"] in m.set_aside_ids} == {
        "not on the page"
    }
    assert answer["acts"][0]["cites"][:2] == ["A1", "C1"] and "L1" in answer["acts"][0]["cites"]
    # "Folio 1" matches no entry: set aside, never cited as the act's text.
    assert "A2" in aside
    # A reference that brings its own cites keeps them and weighs them fully.
    own = M.Reference("p001", "silver", [{"kind": "act", "label": "b", "text": TEXT, "cites": ["A1", "L1"],
                                          "continues_from_previous_page": False,
                                          "continues_to_next_page": True}])  # fmt: skip
    answer = E.build_answer(own, page.feed, [])
    assert (
        answer["acts"][0]["cites"] == ["A1", "L1"] and answer["acts"][0]["continues_to_next_page"]
    )


def test_reference_json_loader():
    record = {
        "schema": E.REFERENCE_SCHEMA, "stem": "p001", "status_label": "silver",
        "entries": [{"kind": "act", "text": "Le dix mai",
                     "words": [{"text": "Le", "status": "checked"}, {"text": "dix", "status": "agreed"},
                               {"text": "mai", "status": "unresolved", "cls": "date"}]},
                    {"kind": "other", "label": "heading", "text": "Baptêmes 1841"}],
    }  # fmt: skip
    ref = E.reference_from_json(record)
    assert [w.status for w in ref.words] == ["checked", "agreed", "unresolved", "draft", "draft"]
    assert [w.cls for w in ref.words] == ["word", "number", "date", "name", "date"]
    assert ref.entries[1]["kind"] == "other" and ref.entries[1]["cites"] is None
    bad = {
        **record,
        "entries": [{"kind": "act", "text": "x", "words": [{"text": "x", "status": "sure"}]}],
    }
    with pytest.raises(SystemExit):
        E.reference_from_json(bad)
    with pytest.raises(SystemExit):
        E.reference_from_json({**record, "schema": "other"})
    checked = E.reference_from_json({**record, "status_label": "lead-checked",
                                     "entries": [{"kind": "act", "text": "Le dix"}]})  # fmt: skip
    assert {w.status for w in checked.words} == {"checked"}


def test_cli_end_to_end_with_mix_and_blinding(tmp_path):
    tree = make_run_tree(tmp_path / "run", pages=PAGES)
    gold = tmp_path / "gold"
    gold.mkdir()
    for stem, text in TEXTS.items():
        (gold / f"{stem}.txt").write_text(GOLD.format(stem=stem, status="fool's gold", text=text))
    held = tmp_path / "held.txt"
    held.write_text("p003\n")
    out = tmp_path / "out"
    argv = ["--run-tree", str(tree), "--gold", str(gold), "--gold-glob", "*.txt", "--held-out", str(held),
            "--out", str(out), "--seed", "7", "--variants-per-page", "6",
            "--mix", "honest=1,plant-1=1,permute=1", "--blinded-share", "1.0"]  # fmt: skip
    assert E.main(argv) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert (
        manifest["pages"] == 2
        and manifest["examples"] == 12
        and manifest["held_out_excluded"] == ["p003"]
    )
    assert set(manifest["by_scenario"]) <= {"honest", "plant-1", "permute"}
    assert manifest["blinded_examples"] == 12 and manifest["honest_prompts_checked"] == 0
    examples = _examples(out)
    assert all(e["witness_regime"] == "blinded" for e in examples)
    assert all("witness-" in e["messages"][0]["content"][1]["text"] for e in examples)
    assert manifest["planted"].get("sites", 0) == sum(e["planted_sites"] for e in examples) > 0
    with pytest.raises(SystemExit):
        E.parse_mix("honest=1,no-such=2")
    assert E.parse_mix(None) == M.DEFAULT_MIX and sum(M.DEFAULT_MIX.values()) == 100


SECRET = "Le vingt mai mil huit cent Secret Heldout fils de Pierre Heldout"


def test_donor_acts_never_come_from_held_out_pages_or_sibling_halves(tmp_path):
    # C7 P1: invented acts and injections borrowed text from every reference, held-out
    # pages included. p001's only possible donors here are held out (p002_1L) or its own
    # sibling half (p001_2R is not a donor for p001_1L).
    pages = ((TEXT, TEXT), (SECRET, SECRET), (SECRET, SECRET))
    tree = F.load_run_tree(make_run_tree(tmp_path / "run", pages=pages))
    stems = {1: "p001_1L", 2: "p002_1L", 3: "p001_2R"}
    for ordinal, stem in stems.items():
        tree.pages[ordinal].stem = stem
    texts = {"p001_1L": TEXT, "p002_1L": SECRET, "p001_2R": SECRET.replace("Secret", "Sibling")}
    refs = {}
    for stem, text in texts.items():
        refs[stem] = M.reference_from_gold(
            parse_gold(GOLD.format(stem=stem, status="fool's gold", text=text), stem)
        )
    assert M.donor_acts(refs, "p001_1L") == [" ".join(SECRET.split())]  # only p002_1L's act
    out = tmp_path / "out"
    E.export(tree, refs, {"p002_1L"}, out, seed=0, variants_per_page=8, blinded_share=0.0,
             mix={"invented-act": 1, "injection": 1}, pages=[tree.pages[1]])  # fmt: skip
    examples = _examples(out)
    assert examples and {e["scenario"] for e in examples} <= {"invented-act", "injection"}
    for example in examples:
        prompt = example["messages"][0]["content"][1]["text"]
        assert "Heldout" not in prompt and "Sibling" not in prompt


def test_targets_keep_unread_ink_and_uncertain_readings(tmp_path):
    # C7 P1: the assistant target was the scoring text, so `[[?]]` vanished and
    # `[[juin|juillet]]` became a plain `juin`. The target now keeps the doubt grammar;
    # the readings inside carry the unresolved weight (0) and the mark syntax a draft's.
    pages = ((TEXT, TEXT), ("Le onze juin", "Le onze juin"), ("Le douze mai", "Le douze mai"))
    tree = F.load_run_tree(make_run_tree(tmp_path / "run", pages=pages))
    texts = {**TEXTS, "p002": "Le onze [[?]] [[juin|juillet]]", "p003": "[[?]]"}
    refs = _refs(tree, texts)
    answer = E.build_answer(refs["p002"], tree.pages[2].feed, [])
    assert answer["acts"][0]["text"] == "Le onze [[?]] [[juin|juillet]]"
    j = json.dumps(answer, ensure_ascii=False)
    state, parsed, problems = parse_page_answer(j)
    assert state == "parsed", problems
    # The reading scores exactly as the reference's scored words.
    from operations.bakeoff import score as S

    assert (
        S.tokens(S.normalise_output("perlector", F.reading_text(parsed))) == refs["p002"].tokens()
    )
    weights = {j[s:e]: w for s, e, w in E.loss_spans(j, answer, refs["p002"], E.CITES_WEIGHT)}
    assert weights["juin"] == 0.0 and weights["onze"] == 0.7
    assert weights["[[?]]"] == weights["[["] == weights["|juillet]]"] == E.WEIGHTS["draft"]
    # A gap-only act is still an act in the target.
    gap = E.build_answer(refs["p003"], tree.pages[3].feed, [])
    assert [e["text"] for e in gap["acts"]] == ["[[?]]"]
    # A lead-checked reference weighs the mark syntax fully.
    refs["p002"].status_label = "lead-checked"
    weights = {j[s:e]: w for s, e, w in E.loss_spans(j, answer, refs["p002"], 1.0)}
    assert weights["[[?]]"] == 1.0
