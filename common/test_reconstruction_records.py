"""The Coniector's prompt and record derivation, without a run tree."""

from __future__ import annotations

import json

import pytest

from common.contracts.errors import ContractError
from common.reconstruction import load_reconstruction_policy
from common.reconstruction_answer import PARSED
from common.reconstruction_prompt import build_reconstruction_prompt, shown_keys, shown_texts
from common.reconstruction_records import (
    DOES_NOT_CONTINUE,
    LABEL,
    NOT_ASKED,
    REPLY_ANSWER_INVALID,
    REPLY_CUT_OFF,
    REPLY_MALFORMED,
    derive_reconstructions,
    reconstruction_outcome,
    reconstruction_subject,
    reply_state,
)

POLICY = load_reconstruction_policy()
CALL_REF = {"relative_path": "call", "sha256": "0" * 64}
MAKER = {
    "kind": "model",
    "chair": "reconstructor",
    "chair_state": "configured",
    "resolved_identity": None,
    "resolved_revision": None,
    "receipt_ref": None,
}


def _entry(key, ordinal, n, text, kind="act"):
    return {
        "act_key": key,
        "act_id": f"act_{key.replace(':', '_')}",
        "page_id": f"page_{ordinal}",
        "page_ordinal": ordinal,
        "n": n,
        "kind": kind,
        "label": None,
        "text": text,
        "clean_text": text.replace("[[gamma|gamna]]", "gamma"),
    }


SHOWN = {
    "p1:1": _entry("p1:1", 1, 1, "Baptême de Jean [[gamma|gamna]] le deux"),
    "p1:2": _entry("p1:2", 1, 2, "Mariage de Pierre et"),
    "p1:3": _entry("p1:3", 1, 3, "marginal note", kind="other"),
    "p2:1": _entry("p2:1", 2, 1, "Marie, le trois"),
}
CALL = {"page_ordinal": 1, "subjects": ["p1:1"], "chains": [], "context": ["p2:1"]}
JOIN_CALL = {"page_ordinal": 2, "subjects": [], "chains": [["p1:2", "p2:1"]], "context": []}


def _derive(call, reply):
    state, answer, problems = reply_state(reply, "stop", call)
    return derive_reconstructions(
        call,
        SHOWN,
        parse_state=state,
        answer=answer,
        problems=problems,
        policy=POLICY,
        call_ref=CALL_REF,
        maker=MAKER,
    )


def test_the_prompt_shows_the_page_in_order_then_its_context_and_asks_for_the_subjects():
    text = build_reconstruction_prompt(CALL, SHOWN)
    page, context = text.index("## Page 1"), text.index("## Neighbouring pages")
    assert text.index("[p1:1]") < text.index("[p1:2]") < text.index("[p1:3]") < context
    assert page < context < text.index("[p2:1]")
    assert '["p1:1"]' in text and "image" not in text.split("## Page 1")[1]
    assert shown_keys(CALL, SHOWN) == ["p1:1", "p1:2", "p1:3", "p2:1"]
    assert shown_texts(CALL, SHOWN)[-1] == "Marie, le trois"


def test_the_prompt_refuses_a_subject_that_is_not_on_its_page():
    with pytest.raises(ContractError, match="not entries of the call's page"):
        build_reconstruction_prompt({**CALL, "subjects": ["p2:1"]}, SHOWN)


def test_a_parsed_answer_gives_each_act_its_reconstruction_and_its_findings():
    reply = json.dumps(
        {
            "acts": [
                {
                    "act": "p1:1",
                    "findings": [{"code": "out-of-sequence", "reason": "1888 among 1666"}],
                    "departures": [{"diplomatic": "deux", "reconstruction": "deuxième"}],
                }
            ],
            "joins": [],
        }
    )
    (record,) = _derive(CALL, reply)
    assert record["made"] and record["label"] == LABEL
    assert record["reconstruction_raw"] == "Baptême de Jean [[gamma|gamna]] le deuxième"
    assert record["reconstruction_text"] == "Baptême de Jean gamma le deuxième"
    assert record["findings"] == [{"code": "out-of-sequence", "reason": "1888 among 1666"}]
    assert reconstruction_outcome(record) == "made"
    assert reconstruction_subject(record) == "act_p1_1"


def test_a_departure_that_cannot_apply_leaves_only_its_act_not_made():
    reply = json.dumps(
        {
            "acts": [
                {
                    "act": "p1:1",
                    "findings": [],
                    "departures": [{"diplomatic": "absent", "reconstruction": "x"}],
                }
            ],
            "joins": [],
        }
    )
    (record,) = _derive(CALL, reply)
    assert record["made"] is False and record["reconstruction_raw"] is None
    assert [reason["code"] for reason in record["not_made"]] == ["departure-span-not-found"]


@pytest.mark.parametrize(
    "reply, stop, code",
    [
        ("not json", "stop", REPLY_MALFORMED),
        ('{"acts": [], "joins": []}', "stop", REPLY_ANSWER_INVALID),
        ('{"acts": [], "joins": []}', "length", REPLY_CUT_OFF),
    ],
)
def test_a_call_with_no_usable_answer_leaves_every_act_not_made_for_its_reason(reply, stop, code):
    state, answer, problems = reply_state(reply, stop, CALL)
    assert state != PARSED and answer is None
    (record,) = derive_reconstructions(
        CALL,
        SHOWN,
        parse_state=state,
        answer=answer,
        problems=problems,
        policy=POLICY,
        call_ref=CALL_REF,
        maker=MAKER,
    )
    assert [reason["code"] for reason in record["not_made"]] == [code]


def test_a_call_not_asked_carries_its_one_reason():
    assert reply_state(None, None, CALL) == (NOT_ASKED, None, [])
    (record,) = derive_reconstructions(
        CALL,
        SHOWN,
        parse_state=NOT_ASKED,
        answer=None,
        problems=[{"code": "chair-absent", "detail": "no chair"}],
        policy=POLICY,
        call_ref=CALL_REF,
        maker=MAKER,
    )
    assert record["not_made"] == [{"code": "chair-absent", "detail": "no chair"}]


def test_a_join_applies_to_its_pieces_joined_by_one_newline_or_says_it_does_not_continue():
    joined = json.dumps(
        {
            "acts": [],
            "joins": [
                {
                    "acts": ["p1:2", "p2:1"],
                    "continues": True,
                    "departures": [{"diplomatic": "et\nMarie", "reconstruction": "et Marie"}],
                }
            ],
        }
    )
    (join,) = _derive(JOIN_CALL, joined)
    assert join["unit"] == "join" and join["act_keys"] == ["p1:2", "p2:1"]
    assert join["reconstruction_raw"] == "Mariage de Pierre et Marie, le trois"
    assert reconstruction_subject(join) == "act_p1_2"
    apart = json.dumps(
        {"acts": [], "joins": [{"acts": ["p1:2", "p2:1"], "continues": False, "departures": []}]}
    )
    (join,) = _derive(JOIN_CALL, apart)
    assert join["made"] is False and join["continues"] is False
    assert [reason["code"] for reason in join["not_made"]] == [DOES_NOT_CONTINUE]
