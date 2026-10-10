"""The Perlector stage wired to a live chair, proven offline end to end.

Nothing here starts a pod, opens a socket, or loads a model. The run tree is
built by the real Door-through-Attestatores chain as subprocesses, and then
`run.py`'s own `main` is called in this process with the fake endpoint from
`operations/serving/fakes.py` behind it — so what is proved is the stage's
wiring: which reader the sealed catalogue selects, how a live call's record is
held to the call that produced it, how a resumed pass accounts for retained
replies, and that the chair is stopped before the seal.

The selector is deliberately not a flag on this stage. A run is live because the
serving-recipe row sealed into its `config_digest` says `kind = "vllm"` for the
resolved Perlector chair, so these tests build a catalogue whose Perlector rows
are live and let the run bind it exactly as a real one would.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from common.chairs.registry import ChairRegistry
from common.contracts.canonical import digest_bytes, self_hash
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import ATTESTATORES, PERLECTOR
from common.decoding import load_decoding_policy
from common.in_order_window import HELD_PER_SLOT
from common.page_path import distinct_refs
from common.page_testimonia import validate_page_testimonium_record
from common.runtree.store import SERVING_LOGS_DIR, RunTree
from common.sealed_config import read_sealed_toml
from common.stage import StageContext
from conftest import load_stage, programs_through
from operations.serving.assembly import retain_chair_bytes, stage_chair_client
from operations.serving.client import ChairClient, ServingModeRefusal
from operations.serving.config import (
    ServingConfigInputs,
    chair_preflight_identity_digest,
    load_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.errors import ChairTransportFailure, ServiceStopError
from operations.serving.fakes import (
    FakeEndpoint,
    FakeLauncher,
    FakePackages,
    FakeRegistry,
    ScriptedAnswer,
)
from operations.serving.manager import ServingManager, StageContextReceiptPublisher
from operations.serving.residency import POD_RESIDENCY_LOCK_PATH, FileResidencyLease

ROOT = Path(__file__).resolve().parents[2]
CHAIN_THROUGH_ATTESTATORES = programs_through("attestatores")
TIER = "generic-48gb"
SERVED_MODEL_ID = "perlector-under-test"
# Long enough that `truncation.is_length_suspicious` never fires on this
# fixture's regions (80 characters over 12,800 or 16,000 pixels of a 52,000-pixel
# page is 260-325 characters per page-equivalent, on a page too small for the
# sealed floor in `config/perlector_protocol.toml` to judge): the tests below are about the engine's
# own stop word, and a reading the length heuristic independently called
# suspicious would prove the wrong thing.
READING = "SYNTHETIC LIVE READING alpha beta gamma delta epsilon zeta eta theta iota kappa"


perlector = load_stage("4_perlector")
live_calls = perlector.page_run.live_calls


def _perlector_identity():
    return ChairRegistry.from_toml(str(ROOT / "config" / "models.toml")).resolve("perlector")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return "{ " + ", ".join(f'"{k}" = {_toml_value(v)}' for k, v in value.items()) + " }"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _live_row(identity, *, max_num_seqs: int = 1) -> dict[str, Any]:
    """One `kind = "vllm"` row for the fixture roster's own Perlector chair.

    `preflight_state = "proven"` with the two real digests, because the manager
    refuses to launch an unproven row (`_launchable`) and these tests exercise
    the manager the production factory would build, not a relaxed one. The proof
    is a test fixture and lives only in a tmp directory — no catalogue in the
    repository is edited.
    """
    row: dict[str, Any] = {
        "kind": "vllm",
        "recipe": identity.serving_recipe,
        "chair": identity.role,
        "tier": TIER,
        "host": "127.0.0.1",
        "port": 8106,
        "served_model_id": SERVED_MODEL_ID,
        "dtype": "bfloat16",
        "seed": 0,
        "required_packages": {"vllm": "0.test"},
        # The real catalogue's 16,384 for this chair at every tier
        # (`config/serving_recipes_real.toml`), so the stand-in row admits what
        # the real row admits.
        "max_model_len": 16384,
        "max_num_seqs": max_num_seqs,
        "max_num_batched_tokens": 512,
        "gpu_memory_utilization": "0.58",
        "min_pixels": 3136,
        "max_pixels": 1806336,
        # The chair's own vision-encoder geometry, as the shipped real
        # catalogue states it: without it nothing can say what one image costs
        # this chair in prompt tokens, and the request builders refuse by name
        # rather than counting against a default (`common/request_capacity.py`).
        "patch_size": 16,
        "merge_size": 2,
        "enable_prefix_caching": True,
        "enforce_eager": False,
        "trust_remote_code": False,
        "generation_config": "vllm",
        "preflight_state": "proven",
        "startup_timeout_seconds": 3,
        "poll_interval_seconds": 1,
        "request_timeout_seconds": 30,
        "readiness_probe": {
            "kind": "chat-completions",
            "request_json": '{"messages":[{"role":"user","content":"READY"}],"max_tokens":4}',
        },
    }
    row["preflight_identity_digest"] = chair_preflight_identity_digest(identity)
    row["preflight_digest"] = profile_preflight_digest(row)
    return row


def _live_catalogue(destination: Path, *, max_num_seqs: int = 1) -> Path:
    """The committed fixture catalogue with its Perlector rows made live.

    Every other chair keeps its fixture row, so this is exactly the shape
    `serving_mode_for` reads: three names into one catalogue, and the row it
    lands on decides. The committed file is never touched.
    """
    source = (ROOT / "config" / "serving_recipes.toml").read_text(encoding="utf-8")
    marker = '[[profiles]]\nkind = "fixture"\nrecipe = "fake-perlector-v0"'
    head, *perlector_rows = source.split(marker)
    assert len(perlector_rows) == 3, "the fixture catalogue no longer carries three Perlector rows"
    assert "[[profiles]]" not in perlector_rows[-1], (
        "a profile follows the last Perlector row; this helper only writes the "
        "head plus one live Perlector row, so that trailing profile would be "
        "dropped from the rebuilt catalogue"
    )
    row = _live_row(_perlector_identity(), max_num_seqs=max_num_seqs)
    body = "\n".join(f"{key} = {_toml_value(value)}" for key, value in row.items())
    path = destination / "serving_recipes_live_perlector.toml"
    path.write_text(f"{head}[[profiles]]\n{body}\n", encoding="utf-8")
    return path


def _chain_through_attestatores(
    root: Path, catalogue: Path, *, scenario: str = "happy", extra: tuple[str, ...] = ()
) -> None:
    for program in CHAIN_THROUGH_ATTESTATORES:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "r",
                "--scenario",
                scenario,
                "--serving-recipes-config",
                str(catalogue),
                *extra,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"


@pytest.fixture(scope="module")
def chained_run(tmp_path_factory) -> tuple[Path, Path]:
    """One Door-through-Attestatores tree, built once and copied per test.

    The chain is five subprocesses over the same synthetic pages every time; the
    thing under test is what happens *after* it, so it runs once and each test
    takes its own copy to write into.
    """
    base = tmp_path_factory.mktemp("live-perlector")
    catalogue = _live_catalogue(base)
    root = base / "runs"
    _chain_through_attestatores(root, catalogue)
    return root, catalogue


@pytest.fixture()
def live_run(chained_run, tmp_path: Path) -> tuple[Path, Path]:
    template, catalogue = chained_run
    root = tmp_path / "runs"
    shutil.copytree(template, root)
    return root, catalogue


class _TreeBlobs:
    """`FakeEndpoint`'s response-as-arrival probe, pointed at the real run tree.

    The stage retains through `RunTree.put_blob`, not through the fakes' own
    store, so this is the adapter that lets the endpoint assert the previous
    response's exact digest is already on disk before it answers the next
    request.
    """

    def __init__(self, root: Path) -> None:
        self._tree = RunTree(root, "r")

    def has(self, sha256: str) -> bool:
        return self._tree.resolve(self._tree.blob_path(PERLECTOR, sha256)).exists()


def _serving_factory(
    endpoint: FakeEndpoint, catalogue: Path, log_root: Path, lock: Path, *, now=None
):
    """The `(context, chair, tier) -> ChairClient` seam `main` injects against.

    Deliberately close to `stage_chair_client`: the same manager, the
    same real `StageContextReceiptPublisher`, the same `retain_chair_bytes` into
    the stage's own blob area, the same receipt re-read through the tree. Only
    the launcher, the transport and the package inspector are fakes — the three
    things that would otherwise need a card.
    """
    decoding_policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
    recipes = load_serving_recipes(catalogue)

    def factory(context, chair, tier) -> ChairClient:
        manager = ServingManager(
            registry=FakeRegistry({chair.role: chair}, log_root),
            recipes=recipes,
            config_inputs=ServingConfigInputs.from_record(context.serving_config_inputs),
            launcher=FakeLauncher(endpoint),
            http=endpoint,
            receipt_publisher=StageContextReceiptPublisher(context),
            log_root=log_root,
            package_inspector=FakePackages({"vllm": "0.test"}),
            residency_lease=FileResidencyLease(lock),
            now=now,
        )
        return ChairClient(
            manager=manager,
            identity=chair,
            tier=tier,
            retain=lambda data: retain_chair_bytes(context, data),
            decoding_config_sha256=decoding_sha256,
            decoding_policy=decoding_policy,
            read_receipt=context.tree.read_run_receipt,
        )

    return factory


def _run_perlector(
    live_run,
    tmp_path: Path,
    monkeypatch,
    *answers: ScriptedAnswer,
    scenario: str = "happy",
    endpoint_out: list | None = None,
    extra_args: tuple[str, ...] = (),
):
    """Run the real stage in this process against a scripted endpoint.

    `scenario` names only this invocation's own `--scenario`, independent of
    whatever scenario built the run tree ahead of it (always `"happy"` — see
    `_chain_through_attestatores`): `open_context` binds `context.scenario` and
    `context.fixture` from this process's own `args.scenario`
    (`common/stage.py`), not from anything sealed upstream, exactly as it does
    for a real Perlector invocation of a resumed run.
    """
    root, catalogue = live_run
    endpoint = FakeEndpoint(
        served_model_id=SERVED_MODEL_ID,
        blob_store=_TreeBlobs(root),
        assert_retained_before_next_request=True,
    )
    # Padded rather than exactly counted: one call per page the pass reads.
    endpoint.script(*answers, *(answers[-1:] or ()) * 60)
    if endpoint_out is not None:
        endpoint_out.append(endpoint)
    factory = _serving_factory(endpoint, catalogue, tmp_path / "logs", tmp_path / "pod-gpu.lock")
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ROOT / "pipeline" / "4_perlector" / "run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            scenario,
            "--serving-recipes-config",
            str(catalogue),
            "--placement-tier",
            TIER,
            *extra_args,
        ],
    )
    return endpoint, perlector.main(serving_factory=factory)


# --- the selector: the sealed row kind, and nothing else ----------------------


def _mode_arguments(catalogue: Path, tier: str | None):
    placement = ROOT / "config" / "pod_placement.toml"
    context = SimpleNamespace(
        serving_config_inputs={
            "schema": "serving-config-inputs.v2",
            "serving_recipes_sha256": read_sealed_toml(catalogue, "recipes")[1],
            "pod_placement_sha256": read_sealed_toml(placement, "placement")[1],
        }
    )
    args = SimpleNamespace(serving_recipes_config=str(catalogue), placement_tier=tier)
    return context, args


def test_the_committed_fixture_catalogue_resolves_to_the_fixture_reader():
    """The default pair is fixture for every chair, with or without a tier.

    This is what keeps the acceptance pin still: no run that seals the committed
    catalogue can reach a live reader, whatever else is passed on the command
    line.
    """
    context, args = _mode_arguments(ROOT / "config" / "serving_recipes.toml", None)
    identity = _perlector_identity()
    assert perlector.perlector_serving_mode(context, args, identity) == "fixture"
    _, with_tier = _mode_arguments(ROOT / "config" / "serving_recipes.toml", TIER)
    assert perlector.perlector_serving_mode(context, with_tier, identity) == "fixture"


def test_a_live_row_resolves_to_live_only_with_the_measured_tier(chained_run):
    _root, catalogue = chained_run
    identity = _perlector_identity()
    context, args = _mode_arguments(catalogue, TIER)
    assert perlector.perlector_serving_mode(context, args, identity) == "live"
    _, no_tier = _mode_arguments(catalogue, None)
    with pytest.raises(ServingModeRefusal, match="placement-tier"):
        perlector.perlector_serving_mode(context, no_tier, identity)


def test_a_catalogue_that_is_not_the_sealed_one_is_refused(chained_run, tmp_path: Path):
    """The row kind decides the posture, so it is read from sealed bytes only.

    Without this the selector could be moved by pointing `--serving-recipes-config`
    at another file after the run was bound — the run authority would still say
    fixture while a chair was being started.
    """
    _root, catalogue = chained_run
    substitute = tmp_path / "substituted.toml"
    moved = Path(catalogue).read_bytes().replace(b"offline walking-skeleton", b"moved", 1)
    assert moved != Path(catalogue).read_bytes()
    substitute.write_bytes(moved)
    context, args = _mode_arguments(catalogue, TIER)
    args.serving_recipes_config = str(substitute)
    with pytest.raises(
        perlector.ContractError,
        match=r"refused for .*substituted\.toml .*rerun with the files this run sealed",
    ):
        perlector.perlector_serving_mode(context, args, _perlector_identity())


def test_a_placement_table_that_is_not_the_sealed_one_is_refused(chained_run):
    """The Perlector proves the placement bytes too, as the other serving stages do."""
    _root, catalogue = chained_run
    context, args = _mode_arguments(catalogue, TIER)
    context.serving_config_inputs["pod_placement_sha256"] = "1" * 64
    with pytest.raises(perlector.ContractError, match="pod placement differs from the run-sealed"):
        perlector.perlector_serving_mode(context, args, _perlector_identity())


def test_an_absent_chair_resolves_to_fixture_without_consulting_the_catalogue(
    absent_third_chair_config,
):
    """An absence has no identity to look a row up by, so none is looked up."""
    absent = ChairRegistry.from_toml(str(absent_third_chair_config)).resolve("attestator_3")
    context = SimpleNamespace(serving_config_inputs=None)
    args = SimpleNamespace(serving_recipes_config="/nonexistent.toml", placement_tier=None)
    assert perlector.perlector_serving_mode(context, args, absent) == "fixture"


# --- a live pass, and what its record carries ---------------------------------


@pytest.mark.parametrize("minutes,refused", [(1, True), (60, False)])
def test_a_launch_the_reading_deadline_cannot_cover_is_refused_before_the_chair_starts(
    live_run, tmp_path, monkeypatch, minutes, refused
):
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    answer = ScriptedAnswer(content=READING, finish_reason="stop")
    endpoints: list = []
    if not refused:
        _endpoint, exit_code = _run_perlector(
            live_run, tmp_path, monkeypatch, answer, extra_args=("--reading-deadline", deadline)
        )
        assert exit_code == 0
        return
    with pytest.raises(ContractError, match="starting the Perlector"):
        _run_perlector(
            live_run,
            tmp_path,
            monkeypatch,
            answer,
            endpoint_out=endpoints,
            extra_args=("--reading-deadline", deadline),
        )
    assert endpoints[0].requests == []


def test_the_chair_starts_while_pages_are_prepared_and_is_up_before_the_first_send(
    live_run, tmp_path, monkeypatch
):
    """The first page's preparation waits for the chair's start to begin: it can only
    finish because the start runs beside it, not after it."""
    page_run = perlector.page_run
    began = threading.Event()
    started_on: list[str] = []
    start_chair = live_calls.start_chair

    def start(run):
        started_on.append(threading.current_thread().name)
        began.set()
        start_chair(run)

    prepare = page_run._prepare

    def prepared(state, ordinal, page_id):
        assert began.wait(timeout=10), "the chair did not start while pages were prepared"
        return prepare(state, ordinal, page_id)

    monkeypatch.setattr(live_calls, "start_chair", start)
    monkeypatch.setattr(page_run, "_prepare", prepared)
    endpoint, exit_code = _run_perlector(
        live_run, tmp_path, monkeypatch, ScriptedAnswer(content=READING, finish_reason="stop")
    )
    assert exit_code == 0
    assert started_on == ["chair-start"]
    assert endpoint.requests


def test_a_chair_that_fails_to_start_in_the_background_stops_the_pass_on_the_main_thread(
    live_run, tmp_path, monkeypatch
):
    def refuse(run):
        raise ContractError("the engine would not load")

    monkeypatch.setattr(live_calls, "start_chair", refuse)
    with pytest.raises(ContractError, match="the engine would not load"):
        _run_perlector(
            live_run, tmp_path, monkeypatch, ScriptedAnswer(content=READING, finish_reason="stop")
        )


def test_a_live_pass_notes_its_launch_audit_beside_its_engine_logs(live_run, tmp_path, monkeypatch):
    """The orchestrator finds the launch by this note, without reading the stage's blobs."""
    from common.contracts.serving import SERVING_LAUNCH_AUDIT_SCHEMA
    from common.runtree.store import LAUNCH_AUDIT_NOTE_PREFIX

    _endpoint, exit_code = _run_perlector(
        live_run, tmp_path, monkeypatch, ScriptedAnswer(content=READING, finish_reason="stop")
    )
    assert exit_code == 0
    stage = live_run[0] / "r" / "4_perlector"
    [note] = [
        path
        for path in (stage / SERVING_LOGS_DIR).iterdir()
        if path.name.startswith(LAUNCH_AUDIT_NOTE_PREFIX)
    ]
    digest = note.name.removeprefix(LAUNCH_AUDIT_NOTE_PREFIX)
    audit = json.loads((stage / "blobs" / "sha256" / digest).read_bytes())
    assert audit["schema"] == SERVING_LAUNCH_AUDIT_SCHEMA and audit["chair"] == "perlector"


