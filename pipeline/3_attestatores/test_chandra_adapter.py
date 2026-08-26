"""Chandra's offline fixture seam: retained bytes, native boxes, and ordering."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

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
    so whichever stage's `run.py` was imported earlier in the pytest process
    wins regardless of `sys.path` order — measured: the Perlector's nuda tests
    cache their own `run`, and this file then received a Perlector module where
    it needed the Attestatores. Unique spec names make the cache honest.
    """
    spec = importlib.util.spec_from_file_location(f"attestatores_{name}", STAGE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_chandra():
    spec = importlib.util.spec_from_file_location("attestatores_chandra", STAGE / "chandra.py")
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
    chandra = _load_chandra()
    raw = b'{"markdown":"one","blocks":[{"bbox":[20.25,20.5,180.0,100.1]}]}'
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
            "geometry_quantization": _load_chandra().QUANTIZATION_RULE
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
        "outcome": "missing-text",
    }
    chandra = _load_chandra()
    assert chandra.parse(b'{"markdown":"text","blocks":[{"bbox":[0,0,"bad",1]}]}') == {
        "parse_outcome": "malformed-block-geometry"
    }


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


# --- Adversarial re-derivation of the unit's three definition-of-done bullets --
#
# The happy-path tests above prove the DoD reachable. These ask the harder half:
# what a real layout detector emits that the fixture never does -- overlapping
# blocks, degenerate boxes, geometry that runs off the page edge -- and whether
# each named outcome survives the write path rather than only the parser.


