"""The structure chair marking out a run, and the whole roster reading what it marked.

Section D's other suites each prove one stage against the chair in front of it.
This one closes the circle: the acts a run reads are the ones a *model* drew,
not the ones a fixture declared. A tmp catalogue marks `designator_structure`
live beside the three witness chairs and the Perlector, the Door, Exemplar and
Ink Map build the tree as real programs, the Designator's live pass asks the
structure chair for each sealed page, and the acts it mints from the answer's
own rectangles are then witnessed, read, reviewed and exported by the stages
after it.

Nothing here starts a pod, opens a socket, loads a model or reaches a network.
Every chair answers through `operations/serving/fakes.py`, and the structure
chair's answers are built by that module's own scripted-answer builders, which
invert `common.structure_answer.to_page_bounds` and then parse what they built
through the contract that will parse it live — so a rectangle this file names
in page pixels is the rectangle the run will mint, or the builder refuses
before the run starts.

**What makes it live is the sealed row kind, on every chair at once.** No flag
selects the structure pass; the run binds a catalogue whose
`designator_structure` rows say `kind = "vllm"`, exactly as a card would. That
is also what makes this suite different from
`pipeline/test_live_reading_seam_e2e.py`, which keeps the Designator on its
fixture rows: there the live seam is read against declared acts, here against
proposed ones, and the fixture path's declared-act machinery is not consulted
anywhere in between.

**The export is held for review, and the reason is not the structure chair.**
Every act is minted, witnessed and read; Churro publishes no native layout, so
it never attaches to an act by geometry on a live path, and two witnesses of a
floor of three count. That limit belongs to `pipeline/3_attestatores/HANDOFF.md`
and is measured identically by the live-seam suite over declared acts. The
structure chair changes which acts exist, not how many witnesses reach them.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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
    _load_stage,
    _toml_profile,
    _vllm_row,
    act_records,
    attestatores,
    invoke_stage,
    perlector,
    published_readings,
    run_in_process,
)

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.identities import act_bindings  # noqa: E402
from common.contracts.identities import verify as verify_identity  # noqa: E402
from common.contracts.outcomes import ArmariumCategory  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR  # noqa: E402
from common.decoding import load_decoding_policy  # noqa: E402
from common.runtree.store import RECEIPTS_DIR, RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    STRUCTURE_ANSWER_KIND,
    expected_acts,
    open_stage_context,
    stage_parser,
    verify_final_seal,
)
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
    ScriptedAnswer,
    scripted_structure_answer,
    scripted_structure_cut_off,
    scripted_structure_refusal,
    structure_box_1000,
)
from operations.serving.manager import ServingManager, StageContextReceiptPublisher  # noqa: E402
from operations.serving.residency import FileResidencyLease  # noqa: E402

designator = _load_stage(DESIGNATOR_DIR, "structure_chair_e2e_designator")
structure_pass = designator.structure_pass

CHAIN_TO_INK_MAP = (
    "pipeline/1_exemplar/door.py",
    "pipeline/1_exemplar/run.py",
    "pipeline/1_ink_map/run.py",
)
TAIL_FROM_RECENSOR = (
    "pipeline/5_recensor/run.py",
    "pipeline/6_archetypus/run.py",
    "pipeline/7_armarium/run.py",
)
LIVE_CHAIRS = ("designator_structure", *WITNESS_CHAIRS, "perlector")
PAGE_WIDTH, PAGE_HEIGHT = 200, 260

# The rectangles the structure chair draws, in the sealed pages' own pixels.
# They lie over this fixture's own ink (`proof/synthetic_pages.py`) so the ink
# scan corroborates every one of them and the coordinate tripwire stays silent
# — the tripwire has its own suite in `pipeline/2_designator/test_structure_pass.py`.
# Page 2's act is a whole act of its own: a per-page call has no cross-page
# knowledge and D proposes no continuations, so unlike the fixture's declared
# a2 nothing here runs across the page break.
PAGE_ONE_ACTS = (
    ({"x": 20, "y": 20, "w": 160, "h": 80}, "SYNTHETIC ACT ONE alpha beta gamma"),
    ({"x": 20, "y": 120, "w": 160, "h": 100}, "SYNTHETIC ACT TWO delta epsilon zeta eta"),
)
PAGE_TWO_ACTS = (({"x": 20, "y": 20, "w": 160, "h": 60}, "SYNTHETIC ACT THREE theta iota"),)
ACTS_BY_PAGE = {1: PAGE_ONE_ACTS, 2: PAGE_TWO_ACTS}
ACT_KEYS = ("proposal:1:0", "proposal:1:1", "proposal:2:0")
SCRIPTED_TEXTS = tuple(text for page in ACTS_BY_PAGE.values() for _bounds, text in page)
READING = "SYNTHETIC LIVE READING alpha beta gamma delta epsilon zeta eta theta iota kappa"


# ------------------------------ the tmp catalogue -----------------------------


def write_catalogue(path: Path, registry) -> Path:
    """Every configured chair live at every tier, the structure chair included.

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
            row["preflight_identity_digest"] = identity_digest
            row["preflight_digest"] = profile_preflight_digest(row)
            rows.append(row)
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

    Deliberately close to `structure_pass.default_serving_factory`: the same
    manager, the same real `StageContextReceiptPublisher`, the same
    `retain_chair_bytes` into the Designator's own blob area, the same receipt
    re-read through the tree. The launcher, the transport and the package
    inspector are the fakes; nothing else is.
    """

    def __init__(self, catalogue: Path, work: Path, answers: list[ScriptedAnswer]) -> None:
        self.catalogue = catalogue
        self.work = work
        self.work.mkdir(parents=True, exist_ok=True)
        self.answers = answers
        self.endpoint: RecordingEndpoint | None = None

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
            retain=lambda data: structure_pass.retain_chair_bytes(context, data),
            decoding_config_sha256=decoding_sha256,
            record_temperature=structure_pass.executable_temperature(policy),
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


def happy_structure_answers() -> list[ScriptedAnswer]:
    return [
        scripted_structure_answer(PAGE_ONE_ACTS, PAGE_WIDTH, PAGE_HEIGHT),
        scripted_structure_answer(PAGE_TWO_ACTS, PAGE_WIDTH, PAGE_HEIGHT),
    ]


def mark_out(designated: SimpleNamespace, run_root: Path, work: Path, answers=None):
    """Run the Designator's live pass over one tree, and return its world."""
    world = StructureWorld(
        designated.catalogue, work, happy_structure_answers() if answers is None else answers
    )
    exit_code = run_in_process(
        designator,
        run_root,
        designated.catalogue,
        placement_tier=TIER,
        serving_factory=world.factory,
    )
    return world, exit_code


