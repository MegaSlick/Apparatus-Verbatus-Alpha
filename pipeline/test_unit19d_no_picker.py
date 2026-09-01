"""Consult §7/§8.6 static guards for Unit 19D's own new surface.

`common/test_unit19_no_picker.py` guards Unit 19A's identity surface and says
plainly that the guards for 19B-19D "still have to build." Neither 19B nor 19C
added a static screen for their own new files, and 19D's build (the logical
Archetypus record/index, the Armarium's logical projection and readable export
path, and the new `cross-capture-dissent.v1` module) landed the same way: exercised by
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
ARMARIUM_EXPORT_SOURCE = ROOT / "pipeline" / "7_armarium" / "armarium_export.py"

# The exact new callables 19D added to each stage file, followed by the existing
# bundle route that logical multi-capture output newly activates. A whole-file scan would
# false-positive on unrelated, already-guarded code (`established[0]` behind a
# `len(established) != 1` refusal, path/doc-string slicing); a name that stops
# matching anything here is itself worth noticing -- `_functions_named` fails
# on a stale name, though nothing mechanically proves the list is complete:
# a reviewer adding a 19D callable must add it here too.
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
ARMARIUM_EXPORT_LOGICAL_FUNCTIONS = frozenset(
    {
        "_act_partition_claim",
        "_validate_logical_act_conservation",
        # The membership rows both the manifest claim and `sources.json` carry,
        # and the clean-machine recompute that decides whether a rebuilt
        # bundle's member accounting is honest. Both walk member ids, keys and
        # page ordinals, which is where a positional read would be born.
        "_logical_membership_map",
        "_verify_logical_partition_claim",
        # The bundle writer groups output by source folder. A positional read
        # here would let region order pick one capture to represent a logical
        # act, even though the projection itself retained every capture.
        "_text_bundle_members",
    }
)


def _module(path: Path) -> ast.Module:
    # Pinned encoding: the scanned files carry non-ASCII characters, and a
    # guard that cannot run on a non-UTF-8 locale is a failure, not a pass.
    return ast.parse(path.read_text(encoding="utf-8"))


def _functions_named(tree: ast.Module, names: frozenset[str]) -> list[ast.FunctionDef]:
    found = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    missing = names - {node.name for node in found}
    assert not missing, f"expected function(s) {sorted(missing)} not found; guard is stale"
    return found


def _identifiers(node: ast.AST) -> set[str]:
    """Names in the forms a preference claim would take: identifiers,
    attributes, arguments, and whole string constants (a flag value or field
    name). Deliberately not a tokenizer over prose -- docstrings and refusal
    messages legitimately use words like "canonical" and "selected" in their
    serialization and product-format senses, and wording is review's business;
    the mechanical enforcement against preference FIELDS at any depth is the
    runtime screens (`refuse_capture_preference`, `_refuse_scalar_claim_keys`),
    which these shapes-1 scans complement rather than replace."""
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
    calls = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        if isinstance(sub.func, ast.Name):
            calls.add(sub.func.id)
        elif isinstance(sub.func, ast.Attribute):
            calls.add(sub.func.attr)
    return calls


def _int_subscripts(node: ast.AST) -> list[ast.Subscript]:
    def is_integer_literal(value: ast.AST) -> bool:
        if isinstance(value, ast.Constant):
            return isinstance(value.value, int) and not isinstance(value.value, bool)
        return (
            isinstance(value, ast.UnaryOp)
            and isinstance(value.op, (ast.UAdd, ast.USub))
            and isinstance(value.operand, ast.Constant)
            and isinstance(value.operand.value, int)
            and not isinstance(value.operand.value, bool)
        )

    return [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Subscript) and is_integer_literal(sub.slice)
    ]


def test_the_static_guard_sees_qualified_calls_and_negative_positions():
    synthetic = ast.parse("random.choice(rows)\nlast = rows[-1]")
    assert _calls(synthetic) == {"choice"}
    assert [ast.unparse(node) for node in _int_subscripts(synthetic)] == ["rows[-1]"]


def test_the_whole_new_dissent_module_names_no_shape_one_preference_word():
    """§7 shape 1 over every name `common/cross_capture_dissent.py` added.

    Names, not prose: see `_identifiers` for what this scan can and cannot
    catch, and the runtime preference screens for the field-level enforcement.
    """
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


def test_the_armarium_logical_export_path_names_no_preference_or_selector():
    """§7 shapes 1-3/11 through conservation and the human-readable bundle."""
    tree = _module(ARMARIUM_EXPORT_SOURCE)
    for node in _functions_named(tree, ARMARIUM_EXPORT_LOGICAL_FUNCTIONS):
        read = _identifiers(node)
        assert not read & SHAPE_ONE_WORDS, (node.name, sorted(read & SHAPE_ONE_WORDS))
        calls = _calls(node)
        assert not calls & FORBIDDEN_CALLS, (node.name, sorted(calls & FORBIDDEN_CALLS))
        subs = _int_subscripts(node)
        assert subs == [], (node.name, [ast.unparse(n) for n in subs])


def test_the_logical_projection_carries_no_member_act_rows_beside_its_subject(monkeypatch):
    """§7 shape 15/19: the Armarium logical projection field set is closed and

    carries no per-member act_id/act_key -- only the logical subject and the
    member lists retained as opaque provenance, never re-exported as their own
    rows. Exercised through `logical_act_projection_entry`'s derived identity
    and its refusals; the export-side double-count screen has its own test in
    the cluster-path suite.
    """
    # syspath scoped to this test: pipeline/7_armarium holds run.py and
    # display.py, and a leaked path entry would let any later test in the
    # session import the Armarium's module under a generic name.
    monkeypatch.syspath_prepend(str(ARMARIUM_SOURCE.parent))
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
        record, category="delivered", source_regions=record["regions"], witnesses=[]
    )
    # The member's own local act_id/act_key never leaks into the projected
    # export identity, which is derived from the logical subject alone.
    assert entry["act_id"] == "pac_0123456789abcdef"
    assert entry["act_key"] != "member-key"
    assert "act_0123456789abcdef" not in (entry["act_id"], entry["act_key"])

    # Not re-exported as its own row is only half of §5.2; the other half is
    # retained under the one logical entry. Without these, the test passes for a
    # `logical_act_projection_entry` that drops `logical_membership` entirely --
    # which would take the member's local act and its capture attribution out of
    # the export while this test still reported the projection protected.
    assert entry["logical_membership"] == {
        "member_local_act_ids": ["act_0123456789abcdef"],
        "member_act_keys": ["member-key"],
        "member_source_page_ordinals": [1],
        "physical_page_components": [
            {
                "physical_page_id": "ppg_0123456789abcdef",
                "required_capture_sha256s": [source],
            }
        ],
    }

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
        # Self-hash recomputed over the forged digest on purpose: with the
        # original self_hash left in place, verify_self_hash refuses first and
        # the text_hash comparison is never reached, so this variant would
        # duplicate the self_hash case below instead of covering its own guard.
        {
            **record,
            "text_hash": "0" * 64,
            "self_hash": self_hash({**record, "text_hash": "0" * 64}),
        },
        {**record, "self_hash": "0" * 64},
    ):
        try:
            module.logical_act_projection_entry(
                bad, category="delivered", source_regions=bad["regions"], witnesses=[]
            )
        except SchemaRefusal:
            continue
        raise AssertionError(f"a malformed logical record reached export unrefused: {bad}")

    # A forger recomputes the self-hash, so the member's `source_sha256` reaches
    # the `set(...)` that builds `member_sources` with only its field name
    # proved. An unhashable value there raised TypeError -- not a refused
    # record but an ended export, taking every other act in the run with it.
    # Its two neighbours in the same member row were already checked; this one
    # was not.
    for forged_source in ([], {}, 7, "not-a-digest", "g" * 64, "A" * 64):
        member = {**record["member_local_acts"][0], "source_sha256": forged_source}
        bad = {**record, "member_local_acts": [member]}
        # Recomputed on purpose: `verify_self_hash` runs before the member loop,
        # so with the original hash left in place this would only re-prove the
        # seal check and never reach the guard under test.
        bad["self_hash"] = self_hash(bad)
        try:
            module.logical_act_projection_entry(
                bad, category="delivered", source_regions=bad["regions"], witnesses=[]
            )
        except SchemaRefusal as refusal:
            assert "source capture digest" in str(refusal), (
                f"a member source digest {forged_source!r} was refused for some other "
                f"reason than its own: {refusal}"
            )
            continue
        raise AssertionError(
            f"a member carrying source_sha256={forged_source!r} reached export unrefused"
        )
