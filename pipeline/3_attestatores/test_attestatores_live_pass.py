"""The live Attestatores pass: selection, chair-outer serving, and the resume rule.

Every test here runs offline against `operations/serving/fakes.py`. Nothing
starts a pod, contacts a provider or loads a model: a scripted `FakeEndpoint`
answers one chair at a time behind a real `ServingManager`, a real
`ChairClient`, and this stage's own `main`, over a run tree carried to the
Designator by the real upstream stage programs.

**The roster here is the committed one, complete.** It was, for a while, the
two page-scoped chairs and an `attestator_2` marked absent, because two
defects outside that unit's files stopped a `dai.v1` chair from being served
at all: `feeding.dai_generation()` carries floats and the `chair-call-record.v1`
blob goes through `canonical_bytes`, which refuses floats, so the request could
not be recorded and therefore was never made; and `feeding.dai_model_view`'s
identity-transform rule compared whole reference dicts across two stages' blob
namespaces, so every no-resize act -- which is every act crop in this fixture --
was refused *after* its response had already come back. Both are closed
(`operations/serving/client.py` records a float as the exact decimal text the
wire carried; `dai_model_view` compares the digest the two references share),
and the act-scoped arm of the live pass is exercised here end to end rather
than described.
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
LIVE_CHAIRS = ("attestator_1", "attestator_2", "attestator_3")
CATALOGUE_CHAIRS = LIVE_CHAIRS
FIXTURE_ROOT = ROOT / "proof"

# Chandra answers in the closed shape its own prompt asks for
# (`chandra_response.py`): block text with normalized `box_1000` geometry. The
# boxes below convert, on this fixture's 200x260 pages, to exactly the sealed
# proposal rectangles of `a1` (20,20 160x80), `a2` (20,120 160x100) and a2's
# page-2 continuation (20,20 160x60), so the served witness's own geometry
# overlaps the acts it reports on.
CHANDRA_PAGE_ONE = (
    '{"schema": "verbatus-chandra-page-response.v1", "blocks": ['
    '{"box_1000": [100, 77, 900, 385], "text": "SYNTHETIC ACT ONE alpha beta gamma"}, '
    '{"box_1000": [100, 462, 900, 846], "text": "SYNTHETIC ACT TWO delta epsilon zeta eta"}]}'
)
CHANDRA_PAGE_TWO = (
    '{"schema": "verbatus-chandra-page-response.v1", "blocks": ['
    '{"box_1000": [100, 77, 900, 308], "text": "SYNTHETIC ACT TWO delta epsilon zeta eta"}]}'
)
CHANDRA_BODY = CHANDRA_PAGE_ONE
# A body in neither declared shape -- what a model answering in its own native
# mode rather than the asked-for contract would look like on the wire. It is
# retained and refused by name, never read.
CHANDRA_UNRECOGNIZED_BODY = '{"pages": [{"markdown": "a real Chandra body, not the contract"}]}'
CHURRO_PAGE_ONE = (
    "<output>SYNTHETIC ACT ONE alpha beta\nSYNTHETIC ACT TWO delta epsiIon zeta eta</output>"
)
CHURRO_PAGE_TWO = "<output>SYNTHETIC ACT TWO delta epsiIon zeta eta</output>"
# DAI is act-scoped and its parser is plain UTF-8 text
# (`feeding.validate_dai_text`), so its answers are one per act.
DAI_ACT_ONE = "SYNTHETIC ACT ONE alpha beta"
DAI_ACT_TWO = "SYNTHETIC ACT TWO delta epsiIon zeta eta"


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


def committed_models_config() -> Path:
    """The committed roster, unedited: three configured witness chairs.

    This used to rewrite `attestator_2` to `state = "absent"`, because a
    `dai.v1` chair could not be served at all (see the module docstring). Both
    defects are closed, so the live pass is exercised against exactly the
    roster the repository ships -- the assertions below are what would notice
    if that roster stopped describing the three scopes these tests exercise.
    """
    path = ROOT / "config" / "models.toml"
    chairs = tomllib.loads(path.read_text(encoding="utf-8"))["chairs"]
    assert chairs["attestator_2"]["state"] == "configured"
    assert chairs["attestator_2"]["witness_adapter"] == "dai.v1"
    assert chairs["attestator_2"]["witness_scope"] == "act"
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
    models = committed_models_config()
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
    """A chair's unit of work is its own sealed scope, and the scripts say so.

    The two page-scoped chairs answer once per page -- two pages carry these
    two acts -- and the act-scoped chair answers once per act. A script whose
    length disagreed with that would be the first thing to notice a scope
    regression, which is why they are written out rather than generated.
    """
    return {
        "attestator_1": [
            ScriptedAnswer(content=CHANDRA_PAGE_ONE, finish_reason="stop"),
            ScriptedAnswer(content=CHANDRA_PAGE_TWO, finish_reason="stop"),
        ],
        "attestator_2": [
            ScriptedAnswer(content=DAI_ACT_ONE, finish_reason="stop"),
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


def attachment_entries(tree: RunTree) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Every act-attachment entry, by the act's fixture key and then by chair.

    A page witness contributes one entry per contributing page, so each chair
    maps to a list; an act-scoped chair's list has exactly one entry.
    """
    key_of_act = {
        record["subject_id"]: record["payload"]["act_key"] for record in act_records(tree).values()
    }
    entries: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "act-attachment":
            continue
        record = tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        by_chair = entries.setdefault(key_of_act[record["subject_id"]], {})
        for attachment in record["payload"]["attachments"]:
            by_chair.setdefault(attachment["chair"], []).append(attachment)
    return entries


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
    # The act-scoped chair is asked once per act, on the same two acts: the
    # same corpus read through a different sealed scope, which is the whole of
    # what `witness_scope` means.
    assert len(world.requests("attestator_2")) == 2

    tree = RunTree(run_root, RUN_ID)
    records = act_records(tree)
    # Every configured chair answers for every expected act, and every one of
    # them is a chair that really served this run.
    assert {chair for _act, chair in records} == {"attestator_1", "attestator_2", "attestator_3"}
    assert records[("a1", "attestator_2")]["outcome"] == "read"
    assert records[("a1", "attestator_2")]["payload"]["payload"] == DAI_ACT_ONE
    assert records[("a2", "attestator_2")]["payload"]["payload"] == DAI_ACT_TWO
    assert records[("a1", "attestator_3")]["outcome"] == "read"
    assert page_records(tree)[(1, "attestator_3")]["outcome"] == "read"


