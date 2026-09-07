"""The whole live reading seam over one run tree, from the Door to the export.

Every other suite in this section proves one stage's own wiring. This one
proves the seam *between* them: a run tree carried to the Designator by the
real stage programs, read by an Attestatores roster whose three chairs really
served, then by a Perlector chair that really served, and then carried on
through the Recensor, Archetypus and Armarium — which have never before been
asked to consume a live tree — to a terminal export.

Nothing here starts a pod, opens a socket, loads a model or reaches a network.
The chairs answer through `operations/serving/fakes.py`: a scripted
`FakeEndpoint` behind a real `ServingManager`, a real `ChairClient`, and each
stage's own `main`. What makes the run live is the sealed serving-recipe row
kind, exactly as it would be on a card — the tmp catalogue below marks the
three witness chairs and the Perlector `kind = "vllm"` at every tier
`config/pod_placement.toml` defines, and the Designator, which has no live
reader, keeps its fixture rows.

**The roster is the committed one, complete.** All three witness chairs go
live, through all three witness scopes and adapters (`chandra.v1` page,
`dai.v1` act, `churro.v1` page), and so does the Perlector. If a chair ever
stops being able to go live, this module is where that shows up as a named
refusal rather than as a quietly shortened roster.

**What this scripted run produces is a delivered export, and that is measured
here rather than asserted around.** Every act is read, every reading names the
bytes its engine sent, and the run seals a terminal export. Until Unit 12 it was
held twice over: two witnesses of a floor of three counted, because Churro
published no native layout and so never attached to an act by geometry; and,
unasserted, testimony content coverage held every page, because an unattached
witness's whole page text is uncovered. Churro is now asked for block geometry
and answers with it, so it attaches by its own boxes, three of three count, and
this fixture's page text — which is exactly its two acts — is fully covered by
those acts' aligned spans. Both causes are asserted separately below, by name,
so that either returning says which.

**A delivered offline e2e is not a proven pipeline** (GOVERNANCE 10, hard rule
1). One scripted run over a synthetic fixture reaches `delivered`; nothing
follows about a real page. A real register carries headers, folio numbers and
marginalia no proposal covers, Churro will transcribe them, and that page will
hold on content coverage — the rule working, not a regression.

The last test is the counterweight: the identical driver, in fixture mode,
against the tree `pipeline/orchestrator/run.py` produces for itself. They must
be byte-identical — which is what says the live seam, the `--placement-tier`
flag, and calling two stages in-process rather than as subprocesses have moved
no fixture byte. It is compared against the tree that orchestration produces in
this same test rather than against a copied digest constant: a pin nobody
recomputes would go stale silently, and the acceptance suite already owns the
constant.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
ATTESTATORES_DIR = ROOT / "pipeline" / "3_attestatores"
PERLECTOR_DIR = ROOT / "pipeline" / "4_perlector"
# Each stage program imports its own directory's modules by bare name
# (`import feeding`, `import live_reader`), which is the stage import boundary
# `pipeline/test_stage_import_boundaries.py` enforces. Loading two stages'
# `run.py` in one process therefore needs both directories importable; they
# share no module name, so neither shadows the other.
for _stage_directory in (ATTESTATORES_DIR, PERLECTOR_DIR):
    if str(_stage_directory) not in sys.path:
        sys.path.insert(0, str(_stage_directory))

from live_reader import EngineSignalRefusal  # noqa: E402

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.outcomes import ArmariumCategory  # noqa: E402
from common.contracts.serving import STOP_REASON_UNREPORTED  # noqa: E402
from common.contracts.stages import ATTESTATORES, PERLECTOR  # noqa: E402
from common.decoding import load_decoding_policy  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import EXIT_COMPLETE, EXIT_HELD, verify_final_seal  # noqa: E402
from operations.serving.client import ChairClient  # noqa: E402
from operations.serving.config import (  # noqa: E402
    ServingConfigInputs,
    chair_preflight_identity_digest,
    load_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.fakes import (  # noqa: E402
    ABSENT,
    FakeEndpoint,
    FakeLauncher,
    FakePackages,
    ScriptedAnswer,
)
from operations.serving.http import chat_image_bytes_all  # noqa: E402
from operations.serving.manager import ServingManager, StageContextReceiptPublisher  # noqa: E402
from operations.serving.residency import FileResidencyLease  # noqa: E402

RUN_ID = "r"
FIXTURE_ID = "synthetic-two-page-v0"
FIXTURE_ROOT = ROOT / "proof"
TIER = "generic-48gb"
TIERS = ("generic-24gb", "generic-48gb", "generic-80gb-plus")
WITNESS_CHAIRS = ("attestator_1", "attestator_2", "attestator_3")
LIVE_CHAIRS = (*WITNESS_CHAIRS, "perlector")
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
CHAIN_TO_DESIGNATOR = (
    "pipeline/1_exemplar/door.py",
    "pipeline/1_exemplar/run.py",
    "pipeline/1_ink_map/run.py",
    "pipeline/2_designator/run.py",
)
TAIL_FROM_RECENSOR = (
    "pipeline/5_recensor/run.py",
    "pipeline/6_archetypus/run.py",
    "pipeline/7_armarium/run.py",
)

# What each chair answers with. The witness bodies are the shapes their own
# adapters parse: Chandra answers in the closed contract its prompt asks for
# (`pipeline/3_attestatores/chandra_response.py`) -- block text with normalized
# `box_1000` geometry that converts, on this fixture's 200x260 pages, to
# exactly the sealed proposal rectangles of `a1`, `a2` and a2's page-2
# continuation; Churro speaks its `<output>` envelope once per page; DAI is
# act-scoped and answers plain text once per act. Churro answers its own closed
# contract on page 1 and its trained `<output>` envelope on page 2, so both of
# its legal shapes cross this seam.
CHANDRA_PAGE_ONE = (
    '{"schema": "verbatus-chandra-page-response.v1", "blocks": ['
    '{"box_1000": [100, 77, 900, 385], "text": "SYNTHETIC ACT ONE alpha beta gamma"}, '
    '{"box_1000": [100, 462, 900, 846], "text": "SYNTHETIC ACT TWO delta epsilon zeta eta"}]}'
)
# Page 2 carries only a2's continuation, and it is answered in the contract's
# page-text form. That is one legitimate answer of the two, exercised
# deliberately so this seam covers both across its two pages -- not the only
# one the Perlector will read. The geometry form on a continuation page is
# accepted now: `pipeline/4_perlector/run.py::act_attachment_view` still
# requires a page witness's `attached` to equal its geometric overlap with the
# act's sealed regions on that page, but no longer separately refuses an
# attached continuation entry (run.py ~1239-1259), because between the two
# rules such an entry had no legal spelling at all. What a continuation page
# genuinely lacks is an ANCHOR, and that is what the surviving rule says.
# `pipeline/4_perlector/test_live_perlector.py::test_a_page_witness_attached_by_geometry_on_a_continuation_page_is_readable`
# pins the geometry form here, and
# `pipeline/3_attestatores/test_attestatores_live_pass.py` pins it at the
# Attestatores' own boundary.
CHANDRA_PAGE_TWO = (
    '{"schema": "verbatus-chandra-page-response.v1", '
    '"text": "SYNTHETIC ACT TWO delta epsilon zeta eta"}'
)
# Churro answers page 1 in the closed shape ITS own prompt asks for
# (`feeding.churro_layout_prompt`, parsed by `common/churro_response.py`). The
# boxes are derived for this fixture rather than copied from `CHANDRA_PAGE_ONE`:
# Churro's blocks are Churro's own testimony, and two chairs reporting identical
# rectangles would make one page's geometry read as one reading counted twice.
# On these 200x260 pages they convert to {22,22 156x76} and {22,122 156x96},
# each inside the sealed proposal of the act it transcribes (a1 at 20,20 160x80;
# a2 at 20,120 160x100) with positive area -- so each block overlaps exactly the
# act it read, and nothing selects among witnesses (GOVERNANCE 3).
CHURRO_PAGE_ONE = (
    '{"schema": "verbatus-churro-page-response.v1", "blocks": ['
    '{"box_1000": [110, 85, 890, 375], "text": "SYNTHETIC ACT ONE alpha beta gamma"}, '
    '{"box_1000": [110, 470, 890, 835], "text": "SYNTHETIC ACT TWO delta epsilon zeta eta"}]}'
)
# Page 2 stays in the TRAINED envelope, deliberately, and it is the shape a
# model that ignores the layout clause would answer with everywhere -- so this
# module covers both of Churro's legal shapes across its two pages, exactly as
# it already does for Chandra's two forms. It is also the more conservative
# choice on a continuation page; the wire body there is pinned at the stage
# boundary instead
# (`pipeline/3_attestatores/test_attestatores_live_pass.py::test_a_churro_continuation_page_wire_body_attaches_and_carries_no_act_anchor`),
# where the same question is already pinned for Chandra.
CHURRO_PAGE_TWO = "<output>SYNTHETIC ACT TWO delta epsilon zeta eta</output>"
DAI_ACT_ONE = "SYNTHETIC ACT ONE alpha beta gamma"
DAI_ACT_TWO = "SYNTHETIC ACT TWO delta epsilon zeta eta"
# Long enough that `truncation.is_length_suspicious` never fires on this
# fixture's small regions: these tests are about the engine's own stop word,
# and a reading the length heuristic independently called suspicious would
# prove something else.
READING = "SYNTHETIC LIVE READING alpha beta gamma delta epsilon zeta eta theta iota kappa"


def _load_stage(directory: Path, name: str):
    """Load one stage program as a module, the way its own suite does."""
    spec = importlib.util.spec_from_file_location(name, directory / "run.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attestatores = _load_stage(ATTESTATORES_DIR, "e2e_attestatores_under_test")
perlector = _load_stage(PERLECTOR_DIR, "e2e_perlector_under_test")


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
        # Churro's row moved for the same reason at a different seam. Its prompt
        # and answer are sealed constants rather than a floor, but Unit 12
        # changed which prompt it is asked (`feeding.churro_layout_prompt`) and
        # which shape it answers in, so both constants were re-measured: 441 and
        # 1,631 against the trained framing's 281 and 1,433, which is 2,073 with
        # this fixture's one image token and does not fit 2,048. The shipped
        # catalogue states 8,192 for this chair at every tier, so the stand-in
        # states it too -- the row moves, never the arithmetic and never the
        # pixels. Chandra's row is untouched and still needs 1,777.
        "max_model_len": {"perlector": 16384, "attestator_3": 8192}.get(chair, 2048),
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
    """Every chair this seam can serve, live, at every tier the placement file names.

    The Designator keeps its fixture rows: this module's subject is the reading
    seam, and a live row for `designator_structure` would start its structure
    pass instead (`pipeline/2_designator/structure_pass.py`, exercised end to end
    in `pipeline/test_structure_chair_e2e.py`). Every other configured
    chair is live at all three tiers, which is also what
    `verify_recipes_cover_chairs` requires of any catalogue a real run seals.
    """
    rows: list[dict[str, Any]] = [
        {
            "kind": "fixture",
            "recipe": "fake-designator-v0",
            "chair": "designator_structure",
            "tier": tier,
            "description": "offline walking-skeleton fixture row",
        }
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
        "--designator-padding-config",
        str(config / "designator_padding.toml"),
        "--designator-geometry-config",
        str(config / "designator_geometry.toml"),
        "--alignment-config",
        str(config / "alignment.toml"),
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
        "--witness-context-config",
        str(config / "witness_context.toml"),
        "--nuda-per-mille",
        "0",
        "--nuda-approval-ref",
        "",
        "--perlector-instrument-per-mille",
        "0",
        "--perlector-instrument-approval-ref",
        "",
        "--perlector-protocol-config",
        str(config / "perlector_protocol.toml"),
        "--perlector-audit-config",
        str(config / "perlector_audit.toml"),
        "--draft-fed",
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


def run_in_process(module, run_root: Path, catalogue: Path, *, placement_tier, serving_factory):
    """Call one stage's own `main` here, with the serving seam injected.

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
        return module.main(serving_factory=serving_factory)
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


