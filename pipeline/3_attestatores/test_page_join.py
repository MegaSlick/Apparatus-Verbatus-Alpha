"""`page_join`: what a page witness's synthetic page reading may claim.

R0 has no live page-scoped witness. A page Testimonium is built by joining one
chair's own act attempts on that page, so everything the page record asserts has
to be derivable from those attempts and nothing else.

The join used to be `"\\n".join(readable)` over every joined payload, with the
outcome `read` whenever the *list* was non-empty. Two acts a chair genuinely read
as empty therefore produced `payload="\\n"` under `outcome="read"`: a separator
character no act delivered, retained as a reading of it, and counted as page
content by every consumer downstream (CodeRabbit W44). Separators now appear only
between delivered characters, and the outcome is derived from the joined text.
"""

import importlib.util
from pathlib import Path


def _load_attestatores():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("attestatores_page_join", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attestatores = _load_attestatores()
Attempt = attestatores.Attempt


def _act(key: str) -> dict:
    return {"act_id": f"act_{key}", "act_key": key}


def _attempt(outcome: str, payload, *, reason: str | None = None) -> Attempt:
    return Attempt(
        outcome=outcome,
        native_payload=payload,
        witness_reported=None,
        format_capabilities=attestatores.DEFAULT_FORMAT_CAPABILITIES,
        health=attestatores.content_health(payload, completed=True),
        reason=reason,
    )


def _join(*pairs):
    return attestatores.page_join([(_act(key), attempt) for key, attempt in pairs])


def test_a_page_of_genuinely_empty_acts_is_genuinely_empty_not_a_read_separator():
    """W44 itself. Two empty readings joined to "\\n" and reported `read`."""
    join = _join(
        ("a1", _attempt("genuinely-empty", "")),
        ("a2", _attempt("genuinely-empty", "")),
    )

    assert join.native_payload == ""
    assert join.outcome == "genuinely-empty"
    assert join.unjoined_act_attempts == []


def test_one_empty_act_contributes_no_leading_separator():
    """The surviving half of the same defect: a separator before the first
    delivered character is a character the page witness never reported, and
    every span this stage publishes indexes into this exact text."""
    join = _join(
        ("a1", _attempt("genuinely-empty", "")),
        ("a2", _attempt("read", "SYNTHETIC ACT TWO")),
    )

    assert join.native_payload == "SYNTHETIC ACT TWO"
    assert join.outcome == "read"


def test_delivered_readings_are_still_separated_from_each_other():
    join = _join(
        ("a1", _attempt("read", "ACT ONE")),
        ("a2", _attempt("read", "ACT TWO")),
    )

    assert join.native_payload == "ACT ONE\nACT TWO"
    assert join.outcome == "read"


def test_a_blank_but_delivered_reading_is_still_a_reading():
    """`genuinely-empty` means `payload == ""` everywhere in this stage. A
    witness that delivered whitespace delivered characters, and promoting that
    to an absence would feed the Recensor's terminal blank seal a reading that
    is not one."""
    join = _join(("a1", _attempt("read", "   ")))

    assert join.native_payload == "   "
    assert join.outcome == "read"


def test_nothing_joined_is_failed_rather_than_an_empty_reading():
    """No act on this page was read by this chair, so there is no page reading.
    Distinct from `genuinely-empty`, which is a completed read of an absence."""
    join = _join(
        ("a1", _attempt("failed", None, reason="the chair returned no usable response")),
        ("a2", _attempt("not-run", None, reason="no attempt was made for this configured chair")),
    )

    assert join.native_payload == ""
    assert join.outcome == "failed"
    assert [row["act_key"] for row in join.unjoined_act_attempts] == ["a1", "a2"]
    assert [row["reason"] for row in join.unjoined_act_attempts] == [
        "the chair returned no usable response",
        "no attempt was made for this configured chair",
    ]


def test_a_failed_attempt_carrying_text_is_disclosed_rather_than_folded_in():
    """F-S1, held down here now that the partition is its own function: an
    attempt whose outcome is `failed` can still carry parsed text."""
    join = _join(
        ("a1", _attempt("failed", "half a reading", reason="capabilities were unrecordable")),
        ("a2", _attempt("read", "ACT TWO")),
    )

    assert join.native_payload == "ACT TWO"
    assert join.outcome == "read"
    assert join.unjoined_act_attempts == [
        {
            "act_id": "act_a1",
            "act_key": "a1",
            "outcome": "failed",
            "reason": "capabilities were unrecordable",
        }
    ]


def test_a_structured_reading_is_disclosed_with_the_joins_own_limit():
    """F-O7: a reading the text join cannot carry has no reason to borrow, so
    the join states its own."""
    join = _join(
        ("a1", _attempt("read", {"tokens": ["alpha"]})),
        ("a2", _attempt("read", "ACT TWO")),
    )

    assert join.native_payload == "ACT TWO"
    assert join.unjoined_act_attempts == [
        {
            "act_id": "act_a1",
            "act_key": "a1",
            "outcome": "read",
            "reason": (
                "this chair delivered a structured native reading for the act; R0's "
                "synthetic page join concatenates delivered text only"
            ),
        }
    ]


def test_an_empty_reading_beside_an_unjoinable_one_claims_no_completed_absence():
    """The chair read one act and found nothing; the other could not be
    carried. A completed absence over a page partly unread would be the
    fabrication defect one scope up (invariant 6), so the page record refuses
    to claim one -- the omission is disclosed, and the read-empty fact stays
    visible in that act's own Testimonium."""
    join = _join(
        ("a1", _attempt("genuinely-empty", "")),
        ("a2", _attempt("dead", None, reason="chair is explicitly absent: no pod")),
    )

    assert join.native_payload == ""
    assert join.outcome == "failed"
    assert [row["act_key"] for row in join.unjoined_act_attempts] == ["a2"]


# --- the page record's stated reason must match the evidence it retained ---------


def _reason(*pairs) -> str:
    """The reason a failed page record would carry for this exact set of attempts."""
    join = _join(*pairs)
    return attestatores.page_failure_reason(join.unjoined_act_attempts, join.joined_act_attempts)


def test_a_structured_reading_the_join_cannot_carry_is_not_called_unread():
    """CodeRabbit, PR #63. The defect this replaces, stated exactly.

    An empty textual reading joins; a structured native reading does not, because
    the synthetic page join concatenates text only. The old reason counted the
    unjoined rows and, finding fewer than the acts on the page, said the page was
    "partly unread" — of an act the chair had read and reported in full. That
    points recovery at a missing-ink diagnosis for a page where no ink is missing.
    """
    reason = _reason(
        ("a1", _attempt("genuinely-empty", "")),
        ("a2", _attempt("read", {"lines": ["Maria"]})),
    )

    # The guard is the false CLAIM, not the word: the message may say "no part of
    # it is claimed unread", which is the opposite assertion and contains the same
    # substring. Pinning the bare word would fail on correct wording.
    assert "partly unread" not in reason
    assert "structured native reading" in reason
    assert "the page was read and no part of it is claimed unread" in reason


def test_an_act_read_as_empty_beside_a_failure_is_the_partly_unread_page():
    """One act joined as genuinely empty, one attempt that was not a reading.

    "Only empty readings, and not every attempt carried" is exactly true here, so
    this wording stays. The rewrite must not soften a genuine absence into a join
    detail.
    """
    reason = _reason(
        ("a1", _attempt("genuinely-empty", "")),
        ("a2", _attempt("failed", None, reason="provider returned nothing")),
    )

    assert "partly unread" in reason
    assert "only empty readings" in reason
    assert "structured" not in reason


def test_a_page_where_nothing_joined_is_unread_not_read_and_empty():
    """CodeRabbit CLI, PR #63 — the defect the previous fix introduced.

    Every attempt failed, so the join carried nothing at all. The reason said "the
    page join carried only empty readings", which names readings that do not exist:
    an unjoined list of non-readings looks identical whether one act joined empty or
    none did, and only the joined count separates a page read as blank from a page
    not read. Reporting the first over the second would send a reviewer looking for
    a blank page instead of a dead chair.
    """
    reason = _reason(
        ("a1", _attempt("failed", None, reason="provider returned nothing")),
        ("a2", _attempt("failed", None, reason="provider returned nothing")),
    )

    assert "no act attempt on this page was a reading at all" in reason
    assert "2 attempts" in reason
    assert "empty readings" not in reason
    assert "unread rather than read and empty" in reason


def test_a_mixed_page_names_both_kinds_and_their_counts():
    """Neither kind may hide behind the other. An operator reading this has to be
    able to tell how much of the page needs a provider look and how much needs a
    join that understands structured readings."""
    reason = _reason(
        ("a1", _attempt("genuinely-empty", "")),
        ("a2", _attempt("read", {"lines": ["Maria"]})),
        ("a3", _attempt("failed", None, reason="provider returned nothing")),
    )

    assert "2 act attempts" in reason
    assert "1 were not readings" in reason
    assert "1 were structured native readings" in reason


def test_no_unjoined_attempts_at_all_names_the_join_and_blames_no_one():
    """Every act joined and the joined text was still empty of delivered
    characters. Nothing was lost and no chair failed; the page simply carries no
    textual reading, and that is all the record may say."""
    assert _reason(("a1", _attempt("genuinely-empty", ""))) == (
        "the page join carried no textual reading"
    )
