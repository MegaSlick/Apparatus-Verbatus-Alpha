"""R0 falsification tests: the vertical slice exit criterion.

Written blind, from /out/R0_CONTRACT_NOTE.md (v2) before the R0 build chamber runs.
Every test here must fail RED on the chamber's base commit (main 176b09e) because the
feature it checks is not yet built, and must keep collecting cleanly regardless.

Exit criterion (R0_CONTRACT_NOTE.md, binding): "The fixture/walking-skeleton path
produces and consumes at least one page-scoped Testimonium and its derived act
attachment end-to-end through the real stage programs in both scenarios (`happy` and
`review` in proof/skeleton_fixture.toml)."

Drives the REAL orchestrator as a subprocess over the REAL synthetic fixture, exactly
as `pipeline/orchestrator/test_orchestrator_acceptance.py`'s own `orchestrate` helper
does -- meta-invariant #86 ("a fix proven only on a fixture is not proven") applies to
a falsification test as much as to an implementation one.

NEW test file only; nothing here modifies `proof/skeleton_fixture.toml`,
`proof/build_fixture.py`, or any existing test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from common.chairs.registry import ChairRegistry
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.stages import ATTESTATORES, PERLECTOR
from common.runtree.store import RECENSOR_PARTITION_RECEIPT_FILE, RunTree
from common.stage import act_by_key, act_identity, load_fixture, page_identity, run_config_bindings

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE_ROOT = ROOT / "proof"
FIXTURE = "synthetic-two-page-v0"

# R0_CONTRACT_NOTE.md kind table: "Fixture chairs attestator_1 and attestator_3 become
# page-witnesses (mirroring Chandra/Churro); real attestatores program writes them in
# both scenarios."
PAGE_WITNESS_CHAIRS = ("attestator_1", "attestator_3")

# D1: "attestator_2 stays act-scoped (mirrors DAI, an act-crop witness by design §2).
# The migration ADDS page scope; it does not replace act scope."
RETAINED_ACT_WITNESS_CHAIR = "attestator_2"


def orchestrate(run_root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    """Run the pipeline the way a person would, mirroring the acceptance suite's helper."""
    command = [
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
    ]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def invoke_stage(run_root: Path, run_id: str, scenario: str, program: str):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module", params=["happy", "review"])
def run_tree(tmp_path_factory, request):
    """One real end-to-end orchestrator run per fixture scenario, per the exit criterion."""
    scenario = request.param
    root = tmp_path_factory.mktemp(f"r0-vslice-{scenario}")
    result = orchestrate(root, "r", scenario)
    assert result.returncode in (0, 3), f"scenario {scenario!r}: {result.stderr}"
    return scenario, RunTree(root, "r")


@pytest.fixture(scope="module")
def fixture():
    return load_fixture(str(FIXTURE_ROOT))


def _attestatores_artifacts(tree: RunTree) -> list[dict]:
    manifest = tree.build_manifest(ATTESTATORES)
    return [
        tree.read_artifact(ATTESTATORES, entry["kind"], entry["artifact_id"])
        for entry in manifest["artifacts"]
    ]


def _latest_completed_reading(tree: RunTree, act_id: str) -> dict:
    """The highest-attempt-ordinal `read` Perlectio for one act, from real artifacts."""
    entries = [
        entry
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio" and entry["subject_id"] == act_id
    ]
    records = [
        tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"]) for entry in entries
    ]
    completed = [record for record in records if record["outcome"] == "read"]
    assert completed, f"act {act_id} has no completed Perlectio in this run's tree"
    return max(completed, key=lambda record: record["payload"]["attempt_ordinal"])


# --- 1. The vertical slice itself -----------------------------------------------