def test_the_act_scoped_chair_records_its_own_crop_prompt_and_generation_view(live_run, tmp_path):
    """The DAI arm of the live pass, end to end through the real adapter.

    Its closed model view is the thing the two closed gaps were blocking: the
    exact crop it was shown, the exact carried prompt bytes, and the carried
    generation config, all named by digest-checked references. The identity
    transform is the ordinary case here -- these act crops need no resize -- so
    the source and model images are one set of bytes under the two stage-owned
    paths that legitimately hold them.
    """
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    payload = act_records(tree)[("a1", "attestator_2")]["payload"]
    view = payload["native_capture"]["view"]
    assert payload["native_capture"]["adapter"] == "dai.v1"
    assert view["adapter"] == "dai-atr.v1"
    if view["transform"]["kind"] == "identity":
        assert view["source_image_ref"]["sha256"] == view["model_image_ref"]["sha256"]
    else:
        assert view["source_image_ref"]["sha256"] != view["model_image_ref"]["sha256"]
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
    # included, beside the three that actually went on the wire.
    call = json.loads(tree.read_bytes(payload["serving_call_ref"]["relative_path"]))
    declared = feeding.dai_generation()
    assert set(call["generation_sent"]) == {"repetition_penalty", "top_k", "top_p"}
    assert call["generation_declared"]["repetition_penalty"] == {
        "schema": "wire-decimal.v1",
        "decimal": json.dumps(declared["repetition_penalty"]),
    }
    assert call["generation_declared"]["do_sample"] is True


def test_every_live_record_says_which_kind_of_bytes_it_retained(live_run, tmp_path):
    """U8's sixth gap: `raw_response_ref` meant two things and said neither.

    On every branch where an adapter parsed, the retained blob is the model's
    own output; on the one branch where none could, it is the whole transport
    body. Both are evidence and neither substitutes for the other, so the
    record names which it holds -- and the tally still re-reads and
    digest-checks the very blob it names.
    """
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    for (_act, chair), record in act_records(tree).items():
        payload = record["payload"]
        if "serving_call_ref" not in payload:
            continue
        assert payload["raw_response_kind"] == "model-output", chair
        assert payload["native_capture"]["raw_response_ref"] == payload["raw_response_ref"]
        attestatores.validate_retained_response_blob(tree, payload["raw_response_ref"])

    # The record may not claim the other kind while carrying an adapter's own
    # account of the bytes: a capture describes model output and nothing else.
    payload = dict(act_records(tree)[("a1", "attestator_3")]["payload"])
    payload["raw_response_kind"] = "transport-response-body"
    with pytest.raises(SchemaRefusal, match="a capture describes the model's own output"):
        attestatores.validate_testimonium_payload(payload)

    # Nor may a live record stay silent about it.
    del payload["raw_response_kind"]
    with pytest.raises(SchemaRefusal, match="without saying which kind of bytes"):
        attestatores.validate_testimonium_payload(payload)

    # An unrecognized vocabulary word is refused by name, not silently accepted.
    unknown_kind = dict(payload)
    unknown_kind["raw_response_kind"] = "bogus-kind"
    with pytest.raises(SchemaRefusal, match="which is not one of"):
        attestatores.validate_testimonium_payload(unknown_kind)

    # A kind with no retained bytes beside it is refused too -- naming a kind
    # is meaningless without the response it describes.
    kind_without_bytes = dict(payload)
    del kind_without_bytes["raw_response_ref"]
    del kind_without_bytes["native_capture"]
    del kind_without_bytes["serving_call_ref"]
    kind_without_bytes["raw_response_kind"] = "model-output"
    with pytest.raises(SchemaRefusal, match="while retaining none"):
        attestatores.validate_testimonium_payload(kind_without_bytes)


