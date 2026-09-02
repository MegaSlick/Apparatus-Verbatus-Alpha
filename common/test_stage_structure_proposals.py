"""Unit tests: the consumer's side of a served structure chair's proposals.

Three things are under test, and all three live in `common/stage.py` because
they are what every stage after the Designator has to be able to check for
itself:

- `_verify_proposal_act_row` — a structural act recomputed from the rectangle
  its own region record was cut over, and then followed through the
  digest-checked hop to the retained `structure-answer` record, which must list
  that rectangle on that page at that page ordinal;
- the `expected_acts` branch that takes that route because the *seal* records a
  served structure chair, not because the run happened to arrive through real
  ingress;
- `validate_serving_provenance`'s structure-chair branch — `engine_call` is
  owned by one producer (the Designator) and one chair
  (`designator_structure`), carries a closed schema, and binds the sealed
  `[structure]` decoding policy digest this run actually sealed.

**How the tree is built.** The run tree is real to the Ink Map's seal: the Door,
the Exemplar and the Ink Map run as programs over a genuine real submission made
of the synthetic fixture's own two pages, exactly as
`common/test_stage_real_ingress.py` builds it. The Designator's records are then
**hand-built**, because the live structural pass is D4's work and this unit is
the contract that pass must meet before it is written. Nothing here invokes a
Designator, and nothing here serves a model: the serving receipt is a *declared*
moment (`fixture_serving_details`), since nothing was served. That is honest for
what is under test — the verifier checks the binding between a seal, a receipt,
a page's status and the answer a chair returned, and it never claims to check
that an endpoint existed. Endpoint and start moment are receipt-only fields
(GOVERNANCE 6) and no stage artifact carries them.

**The mutation-checked happy path.** One builder produces the honest tree; every
refusal test below is that same builder with exactly one element changed, so a
check that stopped being load-bearing would show up as a test that passes
without it. Each mutation is refused by its own sentence, never by a shared
"malformed" catch-all — a reviewer holding only the refusal has to be able to
tell a forged rectangle from a forged hop.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from common.chairs import ChairIdentity, ChairRegistry
from common.contracts.canonical import self_hash
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal
from common.contracts.identities import act_id as derive_act_id
from common.contracts.identities import attempt_id
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR
from common.imaging import dimensions
from common.runtree.store import RunTree
from common.stage import (
    DESIGNATOR_CHAIR,
    REAL_SCENARIO,
    STRUCTURE_ANSWER_KIND,
    STRUCTURE_ANSWER_PARSED,
    STRUCTURE_ANSWER_RECORD_SCHEMA,
    STRUCTURE_CALL_KIND,
    STRUCTURE_CALL_SCHEMA,
    STRUCTURE_DECODING_POLICY,
    StageContext,
    adapter_recipe_for,
    expected_acts,
    fixture_serving_details,
    open_stage_context,
    run_sealed_config_digests,
    stage_parser,
    validate_serving_provenance,
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
RUN_ID = "structure-proposal-unit"
FIXTURE_RUN_ID = "structure-proposal-fixture-unit"


def _run_program(program: Path, *argv: str) -> None:
    result = subprocess.run(
        [sys.executable, str(program), *argv], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, f"{program.name}: {result.stderr}"


@pytest.fixture(scope="module")
def real_template(tmp_path_factory) -> Path:
    """One real submission, carried by the real programs to the Ink Map's seal."""
    base = tmp_path_factory.mktemp("structure-proposal-template")
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
    return root


@pytest.fixture
def real_root(real_template, tmp_path) -> Path:
    """A private copy of the real run, so each test may publish into it."""
    root = tmp_path / "runs"
    shutil.copytree(real_template, root)
    return root


@pytest.fixture(scope="module")
def fixture_template(tmp_path_factory) -> Path:
    """The happy synthetic fixture run, Door and Exemplar only.

    The counterpart tree for the one question ingress cannot answer: whether the
    recomputing route is opened by where the pages came from or by what the seal
    says produced it.
    """
    root = tmp_path_factory.mktemp("structure-proposal-fixture-template") / "runs"
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


@pytest.fixture
def fixture_root(fixture_template, tmp_path) -> Path:
    root = tmp_path / "fixture-runs"
    shutil.copytree(fixture_template, root)
    return root


