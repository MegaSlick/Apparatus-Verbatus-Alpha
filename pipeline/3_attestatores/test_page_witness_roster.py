"""The sealed page-witness roster, read and refused before any page record is written."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.chairs.models import ChairIdentity
from common.contracts.errors import SchemaRefusal
from conftest import load_stage

attestatores = load_stage("3_attestatores")


class _UnhashableString(str):
    __hash__ = None


class _HostileReprString(str):
    def __repr__(self):
        raise RuntimeError("the refusal rendered an untrusted chair-name subclass")


def _identity(role: str, scope: str) -> ChairIdentity:
    return ChairIdentity(
        role=role,
        source="local-repository",
        repo=None,
        path=role,
        revision=None,
        digest_manifest="a" * 64,
        manifest=f"manifests/{role}.json",
        adapter_of=None,
        serving_recipe="fixture",
        license_note="fixture",
        witness_adapter="churro.v1",
        witness_scope=scope,
    )


def _scope_context(chairs=None, *, scopes=None, **fields):
    scopes = scopes or {"attestator_1": "page", "attestator_3": "page"}
    configured = {role: _identity(role, scope) for role, scope in scopes.items()}
    return SimpleNamespace(
        witness_chairs=list(scopes) if chairs is None else chairs,
        registry=SimpleNamespace(config=SimpleNamespace(chairs=configured)),
        **fields,
    )


@pytest.mark.parametrize(
    "bad_chair",
    ([], {}, [[]], {"nested": []}, {"a": {"b": [1, 2]}}, [[[]]], [{"a": [{}]}]),
)
def test_declared_page_witness_chairs_refuses_unhashable_json_values(bad_chair):
    context = _scope_context([bad_chair])

    with pytest.raises(SchemaRefusal, match="unique list of chair names"):
        attestatores.declared_page_witness_chairs(context)


def test_an_unknown_page_witness_chair_is_refused_before_the_pass_writes():
    context = _scope_context(["attestator_33"])

    with pytest.raises(SchemaRefusal, match="absent from the current models configuration"):
        attestatores.preflight(context, [], 1, {}, fixture=False)


def test_an_unknown_page_witness_chair_is_refused_by_the_shared_accessor_itself():
    """Every page-record writer reads `declared_page_witness_chairs` directly, so
    the roster check must live in the accessor itself or a mismatched chair
    silently reaches a write path unrefused."""
    context = _scope_context(["attestator_33"])

    with pytest.raises(SchemaRefusal, match="absent from the current models configuration"):
        attestatores.declared_page_witness_chairs(context)


def test_the_roster_refusal_names_the_roster_and_not_only_the_offender():
    context = _scope_context(["attestator_33"])

    with pytest.raises(SchemaRefusal) as caught:
        attestatores.declared_page_witness_chairs(context)
    message = str(caught.value)
    assert "attestator_33" in message
    assert "attestator_1" in message and "attestator_3" in message
    assert "do not describe the same witness set" in message
    assert "start a new run; do not edit sealed evidence" in message


@pytest.mark.parametrize(
    "bad_chair",
    (
        float("nan"),
        float("inf"),
        1.5,
        True,
        pytest.param(10**5000, id="huge-int"),
        None,
        # Recursive values can reach direct callers; validation must not walk
        # them, hash them, or render them.
        "recursive",
    ),
)
def test_declared_page_witness_chairs_refuses_values_no_chair_name_could_be(bad_chair):
    if bad_chair == "recursive":
        recursive: list = []
        recursive.append(recursive)
        bad_chair = recursive
    context = _scope_context([bad_chair])

    with pytest.raises(SchemaRefusal, match="unique list of chair names"):
        attestatores.declared_page_witness_chairs(context)


@pytest.mark.parametrize(
    "chair",
    (
        pytest.param(_UnhashableString("attestator_1"), id="unhashable-string-subclass"),
        pytest.param(_HostileReprString("attestator_33"), id="hostile-repr-string-subclass"),
    ),
)
def test_a_chair_name_string_subclass_is_refused_before_set_or_rendering(chair):
    context = _scope_context([chair])

    with pytest.raises(SchemaRefusal, match="unique list of chair names"):
        attestatores.declared_page_witness_chairs(context)


def test_a_chair_name_carrying_a_surrogate_is_refused_printably():
    """A chair name is a string, so it clears the shape check and reaches the
    roster refusal — which then puts it in a message an operator's stderr has to
    encode. `repr` escapes the surrogate; an f-string interpolating it raw would
    raise `UnicodeEncodeError` out of the report of the refusal."""
    context = _scope_context(["attestator_\ud800"])

    with pytest.raises(SchemaRefusal) as caught:
        attestatores.declared_page_witness_chairs(context)
    str(caught.value).encode("utf-8")


@pytest.mark.parametrize("chair", ("NaN", "attestator_\0"))
def test_hostile_but_encodable_chair_strings_are_refused_printably(chair):
    context = _scope_context([chair])

    with pytest.raises(SchemaRefusal) as caught:
        attestatores.declared_page_witness_chairs(context)
    str(caught.value).encode("utf-8")


def test_no_testimonium_is_sealed_before_the_declaration_is_validated():
    """The timing guarantee, driven rather than reasoned about.

    `publish_page_testimonium` builds its payload first and publishes last, so
    the constraint is that the roster is read on the near side of the write. A
    recording context answers it: the refusal must arrive with the publish list
    still empty, because a page record sealed for a chair the sealed roster does
    not name is immutable and nothing later can take it back.
    """
    published: list = []
    context = _scope_context(
        ["attestator_33"],
        adapter_revision="fake-attestatores-v0",
        publish=lambda **kwargs: published.append(kwargs),
    )

    with pytest.raises(SchemaRefusal, match="absent from the current models configuration"):
        attestatores.publish_page_testimonium(
            context,
            chair="attestator_1",
            resolved=_identity("attestator_1", "page"),
            page_ordinal=1,
            attempt=attestatores.not_run_attempt("fixture test needs no live chair"),
            ordinal=1,
            page_ids={1: "page-1"},
            live=False,
        )
    assert published == [], "a page record sealed before the declaration was validated"


def test_a_page_record_for_a_chair_outside_the_page_roster_is_refused_before_it_is_built():
    published: list = []
    context = _scope_context(publish=lambda **kwargs: published.append(kwargs))

    with pytest.raises(attestatores.FatalAccounting, match="not a page witness"):
        attestatores.publish_page_testimonium(
            context,
            chair="attestator_2",
            resolved=_identity("attestator_2", "page"),
            page_ordinal=1,
            attempt=attestatores.not_run_attempt("fixture test needs no live chair"),
            ordinal=1,
            page_ids={1: "page-1"},
            live=False,
        )
    assert published == []


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
            if kind_keywords[0].value == "page-testimonium":
                declaration_line = _dominating_declaration_line(function)
                writers.setdefault(function_name, (declaration_line, []))[1].append(call.lineno)
    return writers, dynamic, aliases, bypasses


def test_the_write_scan_detects_a_third_path_even_when_its_syntax_changes():
    literal = """
