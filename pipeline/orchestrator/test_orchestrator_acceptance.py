"""Spec 01's seven acceptance tests, driven over the real pipeline.

Meta-invariant #86, verbatim: "A fix proven only on a fixture is not proven."
Load-bearing tests drive REAL producers — real CLIs, real argv, real subprocesses —
over REAL sealed artifacts. Nothing here imports a stage and calls its main(); every
run below shells out exactly as the operator would, so a stage that only works when
imported would fail here rather than pass.

Meta-invariant #88: no test reports success over an empty population. Every loop
asserts an exact expected count.
"""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes
from common.contracts.envelope import validate_envelope, verify_input_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import (
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    PERLECTOR,
    RECENSOR,
)
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE = "synthetic-two-page-v0"


def orchestrate(run_root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    """Run the pipeline the way a person would, and return the whole result."""
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            scenario,
            "--run-id",
            run_id,
            "--run-root",
            str(run_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def export_of(tree: RunTree) -> dict:
    return tree.read_artifact(ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None))[
        "payload"
    ]


@pytest.fixture(scope="module")
def happy_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("happy")
    result = orchestrate(root, "r", "happy")
    assert result.returncode == 0, result.stderr
    return root, RunTree(root, "r")


@pytest.fixture(scope="module")
def review_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("review")
    result = orchestrate(root, "r", "review")
    # Exit 3 is "accounted, holdable" — the run reached honest terminal states and
    # one act is held. A zero here would be the vacuous green this project exists
    # to notice.
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r")


# --- 1. The happy path runs offline, and every reference resolves --------------


def test_the_happy_path_runs_and_establishes_both_acts(happy_run):
    _, tree = happy_run
    export = export_of(tree)
    assert export["aggregate"]["status"] == "complete"
    assert export["aggregate"]["reasons"] == []
    assert len(export["delivered"]) == 2
    assert export["review"] == []
    assert {item["category"] for item in export["delivered"]} == {"delivered"}


def test_every_input_reference_in_the_run_resolves_and_matches_its_digest(happy_run):
    """The whole traceability claim in one assertion: every artifact names the
    bytes it was derived from, and every one of those references is real."""
    _, tree = happy_run
    checked = 0
    for stage in (DOOR, EXEMPLAR, DESIGNATOR, ATTESTATORES, PERLECTOR, RECENSOR, ARCHETYPUS):
        for entry in tree.build_manifest(stage)["artifacts"]:
            record = tree.read_artifact(stage, entry["kind"], entry["artifact_id"])
            for reference in record["inputs"]:
                verify_input_bytes(reference, tree.read_bytes(reference["relative_path"]))
                checked += 1
    assert checked >= 20, f"only {checked} references checked; the run looks too thin"


def test_every_expected_act_has_exactly_one_terminal_category(happy_run):
    _, tree = happy_run
    export = export_of(tree)
    entries = [
        tree.read_artifact(ARMARIUM, "manifest-entry", entry["artifact_id"])
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "manifest-entry"
    ]
    assert len(entries) == export["expected_acts"] == 2
    assert len({entry["subject_id"] for entry in entries}) == 2


def test_the_seal_carries_an_outcome_and_a_derived_continuation_for_every_act(happy_run):
    """The seal entry is the handoff contract: every entry names its Designator
    outcome, and `has_continuation` reports the regions actually cut, never the
    declaration — a claim of a continuation nothing holds is how half an act gets
    delivered as the act."""
    _, tree = happy_run
    seal = tree.read_artifact(
        DESIGNATOR, "proposal-seal", artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)
    )["payload"]
    by_key = {entry["act_key"]: entry for entry in seal["expected_acts"]}
    assert by_key["a1"]["outcome"] == "proposed"
    assert by_key["a2"]["outcome"] == "proposed"
    assert by_key["a1"]["has_continuation"] is False
    assert by_key["a2"]["has_continuation"] is True