def test_a_wire_response_the_client_cannot_parse_at_all_is_retained_as_the_transport_body(
    live_run, tmp_path
):
    """The second value `raw_response_kind` exists for: no adapter parser ran.

    A body `ChairClient` cannot shape into a reading at all (here, an
    OpenAI-shaped envelope with zero choices) is `_malformed_response_attempt`'s
    branch -- retained, never repaired, with `raw_response_kind` naming the
    whole transport body rather than a model view nothing ever parsed.
    """
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_2"] = [
        ScriptedAnswer(body=json.dumps({"model": "served-attestator_2", "choices": []}).encode()),
        ScriptedAnswer(content=DAI_ACT_TWO, finish_reason="stop"),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    record = act_records(tree)[("a1", "attestator_2")]
    payload = record["payload"]
    assert record["outcome"] == "failed"
    assert payload["raw_response_kind"] == "transport-response-body"
    assert "native_capture" not in payload
    assert "serving_call_ref" in payload
    attestatores.validate_retained_response_blob(tree, payload["raw_response_ref"])

    # The next act on the same chair, unaffected: one malformed reading does
    # not poison the rest of the roster.
    other = act_records(tree)[("a2", "attestator_2")]["payload"]
    assert other["raw_response_kind"] == "model-output"


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


def test_a_served_chandra_publishes_a_real_page_testimonium_with_its_own_geometry(
    live_run, tmp_path
):
    """Attestator 1 is a served Chandra witness like the others (Tyrel, 2026-09-02).

    Its page response parses under the contract its own prompt asks for, so
    the page record is a reading whose text is the block texts joined and whose
    observed geometry is the blocks converted to sealed-page pixels -- each with
    a span into that text. The act views carry the same page-level geometry
    over their one-crop presentation. The page record names the response once,
    through its capture, and does not repeat it in the partition list.
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
    assert payload["native_capture"]["view"] == {"prompt": attestatores.chandra.prompt()}
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

    for key in ("a1", "a2"):
        record = act_records(tree)[(key, "attestator_1")]
        assert record["outcome"] == "read"
        assert record["payload"]["payload"] == payload["payload"]
        assert record["payload"]["page_witness"] is True
        assert record["payload"]["observed"] == payload["observed"]
        assert record["payload"]["adapter_metadata"] == {
            "geometry_quantization": attestatores.chandra.QUANTIZATION_RULE
        }
        assert (
            record["payload"]["raw_response_ref"] == payload["native_capture"]["raw_response_ref"]
        )


def test_a_chandra_body_in_neither_declared_shape_is_retained_and_refused_by_name(
    live_run, tmp_path
):
    """Transported, retained, and honestly unreadable: not the asked-for contract."""
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_1"] = [
        ScriptedAnswer(content=CHANDRA_UNRECOGNIZED_BODY, finish_reason="stop"),
        ScriptedAnswer(content=CHANDRA_UNRECOGNIZED_BODY, finish_reason="stop"),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    record = act_records(tree)[("a1", "attestator_1")]
    payload = record["payload"]
    assert record["outcome"] == "failed"
    assert "unverified-response-schema" in payload["reason"]
    assert payload["content_health"]["recordable"] is False
    # The bytes are retained and the request is accounted for even though no
    # parser could read them -- GOVERNANCE 2.
    assert (
        tree.read_bytes(payload["raw_response_ref"]["relative_path"]).decode()
        == CHANDRA_UNRECOGNIZED_BODY
    )
    assert "serving_call_ref" in payload
    # The adapter's own account of those bytes rides along. It reached
    # `unrecognized-shape` -- the parser ran, read the whole body, and could
    # place no shape it knows -- which is a different fact from a parse
    # failure, and the shared capture contract has room for it, so the
    # retained model view stays beside the blob it describes.
    assert payload["native_capture"]["parse"] == {
        "state": "unrecognized-shape",
        "parser": "json",
        "outcome": "unverified-response-schema",
    }
    assert payload["native_capture"]["raw_response_ref"] == payload["raw_response_ref"]
    assert payload["raw_response_kind"] == "model-output"
    # No anchor can be derived from a page the anchor chair did not read, and
    # the other page witness says exactly that.
    a1 = attachment_entries(tree)["a1"]
    assert a1["attestator_3"][0]["alignment"] == {
        "status": "unaligned",
        "reason": "missing-chandra-page-anchor",
    }


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
    # rebuilt from the act layer the interrupted pass sealed. The act-scoped
    # chair finished both its acts before the interruption, so the resume has
    # nothing to ask it and never starts it -- a chair with no pending unit is
    # a chair that is not loaded, which is what makes a resume cheap as well as
    # safe.
    assert resumed.loads == ["attestator_1", "attestator_3"]
    assert len(resumed.requests("attestator_1")) == 1
    assert len(resumed.requests("attestator_3")) == 1
    assert resumed.requests("attestator_2") == []
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


def test_a_churro_response_with_no_engine_stop_word_publishes_unknown_truncation(
    live_run, tmp_path
):
    """U8's third gap: the third truncation state, published rather than refused.

    A live Churro page whose wire carried no `finish_reason` used to stop the
    pass by name, because the shared page contract asked a two-valued question
    of a three-state fact and could only have published `truncated: false` --
    a completed boundary nobody observed. The third state is now measured on
    both records, so the response is carried instead of refused, and it is
    carried as unknown rather than as either measured answer.
    """
    run_root = fresh_tree(live_run, tmp_path)
    scripts = default_scripts()
    scripts["attestator_3"] = [
        ScriptedAnswer(content=CHURRO_PAGE_ONE, finish_reason=ABSENT),
        ScriptedAnswer(content=CHURRO_PAGE_TWO, finish_reason=ABSENT),
    ]
    world = LiveWorld(live_run, tmp_path, scripts)

    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    for payload in (
        act_records(tree)[("a1", "attestator_3")]["payload"],
        page_records(tree)[(1, "attestator_3")]["payload"],
    ):
        assert payload["content_health"]["truncated"] is None
        assert payload["content_health"]["truncation_basis"] == "not-recorded"
        assert payload["native_capture"]["transport_stop_reason"] == "unreported"
    # The engine's own silence travels verbatim into the request record too:
    # `unreported` is this system's word for the absence, never a stop word the
    # engine did not say.
    call = json.loads(
        tree.read_bytes(
            act_records(tree)[("a1", "attestator_3")]["payload"]["serving_call_ref"][
                "relative_path"
            ]
        )
    )
    assert call["finish_reason"] is None


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


def test_a_resumed_parsed_but_unconfirmed_blank_chandra_page_carries_no_observation_payload():
    """A resume must not rederive a *different* partition than the pass sealed.

    `captured_page_attempt` sets `observation_payload` only on its first
    parsed branch (`completed is True or parsed["text"] != ""`), which is
    ``read``/``genuinely-empty``. A response whose body parsed to a closed
    shape but whose transport word reported neither a natural stop nor any
    text -- cut off, unreported, unrecognized -- lands on the *second* parsed
    branch instead: outcome ``failed``, "not a confirmed blank page", and
    deliberately no `observation_payload`. `_page_capture_from_record` used
    to rehydrate on `parse.state == "parsed"` alone, which is true on that
    same record, so a resume handed the bytes back and re-derived geometry
    the interrupted pass never sealed -- the immutable writer then refuses
    the differing republish. Gating on `record["outcome"] in
    WITNESS_READING_OUTCOMES` (the same set `captured_page_attempt`'s reading
    branch uses) closes the gap; the fake tree's `read_bytes` raising proves
    the rehydration path is never even entered.
    """
    record = {
        "outcome": "failed",
        "payload": {
            "payload": "",
            "witness_reported": None,
            "content_health": {
                "native_type": "text",
                "encoding": "utf-8",
                "recordable": True,
                "empty": True,
                "blank": True,
                "truncated": None,
                "characters": 0,
                "truncation_basis": "not-a-confirmed-blank-page",
            },
            "format_capabilities": attestatores.DEFAULT_FORMAT_CAPABILITIES,
            "reason": "not a confirmed blank page: the response was cut off before any stop word",
            "raw_response_ref": {
                "relative_path": "3_attestatores/blobs/sha256/x",
                "sha256": "x" * 64,
            },
            "native_capture": {
                "adapter": "chandra.v1",
                "parse": {"state": "parsed", "parser": "json"},
                "raw_response_ref": {
                    "relative_path": "3_attestatores/blobs/sha256/x",
                    "sha256": "x" * 64,
                },
            },
            "provenance": {"receipt_ref": {"relative_path": "receipts/x.json", "sha256": "a" * 64}},
        },
    }

    def refuse_read(relative_path):
        raise AssertionError(
            f"rehydration must not read raw bytes for a non-reading outcome: {relative_path}"
        )

    context = SimpleNamespace(
        tree=SimpleNamespace(
            read_run_receipt=lambda reference: {"endpoint": "https://live.example/chair"},
            read_bytes=refuse_read,
        )
    )
    attempt, capture = attestatores._page_capture_from_record(
        context, record, "the page Testimonium sealed for page 1, chair 'attestator_1'"
    )

    assert attempt.observation_payload is None
    assert capture == record["payload"]["native_capture"]


def test_a_live_dai_request_records_its_carried_float_generation_values(tmp_path):
    """The defect that used to keep DAI out of the live roster, from the outside.

    `chair-call-record.v1` is canonical JSON and canonical JSON refuses a
    float, so a request carrying DAI's shipped generation config could not be
    recorded and was therefore never made. It is recorded now as the exact
    decimal text the wire carries, which this test checks against the bytes the
    endpoint actually received rather than against the client's own values --
    a request recorded as something other than what was sent has no provenance
    (GOVERNANCE 6), and rounding it would be the silent version of the same
    problem.
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
    # `temperature` is declared by the vendor and never sent -- the sealed
    # reading-of-record posture is 0 -- and is still recorded to the digit.
    assert record["generation_declared"]["temperature"] == {
        "schema": "wire-decimal.v1",
        "decimal": json.dumps(declared["temperature"]),
    }
    assert "temperature" not in request.generation_sent


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


