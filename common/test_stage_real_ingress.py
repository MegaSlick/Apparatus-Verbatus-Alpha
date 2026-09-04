"""Unit tests: the real-ingress binding contract for stages after the Door.

The run tree is real to the Ink Map's seal -- the Door, the Exemplar and the Ink
Map run as programs over a genuine real submission, made of the synthetic
fixture's own two pages copied into an approved storage root -- and the
Designator's records are then **hand-built** on top. Hand-built precisely
because no real Designator exists: the real structural pass is roadmap work,
and these tests hold the *consumer* (`common/stage.py`) to the contract that
pass will have to meet before it is written. Nothing here fabricates a
Designator inside the stage program; the stage program is not invoked at all.

What is proven, unit by unit:

- `open_stage_context` opens a real run with a registry, the sealed digest map,
  the parsed formats and recovery policy, `fixture=None` behind a refusing
  accessor, and `REAL_SCENARIO` regardless of `--scenario`;
- `_refuse_incompatible_real_reuse` names the sealed policy that moved, fires
  before the predecessor-seal refusal, and writes nothing;
- `expected_acts` on a real run skips the fixture floor by name and recomputes a
  structural row against the producer's own `raw_bounds`, refusing altered
  bounds, unevidenced rows and ambiguous evidence;
- `exemplar_page_ids` agrees with the fixture declaration on the happy fixture
  run and with the sealed bytes on the real run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from common.contracts.approval import real_ingress_record
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import (
    ContractError,
    FatalAccounting,
    IncompatibleReuse,
    SchemaRefusal,
)
from common.contracts.identities import act_id as derive_act_id
from common.contracts.identities import attempt_id
from common.contracts.identities import page_id as derive_page_id
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR, INK_MAP
from common.decoding import DEFAULT_DECODING_CONFIG_PATH
from common.fixture_identity import page_identity
from common.imaging import dimensions
from common.runtree.store import RunTree
from common.stage import (
    DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH,
    REAL_SCENARIO,
    StageContext,
    _designator_records_by_subject,
    adapter_recipe_for,
    exemplar_page_ids,
    expected_acts,
    load_fixture,
    open_context,
    open_stage_context,
    real_run_policy_digest,
    stage_parser,
    submission_identity,
)
from operations.submit import gate, submit

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
DOOR_CLI = ROOT / "pipeline" / "1_exemplar" / "door.py"
EXEMPLAR_CLI = ROOT / "pipeline" / "1_exemplar" / "run.py"
INK_MAP_CLI = ROOT / "pipeline" / "1_ink_map" / "run.py"
MODELS_CONFIG = ROOT / "config" / "models.toml"
FIXTURE = "synthetic-two-page-v0"
FIXTURE_PAGES = ROOT / "proof" / "fixtures" / FIXTURE
RUN_ID = "real-ingress-unit"
FIXTURE_RUN_ID = "fixture-page-index-unit"


def _run_program(program: Path, *argv: str) -> None:
    result = subprocess.run(
        [sys.executable, str(program), *argv], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, f"{program.name}: {result.stderr}"


@pytest.fixture(scope="module")
def real_template(tmp_path_factory) -> tuple[Path, Path]:
    """One real submission, carried by the real programs to the Ink Map's seal.

    Stopping at the Ink Map is the point, not an economy: the Designator is the
    stage whose records are built by hand below, and its own program refuses on
    real ingress by design. Returns the run root and the submission ledger.
    """
    base = tmp_path_factory.mktemp("real-ingress-template")
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
    )
    _run_program(EXEMPLAR_CLI, "--run-root", str(root), "--run-id", RUN_ID)
    _run_program(INK_MAP_CLI, "--run-root", str(root), "--run-id", RUN_ID)
    return root, ledger


@pytest.fixture
def real_root(real_template, tmp_path) -> Path:
    """A private copy of the real run, so each test may publish into it."""
    template, _ledger = real_template
    root = tmp_path / "runs"
    shutil.copytree(template, root)
    return root


@pytest.fixture(scope="module")
def fixture_template(tmp_path_factory) -> Path:
    """The happy synthetic fixture run, Door and Exemplar only."""
    root = tmp_path_factory.mktemp("fixture-page-index-template") / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            "happy",
            "--run-root",
            str(root),
            "--run-id",
            FIXTURE_RUN_ID,
            "--from",
            "door",
            "--to",
            "exemplar",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return root


def _args(root: Path, run_id: str = RUN_ID, *extra: str, scenario: str = "happy"):
    """Argv the orchestrator would forward, with the two cwd-relative defaults pinned."""
    return stage_parser("real-ingress unit context").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
            "--fixture-root",
            str(ROOT / "proof"),
            "--models-config",
            str(MODELS_CONFIG),
            *extra,
        ]
    )


def _open(root: Path, stage: str, *extra: str, scenario: str = "happy") -> StageContext:
    return open_stage_context(_args(root, RUN_ID, *extra, scenario=scenario), stage)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


# Sentinel: "publish the ordinary transform", as against an explicit `None`.
_WELL_FORMED = object()


def _row(
    act: str, key: str, page: str, ordinal: int, outcome: str, evidence: list[dict]
) -> dict[str, Any]:
    return {
        "act_id": act,
        "act_key": key,
        "page_id": page,
        "page_ordinal": ordinal,
        "has_continuation": False,
        "outcome": outcome,
        "evidence": evidence,
    }


class _Designator:
    """The Designator's records over a real run, built by hand.

    The context carries `fixture=None`: the seal these tests hand-build must be
    publishable without a fixture in sight, or the producer this contract is
    written for could not exist either.
    """

    def __init__(self, root: Path):
        self.tree = RunTree(root, RUN_ID)
        run = self.tree.read_run()
        self.context = StageContext(
            tree=self.tree,
            run=run,
            fixture=None,
            scenario=REAL_SCENARIO,
            stage=DESIGNATOR,
            adapter_revision=adapter_recipe_for(run, DESIGNATOR),
            args=None,
            registry=None,
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
        self.rows: list[dict[str, Any]] = []

    def rectangle(self, ordinal: int) -> dict[str, int]:
        """A rectangle strictly inside the sealed page, so it is not the page's own."""
        width, height = dimensions(
            self.tree.read_bytes(self.pages[ordinal]["payload"]["image_path"])
        )
        return {"x": 0, "y": 0, "w": max(1, width // 2), "h": max(1, height // 2)}

    def propose(
        self,
        ordinal: int,
        bounds: dict[str, int],
        *,
        raw_bounds: dict[str, int] | None = None,
        act: str | None = None,
        key: str | None = None,
    ) -> str:
        """One proposal-origin region, shaped as `cut_minted_region` publishes it."""
        page = self.pages[ordinal]
        page_id = page["subject_id"]
        act = derive_act_id(page_id, "proposal", bounds) if act is None else act
        key = f"structural:{ordinal}:{len(self.rows) + 1}" if key is None else key
        published = self.context.publish(
            kind="region",
            subject_id=act,
            outcome="proposed",
            attempt=attempt_id(act, "crop", 1),
            inputs=[self.context.input_ref(page["payload"]["image_path"])],
            payload={
                "act_key": key,
                "attempt_ordinal": 1,
                "origin": "proposal",
                "transform": {
                    "operation": "crop",
                    "source_page_ordinal": ordinal,
                    "source_page_id": page_id,
                    "bounds": bounds,
                },
                "raw_bounds": bounds if raw_bounds is None else raw_bounds,
                "padding": None,
                "image_path": page["payload"]["image_path"],
                "image_sha256": page["payload"]["source_sha256"],
                "provenance": {"kind": "hand-built structural proposal"},
            },
        )
        self.rows.append(
            _row(
                act,
                key,
                page_id,
                ordinal,
                "proposed",
                [self.context.input_ref(published.relative_path)],
            )
        )
        return act

    def propose_far_page_region(
        self,
        act: str,
        key: str,
        ordinal: int,
        bounds: dict[str, int],
        *,
        transform: dict[str, Any] | None | object = _WELL_FORMED,
    ):
        """A second, far-page region for an act `propose` already minted a row for.

        Shaped exactly as `propose`'s own region, on the far page a continuation
        would be cut over. No row is appended -- one act still seals one row --
        so the caller is responsible for splicing the returned record's
        reference into that row's own `evidence` list before sealing.
        `transform` may be overridden to publish a region the consumer cannot
        place; a producer can write one, so the consumer is held to refusing it.
        """
        page = self.pages[ordinal]
        page_id = page["subject_id"]
        return self.context.publish(
            kind="region",
            subject_id=act,
            outcome="proposed",
            attempt=attempt_id(act, "crop", 2),
            inputs=[self.context.input_ref(page["payload"]["image_path"])],
            payload={
                "act_key": key,
                "attempt_ordinal": 2,
                "origin": "proposal",
                "transform": {
                    "operation": "crop",
                    "source_page_ordinal": ordinal,
                    "source_page_id": page_id,
                    "bounds": bounds,
                }
                if transform is _WELL_FORMED
                else transform,
                "raw_bounds": bounds,
                "padding": None,
                "image_path": page["payload"]["image_path"],
                "image_sha256": page["payload"]["source_sha256"],
                "provenance": {"kind": "hand-built structural continuation"},
            },
        )

    def hold_residual(self, ordinal: int, bounds: dict[str, int]) -> str:
        page_id = self.pages[ordinal]["subject_id"]
        act = derive_act_id(page_id, "residual", bounds)
        published = self.context.publish(
            kind="hold",
            subject_id=act,
            outcome="held",
            payload={
                "act_key": f"residual:{ordinal}:0",
                "page_ordinal": ordinal,
                "residual_bounds": bounds,
                "residual_pixel_count": bounds["w"] * bounds["h"],
                "reason": "hand-built residual hold",
            },
        )
        self.rows.append(
            _row(
                act,
                f"residual:{ordinal}:0",
                page_id,
                ordinal,
                "held",
                [self.context.input_ref(published.relative_path)],
            )
        )
        return act

    def hold_beside(self, act: str, ordinal: int) -> None:
        """A hold for an act that already has a row: no rectangle in its payload.

        A producer can write one -- an aborted hold, a payload shape that moved
        -- and it matches no minted class, so the consumer is held to refusing
        it rather than letting the act's regions reclassify it as structural.

        The row is turned `held` and both records spliced into its evidence,
        because that is the seal a producer publishing this hold would actually
        write, and it is the shape nothing else catches:
        `_verify_proposal_seal_evidence` refuses a `proposed` row carrying any
        hold, but a `held` row with exactly one hold is precisely what it
        expects to see.
        """
        published = self.context.publish(
            kind="hold",
            subject_id=act,
            outcome="held",
            payload={
                "act_key": f"held:{ordinal}:0",
                "page_ordinal": ordinal,
                "reason": "hand-built hold naming no rectangle",
            },
        )
        row = self.rows[-1]
        row["outcome"] = "held"
        row["evidence"] = sorted(
            [*row["evidence"], self.context.input_ref(published.relative_path)],
            key=lambda reference: reference["relative_path"],
        )

    def page_rectangle(self, ordinal: int) -> dict[str, int]:
        width, height = dimensions(
            self.tree.read_bytes(self.pages[ordinal]["payload"]["image_path"])
        )
        return {"x": 0, "y": 0, "w": width, "h": height}

    def fallback_record(self, act: str, ordinal: int, *, attempt: str | None = None) -> None:
        """A page-fallback record, shaped as `_publish_page_fallback` publishes it.

        Minus the structure-status input it would cite: these tests stop at the
        classification, and the fallback verifier's own premise check is what
        `common/test_stage_page_residual.py` and the acceptance run already hold.

        `attempt` is what lets a caller publish a *second* record for one act:
        an artifact id binds stage, kind, subject and attempt, so two records
        for one subject reach the manifest only when their attempts differ.
        """
        page = self.pages[ordinal]
        self.context.publish(
            kind="page-fallback",
            subject_id=act,
            outcome="proposed",
            attempt=attempt,
            payload={
                "act_key": f"page-fallback:{ordinal}",
                "page_id": page["subject_id"],
                "page_ordinal": ordinal,
                "page_bounds": self.page_rectangle(ordinal),
            },
        )

    def unevidenced_row(self, ordinal: int) -> str:
        page_id = self.pages[ordinal]["subject_id"]
        act = derive_act_id(page_id, "proposal", {"x": 1, "y": 1, "w": 1, "h": 1})
        self.rows.append(_row(act, f"structural:{ordinal}:9", page_id, ordinal, "proposed", []))
        return act

    def seal(self) -> None:
        payload: dict[str, Any] = {
            "expected_acts": self.rows,
            "count": len(self.rows),
            "provenance": {"kind": "hand-built proposal seal"},
        }
        payload["self_hash"] = self_hash(payload)
        self.context.publish(
            kind="proposal-seal",
            subject_id="proposal-seal",
            outcome="proposed",
            inputs=[reference for row in self.rows for reference in row["evidence"]],
            payload=payload,
        )
        self.context.seal_boundary()
        self.context.finish()


# --- the real context, opened ---------------------------------------------------


def test_a_real_run_opens_with_bindings_and_a_structural_row_recomputes_from_raw_bounds(
    real_root,
):
    """The honest shape, and the regression test for the planted defect.

    A structural `proposed` act on a real seal used to fall through the fixture
    floor -- `context.fixture.get("act", [])` read `[]` -- into the minted-row
    check, which refused it with "extends the denominator beyond the fixture" on
    a run that never had one. Now the floor is skipped by name and the row is
    recomputed against the rectangle its own region record says it was minted
    over.
    """
    designator = _Designator(real_root)
    act = designator.propose(1, designator.rectangle(1))
    designator.seal()

    # `--scenario` is argv nobody sealed on this route, so it is ignored, not
    # honoured: the context's scenario is the constant.
    context = _open(real_root, ATTESTATORES, scenario="no-such-declared-scenario")

    assert context.scenario == REAL_SCENARIO
    assert context.stage == ATTESTATORES
    assert context.registry is not None
    assert context.armarium_formats is not None
    assert context.serving_config_inputs is not None
    sealed = context.sealed_config_digests
    assert {"models", "armarium-formats", "run-policy", "decoding", "recovery"} <= set(sealed)
    assert context.recovery_policy["config_sha256"] == sealed["recovery"]
    with pytest.raises(ContractError, match="attestatores asked its context for fixture"):
        _ = context.fixture

    acts = expected_acts(context)
    assert [row["act_id"] for row in acts] == [act]
    assert acts[0]["outcome"] == "proposed"


def test_altered_raw_bounds_refuse_by_name_and_never_mention_the_fixture(real_root):
    designator = _Designator(real_root)
    bounds = designator.rectangle(1)
    designator.propose(1, bounds, raw_bounds={**bounds, "w": bounds["w"] + 1})
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="does not verify against the proposal class"):
        expected_acts(context)
    # And the refusal is the real-mode one, not the fixture floor's.
    with pytest.raises(FatalAccounting) as refusal:
        expected_acts(context)
    assert "beyond the fixture" not in str(refusal.value)


def test_a_row_with_no_designator_evidence_at_all_is_refused(real_root):
    designator = _Designator(real_root)
    designator.propose(1, designator.rectangle(1))
    unevidenced = designator.unevidenced_row(2)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match=f"act {unevidenced} has no Designator evidence"):
        expected_acts(context)


