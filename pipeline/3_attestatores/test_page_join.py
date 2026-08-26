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

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.chairs.models import AbsentChair
from common.contracts.errors import SchemaRefusal


def _load_attestatores():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("attestatores_page_join", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attestatores = _load_attestatores()
Attempt = attestatores.Attempt


class _UnhashableString(str):
    __hash__ = None


class _HostileReprString(str):
    def __repr__(self):
        raise RuntimeError("the refusal rendered an untrusted chair-name subclass")


@pytest.mark.parametrize(
    "bad_chair",
    ([], {}, [[]], {"nested": []}, {"a": {"b": [1, 2]}}, [[[]]], [{"a": [{}]}]),
)
def test_declared_page_witness_chairs_refuses_unhashable_json_values(bad_chair):
    """Fixture data crosses this boundary before set-based roster handling."""
    context = SimpleNamespace(fixture={"page_witness_chairs": [bad_chair]})

    with pytest.raises(SchemaRefusal, match="unique string list"):
        attestatores.declared_page_witness_chairs(context)


def test_an_unknown_page_witness_chair_is_refused_not_dropped_from_the_join():
    """A declared typo is evidence of a missing witness, not an empty intersection."""
    context = SimpleNamespace(
        fixture={"page_witness_chairs": ["attestator_33"]},
        witness_chairs=("attestator_1",),
    )

    with pytest.raises(SchemaRefusal, match="outside the configured witness roster"):
        attestatores.publish_page_testimonia_and_attachments(
            context, acts=[], ordinal=1, attempts_by_pair={}, regions_by_act={}
        )


def test_an_unknown_page_witness_chair_is_refused_by_the_shared_accessor_itself():
    """The accessor must protect write paths that cannot rely on the later page join."""
    context = SimpleNamespace(
        fixture={"page_witness_chairs": ["attestator_33"]},
        witness_chairs=("attestator_1",),
    )

    with pytest.raises(SchemaRefusal, match="outside the configured witness roster"):
        attestatores.declared_page_witness_chairs(context)


def test_the_roster_refusal_names_the_roster_and_not_only_the_offender():
    """A usable roster mismatch names both the declaration and sealed roster."""
    context = SimpleNamespace(
        fixture={"page_witness_chairs": ["attestator_33"]},
        witness_chairs=("attestator_1", "attestator_3"),
    )

    with pytest.raises(SchemaRefusal) as caught:
        attestatores.declared_page_witness_chairs(context)
    message = str(caught.value)
    assert "attestator_33" in message
    assert "attestator_1" in message and "attestator_3" in message


@pytest.mark.parametrize(
    "bad_chair",
    (
        float("nan"),
        float("inf"),
        1.5,
        True,
        pytest.param(10**5000, id="huge-int"),
        None,
        # The accessor accepts in-memory fixtures, so it must refuse cycles
        # without recursively inspecting or hashing them.
        "recursive",
    ),
)
def test_declared_page_witness_chairs_refuses_values_no_chair_name_could_be(bad_chair):
    """Type validation must precede hashing, traversal, and value rendering."""
    if bad_chair == "recursive":
        recursive: list = []
        recursive.append(recursive)
        bad_chair = recursive
    context = SimpleNamespace(fixture={"page_witness_chairs": [bad_chair]})

    with pytest.raises(SchemaRefusal, match="unique string list"):
        attestatores.declared_page_witness_chairs(context)


@pytest.mark.parametrize(
    "chair",
    (
        pytest.param(_UnhashableString("attestator_1"), id="unhashable-string-subclass"),
        pytest.param(_HostileReprString("attestator_33"), id="hostile-repr-string-subclass"),
    ),
)
def test_a_chair_name_string_subclass_is_refused_before_set_or_rendering(chair):
    context = SimpleNamespace(
        fixture={"page_witness_chairs": [chair]},
        witness_chairs=("attestator_1",),
    )

    with pytest.raises(SchemaRefusal, match="unique string list"):
        attestatores.declared_page_witness_chairs(context)


def test_a_chair_name_carrying_a_surrogate_is_refused_printably():
    """The roster refusal must remain encodable when the chair name is not."""
    context = SimpleNamespace(
        fixture={"page_witness_chairs": ["attestator_\ud800"]},
        witness_chairs=("attestator_1",),
    )

    with pytest.raises(SchemaRefusal) as caught:
        attestatores.declared_page_witness_chairs(context)
    str(caught.value).encode("utf-8")


@pytest.mark.parametrize("chair", ("NaN", "attestator_\0"))
def test_hostile_but_encodable_chair_strings_are_refused_printably(chair):
    context = SimpleNamespace(
        fixture={"page_witness_chairs": [chair]},
        witness_chairs=("attestator_1",),
    )

    with pytest.raises(SchemaRefusal) as caught:
        attestatores.declared_page_witness_chairs(context)
    str(caught.value).encode("utf-8")


def test_no_testimonium_is_sealed_before_the_declaration_is_validated():
    """A malformed declaration must refuse before immutable testimony is written."""
    published: list = []
    resolved = AbsentChair(role="attestator_1", reason="fixture test needs no live chair")
    context = SimpleNamespace(
        fixture={"page_witness_chairs": ["attestator_33"]},
        witness_chairs=("attestator_1",),
        adapter_revision="fake-attestatores-v0",
        publish=lambda **kwargs: published.append(kwargs),
    )

    with pytest.raises(SchemaRefusal, match="outside the configured witness roster"):
        attestatores.publish_attempt(
            context,
            act={"act_id": "act_0123456789abcdef", "act_key": "1-1"},
            chair="attestator_1",
            resolved=resolved,
            ordinal=1,
            regions=[],
            attempt=attestatores.dead_attempt(resolved),
        )
    assert published == [], "an attempt sealed before the declaration was validated"


class _FunctionPublishCalls(ast.NodeVisitor):
    """Calls and aliases of ``.publish`` in one top-level function body."""

    def __init__(self):
        self.calls: list[ast.Call] = []
        self.attributes: list[ast.Attribute] = []
        self.bypass_lines: list[int] = []

    def visit_FunctionDef(self, node):
        # Nested functions are independent write paths; the outer scan visits
        # them separately and must not fold them into their parent's proof.
        return

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Attribute(self, node):
        if node.attr == "publish":
            self.attributes.append(node)
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "publish":
            self.calls.append(node)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "publish_artifact",
            "_publish_bytes",
            "write_bytes",
            "write_text",
        }:
            self.bypass_lines.append(node.lineno)
        if isinstance(node.func, ast.Name) and node.func.id in {"open", "_atomic_create"}:
            self.bypass_lines.append(node.lineno)
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "publish"
        ):
            self.bypass_lines.append(node.lineno)
        self.generic_visit(node)