def test_a_stop_word_that_cannot_be_recorded_honestly_refuses_before_publication():
    """One refusal, on the transport word alone, whatever the adapter.

    The shared capture contract checks the transport word only for
    `churro.v1`, so an unreadable engine word from any other adapter would
    otherwise travel into a record unexamined. The check reads the response's
    own word directly rather than off a capture, so it also covers a wire body
    `ChairClient` could not parse at all (`native_capture = None`), whose
    engine word is still recorded verbatim in its `chair-call-record.v1` blob.
    """
    with pytest.raises(ContractError, match="never measured a meaning for"):
        attestatores.refuse_unpublishable_stop_word("abort", "the response for page 1")


def test_an_unreported_stop_word_is_recorded_rather_than_refused():
    """The vocabulary admits the absence marker; only unmeasured words refuse.

    A second refusal used to live here, for Churro alone, because the shared
    page contract could not reconcile an unreported boundary against a
    truncation state. `common/native_witness.py` measures that third state now,
    so the guard is gone rather than merely narrowed -- what is left is the one
    question it was always for: has this pipeline ever measured a meaning for
    this word?
    """
    attestatores.refuse_unpublishable_stop_word(
        attestatores.STOP_REASON_UNREPORTED, "the response for page 1"
    )
    for word in ("stop", "length", "eos", "max_new_tokens"):
        attestatores.refuse_unpublishable_stop_word(word, "the response for page 1")