# ------------------------------ the witness roster ------------------------------


def chandra_page(blocks) -> str:
    """One page's Chandra answer in the shape its own prompt asks for.

    The block geometry is built from the same page-pixel rectangles the
    structure chair drew, through the same normalized conversion both readings
    of a page share (`common.structure_answer`), so a block lands exactly on
    the act it reports rather than approximately near it.
    """
    return json.dumps(
        {
            "schema": "verbatus-chandra-page-response.v1",
            "blocks": [
                {"box_1000": structure_box_1000(bounds, PAGE_WIDTH, PAGE_HEIGHT), "text": text}
                for bounds, text in blocks
            ],
        }
    )


def witness_scripts() -> dict[str, list[ScriptedAnswer]]:
    """One answer per unit of each chair's own sealed scope, over three acts.

    Two pages for the page-scoped chairs, three acts for the act-scoped one:
    the same corpus read through two scopes, and a script whose length
    disagreed with that is the first thing that would notice a scope
    regression. DAI's answers are in seal order, which the act-by-act
    assertion below then proves rather than assumes.
    """
    return {
        "attestator_1": [
            ScriptedAnswer(content=chandra_page(PAGE_ONE_ACTS), finish_reason="stop"),
            ScriptedAnswer(content=chandra_page(PAGE_TWO_ACTS), finish_reason="stop"),
        ],
        "attestator_2": [
            ScriptedAnswer(content=text, finish_reason="stop") for text in SCRIPTED_TEXTS
        ],
        "attestator_3": [
            ScriptedAnswer(
                content="<output>" + "\n".join(text for _b, text in page) + "</output>",
                finish_reason="stop",
            )
            for page in (PAGE_ONE_ACTS, PAGE_TWO_ACTS)
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


def by_page_ordinal(records: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {record["payload"]["page_ordinal"]: record for record in records}


def seal_rows(root: Path) -> dict[str, dict[str, Any]]:
    (seal,) = artifacts(root, DESIGNATOR, "proposal-seal")
    return {row["act_key"]: row for row in seal["payload"]["expected_acts"]}


def open_at(root: Path, catalogue: Path, stage: str):
    args = stage_parser("structure chair e2e").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            RUN_ID,
            "--scenario",
            "happy",
            "--serving-recipes-config",
            str(catalogue),
            "--placement-tier",
            TIER,
        ]
    )
    return open_stage_context(args, stage)


def designator_artifact_text(root: Path) -> str:
    """Every byte of every Designator artifact, for the no-text sweep."""
    directory = root / RUN_ID / "2_designator" / "artifacts"
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(directory.rglob("*.json")))


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


