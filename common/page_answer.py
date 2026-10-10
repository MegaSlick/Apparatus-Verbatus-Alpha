"""The page answer grammar: one JSON object, read deterministically, repaired in one way only.

    parse_state, answer, problems = parse_page_answer(raw_text)

`parse_state` is `"parsed"` or `"malformed"`. On `parsed`, `answer` is the
object exactly as the model gave it and `problems` is empty; on `malformed`
`answer` is `None`, the whole page is held, and `problems` is every reason the
reply is not the grammar, each `{"code", "detail"}`.

The grammar has two shapes, told apart by their top-level keys. The `entries`
shape names the page's type and each entry's kind (`common.page_types`):

    {"page_type", "writing",
     "entries": [{"n", "kind", "label"?, "cites", "text",
                  "continues_from_previous_page", "continues_to_next_page"}, ...],
     "set_aside": [{"id", "reason"}, ...]}

and a re-ask, which reads ids inside a page already typed, answers it without
`page_type` and `writing`. The older `acts` shape names neither:

    {"acts": [{...the same entry...}, ...], "set_aside": [...]}

* exactly the top-level keys of one shape, no others;
* `page_type` one of `common.page_types.PAGE_TYPES`, `writing` one of `WRITINGS`;
* each entry: `n` an integer, `1..k` contiguous in the order given; `kind` one
  of `common.page_types.ENTRY_KINDS` in the `entries` shape, `"act"` or
  `"other"` in the `acts` shape; `label` absent, `null`, or a non-blank string
  of at most 80 characters; `cites` a list of strings; `text` a string; both
  continuation flags present as booleans;
* each set-aside entry: `id` and `reason`, both strings.

Both shapes are read wherever an answer is read, so a saved run's answers in
the `acts` shape replay as they were read: their kinds are already act
classes and their page type is not stated (`stated_page_type` is `None`).
`answer_entry_list` gives either shape's entries.

A continuation flag set on an entry that is not at its page's edge is grammar,
not a malformed answer: it is a statement about one entry, so it never costs the
page its other entries. Only the first entry of the act class (an `act` or an
`instrument`) may continue from the page before and only the last onto the page
after (`common.page_edges.edge_acts`), so a flag elsewhere is on no page break
and joins nothing; the Recensor holds such an act entry
`continuation-off-page-edge` and notes one on any other entry
(`pipeline/5_recensor/CONTRACT.md`). `stray_continuation_flags` names them.

Whether an id exists, whether a range is well formed and the set-aside rules
are checked against the page feed by the shared page-accounting validator, not
here.

Every value is checked for its type before it is compared or hashed, so a reply
of any JSON shape is `parsed` or `malformed` and never an error; a problem's
detail quotes a scalar it found (shortened) and names any other value by its
JSON type.

## What surrounds the object

The reply is one JSON object and nothing else. JSON whitespace before and
after it carries no content and is accepted. Anything else outside the object
-- prose, a second object, a Markdown code fence -- is `malformed`: the model
was asked for bare JSON, and taking the object out of a wrapper would be
reading a reply the grammar does not admit (a fence is named
`fenced-answer`, so a proof run can count how often it happens). Duplicate
keys and `NaN`/`Infinity` are `malformed` too: each would make the parsed
object say something the bytes do not. So is an array or object nested more
than `MAX_NESTING_DEPTH` deep (`too-deep`), counted before the reply is parsed,
so the answer does not depend on how deep the interpreter's own parser can go;
the grammar itself nests four deep. So is a lone
surrogate (a `\\ud800`-style escape with no partner) in any key or string
value: it is no Unicode character, so the answer could not be encoded as the
UTF-8 every record is written in (`lone-surrogate`).

## The one repair

A reply that is not JSON only because some object keys of the grammar are
written bare (`{\nacts: [` for `{"acts": [`) is read after quoting those keys,
and nothing else (`parse_page_answer_repaired`). A key is quoted only when it is
one of the grammar's own field names, stands outside every string right after
`{` or `,` and is followed by `:`; the repaired text must then be one JSON value
with no other problem, or the reply stays `not-json` as it came. The repair is
returned beside the answer, so the reading records it, and the reply's bytes are
never changed: the raw response blob stays as the engine sent it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from common.page_edges import edge_acts
from common.page_types import ENTRY_KINDS, PAGE_TYPES, WRITINGS, is_act_class

PARSED: Final = "parsed"
MALFORMED: Final = "malformed"
PARSE_STATES: Final = frozenset({PARSED, MALFORMED})

# The act classes: the kinds of the `acts` shape, and the `kind` of every record after it.
ACT_KINDS: Final = frozenset({"act", "other"})
LABEL_MAX_CHARACTERS: Final = 80
FENCED_ANSWER: Final = "fenced-answer"

# The two shapes of an answer, by their top-level keys.
ACTS_GRAMMAR: Final = "acts"
ENTRIES_GRAMMAR: Final = "entries"
_TOP_FIELDS: Final = frozenset({"acts", "set_aside"})
_ENTRIES_TOP_FIELDS: Final = frozenset({"page_type", "writing", "entries", "set_aside"})
_REASK_ENTRIES_TOP_FIELDS: Final = frozenset({"entries", "set_aside"})
_SHAPES: Final = {
    _TOP_FIELDS: ACTS_GRAMMAR,
    _ENTRIES_TOP_FIELDS: ENTRIES_GRAMMAR,
    _REASK_ENTRIES_TOP_FIELDS: ENTRIES_GRAMMAR,
}
_ACT_REQUIRED: Final = frozenset(
    {"n", "kind", "cites", "text", "continues_from_previous_page", "continues_to_next_page"}
)
_ACT_FIELDS: Final = _ACT_REQUIRED | {"label"}
_CONTINUATION_FLAGS: Final = ("continues_from_previous_page", "continues_to_next_page")
_SET_ASIDE_FIELDS: Final = frozenset({"id", "reason"})
_JSON_WHITESPACE: Final = " \t\n\r"
_FENCE_MARK: Final = "```"
MAX_NESTING_DEPTH: Final = 64


class _DuplicateKey(ValueError):
    pass


def _refuse_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _value in pairs]
    duplicated = sorted({key for key in keys if keys.count(key) > 1})
    if duplicated:
        raise _DuplicateKey(f"duplicate key(s) {duplicated}")
    return dict(pairs)


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def _problem(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _nesting_depth(text: str) -> int:
    """The deepest `[`/`{` nesting in `text`, not counting brackets inside strings."""
    depth = deepest = 0
    in_string = escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif character in "]}":
            depth -= 1
    return deepest


def _decode(body: str) -> tuple[Any, list[dict[str, str]]]:
    """Exactly one JSON value spanning `body` (surrounding whitespace aside), or problems."""
    decoder = json.JSONDecoder(
        object_pairs_hook=_refuse_duplicates, parse_constant=_refuse_constant
    )
    stripped = body.strip(_JSON_WHITESPACE)
    if (depth := _nesting_depth(stripped)) > MAX_NESTING_DEPTH:
        return None, [
            _problem("too-deep", f"nested {depth} deep, past the {MAX_NESTING_DEPTH} admitted")
        ]
    try:
        value, end = decoder.raw_decode(stripped)
    except _DuplicateKey as error:
        return None, [_problem("duplicate-key", str(error))]
    except ValueError as error:
        return None, [_problem("not-json", str(error))]
    if end != len(stripped):
        return None, [
            _problem("content-outside-object", f"{len(stripped) - end} characters follow the value")
        ]
    if (where := _lone_surrogate(value)) is not None:
        return None, [_problem("lone-surrogate", f"{where} holds a lone surrogate")]
    return value, []


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _lone_surrogate(value: Any) -> str | None:
    """Where the first key or string holding a lone surrogate sits, or `None`."""
    stack: list[tuple[str, Any]] = [("the answer", value)]
    while stack:
        where, item = stack.pop()
        if isinstance(item, str):
            if not _encodable(item):
                return where
        elif isinstance(item, dict):
            for key in reversed(list(item)):
                if not _encodable(key):
                    return f"a key of {where}"
                stack.append((f"{where}[{_shown(key)}]", item[key]))
        elif isinstance(item, list):
            stack.extend(
                (f"{where}[{index}]", entry) for index, entry in reversed(list(enumerate(item)))
            )
    return None


def _is_fenced(text: str) -> bool:
    """Whether the reply is wrapped in a Markdown code fence, read without a regular expression."""
    stripped = text.strip()
    return (
        len(stripped) >= 2 * len(_FENCE_MARK)
        and stripped.startswith(_FENCE_MARK)
        and stripped.endswith(_FENCE_MARK)
    )


_QUOTED_MAX_CHARACTERS: Final = 40


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _shown(value: Any) -> str:
    """A found value for a problem's detail: a short scalar quoted, anything else its type."""
    if isinstance(value, str):
        quoted = repr(value[:_QUOTED_MAX_CHARACTERS])
        return quoted if len(value) <= _QUOTED_MAX_CHARACTERS else f"{quoted}..."
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)[:_QUOTED_MAX_CHARACTERS]
    return "a list" if isinstance(value, list) else "an object"


