"""Chandra's offline fixture seam: retained bytes, native boxes, and ordering."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.chairs import load_models_toml
from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES
from common.native_witness import (
    partition_disagreement,
    validate_observed,
)
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
STAGE = Path(__file__).resolve().parent


def _load_stage_module(name: str):
    """Load a stage-local module under a unique name.

    A bare `import run` (or `import feeding`) answers from `sys.modules` first,
    so a module cached by another stage can win regardless of `sys.path` order.
    Unique spec names keep this test bound to the Attestatores file it names.
    """
    spec = importlib.util.spec_from_file_location(f"attestatores_{name}", STAGE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _presented():
    return {
        "kind": "page",
        "source_page_id": "page-1",
        "source_page_ordinal": 1,
        "image_path": "1_exemplar/blobs/sha256/" + "0" * 64,
        "image_sha256": "0" * 64,
        "transform": {
            "operation": "whole",
            "source_page_id": "page-1",
            "source_page_ordinal": 1,
            "bounds": {"x": 0, "y": 0, "w": 200, "h": 260},
        },
    }


def test_chandra_quantizes_retained_float_boxes_by_its_declared_rule():
    chandra = _load_stage_module("chandra")
    raw = (
        b'{"schema":"fixture-chandra-response.v1","markdown":"one",'
        b'"blocks":[{"bbox":[20.25,20.5,180.0,100.1]}]}'
    )
    assert chandra.observe(_presented(), raw) == [
        {
            "ordinal": 0,
            "bounds": {"x": 20, "y": 20, "w": 160, "h": 81},
            "bounds_source": "native",
            "span": None,
        }
    ]


def test_fixture_run_retains_chandra_bytes_and_names_an_unverified_shape(tmp_path):
    run_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--fixture-root",
            str(ROOT / "proof"),
            "--scenario",
            "happy",
            "--run-root",
            str(run_root),
            "--run-id",
            "r",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(run_root, "r")
    records = [
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
    ]
    chandra_records = [record for record in records if record["payload"]["chair"] == "attestator_1"]
    assert chandra_records
    for record in chandra_records:
        payload = record["payload"]
        raw = payload["raw_response_ref"]
        assert digest_bytes(tree.read_bytes(raw["relative_path"])) == raw["sha256"]
        assert payload["provenance"]["resolved_identity"] is not None
        assert payload["adapter_metadata"] == {
            "geometry_quantization": _load_stage_module("chandra").QUANTIZATION_RULE
        }
        assert payload["observed"][0]["bounds_source"] == "native"


def test_chandra_shape_surprise_keeps_bytes_with_a_named_parse_outcome(tmp_path):
    feeding = _load_stage_module("feeding")

    class Tree:
        def __init__(self):
            self.blobs = {}

        def put_blob(self, stage, data):
            digest = digest_bytes(data)
            path = f"3_attestatores/blobs/sha256/{digest}"
            self.blobs[path] = data
            return digest, type("Published", (), {"relative_path": path})()

    raw = b'{"unknown":"shape"}'
    record = feeding.retain_model_view(
        Tree(),
        adapter="chandra.v1",
        view={},
        raw_response=raw,
        transport_stop_reason="fixture-complete",
        parser="json",
    )
    assert record["raw_response_ref"]["sha256"] == digest_bytes(raw)
    assert record["parse"] == {
        "state": "unrecognized-shape",
        "parser": "json",
        "outcome": "unverified-response-schema",
    }
    chandra = _load_stage_module("chandra")
    assert chandra.parse(
        b'{"schema":"fixture-chandra-response.v1","markdown":"text",'
        b'"blocks":[{"bbox":[0,0,"bad",1]}]}'
    ) == {"parse_outcome": "malformed-block-geometry"}


def test_chandra_shape_surprise_is_a_failed_attempt_not_a_successful_read(tmp_path):
    """Named bytes remain evidence, but an unread schema is not a reading."""
    attestatores = _load_stage_module("run")
    resolved = load_models_toml(ROOT / "config/models.toml").chairs["attestator_1"]
    context = SimpleNamespace(
        tree=RunTree(tmp_path / "runs", "r"),
        scenario="shape-surprise",
        fixture={
            "testimony": [
                {
                    "scenario": "shape-surprise",
                    "act_key": "a1",
                    "chair": "attestator_1",
                    "payload": "declared fixture text",
                    "raw_response": '{"schema":"fixture-chandra-response.v1","unknown":"shape"}',
                }
            ],
            "witness_empty": [],
        },
    )
    attempt = attestatores.resolve_attempt(
        context,
        {"act_key": "a1"},
        "attestator_1",
        resolved,
        {"ordinal": 1, "empty": set(), "not_run": set(), "failures": set(), "malformed": {}},
    )
    assert attempt.outcome == "failed"
    assert attempt.native_payload == {"parse_outcome": "missing-text"}
    assert attempt.reason == "the Chandra response shape was not recognized: missing-text"
    assert attempt.raw_response_ref is not None


def test_chandra_raw_text_must_equal_the_fixture_payload_after_retention(tmp_path):
    """A fixture row cannot declare two readings for the same response."""
    attestatores = _load_stage_module("run")
    resolved = load_models_toml(ROOT / "config/models.toml").chairs["attestator_1"]
    tree = RunTree(tmp_path / "runs", "r")
    raw = b'{"schema":"fixture-chandra-response.v1","markdown":"actual","blocks":[]}'
    context = SimpleNamespace(
        tree=tree,
        scenario="mismatch",
        fixture={
            "testimony": [
                {
                    "scenario": "mismatch",
                    "act_key": "a1",
                    "chair": "attestator_1",
                    "payload": "different declaration",
                    "raw_response": raw.decode(),
                }
            ],
            "witness_empty": [],
        },
    )
    with pytest.raises(SchemaRefusal, match="raw response text differs"):
        attestatores.resolve_attempt(
            context,
            {"act_key": "a1"},
            "attestator_1",
            resolved,
            {
                "ordinal": 1,
                "empty": set(),
                "not_run": set(),
                "failures": set(),
                "malformed": {},
            },
        )
    digest = digest_bytes(raw)
    assert tree.read_bytes(f"3_attestatores/blobs/sha256/{digest}") == raw


def test_chandra_malformed_capabilities_fail_only_that_retained_attempt(tmp_path):
    attestatores = _load_stage_module("run")
    resolved = load_models_toml(ROOT / "config/models.toml").chairs["attestator_1"]
    context = SimpleNamespace(
        tree=RunTree(tmp_path / "runs", "r"),
        scenario="bad-capabilities",
        fixture={
            "testimony": [
                {
                    "scenario": "bad-capabilities",
                    "act_key": "a1",
                    "chair": "attestator_1",
                    "payload": "actual",
                    "raw_response": (
                        '{"schema":"fixture-chandra-response.v1","markdown":"actual","blocks":[]}'
                    ),
                    "format_capabilities": "not-an-object",
                }
            ],
            "witness_empty": [],
        },
    )
    attempt = attestatores.resolve_attempt(
        context,
        {"act_key": "a1"},
        "attestator_1",
        resolved,
        {"ordinal": 1, "empty": set(), "not_run": set(), "failures": set(), "malformed": {}},
    )
    assert attempt.outcome == "failed"
    assert attempt.native_payload == "actual"
    assert attempt.raw_response_ref is not None
    assert "format capabilities could not be retained" in attempt.reason


def test_chandra_conflicting_text_fields_and_huge_coordinates_are_named():
    chandra = _load_stage_module("chandra")
    assert chandra.parse(
        b'{"schema":"fixture-chandra-response.v1","markdown":"one","text":"two","blocks":[]}'
    ) == {"parse_outcome": "conflicting-text-fields"}
    raw = json.dumps(
        {
            "schema": "fixture-chandra-response.v1",
            "markdown": "one",
            "blocks": [{"bbox": [0, 0, 10**400, 1]}],
        }
    ).encode()
    assert chandra.parse(raw) == {"parse_outcome": "malformed-block-geometry"}
    assert chandra.observe(_presented(), raw) == []


def test_chandra_bounds_native_json_before_decode_and_geometry_expansion(monkeypatch):
    """One native response cannot crash or amplify past the adapter boundary."""
    chandra = _load_stage_module("chandra")

    monkeypatch.setattr(chandra, "MAX_RESPONSE_BYTES", 8)
    assert chandra.parse(b"123456789") == {"parse_outcome": "response-too-large"}
    assert chandra.observe(_presented(), b"123456789") == []

    monkeypatch.setattr(chandra, "MAX_RESPONSE_BYTES", 16 * 1024 * 1024)
    monkeypatch.setattr(chandra, "MAX_LAYOUT_BLOCKS", 1)
    raw = json.dumps(
        {
            "schema": "fixture-chandra-response.v1",
            "markdown": "two",
            "blocks": [{"bbox": [0, 0, 1, 1]}, {"bbox": [1, 1, 2, 2]}],
        }
    ).encode()
    assert chandra.parse(raw) == {"parse_outcome": "too-many-layout-blocks"}
    assert chandra.observe(_presented(), raw) == []


def test_chandra_names_excessive_json_nesting_with_a_fixed_outcome(monkeypatch):
    """The refusal name is pinned, not left to the interpreter's spare stack.

    Asserting `parse(deep) in ("x", {...})` accepted whichever branch happened
    to run, so a renamed outcome -- or a parser that silently began accepting
    the deep value -- kept passing. The scanner's `RecursionError` is raised
    directly here instead, so the name this adapter answers with is the thing
    under test.
    """
    chandra = _load_stage_module("chandra")

    def _exhausts_the_stack(_text):
        raise RecursionError("maximum recursion depth exceeded while decoding")

    monkeypatch.setattr(chandra.json, "loads", _exhausts_the_stack)
    raw = b'{"schema":"fixture-chandra-response.v1","markdown":"x","blocks":[]}'

    assert chandra.parse(raw) == {"parse_outcome": "excessive-json-nesting"}


def test_chandra_never_lets_a_deep_document_or_a_non_byte_input_escape():
    """Whichever branch the real interpreter takes, neither is a crash."""
    chandra = _load_stage_module("chandra")
    nested = (
        b'{"schema":"fixture-chandra-response.v1","markdown":"x","blocks":[],"extra":'
        + b"[" * 10_000
        + b"0"
        + b"]" * 10_000
        + b"}"
    )
    # Parse-time recursion exhaustion is stack-dependent: when the interpreter's
    # C recursion headroom runs out the nesting is named, and when the parser
    # survives the depth the declared markdown parses normally. This case is
    # only about the absence of an escaping error; the name is pinned above.
    assert chandra.parse(nested) in ("x", {"parse_outcome": "excessive-json-nesting"})
    assert chandra.observe(_presented(), nested) == []
    assert chandra.parse("not bytes") == {"parse_outcome": "raw-response-not-bytes"}


def test_an_unverified_chandra_wire_shape_cannot_acquire_fixture_geometry():
    """Only explicitly synthetic bytes use the placeholder page-pixel rule."""
    chandra = _load_stage_module("chandra")
    raw = b'{"markdown":"plausible live response","blocks":[{"bbox":[0,0,100,100]}]}'
    assert chandra.parse(raw) == {"parse_outcome": "unverified-response-schema"}
    assert chandra.observe(_presented(), raw) == []


@pytest.mark.parametrize(
    ("adapter_name", "raw_response", "message"),
    (
        ("chandra.v1", 7, "raw_response is not text encoding"),
        ("churro.v1", "native bytes", "has no native byte route"),
    ),
)
def test_fixture_raw_response_cannot_be_silently_discarded(
    tmp_path, adapter_name, raw_response, message
):
    attestatores = _load_stage_module("run")
    resolved = load_models_toml(ROOT / "config/models.toml").chairs["attestator_1"]
    if adapter_name != resolved.witness_adapter:
        resolved = replace(resolved, witness_adapter=adapter_name)
    context = SimpleNamespace(
        tree=RunTree(tmp_path / "runs", "r"),
        scenario="bad-raw",
        fixture={
            "testimony": [
                {
                    "scenario": "bad-raw",
                    "act_key": "a1",
                    "chair": "attestator_1",
                    "payload": "declared text",
                    "raw_response": raw_response,
                }
            ],
            "witness_empty": [],
        },
    )
    with pytest.raises(SchemaRefusal, match=message):
        attestatores.resolve_attempt(
            context,
            {"act_key": "a1"},
            "attestator_1",
            resolved,
            {"ordinal": 1, "empty": set(), "not_run": set(), "failures": set(), "malformed": {}},
        )


def test_a_second_fixture_native_adapter_cannot_be_filed_under_chandras_boundary(
    tmp_path, monkeypatch
):
    """The retain recipe inside that branch is Chandra's, and now it says so.

    `FIXTURE_NATIVE_RESPONSE_ADAPTERS` decides whose fixture rows may declare
    `raw_response` bytes; the branch it opens retains them through Chandra's own
    registry entry, `chandra.FIXTURE_PROMPT` and the `json` parser. Widening the
    set without writing the new adapter's own branch would therefore publish a
    Testimonium whose retained view and parser name a chair that never produced
    those bytes -- the resolved identity and the record disagreeing, which
    GOVERNANCE 6 does not permit. A comment said so and nothing checked it; this
    is the check, and it fires where the set widens rather than at whatever
    later reads the misfiled record.
    """
    attestatores = _load_stage_module("run")
    monkeypatch.setattr(
        attestatores, "FIXTURE_NATIVE_RESPONSE_ADAPTERS", frozenset({"chandra.v1", "churro.v1"})
    )
    resolved = replace(
        load_models_toml(ROOT / "config/models.toml").chairs["attestator_1"],
        witness_adapter="churro.v1",
    )
    context = SimpleNamespace(
        tree=RunTree(tmp_path / "runs", "r"),
        scenario="bad-raw",
        fixture={
            "testimony": [
                {
                    "scenario": "bad-raw",
                    "act_key": "a1",
                    "chair": "attestator_1",
                    "payload": "declared text",
                    "raw_response": "<output>declared text</output>",
                }
            ],
            "witness_empty": [],
        },
    )
    with pytest.raises(SchemaRefusal, match="would be retained through Chandra's recipe"):
        attestatores.resolve_attempt(
            context,
            {"act_key": "a1"},
            "attestator_1",
            resolved,
            {"ordinal": 1, "empty": set(), "not_run": set(), "failures": set(), "malformed": {}},
        )


def test_empty_fixture_raw_response_cannot_be_silently_discarded(tmp_path):
    attestatores = _load_stage_module("run")
    resolved = load_models_toml(ROOT / "config/models.toml").chairs["attestator_1"]
    context = SimpleNamespace(
        tree=RunTree(tmp_path / "runs", "r"),
        scenario="bad-empty-raw",
        fixture={
            "testimony": [],
            "witness_empty": [
                {
                    "scenario": "bad-empty-raw",
                    "act_key": "a1",
                    "chair": "attestator_1",
                    "raw_response": 7,
                }
            ],
        },
    )
    with pytest.raises(SchemaRefusal, match="raw_response is not text encoding"):
        attestatores.resolve_attempt(
            context,
            {"act_key": "a1"},
            "attestator_1",
            resolved,
            {
                "ordinal": 1,
                "empty": {("a1", "attestator_1")},
                "not_run": set(),
                "failures": set(),
                "malformed": {},
            },
        )


def test_chandra_out_of_order_stage_invocation_holds_cleanly(tmp_path):
    run_root = tmp_path / "runs"
    door = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/1_exemplar/door.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--fixture-root",
            str(ROOT / "proof"),
            "--scenario",
            "happy",
            "--run-root",
            str(run_root),
            "--run-id",
            "out-of-order",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert door.returncode == 0, door.stderr
    result = subprocess.run(
        [
            sys.executable,
            str(STAGE / "run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--fixture-root",
            str(ROOT / "proof"),
            "--scenario",
            "happy",
            "--run-root",
            str(run_root),
            "--run-id",
            "out-of-order",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "missing predecessor" in result.stderr.lower() or "predecessor" in result.stderr.lower()


def test_overlapping_native_blocks_are_both_retained_as_reported_geometry():
    """Two blocks over the same ink is a layout fact, not a contradiction.

    A real page segmenter overlaps constantly: a heading inside its own column,
    a marginal name inside the act it belongs to. Nothing here may merge, drop
    or prefer between them -- the pipeline retains every reported box and lets
    the partition record hold the competing pairings (GOVERNANCE 3).
    """
    chandra = _load_stage_module("chandra")
    raw = (
        b'{"schema":"fixture-chandra-response.v1","markdown":"one",'
        b'"blocks":[{"bbox":[10,10,100,100]},{"bbox":[50,50,150,150]}]}'
    )
    observed = chandra.observe(_presented(), raw)
    assert [item["bounds"] for item in observed] == [
        {"x": 10, "y": 10, "w": 90, "h": 90},
        {"x": 50, "y": 50, "w": 100, "h": 100},
    ]
    # Intersecting boxes still require dense response-order ordinals.
    validate_observed(observed, presented=_presented(), page_size=(200, 260))


@pytest.mark.parametrize(
    "bbox",
    (
        [10, 10, 10, 10],  # zero area in both axes
        [10, 10, 100, 10],  # zero height
        [10, 10, 10, 100],  # zero width
        [100, 10, 10, 100],  # max edge before min edge: reading indices crossed
        [-5, -5, 100, 100],  # negative origin
        [10, 10, 100, "100"],  # a string where a coordinate belongs
        [10, 10, 100],  # not four coordinates
    ),
)
def test_a_degenerate_native_box_is_named_and_derives_no_geometry(bbox):
    """A box with no area cannot become an observation, and does not vanish.

    `parse` names the whole malformed response while `observe` emits no partial
    geometry that could look like a complete partition.
    """
    chandra = _load_stage_module("chandra")
    raw = json.dumps(
        {"schema": "fixture-chandra-response.v1", "markdown": "one", "blocks": [{"bbox": bbox}]}
    ).encode("utf-8")
    assert chandra.parse(raw) == {"parse_outcome": "malformed-block-geometry"}
    assert chandra.observe(_presented(), raw) == []


def test_one_degenerate_box_does_not_let_its_neighbours_pass_unnamed():
    """A mixed response is named for the whole response, not per block.

    Deliberate and worth stating: the alternative -- keep the good blocks, drop
    the bad one -- would publish a partition that looks complete while silently
    missing a region the witness reported (invariant 6). The response is retained
    whole in its blob either way, so nothing is lost; what changes is whether the
    derived layer claims to be the witness's full report.
    """
    chandra = _load_stage_module("chandra")
    raw = json.dumps(
        {
            "schema": "fixture-chandra-response.v1",
            "markdown": "one",
            "blocks": [{"bbox": [10, 10, 100, 100]}, {"bbox": [10, 10, 10, 10]}],
        }
    ).encode("utf-8")
    assert chandra.parse(raw) == {"parse_outcome": "malformed-block-geometry"}
    assert chandra.observe(_presented(), raw) == []


def test_reading_order_is_the_response_order_and_no_other_key_reorders_it():
    """The declared basis for `ordinal` is the order the response listed.

    Chandra's published behaviour is that its output preserves reading order, so
    the list *is* the order. A block key this adapter does not read cannot
    quietly become an ordering authority -- the derived ordinals stay the
    positions the response gave, and the unread key survives verbatim in the
    retained blob without affecting this adapter's declared order.
    """
    chandra = _load_stage_module("chandra")
    raw = json.dumps(
        {
            "schema": "fixture-chandra-response.v1",
            "markdown": "two blocks",
            "blocks": [
                {"bbox": [10, 120, 100, 200], "reading_index": 7},
                {"bbox": [10, 10, 100, 100], "reading_index": 0},
            ],
        }
    ).encode("utf-8")
    observed = chandra.observe(_presented(), raw)
    assert [item["ordinal"] for item in observed] == [0, 1]
    assert [item["bounds"]["y"] for item in observed] == [120, 10]
    assert [block["reading_index"] for block in json.loads(raw)["blocks"]] == [7, 0]


def test_a_page_edge_overshoot_is_named_per_block_without_clamping_or_losing_neighbours():
    """One bad page-edge box is retained as a finding, never an in-page box.

    `ceil` on a max edge means any block whose float edge sits fractionally past
    the sealed page derives a box the shared wall refuses.  That fact belongs to
    this block, not to a valid neighbouring block from the same retained response.
    The durable page partition therefore carries the exact, out-of-page box as a
    response-linked finding and retains the valid block in its ordinary observed
    list.  Clamping would instead hand a fallback crop retrospective witness
    coverage it never received.
    """
    chandra = _load_stage_module("chandra")
    attestatores = _load_stage_module("run")
    raw = b'{"schema":"fixture-chandra-response.v1","markdown":"two","blocks":[{"bbox":[10,10,100,100]},{"bbox":[0,0,200.2,260.0]}]}'
    raw_ref = {
        "relative_path": "3_attestatores/blobs/sha256/" + digest_bytes(raw),
        "sha256": digest_bytes(raw),
    }
    surviving, overshoots = attestatores.page_partition_entries(
        chandra.observe(_presented(), raw), page_size=(200, 260), raw_response_ref=raw_ref
    )
    assert surviving == [
        {
            "ordinal": 0,
            "bounds": {"x": 10, "y": 10, "w": 90, "h": 90},
            "bounds_source": "native",
            "span": None,
        }
    ]
    assert overshoots == [
        {
            "kind": "page-edge-overshoot",
            "response_sha256": raw_ref["sha256"],
            "ordinal": 1,
            "bounds": {"x": 0, "y": 0, "w": 201, "h": 260},
            "sealed_page_bounds": {"x": 0, "y": 0, "w": 200, "h": 260},
        }
    ]

    disagreement = partition_disagreement(
        {
            "artifact_id": "page-testimonium",
            "payload": {"presented": _presented(), "observed": surviving},
        },
        [],
        page_edge_overshoots=overshoots,
    )
    durable = attestatores.page_testimonium_payload(
        page_ordinal=1,
        page_role="primary",
        unjoined_act_attempts=[],
        partition_disagreement=disagreement,
        testimonium_id="page-testimonium",
        raw_response_refs=[raw_ref],
        adapter_metadata={"geometry_quantization": chandra.QUANTIZATION_RULE},
        chair="attestator_1",
        act_key="page-1",
        ordinal=1,
        regions=[],
        provenance={"chair": "attestator_1"},
        format_capabilities=attestatores.DEFAULT_FORMAT_CAPABILITIES,
        native_payload="two",
        witness_reported=None,
        health=attestatores.content_health("two", completed=True),
        presented=_presented(),
        observed=surviving,
        unpresented_regions=[],
        outcome="read",
    )
    assert durable["partition_disagreement"]["observed_boxes"] == [
        {"ordinal": 0, "bounds": {"x": 10, "y": 10, "w": 90, "h": 90}, "bounds_source": "native"}
    ]
    assert durable["partition_disagreement"]["page_edge_overshoots"] == [overshoots[0]]

    # The shared wall remains the refusal: the finding preserves these exact
    # derived bounds, and they still cannot masquerade as an observation.
    with pytest.raises(SchemaRefusal, match="outside the sealed source page"):
        validate_observed(
            [
                {
                    "ordinal": 0,
                    "bounds": overshoots[0]["bounds"],
                    "bounds_source": "native",
                    "span": None,
                }
            ],
            presented=_presented(),
            page_size=(200, 260),
        )


def test_two_acts_sharing_one_chandra_response_do_not_double_count_its_overshoot():
    """Two acts on one page legitimately re-derive the same chair's response.

    `publish_page_testimonia_and_attachments` calls `page_partition_entries`
    once per act on a page for a page-scoped Chandra chair, and two acts commonly
    share one raw response (the page record already dedupes `raw_response_refs` for
    exactly this reason). Re-deriving an out-of-page block from that same response
    twice must not double-count it: `validate_partition_disagreement` refuses one
    page-edge finding named twice, so an unrefined concatenation would abort the
    whole page's publish over ordinary shared testimony rather than a malformed
    record. The page writer must dedupe by the finding's own identity --
    `(response_sha256, ordinal)` -- exactly as it already dedupes response refs.
    """
    chandra = _load_stage_module("chandra")
    attestatores = _load_stage_module("run")
    raw = b'{"schema":"fixture-chandra-response.v1","markdown":"two","blocks":[{"bbox":[10,10,100,100]},{"bbox":[0,0,200.2,260.0]}]}'
    raw_ref = {
        "relative_path": "3_attestatores/blobs/sha256/" + digest_bytes(raw),
        "sha256": digest_bytes(raw),
    }

    # Two acts on the page independently re-derive the identical response.
    first_survivors, first_overshoots = attestatores.page_partition_entries(
        chandra.observe(_presented(), raw), page_size=(200, 260), raw_response_ref=raw_ref
    )
    second_survivors, second_overshoots = attestatores.page_partition_entries(
        chandra.observe(_presented(), raw), page_size=(200, 260), raw_response_ref=raw_ref
    )
    assert first_overshoots == second_overshoots

    # Mirrors the page writer's own renumbering of the aggregate `observed`
    # list across every act contributing to this page/chair
    # (`observed.append({**item, "ordinal": len(observed)})`), so this test
    # isolates the overshoot-identity question from ordinary survivor
    # renumbering, which the writer already gets right.
    merged_observed = []
    for item in [*first_survivors, *second_survivors]:
        merged_observed.append({**item, "ordinal": len(merged_observed)})

    def _build(overshoots):
        disagreement = partition_disagreement(
            {
                "artifact_id": "page-testimonium",
                "payload": {"presented": _presented(), "observed": merged_observed},
            },
            [],
            page_edge_overshoots=overshoots,
        )
        return attestatores.page_testimonium_payload(
            page_ordinal=1,
            page_role="primary",
            unjoined_act_attempts=[],
            partition_disagreement=disagreement,
            testimonium_id="page-testimonium",
            raw_response_refs=[raw_ref],
            adapter_metadata={"geometry_quantization": chandra.QUANTIZATION_RULE},
            chair="attestator_1",
            act_key="page-1",
            ordinal=1,
            regions=[],
            provenance={"chair": "attestator_1"},
            format_capabilities=attestatores.DEFAULT_FORMAT_CAPABILITIES,
            native_payload="two",
            witness_reported=None,
            health=attestatores.content_health("two", completed=True),
            presented=_presented(),
            observed=merged_observed,
            unpresented_regions=[],
            outcome="read",
        )

    # Naively concatenating both acts' re-derivations names one page-edge
    # finding twice -- the exact crash this defect let a normal, shared page
    # response trigger mid-publish.
    with pytest.raises(SchemaRefusal, match="names one page-edge finding twice"):
        _build([*first_overshoots, *second_overshoots])

    # Deduplicated by the finding's own identity -- exactly what the page
    # writer now does before it extends `page_edge_overshoots` -- the shared
    # response's overshoot is retained exactly once.
    seen: set[tuple[str, int]] = set()
    deduped = []
    for overshoot in [*first_overshoots, *second_overshoots]:
        key = (overshoot["response_sha256"], overshoot["ordinal"])
        if key not in seen:
            seen.add(key)
            deduped.append(overshoot)
    durable = _build(deduped)
    assert durable["partition_disagreement"]["page_edge_overshoots"] == first_overshoots


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("ordinal", 7, "ordinals that are not dense"),
        ("bounds_source", "presented", "not reported witness geometry"),
        ("bounds_source", [], "not reported witness geometry"),
        ("span", {"start": 0, "end": 1}, "would lose evidence"),
    ],
)
def test_an_overshoot_cannot_hide_malformed_observation_facts(field, value, message):
    """Removing a bad box must not sanitize fields the finding does not retain."""
    chandra = _load_stage_module("chandra")
    attestatores = _load_stage_module("run")
    observed = chandra.observe(
        _presented(),
        b'{"schema":"fixture-chandra-response.v1","markdown":"one","blocks":[{"bbox":[0,0,200.2,260.0]}]}',
    )
    observed[0][field] = value

    with pytest.raises(SchemaRefusal, match=message):
        attestatores.page_partition_entries(
            observed,
            page_size=(200, 260),
            raw_response_ref={"relative_path": "retained", "sha256": "a" * 64},
        )


def test_an_in_page_observation_keeps_its_supported_text_span():
    """Only converting an overshoot loses the span; an in-page box survives intact."""
    attestatores = _load_stage_module("run")
    observed = [
        {
            "ordinal": 0,
            "bounds": {"x": 10, "y": 10, "w": 20, "h": 20},
            "bounds_source": "native",
            "span": {"start": 0, "end": 4},
        }
    ]

    survivors, findings = attestatores.page_partition_entries(
        observed,
        page_size=(200, 260),
        raw_response_ref={"relative_path": "retained", "sha256": "a" * 64},
    )

    assert survivors == observed
    assert findings == []


def test_a_parse_failure_keeps_its_bytes_and_its_name_through_the_written_record(tmp_path):
    """A written shape refusal must retain both its name and referenced bytes."""
    feeding = _load_stage_module("feeding")
    attestatores = _load_stage_module("run")

    tree = RunTree(tmp_path / "runs", "r")
    raw = (
        b'{"schema":"fixture-chandra-response.v1","markdown":"text",'
        b'"blocks":[{"bbox":[0,0,"bad",1]}]}'
    )
    retained = feeding.retain_model_view(
        tree,
        adapter="chandra.v1",
        view={"prompt": {"instruction": "x"}},
        raw_response=raw,
        transport_stop_reason="fixture-complete",
        parser="json",
    )
    assert retained["parse"]["outcome"] == "malformed-block-geometry"
    assert retained["stop_reason"] == "partial-parse-unrecognized-shape"
    assert tree.read_bytes(retained["raw_response_ref"]["relative_path"]) == raw

    payload = attestatores.testimonium_payload(
        chair="attestator_1",
        act_key="a1",
        ordinal=1,
        regions=[],
        provenance={"chair": "attestator_1"},
        format_capabilities=attestatores.DEFAULT_FORMAT_CAPABILITIES,
        native_payload={"parse_outcome": retained["parse"]["outcome"]},
        witness_reported=None,
        health=attestatores.content_health(
            {"parse_outcome": retained["parse"]["outcome"]}, completed=True
        ),
        presented={},
        observed=[],
        unpresented_regions=[],
        outcome="read",
        raw_response_ref=retained["raw_response_ref"],
        adapter_metadata={"geometry_quantization": _load_stage_module("chandra").QUANTIZATION_RULE},
    )
    assert payload["payload"] == {"parse_outcome": "malformed-block-geometry"}
    assert payload["raw_response_ref"] == retained["raw_response_ref"]
    assert "reported" not in payload
    assert payload["content_health"]["recordable"] is True


def test_an_unknown_quantization_rule_is_refused_by_name(tmp_path):
    """Admissible rules derive from bindings, not from one adapter's literal."""
    attestatores = _load_stage_module("run")

    chandra_rule = _load_stage_module("chandra").QUANTIZATION_RULE
    attestatores.validate_adapter_metadata(
        {"adapter_metadata": {"geometry_quantization": chandra_rule}}
    )
    with pytest.raises(SchemaRefusal, match="not a quantization rule any bound adapter"):
        attestatores.validate_adapter_metadata(
            {"adapter_metadata": {"geometry_quantization": "invented.v9.round-to-taste"}}
        )