class VaryingReadingEndpoint(RecordingEndpoint):
    """A reader endpoint whose answer content is derived from the pixels it was sent.

    A fixed scripted answer, replayed for every reading POST, cannot say
    whether a Perlectio is bound to *its own* engine response or to any
    canonical one: sixty identical replies make every response blob
    identical too. Hashing the images the request actually carries into the
    content instead ties each answer to the act whose pixels asked for it,
    while answering the same for the Pass A / Pass B / re-proof calls of that
    one act.

    **Why the delivered images and not the whole body.** Pass A and Pass B do
    render the same request, but the audit re-proof does not: it appends every
    reproof prompt to the same dossier (`live_reader.read`), so a whole-body
    hash answers the re-proof with different text. That is not a re-proof this
    seam may serve. `audit.change_record` admits only a change that falls
    inside a flagged location, and a tail digest that moved sits inside a
    testimony-diff flag only when the witness text happens to end in the same
    character as the reading -- so the pass would refuse, or not, on a
    coincidence between a witness constant and a hash. The delivered pixels are
    the act's own bytes and are identical across its three passes, which is the
    property this endpoint needed all along.
    """

    def __init__(self, *, finish_reason: Any, **keywords: Any) -> None:
        super().__init__(**keywords)
        self._finish_reason = finish_reason

    def request(self, method: str, url: str, *, body: bytes | None, timeout_seconds: float):
        if (
            method == "POST"
            and url.endswith("/chat/completions")
            and self._readiness_probe_answered
        ):
            images = chat_image_bytes_all(json.loads(body)) if body is not None else []
            digest = hashlib.sha256(b"".join(images)).hexdigest()[:12] if images else "no-pixels"
            # Bracketed, not bare: a bare hex digest ends in "a" one time in
            # sixteen, and every witness body this fixture serves also ends
            # in "a" (`DAI_ACT_ONE`, `DAI_ACT_TWO`, and Churro's parsed
            # `<output>` text all end mid-word on "...gamma"/"...eta"). When
            # both coincide, a testimony-diff flag's suffix-trimmed end lands
            # one character short of a re-proof envelope that reaches the
            # true end of the text -- a real production coincidence
            # (`common.perlector_audit.change_record` now tolerates exactly
            # that one-byte gap), but not one this fixture needs to also
            # roll on every run. "]" is not a character any scripted witness
            # body ends with, so the coincidence this constant final
            # character could still produce is structural, not random.
            content = f"{READING} [{digest}]"
            assert content.startswith(READING)
            self.script(ScriptedAnswer(content=content, finish_reason=self._finish_reason))
        return super().request(method, url, body=body, timeout_seconds=timeout_seconds)


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

    Two pages carry this fixture's two acts, so a page-scoped chair answers
    twice and the act-scoped chair answers twice as well — the same corpus read
    through two different scopes. A script whose length disagreed with that is
    the first thing that would notice a scope regression.
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
            retain=lambda data: attestatores.retained_blob_ref(context, data),
            decoding_config_sha256=self.decoding_sha256,
            record_temperature=0,
            # Bare, not through a converter: `ChairClient.__enter__` normalizes
            # the manager's read-only receipt reference itself.
            read_receipt=context.tree.read_run_receipt,
        )

    def served(self, chair: str) -> list[bytes]:
        endpoint = self.endpoints.get(chair)
        return [] if endpoint is None else endpoint.served