def _args(root: Path, run_id: str):
    return stage_parser("structure proposal unit context").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            "happy",
            "--fixture-root",
            str(ROOT / "proof"),
            "--models-config",
            str(MODELS_CONFIG),
        ]
    )


def _open(root: Path, stage: str, run_id: str = RUN_ID) -> StageContext:
    return open_stage_context(_args(root, run_id), stage)


class _StructureDesignator:
    """A live-shaped Designator's records over an existing run tree, by hand.

    Publishes in the order the real pass must: the page's `structure-answer`
    first, then the `structure-status` that names it, then a proposal region per
    rectangle, then the seal. Every knob a refusal test needs is a keyword here
    rather than a second builder, so the honest tree and the mutated one differ
    by exactly the element under test.
    """

    def __init__(self, root: Path, run_id: str, *, scenario: str, fixture: Any):
        self.tree = RunTree(root, run_id)
        run = self.tree.read_run()
        self.registry = ChairRegistry.from_toml(MODELS_CONFIG)
        self.context = StageContext(
            tree=self.tree,
            run=run,
            fixture=fixture,
            scenario=scenario,
            stage=DESIGNATOR,
            adapter_revision=adapter_recipe_for(run, DESIGNATOR),
            args=None,
            registry=self.registry,
        )
        identity = self.registry.resolve(DESIGNATOR_CHAIR)
        assert isinstance(identity, ChairIdentity)
        self.identity = identity
        self.receipt = self.context.write_serving_receipt(
            identity, fixture_serving_details(identity)
        )
        self.decoding_sha256 = run_sealed_config_digests(run)["decoding"]
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

    # --- provenance -------------------------------------------------------

    def engine_call(self, **changes: Any) -> dict[str, Any]:
        call = {
            "schema": STRUCTURE_CALL_SCHEMA,
            "call_kind": STRUCTURE_CALL_KIND,
            "decoding_policy": STRUCTURE_DECODING_POLICY,
            "decoding_config_sha256": self.decoding_sha256,
        }
        call.update(changes)
        return call

    def provenance(
        self,
        *,
        identity: ChairIdentity | None = None,
        call: dict[str, Any] | None | str = "default",
    ) -> dict[str, Any]:
        resolved = self.identity if identity is None else identity
        receipt = (
            self.receipt
            if resolved is self.identity
            else self.context.write_serving_receipt(resolved, fixture_serving_details(resolved))
        )
        record: dict[str, Any] = {
            "chair": resolved.role,
            "chair_state": "configured",
            "resolved_identity": resolved.to_record(),
            "resolved_revision": {
                "kind": resolved.receipt_revision_kind,
                "value": resolved.receipt_revision,
            },
            "receipt_ref": receipt,
            "adapter_revision": self.context.adapter_revision,
        }
        if call != "default":
            if call is not None:
                record["engine_call"] = call
        else:
            record["engine_call"] = self.engine_call()
        return record

    # --- geometry ---------------------------------------------------------

    def page_size(self, ordinal: int) -> tuple[int, int]:
        return dimensions(self.tree.read_bytes(self.pages[ordinal]["payload"]["image_path"]))

    def rectangle(self, ordinal: int, index: int = 0) -> dict[str, int]:
        """A rectangle strictly inside the sealed page, distinct per index."""
        width, height = self.page_size(ordinal)
        return {
            "x": index,
            "y": index,
            "w": max(1, width // 2),
            "h": max(1, height // 3),
        }

    # --- the retained answer ---------------------------------------------

    def answer(
        self,
        ordinal: int,
        rectangles: list[dict[str, int]],
        *,
        provenance: dict[str, Any] | None = None,
        schema: str = STRUCTURE_ANSWER_RECORD_SCHEMA,
        parse_state: str = STRUCTURE_ANSWER_PARSED,
        parse_outcome: str | None = None,
        page_id: str | None = None,
        page_ordinal: int | None = None,
        act_count: int | None = None,
        call_record_ref: Any = "default",
    ) -> dict[str, str]:
        """One `structure-answer` record, text-free as SPEC_D §1.3 requires."""
        page = self.pages[ordinal]
        _, stored = self.tree.put_blob(
            DESIGNATOR, json.dumps({"call": "hand-built chair call record"}).encode("utf-8")
        )
        published = self.context.publish(
            kind=STRUCTURE_ANSWER_KIND,
            subject_id=page["subject_id"],
            outcome="proposed",
            inputs=[self.context.input_ref(page["payload"]["image_path"])],
            payload={
                "schema": schema,
                "page_id": page["subject_id"] if page_id is None else page_id,
                "page_ordinal": ordinal if page_ordinal is None else page_ordinal,
                "parse_state": parse_state,
                "parse_outcome": parse_outcome,
                "call_record_ref": (
                    self.context.input_ref(stored.relative_path)
                    if call_record_ref == "default"
                    else call_record_ref
                ),
                "act_count": len(rectangles) if act_count is None else act_count,
                "acts": [
                    {
                        "ordinal": index,
                        "raw_bounds": rectangle,
                        "text_length": 0,
                    }
                    for index, rectangle in enumerate(rectangles, start=1)
                ],
                "provenance": self.provenance() if provenance is None else provenance,
            },
        )
        return self.context.input_ref(published.relative_path)

    def status(
        self,
        ordinal: int,
        answer_ref: dict[str, str] | None,
        *,
        state: str = "scanned",
        page_id: str | None = None,
        page_ordinal: int | None = None,
    ) -> None:
        page = self.pages[ordinal]
        payload: dict[str, Any] = {
            "page_id": page["subject_id"] if page_id is None else page_id,
            "page_ordinal": ordinal if page_ordinal is None else page_ordinal,
            "state": state,
            "reason_code": None,
            "structure_evidence": "detected" if state == "scanned" else None,
        }
        if answer_ref is not None:
            payload["structure_answer_ref"] = answer_ref
        self.context.publish(
            kind="structure-status",
            subject_id=page["subject_id"],
            outcome="proposed" if state == "scanned" else "held",
            inputs=[self.context.input_ref(page["payload"]["image_path"])],
            payload=payload,
        )

    # --- the proposals ----------------------------------------------------

    def propose(self, ordinal: int, bounds: dict[str, int]) -> str:
        """One proposal-origin region, shaped as `cut_minted_region` publishes it."""
        page = self.pages[ordinal]
        page_id = page["subject_id"]
        act = derive_act_id(page_id, "proposal", bounds)
        key = f"proposal:{ordinal}:{len(self.rows) + 1}"
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
                "raw_bounds": bounds,
                "padding": None,
                "image_path": page["payload"]["image_path"],
                "image_sha256": page["payload"]["source_sha256"],
                "provenance": self.provenance(),
            },
        )
        self.rows.append(
            {
                "act_id": act,
                "act_key": key,
                "page_id": page_id,
                "page_ordinal": ordinal,
                "has_continuation": False,
                "outcome": "proposed",
                "evidence": [self.context.input_ref(published.relative_path)],
            }
        )
        return act

    def seal(self, *, provenance: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "expected_acts": self.rows,
            "count": len(self.rows),
            "provenance": self.provenance() if provenance is None else provenance,
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


def _real_designator(root: Path) -> _StructureDesignator:
    return _StructureDesignator(root, RUN_ID, scenario=REAL_SCENARIO, fixture=None)


def _honest_tree(root: Path) -> _StructureDesignator:
    """Two rectangles answered and minted on page 1, one on page 2."""
    designator = _real_designator(root)
    first = [designator.rectangle(1, 0), designator.rectangle(1, 1)]
    designator.status(1, designator.answer(1, first))
    for rectangle in first:
        designator.propose(1, rectangle)
    second = [designator.rectangle(2, 0)]
    designator.status(2, designator.answer(2, second))
    designator.propose(2, second[0])
    designator.seal()
    return designator


# --- the happy path -------------------------------------------------------


def test_a_served_structure_seal_verifies_every_row_against_its_own_answer(real_root):
    """The honest tree: three acts, each recomputed and each found in its answer."""
    designator = _honest_tree(real_root)
    context = _open(real_root, ATTESTATORES)

    acts = expected_acts(context)

    assert [row["act_id"] for row in acts] == [row["act_id"] for row in designator.rows]
    assert {row["outcome"] for row in acts} == {"proposed"}
    assert [row["page_ordinal"] for row in acts] == [1, 1, 2]


# --- the forged rectangle -------------------------------------------------


def test_a_rectangle_no_answer_lists_is_refused_and_names_the_rectangle(real_root):
    """The check the whole hop exists for.

    The act is internally perfect: its identity recomputes from its own
    `raw_bounds` and the proposal class, its act key and page ordinal agree with
    the region record, and the page was scanned. What it is not is a rectangle
    the structure chair ever returned — so without this refusal every downstream
    stage would witness, read and establish text over ink no model proposed.
    """
    designator = _real_designator(real_root)
    answered = designator.rectangle(1, 0)
    forged = designator.rectangle(1, 5)
    designator.status(1, designator.answer(1, [answered]))
    designator.propose(1, forged)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting) as refusal:
        expected_acts(context)
    assert "structure answer does not list at any ordinal" in str(refusal.value)
    assert str(forged["x"]) in str(refusal.value)