def _dominating_declaration_line(function: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """A direct top-level declaration call, or -1 when none exists.

    A top-level expression or assignment must execute before Python can reach a
    later statement in the function. Merely finding the call anywhere in the
    body is not enough: it may sit under a branch that never runs while a publish
    below it still does.
    """
    for statement in function.body:
        value = None
        if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr)):
            value = statement.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "declared_page_witness_chairs"
        ):
            return statement.lineno
    return -1


def _testimonium_write_scan(source: str):
    """Return Testimonium writers plus publish forms the proof cannot classify."""
    writers: dict[str, tuple[int, list[int]]] = {}
    dynamic: dict[str, list[int]] = {}
    aliases: dict[str, list[int]] = {}
    bypasses: dict[str, list[int]] = {}
    tree = ast.parse(source)
    top_level = {id(node) for node in tree.body}
    for function in (
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        function_name = (
            function.name if id(function) in top_level else f"{function.name}@{function.lineno}"
        )
        visitor = _FunctionPublishCalls()
        for statement in function.body:
            visitor.visit(statement)

        direct_attributes = {id(call.func) for call in visitor.calls}
        indirect = [node.lineno for node in visitor.attributes if id(node) not in direct_attributes]
        if indirect:
            aliases[function_name] = indirect
        if visitor.bypass_lines:
            bypasses[function_name] = visitor.bypass_lines

        for call in visitor.calls:
            kind_keywords = [keyword.value for keyword in call.keywords if keyword.arg == "kind"]
            if (
                len(kind_keywords) != 1
                or not isinstance(kind_keywords[0], ast.Constant)
                or not isinstance(kind_keywords[0].value, str)
            ):
                dynamic.setdefault(function_name, []).append(call.lineno)
                continue
            if kind_keywords[0].value in {"testimonium", "page-testimonium"}:
                declaration_line = _dominating_declaration_line(function)
                writers.setdefault(function_name, (declaration_line, []))[1].append(call.lineno)
    return writers, dynamic, aliases, bypasses


def test_the_write_scan_detects_a_third_path_even_when_its_syntax_changes():
    """The source pin must reject unclassified writes, aliases, and hidden validation."""
    literal = """
def third(context):
    declared_page_witness_chairs(context)
    context.publish(kind = 'testimonium', payload={})
"""
    dynamic = """
def third(context):
    declared_page_witness_chairs(context)
    kind = 'testimonium'
    context.publish(kind=kind, payload={})
"""
    aliased = """
def third(context):
    declared_page_witness_chairs(context)
    writer = context.publish
    writer(kind='testimonium', payload={})
"""
    nested = """
def wrapper(context):
    def third():
        context.publish(kind='testimonium', payload={})
    third()
"""
    lower_level = """
def third(context):
    context.tree.publish_artifact({'kind': 'testimonium'})
"""
    reflected = """
def third(context):
    writer = getattr(context, 'publish')
    writer(kind='testimonium', payload={})
"""
    conditional = """
def third(context):
    if False:
        declared_page_witness_chairs(context)
    context.publish(kind='testimonium', payload={})
"""

    assert set(_testimonium_write_scan(literal)[0]) == {"third"}
    assert set(_testimonium_write_scan(dynamic)[1]) == {"third"}
    assert set(_testimonium_write_scan(aliased)[2]) == {"third"}
    assert next(iter(_testimonium_write_scan(nested)[0])).startswith("third@")
    assert set(_testimonium_write_scan(lower_level)[3]) == {"third"}
    assert set(_testimonium_write_scan(reflected)[3]) == {"third"}
    assert _testimonium_write_scan(conditional)[0]["third"][0] == -1


def test_every_testimonium_writer_has_a_dominating_declaration_call():
    """Every Testimonium writer must validate in an unconditional earlier statement.

    Runtime fixtures cannot detect an uncalled future writer. This source pin
    therefore rejects new, indirect, or lower-level write paths it cannot prove.
    """
    module_path = Path(__file__).resolve().parent / "run.py"
    writers, dynamic, aliases, bypasses = _testimonium_write_scan(
        module_path.read_text(encoding="utf-8")
    )

    assert set(writers) == {"publish_attempt", "publish_page_testimonia_and_attachments"}, (
        f"{module_path} publishes a Testimonium from {sorted(writers)}; a new write path "
        "must validate the page-witness declaration before it seals, and this scan is what "
        "notices it was added"
    )
    assert dynamic == {}, (
        f"{module_path} has publish calls with a non-literal or missing kind at {dynamic}; "
        "the two-function Testimonium-write proof cannot classify them"
    )
    assert aliases == {}, (
        f"{module_path} aliases a publish method at {aliases}; the two-function "
        "Testimonium-write proof cannot follow indirect calls"
    )
    assert bypasses == {}, (
        f"{module_path} reaches a lower-level or raw write sink at {bypasses}; all stage "
        "artifacts must pass through the context publisher for this proof to be complete"
    )
    for name, (declaration_line, publish_lines) in writers.items():
        assert declaration_line >= 0 and declaration_line < min(publish_lines), (
            f"{name} seals a Testimonium before validating the fixture's page-witness "
            "declaration; a sealed record is immutable, so a refusal after the write "
            "cannot take back the wrong page_witness flag it carries"
        )


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
