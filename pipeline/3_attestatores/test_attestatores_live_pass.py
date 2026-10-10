"""The live Attestatores pass: selection, chair-outer serving, and the resume rule.

Every test here runs offline against `operations/serving/fakes.py`. Nothing
starts a pod, contacts a provider or loads a model: a scripted `FakeEndpoint`
answers one chair at a time behind a real `ServingManager`, a real
`ChairClient`, and this stage's own `main`, over a run tree carried to the
Designator by the real upstream stage programs.

**The roster here is the committed one, complete**: three page-scoped chairs.
Chandra and Churro answer once per page; DAI answers once per record its own
detector found, and the Designator's detector runs on its fixture row.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import load_stage, programs_through

STAGE = Path(__file__).resolve().parent
ROOT = STAGE.parents[1]
if str(STAGE) not in sys.path:
    sys.path.insert(0, str(STAGE))

import feeding  # noqa: E402

from common import chandra_layout  # noqa: E402
from common.chairs.models import ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.serving import (  # noqa: E402
    CHAIR_CALL_RECORD_SCHEMA,
    CHAIR_STREAM_CALL_RECORD_SCHEMA,
)
from common.contracts.stages import ATTESTATORES  # noqa: E402
from common.decoding import load_decoding_policy  # noqa: E402
from common.request_capacity import (  # noqa: E402
    DECLARED_ANSWER_BOUND_TOKENS,
    RequestCapacityRefusal,
)
from common.runtree.store import RunTree  # noqa: E402
from operations.serving import assembly  # noqa: E402
from operations.serving.assembly import retain_chair_bytes  # noqa: E402
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
    scripted_prompt_too_long,
    shipped_decoding_policy,
)
from operations.serving.manager import (  # noqa: E402
    ServingManager,
    StageContextReceiptPublisher,
)
from operations.serving.residency import FileResidencyLease  # noqa: E402

TIER = "generic-48gb"
RUN_ID = "live"
LIVE_CHAIRS = ("attestator_1", "attestator_2", "attestator_3")
CATALOGUE_CHAIRS = LIVE_CHAIRS
FIXTURE_ROOT = ROOT / "proof"

# Chandra answers in its vendor's own layout grammar (`common/chandra_layout.py`,
# the answer `OCR_LAYOUT_PROMPT` asks for): top-level divs carrying a
# `data-bbox` normalized 0-1000 against the sealed page. The boxes below convert
# by `chandra_layout.to_page_bounds` to the page-pixel rectangles (20,20 160x81)
# and (20,120 160x100) on page 1 and the matching block on page 2, over the
# fixture's ink. Only whitespace sits between the divs: text outside every
# top-level block would be a `content-outside-blocks` finding, which is the
# grammar reporting ink no block carries rather than anything this fixture means
# to say.
CHANDRA_PAGE_ONE = (
    '<div data-bbox="100 77 900 385" data-label="Text">'
    "SYNTHETIC ACT ONE alpha beta gamma</div>\n"
    '<div data-bbox="100 462 900 846" data-label="Text">'
    "SYNTHETIC ACT TWO delta epsilon zeta eta</div>"
)
CHANDRA_PAGE_TWO = (
    '<div data-bbox="100 77 900 308" data-label="Text">'
    "SYNTHETIC ACT TWO delta epsilon zeta eta</div>"
)
CHANDRA_BODY = CHANDRA_PAGE_ONE
# A body the layout grammar can place nothing in -- prose, which is what a model
# answering in its own markdown mode rather than the layout mode its prompt asks
# for produces. It is retained and refused by name (`no-layout-blocks`), never
# read.
CHANDRA_UNRECOGNIZED_BODY = "A real Chandra markdown body, with no layout block in it."
# Churro's answers in its `HistoricalDocument` grammar, one `Line` per line.
CHURRO_PAGE_ONE = (
    "<HistoricalDocument><Page><Body><Line>SYNTHETIC ACT ONE alpha beta</Line>"
    "<Line>SYNTHETIC ACT TWO delta epsiIon zeta eta</Line></Body></Page></HistoricalDocument>"
)
CHURRO_PAGE_TWO = (
    "<HistoricalDocument><Page><Body><Line>SYNTHETIC ACT TWO delta epsiIon zeta eta</Line>"
    "</Body></Page></HistoricalDocument>"
)
# Churro's answer in the vendor's own `HistoricalDocument` grammar -- the shape
# `churro.prompt` actually asks for. It carries no geometry, because the grammar
# has no coordinate vocabulary anywhere, which is why this chair's `observed` is
# the presented echo whatever it answers.
CHURRO_DOCUMENT_PAGE_ONE = (
    "<HistoricalDocument><Page><Body>"
    "<Line>SYNTHETIC ACT ONE alpha beta</Line>"
    "<Line>SYNTHETIC ACT TWO delta epsiIon zeta eta</Line>"
    "</Body></Page></HistoricalDocument>"
)
CHURRO_DOCUMENT_PAGE_TWO = (
    "<HistoricalDocument><Page><Body>"
    "<Line>SYNTHETIC ACT TWO delta epsiIon zeta eta</Line>"
    "</Body></Page></HistoricalDocument>"
)
# Well-formed XML rooted at an element the grammar names nowhere.
CHURRO_UNRECOGNIZED_BODY = "<transcription>a shape nobody asked this chair for</transcription>"
# DAI's parser is plain UTF-8 text (`feeding.validate_dai_text`), and it answers
# once per detector record: two records on page 1, one on page 2.
DAI_ACT_ONE = "SYNTHETIC ACT ONE alpha beta"
DAI_ACT_TWO = "SYNTHETIC ACT TWO delta epsiIon zeta eta"


attestatores = load_stage("3_attestatores")


# --------------------------- the sealed live catalogue ------------------------


# What each chair's stand-in row states, per chair, as the shipped real
# catalogue states it at its smallest tier. Chandra's `OCR_LAYOUT_PROMPT`
# (593) plus its dense-page answer (1,645) and one image token add to 2,239,
# which overruns a 2,048 context; Churro's system string (27) plus its
# vendor answer bound (25,000) and one image token add to 25,028, which
# overruns 2,048. The row moves to what the shipped catalogue states for these
# chairs at every tier (8,192; Churro 32,768)
# regardless, never the arithmetic or the pixels: this fixture mirrors the
# real catalogue's own row rather than deriving one from local arithmetic.
LIVE_ROW_CONTEXTS: dict[str, int] = {"attestator_1": 8192, "attestator_3": 32768}


def _vllm_row(
    *, recipe: str, chair: str, port: int, max_model_len: int | None = None
) -> dict[str, Any]:
    """One complete `kind = "vllm"` profile row, in the shape `config.py` closes.

    Mirrors `operations/serving/test_manager.py::profile_row` and the row
    `test_live_witness.py` already builds, because a live posture is exactly
    what those rows describe; the figures are test values and are never written
    into a committed catalogue.
    """
    if max_model_len is None:
        max_model_len = LIVE_ROW_CONTEXTS.get(chair, 2048)
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
        "max_model_len": max_model_len,
        "max_num_seqs": 1,
        "max_num_batched_tokens": 256,
        "gpu_memory_utilization": "0.85",
        "min_pixels": 1,
        "max_pixels": 1024,
        # The chair's own vision-encoder geometry, as the shipped real
        # catalogue states it: without it nothing can say what one image costs
        # this chair in prompt tokens, and the request builders refuse by name
        # rather than counting against a default (`common/request_capacity.py`).
        "patch_size": 14,
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


def write_live_catalogue(path: Path, registry, *, contexts: dict[str, int] | None = None) -> Path:
    """A serving catalogue whose witness rows are live, sealed into this run.

    The non-witness chairs keep fixture rows: this run's Designator (with its
    Surya detector) and Perlector are not what the Attestatores reads, and
    inventing live rows for them would put figures nobody measured beside
    chairs nothing starts.
    """
    rows: list[dict[str, Any]] = []
    for chair, recipe in (
        ("designator_surya", "fake-surya-v0"),
        ("secondary_proposer", "fake-secondary-proposer-v0"),
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
        row = _vllm_row(
            recipe=identity.serving_recipe,
            chair=chair,
            port=8000 + index,
            # A narrower context than the 2,048 the rest of this module uses is
            # how a request is made not to fit without touching a single pixel:
            # the sealed row is the only thing that decides it.
            max_model_len=(contexts or {}).get(chair),
        )
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


def committed_models_config() -> Path:
    """The committed roster, unedited: three configured witness chairs.

    The live pass is exercised against exactly the roster the repository
    ships; the assertions below are what would notice if that roster stopped
    describing the three page witnesses these tests exercise.
    """
    path = ROOT / "config" / "models.toml"
    chairs = tomllib.loads(path.read_text(encoding="utf-8"))["chairs"]
    assert chairs["attestator_2"]["state"] == "configured"
    assert chairs["attestator_2"]["witness_adapter"] == "dai.v1"
    assert chairs["attestator_2"]["witness_scope"] == "page"
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


def _carried_to_the_designator_boundary(
    work: Path, *, contexts: dict[str, int] | None = None
) -> SimpleNamespace:
    """One run walked to the Designator boundary under a live serving catalogue.

    The catalogue is sealed into the run by the upstream stages, so a run whose
    rows state a different context is a different run and has to be walked
    again from the Door -- which is why this is a helper two module fixtures
    call rather than one fixture with an argument.
    """

    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    catalogue = write_live_catalogue(
        work / "serving_recipes_live.toml", registry, contexts=contexts
    )
    models = committed_models_config()
    run_root = work / "runs"
    for program in programs_through("designator"):
        _invoke_stage(program, run_root=run_root, catalogue=catalogue, models=models)
    _policy, decoding_sha256 = load_decoding_policy(ROOT / "config" / "decoding.toml")
    return SimpleNamespace(
        work=work,
        catalogue=catalogue,
        models=models,
        run_root=run_root,
        decoding_sha256=decoding_sha256,
    )


@pytest.fixture(scope="module")
def live_run(tmp_path_factory) -> SimpleNamespace:
    """One run carried to the Designator boundary under a live serving catalogue.

    Built once: the four upstream stage programs are real subprocesses, and the
    Attestatores tests below each copy the finished tree so no test writes into
    another's evidence.
    """
    return _carried_to_the_designator_boundary(tmp_path_factory.mktemp("live-seam"))


# The two chairs whose rows cannot hold their own request in `refusing_run`,
# and the arithmetic that decides it. Every page in this fixture is 200x260,
# which at the test row's `max_pixels = 1024` costs one prompt token; what
# refuses is the prompt and the reserved answer, both measured constants
# (`common/request_capacity.py`). DAI reads one record crop per request: 1 + 84 + 230 = 315 against
# 256. Churro is page-scoped and reserves the vendor's whole answer bound
# (25,000): 1 + 27 + 25,000 = 25,028 against 512. Attestator 1 keeps the
# module's own 8,192 and needs 1 + 593 + 1,645 =
# 2,239 under the carried vendor prompt and answer, so its testimony proves
# the refusals were per request.
REFUSING_CONTEXTS = {"attestator_2": 256, "attestator_3": 512}
REFUSING_NEEDS = {"attestator_2": (315, 256), "attestator_3": (25028, 512)}


@pytest.fixture(scope="module")
def refusing_run(tmp_path_factory) -> SimpleNamespace:
    """The same run, under a catalogue two chairs' own requests cannot fit."""

    return _carried_to_the_designator_boundary(
        tmp_path_factory.mktemp("refusing-seam"), contexts=REFUSING_CONTEXTS
    )