def test_overlapping_native_blocks_are_both_retained_as_reported_geometry():
    """Two blocks over the same ink is a layout fact, not a contradiction.

    A real page segmenter overlaps constantly: a heading inside its own column,
    a marginal name inside the act it belongs to. Nothing here may merge, drop
    or prefer between them -- the pipeline retains every reported box and lets
    the partition record hold the competing pairings (GOVERNANCE 3).
    """
    chandra = _load_chandra()
    raw = b'{"markdown":"one","blocks":[{"bbox":[10,10,100,100]},{"bbox":[50,50,150,150]}]}'
    observed = chandra.observe(_presented(), raw)
    assert [item["bounds"] for item in observed] == [
        {"x": 10, "y": 10, "w": 90, "h": 90},
        {"x": 50, "y": 50, "w": 100, "h": 100},
    ]
    # Dense, unique and zero-based even though the boxes intersect, and accepted
    # by the shared wall rather than merely by this adapter's own arithmetic.
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

    `_quantize_box` refusing is only half of it: the refusal has to reach the
    record as a name. `parse` turns the whole response into
    `malformed-block-geometry`, which is what the Testimonium then carries, and
    `observe` derives nothing rather than emitting a box the shared wall would
    have to catch downstream.
    """
    chandra = _load_chandra()
    raw = json.dumps({"markdown": "one", "blocks": [{"bbox": bbox}]}).encode("utf-8")
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
    chandra = _load_chandra()
    raw = json.dumps(
        {"markdown": "one", "blocks": [{"bbox": [10, 10, 100, 100]}, {"bbox": [10, 10, 10, 10]}]}
    ).encode("utf-8")
    assert chandra.parse(raw) == {"parse_outcome": "malformed-block-geometry"}
    assert chandra.observe(_presented(), raw) == []


def test_reading_order_is_the_response_order_and_no_other_key_reorders_it():
    """The declared basis for `ordinal` is the order the response listed.

    Chandra's published behaviour is that its output preserves reading order, so
    the list *is* the order. A block key this adapter does not read cannot
    quietly become an ordering authority -- the derived ordinals stay the
    positions the response gave, and the unread key survives verbatim in the
    retained blob, where a later unit can decide what it means.
    """
    chandra = _load_chandra()
    raw = json.dumps(
        {
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
    chandra = _load_chandra()
    attestatores = _load_stage_module("run")
    raw = b'{"markdown":"two","blocks":[{"bbox":[10,10,100,100]},{"bbox":[0,0,200.2,260.0]}]}'
    raw_ref = {
        "relative_path": "3_attestatores/blobs/sha256/" + digest_bytes(raw),
        "sha256": digest_bytes(raw),
    }
    surviving, overshoots = attestatores.chandra_page_partition_entries(
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
    chandra = _load_chandra()
    attestatores = _load_stage_module("run")
    observed = chandra.observe(
        _presented(), b'{"markdown":"one","blocks":[{"bbox":[0,0,200.2,260.0]}]}'
    )
    observed[0][field] = value

    with pytest.raises(SchemaRefusal, match=message):
        attestatores.chandra_page_partition_entries(
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

    survivors, findings = attestatores.chandra_page_partition_entries(
        observed,
        page_size=(200, 260),
        raw_response_ref={"relative_path": "retained", "sha256": "a" * 64},
    )

    assert survivors == observed
    assert findings == []


def test_a_parse_failure_keeps_its_bytes_and_its_name_through_the_written_record(tmp_path):
    """DoD bullet two, re-derived at the seam that actually publishes.

    The sibling test above proves retention against a stub tree. This one runs
    the real run-tree blob store and then carries the named outcome the whole way
    into a closed Testimonium payload, because that is where a silent absence
    would actually occur: a record whose `payload` had quietly become `None`
    while its bytes sat unreferenced on disk.
    """
    feeding = _load_stage_module("feeding")
    attestatores = _load_stage_module("run")

    # The blob store is content-addressed and directory-creating on write,
    # so this needs the real store rather than the stub above, not a whole
    # orchestrated run.
    tree = RunTree(tmp_path / "runs", "r")
    raw = b'{"markdown":"text","blocks":[{"bbox":[0,0,"bad",1]}]}'
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
        adapter_metadata={"geometry_quantization": _load_chandra().QUANTIZATION_RULE},
    )
    # The name is in the record, the bytes are addressed from the record, and
    # neither is a silent absence: `payload` is not None and `reported` -- the
    # textual bridge -- is correctly not offered for a non-textual payload.
    assert payload["payload"] == {"parse_outcome": "malformed-block-geometry"}
    assert payload["raw_response_ref"] == retained["raw_response_ref"]
    assert "reported" not in payload
    assert payload["content_health"]["recordable"] is True


def test_an_unknown_quantization_rule_is_refused_by_name(tmp_path):
    """The record's declared rule is closed against what the adapters declare.

    Not against Chandra's literal string: Units 12 and 13 also retain raw bytes,
    and a schema that admitted one adapter's rule by name would have refused
    theirs for not being Chandra's.
    """
    attestatores = _load_stage_module("run")

    chandra_rule = _load_chandra().QUANTIZATION_RULE
    attestatores.validate_adapter_metadata(
        {"adapter_metadata": {"geometry_quantization": chandra_rule}}
    )
    with pytest.raises(SchemaRefusal, match="not a quantization rule any bound adapter"):
        attestatores.validate_adapter_metadata(
            {"adapter_metadata": {"geometry_quantization": "invented.v9.round-to-taste"}}
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
            "geometry_quantization": _load_chandra().QUANTIZATION_RULE
        }
        assert payload["provenance"]["resolved_identity"] is not None

    # The other half of the pairing, and the reason it is a pairing: a record
    # whose geometry is only the presentation echo reports no conversion,
    # because none happened. A rule stated there would describe a float this
    # record never saw.
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
    """DoD bullet three, both halves in one place.

    The existing out-of-order test proves the refusal by exit code and reason.
    What it does not ask is the consequence the handoff makes load-bearing: a
    pass held before it published stage evidence must leave *no* seal, so its
    successor refuses a missing boundary rather than reading a stale one. A run
    that refuses loudly and seals anyway is the more dangerous failure, and only
    an inventory comparison can tell the two apart.
    """
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