def _act_problems(index: int, act: Any, list_key: str = "acts") -> list[dict[str, str]]:
    where = f"{list_key}[{index}]"
    kinds = ACT_KINDS if list_key == "acts" else frozenset(ENTRY_KINDS)
    if not isinstance(act, dict):
        return [_problem("act-not-object", f"{where} is not an object")]
    problems = []
    missing = sorted(_ACT_REQUIRED - set(act))
    extra = sorted(set(act) - _ACT_FIELDS)
    if missing:
        problems.append(_problem("act-field-missing", f"{where} lacks {missing}"))
    if extra:
        problems.append(_problem("act-field-unknown", f"{where} carries {extra}"))
    if "n" in act and not _is_int(act["n"]):
        problems.append(_problem("n-not-integer", f"{where}.n is {_shown(act['n'])}"))
    elif "n" in act and act["n"] != index + 1:
        problems.append(
            _problem("n-not-contiguous", f"{where}.n is {act['n']}, expected {index + 1}")
        )
    if "kind" in act and not (isinstance(act["kind"], str) and act["kind"] in kinds):
        problems.append(_problem("kind-unknown", f"{where}.kind is {_shown(act['kind'])}"))
    label = act.get("label")
    if label is not None and (
        not isinstance(label, str) or not label.strip() or len(label) > LABEL_MAX_CHARACTERS
    ):
        problems.append(
            _problem(
                "label-invalid",
                f"{where}.label is not a non-blank string of at most {LABEL_MAX_CHARACTERS} "
                "characters",
            )
        )
    cites = act.get("cites")
    if "cites" in act and (
        not isinstance(cites, list) or not all(isinstance(cite, str) for cite in cites)
    ):
        problems.append(_problem("cites-invalid", f"{where}.cites is not a list of strings"))
    if "text" in act and not isinstance(act["text"], str):
        problems.append(_problem("text-invalid", f"{where}.text is not a string"))
    for flag in _CONTINUATION_FLAGS:
        if flag in act and not isinstance(act[flag], bool):
            problems.append(_problem("flag-invalid", f"{where}.{flag} is not true or false"))
    return problems


