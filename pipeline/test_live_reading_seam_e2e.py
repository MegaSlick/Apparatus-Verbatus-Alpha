"""The live reading seam over one run tree, from the Door to the export.

A run tree is carried to the Designator by the real stage programs, read by an
Attestatores roster whose three chairs really served, then by a Perlector chair
that really served, page by page, and carried on through the Recensor,
Archetypus and Armarium to a terminal export.

Nothing here starts a pod, opens a socket, loads a model or reaches a network.
The chairs answer through `operations/serving/fakes.py`: a scripted
`FakeEndpoint` behind a real `ServingManager`, a real `ChairClient`, and each
stage's own `main`. What makes the run live is the sealed serving-recipe row
kind: the tmp catalogue below marks the three witness chairs and the Perlector
`kind = "vllm"` at every tier `config/pod_placement.toml` defines, and the
Designator's chairs and the Coniector's reconstructor keep their fixture rows.

The page chairs answer once per page and DAI once per record its detector
found; the Perlector answers each page with the fixture's scripted `happy` page
answer. The driver and chair worlds here are shared with
`pipeline/test_dai_detector_records_e2e.py` and
`pipeline/test_structure_chair_e2e.py`.

**A delivered offline e2e is not a proven pipeline.** One scripted run over a
synthetic fixture reaches an export; nothing follows about a real page.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import load_stage, programs_through

ROOT = Path(__file__).resolve().parents[1]
ATTESTATORES_DIR = ROOT / "pipeline" / "3_attestatores"
PERLECTOR_DIR = ROOT / "pipeline" / "4_perlector"
# Each stage program imports its own directory's modules by bare name
# (`import feeding`, `import page_run`), which is the stage import boundary
# `pipeline/test_stage_import_boundaries.py` enforces. Loading two stages'
# `run.py` in one process therefore needs both directories importable; they
# share no module name, so neither shadows the other.
for _stage_directory in (ATTESTATORES_DIR, PERLECTOR_DIR):
    if str(_stage_directory) not in sys.path:
        sys.path.insert(0, str(_stage_directory))

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.outcomes import (  # noqa: E402
    ArmariumCategory,
)
from common.contracts.stages import ATTESTATORES, PERLECTOR  # noqa: E402
from common.decoding import load_decoding_policy  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import EXIT_COMPLETE, EXIT_HELD, verify_final_seal  # noqa: E402
from operations.serving.assembly import retain_chair_bytes  # noqa: E402
from operations.serving.client import ChairClient  # noqa: E402
from operations.serving.config import (  # noqa: E402
    ServingConfigInputs,
    chair_preflight_identity_digest,
    load_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.fakes import (  # noqa: E402
    FakeEndpoint,
    FakeLauncher,
    FakePackages,
    ScriptedAnswer,
    shipped_decoding_policy,
)
from operations.serving.manager import ServingManager, StageContextReceiptPublisher  # noqa: E402
from operations.serving.residency import FileResidencyLease  # noqa: E402

RUN_ID = "r"
FIXTURE_ROOT = ROOT / "proof"
TIER = "generic-48gb"
TIERS = ("generic-24gb", "generic-48gb", "generic-80gb-plus")
WITNESS_CHAIRS = ("attestator_1", "attestator_2", "attestator_3")
LIVE_CHAIRS = (*WITNESS_CHAIRS, "perlector")
CHAIN_TO_DESIGNATOR = programs_through("designator")
TAIL_FROM_RECENSOR = (
    "pipeline/5_recensor/run.py",
    "pipeline/6_archetypus/run.py",
    "pipeline/4b_coniector/run.py",
    "pipeline/7_armarium/run.py",
)

# What each chair answers with. The witness bodies are the shapes their own
# adapters parse: Chandra answers in its vendor's own layout grammar
# (`common/chandra_layout.py`) -- top-level divs carrying a `data-bbox`
# normalized 0-1000, which convert, on this fixture's 200x260 pages, to the
# sealed proposal rectangles of `a1`, `a2` and a2's page-2 continuation; DAI
# answers plain text once per record its detector found. Churro answers its own
# closed contract on page 1 and plain reading-order text on page 2, so both of
# its legal shapes cross this seam.
CHANDRA_PAGE_ONE = (
    '<div data-bbox="100 77 900 385" data-label="Text">'
    "SYNTHETIC ACT ONE alpha beta gamma</div>\n"
    '<div data-bbox="100 462 900 846" data-label="Text">'
    "SYNTHETIC ACT TWO delta epsilon zeta eta</div>"
)
# Page 2 carries only a2's continuation, and it is answered as a block the
# model transcribed but placed no box on -- a `<div>` with no `data-bbox`. That
# is exactly a page a chair read but reported no geometry for, so the coverage
# is kept rather than dropped. The grammar names it (`malformed-bbox`, reason
# "no data-bbox attribute") instead of substituting the [0,0,1,1] rectangle the
# vendor's own parser would have.
CHANDRA_PAGE_TWO = '<div data-label="Text">SYNTHETIC ACT TWO delta epsilon zeta eta</div>'
# Churro answers page 1 in the vendor's own `HistoricalDocument` grammar -- the
# shape `churro.prompt` asks for, read by `common/churro_document.py`. It
# carries no geometry, and nothing in the grammar could: `Page`, `Header`,
# `Body`, `Footer` and `Line`, and not one coordinate in the guide or the XSD.
# The chair reports the `bounds_source="presented"` echo that routing and
# coverage exclude.
CHURRO_PAGE_ONE = (
    "<HistoricalDocument><Page><Body>"
    "<Line>SYNTHETIC ACT ONE alpha beta gamma</Line>"
    "<Line>SYNTHETIC ACT TWO delta epsilon zeta eta</Line>"
    "</Body></Page></HistoricalDocument>"
)
# Page 2 is plain reading-order text, Churro's other legal shape, so this module
# covers both of Churro's shapes across its two pages, as it does Chandra's two
# forms.
CHURRO_PAGE_TWO = "SYNTHETIC ACT TWO delta epsilon zeta eta"
DAI_ACT_ONE = "SYNTHETIC ACT ONE alpha beta gamma"
DAI_ACT_TWO = "SYNTHETIC ACT TWO delta epsilon zeta eta"
DAI_CONTINUATION = "zeta eta"


attestatores = load_stage("3_attestatores")
perlector = load_stage("4_perlector")


# ------------------------------ the tmp catalogue -----------------------------


def _vllm_row(*, recipe: str, chair: str, tier: str, port: int) -> dict[str, Any]:
    """One complete `kind = "vllm"` profile row, in the shape `config.py` closes.

    Every figure is a test value in a tmp file; no committed catalogue is
    edited, and none of these numbers is a measurement of anything.
    `preflight_state = "proven"` carries both real digests because
    `ServingManager._launchable` refuses an unproven row, and this suite
    exercises the manager a real run would build rather than a relaxed one.
    """
    return {
        "kind": "vllm",
        "recipe": recipe,
        "chair": chair,
        "tier": tier,
        "host": "127.0.0.1",
        "port": port,
        "served_model_id": f"served-{chair}",
        "dtype": "bfloat16",
        "seed": 7,
        "required_packages": {"vllm": "0.test"},
        # Per chair, as the shipped real catalogue states them: the Perlector's
        # row is the long one. It was 2,048 for every chair while the reader
        # admitted on a prompt *floor* of 790; the seam now admits on the
        # measured upper bound, and this run's four-image Perlector request
        # costs 2,080 by it.
        #
        # Churro reserves its vendor's whole answer bound, 25,000
        # (`DECLARED_ANSWER_BOUND_TOKENS`), beside its 27-token system string
        # and the image, so no row shorter than that can admit a page. The
        # shipped catalogue states 32,768 for this chair at every tier, so the
        # stand-in states it too -- the row moves, never the arithmetic and
        # never the pixels.
        #
        # Chandra's row moved next, for the same reason once more: the chair is
        # asked in its vendor's own `OCR_LAYOUT_PROMPT` rather than this
        # repository's retired instruction, re-measured at 593 against 256, so
        # its need is 1 + 593 + 1,645 = 2,239 (U14's re-measured answer) where
        # the row left 2,048. The shipped
        # catalogue states 18,000 for it (U15) at every tier. DAI keeps 2,048:
        # its act crop needs 1 + 84 + 230 and fits with room to spare, and raising a row nothing refuses would
        # remove the one chair this stand-in still proves the arithmetic
        # against.
        "max_model_len": {
            "perlector": 16384,
            "attestator_1": 18000,
            "attestator_3": 32768,
        }.get(chair, 2048),
        "max_num_seqs": 1,
        "max_num_batched_tokens": 256,
        "gpu_memory_utilization": "0.85",
        "min_pixels": 1,
        "max_pixels": 1806336,
        # The chair's own vision-encoder geometry, as the shipped real
        # catalogue states it: without it nothing can say what one image costs
        # this chair in prompt tokens, and the request builders refuse by name
        # rather than counting against a default (`common/request_capacity.py`).
        "patch_size": 14 if chair in {"attestator_2", "attestator_3"} else 16,
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


def write_live_catalogue(path: Path, registry) -> Path:
    """Every chair this seam can serve, live, at every tier the placement file names.

    The Designator's two detectors and the Coniector's reconstructor keep their
    fixture rows (a fixture row answers only a synthetic run): this module's
    subject is the reading seam. Every other configured chair is live at all three
    tiers, which is also what `verify_recipes_cover_chairs` requires of any
    catalogue a real run seals.
    """
    rows: list[dict[str, Any]] = [
        {
            "kind": "fixture",
            "recipe": recipe,
            "chair": chair,
            "tier": tier,
            "description": "offline walking-skeleton fixture row",
        }
        for chair, recipe in (
            ("designator_surya", "fake-surya-v0"),
            ("secondary_proposer", "fake-secondary-proposer-v0"),
            ("reconstructor", "fake-reconstructor-v0"),
        )
        for tier in TIERS
    ]
    for index, chair in enumerate(LIVE_CHAIRS):
        identity = registry.resolve(chair)
        identity_digest = chair_preflight_identity_digest(identity)
        for tier in TIERS:
            row = _vllm_row(
                recipe=identity.serving_recipe, chair=chair, tier=tier, port=8300 + index
            )
            row["preflight_identity_digest"] = identity_digest
            row["preflight_digest"] = profile_preflight_digest(row)
            rows.append(row)
    path.write_text(
        'schema = "serving-recipes.v1"\n\n' + "\n".join(_toml_profile(row) for row in rows),
        encoding="utf-8",
    )
    # Parsed once here so a malformed catalogue fails in this helper, naming
    # itself, rather than four subprocesses later inside a stage program.
    load_serving_recipes(path)
    return path


# ------------------------------ driving the run -------------------------------


def stage_argv(run_root: Path, catalogue: Path, *, placement_tier: str | None) -> list[str]:
    """Exactly the flags `pipeline/orchestrator/run.py::invoke` gives every stage.

    Mirrored rather than imported: the point of the fixture-mode comparison
    below is that two independent drivers reach the same bytes, and a driver
    that borrowed the orchestrator's own argv builder could not notice a stage
    that had started reading something the orchestrator never sends.
    """
    config = ROOT / "config"
    argv = [
        "--run-root",
        str(run_root),
        "--run-id",
        RUN_ID,
        "--scenario",
        "happy",
        "--fixture-root",
        str(FIXTURE_ROOT),
        "--models-config",
        str(config / "models.toml"),
        "--decoding-config",
        str(config / "decoding.toml"),
        "--serving-recipes-config",
        str(catalogue),
        "--pdf-render-config",
        str(config / "pdf_render.toml"),
        "--designator-geometry-config",
        str(config / "designator_geometry.toml"),
        "--formats-config",
        str(config / "formats.toml"),
        "--recovery-config",
        str(config / "recovery.toml"),
        "--hard-failure-config",
        str(config / "hard_failure.toml"),
    ]
    if placement_tier is not None:
        argv += ["--placement-tier", placement_tier]
    argv += [
        "--witness-context",
        "named",
        "--perlector-protocol-config",
        str(config / "perlector_protocol.toml"),
        "--perlector-audit-config",
        str(config / "perlector_audit.toml"),
    ]
    return argv


def invoke_stage(program: str, run_root: Path, catalogue: Path, *, placement_tier=None) -> int:
    """Run one stage program as a real subprocess, and return its exit code.

    A held stage is not a failed one: the orchestrator carries a run past
    `EXIT_HELD` and stops only on the codes outside its own accepted set, and a
    live run's Recensor really does hold (see the completion test below). This
    is stricter than the orchestrator, which also carries `EXIT_RUN_HALTED` to
    its own halt reporting, so a driver written here cannot be more permissive
    than the one operators use.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / program),
            *stage_argv(run_root, catalogue, placement_tier=placement_tier),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode in (EXIT_COMPLETE, EXIT_HELD), (
        f"{program} exited {result.returncode}: {result.stderr}"
    )
    return result.returncode


