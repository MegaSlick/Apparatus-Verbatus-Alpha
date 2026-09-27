"""Boot B renders, and the shape it renders is one the money path accepts.

The first half of this module is the ordinary rendering contract. The second
half is the regression a pre-launch review found by composing a ``pod_run``
``docker_start_cmd`` by hand: nothing in the tree had ever built one, so the
create gate's "at most one nested --report-path" rule -- which a ``pod_run``
argv necessarily breaks, because each of its two halves requires its own --
made the first real pipeline run unconstructible with a green suite over it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath

import pytest

from .boot_a_request import cheapest_card
from .boot_b_request import (
    BOOT_B_REPOSITORY_PATH,
    BOOT_B_VOLUME_MOUNT_PATH,
    pod_request,
    render_boot_b_request,
    validated_pod_request,
)
from .cli import _request
from .launch import _bind_report_path_to_launch
from .models import (
    BIG_CARD_CONTAINER_DISK_GB,
    DEFAULT_CONTAINER_DISK_GB,
    PodCreateRequest,
    container_disk_gb_for_tier,
    run_report_paths,
    terminating_path,
)
from .preflight import load_placement_table
from .spend import SpendPolicy, load_spend_policy

UTC = timezone.utc
REPOSITORY = Path(__file__).resolve().parents[2]
PLACEMENT = REPOSITORY / "config" / "pod_placement.toml"
COMMITTED_SPEND = REPOSITORY / "config" / "spend.toml"

IMAGE = "registry.example/verbatus@sha256:" + "a" * 64
COMMIT = "b" * 40


def configured(**overrides: object) -> SpendPolicy:
    fields: dict[str, object] = {
        "state": "configured",
        "max_hourly_usd": Decimal("1.00"),
        "max_estimated_metered_cost_usd": Decimal("20.00"),
        "account_balance_floor_usd": Decimal("50.00"),
        "account_balance_alert_usd": Decimal("75.00"),
        "hard_lifetime_seconds": 3600,
        "laptop_heartbeat_timeout_seconds": 30,
        "shutdown_poll_interval_seconds": 1,
        "shutdown_deadline_seconds": 5,
        "billing_cutoff_margin_seconds": 3600,
    }
    fields.update(overrides)
    return SpendPolicy(**fields)  # type: ignore[arg-type]


def filled_request() -> dict[str, object]:
    """A rendered request with every hand-filled field supplied."""

    return pod_request(
        cheapest_card(load_placement_table(PLACEMENT)),
        image=IMAGE,
        volume_id="volume-abc",
        repository_commit=COMMIT,
        run_id="boot-b-0001",
        hard_deadline=_deadline(),
    )


def _deadline() -> str:
    return (
        (datetime.now(UTC) + timedelta(hours=8))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _nested_argv(request: dict[str, object]) -> list[str]:
    command = list(request["docker_start_cmd"])  # type: ignore[arg-type]
    return json.loads(command[command.index("--bootstrap-command-json") + 1])


def _with_nested_argv(request: dict[str, object], nested: list[str]) -> dict[str, object]:
    command = list(request["docker_start_cmd"])  # type: ignore[arg-type]
    command[command.index("--bootstrap-command-json") + 1] = json.dumps(nested)
    return {**request, "docker_start_cmd": command}


def _sealed_nested_argv(command: tuple[str, ...]) -> list[str]:
    return json.loads(command[command.index("--bootstrap-command-json") + 1])


# --- the rendering contract ------------------------------------------------


def test_the_committed_policy_renders_a_boot_b_request() -> None:
    policy = load_spend_policy(COMMITTED_SPEND)

    rendered = render_boot_b_request(policy, load_placement_table(PLACEMENT))

    assert not rendered.refused
    assert rendered.hard_lifetime_seconds == policy.hard_lifetime_seconds
    for phrase in (
        "Boot B -- the real run",
        "ChannelControllerArmer",
        "operations.pod.pod_run",
        "nothing-to-transfer",
        "timer_context_from_environment",
        "container disk",
    ):
        assert phrase in rendered.text, phrase


def test_boot_b_container_disk_follows_the_selected_card_tier() -> None:
    placement = load_placement_table(PLACEMENT)
    cards = {card.tier: card for card in placement.card_profiles}

    def disk_for(tier: str) -> object:
        return pod_request(
            cards[tier],
            image=IMAGE,
            volume_id="volume-abc",
            repository_commit=COMMIT,
            run_id="boot-b-0001",
            hard_deadline=_deadline(),
        )["container_disk_gb"]

    assert disk_for("generic-80gb-plus") == BIG_CARD_CONTAINER_DISK_GB
    assert disk_for("generic-48gb") == DEFAULT_CONTAINER_DISK_GB


def test_boot_b_forwards_canary_inputs_only_as_a_pair() -> None:
    args = dict(
        image=IMAGE,
        volume_id="volume-abc",
        repository_commit=COMMIT,
        run_id="boot-b-0001",
        hard_deadline=_deadline(),
    )
    card = cheapest_card(load_placement_table(PLACEMENT))
    nested = _nested_argv(pod_request(card, **args))
    assert "--canary-folder" not in nested
    with pytest.raises(ValueError, match="together"):
        pod_request(card, **args, canary_folder="/volume/canary")
    nested = _nested_argv(
        pod_request(
            card, **args, canary_folder="/volume/canary", canary_manifest="/volume/canary.json"
        )
    )
    assert nested[nested.index("--canary-folder") + 1] == "/volume/canary"
    assert nested[nested.index("--canary-manifest") + 1] == "/volume/canary.json"


def test_rendered_boot_b_request_carries_a_parsed_canary_pair() -> None:
    rendered = render_boot_b_request(
        configured(),
        load_placement_table(PLACEMENT),
        image=IMAGE,
        volume_id="volume-abc",
        repository_commit=COMMIT,
        run_id="boot-b-0001",
        hard_deadline=_deadline(),
        canary_folder="/volume/canary/pages",
        canary_manifest="/volume/canary/submission-manifest.json",
    )
    request = json.loads(rendered.text.split("```json\n", 1)[1].split("\n```", 1)[0])
    parsed = validated_pod_request(request)
    nested = _sealed_nested_argv(parsed.docker_start_cmd)
    assert nested[nested.index("--canary-folder") + 1] == "/volume/canary/pages"
    assert (
        nested[nested.index("--canary-manifest") + 1] == "/volume/canary/submission-manifest.json"
    )


def test_an_unconfigured_policy_refuses_rather_than_rendering_blanks() -> None:
    rendered = render_boot_b_request(
        SpendPolicy(state="unconfigured"),  # type: ignore[arg-type]
        load_placement_table(PLACEMENT),
    )

    assert rendered.refused
    assert "REFUSED" in rendered.text
    assert "docker_start_cmd" not in rendered.text


def test_a_card_the_placement_table_has_not_reviewed_is_refused() -> None:
    with pytest.raises(ValueError, match="not a reviewed card"):
        render_boot_b_request(
            configured(), load_placement_table(PLACEMENT), gpu_type="no-such-card"
        )


def test_the_rendered_request_carries_no_transfer_half() -> None:
    """Boot B consumes a submission already on the volume."""

    nested = _nested_argv(filled_request())
    separator = nested.index("--")
    run_half, bootstrap_half = nested[:separator], nested[separator + 1 :]

    assert "--transfer-target-factory" not in nested
    assert "--submission-manifest" not in bootstrap_half
    assert f"{BOOT_B_VOLUME_MOUNT_PATH}/submission-manifest.json" in run_half


# --- the shape the money path has to accept --------------------------------


def test_a_real_pod_run_argv_constructs_a_pod_create_request() -> None:
    """Two nested halves, one ``--report-path`` each, and they differ."""

    request = validated_pod_request(filled_request())

    assert isinstance(request, PodCreateRequest)
    nested = _sealed_nested_argv(request.docker_start_cmd)
    separator = nested.index("--")
    run_half, bootstrap_half = nested[:separator], nested[separator + 1 :]
    assert run_half.count("--report-path") == 1
    assert bootstrap_half.count("--report-path") == 1
    assert (
        run_half[run_half.index("--report-path") + 1]
        != bootstrap_half[bootstrap_half.index("--report-path") + 1]
    )


def test_the_rendered_json_is_accepted_by_the_create_surface(tmp_path: Path) -> None:
    """End to end: what the renderer prints is what ``cli create --request`` loads."""

    request = filled_request()
    path = tmp_path / "boot-b.json"
    path.write_text(json.dumps(request, indent=2, sort_keys=True), encoding="utf-8")

    loaded = _request(path)

    assert loaded.volume_mount_path == BOOT_B_VOLUME_MOUNT_PATH
    expected_disk = container_disk_gb_for_tier(cheapest_card(load_placement_table(PLACEMENT)).tier)
    assert loaded.container_disk_gb == expected_disk
    assert request["container_disk_gb"] == expected_disk
    assert BOOT_B_REPOSITORY_PATH in _sealed_nested_argv(loaded.docker_start_cmd)[-1]


def test_sealing_binds_the_token_into_both_report_paths_and_the_journal() -> None:
    """Every durable name a Boot B pod writes carries the launch's own token.

    ``bootstrap_main`` refuses an unbound ``--report-path`` *and* an unbound
    ``--journal`` on the pod, and the token is minted inside ``create``, so a
    launch that bound only the report paths left the journal refusing after
    billing had started.
    """

    token = "c" * 32
    request = validated_pod_request(filled_request())

    sealed = _bind_report_path_to_launch(request.docker_start_cmd, token)
    bound = replace(request, docker_start_cmd=sealed, metadata={"VERBATUS_LAUNCH_TOKEN": token})

    assert (
        token
        in Path(bound.docker_start_cmd[bound.docker_start_cmd.index("--report-path") + 1]).name
    )
    nested = _sealed_nested_argv(bound.docker_start_cmd)
    assert nested.count("--report-path") == 2
    assert nested.count("--journal") == 1
    for index, item in enumerate(nested):
        if item in {"--report-path", "--journal"}:
            assert token in Path(nested[index + 1]).name, item


def test_the_self_validation_runs_under_a_launch_token_like_a_real_create() -> None:
    """The token rules are the ones this renderer exists to prove, so they must fire.

    Every nested ``--report-path`` and ``--journal`` token rule in
    ``models._required_timer_arguments`` is written ``if launch_token and
    ...``. Validating with ``metadata={}`` left the token unset, so the
    renderer proved its shape with exactly those rules switched off -- and the
    class of defect it was written to stop could return unnoticed, to be
    refused after the project lead had authorised the run and the card was
    rented.
    """

    request = validated_pod_request(filled_request())

    token = request.metadata["VERBATUS_LAUNCH_TOKEN"]
    assert token
    assert request.metadata["VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS"]
    assert (
        token
        in Path(request.docker_start_cmd[request.docker_start_cmd.index("--report-path") + 1]).name
    )
    nested = _sealed_nested_argv(request.docker_start_cmd)
    bound = [index for index, item in enumerate(nested) if item in {"--report-path", "--journal"}]
    assert len(bound) == 3
    for index in bound:
        assert token in Path(nested[index + 1]).name, nested[index]


def test_two_nested_report_paths_naming_one_file_are_refused() -> None:
    """``pod_run`` refuses this on the pod; the create gate refuses it before billing."""

    nested = _nested_argv(filled_request())
    collision = f"{BOOT_B_VOLUME_MOUNT_PATH}/one-report.json"
    for index, item in enumerate(nested):
        if item == "--report-path":
            nested[index + 1] = collision

    with pytest.raises(ValueError, match="collides with the run report"):
        validated_pod_request(_with_nested_argv(filled_request(), nested))


def test_bootstrap_argument_named_pod_run_does_not_hide_report_collision() -> None:
    base = validated_pod_request(filled_request())
    command = list(base.docker_start_cmd)
    nested = _sealed_nested_argv(base.docker_start_cmd)
    report_indexes = [i + 1 for i, value in enumerate(nested) if value == "--report-path"]
    run_path = PurePosixPath(nested[report_indexes[0]])
    nested[report_indexes[1]] = str(run_report_paths(run_path)[1])
    nested[nested.index("--repository") + 1] = "/opt/pod_run.py"
    command[command.index("--bootstrap-command-json") + 1] = json.dumps(nested)

    with pytest.raises(ValueError, match="bootstrap report path collides with the run report"):
        replace(base, docker_start_cmd=tuple(command))


@pytest.mark.parametrize(
    "target", ("run", "hold", "liveness", "timings", "transcript", "bootstrap", "journal")
)
def test_timer_report_and_terminating_path_cannot_overlap_run_records(target: str) -> None:
    base = validated_pod_request(filled_request())
    command = list(base.docker_start_cmd)
    outer = PurePosixPath(command[command.index("--report-path") + 1])
    nested = _sealed_nested_argv(base.docker_start_cmd)
    report_indexes = [i + 1 for i, value in enumerate(nested) if value == "--report-path"]
    if target == "bootstrap":
        nested[report_indexes[1]] = str(outer)
    elif target == "journal":
        nested[nested.index("--journal") + 1] = str(outer)
    else:
        token = base.metadata["VERBATUS_LAUNCH_TOKEN"]
        run_path = PurePosixPath(f"{BOOT_B_VOLUME_MOUNT_PATH}/run-evidence-{token}.json")
        side_index = {"run": 0, "hold": 1, "liveness": 2, "timings": 3, "transcript": 4}
        command[command.index("--report-path") + 1] = str(
            run_report_paths(run_path)[side_index[target]]
        )
        nested[report_indexes[0]] = str(run_path)
    command[command.index("--bootstrap-command-json") + 1] = json.dumps(nested)

    with pytest.raises(ValueError, match="pod timer report or terminating path collides"):
        replace(base, docker_start_cmd=tuple(command))


@pytest.mark.parametrize("target", ("run", "bootstrap", "journal"))
def test_timer_terminating_path_cannot_overlap_another_report(target: str) -> None:
    base = validated_pod_request(filled_request())
    command = list(base.docker_start_cmd)
    outer = PurePosixPath(command[command.index("--report-path") + 1])
    terminating = terminating_path(outer)
    nested = _sealed_nested_argv(base.docker_start_cmd)
    reports = [i + 1 for i, value in enumerate(nested) if value == "--report-path"]
    index = (
        nested.index("--journal") + 1
        if target == "journal"
        else reports[0 if target == "run" else 1]
    )
    nested[index] = str(terminating)
    command[command.index("--bootstrap-command-json") + 1] = json.dumps(nested)

    with pytest.raises(ValueError, match="pod timer report or terminating path collides"):
        replace(base, docker_start_cmd=tuple(command))


def test_log_timer_terminating_path_cannot_be_a_bootstrap_report() -> None:
    base = validated_pod_request(filled_request())
    command = list(base.docker_start_cmd)
    timer_index = command.index("--report-path") + 1
    timer_report = PurePosixPath(command[timer_index]).with_suffix(".log")
    command[timer_index] = str(timer_report)
    nested = _sealed_nested_argv(base.docker_start_cmd)
    reports = [i + 1 for i, value in enumerate(nested) if value == "--report-path"]
    nested[reports[1]] = str(terminating_path(timer_report))
    command[command.index("--bootstrap-command-json") + 1] = json.dumps(nested)

    with pytest.raises(ValueError, match="pod timer report or terminating path collides"):
        replace(base, docker_start_cmd=tuple(command))


def test_a_nested_journal_outside_the_volume_is_refused() -> None:
    """The journal is held to the report path's own containment rule."""

    nested = _nested_argv(filled_request())
    nested[nested.index("--journal") + 1] = "/tmp/bootstrap-journal.json"

    with pytest.raises(ValueError, match="nested --journal must be inside"):
        validated_pod_request(_with_nested_argv(filled_request(), nested))


def test_the_rendered_bootstrap_half_is_a_plan_bootstrap_main_accepts() -> None:
    from .bootstrap_main import build_parser, resolve_plan

    request = validated_pod_request(filled_request())
    nested = _sealed_nested_argv(request.docker_start_cmd)
    bootstrap_half = nested[nested.index("--") + 1 :]

    plan = resolve_plan(build_parser().parse_args(bootstrap_half), request.metadata)

    assert str(plan.repository) == BOOT_B_REPOSITORY_PATH
