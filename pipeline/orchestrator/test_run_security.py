"""Security pins at the staged driver's subprocess and terminal boundaries."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.contracts.outcomes import ArmariumCategory
from conftest import load_stage

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


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
        designator_geometry_config="config/designator_geometry.toml",
        alignment_config="config/alignment.toml",
        page_accounting_config="config/page_accounting.toml",
        reconstruction_config="config/reconstruction.toml",
        ink_map_config="config/ink_map.toml",
        formats_config="config/formats.toml",
        recovery_config="config/recovery.toml",
        hard_failure_config="config/hard_failure.toml",
        review_config="config/review.toml",
        pdf_target_dpi=None,
        placement_tier=None,
        capacity_plan=None,
        corpus_register=None,
        witness_context="named",
        perlector_protocol_config="config/perlector_protocol.toml",
        decoding_config="config/decoding.toml",
        perlector_audit_config="config/perlector_audit.toml",
        # The real-submission argv surface. `require_coherent_ingress_options`
        # reads these three by name on every `invoke`, so a stand-in Namespace
        # that omits them is not the surface it claims to mirror.
        submission_folder=None,
        submission_manifest=None,
        canary_folder=None,
        canary_manifest=None,
        data_gate_policy=None,
        triage_decision_manifest=None,
        triage_clusters=None,
        triage_producer_recipe=None,
        cache_root=None,
        store_root=None,
        mechanics_qualification=False,
        perlector_concurrency=None,
    )


def test_invoke_forwards_explicit_mechanics_qualification_to_stage(tmp_path, monkeypatch):
    orchestrator = load_stage("orchestrator")
    observed = {}

    def completed(command, **kwargs):
        observed["command"] = command
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(orchestrator.subprocess, "run", completed)
    args = _invoke_args(tmp_path)
    args.mechanics_qualification = True

    assert orchestrator.invoke("pipeline/1_exemplar/door.py", args) == 0
    assert observed["command"].count("--mechanics-qualification") == 1


def test_invoke_forwards_perlector_concurrency_to_the_perlector_alone(tmp_path, monkeypatch):
    orchestrator = load_stage("orchestrator")
    commands = []

    def completed(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(orchestrator.subprocess, "run", completed)
    args = _invoke_args(tmp_path)
    assert orchestrator.invoke(orchestrator.STAGE_PROGRAMS["perlector"], args) == 0
    # Unset means the stage's own default, the served row's bound.
    assert "--perlector-concurrency" not in commands[-1]

    args.perlector_concurrency = 1
    for program in orchestrator.STAGE_PROGRAMS.values():
        assert orchestrator.invoke(program, args) == 0
    forwarded = [
        command[command.index("--perlector-concurrency") + 1]
        for command in commands[1:]
        if "--perlector-concurrency" in command
    ]
    assert forwarded == ["1"]
    assert (
        "--perlector-concurrency"
        in commands[1:][list(orchestrator.STAGE_PROGRAMS).index("perlector")]
    )


def test_the_timing_journal_names_the_perlector_concurrency_asked_for(tmp_path):
    orchestrator = load_stage("orchestrator")
    journal = tmp_path / "timings.jsonl"
    args = argparse.Namespace(
        stage_timing_journal=journal,
        run_id="r",
        run_root=tmp_path / "runs",
        repository_commit=None,
        perlector_concurrency=2,
    )
    for program in (
        orchestrator.STAGE_PROGRAMS["perlector"],
        orchestrator.STAGE_PROGRAMS["recensor"],
    ):
        orchestrator._record_stage_timing(
            args,
            program=program,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            duration_ms=1000,
            exit_code=0,
            gpu_utilization=(None, "not sampled"),
        )
    entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [entry["perlector_concurrency"] for entry in entries] == [2, None]


def test_child_python_ignores_an_injected_pythonpath_sitecustomize(tmp_path, monkeypatch):
    """No environment module executes before a stage reaches its refusal boundary."""
    orchestrator = load_stage("orchestrator")
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
    orchestrator = load_stage("orchestrator")
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
    orchestrator = load_stage("orchestrator")
    export = {
        "outcome": ArmariumCategory.DELIVERED.value,
        "payload": {"aggregate": {"status": "partial", "reasons": ["act remains held"]}},
    }

    with pytest.raises(ContractError, match="refuses to report complete over a conflict"):
        orchestrator.terminal_report(export)


def test_terminal_report_turns_malformed_reasons_into_a_named_refusal():
    """Untrusted terminal bytes cannot replace the refusal with a TypeError traceback."""
    orchestrator = load_stage("orchestrator")
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


def test_invoke_runs_the_stage_unbuffered_and_prints_its_start_and_end(
    tmp_path, monkeypatch, capsys
):
    orchestrator = load_stage("orchestrator")
    commands = []

    def completed(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(orchestrator.subprocess, "run", completed)
    assert (
        orchestrator.invoke(orchestrator.STAGE_PROGRAMS["perlector"], _invoke_args(tmp_path)) == 0
    )
    assert commands[0][1:3] == ["-I", "-u"]
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("run r: perlector started at ")
    assert lines[1].startswith("run r: perlector ended, exit 0, after ")


def _audit(chair: str, started_at: str, ready_at: str) -> bytes:
    from common.contracts.serving import SERVING_LAUNCH_AUDIT_SCHEMA

    return json.dumps(
        {
            "schema": SERVING_LAUNCH_AUDIT_SCHEMA,
            "chair": chair,
            "launch_purpose": "stage",
            "started_at": started_at,
            "readiness": {"ready_at": ready_at},
        }
    ).encode()


def _stage_with_blobs(tmp_path: Path, blobs: dict[str, bytes], noted: list[str]):
    from common.runtree.store import LAUNCH_AUDIT_NOTE_PREFIX

    stage = tmp_path / "runs" / "r" / "4_perlector"
    store = stage / "blobs" / "sha256"
    logs = stage / "serving-logs"
    store.mkdir(parents=True)
    logs.mkdir()
    (logs / "vllm-perlector-0.log").write_text("engine log\n")
    for name, data in blobs.items():
        (store / name).write_bytes(data)
    for name in noted:
        (logs / f"{LAUNCH_AUDIT_NOTE_PREFIX}{name}").write_bytes(b"")
    return store


def test_serving_spans_are_this_invocations_noted_launch_audits_and_nothing_else(tmp_path):
    orchestrator = load_stage("orchestrator")
    args = argparse.Namespace(run_root=tmp_path / "runs", run_id="r")
    current, earlier, unnoted = "a" * 64, "b" * 64, "c" * 64
    _stage_with_blobs(
        tmp_path,
        {
            current: _audit("perlector", "2026-10-07T10:00:05Z", "2026-10-07T10:06:20Z"),
            earlier: _audit("perlector", "2026-10-06T10:00:00Z", "2026-10-06T10:05:00Z"),
            unnoted: _audit("perlector", "2026-10-07T11:00:00Z", "2026-10-07T11:05:00Z"),
        },
        # An earlier pass's launch is noted too; a note naming a missing blob is skipped.
        [current, earlier, "d" * 64],
    )
    spans = orchestrator._serving_spans(
        args, orchestrator.STAGE_PROGRAMS["perlector"], "2026-10-07T10:00:00Z"
    )
    assert spans == [
        {
            "chair": "perlector",
            "launch_purpose": "stage",
            "started_at": "2026-10-07T10:00:05Z",
            "ready_at": "2026-10-07T10:06:20Z",
            "ready_seconds": 375,
        }
    ]
    # A stage with no serving logs launched nothing.
    assert orchestrator._serving_spans(args, "pipeline/5_recensor/run.py", "2026") == []


def test_serving_spans_read_no_blob_but_the_noted_audits(tmp_path, monkeypatch):
    """A stage store of thousands of page and call blobs costs nothing to look through."""
    orchestrator = load_stage("orchestrator")
    args = argparse.Namespace(run_root=tmp_path / "runs", run_id="r")
    audit = "a" * 64
    blobs = {f"{index:064x}": b'{"schema": "chair-call-record.v1"}' for index in range(1, 2000)}
    blobs[audit] = _audit("perlector", "2026-10-07T10:00:05Z", "2026-10-07T10:06:20Z")
    store = _stage_with_blobs(tmp_path, blobs, [audit])
    touched: list[str] = []
    read_bytes, listed = Path.read_bytes, Path.iterdir

    def counted_read(self):
        touched.append(self.name)
        return read_bytes(self)

    def counted_list(self):
        assert self != store, "the blob store was listed"
        return listed(self)

    monkeypatch.setattr(Path, "read_bytes", counted_read)
    monkeypatch.setattr(Path, "iterdir", counted_list)
    spans = orchestrator._serving_spans(
        args, orchestrator.STAGE_PROGRAMS["perlector"], "2026-10-07T10:00:00Z"
    )
    assert [span["ready_seconds"] for span in spans] == [375]
    assert touched == [audit]


def test_a_stored_launch_audit_is_noted_beside_the_engine_logs(tmp_path):
    from common.runtree.store import LAUNCH_AUDIT_NOTE_PREFIX, RunTree

    tree = RunTree(tmp_path, "r")
    tree.note_launch_audit("perlector", "e" * 64)
    tree.note_launch_audit("perlector", "e" * 64)  # an identical note is reused
    logs = tmp_path / "r" / tree.serving_log_path("perlector")
    assert [path.name for path in logs.iterdir()] == [f"{LAUNCH_AUDIT_NOTE_PREFIX}{'e' * 64}"]


def test_the_timing_journal_carries_serving_spans_and_stays_v4(tmp_path):
    orchestrator = load_stage("orchestrator")
    journal = tmp_path / "timings.jsonl"
    args = argparse.Namespace(
        stage_timing_journal=journal,
        run_id="r",
        run_root=tmp_path / "runs",
        repository_commit=None,
        perlector_concurrency=None,
    )
    span = {"chair": "perlector", "started_at": "a", "ready_at": "b", "ready_seconds": 1}
    orchestrator._record_stage_timing(
        args,
        program=orchestrator.STAGE_PROGRAMS["perlector"],
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        duration_ms=1000,
        exit_code=0,
        gpu_utilization=(None, "not sampled"),
        serving_spans=[span],
    )
    (entry,) = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert entry["schema"] == "stage-timing-journal.v4"
    assert entry["serving_spans"] == [span]