def test_page_scoped_testimonium_is_produced_while_act_scope_is_retained_for_attestator_2(
    run_tree, fixture
):
    """R0_CONTRACT_NOTE.md kind table + D1, in both scenarios.

    On the base commit every Attestatores Testimonium -- for all three configured
    chairs, including attestator_1 and attestator_3 -- has `subject_id == act_id`.
    Nothing writes a page-scoped Testimonium (`subject_id == page_id`) at all, so the
    first assertion below fails red for the exit criterion's own reason. The second
    assertion (attestator_2 stays act-scoped) is expected to keep passing through a
    correct build -- D1 says the migration ADDS page scope, it does not remove act
    scope from the chair that never had it.
    """
    scenario, tree = run_tree
    target_page_ids = {page_identity(fixture, ordinal) for ordinal in (1, 2)}
    target_act_ids = {act_identity(fixture, act_by_key(fixture, key)) for key in ("a1", "a2")}

    page_scoped_chairs_found: set[str] = set()
    act_2_subjects: set[str] = set()
    for record in _attestatores_artifacts(tree):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        chair = payload.get("chair")
        subject_id = record.get("subject_id")
        if chair in PAGE_WITNESS_CHAIRS and subject_id in target_page_ids:
            page_scoped_chairs_found.add(chair)
        if chair == RETAINED_ACT_WITNESS_CHAIR and record.get("kind") == "testimonium":
            act_2_subjects.add(subject_id)

    missing_page_witnesses = set(PAGE_WITNESS_CHAIRS) - page_scoped_chairs_found
    assert not missing_page_witnesses, (
        f"scenario {scenario!r}: no Attestatores artifact was found whose subject_id is a "
        f"page identity ({sorted(target_page_ids)}) for chair(s) {sorted(missing_page_witnesses)}; "
        "R0's kind table requires attestator_1 and attestator_3 to become page-witnesses, "
        "writing a page-scoped Testimonium (subject_id == page_id, never act_id) in both "
        "the happy and review scenarios"
    )
    assert act_2_subjects and act_2_subjects <= target_act_ids, (
        f"scenario {scenario!r}: attestator_2's testimonium subject_id(s) {act_2_subjects} are "
        f"not exactly among the expected act identities {target_act_ids}; D1 requires "
        "attestator_2 to stay act-scoped (kind='testimonium', subject_id == act_id) unchanged"
    )


def test_derived_act_attachment_record_is_written_by_attestatores_for_every_proposed_act(
    run_tree, fixture
):
    """D1: the derived act-attachment record is written by the Attestatores stage
    program, in the same stage invocation that writes page testimony -- so it must
    already be present in the ATTESTATORES manifest, not created by a later stage.
    """
    scenario, tree = run_tree
    expected_act_ids = {act_identity(fixture, act_by_key(fixture, key)) for key in ("a1", "a2")}
    attachment_act_ids = {
        record["subject_id"]
        for record in _attestatores_artifacts(tree)
        if record.get("kind") == "act-attachment"
    }
    missing = expected_act_ids - attachment_act_ids
    assert not missing, (
        f"scenario {scenario!r}: the Attestatores stage wrote no 'act-attachment' artifact "
        f"for act(s) {sorted(missing)} (found kinds "
        f"{sorted({record.get('kind') for record in _attestatores_artifacts(tree)})}); "
        "R0_CONTRACT_NOTE.md D1 requires a derived act-attachment record per act, written by "
        "the Attestatores stage program in the same invocation that writes page testimony"
    )


def test_dissent_names_a_page_witness_row_as_compared_unknown_with_a_named_reason(
    run_tree, fixture
):
    """D5: page-witness rows emit compared:"unknown" with a named reason (alignment
    views are R4's), per the existing is_comparable doctrine in
    `pipeline/4_perlector/dissent.py`.

    On the base commit attestator_1 and attestator_3 are ordinary act-scoped
    witnesses, so their dissent rows for act a1 compare normally
    (`compared: True`) rather than carrying D5's page-witness `unknown` reason --
    this fails red independently of whether page-scoped Testimonium exists yet.
    """
    scenario, tree = run_tree
    act_a1_id = act_identity(fixture, act_by_key(fixture, "a1"))
    reading = _latest_completed_reading(tree, act_a1_id)
    dissent_rows = reading["payload"].get("dissent", [])
    page_witness_rows = [row for row in dissent_rows if row.get("chair") in PAGE_WITNESS_CHAIRS]
    assert page_witness_rows, (
        f"scenario {scenario!r}: act a1's established reading carries no dissent row at all "
        f"for chair(s) {PAGE_WITNESS_CHAIRS}"
    )
    for row in page_witness_rows:
        reason = row.get("reason")
        assert row.get("compared") == "unknown" and isinstance(reason, str) and reason.strip(), (
            f"scenario {scenario!r}: dissent row for chair {row.get('chair')!r} is {row!r}; D5 "
            "requires a page-witness row to emit compared:'unknown' with a non-blank named "
            "reason, since act-anchored alignment views belong to R4"
        )


