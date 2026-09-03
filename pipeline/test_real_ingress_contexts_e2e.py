"""One real submission, carried by the real stage programs as far as it goes.

Section B gave stages 3-7 a real-ingress context. Every other suite in the
section proves one stage's own wiring against a run that stops at the missing
Designator seal. This module is the seam between them: a genuine real
submission -- the synthetic fixture's own two pages copied into an approved
storage root and admitted through `operations/submit/submit.py` -- driven by
the real Door, Exemplar and Ink Map programs, read by a full served
Attestatores roster and a served Perlector, and then carried into the Recensor,
Archetypus and Armarium, which no real run had ever reached.

**The Designator's layer is hand-built, and that is the honest part of this
module.** `pipeline/2_designator/run.py` refuses on real ingress by design --
"real structural proposal/model work is outside System 03" -- and the first
test below drives its program to exactly that refusal rather than describing
it. A real structural pass does not exist, so the records this module puts in
its place are built by
`pipeline/3_attestatores/test_attestatores_real_ingress.py`'s own
`_RealDesignator`, reused rather than copied: crops really cut from the sealed
pages, `region_id` binding each act to its transform, and `raw_bounds` equal to
the rectangle the act identity was minted from -- which is the exact contract
`common.stage.expected_acts` recomputes a real denominator against, and which
`pipeline/2_designator/HANDOFF.md` now records as what the real pass must meet.

**Where a real run stops today, and why, is the measurement this module is
for.** The witness pass and the reading pass complete. The Recensor then asks
the Designator for the R2 conservation denominator -- per sealed page, the ink
there is, the ink the crops claim, and what the unclaimed remainder became --
and a hand-built layer has none, because those are measurements of the real
page and composing them here would be exactly the fabricated structural
accounting the Designator refuses to produce (GOVERNANCE 10). So the run stops
at the Recensor, by name, with nothing written, and the Archetypus and Armarium
refuse at their own predecessor boundaries. That is the boundary of real
ingress today; the real structural pass is what moves it, and these assertions
are what will say when it does.

Nothing here starts a pod, opens a socket, loads a model or reaches a network.
The served chairs answer through `operations/serving/fakes.py` behind a real
`ServingManager` and a real `ChairClient`, under a tmp catalogue whose rows say
`kind = "vllm"` -- the same fakes and the same catalogue writer
`pipeline/test_live_reading_seam_e2e.py` uses for the fixture route, reused here
so the two seams cannot drift in how a chair is stood up.

What this module proves that no other suite in the section can:

- every stage after the Designator opens its context through
  `open_stage_context` on real ingress and carries what stages 3-7 need: a
  registry, the sealed digest map (the three real-only names included), the
  format projection, the recovery policy, `REAL_SCENARIO`, and a `fixture` slot
  that refuses by name. The two stages past the stopping point reach their own
  predecessor-seal refusal, which fires after the whole binding recheck, so
  they too are stages whose real context was built;
- no fixture accessor is reached anywhere on the run: two whole stages
  completing is that proof, because one touch would have refused them;
- the real denominator is recomputed from the Designator's own regions rather
  than counted from a declaration, and it is the same three acts every later
  stage reads;
- a real run's export identity is the submission's filename-ledger self-hash,
  never a `fixture_id` -- checked at the Armarium's own `export_run_identity`,
  the function that decides it, since the export itself is past the stopping
  point;
- a moved configuration is refused at open, by the name that moved, before
  anything is written.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PIPELINE = Path(__file__).resolve().parent
ROOT = PIPELINE.parents[0]
ATTESTATORES_DIR = PIPELINE / "3_attestatores"
# The Attestatores' own real-ingress suite imports its stage siblings by bare
# name; its directory has to be importable before that module is loaded here.
for _directory in (PIPELINE, ATTESTATORES_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

# Two suites' helpers, reused rather than copied.
#
# `test_attestatores_real_ingress` owns the real-submission builder and the
# hand-built Designator layer; `RUN_ID` and `ACTS` come with them, and the
# witness scripts are sized by that same act table, so the two cannot drift
# apart. `test_live_reading_seam_e2e` owns the serving world: the catalogue
# writer that makes every chair live at every tier, the scripted witness and
# reader worlds, the tree snapshotter, and the two stage programs loaded for
# in-process invocation — so a change in how a fake chair is stood up is made
# once for both seams.
from test_attestatores_real_ingress import (  # noqa: E402
    ACTS,
    RUN_ID,
    _designate,
    _real_submission,
)
from test_attestatores_real_ingress import _scripts as witness_scripts  # noqa: E402
from test_live_reading_seam_e2e import (  # noqa: E402
    READING,
    TIER,
    WITNESS_CHAIRS,
    ReaderWorld,
    WitnessWorld,
    attestatores,
    perlector,
    snapshot,
    write_live_catalogue,
)

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.errors import (  # noqa: E402
    ContractError,
    IncompatibleReuse,
    SchemaRefusal,
)
from common.contracts.stages import (  # noqa: E402
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    PERLECTOR,
    RECENSOR,
    SEAL_PREDECESSORS,
)
from common.decoding import DEFAULT_DECODING_CONFIG_PATH, load_decoding_policy  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH,
    EXIT_COMPLETE,
    EXIT_FATAL,
    REAL_SCENARIO,
    StageContext,
    adapter_recipe_for,
    exemplar_page_ids,
    expected_acts,
    open_stage_context,
    stage_parser,
    submission_identity,
)

# The binding recheck's own suite owns how a sealed input is moved: an appended
# byte for a file, and for the roster a record that moves without its membership
# moving — the case `run.json`'s `witness_chairs` cannot see. Imported rather
# than rewritten, so the unit and the end-to-end move the same bytes.
from common.test_stage_real_ingress import _appended, _moved_models_config  # noqa: E402
from operations.serving.fakes import ScriptedAnswer  # noqa: E402

MODELS_CONFIG = ROOT / "config" / "models.toml"
DESIGNATOR_CLI = PIPELINE / "2_designator" / "run.py"
TAIL_FROM_RECENSOR = (
    PIPELINE / "5_recensor" / "run.py",
    PIPELINE / "6_archetypus" / "run.py",
    PIPELINE / "7_armarium" / "run.py",
)
# The stages whose predecessor really sealed on this run, so their real context
# is built all the way, and the two past the stopping point, whose context is
# built up to the seal check that then refuses by name.
OPENING_STAGES = (ATTESTATORES, PERLECTOR, RECENSOR)
SEAL_REFUSING_STAGES = (ARCHETYPUS, ARMARIUM)
ACT_KEYS = tuple(key for _ordinal, _bounds, key in ACTS)


def _load_program(program: Path, name: str):
    """Load one stage program as a module, the sanctioned cross-stage way.

    `pipeline/test_stage_import_boundaries.py` names `spec_from_file_location`
    under a synthetic module name as the deliberate, visible load a boundary
    test may make; the Armarium is loaded here for one function of its own,
    `export_run_identity`, which is where a real run's export identity is
    actually decided.
    """
    spec = importlib.util.spec_from_file_location(name, program)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    original_path = list(sys.path)
    try:
        sys.path.insert(0, str(program.parent))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    return module


armarium = _load_program(PIPELINE / "7_armarium" / "run.py", "e2e_armarium_under_test")


# --------------------------------- driving ----------------------------------


def stage_argv(run_root: Path, catalogue: Path, *extra: str) -> list[str]:
    """The flags every stage of this run is given, the Door included.

    Only the roster and the catalogue are named explicitly: every other sealed
    input is `config/`'s own file at `stage_parser`'s default, so the Door and
    each later open recompute the same digest from the same bytes. Naming them
    a second time here would only create a place for the two to disagree.
    """
    return [
        "--run-root",
        str(run_root),
        "--run-id",
        RUN_ID,
        "--models-config",
        str(MODELS_CONFIG),
        "--serving-recipes-config",
        str(catalogue),
        *extra,
    ]


def invoke_stage(program: Path, run_root: Path, catalogue: Path, *extra: str):
    """Run one stage program as a real subprocess, and return the whole result."""
    return subprocess.run(
        [sys.executable, str(program), *stage_argv(run_root, catalogue, *extra)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def run_in_process(module, run_root: Path, catalogue: Path, *, serving_factory):
    """Call one stage's own `main` here, with the serving seam injected.

    `main(serving_factory=…)` is the sanctioned in-process injection point and
    is not what makes a run live: the sealed row kind decides that.
    `sys.argv[0]` is the stage's own program path, as it would be under the
    orchestrator, so a refusal names a file that exists.
    """
    argv = [module.__file__, *stage_argv(run_root, catalogue, "--placement-tier", TIER)]
    original, sys.argv = sys.argv, argv
    try:
        return module.main(serving_factory=serving_factory)
    finally:
        sys.argv = original


def open_real_context(run_root: Path, catalogue: Path, stage: str):
    """Open one stage's context on this run, through the shared constructor."""
    parser = stage_parser("real-ingress stage context", accepts_chair=True)
    args = parser.parse_args(stage_argv(run_root, catalogue, "--placement-tier", TIER))
    return open_stage_context(args, stage)