def test_a_failed_preparation_keeps_its_error_and_notes_a_failed_start(monkeypatch):
    def refuse(run):
        raise RuntimeError("no card")

    monkeypatch.setattr(live_calls, "start_chair", refuse)
    starting = live_calls.BackgroundStart(SimpleNamespace())
    starting._thread.join(timeout=10)
    raised = ContractError("a page could not be prepared")
    starting.note_failure(raised)
    assert "no card" in raised.__notes__[0]
    starting.join()  # the failure was reported once, on the error that propagated


class _SlowClient:
    """A chair whose start blocks until released, and which records its stop."""

    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate
        self.stopped = threading.Event()

    def start(self, run) -> None:
        run.service.client = self
        assert self.gate.wait(timeout=10), "the test never released the start"

    def __exit__(self, *exc) -> None:
        self.stopped.set()


def test_closing_during_a_slow_start_returns_at_once_and_the_start_stops_the_chair(
    monkeypatch,
):
    gate = threading.Event()
    client = _SlowClient(gate)
    monkeypatch.setattr(live_calls, "start_chair", client.start)
    run = SimpleNamespace(service=perlector.ResidentChair())
    run.service.starting = starting = live_calls.BackgroundStart(run)
    try:
        began = time.monotonic()
        run.service.close()
        assert time.monotonic() - began < 1
        assert not starting.done and not client.stopped.is_set()
    finally:
        gate.set()
    starting._thread.join(timeout=10)
    assert client.stopped.is_set() and run.service.client is None


