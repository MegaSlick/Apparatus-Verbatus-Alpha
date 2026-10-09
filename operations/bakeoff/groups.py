"""Which pages each model is scored on, and by which metric: the fairness table as data.

A model is scored only where its design applies, by the metric that fits the page type,
and never pooled across page types that mean different things. Edit the three tables
below by hand; `validate()` refuses a category or an arm it does not know.

Test pages (`TEST PAGE: yes: ...`) are their own group whatever their category: each one
is reported per page for every model, never pooled and never in another group.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

CATEGORIES = (
    "acts-18c",
    "acts-19c",
    "acts-20c",
    "index",
    "list",
    "ledger",
    "contract",
    "blank",
    "near-blank",
    "non-register",
)

# Every arm name a cache record may carry; `*` stands for the segmenter suffix.
ARMS = (
    "chandra",
    "dai",
    "churro",
    "qwen-blind",
    "qwen-vendor",
    "chandra-native",
    "churro-native",
    "dots-mocr",
    "kraken-ppocrv6-blla",
    "kraken-ppocrv6-surya",
    "pylaia-belfort-*",
    "pylaia-popp-*",
    "party-blla",
    "surya-rec-surya",
)

# metric -> (how the report names it, whether higher is better)
METRICS = {
    "cer": ("CER", False),
    "wer": ("WER", False),
    "line_recall": ("line recall", True),
    "surname_recall": ("surname recall", True),
    "false_line_rate": ("false lines", False),
    "false_text_rate": ("false text (pages with no gold text)", False),
}

TEST = "test"

# The gold FORM header, folded to these; the cross-model tables compare handwritten pages
# and, apart, typed ones. A page with no FORM counts as handwritten.
FORMS = ("handwritten", "typed", "printed form", "mixed")


def form_of(value: str) -> str:
    """One gold FORM header value as one of FORMS, or the value itself if none fits."""
    word = value.strip().lower()
    for form, cue in (("mixed", "mix"), ("typed", "typ"), ("printed form", "print")):
        if cue in word:
            return form
    if not word or "hand" in word:
        return "handwritten"
    return word


@dataclass(frozen=True)
class Group:
    name: str
    categories: tuple[str, ...]
    headline: str
    also: tuple[str, ...]


GROUPS = (
    Group("acts", ("acts-18c", "acts-19c", "acts-20c"), "cer", ("wer",)),
    Group("prose-other", ("contract",), "cer", ()),
    Group("tables", ("ledger",), "line_recall", ("cer",)),
    Group("index-list", ("index", "list"), "line_recall", ("surname_recall", "false_line_rate")),
    Group("blank-like", ("blank", "near-blank", "non-register"), "false_text_rate", ("cer",)),
    Group(TEST, (), "cer", ()),
)

EVERY = tuple(g.name for g in GROUPS)

# arm (or arm pattern) -> (the groups it is scored on, why)
MODEL_GROUPS = {
    "chandra": (EVERY, "whole-page reader"),
    "dai": (("acts", TEST), "reads records on act pages by design; empty elsewhere is no failure"),
    "churro": (EVERY, "whole-page reader"),
    "qwen-blind": (EVERY, "whole-page reader"),
    "qwen-vendor": (EVERY, "whole-page reader"),
    "chandra-native": (EVERY, "whole-page reader"),
    "churro-native": (EVERY, "whole-page reader"),
    "dots-mocr": (EVERY, "whole-page reader"),
    "kraken-ppocrv6-blla": (EVERY, "line recogniser on every line"),
    "kraken-ppocrv6-surya": (EVERY, "line recogniser on every line"),
    "pylaia-belfort-*": (EVERY, "line recogniser on every line"),
    "pylaia-popp-*": (
        ("index-list", "tables", "acts", TEST),
        "census-table model; acts for reference only",
    ),
    "party-blla": (EVERY, "line recogniser on every line"),
    "surya-rec-surya": (EVERY, "line recogniser on every line"),
}

UNKNOWN_ARM_REASON = "unknown arm: scored on every group"

_BY_NAME = {g.name: g for g in GROUPS}
_BY_CATEGORY = {c: g.name for g in GROUPS for c in g.categories}


def group(name: str) -> Group:
    return _BY_NAME[name]


def group_of_category(category: str) -> str | None:
    """The group a gold category belongs to, or None for a category not in the table."""
    return _BY_CATEGORY.get(category)


def _pattern_for(arm: str) -> str | None:
    if arm in MODEL_GROUPS:
        return arm
    return next((p for p in MODEL_GROUPS if fnmatch.fnmatchcase(arm, p)), None)


def groups_for(arm: str) -> tuple[tuple[str, ...], str, bool]:
    """(groups, reason, known) for one arm name; an unknown arm is scored on every group."""
    pattern = _pattern_for(arm)
    if pattern is None:
        return EVERY, UNKNOWN_ARM_REASON, False
    names, reason = MODEL_GROUPS[pattern]
    return names, reason, True


def is_known_arm(arm: str) -> bool:
    return any(arm == a or fnmatch.fnmatchcase(arm, a) for a in ARMS)


def validate() -> None:
    """Refuse a table naming a category, group, metric or arm this module does not know.

    Reads the tables as they stand now (not the lookups built at import), so a table
    patched in a test is checked as it is.
    """
    problems = []
    names = [g.name for g in GROUPS]
    seen: dict[str, str] = {}
    for g in GROUPS:
        if g.headline not in METRICS or any(m not in METRICS for m in g.also):
            problems.append(f"group {g.name}: unknown metric")
        for category in g.categories:
            if category not in CATEGORIES:
                problems.append(f"group {g.name}: unknown category {category!r}")
            if category in seen:
                problems.append(f"category {category!r} is in {seen[category]} and {g.name}")
            seen[category] = g.name
    problems += [f"category {c!r} is in no group" for c in CATEGORIES if c not in seen]
    if len(set(names)) != len(names):
        problems.append("two groups share a name")
    if TEST not in names:
        problems.append(f"no {TEST!r} group")
    for arm, (groups, _reason) in MODEL_GROUPS.items():
        if arm not in ARMS:
            problems.append(f"MODEL_GROUPS names an unknown arm {arm!r}")
        problems += [f"arm {arm}: unknown group {n!r}" for n in groups if n not in names]
    problems += [f"arm {a!r} has no MODEL_GROUPS entry" for a in ARMS if a not in MODEL_GROUPS]
    if problems:
        raise ValueError("; ".join(problems))


validate()