def test_the_run_used_no_network_and_no_model(happy_run):
    """The adapters are all fakes, declared as such. A run that had reached a real
    model would carry a resolved identity that was not a `fake-*` recipe."""
    _, tree = happy_run
    recipes = tree.read_run()["adapter_recipes"]
    assert len(recipes) == 8
    assert all(revision.startswith("fake-") for revision in recipes.values())


# --- 2. Repeating the identical command changes nothing ------------------------


def test_repeating_the_identical_command_leaves_every_byte_unchanged(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    before = snapshot(root)

    assert orchestrate(root, "r", "happy").returncode == 0
    after = snapshot(root)

    assert len(before) > 20
    assert after == before


def test_repeating_the_review_scenario_also_changes_nothing(tmp_path):
    """The scenario with a recovery loop in it is the one that can most easily
    append on every run — which it did, until the reading attempt stopped being a
    count of invocations."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "review").returncode == 3
    before = snapshot(root)

    assert orchestrate(root, "r", "review").returncode == 3
    assert snapshot(root) == before


# --- 3. An incompatible run id fails before writing ----------------------------


def test_reusing_a_run_id_with_a_changed_configuration_fails_before_writing(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    before = snapshot(root)

    # The scenario is part of the run's configuration digest, so the same run id
    # under a different scenario is a different run wearing an old name.
    result = orchestrate(root, "r", "review")

    assert result.returncode != 0
    assert "IncompatibleReuse" in result.stderr
    assert snapshot(root) == before, "a refused reuse must leave the tree untouched"


# --- 4. Resume reuses valid artifacts without rewriting them -------------------


def test_an_interrupted_run_resumes_without_rewriting_what_survived(tmp_path):
    """Interrupt for real: delete everything from the Perlector onward, as though
    the process died mid-run, then run the same command again."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    complete = snapshot(root)

    for stage_directory in ("4_perlector", "5_recensor", "6_archetypus", "7_armarium"):
        shutil.rmtree(root / "r" / stage_directory)
    survivors = snapshot(root)
    assert len(survivors) < len(complete)

    assert orchestrate(root, "r", "happy").returncode == 0
    resumed = snapshot(root)

    # Everything that survived is byte-identical: resume reused it rather than
    # redoing it. And the finished tree is identical to the uninterrupted one.
    for path, digest in survivors.items():
        assert resumed[path] == digest, f"{path} was rewritten on resume"
    assert resumed == complete


# --- 5. The review scenario preserves the whole history ------------------------


def artifacts(tree: RunTree, stage: str, kind: str) -> list[dict]:
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind
    ]


def test_the_recovered_act_keeps_one_identity_across_two_regions(review_run):
    """ARCHITECTURE invariant 1, driven end to end rather than unit-tested: act
    identity survives recropping, and the region identity does not."""
    _, tree = review_run
    regions = [
        record
        for record in artifacts(tree, DESIGNATOR, "region")
        if record["payload"]["act_key"] == "a1"
    ]
    assert len(regions) == 2
    assert len({record["subject_id"] for record in regions}) == 1
    assert len({record["payload"]["region_id"] for record in regions}) == 2
    assert {record["payload"]["origin"] for record in regions} == {"proposal", "recovery"}


def test_the_recovery_request_and_both_reading_attempts_survive(review_run):
    _, tree = review_run
    requests = artifacts(tree, RECENSOR, "recovery-request")
    assert len(requests) == 1
    assert requests[0]["payload"]["act_key"] == "a1"

    readings = [
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a1"
    ]
    assert sorted(record["payload"]["attempt_ordinal"] for record in readings) == [1, 2]


def test_both_recensor_outcomes_for_the_recovered_act_survive(review_run):
    """Nothing is lost inside a recovery loop: the request and the acceptance are
    both still there, in order."""
    _, tree = review_run
    reviews = [
        record
        for record in artifacts(tree, RECENSOR, "review")
        if record["payload"]["act_key"] == "a1"
    ]
    assert len(reviews) == 2
    by_ordinal = {record["payload"]["attempt_ordinal"]: record["outcome"] for record in reviews}
    assert by_ordinal == {1: "recovery-requested", 2: "accepted"}