# ============================ resume: mid-page interruption ===================


def test_a_pass_interrupted_between_two_act_views_of_one_page_completes_on_resume(
    live_run, tmp_path, monkeypatch
):
    """The resume rule's own hard case: a crash between two act publications of
    the same page response.

    A page-scoped chair publishes one act view per act on its page from a
    single response (`publish_page_act_views`, shared by `_serve_page_unit`
    and the resume repair in `live_attempt_pass`). The happy fixture puts both
    `a1` and `a2` on page 1, so a crash after `a1` publishes and before `a2`
    does leaves `a1` sealed, `a2` sealed nowhere, and the page Testimonium
    itself unsealed either way. Before this fix the resume could rebuild the
    page's capture from `a1` alone but never revisited `a2`, so the pass died
    on `FatalAccounting: ... unresolved witness attempt(s)`, at that ordinal,
    forever. The resumed pass here must finish `a2` from the retained `a1`
    response, ask attestator_1 for page 2 only, and exit 0.
    """
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    real_publish_attempt = attestatores.publish_attempt

    def crashing_publish_attempt(
        context, *, act, chair, resolved, ordinal, regions, attempt, live=False
    ):
        # `act["act_id"]` is the internal per-act identity (`act_<hash>`); the
        # fixture's own human-readable `key` -- what `act_records()` below
        # indexes by -- is carried as `act["act_key"]`.
        if chair == "attestator_1" and act["act_key"] == "a2":
            raise RuntimeError("simulated crash between two act publications of one page")
        return real_publish_attempt(
            context,
            act=act,
            chair=chair,
            resolved=resolved,
            ordinal=ordinal,
            regions=regions,
            attempt=attempt,
            live=live,
        )

    monkeypatch.setattr(attestatores, "publish_attempt", crashing_publish_attempt)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_attestatores(live_run, run_root, factory=world.factory)
    monkeypatch.undo()

    interrupted = act_records(RunTree(run_root, RUN_ID))
    assert ("a1", "attestator_1") in interrupted
    assert ("a2", "attestator_1") not in interrupted
    # attestator_1 is alphabetically first and page-scoped: the crash inside
    # its own page-1 act publications means attestator_3 never started.
    assert ("a1", "attestator_3") not in interrupted
    assert not page_records(RunTree(run_root, RUN_ID))

    resumed_scripts = {
        # Page 1 is rebuilt from the sealed `a1` record; only page 2 is asked.
        "attestator_1": [ScriptedAnswer(content=CHANDRA_BODY, finish_reason="stop")],
        # The two chairs after it never sealed anything and are asked fresh:
        # the act-scoped one once per act, the page-scoped one once per page.
        "attestator_2": [
            ScriptedAnswer(content=DAI_ACT_ONE, finish_reason="stop"),
            ScriptedAnswer(content=DAI_ACT_TWO, finish_reason="stop"),
        ],
        "attestator_3": [
            ScriptedAnswer(content=CHURRO_PAGE_ONE, finish_reason="stop"),
            ScriptedAnswer(content=CHURRO_PAGE_TWO, finish_reason="stop"),
        ],
    }
    resumed = LiveWorld(live_run, tmp_path / "resumed", resumed_scripts)
    assert run_attestatores(live_run, run_root, factory=resumed.factory) == 0

    assert len(resumed.requests("attestator_1")) == 1
    assert len(resumed.requests("attestator_2")) == 2
    assert len(resumed.requests("attestator_3")) == 2

    tree = RunTree(run_root, RUN_ID)
    records = act_records(tree)
    # a1's record is untouched -- a live chair cannot reproduce immutable bytes.
    assert records[("a1", "attestator_1")] == interrupted[("a1", "attestator_1")]
    # a2 is finally published, from the same retained response as a1: same
    # outcome, same retained bytes, never a second Chandra call.
    assert records[("a2", "attestator_1")]["outcome"] == records[("a1", "attestator_1")]["outcome"]
    assert (
        records[("a2", "attestator_1")]["payload"]["raw_response_ref"]
        == records[("a1", "attestator_1")]["payload"]["raw_response_ref"]
    )
    assert records[("a1", "attestator_3")]["outcome"] == "read"
    assert records[("a2", "attestator_3")]["outcome"] == "read"


