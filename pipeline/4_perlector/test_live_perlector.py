"""The Perlector stage wired to a live chair, proven offline end to end.

Nothing here starts a pod, opens a socket, or loads a model. The run tree is
built by the real Door-through-Attestatores chain as subprocesses, and then
`run.py`'s own `main` is called in this process with the fake endpoint from
`operations/serving/fakes.py` behind it — so what is proved is the stage's
wiring: which reader the sealed catalogue selects, what the record carries
about the call that produced it, which acts a resumed pass declines to ask
about again, and which engine answers stop the pass rather than being published.

The selector is deliberately not a flag on this stage. A run is live because the
serving-recipe row sealed into its `config_digest` says `kind = "vllm"` for the
resolved Perlector chair, so these tests build a catalogue whose Perlector rows
are live and let the run bind it exactly as a real one would.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from live_reader import EngineSignalRefusal

from common.chairs.registry import ChairRegistry
from common.contracts.canonical import digest_bytes
from common.contracts.envelope import validate_input_refs
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import PERLECTOR
from common.decoding import load_decoding_policy
from common.runtree.store import RunTree
from operations.serving.client import ChairClient, ServingModeRefusal
from operations.serving.config import (
    ServingConfigInputs,
    chair_preflight_identity_digest,
    load_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.errors import ServiceStopError
from operations.serving.fakes import (
    ABSENT,
    FakeEndpoint,
    FakeLauncher,
    FakePackages,
    FakeRegistry,
    ScriptedAnswer,
)
from operations.serving.manager import ServingManager, StageContextReceiptPublisher
from operations.serving.residency import FileResidencyLease

ROOT = Path(__file__).resolve().parents[2]
CHAIN_THROUGH_ATTESTATORES = (
    "pipeline/1_exemplar/door.py",
    "pipeline/1_exemplar/run.py",
    "pipeline/1_ink_map/run.py",
    "pipeline/2_designator/run.py",
    "pipeline/3_attestatores/run.py",
)
TIER = "generic-48gb"
SERVED_MODEL_ID = "perlector-under-test"
# Long enough that `truncation.is_length_suspicious` never fires on this
# fixture's small regions (12,800 and 16,000 pixels against a 2,000
# pixels-per-character floor): the tests below are about the engine's own stop
# word, and a reading the length heuristic independently called suspicious would
# prove the wrong thing.
READING = "SYNTHETIC LIVE READING alpha beta gamma delta epsilon zeta eta theta iota kappa"


def _perlector():
    spec = importlib.util.spec_from_file_location(
        "live_perlector_under_test", ROOT / "pipeline" / "4_perlector" / "run.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _perlector()


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


def _live_row(identity) -> dict[str, Any]:
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
        "max_model_len": 2048,
        "max_num_seqs": 1,
        "max_num_batched_tokens": 512,
        "gpu_memory_utilization": "0.58",
        "min_pixels": 3136,
        "max_pixels": 1806336,
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


def _live_catalogue(destination: Path) -> Path:
    """The committed fixture catalogue with its Perlector rows made live.

    Every other chair keeps its fixture row, so this is exactly the shape
    `serving_mode_for` reads: three names into one catalogue, and the row it
    lands on decides. The committed file is never touched.
    """
    source = (ROOT / "config" / "serving_recipes.toml").read_text(encoding="utf-8")
    marker = '[[profiles]]\nkind = "fixture"\nrecipe = "fake-perlector-v0"'
    head, *perlector_rows = source.split(marker)
    assert len(perlector_rows) == 3, "the fixture catalogue no longer carries three Perlector rows"
    row = _live_row(_perlector_identity())
    body = "\n".join(f"{key} = {_toml_value(value)}" for key, value in row.items())
    path = destination / "serving_recipes_live_perlector.toml"
    path.write_text(f"{head}[[profiles]]\n{body}\n", encoding="utf-8")
    return path


def _chain_through_attestatores(root: Path, catalogue: Path, *, scenario: str = "happy") -> None:
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


@pytest.fixture(scope="module")
def declaring_chained_run(tmp_path_factory) -> tuple[Path, Path]:
    """A run tree sealed under `no-readable-text-reading` throughout.

    `config_digest` binds the scenario along with everything else
    (`run_config_bindings`), so a live Perlector pass over this scenario
    must be sealed by the whole chain under it — asking a run built as
    `happy` to read as a different scenario is refused by `open_context`
    itself (`IncompatibleReuse`) before the Perlector's own guard is ever
    reached, and rightly so: it is a different question from the one this
    guard answers.
    """
    base = tmp_path_factory.mktemp("live-perlector-declaring")
    catalogue = _live_catalogue(base)
    root = base / "runs"
    _chain_through_attestatores(root, catalogue, scenario="no-readable-text-reading")
    return root, catalogue


@pytest.fixture()
def declaring_run(declaring_chained_run, tmp_path: Path) -> tuple[Path, Path]:
    template, catalogue = declaring_chained_run
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


def _serving_factory(endpoint: FakeEndpoint, catalogue: Path, log_root: Path, lock: Path):
    """The `(context, chair, tier) -> ChairClient` seam `main` injects against.

    Deliberately close to `run.default_serving_factory`: the same manager, the
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
        )
        return ChairClient(
            manager=manager,
            identity=chair,
            tier=tier,
            retain=lambda data: perlector.retain_chair_bytes(context, data),
            decoding_config_sha256=decoding_sha256,
            record_temperature=decoding_policy["reading_of_record"]["temperature"],
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
    # Padded rather than exactly counted: the number of reader calls per act is
    # the pass structure's business (Pass A, Pass B, and a re-proof when the
    # frozen flags ask for one), and a test that pinned it here would fail on
    # any honest change to that structure while proving nothing about the seam.
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
        ],
    )
    return endpoint, perlector.main(serving_factory=factory)