def third(context):
    declared_page_witness_chairs(context)
    context.publish(kind = 'page-testimonium', payload={})
"""
    dynamic = """
def third(context):
    declared_page_witness_chairs(context)
    kind = 'page-testimonium'
    context.publish(kind=kind, payload={})
"""
    aliased = """
def third(context):
    declared_page_witness_chairs(context)
    writer = context.publish
    writer(kind='page-testimonium', payload={})
"""
    nested = """
def wrapper(context):
    def third():
        context.publish(kind='page-testimonium', payload={})
    third()
"""
    lower_level = """
def third(context):
    context.tree.publish_artifact({'kind': 'page-testimonium'})
"""
    reflected = """
def third(context):
    writer = getattr(context, 'publish')
    writer(kind='page-testimonium', payload={})
"""
    conditional = """
def third(context):
    if False:
        declared_page_witness_chairs(context)
    context.publish(kind='page-testimonium', payload={})
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

    assert set(writers) == {
        "publish_page_testimonium",
        "publish_detector_page_testimonium",
    }, (
        f"{module_path} publishes a Testimonium from {sorted(writers)}; a new write path "
        "must validate sealed page-witness scope before it seals, and this scan is what "
        "notices it was added"
    )
    assert dynamic == {}, (
        f"{module_path} has publish calls with a non-literal or missing kind at {dynamic}; "
        "the Testimonium-write proof cannot classify them"
    )
    assert aliases == {}, (
        f"{module_path} aliases a publish method at {aliases}; the "
        "Testimonium-write proof cannot follow indirect calls"
    )
    assert bypasses == {}, (
        f"{module_path} reaches a lower-level or raw write sink at {bypasses}; all stage "
        "artifacts must pass through the context publisher for this proof to be complete"
    )
    for name, (declaration_line, publish_lines) in writers.items():
        assert declaration_line >= 0 and declaration_line < min(publish_lines), (
            f"{name} seals a Testimonium before validating sealed page-witness scope; "
            "a sealed record is immutable, so a refusal after the write "
            "cannot take back a record for a chair the roster does not name"
        )