def test_a_row_with_both_a_hold_and_a_page_fallback_record_is_refused_as_ambiguous(real_root):
    """Class is decided by which evidence exists; two kinds of evidence is no class.

    Nothing tries residual, then page-fallback, until one verifies -- that would
    be a picker over the producer's own records (hard rule 8).
    """
    designator = _Designator(real_root)
    designator.propose(1, designator.rectangle(1))
    residual = designator.hold_residual(2, {"x": 1, "y": 1, "w": 1, "h": 1})
    designator.fallback_record(residual, 2)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="matches more than one act class") as refusal:
        expected_acts(context)
    assert "residual, page-fallback" in str(refusal.value)


def test_a_page_fallback_act_with_its_crop_regions_is_one_class_not_two(real_root):
    """A fallback act's predetermined crops are proposal regions of the same act.

    The `page-fallback` record decides the class; the regions beside it are its
    consequence, not a second claim. So the row reaches the fallback verifier --
    which here refuses on the premise it cannot find, naming the class -- and is
    never called ambiguous.
    """
    designator = _Designator(real_root)
    rectangle = designator.page_rectangle(2)
    fallback = derive_act_id(designator.pages[2]["subject_id"], "page-fallback", rectangle)
    designator.propose(2, designator.rectangle(2), act=fallback, key="page-fallback:2")
    designator.fallback_record(fallback, 2)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting) as refusal:
        expected_acts(context)
    assert "page-fallback" in str(refusal.value)
    assert "more than one act class" not in str(refusal.value)
    assert "no Designator evidence" not in str(refusal.value)