def test_resumed_page_captures_skips_a_not_run_pair_instead_of_refusing():
    """A held or refused act sealed as `not-run` is not fixture-posture evidence.

    `live_attempt_pass`'s own first loop seals `not-run`/`dead` records, with
    `serving_call_ref=None`, for every pair no chair was asked about -- a held
    act, a refused proposal crop. That is the *same* shape a fixture-posture
    record has, but it is not the same fact: only an *attempted* outcome
    naming no serving call is evidence this pair was declared rather than
    served. A page whose first-listed act was held must not have its resume
    refused over that unrelated record.
    """
    not_run = attestatores.Attempt(
        outcome="not-run",
        native_payload=None,
        witness_reported=None,
        format_capabilities=dict(attestatores.DEFAULT_FORMAT_CAPABILITIES),
        health=attestatores.no_response_health(reason="not-attempted"),
        reason="the Designator held this act",
    )
    read_attempt = attestatores.Attempt(
        outcome="read",
        native_payload="declared text",
        witness_reported=None,
        format_capabilities=dict(attestatores.DEFAULT_FORMAT_CAPABILITIES),
        health=attestatores.content_health("declared text", completed=True),
        reason=None,
        serving_call_ref={"relative_path": "3_attestatores/blobs/sha256/x", "sha256": "x"},
    )
    context = SimpleNamespace(tree=SimpleNamespace(build_manifest=lambda stage: {"artifacts": []}))
    captures = attestatores.resumed_page_captures(
        context,
        acts_by_page={
            1: [
                {"act_id": "held-act", "page_ordinal": 1},
                {"act_id": "act-1", "page_ordinal": 1},
            ]
        },
        page_chairs=["attestator_3"],
        ordinal=1,
        attempts_by_pair={
            ("held-act", "attestator_3"): not_run,
            ("act-1", "attestator_3"): read_attempt,
        },
        sealed_pairs=frozenset({("held-act", "attestator_3"), ("act-1", "attestator_3")}),
    )
    assert captures[(1, "attestator_3")] == (read_attempt, read_attempt.native_capture)


def test_resumed_page_captures_refuses_two_sealed_acts_that_disagree():
    """Two records claiming the same page response must actually agree.

    `resumed_page_captures` used to take the first sealed act it found and
    never check the rest; a page with two acts whose sealed records disagree
    about which response produced them must be named, not silently resolved
    by taking whichever act sorts first.
    """
    first = attestatores.Attempt(
        outcome="read",
        native_payload="one response",
        witness_reported=None,
        format_capabilities=dict(attestatores.DEFAULT_FORMAT_CAPABILITIES),
        health=attestatores.content_health("one response", completed=True),
        reason=None,
        raw_response_ref={"relative_path": "3_attestatores/blobs/sha256/a", "sha256": "a" * 64},
        serving_call_ref={"relative_path": "3_attestatores/blobs/sha256/x", "sha256": "x" * 64},
    )
    second = attestatores.Attempt(
        outcome="read",
        native_payload="a different response",
        witness_reported=None,
        format_capabilities=dict(attestatores.DEFAULT_FORMAT_CAPABILITIES),
        health=attestatores.content_health("a different response", completed=True),
        reason=None,
        raw_response_ref={"relative_path": "3_attestatores/blobs/sha256/b", "sha256": "b" * 64},
        serving_call_ref={"relative_path": "3_attestatores/blobs/sha256/y", "sha256": "y" * 64},
    )
    context = SimpleNamespace(tree=SimpleNamespace(build_manifest=lambda stage: {"artifacts": []}))
    with pytest.raises(SchemaRefusal, match="disagree"):
        attestatores.resumed_page_captures(
            context,
            acts_by_page={
                1: [
                    {"act_id": "a1", "page_ordinal": 1},
                    {"act_id": "a2", "page_ordinal": 1},
                ]
            },
            page_chairs=["attestator_3"],
            ordinal=1,
            attempts_by_pair={("a1", "attestator_3"): first, ("a2", "attestator_3"): second},
            sealed_pairs=frozenset({("a1", "attestator_3"), ("a2", "attestator_3")}),
        )