def test_a_duplicate_rectangle_mints_once_and_is_not_refused_for_being_listed_twice(real_root):
    """SPEC_D §2.2: two identical rectangles on one page are one crop.

    The class-and-bounds identity has no ordinal namespace, so the second
    occurrence is recorded as a `duplicate-rectangle` finding and mints nothing.
    A verifier that demanded a unique answer entry per act would turn that
    deliberate, recorded merge into a fatal accounting error.
    """
    designator = _real_designator(real_root)
    rectangle = designator.rectangle(1, 0)
    designator.status(1, designator.answer(1, [rectangle, rectangle]))
    designator.propose(1, rectangle)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    assert [row["act_id"] for row in expected_acts(context)] == [designator.rows[0]["act_id"]]


# --- the forged hop -------------------------------------------------------


def test_a_status_pointing_at_bytes_that_are_not_the_answers_is_refused(real_root):
    """The hop is digest-checked, so a reference is evidence and not an address."""
    designator = _real_designator(real_root)
    rectangle = designator.rectangle(1, 0)
    reference = designator.answer(1, [rectangle])
    designator.status(1, {**reference, "sha256": "0" * 64})
    designator.propose(1, rectangle)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(SchemaRefusal, match="bytes changed under a sealed reference"):
        expected_acts(context)


