"""SPEC_D §1.2, §4, §6 (D1 row) -- the structure chair's closed answer contract.

Every `PARSE_OUTCOMES` code is reached by its own targeted input; the parser's
float quantization, page-text/spans join, and page-pixel conversion are pinned
against the rules `common/structure_answer.py` declares; and a static AST check
pins the import boundary named in the brief -- `common/structure_answer.py`
imports nothing from `pipeline/` or `operations/`.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from common import structure_answer
from common.structure_answer import (
    PARSE_OUTCOMES,
    STRUCTURE_ANSWER_SCHEMA,
    parse,
    text_digest,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "common" / "structure_answer.py"


def _answer(acts: list[dict]) -> bytes:
    return json.dumps({"schema": STRUCTURE_ANSWER_SCHEMA, "acts": acts}).encode("utf-8")


def _act(box=(0, 0, 500, 500), text="hello", label=None) -> dict:
    act = {"box_1000": list(box), "text": text}
    if label is not None:
        act["label"] = label
    return act


# ---------------------------------------------------------------------------
# Every PARSE_OUTCOME, reached by its own input.
# ---------------------------------------------------------------------------


def test_raw_response_not_bytes_is_refused_by_name():
    result = parse("not bytes", page_w=100, page_h=100)
    assert result == {"parse_outcome": "raw-response-not-bytes"}


def test_response_too_large_is_refused_by_name(monkeypatch):
    monkeypatch.setattr(structure_answer, "MAX_RESPONSE_BYTES", 4)
    result = parse(_answer([]), page_w=100, page_h=100)
    assert result == {"parse_outcome": "response-too-large"}


def test_invalid_json_is_refused_by_name():
    result = parse(b"{not json", page_w=100, page_h=100)
    assert result == {"parse_outcome": "invalid-json"}


def test_invalid_utf8_is_invalid_json():
    result = parse(b"\xff\xfe", page_w=100, page_h=100)
    assert result == {"parse_outcome": "invalid-json"}


def test_excessive_json_nesting_is_refused_by_name_not_a_recursion_error():
    """Pinned at the same depth `common/corpus_register.py` pins its own version
    of this refusal at, and for the same reason: 10,000 exhausted CPython's C
    recursion allowance on macOS but not on Linux CI, so the depth has to beat
    the parser's allowance on every platform this runs on."""
    depth = 1_000_000
    data = (
        b'{"schema":"'
        + STRUCTURE_ANSWER_SCHEMA.encode()
        + b'","acts":'
        + b"[" * depth
        + b"]" * depth
        + b"}"
    )
    result = parse(data, page_w=100, page_h=100)
    assert result == {"parse_outcome": "excessive-json-nesting"}


def test_top_level_not_object_is_refused_by_name():
    result = parse(b"[1, 2, 3]", page_w=100, page_h=100)
    assert result == {"parse_outcome": "top-level-not-object"}


def test_a_duplicate_top_level_member_is_refused_not_resolved_last_wins():
    """The stdlib's default reads a duplicate key last-wins, silently -- two
    values for one field and one just disappears, with nothing reporting the
    loss. Pinned separately from the nesting case, mirroring
    `test_door.py:1137`'s own comment about why: this is a parse-time refusal
    on well-formed JSON, not the closed-schema check catching a bad shape."""
    body = b'{"schema":"' + STRUCTURE_ANSWER_SCHEMA.encode() + b'","acts":[],"acts":[]}'
    result = parse(body, page_w=100, page_h=100)
    assert result == {"parse_outcome": "unverified-response-schema"}


def test_unverified_response_schema_on_wrong_schema_string():
    body = json.dumps({"schema": "some-other-schema", "acts": []}).encode("utf-8")
    result = parse(body, page_w=100, page_h=100)
    assert result == {"parse_outcome": "unverified-response-schema"}


def test_unverified_response_schema_on_an_unknown_top_level_key():
    body = json.dumps({"schema": STRUCTURE_ANSWER_SCHEMA, "acts": [], "confidence": 1}).encode(
        "utf-8"
    )
    result = parse(body, page_w=100, page_h=100)
    assert result == {"parse_outcome": "unverified-response-schema"}


def test_unverified_response_schema_on_an_unknown_nested_act_key():
    """Any other key at any level refuses by the same code, per §1.2 --
    including one nested inside a single act, not only at the top."""
    body = json.dumps(
        {
            "schema": STRUCTURE_ANSWER_SCHEMA,
            "acts": [{"box_1000": [0, 0, 500, 500], "text": "x", "rank": 1}],
        }
    ).encode("utf-8")
    result = parse(body, page_w=100, page_h=100)
    assert result == {"parse_outcome": "unverified-response-schema"}