# --------------------------------- the run ----------------------------------


@pytest.fixture(scope="module")
def real_run(tmp_path_factory) -> SimpleNamespace:
    """One real submission carried the whole way, once.

    Built once for the several claims below: each of them is about a different
    part of the same single run, and reproducing it per assertion would say
    nothing more and cost four more model-free passes.
    """
    work = tmp_path_factory.mktemp("real-ingress-contexts-e2e")
    registry = ChairRegistry.from_toml(str(MODELS_CONFIG))
    catalogue = write_live_catalogue(work / "serving_recipes_live.toml", registry)
    run_root = _real_submission(
        work, "--serving-recipes-config", str(catalogue), "--models-config", str(MODELS_CONFIG)
    )
    designator = _designate(run_root)

    _policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
    witnesses = WitnessWorld(catalogue, decoding_sha256, work / "witness-world", witness_scripts())
    witness_exit = run_in_process(
        attestatores, run_root, catalogue, serving_factory=witnesses.factory
    )
    reader = ReaderWorld(
        catalogue, work / "reader", ScriptedAnswer(content=READING, finish_reason="stop")
    )
    reader_exit = run_in_process(perlector, run_root, catalogue, serving_factory=reader.factory)
    read = snapshot(run_root)
    tail = {
        program.parent.name: invoke_stage(program, run_root, catalogue, "--placement-tier", TIER)
        for program in TAIL_FROM_RECENSOR
    }
    return SimpleNamespace(
        work=work,
        catalogue=catalogue,
        run_root=run_root,
        acts=[row["act_id"] for row in designator.rows],
        witnesses=witnesses,
        reader=reader,
        witness_exit=witness_exit,
        reader_exit=reader_exit,
        read=read,
        tail=tail,
    )