class ReaderWorld:
    """The Perlector's single resident chair, over one scripted endpoint."""

    def __init__(self, catalogue: Path, work: Path, *, finish_reason: Any) -> None:
        self.catalogue = catalogue
        self.work = work
        self.work.mkdir(parents=True, exist_ok=True)
        self.finish_reason = finish_reason
        self.endpoint: RecordingEndpoint | None = None

    def factory(self, context, identity, tier: str) -> ChairClient:
        policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
        endpoint = VaryingReadingEndpoint(
            finish_reason=self.finish_reason,
            served_model_id=f"served-{identity.role}",
            blob_store=_TreeBlobs(context, PERLECTOR),
            assert_retained_before_next_request=True,
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
            retain=lambda data: perlector.retain_chair_bytes(context, data),
            decoding_config_sha256=decoding_sha256,
            record_temperature=policy["reading_of_record"]["temperature"],
            read_receipt=context.tree.read_run_receipt,
        )


# ------------------------------ reading the tree ------------------------------


def act_records(tree: RunTree) -> dict[tuple[str, str], dict[str, Any]]:
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        key = (record["subject_id"], record["payload"]["chair"])
        assert key not in records, (
            f"act_records saw two Testimonia for {key}: "
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


def published_readings(run_root: Path) -> list[dict[str, Any]]:
    """Every Perlectio on disk that records an attempted reading.

    Read from the artifact files rather than through a manifest: a pass that
    stopped never wrote one, and these tests must see exactly what a stopped
    pass did and did not publish.
    """
    directory = run_root / RUN_ID / "4_perlector" / "artifacts" / "perlectio"
    if not directory.exists():
        return []
    records = [json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")]
    return [record for record in records if record["outcome"] != "not-run"]


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


def fresh_tree(designated: SimpleNamespace, tmp_path: Path, name: str = "runs") -> Path:
    run_root = tmp_path / name
    shutil.copytree(designated.run_root, run_root)
    return run_root


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


@pytest.fixture(scope="module")
def live_seam(designated, witnessed, tmp_path_factory) -> SimpleNamespace:
    """The whole seam: live witnesses, a live reader, and the tail to the export.

    Run once for the several claims below, because each of them is about a
    different part of the same single run — reproducing the run per assertion
    would say nothing more and cost four more model-free passes.
    """
    work = tmp_path_factory.mktemp("live-seam-complete")
    run_root = work / "runs"
    shutil.copytree(witnessed.run_root, run_root)
    reader = ReaderWorld(designated.catalogue, work / "reader", finish_reason="stop")
    exit_code = run_in_process(
        perlector,
        run_root,
        designated.catalogue,
        placement_tier=TIER,
        serving_factory=reader.factory,
    )
    assert exit_code == EXIT_COMPLETE
    tail = {
        program: invoke_stage(program, run_root, designated.catalogue, placement_tier=TIER)
        for program in TAIL_FROM_RECENSOR
    }
    return SimpleNamespace(
        run_root=run_root,
        reader=reader,
        witnesses=witnessed.world,
        tail=tail,
        catalogue=designated.catalogue,
    )


# =============================== the live seam ================================


def test_every_act_reaches_a_perlectio_bound_to_the_bytes_the_engine_sent(live_seam):
    """The claim the whole seam exists for, checked from both ends.

    Every act the witnesses reported on is read, and each reading's
    `engine_call` names a blob in this run's own store whose bytes are exactly
    what the endpoint put on the wire — not merely a blob that exists, and not
    merely a digest that matches itself.
    """
    tree = RunTree(live_seam.run_root, RUN_ID)
    acts = {act_id for act_id, _chair in act_records(tree)}
    readings = published_readings(live_seam.run_root)
    assert acts, "the live witness pass reported on no act at all"
    assert {record["subject_id"] for record in readings} == acts

    served = live_seam.reader.endpoint.served
    assert served, "the live Perlector pass sent no reading request"
    response_digests = set()
    for record in readings:
        call = record["payload"]["engine_call"]
        retained = tree.read_bytes(call["raw_response_ref"]["relative_path"])
        assert retained in served, (
            "a Perlectio names retained bytes the engine never sent; the reading "
            "cannot be traced back to the response that produced it"
        )
        assert call["raw_response_ref"]["sha256"] == call["response_sha256"]
        assert json.loads(retained)["choices"][0]["message"]["content"].startswith(READING)
        # And the envelope binds both blobs as direct inputs, so an ordinary
        # artifact read re-hashes them rather than trusting a nested reference.
        bound = {reference["relative_path"] for reference in record["inputs"]}
        assert call["raw_response_ref"]["relative_path"] in bound
        assert call["call_record_ref"]["relative_path"] in bound
        response_digests.add(call["raw_response_ref"]["sha256"])
    # The endpoint's answer is derived from each request's own body
    # (`VaryingReadingEndpoint`), so two acts binding to the same response
    # digest would mean one act's Perlectio was proven against another
    # act's bytes, or against a canonical answer neither act actually sent.
    assert len(response_digests) == len(readings), (
        "two acts' Perlectios name the same response blob; per-act binding is "
        "asserted, not measured"
    )


def test_the_whole_live_roster_answered_through_its_own_scope(live_seam):
    """Three witness chairs, three adapters, three scopes — none dropped.

    A roster that quietly shrank to the chairs that happen to work is the
    failure this assertion exists to make impossible: every configured witness
    chair must have an act record naming the serving call that produced it.
    """
    tree = RunTree(live_seam.run_root, RUN_ID)
    records = act_records(tree)
    # One residency each, in the deterministic chair-outer order the schedule
    # builds: a second load of an unloaded chair is what `SingleChairResidency`
    # refuses, and a chair never loaded is a chair that never answered.
    assert live_seam.witnesses.loads == sorted(WITNESS_CHAIRS)
    assert {chair for _act, chair in records} == set(WITNESS_CHAIRS)
    for (act_id, chair), record in records.items():
        payload = record["payload"]
        assert "serving_call_ref" in payload, (
            f"{chair}'s record for {act_id} names no serving call, so nothing says a "
            "chair was ever asked; a live pass may not publish a declared answer"
        )
        call = json.loads(tree.read_bytes(payload["serving_call_ref"]["relative_path"]))
        assert call["schema"] == "chair-call-record.v1"
        assert call["chair"] == chair
        # The witness half of "every reading names the exact bytes its engine
        # sent": chain record -> adapter output -> wire content -> served
        # bytes, so `WitnessWorld.served` is actually exercised rather than
        # left as dead scaffolding.
        wire = tree.read_bytes(call["raw_response_ref"]["relative_path"])
        assert wire in live_seam.witnesses.served(chair)
        assert call["response_sha256"] == call["raw_response_ref"]["sha256"]
        assert json.loads(wire)["choices"][0]["message"]["content"].encode() == tree.read_bytes(
            payload["raw_response_ref"]["relative_path"]
        )


def test_every_finish_reason_travels_verbatim_from_the_wire_to_both_records(live_seam):
    """One engine word, recorded unchanged by two stages that mean it differently.

    The witness turns `"stop"` into `truncated: false` on a trusted boundary
    and the Perlector turns it into a complete reading, but neither rewrites
    the word itself: the call record on both sides carries what the engine
    said.
    """
    tree = RunTree(live_seam.run_root, RUN_ID)
    for (act_id, chair), record in act_records(tree).items():
        payload = record["payload"]
        call = json.loads(tree.read_bytes(payload["serving_call_ref"]["relative_path"]))
        assert call["finish_reason"] == "stop", (act_id, chair)
        assert payload["native_capture"]["transport_stop_reason"] == "stop"
    for record in published_readings(live_seam.run_root):
        assert record["payload"]["engine_call"]["finish_reason"] == "stop"
        assert record["payload"]["truncation"]["signals"]["stop_reason_declared"] == "stop"
        assert record["outcome"] == "read"


def test_the_receipts_on_provenance_are_the_receipts_the_chairs_really_published(live_seam):
    """GOVERNANCE 6: the record protects the past, so it names the real moment.

    A fixture posture writes a declared `fixture://` receipt. Every record this
    run wrote must instead name the receipt its own client re-read through the
    tree at start — for the witnesses and for the reader alike.
    """
    tree = RunTree(live_seam.run_root, RUN_ID)
    for (act_id, chair), record in act_records(tree).items():
        receipt = tree.read_run_receipt(record["payload"]["provenance"]["receipt_ref"])
        assert receipt["chair"] == chair, act_id
        assert receipt["engine"] == "vllm"
        assert not receipt["endpoint"].startswith("fixture://")
    for record in published_readings(live_seam.run_root):
        receipt = tree.read_run_receipt(record["payload"]["provenance"]["receipt_ref"])
        assert receipt["chair"] == "perlector"
        assert receipt["engine"] == "vllm"
        assert not receipt["endpoint"].startswith("fixture://")


def _reviews(live_seam) -> list[dict[str, Any]]:
    """Every Recensor review this run sealed, read straight off disk."""
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(
            (live_seam.run_root / RUN_ID / "5_recensor" / "artifacts" / "review").glob("*.json")
        )
    ]


def _assert_the_continuation_page_is_unmeasured_by_name(reviews: list[dict[str, Any]]) -> None:
    """Tyrel's ruling on Unit 12 F2, asserted on a live tree.

    Page 2 carries a2's continuation region and both page witnesses transcribe
    its whole text. No attachment on it can ever be `aligned` -- the Perlector
    declares every row there `continuation-page-no-act-anchor`, because the act
    anchor is derived from the act's own primary page -- so the diff has no span
    union to take. Before the ruling that produced `shortfall: True` on a page
    whose finding no act's review read: dropped in silence under a DELIVERED
    export. It is now recorded as unmeasured, by name, with the observation
    kept, on the act that spans the page. The counterfactual below drops the row
    again and requires this to fail.
    """
    # A list, not a lookup keyed by act and page: a second row for one act's
    # page is an accounting failure, and a dict comprehension would collapse it
    # into the row this helper then reads. The counterfactual below removes a
    # row; nothing here may quietly absorb an added one.
    rows = [
        ((review["payload"]["act_key"], row["page_ordinal"]), row)
        for review in reviews
        for row in review["payload"]["testimony_content_coverage_continuation"]
    ]
    assert [key for key, _row in rows] == [("a2", 2)], sorted(key for key, _row in rows)
    row = rows[0][1]
    assert row["shortfall"] is None, row
    assert sorted(row["by_chair"]) == ["attestator_1", "attestator_3"], row
    for chair, measured in sorted(row["by_chair"].items()):
        assert measured["attached_spans"] == [], chair
        assert measured["uncovered_non_whitespace"]["count"] == 34, chair
        assert f"chair {chair!r} saw 34 uncovered non-whitespace" in row["reason"], chair
    assert "continuation-page-no-act-anchor" in row["reason"], row["reason"]
    assert "page 2's testimony content coverage is unmeasured" in row["reason"], row["reason"]


def test_the_run_carries_on_through_the_recensor_to_a_sealed_terminal_export(live_seam):
    """The stages after the seam have never met a live tree before this one.

    The Recensor, Archetypus and Armarium read records carrying `engine_call`,
    `native_capture`, `serving_call_ref` and `raw_response_kind` — fields no
    fixture record has. Running them here is what says a live run reaches an
    export at all, rather than reaching the Perlector and stopping.

    Since Unit 12 it reaches a **delivered** one. Churro is asked for block
    geometry (`feeding.churro_layout_prompt`), answers in the shape
    `common/churro_response.py` declares, and attaches to each act by its own
    boxes against that act's own sealed proposal — so three of a floor of three
    count and no act is under-witnessed. Two independent things had to be true
    for `reasons == []`, and the assertion is written so that either failing
    says which: the witness floor, and testimony content coverage, which holds a
    page whenever a witness transcribed non-whitespace text no aligned act
    attachment covers. That second hold fired on every live export before this
    unit — Churro was unattached, so its whole page text was uncovered — and
    nothing asserted it, which is why it is now asserted BY NAME below rather
    than left to `reasons == []` to imply.

    **A delivered offline e2e is not a proven pipeline** (GOVERNANCE 10, hard
    rule 1). The claim is that one scripted run over a fixture whose page text
    is exactly its two acts reaches `delivered`, and nothing more. A real
    register page carries headers, folio numbers and marginalia the Designator
    did not propose; Churro will transcribe them; that page will hold on content
    coverage, and that is the rule working (GOALS 1), not a regression.
    """
    assert live_seam.tail == {
        "pipeline/5_recensor/run.py": EXIT_COMPLETE,
        "pipeline/6_archetypus/run.py": EXIT_COMPLETE,
        "pipeline/7_armarium/run.py": EXIT_COMPLETE,
    }
    export = verify_final_seal(RunTree(live_seam.run_root, RUN_ID))
    aggregate = export["payload"]["aggregate"]
    # The two halves, each named, before the aggregate that depends on both. A
    # bare `reasons == []` would fail identically whichever one broke.
    for review in _reviews(live_seam):
        coverage = review["payload"]["coverage"]
        assert coverage["under_witnessed"] is False, coverage
        content = review["payload"]["testimony_content_coverage"]
        assert content["shortfall"] is False, content
        for chair, measured in content["by_chair"].items():
            assert measured["uncovered_non_whitespace"]["count"] == 0, (chair, measured)
    # The third named half, since Tyrel's F2 ruling: the page neither act is
    # primary on. Delivered, and visibly unmeasured rather than silently clean.
    _assert_the_continuation_page_is_unmeasured_by_name(_reviews(live_seam))
    delivered = {item["act_key"]: item for item in export["payload"]["delivered"]}
    assert sorted(delivered) == ["a1", "a2"]
    assert delivered["a1"]["testimony_content_coverage_continuation"] == []
    assert [
        (row["page_ordinal"], row["shortfall"])
        for row in delivered["a2"]["testimony_content_coverage_continuation"]
    ] == [(2, None)]
    assert sorted(aggregate["reasons"]) == []
    assert export["outcome"] == ArmariumCategory.DELIVERED.value
    assert aggregate["status"] == "complete"
    assert {record["outcome"] for record in published_readings(live_seam.run_root)} == {"read"}


def test_the_continuation_row_is_present_on_the_unmodified_live_tree(live_seam):
    """The regression itself, run against this same live tree, unmodified.

    Before the F2 ruling every review read its primary `page_ordinal` alone, so
    page 2's finding existed in the Recensor and reached no record -- dropped
    in silence under a DELIVERED export. This asserts the restatement directly
    against what the live run actually produced: the row is there, named on
    `a2` page 2, `shortfall` is `None` (unmeasured, not silently clean), and
    both page witnesses are the ones counted. If production ever again omits
    the row, this test -- not a synthetic drop of it -- is what fails.
    """
    _assert_the_continuation_page_is_unmeasured_by_name(_reviews(live_seam))


def test_the_unmeasured_assertion_itself_catches_a_dropped_continuation_row(live_seam):
    """Helper-drill: proves the assertion above is not vacuously true.

    Takes this same live tree's reviews, deletes the continuation row the way
    the pre-F2 code silently did, and requires
    `_assert_the_continuation_page_is_unmeasured_by_name` to reject that tree.
    This does not stand in for the regression test above -- it only shows that
    test's own assertion has teeth.
    """
    dropped = copy.deepcopy(_reviews(live_seam))
    for review in dropped:
        review["payload"]["testimony_content_coverage_continuation"] = []

    with pytest.raises(AssertionError):
        _assert_the_continuation_page_is_unmeasured_by_name(dropped)


def test_the_witness_coverage_a_live_run_reaches_is_named_chair_by_chair(live_seam):
    """Which chair counts live, and exactly on what evidence — no silent roster.

    The whole roster is served and every chair reads. Chandra's page response
    parses under the contract its prompt asks for, its own block geometry
    overlaps the acts, and the live alignment anchor is derived from that same
    response. DAI is shown one act crop, so its basis is `presented-region`.
    Churro answers the closed shape its own prompt asks for and overlaps the
    same acts with ITS OWN boxes — two chairs independently reporting geometry
    over one rectangle, not one chair's geometry attributed to another and not
    anything selecting among them (GOVERNANCE 3, hard rule 8). The boxes are
    asserted to differ for exactly that reason.

    **What `comparable` costs, said here because the floor now depends on it.**
    `comparable` needs `attached` and `alignment.status == "aligned"`, and both
    page witnesses' alignment is computed against the anchor derived from
    Chandra's response. So the third witness counts toward the floor only
    because the first located its text. The geometry is Churro's own and the
    anchor is a text-locating instrument, not a preference — but "three
    independent witnesses" now means two independent readings and one dependent
    comparability, and any later claim about witness independence has to say so.
    """
    tree = RunTree(live_seam.run_root, RUN_ID)
    records = act_records(tree)
    for (act_id, chair), record in records.items():
        assert record["outcome"] == "read", (act_id, chair)
    page_text = "SYNTHETIC ACT ONE alpha beta gamma\nSYNTHETIC ACT TWO delta epsilon zeta eta"
    # `act_records` keys by the sealed act identity, not the fixture's key; both
    # acts are primary on page 1, so both act views carry that page's reading.
    # Both page witnesses join their blocks to the same page text and report two
    # `native` boxes; neither restates the other's rectangles.
    views_by_chair = {}
    for chair in ("attestator_1", "attestator_3"):
        views = [record["payload"] for (_act, seat), record in records.items() if seat == chair]
        assert len(views) == 2, chair
        for view in views:
            assert view["payload"] == page_text, chair
            assert [box["bounds_source"] for box in view["observed"]] == ["native", "native"], chair
        views_by_chair[chair] = views[0]
    assert [box["bounds"] for box in views_by_chair["attestator_1"]["observed"]] != [
        box["bounds"] for box in views_by_chair["attestator_3"]["observed"]
    ]

    attachments = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] == "act-attachment":
            record = tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
            attachments[record["subject_id"]] = {
                item["chair"]: item
                for item in record["payload"]["attachments"]
                # A page witness has one entry per contributing page; the
                # primary page's entry is the one that holds the comparison view.
                if item["page_ordinal"] in (None, 1)
            }
            for item in record["payload"]["attachments"]:
                if item["page_ordinal"] == 2:
                    # a2's continuation page. Chandra answers it in the
                    # page-text form and Churro in its trained `<output>`
                    # envelope, so neither reports geometry there and neither
                    # attaches; a continuation page carries no act anchor
                    # either way.
                    assert item["attached"] is False
                    assert item["alignment"] == {
                        "status": "unaligned",
                        "reason": "continuation-page-no-act-anchor",
                    }
    assert attachments
    for act_id, by_chair in attachments.items():
        chandra, dai, churro = (by_chair[chair] for chair in WITNESS_CHAIRS)
        assert chandra["attached"] and chandra["comparable"], act_id
        assert chandra["alignment"]["status"] == "aligned"
        assert chandra["alignment"]["anchor_basis"] == "act-anchor"
        assert chandra["alignment"]["anchor_chair"] == "attestator_1"
        assert dai["attached"] and dai["comparable"], act_id
        assert dai["attachment_basis"] == "presented-region", act_id
        # Attached on its OWN geometry, aligned against Chandra's anchor, and
        # the record says which is which.
        assert churro["attached"] and churro["comparable"], act_id
        assert churro["attachment_basis"] == "geometric-overlap", act_id
        assert churro["alignment"]["status"] == "aligned", act_id
        assert churro["alignment"]["anchor_chair"] == "attestator_1", act_id

    reviews = _reviews(live_seam)
    assert reviews
    for review in reviews:
        coverage = review["payload"]["coverage"]
        assert review["outcome"] == "accepted"
        assert coverage["configured"] == 3
        assert coverage["floor"] == 3
        assert coverage["by_outcome"] == {"read": 3}
        assert coverage["shortfalls"] == {"failed": 0, "truncated": 0, "unaligned": 0}


