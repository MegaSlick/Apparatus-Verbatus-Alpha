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
        designator_grouping_config=ROOT / "config" / "designator_grouping.toml",
        alignment_config=ROOT / "config" / "alignment.toml",
        formats_config=ROOT / "config" / "formats.toml",
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


def test_invoke_namespace_fields_mirrors_the_argv_surface_invoke_reads():
    """This file's own stand-in must carry exactly the attributes `invoke` reads.

    A bidirectional AST guard, mirrored from
    `test_the_stand_in_namespace_mirrors_the_argv_surface_it_claims_to`
    (pipeline/orchestrator/test_run_security.py): parse `invoke` and
    `require_coherent_ingress_options` in the real orchestrator module and
    collect every `args.<attr>` read, then require that set to match
    `_invoke_namespace_fields` exactly in both directions. A stand-in that
    merely repeats a caller's assertion (the previous version of this test)
    cannot fail when `invoke`'s surface moves; this one does — it is what
    caught pr/14's dropped `placement_tier` attribute.
    """
    import ast

    tree = ast.parse((ROOT / "pipeline" / "orchestrator" / "run.py").read_text(encoding="utf-8"))
    reads: set[str] = set()
    for name in ("invoke", "require_coherent_ingress_options"):
        target = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
        )
        for node in ast.walk(target):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "args"
            ):
                reads.add(node.attr)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "args"
                and isinstance(node.args[1], ast.Constant)
            ):
                reads.add(node.args[1].value)

    supplied = set(_invoke_namespace_fields(Path("/tmp")))
    assert reads - supplied == set(), (
        f"invoke() reads {sorted(reads - supplied)}, which _invoke_namespace_fields never "
        "sets; a probe built from it would raise AttributeError before asserting anything"
    )
    assert supplied - reads == set(), (
        f"_invoke_namespace_fields sets {sorted(supplied - reads)}, which invoke() never reads"
    )


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
    assert set(bindings["serving_config_inputs"]) == {
        "schema",
        "serving_recipes_sha256",
        "pod_placement_sha256",
    }
