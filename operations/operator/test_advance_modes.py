"""The operator describes staged-run boundaries without choosing for a person."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import ApprovalRefusal
from common.contracts.stages import STAGES
from common.runtree.store import RunTree
from common.stage import ALWAYS_HELD_BOUNDARIES, EXIT_HELD, RUN_MODES, held_advance_boundaries
from operations.operator.errors import ErrorCode, OperatorError

from . import advance, cli, review

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def _run(
    orchestrated_run, tmp_path: Path, *, scenario: str = "page-unbroken", expected_exit: int = 0
) -> tuple[Path, str]:
    return orchestrated_run(tmp_path / "runs", "staged", scenario, expected_exit), "staged"


@pytest.mark.parametrize(
    ("mode", "stage", "first", "last", "expected"),
    (
        ("manual", "designator", None, None, {"designator"}),
        # Attestatores holds before the driver consults mode, so a spanning
        # semi range holds there as well as at its declared endpoint.
        ("semi", "perlector", "designator", "perlector", {"attestatores", "perlector"}),
        ("semi", "designator", "door", "designator", {"designator"}),
        # Armarium's own terminal report can hold before the driver consults
        # mode too, so auto can advance it just like Attestatores, and a Recensor
        # that holds anything stops every mode before the Archetypus.
        ("auto", "armarium", None, None, {"attestatores", "recensor", "armarium"}),
        ("semi", "recensor", "perlector", "archetypus", {"recensor", "archetypus"}),
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


def test_review_run_seals_attestatores_before_the_terminal_hold(
    orchestrated_run, tmp_path: Path
) -> None:
    root, run_id = _run(orchestrated_run, tmp_path, scenario="page-review", expected_exit=3)
    tree = RunTree(root, run_id)
    assert any(
        entry["kind"] == "stage-seal" for entry in tree.build_manifest("attestatores")["artifacts"]
    )


def test_mode_independent_driver_holds_match_advance_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each stage exits held, followed by its successor in the sequence, in auto mode.

    A stage the driver stops at whatever the mode is one whose successor is
    never invoked. The Recensor's hold is read from its records before the
    Archetypus, so the probe says the Recensor holds exactly when it exited held.
    """
    from pipeline.orchestrator import run as driver

    stopped: set[str] = set()
    monkeypatch.setattr(driver, "checkpoint", lambda *_args: None)
    monkeypatch.setattr(
        driver, "_run_tree", lambda _args: type("Tree", (), {"read_run": lambda self: {}})()
    )
    monkeypatch.setattr(driver, "_require_sealed_hard_failure_policy", lambda *_args: None)
    monkeypatch.setattr(driver, "verify_final_seal", lambda *_args: {})
    monkeypatch.setattr(driver, "terminal_report", lambda _export: ("partial", []))
    monkeypatch.setattr(driver, "report_held_recensor", lambda *_args: None)

    for index, stage in enumerate(driver.SEQUENCE_NAMES):
        visited: list[str] = []

        def invoke(
            program: str, _args: object, *, visited: list[str] = visited, stage: str = stage
        ) -> int:
            name = next(name for name, path in driver.STAGE_PROGRAMS.items() if path == program)
            visited.append(name)
            return EXIT_HELD if name == stage else 0

        def recensor_holds(_args: object, *, visited: list[str] = visited, stage: str = stage):
            held = stage == "recensor" and "recensor" in visited
            return [{"subject_id": "probe", "what": "probe", "hold_codes": []}] if held else []

        monkeypatch.setattr(driver, "invoke", invoke)
        monkeypatch.setattr(driver, "recensor_holds", recensor_holds)
        args = type("Args", (), {"run_root": str(tmp_path), "run_id": "probe"})()
        names = driver.SEQUENCE_NAMES[index : index + 2]
        result = driver.run_sequence(args, names, "auto", {})
        if result == EXIT_HELD and visited == [stage]:
            stopped.add(stage)

    assert stopped == ALWAYS_HELD_BOUNDARIES