# --- 2. Corpus-frame binding -----------------------------------------------------


def test_run_creation_records_corpus_frame_membership(run_tree):
    """R0_CONTRACT_NOTE.md: "Bound at run creation: every orchestrator run records
    corpus-frame membership (frame digest + page digest + seed)."

    `common/runtree/store.py::RunTree._BOUND_FIELDS` is exactly
    `("source_manifest", "config_digest", "adapter_recipes", "witness_chairs")` on the
    base commit -- no frame/page/seed membership fact is recorded in `run.json` at all.
    """
    scenario, tree = run_tree
    run = tree.read_run()
    candidate_keys = ("corpus_frame", "corpus_frame_membership", "frame_membership")
    present = [key for key in candidate_keys if key in run]
    assert present, (
        f"scenario {scenario!r}: run.json carries none of {candidate_keys} (top-level keys: "
        f"{sorted(run)}); R0_CONTRACT_NOTE.md requires every orchestrator run to record "
        "corpus-frame membership (frame digest + page digest + seed) at run creation"
    )
    membership = run[present[0]]
    required_facts = {"frame_digest", "page_digest", "seed"}
    assert isinstance(membership, dict) and required_facts <= set(membership), (
        f"scenario {scenario!r}: run.json's {present[0]!r} is {membership!r}, which does not "
        f"carry all of {required_facts}"
    )


def test_shard_size_knob_is_sealed_with_a_point_of_use_recheck_entry():
    """R0_CONTRACT_NOTE.md: "shard size <=1,000 is R0's own sealed knob in
    config_digest with point-of-use recheck."

    Mirrors the existing `designator-padding` entry in
    `run_config_bindings(...)["sealed_config_digests"]`
    (`common/stage.py::StageContext.require_sealed_config` is the point-of-use
    recheck mechanism already built for that entry). On the base commit
    `sealed_config_digests` carries exactly one key, `designator-padding`; nothing
    names a shard-size knob at all.
    """
    fixture_data = load_fixture(str(FIXTURE_ROOT))
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    bindings = run_config_bindings(registry.config, fixture_data, "happy")
    sealed = bindings["sealed_config_digests"]
    shard_keys = [key for key in sealed if "shard" in key]
    assert shard_keys, (
        f"run_config_bindings()'s sealed_config_digests is {sorted(sealed)}, which names no "
        "shard-size entry; R0's shard-size knob must be sealed into config_digest with a "
        "point-of-use recheck, exactly as 'designator-padding' already is"
    )


# --- 3. Audit-and-repair regression tests (F-S1, F-S2) ---------------------------
#
# Sonnet audit-and-repair seat 1, R0. Both reproduced against the real orchestrator
# over the real fixture on the pre-fix candidate before being fixed -- not merely
# unit-level constructions -- per the s11-audit brief's "real fixture runs...  test
# first, then fix" rule.


