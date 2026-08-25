"""Consult §7/§8.6 static guards for Unit 19D's own new surface.

`common/test_unit19_no_picker.py` guards Unit 19A's identity surface and says
plainly that the guards for 19B-19D "still have to build." Neither 19B nor 19C
added a static screen for their own new files, and 19D's build (the logical
Archetypus record/index, the Armarium's logical projection entry, and the new
`cross-capture-dissent.v1` module) landed the same way: exercised by
`pipeline/4_perlector/test_cross_capture_cluster_path.py`'s fixture, but never
scanned for the forbidden shapes §7 names as binding review vocabulary.

This closes that gap for 19D's own additions only. It reuses §7's exact word
and call lists from the 19A guard rather than restating them, so the two files
cannot drift about what "forbidden" means.

The Archetypus and Armarium stage files carry a great deal of unrelated,
legitimate code -- `established[0]` behind a proven `len(...) == 1` guard,
`Path(...).parents[2]`, `__doc__.splitlines()[0]` -- so this scans the exact
new function bodies 19D added, not the whole file, the same way the 19A guard
scans its own single narrow module rather than every file that imports it.
"""

import ast
from pathlib import Path

from common.test_unit19_no_picker import FORBIDDEN_CALLS, SHAPE_ONE_WORDS

ROOT = Path(__file__).resolve().parent.parent
DISSENT_SOURCE = ROOT / "common" / "cross_capture_dissent.py"
ARCHETYPUS_SOURCE = ROOT / "pipeline" / "6_archetypus" / "run.py"
ARMARIUM_SOURCE = ROOT / "pipeline" / "7_armarium" / "run.py"

# The exact new callables 19D added to each stage file. A whole-file scan would
# false-positive on unrelated, already-guarded code (`established[0]` behind a
# `len(established) != 1` refusal, path/doc-string slicing); a name that stops
# matching anything here is itself worth noticing; see the completeness test.
ARCHETYPUS_LOGICAL_FUNCTIONS = frozenset(
    {
        "validate_logical_record",
        "_logical_sha",
        "_logical_component",
        "_logical_member",
        "establish_logical_record",
        # The evidence cross-check `establish_logical_record` delegates to. It
        # reads a partition, an autopsia and a dissent to decide whether the
        # row may supply this record's provenance, which is precisely where a
        # "take the row that fits best" shape would be born.
        "_require_the_partition_this_reading_was_made_over",
        "_require_joint_evidence_binding",
        "build_logical_index",
    }
)
ARMARIUM_LOGICAL_FUNCTIONS = frozenset(
    {"logical_act_projection_entry", "logical_cross_capture_review_entry"}
)


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _functions_named(tree: ast.Module, names: frozenset[str]) -> list[ast.FunctionDef]:
    found = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    missing = names - {node.name for node in found}
    assert not missing, f"expected function(s) {sorted(missing)} not found; guard is stale"
    return found


def _identifiers(node: ast.AST) -> set[str]:
    read: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            read.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            read.add(sub.attr)
        elif isinstance(sub, ast.arg):
            read.add(sub.arg)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            read.add(sub.value)
    return read


def _calls(node: ast.AST) -> set[str]:
    return {
        sub.func.id
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
    }


def _int_subscripts(node: ast.AST) -> list[ast.Subscript]:
    return [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Subscript)
        and isinstance(sub.slice, ast.Constant)
        and isinstance(sub.slice.value, int)
    ]


def test_the_whole_new_dissent_module_names_no_shape_one_preference_word():
    """§7 shape 1 over every line `common/cross_capture_dissent.py` added."""
    read = _identifiers(_module(DISSENT_SOURCE))
    assert not read & SHAPE_ONE_WORDS, sorted(read & SHAPE_ONE_WORDS)


def test_the_whole_new_dissent_module_calls_no_forbidden_selector():
    """§7 shapes 2, 3, 11 over the new dissent module."""
    tree = _module(DISSENT_SOURCE)
    calls = _calls(tree)
    assert not calls & FORBIDDEN_CALLS, sorted(calls & FORBIDDEN_CALLS)
    assert _int_subscripts(tree) == [], [ast.unparse(n) for n in _int_subscripts(tree)]


def test_the_new_archetypus_logical_functions_name_no_shape_one_preference_word():
    """§7 shape 1 over exactly the functions 19D added to Archetypus."""
    tree = _module(ARCHETYPUS_SOURCE)
    for node in _functions_named(tree, ARCHETYPUS_LOGICAL_FUNCTIONS):
        read = _identifiers(node)
        assert not read & SHAPE_ONE_WORDS, (node.name, sorted(read & SHAPE_ONE_WORDS))