def stray_continuation_flags(acts: list[Any]) -> dict[int, list[str]]:
    """`{index: [flag, ...]}` for each entry whose continuation flag is set off its page's edge.

    `continues_from_previous_page` belongs on the first entry of the act class
    and `continues_to_next_page` on the last (`common.page_edges.edge_acts`); any
    other entry may carry one as the answer's first or last entry. Anything
    else is on no page break. It is named, never an answer problem.
    """
    entries = [act for act in acts if isinstance(act, dict)]
    first, last = edge_acts(entries) or (None, None)
    found: dict[int, list[str]] = {}
    for index, act in enumerate(acts):
        if not isinstance(act, dict):
            continue
        is_act = is_act_class(act.get("kind"))
        for flag, edge, position in (
            ("continues_from_previous_page", first, 0),
            ("continues_to_next_page", last, len(acts) - 1),
        ):
            if act.get(flag) is True and not (act is edge if is_act else index == position):
                found.setdefault(index, []).append(flag)
    return found


def _set_aside_problems(index: int, entry: Any) -> list[dict[str, str]]:
    where = f"set_aside[{index}]"
    if not isinstance(entry, dict) or set(entry) != _SET_ASIDE_FIELDS:
        return [_problem("set-aside-invalid", f"{where} is not exactly {{id, reason}}")]
    if not isinstance(entry["id"], str) or not isinstance(entry["reason"], str):
        return [_problem("set-aside-invalid", f"{where} id and reason are not both strings")]
    return []