@pytest.fixture(scope="module")
def marked_out(designated) -> SimpleNamespace:
    """The Ink Map tree, marked out by the structure chair's own rectangles."""
    run_root = designated.work / "marked-out"
    shutil.copytree(designated.run_root, run_root)
    world, exit_code = mark_out(designated, run_root, designated.work / "structure-world")
    assert exit_code == EXIT_COMPLETE
    return SimpleNamespace(run_root=run_root, world=world, catalogue=designated.catalogue)


@pytest.fixture(scope="module")
def whole_run(designated, marked_out) -> SimpleNamespace:
    """The proposed acts, read by the whole live roster and carried to the export.

    Run once for the several claims below: each is about a different part of
    one run, and reproducing it per assertion would say nothing more.
    """
    run_root = designated.work / "whole-run"
    shutil.copytree(marked_out.run_root, run_root)
    _policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
    witnesses = WitnessWorld(
        designated.catalogue,
        decoding_sha256,
        designated.work / "witness-world",
        witness_scripts(),
    )
    assert (
        run_in_process(
            attestatores,
            run_root,
            designated.catalogue,
            placement_tier=TIER,
            serving_factory=witnesses.factory,
        )
        == EXIT_COMPLETE
    )
    reader = ReaderWorld(
        designated.catalogue,
        designated.work / "reader",
        ScriptedAnswer(content=READING, finish_reason="stop"),
    )
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
    tail = {
        program: invoke_stage(program, run_root, designated.catalogue, placement_tier=TIER)
        for program in TAIL_FROM_RECENSOR
    }
    return SimpleNamespace(
        run_root=run_root,
        witnesses=witnesses,
        reader=reader,
        tail=tail,
        catalogue=designated.catalogue,
    )


# ============================== the marked-out tree ==============================