# ------------------------------- the live world -------------------------------


def default_scripts() -> dict[str, list[ScriptedAnswer]]:
    """A chair's unit of work is its own, and the scripts say so.

    Chandra and Churro answer once per page -- two pages carry these two acts
    -- and DAI once per detector record. A script whose length disagreed with
    that would be the first thing to notice a unit regression, which is why
    they are written out rather than generated.
    """
    return {
        "attestator_1": [
            ScriptedAnswer(content=CHANDRA_PAGE_ONE, finish_reason="stop"),
            ScriptedAnswer(content=CHANDRA_PAGE_TWO, finish_reason="stop"),
        ],
        "attestator_2": [
            ScriptedAnswer(content=DAI_ACT_ONE, finish_reason="stop"),
            ScriptedAnswer(content=DAI_ACT_TWO, finish_reason="stop"),
            ScriptedAnswer(content=DAI_ACT_TWO, finish_reason="stop"),
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
            retain=lambda data: retain_chair_bytes(context, data),
            decoding_config_sha256=self.live_run.decoding_sha256,
            decoding_policy=shipped_decoding_policy()[0],
            read_receipt=lambda reference: context.tree.read_run_receipt(dict(reference)),
        )

    def requests(self, chair: str) -> list[dict[str, object]]:
        endpoint = self.endpoints.get(chair)
        return [] if endpoint is None else endpoint.requests


def refusing_factory(context, identity, tier):
    raise AssertionError(f"a chair was started when none should have been: {identity.role!r}")


def open_live_context(live_run: SimpleNamespace, run_root: Path):
    """A real `StageContext` over the live run, for the seams `main` composes."""
    parser = attestatores.stage_parser("live pass under test")
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
    return attestatores.open_stage_context(args, ATTESTATORES)


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


def page_records(tree: RunTree) -> dict[tuple[int, str], dict[str, Any]]:
    records: dict[tuple[int, str], dict[str, Any]] = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "page-testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        key = (record["payload"]["page_ordinal"], record["payload"]["chair"])
        assert key not in records, (
            f"page_records saw two page-Testimonia for {key}: "
            f"{records.get(key, {}).get('artifact_id')} "
            f"(ordinal {records.get(key, {}).get('payload', {}).get('attempt_ordinal')}) "
            f"vs {record.get('artifact_id')} "
            f"(ordinal {record['payload'].get('attempt_ordinal')}) -- these trees are a "
            "single ordinal-1 pass, so a second record here is either a duplicate "
            "publication or an unintended second attempt, and manifest hash order "
            "must not silently pick one over the other"
        )
        records[key] = record
    return records


# ================================ the live pass ===============================


def test_chandra_retries_retain_each_physical_request_but_publish_only_final_text(
    live_run, tmp_path
):
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    repeated = CHANDRA_PAGE_ONE + ("<!--repeat-->" * 24)
    scripts["attestator_1"] = [
        ScriptedAnswer(content=repeated, finish_reason="stop"),
        ScriptedAnswer(content=CHANDRA_PAGE_ONE, finish_reason="stop"),
        ScriptedAnswer(content=CHANDRA_PAGE_TWO, finish_reason="stop"),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)

    assert run_attestatores(live_run, run_root, factory=world.factory) == 0
    assert len(world.requests("attestator_1")) == 3
    assert [request["temperature"] for request in world.requests("attestator_1")] == [
        0.0,
        0.2,
        0.0,
    ]
    assert [request["top_p"] for request in world.requests("attestator_1")] == [
        0.1,
        0.95,
        0.1,
    ]
    assert all("seed" not in request for request in world.requests("attestator_1"))

    tree = RunTree(run_root, RUN_ID)
    page_one = page_records(tree)[(1, "attestator_1")]
    trace = page_one["payload"]["native_inference"]
    assert trace["physical_request_count"] == 2
    assert trace["returned_attempt_ordinal"] == 2
    assert [row["trigger"] for row in trace["attempts"]] == ["repeat-token", None]
    assert page_one["payload"]["payload"] == (
        "SYNTHETIC ACT ONE alpha beta gamma\nSYNTHETIC ACT TWO delta epsilon zeta eta"
    )
    capture_ref = page_one["payload"]["native_capture"]["raw_response_ref"]
    assert tree.read_bytes(capture_ref["relative_path"]).decode("utf-8") == CHANDRA_PAGE_ONE
    native = [
        entry
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] in {"chandra-native-attempt-intent", "chandra-native-attempt"}
    ]
    assert len([entry for entry in native if entry["kind"].endswith("intent")]) == 3
    assert len([entry for entry in native if entry["kind"] == "chandra-native-attempt"]) == 3


def test_chandra_terminal_reconciles_trigger_call_raw_and_receipt(live_run, tmp_path):
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0
    context = open_live_context(live_run, run_root)
    entries = [
        entry
        for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "chandra-native-attempt"
    ]
    entry = entries[0]
    record = context.tree.read_artifact(ATTESTATORES, entry["kind"], entry["artifact_id"])
    ordinal = record["payload"]["native_attempt_ordinal"]

    moved_trigger = copy.deepcopy(record)
    moved_trigger["payload"]["returned_condition"] = "inference-error"
    with pytest.raises(SchemaRefusal, match="retry trigger|trigger disagrees"):
        attestatores.chandra_native._validate_chandra_terminal(
            context,
            subject_id=entry["subject_id"],
            native_attempt_ordinal=ordinal,
            record=moved_trigger,
        )

    moved_call = copy.deepcopy(record)
    moved_call["payload"]["resolved_attempt"]["serving_call_ref"] = moved_call["payload"][
        "transport_response_ref"
    ]
    with pytest.raises(SchemaRefusal, match="serving call record"):
        attestatores.chandra_native._validate_chandra_terminal(
            context,
            subject_id=entry["subject_id"],
            native_attempt_ordinal=ordinal,
            record=moved_call,
        )

    moved_raw = copy.deepcopy(record)
    moved_raw["payload"]["resolved_attempt"]["raw_response_ref"] = moved_raw["payload"][
        "transport_response_ref"
    ]
    with pytest.raises(SchemaRefusal, match="trigger disagrees|model output"):
        attestatores.chandra_native._validate_chandra_terminal(
            context,
            subject_id=entry["subject_id"],
            native_attempt_ordinal=ordinal,
            record=moved_raw,
        )

    moved_receipt = copy.deepcopy(record)
    moved_receipt["payload"]["resolved_attempt"]["receipt_ref"] = moved_receipt["payload"][
        "transport_response_ref"
    ]
    with pytest.raises(SchemaRefusal, match="serving call record moved"):
        attestatores.chandra_native._validate_chandra_terminal(
            context,
            subject_id=entry["subject_id"],
            native_attempt_ordinal=ordinal,
            record=moved_receipt,
        )

    call_ref = record["payload"]["resolved_attempt"]["serving_call_ref"]
    call = json.loads(context.tree.read_bytes(call_ref["relative_path"]))
    call["receipt_ref"] = record["payload"]["transport_response_ref"]
    moved_call_receipt = copy.deepcopy(record)
    moved_call_receipt["payload"]["resolved_attempt"]["serving_call_ref"] = retain_chair_bytes(
        context, json.dumps(call, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    with pytest.raises(SchemaRefusal, match="serving call record moved"):
        attestatores.chandra_native._validate_chandra_terminal(
            context,
            subject_id=entry["subject_id"],
            native_attempt_ordinal=ordinal,
            record=moved_call_receipt,
        )


def test_chandra_orphan_intent_fails_closed_without_reissuing(live_run, tmp_path, monkeypatch):
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)

    def crash_after_call_record(*_args, **_kwargs):
        raise RuntimeError("simulated crash after response and call record")

    monkeypatch.setattr(
        attestatores.chandra_native, "_publish_chandra_terminal", crash_after_call_record
    )
    with pytest.raises(RuntimeError, match="after response and call record"):
        run_attestatores(live_run, run_root, factory=world.factory)
    monkeypatch.undo()

    tree = RunTree(run_root, RUN_ID)
    native = [
        entry
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] in {"chandra-native-attempt-intent", "chandra-native-attempt"}
    ]
    assert [entry["kind"] for entry in native] == ["chandra-native-attempt-intent"]
    assert len(world.requests("attestator_1")) == 1

    resumed = LiveWorld(live_run, tmp_path / "resumed")
    with pytest.raises(SchemaRefusal, match="delivery is unknown"):
        run_attestatores(live_run, run_root, factory=resumed.factory)
    assert resumed.requests("attestator_1") == []
    assert [
        entry
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] in {"chandra-native-attempt-intent", "chandra-native-attempt"}
    ] == native


def test_chandra_error_terminal_resume_waits_full_backoff_before_next_request(
    live_run, tmp_path, monkeypatch
):
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_1"] = [
        ScriptedAnswer(content="overloaded", finish_reason="stop", status=503)
    ]
    interrupted = LiveWorld(live_run, tmp_path / "interrupted", scripts)

    def crash_during_backoff(_seconds):
        raise RuntimeError("simulated crash during native error backoff")

    monkeypatch.setattr(attestatores.chandra_native.time, "sleep", crash_during_backoff)
    with pytest.raises(RuntimeError, match="during native error backoff"):
        run_attestatores(live_run, run_root, factory=interrupted.factory)
    assert len(interrupted.requests("attestator_1")) == 1

    delays: list[int] = []
    monkeypatch.setattr(attestatores.chandra_native.time, "sleep", delays.append)
    resumed_scripts = default_scripts()
    resumed_scripts["attestator_1"] = [
        ScriptedAnswer(content=CHANDRA_PAGE_ONE, finish_reason="stop"),
        ScriptedAnswer(content=CHANDRA_PAGE_TWO, finish_reason="stop"),
    ]
    resumed = LiveWorld(live_run, tmp_path / "resumed", resumed_scripts)
    assert run_attestatores(live_run, run_root, factory=resumed.factory) == 0
    assert delays == [2]
    assert [request["temperature"] for request in resumed.requests("attestator_1")] == [0.2, 0.0]


