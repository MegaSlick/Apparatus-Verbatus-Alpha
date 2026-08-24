"""Chandra's offline fixture seam: retained bytes, native boxes, and ordering."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES
from common.native_witness import validate_observed
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
        "outcome": "unverified-response-schema",
    }
    chandra = _load_chandra()
    assert chandra.parse(
        b'{"schema":"fixture-chandra-response.v1","markdown":"text",'
        b'"blocks":[{"bbox":[0,0,"bad",1]}]}'
    ) == {"parse_outcome": "malformed-block-geometry"}


def test_chandra_shape_surprise_is_a_failed_attempt_not_a_successful_read(tmp_path):
    """Named bytes remain evidence, but an unread schema is not a reading."""
    from common.chairs import load_models_toml

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
    from common.chairs import load_models_toml

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
    from common.chairs import load_models_toml

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
    chandra = _load_chandra()
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


def test_an_unverified_chandra_wire_shape_cannot_acquire_fixture_geometry():
    """Only explicitly synthetic bytes use the placeholder page-pixel rule."""
    chandra = _load_chandra()
    raw = b'{"markdown":"plausible live response","blocks":[{"bbox":[0,0,100,100]}]}'
    assert chandra.parse(raw) == {"parse_outcome": "unverified-response-schema"}
    assert chandra.observe(_presented(), raw) == []


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
    raw = (
        b'{"schema":"fixture-chandra-response.v1","markdown":"one",'
        b'"blocks":[{"bbox":[10,10,100,100]},{"bbox":[50,50,150,150]}]}'
    )
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
    chandra = _load_chandra()
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
    retained blob, where a later unit can decide what it means.
    """
    chandra = _load_chandra()
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


def test_a_block_past_the_page_edge_is_refused_rather_than_clamped_into_the_page():
    """The declared rule rounds outward, and outward past the page is a refusal.

    `ceil` on a max edge means any block whose float edge sits fractionally past
    the sealed page derives a box the shared wall refuses, which takes the whole
    Attestatores pass to `UNKNOWN`. Clamping it to the page instead would be the
    cheaper failure and is the wrong one: on a Designator fallback crop -- whose
    region *is* the whole page -- a clamped box lands exactly on the recovery
    region and hands it the retrospective witness coverage a recovery crop may
    never acquire. So the conversion never invents an in-page box from an
    out-of-page report; it reports what it derived and lets the wall speak.

    This test pins that ruling in both directions, so a later seat that finds the
    hold expensive cannot quietly buy relief with a clamp.
    """
    chandra = _load_chandra()
    raw = (
        b'{"schema":"fixture-chandra-response.v1","markdown":"one",'
        b'"blocks":[{"bbox":[0,0,200.2,260.0]}]}'
    )
    observed = chandra.observe(_presented(), raw)
    assert observed[0]["bounds"] == {"x": 0, "y": 0, "w": 201, "h": 260}
    with pytest.raises(SchemaRefusal, match="outside the sealed source page"):
        validate_observed(observed, presented=_presented(), page_size=(200, 260))


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


def test_quantization_metadata_belongs_to_the_recorded_adapter_and_blob():
    attestatores = _load_stage_module("run")
    chandra_rule = _load_chandra().QUANTIZATION_RULE
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


def test_the_page_record_names_the_bytes_its_own_geometry_was_quantized_from(tmp_path):
    """DoD bullet one, asked of the record that actually carries the geometry.

    The act-scoped Testimonia are a compatibility bridge Unit 14 removes. The
    durable output of a page-scoped occupant is the page Testimonium, and it is
    the record whose `observed` boxes are integers this pipeline computed from
    floats it did not keep. Without a reference to the retained response, that
    record states a derived result with no route back to the evidence -- and the
    route has to be in the record, not reconstructable by re-joining records that
    are scheduled for deletion (GOALS 5).
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