def test_the_new_archetypus_logical_functions_call_no_forbidden_selector():
    """§7 shapes 2, 3, 11 over exactly the functions 19D added to Archetypus."""
    tree = _module(ARCHETYPUS_SOURCE)
    for node in _functions_named(tree, ARCHETYPUS_LOGICAL_FUNCTIONS):
        calls = _calls(node)
        assert not calls & FORBIDDEN_CALLS, (node.name, sorted(calls & FORBIDDEN_CALLS))
        subs = _int_subscripts(node)
        assert subs == [], (node.name, [ast.unparse(n) for n in subs])


def test_the_new_armarium_logical_projection_names_no_shape_one_preference_word():
    """§7 shape 1 over exactly the function 19D added to Armarium."""
    tree = _module(ARMARIUM_SOURCE)
    for node in _functions_named(tree, ARMARIUM_LOGICAL_FUNCTIONS):
        read = _identifiers(node)
        assert not read & SHAPE_ONE_WORDS, (node.name, sorted(read & SHAPE_ONE_WORDS))


def test_the_new_armarium_logical_projection_calls_no_forbidden_selector():
    """§7 shapes 2, 3, 11 over exactly the function 19D added to Armarium."""
    tree = _module(ARMARIUM_SOURCE)
    for node in _functions_named(tree, ARMARIUM_LOGICAL_FUNCTIONS):
        calls = _calls(node)
        assert not calls & FORBIDDEN_CALLS, (node.name, sorted(calls & FORBIDDEN_CALLS))
        subs = _int_subscripts(node)
        assert subs == [], (node.name, [ast.unparse(n) for n in subs])


def test_no_export_emits_capture_member_acts_beside_the_logical_act():
    """§7 shape 15/19: the Armarium logical projection field set is closed and

    carries no per-member act_id/act_key -- only the logical subject and the
    member lists retained as opaque provenance, never re-exported as their own
    rows.
    """
    import sys  # noqa: PLC0415

    sys.path.insert(0, str(ARMARIUM_SOURCE.parent))
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("u19d_no_picker_armarium", ARMARIUM_SOURCE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    from common.contracts.canonical import digest_of, self_hash  # noqa: PLC0415
    from common.contracts.errors import SchemaRefusal  # noqa: PLC0415
    from common.contracts.uncertainty import from_perlectio  # noqa: PLC0415

    text = "established text"
    source = "a" * 64
    record = {
        "logical_act_id": "pac_0123456789abcdef",
        "physical_page_components": [
            {
                "physical_page_id": "ppg_0123456789abcdef",
                "required_capture_sha256s": [source],
            }
        ],
        "member_local_acts": [
            {
                "act_id": "act_0123456789abcdef",
                "act_key": "member-key",
                "page_id": "pg_0123456789abcdef",
                "page_ordinal": 1,
                "source_sha256": source,
                "proposal_refs": ["proposal:member"],
            }
        ],
        "text": text,
        "text_hash": digest_of(text),
        "status": "established",
        "text_status": "established",
        "regions": [{"region_id": "rgn_0123456789abcdef"}],
        "provenance": {"chair": "perlector", "revision": "fixture"},
        "annotations": [],
        "uncertainty": from_perlectio(
            {"text": text, "uncertain_spans": [], "gaps": [], "self_revision": []}
        ),
        "evidence_ref": None,
        "cross_capture_dissent_ref": {"relative_path": "dissent", "sha256": "0" * 64},
        "perlectio_ref": {"relative_path": "perlectio", "sha256": "1" * 64},
        "recensor_ref": {"relative_path": "review", "sha256": "2" * 64},
    }
    record["self_hash"] = self_hash(record)
    entry = module.logical_act_projection_entry(
        record, category="delivered", source_regions=[], witnesses=[]
    )
    # The member's own local act_id/act_key never leaks into the projected
    # export identity, which is derived from the logical subject alone.
    assert entry["act_id"] == "pac_0123456789abcdef"
    assert entry["act_key"] != "member-key"
    assert "act_0123456789abcdef" not in (entry["act_id"], entry["act_key"])

    # A forged self-hash or text_hash, or a memberless record, must never
    # reach export as though it were a genuine, sealed logical act.
    for bad in (
        {
            **record,
            "member_local_acts": [],
            "self_hash": self_hash({**record, "member_local_acts": []}),
        },
        {
            **record,
            "physical_page_components": [],
            "self_hash": self_hash({**record, "physical_page_components": []}),
        },
        {**record, "text_hash": "0" * 64},
        {**record, "self_hash": "0" * 64},
    ):
        try:
            module.logical_act_projection_entry(
                bad, category="delivered", source_regions=[], witnesses=[]
            )
        except SchemaRefusal:
            continue
        raise AssertionError(f"a malformed logical record reached export unrefused: {bad}")