def test_an_interrupt_while_pages_are_prepared_does_not_wait_for_the_chair_to_load(
    live_run, tmp_path, monkeypatch
):
    """The pass stops at once; the chair, still loading, is stopped by its start's thread
    as soon as the load returns."""
    gate = threading.Event()
    client = _SlowClient(gate)

    def interrupted(state, ordinal, page_id):
        raise KeyboardInterrupt

    monkeypatch.setattr(live_calls, "start_chair", client.start)
    monkeypatch.setattr(perlector.page_run, "_prepare", interrupted)
    try:
        began = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            _run_perlector(
                live_run,
                tmp_path,
                monkeypatch,
                ScriptedAnswer(content=READING, finish_reason="stop"),
            )
        assert time.monotonic() - began < 5
        assert not client.stopped.is_set()
    finally:
        gate.set()
    [thread] = [thread for thread in threading.enumerate() if thread.name == "chair-start"]
    thread.join(timeout=10)
    assert client.stopped.is_set()


def test_a_typed_transport_failure_preserves_unknown_completion_call_evidence():
    """A dispatched request's uncertain outcome keeps its closed call evidence."""
    call_ref = {
        "relative_path": "r/operations/serving/calls/transport.json",
        "sha256": "a" * 64,
    }
    receipt_ref = {
        "relative_path": "r/operations/serving/receipts/receipt.json",
        "sha256": "b" * 64,
    }
    error = ChairTransportFailure(
        "timed out after request dispatch",
        call_record_ref=call_ref,
        request_sha256="c" * 64,
        receipt_ref=receipt_ref,
        served_model_id="perlector-under-test",
    )

    phase = perlector.page_run.PAGE_READING_PASS
    assert live_calls.failure_record(error, phase=phase) == {
        "phase": phase,
        "kind": "transport",
        "code": "CHAIR_TRANSPORT_FAILURE",
        "detail": "timed out after request dispatch",
        "raw_response_ref": None,
        "call_record_ref": call_ref,
        "request_sha256": "c" * 64,
        "receipt_ref": receipt_ref,
        "served_model_id": "perlector-under-test",
        "response_completion": "unknown",
    }


