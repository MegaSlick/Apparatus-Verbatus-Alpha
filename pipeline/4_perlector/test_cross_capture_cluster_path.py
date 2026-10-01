"""The cross-capture boundary: a deep autopsia view is refused by name, and no
established stage dispatches by logical act.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import SchemaRefusal  # noqa: E402
from common.cross_capture_autopsia import (  # noqa: E402
    build_autopsia,
)

ARCHETYPUS_RUN = ROOT / "pipeline" / "6_archetypus" / "run.py"
ARMARIUM_RUN = ROOT / "pipeline" / "7_armarium" / "run.py"


def test_a_deeply_nested_autopsia_view_becomes_a_refusal_not_a_recursion_crash():
    """`_reject_preference` walks `views` before `_view` proves its shape
    (`common/cross_capture_autopsia.py`), so an unvalidated caller can nest it
    past Python's recursion limit. That must become a `SchemaRefusal`, never an
    uncaught `RecursionError` that would crash the whole stage process and take
    every other logical act in the run down with it. The composed screen walks
    iteratively (14A), so a deep nest is either named by a recursion guard (a
    recursive walk) or walked whole and refused at the closed-shape check --
    both are named refusals, and both prove the boundary."""
    nested: Any = "leaf"
    for _ in range(5000):
        nested = {"views": nested}
    with pytest.raises(SchemaRefusal, match="nests too deeply|closed schema"):
        build_autopsia(
            logical_act_id="pac_0123456789abcdef",
            partition_ref={
                "relative_path": "4_perlector/blobs/physical-act-partition.json",
                "sha256": digest_bytes(b"partition"),
            },
            required_capture_sha256s=[digest_bytes(b"capture")],
            views=[nested],
        )


def test_neither_established_stage_dispatches_by_logical_act_yet():
    """No established stage's `main()` dispatches by logical act.

    `establish_logical_record`, `build_logical_index`, the delivered logical
    projection and the cross-capture review projection exist, but the Perlector
    reads whole pages and publishes no clustered Perlectio for them to consume.
    A change that wires either stage by logical act must flip this test.
    """
    logical_callables = (
        "establish_logical_record",
        "build_logical_index",
        "logical_act_projection_entry",
        "logical_cross_capture_review_entry",
    )
    for path in (ARCHETYPUS_RUN, ARMARIUM_RUN):
        tree = ast.parse(path.read_text())
        mains = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
        ]
        assert len(mains) == 1, (
            f"expected exactly one main() in the stage, found {len(mains)}; the "
            "clustered-dispatch gap pin cannot say whether the stage dispatches "
            "by logical act without its entry point"
        )
        (main,) = mains
        # Attribute calls too: wiring the clustered path as
        # `archetypus.establish_logical_record(...)` must move this gap pin,
        # not slip past a bare-name-only scan.
        called = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
        }
        assert not called & set(logical_callables), (
            f"{path.name}::main now dispatches by logical act; the composed-acceptance gap "
            "this test records has closed"
        )