def test_a_structural_row_naming_the_wrong_page_ordinal_is_refused_by_name(real_root):
    """Act identity binds page, class and bounds -- never page_ordinal or act_key.

    A row free to disagree with its own region on either field would still
    verify: stages 3-7 index pages and join continuations by `page_ordinal`,
    not by identity, so a believed mismatch reaches them silently.
    """
    designator = _Designator(real_root)
    act = designator.propose(1, designator.rectangle(1))
    designator.rows[-1]["page_ordinal"] = 2
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="page_ordinal") as refusal:
        expected_acts(context)
    assert act in str(refusal.value)


def test_a_structural_row_naming_a_foreign_act_key_is_refused_by_name(real_root):
    designator = _Designator(real_root)
    act = designator.propose(1, designator.rectangle(1))
    designator.rows[-1]["act_key"] = "some-other-key"
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="act_key") as refusal:
        expected_acts(context)
    assert act in str(refusal.value)


def test_a_structural_row_claiming_a_continuation_with_no_far_page_region_is_refused(real_root):
    """`has_continuation` is a belief too, and this is the harmless direction: a
    row claims a page that was never cut."""
    designator = _Designator(real_root)
    designator.propose(1, designator.rectangle(1))
    designator.rows[-1]["has_continuation"] = True
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="has_continuation"):
        expected_acts(context)