# --- the refusals this wiring adds --------------------------------------------


def test_an_outcome_that_attempted_no_reading_cannot_carry_a_receipt():
    """A page not asked and an absent chair name what would have read and stop there."""
    with pytest.raises(SchemaRefusal, match="attempted no reading"):
        perlector.page_run.provenance_for(
            SimpleNamespace(),
            _perlector_identity(),
            attempted=False,
            receipt_ref={"relative_path": "receipts/sha256/x.json", "sha256": "a" * 64},
        )


def test_an_absent_chair_that_attempted_a_reading_cannot_carry_a_receipt(
    absent_third_chair_config,
):
    """An absent chair served nothing, so a receipt reference names a serving
    moment it never had -- the mirror of the not-attempted guard above, for
    the other reading that never happened."""
    absent = ChairRegistry.from_toml(str(absent_third_chair_config)).resolve("attestator_3")
    with pytest.raises(SchemaRefusal, match="absent"):
        perlector.page_run.provenance_for(
            SimpleNamespace(),
            absent,
            attempted=True,
            receipt_ref={"relative_path": "receipts/sha256/x.json", "sha256": "a" * 64},
        )


def test_retaining_a_chair_response_after_the_seal_is_refused():
    """The stage's blob inventory is what its completion seal witnessed."""
    context = SimpleNamespace(sealed=True, stage=PERLECTOR)
    context.retain = partial(StageContext.retain, context)
    with pytest.raises(SchemaRefusal, match="storing a chair response afterwards"):
        retain_chair_bytes(context, b"{}")