def test_quantization_metadata_belongs_to_the_recorded_adapter_and_blob():
    attestatores = _load_stage_module("run")
    chandra_rule = _load_stage_module("chandra").QUANTIZATION_RULE
    payload = {
        "provenance": {"resolved_identity": {"witness_adapter": "churro.v1"}},
        "adapter_metadata": {"geometry_quantization": chandra_rule},
        "raw_response_ref": {
            "relative_path": "3_attestatores/blobs/sha256/" + "a" * 64,
            "sha256": "a" * 64,
        },
    }
    with pytest.raises(SchemaRefusal, match="does not belong"):
        attestatores.validate_adapter_metadata(payload)
    with pytest.raises(SchemaRefusal, match="without a retained response"):
        attestatores.validate_retained_response_pairing(
            {"adapter_metadata": {"geometry_quantization": chandra_rule}}
        )
    with pytest.raises(SchemaRefusal, match="without naming its rule"):
        attestatores.validate_retained_response_pairing(
            {
                "provenance": {"resolved_identity": {"witness_adapter": "chandra.v1"}},
                "raw_response_ref": payload["raw_response_ref"],
            }
        )


@pytest.mark.parametrize(
    "reference",
    (
        {
            "relative_path": "3_attestatores/blobs/sha256/" + "a" * 64,
            "sha256": "z" * 64,
        },
        {
            "relative_path": "3_attestatores/blobs/sha256/" + "a" * 64,
            "sha256": "b" * 64,
        },
        {
            "relative_path": "3_attestatores/blobs/sha256/../../elsewhere",
            "sha256": "a" * 64,
        },
    ),
)
def test_retained_response_reference_is_the_exact_lowercase_digest_path(reference):
    attestatores = _load_stage_module("run")
    with pytest.raises(SchemaRefusal, match="Attestatores blob reference"):
        attestatores.validate_raw_response_ref(reference)