def test_chandra_pages_read_side_by_side_seal_in_page_order_and_resume(
    live_run, tmp_path, monkeypatch
):
    """Page 2 finishes its whole retry loop before page 1 sends anything, yet every
    Chandra record is written in page order, and a pass stopped inside page 1's
    loop resumes it at the next native attempt."""
    run_root = fresh_tree(live_run, tmp_path)
    repeated = CHANDRA_PAGE_ONE + ("<!--repeat-->" * 24)
    scripts = default_scripts()
    # Arrival order: page 2's two attempts, then page 1's first.
    scripts["attestator_1"] = [
        ScriptedAnswer(content=repeated, finish_reason="stop"),
        ScriptedAnswer(content=CHANDRA_PAGE_TWO, finish_reason="stop"),
        ScriptedAnswer(content=repeated, finish_reason="stop"),
    ]
    world = LiveWorld(live_run, tmp_path / "interrupted", scripts)

    # The sealed test row holds one sequence; Chandra's chair gets two here.
    resident: dict[str, str] = {}
    real_window = attestatores.in_order_window

    def factory(context, identity, tier):
        resident["chair"] = identity.role
        return world.factory(context, identity, tier)

    def window(width, jobs):
        return real_window(2 if resident.get("chair") == "attestator_1" else width, jobs)

    # Page 1 waits for page 2's whole loop, which only a window wider than one allows.
    real_read = attestatores.chandra_native.read_page
    page_two_read = threading.Event()

    def page_two_first(context, page, *, page_ordinal, **kwargs):
        if page_ordinal == 1:
            assert page_two_read.wait(timeout=10), "Chandra pages were not read side by side"
        read = real_read(context, page, page_ordinal=page_ordinal, **kwargs)
        if page_ordinal == 2:
            page_two_read.set()
        return read

    real_intent = attestatores.chandra_native._chandra_intent_record

    def stop_before_page_one_retry(context, **fields):
        if fields["page_ordinal"] == 1 and fields["native_attempt_ordinal"] == 2:
            raise RuntimeError("simulated stop inside page 1's retry loop")
        return real_intent(context, **fields)

    written: list[tuple[str, str, bool]] = []
    real_publish = RunTree.publish_artifact

    def recording_publish(tree, envelope):
        written.append(
            (
                envelope["kind"],
                envelope["subject_id"],
                threading.current_thread() is threading.main_thread(),
            )
        )
        return real_publish(tree, envelope)

    monkeypatch.setattr(attestatores, "in_order_window", window)
    monkeypatch.setattr(attestatores.chandra_native, "read_page", page_two_first)
    monkeypatch.setattr(
        attestatores.chandra_native, "_chandra_intent_record", stop_before_page_one_retry
    )
    monkeypatch.setattr(RunTree, "publish_artifact", recording_publish)
    with pytest.raises(RuntimeError, match="inside page 1's retry loop"):
        run_attestatores(live_run, run_root, factory=factory)
    monkeypatch.undo()

    tree = RunTree(run_root, RUN_ID)
    page_two = page_records(tree)[(2, "attestator_1")]
    page_of = {page_two["subject_id"]: 2}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] == "chandra-native-attempt-intent":
            intent = tree.read_artifact(ATTESTATORES, entry["kind"], entry["artifact_id"])
            page_of[entry["subject_id"]] = intent["payload"]["page_ordinal"]
    chandra_kinds = {"chandra-native-attempt-intent", "chandra-native-attempt"}
    assert all(main for kind, _subject, main in written if kind in chandra_kinds)
    assert [
        (kind, page_of[subject])
        for kind, subject, _main in written
        if kind in chandra_kinds or (kind == "page-testimonium" and subject in page_of)
    ] == [
        ("chandra-native-attempt-intent", 1),
        ("chandra-native-attempt", 1),
        ("chandra-native-attempt-intent", 2),
        ("chandra-native-attempt", 2),
        ("chandra-native-attempt-intent", 2),
        ("chandra-native-attempt", 2),
        ("page-testimonium", 2),
    ]
    assert page_two["payload"]["native_inference"]["physical_request_count"] == 2

    resumed_scripts = default_scripts()
    resumed_scripts["attestator_1"] = [
        ScriptedAnswer(content=CHANDRA_PAGE_ONE, finish_reason="stop")
    ]
    resumed = LiveWorld(live_run, tmp_path / "resumed", resumed_scripts)
    assert run_attestatores(live_run, run_root, factory=resumed.factory) == 0
    # Only page 1's next native attempt is sent; its first is not repeated.
    assert [request["temperature"] for request in resumed.requests("attestator_1")] == [0.2]
    trace = page_records(tree)[(1, "attestator_1")]["payload"]["native_inference"]
    assert [row["trigger"] for row in trace["attempts"]] == ["repeat-token", None]


def test_chandra_post_response_refusal_is_terminal_and_reproduced_on_resume(
    live_run, tmp_path, monkeypatch
):
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_1"] = [ScriptedAnswer(content=CHANDRA_PAGE_ONE, finish_reason="stop")]
    real_capture = attestatores.live_witness.captured_page_attempt

    def refusing_capture(context, page_ordinal, chair, *args, **kwargs):
        if chair == "attestator_1":
            raise ContractError("simulated post-response refusal")
        return real_capture(context, page_ordinal, chair, *args, **kwargs)

    monkeypatch.setattr(attestatores.live_witness, "captured_page_attempt", refusing_capture)
    first = LiveWorld(live_run, tmp_path / "first", scripts)
    with pytest.raises(ContractError, match="simulated post-response refusal"):
        run_attestatores(live_run, run_root, factory=first.factory)
    assert len(first.requests("attestator_1")) == 1
    monkeypatch.undo()

    tree = RunTree(run_root, RUN_ID)
    assert any(
        entry["kind"] == "chandra-native-attempt"
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
    )
    resumed = LiveWorld(live_run, tmp_path / "resumed", {"attestator_1": []})
    with pytest.raises(ContractError, match="simulated post-response refusal") as caught:
        run_attestatores(live_run, run_root, factory=resumed.factory)
    assert "delivery is unknown" not in str(caught.value)
    assert resumed.requests("attestator_1") == []


def test_chandra_retry_retains_post_response_refusal_in_earlier_terminal(live_run, tmp_path):
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    repeated = CHANDRA_PAGE_ONE + ("<!--repeat-->" * 24)
    scripts["attestator_1"] = [
        ScriptedAnswer(content=repeated, finish_reason="unmeasured-stop"),
        ScriptedAnswer(content=CHANDRA_PAGE_ONE, finish_reason="stop"),
        ScriptedAnswer(content=CHANDRA_PAGE_TWO, finish_reason="stop"),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)

    assert run_attestatores(live_run, run_root, factory=world.factory) == 0
    assert len(world.requests("attestator_1")) == 3

    tree = RunTree(run_root, RUN_ID)
    terminals = [
        tree.read_artifact(ATTESTATORES, entry["kind"], entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "chandra-native-attempt"
    ]
    refused = [record for record in terminals if record["payload"]["trigger"] == "repeat-token"]
    assert len(refused) == 1
    first_attempt = refused[0]["payload"]["resolved_attempt"]
    assert first_attempt["outcome"] == "failed"
    assert "'unmeasured-stop'" in first_attempt["reason"]
    assert "retained and not read" in first_attempt["reason"]
    assert first_attempt["native_capture"] is None


def test_chandra_fatal_capture_accounting_stops_before_terminal_or_retry(
    live_run, tmp_path, monkeypatch
):
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    repeated = CHANDRA_PAGE_ONE + ("<!--repeat-->" * 24)
    scripts["attestator_1"] = [
        *[ScriptedAnswer(content=repeated, finish_reason="stop") for _ in range(7)]
    ]
    world = LiveWorld(live_run, tmp_path, scripts)

    def refuse_capture(*_args, **_kwargs):
        raise FatalAccounting("simulated fatal native-capture accounting")

    monkeypatch.setattr(attestatores.live_witness, "captured_page_attempt", refuse_capture)
    with pytest.raises(FatalAccounting, match="fatal native-capture accounting"):
        run_attestatores(live_run, run_root, factory=world.factory)
    assert len(world.requests("attestator_1")) == 1

    native_kinds = [
        entry["kind"]
        for entry in RunTree(run_root, RUN_ID).build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] in {"chandra-native-attempt-intent", "chandra-native-attempt"}
    ]
    assert native_kinds == ["chandra-native-attempt-intent"]


def test_chandra_error_exhaustion_is_failed_and_records_every_backoff(
    live_run, tmp_path, monkeypatch
):
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_1"] = [
        *[
            ScriptedAnswer(content=f"error-{ordinal}", finish_reason="stop", status=503)
            for ordinal in range(1, 8)
        ],
        ScriptedAnswer(content=CHANDRA_PAGE_TWO, finish_reason="stop"),
    ]
    delays: list[int] = []
    monkeypatch.setattr(attestatores.chandra_native.time, "sleep", delays.append)
    world = LiveWorld(live_run, tmp_path, scripts)

    assert run_attestatores(live_run, run_root, factory=world.factory) == 0
    assert delays == [2, 4, 6, 8, 10, 12]
    page = page_records(RunTree(run_root, RUN_ID))[(1, "attestator_1")]
    assert page["outcome"] == "failed"
    trace = page["payload"]["native_inference"]
    assert trace["physical_request_count"] == 7
    assert trace["exhausted_condition"] == "inference-error"
    assert all(row["error"] is True for row in trace["attempts"])


def test_dai_records_each_record_crop_prompt_and_generation_view(live_run, tmp_path):
    """The DAI arm of the live pass, end to end through the real adapter.

    Each detector record DAI reads keeps its closed model view on the page
    Testimonium: the exact crop it was shown, the exact carried prompt bytes,
    and the carried generation config, all named by digest-checked references.
    The identity transform is the ordinary case here -- these record crops need
    no resize -- so the source and model images are one set of bytes under the
    two stage-owned paths that legitimately hold them.
    """
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    page = page_records(tree)[(1, "attestator_2")]["payload"]
    capture = page["unit_captures"][0]
    view = capture["view"]
    assert capture["adapter"] == "dai.v1"
    assert view["adapter"] == "dai-atr.v2"
    assert view["generation_accounting"] == feeding.dai_generation_accounting()
    # `feeding.dai_model_view` already refuses either mismatched state (a
    # resize whose digests still agree, or a claimed identity whose digests
    # differ -- feeding.py), so a persisted view's kind and digest relation
    # can never disagree with each other; asserting on `kind` alone would be
    # tautological. These record crops need no resize, so pin the identity case
    # directly: no resampler, one set of bytes, under the two stage-owned
    # paths that legitimately hold them.
    assert view["transform"]["kind"] == "identity"
    assert view["transform"]["resampler"] is None
    assert view["source_image_ref"]["sha256"] == view["model_image_ref"]["sha256"]
    assert view["source_image_ref"]["relative_path"] != view["model_image_ref"]["relative_path"]
    # Every reference in the closed view names real bytes in this run's tree.
    for reference in (
        view["source_image_ref"],
        view["model_image_ref"],
        view["prompts"]["system"],
        view["prompts"]["query"],
        view["generation_config_ref"],
    ):
        assert tree.read_bytes(reference["relative_path"])
    prompt = feeding.dai_prompt()
    assert tree.read_bytes(view["prompts"]["system"]["relative_path"]).decode() == prompt["system"]
    assert tree.read_bytes(view["prompts"]["query"]["relative_path"]).decode() == prompt["user"]
    # And the call record carries the vendor's own declared values, floats
    # included, beside everything that actually went on the wire: DAI's sealed
    # sampling row, the bound derived from the sealed serving row, and the
    # second EOS id sent as well as read by the engine from the pinned file.
    call = json.loads(tree.read_bytes(page["unit_call_refs"][0]["relative_path"]))
    declared = feeding.dai_generation()
    assert set(call["generation_sent"]) == {
        "repetition_penalty",
        "top_k",
        "top_p",
        "min_p",
        "max_tokens",
        "stop_token_ids",
        "seed",
        "temperature",
    }
    assert call["generation_sent"]["stop_token_ids"] == [151643]
    assert call["generation_sent"]["seed"] == 7
    assert call["generation_sent"]["temperature"] == {"schema": "wire-decimal.v1", "decimal": "0.1"}
    # DAI's declared ceiling (1,024) is strictly below what any shipped row
    # leaves after its image and prompt tokens, so it is always the vendor
    # bound that binds here, exactly -- never merely an upper bound on it.
    assert call["generation_sent"]["max_tokens"] == DECLARED_ANSWER_BOUND_TOKENS["attestator_2"]
    assert call["generation_declared"]["repetition_penalty"] == {
        "schema": "wire-decimal.v1",
        "decimal": json.dumps(declared["repetition_penalty"]),
    }
    assert call["generation_declared"]["do_sample"] is True