def test_every_act_is_the_rectangle_the_chair_drew(marked_out):
    """The denominator of the whole run comes back from a model, byte for byte.

    Three claims in one, because they are one fact seen from three sides: the
    retained answer records the rectangles, the minted regions carry them as
    `raw_bounds`, and each act's identity recomputes from those same bounds. If
    any conversion in between rounded, clamped or reordered, one of the three
    would disagree with the other two.
    """
    root = marked_out.run_root
    answers = by_page_ordinal(artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    assert set(answers) == {1, 2}
    for ordinal, scripted in ACTS_BY_PAGE.items():
        payload = answers[ordinal]["payload"]
        assert payload["parse_state"] == "parsed"
        assert payload["parse_outcome"] is None
        assert payload["finish_reason"] == "stop"
        assert [act["raw_bounds"] for act in payload["acts"]] == [
            dict(bounds) for bounds, _text in scripted
        ]

    regions = {
        record["payload"]["act_key"]: record for record in artifacts(root, DESIGNATOR, "region")
    }
    assert set(regions) == set(ACT_KEYS)
    expected = {
        "proposal:1:0": PAGE_ONE_ACTS[0][0],
        "proposal:1:1": PAGE_ONE_ACTS[1][0],
        "proposal:2:0": PAGE_TWO_ACTS[0][0],
    }
    for key, record in regions.items():
        payload = record["payload"]
        assert payload["origin"] == "proposal"
        assert payload["raw_bounds"] == dict(expected[key])
        verify_identity(
            record["subject_id"],
            "act",
            act_bindings(payload["transform"]["source_page_id"], "proposal", payload["raw_bounds"]),
        )
    rows = seal_rows(root)
    assert set(rows) == set(ACT_KEYS)
    assert {row["outcome"] for row in rows.values()} == {"proposed"}


def test_the_retained_answer_is_what_the_downstream_verifier_reads(marked_out):
    """D3's `expected_acts` follows the seal back to the answer, on the real tree.

    Not a re-implementation of the verifier: the stage context is opened at the
    exact boundary the Attestatores open the run under, and the acts it returns
    are the acts the seal named. A row whose premise (`structure-status` →
    `structure_answer_ref` → the answer's rectangle at some ordinal) had drifted
    would refuse here rather than pass with a rebuilt expectation.
    """
    context = open_at(marked_out.run_root, marked_out.catalogue, ATTESTATORES)
    acts = expected_acts(context)
    assert sorted(row["act_key"] for row in acts) == sorted(ACT_KEYS)


def test_the_sealed_structure_temperature_is_recorded_on_every_call(marked_out):
    """The executed decoding posture, per call, from the sealed `[structure]` table.

    A number reported on a record and a number put on the wire are two
    different claims (GOVERNANCE 10), so both are read here: the request the
    endpoint actually received, and the call record the client retained beside
    the answer.
    """
    policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
    sealed = policy["structure"]["temperature"]
    endpoint = marked_out.world.endpoint
    assert endpoint is not None
    assert len(endpoint.requests) == 2
    for request in endpoint.requests:
        assert request["temperature"] == sealed
        assert "max_tokens" not in request

    tree = RunTree(marked_out.run_root, RUN_ID)
    for record in artifacts(marked_out.run_root, DESIGNATOR, STRUCTURE_ANSWER_KIND):
        decoding = record["payload"]["decoding"]
        assert decoding["policy"] == "structure"
        assert decoding["temperature"] == sealed
        assert decoding["decoding_config_sha256"] == decoding_sha256
        call = json.loads(tree.read_bytes(record["payload"]["call_record_ref"]["relative_path"]))
        assert call["chair"] == "designator_structure"
        assert call["decoding_config_sha256"] == decoding_sha256
        # The bytes the record names are the bytes the endpoint served.
        assert tree.read_bytes(record["payload"]["raw_response_ref"]["relative_path"]) in (
            endpoint.served
        )


def test_no_designator_artifact_carries_the_chair_s_transcription(marked_out):
    """SPEC_D §4: the custody blob is the one permitted home for the text.

    The structure chair returns a whole page's transcription, and the Designator
    is not a witness. If any of that text reached an artifact, the Perlector's
    no-picker fences would be guarding a door the text had already walked
    through.
    """
    root = marked_out.run_root
    body = designator_artifact_text(root)
    for text in SCRIPTED_TEXTS:
        assert text not in body
    tree = RunTree(root, RUN_ID)
    retained = tree.read_bytes(
        by_page_ordinal(artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))[1]["payload"][
            "raw_response_ref"
        ]["relative_path"]
    ).decode()
    assert all(text in retained for text in SCRIPTED_TEXTS[:2])


def test_no_fixture_receipt_is_written_on_the_live_path(marked_out):
    """GOVERNANCE 6: the one receipt is the moment the chair really served."""
    directory = marked_out.run_root / RUN_ID / RECEIPTS_DIR
    receipts = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.rglob("*.json"))
    ]
    assert [receipt["chair"] for receipt in receipts] == ["designator_structure"]
    assert not receipts[0]["endpoint"].startswith("fixture://")


# ============================ the roster over those acts =========================