def test_a_structural_row_denying_a_continuation_the_designator_actually_cut_is_refused(real_root):
    """The silent-loss direction: a published far-page crop the flag denies.

    The Attestatores append the far page only when `has_continuation` is set
    (3_attestatores/run.py), so a `False` flag beside a real far-page region
    would drop that crop before any witness ever saw it.
    """
    designator = _Designator(real_root)
    act = designator.propose(1, designator.rectangle(1))
    key = designator.rows[-1]["act_key"]
    far_region = designator.propose_far_page_region(act, key, 2, designator.rectangle(2))
    designator.rows[-1]["evidence"].append(designator.context.input_ref(far_region.relative_path))
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="has_continuation"):
        expected_acts(context)


def test_a_far_page_region_the_denominator_cannot_place_is_refused_not_dropped(real_root):
    """A region with no transform object names no page, and must not vanish.

    `has_continuation` is reconciled against the far-page regions the Designator
    actually cut. A region whose `transform` is not an object belongs to neither
    page list, so filtering it out silently would leave a `False` flag agreeing
    with an empty far-page count while a continuation crop sat published beside
    it -- and the Attestatores append the far page only when the flag is set.
    """
    designator = _Designator(real_root)
    act = designator.propose(1, designator.rectangle(1))
    key = designator.rows[-1]["act_key"]
    far_region = designator.propose_far_page_region(
        act, key, 2, designator.rectangle(2), transform=None
    )
    designator.rows[-1]["evidence"].append(designator.context.input_ref(far_region.relative_path))
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="carries no transform object") as refusal:
        expected_acts(context)
    assert act in str(refusal.value)