def test_an_engine_that_reported_no_stop_word_is_recorded_as_unreported_and_held(
    designated, tmp_path
):
    """The absence is measured, never filled in (GOVERNANCE 10).

    The witness publishes an unknown truncation over a boundary nobody
    observed, and the Perlector holds the reading as `unknown` rather than
    calling it complete. `unreported` is this system's own word for the silence
    and never a stop word the engine did not say — the call record on both
    sides carries `null`.
    """
    run_root = fresh_tree(designated, tmp_path)
    silent = {
        chair: [
            ScriptedAnswer(content=answer.content, finish_reason=ABSENT)
            for answer in witness_scripts()[chair]
        ]
        for chair in WITNESS_CHAIRS
    }
    read_by_live_witnesses(designated, run_root, tmp_path / "witness-world", silent)

    tree = RunTree(run_root, RUN_ID)
    for (act_id, chair), record in act_records(tree).items():
        payload = record["payload"]
        call = json.loads(tree.read_bytes(payload["serving_call_ref"]["relative_path"]))
        assert call["finish_reason"] is None, (act_id, chair)
        assert payload["native_capture"]["transport_stop_reason"] == STOP_REASON_UNREPORTED
        assert payload["content_health"]["truncated"] is None

    reader = ReaderWorld(designated.catalogue, tmp_path / "reader", finish_reason=ABSENT)
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
    readings = published_readings(run_root)
    assert readings
    for record in readings:
        assert record["outcome"] == "truncated"
        assert record["payload"]["truncation"]["classification"] == "unknown"
        assert record["payload"]["truncation"]["signals"]["stop_reason_declared"] is None
        assert record["payload"]["engine_call"]["finish_reason"] is None


