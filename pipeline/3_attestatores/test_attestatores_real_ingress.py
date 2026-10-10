"""Real ingress at the Attestatores: the context opens through the shared
constructor, every fixture reader is replaced or refuses by name, and a roster
where every witness is served runs its full pass over a real submission.

The run tree is real to the Ink Map's seal -- the Door, the Exemplar and the Ink
Map run as programs over a genuine real submission, made of the synthetic
fixture's own two pages copied into an approved storage root -- and the
Designator's record-detector census is then **hand-built** on top, shaped
exactly as `pipeline/2_designator/run.py::_publish_detector_records` publishes
it (a crop really cut from the sealed page, an identity that binds its
transform, provenance naming a receipt this run wrote), so the checks this stage
runs before any chair is asked hold over it as they would over the real
producer's output.

Every chair here is `operations/serving/fakes.py`: nothing starts a pod,
contacts a provider or loads a model. The fakes stand behind a real
`ServingManager`, a real `ChairClient` and this stage's own `main`, through the
same in-process factory seam the live-pass tests use.

What is proven:

- `main` owns no opener of its own and hands argv, its stage name and the
  registry factory to `open_stage_context`;
- a real run with no Designator seal refuses at that seal with its context
  already open -- no "sealed no digest", no fixture refusal, no traceback,
  nothing written;
- a real run sealed under the fixture catalogue is refused by name before any
  chair is asked, because a real submission has no fixture to answer for a
  witness (every witness runs its own full pass);
- the shipped real catalogue serves every witness chair at every placement
  tier, so the mixed-posture guard never fires on it;
- a full pass over a real submission completes: every page record is
  published and names the Exemplar's own page subject, no fixture declaration
  is read or reported, and the fixture accessor is never touched (any touch
  would have refused the pass);
- a page ordinal the Exemplar never accounted for is a named refusal.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import load_stage

STAGE = Path(__file__).resolve().parent
ROOT = STAGE.parents[1]
if str(STAGE) not in sys.path:
    sys.path.insert(0, str(STAGE))

# The live-pass module's own helpers: the served catalogue writer, the scripted
# fake world, the record readers. Reused rather than copied, so a change to how
# a fake chair is stood up is made once.
from test_attestatores_live_pass import (  # noqa: E402
    CHANDRA_BODY,
    TIER,
    LiveWorld,
    page_records,
    refusing_factory,
    write_live_catalogue,
)

from common.chairs.models import ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.approval import real_ingress_record  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting  # noqa: E402
from common.contracts.identities import region_id  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR, INK_MAP  # noqa: E402
from common.decoding import load_decoding_policy  # noqa: E402
from common.exemplar_boundary import cut_exemplar_crop  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    REAL_SCENARIO,
    StageContext,
    adapter_recipe_for,
    exemplar_page_ids,
    fixture_serving_details,
    open_stage_context,
)
from operations.serving.config import load_serving_recipes  # noqa: E402
from operations.serving.fakes import ScriptedAnswer  # noqa: E402
from operations.submit import gate, submit  # noqa: E402

DOOR_CLI = ROOT / "pipeline" / "1_exemplar" / "door.py"
EXEMPLAR_CLI = ROOT / "pipeline" / "1_exemplar" / "run.py"
INK_MAP_CLI = ROOT / "pipeline" / "1_ink_map" / "run.py"
ATTESTATORES_CLI = STAGE / "run.py"
MODELS_CONFIG = ROOT / "config" / "models.toml"
FIXTURE_CATALOGUE = ROOT / "config" / "serving_recipes.toml"
FIXTURE_PAGES = ROOT / "proof" / "fixtures" / "synthetic-two-page-v0"
RUN_ID = "real-attestatores"
WITNESS_CHAIRS = ("attestator_1", "attestator_2", "attestator_3")

# Three detector records over the two submitted 200x260 pages: two on page 1,
# one on page 2. Chandra and Churro therefore answer once per page, and DAI once
# per detector record, three times; a script whose length disagreed would be the
# first thing to notice a unit regression.
ACTS: tuple[tuple[int, dict[str, int], str], ...] = (
    (1, {"x": 10, "y": 10, "w": 180, "h": 100}, "structural:1:1"),
    (1, {"x": 10, "y": 130, "w": 180, "h": 100}, "structural:1:2"),
    (2, {"x": 10, "y": 10, "w": 180, "h": 100}, "structural:2:1"),
)


attestatores = load_stage("3_attestatores")


# ------------------------------ the real run tree ------------------------------


def _run_program(program: Path, *argv: str) -> None:
    result = subprocess.run(
        [sys.executable, str(program), *argv], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, f"{program.name}: {result.stderr}"


def _real_submission(base: Path, *stage_argv: str) -> Path:
    """Door, Exemplar and Ink Map as programs over a real submission of two pages.

    `stage_argv` is forwarded to all three, the way the orchestrator forwards a
    run's configuration flags: the Door seals the serving catalogue's digest and
    every later open rechecks it, so the three must name the same catalogue.
    """
    approved = base / "approved-storage"
    source = approved / "submitted-pages"
    source.mkdir(parents=True)
    for name in ("page-1.png", "page-2.png"):
        shutil.copyfile(FIXTURE_PAGES / name, source / name)
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(approved)]
    policy_path = base / "data-gate-policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    ledger = approved / "submission-ledger.json"
    submit.submit(source, ledger, policy_path=policy_path)
    root = approved / "runs"
    _run_program(
        DOOR_CLI,
        "--run-root",
        str(root),
        "--run-id",
        RUN_ID,
        "--submission-folder",
        str(source),
        "--submission-manifest",
        str(ledger),
        "--data-gate-policy",
        str(policy_path),
        *stage_argv,
    )
    _run_program(EXEMPLAR_CLI, "--run-root", str(root), "--run-id", RUN_ID, *stage_argv)
    _run_program(INK_MAP_CLI, "--run-root", str(root), "--run-id", RUN_ID, *stage_argv)
    return root


@pytest.fixture(scope="module")
def served_run(tmp_path_factory) -> SimpleNamespace:
    """A real submission sealed under a catalogue whose witness rows are live."""
    work = tmp_path_factory.mktemp("real-served")
    registry = ChairRegistry.from_toml(str(MODELS_CONFIG))
    catalogue = write_live_catalogue(work / "serving_recipes_live.toml", registry)
    root = _real_submission(
        work, "--serving-recipes-config", str(catalogue), "--models-config", str(MODELS_CONFIG)
    )
    _policy, decoding_sha256 = load_decoding_policy(ROOT / "config" / "decoding.toml")
    return SimpleNamespace(
        catalogue=catalogue, models=MODELS_CONFIG, run_root=root, decoding_sha256=decoding_sha256
    )


@pytest.fixture(scope="module")
def fixture_catalogue_run(tmp_path_factory) -> Path:
    """The same real submission, sealed under the committed fixture catalogue."""
    return _real_submission(tmp_path_factory.mktemp("real-fixture-catalogue"))


def _copy(template: Path, tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    shutil.copytree(template, root)
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def _argv(run_root: Path, catalogue: Path, *extra: str) -> list[str]:
    return [
        "--run-root",
        str(run_root),
        "--run-id",
        RUN_ID,
        "--serving-recipes-config",
        str(catalogue),
        "--models-config",
        str(MODELS_CONFIG),
        *extra,
    ]


def _open(run_root: Path, catalogue: Path, stage: str, *extra: str) -> StageContext:
    parser = attestatores.stage_parser("real-ingress stage context")
    return open_stage_context(parser.parse_args(_argv(run_root, catalogue, *extra)), stage)


def _run_main(run_root: Path, catalogue: Path, *, factory, extra: tuple[str, ...] = ()) -> int:
    original, sys.argv = sys.argv, ["run.py", *_argv(run_root, catalogue, *extra)]
    try:
        return attestatores.main(serving_factory=factory)
    finally:
        sys.argv = original


# ------------------------- the Designator, built by hand -------------------------


class _RealDesignator:
    """The Designator's record-detector census over a real run, hand-built.

    The context carries `fixture=None`: a seal that needed a fixture to publish
    could not come from a real producer either.
    """

    def __init__(self, root: Path):
        self.root = root
        self.tree = RunTree(root, RUN_ID)
        run = self.tree.read_run()
        registry = ChairRegistry.from_toml(str(MODELS_CONFIG))
        self.context = StageContext(
            tree=self.tree,
            run=run,
            fixture=None,
            scenario=REAL_SCENARIO,
            stage=DESIGNATOR,
            adapter_revision=adapter_recipe_for(run, DESIGNATOR),
            args=None,
            registry=registry,
        )
        self.pages = {
            record["payload"]["ordinal"]: record
            for record in (
                self.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
                for entry in self.tree.build_manifest(EXEMPLAR)["artifacts"]
                if entry["kind"] == "page"
            )
            if record["outcome"] == "sealed"
        }
        self.detector_provenance = self._served_provenance(registry, "secondary_proposer")

    def _served_provenance(self, registry: ChairRegistry, chair: str) -> dict[str, Any]:
        resolved = registry.resolve(chair)
        assert isinstance(resolved, ChairIdentity)
        return {
            "chair": resolved.role,
            "chair_state": "configured",
            "resolved_identity": resolved.to_record(),
            "resolved_revision": {
                "kind": resolved.receipt_revision_kind,
                "value": resolved.receipt_revision,
            },
            "receipt_ref": self.context.write_serving_receipt(
                resolved, fixture_serving_details(resolved)
            ),
            "adapter_revision": self.context.adapter_revision,
        }

    def detect(self, ordinal: int, rectangles: list[dict[str, int]]) -> None:
        """DAI's record detector census for one page: one cut record per rectangle.

        Shaped as the Designator's `_publish_detector_records` publishes it, so
        the Attestatores' own crop re-derivation holds over every record crop.
        """
        page = self.pages[ordinal]
        page_id = page["subject_id"]
        page_bytes = self.tree.read_bytes(page["payload"]["image_path"])
        page_ref = self.context.input_ref(page["payload"]["image_path"])
        subjects = []
        for index, bounds in enumerate(rectangles):
            subject = f"{page_id}-detector-{index}"
            crop = cut_exemplar_crop(self.context.retain, page_bytes, ordinal, page_id, bounds)
            region = self.context.publish(
                kind="detector-region",
                subject_id=subject,
                outcome="proposed",
                inputs=[page_ref],
                payload={
                    "region_id": region_id(subject, crop["transform"]),
                    "record_key": subject,
                    "origin": "detector",
                    **crop,
                    "raw_bounds": bounds,
                    "padding": None,
                    "provenance": self.detector_provenance,
                },
            )
            region_ref = self.context.input_ref(region.relative_path)
            self.context.publish(
                kind="detector-record",
                subject_id=subject,
                outcome="proposed",
                inputs=[page_ref, region_ref],
                payload={
                    "page_ordinal": ordinal,
                    "detector_ordinal": index,
                    "bounds": bounds,
                    "cut": True,
                    "region_ref": region_ref,
                    "provenance": self.detector_provenance,
                },
            )
            subjects.append(subject)
        self.context.publish(
            kind="detector-page",
            subject_id=page_id,
            outcome="proposed",
            inputs=[page_ref],
            payload={
                "page_ordinal": ordinal,
                "detection_count": len(subjects),
                "record_subjects": subjects,
                "provenance": self.detector_provenance,
            },
        )

    def seal(self) -> None:
        self.context.seal_boundary()
        self.context.finish()


def _designate(run_root: Path) -> _RealDesignator:
    designator = _RealDesignator(run_root)
    by_page: dict[int, list[dict[str, int]]] = {}
    for ordinal, bounds, _key in ACTS:
        by_page.setdefault(ordinal, []).append(bounds)
    for ordinal in sorted(by_page):
        designator.detect(ordinal, by_page[ordinal])
    designator.seal()
    return designator


def _scripts() -> dict[str, list[ScriptedAnswer]]:
    return {
        "attestator_1": [
            ScriptedAnswer(content=CHANDRA_BODY, finish_reason="stop"),
            ScriptedAnswer(content=CHANDRA_BODY, finish_reason="stop"),
        ],
        "attestator_2": [
            ScriptedAnswer(content=f"REAL ACT {number}", finish_reason="stop")
            for number in ("ONE", "TWO", "THREE")
        ],
        "attestator_3": [
            ScriptedAnswer(
                content="<HistoricalDocument><Page><Body><Line>REAL ACT ONE</Line><Line>REAL ACT TWO</Line></Body></Page></HistoricalDocument>",
                finish_reason="stop",
            ),
            ScriptedAnswer(
                content="<HistoricalDocument><Page><Body><Line>REAL ACT THREE</Line></Body></Page></HistoricalDocument>",
                finish_reason="stop",
            ),
        ],
    }


def _real_context(**fields: Any) -> StageContext:
    """A real-route context with no tree: enough for the readers that never touch one."""
    return StageContext(
        tree=None,
        run={"ingress": real_ingress_record()},
        fixture=None,
        scenario=REAL_SCENARIO,
        stage=ATTESTATORES,
        adapter_revision="unproven-real-attestatores",
        args=None,
        registry=None,
        **fields,
    )


# ================================ the constructor ================================


def test_main_opens_through_the_shared_constructor_and_owns_no_opener(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    class _Opened(Exception):
        pass

    def opener(args, stage, *, registry_factory):
        seen.update(run_root=args.run_root, stage=stage, registry_factory=registry_factory)
        raise _Opened

    monkeypatch.setattr(attestatores, "open_stage_context", opener)
    monkeypatch.setattr(sys, "argv", ["run.py", "--run-root", str(tmp_path), "--run-id", RUN_ID])
    factory = object()

    with pytest.raises(_Opened):
        attestatores.main(registry_factory=factory)

    assert seen == {"run_root": str(tmp_path), "stage": ATTESTATORES, "registry_factory": factory}
    assert not hasattr(attestatores, "open_context")


def test_a_real_run_refuses_at_the_missing_designator_seal_with_its_context_opened(
    served_run, tmp_path
):
    """A stage that reached its seal refusal is a stage whose context opened.

    The refusal names the predecessor boundary, not a configuration nobody
    changed, not the fixture, and not a traceback; and the tree is byte for
    byte what the Ink Map left.
    """
    run_root = _copy(served_run.run_root, tmp_path)
    before = _snapshot(run_root)

    result = subprocess.run(
        [
            sys.executable,
            str(ATTESTATORES_CLI),
            *_argv(run_root, served_run.catalogue, "--placement-tier", TIER),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "predecessor designator has no stage-seal" in result.stderr
    assert "sealed no digest" not in result.stderr
    assert "asked its context for fixture" not in result.stderr
    assert "Traceback" not in result.stderr
    assert _snapshot(run_root) == before


# ============================== the served posture ==============================


def test_a_real_run_under_the_fixture_catalogue_is_refused_before_any_chair_is_asked(
    fixture_catalogue_run, tmp_path
):
    run_root = _copy(fixture_catalogue_run, tmp_path)
    _designate(run_root)
    before = _snapshot(run_root)

    with pytest.raises(ContractError, match="every configured witness chair must be served"):
        _run_main(run_root, FIXTURE_CATALOGUE, factory=refusing_factory)

    assert _snapshot(run_root) == before, "a refused posture writes nothing"


def test_require_every_witness_served_names_the_unserved_chair_and_an_empty_roster():
    served = {chair: "live" for chair in WITNESS_CHAIRS}
    attestatores.require_every_witness_served(served)

    with pytest.raises(ContractError, match=r"\['attestator_2'\] a fixture posture"):
        attestatores.require_every_witness_served({**served, "attestator_2": "fixture"})
    with pytest.raises(ContractError, match="no configured witness chair serves"):
        attestatores.require_every_witness_served({})


def test_the_shipped_real_catalogue_serves_every_witness_chair_at_every_tier():
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models-real.toml"))
    recipes = load_serving_recipes(ROOT / "config" / "serving_recipes_real.toml")
    placement = tomllib.loads((ROOT / "config" / "pod_placement.toml").read_text(encoding="utf-8"))
    tiers = [row["id"] for row in placement["tiers"]]
    assert tiers, "the placement policy names at least one tier"
    context = SimpleNamespace(
        witness_chairs=list(registry.config.witness_chairs), registry=registry
    )

    for tier in tiers:
        modes = attestatores.witness_serving_modes(context, recipes, tier)
        assert modes == {chair: "live" for chair in registry.config.witness_chairs}, tier
        attestatores.require_every_witness_served(modes)


# ================================ the full pass ================================


def test_every_witness_runs_its_full_pass_over_a_real_submission(served_run, tmp_path, capsys):
    """The pass this section exists for, offline: three served chairs, no fixture.

    `--scenario` names a scenario no fixture declares, and the pass never reads
    it: the real route's scenario is the constant. Any touch of
    `context.fixture` would have refused the pass, so completion is the proof
    that every fixture reader on this path was replaced or gated.
    """
    run_root = _copy(served_run.run_root, tmp_path)
    _designate(run_root)
    world = LiveWorld(served_run, tmp_path / "world", scripts=_scripts())

    exit_code = _run_main(
        run_root,
        served_run.catalogue,
        factory=world.factory,
        extra=("--placement-tier", TIER, "--scenario", "no-such-declared-scenario"),
    )

    assert exit_code == attestatores.EXIT_COMPLETE
    assert "does not read" not in capsys.readouterr().err, "no fixture rows exist to pass over"
    assert world.loads == list(WITNESS_CHAIRS), "one residency per chair, chair-outer"
    assert len(world.requests("attestator_1")) == 2, "a page-scoped chair is asked once per page"
    assert len(world.requests("attestator_2")) == 3, "DAI is asked once per detector record"
    assert len(world.requests("attestator_3")) == 2

    tree = RunTree(run_root, RUN_ID)
    context = _open(run_root, served_run.catalogue, ATTESTATORES, "--placement-tier", TIER)
    pages = exemplar_page_ids(context)
    assert sorted(pages) == [1, 2]

    kinds = {entry["kind"] for entry in tree.build_manifest(ATTESTATORES)["artifacts"]}
    assert "testimonium" not in kinds and "act-attachment" not in kinds
    page = page_records(tree)
    assert set(page) == {(ordinal, chair) for ordinal in (1, 2) for chair in WITNESS_CHAIRS}
    for (ordinal, _chair), record in page.items():
        assert record["subject_id"] == pages[ordinal], (
            "the page record names the Exemplar's own page"
        )
        receipt = tree.read_run_receipt(record["payload"]["provenance"]["receipt_ref"])
        assert not receipt["endpoint"].startswith("fixture://"), (
            "a real page record's receipt answers for a live chair, not a fixture"
        )
    assert "stage-seal" in kinds


# ============================ the replaced readers ============================


def test_a_real_pass_names_nothing_unread(capsys):
    attestatores.refuse_unread_fixture_declarations(_real_context(), list(WITNESS_CHAIRS))
    assert capsys.readouterr().err == ""


def test_a_page_ordinal_the_exemplar_never_accounted_for_is_refused_by_name(served_run, tmp_path):
    run_root = _copy(served_run.run_root, tmp_path)
    context = _open(run_root, served_run.catalogue, INK_MAP)

    assert attestatores.page_subject(context, 1) == exemplar_page_ids(context)[1]
    with pytest.raises(FatalAccounting, match="page ordinal 9 names no Exemplar page"):
        attestatores.page_subject(context, 9)


# ============================== review findings ================================


def test_page_subject_reuses_a_supplied_index_rather_than_rewalking_the_exemplar():
    """`page_ids` is an opt-in cache: given one, `page_subject` never asks the tree.

    Review found the un-cached form rebuilding the whole Exemplar page index --
    an O(P) validated-artifact walk -- on every one of the ~7 lookups a
    page-scoped chair's pass makes per page. `_RefusingTree` proves the fast
    path never reaches the tree at all once a caller has built the index once.
    """

    class _RefusingTree:
        def build_manifest(self, stage):
            raise AssertionError(
                "page_subject must not rewalk the Exemplar when page_ids is supplied"
            )

    context = StageContext(
        tree=_RefusingTree(),
        run={"ingress": real_ingress_record()},
        fixture=None,
        scenario=REAL_SCENARIO,
        stage=ATTESTATORES,
        adapter_revision="unproven-real-attestatores",
        args=None,
        registry=None,
    )

    assert attestatores.page_subject(context, 1, page_ids={1: "page-one"}) == "page-one"
    with pytest.raises(FatalAccounting, match="page ordinal 9 names no Exemplar page"):
        attestatores.page_subject(context, 9, page_ids={1: "page-one"})


def test_a_pass_walks_the_exemplar_index_once(served_run, tmp_path, monkeypatch):
    """The full pass builds the Exemplar page index once and threads it through.

    Every `page_subject`/`presentation_for_page` call is handed the index, so a
    pass never rewalks the validated Exemplar inventory per page and chair.
    """
    run_root = _copy(served_run.run_root, tmp_path)
    _designate(run_root)
    world = LiveWorld(served_run, tmp_path / "world", scripts=_scripts())

    real_exemplar_page_ids = attestatores.exemplar_page_ids
    calls: list[Any] = []

    def counting(context):
        calls.append(context)
        return real_exemplar_page_ids(context)

    monkeypatch.setattr(attestatores, "exemplar_page_ids", counting)

    exit_code = _run_main(
        run_root,
        served_run.catalogue,
        factory=world.factory,
        extra=("--placement-tier", TIER, "--scenario", "no-such-declared-scenario"),
    )

    assert exit_code == attestatores.EXIT_COMPLETE
    assert len(calls) == 1, "one walk per pass, not one per page_subject call site"


def test_presentation_for_page_refuses_a_refused_page_by_name_rather_than_a_keyerror():
    """A refused Exemplar page keeps its ordinal but carries no sealed pixels.

    Review found `presentation_for_page` indexing a refused page's payload
    directly, which raises a raw `KeyError: 'image_path'` rather than naming
    what is wrong: a refused page's payload is `ordinal`, `declared_path`,
    `declared_sha256`, `reason` -- never `image_path` -- because no witness may
    be shown pixels the Door never admitted.
    """

    class _RefusedPageTree:
        def build_manifest(self, stage):
            assert stage == EXEMPLAR
            return {"artifacts": [{"kind": "page", "artifact_id": "exemplar-page-refused-1"}]}

        def read_artifact(self, stage, kind, artifact_id):
            assert (stage, kind) == (EXEMPLAR, "page")
            return {
                "subject_id": "source-1",
                "outcome": "refused",
                "payload": {
                    "ordinal": 1,
                    "declared_path": "page-1.png",
                    "declared_sha256": "0" * 64,
                    "reason": "declared-digest-mismatch",
                },
            }

    context = StageContext(
        tree=_RefusedPageTree(),
        run={"ingress": real_ingress_record(), "source_manifest": [{"ordinal": 1}]},
        fixture=None,
        scenario=REAL_SCENARIO,
        stage=ATTESTATORES,
        adapter_revision="unproven-real-attestatores",
        args=None,
        registry=None,
    )

    with pytest.raises(
        FatalAccounting, match="page ordinal 1 was refused at the Door and carries no sealed"
    ):
        attestatores.presentation_for_page(context, 1)