def test_advance_accepts_the_recensor_boundary_in_auto_mode() -> None:
    """An unattended run stops at a held Recensor, so a person may advance it there."""
    held = advance.held_boundaries_for_mode("auto", stage="recensor")
    assert "recensor" in held


def test_attestatores_final_tally_hold_seals_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            "--from",
            "door",
            "--to",
            "designator",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    path = ROOT / "pipeline" / "3_attestatores" / "run.py"
    spec = importlib.util.spec_from_file_location("attestatores_tally_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "attempt_tally", lambda *_args, **_kwargs: {"hold": True, "reason": "probe"}
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(path),
            "--fixture-root",
            "proof",
            "--scenario",
            "happy",
            "--run-id",
            "staged",
            "--run-root",
            str(root),
        ],
    )

    assert module.main() == EXIT_HELD
    tree = RunTree(root, "staged")
    assert any(
        row["kind"] == "stage-seal" for row in tree.build_manifest("attestatores")["artifacts"]
    )


def test_every_advanceable_boundary_is_a_driver_member_in_the_same_order() -> None:
    """`held_advance_boundaries` indexes `STAGES`; the driver indexes its own sequence.

    A semi range is resolved independently over `SEQUENCE_NAMES` and `STAGES`,
    so their boundary order must agree, member for member.
    """

    from pipeline.orchestrator.run import SEQUENCE_NAMES

    assert SEQUENCE_NAMES == STAGES


def test_a_range_endpoint_with_no_boundary_names_the_boundaries_that_do() -> None:
    with pytest.raises(ApprovalRefusal) as refusal:
        advance.held_boundaries_for_mode(
            "semi", stage="recensor", from_stage="designator", to_stage="not-a-stage"
        )

    detail = str(refusal.value)
    assert "'not-a-stage'" in detail and "owns no stage completion boundary" in detail
    assert all(boundary in detail for boundary in STAGES)


def test_auto_mode_shows_boundary_state_then_refuses_an_advance_record(
    orchestrated_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Designator cannot hold in auto mode, unlike Attestatores and Armarium."""

    run_root, run_id = _run(orchestrated_run, tmp_path)

    with pytest.raises(OperatorError) as refusal:
        cli._advance_with_confirmation(
            run_root,
            run_id,
            "designator",
            reason="operator reviewed the completed run",
            mode="auto",
        )

    assert "auto mode" in (refusal.value.detail or "").lower()
    rendered = capsys.readouterr().out
    assert "Current boundary state" in rendered
    assert "designator: seal" in rendered


def test_semi_mode_confirmation_binds_the_displayed_last_boundary(
    orchestrated_run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_root, run_id = _run(orchestrated_run, tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "perlector",
        reason="operator reviewed the range endpoint",
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
    orchestrated_run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_root, run_id = _run(orchestrated_run, tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "designator",
        reason="operator reviewed the manual boundary",
        mode="manual",
    )

    rendered = capsys.readouterr().out
    assert "Manual mode runs designator alone and passes nothing." in rendered
    assert "This declared selection can require a person-held advance at: designator." in rendered
    assert "Sealed evidence summary:" in rendered
    assert "Advance record:" in rendered


def test_a_supplied_surface_records_the_advance_for_status(
    orchestrated_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing `surface` wires the advance into status; omitting it costs nothing."""
    from .surface import OperatorSurface

    run_root, run_id = _run(orchestrated_run, tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)
    surface = OperatorSurface(ROOT, tmp_path / "operator-state", present=lambda _line="": None)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "designator",
        reason="operator reviewed the manual boundary",
        surface=surface,
        mode="manual",
    )

    status = "\n".join(surface.status())
    assert any(line.startswith("- advance record 1: ") for line in status.splitlines())
    assert f"Run: {run_id}; run root: {run_root}; stage: designator." in status
    assert "Reason: operator reviewed the manual boundary" in status