def run_in_process(
    module, run_root: Path, catalogue: Path, *, placement_tier, serving_factory, **seams
):
    """Call one stage's own `main` here, with the serving seam injected.

    `seams` are any further in-process seams that stage's `main` takes, such
    as the Designator's `surya_runner`.

    `main(serving_factory=…)` is the sanctioned in-process injection point and
    is not what makes a run live: the sealed row kind decides that, and this
    same call with the committed catalogue takes the fixture path.

    `sys.argv[0]` is the stage's own program path, as it would be under the
    orchestrator: argparse reads it for the usage line, and a stand-in name
    there would make a stage's own refusal text name a file that does not
    exist.
    """
    argv = [module.__file__] + stage_argv(run_root, catalogue, placement_tier=placement_tier)
    original = sys.argv
    sys.argv = argv
    try:
        return module.main(serving_factory=serving_factory, **seams)
    finally:
        sys.argv = original


def snapshot(root: Path) -> dict[str, str]:
    """Every file under a runs root, by relative path and digest."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --------------------------------- the chairs ---------------------------------


class RecordingEndpoint(FakeEndpoint):
    """A scripted endpoint that also keeps the exact bytes it served.

    `FakeEndpoint` records the requests it received, which is what the stage
    suites need. Here the claim runs the other way too — the blob a record
    names must be the bytes the engine actually sent — and only the endpoint
    knows those, so it keeps them.
    """

    def __init__(self, **keywords: Any) -> None:
        super().__init__(**keywords)
        self.served: list[bytes] = []

    def request(self, method: str, url: str, *, body: bytes | None, timeout_seconds: float):
        response = super().request(method, url, body=body, timeout_seconds=timeout_seconds)
        if method == "POST" and url.endswith("/chat/completions"):
            self.served.append(response.body)
        return response


class _TreeBlobs:
    """`FakeEndpoint`'s response-as-arrival probe, over the real run tree."""

    def __init__(self, context, stage: str) -> None:
        self.context = context
        self.stage = stage

    def has(self, sha256: str) -> bool:
        tree = self.context.tree
        return tree.resolve(tree.blob_path(self.stage, sha256)).exists()