def test_capacity_refusal_attempt_declares_the_refused_chairs_own_format_capabilities():
    """A pre-send refusal never reaches the chair, but the chair still has a
    grammar, and `format_capabilities` is a fact about that grammar rather than
    about whether this one request fit the row. Exercised directly against
    `capacity_refusal_attempt` because the fact under test is local to it.
    """

    error = RequestCapacityRefusal("too many image tokens for this row")
    receipt_ref = {"relative_path": "receipts/x", "sha256": "a" * 64}

    # No adapter in hand: the blanket default.
    bare = attestatores.capacity_refusal_attempt(
        error, receipt_ref=receipt_ref, what="the test request"
    )
    assert bare.format_capabilities == attestatores.DEFAULT_FORMAT_CAPABILITIES

    # An adapter that names its own grammar: that value, not the default.
    declared = {"can_express_uncertainty": True, "can_express_layout": True}
    adapter = SimpleNamespace(format_capabilities=declared)
    named = attestatores.capacity_refusal_attempt(
        error, receipt_ref=receipt_ref, what="the test request", adapter=adapter
    )
    assert named.format_capabilities == declared
    assert named.outcome == "failed"


def test_capacity_refusal_attempt_refuses_a_malformed_adapter_declaration():
    """A declaration that is not the two-key boolean object is this seam's own
    bug -- a broken adapter, not a broken response -- and is refused by name
    rather than silently recorded."""

    error = RequestCapacityRefusal("too many image tokens for this row")
    receipt_ref = {"relative_path": "receipts/x", "sha256": "a" * 64}
    adapter = SimpleNamespace(format_capabilities={"can_express_layout": "yes"})

    with pytest.raises(SchemaRefusal, match="format_capabilities"):
        attestatores.capacity_refusal_attempt(
            error, receipt_ref=receipt_ref, what="the test request", adapter=adapter
        )


def test_a_captured_pages_own_format_capabilities_reaches_its_testimonium(
    live_run, tmp_path, monkeypatch
):
    """The captured attempt's declared value must reach the sealed page record.

    `True`/`True` differs from `run.py`'s `DEFAULT_FORMAT_CAPABILITIES`
    (`False`/`False`), so `captured_page_attempt` is wrapped to hand back the
    same `Attempt` with that non-default value, exactly as if it had read it off
    a declaring adapter -- proving the write is not hardcoded.
    """

    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path, default_scripts())
    declared = {"can_express_uncertainty": True, "can_express_layout": True}
    real_captured_page_attempt = attestatores.live_witness.captured_page_attempt

    def relabeled(*args, **kwargs):
        attempt = real_captured_page_attempt(*args, **kwargs)
        return attempt._replace(format_capabilities=declared)

    monkeypatch.setattr(attestatores.live_witness, "captured_page_attempt", relabeled)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    # attestator_3 (churro.v1) is page-scoped and served live, so its page
    # record is derived from exactly the `Attempt` this wrapper relabeled.
    page = page_records(tree)[(1, "attestator_3")]["payload"]
    assert page["format_capabilities"] == declared
    assert page["format_capabilities"] != attestatores.DEFAULT_FORMAT_CAPABILITIES


@pytest.mark.parametrize("chair", ["attestator_3", "attestator_2"])
def test_a_page_record_its_readers_would_refuse_is_never_published(
    live_run, tmp_path, monkeypatch, chair
):
    """Both page-record writers check the record exactly as every reader will,
    before it becomes immutable: one whose presentation names another page is
    refused, and nothing is sealed for it."""

    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    real_payload = attestatores.page_testimonium_payload

    def misattributed(**fields):
        payload = real_payload(**fields)
        if payload["chair"] == chair and payload["page_ordinal"] == 1:
            payload = {**payload, "page_ordinal": 2}
        return payload

    monkeypatch.setattr(attestatores, "page_testimonium_payload", misattributed)
    with pytest.raises(SchemaRefusal, match="names a different page"):
        run_attestatores(live_run, run_root, factory=world.factory)

    assert (1, chair) not in page_records(RunTree(run_root, RUN_ID))
    assert (2, chair) not in page_records(RunTree(run_root, RUN_ID))


def test_a_prompt_too_long_400_at_the_page_unit_still_stops_the_stage(live_run, tmp_path):
    """The other half of the boundary: a wire refusal is not a per-attempt hold.

    Two refusals reach `_serve_page_unit` and only one of them is this stage's
    to absorb. A *pre-send* capacity refusal is arithmetic about a request that
    never left, and it becomes this attempt's failure. An HTTP 400 is the
    engine's own refusal of a request that did leave: bytes arrived from a
    chair that was asked, and this stage's contract for a serving refusal is to
    stop and say so with those bytes retained, not to publish a Testimonium
    about a response it decided to overlook.
    """

    run_root = fresh_tree(live_run, tmp_path)
    refusal = scripted_prompt_too_long(
        max_model_len=2048,
        # The seam's own arithmetic for a 300-dpi page at this row, under the
        # layout instruction: 2,280 image + 441 prompt + a 1,631-token
        # dense-page answer.
        requested_tokens=4352,
        prompt_tokens=2721,
        completion_tokens=1631,
    )
    scripts = dict(default_scripts())
    scripts["attestator_3"] = [refusal, refusal]
    world = LiveWorld(live_run, tmp_path, scripts)

    with pytest.raises(ContractError, match="a live witness reading was refused"):
        run_attestatores(live_run, run_root, factory=world.factory)

    tree = RunTree(run_root, RUN_ID)
    # The engine's own diagnostic is on disk before the stage stopped, by its
    # own digest, which is the artefact a rented card would have been paying
    # for.
    assert _RunTreeBlobs(SimpleNamespace(tree=tree)).has(hashlib.sha256(refusal.body).hexdigest())
    # No page Testimonium for the page it refused: nothing was published about
    # a response this stage would not read.
    assert (1, "attestator_3") not in page_records(tree)


def test_a_second_live_pass_keeps_the_churro_page_it_sealed(live_run, tmp_path):
    """A sealed page record stands; a second pass at its ordinal neither re-asks
    nor republishes it. Churro's page carries only the presentation echo,
    because `HistoricalDocument` publishes no coordinates.

    The second pass runs against a `refusing_factory`: a live chair cannot reproduce
    immutable bytes, so a resume that started one would already be wrong, and
    the factory is itself part of the assertion.
    """
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_3"] = [
        ScriptedAnswer(content=CHURRO_DOCUMENT_PAGE_ONE, finish_reason="stop"),
        ScriptedAnswer(content=CHURRO_DOCUMENT_PAGE_TWO, finish_reason="stop"),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0
    sealed = page_records(RunTree(run_root, RUN_ID))

    assert run_attestatores(live_run, run_root, factory=refusing_factory) == 0
    republished = page_records(RunTree(run_root, RUN_ID))

    assert republished == sealed
    page_one = republished[(1, "attestator_3")]["payload"]
    assert page_one["payload"] == (
        "SYNTHETIC ACT ONE alpha beta\nSYNTHETIC ACT TWO delta epsiIon zeta eta"
    )
    assert [box["bounds_source"] for box in page_one["observed"]] == ["presented"]


def test_the_pass_names_the_fixture_witness_rows_its_posture_does_not_read(
    live_run, tmp_path, capsys
):
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    reported = capsys.readouterr().err
    assert "does not read" in reported
    assert "testimony" in reported and "churro_page_response" in reported


# ============================ selection and refusals ==========================


def test_witness_serving_modes_reads_the_sealed_row_kind_for_every_chair(live_run):
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
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    mixed = write_mixed_catalogue(tmp_path / "mixed.toml", registry)
    context = SimpleNamespace(witness_chairs=list(CATALOGUE_CHAIRS), registry=registry)
    with pytest.raises(ContractError, match="mixes serving postures"):
        attestatores.witness_serving_modes(context, load_serving_recipes(mixed), TIER)


def test_witness_serving_modes_refuses_a_live_chair_with_no_measured_placement_tier(live_run):
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    context = SimpleNamespace(witness_chairs=list(CATALOGUE_CHAIRS), registry=registry)
    with pytest.raises(ContractError, match="placement-tier"):
        attestatores.witness_serving_modes(context, load_serving_recipes(live_run.catalogue), None)


def test_bound_serving_recipes_refuses_a_catalogue_it_cannot_read(tmp_path):
    context = SimpleNamespace(
        args=SimpleNamespace(serving_recipes_config=str(tmp_path / "absent.toml")),
        serving_config_inputs={
            "schema": "serving-config-inputs.v2",
            "serving_recipes_sha256": "0" * 64,
            "pod_placement_sha256": "1" * 64,
        },
    )
    with pytest.raises(ContractError, match=r"refused for .*absent\.toml .*rerun with the files"):
        attestatores.bound_serving_recipes(context, context.args.serving_recipes_config)


def test_bound_serving_recipes_names_an_unreadable_placement_file(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "DEFAULT_POD_PLACEMENT_CONFIG_PATH", tmp_path / "absent.toml")
    context = SimpleNamespace(
        args=SimpleNamespace(serving_recipes_config=str(ROOT / "config" / "serving_recipes.toml")),
        serving_config_inputs={
            "schema": "serving-config-inputs.v2",
            "serving_recipes_sha256": "0" * 64,
            "pod_placement_sha256": "1" * 64,
        },
    )
    with pytest.raises(
        ContractError, match=r"refused for .*absent\.toml: cannot read placement table"
    ):
        attestatores.bound_serving_recipes(context, context.args.serving_recipes_config)