def _published_readings(root: Path) -> list[dict[str, Any]]:
    """Every Perlectio on disk that records an attempted reading.

    Read from the artifact files rather than through a manifest: a pass that
    stopped never wrote one, and these tests need to see exactly what a stopped
    pass did and did not publish.
    """
    directory = root / "r" / "4_perlector" / "artifacts" / "perlectio"
    if not directory.exists():
        return []
    records = [json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")]
    return [record for record in records if record["outcome"] != "not-run"]


# --- the selector: the sealed row kind, and nothing else ----------------------


def _mode_arguments(catalogue: Path, tier: str | None):
    placement = ROOT / "config" / "pod_placement.toml"
    context = SimpleNamespace(
        serving_config_inputs={
            "schema": "serving-config-inputs.v1",
            "serving_recipes_sha256": digest_bytes(Path(catalogue).read_bytes()),
            "pod_placement_sha256": digest_bytes(placement.read_bytes()),
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
    substitute.write_bytes(Path(catalogue).read_bytes() + b"\n# a byte that moved\n")
    context, args = _mode_arguments(catalogue, TIER)
    args.serving_recipes_config = str(substitute)
    with pytest.raises(perlector.ContractError, match="not the catalogue this run sealed"):
        perlector.perlector_serving_mode(context, args, _perlector_identity())


def test_an_absent_chair_resolves_to_fixture_without_consulting_the_catalogue():
    """An absence has no identity to look a row up by, so none is looked up."""
    absent = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml")).resolve(
        "secondary_proposer"
    )
    context = SimpleNamespace(serving_config_inputs=None)
    args = SimpleNamespace(serving_recipes_config="/nonexistent.toml", placement_tier=None)
    assert perlector.perlector_serving_mode(context, args, absent) == "fixture"


# --- a live pass, and what its record carries ---------------------------------


def test_a_live_pass_reads_through_the_chair_and_binds_the_call_it_read_from(
    live_run, tmp_path, monkeypatch
):
    root, _catalogue = live_run
    endpoint, exit_code = _run_perlector(
        live_run, tmp_path, monkeypatch, ScriptedAnswer(content=READING, finish_reason="stop")
    )
    assert exit_code == 0
    assert endpoint.requests, "the live pass sent no reading request"

    tree = RunTree(root, "r")
    readings = _published_readings(root)
    assert readings, "the live pass published no reading"
    for record in readings:
        payload = record["payload"]
        assert payload["text"] == READING
        call = payload["engine_call"]
        assert call["finish_reason"] == "stop"
        assert call["served_model_id"] == SERVED_MODEL_ID
        # The retained response is on disk, at the digest the record names, and
        # the envelope binds it as a direct input rather than merely citing it.
        assert tree.resolve(call["raw_response_ref"]["relative_path"]).exists()
        assert call["raw_response_ref"]["sha256"] == call["response_sha256"]
        bound = {reference["relative_path"] for reference in record["inputs"]}
        assert call["raw_response_ref"]["relative_path"] in bound
        assert call["call_record_ref"]["relative_path"] in bound
        # The receipt is the one the live service published, never the declared
        # fixture stand-in.
        receipt = tree.read_run_receipt(payload["provenance"]["receipt_ref"])
        assert receipt["engine"] == "vllm"
        assert receipt["chair"] == "perlector"


def test_the_pass_asks_the_engine_exactly_once_per_reading_and_never_retries(
    live_run, tmp_path, monkeypatch
):
    """One request per reader call, and one reader call per arm.

    GOVERNANCE 7: the pipeline does not gate model behaviour. A retry, a second
    sample, or a re-ask on a disappointing answer would all show up here as more
    requests than the pass has arms.
    """
    root, _catalogue = live_run
    endpoint, _exit = _run_perlector(
        live_run, tmp_path, monkeypatch, ScriptedAnswer(content=READING, finish_reason="stop")
    )
    readings = _published_readings(root)
    # Pass A and Pass B for every act that was read, plus at most one re-proof
    # each; nothing in this stage may ask twice for one arm.
    assert 2 * len(readings) <= len(endpoint.requests) <= 3 * len(readings)
    # Every request carries the sealed record posture and nothing sampled.
    for request in endpoint.requests:
        assert request["temperature"] == 0
        assert request["stream"] is False
        assert request["model"] == SERVED_MODEL_ID


def test_an_engine_length_publishes_a_held_truncation_and_never_a_reading(
    live_run, tmp_path, monkeypatch
):
    root, _catalogue = live_run
    _endpoint, exit_code = _run_perlector(
        live_run, tmp_path, monkeypatch, ScriptedAnswer(content=READING, finish_reason="length")
    )
    readings = _published_readings(root)
    assert readings
    for record in readings:
        assert record["outcome"] == "truncated"
        truncation = record["payload"]["truncation"]
        assert truncation["classification"] == "truncated"
        assert truncation["signals"]["stop_reason_declared"] == "length"
        assert record["payload"]["engine_call"]["finish_reason"] == "length"
    # The stage still completes: a truncated act is *recorded* as truncated and
    # the Recensor routes it to review. Losing the run's other acts to one held
    # reading is the failure this stage does not have.
    assert exit_code == 0


def test_an_unreported_stop_reason_holds_the_reading_as_unknown(live_run, tmp_path, monkeypatch):
    """An engine that reported nothing is never `complete` (`truncation.py`).

    The absence travels verbatim: `finish_reason` is `null` on the call record
    and `stop_reason_declared` is `None` on the instrument, rather than a `"stop"`
    nothing observed.
    """
    root, _catalogue = live_run
    _endpoint, _exit = _run_perlector(
        live_run, tmp_path, monkeypatch, ScriptedAnswer(content=READING, finish_reason=ABSENT)
    )
    readings = _published_readings(root)
    assert readings
    for record in readings:
        assert record["outcome"] == "truncated"
        assert record["payload"]["truncation"]["classification"] == "unknown"
        assert record["payload"]["truncation"]["signals"]["stop_reason_declared"] is None
        assert record["payload"]["engine_call"]["finish_reason"] is None


def test_an_unrecognized_stop_reason_stops_the_pass_with_the_bytes_retained(
    live_run, tmp_path, monkeypatch
):
    """`"abort"` is neither a completion nor a cutoff, so it is refused by name.

    Nothing is lost by stopping: the client retained the response before it was
    parsed, so the bytes that stopped the pass are on disk and the act can be
    traced back to exactly them. Nothing is published for the act, because a
    Perlectio has no `failed` shape and minting one here would invent a record
    kind this seam does not own.
    """
    root, _catalogue = live_run
    with pytest.raises(EngineSignalRefusal, match="abort"):
        _run_perlector(
            live_run, tmp_path, monkeypatch, ScriptedAnswer(content=READING, finish_reason="abort")
        )
    assert _published_readings(root) == []
    blobs = root / "r" / "4_perlector" / "blobs" / "sha256"
    retained = [path.read_bytes() for path in blobs.glob("*")] if blobs.exists() else []
    assert any(b'"abort"' in body for body in retained), "the refusing response was not retained"


def test_a_body_that_is_not_a_reading_stops_the_pass_rather_than_being_published(
    live_run, tmp_path, monkeypatch
):
    """A malformed body is retained evidence, never a Perlectio.

    The witness path turns one into a `failed` Testimonium; a reading has no
    such shape, so the honest outcome here is a loud stop over retained bytes.
    """
    root, _catalogue = live_run
    with pytest.raises(EngineSignalRefusal, match="CHAIR_RESPONSE_"):
        _run_perlector(
            live_run, tmp_path, monkeypatch, ScriptedAnswer(body=b"{ this is not a reading")
        )
    assert _published_readings(root) == []


def test_a_resumed_live_pass_never_asks_the_chair_about_an_act_already_sealed(
    live_run, tmp_path, monkeypatch
):
    """GOVERNANCE 4: a live chair cannot reproduce immutable bytes.

    A fixture resume republishes byte-identical readings and the store reuses
    them. A live one cannot, so an act already sealed at this ordinal is left
    exactly as it is and never asked again — and with every act already sealed,
    no chair is started at all.
    """
    root, _catalogue = live_run
    first, _exit = _run_perlector(
        live_run, tmp_path, monkeypatch, ScriptedAnswer(content=READING, finish_reason="stop")
    )
    sealed = len(_published_readings(root))
    assert sealed and first.requests

    second, exit_code = _run_perlector(
        live_run,
        tmp_path / "resume",
        monkeypatch,
        ScriptedAnswer(
            content="A SECOND READING THAT MUST NEVER BE ASKED FOR", finish_reason="stop"
        ),
    )
    assert exit_code == 0
    assert second.requests == [], "a resumed live pass re-read an act it had already sealed"
    assert len(_published_readings(root)) == sealed


def test_a_live_pass_refuses_a_fixture_declared_reading_failure(
    declaring_run, tmp_path, monkeypatch
):
    """A declared `reading_failure` is a stand-in for a real engine's own
    report (`_reconciled_truncation`'s own docstring). A live pass answering a
    declared act is a misconfiguration knowable from the fixture and the act
    key alone, before any chair is started -- so the refusal fires ahead of
    every reader call, chair start and publication, leaving the tree exactly
    as this invocation found it (no orphaned Pass A, no engine call spent on a
    reading that would be discarded). `declaring_run` is sealed under
    `no-readable-text-reading` throughout the chain, so `config_digest`
    matches this same scenario."""
    root, _catalogue = declaring_run
    captured_endpoint: list = []
    with pytest.raises(perlector.ContractError, match="declares reading outcome"):
        _run_perlector(
            declaring_run,
            tmp_path,
            monkeypatch,
            ScriptedAnswer(content=READING, finish_reason="stop"),
            scenario="no-readable-text-reading",
            endpoint_out=captured_endpoint,
        )
    assert _published_readings(root) == [], (
        "a refused act must publish nothing rather than a reading contradicted "
        "by its own declared outcome"
    )
    assert captured_endpoint and captured_endpoint[0].requests == [], (
        "the refusal must fire before any chair is asked to read"
    )
    artifacts_dir = root / "r" / "4_perlector" / "artifacts"
    published_kinds = (
        {path.name for path in artifacts_dir.iterdir() if path.is_dir()}
        if artifacts_dir.exists()
        else set()
    )
    assert published_kinds == set(), (
        "the refusal must fire before any establishing pass is published -- "
        f"found {published_kinds}"
    )


def test_a_duplicated_page_render_input_still_refuses_the_double_count(live_run):
    """The dedup added for a repeated re-proof reference must stay scoped to
    the re-proof: `row["inputs"]` (the image, testimonia, attachment and prior
    references) can never legitimately repeat, and a duplicate there must
    still hit the envelope's own two-digests-for-one-path refusal rather than
    being silently absorbed by `_distinct_inputs` across the whole list."""
    page = {"relative_path": "4_perlector/blobs/sha256/aa", "sha256": "a" * 64}
    row_inputs = [page, page]
    reproof_inputs: list[dict[str, str]] = []
    deduped = [
        reference
        for reference in perlector._distinct_inputs(reproof_inputs)
        if reference not in row_inputs
    ]
    reading_inputs = row_inputs + deduped
    assert reading_inputs.count(page) == 2, (
        "a page repeated in row['inputs'] must reach the envelope's own "
        "double-count refusal unchanged, not be collapsed here"
    )
    with pytest.raises(SchemaRefusal, match="is listed twice"):
        validate_input_refs(reading_inputs)


# --- the refusals this wiring adds --------------------------------------------


def test_an_outcome_that_attempted_no_reading_cannot_carry_a_receipt():
    """A held act and an absent chair name what would have read and stop there."""
    with pytest.raises(SchemaRefusal, match="attempted no reading"):
        perlector.provenance_for(
            SimpleNamespace(),
            _perlector_identity(),
            attempted=False,
            receipt_ref={"relative_path": "receipts/sha256/x.json", "sha256": "a" * 64},
        )


def test_an_absent_chair_that_attempted_a_reading_cannot_carry_a_receipt():
    """An absent chair served nothing, so a receipt reference names a serving
    moment it never had -- the mirror of the not-attempted guard above, for
    the other reading that never happened."""
    absent = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml")).resolve(
        "secondary_proposer"
    )
    with pytest.raises(SchemaRefusal, match="absent"):
        perlector.provenance_for(
            SimpleNamespace(),
            absent,
            attempted=True,
            receipt_ref={"relative_path": "receipts/sha256/x.json", "sha256": "a" * 64},
        )


def test_retaining_a_chair_response_after_the_seal_is_refused():
    """The stage's blob inventory is what its completion seal witnessed."""
    with pytest.raises(SchemaRefusal, match="witnessed blob inventory false"):
        perlector.retain_chair_bytes(SimpleNamespace(sealed=True), b"{}")


def test_two_digests_for_one_input_path_are_refused():
    """Content addressing makes this impossible, so it is a rewritten blob."""
    first = {"relative_path": "4_perlector/blobs/sha256/aa", "sha256": "a" * 64}
    second = {"relative_path": "4_perlector/blobs/sha256/aa", "sha256": "b" * 64}
    assert perlector._distinct_inputs([first, first]) == [first]
    with pytest.raises(SchemaRefusal, match="two different digests"):
        perlector._distinct_inputs([first, second])


def test_an_engine_call_naming_bytes_that_moved_is_refused(live_run):
    """A record whose response reference resolves to nothing reads as evidence."""
    root, _catalogue = live_run
    tree = RunTree(root, "r")
    _digest, result = tree.put_blob(PERLECTOR, b"a retained response")
    context = SimpleNamespace(
        input_ref=lambda relative_path: {
            "relative_path": relative_path,
            "sha256": digest_bytes(tree.read_bytes(relative_path)),
        }
    )
    honest = {"relative_path": result.relative_path, "sha256": digest_bytes(b"a retained response")}
    full_call = {
        "raw_response_ref": honest,
        "call_record_ref": honest,
        "response_sha256": honest["sha256"],
        "finish_reason": "stop",
        "served_model_id": SERVED_MODEL_ID,
    }
    assert perlector.engine_call_inputs(context, full_call) == [honest, honest]
    lying = {"relative_path": result.relative_path, "sha256": "c" * 64}
    with pytest.raises(SchemaRefusal, match="retained bytes at that path"):
        perlector.engine_call_inputs(
            context, {**full_call, "raw_response_ref": lying, "response_sha256": lying["sha256"]}
        )


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


def test_a_fixture_reading_carries_no_engine_call_field():
    """The field exists only on the shape that has an engine behind it.

    `with_engine_call` is the one place a record's closed field set widens, and
    a `LectioResult` with no `engine_call` — every `FixtureReader` result — must
    leave both the payload and the schema exactly as they were, or the
    acceptance pin over the fixture path would move.
    """
    payload = {"text": "alpha"}
    fields = perlector.with_engine_call(
        payload, {"text": "alpha", "stop_reason": "stop"}, frozenset({"text"})
    )
    assert payload == {"text": "alpha"}
    assert fields == frozenset({"text"})
    live = {"text": "alpha", "stop_reason": "stop", "engine_call": {"finish_reason": "stop"}}
    fields = perlector.with_engine_call(payload, live, frozenset({"text"}))
    assert payload["engine_call"] == {"finish_reason": "stop"}
    assert fields == frozenset({"text", "engine_call"})


# --- the shutdown-before-seal ordering, and the production factory ------------


def test_a_failed_chair_shutdown_stops_the_pass_before_the_seal_is_written(
    live_run, tmp_path, monkeypatch
):
    """HANDOFF.md: 'One chair, started late, stopped before the seal.' A
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


def test_default_serving_factory_writes_its_log_and_lease_under_the_run_tree(live_run, monkeypatch):
    """`default_serving_factory` is the only path a real run takes, and nothing
    else in this suite ever constructs it -- the injected `_serving_factory`
    above deliberately diverges on the two things production alone decides:
    where the serving log directory and the pod-GPU residency lease live.
    Constructing the client starts nothing (`ChairClient.__init__` only stores
    its manager), so this proves both locations, and the manager keyword set
    that builds them, without starting a service or needing a card."""
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
    args = perlector.stage_parser(perlector.__doc__.splitlines()[0]).parse_args()
    context = perlector.open_context(args, PERLECTOR, registry_factory=ChairRegistry.from_toml)
    decoding_policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
    recipes = load_serving_recipes(catalogue)
    factory = perlector.default_serving_factory(
        recipes,
        decoding_config_sha256=decoding_sha256,
        record_temperature=decoding_policy["reading_of_record"]["temperature"],
    )
    client = factory(context, _perlector_identity(), TIER)
    tree_root = context.tree.root
    assert client._manager.log_root.is_relative_to(tree_root)
    assert client._manager.residency_lease.path.is_relative_to(tree_root)
    # Neither write disturbs the witnessed inventory (`build_manifest` walks
    # only `<stage>/artifacts`, the blob inventory only `<stage>/blobs`), so the
    # seal must still succeed with these paths named but nothing started.
    context.seal_boundary()
