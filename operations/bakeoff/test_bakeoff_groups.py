"""The fairness table: lookups, pattern arms and the validator."""

import pytest

from operations.bakeoff import groups as G


def test_every_category_has_one_group_and_the_table_validates():
    G.validate()
    assert G.group_of_category("acts-18c") == "acts"
    assert G.group_of_category("list") == "index-list"
    assert G.group_of_category("non-register") == "blank-like"
    assert G.group_of_category("nonsense") is None
    assert G.group("index-list").headline == "line_recall"


def test_groups_for_arms_and_patterns():
    assert G.groups_for("dai")[0] == ("acts", G.TEST)
    names, reason, known = G.groups_for("pylaia-popp-surya")
    assert known and set(names) == {"index-list", "tables", "acts", G.TEST}
    assert "census" in reason
    assert G.groups_for("pylaia-belfort-lm-blla")[0] == G.EVERY
    assert G.groups_for("not-an-arm") == (G.EVERY, G.UNKNOWN_ARM_REASON, False)
    assert G.is_known_arm("pylaia-belfort-blla") and not G.is_known_arm("label-x")


def test_validate_refuses_unknown_category_and_arm(monkeypatch):
    bad = G.Group("odd", ("nonsense",), "cer", ())
    monkeypatch.setattr(G, "GROUPS", (*G.GROUPS, bad))
    with pytest.raises(ValueError, match="unknown category 'nonsense'"):
        G.validate()
    monkeypatch.undo()
    monkeypatch.setattr(G, "MODEL_GROUPS", {**G.MODEL_GROUPS, "mystery": (G.EVERY, "x")})
    with pytest.raises(ValueError, match="unknown arm 'mystery'"):
        G.validate()
    monkeypatch.setattr(G, "MODEL_GROUPS", {"dai": (("nowhere",), "x")})
    with pytest.raises(ValueError, match="unknown group 'nowhere'"):
        G.validate()