# ========================= what a real run cannot do =========================


def test_the_designator_itself_refuses_a_real_submission_and_writes_nothing(tmp_path):
    """Why the Designator layer above is hand-built, driven rather than asserted.

    The stage that would produce a real structural denominator refuses to
    invent one, so this module supplies the layer by hand and says so. The
    refusal is honest in both directions: it names what it will not do, and the
    tree it leaves is byte for byte what the Ink Map sealed.
    """
    registry = ChairRegistry.from_toml(str(MODELS_CONFIG))
    catalogue = write_live_catalogue(tmp_path / "serving_recipes_live.toml", registry)
    run_root = _real_submission(
        tmp_path, "--serving-recipes-config", str(catalogue), "--models-config", str(MODELS_CONFIG)
    )
    before = snapshot(run_root)

    result = invoke_stage(DESIGNATOR_CLI, run_root, catalogue)

    assert result.returncode == EXIT_FATAL, result.stderr
    assert "real structural proposal/model work is outside System 03" in result.stderr
    assert "no proposals or holds were fabricated" in result.stderr
    assert snapshot(run_root) == before, "a refused structural pass writes nothing"


# ======================= the contexts, stage by stage ========================


def test_every_stage_whose_predecessor_sealed_opens_a_real_context_through_the_constructor(
    real_run,
):
    """The section's whole subject, checked on a run that really carried.

    Each stage opens through `open_stage_context` over this run's own authority
    and receives what its body needs before its first line: the roster, the
    sealed digest map including the three names only a real run seals, the
    format projection, the parsed recovery policy, and the constant scenario --
    never `--scenario`. The `fixture` slot refuses by name, naming the stage
    that asked, so an unconverted reader is a named refusal rather than an empty
    list that passes.
    """
    for stage in OPENING_STAGES:
        context = open_real_context(real_run.run_root, real_run.catalogue, stage)
        assert context.stage == stage
        assert context.scenario == REAL_SCENARIO
        assert context.registry is not None, stage
        assert context.armarium_formats is not None, stage
        assert context.recovery_policy is not None, stage
        sealed = context.sealed_config_digests
        for name in ("decoding", "alignment", "recovery", "models", "armarium-formats"):
            assert sealed.get(name), f"{stage} opened without a sealed {name} digest"
        assert sealed.get("run-policy"), f"{stage} opened without the sealed run policy"
        with pytest.raises(ContractError, match=f"{stage} asked its context for fixture"):
            _ = context.fixture