def test_the_whole_roster_witnessed_the_acts_the_chair_proposed(whole_run):
    """Three chairs, three scopes, three proposed acts — and no declared one.

    The witnesses have never before been asked about an act a model drew. What
    proves they were is that every act record keys on a `proposal`-class
    identity and names the serving call that produced it.
    """
    tree = RunTree(whole_run.run_root, RUN_ID)
    records = act_records(tree)
    keys = {
        record["payload"]["act_key"]
        for record in artifacts(whole_run.run_root, DESIGNATOR, "region")
    }
    assert keys == set(ACT_KEYS)
    assert {chair for _act, chair in records} == set(WITNESS_CHAIRS)
    assert len(records) == len(ACT_KEYS) * len(WITNESS_CHAIRS)
    for (act_id, chair), record in records.items():
        assert record["outcome"] == "read", (act_id, chair)
        payload = record["payload"]
        assert payload["act_key"] in ACT_KEYS
        call = json.loads(tree.read_bytes(payload["serving_call_ref"]["relative_path"]))
        assert call["chair"] == chair
        assert call["finish_reason"] == "stop"

    # The act-scoped chair read each act's own crop, and the text it returned
    # is the text scripted for that act — which is also what says the schedule
    # asked about the acts in seal order.
    expected_text = dict(zip(ACT_KEYS, SCRIPTED_TEXTS, strict=True))
    for (_act_id, chair), record in records.items():
        if chair == "attestator_2":
            assert record["payload"]["payload"] == expected_text[record["payload"]["act_key"]]


def test_the_page_witness_transcription_is_retained_and_anchors_the_alignment(whole_run):
    """A transcription reaches the run as testimony, from the chair that served it.

    Chandra answers per page under its own contract; the page Testimonium
    retains those bytes, its block geometry attaches it to each proposed act,
    and the live alignment anchor is derived from that same response. The
    Designator's own custody blob is evidence of what the structure chair said,
    never testimony — a witness that had been handed the proposer's answer
    would be corroborating itself.
    """
    tree = RunTree(whole_run.run_root, RUN_ID)
    pages = [
        tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
    ]
    chandra_pages = {
        record["payload"]["page_ordinal"]: record
        for record in pages
        if record["payload"]["chair"] == "attestator_1"
    }
    assert set(chandra_pages) == {1, 2}
    served_contents = {
        json.loads(body)["choices"][0]["message"]["content"]
        for body in whole_run.witnesses.served("attestator_1")
    }
    for ordinal, record in chandra_pages.items():
        payload = record["payload"]
        assert record["outcome"] == "read"
        expected = "\n".join(text for _bounds, text in ACTS_BY_PAGE[ordinal])
        assert payload["payload"] == expected
        capture = payload["native_capture"]
        assert capture["adapter"] == "chandra.v1"
        retained = tree.read_bytes(capture["raw_response_ref"]["relative_path"])
        assert digest_bytes(retained) == capture["raw_response_ref"]["sha256"]
        # The retained capture is the chair's own answer, not a record built
        # around it: the bytes come back out of one of the wire bodies the
        # endpoint actually served.
        assert retained.decode() in served_contents
        assert [box["bounds_source"] for box in payload["observed"]] == [
            "native" for _ in ACTS_BY_PAGE[ordinal]
        ]

    attachments = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "act-attachment":
            continue
        record = tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        attachments[record["payload"]["act_key"]] = {
            item["chair"]: item for item in record["payload"]["attachments"]
        }
    assert set(attachments) == set(ACT_KEYS)
    for key, by_chair in attachments.items():
        chandra = by_chair["attestator_1"]
        assert chandra["attached"] and chandra["comparable"], key
        assert chandra["alignment"]["anchor_basis"] == "act-anchor"
        assert chandra["alignment"]["anchor_chair"] == "attestator_1"
        # No act runs across the page break, so every page witness contributes
        # exactly one entry per act: D proposes no continuations.
        assert chandra["page_ordinal"] == int(key.split(":")[1])
        assert by_chair["attestator_2"]["attached"]