def witness_scripts() -> dict[str, list[ScriptedAnswer]]:
    """One answer per unit of each chair's own sealed scope.

    The two page chairs answer once per page. DAI is page-scoped and asked once
    per record its detector found, in the detector's order: the fixture's three
    records are a1, a2 and a2's continuation on page 2.
    """
    return {
        "attestator_1": [
            ScriptedAnswer(content=CHANDRA_PAGE_ONE, finish_reason="stop"),
            ScriptedAnswer(content=CHANDRA_PAGE_TWO, finish_reason="stop"),
        ],
        "attestator_2": [
            ScriptedAnswer(content=answer, finish_reason="stop")
            for answer in (DAI_ACT_ONE, DAI_ACT_TWO, DAI_CONTINUATION)
        ],
        "attestator_3": [
            ScriptedAnswer(content=CHURRO_PAGE_ONE, finish_reason="stop"),
            ScriptedAnswer(content=CHURRO_PAGE_TWO, finish_reason="stop"),
        ],
    }


class WitnessWorld:
    """A serving factory over one scripted endpoint per witness chair.

    One endpoint per chair rather than one shared: `FakeEndpoint` auto-answers
    the first POST it ever sees as the manager's readiness probe, so a second
    chair sharing an instance would eat a scripted reading answer.
    """

    def __init__(self, catalogue: Path, decoding_sha256: str, work: Path, scripts=None) -> None:
        self.catalogue = catalogue
        self.decoding_sha256 = decoding_sha256
        self.work = work
        self.work.mkdir(parents=True, exist_ok=True)
        self.scripts = witness_scripts() if scripts is None else scripts
        self.endpoints: dict[str, RecordingEndpoint] = {}
        self.loads: list[str] = []

    def factory(self, context, identity, tier: str) -> ChairClient:
        self.loads.append(identity.role)
        endpoint = RecordingEndpoint(
            served_model_id=f"served-{identity.role}",
            blob_store=_TreeBlobs(context, ATTESTATORES),
            # Response-as-arrival, checked from outside the client: the exact
            # bytes of the previous reading must already be on disk, by their
            # own digest, before the next request is allowed to leave.
            assert_retained_before_next_request=True,
        )
        endpoint.script(*self.scripts.get(identity.role, []))
        self.endpoints[identity.role] = endpoint
        manager = ServingManager(
            registry=context.registry,
            recipes=load_serving_recipes(self.catalogue),
            config_inputs=ServingConfigInputs.from_record(dict(context.serving_config_inputs)),
            launcher=FakeLauncher(endpoint),
            http=endpoint,
            receipt_publisher=StageContextReceiptPublisher(context),
            log_root=self.work / "serving-logs" / identity.role,
            package_inspector=FakePackages({"vllm": "0.test"}),
            residency_lease=FileResidencyLease(self.work / "pod-gpu.lock"),
            producer="pipeline/3_attestatores/run.py",
        )
        return ChairClient(
            manager=manager,
            identity=identity,
            tier=tier,
            retain=lambda data: retain_chair_bytes(context, data),
            decoding_config_sha256=self.decoding_sha256,
            decoding_policy=shipped_decoding_policy()[0],
            # Bare, not through a converter: `ChairClient.__enter__` normalizes
            # the manager's read-only receipt reference itself.
            read_receipt=context.tree.read_run_receipt,
        )

    def served(self, chair: str) -> list[bytes]:
        endpoint = self.endpoints.get(chair)
        return [] if endpoint is None else endpoint.served