def test_a_live_dai_request_records_its_carried_float_generation_values(tmp_path):
    """DAI's shipped floats are recorded as the exact decimal text the wire carries.

    `chair-call-record.v1` is canonical JSON, which refuses a float outright,
    so this checks the recorded decimal against the bytes the endpoint
    actually received rather than the client's own values: a request recorded
    as something other than what was sent has no provenance, and
    rounding it would be the silent version of the same problem.
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
        witness_scope="page",
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
        decoding_config_sha256=shipped_decoding_policy()[1],
        decoding_policy=shipped_decoding_policy()[0],
        read_receipt=lambda reference: {
            "chair": identity.role,
            "source": identity.source,
            "resolved": identity.source_reference,
            "revision": identity.receipt_revision,
            "revision_kind": identity.receipt_revision_kind,
            "digest_manifest": identity.digest_manifest,
        },
    )
    declared = feeding.dai_generation()
    request = ChairRequest(
        kind="chat-completions",
        messages=({"role": "user", "content": [{"type": "text", "text": "read this"}]},),
        image_sha256s=(),
        generation_declared=declared,
        generation_sent={},
    )
    with client:
        endpoint.script(ScriptedAnswer(content="transcribed", finish_reason="stop"))
        response = client.read(request)

    record = json.loads(next(data for data in blob_store.written if data != response.raw_response))
    posted = endpoint.requests[0]
    for key in ("repetition_penalty", "top_p"):
        assert posted[key] == declared[key]
        assert record["generation_sent"][key] == {
            "schema": "wire-decimal.v1",
            "decimal": json.dumps(declared[key]),
        }
        assert float(record["generation_sent"][key]["decimal"]) == declared[key]
    # The declared view is recorded to the digit as well, beside what was sent.
    assert record["generation_declared"]["temperature"] == {
        "schema": "wire-decimal.v1",
        "decimal": json.dumps(declared["temperature"]),
    }
    assert posted["temperature"] == declared["temperature"]


def test_the_production_serving_factory_binds_the_run_that_will_record_the_reading(
    live_run, tmp_path
):
    """Construction only: the registry, receipts and catalogue are the run's own.

    Nothing here starts a process or opens a socket -- building a `ChairClient`
    is inert until it is entered -- so the production wiring can be proven
    offline even though the chair it names could only be started on a card.
    """
    run_root = fresh_tree(live_run, tmp_path)
    context = open_live_context(live_run, run_root)
    identity = context.registry.resolve("attestator_3")

    policy, decoding_sha256 = load_decoding_policy(ROOT / "config" / "decoding.toml")
    client = attestatores.production_serving_factory(policy, decoding_sha256)(
        context, identity, TIER
    )

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
    # The launch audit names the stage that ran it, not the library that
    # happens to construct the manager -- an operator reading two audits from
    # different stages must be able to tell them apart by this field alone.
    assert manager.producer == "pipeline/3_attestatores/run.py"
    # And it is inert: no service exists until the pass enters the client.
    with pytest.raises(Exception, match="enter it as a context manager"):
        assert client.handle is None
    # A chair response retained after the seal would falsify the witnessed blob inventory.
    context.sealed = True
    with pytest.raises(SchemaRefusal, match="witnessed blob inventory false"):
        client._retain(b"{}")


@pytest.mark.parametrize("word", [None, "stop", "length"])
def test_a_measured_or_absent_stop_word_leaves_the_response_to_be_read(word):
    response = SimpleNamespace(finish_reason=word)
    assert (
        attestatores.live_witness.unmeasured_stop_reason(response, "the response for page 1")
        is None
    )


# The fixture transport's own words are not words a served engine has been measured to send.
@pytest.mark.parametrize("word", ["abort", "eos", "max_new_tokens"])
def test_an_unmeasured_stop_word_is_named_as_the_reason_its_response_is_not_read(word):
    reason = attestatores.live_witness.unmeasured_stop_reason(
        SimpleNamespace(finish_reason=word), "the response for page 1"
    )
    assert reason is not None and repr(word) in reason and "not read" in reason


def test_a_churro_body_in_neither_declared_shape_is_retained_and_refused_by_name(
    live_run, tmp_path
):
    """A named surprise, not a parse failure and not a silent reading.

    What reaches `unrecognized-shape` is what the phrase means: the parser ran,
    read the whole response, and could name no shape it knows -- and the outcome
    says which root element arrived, so the surprise is named rather than merely
    counted. The bytes were retained before it ran.
    """
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_3"] = [
        ScriptedAnswer(content=CHURRO_UNRECOGNIZED_BODY, finish_reason="stop"),
        ScriptedAnswer(content=CHURRO_UNRECOGNIZED_BODY, finish_reason="stop"),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    record = page_records(tree)[(1, "attestator_3")]
    payload = record["payload"]
    assert record["outcome"] == "failed"
    parse = payload["native_capture"]["parse"]
    assert parse["state"] == "unrecognized-shape"
    assert parse["parser"] == "xml"
    assert "transcription" in parse["outcome"]
    assert payload["native_capture"]["stop_reason"] == "partial-parse-unrecognized-shape"
    assert (
        tree.read_bytes(payload["native_capture"]["raw_response_ref"]["relative_path"]).decode()
        == CHURRO_UNRECOGNIZED_BODY
    )


# ========================= the live pass, page by page =========================


def test_a_live_chair_uses_its_sequence_width_and_seals_pages_in_order(monkeypatch):
    chair = "attestator_3"
    resolved = SimpleNamespace(witness_adapter="churro.v1")
    context = SimpleNamespace(registry=SimpleNamespace(config={}, resolve=lambda _chair: resolved))
    client = SimpleNamespace(handle=SimpleNamespace(profile=SimpleNamespace(max_num_seqs=3)))
    active = most = 0
    returned: list[int] = []
    sealed: list[tuple[int, str]] = []
    lock = threading.Lock()
    first_three = threading.Barrier(3)
    third_returned = threading.Event()

    class ResidentChair:
        def __enter__(self):
            return client

        def __exit__(self, *_args):
            return None

    def read(_context, *, page_ordinal, **_kwargs):
        nonlocal active, most
        with lock:
            active += 1
            most = max(most, active)
        if page_ordinal <= 3:
            first_three.wait(timeout=5)
        if page_ordinal == 1:
            assert third_returned.wait(timeout=5)
        with lock:
            active -= 1
            returned.append(page_ordinal)
            if page_ordinal == 3:
                third_returned.set()
        return f"answer-{page_ordinal}"

    def seal(answer, *, page_ordinal, **_kwargs):
        assert threading.current_thread() is threading.main_thread()
        sealed.append((page_ordinal, answer))

    monkeypatch.setattr(attestatores, "page_witness_roster", lambda _context: [chair])
    monkeypatch.setattr(attestatores, "reads_detector_records", lambda _resolved: False)
    monkeypatch.setattr(attestatores, "_sealed_page_testimonia", lambda *_args: {})
    monkeypatch.setattr(attestatores.witness_adapters, "framing_for", lambda *_args: None)
    monkeypatch.setattr(
        attestatores.witness_adapters, "resolve_runnable_adapter", lambda _name: object()
    )
    monkeypatch.setattr(attestatores, "_read_page_unit", read)
    monkeypatch.setattr(attestatores, "witness_reply_guard", lambda *_args: None)
    monkeypatch.setattr(attestatores, "_serve_page_unit", seal)
    pages = [(page, f"p{page}") for page in range(1, 6)]

    assert (
        attestatores.live_pass(
            context,
            pages,
            1,
            page_ids=dict(pages),
            serving_factory=lambda *_args: ResidentChair(),
            tier=TIER,
        )
        == 5
    )
    assert most == 3
    assert returned.index(3) < returned.index(1)
    assert sealed == [(page, f"answer-{page}") for page in range(1, 6)]


def _record_reader_pass(monkeypatch, *, width: int, records: dict[int, int], read, seal):
    """Run the live pass over one record reader with the record call and the seal replaced."""
    chair = "attestator_2"
    resolved = SimpleNamespace(witness_adapter="dai.v1")
    context = SimpleNamespace(registry=SimpleNamespace(config={}, resolve=lambda _chair: resolved))
    client = SimpleNamespace(
        handle=SimpleNamespace(
            profile=SimpleNamespace(max_num_seqs=width), receipt_reference={"receipt": "r"}
        )
    )
    units = {
        page: [{"subject_id": f"p{page}-r{index}"} for index in range(count)]
        for page, count in records.items()
    }

    class ResidentChair:
        def __enter__(self):
            return client

        def __exit__(self, *_args):
            return None

    def record(_context, *, region, **_kwargs):
        return region, {"crop": region["subject_id"]}, read(region["subject_id"])

    def publish(_context, *, page_ordinal, served, **_kwargs):
        assert threading.current_thread() is threading.main_thread()
        seal(page_ordinal, [attempt for _region, _presented, attempt in served])

    monkeypatch.setattr(attestatores, "page_witness_roster", lambda _context: [chair])
    monkeypatch.setattr(attestatores, "reads_detector_records", lambda _resolved: True)
    monkeypatch.setattr(attestatores, "detector_units_by_page", lambda _context: (units, {}))
    monkeypatch.setattr(attestatores, "_sealed_page_testimonia", lambda *_args: {})
    monkeypatch.setattr(attestatores.witness_adapters, "framing_for", lambda *_args: None)
    monkeypatch.setattr(
        attestatores.witness_adapters, "resolve_runnable_adapter", lambda _name: object()
    )
    monkeypatch.setattr(attestatores, "_read_detector_record", record)
    monkeypatch.setattr(attestatores, "witness_reply_guard", lambda *_args: None)
    monkeypatch.setattr(attestatores, "publish_detector_page_testimonium", publish)
    pages = [(page, f"p{page}") for page in sorted(records)]
    return attestatores.live_pass(
        context,
        pages,
        1,
        page_ids=dict(pages),
        serving_factory=lambda *_args: ResidentChair(),
        tier=TIER,
    )


def test_a_record_readers_records_share_the_window_and_each_page_seals_once_whole(monkeypatch):
    active = most = 0
    lock = threading.Lock()
    # Page 1's three records and page 2's first are all out together before any returns.
    four_out = threading.Barrier(4)
    sealed: list[tuple[int, list[str]]] = []

    def read(subject):
        nonlocal active, most
        with lock:
            active += 1
            most = max(most, active)
        if subject in {"p1-r0", "p1-r1", "p1-r2", "p2-r0"}:
            four_out.wait(timeout=5)
        with lock:
            active -= 1
        return f"answer-{subject}"

    recorded = _record_reader_pass(
        monkeypatch,
        width=4,
        records={1: 3, 2: 2, 3: 1},
        read=read,
        seal=lambda page, attempts: sealed.append((page, attempts)),
    )

    assert recorded == 3
    assert most == 4
    assert sealed == [
        (1, ["answer-p1-r0", "answer-p1-r1", "answer-p1-r2"]),
        (2, ["answer-p2-r0", "answer-p2-r1"]),
        (3, ["answer-p3-r0"]),
    ]


def test_a_record_reader_interrupted_inside_a_page_seals_only_whole_pages(monkeypatch):
    sealed: list[int] = []
    page_one_sealed = threading.Event()

    def read(subject):
        if subject == "p2-r1":
            # Page 2's second record never answers before the pass is interrupted.
            page_one_sealed.wait(timeout=5)
            raise KeyboardInterrupt
        return f"answer-{subject}"

    def seal(page, _attempts):
        sealed.append(page)
        page_one_sealed.set()

    with pytest.raises(KeyboardInterrupt):
        _record_reader_pass(monkeypatch, width=3, records={1: 2, 2: 2}, read=read, seal=seal)

    # Page 1 is sealed whole; page 2 has a record unanswered, so nothing of it is sealed
    # and a resume asks both its records again.
    assert sealed == [1]


def test_a_live_roster_reads_each_chair_once_through_its_own_scope(live_run, tmp_path):
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)

    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    # One load per chair, chair by chair in a fixed order; a chair is never
    # loaded a second time.
    assert world.loads == sorted(LIVE_CHAIRS)
    # Two sealed pages, so a whole-page chair answers twice.
    assert len(world.requests("attestator_1")) == 2
    assert len(world.requests("attestator_3")) == 2
    # DAI is asked once per record its own detector found: two on page 1, one
    # on page 2.
    assert len(world.requests("attestator_2")) == 3

    tree = RunTree(run_root, RUN_ID)
    records = page_records(tree)
    # Every roster chair answers for every sealed page, and nothing else is kept.
    assert set(records) == {(page, chair) for page in (1, 2) for chair in LIVE_CHAIRS}
    assert {record["outcome"] for record in records.values()} == {"read"}
    kinds = {entry["kind"] for entry in tree.build_manifest(ATTESTATORES)["artifacts"]}
    assert "testimonium" not in kinds and "act-attachment" not in kinds
    # A whole-page chair names its one request; DAI names one per record.
    for (_page, chair), record in records.items():
        payload = record["payload"]
        if chair == "attestator_2":
            assert "serving_call_ref" not in payload
            assert all(payload["unit_call_refs"])
        else:
            assert payload["serving_call_ref"] in record["inputs"]


def test_chandra_trace_is_restricted_to_its_declared_chair_and_page_scope(live_run, tmp_path):
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0
    tree = RunTree(run_root, RUN_ID)

    page_payload = copy.deepcopy(page_records(tree)[(1, "attestator_1")]["payload"])
    page_payload["chair"] = "attestator_3"
    with pytest.raises(SchemaRefusal, match="belongs only to page-scoped attestator_1"):
        attestatores.validate_page_testimonium_payload(page_payload)


def _crash_once(monkeypatch, chair: str, page_ordinal: int) -> None:
    """Crash the pass at the write of one chair's record for one page."""
    real = attestatores.publish_page_testimonium

    def crashing(context, **kwargs):
        if kwargs["chair"] == chair and kwargs["page_ordinal"] == page_ordinal:
            raise RuntimeError(f"simulated crash before {chair}'s page {page_ordinal} sealed")
        return real(context, **kwargs)

    monkeypatch.setattr(attestatores, "publish_page_testimonium", crashing)


