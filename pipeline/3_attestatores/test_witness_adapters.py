"""The native callables remain a private Attestatores sibling module."""

import copy
import dataclasses
import importlib.util
import sys
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ATTESTATORES, EXEMPLAR
from common.imaging import encode_grayscale_png_deterministic
from common.native_witness import validate_presented_page_binding
from common.witness_adapters import KNOWN_WITNESS_ADAPTER_NAMES

STAGE = Path(__file__).resolve().parent


def _load_local_adapters():
    path = STAGE / "witness_adapters.py"
    spec = importlib.util.spec_from_file_location("attestatores_witness_adapters", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Snapshot rather than `remove`: the stage directory is already on the path
    # once `run.py` has been imported, and value-based removal takes the first
    # occurrence -- that module's entry, not the one inserted here.
    original_path = list(sys.path)
    sys.path.insert(0, str(STAGE))
    try:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
        sys.path[:] = original_path
    return module


def test_every_declared_adapter_has_a_runnable_fixture_shape():
    adapters = _load_local_adapters()
    assert set(adapters.RUNNABLE_ADAPTERS) == KNOWN_WITNESS_ADAPTER_NAMES
    spec = adapters.resolve_runnable_adapter("churro.v1")
    assert spec is adapters.RUNNABLE_ADAPTERS["churro.v1"]
    assert set(spec.prompt()) == {"system", "user"}
    assert spec.parse(b"<output>text</output>") == "text"
    # Bound by identity, not by "is not None": the point of the slot is which
    # function answers there, and a rebinding to a different one is exactly the
    # change a later adapter unit must not make silently. Churro's slot binds
    # the relabel-proof wrapper the test below proves out.
    assert spec.retain is adapters._retain_churro_model_view
    chandra = adapters.resolve_runnable_adapter("chandra.v1")
    assert set(chandra.prompt()) == {"instruction"}
    assert (
        chandra.parse(b'{"schema":"fixture-chandra-response.v1","markdown":"text","blocks":[]}')
        == "text"
    )
    assert chandra.retain is adapters.chandra.retain


def test_retention_is_bound_to_the_resolved_adapter_and_cannot_be_relabeled():
    """The registry name remains the retained model-view provenance.

    Returning the generic ``retain_model_view`` function exposed its
    ``adapter=`` argument to the caller. A caller could resolve ``churro.v1``
    and then retain the same bytes under another adapter's name, making the
    sealed registry advisory precisely where it is meant to bind provenance.
    """
    adapters = _load_local_adapters()
    spec = adapters.resolve_runnable_adapter("churro.v1")
    blobs: list[bytes] = []

    def put_blob(_stage, payload):
        blobs.append(payload)
        return "a" * 64, SimpleNamespace(relative_path="blobs/a")

    retained = spec.retain(
        SimpleNamespace(put_blob=put_blob),
        view={"kind": "fixture"},
        raw_response=b"<output>text</output>",
        transport_stop_reason="complete",
    )

    assert retained["adapter"] == "churro.v1"
    assert blobs == [b"<output>text</output>"]
    with pytest.raises(TypeError, match="unexpected keyword argument 'adapter'"):
        spec.retain(
            SimpleNamespace(put_blob=put_blob),
            adapter="another.v1",
            view={"kind": "fixture"},
            raw_response=b"<output>text</output>",
            transport_stop_reason="complete",
        )


class _Published:
    def __init__(self, relative_path):
        self.relative_path = relative_path


class _DaiTree:
    def __init__(self, page_bytes):
        self.page_bytes = page_bytes
        self.blobs = {}

    def read_artifact(self, stage, kind, item_id):
        assert (stage, kind, item_id) == (EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", "page-1"))
        return {
            "payload": {
                "image_path": "1_exemplar/page-1.png",
                "source_sha256": digest_bytes(self.page_bytes),
                "ordinal": 1,
            }
        }

    def read_bytes(self, relative_path):
        if relative_path == "1_exemplar/page-1.png":
            return self.page_bytes
        return self.blobs[relative_path]

    def put_blob(self, stage, payload):
        assert stage == ATTESTATORES
        digest = digest_bytes(payload)
        path = f"3_attestatores/blobs/sha256/{digest}"
        self.blobs[path] = payload
        return digest, _Published(path)


class _DaiContext:
    def __init__(self, page_bytes):
        self.tree = _DaiTree(page_bytes)


def test_dai_crop_resize_is_a_rederivable_adapter_crop_and_preserves_uncertainty_tokens():
    """The shown pixels come from the sealed page recipe, not an opaque resize blob."""
    page = encode_grayscale_png_deterministic(3_000, 2, [bytearray(3_000), bytearray(3_000)])
    context = _DaiContext(page)
    source = _dai_region(3_000, 2)
    adapters = _load_local_adapters()
    adapter = adapters.resolve_runnable_adapter("dai.v1")
    presented = adapter.present(context, source)

    assert presented["kind"] == "adapter-crop"
    assert presented["transform"]["operation"] == "crop-resize-preserve-aspect"
    assert presented["transform"]["resize"] == {
        "resampler": "pillow-lanczos",
        "dimension_rounding": "floor",
        "source_width_px": 3_000,
        "source_height_px": 2,
        "target_width_px": 1_500,
        "target_height_px": 1,
    }
    validate_presented_page_binding(
        presented,
        page_ordinal=1,
        page_image_path="1_exemplar/page-1.png",
        page_sha256=digest_bytes(page),
        page_size=(3_000, 2),
        page_bytes=page,
    )
    response = "[UNCERTAIN] Marie [CROSSED_OUT]"
    assert adapter.parse(response.encode()) == response
    assert adapter.observe(presented, response)[0]["span"] == {
        "start": 0,
        "end": len(response),
    }


def test_dai_readback_refuses_an_aspect_valid_size_its_ceiling_recipe_cannot_produce():
    """Page re-derivation alone accepts any aspect-valid target whose digest
    matches; the tally must also prove DAI's largest-fit recipe chose it."""
    page = encode_grayscale_png_deterministic(3_000, 2, [bytearray(3_000), bytearray(3_000)])
    context = _DaiContext(page)
    source = _dai_region(3_000, 2)
    adapters = _load_local_adapters()
    presented = adapters.resolve_runnable_adapter("dai.v1").present(context, source)
    adapters.validate_adapter_presentation("dai.v1", source, presented)

    forged = copy.deepcopy(presented)
    forged["transform"]["resize"]["target_width_px"] = 1_499
    # Both 1500x1 and 1499x1 satisfy the recorded floor-aspect identity, but
    # only the former is the largest view under DAI's sealed ceilings.
    with pytest.raises(SchemaRefusal, match="assigned proposal and sealed resize ceilings"):
        adapters.validate_adapter_presentation("dai.v1", source, forged)


def test_dai_readback_refuses_a_same_page_crop_other_than_its_assigned_proposal():
    """A different crop can re-derive perfectly from the same sealed page; its
    valid digest does not make it the proposal this witness was assigned."""
    page = encode_grayscale_png_deterministic(3_000, 2, [bytearray(3_000), bytearray(3_000)])
    context = _DaiContext(page)
    source = _dai_region(3_000, 2)
    adapters = _load_local_adapters()
    forged = adapters.resolve_runnable_adapter("dai.v1").present(
        context, _dai_region(2_999, 2, x=1)
    )
    validate_presented_page_binding(
        forged,
        page_ordinal=1,
        page_image_path="1_exemplar/page-1.png",
        page_sha256=digest_bytes(page),
        page_size=(3_000, 2),
        page_bytes=page,
    )
    with pytest.raises(SchemaRefusal, match="assigned proposal and sealed resize ceilings"):
        adapters.validate_adapter_presentation("dai.v1", source, forged)


def test_dai_crop_refuses_bytes_swapped_after_page_artifact_verification():
    page = _dai_page(20, 10)
    context = _DaiContext(page)
    original_read_artifact = context.tree.read_artifact

    def verified_before_swap(stage, kind, item_id):
        page_record = original_read_artifact(stage, kind, item_id)
        context.tree.page_bytes = page[:-1] + bytes([page[-1] ^ 1])
        return page_record

    context.tree.read_artifact = verified_before_swap
    adapters = _load_local_adapters()

    with pytest.raises(SchemaRefusal, match="changed between artifact verification and crop use"):
        adapters.resolve_runnable_adapter("dai.v1").present(context, _dai_region(20, 10))
    assert context.tree.blobs == {}


def test_the_registry_binds_the_native_intake_contract_seams():
    """Every adapter exposes the closed native and derived intake seams."""
    adapters = _load_local_adapters()
    fields = {field.name for field in dataclasses.fields(adapters.RunnableAdapter)}
    # Quantization is data beside the five operations; no-layout adapters must
    # explicitly remain without a conversion rule.
    assert fields == {"prompt", "parse", "retain", "present", "observe", "quantization"}
    assert adapters.RUNNABLE_ADAPTERS["churro.v1"].quantization is None
    assert (
        adapters.RUNNABLE_ADAPTERS["chandra.v1"].quantization == adapters.chandra.QUANTIZATION_RULE
    )
    assert adapters.declared_quantization_rules() == {adapters.chandra.QUANTIZATION_RULE}
    presented = {
        "kind": "page",
        "source_page_id": "page-1",
        "source_page_ordinal": 1,
        "image_path": "1_exemplar/blobs/sha256/" + "0" * 64,
        "image_sha256": "0" * 64,
        "transform": {
            "operation": "whole",
            "source_page_id": "page-1",
            "source_page_ordinal": 1,
            "bounds": {"x": 0, "y": 0, "w": 20, "h": 10},
        },
    }
    assert adapters.resolve_runnable_adapter("churro.v1").present(object(), presented) is presented
    assert adapters.resolve_runnable_adapter("churro.v1").observe(presented, "retained text") == [
        {
            "ordinal": 0,
            "bounds": {"x": 0, "y": 0, "w": 20, "h": 10},
            "bounds_source": "presented",
            "span": None,
        }
    ]


def test_a_callable_binding_that_raises_at_import_fails_loudly_without_fallback(monkeypatch):
    """A broken eager binding must propagate before a run opens, with no fallback."""

    exploding = ModuleType("feeding")

    def broken_binding(name):
        raise RuntimeError(f"fixture callable import failed at {name}")

    exploding.__getattr__ = broken_binding
    monkeypatch.setitem(sys.modules, "feeding", exploding)

    with pytest.raises(RuntimeError, match="fixture callable import failed at churro_prompt"):
        _load_local_adapters()


@pytest.mark.parametrize(
    "name",
    ("", " ", None, "churro.v2", pytest.param(10**5000, id="huge-int")),
)
def test_local_callable_resolution_refuses_missing_or_unknown_names(name):
    adapters = _load_local_adapters()
    with pytest.raises(adapters.AdapterRefusal) as caught:
        adapters.resolve_runnable_adapter(name)
    assert caught.value.name == name
    message = str(caught.value)
    expected_display = (
        repr(name) if isinstance(name, str) or name is None else f"<{type(name).__name__}>"
    )
    assert expected_display in message
    assert "No exact adapter can be resolved" in message or "No adapter code can run" in message
    assert "Set witness_adapter" in message


def test_a_non_string_adapter_with_a_broken_repr_still_gets_the_named_refusal():
    adapters = _load_local_adapters()

    class BrokenRepr:
        def __repr__(self):
            raise RuntimeError("repr must not run")

    name = BrokenRepr()
    with pytest.raises(adapters.AdapterRefusal) as caught:
        adapters.resolve_runnable_adapter(name)

    assert caught.value.name is name
    assert "witness adapter <BrokenRepr> is blank or not a string" in str(caught.value)


def test_a_shared_name_without_a_runnable_binding_refuses_with_the_repair(monkeypatch):
    adapters = _load_local_adapters()
    monkeypatch.delitem(adapters.RUNNABLE_ADAPTERS, "churro.v1")

    with pytest.raises(adapters.AdapterRefusal) as caught:
        adapters.resolve_runnable_adapter("churro.v1")

    message = str(caught.value)
    assert "has no runnable Attestatores binding" in message
    assert "shared declaration cannot execute" in message
    assert "Add the same exact name to RUNNABLE_ADAPTERS" in message


def _dai_page(width, height, mode="L"):
    """Only modes and encoding paths admitted by the door may reach the adapter."""
    from common.imaging import _encode_crop_deterministic

    image = Image.new(mode, (width, height), 1 if mode == "1" else 0)
    for x in range(width):
        for y in range(height):
            image.putpixel((x, y), 0 if mode == "1" and x % 3 == 0 else (x * 7 + y * 13) % 256)
    return _encode_crop_deterministic(image)


def _dai_region(width, height, x=0, y=0):
    return {
        "kind": "region",
        "source_page_id": "page-1",
        "source_page_ordinal": 1,
        "image_path": "2_designator/crop.png",
        "image_sha256": "a" * 64,
        "transform": {
            "operation": "crop",
            "source_page_id": "page-1",
            "source_page_ordinal": 1,
            "bounds": {"x": x, "y": y, "w": width, "h": height},
        },
        "region_ref": {"region_id": "region-1"},
    }


@pytest.mark.parametrize(
    ("width", "height", "mode", "target", "operation"),
    [
        pytest.param(1_500, 100, "L", (1_500, 100), "crop", id="at-the-width-ceiling"),
        pytest.param(
            1_501,
            100,
            "L",
            (1_500, 99),
            "crop-resize-preserve-aspect",
            id="one-past-the-width-ceiling",
        ),
        # 1536x1536 is the total-pixel ceiling exactly, and it is the width
        # ceiling that binds there rather than the pixel budget.
        pytest.param(
            1_536,
            1_536,
            "L",
            (1_500, 1_500),
            "crop-resize-preserve-aspect",
            id="at-the-total-pixel-ceiling",
        ),
        # 576x4096 = 2359296 exactly: all three ceilings meet on this one shape,
        # which `DAI_LIMIT_SOURCES` names as the reason the height ceiling is 4096.
        pytest.param(
            576,
            4_096,
            "L",
            (576, 4_096),
            "crop",
            id="where-all-three-ceilings-meet",
        ),
        pytest.param(
            576,
            4_097,
            "L",
            (575, 4_089),
            "crop-resize-preserve-aspect",
            id="one-past-the-height-ceiling",
        ),
        # Nested flooring undercuts the largest feasible width in this aspect band.
        pytest.param(
            581,
            4_212,
            "L",
            (565, 4_096),
            "crop-resize-preserve-aspect",
            id="in-the-rounding-band",
        ),
        pytest.param(1, 1, "L", (1, 1), "crop", id="one-pixel"),
        # A bilevel scan, which the door seals as mode `1` rather than promoting.
        pytest.param(
            1_600,
            400,
            "1",
            (1_500, 375),
            "crop-resize-preserve-aspect",
            id="bilevel-resized",
        ),
        pytest.param(800, 400, "1", (800, 400), "crop", id="bilevel-needing-no-resize"),
    ],
)
def test_the_recorded_transform_replays_to_the_same_bytes_at_every_ceiling(
    width, height, mode, target, operation
):
    """Recorded transforms, not derivation arithmetic, must reproduce boundary views."""
    page = _dai_page(width, height, mode)
    context = _DaiContext(page)
    adapters = _load_local_adapters()

    presented = adapters.resolve_runnable_adapter("dai.v1").present(
        context, _dai_region(width, height)
    )

    assert presented["transform"]["operation"] == operation
    if operation == "crop":
        assert "resize" not in presented["transform"]
        assert target == (width, height)
    else:
        resize = presented["transform"]["resize"]
        assert (resize["target_width_px"], resize["target_height_px"]) == target
        assert resize["target_width_px"] <= 1_500
        assert resize["target_height_px"] <= 4_096
        assert resize["target_width_px"] * resize["target_height_px"] <= 2_359_296
    validate_presented_page_binding(
        presented,
        page_ordinal=1,
        page_image_path="1_exemplar/page-1.png",
        page_sha256=digest_bytes(page),
        page_size=(width, height),
        page_bytes=page,
    )
    published = context.tree.blobs[presented["image_path"]]
    assert digest_bytes(published) == presented["image_sha256"]
    with Image.open(BytesIO(published)) as shown:
        assert shown.size == target


@pytest.mark.parametrize(
    ("width", "height", "x", "y"),
    [
        pytest.param(2_001, 1_000, 0, 0, id="wider-than-the-page"),
        pytest.param(2_000, 1_000, 1, 0, id="shifted-off-the-right-edge"),
        pytest.param(500, 500, 1_900, 900, id="a-corner-box-running-off-two-edges"),
    ],
)
def test_a_proposal_box_past_the_page_edge_is_refused_by_name(width, height, x, y):
    """Bounds failures must be schema refusals, with no adapter blob published."""
    page = _dai_page(2_000, 1_000)
    adapters = _load_local_adapters()
    context = _DaiContext(page)

    with pytest.raises(SchemaRefusal, match="falls outside the sealed source page"):
        adapters.resolve_runnable_adapter("dai.v1").present(
            context, _dai_region(width, height, x, y)
        )
    assert context.tree.blobs == {}, "a refused presentation published adapter bytes"