def test_a_scanned_page_naming_no_retained_answer_is_refused_by_name(real_root):
    designator = _real_designator(real_root)
    rectangle = designator.rectangle(1, 0)
    designator.answer(1, [rectangle])
    designator.status(1, None)
    designator.propose(1, rectangle)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="names no retained structure answer"):
        expected_acts(context)


def test_a_proposal_on_a_page_the_structure_pass_held_is_refused_by_name(real_root):
    designator = _real_designator(real_root)
    rectangle = designator.rectangle(1, 0)
    designator.status(1, designator.answer(1, [rectangle]), state="held")
    designator.propose(1, rectangle)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="the structure pass did not scan"):
        expected_acts(context)


def test_a_status_naming_a_different_page_ordinal_than_the_row_is_refused(real_root):
    designator = _real_designator(real_root)
    rectangle = designator.rectangle(1, 0)
    designator.status(1, designator.answer(1, [rectangle]), page_ordinal=9)
    designator.propose(1, rectangle)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="nor under an ordinal that page never had"):
        expected_acts(context)


# --- what the answer itself has to say ------------------------------------


def test_an_answer_that_did_not_parse_proposes_nothing(real_root):
    """A held page is terminal: SPEC_D §1.4 mints no act from a refused answer."""
    designator = _real_designator(real_root)
    rectangle = designator.rectangle(1, 0)
    designator.status(
        1,
        designator.answer(
            1, [rectangle], parse_state="refused", parse_outcome="unverified-response-schema"
        ),
    )
    designator.propose(1, rectangle)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting) as refusal:
        expected_acts(context)
    assert "a held page proposes nothing" in str(refusal.value)
    assert "unverified-response-schema" in str(refusal.value)


def test_an_answer_under_the_wrong_record_schema_is_refused_by_name(real_root):
    designator = _real_designator(real_root)
    rectangle = designator.rectangle(1, 0)
    designator.status(1, designator.answer(1, [rectangle], schema="structure-answer.v0"))
    designator.propose(1, rectangle)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="whose schema is 'structure-answer.v0'"):
        expected_acts(context)


def test_an_answer_naming_a_different_page_than_the_row_is_refused(real_root):
    designator = _real_designator(real_root)
    rectangle = designator.rectangle(1, 0)
    designator.status(
        1, designator.answer(1, [rectangle], page_id=designator.pages[2]["subject_id"])
    )
    designator.propose(1, rectangle)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="but the structure answer it rests on names page"):
        expected_acts(context)