def test_page_testimony_excludes_text_from_an_act_the_same_chair_failed(tmp_path, fixture):
    """F-S1: a page witness's joined page testimony must never fold in text from an
    act-scoped attempt the SAME chair recorded as `failed`.

    `malformed-capabilities` declares attestator_3's response to act a1 with a
    non-object `format_capabilities`; `prepared_response` cannot retain that
    self-report, so the whole attempt is `failed` -- but its `native_payload` is
    still the parsed string "SYNTHETIC ACT ONE alpha beta". Before this audit's
    fix, `publish_page_testimonia_and_attachments` filtered its page join on
    `isinstance(attempt.native_payload, str)` alone (never `attempt.outcome`), so
    this failed act's own text was folded into attestator_3's page-1 testimony,
    which reported `outcome: "read"` as though nothing had failed -- a recorded
    failure silently counted as page coverage (D2/D3; GOVERNANCE 2). Confirmed
    against the real run tree before the fix: the page-1 testimonium for
    attestator_3 carried the full two-act joined text including act a1's, while
    the act-scoped Testimonium for the same chair on act a1 was `outcome: "failed"`.
    """
    result = orchestrate(tmp_path, "r", "malformed-capabilities")
    assert result.returncode in (0, 3), result.stderr
    tree = RunTree(tmp_path, "r")

    act_a1_id = act_identity(fixture, act_by_key(fixture, "a1"))
    act_records = [
        record
        for record in _attestatores_artifacts(tree)
        if record.get("kind") == "testimonium"
        and record["subject_id"] == act_a1_id
        and record.get("payload", {}).get("chair") == "attestator_3"
    ]
    assert act_records, "no act-scoped Testimonium found for attestator_3 on act a1"
    act_record = act_records[0]
    assert act_record["outcome"] == "failed", (
        "fixture precondition: attestator_3's act-scoped attempt on a1 must be "
        f"'failed' for this test to exercise the leak; got {act_record['outcome']!r}"
    )
    failed_text = act_record["payload"]["payload"]
    assert isinstance(failed_text, str) and failed_text

    page_records = [
        record
        for record in _attestatores_artifacts(tree)
        if record.get("kind") == "page-testimonium"
        and record.get("payload", {}).get("chair") == "attestator_3"
        and record.get("payload", {}).get("page_ordinal") == 1
    ]
    assert page_records, "no page-testimonium found for attestator_3 on page 1"
    page_payload = page_records[0]["payload"]
    page_text = page_payload["payload"]
    assert failed_text not in (page_text or ""), (
        f"attestator_3's page-1 testimony {page_text!r} contains the text of its own "
        f"failed act-a1 attempt {failed_text!r}; a recorded failure must never be folded "
        "into a page witness's 'read' testimony (F-S1)"
    )
    unjoined = page_payload.get("unjoined_act_attempts")
    assert isinstance(unjoined, list) and len(unjoined) == 1, (
        "a page Testimonium whose synthetic join omitted a failed act must name "
        f"that omission; got {unjoined!r}"
    )
    assert unjoined[0]["act_id"] == act_a1_id
    assert unjoined[0]["act_key"] == "a1"
    assert unjoined[0]["outcome"] == "failed"
    assert isinstance(unjoined[0]["reason"], str) and unjoined[0]["reason"].strip()
    attachment_record = next(
        record
        for record in _attestatores_artifacts(tree)
        if record.get("kind") == "act-attachment" and record["subject_id"] == act_a1_id
    )
    failed_attachment = next(
        row for row in attachment_record["payload"]["attachments"] if row["chair"] == "attestator_3"
    )
    assert failed_attachment["attached"] is False
    assert failed_attachment["span"] is None, (
        "an unattached attempt has no alignment span; a zero-length or payload-length "
        f"span can be mistaken for real coverage, got {failed_attachment['span']!r}"
    )


def test_act_attachment_span_reflects_this_chairs_own_delivered_text_not_the_act_key(
    run_tree,
):
    """F-S2: an act-attachment's `span` must derive from the chair's own delivered
    reading, never from `len(act_key)` -- and it must not be mislabeled
    "fixture-declared" when the fixture declares no span at all.

    Before this audit's fix, every attachment entry's span was
    `{"start": 0, "end": len(act["act_key"])}`. The synthetic act keys are "a1"/"a2"
    (length 2 either way), so `span.end` was the constant 2 for every chair on
    every act regardless of how much text was actually delivered -- confirmed
    against the real 'happy' run tree, where content_health.characters varied
    (28, 34, 40) while every span.end was 2. That is not a "fixture-declared"
    span (nothing in `proof/skeleton_fixture.toml` declares one); it is a
    provenance claim nothing backed.
    """
    scenario, tree = run_tree
    attachment_records = [
        record for record in _attestatores_artifacts(tree) if record.get("kind") == "act-attachment"
    ]
    assert attachment_records, f"scenario {scenario!r}: no act-attachment records found"
    checked_attached_entries = 0
    for record in attachment_records:
        for entry in record["payload"]["attachments"]:
            if not entry.get("attached"):
                continue
            characters = entry.get("content_health", {}).get("characters")
            if not isinstance(characters, int):
                continue
            span = entry["span"]
            assert span == {"start": 0, "end": characters}, (
                f"scenario {scenario!r}: attachment entry for chair {entry.get('chair')!r} on "
                f"{record['subject_id']!r} has span {span!r}, but its own content_health reports "
                f"{characters} character(s) delivered; the span must reflect this chair's own "
                "delivered text (F-S2), not a value derived from the act key"
            )
            checked_attached_entries += 1
    assert checked_attached_entries, (
        f"scenario {scenario!r}: no attached entry had a character count to check"
    )