def test_two_digests_for_one_input_path_are_refused():
    """Content addressing makes this impossible, so it is a rewritten blob."""
    first = {"relative_path": "4_perlector/blobs/sha256/aa", "sha256": "a" * 64}
    second = {"relative_path": "4_perlector/blobs/sha256/aa", "sha256": "b" * 64}
    assert distinct_refs([first, first]) == [first]
    with pytest.raises(SchemaRefusal, match="two different digests"):
        distinct_refs([first, second])


def _engine_call_world(tree, *, seed: int, schema: str = live_calls.CHAIR_CALL_RECORD_SCHEMA):
    """A retained Perlector call record at its sealed row, and a context that reads it.

    The serving receipt's seed is 7.
    """
    from common.decoding import (
        DEFAULT_DECODING_CONFIG_PATH,
        chair_decoding,
        engine_effective_sampling,
        load_decoding_policy,
        recorded_wire_decimals,
    )

    policy, _digest = load_decoding_policy()
    sampling = chair_decoding(policy, "perlector")
    receipt_ref = {"relative_path": "receipts/sha256/r.json", "sha256": "a" * 64}
    call = {
        "schema": schema,
        "receipt_ref": receipt_ref,
        "generation_sent": {**recorded_wire_decimals(sampling), "max_tokens": 10, "seed": seed},
        "sampling_effective": recorded_wire_decimals(engine_effective_sampling(sampling)),
    }
    _digest, raw = tree.put_blob(PERLECTOR, b"a retained response")
    _digest, retained_call = tree.put_blob(PERLECTOR, json.dumps(call).encode())
    context = SimpleNamespace(
        input_ref=lambda relative_path: {
            "relative_path": relative_path,
            "sha256": digest_bytes(tree.read_bytes(relative_path)),
        },
        tree=SimpleNamespace(
            read_bytes=tree.read_bytes,
            read_run_receipt=lambda reference: {"seed": 7} if reference == receipt_ref else {},
        ),
        args=SimpleNamespace(decoding_config=DEFAULT_DECODING_CONFIG_PATH),
        require_sealed_config=lambda _name, _digest: None,
    )
    raw_ref = {"relative_path": raw.relative_path, "sha256": digest_bytes(b"a retained response")}
    call_ref = context.input_ref(retained_call.relative_path)
    engine_call = {
        "raw_response_ref": raw_ref,
        "call_record_ref": call_ref,
        "response_sha256": raw_ref["sha256"],
        "finish_reason": "stop",
        "served_model_id": SERVED_MODEL_ID,
    }
    return context, engine_call


def test_an_engine_call_naming_bytes_that_moved_is_refused(live_run):
    """A record whose response reference resolves to nothing reads as evidence."""
    root, _catalogue = live_run
    context, full_call = _engine_call_world(RunTree(root, "r"), seed=7)
    honest = [full_call["raw_response_ref"], full_call["call_record_ref"]]
    assert live_calls.engine_call_inputs(context, full_call) == honest
    lying = {"relative_path": full_call["raw_response_ref"]["relative_path"], "sha256": "c" * 64}
    with pytest.raises(SchemaRefusal, match="retained bytes at that path"):
        live_calls.engine_call_inputs(
            context,
            {**full_call, "raw_response_ref": lying, "response_sha256": lying["sha256"]},
        )


def test_an_engine_call_is_held_to_the_receipts_sealed_seed(live_run):
    """A page reading sends the receipt's seed; a call under any other seed is
    refused where the reading binds it."""
    root, _catalogue = live_run
    arm_seed = 8
    tree = RunTree(root, "r")
    context, at_receipt_seed = _engine_call_world(tree, seed=7)
    live_calls.engine_call_inputs(context, at_receipt_seed)
    _context, at_arm_seed = _engine_call_world(tree, seed=arm_seed)
    with pytest.raises(SchemaRefusal, match=f"sent seed {arm_seed}, not 7"):
        live_calls.engine_call_inputs(context, at_arm_seed)


def test_an_engine_call_off_its_sealed_row_or_retired_is_refused(live_run):
    root, _catalogue = live_run
    tree = RunTree(root, "r")
    context, retired = _engine_call_world(tree, seed=7, schema="chair-call-record.v2")
    with pytest.raises(
        SchemaRefusal, match="has schema .chair-call-record.v2., not one this build writes"
    ):
        live_calls.engine_call_inputs(context, retired)


def test_an_engine_call_with_the_wrong_shape_is_refused_by_name():
    """`engine_call_inputs` is the one publication path with no closed schema
    until this refusal: every other field's shape is checked, and a live
    reading's `engine_call` should not be the one exception."""
    with pytest.raises(SchemaRefusal, match="wrong shape"):
        live_calls.engine_call_inputs(SimpleNamespace(), {"raw_response_ref": {}})


def test_an_engine_call_with_two_digests_for_one_response_is_refused():
    """`response_sha256` and `raw_response_ref["sha256"]` must never disagree --
    two digests for one response is exactly the ambiguity a content-addressed
    store is supposed to make impossible."""
    ref = {"relative_path": "4_perlector/blobs/sha256/aa", "sha256": "a" * 64}
    engine_call = {
        "raw_response_ref": ref,
        "call_record_ref": ref,
        "response_sha256": "b" * 64,
        "finish_reason": "stop",
        "served_model_id": SERVED_MODEL_ID,
    }
    with pytest.raises(SchemaRefusal, match="two different digests"):
        live_calls.engine_call_inputs(SimpleNamespace(), engine_call)