def test_an_engine_word_this_pipeline_never_measured_stops_the_pass_publishing_nothing(
    designated, witnessed, tmp_path
):
    """`"abort"` is neither a completion nor a cut-off, so it is refused by name.

    Nothing is lost by stopping: the client retained the response before it
    parsed it, so the bytes that stopped the pass are on disk under their own
    digest. Nothing is published for the act, because a Perlectio has no
    `failed` shape and minting one here would invent a record kind this seam
    does not own.
    """
    run_root = tmp_path / "runs"
    shutil.copytree(witnessed.run_root, run_root)
    reader = ReaderWorld(designated.catalogue, tmp_path / "reader", finish_reason="abort")
    with pytest.raises(EngineSignalRefusal, match="abort"):
        run_in_process(
            perlector,
            run_root,
            designated.catalogue,
            placement_tier=TIER,
            serving_factory=reader.factory,
        )
    assert published_readings(run_root) == []
    blobs = run_root / RUN_ID / "4_perlector" / "blobs" / "sha256"
    retained = [path.read_bytes() for path in blobs.glob("*")] if blobs.exists() else []
    assert any(b'"abort"' in body for body in retained), "the refusing response was not retained"
    # A blob merely containing the word is satisfied by the chair-call-record
    # too, which also carries `"finish_reason":"abort"`. Pin the exact raw
    # response bytes the endpoint served, by their own digest, so this proves
    # the response itself was retained before it was parsed -- not only that
    # a record describing it was.
    served = reader.endpoint.served[-1]
    assert (blobs / hashlib.sha256(served).hexdigest()).read_bytes() == served


