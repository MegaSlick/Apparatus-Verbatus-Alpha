"""Security pins at the staged driver's subprocess and terminal boundaries."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import subprocess
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.contracts.outcomes import ArmariumCategory

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def _load_orchestrator():
    spec = importlib.util.spec_from_file_location("orchestrator_run_security", ORCHESTRATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _invoke_args(tmp_path: Path) -> argparse.Namespace:
    """The stable argv surface ``invoke`` forwards; the probe ignores its values."""
    return argparse.Namespace(
        run_root=tmp_path / "runs",
        run_id="r",
        scenario="happy",
        fixture_root="proof",
        models_config="config/models.toml",
        serving_recipes_config="config/serving_recipes.toml",
        pdf_render_config="config/pdf_render.toml",
        designator_padding_config="config/designator_padding.toml",
        designator_geometry_config="config/designator_geometry.toml",
        designator_grouping_config="config/designator_grouping.toml",
        alignment_config="config/alignment.toml",
        formats_config="config/formats.toml",
        recovery_config="config/recovery.toml",
        hard_failure_config="config/hard_failure.toml",
        pdf_target_dpi=None,
        corpus_register=None,
        witness_context="named",
        witness_context_config="config/witness_context.toml",
        nuda_per_mille=0,
        nuda_approval_ref="",
        perlector_instrument_per_mille=0,
        perlector_instrument_approval_ref="",
        perlector_protocol_config="config/perlector_protocol.toml",
        decoding_config="config/decoding.toml",
        perlector_audit_config="config/perlector_audit.toml",
        draft_fed=True,
        # The real-submission argv surface. `require_coherent_ingress_options`
        # reads these three by name on every `invoke`, so a stand-in Namespace
        # that omits them is not the surface it claims to mirror.
        submission_folder=None,
        submission_manifest=None,
        data_gate_policy=None,
    )


def test_child_python_ignores_an_injected_pythonpath_sitecustomize(tmp_path, monkeypatch):
    """No environment module executes before a stage reaches its refusal boundary."""
    orchestrator = _load_orchestrator()
    marker = tmp_path / "sitecustomize-ran"
    (tmp_path / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    probe = tmp_path / "probe.py"
    probe.write_text("raise SystemExit(0)\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    assert orchestrator.invoke(str(probe), _invoke_args(tmp_path)) == 0
    assert not marker.exists()


def test_invoke_inherits_streams_instead_of_buffering_unbounded_stage_output(tmp_path, monkeypatch):
    orchestrator = _load_orchestrator()
    observed = {}

    def completed(command, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(orchestrator.subprocess, "run", completed)

    assert orchestrator.invoke("pipeline/1_exemplar/door.py", _invoke_args(tmp_path)) == 0
    assert "capture_output" not in observed
    assert "stdout" not in observed
    assert "stderr" not in observed


def test_terminal_report_refuses_delivered_over_partial_aggregate():
    """A contradictory record never becomes a successful complete report."""
    orchestrator = _load_orchestrator()
    export = {
        "outcome": ArmariumCategory.DELIVERED.value,
        "payload": {"aggregate": {"status": "partial", "reasons": ["act remains held"]}},
    }

    with pytest.raises(ContractError, match="refuses to report complete over a conflict"):
        orchestrator.terminal_report(export)


def test_terminal_report_turns_malformed_reasons_into_a_named_refusal():
    """Untrusted terminal bytes cannot replace the refusal with a TypeError traceback."""
    orchestrator = _load_orchestrator()
    export = {
        "outcome": ArmariumCategory.HELD_FOR_REVIEW.value,
        "payload": {"aggregate": {"status": "partial", "reasons": [None]}},
    }

    with pytest.raises(ContractError, match="blank or non-string terminal reason"):
        orchestrator.terminal_report(export)


def test_the_stand_in_namespace_mirrors_the_argv_surface_it_claims_to():
    """Two ways this stand-in silently stops being the surface it says it is.

    It drifts when `invoke` starts reading an attribute the Namespace does not
    carry -- which is not a caught refusal but an AttributeError raised before
    either probe reaches its assertion, so the security pin stops running while
    still looking green in a count. Both this file's subprocess-boundary tests
    were doing exactly that until the attribute was restored.

    It also drifts in its values. `formats_config` named
    `config/armarium_formats.toml`, a file that has never existed; the real
    default is `config/formats.toml`. Nothing failed, because the probe ignores
    the values -- which is precisely why a wrong one can sit here indefinitely
    while the docstring above calls this the stable argv surface.
    """
    reads: set[str] = set()
    for name in ("invoke", "require_coherent_ingress_options"):
        function = ast.parse(ORCHESTRATOR.read_text(encoding="utf-8"))
        target = next(
            node
            for node in function.body
            if isinstance(node, ast.FunctionDef) and node.name == name
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

    supplied = vars(_invoke_args(Path("/tmp")))
    assert reads - set(supplied) == set(), (
        f"invoke() reads {sorted(reads - set(supplied))}, which this stand-in never sets; "
        "the probes would raise AttributeError before asserting anything"
    )
    assert set(supplied) - reads == set(), (
        f"this stand-in sets {sorted(set(supplied) - reads)}, which invoke() never reads"
    )

    for attribute, value in supplied.items():
        if isinstance(value, str) and value.startswith("config/"):
            assert (ROOT / value).is_file(), (
                f"{attribute}={value!r} names a config file that does not exist; a stand-in "
                "for the real argv surface must not carry a path the real run could not use"
            )
