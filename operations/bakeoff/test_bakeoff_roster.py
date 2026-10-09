"""Roster metrics on synthetic gold and readings (invented words, never real text)."""

import json
import math

import pytest

from operations.bakeoff import roster as R
from operations.bakeoff.test_bakeoff_score import ROWS, gold_text, write_gold, write_reading

WORDS = ["alba", "bruma", "caelum", "dolor", "equus", "flumen", "gemma", "hortus", "ignis", "lapis"]


def test_align_marks_each_gold_token_and_collects_insertions():
    correct, inserted = R.align(("Anno", "domini", "vigesimo"), ("Anno", "dommi", "vigesimo", "x"))
    assert correct == [True, False, True] and inserted == ["x"]
    assert R.align(("a", "b"), ()) == ([False, False], [])
    assert R.align((), ("a",)) == ([], ["a"])


def test_phi_hand_computed_and_degenerate():
    x = [True, True, False, False, True, False]
    y = [True, False, False, False, True, True]
    assert R.phi(x, y) == pytest.approx(1 / 3)  # n11=2 n10=1 n01=1 n00=2: 3/9
    assert R.phi(x, x) == pytest.approx(1)
    assert R.phi([True, True], [True, False]) is None  # a constant vector


def test_rescue_and_shared_fabrication_and_union():
    baselines = [[1, 1, 1, 0], [1, 1, 0, 0], [1, 1, 1, 1]]
    assert R.rescue_rate([0, 1, 0, 0], baselines) == (0.5, 2)
    assert R.rescue_rate([1, 1], [[0, 0]]) == (None, 0)
    share, each = R.shared_fabrication([["x", "y", "x"]], {"b1": [["x"]], "b2": [["y", "z"]]})
    assert share == pytest.approx(2 / 3) and each == {"b1": 1 / 3, "b2": 1 / 3}
    assert R.shared_fabrication([[]], {"b1": [["x"]]})[0] is None
    rows = [r.replace(" | ", " ") for r in ROWS]
    two = [rows[:1], rows[1:2]]
    assert R.union_line_recall([(rows, two)]) == pytest.approx(2 / 3)
    assert R.union_line_recall([(rows, [*two, rows[2:]])]) == 1


def test_leader_lowest_and_rule():
    headline = {"a": 0.10, "b": 0.15, "c": 0.30}
    leader, lowest = R.leader_and_lowest(headline, {"a": 0.2, "b": 0.05, "c": 0.0}, "cer")
    assert (leader, lowest) == ("a", ["b"])  # c invents least but is 20 points behind
    assert R.leader_and_lowest({"a": 0.5, "b": 0.45}, {"a": 0.1, "b": 0.2}, "line_recall")[0] == "a"
    assert R.top_two({"a": 0.9, "b": 0.5, "c": 0.5, "d": 0.1, "e": None}) == {"a", "b", "c"}
    row = {"candidate": "a", "correlation": {"x": 0.1, "y": 0.2}}
    assert R.suggest(row, 0.3, {"a"}, set()).startswith("earns")
    assert R.suggest(row, 0.15, {"a"}, set()) == "no"
    assert R.suggest(row, None, {"a"}, set()) == "no"
    assert "lowest invention" in R.suggest(row, 0.15, set(), {"a"})


def _reading(wrong, extra=()):
    """WORDS with the tokens at `wrong` misread and `extra` inserted between two right ones."""
    tokens = [("x" + w if i in wrong else w) for i, w in enumerate(WORDS)]
    return " ".join(tokens[:7] + list(extra) + tokens[7:])


