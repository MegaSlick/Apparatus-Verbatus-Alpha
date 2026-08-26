"""The operator describes staged-run boundaries without choosing for a person."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts import stages as stage_names
from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import ApprovalRefusal
from common.contracts.stages import STAGES
from common.runtree.store import RunTree
from common.stage import ALWAYS_HELD_BOUNDARIES, RUN_MODES, held_advance_boundaries
from operations.operator.errors import ErrorCode, OperatorError

from . import advance, cli, review

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def _run(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "runs"
    completed = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-id",
            "staged",
            "--run-root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return root, "staged"


@pytest.mark.parametrize(
    ("mode", "stage", "first", "last", "expected"),
    (
        ("manual", "designator", None, None, {"designator"}),
        # Attestatores holds before the driver consults mode, so a spanning
        # semi range holds there as well as at its declared endpoint.
        ("semi", "perlector", "designator", "perlector", {"attestatores", "perlector"}),
        ("semi", "designator", "door", "designator", {"designator"}),
        # Armarium's own terminal report can hold before the driver consults
        # mode too (F-R21C1), so auto can advance it just like Attestatores.
        ("auto", "armarium", None, None, {"attestatores", "armarium"}),
    ),
)
def test_staged_mode_semantics_name_every_boundary_that_can_wait(
    mode: str, stage: str, first: str | None, last: str | None, expected: set[str]
) -> None:
    assert held_advance_boundaries(mode, stage=stage, from_stage=first, to_stage=last) == expected


def test_semi_mode_refuses_an_intermediate_boundary_that_cannot_hold() -> None:
    with pytest.raises(
        ApprovalRefusal,
        match="can require a person-held advance at attestatores, perlector, not designator",
    ):
        advance.held_boundaries_for_mode(
            "semi", stage="designator", from_stage="designator", to_stage="perlector"
        )


def _mode_independent_held_stages() -> frozenset[str]:
    """Every stage the driver returns EXIT_HELD for without consulting ``mode``.

    Derive the set from `run_sequence` rather than restating the console's
    cross-module claim in a second hand-written list.

    Two distinct shapes hold without consulting ``mode``, and this function must
    catch both or the claim it derives is not the one the driver actually keeps.
    The Attestatores' `if name == ATTESTATORES and result == EXIT_HELD: ...
    return EXIT_HELD` is an ordinary branch the walk below finds. Armarium's own
    terminal hold is not: `run_sequence` ends with a bare
    `return EXIT_COMPLETE if status == "complete" else EXIT_HELD`, reached only
    once the loop has run every member and the earlier guard has already refused
    every selection whose last member is not armarium -- so it is unconditional
    on `mode` without a single `if name == ...` branch for the walk to match.
    Before this closed (F-R21C1), that gap meant `ALWAYS_HELD_BOUNDARIES` could
    silently drop Armarium and this very test would still report green, because
    both the production set and its derivation shared the identical blind spot.
    """

    module = ast.parse(ORCHESTRATOR.read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "run_sequence"
    )
    held: set[str] = set()
    for branch in ast.walk(function):
        if not isinstance(branch, ast.If):
            continue
        returns_held = any(
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Name)
            and node.value.id == "EXIT_HELD"
            for statement in branch.body
            for node in ast.walk(statement)
        )
        if not returns_held:
            continue
        if any(isinstance(node, ast.Name) and node.id == "mode" for node in ast.walk(branch.test)):
            continue  # a stop this selection's mode chose, not one every mode takes
        for comparison in ast.walk(branch.test):
            if (
                not isinstance(comparison, ast.Compare)
                or not isinstance(comparison.left, ast.Name)
                or comparison.left.id != "name"
                or not isinstance(comparison.ops[0], ast.Eq)
            ):
                continue
            operand = comparison.comparators[0]
            if isinstance(operand, ast.Constant):
                held.add(operand.value)
            elif isinstance(operand, ast.Name):
                held.add(getattr(stage_names, operand.id))
    tail = function.body[-1]
    if (
        isinstance(tail, ast.Return)
        and isinstance(tail.value, ast.IfExp)
        and isinstance(tail.value.orelse, ast.Name)
        and tail.value.orelse.id == "EXIT_HELD"
    ):
        held.add(stage_names.ARMARIUM)
    return frozenset(held)


def test_the_always_held_set_is_exactly_the_drivers_own_mode_independent_stops() -> None:
    assert _mode_independent_held_stages() == ALWAYS_HELD_BOUNDARIES


def test_the_attestatores_holds_after_it_has_already_sealed_its_boundary() -> None:
    """Attestatores must seal before its mode-independent hold can be advanced."""

    source = (ROOT / "pipeline" / "3_attestatores" / "run.py").read_text(encoding="utf-8")
    sealed_at = source.index("context.seal_boundary()")
    tally_hold = source.index("Attestatores attempt tally UNKNOWN", sealed_at)
    assert sealed_at < tally_hold


def test_every_advanceable_boundary_is_a_driver_member_in_the_same_order() -> None:
    """`held_advance_boundaries` indexes `STAGES`; the driver indexes its own sequence.

    A semi range is resolved independently over `SEQUENCE_NAMES` and `STAGES`,
    so their boundary order must agree. `recovery` remains a legal driver member
    but has no stage program or completion boundary.
    """

    from pipeline.orchestrator.run import SEQUENCE_NAMES

    assert tuple(name for name in SEQUENCE_NAMES if name in STAGES) == STAGES
    assert set(SEQUENCE_NAMES) - set(STAGES) == {"recovery"}


def test_a_range_endpoint_with_no_boundary_names_the_boundaries_that_do() -> None:
    with pytest.raises(ApprovalRefusal) as refusal:
        advance.held_boundaries_for_mode(
            "semi", stage="recensor", from_stage="designator", to_stage="recovery"
        )

    detail = str(refusal.value)
    assert "'recovery'" in detail and "owns no stage completion boundary" in detail
    assert all(boundary in detail for boundary in STAGES)


def test_auto_mode_shows_boundary_state_then_refuses_an_advance_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Designator cannot hold in auto mode, unlike Attestatores and Armarium."""

    run_root, run_id = _run(tmp_path)

    with pytest.raises(OperatorError) as refusal:
        cli._advance_with_confirmation(
            run_root,
            run_id,
            "designator",
            reason="operator reviewed the completed run",
            workspace=ROOT,
            mode="auto",
        )

    assert "auto mode" in (refusal.value.detail or "").lower()
    rendered = capsys.readouterr().out
    assert "Current boundary state" in rendered
    assert "designator: seal" in rendered


def test_semi_mode_confirmation_binds_the_displayed_last_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_root, run_id = _run(tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "perlector",
        reason="operator reviewed the range endpoint",
        workspace=ROOT,
        mode="semi",
        from_stage="designator",
        to_stage="perlector",
    )

    rendered = capsys.readouterr().out
    assert "Semi mode runs the inclusive range designator through perlector" in rendered
    assert (
        "This declared selection can require a person-held advance at: attestatores, perlector."
        in rendered
    )
    assert "Sealed evidence summary:" in rendered
    assert "Advance record:" in rendered


def test_manual_mode_confirmation_binds_the_named_boundary_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_root, run_id = _run(tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "designator",
        reason="operator reviewed the manual boundary",
        workspace=ROOT,
        mode="manual",
    )

    rendered = capsys.readouterr().out
    assert "Manual mode runs designator alone and passes nothing." in rendered
    assert "This declared selection can require a person-held advance at: designator." in rendered
    assert "Sealed evidence summary:" in rendered
    assert "Advance record:" in rendered


def test_semi_mode_refuses_an_intermediate_boundary_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, run_id = _run(tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)
    receipts = RunTree(run_root, run_id).root / "receipts" / "sha256"
    before = set(receipts.glob("*.json"))

    with pytest.raises(OperatorError) as refusal:
        cli._advance_with_confirmation(
            run_root,
            run_id,
            "designator",
            reason="operator reviewed the intermediate boundary",
            workspace=ROOT,
            mode="semi",
            from_stage="designator",
            to_stage="perlector",
        )

    assert "person-held advance at attestatores, perlector, not designator" in (
        refusal.value.detail or ""
    )
    after = set(receipts.glob("*.json"))
    assert after == before


def test_a_boundary_resealed_between_presentation_and_confirmation_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The typed digest binds the shown seal even if it changes during the prompt."""

    run_root, run_id = _run(tmp_path)
    tree = RunTree(run_root, run_id)
    receipts = tree.root / "receipts" / "sha256"
    before = set(receipts.glob("*.json"))
    shown: list[str] = []

    def reseal_then_type(phrase: str) -> str:
        shown.append(phrase)
        seal, _ = advance.sealed_boundary(tree, "armarium")
        record = tree.read_artifact("armarium", "stage-seal", seal["artifact_id"])
        record["payload"] = {
            **record["payload"],
            "census": [
                *record["payload"]["census"],
                {"kind": "probe", "outcome": "sealed", "count": 1},
            ],
        }
        record["self_hash"] = self_hash(record)
        tree.resolve(tree.artifact_path("armarium", "stage-seal", seal["artifact_id"])).write_bytes(
            canonical_bytes(record)
        )
        return phrase

    monkeypatch.setattr(cli, "_typed_advance_confirmation", reseal_then_type)

    with pytest.raises(OperatorError) as refusal:
        cli._advance_with_confirmation(
            run_root,
            run_id,
            "armarium",
            reason="operator reviewed the boundary that then moved",
            workspace=ROOT,
            mode="manual",
        )

    assert "changed after it was shown for confirmation" in (refusal.value.detail or "")
    assert len(shown) == 1
    after = set(receipts.glob("*.json"))
    assert after == before


def test_typed_grant_binds_the_exact_reason_written_to_the_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker may not record decision text the operator never confirmed."""

    run_root, run_id = _run(tmp_path)
    reason = 'reviewed "census"\nwith the page image'
    shown: list[str] = []

    def capture_and_confirm(phrase: str) -> str:
        shown.append(phrase)
        return phrase

    monkeypatch.setattr(cli, "_typed_advance_confirmation", capture_and_confirm)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "armarium",
        reason=reason,
        workspace=ROOT,
        mode="manual",
    )

    assert len(shown) == 1
    assert 'for reason "reviewed \\"census\\"\\nwith the page image"' in shown[0]
    assert "\n" not in shown[0]
    records = review.ReadOnlyRun(run_root, run_id).projection().advance_records
    written = [record for record in records if record["reason"] == reason]
    assert len(written) == 1


def test_auto_mode_never_solicits_a_typed_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ineligible auto boundary must refuse before asking for a decision."""

    run_root, run_id = _run(tmp_path)
    solicited: list[str] = []

    def record_then_fail(phrase: str) -> str:
        solicited.append(phrase)
        raise AssertionError(f"auto mode solicited a confirmation: {phrase!r}")

    monkeypatch.setattr(cli, "_typed_advance_confirmation", record_then_fail)

    with pytest.raises(OperatorError) as refusal:
        cli._advance_with_confirmation(
            run_root,
            run_id,
            "designator",
            reason="operator reviewed the completed run",
            workspace=ROOT,
            mode="auto",
        )

    assert solicited == []
    assert "auto mode" in (refusal.value.detail or "").lower()


def test_auto_mode_can_advance_the_boundary_that_may_hold_in_every_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Auto may advance Attestatores because its sealed hold precedes mode handling."""

    run_root, run_id = _run(tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "attestatores",
        reason="operator reviewed the held witness boundary",
        workspace=ROOT,
        mode="auto",
    )

    rendered = capsys.readouterr().out
    assert (
        "This declared selection can require a person-held advance at: armarium, attestatores."
        in rendered
    )
    assert "This invocation waits" not in rendered
    assert "Advance record:" in rendered


def test_auto_mode_can_advance_the_armarium_boundary_that_may_hold_in_every_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Auto may advance Armarium: its terminal report can hold without consulting mode.

    Pins F-R21C1 closed: `ALWAYS_HELD_BOUNDARIES` used to name only Attestatores,
    so `verbatus advance --mode auto --stage armarium` refused every request for
    this boundary even when a real auto run stopped there, because
    `run_sequence`'s tail returns `EXIT_HELD` for a non-complete terminal report
    with no reference to `mode` at all -- the identical mode-independent shape
    as the Attestatores' hold, just spelled as a bare ternary the branch-shaped
    AST scan below could not see.
    """

    run_root, run_id = _run(tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "armarium",
        reason="operator reviewed the terminal boundary",
        workspace=ROOT,
        mode="auto",
    )

    rendered = capsys.readouterr().out
    assert (
        "This declared selection can require a person-held advance at: armarium, attestatores."
        in rendered
    )
    assert "Advance record:" in rendered


def test_an_unvalidated_mode_selection_states_no_boundary_before_it_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An invalid semi range must not be presented as an established boundary claim."""

    run_root, run_id = _run(tmp_path)

    with pytest.raises(OperatorError) as refusal:
        cli._advance_with_confirmation(
            run_root,
            run_id,
            "perlector",
            reason="operator forgot the range",
            workspace=ROOT,
            mode="semi",
        )

    assert "needs both the first and last stage" in (refusal.value.detail or "")
    rendered = capsys.readouterr().out
    assert "waits at" not in rendered
    assert "None" not in rendered
    assert "Current boundary state" in rendered


def test_unreadable_boundary_evidence_is_refused_not_reported_as_unsealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A damaged seal is evidence of damage, not evidence that no seal exists."""

    run_root, run_id = _run(tmp_path)
    tree = RunTree(run_root, run_id)
    seal, _ = advance.sealed_boundary(tree, "designator")
    tree.resolve(tree.artifact_path("designator", "stage-seal", seal["artifact_id"])).write_text(
        "not json", encoding="utf-8"
    )
    monkeypatch.setattr(
        cli,
        "_typed_advance_confirmation",
        lambda phrase: pytest.fail(f"damaged evidence reached confirmation: {phrase}"),
    )

    with pytest.raises(OperatorError) as refusal:
        cli._advance_with_confirmation(
            run_root,
            run_id,
            "armarium",
            reason="operator must see the damaged earlier boundary",
            workspace=ROOT,
            mode="manual",
        )

    assert refusal.value.code == ErrorCode.ADVANCE_REFUSED
    assert "could not read designator's stored completion seal" in (refusal.value.detail or "")
    rendered = capsys.readouterr().out
    assert "designator: no stored completion seal" not in rendered


def test_missing_earlier_seal_in_a_later_sealed_chain_is_refused_as_lost_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deleted seal in a completed chain is not an ordinary unstarted stage."""

    run_root, run_id = _run(tmp_path)
    tree = RunTree(run_root, run_id)
    seal, _ = advance.sealed_boundary(tree, "designator")
    tree.resolve(tree.artifact_path("designator", "stage-seal", seal["artifact_id"])).unlink()
    monkeypatch.setattr(
        cli,
        "_typed_advance_confirmation",
        lambda phrase: pytest.fail(f"missing evidence reached confirmation: {phrase}"),
    )

    with pytest.raises(OperatorError) as refusal:
        cli._advance_with_confirmation(
            run_root,
            run_id,
            "armarium",
            reason="operator must see the broken seal chain",
            workspace=ROOT,
            mode="manual",
        )

    assert refusal.value.code == ErrorCode.ADVANCE_REFUSED
    detail = refusal.value.detail or ""
    assert "designator has no completion seal although later stage attestatores is sealed" in detail
    assert "evidence is missing, not merely unfinished" in detail
    assert "designator: no stored completion seal" not in capsys.readouterr().out


def test_the_advance_presentation_never_phrases_a_recommendation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The console may project facts but must never recommend a boundary."""

    run_root, run_id = _run(tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "designator",
        reason="operator reviewed the boundary",
        workspace=ROOT,
        mode="manual",
    )

    rendered = capsys.readouterr().out
    lines = [line.lower() for line in rendered.splitlines() if "not a recommendation" not in line]
    for phrasing in (
        "recommend",
        "suggest",
        "advise",
        "you should",
        "ready to",
        "safe to",
        "looks ",
        "best ",
        "prefer",
    ):
        assert not any(phrasing in line for line in lines), phrasing


def test_the_double_click_route_names_every_legal_value_it_asks_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person at the double-click window has no `--help` to consult.

    `--stage`, `--from-stage`, and `--to-stage` are checked against a closed
    list the parser never shows on this route, so a prompt that does not name
    the boundaries asks the operator to guess `ink-map` against `ink_map` and
    then refuses the spelling it never offered.
    """

    prompts: list[str] = []
    answers = iter(
        ("advance", "/runs", "staged", "perlector", "reviewed", "semi", "designator", "perlector")
    )

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", ask)

    arguments = cli._interactive_arguments()

    assert arguments == [
        "advance",
        "--run-root",
        "/runs",
        "--run-id",
        "staged",
        "--stage",
        "perlector",
        "--reason",
        "reviewed",
        "--mode",
        "semi",
        "--from-stage",
        "designator",
        "--to-stage",
        "perlector",
    ]
    cli.build_parser().parse_args(arguments)

    boundary_prompts = [
        prompt for prompt in prompts if "one of:" in prompt and "invocation mode" not in prompt
    ]
    assert len(boundary_prompts) == 3
    for prompt in boundary_prompts:
        assert all(boundary in prompt for boundary in STAGES)
    mode_prompt = next(prompt for prompt in prompts if "invocation mode" in prompt)
    assert all(mode in mode_prompt for mode in RUN_MODES)


@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        ("manual", "manual mode names one stage, not a range"),
        ("auto", "auto mode names no held range"),
    ),
)
def test_a_range_given_to_a_rangeless_mode_is_refused_not_ignored(mode: str, expected: str) -> None:
    """Silently dropping the range would make two different invocations one.

    `--mode manual --from-stage designator --to-stage perlector` describes a run
    that does not exist. Ignoring the endpoints would advance the named stage
    anyway and record a decision about a selection nobody made.
    """

    with pytest.raises(ApprovalRefusal, match=expected):
        advance.held_boundaries_for_mode(
            mode, stage="perlector", from_stage="designator", to_stage="perlector"
        )


def test_the_declared_mode_is_presented_as_a_declaration_not_a_read_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--mode` is unverifiable, and the surface has to say so.

    No run-tree record carries invocation mode, so the operator's declaration
    must remain distinct from seal facts read from the tree.
    """

    run_root, run_id = _run(tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "designator",
        reason="operator reviewed the boundary",
        workspace=ROOT,
        mode="manual",
    )

    rendered = capsys.readouterr().out
    assert "Staged invocation mode, as you declared it: manual." in rendered
    assert "records no invocation mode" in rendered