def test_exhausted_repeat_geometry_survives_a_crash_resume(live_run, tmp_path, monkeypatch):
    """Chandra's page is sealed when its loop returns, before any other chair runs.

    A crash after it leaves the exhausted page record as the loop sealed it,
    reported geometry included, and the resume asks Chandra nothing again.
    """
    run_root = fresh_tree(live_run, tmp_path)
    repeated = CHANDRA_PAGE_ONE + ("<!--repeat-->" * 24)
    scripts = default_scripts()
    scripts["attestator_1"] = [
        *[ScriptedAnswer(content=repeated, finish_reason="stop") for _ in range(7)],
        ScriptedAnswer(content=CHANDRA_PAGE_TWO, finish_reason="stop"),
    ]
    interrupted = LiveWorld(live_run, tmp_path / "interrupted", scripts)
    _crash_once(monkeypatch, "attestator_3", 1)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_attestatores(live_run, run_root, factory=interrupted.factory)
    monkeypatch.undo()

    sealed = page_records(RunTree(run_root, RUN_ID))[(1, "attestator_1")]
    assert sealed["outcome"] == "failed"
    assert sealed["payload"]["native_inference"]["exhausted_condition"] == "repeat-token"
    assert sealed["payload"]["native_inference"]["physical_request_count"] == 7
    assert any(
        observation["bounds_source"] in {"native", "derived"}
        for observation in sealed["payload"]["observed"]
    )

    resumed = LiveWorld(
        live_run, tmp_path / "resumed", {"attestator_3": default_scripts()["attestator_3"]}
    )
    assert run_attestatores(live_run, run_root, factory=resumed.factory) == 0
    assert resumed.requests("attestator_1") == []
    assert page_records(RunTree(run_root, RUN_ID))[(1, "attestator_1")] == sealed


def test_unparsed_exhausted_repeat_carries_no_geometry(live_run, tmp_path):
    run_root = fresh_tree(live_run, tmp_path)
    repeated_unrecognized = "x" * 17
    scripts = default_scripts()
    scripts["attestator_1"] = [
        *[ScriptedAnswer(content=repeated_unrecognized, finish_reason="stop") for _ in range(7)],
        ScriptedAnswer(content=CHANDRA_PAGE_TWO, finish_reason="stop"),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    page = page_records(RunTree(run_root, RUN_ID))[(1, "attestator_1")]
    assert page["outcome"] == "failed"
    assert page["payload"]["native_inference"]["physical_request_count"] == 7
    assert page["payload"]["native_capture"]["parse"]["state"] == "unrecognized-shape"
    assert {observation["bounds_source"] for observation in page["payload"]["observed"]} == {
        "presented"
    }


def test_a_wire_response_the_client_cannot_parse_at_all_fails_only_its_page(live_run, tmp_path):
    """A body `ChairClient` cannot shape into a reading at all (here, an
    OpenAI-shaped envelope with zero choices) is retained, never repaired, and
    the page it answered fails with no model view, since no adapter ran."""
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_3"] = [
        ScriptedAnswer(body=json.dumps({"model": "served-attestator_3", "choices": []}).encode()),
        ScriptedAnswer(content=CHURRO_PAGE_TWO, finish_reason="stop"),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    record = page_records(tree)[(1, "attestator_3")]
    assert record["outcome"] == "failed"
    assert "native_capture" not in record["payload"]
    assert record["payload"]["reason"]
    # The next page on the same chair, unaffected: one malformed reading does
    # not poison the rest of the roster.
    assert page_records(tree)[(2, "attestator_3")]["outcome"] == "read"


def test_a_request_the_sealed_row_cannot_hold_costs_that_page_and_not_the_pass(
    refusing_run, tmp_path
):
    """The Attestatores hold per request, exactly as the Designator already did.

    A pre-send capacity refusal becomes that page's own failure, and the pass
    carries on: one oversized request must not cost every other page's
    testimony. Both unit kinds at once: DAI reads record crops and Churro whole
    pages, their rows cannot hold their own requests (`REFUSING_NEEDS`), and
    Attestator 1's row can.
    """

    run_root = fresh_tree(refusing_run, tmp_path)
    scripts = dict(default_scripts())
    # Nothing is sent for either refused chair, so scripting an answer for one
    # would be an answer no request ever asked for.
    scripts["attestator_2"] = []
    scripts["attestator_3"] = []
    world = LiveWorld(refusing_run, tmp_path, scripts)

    assert run_attestatores(refusing_run, run_root, factory=world.factory) == 0

    # Both refused chairs were started -- the pass loads a chair before it can
    # ask it anything -- and neither was ever asked.
    assert world.loads == sorted(LIVE_CHAIRS)
    assert world.requests("attestator_2") == []
    assert world.requests("attestator_3") == []
    assert len(world.requests("attestator_1")) == 2

    tree = RunTree(run_root, RUN_ID)
    records = page_records(tree)
    for chair, (need, context) in REFUSING_NEEDS.items():
        for page in (1, 2):
            record = records[(page, chair)]
            payload = record["payload"]
            assert record["outcome"] == "failed", (page, chair)
            assert "was refused before it was sent" in payload["reason"]
            assert f"that is {need} against a max_model_len of {context}" in payload["reason"]
            # Nothing arrived, so there is no channel to call unrecordable and
            # no bytes to name.
            assert payload["content_health"]["recordable"] is None
            assert payload["payload"] is None
            assert "native_capture" not in payload
            assert not payload.get("raw_response_refs")
            # The serving moment is real: the chair started, and its receipt is
            # this run's own rather than a fixture stand-in.
            receipt = tree.read_run_receipt(payload["provenance"]["receipt_ref"])
            assert not receipt["endpoint"].startswith("fixture://")

    # And the chair whose row could hold its request is untouched: this is the
    # whole point of holding per request rather than per pass.
    assert records[(1, "attestator_1")]["outcome"] == "read"
    assert records[(2, "attestator_1")]["outcome"] == "read"


def test_a_pass_interrupted_after_a_refused_page_resumes_over_it(
    refusing_run, tmp_path, monkeypatch
):
    """A sealed page record of a request never sent is kept, never asked again."""
    run_root = fresh_tree(refusing_run, tmp_path)
    scripts = dict(default_scripts())
    scripts["attestator_2"] = []
    scripts["attestator_3"] = []
    world = LiveWorld(refusing_run, tmp_path, scripts)
    _crash_once(monkeypatch, "attestator_3", 2)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_attestatores(refusing_run, run_root, factory=world.factory)
    monkeypatch.undo()

    interrupted = page_records(RunTree(run_root, RUN_ID))
    assert interrupted[(1, "attestator_3")]["outcome"] == "failed"
    assert (2, "attestator_3") not in interrupted

    resumed = LiveWorld(refusing_run, tmp_path / "resumed", scripts)
    assert run_attestatores(refusing_run, run_root, factory=resumed.factory) == 0
    assert resumed.requests("attestator_3") == []
    finished = page_records(RunTree(run_root, RUN_ID))
    assert finished[(1, "attestator_3")] == interrupted[(1, "attestator_3")]
    assert finished[(2, "attestator_3")]["outcome"] == "failed"


def test_every_live_page_record_names_the_serving_moment_and_its_retained_response(
    live_run, tmp_path
):
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    record = page_records(tree)[(1, "attestator_3")]
    payload = record["payload"]

    # The receipt is the live one the client re-read at start, never the
    # declared `fixture://` stand-in `fixture_serving_details` writes.
    receipt = tree.read_run_receipt(payload["provenance"]["receipt_ref"])
    assert not receipt["endpoint"].startswith("fixture://")
    assert receipt["chair"] == "attestator_3"

    # The retained response is real, digest-checked bytes in this stage's
    # store, bound as an input of the record that names it.
    reference = payload["native_capture"]["raw_response_ref"]
    assert reference["relative_path"] == f"3_attestatores/blobs/sha256/{reference['sha256']}"
    attestatores.validate_retained_response_blob(tree, reference)
    assert reference in record["inputs"]
    assert payload["native_capture"]["transport_stop_reason"] == "stop"


@pytest.mark.parametrize(
    ("finish_reason", "truncated", "basis"),
    [("stop", False, "trusted-response-boundary"), ("length", True, "trusted-response-boundary")],
)
def test_the_engine_stop_word_decides_the_truncation_a_live_record_publishes(
    live_run, tmp_path, finish_reason, truncated, basis
):
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_3"] = [
        ScriptedAnswer(content=CHURRO_PAGE_ONE, finish_reason=finish_reason),
        ScriptedAnswer(content=CHURRO_PAGE_TWO, finish_reason=finish_reason),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    health = page_records(tree)[(1, "attestator_3")]["payload"]["content_health"]
    assert health["truncated"] is truncated
    assert health["truncation_basis"] == basis


def test_a_served_chandra_publishes_a_real_page_testimonium_with_its_own_geometry(
    live_run, tmp_path
):
    """Attestator 1 is a served Chandra witness like the others.

    Its page response parses under the vendor's own layout grammar, so the page
    record is a reading whose text is the block texts joined and whose observed
    geometry is each `data-bbox` converted to sealed-page pixels -- with a span
    into that text. The page record names the response once, through its
    capture, and does not repeat it in the partition list. The retained view
    carries the vendor's own declared answer bound beside the vendor's own
    prompt bytes, and the capture names the vendor pin those bytes came from.
    """
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    page = page_records(tree)[(1, "attestator_1")]
    payload = page["payload"]
    assert page["outcome"] == "read"
    assert payload["payload"] == (
        "SYNTHETIC ACT ONE alpha beta gamma\nSYNTHETIC ACT TWO delta epsilon zeta eta"
    )
    assert payload["native_capture"]["parse"]["state"] == "parsed"
    assert payload["native_capture"]["view"] == {
        "prompt": attestatores.chandra.prompt(),
        "generation": {"max_new_tokens": 12384},
    }
    assert payload["native_capture"]["vendor_identity"] == {
        "repository": "github.com/datalab-to/chandra",
        "sha": "d4f7467435aa4137d9539f000ddf0b7ced3eb43f",
        "carried_strings": {
            "OCR_LAYOUT_PROMPT": chandra_layout.OCR_LAYOUT_PROMPT_SHA256,
            "PROMPT_ENDING": chandra_layout.PROMPT_ENDING_SHA256,
        },
    }
    assert payload["observed"] == [
        {
            "ordinal": 0,
            "bounds": {"x": 20, "y": 20, "w": 160, "h": 81},
            "bounds_source": "native",
            "span": {"start": 0, "end": 34},
        },
        {
            "ordinal": 1,
            "bounds": {"x": 20, "y": 120, "w": 160, "h": 100},
            "bounds_source": "native",
            "span": {"start": 35, "end": 75},
        },
    ]
    assert "raw_response_refs" not in payload
    assert payload["native_capture"]["raw_response_ref"] in page["inputs"]
    assert tree.read_bytes(payload["native_capture"]["raw_response_ref"]["relative_path"]) == (
        CHANDRA_PAGE_ONE.encode("utf-8")
    )


def test_a_chandra_body_in_neither_declared_shape_is_retained_and_refused_by_name(
    live_run, tmp_path
):
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_1"] = [
        ScriptedAnswer(content=CHANDRA_UNRECOGNIZED_BODY, finish_reason="stop"),
        ScriptedAnswer(content=CHANDRA_UNRECOGNIZED_BODY, finish_reason="stop"),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    record = page_records(tree)[(1, "attestator_1")]
    payload = record["payload"]
    assert record["outcome"] == "failed"
    assert "no-layout-blocks" in payload["reason"]
    assert payload["content_health"]["recordable"] is False
    # The adapter's own account of the bytes rides along: it reached
    # `unrecognized-shape` -- the parser ran, read the whole body, and could
    # place no shape it knows -- and the bytes are retained beside it.
    assert payload["native_capture"]["parse"] == {
        "state": "unrecognized-shape",
        "parser": "html",
        "outcome": "no-layout-blocks",
    }
    assert (
        tree.read_bytes(payload["native_capture"]["raw_response_ref"]["relative_path"]).decode()
        == CHANDRA_UNRECOGNIZED_BODY
    )
    assert {item["bounds_source"] for item in payload["observed"]} == {"presented"}


def test_a_resumed_live_pass_asks_no_chair_again(live_run, tmp_path):
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0
    before = page_records(RunTree(run_root, RUN_ID))

    # The factory itself is the assertion: a live chair cannot reproduce
    # immutable bytes, so a resume that started one would already be wrong.
    assert run_attestatores(live_run, run_root, factory=refusing_factory) == 0
    assert page_records(RunTree(run_root, RUN_ID)) == before


def test_a_resumed_live_pass_uses_chandra_terminal_evidence_without_reissuing(
    live_run, tmp_path, monkeypatch
):
    """The crash the terminal evidence exists for: the vendor loop sealed, the page not.

    Chandra's page 2 call completes and seals its native terminal artifact, and
    the pass dies before the page record is written. The resumed Chandra route
    rebuilds the returned attempt from that terminal evidence and issues no
    HTTP call.
    """
    run_root = fresh_tree(live_run, tmp_path)
    crashed = LiveWorld(live_run, tmp_path / "crashed")
    _crash_once(monkeypatch, "attestator_1", 2)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_attestatores(live_run, run_root, factory=crashed.factory)
    monkeypatch.undo()
    assert len(crashed.requests("attestator_1")) == 2
    assert (2, "attestator_1") not in page_records(RunTree(run_root, RUN_ID))

    resumed = LiveWorld(live_run, tmp_path / "resumed", {**default_scripts(), "attestator_1": []})
    assert run_attestatores(live_run, run_root, factory=resumed.factory) == 0

    # The Chandra client is opened because its page 2 record is pending, but
    # the sealed native terminal artifact makes the physical call complete.
    assert resumed.requests("attestator_1") == []
    published = page_records(RunTree(run_root, RUN_ID))
    assert published[(2, "attestator_1")]["outcome"] == "read"
    assert (
        published[(2, "attestator_1")]["payload"]["native_inference"]["physical_request_count"] == 1
    )


@pytest.mark.parametrize("chair", ["attestator_1", "attestator_2", "attestator_3"])
def test_an_engine_stop_word_this_pipeline_cannot_read_fails_that_page_alone(
    live_run, tmp_path, chair
):
    """The answer is kept, not read and not defaulted to whole or cut off; the
    page's record says why, and every other page and chair is still read."""
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    first = scripts[chair][0]
    scripts[chair][0] = ScriptedAnswer(content=first.content, finish_reason="abort")
    world = LiveWorld(live_run, tmp_path, scripts)

    assert run_attestatores(live_run, run_root, factory=world.factory) == attestatores.EXIT_COMPLETE

    tree = RunTree(run_root, RUN_ID)
    records = page_records(tree)
    failed = records[(1, chair)]
    assert failed["outcome"] == "failed"
    payload = failed["payload"]
    assert "'abort'" in json.dumps(payload)
    # A whole page answer is not read at all; DAI's page joins its other records.
    assert payload["payload"] is None or chair == "attestator_2"
    # A whole-page chair's record is that one answer, kept unread: the same
    # unrecordable health DAI's unit attempt carries, its basis the reason.
    if chair != "attestator_2":
        assert payload["content_health"] == attestatores.unrecordable_health(payload["reason"])
    assert records[(2, chair)]["outcome"] == "read"
    assert all(
        records[(page, other)]["outcome"] == "read"
        for page in (1, 2)
        for other in {"attestator_1", "attestator_2", "attestator_3"} - {chair}
    )
    # The answer is kept: the record binds the call record, which names its bytes.
    call_ref = payload.get("serving_call_ref") or payload["unit_call_refs"][0]
    assert call_ref in failed["inputs"]
    call = json.loads(tree.read_bytes(call_ref["relative_path"]))
    assert call["finish_reason"] == "abort"
    assert tree.read_bytes(call["raw_response_ref"]["relative_path"])


def test_a_churro_response_with_no_engine_stop_word_publishes_unknown_truncation(
    live_run, tmp_path
):
    """A wire response with no `finish_reason` carries as unknown truncation.

    Truncation is a three-state fact (true, false, unknown), and a completed
    boundary nobody observed must not become a claimed `truncated: false`.
    """
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_3"] = [
        ScriptedAnswer(content=CHURRO_PAGE_ONE, finish_reason=ABSENT),
        ScriptedAnswer(content=CHURRO_PAGE_TWO, finish_reason=ABSENT),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)

    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    payload = page_records(RunTree(run_root, RUN_ID))[(1, "attestator_3")]["payload"]
    assert payload["content_health"]["truncated"] is None
    assert payload["content_health"]["truncation_basis"] == "not-recorded"
    assert payload["native_capture"]["transport_stop_reason"] == "unreported"