def grammar_problems(answer: Any) -> list[dict[str, str]]:
    """Every way a decoded value departs from the page answer grammar."""
    if not isinstance(answer, dict):
        return [_problem("not-object", "the answer is not a JSON object")]
    shape = _SHAPES.get(frozenset(answer))
    if shape is None:
        return [
            _problem(
                "top-fields",
                f"the answer's keys are {sorted(answer)}, not exactly {sorted(_TOP_FIELDS)} "
                f"or {sorted(_ENTRIES_TOP_FIELDS)} (a re-ask: without page_type and writing)",
            )
        ]
    list_key = "acts" if shape == ACTS_GRAMMAR else "entries"
    acts, set_aside = answer[list_key], answer["set_aside"]
    problems = []
    if "page_type" in answer and not (
        isinstance(answer["page_type"], str) and answer["page_type"] in PAGE_TYPES
    ):
        problems.append(
            _problem("page-type-unknown", f"page_type is {_shown(answer['page_type'])}")
        )
    if "writing" in answer and not (
        isinstance(answer["writing"], str) and answer["writing"] in WRITINGS
    ):
        problems.append(_problem("writing-unknown", f"writing is {_shown(answer['writing'])}"))
    if not isinstance(acts, list):
        problems.append(_problem("acts-not-list", f"{list_key} is not a list"))
    else:
        for index, act in enumerate(acts):
            problems.extend(_act_problems(index, act, list_key))
    if not isinstance(set_aside, list):
        problems.append(_problem("set-aside-not-list", "set_aside is not a list"))
    else:
        for index, entry in enumerate(set_aside):
            problems.extend(_set_aside_problems(index, entry))
    return problems


def answer_grammar(answer: Mapping[str, Any]) -> str:
    """The shape of a parsed answer: `"entries"` or `"acts"`."""
    shape = _SHAPES.get(frozenset(answer))
    if shape is None:
        raise ValueError("not a parsed page answer")
    return shape


def answer_entry_list(answer: Mapping[str, Any]) -> list[Any]:
    """A parsed answer's entries, in either shape, in the order given."""
    return answer["entries"] if answer_grammar(answer) == ENTRIES_GRAMMAR else answer["acts"]