# --------------------------------- the fixtures -------------------------------


@pytest.fixture(scope="module")
def designated(tmp_path_factory) -> SimpleNamespace:
    """One run tree carried to the Designator boundary under a live catalogue.

    Built once by the four real stage programs; every test below copies it, so
    no test writes into another's evidence.
    """
    work = tmp_path_factory.mktemp("live-seam-e2e")
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    catalogue = write_live_catalogue(work / "serving_recipes_live.toml", registry)
    run_root = work / "designated"
    for program in CHAIN_TO_DESIGNATOR:
        assert invoke_stage(program, run_root, catalogue) == EXIT_COMPLETE, program
    _policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
    return SimpleNamespace(
        work=work, catalogue=catalogue, run_root=run_root, decoding_sha256=decoding_sha256
    )


def read_by_live_witnesses(
    designated: SimpleNamespace, run_root: Path, work: Path, scripts=None
) -> WitnessWorld:
    world = WitnessWorld(designated.catalogue, designated.decoding_sha256, work, scripts)
    exit_code = run_in_process(
        attestatores,
        run_root,
        designated.catalogue,
        placement_tier=TIER,
        serving_factory=world.factory,
    )
    assert exit_code == EXIT_COMPLETE
    return world


@pytest.fixture(scope="module")
def witnessed(designated) -> SimpleNamespace:
    """The Designator tree, read once by the whole live witness roster."""
    run_root = designated.work / "witnessed"
    shutil.copytree(designated.run_root, run_root)
    world = read_by_live_witnesses(designated, run_root, designated.work / "witness-world")
    return SimpleNamespace(run_root=run_root, world=world)