def test_act_tally_rechecks_retained_response_bytes(tmp_path):
    attestatores = _load_stage_module("run")
    tree = RunTree(tmp_path / "runs", "r")
    raw = b"retained response"
    digest, published = tree.put_blob(ATTESTATORES, raw)
    reference = {"relative_path": published.relative_path, "sha256": digest}
    attestatores.validate_retained_response_blob(tree, reference)
    tree.resolve(published.relative_path).write_bytes(b"changed")
    with pytest.raises(SchemaRefusal, match="differs from its digest"):
        attestatores.validate_retained_response_blob(tree, reference)


def test_resume_collision_compares_native_response_digest_not_only_parsed_text():
    """Same text with different native geometry is a different attempt."""
    attestatores = _load_stage_module("run")
    sealed_ref = {
        "relative_path": "3_attestatores/blobs/sha256/" + "a" * 64,
        "sha256": "a" * 64,
    }
    candidate_ref = {
        "relative_path": "3_attestatores/blobs/sha256/" + "b" * 64,
        "sha256": "b" * 64,
    }
    health = attestatores.content_health("same text", completed=True)
    history = {
        ("act-1", "attestator_1"): [
            {
                "outcome": "read",
                "payload": {
                    "attempt_ordinal": 1,
                    "payload": "same text",
                    "witness_reported": None,
                    "format_capabilities": attestatores.DEFAULT_FORMAT_CAPABILITIES,
                    "content_health": health,
                    "raw_response_ref": sealed_ref,
                },
            }
        ]
    }
    candidate = attestatores.Attempt(
        outcome="read",
        native_payload="same text",
        witness_reported=None,
        format_capabilities=attestatores.DEFAULT_FORMAT_CAPABILITIES,
        health=health,
        reason=None,
        raw_response_ref=candidate_ref,
        observation_payload=b"different layout bytes",
    )

    with pytest.raises(SchemaRefusal, match="would record a different attempt"):
        attestatores._refuse_write_collision(
            history,
            {"act_id": "act-1", "act_key": "a1"},
            "attestator_1",
            1,
            candidate,
        )