def stated_page_type(answer: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """`(page_type, writing)` as a parsed answer states them, `(None, None)` where it does not."""
    if answer is None:
        return None, None
    return answer.get("page_type"), answer.get("writing")


def decode_json_reply(raw: Any) -> tuple[Any, list[dict[str, str]]]:
    """`(value, problems)` for a reply that must be one bare JSON value, never repaired.

    `problems` is empty exactly when the reply is text holding one JSON value and
    nothing else: no code fence, no duplicate key, no `NaN`/`Infinity`, nested at
    most `MAX_NESTING_DEPTH` deep and with no lone surrogate. On any problem the
    value is `None`. Shared by every grammar that asks a model for bare JSON.
    """
    if not isinstance(raw, str):
        return None, [_problem("not-text", "the reply is not text")]
    if _is_fenced(raw):
        return None, [
            _problem(FENCED_ANSWER, "the reply wraps its object in a Markdown code fence")
        ]
    return _decode(raw)


def parse_page_answer(raw_text: str) -> tuple[str, dict[str, Any] | None, list[dict[str, str]]]:
    """`(parse_state, answer | None, problems)` for one page reading's reply text, unrepaired."""
    value, problems = decode_json_reply(raw_text)
    return _graded(value, problems)


def _graded(
    value: Any, problems: list[dict[str, str]]
) -> tuple[str, dict[str, Any] | None, list[dict[str, str]]]:
    if not problems:
        problems = grammar_problems(value)
    if problems:
        return MALFORMED, None, problems
    return PARSED, value, []


# --- the one repair: bare grammar keys ------------------------------------------

UNQUOTED_KEYS_QUOTED: Final = "unquoted-keys-quoted"
_GRAMMAR_KEYS: Final = _TOP_FIELDS | _ENTRIES_TOP_FIELDS | _ACT_FIELDS | _SET_ASIDE_FIELDS
_KEY_CHARACTERS: Final = frozenset("abcdefghijklmnopqrstuvwxyz_")


def quote_bare_keys(text: str) -> tuple[str, list[str]]:
    """`text` with each bare grammar key quoted, and the keys quoted in order.

    A key is quoted only outside every string, right after `{` or `,` (JSON
    whitespace between), when it is one of the grammar's field names and is
    followed (after whitespace) by `:`. Nothing else is touched.
    """
    out: list[str] = []
    quoted: list[str] = []
    in_string = escaped = False
    expecting_key = False
    index = 0
    while index < len(text):
        character = text[index]
        if in_string:
            out.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if expecting_key and character in _KEY_CHARACTERS:
            end = index
            while end < len(text) and text[end] in _KEY_CHARACTERS:
                end += 1
            after = end
            while after < len(text) and text[after] in _JSON_WHITESPACE:
                after += 1
            word = text[index:end]
            if word in _GRAMMAR_KEYS and after < len(text) and text[after] == ":":
                out.append(f'"{word}"')
                quoted.append(word)
                index = end
                expecting_key = False
                continue
        if character == '"':
            in_string = True
        if character not in _JSON_WHITESPACE:
            expecting_key = character in "{,"
        out.append(character)
        index += 1
    return "".join(out), quoted


def parse_page_answer_repaired(
    raw_text: str,
) -> tuple[str, dict[str, Any] | None, list[dict[str, str]], list[dict[str, Any]]]:
    """`(parse_state, answer | None, problems, repairs)`: `parse_page_answer`, with the one repair.

    `repairs` is empty unless the reply was `not-json` and quoting its bare
    grammar keys (`quote_bare_keys`) made it one JSON value with no other
    decode problem; then it holds one `{"code": "unquoted-keys-quoted", "keys",
    "detail"}` and the answer is graded from the repaired text. Otherwise the
    reply's own result is returned unchanged.
    """
    value, problems = decode_json_reply(raw_text)
    if [problem["code"] for problem in problems] == ["not-json"]:
        repaired, keys = quote_bare_keys(raw_text)
        if keys:
            repaired_value, repaired_problems = _decode(repaired)
            if not repaired_problems:
                repair = {
                    "code": UNQUOTED_KEYS_QUOTED,
                    "keys": len(keys),
                    "detail": f"{len(keys)} bare grammar key(s) quoted before parsing "
                    f"({', '.join(sorted(set(keys)))}); the reply's bytes are kept as sent",
                }
                return (*_graded(repaired_value, []), [repair])
    return (*_graded(value, problems), [])