def test_roster_on_a_constructed_case(tmp_path):
    pages = {
        "a001": gold_text("acts-19c", acts=[" ".join(WORDS)]),
        "i001": gold_text("index", rows=ROWS),
        "t001": gold_text("index", rows=ROWS, test="yes: anything"),
    }
    write_gold(tmp_path / "gold", pages)
    cache = tmp_path / "cache"
    acts = {
        ("chandra", "chandra-native"): _reading({0, 1, 8, 9}, ["fictum"]),
        ("dai", "dai"): _reading({0, 2, 8, 9}),
        ("churro", "churro-native"): _reading({1, 2, 8, 9}),
        ("cand-a", "party-blla"): _reading({3, 4}),
        ("cand-b", "surya-rec-surya"): _reading({0, 1, 2, 8}, ["fictum"]),
        ("cand-c", "kraken-ppocrv6-blla"): _reading({0, 1, 2, 3, 4, 5, 8, 9}, ["fictum", "aliud"]),
    }
    rows = [r.replace(" | ", " ") for r in ROWS]
    index = {"chandra": rows[0], "dai": "", "churro": rows[1], "cand-a": "\n".join(rows)}
    for (model, arm), text in acts.items():
        write_reading(cache, model, arm, "a001", text)
        write_reading(cache, model, arm, "i001", index.get(model, ""))
        write_reading(cache, model, arm, "t001", "")
    argv = ["--cache", str(cache), "--gold", str(tmp_path / "gold"), "--out", str(tmp_path)]
    assert R.main(argv) == 0
    out = [json.loads(x) for x in (tmp_path / "roster.jsonl").read_text().splitlines()]
    base = next(r for r in out if r["group"] == "acts" and r["kind"] == "baselines")
    assert base["baseline_pairwise"]["chandra~dai"] == pytest.approx(14 / 24)
    assert base["baseline_pairwise_min"] == pytest.approx(14 / 24)
    acts_rows = {r["candidate"]: r for r in out if r["group"] == "acts" and "candidate" in r}
    a, b, c = acts_rows["cand-a"], acts_rows["cand-b"], acts_rows["cand-c"]
    assert (a["rescue_rate"], b["rescue_rate"], c["rescue_rate"]) == (1.0, 0.5, 0.0)
    assert a["correlation"]["chandra"] == pytest.approx(-8 / math.sqrt(384))
    assert b["correlation"]["dai"] == pytest.approx(14 / 24)  # not below the baselines'
    assert a["suggestion"].startswith("earns") and "rescue" in a["suggestion"]
    assert b["top_two_rescue"] and b["suggestion"] == "no"
    assert c["suggestion"] == "no"
    assert a["shared_fabrication"] is None
    assert (b["shared_fabrication"], c["shared_fabrication"]) == (1.0, 0.5)
    assert c["insertion_rate"] == pytest.approx(2 / 10)
    assert "dai" in base["lowest_invention"] and "invention" not in a["suggestion"]  # a tie
    # DAI is no witness off act pages: its empty index reading must not blank the
    # correlations (an all-wrong vector has no phi) and so block the rescue route.
    index_base = next(r for r in out if r["group"] == "index-list" and r["kind"] == "baselines")
    assert index_base["baselines"] == ["chandra", "churro"]
    assert list(index_base["baseline_pairwise"]) == ["chandra~churro"]
    assert index_base["baseline_pairwise_min"] is not None
    union = next(r for r in out if r["group"] == "index-list" and r.get("candidate") == "cand-a")
    assert set(union["correlation"]) == {"chandra", "churro"}
    assert union["union_line_recall_baselines"] == pytest.approx(2 / 3)
    assert union["union_line_recall_with"] == 1
    assert not any(r["group"] == "test" for r in out)
    md = (tmp_path / "roster.md").read_text()
    assert "## acts" in md and "## index-list" in md and "| cand-a | 1 | 1.000 |" in md
    assert "2/3" not in md and "0.667 → 1.000" in md
    assert "never a decision" in md


def test_missing_baseline_is_refused(tmp_path):
    write_gold(tmp_path / "gold", {"a001": gold_text("acts-19c", acts=["alba"])})
    write_reading(tmp_path / "cache", "chandra", "chandra-native", "a001", "alba")
    argv = ["--cache", str(tmp_path / "cache"), "--gold", str(tmp_path / "gold")]
    assert R.main([*argv, "--out", str(tmp_path)]) == 2
