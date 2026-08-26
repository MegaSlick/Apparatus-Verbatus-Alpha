"""`edge_deltas` are correspondence evidence, and may never become a trigger.

The dossier records four signed offsets per observed box: how one chair's native
or derived geometry sits against this act's sealed proposal. The comparison is
never chair against chair, and the magnitudes may not feed n-of-m agreement,
IoU or similarity, thresholds, per-chair weights, or two-chair disagreement.

The recovery gate itself is already fenced structurally by
`pipeline/5_recensor/test_quality_firewall.py`, which pins the names the gate may
consult and the whole derivation of `wants_recovery`. What that firewall cannot
see is a magnitude thresholded *before* it reaches the gate, in the stage that
records these numbers. This module combines behavioral checks with narrow
syntax tripwires around the direct spellings of those mistakes:

- one chair at a time, against sealed geometry, with no second chair in reach;
- recorded, never ranked -- an ordering comparison is the one thing a threshold
  cannot be written without.

A reviewer reading a change here should still trace aliases and refuse anything
that puts an offset on either side of `<`, `>`, or `abs()`, exactly as
`dissent.py` refuses a ratio.
"""

import ast
import importlib.util
import inspect
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_perlector():
    spec = importlib.util.spec_from_file_location(
        "perlector_edge_deltas_under_test", ROOT / "pipeline/4_perlector/run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()


def _derivation_tree() -> ast.Module:
    return ast.parse(textwrap.dedent(inspect.getsource(perlector.sealed_proposal_edge_deltas)))


# Every stage that can hold a dossier, an act attachment or a page partition --
# and no others. The Exemplar and Designator never see this evidence, and their
# own byte and pixel offsets would otherwise be swept in by the word alone,
# failing this guard for something it does not govern.
_EVIDENCE_SOURCES = sorted(
    path
    for pattern in (
        "pipeline/3_attestatores/*.py",
        "pipeline/4_perlector/*.py",
        "pipeline/5_recensor/*.py",
        "pipeline/6_archetypus/*.py",
        "pipeline/7_armarium/*.py",
        "common/native_witness.py",
        "common/contracts/*.py",
    )
    for path in ROOT.glob(pattern)
    if not path.name.startswith("test_")
)


def _basis(region_id, bounds):
    return {
        "region_id": region_id,
        "source_page_id": "page-1",
        "source_page_ordinal": 1,
        "transform": {"bounds": dict(bounds)},
    }


def _payload(*boxes, bounds_source="native"):
    return {
        "observed": [
            {
                "ordinal": ordinal,
                "bounds": dict(bounds),
                "bounds_source": bounds_source,
                "span": None,
            }
            for ordinal, bounds in enumerate(boxes)
        ]
    }


def test_offsets_are_measured_against_the_sealed_proposal_and_nothing_else():
    """Four signed offsets, one region, one chair. No comparison, no verdict."""
    payload = _payload({"x": 12, "y": 8, "w": 40, "h": 30})
    rows = perlector.sealed_proposal_edge_deltas(
        payload, [_basis("rgn-1", {"x": 10, "y": 10, "w": 50, "h": 50})]
    )

    assert rows == [
        {
            "ordinal": 0,
            "region_id": "rgn-1",
            # Signed, and the sign is the whole point: `left: 2` and
            # `bottom: -22` are inside the proposal, `top: -2` is 2 pixels of
            # this chair's box above it. The number is retained for a reader,
            # never turned into "how far outside is too far".
            "offsets": {"left": 2, "top": -2, "right": -8, "bottom": -22},
        }
    ]


def test_every_overlapping_proposal_is_retained_and_none_is_chosen():
    """A box over two sealed regions produces two rows, in a stable order.

    Keeping only the "best" overlap would be a picker over the act's own
    geometry: the correspondence is evidence for a reader, not a resolution.
    """
    payload = _payload({"x": 40, "y": 40, "w": 30, "h": 30})
    rows = perlector.sealed_proposal_edge_deltas(
        payload,
        [
            _basis("rgn-2", {"x": 60, "y": 0, "w": 50, "h": 100}),
            _basis("rgn-1", {"x": 0, "y": 0, "w": 50, "h": 100}),
        ],
    )

    assert [row["region_id"] for row in rows] == ["rgn-1", "rgn-2"]


def test_multi_page_rows_are_ordered_across_attachment_contributions():
    """Per-page ordinals restart; concatenating page groups is not the declared order."""

    def row(ordinal, region_id):
        return {
            "ordinal": ordinal,
            "region_id": region_id,
            "offsets": {"left": 0, "top": 0, "right": 0, "bottom": 0},
        }

    ordered = perlector.ordered_edge_deltas(
        {"attestator_1": [row(0, "page-1-a"), row(2, "page-1-b"), row(0, "page-2-a")]}
    )

    assert [(item["ordinal"], item["region_id"]) for item in ordered["attestator_1"]] == [
        (0, "page-1-a"),
        (0, "page-2-a"),
        (2, "page-1-b"),
    ]


def test_a_presented_box_contributes_no_delta_at_all():
    """Only reported geometry counts; a crop echo is not an observation."""
    payload = _payload({"x": 12, "y": 8, "w": 40, "h": 30}, bounds_source="presented")

    assert (
        perlector.sealed_proposal_edge_deltas(
            payload, [_basis("rgn-1", {"x": 10, "y": 10, "w": 50, "h": 50})]
        )
        == []
    )


def test_derivation_signature_exposes_neither_a_second_chair_nor_a_threshold():
    """Pinned like `dissent.comparison_view`'s missing similarity parameter.

    A chair-vs-chair delta or a tolerance would have to arrive through this
    signature, so the signature is the guard: one payload, one act's sealed
    bases, nothing else.
    """
    parameters = inspect.signature(perlector.sealed_proposal_edge_deltas).parameters

    assert list(parameters) == ["payload", "bases"]
    assert all(parameter.default is inspect.Parameter.empty for parameter in parameters.values())


def test_derivation_has_no_direct_sum_or_len_call_for_an_n_of_m_denominator():
    """Forbidden trigger 1's direct spelling is absent from this derivation."""
    parameters = inspect.signature(perlector.sealed_proposal_edge_deltas).parameters
    assert list(parameters) == ["payload", "bases"]
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"sum", "len"}
        for node in ast.walk(_derivation_tree())
    )