def test_semi_mode_refuses_an_intermediate_boundary_end_to_end(
    orchestrated_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, run_id = _run(orchestrated_run, tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)
    receipts = RunTree(run_root, run_id).root / "receipts" / "sha256"
    before = set(receipts.glob("*.json"))

    with pytest.raises(OperatorError) as refusal:
        cli._advance_with_confirmation(
            run_root,
            run_id,
            "designator",
            reason="operator reviewed the intermediate boundary",
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
    orchestrated_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The typed digest binds the shown seal even if it changes during the prompt."""

    run_root, run_id = _run(orchestrated_run, tmp_path)
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
            mode="manual",
        )

    # Two protections can name this reseal: the digest binding ("changed after
    # it was shown") and, since the forged seal's census is not backed by disk,
    # the boundary verification that now runs first. Either named refusal
    # proves a seal that moved during the prompt cannot be advanced, and the
    # receipt assertions below prove nothing was written either way.
    detail = refusal.value.detail or ""
    assert (
        "changed after it was shown for confirmation" in detail
        or "no longer verifies against the run tree" in detail
    )
    assert len(shown) == 1
    after = set(receipts.glob("*.json"))
    assert after == before


def test_typed_grant_binds_the_exact_reason_written_to_the_receipt(
    orchestrated_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advance may not record decision text the operator never confirmed."""

    run_root, run_id = _run(orchestrated_run, tmp_path)
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
        mode="manual",
    )

    assert len(shown) == 1
    assert 'for reason "reviewed \\"census\\"\\nwith the page image"' in shown[0]
    assert "\n" not in shown[0]
    records = review.ReadOnlyRun(run_root, run_id).projection().advance_records
    written = [record for record in records if record["reason"] == reason]
    assert len(written) == 1


def test_auto_mode_never_solicits_a_typed_confirmation(
    orchestrated_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ineligible auto boundary must refuse before asking for a decision."""

    run_root, run_id = _run(orchestrated_run, tmp_path)
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
            mode="auto",
        )

    assert solicited == []
    assert "auto mode" in (refusal.value.detail or "").lower()


def test_auto_mode_can_advance_the_boundary_that_may_hold_in_every_mode(
    orchestrated_run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Auto may advance Attestatores because its sealed hold precedes mode handling."""

    run_root, run_id = _run(orchestrated_run, tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "attestatores",
        reason="operator reviewed the held witness boundary",
        mode="auto",
    )

    rendered = capsys.readouterr().out
    assert (
        "This declared selection can require a person-held advance at: "
        f"{', '.join(sorted(ALWAYS_HELD_BOUNDARIES))}." in rendered
    )
    # The surface never emits the word "waits", so asserting its absence could
    # not fail. Assert the line it does emit for the declared mode instead.
    assert "as you declared it: auto." in rendered
    assert "Advance record:" in rendered


def test_auto_mode_can_advance_the_armarium_boundary_that_may_hold_in_every_mode(
    orchestrated_run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Auto may advance Armarium: its terminal report can hold without consulting mode."""

    run_root, run_id = _run(orchestrated_run, tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "armarium",
        reason="operator reviewed the terminal boundary",
        mode="auto",
    )

    rendered = capsys.readouterr().out
    assert (
        "This declared selection can require a person-held advance at: "
        f"{', '.join(sorted(ALWAYS_HELD_BOUNDARIES))}." in rendered
    )
    assert "Advance record:" in rendered


def test_an_unvalidated_mode_selection_states_no_boundary_before_it_refuses(
    orchestrated_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An invalid semi range must not be presented as an established boundary claim."""

    run_root, run_id = _run(orchestrated_run, tmp_path)

    with pytest.raises(OperatorError) as refusal:
        cli._advance_with_confirmation(
            run_root,
            run_id,
            "perlector",
            reason="operator forgot the range",
            mode="semi",
        )

    assert "needs both the first and last stage" in (refusal.value.detail or "")
    rendered = capsys.readouterr().out
    # The claim line this test exists to keep off the screen is the one the
    # console actually prints. "waits at" appears nowhere in the surface, so
    # the previous spelling was true however the code behaved -- printing the
    # boundary claim before validating the range would not have failed it.
    assert "person-held advance at" not in rendered
    assert "None" not in rendered
    assert "Current boundary state" in rendered


@pytest.mark.parametrize(
    "missing", ["census", "config_digest", "artifact_inventory", "blob_inventory"]
)
def test_a_seal_payload_missing_a_displayed_key_is_a_named_refusal(
    orchestrated_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """Damaged evidence this tool can name must not arrive as an unclassified fault.

    `stored_boundary` converts every read failure into a named refusal, but
    `boundary_summary` then indexes five payload keys and `latest_attempt`
    proves only `attempt_ordinal`. A payload that had lost one of the other four
    raised a bare `KeyError`, which is not an `ApprovalRefusal`, so it passed
    `_advance_with_confirmation`'s handler and reached the unclassified one --
    the "photograph this and find a maintainer" path `_bound_run_tree`'s
    docstring calls a tool that broke rather than a request that was refused.

    Driven at this seam rather than through a rewritten artifact on disk: the
    run tree's own envelope checks refuse a doctored payload earlier, so a
    tree-level fixture would prove `read_artifact`'s guard and never reach this
    one. The claim is only that this function refuses by name when handed such
    a payload, which is what its caller depends on.
    """

    run_root, run_id = _run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    seal, digest = advance.stored_boundary(tree, "designator")
    damaged = {**seal, "payload": {k: v for k, v in seal["payload"].items() if k != missing}}
    monkeypatch.setattr(advance, "stored_boundary", lambda _tree, _stage: (damaged, digest))

    with pytest.raises(ApprovalRefusal, match="could not read designator's stored completion seal"):
        advance.boundary_summary(tree, "designator")


def test_unreadable_boundary_evidence_is_refused_not_reported_as_unsealed(
    orchestrated_run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A damaged seal is evidence of damage, not evidence that no seal exists."""

    run_root, run_id = _run(orchestrated_run, tmp_path)
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
            mode="manual",
        )

    assert refusal.value.code == ErrorCode.ADVANCE_REFUSED
    assert "could not read designator's stored completion seal" in (refusal.value.detail or "")
    rendered = capsys.readouterr().out
    assert "designator: no stored completion seal" not in rendered


def test_missing_earlier_seal_in_a_later_sealed_chain_is_refused_as_lost_evidence(
    orchestrated_run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A deleted seal in a completed chain is not an ordinary unstarted stage."""

    run_root, run_id = _run(orchestrated_run, tmp_path)
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
            mode="manual",
        )

    assert refusal.value.code == ErrorCode.ADVANCE_REFUSED
    detail = refusal.value.detail or ""
    assert "designator has no completion seal although later stage attestatores is sealed" in detail
    assert "evidence is missing, not merely unfinished" in detail
    assert "designator: no stored completion seal" not in capsys.readouterr().out


def test_the_advance_presentation_never_phrases_a_recommendation(
    orchestrated_run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The console may project facts but must never recommend a boundary."""

    run_root, run_id = _run(orchestrated_run, tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "designator",
        reason="operator reviewed the boundary",
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
    orchestrated_run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--mode` is unverifiable, and the surface has to say so.

    No run-tree record carries invocation mode, so the operator's declaration
    must remain distinct from seal facts read from the tree.
    """

    run_root, run_id = _run(orchestrated_run, tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "designator",
        reason="operator reviewed the boundary",
        mode="manual",
    )

    rendered = capsys.readouterr().out
    assert "Staged invocation mode, as you declared it: manual." in rendered
    assert "records no invocation mode" in rendered