def test_a_pass_whose_page_record_never_arrives_holds(live_run, tmp_path, monkeypatch):
    """A served page that publishes nothing leaves the tally short, and it holds.

    The stop the orchestrator makes on an Attestatores hold is the guarantee:
    a pass whose page/chair count cannot be established never reads as complete.
    """
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    monkeypatch.setattr(attestatores, "_serve_page_unit", lambda *args, **kwargs: None)

    assert run_attestatores(live_run, run_root, factory=world.factory) == attestatores.EXIT_HELD


def test_a_resumed_terminal_refuses_a_capture_read_under_another_text_view():
    """The one place a resumed live pass reuses a sealed capture refuses one read
    under a view this build does not produce, by name, before its bytes are
    reused."""
    reference = {"relative_path": "3_attestatores/blobs/sha256/" + "a" * 64, "sha256": "a" * 64}
    capture = {
        "schema": "attestatores-model-view.v1",
        "adapter": "chandra.v1",
        "view": {},
        "transport_stop_reason": "stop",
        "stop_reason": "stop",
        "findings": [],
        "parse": {"state": "parsed", "parser": "html", "text": "read"},
        "raw_response_ref": reference,
        "text_view": "chandra-layout-text.v1",
    }

    def refuse_read(relative_path):
        raise AssertionError(f"another view's capture bytes must not be reused: {relative_path}")

    evidence = dict.fromkeys(attestatores.chandra_native._CHANDRA_RESULT_FIELDS)
    evidence["native_capture"] = capture
    refusal = "names text view 'chandra-layout-text.v1'.*re-run the submission from the Door"
    with pytest.raises(SchemaRefusal, match=refusal):
        attestatores.chandra_native._attempt_from_evidence_record(
            SimpleNamespace(tree=SimpleNamespace(read_bytes=refuse_read)), evidence
        )


def test_a_damaged_native_capture_on_a_page_record_is_a_named_refusal(live_run, tmp_path):
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0
    payload = copy.deepcopy(page_records(RunTree(run_root, RUN_ID))[(1, "attestator_3")]["payload"])
    del payload["native_capture"]["schema"]

    with pytest.raises(SchemaRefusal, match="retained model-view schema"):
        attestatores.validate_page_testimonium_payload(payload)


def test_the_pass_names_the_fixture_tables_it_does_not_read(live_run, tmp_path, capsys):
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    reported = capsys.readouterr().err
    for table in ("testimony", "churro_page_response", "dai_record_response"):
        assert table in reported


def test_a_served_churro_reads_the_vendor_grammar_and_reports_no_geometry(live_run, tmp_path):
    """What this chair actually produces once it runs its vendor's own system.

    Churro-DS carries no geometry, so the reading is the grammar's flattened
    text and the only observation is the `bounds_source="presented"` echo,
    excluded from routing and coverage -- an honest no-layout record rather
    than rectangles nobody reported.
    """
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_3"] = [
        ScriptedAnswer(content=CHURRO_DOCUMENT_PAGE_ONE, finish_reason="stop"),
        ScriptedAnswer(content=CHURRO_DOCUMENT_PAGE_TWO, finish_reason="stop"),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    page_one = page_records(tree)[(1, "attestator_3")]["payload"]
    assert page_one["payload"] == (
        "SYNTHETIC ACT ONE alpha beta\nSYNTHETIC ACT TWO delta epsiIon zeta eta"
    )
    assert [box["bounds_source"] for box in page_one["observed"]] == ["presented"]
    capture = page_one["native_capture"]
    assert capture["parse"]["parser"] == "xml"
    assert capture["findings"] == []
    # And the vendor pin travels with the reading.
    assert capture["vendor_identity"]["repository"] == "github.com/stanford-oval/Churro"


def _call_world(
    generation_sent: dict[str, Any],
    *,
    field: str = "unit_call_refs",
    schema: str = CHAIR_CALL_RECORD_SCHEMA,
    endpoint: str = "http://127.0.0.1:8100",
):
    """A page record naming one retained call, and a context that reads it."""
    from common.decoding import DEFAULT_DECODING_CONFIG_PATH

    receipt_ref = {"relative_path": "receipts/sha256/r.json", "sha256": "a" * 64}
    call = {"schema": schema, "receipt_ref": receipt_ref, "generation_sent": generation_sent}
    blobs: dict[str, bytes] = {}

    def retained(value: dict[str, Any] | None) -> dict[str, Any]:
        record: dict[str, Any] = {"provenance": {"receipt_ref": receipt_ref}}
        if value is None:
            record["native_capture"] = {"raw_response_ref": receipt_ref}
            return record
        data = json.dumps(value).encode()
        digest = hashlib.sha256(data).hexdigest()
        path = f"3_attestatores/blobs/sha256/{digest}"
        blobs[path] = data
        reference = {"relative_path": path, "sha256": digest}
        record[field] = [reference] if field == "unit_call_refs" else reference
        return record

    context = SimpleNamespace(
        tree=SimpleNamespace(
            read_bytes=lambda path: blobs[path],
            read_run_receipt=lambda reference: (
                {"seed": 7, "endpoint": endpoint} if reference == receipt_ref else {}
            ),
        ),
        args=SimpleNamespace(decoding_config=DEFAULT_DECODING_CONFIG_PATH),
        require_sealed_config=lambda _name, _digest: None,
    )
    return context, call, retained