def test_missing_act_list_when_acts_key_absent():
    body = json.dumps({"schema": STRUCTURE_ANSWER_SCHEMA}).encode("utf-8")
    result = parse(body, page_w=100, page_h=100)
    assert result == {"parse_outcome": "missing-act-list"}


def test_missing_act_list_when_acts_is_not_a_list():
    body = json.dumps({"schema": STRUCTURE_ANSWER_SCHEMA, "acts": "nope"}).encode("utf-8")
    result = parse(body, page_w=100, page_h=100)
    assert result == {"parse_outcome": "missing-act-list"}


def test_too_many_acts_is_refused_by_name(monkeypatch):
    monkeypatch.setattr(structure_answer, "MAX_ACTS", 1)
    result = parse(_answer([_act(), _act()]), page_w=100, page_h=100)
    assert result == {"parse_outcome": "too-many-acts"}


def test_malformed_act_when_an_act_is_not_an_object():
    result = parse(_answer(["not an object"]), page_w=100, page_h=100)
    assert result == {"parse_outcome": "malformed-act"}


def test_malformed_act_when_a_required_key_is_missing():
    body = json.dumps(
        {"schema": STRUCTURE_ANSWER_SCHEMA, "acts": [{"box_1000": [0, 0, 500, 500]}]}
    ).encode("utf-8")
    result = parse(body, page_w=100, page_h=100)
    assert result == {"parse_outcome": "malformed-act"}


def test_malformed_act_when_label_has_the_wrong_type():
    body = json.dumps(
        {
            "schema": STRUCTURE_ANSWER_SCHEMA,
            "acts": [{"box_1000": [0, 0, 500, 500], "text": "x", "label": 7}],
        }
    ).encode("utf-8")
    result = parse(body, page_w=100, page_h=100)
    assert result == {"parse_outcome": "malformed-act"}


def test_malformed_act_when_label_is_explicit_json_null():
    """`label` is an optional *string*: present-as-string, or absent. An
    explicit `null` is neither, and is refused rather than read as a synonym
    for absent -- SPEC_D §1.2 declares the shape, and this module does not
    normalize a value it does not contain (GOVERNANCE 7)."""
    body = json.dumps(
        {
            "schema": STRUCTURE_ANSWER_SCHEMA,
            "acts": [{"box_1000": [0, 0, 500, 500], "text": "x", "label": None}],
        }
    ).encode("utf-8")
    result = parse(body, page_w=100, page_h=100)
    assert result == {"parse_outcome": "malformed-act"}


@pytest.mark.parametrize(
    "box",
    [
        [0, 0, 500],  # wrong length
        "not-a-list",  # wrong type
        [0, 0, 500, "500"],  # non-numeric entry
        [0, 0, 500, True],  # bool is not a number
        [0, 0, 500, float("nan")],  # not finite
        [0, 0, 500, float("inf")],  # not finite
        [-1, 0, 500, 500],  # out of [0, 1000]
        [0, 0, 1001, 500],  # out of [0, 1000]
        [500, 0, 500, 500],  # x1 == x0
        [0, 500, 500, 500],  # y1 == y0
        [500, 0, 0, 500],  # x1 < x0
    ],
)
def test_malformed_act_geometry_is_refused_by_name(box):
    result = parse(_answer([_act(box=box)]), page_w=100, page_h=100)
    assert result == {"parse_outcome": "malformed-act-geometry"}


def test_malformed_act_text_is_refused_by_name():
    body = json.dumps(
        {
            "schema": STRUCTURE_ANSWER_SCHEMA,
            "acts": [{"box_1000": [0, 0, 500, 500], "text": 5}],
        }
    ).encode("utf-8")
    result = parse(body, page_w=100, page_h=100)
    assert result == {"parse_outcome": "malformed-act-text"}


def test_no_outcome_can_be_added_to_the_contract_without_a_test_above():
    """The guard on the guard, and it is a *list*, not a coverage measurement.

    Nothing here runs the tests above or observes which outcomes they reached.
    It pins the declared set against a second copy written out by hand, so
    adding a code to `PARSE_OUTCOMES` fails this until someone edits this list
    too -- and the edit is where they notice the code has no test. Its
    weakness is the same as its strength: a line added to both places without
    a test passes, so the list is a prompt to the next author, not proof.
    """
    exercised = {
        "raw-response-not-bytes",
        "response-too-large",
        "invalid-json",
        "excessive-json-nesting",
        "top-level-not-object",
        "unverified-response-schema",
        "missing-act-list",
        "too-many-acts",
        "malformed-act",
        "malformed-act-geometry",
        "malformed-act-text",
    }
    assert exercised == PARSE_OUTCOMES


# ---------------------------------------------------------------------------
# Empty acts, accepted rather than refused.
# ---------------------------------------------------------------------------


