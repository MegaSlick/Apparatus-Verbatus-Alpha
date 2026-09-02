"""The live Attestatores pass: selection, chair-outer serving, and the resume rule.

Every test here runs offline against `operations/serving/fakes.py`. Nothing
starts a pod, contacts a provider or loads a model: a scripted `FakeEndpoint`
answers one chair at a time behind a real `ServingManager`, a real
`ChairClient`, and this stage's own `main`, over a run tree carried to the
Designator by the real upstream stage programs.

**Why the DAI chair is absent from the live roster these tests use.** Two
defects outside this unit's files stop a `dai.v1` chair from being served at
all, and both are proven here rather than described:

1. `feeding.dai_generation()` carries floats (`repetition_penalty`, `top_p`,
   `temperature`). `ChairClient.read` writes its `chair-call-record.v1` blob
   with `common.contracts.canonical.canonical_bytes`, which refuses a float
   outright, so the request cannot be recorded and therefore cannot be made
   (`test_a_live_dai_request_cannot_be_recorded_and_says_which_value_stops_it`).
2. `witness_adapters._dai_present` republishes its own crop even when DAI needs
   no resize, while `feeding.dai_model_view`'s identity-transform rule requires
   the source and model image references to be the *same* retained blob -- so a
   small act crop, which is every act crop in this fixture, is refused after the
   response comes back. `pipeline/3_attestatores/live_witness.py`'s module
   docstring names this one too.

Both are one-file changes in files this unit does not own, and neither can be
worked around here without recording something nobody measured. The live roster
below is therefore the two page-scoped chairs, and the HANDOFF carries both as
owed work.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

STAGE = Path(__file__).resolve().parent
ROOT = STAGE.parents[1]
if str(STAGE) not in sys.path:
    sys.path.insert(0, str(STAGE))

import feeding  # noqa: E402

from common.chairs.models import ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.stages import ATTESTATORES  # noqa: E402
from common.decoding import load_decoding_policy  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from operations.serving.client import ChairClient, ChairRequest  # noqa: E402
from operations.serving.config import (  # noqa: E402
    ServingConfigInputs,
    chair_preflight_identity_digest,
    load_serving_recipes,
    parse_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.fakes import (  # noqa: E402
    ABSENT,
    FakeBlobStore,
    FakeEndpoint,
    FakeLauncher,
    FakePackages,
    FakePublisher,
    FakeRegistry,
    ScriptedAnswer,
)
from operations.serving.manager import (  # noqa: E402
    ServingManager,
    StageContextReceiptPublisher,
)
from operations.serving.residency import FileResidencyLease  # noqa: E402

TIER = "generic-48gb"
RUN_ID = "live"
LIVE_CHAIRS = ("attestator_1", "attestator_3")
CATALOGUE_CHAIRS = ("attestator_1", "attestator_2", "attestator_3")
FIXTURE_ROOT = ROOT / "proof"

# A body shaped like something a real Chandra service would return, and
# deliberately *not* `fixture-chandra-response.v1`: `chandra.parse` recognizes
# only that fixture schema, so this is what "live in transport only" looks like
# on the wire (SPEC_A section 2.2).
CHANDRA_BODY = '{"pages": [{"markdown": "a real Chandra body, not the fixture schema"}]}'
CHURRO_PAGE_ONE = (
    "<output>SYNTHETIC ACT ONE alpha beta\nSYNTHETIC ACT TWO delta epsiIon zeta eta</output>"
)
CHURRO_PAGE_TWO = "<output>SYNTHETIC ACT TWO delta epsiIon zeta eta</output>"


def _load_attestatores():
    path = STAGE / "run.py"
    spec = importlib.util.spec_from_file_location("attestatores_live_pass_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attestatores = _load_attestatores()


# --------------------------- the sealed live catalogue ------------------------


def _vllm_row(*, recipe: str, chair: str, port: int) -> dict[str, Any]:
    """One complete `kind = "vllm"` profile row, in the shape `config.py` closes.

    Mirrors `operations/serving/test_manager.py::profile_row` and the row
    `test_live_witness.py` already builds, because a live posture is exactly
    what those rows describe; the figures are test values and are never written
    into a committed catalogue.
    """
    return {
        "kind": "vllm",
        "recipe": recipe,
        "chair": chair,
        "tier": TIER,
        "host": "127.0.0.1",
        "port": port,
        "served_model_id": f"served-{chair}",
        "dtype": "bfloat16",
        "seed": 7,
        "required_packages": {"vllm": "0.test"},
        "max_model_len": 2048,
        "max_num_seqs": 1,
        "max_num_batched_tokens": 256,
        "gpu_memory_utilization": "0.85",
        "min_pixels": 1,
        "max_pixels": 1024,
        "enable_prefix_caching": True,
        "enforce_eager": False,
        "trust_remote_code": False,
        "enable_tower_connector_lora": False,
        "max_lora_rank": 16,
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


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return f"'{value}'" if '"' in value else f'"{value}"'
    raise TypeError(f"no TOML rendering for {value!r}")


def _toml_profile(row: dict[str, Any]) -> str:
    lines = ["[[profiles]]"]
    tables = [(key, value) for key, value in row.items() if isinstance(value, dict)]
    lines.extend(
        f"{key} = {_toml_value(value)}" for key, value in row.items() if not isinstance(value, dict)
    )
    for name, table in tables:
        lines.append(f"[profiles.{name}]")
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in table.items())
    return "\n".join(lines) + "\n"


def write_live_catalogue(path: Path, registry) -> Path:
    """A serving catalogue whose witness rows are live, sealed into this run.

    The two non-witness chairs keep fixture rows: this run's Designator and
    Perlector are not what the Attestatores reads, and inventing live rows for
    them would put figures nobody measured beside chairs nothing starts.
    """
    rows: list[dict[str, Any]] = []
    for chair, recipe in (
        ("designator_structure", "fake-designator-v0"),
        ("perlector", "fake-perlector-v0"),
    ):
        rows.extend(
            {
                "kind": "fixture",
                "recipe": recipe,
                "chair": chair,
                "tier": tier,
                "description": "offline walking-skeleton fixture row",
            }
            for tier in ("generic-24gb", "generic-48gb", "generic-80gb-plus")
        )
    for index, chair in enumerate(CATALOGUE_CHAIRS):
        identity = registry.resolve(chair)
        row = _vllm_row(recipe=identity.serving_recipe, chair=chair, port=8000 + index)
        row["preflight_identity_digest"] = chair_preflight_identity_digest(identity)
        row["preflight_digest"] = profile_preflight_digest(row)
        rows.append(row)
    path.write_text(
        'schema = "serving-recipes.v1"\n\n' + "\n".join(_toml_profile(row) for row in rows),
        encoding="utf-8",
    )
    # Parsed once here so a malformed catalogue fails in this helper, naming
    # itself, rather than three subprocesses later inside a stage program.
    load_serving_recipes(path)
    return path


def write_mixed_catalogue(path: Path, registry) -> Path:
    """One witness chair fixture, the other two live: a posture nothing can read."""
    rows: list[dict[str, Any]] = [
        {
            "kind": "fixture",
            "recipe": registry.resolve("attestator_1").serving_recipe,
            "chair": "attestator_1",
            "tier": TIER,
            "description": "a fixture row beside live siblings",
        }
    ]
    for index, chair in enumerate(("attestator_2", "attestator_3")):
        identity = registry.resolve(chair)
        row = _vllm_row(recipe=identity.serving_recipe, chair=chair, port=8200 + index)
        row["preflight_identity_digest"] = chair_preflight_identity_digest(identity)
        row["preflight_digest"] = profile_preflight_digest(row)
        rows.append(row)
    path.write_text(
        'schema = "serving-recipes.v1"\n\n' + "\n".join(_toml_profile(row) for row in rows),
        encoding="utf-8",
    )
    return path


def write_absent_dai_models_config(work: Path) -> Path:
    """The committed roster with its one act-scoped chair marked absent.

    See the module docstring: a `dai.v1` chair cannot be served at all until two
    defects outside this unit are fixed, and an absent chair is the roster's own
    honest way to say a witness is not there.
    """
    config_root = work / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    committed = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    assert tomllib.loads(committed)["chairs"]["attestator_2"]["state"] == "configured"
    start = committed.index("[chairs.attestator_2]\n")
    following = committed.find("\n[", start + 1)
    end = len(committed) - 1 if following == -1 else following
    absent = (
        "[chairs.attestator_2]\n"
        'state = "absent"\n'
        'reason = "the live reading seam cannot yet carry DAI\'s float generation values"\n'
    )
    path = config_root / "models.toml"
    path.write_text(committed[:start] + absent + committed[end + 1 :], encoding="utf-8")
    rewritten = tomllib.loads(path.read_text(encoding="utf-8"))
    assert rewritten["chairs"]["attestator_2"]["state"] == "absent"
    assert set(rewritten["chairs"]) == set(tomllib.loads(committed)["chairs"])
    return path


def _invoke_stage(program: str, *, run_root: Path, catalogue: Path, models: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(run_root),
            "--run-id",
            RUN_ID,
            "--scenario",
            "happy",
            "--fixture-root",
            str(FIXTURE_ROOT),
            "--serving-recipes-config",
            str(catalogue),
            "--models-config",
            str(models),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{program}: {result.stderr}"


@pytest.fixture(scope="module")
def live_run(tmp_path_factory) -> SimpleNamespace:
    """One run carried to the Designator boundary under a live serving catalogue.

    Built once: the four upstream stage programs are real subprocesses, and the
    Attestatores tests below each copy the finished tree so no test writes into
    another's evidence.
    """
    work = tmp_path_factory.mktemp("live-seam")
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    catalogue = write_live_catalogue(work / "serving_recipes_live.toml", registry)
    models = write_absent_dai_models_config(work)
    run_root = work / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
    ):
        _invoke_stage(program, run_root=run_root, catalogue=catalogue, models=models)
    _policy, decoding_sha256 = load_decoding_policy(ROOT / "config" / "decoding.toml")
    return SimpleNamespace(
        work=work,
        catalogue=catalogue,
        models=models,
        run_root=run_root,
        decoding_sha256=decoding_sha256,
    )


# ------------------------------- the live world -------------------------------


def default_scripts() -> dict[str, list[ScriptedAnswer]]:
    """One answer per page, per chair: two pages carry these two acts."""
    return {
        "attestator_1": [
            ScriptedAnswer(content=CHANDRA_BODY, finish_reason="stop"),
            ScriptedAnswer(content=CHANDRA_BODY, finish_reason="stop"),
        ],
        "attestator_3": [
            ScriptedAnswer(content=CHURRO_PAGE_ONE, finish_reason="stop"),
            ScriptedAnswer(content=CHURRO_PAGE_TWO, finish_reason="stop"),
        ],
    }


class _RunTreeBlobs:
    """`FakeEndpoint`'s retention check, pointed at the real stage blob store.

    The production `retain` writes into the run tree, not into a
    `FakeBlobStore`, so this exposes the one method the fake endpoint asks for:
    is this exact digest already on disk?
    """

    def __init__(self, context) -> None:
        self.context = context

    def has(self, sha256: str) -> bool:
        return self.context.tree.resolve(self.context.tree.blob_path(ATTESTATORES, sha256)).exists()


class LiveWorld:
    """A serving factory over scripted fake endpoints, one endpoint per chair.

    One endpoint per chair rather than one shared: `FakeEndpoint` auto-answers
    the first POST it ever sees as the manager's readiness probe, so a second
    chair sharing an instance would eat a scripted reading answer for its own
    readiness.
    """

    def __init__(self, live_run: SimpleNamespace, work: Path, scripts=None) -> None:
        self.live_run = live_run
        self.work = work
        self.work.mkdir(parents=True, exist_ok=True)
        self.scripts = default_scripts() if scripts is None else scripts
        self.endpoints: dict[str, FakeEndpoint] = {}
        self.loads: list[str] = []

    def factory(self, context, identity, tier: str) -> ChairClient:
        self.loads.append(identity.role)
        endpoint = FakeEndpoint(
            served_model_id=f"served-{identity.role}",
            blob_store=_RunTreeBlobs(context),
            # Response-as-arrival, checked from outside the client: the exact
            # bytes of the previous reading must already be on disk, by their
            # own digest, before the next request is allowed to leave.
            assert_retained_before_next_request=True,
        )
        endpoint.script(*self.scripts.get(identity.role, []))
        self.endpoints[identity.role] = endpoint
        manager = ServingManager(
            registry=context.registry,
            recipes=load_serving_recipes(self.live_run.catalogue),
            config_inputs=ServingConfigInputs.from_record(dict(context.serving_config_inputs)),
            launcher=FakeLauncher(endpoint),
            http=endpoint,
            # The run's own publisher: a live Testimonium names a receipt this
            # run really wrote, and `validate_serving_provenance` reads it back.
            receipt_publisher=StageContextReceiptPublisher(context),
            log_root=self.work / "serving-logs" / identity.role,
            package_inspector=FakePackages({"vllm": "0.test"}),
            residency_lease=FileResidencyLease(self.work / "pod-gpu.lock"),
        )
        return ChairClient(
            manager=manager,
            identity=identity,
            tier=tier,
            retain=lambda data: attestatores.retained_blob_ref(context, data),
            decoding_config_sha256=self.live_run.decoding_sha256,
            record_temperature=0,
            read_receipt=lambda reference: context.tree.read_run_receipt(dict(reference)),
        )

    def requests(self, chair: str) -> list[dict[str, object]]:
        endpoint = self.endpoints.get(chair)
        return [] if endpoint is None else endpoint.requests


def refusing_factory(context, identity, tier):
    raise AssertionError(f"a chair was started when none should have been: {identity.role!r}")


def open_live_context(live_run: SimpleNamespace, run_root: Path):
    """A real `StageContext` over the live run, for the seams `main` composes."""
    parser = attestatores.stage_parser("live pass under test", accepts_chair=True)
    parser.add_argument("--attempt-ordinal", type=int, default=None)
    args = parser.parse_args(
        [
            "--run-root",
            str(run_root),
            "--run-id",
            RUN_ID,
            "--scenario",
            "happy",
            "--fixture-root",
            str(FIXTURE_ROOT),
            "--serving-recipes-config",
            str(live_run.catalogue),
            "--models-config",
            str(live_run.models),
            "--placement-tier",
            TIER,
        ]
    )
    return attestatores.open_context(args, ATTESTATORES)


def fresh_tree(live_run: SimpleNamespace, tmp_path: Path) -> Path:
    """A private copy of the Designator-boundary run tree for one test."""
    run_root = tmp_path / "runs"
    shutil.copytree(live_run.run_root, run_root)
    return run_root


def run_attestatores(
    live_run: SimpleNamespace,
    run_root: Path,
    *,
    factory,
    extra: tuple[str, ...] = (),
    placement_tier: str | None = TIER,
) -> int:
    argv = [
        "run.py",
        "--run-root",
        str(run_root),
        "--run-id",
        RUN_ID,
        "--scenario",
        "happy",
        "--fixture-root",
        str(FIXTURE_ROOT),
        "--serving-recipes-config",
        str(live_run.catalogue),
        "--models-config",
        str(live_run.models),
        *extra,
    ]
    if placement_tier is not None:
        argv += ["--placement-tier", placement_tier]
    original, sys.argv = sys.argv, argv
    try:
        return attestatores.main(serving_factory=factory)
    finally:
        sys.argv = original


def act_records(tree: RunTree) -> dict[tuple[str, str], dict[str, Any]]:
    records = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        records[(record["payload"]["act_key"], record["payload"]["chair"])] = record
    return records


def page_records(tree: RunTree) -> dict[tuple[int, str], dict[str, Any]]:
    records = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "page-testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        records[(record["payload"]["page_ordinal"], record["payload"]["chair"])] = record
    return records


# ================================ the live pass ===============================


def test_a_live_roster_reads_each_chair_once_through_its_own_scope(live_run, tmp_path):
    """Chair-outer, one residency each, and a page chair asked once per page."""
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)

    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    # One load per chair, in the deterministic chair-outer order the schedule
    # builds; a second load of an unloaded chair is what `SingleChairResidency`
    # and `execute_stage_major_schedule` exist to refuse.
    assert world.loads == sorted(LIVE_CHAIRS)
    # Two pages carry these two acts, so a page-scoped chair answers twice --
    # not once per (act, chair), which is what the act layer would have asked.
    assert len(world.requests("attestator_1")) == 2
    assert len(world.requests("attestator_3")) == 2

    tree = RunTree(run_root, RUN_ID)
    records = act_records(tree)
    # Every configured chair still answers for every expected act: the absent
    # one through a `dead` record it never had to be asked for.
    assert {chair for _act, chair in records} == {"attestator_1", "attestator_2", "attestator_3"}
    assert records[("a1", "attestator_2")]["outcome"] == "dead"
    assert records[("a1", "attestator_3")]["outcome"] == "read"
    assert page_records(tree)[(1, "attestator_3")]["outcome"] == "read"


def test_every_live_act_record_names_the_serving_moment_and_the_call_that_produced_it(
    live_run, tmp_path
):
    """SPEC_A section 2.3: receipt, retained response, call record, capture."""
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    payload = act_records(tree)[("a1", "attestator_3")]["payload"]

    # The receipt is the live one the client re-read at start, never the
    # declared `fixture://` stand-in `fixture_serving_details` writes.
    receipt = tree.read_run_receipt(payload["provenance"]["receipt_ref"])
    assert not receipt["endpoint"].startswith("fixture://")
    assert receipt["chair"] == "attestator_3"

    # Both retained blobs are real, digest-checked bytes in this stage's store.
    for field in ("raw_response_ref", "serving_call_ref"):
        reference = payload[field]
        assert reference["relative_path"] == f"3_attestatores/blobs/sha256/{reference['sha256']}"
        attestatores.validate_retained_response_blob(tree, reference, field)

    # The retained model view names the very response the record names.
    assert payload["native_capture"]["raw_response_ref"] == payload["raw_response_ref"]
    assert payload["native_capture"]["transport_stop_reason"] == "stop"

    call = json.loads(tree.read_bytes(payload["serving_call_ref"]["relative_path"]))
    assert call["schema"] == "chair-call-record.v1"
    assert call["chair"] == "attestator_3"
    # Verbatim, and never defaulted: the engine's own word travels into the
    # request record whatever this stage later makes of it.
    assert call["finish_reason"] == "stop"
    assert call["receipt_ref"] == payload["provenance"]["receipt_ref"]


@pytest.mark.parametrize(
    ("finish_reason", "truncated", "basis"),
    [("stop", False, "trusted-response-boundary"), ("length", True, "trusted-response-boundary")],
)
def test_the_engine_stop_word_decides_the_truncation_a_live_record_publishes(
    live_run, tmp_path, finish_reason, truncated, basis
):
    """`stop` and `length` are the two words this pipeline has a meaning for."""
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_3"] = [
        ScriptedAnswer(content=CHURRO_PAGE_ONE, finish_reason=finish_reason),
        ScriptedAnswer(content=CHURRO_PAGE_TWO, finish_reason=finish_reason),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    health = act_records(tree)[("a1", "attestator_3")]["payload"]["content_health"]
    assert health["truncated"] is truncated
    assert health["truncation_basis"] == basis
    page_health = page_records(tree)[(1, "attestator_3")]["payload"]["content_health"]
    assert page_health["truncated"] is truncated


def test_chandra_goes_live_in_transport_only_and_the_record_says_so(live_run, tmp_path):
    """Transported, retained, and honestly unreadable: no wire schema exists."""
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    record = act_records(tree)[("a1", "attestator_1")]
    payload = record["payload"]
    assert record["outcome"] == "failed"
    assert "unverified-response-schema" in payload["reason"]
    assert payload["content_health"]["recordable"] is False
    # The bytes are retained and the request is accounted for even though no
    # parser could read them -- GOVERNANCE 2, and the whole point of
    # transporting Chandra before its schema is known.
    assert tree.read_bytes(payload["raw_response_ref"]["relative_path"]).decode() == CHANDRA_BODY
    assert "serving_call_ref" in payload
    # The retained model view is left off deliberately: the shared capture
    # contract admits no `unrecognized-shape` parse state (see
    # `publishable_native_capture`).
    assert "native_capture" not in payload


def test_a_resumed_live_pass_asks_no_chair_again(live_run, tmp_path):
    """A pair sealed at this ordinal is reused, never re-requested."""
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0
    before = act_records(RunTree(run_root, RUN_ID))

    # The factory itself is the assertion: a live chair cannot reproduce
    # immutable bytes, so a resume that started one would already be wrong.
    assert run_attestatores(live_run, run_root, factory=refusing_factory) == 0
    assert act_records(RunTree(run_root, RUN_ID)) == before


def test_a_resumed_live_pass_recovers_a_page_response_from_the_act_layer_it_sealed(
    live_run, tmp_path
):
    """The crash the resume rule exists for: act records sealed, page records not.

    The first pass answers page 1 and then runs out of scripted answers inside
    the page-2 request, exactly where a real interruption would land: page 1's
    act-scoped compatibility records are sealed, no page record is. The resume
    must rebuild page 1's response from those act records -- never re-asking a
    chair that cannot reproduce its own bytes -- and ask again only for the
    pages no sealed record depends on. Page 2 is one of those for both chairs:
    it carries this fixture's continuation, and a continuation page's response
    feeds no act-scoped record at all (an act's own view comes from its primary
    page), so asking for it again contradicts nothing already sealed.
    """
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_3"] = [ScriptedAnswer(content=CHURRO_PAGE_ONE, finish_reason="stop")]
    crashed = LiveWorld(live_run, tmp_path, scripts)
    with pytest.raises(IndexError):
        run_attestatores(live_run, run_root, factory=crashed.factory)

    interrupted = act_records(RunTree(run_root, RUN_ID))
    assert ("a1", "attestator_3") in interrupted
    assert not page_records(RunTree(run_root, RUN_ID))

    resumed_scripts = {
        "attestator_1": [ScriptedAnswer(content=CHANDRA_BODY, finish_reason="stop")],
        "attestator_3": [ScriptedAnswer(content=CHURRO_PAGE_TWO, finish_reason="stop")],
    }
    resumed = LiveWorld(live_run, tmp_path / "resumed", resumed_scripts)
    assert run_attestatores(live_run, run_root, factory=resumed.factory) == 0

    # Exactly one request each, for the continuation page alone: page 1 is
    # rebuilt from the act layer the interrupted pass sealed.
    assert resumed.loads == sorted(LIVE_CHAIRS)
    assert len(resumed.requests("attestator_1")) == 1
    assert len(resumed.requests("attestator_3")) == 1
    tree = RunTree(run_root, RUN_ID)
    assert act_records(tree)[("a1", "attestator_3")] == interrupted[("a1", "attestator_3")]
    published = page_records(tree)
    assert published[(1, "attestator_3")]["outcome"] == "read"
    assert published[(2, "attestator_3")]["outcome"] == "read"


def test_an_engine_stop_word_this_pipeline_cannot_read_is_refused_not_defaulted(live_run, tmp_path):
    """GOVERNANCE 10: an unmeasured boundary is never recorded as a measured one."""
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_1"] = [ScriptedAnswer(content=CHANDRA_BODY, finish_reason="abort")]
    world = LiveWorld(live_run, tmp_path, scripts)

    with pytest.raises(ContractError, match="'abort'"):
        run_attestatores(live_run, run_root, factory=world.factory)

    # Nothing about that response was published, and its bytes are retained.
    assert ("a1", "attestator_1") not in act_records(RunTree(run_root, RUN_ID))


def test_a_churro_response_with_no_engine_stop_word_is_refused_by_name(live_run, tmp_path):
    """The shared page contract cannot hold an unknown truncation state yet."""
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_3"] = [ScriptedAnswer(content=CHURRO_PAGE_ONE, finish_reason=ABSENT)]
    world = LiveWorld(live_run, tmp_path, scripts)

    with pytest.raises(ContractError, match="reconcile"):
        run_attestatores(live_run, run_root, factory=world.factory)


def test_a_live_reread_is_refused_by_name(live_run, tmp_path):
    """No live reread is built, and half-performing one would start a chair."""
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    with pytest.raises(ContractError, match="no live reread is built"):
        run_attestatores(
            live_run,
            run_root,
            factory=refusing_factory,
            extra=("--operation", "reread", "--act", "act-1", "--chair", "attestator_3"),
        )


def test_the_pass_names_the_fixture_witness_rows_its_posture_does_not_read(
    live_run, tmp_path, capsys
):
    """Ignoring a declaration silently is the loss GOVERNANCE 2 refuses."""
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    reported = capsys.readouterr().err
    assert "does not read" in reported
    assert "testimony" in reported and "churro_page_response" in reported


def test_an_unresolved_attempt_stops_the_pass_rather_than_publishing_a_gap(
    live_run, tmp_path, monkeypatch
):
    """Every configured chair answers for every expected act, or the record says why."""
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    monkeypatch.setattr(attestatores, "_serve_page_unit", lambda *args, **kwargs: 0)

    with pytest.raises(FatalAccounting, match="unresolved"):
        run_attestatores(live_run, run_root, factory=world.factory)


# ============================ selection and refusals ==========================


def test_witness_serving_modes_reads_the_sealed_row_kind_for_every_chair(live_run):
    """The committed catalogue is fixture for every chair; the live one is not."""
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    context = SimpleNamespace(
        witness_chairs=list(CATALOGUE_CHAIRS),
        registry=registry,
    )
    committed = load_serving_recipes(ROOT / "config" / "serving_recipes.toml")
    assert attestatores.witness_serving_modes(context, committed, None) == {
        chair: "fixture" for chair in CATALOGUE_CHAIRS
    }
    live = load_serving_recipes(live_run.catalogue)
    assert attestatores.witness_serving_modes(context, live, TIER) == {
        chair: "live" for chair in CATALOGUE_CHAIRS
    }


def test_witness_serving_modes_refuses_a_roster_that_mixes_postures(tmp_path):
    """One run, one serving posture: never half a card and half a fixture."""
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    mixed = write_mixed_catalogue(tmp_path / "mixed.toml", registry)
    context = SimpleNamespace(witness_chairs=list(CATALOGUE_CHAIRS), registry=registry)
    with pytest.raises(ContractError, match="mixes serving postures"):
        attestatores.witness_serving_modes(context, load_serving_recipes(mixed), TIER)


def test_witness_serving_modes_refuses_a_live_chair_with_no_measured_placement_tier(live_run):
    """A live row is resolved by three names; the tier is one of them."""
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    context = SimpleNamespace(witness_chairs=list(CATALOGUE_CHAIRS), registry=registry)
    with pytest.raises(ContractError, match="placement-tier"):
        attestatores.witness_serving_modes(context, load_serving_recipes(live_run.catalogue), None)


def test_bound_serving_recipes_refuses_a_catalogue_it_cannot_read(tmp_path):
    """The rows a posture is read from must be the rows the run sealed."""
    context = SimpleNamespace(
        args=SimpleNamespace(serving_recipes_config=str(tmp_path / "absent.toml")),
        serving_config_inputs={
            "schema": "serving-config-inputs.v1",
            "serving_recipes_sha256": "0" * 64,
            "pod_placement_sha256": "1" * 64,
        },
    )
    with pytest.raises(ContractError, match="serving configuration"):
        attestatores.bound_serving_recipes(context)


def test_require_live_page_capture_refuses_a_page_nobody_was_asked_about():
    """A live page record has exactly one source: the response for that page."""
    with pytest.raises(FatalAccounting, match="never requested"):
        attestatores.require_live_page_capture({}, 2, "attestator_3")


def test_resumed_page_captures_refuses_a_sealed_act_record_no_chair_served():
    """A live pass cannot resume over evidence the fixture posture wrote."""
    fixture_attempt = attestatores.Attempt(
        outcome="read",
        native_payload="declared text",
        witness_reported=None,
        format_capabilities=dict(attestatores.DEFAULT_FORMAT_CAPABILITIES),
        health=attestatores.content_health("declared text", completed=True),
        reason=None,
    )
    context = SimpleNamespace(
        tree=SimpleNamespace(build_manifest=lambda stage: {"artifacts": []}),
    )
    with pytest.raises(SchemaRefusal, match="names no serving call"):
        attestatores.resumed_page_captures(
            context,
            acts_by_page={1: [{"act_id": "act-1", "page_ordinal": 1}]},
            page_chairs=["attestator_3"],
            ordinal=1,
            attempts_by_pair={("act-1", "attestator_3"): fixture_attempt},
            sealed_pairs=frozenset({("act-1", "attestator_3")}),
        )


def test_a_page_record_the_fixture_posture_wrote_is_not_resumed_into_a_live_pass():
    """The receipt says who served it, and a `fixture://` endpoint says nobody did."""
    record = {
        "outcome": "read",
        "payload": {
            "payload": "declared text",
            "content_health": {},
            "format_capabilities": {},
            "provenance": {"receipt_ref": {"relative_path": "receipts/x.json", "sha256": "a" * 64}},
        },
    }
    context = SimpleNamespace(
        tree=SimpleNamespace(
            read_run_receipt=lambda reference: {"endpoint": "fixture://offline-chair-runner"}
        )
    )
    with pytest.raises(SchemaRefusal, match="no live serving receipt"):
        attestatores._page_capture_from_record(context, record, "the page Testimonium")


def test_a_live_dai_request_cannot_be_recorded_and_says_which_value_stops_it(tmp_path):
    """The first of the two defects that keep DAI out of the live roster.

    `chair-call-record.v1` is canonical JSON, and canonical JSON refuses a
    float; DAI's carried generation config is mostly floats. The request is
    therefore refused while it is being *recorded*, before any answer is read,
    which is the honest place for it -- a reading whose request cannot be
    recorded has no provenance (GOVERNANCE 6).
    """
    identity = ChairIdentity(
        role="attestator_2",
        source="huggingface",
        repo="example/dai",
        path=None,
        revision="a" * 40,
        digest_manifest="b" * 64,
        manifest="manifests/attestator_2.json",
        adapter_of=None,
        serving_recipe="recipe-live",
        license_note="test identity only",
        witness_adapter="dai.v1",
        witness_scope="act",
    )
    row = _vllm_row(recipe=identity.serving_recipe, chair=identity.role, port=8100)
    row["preflight_identity_digest"] = chair_preflight_identity_digest(identity)
    row["preflight_digest"] = profile_preflight_digest(row)
    blob_store = FakeBlobStore(tmp_path / "blobs")
    endpoint = FakeEndpoint(served_model_id=row["served_model_id"], blob_store=blob_store)
    manager = ServingManager(
        registry=FakeRegistry({identity.role: identity}, tmp_path),
        recipes=parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": [row]}),
        config_inputs=ServingConfigInputs("1" * 64, "2" * 64),
        launcher=FakeLauncher(endpoint),
        http=endpoint,
        receipt_publisher=FakePublisher(),
        log_root=tmp_path / "logs",
        package_inspector=FakePackages({"vllm": "0.test"}),
        residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
    )
    client = ChairClient(
        manager=manager,
        identity=identity,
        tier=TIER,
        retain=blob_store.retain,
        decoding_config_sha256="c" * 64,
        record_temperature=0,
        read_receipt=lambda reference: {
            "chair": identity.role,
            "revision": identity.receipt_revision,
        },
    )
    declared = feeding.dai_generation()
    request = ChairRequest(
        kind="chat-completions",
        messages=({"role": "user", "content": [{"type": "text", "text": "read this"}]},),
        image_sha256s=(),
        generation_declared=declared,
        generation_sent={key: declared[key] for key in ("repetition_penalty", "top_k", "top_p")},
    )
    with client:
        endpoint.script(ScriptedAnswer(content="transcribed", finish_reason="stop"))
        with pytest.raises(TypeError, match="repetition_penalty"):
            client.read(request)


def test_the_default_serving_factory_binds_the_run_that_will_record_the_reading(live_run, tmp_path):
    """Construction only: the registry, receipts and catalogue are the run's own.

    Nothing here starts a process or opens a socket -- building a `ChairClient`
    is inert until it is entered -- so the production wiring can be proven
    offline even though the chair it names could only be started on a card.
    """
    run_root = fresh_tree(live_run, tmp_path)
    context = open_live_context(live_run, run_root)
    identity = context.registry.resolve("attestator_3")

    client = attestatores.default_serving_factory(context, identity, TIER)

    assert isinstance(client, ChairClient)
    # Reaching into the client for its manager: the whole claim of this test is
    # about what the factory bound, and there is no public accessor for it.
    manager = client._manager
    assert manager.registry is context.registry
    assert manager.receipt_publisher.context is context
    assert manager.config_inputs == ServingConfigInputs.from_record(
        dict(context.serving_config_inputs)
    )
    assert manager.recipes.source_sha256 == load_serving_recipes(live_run.catalogue).source_sha256
    # And it is inert: no service exists until the pass enters the client.
    with pytest.raises(Exception, match="enter it as a context manager"):
        assert client.handle is None


def test_the_live_preflight_refuses_to_leave_a_sealed_pair_unresolved(live_run, tmp_path):
    """The guard behind the live resolver, exercised where it can actually fire.

    `live_attempt_pass` reuses every pair sealed at this ordinal, so a pending
    pair never meets one in the pass itself. Called with the resolver but
    without that reuse -- the shape a future caller could get wrong -- the
    preflight refuses rather than carrying a sentinel into publication.
    """
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    context = open_live_context(live_run, run_root)
    acts = attestatores.expected_acts(context)
    index = attestatores._attempt_history(context)
    with pytest.raises(FatalAccounting, match="unresolved"):
        attestatores.preflight_appendable_ordinals(
            context,
            acts,
            1,
            attestatores.declarations_for(context, 1),
            index,
            resume_incomplete_pass=False,
            resolve=attestatores.pending_live_attempt,
        )


@pytest.mark.parametrize(
    ("adapter", "stop", "message"),
    [
        ("chandra.v1", "abort", "never measured a meaning for"),
        ("churro.v1", "unreported", "reconcile"),
    ],
)
def test_a_stop_word_that_cannot_be_recorded_honestly_refuses_before_publication(
    adapter, stop, message
):
    """The two live stop-word refusals, at the boundary that owns them.

    The shared capture contract checks the transport word only for `churro.v1`,
    so an unreadable engine word from any other adapter would otherwise travel
    into a record unexamined; and an unreported word is refused for Churro alone,
    because its page contract is the one that cannot hold the third truncation
    state.
    """
    capture = {
        "schema": "attestatores-model-view.v1",
        "adapter": adapter,
        "view": {},
        "raw_response_ref": {"relative_path": "3_attestatores/blobs/sha256/x", "sha256": "x"},
        "transport_stop_reason": stop,
        "stop_reason": stop,
        "findings": [],
        "parse": {"state": "parsed", "parser": "json", "text": ""},
    }
    with pytest.raises(ContractError, match=message):
        attestatores.refuse_unpublishable_stop_word(adapter, capture, "the response for page 1")
