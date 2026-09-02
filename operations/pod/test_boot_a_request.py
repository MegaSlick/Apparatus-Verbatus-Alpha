"""The Boot A request renders from the sealed policy, or refuses by name."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from .boot_a_request import (
    BOOT_A_HARD_LIFETIME_SECONDS,
    cheapest_card,
    main,
    pod_request,
    render_boot_a_request,
)
from .models import PodCreateRequest, utc_now
from .preflight import load_placement_table
from .spend import SpendPolicy, load_spend_policy

REPOSITORY = Path(__file__).resolve().parents[2]
PLACEMENT = REPOSITORY / "config" / "pod_placement.toml"
COMMITTED_SPEND = REPOSITORY / "config" / "spend.toml"


def configured(**overrides: object) -> SpendPolicy:
    fields: dict[str, object] = {
        "state": "configured",
        "max_hourly_usd": Decimal("1.00"),
        "max_estimated_metered_cost_usd": Decimal("2.00"),
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


def test_the_committed_unconfigured_policy_renders_a_refusal_not_a_request() -> None:
    rendered = render_boot_a_request(
        load_spend_policy(COMMITTED_SPEND), load_placement_table(PLACEMENT)
    )

    assert rendered.refused
    assert "REFUSED" in rendered.text
    assert rendered.card is None and rendered.hard_lifetime_seconds is None
    # Nothing in a refusal reads as a runnable plan.
    assert "python -m operations.pod.cli" not in rendered.text
    assert "create --request" not in rendered.text
    assert "No pod, lease, preview or provider call results" in rendered.text


def test_a_configured_policy_renders_the_drill_from_its_own_numbers() -> None:
    placement = load_placement_table(PLACEMENT)
    rendered = render_boot_a_request(configured(), placement)

    assert not rendered.refused
    card = cheapest_card(placement)
    assert rendered.card is card
    assert card.hourly_usd == min(profile.hourly_usd for profile in placement.card_profiles)
    assert rendered.hard_lifetime_seconds == BOOT_A_HARD_LIFETIME_SECONDS == 900
    # 900 s at the cheapest reviewed rate, rounded up to the cent.
    expected = (card.hourly_usd * Decimal(900) / Decimal(3600)).quantize(Decimal("0.01"))
    assert rendered.estimated_pod_cost_usd >= expected
    text = rendered.text
    for phrase in (
        card.name,
        card.gpu_type_id,
        "ObservingControllerArmer",
        "--hold-only",
        "--record-fixture",
        "immediate close",
        "max_hourly_usd` = $1.00",
        "account_balance_floor_usd` = $50.00",
        "900 seconds",
        "authorizes nothing",
    ):
        assert phrase in text, phrase
    assert "Why the preview will refuse" not in text


def test_a_shorter_policy_lifetime_bounds_the_drill() -> None:
    rendered = render_boot_a_request(
        configured(hard_lifetime_seconds=600, shutdown_deadline_seconds=5),
        load_placement_table(PLACEMENT),
    )

    assert rendered.hard_lifetime_seconds == 600
    assert "600 seconds" in rendered.text


def test_a_card_above_the_hourly_ceiling_is_named_as_a_coming_refusal() -> None:
    rendered = render_boot_a_request(
        configured(max_hourly_usd=Decimal("0.10")), load_placement_table(PLACEMENT)
    )

    assert not rendered.refused
    assert "Why the preview will refuse" in rendered.text
    assert "above max_hourly_usd $0.10" in rendered.text


def test_the_pod_request_validates_once_tyrel_supplies_his_three_values() -> None:
    card = cheapest_card(load_placement_table(PLACEMENT))
    raw = pod_request(
        card,
        image="registry.example/verbatus@sha256:" + "a" * 64,
        volume_id="volume-1",
        repository_commit="b" * 40,
    )
    raw["hard_deadline"] = utc_now()
    raw["metadata"] = {"VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS": "3600"}
    raw["docker_start_cmd"] = tuple(raw["docker_start_cmd"])  # type: ignore[arg-type]

    request = PodCreateRequest(**raw)  # type: ignore[arg-type]

    assert request.gpu_type == card.gpu_type_id
    bootstrap = json.loads(
        request.docker_start_cmd[request.docker_start_cmd.index("--bootstrap-command-json") + 1]
    )
    assert bootstrap[:4] == ["python", "-m", "operations.pod.bootstrap_main", "--hold-only"]
    assert "--volume-mount-path" in bootstrap and "--report-path" in bootstrap


def test_placeholders_stay_visible_until_supplied() -> None:
    raw = pod_request(
        cheapest_card(load_placement_table(PLACEMENT)),
        image=None,
        volume_id=None,
        repository_commit=None,
    )

    assert raw["image"] == raw["volume_id"] == raw["repository_commit"] == "<not yet supplied>"


def test_main_exits_two_on_the_committed_policy(capsys: pytest.CaptureFixture[str]) -> None:
    status = main(["--spend", str(COMMITTED_SPEND), "--placement", str(PLACEMENT)])

    assert status == 2
    assert "REFUSED" in capsys.readouterr().out


def test_main_exits_zero_on_a_configured_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spend = tmp_path / "spend.toml"
    spend.write_text(
        "\n".join(
            (
                'schema = "pod-spend.v3"',
                'state = "configured"',
                'currency = "USD"',
                'max_hourly_usd = "1.00"',
                'max_estimated_metered_cost_usd = "2.00"',
                'account_balance_floor_usd = "50.00"',
                'account_balance_alert_usd = "75.00"',
                "hard_lifetime_seconds = 3600",
                "laptop_heartbeat_timeout_seconds = 30",
                "shutdown_poll_interval_seconds = 1",
                "shutdown_deadline_seconds = 5",
                "billing_cutoff_margin_seconds = 3600",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    status = main(["--spend", str(spend), "--placement", str(PLACEMENT), "--volume-id", "vol-9"])

    out = capsys.readouterr().out
    assert status == 0
    assert '"volume_id": "vol-9"' in out
    assert "--record-fixture" in out
