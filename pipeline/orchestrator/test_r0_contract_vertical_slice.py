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
from common.fixture_identity import act_identity, page_identity
from common.runtree.store import RECENSOR_PARTITION_RECEIPT_FILE, RunTree
from common.stage import (
    act_by_key,
    load_fixture,
    run_config_bindings,
)
from conftest import rebind_stage_seal_artifact as _rebind_stage_seal

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


def test_dissent_compares_each_page_witness_against_its_act_anchored_view(run_tree, fixture):
    """R4 restores the real roster: page rows compare their computed act slices."""
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
        assert row.get("compared") is True, row


def test_a_page_witness_matching_the_ink_exactly_records_no_departure(run_tree, fixture):
    """F-X2. The act-anchored view must be THIS act's slice, not the chair's
    whole page reading.

    `attestator_1` reproduces both fixture acts exactly, so its dissent row for
    each act must be empty: "a metric that rewards disagreement rewards
    hallucination" (ARCHITECTURE, on dissent), and this is the one instrument
    that catches a chair which learned to agree with witnesses rather than read
    ink. Hulling whole overlapping matching blocks handed act a1 the entire
    page -- so the chair that agreed with the ink perfectly was recorded as
    departing over the whole of act a2, and the better the witness the larger
    the false departure.
    """
    scenario, tree = run_tree
    for act_key in ("a1", "a2"):
        act_id = act_identity(fixture, act_by_key(fixture, act_key))
        reading = _latest_completed_reading(tree, act_id)
        row = next(
            row for row in reading["payload"]["dissent"] if row.get("chair") == "attestator_1"
        )
        assert row["compared"] is True, row
        assert row["departed"] is False, (
            f"scenario {scenario!r}: act {act_key} records a departure against a page witness "
            f"whose text reproduces the reading exactly: {row!r}"
        )
        assert row["departures"] == [], row