# --- the shutdown-before-seal ordering, and the production factory ------------


def test_a_failed_chair_shutdown_stops_the_pass_before_the_seal_is_written(
    live_run, tmp_path, monkeypatch
):
    """The chair is stopped before the seal. The shutdown itself fails here, and the
    seal must never be reached: had `close()` run after the seal, the failure would
    be swallowed by `main`'s own `finally` or reported over an already-sealed stage."""
    root, catalogue = live_run
    endpoint = FakeEndpoint(
        served_model_id=SERVED_MODEL_ID,
        blob_store=_TreeBlobs(root),
        assert_retained_before_next_request=True,
    )
    answer = ScriptedAnswer(content=READING, finish_reason="stop")
    endpoint.script(answer, *([answer] * 60))
    inner_factory = _serving_factory(
        endpoint, catalogue, tmp_path / "logs", tmp_path / "pod-gpu.lock"
    )

    class _ExitFails:
        """Wraps the real client so shutdown itself fails, after really
        shutting down -- proving the ordering, not merely leaking a process."""

        def __init__(self, client: ChairClient) -> None:
            self._client = client

        def __enter__(self):
            self._client.__enter__()
            return self

        def __exit__(self, *exc: object) -> None:
            self._client.__exit__(*exc)
            raise ServiceStopError("simulated shutdown verification failure")

        def __getattr__(self, name):
            return getattr(self._client, name)

    def failing_factory(context, chair, tier):
        return _ExitFails(inner_factory(context, chair, tier))

    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ROOT / "pipeline" / "4_perlector" / "run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
            "--serving-recipes-config",
            str(catalogue),
            "--placement-tier",
            TIER,
        ],
    )
    with pytest.raises(ServiceStopError, match="simulated shutdown"):
        perlector.main(serving_factory=failing_factory)
    seal_dir = root / "r" / "4_perlector" / "artifacts" / "stage-seal"
    assert not seal_dir.exists() or not any(seal_dir.iterdir()), (
        "the completion boundary must never be written over a chair whose "
        "shutdown could not be verified"
    )


def test_stage_chair_client_logs_under_the_run_tree_and_leases_off_it(live_run, monkeypatch):
    """`stage_chair_client` is the only path a real run takes, and nothing
    else in this suite ever constructs it -- the injected `_serving_factory`
    above deliberately diverges on the two things production alone decides:
    where the serving log directory and the pod-GPU residency lease live.
    Constructing the client starts nothing (`ChairClient.__init__` only stores
    its manager), so this proves both locations, and the manager keyword set
    that builds them, without starting a service or needing a card.

    **The two locations are deliberately not the same.** The logs belong to the
    run and travel with it. The lease belongs to the *card*, which belongs to
    the pod: a lease resolved inside a run tree would let two stages resumed
    under different run ids each acquire their own and co-reside on one GPU,
    never meet the pod preflight's own lock at all, and put an advisory lock on
    a network mount that is not known to honour one."""
    root, catalogue = live_run
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ROOT / "pipeline" / "4_perlector" / "run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
            "--serving-recipes-config",
            str(catalogue),
            "--placement-tier",
            TIER,
        ],
    )
    args = perlector.stage_parser(perlector.DESCRIPTION).parse_args()
    context = perlector.open_stage_context(
        args, PERLECTOR, registry_factory=ChairRegistry.from_toml
    )
    decoding_policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
    client = stage_chair_client(
        context,
        _perlector_identity(),
        TIER,
        decoding_policy=decoding_policy,
        decoding_config_sha256=decoding_sha256,
    )
    tree_root = context.tree.root
    assert client._manager.log_root.is_relative_to(tree_root)
    assert client._manager.log_root.name == SERVING_LOGS_DIR
    assert client._manager.residency_lease.path == POD_RESIDENCY_LOCK_PATH
    assert not client._manager.residency_lease.path.is_relative_to(tree_root)
    # Neither write disturbs the witnessed inventory (`build_manifest` walks
    # only `<stage>/artifacts`, the blob inventory only `<stage>/blobs`), so the
    # seal must still succeed with these paths named but nothing started.
    context.seal_boundary()


# --- two consumer-side rules only a served page witness reaches ---------------
#
# The fixture posture declares no geometry on a continuation page, and a fixture
# page record's partition and its capture never name one blob twice. A served
# Chandra reaches both (`pipeline/3_attestatores/CONTRACT.md`).


def _page_context(root: Path, catalogue: Path, monkeypatch):
    """A real Perlector stage context over a chained run tree."""
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ROOT / "pipeline" / "4_perlector" / "run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
            "--serving-recipes-config",
            str(catalogue),
            "--placement-tier",
            TIER,
        ],
    )
    args = perlector.stage_parser(perlector.DESCRIPTION).parse_args()
    return perlector.open_stage_context(args, PERLECTOR, registry_factory=ChairRegistry.from_toml)


