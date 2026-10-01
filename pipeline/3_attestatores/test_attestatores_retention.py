"""Spec 07 retention and native page-Testimonium tests over the real stage program."""

import ast
import copy
import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.envelope import build_envelope
from common.contracts.errors import ApprovalRefusal, IncompatibleReuse, SchemaRefusal
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.outcomes import witness_coverage
from common.contracts.stages import ATTESTATORES, DESIGNATOR
from common.native_witness import PAGE_TESTIMONIUM_REQUIRED_FIELDS
from common.runtree.store import RunTree
from common.stage import latest_per_chair
from conftest import load_stage, programs_through

EXPECTED_MANIFEST_CALLS = 8

ROOT = Path(__file__).resolve().parents[2]
PAGE_KIND = "page-testimonium"

attestatores = load_stage("3_attestatores")


def invoke_stage(
    run_root: Path,
    run_id: str,
    scenario: str,
    program: str,
    *,
    fixture_root: Path = ROOT / "proof",
    **extra,
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(run_root),
        "--run-id",
        run_id,
        "--scenario",
        scenario,
        "--fixture-root",
        str(fixture_root),
    ]
    for key, value in extra.items():
        command.extend((f"--{key.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def _argv(run_root: Path, scenario: str, *extra: str) -> list[str]:
    return [
        "run.py",
        "--run-root",
        str(run_root),
        "--run-id",
        "retention",
        "--scenario",
        scenario,
        "--fixture-root",
        str(ROOT / "proof"),
        *extra,
    ]


def run_to_designator(tmp_path: Path, scenario: str) -> tuple[Path, RunTree]:
    run_root = tmp_path / "runs"
    for program in programs_through("designator"):
        result = invoke_stage(run_root, "retention", scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return run_root, RunTree(run_root, "retention")


def run_attestatores(run_root: Path, scenario: str = "happy", **extra) -> None:
    result = invoke_stage(
        run_root, "retention", scenario, "pipeline/3_attestatores/run.py", **extra
    )
    assert result.returncode == 0, result.stderr


def stage_context(run_root: Path, scenario: str = "happy"):
    parser = attestatores.stage_parser("retention under test")
    args = parser.parse_args(_argv(run_root, scenario)[1:])
    return attestatores.open_stage_context(args, ATTESTATORES)


def tally(run_root: Path, scenario: str = "happy", **kwargs) -> dict:
    context = stage_context(run_root, scenario)
    proposals = attestatores.sealed_proposal_regions(context)
    return attestatores.attempt_tally(context, proposals=proposals, **kwargs)


def _page_records(tree: RunTree) -> list[dict]:
    return [
        tree.read_artifact(ATTESTATORES, PAGE_KIND, entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == PAGE_KIND
    ]


def _record_for(tree: RunTree, *, page_ordinal: int, chair: str, ordinal: int = 1) -> dict:
    return next(
        record
        for record in _page_records(tree)
        if record["payload"]["page_ordinal"] == page_ordinal
        and record["payload"]["chair"] == chair
        and record["payload"]["attempt_ordinal"] == ordinal
    )


def _record_path(tree: RunTree, record: dict) -> Path:
    return tree.resolve(tree.artifact_path(ATTESTATORES, PAGE_KIND, record["artifact_id"]))


def _reseal(tree: RunTree, record: dict, changed: dict, *, manifest: bool = True) -> None:
    changed["self_hash"] = self_hash(changed)
    _record_path(tree, record).write_bytes(canonical_bytes(changed))
    if manifest:
        tree.write_manifest(ATTESTATORES)


def test_page_testimonium_role_with_an_unhashable_value_is_a_named_refusal():
    payload = {field: None for field in PAGE_TESTIMONIUM_REQUIRED_FIELDS}
    payload.update(
        {
            "scope": "page",
            "page_ordinal": 1,
            "page_role": [],
            "unjoined_act_attempts": [],
        }
    )

    with pytest.raises(SchemaRefusal, match="invalid page scope facts"):
        attestatores.validate_page_testimonium_payload(payload)


def test_a_whole_pass_appends_its_next_ordinal_and_keeps_every_earlier_attempt(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root, attempt_ordinal=1)
    first = _record_for(tree, page_ordinal=1, chair="attestator_1")
    first_bytes = _record_path(tree, first).read_bytes()

    # A same-ordinal resume is exact-byte reuse, not an overwrite path.
    run_attestatores(run_root, attempt_ordinal=1)
    assert _record_path(tree, first).read_bytes() == first_bytes

    run_attestatores(run_root, attempt_ordinal=2)
    records = [
        record
        for record in _page_records(tree)
        if record["payload"]["page_ordinal"] == 1 and record["payload"]["chair"] == "attestator_1"
    ]
    assert sorted(record["payload"]["attempt_ordinal"] for record in records) == [1, 2]
    assert _record_path(tree, first).read_bytes() == first_bytes
    assert len({record["artifact_id"] for record in records}) == 2
    (current,) = latest_per_chair(records, "appended page Testimonia")
    assert current["payload"]["attempt_ordinal"] == 2
    # The fixture declares no answer for attempt 2, so the new attempt is not-run
    # and attempt 1's reading is kept beside it.
    assert current["outcome"] == "not-run"
    assert tally(run_root)["count"] == 12


def test_an_unsealed_whole_pass_resumes_over_what_it_already_sealed(tmp_path, monkeypatch):
    """A crash mid-pass resumes at the same ordinal, and the run still completes.

    The crash follows the first sealed page record, so the next process meets
    immutable evidence, no stored inventory and no stage seal. It repeats the
    pass at its own ordinal: the sealed record is reused byte-for-byte and every
    page/chair pair ends at ordinal one. A per-pair resume ordinal would publish
    a `not-run` over that chair's good `read` and cost the page its coverage.
    """
    run_root, tree = run_to_designator(tmp_path, "page-unbroken")
    real_publish = attestatores.publish_page_testimonium
    writes = 0

    def crash_after_first_page(*args, **kwargs):
        nonlocal writes
        real_publish(*args, **kwargs)
        writes += 1
        raise RuntimeError("simulated process crash after one page response")

    monkeypatch.setattr(attestatores, "publish_page_testimonium", crash_after_first_page)
    monkeypatch.setattr(sys, "argv", _argv(run_root, "page-unbroken"))
    with pytest.raises(RuntimeError, match="simulated process crash"):
        attestatores.main()
    assert writes == 1
    (first,) = _page_records(tree)
    assert first["outcome"] == "read", "the crash must follow a configured chair response"
    assert not tree.resolve(tree.manifest_path(ATTESTATORES)).exists()
    monkeypatch.undo()

    run_attestatores(run_root, "page-unbroken")

    records = _page_records(tree)
    by_pair: dict[tuple[str, str], list[int]] = {}
    for record in records:
        key = (record["subject_id"], record["payload"]["chair"])
        by_pair.setdefault(key, []).append(record["payload"]["attempt_ordinal"])
    crashed_pair = (first["subject_id"], first["payload"]["chair"])
    assert crashed_pair in by_pair
    assert all(sorted(ordinals) == [1] for ordinals in by_pair.values()), by_pair
    survivor = next(
        record
        for record in records
        if (record["subject_id"], record["payload"]["chair"]) == crashed_pair
    )
    assert survivor == first
    assert tally(run_root, "page-unbroken")["state"] == "KNOWN"

    # The resumed folder still reads as a complete witness layer downstream.
    for program in (
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
        "pipeline/6_archetypus/run.py",
    ):
        result = invoke_stage(run_root, "retention", "page-unbroken", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"


def test_a_resume_that_would_answer_a_sealed_page_differently_is_refused(tmp_path, monkeypatch):
    """A fixture resume resolves every page again; a sealed page must not change.

    The refusal comes before any write, so the record the crashed pass sealed
    stays the only evidence for that pair.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    real_publish = attestatores.publish_page_testimonium
    real_attempt = attestatores.fixture_page_attempt
    calls: dict[tuple[int, str], int] = {}

    def changing_attempt(context, page_ordinal, chair, resolved, ordinal):
        key = (page_ordinal, chair)
        calls[key] = calls.get(key, 0) + 1
        attempt = real_attempt(context, page_ordinal, chair, resolved, ordinal)
        if calls[key] > 1 and isinstance(attempt.native_payload, str):
            changed = attempt.native_payload + " [different resumed decode]"
            return attempt._replace(
                native_payload=changed,
                health=attestatores.content_health(changed, completed=True),
            )
        return attempt

    def crash_after_first_page(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise RuntimeError("simulated process crash after one page response")

    monkeypatch.setattr(attestatores, "fixture_page_attempt", changing_attempt)
    monkeypatch.setattr(attestatores, "publish_page_testimonium", crash_after_first_page)
    monkeypatch.setattr(sys, "argv", _argv(run_root, "happy"))
    with pytest.raises(RuntimeError, match="simulated process crash"):
        attestatores.main()
    (sealed,) = _page_records(tree)
    sealed_bytes = _record_path(tree, sealed).read_bytes()

    monkeypatch.setattr(attestatores, "publish_page_testimonium", real_publish)
    assert attestatores.main() == attestatores.EXIT_HELD

    assert _page_records(tree) == [sealed]
    assert _record_path(tree, sealed).read_bytes() == sealed_bytes


def test_a_resume_over_a_lost_proposal_crop_refuses_by_name_before_any_write(tmp_path, monkeypatch):
    """A crash mid-pass, then a lost Designator proposal before resume.

    The proposals are read before anything else, so the resume names the lost
    proposal and leaves the sealed record alone.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    real_publish = attestatores.publish_page_testimonium

    def crash_after_first_page(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise RuntimeError("simulated process crash after one page response")

    monkeypatch.setattr(attestatores, "publish_page_testimonium", crash_after_first_page)
    monkeypatch.setattr(sys, "argv", _argv(run_root, "happy"))
    with pytest.raises(RuntimeError, match="simulated process crash"):
        attestatores.main()
    (sealed,) = _page_records(tree)

    def lost_crop(context):
        raise attestatores.ContractError("a proposed region's crop is gone from the tree")

    monkeypatch.setattr(attestatores, "publish_page_testimonium", real_publish)
    monkeypatch.setattr(attestatores, "sealed_proposal_regions", lost_crop)

    with pytest.raises(attestatores.ContractError, match="crop is gone"):
        attestatores.main()
    assert _page_records(tree) == [sealed], "the refused resume must not touch the sealed record"


def test_a_whole_pass_resolves_each_page_once_and_reads_the_proposals_once(tmp_path, monkeypatch):
    """Preflight and publication share the resolved attempts and the proposals.

    The append/collision history is one manifest walk, and the closing tally
    remains an independent rebuild.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    proposal_reads = 0
    attempt_calls = 0
    manifest_calls = 0
    real_proposals = attestatores.sealed_proposal_regions
    real_attempt = attestatores.fixture_page_attempt
    real_build_manifest = RunTree.build_manifest

    def counted_proposals(context):
        nonlocal proposal_reads
        proposal_reads += 1
        return real_proposals(context)

    def counted_attempt(*args, **kwargs):
        nonlocal attempt_calls
        attempt_calls += 1
        return real_attempt(*args, **kwargs)

    def counted_manifest(self, stage, **kwargs):
        nonlocal manifest_calls
        if self.root == tree.root and stage == ATTESTATORES:
            manifest_calls += 1
        return real_build_manifest(self, stage, **kwargs)

    monkeypatch.setattr(attestatores, "sealed_proposal_regions", counted_proposals)
    monkeypatch.setattr(attestatores, "fixture_page_attempt", counted_attempt)
    monkeypatch.setattr(RunTree, "build_manifest", counted_manifest)
    monkeypatch.setattr(sys, "argv", _argv(run_root, "happy"))

    assert attestatores.main() == 0
    # A fixed number of stage-level walks, measured on the composed tree, pinned
    # so completion evidence cannot quietly reintroduce a walk per page or chair.
    assert manifest_calls == EXPECTED_MANIFEST_CALLS
    # Two pages for each of the two whole-page chairs; DAI reads its detector's
    # records instead.
    assert attempt_calls == 4
    assert proposal_reads == 1


def test_a_whole_pass_may_not_skip_an_ordinal_over_any_seat(tmp_path):
    """A whole pass repeats or appends an ordinal, never skips one: `current + 2`
    would leave a hole where an attempt that existed is no longer here."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root)
    before = len(_page_records(tree))

    skipped = invoke_stage(
        run_root, "retention", "happy", "pipeline/3_attestatores/run.py", attempt_ordinal=3
    )

    assert skipped.returncode == 3
    assert "neither a rerun of an attempt it holds" in skipped.stderr
    assert len(_page_records(tree)) == before


@pytest.mark.parametrize("flag", ["operation", "act", "chair"])
def test_a_whole_pass_refuses_an_instruction_it_cannot_carry_out(tmp_path, flag):
    """A whole pass reads every chair on every page; an operation, act or chair
    beside it would be an instruction the stage did not carry out, so the parser
    refuses it before anything is written."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    before = (
        list(tree.resolve("3_attestatores").rglob("*"))
        if tree.resolve("3_attestatores").exists()
        else []
    )

    result = invoke_stage(
        run_root, "retention", "happy", "pipeline/3_attestatores/run.py", **{flag: "x"}
    )
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
    after = (
        list(tree.resolve("3_attestatores").rglob("*"))
        if tree.resolve("3_attestatores").exists()
        else []
    )
    assert after == before


def test_a_pass_interrupted_before_its_manifest_was_written_can_still_be_completed(tmp_path):
    """A pass killed part way through leaves records on disk and no stored tally.

    The tally stays UNKNOWN until someone re-derives the inventory; after that
    the same command supplies the missing pairs, and the page/chair denominator
    is checked when the pass closes.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root)
    tree.resolve(tree.manifest_path(ATTESTATORES)).unlink()
    for record in _page_records(tree)[:2]:
        _record_path(tree, record).unlink()

    held = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")
    assert held.returncode == 3, "an absent stored tally still holds, before anything else"
    assert "UNKNOWN" in held.stderr

    tree.write_manifest(ATTESTATORES)
    assert len(_page_records(tree)) == 4

    run_attestatores(run_root)

    assert len(_page_records(tree)) == 6
    assert all(record["payload"]["attempt_ordinal"] == 1 for record in _page_records(tree))
    assert tally(run_root)["state"] == "KNOWN"


def test_a_wiped_attempt_layer_holds_rather_than_silently_restarting_history(tmp_path):
    """The stored inventory is evidence that attempts existed, even with none left.

    Losing all of them must hold rather than take the first-run path and write
    attempt 1 for every pair. An appended second pass makes the loss material:
    the ordinal-2 attempts would not come back at all.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    for ordinal in (1, 2):
        run_attestatores(run_root, attempt_ordinal=ordinal)
    manifest = tree.resolve(tree.manifest_path(ATTESTATORES))
    stored = json.loads(manifest.read_text(encoding="utf-8"))
    # Two pages, three chairs, two ordinals of page Testimonia.
    assert len([entry for entry in stored["artifacts"] if entry["kind"] == PAGE_KIND]) == 12

    shutil.rmtree(tree.resolve("3_attestatores/artifacts"))

    wiped = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")

    assert wiped.returncode == 3, wiped.stderr
    assert "UNKNOWN" in wiped.stderr
    assert _page_records(tree) == [], "a held pass writes no attempt over a damaged inventory"
    assert json.loads(manifest.read_text(encoding="utf-8")) == stored, (
        "the inventory that recorded the lost attempts must survive the refusal"
    )


def test_an_interrupted_second_pass_resumes_once_its_manifest_is_re_derived(tmp_path, monkeypatch):
    """A crash inside an appending pass leaves one page's chairs at different ordinals.

    The stage holds until the manifest is re-derived; then the same command
    finishes the pass at its own ordinal, and every pair holds 1 and 2.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root)
    real_publish = attestatores.publish_page_testimonium

    def crash_after_first_write(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise RuntimeError("simulated process crash inside the second pass")

    monkeypatch.setattr(attestatores, "publish_page_testimonium", crash_after_first_write)
    monkeypatch.setattr(sys, "argv", _argv(run_root, "happy", "--attempt-ordinal", "2"))
    with pytest.raises(RuntimeError, match="simulated process crash"):
        attestatores.main()
    monkeypatch.undo()
    assert sum(record["payload"]["attempt_ordinal"] == 2 for record in _page_records(tree)) == 1

    held = invoke_stage(
        run_root, "retention", "happy", "pipeline/3_attestatores/run.py", attempt_ordinal=2
    )
    assert held.returncode == 3, held.stderr
    assert "UNKNOWN" in held.stderr

    tree.write_manifest(ATTESTATORES)
    run_attestatores(run_root, attempt_ordinal=2)

    by_pair: dict[tuple[str, str], set[int]] = {}
    for record in _page_records(tree):
        key = (record["subject_id"], record["payload"]["chair"])
        by_pair.setdefault(key, set()).add(record["payload"]["attempt_ordinal"])
    assert len(by_pair) == 6
    assert all(ordinals == {1, 2} for ordinals in by_pair.values()), by_pair
    assert tally(run_root)["state"] == "KNOWN"


def test_the_closing_tally_holds_when_the_folder_no_longer_accounts_for_every_pair(tmp_path):
    """The closing page/chair denominator: a missing pair is never a complete layer.

    The inventory is re-derived deliberately, so the divergent-inventory refusal
    is not what fires and the denominator check is the only thing left that can.
    """
    run_root, tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root)
    pages = sorted(
        {
            (record["payload"]["page_ordinal"], record["subject_id"])
            for record in _page_records(tree)
        }
    )
    assert tally(run_root, pages=pages)["state"] == "KNOWN"

    missing = _record_for(tree, page_ordinal=2, chair="attestator_2")
    _record_path(tree, missing).unlink()
    tree.write_manifest(ATTESTATORES)

    held = tally(run_root, pages=pages)
    assert held["hold"] is True
    assert "does not account for every sealed page/chair pair" in held["reason"]


# --- `witness_reported`: kept, and demoted ---------------------------------------


def test_a_confident_self_report_is_retained_verbatim_and_grades_nothing(tmp_path):
    """Spec 07's reason for `format_capabilities`.

    The Chandra chair's format cannot express uncertainty and claims high
    confidence anyway. The claim is retained exactly as made and reaches
    neither the outcome nor `content_health`.
    """
    run_root, tree = run_to_designator(tmp_path, "witness-capabilities")
    run_attestatores(run_root, "witness-capabilities")

    cannot_say_unsure = _record_for(tree, page_ordinal=1, chair="attestator_1")
    assert cannot_say_unsure["payload"]["format_capabilities"]["can_express_uncertainty"] is False
    assert cannot_say_unsure["payload"]["witness_reported"] == {"confidence": "high"}
    assert cannot_say_unsure["outcome"] == "read"

    silent = _record_for(tree, page_ordinal=1, chair="attestator_3")
    assert silent["payload"]["witness_reported"] is None, (
        "a witness that said nothing about itself has nothing invented for it"
    )
    assert silent["outcome"] == cannot_say_unsure["outcome"]
    assert set(silent["payload"]["content_health"]) == set(
        cannot_say_unsure["payload"]["content_health"]
    )


def test_no_self_report_can_reach_a_coverage_count_or_an_outcome_class():
    """`witness_coverage` takes chair outcomes and a floor; no parameter on that
    boundary could carry a witness's claim about itself to a count or a class."""
    parameters = inspect.signature(witness_coverage).parameters
    assert "witness_reported" not in parameters
    assert "witness_reported" not in inspect.signature(attestatores.content_health).parameters


@pytest.mark.parametrize(
    "fixture_key",
    ("witness_failure", "witness_empty", "witness_not_run", "witness_malformed"),
)
def test_a_witness_declaration_without_a_scenario_names_its_table_and_row(fixture_key):
    row = {"page_ordinal": 1, "chair": "attestator_3"}
    if fixture_key == "witness_malformed":
        row["reason"] = "provider body was malformed"
    context = SimpleNamespace(
        scenario="happy",
        witness_chairs=[],
        fixture={"page": [{"ordinal": 1}], fixture_key: [row]},
        registry=SimpleNamespace(config=SimpleNamespace(chairs={})),
    )

    with pytest.raises(
        SchemaRefusal,
        match=rf"fixture \[\[{fixture_key}\]\] row 1 has no scenario",
    ):
        attestatores.validate_declared_page_responses(context, {"attestator_3"})


def test_an_excluded_testimonium_without_an_approval_reference_is_refused_at_the_schema():
    """Spec 07 test 2: `excluded` exists only as a reference to a project-lead
    approval-record artifact; the word alone buys nothing."""
    subject = "page_0123456789abcdef"
    envelope = dict(
        run_id="r",
        subject_id=subject,
        stage=ATTESTATORES,
        kind=PAGE_KIND,
        config_digest="0" * 64,
        adapter_revision="test",
        inputs=[],
        payload={"chair": "attestator_1"},
        attempt=attempt_id(subject, "read:attestator_1", 1),
    )
    envelope["artifact_id"] = artifact_id(ATTESTATORES, PAGE_KIND, subject, envelope["attempt"])

    with pytest.raises(ApprovalRefusal, match="only the project lead approves an exclusion"):
        build_envelope(outcome="excluded", **envelope)

    # The generic envelope accepts a well-formed-looking identifier; it does not
    # resolve approval-record bytes, so this pins only the negative guarantee.
    approved = build_envelope(outcome="excluded", approval_ref="art_0123456789abcdef", **envelope)
    assert approved["approval_ref"] == "art_0123456789abcdef"


def test_an_actual_page_testimonium_identity_refuses_replacement_at_the_store_boundary(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root)
    original = _record_for(tree, page_ordinal=1, chair="attestator_1")
    before = _record_path(tree, original).read_bytes()
    changed = copy.deepcopy(original)
    changed["payload"]["payload"] = "different native witness output"
    changed["self_hash"] = self_hash(changed)

    with pytest.raises(IncompatibleReuse, match="immutable"):
        tree.publish_artifact(changed)
    assert _record_path(tree, original).read_bytes() == before


def test_every_page_testimonium_is_written_by_one_of_two_identity_bearing_writers():
    module = ast.parse(inspect.getsource(attestatores))
    publishers = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "publish":
            continue
        kind = next((item.value for item in node.keywords if item.arg == "kind"), None)
        if isinstance(kind, ast.Constant) and kind.value == PAGE_KIND:
            publishers.append(node.lineno)
    spans = []
    for writer in (
        attestatores.publish_page_testimonium,
        attestatores.publish_detector_page_testimonium,
    ):
        lines, start = inspect.getsourcelines(writer)
        spans.append(range(start, start + len(lines)))
    assert len(publishers) == 2
    assert all(any(line in span for span in spans) for line in publishers)
    assert all(any(line in span for line in publishers) for span in spans)


def test_parseable_native_payload_and_self_report_remain_separate():
    native = {"tokens": ["μ", "beta"], "layout": {"line": 4}, "uncertain": True}
    payload, self_report, _, health, problem = attestatores.prepared_response(
        {"payload": native, "witness_reported": {"confidence": "certain"}}
    )
    assert problem is None
    assert payload == native
    assert self_report == {"confidence": "certain"}
    assert health == attestatores.content_health(native, completed=True)
    _, _, _, changed_health, problem = attestatores.prepared_response(
        {"payload": native, "witness_reported": {"confidence": "unsure"}}
    )
    assert problem is None
    assert changed_health == health


def test_unrecordable_native_output_becomes_failed_without_replacement_text():
    native, self_report, capabilities, health, problem = attestatores.prepared_response(
        {"payload": "\ud800"}
    )
    assert native is None
    assert self_report is None
    assert capabilities is None
    assert health["recordable"] is False
    assert health["encoding"] == "invalid-or-unrecordable"
    assert "valid UTF-8" in problem


def test_a_deeply_nested_native_payload_becomes_failed_not_a_recursion_crash():
    """A response nested past Python's recursion limit is one `failed` attempt,
    never an uncaught RecursionError that takes the whole pass down."""
    nested = "leaf"
    for _ in range(5000):
        nested = [nested]

    problem = attestatores._native_problem(nested)
    assert problem is not None
    assert "nests deeper" in problem

    health = attestatores.content_health(nested, completed=True)
    assert health["recordable"] is False
    assert health["truncation_basis"] == problem

    native, self_report, capabilities, prepared_health, prepared_problem = (
        attestatores.prepared_response({"payload": nested})
    )
    assert native is None
    assert self_report is None
    assert capabilities is None
    assert prepared_health["recordable"] is False
    assert prepared_problem == problem

    reasonable = {"tokens": ["a", "b"], "layout": {"line": 4, "spans": [{"a": 1}, {"b": 2}]}}
    assert attestatores._native_problem(reasonable) is None


@pytest.mark.parametrize(
    ("native", "expected_type", "where"),
    ((1.5, "float", "payload"), ({"score": 0.5}, "object", "payload.score")),
)
def test_a_float_in_a_native_payload_is_a_failed_attempt_not_a_coerced_number(
    native, expected_type, where
):
    """The canonical writer refuses floats, so a witness returning one gets a
    `failed` attempt that says so, never a rounded or dropped number wearing
    `read`. The fixture builder cannot declare a float, so this is the boundary
    at which the claim can be checked."""
    payload, self_report, capabilities, health, problem = attestatores.prepared_response(
        {"payload": native}
    )

    assert payload is None
    assert self_report is None
    assert capabilities is None
    assert health["recordable"] is False
    assert health["native_type"] == expected_type
    assert problem == f"{where} has unsupported native type 'float'"
    attestatores.validate_content_health(payload, health)


def test_configured_never_attempted_seat_is_not_run_not_dead(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "not-run-witness")
    run_attestatores(run_root, "not-run-witness")
    record = _record_for(tree, page_ordinal=1, chair="attestator_3")
    assert record["outcome"] == "not-run"
    assert record["inputs"] == []
    assert record["payload"]["presented"] == {}
    assert record["payload"]["payload"] is None
    assert record["payload"]["provenance"]["chair_state"] == "configured"
    assert record["payload"]["provenance"]["receipt_ref"] is None


def test_a_crop_broken_after_the_designator_sealed_it_stops_at_that_boundary(tmp_path):
    """Crop bytes that moved after the boundary are tree damage: the Designator's
    seal reads every blob back, so this is refused before any page is witnessed."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    entry = next(
        entry for entry in tree.build_manifest(DESIGNATOR)["artifacts"] if entry["kind"] == "region"
    )
    region = tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
    tree.resolve(region["payload"]["image_path"]).write_bytes(b"broken crop bytes")

    result = invoke_stage(run_root, "retention", "happy", "pipeline/3_attestatores/run.py")

    assert result.returncode == 2, result.stderr
    assert "designator stage-seal" in result.stderr
    assert "named inventory no longer matches disk" in result.stderr
    assert _page_records(tree) == []


def test_malformed_one_witness_response_is_a_failed_attempt_that_does_not_hold_the_folder(
    tmp_path,
):
    """Spec 07's isolation bullet: one malformed response never kills the folder.

    The response is refused without repair, as one `failed` attempt with its
    reason; it is counted, so the tally stays KNOWN and the other chairs'
    readings of the same page reach the Perlector.
    """
    run_root, tree = run_to_designator(tmp_path, "malformed-witness")
    run_attestatores(run_root, "malformed-witness")
    records = _page_records(tree)
    assert len(records) == 6
    malformed = _record_for(tree, page_ordinal=1, chair="attestator_3")
    assert malformed["outcome"] == "failed"
    assert malformed["payload"]["payload"] is None
    assert malformed["payload"]["content_health"]["recordable"] is False
    assert malformed["payload"]["reason"]
    assert "�" not in str(malformed)
    assert sum(record["outcome"] == "read" for record in records) == 5
    counted = tally(run_root, "malformed-witness")
    assert counted["state"] == "KNOWN"
    assert counted["count"] == 6
    assert counted["hold"] is False


def test_malformed_capabilities_fail_one_attempt_without_aborting_other_chairs(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "malformed-capabilities")
    run_attestatores(run_root, "malformed-capabilities")
    records = _page_records(tree)
    assert len(records) == 6
    malformed = _record_for(tree, page_ordinal=1, chair="attestator_3")
    assert malformed["outcome"] == "failed"
    assert malformed["payload"]["payload"] == (
        "SYNTHETIC ACT ONE alpha beta\nSYNTHETIC ACT TWO delta epsiIon zeta eta"
    )
    assert malformed["payload"]["format_capabilities"] is None
    assert malformed["payload"]["content_health"]["recordable"] is True
    assert "format capabilities could not be retained" in malformed["payload"]["reason"]
    assert malformed["inputs"]
    assert sum(record["outcome"] == "read" for record in records) == 5
    assert tally(run_root, "malformed-capabilities")["state"] == "KNOWN"


CHURRO_PAGE_ONE = "SYNTHETIC ACT ONE alpha beta\nSYNTHETIC ACT TWO delta epsiIon zeta eta"
CHANDRA_PAGE_ONE = "SYNTHETIC ACT ONE alpha beta gamma\nSYNTHETIC ACT TWO delta epsilon zeta eta"
CHANDRA_RAW_RESPONSES = [
    '{"schema":"fixture-chandra-response.v1","markdown":"SYNTHETIC ACT ONE alpha beta gamma",'
    '"blocks":[{"bbox":[20.25,20.5,180,100.1]}]}',
    '{"schema":"fixture-chandra-response.v1","markdown":"SYNTHETIC ACT TWO delta epsilon zeta '
    'eta","blocks":[{"bbox":[20.25,120.5,180,220.1]}]}',
]


def _declare(monkeypatch, chair: str, row: dict) -> None:
    """Replace one page-1 declaration at the fixture-reader seam."""
    real = attestatores.declared_page_response

    def declared(context, page_ordinal, asked_chair, ordinal):
        if (page_ordinal, asked_chair, ordinal) == (1, chair, 1):
            return "testimony", {"page_ordinal": 1, "chair": chair, **row}
        return real(context, page_ordinal, asked_chair, ordinal)

    monkeypatch.setattr(attestatores, "declared_page_response", declared)


@pytest.mark.parametrize(
    ("chair", "row"),
    (
        ("attestator_3", {"payload": CHURRO_PAGE_ONE}),
        (
            "attestator_1",
            {"payload": CHANDRA_PAGE_ONE, "raw_responses": CHANDRA_RAW_RESPONSES},
        ),
    ),
)
def test_combined_unrecordable_witness_metadata_fails_one_attempt_without_holding_the_pass(
    tmp_path, monkeypatch, chair, row
):
    """Both metadata defects are retained in one failed attempt's reason, on
    each whole-page chair's own path, and the native text is still kept."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    _declare(
        monkeypatch,
        chair,
        {**row, "format_capabilities": "not an object", "witness_reported": "\ud800"},
    )
    monkeypatch.setattr(sys, "argv", _argv(run_root, "happy"))

    assert attestatores.main() == 0
    records = _page_records(tree)
    assert len(records) == 6
    malformed = _record_for(tree, page_ordinal=1, chair=chair)
    assert malformed["outcome"] == "failed"
    assert malformed["payload"]["payload"] == row["payload"]
    assert malformed["payload"]["format_capabilities"] is None
    assert malformed["payload"]["witness_reported"] is None
    reason = malformed["payload"]["reason"]
    assert "format capabilities could not be retained" in reason
    assert "self-report could not be retained" in reason
    if "raw_responses" in row:
        assert malformed["payload"]["raw_response_refs"]
    assert sum(record["outcome"] == "read" for record in records) == 5
    assert tally(run_root)["state"] == "KNOWN"


def test_chandra_malformed_confidence_alone_fails_one_attempt_and_keeps_capabilities(
    tmp_path, monkeypatch
):
    """A closed-ordinal violation is refused even with valid format capabilities,
    and a clean capabilities record survives untouched."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    _declare(
        monkeypatch,
        "attestator_1",
        {
            "payload": CHANDRA_PAGE_ONE,
            "raw_responses": CHANDRA_RAW_RESPONSES,
            "witness_reported": {"confidence": "extremely-confident"},
        },
    )
    monkeypatch.setattr(sys, "argv", _argv(run_root, "happy"))

    assert attestatores.main() == 0
    malformed = _record_for(tree, page_ordinal=1, chair="attestator_1")
    assert malformed["outcome"] == "failed"
    assert malformed["payload"]["payload"] == CHANDRA_PAGE_ONE
    assert malformed["payload"]["format_capabilities"] == attestatores.DEFAULT_FORMAT_CAPABILITIES
    assert malformed["payload"]["witness_reported"] is None
    assert "self-report could not be retained" in malformed["payload"]["reason"]
    assert tally(run_root)["state"] == "KNOWN"


def test_a_page_record_refuses_a_witness_reported_confidence_outside_the_closed_set(tmp_path):
    """The one validator both the writer and the tally call; closing it here closes
    it for a retained record a resumed pass would otherwise carry forward."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root)
    record = _record_for(tree, page_ordinal=1, chair="attestator_3")
    forged = {**record["payload"], "witness_reported": {"confidence": "extremely-confident"}}
    with pytest.raises(SchemaRefusal, match="closed ordinal set"):
        attestatores.validate_page_testimonium_payload(forged, testimonium_id=record["artifact_id"])


def test_witness_metadata_cannot_rewrite_native_content_health():
    native = "verbatim native response"
    expected = attestatores.content_health(native, completed=True)

    _, _, capabilities, self_report_health, self_report_problem = attestatores.prepared_response(
        {"payload": native, "witness_reported": "\ud800"}
    )
    assert capabilities == attestatores.DEFAULT_FORMAT_CAPABILITIES
    assert self_report_health == expected
    assert "self-report could not be retained" in self_report_problem

    _, _, capabilities, capability_health, capability_problem = attestatores.prepared_response(
        {"payload": native, "format_capabilities": "not an object"}
    )
    assert capabilities is None
    assert capability_health == expected
    assert "format capabilities could not be retained" in capability_problem


def test_a_reading_that_claims_its_own_channel_was_unrecordable_is_unknown(tmp_path):
    """A record claiming to be a reading while recording that nothing could keep
    what it read is not a reading; the tally refuses it for that alone."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root)
    record = _record_for(tree, page_ordinal=2, chair="attestator_1")
    assert record["outcome"] == "read"
    changed = copy.deepcopy(record)
    changed["payload"]["payload"] = None
    changed["payload"]["content_health"] = {
        "native_type": "unrecordable",
        "encoding": "invalid-or-unrecordable",
        "recordable": False,
        "empty": None,
        "blank": None,
        "truncated": None,
        "characters": None,
        "truncation_basis": "the provider body could not be retained",
    }
    _reseal(tree, record, changed)

    held = tally(run_root)

    assert held["state"] == "UNKNOWN"
    assert held["count"] is None
    assert held["hold"] is True
    assert "is not a reading" in held["reason"]


def test_an_unrecordable_channel_may_not_assert_facts_nothing_measured(tmp_path):
    """A `recordable=False` record must not also claim a character count, a
    truncation state, a valid encoding or a native payload."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root)
    record = _record_for(tree, page_ordinal=2, chair="attestator_1")
    fabricated = copy.deepcopy(record)
    fabricated["outcome"] = "failed"
    fabricated["payload"]["reason"] = "the provider response was refused without repair"
    fabricated["payload"]["content_health"] = {
        "native_type": "string",
        "encoding": "utf-8-json-native",
        "recordable": False,
        "empty": False,
        "blank": False,
        "truncated": False,
        "characters": 9999,
        "truncation_basis": "trusted-response-boundary",
    }
    _reseal(tree, record, fabricated)

    held = tally(run_root)

    assert held["state"] == "UNKNOWN"
    assert held["hold"] is True
    assert "retains a native payload" in held["reason"]


def test_a_failed_attempt_with_an_unrecordable_channel_and_no_reason_is_unknown(tmp_path):
    """An absence with no reason is the silent loss this stage exists to refuse."""
    run_root, tree = run_to_designator(tmp_path, "malformed-witness")
    run_attestatores(run_root, "malformed-witness")
    record = _record_for(tree, page_ordinal=1, chair="attestator_3")
    changed = copy.deepcopy(record)
    del changed["payload"]["reason"]
    _reseal(tree, record, changed)

    held = tally(run_root, "malformed-witness")

    assert held["state"] == "UNKNOWN"
    assert held["hold"] is True
    assert "records no reason" in held["reason"]


def test_a_failed_attempt_with_recordable_native_evidence_still_requires_a_reason(tmp_path):
    run_root, tree = run_to_designator(tmp_path, "malformed-capabilities")
    run_attestatores(run_root, "malformed-capabilities")
    record = _record_for(tree, page_ordinal=1, chair="attestator_3")
    assert record["payload"]["content_health"]["recordable"] is True
    changed = copy.deepcopy(record)
    del changed["payload"]["reason"]
    _reseal(tree, record, changed)

    retry = invoke_stage(
        run_root,
        "retention",
        "malformed-capabilities",
        "pipeline/3_attestatores/run.py",
        attempt_ordinal=2,
    )

    assert retry.returncode == 3
    assert "records no reason" in retry.stderr
    assert all(item["payload"]["attempt_ordinal"] == 1 for item in _page_records(tree))


@pytest.mark.parametrize("damage", ("absent", "garbled", "truncated", "recursion"))
def test_damaged_attempt_tally_is_unknown_and_refuses_to_add_a_replacement(tmp_path, damage):
    """`recursion` pins the manifest read `attempt_tally` does through its own
    `json.loads`: deep nesting must become UNKNOWN and hold, not a traceback."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root)
    manifest_path = tree.resolve(tree.manifest_path(ATTESTATORES))
    original = manifest_path.read_bytes()
    if damage == "absent":
        manifest_path.unlink()
    elif damage == "garbled":
        manifest_path.write_bytes(b"{")
    elif damage == "recursion":
        nesting = 30_000
        deep_text = f'{{"deep": {"[" * nesting}"leaf"{"]" * nesting}}}'
        try:
            json.loads(deep_text)
        except RecursionError:
            pass
        else:
            pytest.skip(
                f"this interpreter's JSON scanner absorbs {nesting} levels, so the "
                "guarded path is unreachable here and this case proves nothing"
            )
        manifest_path.write_bytes(deep_text.encode())
    else:
        manifest_path.write_bytes(original[:-1])

    held = tally(run_root)
    assert held["state"] == "UNKNOWN"
    assert held["count"] is None
    assert held["hold"] is True
    retry = invoke_stage(
        run_root, "retention", "happy", "pipeline/3_attestatores/run.py", attempt_ordinal=2
    )
    assert retry.returncode == 3
    assert "UNKNOWN" in retry.stderr
    assert all(record["payload"]["attempt_ordinal"] == 1 for record in _page_records(tree))


# --- An accounting imbalance is fatal, and a hold is not the same fact ------------


def test_an_accounting_imbalance_is_fatal_and_never_becomes_a_hold(tmp_path, monkeypatch):
    """A hold says the count is unknown; an accounting imbalance says the
    partition itself is broken, and nothing may catch it and carry on."""
    run_root, _tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root)
    assert tally(run_root)["state"] == "KNOWN"

    def imbalanced(*_args, **_kwargs):
        raise attestatores.FatalAccounting("a unit is in no terminal set")

    monkeypatch.setattr(attestatores, "latest_attempt", imbalanced)

    with pytest.raises(attestatores.FatalAccounting, match="no terminal set"):
        tally(run_root)


def test_an_outer_manifest_accounting_imbalance_is_fatal_and_never_becomes_a_hold(
    tmp_path, monkeypatch
):
    run_root, _tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root)
    context = stage_context(run_root)
    proposals = attestatores.sealed_proposal_regions(context)
    assert attestatores.attempt_tally(context, proposals=proposals)["state"] == "KNOWN"

    def imbalanced(_stage):
        raise attestatores.FatalAccounting("the outer manifest partition is broken")

    monkeypatch.setattr(context.tree, "build_manifest", imbalanced)

    with pytest.raises(attestatores.FatalAccounting, match="outer manifest partition"):
        attestatores.attempt_tally(context, proposals=proposals)


def test_a_fatal_closing_tally_does_not_publish_a_completion_seal(tmp_path, monkeypatch):
    run_root, tree = run_to_designator(tmp_path, "happy")

    def imbalanced(*_args, **_kwargs):
        raise attestatores.FatalAccounting("the closing partition is broken")

    monkeypatch.setattr(attestatores, "attempt_tally", imbalanced)
    monkeypatch.setattr(sys, "argv", _argv(run_root, "happy"))

    with pytest.raises(attestatores.FatalAccounting, match="closing partition"):
        attestatores.main()

    # The premise, asserted: the seal was withheld after a full pass, not
    # missing because the pass never happened.
    assert len(_page_records(tree)) == 6
    assert not any(
        entry["kind"] == "stage-seal" for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
    )


def test_main_does_not_turn_a_fatal_manifest_outcome_into_a_hold(tmp_path):
    """The existing-attempt check builds the manifest before the tally; its
    FatalAccounting must reach the stage boundary as fatal, not as a hold."""
    run_root, tree = run_to_designator(tmp_path, "happy")
    run_attestatores(run_root)
    record = _record_for(tree, page_ordinal=1, chair="attestator_1")
    damaged = copy.deepcopy(record)
    damaged["outcome"] = "outside-the-closed-vocabulary"
    _reseal(tree, record, damaged, manifest=False)

    retry = invoke_stage(
        run_root, "retention", "happy", "pipeline/3_attestatores/run.py", attempt_ordinal=2
    )

    assert retry.returncode == 2
    assert "outside-the-closed-vocabulary" in retry.stderr
    assert "attempt tally UNKNOWN" not in retry.stderr