def test_the_page_record_names_the_bytes_its_own_geometry_was_quantized_from(tmp_path):
    """The geometry record must name the retained response that produced it.

    The page Testimonium carries integer boxes derived from native floats. Its
    response reference must travel in that same record rather than require a
    later join through compatibility records (GOALS 5).
    """
    run_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--fixture-root",
            str(ROOT / "proof"),
            "--scenario",
            "happy",
            "--run-root",
            str(run_root),
            "--run-id",
            "r",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(run_root, "r")
    pages = [
        tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
    ]
    native = [
        record["payload"]
        for record in pages
        if any(item["bounds_source"] == "native" for item in record["payload"]["observed"])
        and record["payload"]["chair"] == "attestator_1"
    ]
    assert native, "no Chandra page record reported native geometry"
    for payload in native:
        refs = payload["raw_response_refs"]
        assert refs, "a page record derived native geometry from bytes it does not name"
        for reference in refs:
            assert digest_bytes(tree.read_bytes(reference["relative_path"])) == reference["sha256"]
        assert payload["adapter_metadata"] == {
            "geometry_quantization": _load_stage_module("chandra").QUANTIZATION_RULE
        }
        assert payload["provenance"]["resolved_identity"] is not None

    # Presentation echoes have no native floats and therefore no conversion rule.
    echoes = [
        record["payload"]
        for record in pages
        if all(item["bounds_source"] == "presented" for item in record["payload"]["observed"])
    ]
    assert echoes
    for payload in echoes:
        assert "raw_response_refs" not in payload
        assert "adapter_metadata" not in payload