# ================== resumed observation-payload guard (Chandra) ===============


def test_a_resumed_chandra_record_that_never_parsed_carries_no_observation_payload(
    live_run, tmp_path
):
    """The defect-fix guard `_attempt_from_retained_testimonium` relies on.

    A live Chandra response in neither declared shape never parses into a
    payload, so a resumed act-scoped compatibility record for it names a
    serving call, retains its raw bytes, and reports
    `content_health.recordable=False`. Rehydrating those bytes as
    `observation_payload` would feed page geometry from bytes no parser ever
    recognized -- exactly the measurement nobody made the guard exists to
    refuse (the `served_by_a_chair and not parsed_into_a_payload` branch).
    Proven directly against the function, because the branch depends only on
    the record's own shape, not on running a whole live pass twice.
    """
    run_root = fresh_tree(live_run, tmp_path)
    context = open_live_context(live_run, run_root)
    raw_response_ref = attestatores.retained_blob_ref(
        context, CHANDRA_UNRECOGNIZED_BODY.encode("utf-8")
    )
    record = {
        "outcome": "failed",
        "payload": {
            "payload": None,
            "witness_reported": None,
            "format_capabilities": attestatores.DEFAULT_FORMAT_CAPABILITIES,
            "content_health": {
                "native_type": "unrecordable",
                "encoding": "invalid-or-unrecordable",
                "recordable": False,
                "empty": None,
                "blank": None,
                "truncated": None,
                "characters": None,
                "truncation_basis": "unverified-response-schema",
            },
            "reason": "unverified-response-schema",
            "raw_response_ref": raw_response_ref,
            "serving_call_ref": {
                "relative_path": "3_attestatores/blobs/sha256/call",
                "sha256": "c" * 64,
            },
            "native_capture": None,
            "provenance": {"receipt_ref": None},
        },
    }

    attempt = attestatores._attempt_from_retained_testimonium(context.tree, record)

    assert attempt.observation_payload is None


# ==================== the operator-facing unread-declarations line ============


def test_the_pass_names_chandra_anchors_among_what_it_does_not_read(live_run, tmp_path, capsys):
    """`chandra_anchor` is a declared fixture stimulus a live pass discards too.

    It keys on `page_ordinal`, not `chair`, so it cannot ride the same
    `chair in live_chairs` filter as the other families -- and had been left
    off the printed count entirely, even though `live_attempt_pass` discards
    every declared anchor unconditionally.
    """
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    reported = capsys.readouterr().err
    assert "chandra_anchor" in reported


# =========================== the derived anchor (R4) ==========================


