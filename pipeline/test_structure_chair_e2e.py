"""A live structure-chair run harness, and the scripted refusal shapes it documents.

A tmp catalogue marks `designator_structure` live beside the three witness chairs
and the Perlector; the Door, Exemplar and Ink Map build the tree as real
programs, and the Designator's live pass asks the structure chair for each sealed
page. `page_break_run` and its helpers are what
`pipeline/5_recensor/test_continuation_candidate_hold.py` drives. The structure
chair's answers are built by `operations/serving/fakes.py`'s scripted-answer
builders, which read what they built through the grammar that will read it live.

Nothing here starts a pod, opens a socket, loads a model or reaches a network.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import load_stage, programs_through

ROOT = Path(__file__).resolve().parents[1]
DESIGNATOR_DIR = ROOT / "pipeline" / "2_designator"
# The Designator program imports its own directory's modules by bare name, the
# stage import boundary `pipeline/test_stage_import_boundaries.py` enforces.
# `test_live_reading_seam_e2e` does the same for the two stages it loads; the
# three directories share no module name, so none shadows another.
if str(DESIGNATOR_DIR) not in sys.path:
    sys.path.insert(0, str(DESIGNATOR_DIR))

# The live-seam suite already owns a driver for this exact shape of run: the
# orchestrator's own argv, one stage program per subprocess, two stages called
# in process behind a scripted endpoint, and one fake world per chair. Importing
# it rather than copying it is what keeps the two suites' claims comparable --
# if the driver ever stopped matching what an operator runs, both would notice
# together, instead of this one quietly passing against a private copy.
from test_live_reading_seam_e2e import (  # noqa: E402
    RUN_ID,
    TIER,
    TIERS,
    WITNESS_CHAIRS,
    ReaderWorld,
    RecordingEndpoint,
    WitnessWorld,
    _toml_profile,
    _vllm_row,
    attestatores,
    invoke_stage,
    perlector,
)
from test_structure_pass import _real_submission as real_submission  # noqa: E402

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.stages import DESIGNATOR  # noqa: E402
from common.decoding import (  # noqa: E402
    load_decoding_policy,
)
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
)
from operations.serving.assembly import retain_chair_bytes  # noqa: E402
from operations.serving.client import ChairClient  # noqa: E402
from operations.serving.config import (  # noqa: E402
    ServingConfigInputs,
    chair_preflight_identity_digest,
    load_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.fakes import (  # noqa: E402
    FakeLauncher,
    FakePackages,
    InProcessSurya,
    ScriptedAnswer,
    scriptable_structure_refusals,
    scripted_structure_answer,
    structure_box_1000,
)
from operations.serving.manager import ServingManager, StageContextReceiptPublisher  # noqa: E402
from operations.serving.residency import FileResidencyLease  # noqa: E402
from proof.build_fixture import SURYA_BLOCKS, SURYA_LINES  # noqa: E402
from proof.synthetic_pages import PAGE_BREAK_PAGES, render_page  # noqa: E402

designator = load_stage("2_designator")
structure_pass = designator.structure_pass

CHAIN_TO_INK_MAP = programs_through("ink-map")
TAIL_FROM_RECENSOR = (
    "pipeline/5_recensor/run.py",
    "pipeline/6_archetypus/run.py",
    "pipeline/7_armarium/run.py",
)
LIVE_CHAIRS = ("designator_structure", *WITNESS_CHAIRS, "perlector")
PAGE_WIDTH, PAGE_HEIGHT = 200, 260

ACT_KEYS = ("proposal:1:0", "proposal:1:1", "proposal:2:0")


def labelled(page_acts, labels):
    """The same rectangles and texts, each carrying one scripted label."""
    return tuple(
        (bounds, text, label) for (bounds, text), label in zip(page_acts, labels, strict=True)
    )


# ------------------------------ the tmp catalogue -----------------------------


def write_catalogue(path: Path, registry) -> Path:
    """Every configured chair live at every tier, the structure chair included,
    and Surya on its subprocess rows.

    The live-seam suite writes the same file with `designator_structure` left
    on its fixture rows; the one difference is the whole point of this module,
    so the catalogue is built here rather than by importing that helper and
    passing it a flag.
    """
    rows: list[dict[str, Any]] = []
    for index, chair in enumerate(LIVE_CHAIRS):
        identity = registry.resolve(chair)
        identity_digest = chair_preflight_identity_digest(identity)
        for tier in TIERS:
            row = _vllm_row(
                recipe=identity.serving_recipe, chair=chair, tier=tier, port=8400 + index
            )
            if chair == "designator_structure":
                # The row moves, never the arithmetic and never the pixels --
                # the same correction the reading seam's own stand-in made for
                # Churro when Unit 12 changed what that chair is asked. This
                # chair is now asked in Chandra's carried `OCR_LAYOUT_PROMPT`
                # (`verbatus-structure-prompt.v3`), so its two sealed constants
                # were re-measured at 593 prompt tokens and a 1,645-token dense
                # answer; with this fixture's 48 image tokens that needs 2,286
                # and the shared stand-in's 2,048 refuses it before anything is
                # sent. The shipped real catalogue states 8,192 for this chair
                # at every tier, so the stand-in states it here too rather than
                # shrinking a measured cost to fit a test's number.
                row["max_model_len"] = 8192
            row["preflight_identity_digest"] = identity_digest
            row["preflight_digest"] = profile_preflight_digest(row)
            rows.append(row)
    # Surya runs beside the structure chair, as a subprocess on the CPU.
    surya = registry.resolve("designator_surya")
    rows.extend(
        {
            "kind": "subprocess",
            "recipe": surya.serving_recipe,
            "chair": "designator_surya",
            "tier": tier,
            "engine": "surya",
            "environment": "operations/serving/surya",
            "device": "cpu",
            "threads": 2,
            "startup_timeout_seconds": 300,
            "seconds_per_page": 60,
            "required_packages": {"surya-ocr": "0.22.1", "torch": "2.14.0"},
        }
        for tier in TIERS
    )
    path.write_text(
        'schema = "serving-recipes.v1"\n\n' + "\n".join(_toml_profile(row) for row in rows),
        encoding="utf-8",
    )
    # Parsed once here, so a malformed catalogue names itself rather than
    # failing inside a stage program three subprocesses later.
    load_serving_recipes(path)
    return path


# --------------------------- the structure chair --------------------------------


class StructureWorld:
    """A serving factory over one scripted endpoint for the structure chair.

    Deliberately close to production's `stage_chair_client`: the same
    manager, the same real `StageContextReceiptPublisher`, the same
    `retain_chair_bytes` into the Designator's own blob area, the same receipt
    re-read through the tree. The launcher, the transport and the package
    inspector are the fakes; nothing else is.
    """

    def __init__(
        self,
        catalogue: Path,
        work: Path,
        answers: list[ScriptedAnswer],
        surya: InProcessSurya | None = None,
    ) -> None:
        self.catalogue = catalogue
        self.work = work
        self.work.mkdir(parents=True, exist_ok=True)
        self.answers = answers
        self.endpoint: RecordingEndpoint | None = None
        # Surya's subprocess, answered here from the fixture's declared rows.
        self.surya = InProcessSurya(SURYA_LINES, SURYA_BLOCKS) if surya is None else surya

    def factory(self, context, identity, tier: str) -> ChairClient:
        policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
        endpoint = RecordingEndpoint(
            served_model_id=f"served-{identity.role}",
            blob_store=_TreeBlobs(context, DESIGNATOR),
            assert_retained_before_next_request=True,
        )
        endpoint.script(*self.answers)
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
            producer="pipeline/2_designator/run.py",
        )
        return ChairClient(
            manager=manager,
            identity=identity,
            tier=tier,
            retain=lambda data: retain_chair_bytes(context, data),
            decoding_config_sha256=decoding_sha256,
            decoding_policy=policy,
            read_receipt=lambda reference: context.tree.read_run_receipt(dict(reference)),
        )


class _TreeBlobs:
    """`FakeEndpoint`'s response-as-arrival probe, over the real run tree."""

    def __init__(self, context, stage: str) -> None:
        self.context = context
        self.stage = stage

    def has(self, sha256: str) -> bool:
        tree = self.context.tree
        return tree.resolve(tree.blob_path(self.stage, sha256)).exists()