def _foreign_page_id() -> str:
    """A well-formed page identity for bytes no run of this submission carries."""
    return derive_page_id(
        {"kind": "source", "sha256": digest_bytes(b"a page this submission never held")},
        {"operation": "whole"},
    )


def test_a_far_page_region_naming_a_page_this_run_never_published_is_refused(real_root):
    """ "Far" must mean another page *of this run*, not merely "not the row's".

    The far-page list was everything the row's own page id did not match, so a
    transform naming a page id from nowhere satisfied `has_continuation=True`
    with a crop no downstream reader could open: the Attestatores would append
    a page this run's Exemplar never sealed. The region's page is now checked
    against the run's own page index, which is what makes the far list a list
    of real pages rather than a list of mismatches.
    """
    designator = _Designator(real_root)
    act = designator.propose(1, designator.rectangle(1))
    key = designator.rows[-1]["act_key"]
    bounds = designator.rectangle(2)
    far_region = designator.propose_far_page_region(
        act,
        key,
        2,
        bounds,
        transform={
            "operation": "crop",
            "source_page_ordinal": 2,
            "source_page_id": _foreign_page_id(),
            "bounds": bounds,
        },
    )
    designator.rows[-1]["evidence"].append(designator.context.input_ref(far_region.relative_path))
    designator.rows[-1]["has_continuation"] = True
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="which this run's Exemplar never published") as ref:
        expected_acts(context)
    assert act in str(ref.value)