def test_perlector_consumes_the_page_testimonium_named_by_an_act_attachment(tmp_path):
    """The R0 exit criterion says page testimony is consumed, not merely written.

    Removing a page Testimonium after Attestatores has sealed an attachment to it
    must stop Perlector at that reference.  Before F-new-3, every downstream stage
    validated only the outer act-attachment; its nested ``testimonium_ref`` values
    were write-only, so Perlector returned success over evidence that no longer
    existed.
    """
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
    ):
        result = invoke_stage(root, "page-custody", "happy", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    tree = RunTree(root, "page-custody")
    page_entry = next(
        entry
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
    )
    page_path = tree.resolve(page_entry["relative_path"])
    moved = page_path.with_suffix(".json.moved")
    page_path.rename(moved)
    try:
        result = invoke_stage(root, "page-custody", "happy", "pipeline/4_perlector/run.py")
    finally:
        moved.rename(page_path)

    assert result.returncode != 0, (
        "Perlector returned success after a page Testimonium referenced by the "
        "act-attachment disappeared; R0's page-scoped evidence was written but "
        "never consumed"
    )
    assert "referenced artifact" in result.stderr and "could not be read" in result.stderr


def _through_attestatores(root: Path, run_id: str, scenario: str = "happy") -> RunTree:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
    ):
        result = invoke_stage(root, run_id, scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return RunTree(root, run_id)


def _reread(root: Path, run_id: str, scenario: str, act_id: str, chair: str):
    """One targeted Attestatores reread: the append-only second attempt path."""
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "3_attestatores" / "run.py"),
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
            "--operation",
            "reread",
            "--act",
            act_id,
            "--chair",
            chair,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _reseal(path: Path, record: dict) -> None:
    record["self_hash"] = self_hash(
        {key: value for key, value in record.items() if key != "self_hash"}
    )
    path.write_bytes(canonical_bytes(record))


def test_perlector_refuses_an_attachment_for_an_unconfigured_chair(tmp_path):
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "forged-chair")
    entry = next(
        row
        for row in tree.build_manifest(ATTESTATORES)["artifacts"]
        if row["kind"] == "act-attachment"
    )
    path = tree.resolve(entry["relative_path"])
    record = tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
    forged = dict(record["payload"]["attachments"][0], chair="attestator_ghost")
    record["payload"]["attachments"].append(forged)
    _reseal(path, record)

    result = invoke_stage(root, "forged-chair", "happy", "pipeline/4_perlector/run.py")
    assert result.returncode != 0
    assert "configured witnesses" in result.stderr


def test_perlector_refuses_a_referenced_page_ordinal_outside_the_fixture(tmp_path):
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "forged-page")
    manifest = tree.build_manifest(ATTESTATORES)
    page_entry = next(row for row in manifest["artifacts"] if row["kind"] == "page-testimonium")
    page_path = tree.resolve(page_entry["relative_path"])
    page = tree.read_artifact(ATTESTATORES, "page-testimonium", page_entry["artifact_id"])
    page["payload"]["page_ordinal"] = 99
    _reseal(page_path, page)
    page_digest = digest_bytes(page_path.read_bytes())

    attachment_entry = next(row for row in manifest["artifacts"] if row["kind"] == "act-attachment")
    attachment_path = tree.resolve(attachment_entry["relative_path"])
    attachment = tree.read_artifact(ATTESTATORES, "act-attachment", attachment_entry["artifact_id"])
    for row in attachment["payload"]["attachments"]:
        reference = row["testimonium_ref"]
        if reference["relative_path"] == page_entry["relative_path"]:
            reference["sha256"] = page_digest
    _reseal(attachment_path, attachment)

    result = invoke_stage(root, "forged-page", "happy", "pipeline/4_perlector/run.py")
    assert result.returncode != 0
    assert "wrong page Testimonium" in result.stderr