def test_two_acts_on_one_page_never_claim_the_same_page_witness_bytes(run_tree, fixture):
    """F-X2. A span is a provenance claim about which of this chair's characters
    belong to this act. Two acts asserting the identical range of one page
    reading is not a partition of that reading, it is the same claim made
    twice, and GOALS 5 asks every result to return to the exact ink it came
    from.

    Strengthened on the P2 review from identity to disjointness: two acts
    claiming *overlapping* ranges is the same false provenance claim as two
    acts claiming identical ones, only harder to see, and identity alone would
    pass a witness-side hull that absorbed a neighbouring act's characters at
    one end. A zero-length span (a `genuinely-empty` page witness) claims no
    character, so it is exempt from both arms: the trivial attach of one page
    witness is (0, 0) for every act on the page, and that repetition is
    decided behaviour rather than a double claim.
    """
    scenario, tree = run_tree
    spans: dict[str, list[tuple[str, dict]]] = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "act-attachment":
            continue
        record = tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        for attachment in record["payload"]["attachments"]:
            if attachment["page_witness"] and attachment["attached"]:
                spans.setdefault(attachment["chair"], []).append(
                    (record["subject_id"], attachment["span"])
                )
    assert spans, f"scenario {scenario!r}: no attached page-witness span to check"
    for chair, rows in spans.items():
        seen: dict[tuple[int, int], str] = {}
        for act_id, span in rows:
            if span["end"] == span["start"]:
                # A zero-length span claims no character, so two of them are
                # not the same provenance claim made twice -- a page witness
                # that read a page and found nothing attaches at (0, 0) for
                # every act on it (the trivial attach), and that is not the
                # double-claim this arm refuses.
                continue
            key = (span["start"], span["end"])
            clash = seen.get(key)
            assert clash is None, (
                f"scenario {scenario!r}: acts {clash} and {act_id} both claim characters "
                f"{key} of chair {chair}'s page reading"
            )
            seen[key] = act_id
        claimed = sorted(
            (span["start"], span["end"], act_id)
            for act_id, span in rows
            if span["end"] > span["start"]
        )
        for index in range(1, len(claimed)):
            _, earlier_end, earlier_act = claimed[index - 1]
            later_start, _, later_act = claimed[index]
            assert earlier_end <= later_start, (
                f"scenario {scenario!r}: acts {earlier_act} and {later_act} claim overlapping "
                f"characters of chair {chair}'s page reading"
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
    assert "corpus_frame_membership" in run, (
        f"scenario {scenario!r}: run.json carries no corpus_frame_membership (top-level "
        f"keys: {sorted(run)}); R0_CONTRACT_NOTE.md requires every orchestrator run to "
        "record corpus-frame membership (frame digest + page digest + seed) at run creation"
    )
    membership = run["corpus_frame_membership"]
    required_facts = {"frame_digest", "page_digest", "seed"}
    assert isinstance(membership, dict) and required_facts <= set(membership), (
        f"scenario {scenario!r}: run.json's corpus_frame_membership is {membership!r}, "
        f"which does not carry all of {required_facts}"
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
    from common.stage import DEFAULT_CORPUS_FRAME_CONFIG_PATH, load_corpus_frame_policy

    fixture_data = load_fixture(str(FIXTURE_ROOT))
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    bindings = run_config_bindings(registry.config, fixture_data, "happy")
    sealed = bindings["sealed_config_digests"]
    assert "corpus-frame-shard" in sealed, (
        f"run_config_bindings()'s sealed_config_digests is {sorted(sealed)}, which names no "
        "'corpus-frame-shard' entry; R0's shard-size knob must be sealed into config_digest "
        "with a point-of-use recheck, exactly as 'designator-padding' already is"
    )
    _, expected_digest = load_corpus_frame_policy(DEFAULT_CORPUS_FRAME_CONFIG_PATH)
    assert sealed["corpus-frame-shard"] == expected_digest, (
        "the sealed corpus-frame-shard digest does not match the digest of the sealed "
        "config bytes; a renamed or mis-bound knob would pass a name-only check"
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
    checked_page_witness_entries = 0
    for record in attachment_records:
        for entry in record["payload"]["attachments"]:
            if not entry.get("attached"):
                continue
            if entry.get("page_witness"):
                # A page witness's span is a slice of its PAGE reading, so it is
                # not this act's own character count -- but it must still be a
                # well-formed, non-empty range that agrees with the computed
                # alignment it is derived from, never a wider hull carried over
                # from another act's text (F-X2). Checked BEFORE the
                # character-count guard below: a page-witness entry without an
                # integer count would otherwise reach no assertion at all, and
                # this test would stop guarding the exact regression its
                # docstring names.
                span = entry["span"]
                assert entry["alignment"]["status"] == "aligned"
                assert span == entry["alignment"]["witness_span"], entry
                assert span["end"] >= span["start"], entry
                # `or {}`, not a .get default: an explicit `content_health: null`
                # is a valid recorded fact ("health not recorded", per the
                # Recensor), and the default alone would crash this test on it.
                if (entry.get("content_health") or {}).get("characters"):
                    # Only a reading that delivered characters must claim a
                    # non-empty span: the trivial zero-length attach of a
                    # genuinely-empty page witness is a legitimate shape
                    # (asserted as such by the acceptance suite), so demanding
                    # end > start unconditionally would go red against decided
                    # behaviour the moment that scenario joins this
                    # parametrisation.
                    assert span["end"] > span["start"], entry
                checked_page_witness_entries += 1
                continue
            characters = (entry.get("content_health") or {}).get("characters")
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
    assert checked_page_witness_entries, (
        f"scenario {scenario!r}: no attached page-witness entry was checked, so this test "
        "proved nothing about the act-anchored span it exists to guard"
    )


def test_a_non_reading_page_attempt_is_an_explicit_unaligned_reason_never_an_alignment(
    run_tree, fixture
):
    """A page witness's attempt that produced no reading must short-circuit to an
    explicit `unaligned` reason naming that outcome -- never run the page
    alignment. Before this fix, a failed attempt on a page-witness chair could
    reach the alignment path, come back `aligned` (the page text is other acts'
    successful readings), and publish `attached: False` beside an aligned
    alignment -- the exact shape `pipeline/4_perlector/run.py` and
    `pipeline/5_recensor/run.py::act_attachment_facts` both refuse. One failed
    witness attempt then stopped the act for a reason that has nothing to do
    with the ink.

    In `review`, attestator_3 (a page chair) has a declared `witness_failure`
    on act a2, so this pins the produced record; in `happy` it asserts the
    invariant over every entry (no unattached page witness carries anything but
    an explicit unaligned result).
    """
    scenario, tree = run_tree
    attachment_records = [
        record for record in _attestatores_artifacts(tree) if record.get("kind") == "act-attachment"
    ]
    for record in attachment_records:
        for entry in record["payload"]["attachments"]:
            if entry.get("page_witness") and not entry.get("attached"):
                assert entry["alignment"]["status"] == "unaligned", entry
    if scenario == "review":
        a2_id = act_identity(fixture, act_by_key(fixture, "a2"))
        record = next(record for record in attachment_records if record["subject_id"] == a2_id)
        entry = next(
            entry for entry in record["payload"]["attachments"] if entry["chair"] == "attestator_3"
        )
        assert entry["attached"] is False
        assert entry["alignment"] == {
            "status": "unaligned",
            "reason": "non-reading-page-attempt-failed",
        }, entry


def test_an_attached_page_witness_alignment_names_its_anchor_basis(run_tree):
    """Every aligned page-witness alignment states what it aligned against.

    `anchor_basis` is the field that keeps a trivial zero-length attach on a
    page with no located anchor line (`no-page-anchor`/`act-line-not-located`)
    distinguishable from an
    alignment computed through Chandra's anchor (`act-anchor`). Without it, a
    page whose anchor pass failed could satisfy the witness floor and reach
    confirmed-blank while looking identical to a proved blank sheet. Both
    shipped scenarios carry located anchor lines for both acts, so every
    aligned record here must say `act-anchor`.
    """
    scenario, tree = run_tree
    checked = 0
    for record in _attestatores_artifacts(tree):
        if record.get("kind") != "act-attachment":
            continue
        for entry in record["payload"]["attachments"]:
            alignment = entry.get("alignment")
            if isinstance(alignment, dict) and alignment.get("status") == "aligned":
                assert alignment["anchor_basis"] == "act-anchor", entry
                checked += 1
    assert checked, f"scenario {scenario!r}: no aligned page-witness alignment was checked"


def test_perlector_consumes_the_page_testimonium_named_by_an_act_attachment(
    tmp_path, rebind_stage_seal
):
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
    # The nested reference is the claim under test, so the outer boundary is
    # rebound: without it the Attestatores completion seal refuses first and
    # this proves the seal a second time instead of the reference check.
    rebind_stage_seal(tree, ATTESTATORES, rewrite_manifest=False)
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


def _reseal(tree: RunTree, path: Path, record: dict) -> None:
    record["self_hash"] = self_hash(
        {key: value for key, value in record.items() if key != "self_hash"}
    )
    path.write_bytes(canonical_bytes(record))
    _rebind_stage_seal(tree, record["stage"])


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
    _reseal(tree, path, record)

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
    _reseal(tree, page_path, page)
    page_digest = digest_bytes(page_path.read_bytes())

    # Each act attachment that consumes this page Testimonium carries its own
    # sealed reference.  Update every one so the stage can reach the semantic
    # page-ordinal check rather than correctly stopping at stale provenance.
    for attachment_entry in manifest["artifacts"]:
        if attachment_entry["kind"] != "act-attachment":
            continue
        attachment_path = tree.resolve(attachment_entry["relative_path"])
        attachment = tree.read_artifact(
            ATTESTATORES, "act-attachment", attachment_entry["artifact_id"]
        )
        changed = False
        for row in attachment["payload"]["attachments"]:
            reference = row["testimonium_ref"]
            if reference["relative_path"] == page_entry["relative_path"]:
                reference["sha256"] = page_digest
                changed = True
        if changed:
            _reseal(tree, attachment_path, attachment)

    result = invoke_stage(root, "forged-page", "happy", "pipeline/4_perlector/run.py")
    assert result.returncode != 0
    assert "wrong page Testimonium" in result.stderr


def test_perlector_refuses_a_page_role_its_own_ordinal_contradicts(tmp_path):
    """A continuation page's Testimonium may not wear the `primary` label.

    `page_ordinal` is the fact the attachment already reconciles independently
    (`test_perlector_refuses_a_referenced_page_ordinal_outside_the_fixture`
    above); `page_role` was written but never read back against it, so a
    resealed continuation record could claim `primary` and nothing caught the
    flip. Act a2's own contributing pages ({1, 2}) say which page is its
    primary one, so a page-2 record claiming `primary` contradicts a fact the
    reader already holds -- no second artifact needed to see the lie.
    """
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "forged-role")
    manifest = tree.build_manifest(ATTESTATORES)
    page_entry = next(
        row
        for row in manifest["artifacts"]
        if row["kind"] == "page-testimonium"
        and tree.read_artifact(ATTESTATORES, "page-testimonium", row["artifact_id"])["payload"][
            "page_ordinal"
        ]
        == 2
    )
    page_path = tree.resolve(page_entry["relative_path"])
    page = tree.read_artifact(ATTESTATORES, "page-testimonium", page_entry["artifact_id"])
    assert page["payload"]["page_role"] == "continuation"
    page["payload"]["page_role"] = "primary"
    _reseal(tree, page_path, page)
    page_digest = digest_bytes(page_path.read_bytes())

    for attachment_entry in manifest["artifacts"]:
        if attachment_entry["kind"] != "act-attachment":
            continue
        attachment_path = tree.resolve(attachment_entry["relative_path"])
        attachment = tree.read_artifact(
            ATTESTATORES, "act-attachment", attachment_entry["artifact_id"]
        )
        changed = False
        for row in attachment["payload"]["attachments"]:
            reference = row["testimonium_ref"]
            if reference["relative_path"] == page_entry["relative_path"]:
                reference["sha256"] = page_digest
                changed = True
        if changed:
            _reseal(tree, attachment_path, attachment)

    result = invoke_stage(root, "forged-role", "happy", "pipeline/4_perlector/run.py")
    assert result.returncode != 0
    assert "page_role" in result.stderr and "contradicts" in result.stderr


def test_the_recensor_refuses_a_page_role_only_the_whole_page_disproves(tmp_path):
    """`mixed` is the label one act can never contradict, so a later stage must.

    The check above is the Perlector's, and it reads one act at a time: it can
    refuse `primary` on a page this act only continues onto, and `continuation`
    on the act's own primary page, because those two contradict a fact the act
    itself holds. `mixed` claims the page carries BOTH a primary region and a
    continuation, which no single act can disprove -- so the same forgery, one
    label along, walked past the Perlector untouched.

    The Recensor holds every act on the page, so it re-derives the role from the
    acts actually attached to that page and refuses the claim there. Page 2 in
    this fixture is reached only by a2's continuation, so `mixed` is a lie about
    a page whose evidence the run has in full.
    """
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "forged-mixed")
    manifest = tree.build_manifest(ATTESTATORES)
    page_entry = next(
        row
        for row in manifest["artifacts"]
        if row["kind"] == "page-testimonium"
        and tree.read_artifact(ATTESTATORES, "page-testimonium", row["artifact_id"])["payload"][
            "page_ordinal"
        ]
        == 2
    )
    page_path = tree.resolve(page_entry["relative_path"])
    page = tree.read_artifact(ATTESTATORES, "page-testimonium", page_entry["artifact_id"])
    assert page["payload"]["page_role"] == "continuation"
    page["payload"]["page_role"] = "mixed"
    _reseal(tree, page_path, page)
    page_digest = digest_bytes(page_path.read_bytes())

    for attachment_entry in manifest["artifacts"]:
        if attachment_entry["kind"] != "act-attachment":
            continue
        attachment_path = tree.resolve(attachment_entry["relative_path"])
        attachment = tree.read_artifact(
            ATTESTATORES, "act-attachment", attachment_entry["artifact_id"]
        )
        changed = False
        for row in attachment["payload"]["attachments"]:
            reference = row["testimonium_ref"]
            if reference["relative_path"] == page_entry["relative_path"]:
                reference["sha256"] = page_digest
                changed = True
        if changed:
            _reseal(tree, attachment_path, attachment)
    # The forged bytes are now the tree's own record of themselves, so the
    # Recensor's manifest reconciliation refuses the *cache* rather than the
    # claim. Rewriting it is what puts the forgery in front of the check under
    # test instead of in front of an earlier one.
    tree.write_manifest(ATTESTATORES)

    # The Perlector is the control: it accepts what it structurally cannot
    # disprove, which is exactly why the Recensor's check has to exist.
    forward = invoke_stage(root, "forged-mixed", "happy", "pipeline/4_perlector/run.py")
    assert forward.returncode == 0, forward.stderr

    result = invoke_stage(root, "forged-mixed", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode != 0
    assert "page_role 'mixed'" in result.stderr
    assert "'continuation'" in result.stderr


# --- Fresh-context review (P2): the second spelling of the scope claim -----------
#
# The act-scoped Testimonium carries the page-witness claim a second time, as the
# optional `page_witness` payload flag, and `pipeline/4_perlector/dissent.py` reads
# that flag directly. The attachment's copy was reconciled against the run's
# declaration; this one was reconciled against nothing.


def test_perlector_refuses_an_act_scoped_testimonium_wearing_a_page_witness_flag(tmp_path):
    """A resealed flag may not switch off a chair's dissent comparison.

    `page_witness` is an accepted optional field of the closed act-level payload,
    so setting it on attestator_2 -- the chair D1 keeps act-scoped -- is a
    well-formed disguise. Before this fix nothing refused it and the reading
    sealed with attestator_2's dissent row reduced to `compared: "unknown"`,
    carrying the page-witness reason for a chair that is not one: the structural
    parroting instrument disabled behind a plausible sentence, which is precisely
    what ARCHITECTURE's dissent section exists to keep measurable.
    """
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "forged-scope")
    entry = next(
        row
        for row in tree.build_manifest(ATTESTATORES)["artifacts"]
        if row["kind"] == "testimonium"
        and tree.read_artifact(ATTESTATORES, "testimonium", row["artifact_id"])["payload"]["chair"]
        == RETAINED_ACT_WITNESS_CHAIR
    )
    record = tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
    record["payload"]["page_witness"] = True
    _reseal(tree, tree.resolve(entry["relative_path"]), record)

    result = invoke_stage(root, "forged-scope", "happy", "pipeline/4_perlector/run.py")
    assert result.returncode != 0, (
        "the Perlector read on over an act-scoped Testimonium claiming page-witness scope"
    )
    assert "page-witness scope this run did not declare" in result.stderr, result.stderr


# --- 4. Audit-and-repair regression (F-O1): the derived act-attachment ----------
#
# Opus audit-and-repair seat 3, R0. Every consumer of these artifacts collapses
# each chair to its latest attempt on purpose -- `testimonia_of` says so in its own
# docstring ("cannot see a superseded attempt as though it were still live"), and
# `chair_outcomes` says the two derivations are shared so consumers "cannot drift
# on what current means". The attachment was a third consumer that did drift.
#
# The producing path is closed at its source since the one attempt model: an
# act-targeted reread of a page witness is refused by name, and an act-scoped
# reread re-derives this act's attachment as part of its own write, so no ordinary
# invocation leaves a record describing a superseded attempt. See
# `pipeline/orchestrator/test_attempt_model.py` for that half. What these three
# keep is the other half, which does not depend on which invocation could produce
# it: a record on disk that contradicts the current testimony is refused at both
# consumers. They reach it by resealing, because a structural guard is exactly
# what a damaged record is for.


def _latest_attachment(tree: RunTree, act_id: str) -> tuple[Path, dict]:
    """The act's current derived attachment record and the path holding it."""
    entries = [
        entry
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "act-attachment" and entry["subject_id"] == act_id
    ]
    assert entries, f"no act-attachment custody record exists for act {act_id}"
    entry = max(
        entries,
        key=lambda item: tree.read_artifact(ATTESTATORES, "act-attachment", item["artifact_id"])[
            "payload"
        ]["attempt_ordinal"],
    )
    return (
        tree.resolve(entry["relative_path"]),
        tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"]),
    )


def test_perlector_refuses_an_attachment_describing_a_superseded_attempt(tmp_path):
    """F-O1's guard, exercised as the structural guard it now is.

    The producing path that once made this state is closed at its source: a
    targeted reread of a page witness is refused by name (there is no act-scoped
    attempt for a page witness to repeat), and an act-scoped reread re-derives
    this act's attachment as part of its own write, so no ordinary invocation
    leaves the record describing a superseded attempt any more. See
    `pipeline/orchestrator/test_attempt_model.py` for that half.

    What is asserted here is the other half, and it does not depend on which
    invocation could produce it: a record on disk claiming a `content_health` that
    is not the chair's current one is refused at the consumer. `attached` may
    legitimately diverge from a page witness's own outcome — alignment can
    honestly fail against live text — but the health of the attempt the record
    actually describes must always still be that chair's current one, and R0's
    `granularity_basis` claim rests on it.
    """
    root = tmp_path / "runs"
    fixture_data = load_fixture(str(FIXTURE_ROOT))
    act_a2_id = act_identity(fixture_data, act_by_key(fixture_data, "a2"))
    tree = _through_attestatores(root, "superseded", "reread-success")

    path, record = _latest_attachment(tree, act_a2_id)
    entry = next(
        row for row in record["payload"]["attachments"] if row["chair"] == PAGE_WITNESS_CHAIRS[0]
    )
    assert entry["page_witness"] is True
    entry["content_health"] = {**entry["content_health"], "characters": 4096}
    _reseal(tree, path, record)

    result = invoke_stage(root, "superseded", "reread-success", "pipeline/4_perlector/run.py")

    assert result.returncode != 0, (
        "the Perlector accepted an act-attachment describing an attempt that is no longer "
        "the chair's current Testimonium"
    )
    assert "no longer this chair's current Testimonium" in result.stderr, result.stderr


def test_the_witness_floor_is_not_counted_from_a_superseded_attachment(tmp_path):
    """The Recensor's own copy of the check above, on the same damaged record.

    Both consumers are asserted because either can be reached first by hand: the
    Perlector refuses the stale custody record, and the Recensor refuses to count
    a floor from it before it publishes any review. `pipeline/5_recensor/run.py::
    chair_current_attempts` gives the Recensor the same per-chair staleness signal
    `act_attachment_view` uses, and the two must not drift.

    Before F-O1's fix both stages exited 0 and the real partition receipt for act
    a1 recorded `by_outcome {failed: 1, read: 2}` beside `shortfalls {failed: 1,
    truncated: 0, unaligned: 0}` and `health_unrecorded: 0` — the outcome half
    describing one attempt and the attachment half describing another, in one
    self-contradictory record.
    """
    root = tmp_path / "runs"
    fixture_data = load_fixture(str(FIXTURE_ROOT))
    act_a1_id = act_identity(fixture_data, act_by_key(fixture_data, "a1"))
    tree = _through_attestatores(root, "stale-floor", "reread-failure")

    path, record = _latest_attachment(tree, act_a1_id)
    entry = next(
        row for row in record["payload"]["attachments"] if row["chair"] == PAGE_WITNESS_CHAIRS[1]
    )
    entry["content_health"] = {**entry["content_health"], "characters": 4096}

    # Sealed over the correct record first, for the reason given in
    # `test_act_scoped_attachment_must_match_the_current_outcome_when_health_is_current`:
    # a Perlector that never sealed sends the Recensor to the missing-boundary
    # refusal, and the drift this test exists to catch stops being measured.
    assert (
        invoke_stage(
            root, "stale-floor", "reread-failure", "pipeline/4_perlector/run.py"
        ).returncode
        == 0
    )
    _reseal(tree, path, record)

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


def test_act_scoped_attachment_must_match_the_current_outcome_when_health_is_current(tmp_path):
    """F-O1's restored outcome guard carries evidence independent of health.

    An act-scoped reread fails after the original successful read, and the reread
    re-derives this act's attachment so the record on disk is correct. This test
    then makes that correct record wrong in exactly one respect — its positive
    `attached` fact — while leaving every health-derived field current. Perlector
    and Recensor must each refuse that one remaining contradiction rather than
    count a superseded read.
    """
    root = tmp_path / "runs"
    fixture_data = load_fixture(str(FIXTURE_ROOT))
    act_a1_id = act_identity(fixture_data, act_by_key(fixture_data, "a1"))
    tree = _through_attestatores(root, "act-scoped-stale", "happy")

    reread = _reread(
        root,
        "act-scoped-stale",
        "happy",
        act_a1_id,
        RETAINED_ACT_WITNESS_CHAIR,
    )
    assert reread.returncode == 0, reread.stderr

    current = max(
        (
            record
            for record in _attestatores_artifacts(tree)
            if record.get("kind") == "testimonium"
            and record.get("subject_id") == act_a1_id
            and record.get("payload", {}).get("chair") == RETAINED_ACT_WITNESS_CHAIR
        ),
        key=lambda record: record["payload"]["attempt_ordinal"],
    )
    assert current["outcome"] == "failed"

    attachment_path, attachment_record = _latest_attachment(tree, act_a1_id)
    attachment = next(
        row
        for row in attachment_record["payload"]["attachments"]
        if row["chair"] == RETAINED_ACT_WITNESS_CHAIR
    )
    assert attachment["page_witness"] is False
    # The reread re-derived this record, so it correctly reports the failed
    # attempt. Only the positive fact is forged back in.
    assert attachment["attached"] is False
    assert attachment["content_health"] == current["payload"]["content_health"]
    attachment["attached"] = True
    # The span must stay consistent with the current health, or the Perlector
    # refuses on the span before it reaches the outcome guard this test is named
    # for: a non-integer character count skips that span check entirely, and 0
    # matches the forged {0, 0}. Any other value would go red on the span message
    # instead of the outcome guard.
    assert current["payload"]["content_health"]["characters"] in (None, 0), current["payload"]
    attachment["span"] = {"start": 0, "end": 0}

    # The Perlector reads and seals its boundary once over the *correct* record,
    # before the forgery. Otherwise its refusal below leaves no Perlector seal at
    # all and the Recensor stops on the missing boundary instead of on the
    # contradiction — which is the check this test is named for and the one half
    # of "each must refuse" that would then be proven nowhere. The forgery is in
    # the Attestatores' folder, so the Perlector's own sealed boundary stays true.
    assert (
        invoke_stage(root, "act-scoped-stale", "happy", "pipeline/4_perlector/run.py").returncode
        == 0
    )
    _reseal(tree, attachment_path, attachment_record)

    perlector = invoke_stage(root, "act-scoped-stale", "happy", "pipeline/4_perlector/run.py")
    assert perlector.returncode != 0
    assert "disagrees with that chair's current Testimonium outcome" in perlector.stderr

    recensor = invoke_stage(root, "act-scoped-stale", "happy", "pipeline/5_recensor/run.py")
    assert recensor.returncode != 0
    assert "disagrees with the current Testimonium outcome" in recensor.stderr


def test_page_testimony_names_a_reading_the_join_could_not_carry(tmp_path, fixture):
    """F-O7: the page record's closed omission list is the join's exact complement.

    The join drops an act two ways -- a non-reading outcome (F-S1) and a reading
    the join cannot concatenate, because the chair delivered a structured native
    object rather than text. `unjoined_act_attempts` named only the first, so the
    second went behind a successful status: in the shipped `structured-witness`
    scenario, attestator_1's page-1 record reported `read`, carried act a2's text
    alone, and disclosed an empty omission list while act a1 was simply gone
    (GOVERNANCE 2). Measured on the real run tree before the fix.
    """
    root = tmp_path / "runs"
    _through_attestatores(root, "structured", "structured-witness")
    tree = RunTree(root, "structured")
    act_a1_id = act_identity(fixture, act_by_key(fixture, "a1"))

    act_record = next(
        record
        for record in _attestatores_artifacts(tree)
        if record.get("kind") == "testimonium"
        and record["subject_id"] == act_a1_id
        and record.get("payload", {}).get("chair") == "attestator_1"
    )
    assert act_record["outcome"] == "read", (
        "fixture precondition: attestator_1's act-a1 attempt must be a completed reading "
        f"for this test to exercise the silent-omission path; got {act_record['outcome']!r}"
    )
    assert not isinstance(act_record["payload"]["payload"], str), (
        "fixture precondition: that reading must be a structured native object"
    )

    page_payload = next(
        record["payload"]
        for record in _attestatores_artifacts(tree)
        if record.get("kind") == "page-testimonium"
        and record.get("payload", {}).get("chair") == "attestator_1"
        and record.get("payload", {}).get("page_ordinal") == 1
    )
    unjoined = page_payload["unjoined_act_attempts"]
    named = {row["act_id"]: row for row in unjoined}
    assert act_a1_id in named, (
        f"attestator_1's page-1 record joined only {page_payload['payload']!r} and named "
        f"{unjoined!r} as omitted; act a1's structured reading is absent from both, so the "
        "record reports a page it did not fully cover and says nothing about the gap"
    )
    row = named[act_a1_id]
    assert row["outcome"] == "read", (
        "the omission must be disclosed with the attempt outcome that actually happened, "
        f"not relabelled as a failure; got {row['outcome']!r}"
    )
    assert isinstance(row["reason"], str) and row["reason"].strip()