def test_the_run_reaches_a_sealed_terminal_export_over_proposed_acts(whole_run):
    """Every stage after the Designator reads a tree whose acts a model drew.

    The export is **held for review, not delivered**, and the shortfall is the
    one the live-seam suite measures over declared acts: Churro publishes no
    native layout, so it never attaches by geometry live, and two witnesses of
    a floor of three count. Nothing about that is a fact about the structure
    chair — every act it proposed was read — which is the point of asserting it
    here as well: replacing declared acts with proposed ones moved the
    denominator, not the coverage.
    """
    assert whole_run.tail == {
        "pipeline/5_recensor/run.py": EXIT_HELD,
        "pipeline/6_archetypus/run.py": EXIT_COMPLETE,
        "pipeline/7_armarium/run.py": EXIT_HELD,
    }
    export = verify_final_seal(RunTree(whole_run.run_root, RUN_ID))
    assert export["outcome"] == ArmariumCategory.HELD_FOR_REVIEW.value
    aggregate = export["payload"]["aggregate"]
    assert aggregate["status"] == "partial"
    assert sorted(aggregate["reasons"]) == sorted(
        [f"act {key} is held-for-review" for key in ACT_KEYS]
        + [f"act {key} is under-witnessed (2 of a floor of 3)" for key in ACT_KEYS]
    )
    readings = published_readings(whole_run.run_root)
    assert len(readings) == len(ACT_KEYS)
    assert {record["outcome"] for record in readings} == {"read"}


# =============================== the other answers ===============================


def test_a_zero_act_answer_falls_back_to_the_predetermined_tiles(designated, tmp_path):
    """The chair saw no text on a page, so the page is cut on the sealed grid.

    Tyrel, 2026-08-11: a page the Designator sees nothing on is still sent
    downstream as predetermined crops. The page is `scanned`, not held: an
    answer that says "no acts" is an answer.
    """
    run_root = fresh_tree(designated, tmp_path)
    _world, exit_code = mark_out(
        designated,
        run_root,
        tmp_path / "world",
        [
            scripted_structure_answer(PAGE_ONE_ACTS, PAGE_WIDTH, PAGE_HEIGHT),
            scripted_structure_answer((), PAGE_WIDTH, PAGE_HEIGHT),
        ],
    )
    assert exit_code == EXIT_COMPLETE
    statuses = by_page_ordinal(artifacts(run_root, DESIGNATOR, "structure-status"))
    assert statuses[2]["payload"]["state"] == "scanned"
    assert statuses[2]["payload"]["structure_evidence"] == "fallback-tiles"
    (fallback,) = artifacts(run_root, DESIGNATOR, "page-fallback")
    assert fallback["payload"]["page_ordinal"] == 2
    rows = seal_rows(run_root)
    assert set(rows) == {"proposal:1:0", "proposal:1:1", "page-fallback:2"}
    assert rows["page-fallback:2"]["outcome"] == "proposed"
    # The tiles are a real denominator, not a placeholder: the verifier the
    # Attestatores open under accepts them beside the proposed rectangles.
    acts = expected_acts(open_at(run_root, designated.catalogue, ATTESTATORES))
    assert sorted(row["act_key"] for row in acts) == sorted(rows)