def test_recovery_ink_is_recorded_as_witness_uncovered(review_run):
    """The recrop uncovered ink no witness ever saw. Saying so is the difference
    between a gap in the record and a gap nobody can see."""
    _, tree = review_run
    latest = max(
        (
            record
            for record in artifacts(tree, PERLECTOR, "perlectio")
            if record["payload"]["act_key"] == "a1"
        ),
        key=lambda record: record["payload"]["attempt_ordinal"],
    )
    coverage = [basis["witness_covered"] for basis in latest["payload"]["basis"]["regions"]]
    assert coverage == [True, False]


def test_the_cross_page_act_is_witnessed_on_both_sides_of_the_break(review_run):
    """A continuation is part of the original proposal, not a later attempt. A
    witness shown only the near side would have read half an act while the record
    said it read the act."""
    _, tree = review_run
    reading = next(
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a2"
    )
    regions = reading["payload"]["basis"]["regions"]
    assert len(regions) == 2
    assert all(basis["witness_covered"] for basis in regions)


def test_recovery_stayed_inside_its_budget(review_run):
    _, tree = review_run
    requests = artifacts(tree, RECENSOR, "recovery-request")
    assert len(requests) == 1
    assert requests[0]["payload"]["budget_allowed"] <= 3, "the absolute cap is a ruling"


# --- 6. The held act cannot look complete --------------------------------------


def test_the_held_act_has_no_archetypus_at_all(review_run):
    """The absence is the evidence. An export that showed a held act as delivered
    would have to invent a record that does not exist."""
    _, tree = review_run
    established = artifacts(tree, ARCHETYPUS, "archetypus")
    assert len(established) == 1
    assert established[0]["payload"]["act_key"] == "a1"


def test_the_held_act_appears_in_the_review_output_and_forces_partial(review_run):
    _, tree = review_run
    export = export_of(tree)
    assert export["aggregate"]["status"] == "partial"
    assert len(export["review"]) == 1
    assert export["review"][0]["act_key"] == "a2"
    assert export["review"][0]["category"] == "held-for-review"
    assert len(export["delivered"]) == 1
    assert "act a2 is held-for-review" in export["aggregate"]["reasons"]


def test_no_delivered_entry_carries_a_witness_reading_as_its_text(review_run):
    """GOALS 3: a witness reading is never itself an output. The established text
    must not equal any witness's reported words *by accident of the fixture*
    either, so the fixture deliberately makes two seats disagree."""
    _, tree = review_run
    export = export_of(tree)
    testimony = {
        record["payload"]["reported"]
        for record in artifacts(tree, ATTESTATORES, "testimonium")
        if record["outcome"] == "read" and record["payload"]["act_key"] == "a1"
    }
    delivered = export["delivered"][0]["text"]
    disagreeing = [reported for reported in testimony if reported != delivered]
    assert len(disagreeing) == 2, "the fixture must keep dissent exercisable"


def test_the_failed_seat_is_visible_in_the_export(review_run):
    """Sol B-2 / blocker 4, driven end to end: `failed` is a real member of the
    closed vocabulary and reaches the export as a named shortfall."""
    _, tree = review_run
    export = export_of(tree)
    held = export["review"][0]
    assert held["under_witnessed"] is True
    assert held["witness_coverage"]["by_outcome"]["failed"] == 1
    assert held["witness_coverage"]["by_class"] == {"completed": 2, "unresolved": 0, "failed": 1}
    assert any("under-witnessed" in reason for reason in export["aggregate"]["reasons"])


def test_a_delivered_act_still_links_back_to_the_exact_ink(review_run):
    _, tree = review_run
    delivered = export_of(tree)["delivered"][0]
    assert len(delivered["source_regions"]) == 2
    for region in delivered["source_regions"]:
        assert region["image_sha256"]
        assert region["region_id"].startswith("rgn_")
        assert tree.read_bytes(region["image_path"])


