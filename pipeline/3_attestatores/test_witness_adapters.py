"""The native callables remain a private Attestatores sibling module."""

import dataclasses
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from common.witness_adapters import KNOWN_WITNESS_ADAPTER_NAMES

ROOT = Path(__file__).resolve().parents[2]
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
    # Bound by identity, not by "is not None": the point of the slot is which
    # function answers there, and a rebinding to a different one is exactly the
    # change a later adapter unit must not make silently.
    assert spec.retain is adapters.feeding.retain_model_view


def test_the_registry_does_not_pre_empt_the_intake_contract_seam_names():
    """`presented`/`observed` are the intake contract's words for seams this
    slice does not build. A slot wearing either name here would be a placeholder
    that a later unit reads as already bound, and retention is not observation:
    raw bytes are the native layer, an observation is derived."""
    adapters = _load_local_adapters()
    fields = {field.name for field in dataclasses.fields(adapters.RunnableAdapter)}
    assert fields == {"prompt", "parse", "retain"}


def test_a_callable_binding_that_raises_at_import_fails_loudly_without_fallback(monkeypatch):
    """Import finishes before ``run.main`` can open or write a run tree.

    A valid shared name therefore cannot fall through to another adapter when
    its local callable binding is broken: the original exception propagates and
    module construction stops. This models an adapter-local dependency or
    eager binding that raises while its module is imported.
    """

    exploding = ModuleType("feeding")

    def broken_binding(name):
        raise RuntimeError(f"fixture callable import failed at {name}")

    exploding.__getattr__ = broken_binding
    monkeypatch.setitem(sys.modules, "feeding", exploding)

    with pytest.raises(RuntimeError, match="fixture callable import failed at churro_prompt"):
        _load_local_adapters()


@pytest.mark.parametrize("name", ("", " ", None, "churro.v2"))
def test_local_callable_resolution_refuses_missing_or_unknown_names(name):
    adapters = _load_local_adapters()
    with pytest.raises(adapters.AdapterRefusal) as caught:
        adapters.resolve_runnable_adapter(name)
    assert caught.value.name == name
    assert repr(name) in str(caught.value)