# ------------------------------ the witness roster ------------------------------


def chandra_page(blocks) -> str:
    """One page's Chandra answer in the vendor's own layout grammar.

    Top-level divs carrying a `data-bbox` normalized 0-1000, which is what
    `chandra/prompts.py::OCR_LAYOUT_PROMPT` asks for and what
    `common/chandra_layout.py` reads. The block geometry is built from the same
    page-pixel rectangles the structure chair drew, through the same normalized
    conversion both readings of a page share (`common.structure_answer`), so a
    block lands exactly on the act it reports rather than approximately near it.

    Only newlines separate the divs: character data outside every top-level
    block is ink no block carries, and the grammar reports it as a finding
    rather than silently dropping it.
    """
    return "\n".join(
        '<div data-bbox="{} {} {} {}" data-label="Text">{}</div>'.format(
            *structure_box_1000(bounds, PAGE_WIDTH, PAGE_HEIGHT), text
        )
        for bounds, text in blocks
    )


def witness_scripts(page_acts) -> dict[str, list[ScriptedAnswer]]:
    """One answer per unit of each chair's own sealed scope, over the given pages.

    One answer per page for the page-scoped chairs, one per act for the act-scoped
    one: the same corpus read through two scopes, and a script whose length
    disagreed with that is the first thing that would notice a scope
    regression. DAI's answers are in seal order, which the act-by-act
    assertion below then proves rather than assumes.
    """
    return {
        "attestator_1": [
            ScriptedAnswer(content=chandra_page(acts), finish_reason="stop") for acts in page_acts
        ],
        "attestator_2": [
            ScriptedAnswer(content=text, finish_reason="stop")
            for acts in page_acts
            for _bounds, text in acts
        ],
        "attestator_3": [
            ScriptedAnswer(
                content="<output>" + "\n".join(text for _b, text in acts) + "</output>",
                finish_reason="stop",
            )
            for acts in page_acts
        ],
    }