# --- 7. Every one of the seven handoffs refuses corruption ---------------------

# (producer, consumer, the artifact kind that crosses this boundary)
HANDOFF_ARTIFACTS = (
    (DOOR, EXEMPLAR, "admission"),
    (EXEMPLAR, DESIGNATOR, "page"),
    (DESIGNATOR, ATTESTATORES, "region"),
    (ATTESTATORES, PERLECTOR, "testimonium"),
    (PERLECTOR, RECENSOR, "perlectio"),
    (RECENSOR, ARCHETYPUS, "review"),
    (ARCHETYPUS, ARMARIUM, "archetypus"),
)


def one_artifact(tree: RunTree, stage: str, kind: str) -> tuple[Path, dict]:
    entries = [entry for entry in tree.build_manifest(stage)["artifacts"] if entry["kind"] == kind]
    assert entries, f"{stage} produced no {kind} to corrupt"
    entry = entries[0]
    return tree.resolve(entry["relative_path"]), tree.read_artifact(
        stage, kind, entry["artifact_id"]
    )


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_refuses_a_corrupted_schema(happy_run, producer, consumer, kind):
    _, tree = happy_run
    _, record = one_artifact(tree, producer, kind)
    record["schema"] = "skeleton.v99"
    with pytest.raises(SchemaRefusal):
        validate_envelope(record)


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_refuses_a_malformed_identity(happy_run, producer, consumer, kind):
    _, tree = happy_run
    _, record = one_artifact(tree, producer, kind)
    record["artifact_id"] = "art_not_a_real_identity"
    with pytest.raises(SchemaRefusal):
        validate_envelope(record)


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_refuses_duplicate_accounting(happy_run, producer, consumer, kind):
    """A duplicate reference is how one page gets counted twice and a conservation
    check passes over something nobody read."""
    _, tree = happy_run
    _, record = one_artifact(tree, producer, kind)
    assert record["inputs"], (
        f"{producer} {kind} references no input, so this boundary carries nothing "
        "verifiable. Skipping here would be a skip-list, which is how a gap goes "
        "unnoticed (#87) — the producer should name the bytes it acted on"
    )
    record["inputs"] = record["inputs"] + [dict(record["inputs"][0])]
    with pytest.raises(SchemaRefusal):
        validate_envelope(record)


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_refuses_bytes_that_changed_under_a_sealed_reference(
    happy_run, producer, consumer, kind
):
    _, tree = happy_run
    _, record = one_artifact(tree, producer, kind)
    assert record["inputs"], f"{producer} {kind} names no bytes to tamper with"
    reference = record["inputs"][0]
    with pytest.raises(SchemaRefusal):
        verify_input_bytes(reference, b"tampered")


@pytest.mark.full
def test_a_corrupted_artifact_on_disk_stops_the_stage_that_reads_it(tmp_path):
    """The refusals above are checked against the validator. This one proves a
    real consumer actually calls it: corrupt a sealed page on disk, rerun, and the
    pipeline must stop rather than carry on with what it can still parse.
    """
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0

    tree = RunTree(root, "r")
    path, record = one_artifact(tree, EXEMPLAR, "page")
    record["schema"] = "skeleton.v99"
    path.write_bytes(canonical_bytes(record))

    result = orchestrate(root, "r", "happy")
    assert result.returncode != 0
    assert "skeleton.v99" in result.stderr or "SchemaRefusal" in result.stderr


@pytest.mark.full
def test_every_handoff_in_the_contract_is_covered_by_this_table():
    """Meta-invariant #91 — a drift check over an agreement surface. If a handoff
    is added to the contracts and not to this table, the boundary test would
    silently cover six of seven."""
    from common.contracts.stages import HANDOFFS

    assert {(producer, consumer) for producer, consumer, _ in HANDOFF_ARTIFACTS} == set(HANDOFFS)
    assert len(HANDOFF_ARTIFACTS) == 7