def test_an_answer_whose_own_count_does_not_reconcile_is_refused(real_root):
    designator = _real_designator(real_root)
    rectangle = designator.rectangle(1, 0)
    designator.status(1, designator.answer(1, [rectangle], act_count=7))
    designator.propose(1, rectangle)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="own denominator does not reconcile"):
        expected_acts(context)


def test_a_parsed_answer_naming_no_call_record_is_refused(real_root):
    """A reading with no reading behind it."""
    designator = _real_designator(real_root)
    rectangle = designator.rectangle(1, 0)
    designator.status(1, designator.answer(1, [rectangle], call_record_ref=None))
    designator.propose(1, rectangle)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="names no usable call record"):
        expected_acts(context)


def test_an_answer_produced_under_a_different_call_than_the_seal_is_refused(real_root):
    """One run's seal and the answers its acts came from name one posture."""
    designator = _real_designator(real_root)
    rectangle = designator.rectangle(1, 0)
    other = designator.provenance(call=designator.engine_call(decoding_config_sha256="b" * 64))
    designator.status(1, designator.answer(1, [rectangle], provenance=other))
    designator.propose(1, rectangle)
    designator.seal()
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(FatalAccounting, match="a different structure-chair call"):
        expected_acts(context)


# --- the branch is the catalogue's, not the ingress record's ---------------


def test_a_fixture_ingress_run_with_a_served_seal_takes_the_recomputing_route(fixture_root):
    """The offline end-to-end case: a served structure chair over fixture pages.

    Ingress says "synthetic", and the fixture floor would demand that every act
    the sealed fixture declares appear in the seal. The seal says a chair was
    served, so the rows are checked against their own evidence and their own
    answer instead — and the rectangle this one was minted over is not in the
    answer, so the refusal that arrives is the structural one, never the floor's.
    """
    designator = _StructureDesignator(fixture_root, FIXTURE_RUN_ID, scenario="happy", fixture=None)
    designator.status(1, designator.answer(1, [designator.rectangle(1, 0)]))
    designator.propose(1, designator.rectangle(1, 5))
    designator.seal()
    context = _open(fixture_root, ATTESTATORES, run_id=FIXTURE_RUN_ID)

    with pytest.raises(FatalAccounting) as refusal:
        expected_acts(context)
    assert "structure answer does not list at any ordinal" in str(refusal.value)
    assert "synthetic act" not in str(refusal.value)


def test_the_same_fixture_ingress_seal_without_a_call_falls_to_the_fixture_floor(fixture_root):
    """The other half of the branch, so the discriminator is proven to be the seal.

    Byte-for-byte the same hand-built Designator, minus the `engine_call`. The
    refusal is now the synthetic floor's, which is the stronger check on this
    route: a seal that drops the field is measured against the fixture's own
    declaration rather than against its own evidence, so staying silent buys a
    forged seal nothing.
    """
    designator = _StructureDesignator(fixture_root, FIXTURE_RUN_ID, scenario="happy", fixture=None)
    silent = designator.provenance(call=None)
    designator.status(1, designator.answer(1, [designator.rectangle(1, 0)]))
    designator.propose(1, designator.rectangle(1, 5))
    designator.seal(provenance=silent)
    context = _open(fixture_root, ATTESTATORES, run_id=FIXTURE_RUN_ID)

    with pytest.raises(FatalAccounting) as refusal:
        expected_acts(context)
    assert "does not reconcile to every synthetic" in str(refusal.value)


# --- the provenance branch ------------------------------------------------


def test_a_seal_attributing_the_structural_call_to_a_witness_chair_is_refused(real_root):
    """No chair but the structure chair marks out structure (hard rule 8, GOVERNANCE 6)."""
    designator = _real_designator(real_root)
    witness = designator.registry.resolve("attestator_1")
    assert isinstance(witness, ChairIdentity)
    rectangle = designator.rectangle(1, 0)
    designator.status(1, designator.answer(1, [rectangle]))
    designator.propose(1, rectangle)
    designator.seal(provenance=designator.provenance(identity=witness))
    context = _open(real_root, ATTESTATORES)

    with pytest.raises(SchemaRefusal) as refusal:
        expected_acts(context)
    assert "carries a structure-chair engine call" in str(refusal.value)
    assert DESIGNATOR_CHAIR in str(refusal.value)