def test_a_cut_off_answer_holds_the_page_as_cut_off(designated, tmp_path):
    """The engine ran out of room, and the record says so rather than guessing.

    SPEC_D §7 names this as the likeliest first real failure: a page's whole
    transcription overruns `max_model_len`. The body is truncated mid-object
    and the stop word is `length`, and the hold must name the context window
    rather than blaming the chair's JSON — otherwise the one measurement this
    design exists to obtain reads as a model that cannot write JSON.
    """
    run_root = fresh_tree(designated, tmp_path)
    _world, exit_code = mark_out(
        designated,
        run_root,
        tmp_path / "world",
        [
            scripted_structure_answer(PAGE_ONE_ACTS, PAGE_WIDTH, PAGE_HEIGHT),
            scripted_structure_cut_off(PAGE_TWO_ACTS, PAGE_WIDTH, PAGE_HEIGHT),
        ],
    )
    assert exit_code == EXIT_HELD
    statuses = by_page_ordinal(artifacts(run_root, DESIGNATOR, "structure-status"))
    assert statuses[2]["payload"]["state"] == "held"
    assert statuses[2]["payload"]["reason_code"] == "structure-answer-cut-off"
    answers = by_page_ordinal(artifacts(run_root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    payload = answers[2]["payload"]
    assert payload["parse_state"] == "refused"
    assert payload["finish_reason"] == "length"
    # The bytes are retained whatever the disposition: they are the evidence.
    tree = RunTree(run_root, RUN_ID)
    retained = tree.read_bytes(payload["raw_response_ref"]["relative_path"])
    assert digest_bytes(retained) == payload["raw_response_ref"]["sha256"]
    rows = seal_rows(run_root)
    assert any(key.startswith("residual:2:") for key in rows)
    assert not any(key.startswith("proposal:2:") for key in rows)


@pytest.mark.parametrize(
    "outcome",
    (
        "invalid-json",
        "top-level-not-object",
        "unverified-response-schema",
        "missing-act-list",
        "malformed-act",
        "malformed-act-geometry",
        "malformed-act-text",
    ),
)
def test_an_answer_the_contract_refuses_holds_the_page_by_that_name(designated, tmp_path, outcome):
    """The refusal codes `_STRUCTURE_REFUSALS` can script, held under their own code.

    A page whose answer this system cannot read is held with the outcome that
    says why, and its ink reconciles as conservation residual — never repaired,
    never re-asked, and never quietly tiled as though the chair had answered
    (GOVERNANCE 7).

    This covers 7 of `structure_answer.PARSE_OUTCOMES`' 11 codes — every one
    `operations/serving/fakes.py::_STRUCTURE_REFUSALS` builds a scripted body
    for. `raw-response-not-bytes` and `response-too-large` describe the wire
    itself, not a body the fake endpoint hands back, so no scripted answer can
    reach them here. `excessive-json-nesting` and `too-many-acts` are
    constructible over this real chain but have no scripted body yet; both,
    and the full 11, are exercised at the parser level in
    `common/test_structure_answer.py`, guarded against silent drift by its own
    `test_every_declared_outcome_is_exercised_above`.
    """
    run_root = fresh_tree(designated, tmp_path)
    _world, exit_code = mark_out(
        designated,
        run_root,
        tmp_path / "world",
        [
            scripted_structure_answer(PAGE_ONE_ACTS, PAGE_WIDTH, PAGE_HEIGHT),
            scripted_structure_refusal(outcome),
        ],
    )
    assert exit_code == EXIT_HELD
    statuses = by_page_ordinal(artifacts(run_root, DESIGNATOR, "structure-status"))
    assert statuses[2]["payload"]["state"] == "held"
    assert statuses[2]["payload"]["reason_code"] == f"structure-answer-{outcome}"
    answers = by_page_ordinal(artifacts(run_root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    assert answers[2]["payload"]["parse_outcome"] == outcome
    assert answers[2]["payload"]["acts"] == []
    assert not artifacts(run_root, DESIGNATOR, "page-fallback")
    rows = seal_rows(run_root)
    assert rows["residual:2:0"]["outcome"] == "held"


# The same two pages, read a second time by a chair that drew them slightly
# differently — a re-run's ordinary variance, not a corrupted answer. Page 1's
# rectangles move by two pixels on every edge; page 2's do not move at all,
# which is what lets the two halves of the claim below be told apart.
PAGE_ONE_REDRAWN = (
    ({"x": 18, "y": 18, "w": 164, "h": 84}, PAGE_ONE_ACTS[0][1]),
    ({"x": 18, "y": 118, "w": 164, "h": 104}, PAGE_ONE_ACTS[1][1]),
)


def test_a_second_attempt_at_the_same_pages_may_answer_differently_and_seals_what_it_got(
    designated, tmp_path
):
    """Runs are attempts, never reproductions (Tyrel, 2026-09-02).

    The Attestatores read at the fixed reading-of-record posture; the
    Designator's `[structure]` pass may vary, sealed and recorded per run, so
    its re-run variance is a clue beside the witnesses. That ruling only means
    anything if a second attempt whose rectangles moved is an ordinary run
    rather than a refusal, so both attempts are made here over copies of one
    Ink Map tree and each is asked to stand on its own.

    What varies is *visible as acts*, because act identity is derived from the
    page and the rectangle rather than from the order a run happened to mint
    in: page 1's redrawn rectangles are two different acts, and page 2 — whose
    answer did not move — is the **same** act in both trees, which is content
    addressing doing the work rather than two runs agreeing by luck. Neither
    tree is consulted while the other is built; the store's refusal of
    differing bytes under one identity is untouched and is the safety net for
    a genuine re-publication, which this is not.
    """
    first = fresh_tree(designated, tmp_path, "first")
    second = fresh_tree(designated, tmp_path, "second")
    _world, first_exit = mark_out(designated, first, tmp_path / "world-first")
    _world, second_exit = mark_out(
        designated,
        second,
        tmp_path / "world-second",
        [
            scripted_structure_answer(PAGE_ONE_REDRAWN, PAGE_WIDTH, PAGE_HEIGHT),
            scripted_structure_answer(PAGE_TWO_ACTS, PAGE_WIDTH, PAGE_HEIGHT),
        ],
    )
    assert (first_exit, second_exit) == (EXIT_COMPLETE, EXIT_COMPLETE)

    def minted(root: Path) -> dict[str, dict[str, Any]]:
        return {
            record["payload"]["act_key"]: record for record in artifacts(root, DESIGNATOR, "region")
        }

    before, after = minted(first), minted(second)
    # The same positional keys either way: what moved is the ink each key
    # bounds, and a key is not an identity.
    assert set(before) == set(after) == set(ACT_KEYS)
    assert [after[key]["payload"]["raw_bounds"] for key in ("proposal:1:0", "proposal:1:1")] == [
        dict(bounds) for bounds, _text in PAGE_ONE_REDRAWN
    ]
    for key in ("proposal:1:0", "proposal:1:1"):
        assert before[key]["subject_id"] != after[key]["subject_id"], key
        verify_identity(
            after[key]["subject_id"],
            "act",
            act_bindings(
                after[key]["payload"]["transform"]["source_page_id"],
                "proposal",
                after[key]["payload"]["raw_bounds"],
            ),
        )
    # Page 2's answer did not move, so its act is the same act — the identity
    # is a function of the rectangle, not of the attempt that drew it.
    assert before["proposal:2:0"]["subject_id"] == after["proposal:2:0"]["subject_id"]

    # Each attempt sealed its own answer, under its own decoding seal, and each
    # seal is one the next stage accepts: variance is a second reading, not a
    # damaged run.
    _policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
    digests = []
    for root in (first, second):
        answers = by_page_ordinal(artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
        assert answers[1]["payload"]["parse_state"] == "parsed"
        assert answers[1]["payload"]["decoding"]["decoding_config_sha256"] == decoding_sha256
        digests.append(answers[1]["payload"]["raw_response_ref"]["sha256"])
        acts = expected_acts(open_at(root, designated.catalogue, ATTESTATORES))
        assert sorted(row["act_key"] for row in acts) == sorted(ACT_KEYS)
    assert digests[0] != digests[1]


def test_a_structural_label_travels_verbatim_and_its_absence_is_null(designated, tmp_path):
    """The one field the contract retains and uses for nothing, end to end.

    `label` is the chair's own word for what it thinks a rectangle is. It is
    kept verbatim beside the rectangle and consulted by nothing — no branch, no
    ordering, no threshold reads it — which is what keeps it a record of what
    the chair said rather than an instruction to this pipeline. Asserted here
    because a field nothing reads is exactly the field that quietly stops being
    written; the absent case is asserted beside it so `null` stays the honest
    spelling of "the chair offered none" rather than an empty string invented
    for the shape.
    """
    run_root = fresh_tree(designated, tmp_path)
    labelled = (
        (PAGE_ONE_ACTS[0][0], PAGE_ONE_ACTS[0][1], "acte de baptême"),
        PAGE_ONE_ACTS[1],
    )
    _world, exit_code = mark_out(
        designated,
        run_root,
        tmp_path / "world",
        [
            scripted_structure_answer(labelled, PAGE_WIDTH, PAGE_HEIGHT),
            scripted_structure_answer(PAGE_TWO_ACTS, PAGE_WIDTH, PAGE_HEIGHT),
        ],
    )
    assert exit_code == EXIT_COMPLETE
    answers = by_page_ordinal(artifacts(run_root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    assert [act["label"] for act in answers[1]["payload"]["acts"]] == ["acte de baptême", None]
    assert [act["label"] for act in answers[2]["payload"]["acts"]] == [None]
    # A label is not text: it changes no rectangle, and the acts minted from
    # this answer are the same acts the unlabelled answer mints.
    regions = {
        record["payload"]["act_key"]: record["payload"]["raw_bounds"]
        for record in artifacts(run_root, DESIGNATOR, "region")
    }
    assert regions["proposal:1:0"] == dict(PAGE_ONE_ACTS[0][0])