def test_the_two_stages_past_the_stopping_point_still_open_before_they_refuse(real_run):
    """The Archetypus and the Armarium reach their seal refusal, not a mode fault.

    Nothing sealed the Recensor on this run (below), so these two cannot finish
    opening -- `verify_predecessor_seal` is the last step of `_open_real_context`.
    Reaching *that* refusal is the claim: it fires after the register snapshot,
    the roster, and the whole name-by-name binding recheck, so a stage that
    names its missing predecessor is a stage whose real context was otherwise
    built. A context that never got one would have said `sealed no digest`, and
    the fixture-only opener would have said `bound to different config_digest`.
    """
    for stage in SEAL_REFUSING_STAGES:
        with pytest.raises(SchemaRefusal) as refusal:
            open_real_context(real_run.run_root, real_run.catalogue, stage)
        message = str(refusal.value)
        assert f"{stage} refuses: predecessor {SEAL_PREDECESSORS[stage]} has no stage-seal" in (
            message
        )
        assert "never re-derived" in message
        assert "sealed no digest" not in message
        assert "bound to different" not in message


def test_opening_every_real_context_writes_nothing(real_run, tmp_path):
    """GOVERNANCE 4 at the open: a recheck that writes before it decides is a
    recheck that has already spent the evidence it was protecting."""
    run_root = tmp_path / "runs"
    shutil.copytree(real_run.run_root, run_root)
    before = snapshot(run_root)

    for stage in OPENING_STAGES:
        open_real_context(run_root, real_run.catalogue, stage)
    for stage in SEAL_REFUSING_STAGES:
        with pytest.raises(SchemaRefusal):
            open_real_context(run_root, real_run.catalogue, stage)

    assert snapshot(run_root) == before


@pytest.mark.parametrize(
    ("flag", "value", "named"),
    [
        ("--decoding-config", lambda tmp: _appended(tmp, DEFAULT_DECODING_CONFIG_PATH), "decoding"),
        ("--models-config", _moved_models_config, "models"),
        (
            "--formats-config",
            lambda tmp: _appended(tmp, DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH),
            "armarium-formats",
        ),
        ("--witness-context", lambda _tmp: "blinded", "run-policy"),
    ],
)
def test_a_moved_configuration_is_refused_by_name_before_the_run_is_touched(
    real_run, tmp_path, flag, value, named
):
    """The real path's stand-in for `open_context`'s whole-digest check.

    A real run's `config_digest` cannot be recomputed downstream -- it binds the
    Door machine's own decoder versions -- so what protects a resumed real run
    is the name-by-name recheck of the sealed map. Each leg moves one sealed
    input and requires the refusal to name that input, not a missing boundary,
    and to leave the tree exactly as it found it. Three of the four names are
    sealed by the real Door on real ingress alone (`models`,
    `armarium-formats`, `run-policy`); a recheck missing them would let a run
    resume under a different roster, a different export projection or a
    different witness regime with every check green.

    `common/test_stage_real_ingress.py` holds the same rule at unit scale, on a
    tree whose Designator never sealed -- so there the clean outcome is a seal
    refusal either way. What this leg adds is the contrast that tree cannot
    show: the Attestatores' context on *this* run opens cleanly, and the same
    open with one input moved refuses instead. The recheck is decisive on a
    healthy run, not merely reached on a doomed one.
    """
    run_root = tmp_path / "runs"
    shutil.copytree(real_run.run_root, run_root)
    before = snapshot(run_root)
    open_real_context(run_root, real_run.catalogue, ATTESTATORES)

    parser = stage_parser(f"moved {named} configuration")
    args = parser.parse_args(stage_argv(run_root, real_run.catalogue, flag, str(value(tmp_path))))
    with pytest.raises(IncompatibleReuse) as refusal:
        open_stage_context(args, ATTESTATORES)

    message = str(refusal.value)
    assert f"sealed configuration {named} moved" in message, message
    assert "no stage-seal" not in message, message
    assert message.endswith(
        "No stage work was written. Resume with the original sealed inputs, or start a "
        "new run for the changed inputs"
    ), message
    assert snapshot(run_root) == before