def test_one_retained_response_named_by_both_halves_of_a_page_record_is_one_input(
    live_run, monkeypatch
):
    """A blob a page record reaches twice is one response, not two.

    A page witness whose partition was derived from the very bytes its own
    native capture describes names one content-addressed blob through
    `raw_response_refs` and again through `native_capture` -- and a page-edge
    overshoot finding is *required* by the shared contract to be traceable
    through `raw_response_refs` while the capture still names it
    (`common/native_witness.py`). The producer names it once
    (`pipeline/3_attestatores/retained.py::named_once`), and the envelope refuses a
    repeated path outright, so no publishable record could ever have carried
    two entries. Concatenating the two fields here without de-duplication
    therefore built an expectation nothing could satisfy: a correct record,
    correctly published, refused one stage later.
    """
    root, catalogue = live_run
    context = _page_context(root, catalogue, monkeypatch)
    pages = [
        context.tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
    ]
    record = next(
        (page for page in pages if page["payload"].get("native_capture") is not None), None
    )
    assert record is not None, "no page Testimonium in this tree retains a native capture"
    validate_page_testimonium_record(context, record)

    reference = record["payload"]["native_capture"]["raw_response_ref"]
    assert reference in record["inputs"]
    both = copy.deepcopy(record)
    both["payload"]["raw_response_refs"] = [dict(reference)]
    both["self_hash"] = self_hash(both)
    # `inputs` is untouched: it is what the producer would have written, and
    # the point is that this record needs no second entry to be honest.
    validate_page_testimonium_record(context, both)

    # The rule did not go soft. An input the record does not derive from is
    # still refused, and so is one retained response left unbound.
    foreign = next(
        page["payload"]["presented"]["image_path"]
        for page in pages
        if page["payload"]["presented"]
        and page["payload"]["presented"]["image_path"]
        != record["payload"]["presented"]["image_path"]
    )
    extra = copy.deepcopy(both)
    extra["inputs"] = sorted(
        [*extra["inputs"], context.input_ref(foreign)],
        key=lambda item: (item["relative_path"], item["sha256"]),
    )
    with pytest.raises(SchemaRefusal, match="does not bind exactly its presented image"):
        validate_page_testimonium_record(context, extra)
    unbound = copy.deepcopy(both)
    unbound["inputs"] = [item for item in unbound["inputs"] if item != reference]
    with pytest.raises(SchemaRefusal, match="every retained raw response"):
        validate_page_testimonium_record(context, unbound)


# --- concurrent reader calls ---------------------------------------------------


def test_a_fixture_pass_reads_one_page_at_a_time():
    """Only a live engine batches; a fixture pass has no bound to look up."""
    args = SimpleNamespace(perlector_concurrency=4)
    assert perlector._reading_concurrency(SimpleNamespace(), args, None, "fixture") == 1


def test_a_live_pass_reads_as_wide_as_its_chair_is_launched(monkeypatch):
    """The width is the launched row's `max_num_seqs`, which a capacity plan widens;
    `--perlector-concurrency` only ever narrows it."""
    monkeypatch.setattr(perlector, "launch_row", lambda *_args: SimpleNamespace(max_num_seqs=7))
    for asked, width in ((None, 7), (3, 3), (10, 7)):
        args = SimpleNamespace(perlector_concurrency=asked, placement_tier="generic-80gb-plus")
        assert perlector._reading_concurrency(SimpleNamespace(), args, None, "live") == width


def test_the_window_finishes_in_order_within_its_bound():
    lock = threading.Lock()
    in_flight = most = 0
    finished: list[int] = []

    def call(index: int) -> int:
        nonlocal in_flight, most
        with lock:
            in_flight += 1
            most = max(most, in_flight)
        time.sleep(0.05 * (5 - index))  # later jobs finish first
        with lock:
            in_flight -= 1
        return index

    jobs = ((partial(call, index), finished.append) for index in range(5))
    live_calls.in_order_window(3, jobs)
    assert finished == [0, 1, 2, 3, 4]
    assert most == 3


def test_the_window_never_has_more_than_width_calls_in_flight():
    lock = threading.Lock()
    in_flight = most = 0
    order: list[str] = []

    def call() -> None:
        nonlocal in_flight, most
        with lock:
            in_flight += 1
            most = max(most, in_flight)
        time.sleep(0.02)
        with lock:
            in_flight -= 1

    def jobs():
        for index in range(6):
            yield (None if index % 3 == 0 else call), partial(finish, str(index))

    def finish(name: str, _result) -> None:
        order.append(name)

    live_calls.in_order_window(2, jobs())
    assert most == 2
    assert order == [str(index) for index in range(6)]


def test_a_slow_head_does_not_stop_later_jobs_being_sent():
    """Answered jobs wait behind the head; the free slots keep sending."""
    later_sent = threading.Event()
    sent: list[int] = []
    finished: list[int] = []
    drawn_on: set[str] = set()

    def call(index: int) -> int:
        sent.append(index)
        if index == 0:
            assert later_sent.wait(timeout=5)
        elif index == 4:
            later_sent.set()
        return index

    def jobs():
        for index in range(6):
            drawn_on.add(threading.current_thread().name)
            yield partial(call, index), finished.append

    live_calls.in_order_window(2, jobs())
    # Job 0 answers only once job 4 has been sent: a blocked window would time out.
    assert later_sent.is_set()
    assert sorted(sent) == list(range(6))
    assert finished == list(range(6))
    assert drawn_on == {threading.main_thread().name}


def test_answered_jobs_held_behind_a_slow_head_are_bounded():
    width = 2
    release = threading.Event()
    drawn = unfinished = most = 0

    def call(index: int) -> int:
        if index == 0:
            assert release.wait(timeout=5)
        return index

    def jobs():
        nonlocal drawn, unfinished, most
        for index in range(40):
            drawn += 1
            unfinished += 1
            most = max(most, unfinished)
            yield partial(call, index), finish

    def finish(_result) -> None:
        nonlocal unfinished
        unfinished -= 1

    timer = threading.Timer(0.3, release.set)
    timer.start()
    try:
        live_calls.in_order_window(width, jobs())
    finally:
        timer.cancel()
    assert drawn == 40
    assert most <= width + HELD_PER_SLOT * width
    assert most > width


