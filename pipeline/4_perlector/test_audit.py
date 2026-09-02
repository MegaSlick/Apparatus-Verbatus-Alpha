"""`audit.py`'s flag pass, wired through to `change_record`'s containment.

`flags_once_per_page` computes a `testimony-diff` flag's location the same
way `common.perlector_audit.change_record` computes a re-proof's change
span: `text_change_span`, trimming a common suffix. When an act's own text
and the testimony that flags it happen to share a final character, the flag
lands one byte short of the true end of the text — a coincidence of the one
testimony compared, not a narrower disagreement. These tests exercise the
real flag pass (not a hand-built flag) feeding a re-proof result into
`change_record`, end to end.
"""

from __future__ import annotations

import audit
import pytest

from common.contracts.errors import SchemaRefusal
from common.perlector_audit import change_record


def _semi_final(*, act_id: str, text: str, testimonia: list[str]) -> dict:
    return {
        "act_id": act_id,
        "page_id": "p1",
        "text": text,
        "testimonia": testimonia,
        "order": 0,
        "geometry_order": (0, 0),
        "within_crop": True,
    }


def test_a_flag_shy_by_a_shared_final_character_still_contains_a_tail_rewrite():
    """The real flag pass, not a hand-built one, produces the short flag.

    The act's own semi-final text and its one testimony both end in the same
    word-final "a" ("...kappa" against a testimony ending "...gamma"), so
    `flags_once_per_page`'s `text_change_span` call trims that shared byte
    off the flag's end. A re-proof that rewrites the trailing word to
    something that does not end in "a" ("...epsilon") produces a change
    envelope that reaches the true end of the text — one byte past the
    flag's own recorded end.
    """
    text = "reading alpha beta kappa"
    testimony = "reading alpha beta gamma"
    semi_finals = [_semi_final(act_id="a1", text=text, testimonia=[testimony])]

    flags = audit.flags_once_per_page(semi_finals)["a1"]
    testimony_flags = [flag for flag in flags if flag["class"] == "testimony-diff"]
    assert len(testimony_flags) == 1
    assert testimony_flags[0]["location"]["end"] == len(text) - 1

    after = "reading alpha beta epsilon"
    changes = change_record(text, after, flags)

    assert changes == [
        {
            "start": testimony_flags[0]["location"]["start"],
            "end": len(text),
            "triggering_flag_class": "testimony-diff",
        }
    ]


def test_a_tail_rewrite_that_reaches_past_the_shared_character_still_refuses():
    """Two trailing words rewritten, not one: a genuine escape from the flag,
    not the one-byte coincidence above, and still refused.
    """
    text = "reading alpha beta gamma kappa"
    testimony = "reading alpha beta gamma zappa"
    semi_finals = [_semi_final(act_id="a1", text=text, testimonia=[testimony])]

    flags = audit.flags_once_per_page(semi_finals)["a1"]
    testimony_flags = [flag for flag in flags if flag["class"] == "testimony-diff"]
    assert len(testimony_flags) == 1
    # "kappa" and "zappa" also share their trailing "appa", so this flag is
    # already short by four bytes, not one -- the shape this test is for.
    assert testimony_flags[0]["location"]["end"] == len(text) - 4

    after = "reading alpha beta ZZZZZ YYYYY"
    with pytest.raises(SchemaRefusal, match="changed text outside every flagged location"):
        change_record(text, after, flags)