# ============================ the live page seam ==============================

PAGE_ANSWERS = {
    row["page_ordinal"]: row["answer"]
    for row in tomllib.loads((FIXTURE_ROOT / "skeleton_fixture.toml").read_text(encoding="utf-8"))[
        "page_answer"
    ]
    if row["scenario"] == "happy"
}


class PageReaderWorld:
    """The Perlector's single resident chair, answering each page with its scripted answer.

    `answers` maps each page ordinal to the reply text; the fixture's `happy`
    answers by default.
    """

    def __init__(self, catalogue: Path, work: Path, answers: dict[int, str] | None = None) -> None:
        self.catalogue = catalogue
        self.work = work
        self.work.mkdir(parents=True, exist_ok=True)
        self.answers = PAGE_ANSWERS if answers is None else answers
        self.endpoint: RecordingEndpoint | None = None

    def factory(self, context, identity, tier: str) -> ChairClient:
        policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
        endpoint = RecordingEndpoint(
            served_model_id=f"served-{identity.role}",
            blob_store=_TreeBlobs(context, PERLECTOR),
            assert_retained_before_next_request=True,
        )
        endpoint.script(
            *(
                ScriptedAnswer(content=self.answers[ordinal], finish_reason="stop")
                for ordinal in (1, 2)
            )
        )
        self.endpoint = endpoint
        manager = ServingManager(
            registry=context.registry,
            recipes=load_serving_recipes(self.catalogue),
            config_inputs=ServingConfigInputs.from_record(dict(context.serving_config_inputs)),
            launcher=FakeLauncher(endpoint),
            http=endpoint,
            receipt_publisher=StageContextReceiptPublisher(context),
            log_root=self.work / "serving-logs",
            package_inspector=FakePackages({"vllm": "0.test"}),
            residency_lease=FileResidencyLease(self.work / "pod-gpu.lock"),
            producer="pipeline/4_perlector/run.py",
        )
        return ChairClient(
            manager=manager,
            identity=identity,
            tier=tier,
            retain=lambda data: retain_chair_bytes(context, data),
            decoding_config_sha256=decoding_sha256,
            decoding_policy=policy,
            read_receipt=context.tree.read_run_receipt,
        )