def test_live_page_witnesses_align_against_the_anchor_derived_from_chandras_own_response(
    live_run, tmp_path
):
    """R4 on the live path: the anchor is Chandra's served page text and block
    geometry, never the fixture's declared `[[chandra_anchor]]` rows.

    Each act's anchor line is the reported block whose geometry overlaps the
    act's sealed proposal, and both page witnesses align their page text
    against that anchor. Chandra itself is attached (its own blocks overlap
    the acts) and aligned, so it is comparable. Churro's text aligns to the
    same anchor, but Churro publishes no native layout -- its only geometry is
    the presented echo, excluded from routing -- so on the live path it stays
    geometrically unattached with its alignment retained beside it, and no
    span: a fixture run attaches it only through a declared
    `[[native_observation]]` row a live pass does not read.
    """
    run_root = fresh_tree(live_run, tmp_path)
    world = LiveWorld(live_run, tmp_path)
    assert run_attestatores(live_run, run_root, factory=world.factory) == 0

    tree = RunTree(run_root, RUN_ID)
    entries = attachment_entries(tree)
    page_text = page_records(tree)[(1, "attestator_1")]["payload"]["payload"]

    [chandra_a1] = entries["a1"]["attestator_1"]
    assert chandra_a1["attached"] is True
    assert chandra_a1["comparable"] is True
    assert chandra_a1["attachment_basis"] == "geometric-overlap"
    alignment = chandra_a1["alignment"]
    assert alignment["status"] == "aligned"
    assert alignment["anchor_basis"] == "act-anchor"
    assert alignment["anchor_chair"] == "attestator_1"
    assert alignment["line_geometry"] == [{"bbox": {"x": 20, "y": 20, "w": 160, "h": 81}}]
    assert alignment["anchor_span"] == {"start": 0, "end": 34}
    assert page_text[alignment["witness_span"]["start"] : alignment["witness_span"]["end"]] == (
        "SYNTHETIC ACT ONE alpha beta gamma"
    )
    assert chandra_a1["span"] == alignment["witness_span"]

    # a2 runs onto page 2: its primary page carries the comparison view, its
    # continuation page is explicitly unaligned for the reason the schema names.
    primary, continuation = sorted(
        entries["a2"]["attestator_1"], key=lambda entry: entry["page_ordinal"]
    )
    assert primary["page_ordinal"] == 1 and continuation["page_ordinal"] == 2
    assert primary["alignment"]["anchor_span"] == {"start": 35, "end": 75}
    assert primary["alignment"]["line_geometry"] == [
        {"bbox": {"x": 20, "y": 120, "w": 160, "h": 100}}
    ]
    assert (
        page_text[
            primary["alignment"]["witness_span"]["start"] : primary["alignment"]["witness_span"][
                "end"
            ]
        ]
        == "SYNTHETIC ACT TWO delta epsilon zeta eta"
    )
    assert continuation["alignment"] == {
        "status": "unaligned",
        "reason": "continuation-page-no-act-anchor",
    }
    # `attached` is geometry alone, on every contributing page: Chandra's page-2
    # block overlaps a2's continuation region, so the tail is attached while
    # its alignment says no anchor line exists for it. This is the state this
    # stage's contract describes and the Perlector today cannot read (its
    # `act_attachment_view` requires `attached` to equal the geometric overlap
    # and refuses an attached continuation-page entry -- HANDOFF.md names the
    # contradiction); it is pinned here so the fix lands against a measured
    # record rather than a described one.
    assert continuation["attached"] is True
    assert continuation["attachment_basis"] == "geometric-overlap"
    assert continuation["comparable"] is False and continuation["span"] is None

    [churro_a1] = entries["a1"]["attestator_3"]
    assert churro_a1["attached"] is False
    assert churro_a1["comparable"] is False
    assert churro_a1["attachment_basis"] == "unattached"
    assert churro_a1["span"] is None
    churro_alignment = churro_a1["alignment"]
    assert churro_alignment["status"] == "aligned"
    assert churro_alignment["anchor_chair"] == "attestator_1"
    assert churro_alignment["anchor_span"] == {"start": 0, "end": 34}
    churro_text = page_records(tree)[(1, "attestator_3")]["payload"]["payload"]
    assert churro_text[
        churro_alignment["witness_span"]["start"] : churro_alignment["witness_span"]["end"]
    ].startswith("SYNTHETIC ACT ONE alpha beta")

    # The act-scoped chair is untouched by any of this.
    [dai_a1] = entries["a1"]["attestator_2"]
    assert dai_a1["alignment"] is None and dai_a1["attached"] is True


def test_derived_chandra_anchor_locates_lines_by_geometry_and_names_what_it_cannot():
    """The derivation itself, over hand-built facts: geometry decides, text
    follows, and an act no block overlaps -- or whose blocks carry no
    normalizable text -- gets no range rather than a guessed one."""
    page_text = "<p>first  line</p>\n<p>second line</p>   "
    assert page_text[3:14] == "first  line"
    assert page_text[22:33] == "second line"
    assert page_text[37:40] == "   "

    def block(ordinal, y, span, source="native", h=50):
        return {
            "ordinal": ordinal,
            "bounds": {"x": 0, "y": y, "w": 100, "h": h},
            "bounds_source": source,
            "span": {"start": span[0], "end": span[1]},
        }

    observed = [
        block(0, 0, (3, 14)),
        block(1, 60, (22, 33)),
        block(2, 120, (37, 40)),
        # A presented echo is not reported geometry and anchors nothing.
        block(3, 0, (0, 40), source="presented", h=260),
    ]

    def region(page_ordinal, bounds):
        return {"payload": {"transform": {"source_page_ordinal": page_ordinal, "bounds": bounds}}}

    acts = [
        {"act_id": "first", "page_ordinal": 1},
        {"act_id": "second", "page_ordinal": 1},
        {"act_id": "blank", "page_ordinal": 1},
        {"act_id": "elsewhere", "page_ordinal": 1},
        {"act_id": "continued-here", "page_ordinal": 7},
    ]
    regions_by_act = {
        "first": ([region(1, {"x": 10, "y": 10, "w": 20, "h": 20})], None),
        "second": ([region(1, {"x": 10, "y": 70, "w": 20, "h": 20})], None),
        "blank": ([region(1, {"x": 10, "y": 130, "w": 20, "h": 20})], None),
        "elsewhere": ([region(1, {"x": 150, "y": 10, "w": 20, "h": 20})], None),
        "continued-here": ([region(1, {"x": 10, "y": 10, "w": 20, "h": 20})], None),
    }

    anchors = attestatores.derived_chandra_anchor(
        page_text=page_text,
        observed=observed,
        page_ordinal=1,
        page_acts=acts,
        regions_by_act=regions_by_act,
    )

    # Ranges are in the markup-stripped, whitespace-collapsed view:
    # "first line second line".
    assert anchors == {
        "first": {
            "start": 0,
            "end": len("first line"),
            "line_geometry": [{"bbox": {"x": 0, "y": 0, "w": 100, "h": 50}}],
        },
        "second": {
            "start": len("first line "),
            "end": len("first line second line"),
            "line_geometry": [{"bbox": {"x": 0, "y": 60, "w": 100, "h": 50}}],
        },
    }
