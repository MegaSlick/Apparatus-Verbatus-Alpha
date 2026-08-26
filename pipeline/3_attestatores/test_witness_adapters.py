import dataclasses
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from common.witness_adapters import KNOWN_WITNESS_ADAPTER_NAMES

STAGE = Path(__file__).resolve().parent


def _load_local_adapters():
    path = STAGE / "witness_adapters.py"
    spec = importlib.util.spec_from_file_location("attestatores_witness_adapters", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(STAGE))
    try:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
        sys.path.remove(str(STAGE))
    return module


def test_churro_is_the_only_currently_runnable_fixture_adapter_shape():
    adapters = _load_local_adapters()
    assert set(adapters.RUNNABLE_ADAPTERS) == KNOWN_WITNESS_ADAPTER_NAMES
    spec = adapters.resolve_runnable_adapter("churro.v1")
    assert spec is adapters.RUNNABLE_ADAPTERS["churro.v1"]
    assert set(spec.prompt()) == {"system", "user"}
    assert spec.parse(b"<output>text</output>") == "text"


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


def test_the_registry_binds_the_native_intake_contract_seams():
    """10B fills 10A's reserved derived-layer seams with real callables."""
    adapters = _load_local_adapters()
    fields = {field.name for field in dataclasses.fields(adapters.RunnableAdapter)}
    assert fields == {"prompt", "parse", "retain", "present", "observe"}
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