@pytest.mark.parametrize(
    ("chair", "field"),
    (("attestator_2", "unit_call_refs"), ("attestator_3", "serving_call_ref")),
)
def test_a_tallied_call_is_held_to_its_chair_s_row_and_seed(chair, field):
    """The tally re-reads every call a page record names, not only its digest: each
    DAI record's call, and a whole-page chair's one request."""
    from common.decoding import chair_decoding, engine_effective_sampling, recorded_wire_decimals

    policy, _digest = load_decoding_policy()
    sampling = chair_decoding(policy, chair)
    sent = {**recorded_wire_decimals(sampling), "max_tokens": 64, "seed": 7}
    context, call, retained = _call_world(sent, field=field, schema=CHAIR_STREAM_CALL_RECORD_SCHEMA)
    call["sampling_effective"] = recorded_wire_decimals(engine_effective_sampling(sampling))
    call["stream"] = _stream(chair)
    attestatores.verify_page_call_sampling(context, retained(call), chair)

    for moved, message in (
        ({**sent, "seed": 8}, "sent seed 8, not 7"),
        ({**sent, "top_k": 3}, "not the sealed"),
        ({**sent, "n": 2}, r"generation field\(s\) \['n'\]"),
    ):
        with pytest.raises(SchemaRefusal, match=message):
            attestatores.verify_page_call_sampling(
                context, retained({**call, "generation_sent": moved}), chair
            )
    with pytest.raises(
        SchemaRefusal, match="has schema .chair-call-record.v2., not one this build writes"
    ):
        attestatores.verify_page_call_sampling(
            context, retained({**call, "schema": "chair-call-record.v2"}), chair
        )


def _stream(chair: str, stopped: dict[str, Any] | None = None) -> dict[str, Any]:
    from common.decoding import witness_loop_guard

    guard = witness_loop_guard(load_decoding_policy()[0], chair)
    return {"schema": "chair-stream.v1", "loop_guard": guard, "stopped": stopped}


_LOOP_ROWS = [f"Roy, {name} f. 4" for name in ("Jean", "Marie", "Paul")]
_LOOP_TEXT = "\n".join(_LOOP_ROWS + ["Roy, Jean f. 4"] * 31) + "\n"
_LOOP = {"kind": "line", "block_lines": 1, "repeats": 30, "line": 33}


def _stream_world(raw: str):
    """A context holding one retained witness reply, and a capture naming it."""
    from common.decoding import DEFAULT_DECODING_CONFIG_PATH

    data = raw.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    path = f"3_attestatores/blobs/sha256/{digest}"
    context = SimpleNamespace(
        tree=SimpleNamespace(read_bytes={path: data}.__getitem__),
        args=SimpleNamespace(decoding_config=DEFAULT_DECODING_CONFIG_PATH),
        require_sealed_config=lambda _name, _digest: None,
    )
    return context, {"relative_path": path, "sha256": digest}


@pytest.mark.parametrize(
    ("raw", "stopped", "stop_word"),
    [
        (_LOOP_TEXT, _LOOP, "repetition-loop"),
        ("\n".join(_LOOP_ROWS) + "\n", None, "length"),
    ],
    ids=["stopped-on-a-loop", "read-to-its-end"],
)
def test_a_witness_call_is_held_to_its_streamed_guard_and_its_reply(raw, stopped, stop_word):
    """The writer and the tally scan a streamed witness reply again: its loop is
    measured from the retained bytes, never taken from the call record."""
    context, reference = _stream_world(raw)
    capture = {"raw_response_ref": reference, "transport_stop_reason": stop_word}
    call = {"schema": CHAIR_STREAM_CALL_RECORD_SCHEMA, "stream": _stream("attestator_3", stopped)}
    attestatores.verify_call_stream(context, call, "attestator_3", capture)
    # Without a reading there is no reply to scan, but the guard is still held.
    attestatores.verify_call_stream(context, call, "attestator_3", None)

    misstated = {**call, "stream": _stream("attestator_3", None if stopped else _LOOP)}
    with pytest.raises(SchemaRefusal, match="shows the repetition loop"):
        attestatores.verify_call_stream(context, misstated, "attestator_3", capture)
    other_word = {**capture, "transport_stop_reason": "length" if stopped else "repetition-loop"}
    with pytest.raises(SchemaRefusal, match="shows the repetition loop"):
        attestatores.verify_call_stream(context, call, "attestator_3", other_word)


@pytest.mark.parametrize(
    ("chair", "call"),
    [
        # A streamed witness chair's call that was not streamed.
        ("attestator_2", {"schema": CHAIR_CALL_RECORD_SCHEMA}),
        # Streamed under a guard that is not the sealed one.
        (
            "attestator_3",
            {
                "schema": CHAIR_STREAM_CALL_RECORD_SCHEMA,
                "stream": {
                    **_stream("attestator_3"),
                    "loop_guard": {
                        **_stream("attestator_3")["loop_guard"],
                        "loop_line_repeats": 300,
                    },
                },
            },
        ),
        # Chandra is never streamed.
        ("attestator_1", {"schema": CHAIR_STREAM_CALL_RECORD_SCHEMA, "stream": None}),
    ],
    ids=["unstreamed-witness", "wrong-guard", "streamed-chandra"],
)
def test_a_witness_call_streamed_otherwise_than_its_chair_is_refused(chair, call):
    context, _reference = _stream_world("")
    with pytest.raises(SchemaRefusal, match="streamed"):
        attestatores.verify_call_stream(context, call, chair, None)


def test_a_chandra_reading_that_claims_a_loop_stop_is_refused():
    context, reference = _stream_world(_LOOP_TEXT)
    capture = {"raw_response_ref": reference, "transport_stop_reason": "repetition-loop"}
    with pytest.raises(SchemaRefusal, match="never streamed"):
        attestatores.verify_call_stream(
            context, {"schema": CHAIR_CALL_RECORD_SCHEMA}, "attestator_1", capture
        )
    capture["transport_stop_reason"] = "stop"
    attestatores.verify_call_stream(
        context, {"schema": CHAIR_CALL_RECORD_SCHEMA}, "attestator_1", capture
    )


def test_a_live_whole_page_record_that_names_no_serving_call_is_refused():
    """A live response with no call record could have been sampled any way at all."""
    context, _call, retained = _call_world({}, field="serving_call_ref")
    with pytest.raises(SchemaRefusal, match="names no serving call"):
        attestatores.verify_page_call_sampling(context, retained(None), "attestator_3")
    fixture, _call, retained = _call_world(
        {}, field="serving_call_ref", endpoint="fixture://offline-chair-runner"
    )
    attestatores.verify_page_call_sampling(fixture, retained(None), "attestator_3")


def test_a_live_unit_that_retains_a_response_and_names_no_call_is_refused():
    """Per image, the same rule: a DAI unit with a live capture names its call."""
    context, _call, _retained = _call_world({})
    capture = {"raw_response_ref": {"relative_path": "x", "sha256": "b" * 64}}
    record = {
        "provenance": {
            "receipt_ref": {"relative_path": "receipts/sha256/r.json", "sha256": "a" * 64}
        },
        "unit_call_refs": [None],
        "unit_captures": [capture],
    }
    with pytest.raises(SchemaRefusal, match="image 1 and names no call"):
        attestatores.verify_page_call_sampling(context, record, "attestator_2")
    # A unit that retained nothing has no call to name.
    attestatores.verify_page_call_sampling(
        context, {**record, "unit_captures": [None]}, "attestator_2"
    )
    fixture, _call, _retained = _call_world({}, endpoint="fixture://offline-chair-runner")
    attestatores.verify_page_call_sampling(fixture, record, "attestator_2")


def test_a_chandra_answer_under_an_unmeasured_stop_word_resumes_from_its_terminal(
    live_run, tmp_path, monkeypatch
):
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_1"][0] = ScriptedAnswer(content=CHANDRA_PAGE_ONE, finish_reason="abort")
    crashed = LiveWorld(live_run, tmp_path / "crashed", scripts)
    _crash_once(monkeypatch, "attestator_1", 1)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_attestatores(live_run, run_root, factory=crashed.factory)
    monkeypatch.undo()

    resumed_scripts = default_scripts()
    resumed_scripts["attestator_1"] = resumed_scripts["attestator_1"][1:]
    resumed = LiveWorld(live_run, tmp_path / "resumed", resumed_scripts)
    assert run_attestatores(live_run, run_root, factory=resumed.factory) == 0

    # Page 1 is rebuilt from its sealed terminal; only page 2 is asked.
    assert len(resumed.requests("attestator_1")) == 1
    record = page_records(RunTree(run_root, RUN_ID))[(1, "attestator_1")]
    assert record["outcome"] == "failed"
    assert "'abort'" in record["payload"]["reason"]


@pytest.mark.parametrize("damage", ["changed", "removed"])
def test_a_response_kept_unread_is_bound_to_its_page_record(live_run, tmp_path, damage):
    """Its bytes are an input of the page record itself, not only named inside its
    call record, so changing or losing them is refused when the record is read."""
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_3"][0] = ScriptedAnswer(content=CHURRO_PAGE_ONE, finish_reason="abort")
    world = LiveWorld(live_run, tmp_path, scripts)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    record = page_records(tree)[(1, "attestator_3")]
    call = json.loads(tree.read_bytes(record["payload"]["serving_call_ref"]["relative_path"]))
    unread = call["raw_response_ref"]
    assert unread in record["inputs"]
    assert unread in record["payload"]["raw_response_refs"]

    blob = tree.resolve(unread["relative_path"])
    if damage == "changed":
        blob.write_bytes(b"another answer")
    else:
        blob.unlink()
    with pytest.raises(Exception, match="digest|missing|No such file|not found"):
        tree.read_artifact(ATTESTATORES, "page-testimonium", record["artifact_id"])


def test_a_looping_witness_reply_is_abandoned_and_sealed_as_a_cut_off_reading(live_run, tmp_path):
    """Churro and DAI are streamed under the sealed witness guard: a reply that loops
    is stopped at the first loop, kept under `repetition-loop`, and never full testimony.
    Chandra is never streamed."""
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    looping_page = "<HistoricalDocument><Page><Body>\n" + "<Line>alpha beta</Line>\n" * 80
    scripts["attestator_3"][0] = ScriptedAnswer(content=looping_page, finish_reason="length")
    looping_record = "\n".join([DAI_ACT_ONE] * 80)
    scripts["attestator_2"][0] = ScriptedAnswer(content=looping_record, finish_reason="length")
    world = LiveWorld(live_run, tmp_path, scripts)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    assert world.endpoints["attestator_3"].streams_stopped == 1
    assert world.endpoints["attestator_2"].streams_stopped == 1
    assert all(request["stream"] for request in world.requests("attestator_3"))
    assert not any(request.get("stream") for request in world.requests("attestator_1"))
    tree = RunTree(run_root, RUN_ID)
    records = page_records(tree)

    churro = records[(1, "attestator_3")]["payload"]
    assert records[(1, "attestator_3")]["outcome"] == "failed"
    assert churro["native_capture"]["transport_stop_reason"] == "repetition-loop"
    assert "stopped the response on a repetition loop" in churro["reason"]
    call = json.loads(tree.read_bytes(churro["serving_call_ref"]["relative_path"]))
    assert call["schema"] == CHAIR_STREAM_CALL_RECORD_SCHEMA
    assert call["stream"]["stopped"]["repeats"] == 30

    dai = records[(1, "attestator_2")]["payload"]
    assert dai["unit_captures"][0]["transport_stop_reason"] == "repetition-loop"
    assert dai["unit_captures"][0]["parse"]["text"].count(DAI_ACT_ONE) == 30
    assert dai["content_health"]["truncated"] is True