# --- 4. Audit-and-repair regression (F-O1): the reread path ----------------------
#
# Opus audit-and-repair seat 3, R0. The derived act-attachment is written only by
# the whole-pass path; `reread_pass` appends a new act-scoped attempt and writes no
# new attachment. Every other consumer of these artifacts collapses each chair to
# its latest attempt on purpose -- `testimonia_of` says so in its own docstring
# ("cannot see a superseded attempt as though it were still live"), and
# `chair_outcomes` says the two derivations are shared so consumers "cannot drift
# on what current means". The attachment was a third consumer that did drift.


def test_perlector_refuses_an_attachment_describing_a_superseded_attempt(tmp_path):
    """F-O1: a targeted reread leaves the attachment describing the old attempt.

    `reread-success` declares a second, longer response for attestator_1 on act
    a2. Before this fix the run continued in silence with the attachment still
    claiming `span 0..40` and the ordinal-1 `content_health`, while that chair's
    current Testimonium delivered 48 characters -- a positive alignment claim over
    text that is no longer the reading, which is precisely the dishonesty F-S2 and
    F-P2 closed on the first-pass path.
    """
    root = tmp_path / "runs"
    fixture_data = load_fixture(str(FIXTURE_ROOT))
    act_a2_id = act_identity(fixture_data, act_by_key(fixture_data, "a2"))
    _through_attestatores(root, "superseded", "reread-success")
    assert invoke_stage(root, "superseded", "reread-success", "pipeline/4_perlector/run.py")

    reread = _reread(root, "superseded", "reread-success", act_a2_id, "attestator_1")
    assert reread.returncode == 0, reread.stderr

    result = invoke_stage(root, "superseded", "reread-success", "pipeline/4_perlector/run.py")
    assert result.returncode != 0, (
        "the Perlector accepted an act-attachment describing an attempt that is no longer "
        "the chair's current Testimonium"
    )
    assert "no longer this chair's current Testimonium" in result.stderr, result.stderr


def test_the_witness_floor_is_not_counted_from_a_superseded_attachment(tmp_path):
    """F-O1: the floor may not be counted from an attachment the reread outdated.

    Drives the natural order -- whole pass, targeted reread, Perlector, Recensor.
    `reread-failure` declares attestator_3's second attempt on act a1 as a
    failure, so that chair's current Testimonium is `failed` while its ordinal-1
    attachment still says `attached: true` with the successful attempt's health.

    Before this fix both stages exited 0 and the real partition receipt for act a1
    recorded `by_outcome {failed: 1, read: 2}` beside `shortfalls {failed: 1,
    truncated: 0, unaligned: 0}` and `health_unrecorded: 0` -- the outcome half
    describing the current failed attempt and the attachment half describing the
    superseded successful one, in one self-contradictory record. (Compare
    `test_a_failed_act_scoped_attempt_produces_a_real_failed_and_unaligned_
    shortfall`, which pins `unaligned: 1` for exactly this situation on the
    first-pass path.) The same staleness in the other direction -- failed first,
    read on the reread -- drops a recovered read out of `completed` and reports it
    as a `page_granularity_only` contribution that never happened.

    Both consumers are asserted, because either can be reached first by hand: the
    Perlector refuses the stale custody record, and the Recensor refuses to count
    a floor from it before it publishes any review.
    """
    root = tmp_path / "runs"
    fixture_data = load_fixture(str(FIXTURE_ROOT))
    act_a1_id = act_identity(fixture_data, act_by_key(fixture_data, "a1"))
    tree = _through_attestatores(root, "stale-floor", "reread-failure")

    reread = _reread(root, "stale-floor", "reread-failure", act_a1_id, "attestator_3")
    assert reread.returncode == 0, reread.stderr

    perlector = invoke_stage(root, "stale-floor", "reread-failure", "pipeline/4_perlector/run.py")
    assert perlector.returncode != 0, (
        "the Perlector read on over an act-attachment describing a superseded attempt"
    )
    recensor = invoke_stage(root, "stale-floor", "reread-failure", "pipeline/5_recensor/run.py")
    assert recensor.returncode != 0, (
        "the Recensor counted the witness floor from an act-attachment describing a "
        "superseded attempt"
    )
    assert "superseded attempt" in recensor.stderr, recensor.stderr
    assert not tree.resolve(RECENSOR_PARTITION_RECEIPT_FILE).exists(), (
        "a partition receipt was written from a coverage count the reread had already "
        "superseded; the denominator is validated before this stage publishes anything"
    )