def test_a_continuation_region_naming_a_foreign_act_key_is_refused(real_root):
    """The far region's recomputable facts are recomputed, not only counted.

    `act_key` is the field stages 3-7 join on, and the far region publishes its
    own copy exactly as the near one does; nothing before this compared them,
    so a continuation crop could be appended to an act under a key naming a
    different unit entirely.
    """
    designator = _Designator(real_root)
    act = designator.propose(1, designator.rectangle(1))
    far_region = designator.propose_far_page_region(
        act, "structural:9:9", 2, designator.rectangle(2)
    )
    designator.rows[-1]["evidence"].append(designator.context.input_ref(far_region.relative_path))
    designator.rows[-1]["has_continuation"] = True
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="continuation region names act_key"):
        expected_acts(context)


def test_a_continuation_region_naming_the_wrong_page_ordinal_is_refused(real_root):
    """And the far ordinal, against the run's page index rather than the row.

    No seal-row field names the far page, so the row cannot be the authority
    here; the Exemplar's own index is. A continuation whose ordinal disagrees
    with it would place the crop on the wrong page in every reader that orders
    by ordinal.
    """
    designator = _Designator(real_root)
    act = designator.propose(1, designator.rectangle(1))
    key = designator.rows[-1]["act_key"]
    bounds = designator.rectangle(2)
    far_region = designator.propose_far_page_region(
        act,
        key,
        2,
        bounds,
        transform={
            "operation": "crop",
            "source_page_ordinal": 7,
            "source_page_id": designator.pages[2]["subject_id"],
            "bounds": bounds,
        },
    )
    designator.rows[-1]["evidence"].append(designator.context.input_ref(far_region.relative_path))
    designator.rows[-1]["has_continuation"] = True
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="names source_page_ordinal 7"):
        expected_acts(context)


def test_an_act_whose_hold_names_no_rectangle_is_refused_not_read_as_structural(real_root):
    """A hold matching no minted class must not be reclassified away.

    A hold naming neither `residual_bounds` nor `page_bounds` matched no minted
    class, so a `held` act carrying one beside a proposal region fell through
    to the structural pass, was recomputed as a *proposal* against that region,
    and passed -- its hold never examined, and its held-ness never reconciled,
    on the one route where the hold is the only evidence the act was held at
    all. The evidence check downstream cannot catch it either: one hold is
    exactly what a `held` row is supposed to carry.
    """
    designator = _Designator(real_root)
    act = designator.propose(1, designator.rectangle(1))
    designator.hold_beside(act, 1)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="naming neither residual_bounds nor page_bounds"):
        expected_acts(context)


def test_two_page_fallback_records_for_one_act_are_refused_not_silently_last_wins(real_root):
    """The duplicate rule the hold check already applies, applied to every kind.

    `_designator_records_by_subject` built its index by comprehension, so two
    records for one subject left whichever the manifest visited last -- an act
    verified against a rectangle chosen by artifact-hash ordering, while the
    hold rule beside it refuses the same duplication by name. The helper is
    called directly here because every caller reads its result for a different
    purpose and would refuse for its own reason first; what is under test is
    the index, not any one consumer of it.
    """
    designator = _Designator(real_root)
    page_id = designator.pages[1]["subject_id"]
    act = derive_act_id(page_id, "page-fallback", designator.page_rectangle(1))
    designator.fallback_record(act, 1)
    designator.fallback_record(act, 1, attempt=attempt_id(act, "page-fallback", 2))
    designator.propose(1, designator.rectangle(1))
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    entries = context.tree.build_manifest(DESIGNATOR)["artifacts"]
    assert len([entry for entry in entries if entry["kind"] == "page-fallback"]) == 2

    with pytest.raises(FatalAccounting, match="more than one Designator page-fallback record"):
        _designator_records_by_subject(context, "page-fallback")


def test_a_malformed_real_minted_row_never_mentions_the_fixture(real_root):
    """The fixture-worded refusal is fixture-only wording, confined to fixture mode.

    A residual hold sealed as `proposed` -- malformed in a way real ingress can
    produce, since there is no fixture floor to have caught it first -- must
    still refuse, but never with the sentence a real run never had a fixture
    to be measured against.
    """
    designator = _Designator(real_root)
    designator.propose(1, designator.rectangle(1))
    designator.hold_residual(2, {"x": 1, "y": 1, "w": 1, "h": 1})
    designator.rows[-1]["outcome"] = "proposed"
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting) as refusal:
        expected_acts(context)
    assert "beyond the fixture" not in str(refusal.value)
    assert "beyond the structural pass" in str(refusal.value)


# --- the binding recheck ----------------------------------------------------------