# ------------------------------ reading the tree --------------------------------


def artifacts(root: Path, stage: str, kind: str) -> list[dict[str, Any]]:
    tree = RunTree(root, RUN_ID)
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind
    ]


def seal_rows(root: Path) -> dict[str, dict[str, Any]]:
    (seal,) = artifacts(root, DESIGNATOR, "proposal-seal")
    return {row["act_key"]: row for row in seal["payload"]["expected_acts"]}


# --------------------------------- the fixtures ---------------------------------


@pytest.fixture(scope="module")
def designated(tmp_path_factory) -> SimpleNamespace:
    """One run tree carried to the Ink Map's seal under the live catalogue.

    Built once by the three real stage programs; every test below copies it, so
    no test writes into another's evidence. The Designator is deliberately not
    part of the chain: it is the stage under test, and it runs in this process
    against the scripted chair.
    """
    work = tmp_path_factory.mktemp("structure-chair-e2e")
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    catalogue = write_catalogue(work / "serving_recipes_live.toml", registry)
    run_root = work / "inked"
    for program in CHAIN_TO_INK_MAP:
        assert invoke_stage(program, run_root, catalogue) == EXIT_COMPLETE, program
    return SimpleNamespace(work=work, catalogue=catalogue, run_root=run_root)


def fresh_tree(designated: SimpleNamespace, tmp_path: Path, name: str = "runs") -> Path:
    run_root = tmp_path / name
    shutil.copytree(designated.run_root, run_root)
    return run_root


# =============================== the other answers ===============================


_SCRIPTABLE_STRUCTURE_REFUSALS = scriptable_structure_refusals()