def test_empty_acts_list_is_accepted_not_refused():
    result = parse(_answer([]), page_w=100, page_h=100)
    assert result == {
        "schema": STRUCTURE_ANSWER_SCHEMA,
        "acts": [],
        "page_text": "",
        "spans": [],
    }


# ---------------------------------------------------------------------------
# Float quantization: never refused, quantized by the declared rule.
# ---------------------------------------------------------------------------


def test_float_boxes_are_quantized_low_floor_far_ceil_not_refused():
    body = json.dumps(
        {
            "schema": STRUCTURE_ANSWER_SCHEMA,
            "acts": [{"box_1000": [10.9, 20.1, 30.1, 40.9], "text": "x"}],
        }
    ).encode("utf-8")
    result = parse(body, page_w=1000, page_h=1000)
    assert "parse_outcome" not in result
    assert result["acts"][0]["box_1000"] == [10, 20, 31, 41]


@pytest.mark.parametrize(
    "raw,quantized",
    [
        ((0.0, 0.0, 1.0, 1.0), [0, 0, 1, 1]),
        ((0.5, 0.5, 0.6, 0.6), [0, 0, 1, 1]),
        ((999.1, 999.1, 999.9, 999.9), [999, 999, 1000, 1000]),
        ((100, 100, 200, 200), [100, 100, 200, 200]),  # already integers: unchanged
    ],
)
def test_the_quantization_rule_is_pinned_exactly(raw, quantized):
    x0, y0, x1, y1 = raw
    assert structure_answer._quantize(x0, y0, x1, y1) == quantized


# ---------------------------------------------------------------------------
# page_text / spans: newline only between non-empty texts.
# ---------------------------------------------------------------------------


def test_page_text_joins_only_non_empty_acts_with_a_single_newline():
    body = _answer(
        [
            _act(box=(0, 0, 100, 100), text="first"),
            _act(box=(100, 0, 200, 100), text=""),
            _act(box=(200, 0, 300, 100), text="second"),
        ]
    )
    result = parse(body, page_w=1000, page_h=1000)
    assert result["page_text"] == "first\nsecond"


def test_spans_locate_each_act_including_zero_width_empty_ones():
    body = _answer(
        [
            _act(box=(0, 0, 100, 100), text="first"),
            _act(box=(100, 0, 200, 100), text=""),
            _act(box=(200, 0, 300, 100), text="second"),
        ]
    )
    result = parse(body, page_w=1000, page_h=1000)
    spans = result["spans"]
    page_text = result["page_text"]
    assert spans[0] == {"start": 0, "end": 5}
    assert page_text[spans[0]["start"] : spans[0]["end"]] == "first"
    assert spans[1]["start"] == spans[1]["end"] == 5
    assert spans[2] == {"start": 6, "end": 12}
    assert page_text[spans[2]["start"] : spans[2]["end"]] == "second"


def test_a_page_of_only_empty_acts_never_produces_a_bare_separator():
    """The exact defect `page_join`'s own docstring names (CodeRabbit W44):
    joining every payload including the empty ones gave `payload="\\n"` under
    a claimed reading of characters nobody delivered."""
    body = _answer(
        [
            _act(box=(0, 0, 100, 100), text=""),
            _act(box=(100, 0, 200, 100), text=""),
        ]
    )
    result = parse(body, page_w=1000, page_h=1000)
    assert result["page_text"] == ""
    assert result["spans"] == [{"start": 0, "end": 0}, {"start": 0, "end": 0}]


def test_text_digest_lets_a_reader_prove_the_same_derivation():
    assert text_digest("hello") == text_digest("hello")
    assert text_digest("hello") != text_digest("hellO")