# ============================ the fixture path, unmoved =======================


def test_the_identical_driver_in_fixture_mode_reproduces_the_orchestrated_tree(tmp_path):
    """Nothing this section added moves a fixture byte.

    The same driver as above — the stage programs as subprocesses either side,
    both stage `main`s called in this process, `--placement-tier` supplied —
    but pointed at the committed catalogue, whose rows say `fixture` for every
    chair. The comparison is against the tree `pipeline/orchestrator/run.py`
    builds for itself in this same test, so the claim is checked against a tree
    measured now rather than against a digest constant that could go stale
    unnoticed; the acceptance suite owns that constant and re-measures it
    against this same fixture.
    """
    committed = ROOT / "config" / "serving_recipes.toml"
    orchestrated = tmp_path / "orchestrated"
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE_ID,
            "--scenario",
            "happy",
            "--run-id",
            RUN_ID,
            "--run-root",
            str(orchestrated),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == EXIT_COMPLETE, result.stderr

    driven = tmp_path / "driven"
    for program in CHAIN_TO_DESIGNATOR:
        assert invoke_stage(program, driven, committed, placement_tier=TIER) == EXIT_COMPLETE
    for module in (attestatores, perlector):
        assert (
            run_in_process(module, driven, committed, placement_tier=TIER, serving_factory=None)
            == EXIT_COMPLETE
        )
    for program in TAIL_FROM_RECENSOR:
        assert invoke_stage(program, driven, committed, placement_tier=TIER) == EXIT_COMPLETE

    assert snapshot(driven) == snapshot(orchestrated)