def test_the_run_authority_is_never_rewritten_by_any_stage(happy_run):
    root, tree = happy_run
    stored = json.loads((root / "r" / "run.json").read_text(encoding="utf-8"))
    assert stored == tree.read_run()


def test_a_stage_invoked_before_its_producer_refuses_rather_than_inventing(tmp_path):
    """Order is not a convention here. A stage run out of sequence has nothing to
    read, and must say so instead of producing an empty success."""
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "2_designator" / "run.py"),
            "--run-root",
            str(tmp_path),
            "--run-id",
            "never-created",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "IncompatibleReuse" in result.stderr or "ContractError" in result.stderr


def test_contract_error_is_the_only_way_a_stage_reports_refusal():
    """A stage that crashed with a traceback and exited zero would be the vacuous
    green this project exists to notice."""
    assert issubclass(SchemaRefusal, ContractError)


# --- 8. A refused page cannot vanish, and no act rides out over one -------------
#
# The defect all four reviewers filed first: the Designator skipped any act whose
# page was not sealed, wrote nothing for it anywhere, and sealed a shorter
# expected-act list — so the one conservation check in the pipeline reconciled
# perfectly against a record of the loss's absence. A run that lost a whole page
# reported `status: complete, reasons: []`.


@pytest.fixture(scope="module")
def refused_page_run(tmp_path_factory):
    """Page 2 is refused at the door, so a2's continuation cannot be cut."""
    root = tmp_path_factory.mktemp("refused_page")
    result = orchestrate(root, "r", "refused-page")
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r")


@pytest.fixture(scope="module")
def refused_first_page_run(tmp_path_factory):
    """Page 1 — the page both acts live on — is refused at the door."""
    root = tmp_path_factory.mktemp("refused_first_page")
    result = orchestrate(root, "r", "refused-first-page")
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r")


def proposal_seal(tree: RunTree) -> dict:
    return tree.read_artifact(
        DESIGNATOR, "proposal-seal", artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)
    )["payload"]


def test_the_door_really_refused_page_two_through_its_own_inspection(refused_page_run):
    _, tree = refused_page_run
    refusals = [
        record for record in artifacts(tree, EXEMPLAR, "page") if record["outcome"] == "refused"
    ]
    assert len(refusals) == 1
    assert refusals[0]["payload"]["ordinal"] == 2
    assert "digest" in refusals[0]["payload"]["reason"]


def test_the_page_loss_is_named_and_the_run_is_partial(refused_page_run):
    _, tree = refused_page_run
    export = export_of(tree)
    assert export["aggregate"]["status"] == "partial"
    assert any(
        reason.startswith("page 2 was refused:") for reason in export["aggregate"]["reasons"]
    )
    assert export["aggregate"]["by_page_outcome"] == {"sealed": 1, "refused": 1}


def test_the_act_with_the_lost_continuation_is_held_not_delivered(refused_page_run):
    """`has_continuation` is derived from the regions actually cut, so it may not
    claim a continuation nothing holds — and the act whose far side is on the
    lost page is held rather than delivered as a complete reading of half its ink."""
    _, tree = refused_page_run
    seal = proposal_seal(tree)
    by_key = {entry["act_key"]: entry for entry in seal["expected_acts"]}
    assert set(by_key) == {"a1", "a2"}, "the seal must still name every declared act"
    assert by_key["a1"]["outcome"] == "proposed"
    assert by_key["a2"]["outcome"] == "held"
    assert by_key["a2"]["has_continuation"] is False

    export = export_of(tree)
    assert [item["act_key"] for item in export["delivered"]] == ["a1"]
    assert [item["act_key"] for item in export["review"]] == ["a2"]
    assert export["review"][0]["category"] == "held-for-review"


def test_the_hold_is_a_real_artifact_naming_the_lost_page(refused_page_run):
    _, tree = refused_page_run
    holds = artifacts(tree, DESIGNATOR, "hold")
    assert len(holds) == 1
    assert holds[0]["outcome"] == "held"
    assert holds[0]["payload"]["act_key"] == "a2"
    assert "page 2" in holds[0]["payload"]["reason"]
    assert holds[0]["inputs"], "the hold must reference the refusal it rests on"