def test_text_digest_is_sha256_over_the_utf8_bytes_and_nothing_else():
    """Two pinned vectors, computed once with `hashlib` and written here as
    literals. Determinism alone would survive a change of algorithm or of
    encoding -- both halves stay self-consistent -- and a reader who has the
    retained bytes must be able to re-derive this digest with a stock SHA-256
    over UTF-8 and no other knowledge of this repository. The second vector is
    non-ASCII on purpose: it is the half that fails if the encoding moves off
    UTF-8, which an ASCII-only vector cannot see."""
    assert (
        text_digest("hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert (
        text_digest("Élise") == "62875e5633fe8a4fe8bfd9070fe90c87fb9e1b7b4861abfe64acef5b4f891ad2"
    )


# ---------------------------------------------------------------------------
# Page-pixel conversion equality against geometry_layer lives in the
# designator-side test (SPEC_D §1.2: geometry_layer lives in
# pipeline/2_designator, so the equality test does too):
# pipeline/2_designator/test_structure_prompt.py.
# ---------------------------------------------------------------------------


def test_to_page_bounds_is_reachable_and_shaped_like_a_bounds_dict():
    bounds = structure_answer.to_page_bounds([0, 0, 500, 500], 1000, 1000)
    assert set(bounds) == {"x", "y", "w", "h"}
    assert bounds == {"x": 0, "y": 0, "w": 500, "h": 500}


def test_to_page_bounds_reaches_exactly_the_last_page_pixel():
    """Named for what this actually exercises: for any legal box (x1 <= 1000)
    the `min(page_w - 1, ...)` term never binds, so this pins the far edge
    landing exactly on the last pixel, not a clamp. The term is carried
    verbatim from `geometry_layer.py`'s own arithmetic so the two expressions
    read identically -- kept deliberately, as mirroring, not as a live guard."""
    bounds = structure_answer.to_page_bounds([0, 0, 1000, 1000], 7, 11)
    assert bounds == {"x": 0, "y": 0, "w": 7, "h": 11}


# ---------------------------------------------------------------------------
# raw_bounds is always in-page: geometry.validate_bounds's own guarantee.
# ---------------------------------------------------------------------------


def test_raw_bounds_never_falls_outside_the_declared_page():
    body = _answer([_act(box=(0, 0, 1000, 1000))])
    result = parse(body, page_w=37, page_h=53)
    bounds = result["acts"][0]["raw_bounds"]
    assert bounds["x"] >= 0 and bounds["y"] >= 0
    assert bounds["x"] + bounds["w"] <= 37
    assert bounds["y"] + bounds["h"] <= 53


# ---------------------------------------------------------------------------
# ordinal: reading order, unaltered.
# ---------------------------------------------------------------------------


def test_ordinal_is_response_order_and_nothing_reorders_it():
    body = _answer(
        [
            _act(box=(500, 0, 1000, 100), text="second-drawn"),
            _act(box=(0, 0, 500, 100), text="first-drawn"),
        ]
    )
    result = parse(body, page_w=1000, page_h=1000)
    assert [act["ordinal"] for act in result["acts"]] == [0, 1]
    assert [act["text"] for act in result["acts"]] == ["second-drawn", "first-drawn"]


def test_label_is_returned_verbatim_to_the_caller_and_none_when_absent():
    """Verbatim *here*, and no length bound here either.

    The caller is what decides where a label may go: this module hands back
    what the chair said, and `pipeline/2_designator/structure_pass.py` reduces
    it to a digest and a length before anything is published. A label of any
    length is therefore an ordinary answer to this parser -- the long one below
    parses like the short one -- because no ceiling this module could set would
    be the thing that keeps the chair's reading out of a Designator artifact.
    """
    body = _answer([_act(text="x", label="marginal note")])
    result = parse(body, page_w=1000, page_h=1000)
    assert result["acts"][0]["label"] == "marginal note"

    whole_act = "Jean Baptiste, fils de Pierre et de Marie, ne le douzieme jour " * 10
    body = _answer([_act(text="x", label=whole_act)])
    result = parse(body, page_w=1000, page_h=1000)
    assert result["acts"][0]["label"] == whole_act

    body = _answer([_act(text="x")])
    result = parse(body, page_w=1000, page_h=1000)
    assert result["acts"][0]["label"] is None


# ---------------------------------------------------------------------------
# Import boundary: common/structure_answer.py imports nothing from
# pipeline/ or operations/.
# ---------------------------------------------------------------------------


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            function = node.func
            is_dynamic = (
                isinstance(function, ast.Name) and function.id in ("__import__", "import_module")
            ) or (
                isinstance(function, ast.Attribute)
                and function.attr in ("__import__", "import_module")
            )
            if is_dynamic and node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    roots.add(value.split(".")[0])
    return roots


def test_structure_answer_imports_nothing_from_pipeline_or_operations():
    """Static, the same reason `common/chairs/test_chairs_import_boundary.py`
    gives: the module boundary is a naming convention unless the check reads
    the actual `ast.Import`/`ast.ImportFrom`/literal dynamic-import nodes."""
    roots = _imported_roots(MODULE_PATH)
    forbidden = roots & {"pipeline", "operations"}
    assert not forbidden, f"common/structure_answer.py imported forbidden roots: {forbidden}"


def test_the_import_boundary_check_can_see_a_violation(tmp_path):
    """A guard must prove its own check can go red."""
    source = tmp_path / "would_violate.py"
    source.write_text(
        "import pipeline.two_designator.run\n"
        "from operations.serving import client\n"
        "from importlib import import_module\n"
        "import_module('pipeline.somewhere')\n",
        encoding="utf-8",
    )
    roots = _imported_roots(source)
    assert {"pipeline", "operations"} <= roots
