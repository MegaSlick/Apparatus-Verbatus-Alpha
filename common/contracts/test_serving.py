"""Cross-package proofs for the serving configuration input contract, and for
the closed shapes and stop-reason vocabularies the live reading seam adds.
"""

import ast
from pathlib import Path

from common.contracts.serving import (
    CHAIR_CALL_RECORD_FIELDS,
    CHAIR_CALL_RECORD_SCHEMA,
    ENGINE_STOP_COMPLETE,
    ENGINE_STOP_CUT_OFF,
    SERVING_CONFIG_INPUTS_FIELDS,
    SERVING_CONFIG_INPUTS_SCHEMA,
    STOP_REASON_UNREPORTED,
)
from common.stage import _serving_config_inputs
from operations.serving.config import CONFIG_INPUTS_SCHEMA, ServingConfigInputs

SERVING_MODULE = Path(__file__).resolve().parent / "serving.py"


def test_serving_config_serializer_and_validators_share_one_contract() -> None:
    inputs = ServingConfigInputs("1" * 64, "2" * 64)
    record = inputs.to_record()

    assert set(record) == SERVING_CONFIG_INPUTS_FIELDS
    assert record["schema"] == SERVING_CONFIG_INPUTS_SCHEMA
    assert CONFIG_INPUTS_SCHEMA == SERVING_CONFIG_INPUTS_SCHEMA
    assert ServingConfigInputs.from_record(record) == inputs
    assert _serving_config_inputs(record, "contract test") == record


def test_chair_call_record_field_set_is_closed_and_exact() -> None:
    assert isinstance(CHAIR_CALL_RECORD_FIELDS, frozenset)
    assert CHAIR_CALL_RECORD_SCHEMA == "chair-call-record.v1"
    assert CHAIR_CALL_RECORD_FIELDS == frozenset(
        {
            "schema",
            "chair",
            "resolved_identity",
            "resolved_revision",
            "serving_recipe",
            "served_model_id",
            "receipt_ref",
            "launch_audit_ref",
            "decoding_config_sha256",
            "kind",
            "request_sha256",
            "image_sha256s",
            "generation_sent",
            "generation_declared",
            "raw_response_ref",
            "response_sha256",
            "response_model",
            "finish_reason",
            "usage",
            "parse_problem",
            "capacity",
        }
    )


def test_the_two_engine_stop_vocabularies_are_frozensets_with_exact_members() -> None:
    assert isinstance(ENGINE_STOP_COMPLETE, frozenset)
    assert isinstance(ENGINE_STOP_CUT_OFF, frozenset)
    assert ENGINE_STOP_COMPLETE == frozenset({"stop"})
    assert ENGINE_STOP_CUT_OFF == frozenset({"length"})
    # The two vocabularies never overlap: one engine word is never both a
    # complete stop and a cut-off in the same reading.
    assert not (ENGINE_STOP_COMPLETE & ENGINE_STOP_CUT_OFF)


def test_stop_reason_unreported_is_its_own_word_outside_both_vocabularies() -> None:
    assert STOP_REASON_UNREPORTED == "unreported"
    assert STOP_REASON_UNREPORTED not in ENGINE_STOP_COMPLETE
    assert STOP_REASON_UNREPORTED not in ENGINE_STOP_CUT_OFF


def _imported_roots(path: Path) -> set[str]:
    """Every top-level package this module imports, literal absolute imports only.

    Mirrors the walk in ``common/chairs/test_chairs_import_boundary.py``:
    ``ast.walk`` descends into function bodies too, so a deferred import would
    still be caught, and relative imports (``from .x import y``) are
    intra-package by construction and excluded.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots.add((node.module or "").split(".")[0])
    return roots


def test_common_contracts_serving_imports_nothing_from_operations_or_pipeline() -> None:
    """The module this file lives beside must stay constants-only.

    ``common/`` importing ``operations/`` would make the shared-shape file
    depend on the very package it exists to be shared *between*; importing
    ``pipeline/`` would cross the wider boundary ``common/README.md`` states.
    """

    roots = _imported_roots(SERVING_MODULE)
    forbidden = roots & {"operations", "pipeline"}
    assert not forbidden, (
        f"{SERVING_MODULE} imports forbidden root(s) {sorted(forbidden)}; "
        "common/contracts/serving.py must be constants only"
    )