def test_no_witness_and_no_reading_pretends_to_have_seen_the_held_act(refused_page_run):
    """The held act is not silently skipped: every configured seat records an
    explicit not-run, and the Perlector acknowledges the act without reading it —
    a reading of the near side alone would be a truncation delivered as an output."""
    _, tree = refused_page_run
    testimonia = [
        record
        for record in artifacts(tree, ATTESTATORES, "testimonium")
        if record["payload"]["act_key"] == "a2"
    ]
    assert len(testimonia) == 3
    assert {record["outcome"] for record in testimonia} == {"not-run"}

    readings = [
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a2"
    ]
    assert len(readings) == 1
    assert readings[0]["outcome"] == "not-run"
    assert "text" not in readings[0]["payload"]

    established = artifacts(tree, ARCHETYPUS, "archetypus")
    assert [record["payload"]["act_key"] for record in established] == ["a1"]


def test_the_refused_page_scenario_is_deterministic_on_rerun(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "refused-page").returncode == 3
    before = snapshot(root)
    assert orchestrate(root, "r", "refused-page").returncode == 3
    assert snapshot(root) == before


def test_losing_the_first_page_holds_every_act_and_delivers_nothing(refused_first_page_run):
    """Half one of the defect, driven end to end: an act whose own page was never
    sealed used to disappear from the seal entirely. Now it appears, held, with a
    hold artifact each, and the run is partial with the page loss named."""
    _, tree = refused_first_page_run
    seal = proposal_seal(tree)
    assert {entry["act_key"]: entry["outcome"] for entry in seal["expected_acts"]} == {
        "a1": "held",
        "a2": "held",
    }
    assert artifacts(tree, DESIGNATOR, "region") == [], (
        "no region may be cut for an act that cannot be fully marked out — an "
        "orphan continuation crop would be evidence of an act nothing accounts for"
    )
    assert len(artifacts(tree, DESIGNATOR, "hold")) == 2

    export = export_of(tree)
    assert export["aggregate"]["status"] == "partial"
    assert any(
        reason.startswith("page 1 was refused:") for reason in export["aggregate"]["reasons"]
    )
    assert export["delivered"] == []
    assert [item["category"] for item in export["review"]] == [
        "held-for-review",
        "held-for-review",
    ]
    entries = [
        entry
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "manifest-entry"
    ]
    assert len(entries) == 2, "conservation: every expected act still has exactly one category"


def test_the_recensor_refuses_a_continuation_claim_with_one_region(tmp_path):
    """Defence in depth for half two: if the seal claims a continuation and the
    tree holds only one proposal region — drift, tampering, or a future bug —
    the Recensor holds the act rather than accepting a half reading."""
    root = tmp_path / "runs"
    for name, program in (
        ("door", "pipeline/1_exemplar/door.py"),
        ("exemplar", "pipeline/1_exemplar/run.py"),
        ("designator", "pipeline/2_designator/run.py"),
        ("attestatores", "pipeline/3_attestatores/run.py"),
        ("perlector", "pipeline/4_perlector/run.py"),
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "r",
                "--scenario",
                "happy",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"

    tree = RunTree(root, "r")
    continuations = [
        entry
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region"
        and tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])["payload"]["transform"][
            "source_page_ordinal"
        ]
        == 2
    ]
    assert len(continuations) == 1
    tree.resolve(continuations[0]["relative_path"]).unlink()

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "5_recensor" / "run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, result.stderr

    reviews = [
        record
        for record in artifacts(tree, RECENSOR, "review")
        if record["payload"]["act_key"] == "a2"
    ]
    assert len(reviews) == 1
    assert reviews[0]["outcome"] == "held-for-review"
    assert "continuation" in reviews[0]["payload"]["reason"]
