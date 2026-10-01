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
from operations.serving.errors import ServiceStopError
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
        # The 16,384 the shipped real catalogue states for this chair at every
        # tier (`config/serving_recipes_real.toml`). It was 2,048 while the
        # reader admitted on a prompt *floor* of 790; the seam now admits on the
        # measured upper bound, and this suite's own four-image dossier costs
        # 1,732 prompt tokens by it -- 2,080 with the images and the reserve,
        # which 2,048 cannot hold. Raising the stand-in row toward the row it
        # stands in for is the same disposition the capacity unit took for the
        # real catalogue: raise the context, never shrink what the chair is
        # shown.
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
        "enable_tower_connector_lora": False,
        "max_lora_rank": 64,
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


def test_a_typed_transport_failure_preserves_unknown_completion_call_evidence():
    """A dispatched request's uncertain outcome keeps its closed call evidence."""
    failure_type = getattr(perlector.serving_errors, "ChairTransportFailure", None)
    assert failure_type is not None, "the serving transport-failure contract is required"
    call_ref = {
        "relative_path": "r/operations/serving/calls/transport.json",
        "sha256": "a" * 64,
    }
    receipt_ref = {
        "relative_path": "r/operations/serving/receipts/receipt.json",
        "sha256": "b" * 64,
    }
    error = failure_type(
        "timed out after request dispatch",
        call_record_ref=call_ref,
        request_sha256="c" * 64,
        receipt_ref=receipt_ref,
        served_model_id="perlector-under-test",
    )

    assert perlector._failure_record(error, phase="audit-reproof") == {
        "phase": "audit-reproof",
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
        perlector.provenance_for(
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
        perlector.provenance_for(
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


def _engine_call_world(tree, *, seed: int, schema: str = "chair-call-record.v3"):
    """A retained Perlector call record at its sealed row, and a context that reads it.

    The serving receipt's seed is 7; the variance arms' are the sealed policy's.
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
    assert perlector.engine_call_inputs(context, full_call) == honest
    lying = {"relative_path": full_call["raw_response_ref"]["relative_path"], "sha256": "c" * 64}
    with pytest.raises(SchemaRefusal, match="retained bytes at that path"):
        perlector.engine_call_inputs(
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
    perlector.engine_call_inputs(context, at_receipt_seed)
    _context, at_arm_seed = _engine_call_world(tree, seed=arm_seed)
    with pytest.raises(SchemaRefusal, match=f"sent seed {arm_seed}, not 7"):
        perlector.engine_call_inputs(context, at_arm_seed)


def test_an_engine_call_off_its_sealed_row_or_retired_is_refused(live_run):
    root, _catalogue = live_run
    tree = RunTree(root, "r")
    context, retired = _engine_call_world(tree, seed=7, schema="chair-call-record.v2")
    with pytest.raises(SchemaRefusal, match="written as chair-call-record.v2"):
        perlector.engine_call_inputs(context, retired)


def test_an_engine_call_with_the_wrong_shape_is_refused_by_name():
    """`engine_call_inputs` is the one publication path with no closed schema
    until this refusal: every other field's shape is checked, and a live
    reading's `engine_call` should not be the one exception."""
    with pytest.raises(SchemaRefusal, match="wrong shape"):
        perlector.engine_call_inputs(SimpleNamespace(), {"raw_response_ref": {}})


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
        perlector.engine_call_inputs(SimpleNamespace(), engine_call)


# --- the shutdown-before-seal ordering, and the production factory ------------


def test_a_failed_chair_shutdown_stops_the_pass_before_the_seal_is_written(
    live_run, tmp_path, monkeypatch
):
    """CONTRACT.md: 'One chair, started late, stopped before the seal.' A
    mutation probe deleting `service.close()` ahead of `context.seal_boundary()`
    left the rest of this module green, so nothing else here pins the ordering.
    This makes the shutdown itself fail and checks the seal was never reached:
    if `close()` ran *after* the seal, the failure would either be swallowed by
    `main`'s own `finally` or reported over an already-sealed stage."""
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

    **The two locations are deliberately not the same, and this used to pin the
    lease under the run tree.** The logs belong to the run and travel with it.
    The lease belongs to the *card*, which belongs to the pod: a lease resolved
    inside a run tree let two stages resumed under different run ids each
    acquire their own and co-reside on one GPU, never met the pod preflight's
    own lock at all, and put an advisory lock on a network mount that is not
    known to honour one."""
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


# --- the two consumer-side rules a served page witness reached first ----------
#
# Both were unreachable while no page witness parsed live: the fixture posture
# declares no geometry on a continuation page, and a fixture page record's
# partition and its capture never name one blob twice. A served Chandra reaches
# both (`pipeline/3_attestatores/CONTRACT.md`), which is why they are fixed here
# rather than left described.


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
    (`pipeline/3_attestatores/run.py::_named_once`), and the envelope refuses a
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
    perlector._in_order_window(3, jobs)
    assert finished == [0, 1, 2, 3, 4]
    assert most == 3


def test_the_window_never_holds_more_than_width_unfinished_jobs():
    unfinished = most = 0
    order: list[str] = []

    def jobs():
        nonlocal unfinished, most
        for index in range(6):
            unfinished += 1
            most = max(most, unfinished)
            call = None if index % 3 == 0 else partial(time.sleep, 0.02)
            yield call, partial(finish, str(index))

    def finish(name: str, _result) -> None:
        nonlocal unfinished
        unfinished -= 1
        order.append(name)

    perlector._in_order_window(2, jobs())
    assert most == 2
    assert order == [str(index) for index in range(6)]


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
        perlector._in_order_window(3, jobs())
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
        perlector._in_order_window(3, jobs())
    assert finished == [0, 1]


# --- resume never asks again about a reply it has on record -------------------

# Each act by its prompt's own act line; a witness's text also reaches the other
# act's request as a neighbour clue.
ACT_ONE, ACT_TWO = b"act: a1", b"act: a2"


def test_a_reply_another_record_binds_answers_no_send():
    """Two acts with byte-identical crops share an attribution key.

    A reply that some record binds is on record, whichever act that record is about, so
    it never refuses the other act's unanswered send; an unbound one does.
    """
    receipt = {"relative_path": "receipts/r.json", "sha256": "0" * 64}
    images = ["1" * 64]
    call = {
        "schema": perlector.CHAIR_CALL_RECORD_SCHEMA,
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
    calls, unattributed = perlector._unrecorded_replies(context(paths))
    assert (calls, unattributed) == ([], False)
    assert not perlector._answers_a_send(calls, send)
    calls, unattributed = perlector._unrecorded_replies(context([]))
    assert not unattributed and perlector._answers_a_send(calls, send)
    # Raw bytes with no call record naming them cannot be attributed to any act.
    del blobs["call"]
    assert perlector._unrecorded_replies(context([])) == ([], True)
    # A call record from before the decoding bump is refused by its name, not
    # counted as a reply no record binds.
    blobs["call"] = json.dumps({**call, "schema": "chair-call-record.v2"}).encode()
    with pytest.raises(ContractError, match="written as chair-call-record.v2"):
        perlector._unrecorded_replies(context([]))