def test_derivation_has_no_division_or_named_iou_similarity_call():
    """Catch the direct ratio-shaped spellings of forbidden trigger 2."""
    tree = _derivation_tree()
    assert not any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id in {"iou", "ratio", "similarity"}
            or isinstance(node.func, ast.Attribute)
            and node.func.attr in {"iou", "ratio", "similarity"}
        )
        for node in ast.walk(tree)
    )


def test_delta_magnitude_never_filters_a_reported_overlap():
    """Forbidden trigger 3: both a small and a large signed delta survive."""
    rows = perlector.sealed_proposal_edge_deltas(
        _payload(
            {"x": 10, "y": 10, "w": 20, "h": 20},
            {"x": 99, "y": 99, "w": 100, "h": 100},
        ),
        [_basis("rgn-1", {"x": 0, "y": 0, "w": 100, "h": 100})],
    )
    assert [row["ordinal"] for row in rows] == [0, 1]
    assert rows[0]["offsets"] != rows[1]["offsets"]


def test_derivation_has_no_weight_parameter_or_direct_multiplication():
    """Catch the signature and multiplication spellings of forbidden trigger 4."""
    parameters = inspect.signature(perlector.sealed_proposal_edge_deltas).parameters
    assert not set(parameters) & {"chair", "chairs", "weight", "weights"}
    assert not any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult)
        for node in ast.walk(_derivation_tree())
    )


def test_derivation_signature_and_names_expose_no_second_chair_operand():
    """Catch explicit second-chair spellings of forbidden trigger 5."""
    parameters = inspect.signature(perlector.sealed_proposal_edge_deltas).parameters
    assert list(parameters) == ["payload", "bases"]
    assert not any(
        isinstance(node, ast.Name)
        and any(fragment in node.id for fragment in ("other_chair", "second_chair", "peer"))
        for node in ast.walk(_derivation_tree())
    )


def test_no_stage_directly_ranks_an_expression_named_for_edge_deltas_or_offsets():
    """No direct ordering comparison may mention an offset or delta expression.

    Scoped to the stages that can hold this evidence (see `_EVIDENCE_SOURCES`).
    Deliberately narrow and structural: this catches `<`, `<=`, `>`, `>=`, or
    `abs()` when that expression still carries the field's name.  An alias can
    obscure that syntax and remains review work. Shape checks -- `isinstance`,
    exact key sets, equality against a recorded value -- are untouched, because
    they validate the record rather than judge it.
    """
    ranked: list[str] = []
    scanned: list[str] = []
    ordering = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
    for path in _EVIDENCE_SOURCES:
        source = path.read_text(encoding="utf-8")
        if "edge_delta" not in source and "offsets" not in source:
            continue
        scanned.append(str(path.relative_to(ROOT)))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and any(
                isinstance(operator, ordering) for operator in node.ops
            ):
                rendered = ast.unparse(node)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "abs"
            ):
                rendered = ast.unparse(node)
            else:
                continue
            if "edge_delta" in rendered or "offsets" in rendered:
                ranked.append(f"{path.relative_to(ROOT)}:{node.lineno}: {rendered}")
    # Meta-invariant #88: a "nothing found" assertion passes vacuously if the
    # scan looked at nothing. These numbers exist in exactly two places today.
    assert {"pipeline/4_perlector/run.py", "pipeline/4_perlector/dossier.py"} <= set(scanned), (
        f"the edge-delta scan reached {scanned}, which is not where these numbers live"
    )
    assert not ranked, f"an edge delta may be recorded, never ranked: {ranked}"