def _moved_models_config(tmp_path: Path) -> Path:
    """A roster whose only movement is one chair's record, not its membership."""
    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    live = MODELS_CONFIG.read_text(encoding="utf-8")
    note = 'license_note = "fixture identity only; no model weights or model license apply"'
    assert note in live
    moved = live.replace(note, 'license_note = "a moved chair record"', 1)
    path = config_root / "models.toml"
    path.write_text(moved, encoding="utf-8")
    return path


def _appended(tmp_path: Path, source: Path) -> Path:
    copy = tmp_path / source.name
    copy.write_bytes(source.read_bytes() + b"\n# one byte the run never sealed\n")
    return copy


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
def test_a_moved_input_is_refused_by_name_before_the_seal_check_and_writes_nothing(
    real_root, tmp_path, flag, value, named
):
    """The leg that proves the constructor, not only the seal check.

    Opened for the Attestatores on a tree with no Designator seal at all: a
    refusal that reached the predecessor check would be `SchemaRefusal`; the
    binding refusal is `IncompatibleReuse`, names the policy that moved, and
    leaves every byte where it was.
    """
    before = _snapshot(real_root)

    with pytest.raises(IncompatibleReuse) as refusal:
        _open(real_root, ATTESTATORES, flag, str(value(tmp_path)))

    message = str(refusal.value)
    assert f"sealed configuration {named} moved" in message, message
    assert message.endswith(
        "No stage work was written. Resume with the original sealed inputs, or start a "
        "new run for the changed inputs"
    )
    assert "no stage-seal" not in message
    assert _snapshot(real_root) == before


def test_an_unmoved_input_reaches_the_seal_refusal_not_the_binding_one(real_root):
    """The other half of the ordering claim: with nothing moved, the seal is next."""
    with pytest.raises(SchemaRefusal, match="predecessor designator has no stage-seal"):
        _open(real_root, ATTESTATORES)


def test_a_run_sealed_before_the_real_only_names_existed_cannot_be_resumed(real_root):
    """An absent name is named apart from a moved one; it needs a different repair."""
    tree = RunTree(real_root, RUN_ID)
    path = tree.resolve("run.json")
    run = json.loads(path.read_text(encoding="utf-8"))
    del run["sealed_config_digests"]["models"]
    run["self_hash"] = self_hash(run)
    path.write_bytes(canonical_bytes(run))

    with pytest.raises(IncompatibleReuse) as refusal:
        _open(real_root, INK_MAP)

    assert "sealed no digest for the models configuration" in str(refusal.value)
    assert "moved" not in str(refusal.value)


def test_a_run_with_reversed_witness_chairs_is_refused_by_name(real_root):
    """The roster-membership leg: `witness_chairs` compared, not merely present."""
    tree = RunTree(real_root, RUN_ID)
    path = tree.resolve("run.json")
    run = json.loads(path.read_text(encoding="utf-8"))
    chairs = run["witness_chairs"]
    reversed_chairs = list(reversed(chairs))
    assert reversed_chairs != chairs, "witness_chairs must have more than one distinct entry"
    run["witness_chairs"] = reversed_chairs
    run["self_hash"] = self_hash(run)
    path.write_bytes(canonical_bytes(run))

    with pytest.raises(IncompatibleReuse, match="witness_chairs"):
        _open(real_root, INK_MAP)


def test_a_run_with_a_moved_door_adapter_recipe_is_refused_by_name(real_root):
    """The adapter-recipe leg, which is what binds `REAL_DOOR_ADAPTER_REVISION`."""
    tree = RunTree(real_root, RUN_ID)
    path = tree.resolve("run.json")
    run = json.loads(path.read_text(encoding="utf-8"))
    assert run["adapter_recipes"]["door"] != "exemplar-door-v4"
    run["adapter_recipes"] = {**run["adapter_recipes"], "door": "exemplar-door-v4"}
    run["self_hash"] = self_hash(run)
    path.write_bytes(canonical_bytes(run))

    with pytest.raises(IncompatibleReuse, match="adapter_recipes"):
        _open(real_root, INK_MAP)


def test_a_run_sealed_with_no_data_handling_digest_is_refused_by_name(real_root):
    """The presence leg: `data-handling` is real-only and checked apart from the
    digest-map comparison the other sealed names go through."""
    tree = RunTree(real_root, RUN_ID)
    path = tree.resolve("run.json")
    run = json.loads(path.read_text(encoding="utf-8"))
    assert "data-handling" in run["sealed_config_digests"]
    del run["sealed_config_digests"]["data-handling"]
    run["self_hash"] = self_hash(run)
    path.write_bytes(canonical_bytes(run))

    with pytest.raises(IncompatibleReuse) as refusal:
        _open(real_root, INK_MAP)

    assert "sealed no digest for the data-handling configuration" in str(refusal.value)