def test_the_stage_seals_its_boundary_and_an_out_of_order_pass_seals_nothing(tmp_path):
    """A pass held before publication must leave no boundary seal to consume."""
    complete_root = tmp_path / "complete"
    complete = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--fixture-root",
            str(ROOT / "proof"),
            "--scenario",
            "happy",
            "--run-root",
            str(complete_root),
            "--run-id",
            "r",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert complete.returncode == 0, complete.stderr
    sealed = RunTree(complete_root, "r")
    seals = [
        sealed.read_artifact(ATTESTATORES, "stage-seal", entry["artifact_id"])
        for entry in sealed.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "stage-seal"
    ]
    assert len(seals) == 1
    census = {(row["kind"], row["outcome"]): row["count"] for row in seals[0]["payload"]["census"]}
    assert census[("page-testimonium", "read")] > 0
    assert census[("testimonium", "read")] > 0

    held_root = tmp_path / "held"
    door = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/1_exemplar/door.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--fixture-root",
            str(ROOT / "proof"),
            "--scenario",
            "happy",
            "--run-root",
            str(held_root),
            "--run-id",
            "r",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert door.returncode == 0, door.stderr
    out_of_order = subprocess.run(
        [
            sys.executable,
            str(STAGE / "run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--fixture-root",
            str(ROOT / "proof"),
            "--scenario",
            "happy",
            "--run-root",
            str(held_root),
            "--run-id",
            "r",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out_of_order.returncode == 2
    assert not list((held_root / "r" / ATTESTATORES / "artifacts" / "stage-seal").glob("*.json"))


def test_a_page_witness_that_mixed_reported_geometry_with_an_echo_is_refused_by_name():
    """The partition seam fails closed on a shape no adapter has a rule for.

    Both page adapters return either reported geometry or the no-geometry echo,
    never both, so this is unreachable today. Filtering a mix instead of refusing
    it would publish a page geometry derived from half a record and
    indistinguishable from a complete one (GOVERNANCE 2), which is why the
    behaviour is pinned rather than left to the adapters' current manners.
    """
    attestatores = _load_stage_module("run")
    echo = {
        "ordinal": 0,
        "bounds": {"x": 0, "y": 0, "w": 4, "h": 4},
        "bounds_source": "presented",
        "span": None,
    }
    native = {
        "ordinal": 1,
        "bounds": {"x": 1, "y": 1, "w": 2, "h": 2},
        "bounds_source": "native",
        "span": None,
    }

    assert attestatores._partition_geometry([]) == []
    assert attestatores._partition_geometry([echo]) == []
    assert attestatores._partition_geometry([native]) == [native]
    with pytest.raises(SchemaRefusal, match="reported geometry and a presentation echo"):
        attestatores._partition_geometry([echo, native])
