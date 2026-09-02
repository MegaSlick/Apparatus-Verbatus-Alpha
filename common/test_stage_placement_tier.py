"""``--placement-tier`` is a measured runtime fact of the card, not run config.

SPEC_A §5: the flag defaults to ``None`` and is not sealed into ``config_digest``
or ``sealed_config_digests`` — the receipt records the caps that actually bound
the serving moment (GOVERNANCE 6, "the record itself protects the past"), it
does not fold that fact into the reproducibility contract those digests exist
to protect. `serving_mode_for` (operations/serving/client.py) is what actually
requires it when a live catalogue is selected; this suite covers only the
plumbing `stage_parser` and the orchestrator own — parsing, defaulting, and
staying out of the sealed digests.
"""

from __future__ import annotations

import importlib.util
import subprocess
from argparse import Namespace
from pathlib import Path

from common.chairs.registry import ChairRegistry
from common.stage import load_fixture, run_config_bindings, stage_parser

ROOT = Path(__file__).resolve().parents[1]


def _orchestrator_module(name: str):
    path = ROOT / "pipeline" / "orchestrator" / "run.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _invoke_namespace_fields(tmp_path: Path, **overrides) -> dict:
    """A minimal `invoke()` argv surface, mirroring every attribute it reads.

    Kept local to this file rather than imported from
    `test_orchestrator_acceptance._orchestrator_namespace_fields`: that
    function is this unit's one permitted edit in that file, not an import
    surface, and the two neighbouring literal copies are an accepted pattern
    already (see that function's own docstring comments).
    """
    fields = dict(
        run_root=tmp_path / "runs",
        run_id="r",
        scenario="happy",
        fixture_root=ROOT / "proof",
        models_config=ROOT / "config" / "models.toml",
        decoding_config=ROOT / "config" / "decoding.toml",
        serving_recipes_config=ROOT / "config" / "serving_recipes.toml",
        pdf_render_config=ROOT / "config" / "pdf_render.toml",
        designator_padding_config=ROOT / "config" / "designator_padding.toml",
        designator_geometry_config=ROOT / "config" / "designator_geometry.toml",
        alignment_config=ROOT / "config" / "alignment.toml",
        formats_config=ROOT / "config" / "armarium_formats.toml",
        recovery_config=ROOT / "config" / "recovery.toml",
        hard_failure_config=ROOT / "config" / "hard_failure.toml",
        pdf_target_dpi=None,
        placement_tier=None,
        witness_context="named",
        witness_context_config=ROOT / "config" / "witness_context.toml",
        nuda_per_mille=0,
        nuda_approval_ref="",
        perlector_instrument_per_mille=0,
        perlector_instrument_approval_ref="",
        perlector_protocol_config=ROOT / "config" / "perlector_protocol.toml",
        perlector_audit_config=ROOT / "config" / "perlector_audit.toml",
        draft_fed=True,
        corpus_register=None,
        submission_folder=None,
        submission_manifest=None,
        data_gate_policy=None,
    )
    fields.update(overrides)
    return fields


def test_orchestrator_forwards_placement_tier_only_when_set(tmp_path):
    """The orchestrator forwards `--placement-tier` iff it was set (SPEC_A §5).

    Mirrors `test_every_stage_receives_the_runs_selected_serving_recipes_catalogue`
    (pipeline/orchestrator/test_orchestrator_acceptance.py): mock
    `subprocess.run` and inspect the argv actually built, rather than the
    parsed namespace, so a regression that stops `invoke` from reading the
    attribute cannot pass by accident.
    """
    orchestrator = _orchestrator_module("orchestrator_placement_tier_argv")
    observed: list[list[str]] = []

    def fake_run(command, **_kwargs):
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    import types

    orchestrator.subprocess = types.SimpleNamespace(run=fake_run)

    program = orchestrator.STAGE_PROGRAMS["door"]

    unset_args = Namespace(**_invoke_namespace_fields(tmp_path))
    orchestrator.invoke(program, unset_args)
    assert observed and "--placement-tier" not in observed[-1], (
        "an unset --placement-tier must not be forwarded as the literal string "
        "'None'; stage_parser's own default already governs the child"
    )

    set_args = Namespace(**_invoke_namespace_fields(tmp_path, placement_tier="generic-48gb"))
    orchestrator.invoke(program, set_args)
    command = observed[-1]
    assert "--placement-tier" in command
    assert command[command.index("--placement-tier") + 1] == "generic-48gb"


def test_invoke_does_not_crash_on_a_namespace_that_declares_placement_tier(tmp_path):
    """The stand-in must carry the attribute `invoke` reads directly.

    `invoke` reads `args.placement_tier` by name, exactly like every sibling
    flag (`pdf_target_dpi`, `corpus_register`, ...) — no `getattr` fallback.
    A namespace built without the attribute would raise `AttributeError`
    before a single stage ran; this proves the documented namespace shape
    (mirrored in `_orchestrator_namespace_fields`, which U7p also updated to
    carry `placement_tier=None`) is what `invoke` actually needs and gets.
    """
    orchestrator = _orchestrator_module("orchestrator_placement_tier_namespace")
    import types

    orchestrator.subprocess = types.SimpleNamespace(
        run=lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", "")
    )
    args = Namespace(**_invoke_namespace_fields(tmp_path))
    program = orchestrator.STAGE_PROGRAMS["door"]

    orchestrator.invoke(program, args)  # must not raise AttributeError


def test_placement_tier_flag_defaults_to_none():
    parser = stage_parser("placement tier default")
    parsed = parser.parse_args(["--run-root", "runs", "--run-id", "r"])
    assert parsed.placement_tier is None


def test_placement_tier_flag_carries_the_supplied_value():
    parser = stage_parser("placement tier explicit")
    parsed = parser.parse_args(
        ["--run-root", "runs", "--run-id", "r", "--placement-tier", "generic-48gb"]
    )
    assert parsed.placement_tier == "generic-48gb"


def test_placement_tier_is_absent_from_the_sealed_config_digests():
    """A measured runtime fact never enters `run_config_bindings`' inputs.

    `run_config_bindings` accepts no `placement_tier`-shaped keyword at all,
    so nothing that calls it can seal one in — checked structurally against
    its signature — and the two dicts it returns as "what this run sealed"
    carry no key named for the tier either, at the function's real default
    binding.
    """
    import inspect

    signature = inspect.signature(run_config_bindings)
    assert "placement_tier" not in signature.parameters

    models = ChairRegistry.from_toml(ROOT / "config" / "models.toml").config
    fixture = load_fixture(ROOT / "proof")
    bindings = run_config_bindings(models, fixture, "happy")

    assert "placement_tier" not in bindings["sealed_config_digests"]
    assert "placement-tier" not in bindings["sealed_config_digests"]
    assert "tier" not in bindings["sealed_config_digests"]
    assert "placement_tier" not in bindings["serving_config_inputs"]