def test_run_policy_digest_moves_with_each_of_its_seven_fields():
    base = dict(
        witness_context="named",
        witness_context_declaration_sha256="a" * 64,
        nuda_per_mille=0,
        nuda_approval_ref="",
        perlector_instrument_per_mille=0,
        perlector_instrument_approval_ref="",
        draft_fed=True,
    )
    moved = {
        "witness_context": "blinded",
        "witness_context_declaration_sha256": "b" * 64,
        "nuda_per_mille": 1,
        "nuda_approval_ref": "lectio-nuda-sampling-design.v1",
        "perlector_instrument_per_mille": 1,
        "perlector_instrument_approval_ref": "perlector-prior-draft-instrument-design.v1",
        "draft_fed": False,
    }
    assert real_run_policy_digest(**base) == real_run_policy_digest(**base)
    for field, value in moved.items():
        assert real_run_policy_digest(**{**base, field: value}) != real_run_policy_digest(**base), (
            field
        )
    with pytest.raises(ContractError, match="draft_fed must be a bool"):
        real_run_policy_digest(**{**base, "draft_fed": 1})


# --- one page index for both routes ----------------------------------------------


def test_exemplar_page_ids_equals_the_fixture_declaration_on_the_happy_run(fixture_template):
    """No byte moves: the index says exactly what `page_identity` said.

    Opened through `open_stage_context`, so this is also the synthetic branch of
    the constructor -- `open_context` handed the tree and authority it read.
    """
    context = open_stage_context(_args(fixture_template, FIXTURE_RUN_ID), INK_MAP)
    fixture = load_fixture(str(ROOT / "proof"))
    happy_pages = [
        page for page in fixture["page"] if "scenarios" not in page or "happy" in page["scenarios"]
    ]

    assert exemplar_page_ids(context) == {
        page["ordinal"]: page_identity(fixture, page["ordinal"]) for page in happy_pages
    }
    assert context.fixture == fixture
    assert context.scenario == "happy"
    assert submission_identity(context.run) is None


def test_exemplar_page_ids_on_a_real_run_derive_from_the_sealed_bytes(real_root, real_template):
    _template, ledger = real_template
    context = _open(real_root, INK_MAP)

    assert exemplar_page_ids(context) == {
        ordinal: derive_page_id(
            {"kind": "source", "sha256": digest_bytes((FIXTURE_PAGES / name).read_bytes())},
            {"operation": "whole"},
        )
        for ordinal, name in ((1, "page-1.png"), (2, "page-2.png"))
    }
    assert submission_identity(context.run) == json.loads(ledger.read_text())["self_hash"]


# --- submission_identity refuses a forged or absent filename ledger --------------


def test_submission_identity_refuses_a_real_run_with_no_source_manifest():
    """No `source_manifest` at all: nothing to name a submission by."""
    run = {"ingress": real_ingress_record()}
    with pytest.raises(ContractError, match="no submitted source manifest to name a submission by"):
        submission_identity(run)


def test_submission_identity_refuses_a_real_run_naming_two_filename_ledgers():
    """Two source rows disagreeing on `ledger_sha256`: no single identity to choose."""
    run = {
        "ingress": real_ingress_record(),
        "source_manifest": [{"ledger_sha256": "a" * 64}, {"ledger_sha256": "b" * 64}],
    }
    with pytest.raises(ContractError, match="filename ledgers, not one"):
        submission_identity(run)


def test_submission_identity_refuses_a_real_run_with_a_non_sha256_ledger():
    """A `ledger_sha256` that is not a sha256 hex string: nothing may stand in for it."""
    run = {
        "ingress": real_ingress_record(),
        "source_manifest": [{"ledger_sha256": "not-a-sha256"}],
    }
    with pytest.raises(ContractError, match="no filename-ledger sha256"):
        submission_identity(run)


def test_open_context_takes_the_tree_and_its_authority_together(fixture_template):
    tree = RunTree(fixture_template, FIXTURE_RUN_ID)
    with pytest.raises(ContractError, match="together or neither"):
        open_context(_args(fixture_template, FIXTURE_RUN_ID), INK_MAP, tree=tree)