def test_only_the_designator_records_an_engine_call(real_root):
    """A Testimonium carrying a structure call, with the Attestatores' own recipe.

    The realistic shape of the mistake: the structure chair and Attestator 1 are
    both Chandra, so a witness record copying the structural pass's provenance
    is the fabricated serving moment this branch exists to refuse. The sealed
    adapter recipe is swapped to the Attestatores' deliberately, so the refusal
    that arrives is the ownership one and not the recipe check that sits above
    it -- otherwise this test would pass while the ownership rule did nothing.
    """
    designator = _real_designator(real_root)
    context = designator.context

    with pytest.raises(SchemaRefusal, match="only the Designator's structure pass"):
        validate_serving_provenance(
            context,
            {
                **designator.provenance(),
                "adapter_revision": adapter_recipe_for(context.run, ATTESTATORES),
            },
            producer_stage=ATTESTATORES,
            require_receipt=True,
        )


def test_a_chair_that_was_not_run_may_not_carry_a_call(real_root):
    designator = _real_designator(real_root)

    with pytest.raises(SchemaRefusal, match="recorded as not run"):
        validate_serving_provenance(
            designator.context,
            {**designator.provenance(), "receipt_ref": None},
            producer_stage=DESIGNATOR,
            require_receipt=False,
        )


def test_an_absent_chair_may_not_carry_a_call(real_root):
    designator = _real_designator(real_root)
    absent = designator.registry.resolve("secondary_proposer")

    with pytest.raises(SchemaRefusal, match="served nothing"):
        validate_serving_provenance(
            designator.context,
            {
                "chair": DESIGNATOR_CHAIR,
                "chair_state": "absent",
                "absence": absent.to_record(),
                "resolved_identity": None,
                "resolved_revision": None,
                "receipt_ref": None,
                "adapter_revision": designator.context.adapter_revision,
                "engine_call": designator.engine_call(),
            },
            producer_stage=DESIGNATOR,
            require_receipt=True,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema": "structure-chair-call.v0"}, "must declare schema"),
        ({"call_kind": "completions"}, "is served through 'chat-completions'"),
        ({"decoding_policy": "reading_of_record"}, "a posture it did not run under"),
        ({"decoding_config_sha256": "not-a-digest"}, "lowercase SHA-256"),
        ({"decoding_config_sha256": "0" * 64}, "decoding configuration changed"),
    ],
)
def test_the_engine_call_is_a_closed_record_bound_to_the_runs_sealed_decoding(
    real_root, changes, message
):
    """Each field is load-bearing, and each names its own refusal.

    `reading_of_record` is the interesting one: it is a perfectly valid sealed
    decoding section — the one every Attestator reads under — and naming it here
    would report a posture the structure pass did not run under, which is
    GOVERNANCE 10's confusion of a claim with a measurement rather than a
    malformed field.
    """
    designator = _real_designator(real_root)

    with pytest.raises(ContractError, match=message):
        validate_serving_provenance(
            designator.context,
            designator.provenance(call=designator.engine_call(**changes)),
            producer_stage=DESIGNATOR,
            require_receipt=True,
        )


@pytest.mark.parametrize("field", ["temperature", "seed"])
def test_the_engine_call_admits_no_field_beyond_its_closed_schema(real_root, field):
    """Including the temperature itself, deliberately.

    The sealed digest names the bytes; a number copied beside it could disagree
    with them, and then two artifacts in one run would say different things
    about one run's decoding.
    """
    designator = _real_designator(real_root)

    with pytest.raises(SchemaRefusal, match="carries exactly its schema"):
        validate_serving_provenance(
            designator.context,
            designator.provenance(call=designator.engine_call(**{field: 0})),
            producer_stage=DESIGNATOR,
            require_receipt=True,
        )


def test_provenance_without_a_call_is_unchanged_for_every_producer(real_root):
    """The fixture path is byte-for-byte what it was; nothing new is required."""
    designator = _real_designator(real_root)
    silent = designator.provenance(call=None)

    assert "engine_call" not in silent
    assert (
        validate_serving_provenance(
            designator.context, silent, producer_stage=DESIGNATOR, require_receipt=True
        )
        == designator.identity
    )