def test_a_call_that_raises_re_raises_at_its_place_after_every_sent_job_is_finished():
    """Nothing is lost: earlier and later sent jobs are still finished, in order."""
    finished: list[int] = []
    started: list[int] = []

    def call(index: int) -> int:
        started.append(index)
        time.sleep(0.02)
        if index == 1:
            raise RuntimeError("not act-local")
        if index == 2:
            raise ValueError("a second failure")
        return index

    def jobs():
        for index in range(5):
            yield partial(call, index), finished.append

    with pytest.raises(RuntimeError, match="not act-local") as raised:
        live_calls.in_order_window(3, jobs())
    # Job 1's failure stops the drawing; job 0 finished before it, and every job
    # already sent after it was finished too rather than dropped.
    assert finished == [0] + [index for index in sorted(started) if index > 2]
    assert any("a second failure" in note for note in raised.value.__notes__)
    assert 2 in started


def test_a_refused_job_source_still_finishes_every_job_already_sent():
    finished: list[int] = []

    def jobs():
        yield partial(time.sleep, 0.05), lambda _r: finished.append(0)
        yield partial(time.sleep, 0.01), lambda _r: finished.append(1)
        raise ContractError("refused while preparing the third")

    with pytest.raises(ContractError, match="third"):
        live_calls.in_order_window(3, jobs())
    assert finished == [0, 1]


# --- resume never asks again about a reply it has on record -------------------


def test_a_reply_another_record_binds_answers_no_send():
    """Two page readings sharing one call's attribution key.

    A reply that some record binds is on record, so it never refuses another
    unanswered send with the same key; an unbound one does.
    """
    receipt = {"relative_path": "receipts/r.json", "sha256": "0" * 64}
    images = ["1" * 64]
    call = {
        "schema": live_calls.CHAIR_CALL_RECORD_SCHEMA,
        "receipt_ref": receipt,
        "image_sha256s": images,
        "raw_response_ref": {"relative_path": "4_perlector/blobs/raw", "sha256": "2" * 64},
    }
    blobs = {"call": json.dumps(call).encode(), "raw": b"engine bytes"}

    def context(bound: list[str]):
        record = {"inputs": [{"relative_path": path} for path in bound]}
        tree = SimpleNamespace(
            build_manifest=lambda _stage: {
                "artifacts": [{"kind": "page-reading", "artifact_id": "b"}],
                "blobs": list(blobs),
            },
            read_artifact=lambda _stage, _kind, _identifier: record,
            blob_path=lambda _stage, name: f"4_perlector/blobs/{name}",
            read_bytes=lambda path: blobs[path.rsplit("/", 1)[1]],
        )
        return SimpleNamespace(tree=tree)

    send = [{"payload": {"receipt_ref": receipt, "image_sha256s": images}}]
    paths = ["4_perlector/blobs/call", "4_perlector/blobs/raw"]
    calls, unattributed = live_calls.unrecorded_replies(context(paths))
    assert (calls, unattributed) == ([], False)
    assert not live_calls.answers_a_send(calls, send)
    calls, unattributed = live_calls.unrecorded_replies(context([]))
    assert not unattributed and live_calls.answers_a_send(calls, send)
    # Raw bytes with no call record naming them cannot be attributed to any send.
    del blobs["call"]
    assert live_calls.unrecorded_replies(context([])) == ([], True)
    # A blob of any other schema is no call record, so it too may be an unattributed reply.
    # The raw bytes are bound here, so only the unknown-schema blob can count.
    blobs["call"] = json.dumps({**call, "schema": "chair-call-record.v2"}).encode()
    assert live_calls.unrecorded_replies(context(["4_perlector/blobs/raw"])) == ([], True)


# --- page types and entry kinds, live ----------------------------------------------


def _artifacts(root: Path, kind: str) -> list[dict[str, Any]]:
    directory = root / "r" / "4_perlector" / "artifacts" / kind
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))
    ]


def test_a_live_answer_naming_its_page_type_flows_into_every_page_record(
    live_run, tmp_path, monkeypatch
):
    entry = {
        "label": None,
        "cites": [],
        "continues_from_previous_page": False,
        "continues_to_next_page": False,
    }
    answer = {
        "page_type": "index",
        "writing": "typed",
        "entries": [
            {**entry, "n": 1, "kind": "index-row", "text": "114 21 Guyotte, Charles"},
            {**entry, "n": 2, "kind": "act", "text": "Le deux mai a été inhumé Jean Roy"},
        ],
        "set_aside": [],
    }
    endpoint, exit_code = _run_perlector(
        live_run,
        tmp_path,
        monkeypatch,
        ScriptedAnswer(content=json.dumps(answer, ensure_ascii=False), finish_reason="stop"),
    )
    assert exit_code == 0
    root = live_run[0]
    # This catalogue's recipe sends no instruction; the request is the feed alone.
    assert endpoint.requests
    readings = [r["payload"] for r in _artifacts(root, "page-reading")]
    assert readings and all(r["parse_state"] == "parsed" for r in readings)
    assert all(r["disposition"] == "read" for r in readings)
    # First readings only; a page's re-ask, answered by the same script, adds its own.
    perlectios = [
        r["payload"] for r in _artifacts(root, "perlectio") if "reading_attempt" not in r["payload"]
    ]
    assert perlectios
    assert {(p["n"], p["kind"], p["entry_kind"]) for p in perlectios} == {
        (1, "other", "index-row"),
        (2, "act", "act"),
    }
    for record in _artifacts(root, "page-accounting"):
        typed = record["payload"]["page_type"]
        assert (typed["stated"], typed["writing"]) == ("index", "typed")
        assert typed["applicability"]["i"]["applies"] is False
        assert typed["kinds"] == {"agrees": False, "unexpected_kinds": ["act"]}
        kinds = [e["entry_kind"] for e in record["payload"]["entries"]]
        assert kinds[:2] == ["index-row", "act"]