def test_the_scripted_refusal_set_is_the_two_shapes_this_suite_documents():
    """A parametrize decorator over an empty sequence collects zero cases and
    passes silently rather than failing loudly, so the coverage this suite
    documents is asserted here, as an ordinary test, rather than a module-level
    assert that would surface as a collection error instead of a test failure.
    """
    assert len(_SCRIPTABLE_STRUCTURE_REFUSALS) == 2, _SCRIPTABLE_STRUCTURE_REFUSALS


# ============================== an act across a page break ==============================

# `proof/synthetic_pages.PAGE_BREAK_PAGES`: page 1's second act reaches the
# bottom edge and page 2 opens on its tail, unanchored, at the top edge. The
# chair answers one page at a time, so it draws the tail as an act of its own.
PAGE_BREAK_ACTS = {
    page["ordinal"]: tuple(
        (act["bounds"], f"PAGE BREAK {page['ordinal']} ACT {index}")
        for index, act in enumerate(page["acts"])
    )
    for page in PAGE_BREAK_PAGES
}
PAGE_BREAK_HEAD, PAGE_BREAK_TAIL = "proposal:1:1", "proposal:2:0"


def _real_argv(run_root: Path, catalogue: Path) -> list[str]:
    """A real submission's flags: the roster, the catalogue and the tier, nothing fixture."""
    return [
        "--run-root",
        str(run_root),
        "--run-id",
        RUN_ID,
        "--models-config",
        str(ROOT / "config" / "models.toml"),
        "--serving-recipes-config",
        str(catalogue),
        "--placement-tier",
        TIER,
    ]


def _run_real_in_process(module, run_root: Path, catalogue: Path, serving_factory, **seams) -> int:
    original = sys.argv
    sys.argv = [module.__file__, *_real_argv(run_root, catalogue)]
    try:
        return module.main(serving_factory=serving_factory, **seams)
    finally:
        sys.argv = original


def page_break_run(work: Path) -> SimpleNamespace:
    """The page-break pair, submitted for real, marked out live and read to the export."""
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    catalogue = write_catalogue(work / "serving_recipes_live.toml", registry)
    pages = {f"page-{page['ordinal']}.png": render_page(page) for page in PAGE_BREAK_PAGES}
    run_root = real_submission(
        work,
        pages,
        "--models-config",
        str(ROOT / "config" / "models.toml"),
        "--serving-recipes-config",
        str(catalogue),
    )
    answers = [
        scripted_structure_answer(PAGE_BREAK_ACTS[ordinal], PAGE_WIDTH, PAGE_HEIGHT)
        for ordinal in sorted(PAGE_BREAK_ACTS)
    ]
    # The fixture declares no Surya detections for these pages, so Surya finds none.
    structure = StructureWorld(
        catalogue, work / "structure-world", answers, surya=InProcessSurya((), ())
    )
    designator_exit = _run_real_in_process(
        designator, run_root, catalogue, structure.factory, surya_runner=structure.surya
    )

    _policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
    page_acts = [PAGE_BREAK_ACTS[ordinal] for ordinal in sorted(PAGE_BREAK_ACTS)]
    witnesses = WitnessWorld(
        catalogue,
        decoding_sha256,
        work / "witness-world",
        witness_scripts(page_acts),
    )
    witness_exit = _run_real_in_process(attestatores, run_root, catalogue, witnesses.factory)
    reader = ReaderWorld(catalogue, work / "reader", finish_reason="stop")
    reader_exit = _run_real_in_process(perlector, run_root, catalogue, reader.factory)
    tail = {}
    for program in TAIL_FROM_RECENSOR:
        result = subprocess.run(
            [sys.executable, str(ROOT / program), *_real_argv(run_root, catalogue)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode in (EXIT_COMPLETE, EXIT_HELD), f"{program}: {result.stderr}"
        tail[program] = result.returncode
    return SimpleNamespace(
        run_root=run_root,
        exits=(designator_exit, witness_exit, reader_exit),
        tail=tail,
    )