# ============================ the real denominator ===========================


def test_the_real_denominator_is_recomputed_from_the_designators_own_evidence(real_run):
    """No declaration is counted: every row is verified against its own region.

    On the real route `expected_acts` classifies each seal row by which
    Designator evidence exists for it and recomputes a structural act's identity
    from that region's `raw_bounds` -- the producer-independent check the
    fixture route gets from its sealed declaration instead. Reaching three
    verified rows here is what says the denominator a real run's five later
    stages share was measured rather than believed.
    """
    context = open_real_context(real_run.run_root, real_run.catalogue, RECENSOR)
    acts = expected_acts(context)

    assert [act["act_id"] for act in acts] == real_run.acts
    assert [act["act_key"] for act in acts] == list(ACT_KEYS)
    assert {act["outcome"] for act in acts} == {"proposed"}
    assert sorted(exemplar_page_ids(context)) == [1, 2]
    assert [act["page_id"] for act in acts] == [
        exemplar_page_ids(context)[ordinal] for ordinal, _bounds, _key in ACTS
    ]


# ============================== the whole carry ==============================


def test_the_witness_and_reading_passes_complete_over_a_real_submission(real_run):
    """Two whole stages, on a real submission, offline — and nothing declared.

    This is what Section B bought that no run could do before it: the
    Attestatores' full served roster and the Perlector's reading pass both run
    to completion on real ingress. Any touch of `context.fixture` anywhere in
    either pass would have refused it by name, so completion is what says every
    fixture reader on this path was replaced, derived or gated.
    """
    assert real_run.witness_exit == EXIT_COMPLETE
    assert real_run.reader_exit == EXIT_COMPLETE


def test_a_real_run_stops_at_the_recensor_and_names_the_denominator_it_has_no_producer_for(
    real_run,
):
    """How far a real submission goes today, measured rather than described.

    The Recensor is the first stage to ask the Designator for something a
    hand-built structural layer cannot honestly supply: the R2 conservation
    denominator -- per sealed page, how much ink there is, how much the
    proposed crops claim, and what the unclaimed remainder became. Those are
    measurements of the real page, and this module will not invent them: a
    conservation record composed here would be exactly the fabricated
    structural accounting `pipeline/2_designator/run.py` refuses to produce, and
    every stage after it would then be reading numbers no producer measured
    (GOVERNANCE 10).

    So the run stops here, by name, with nothing written -- and that is the
    honest boundary of real ingress today. The context is not what stops it:
    the Recensor opened, recomputed the real denominator from the Designator's
    own regions, and got as far as asking for evidence that does not exist.
    Closing this gap is the real structural pass (roadmap item 4), and this
    assertion is what will say when it lands.
    """
    recensor = real_run.tail["5_recensor"]

    assert recensor.returncode == EXIT_FATAL, recensor.stderr
    assert (
        "Designator conservation pages 1, 2 carry non-held expected acts "
        "but have no conservation records"
    ) in recensor.stderr, recensor.stderr
    assert "asked its context for fixture declarations" not in recensor.stderr
    assert "sealed no digest" not in recensor.stderr
    assert "bound to different" not in recensor.stderr
    assert "Traceback" not in recensor.stderr
    assert snapshot(real_run.run_root) == real_run.read, (
        "the stage that stopped the run wrote nothing on its way out"
    )