def test_a_live_page_read_run_carries_on_through_the_recensor_to_a_sealed_terminal_export(
    designated, witnessed, tmp_path
):
    """Live witnesses and a live page reader, then the stages after them, to an export.

    The Recensor, Archetypus and Armarium read records carrying `engine_call` and
    serving receipts, which no fixture record has; running them here is what says
    a live run reaches an export at all. The fixture's `happy` pages carry an act
    across their break, so every reading is delivered and the run is partial for
    exactly that join, which code never makes (`no-code-join`).
    """
    run_root = tmp_path / "runs"
    shutil.copytree(witnessed.run_root, run_root)
    reader = PageReaderWorld(designated.catalogue, tmp_path / "reader")
    assert (
        run_in_process(
            perlector,
            run_root,
            designated.catalogue,
            placement_tier=TIER,
            serving_factory=reader.factory,
        )
        == EXIT_COMPLETE
    )
    # One chat request per sealed page, each answered once.
    assert len([request for request in reader.endpoint.requests if "messages" in request]) == 2
    tail = {
        program: invoke_stage(program, run_root, designated.catalogue, placement_tier=TIER)
        for program in TAIL_FROM_RECENSOR
    }
    assert tail == {
        "pipeline/5_recensor/run.py": EXIT_COMPLETE,
        "pipeline/6_archetypus/run.py": EXIT_COMPLETE,
        "pipeline/4b_coniector/run.py": EXIT_COMPLETE,
        "pipeline/7_armarium/run.py": EXIT_HELD,
    }

    tree = RunTree(run_root, RUN_ID)
    readings = {
        record["subject_id"]: record
        for record in (
            tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
            for entry in tree.build_manifest(PERLECTOR)["artifacts"]
            if entry["kind"] == "perlectio"
        )
    }
    assert {record["outcome"] for record in readings.values()} == {"read"}
    assert all(record["payload"]["engine_call"] is not None for record in readings.values())

    export = verify_final_seal(tree)
    payload = export["payload"]
    assert sorted(item["act_key"] for item in payload["delivered"]) == ["p1:1", "p1:2", "p2:1"]
    assert payload["non_delivered"] == []
    for item in payload["delivered"]:
        assert item["text"] == readings[item["act_id"]]["payload"]["text"]
        assert item["witnesses"], item["act_key"]
    aggregate = payload["aggregate"]
    assert aggregate["status"] == "partial"
    (reason,) = aggregate["reasons"]
    assert reason.startswith("continuation join join-1-2-0 (not-reconstructed)")
    assert "(no-code-join)" in reason
    assert export["outcome"] == ArmariumCategory.HELD_FOR_REVIEW.value