def test_the_last_two_stages_refuse_by_name_rather_than_carrying_an_unsealed_run(real_run):
    """Nothing downstream invents the seal the stopped stage never wrote.

    The Archetypus and the Armarium are driven anyway, as an operator retrying
    the sequence would: each refuses at its own predecessor boundary, names it,
    writes nothing, and leaves no traceback. A run that stopped is visibly
    stopped at every later stage (GOVERNANCE 2), not quietly resumed.
    """
    for name, stage in (("6_archetypus", ARCHETYPUS), ("7_armarium", ARMARIUM)):
        result = real_run.tail[name]
        assert result.returncode == EXIT_FATAL, result.stderr
        assert f"predecessor {SEAL_PREDECESSORS[stage]} has no stage-seal" in result.stderr
        assert "asked its context for fixture declarations" not in result.stderr, name
        assert "sealed no digest" not in result.stderr, name
        assert "Traceback" not in result.stderr, name


def test_every_witness_and_the_reader_really_served_this_real_submission(real_run):
    """The chairs answered; nothing replayed a declaration.

    A real submission carries no fixture for a witness to answer from, so a
    record that named one would mean the pass had read something that does not
    exist. Each act reaches a Perlectio bound to the bytes the reader's engine
    actually sent.
    """
    tree = RunTree(real_run.run_root, RUN_ID)
    context = open_real_context(real_run.run_root, real_run.catalogue, ATTESTATORES)
    records = {
        (record["subject_id"], record["payload"]["chair"]): record
        for record in (
            tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
            for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
            if entry["kind"] == "testimonium"
        )
    }
    assert {chair for _act, chair in records} == set(WITNESS_CHAIRS)
    assert real_run.witnesses.loads == sorted(WITNESS_CHAIRS), "one residency per chair"
    for (act_id, chair), record in records.items():
        payload = record["payload"]
        assert attestatores.served_live(context, payload["provenance"]), (act_id, chair)
        call = json.loads(tree.read_bytes(payload["serving_call_ref"]["relative_path"]))
        assert call["chair"] == chair
        wire = tree.read_bytes(call["raw_response_ref"]["relative_path"])
        assert wire in real_run.witnesses.served(chair), (
            f"{chair}'s record for {act_id} names bytes its endpoint never served"
        )

    readings = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(
            (real_run.run_root / RUN_ID / "4_perlector" / "artifacts" / "perlectio").glob("*.json")
        )
    ]
    assert {record["subject_id"] for record in readings} == set(real_run.acts)
    assert {record["outcome"] for record in readings} == {"read"}
    served = real_run.reader.endpoint.served
    for record in readings:
        call = record["payload"]["engine_call"]
        assert tree.read_bytes(call["raw_response_ref"]["relative_path"]) in served, (
            "a Perlectio names retained bytes the reader's engine never sent"
        )


# ========================== the export's own identity ========================


def test_the_export_would_be_named_by_the_submissions_own_identity(real_run):
    """A corpus identity never travels under the word `fixture_id`.

    The Armarium cannot run on this tree -- the run stopped at the Recensor --
    so the claim is checked where it is actually decided: the stage's own
    `export_run_identity`, over this run's real authority. It answers with the
    submission's filename-ledger self-hash under `submission_id`, with
    `fixture_id` absent rather than blank, and it never touches the refusing
    fixture accessor to find that out -- the context handed to it carries
    `fixture=None`, so a touch would refuse rather than return a label.

    The identity is checked against the ledger file `operations/submit` wrote
    when the submission was admitted, not against the run authority that quotes
    it, so this is two independent readings agreeing rather than one repeated.
    """
    tree = RunTree(real_run.run_root, RUN_ID)
    run = tree.read_run()
    ledger = json.loads(
        (real_run.run_root.parent / "submission-ledger.json").read_text(encoding="utf-8")
    )
    context = StageContext(
        tree=tree,
        run=run,
        fixture=None,
        scenario=REAL_SCENARIO,
        stage=ARMARIUM,
        adapter_revision=adapter_recipe_for(run, ARMARIUM),
        args=None,
        registry=None,
    )

    submission_id, fixture_id, run_identity = armarium.export_run_identity(context)

    assert submission_id == ledger["self_hash"]
    assert submission_id == submission_identity(run)
    assert fixture_id is None
    assert run_identity == {"submission_id": submission_id}
    assert "fixture_id" not in run_identity
    assert context.scenario == REAL_SCENARIO
    with pytest.raises(ContractError, match="armarium asked its context for fixture"):
        _ = context.fixture
