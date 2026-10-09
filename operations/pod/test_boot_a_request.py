"""The Boot A request renders from the sealed policy, or refuses by name."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal
from pathlib import Path

import pytest

from .boot_a_request import (
    BOOT_A_HARD_LIFETIME_SECONDS,
    cheapest_card,
    main,
    pod_request,
    render_boot_a_request,
)
from .cli import _request
from .conftest import configured_spend_toml
from .models import PodCreateRequest, utc_now
from .preflight import load_placement_table
from .spend import SPEND_SCHEMA, SpendPolicy, load_spend_policy

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
        "soft_max_seconds": 86_400,
        "hard_max_seconds": 86_400,
        "soft_max_cost_usd": Decimal("1000.00"),
        "hard_max_cost_usd": Decimal("1000.00"),
        "pod_budget": "on",
        "ladder_delete": "off",
    }
    fields.update(overrides)
    return SpendPolicy(**fields)  # type: ignore[arg-type]


def test_the_committed_policy_renders_the_a5000_drill_under_the_ledger_ceilings() -> None:
    """`config/spend.toml` is configured: the committed file renders the Boot A
    drill on the cheapest reviewed card under exactly the ceilings the project
    lead approved. The refusal path keeps its own coverage in the
    unconfigured-policy tests below."""
    placement = load_placement_table(PLACEMENT)
    rendered = render_boot_a_request(load_spend_policy(COMMITTED_SPEND), placement)

    assert not rendered.refused
    assert rendered.card is cheapest_card(placement)
    assert rendered.card.name == "RTX A5000"
    assert rendered.hard_lifetime_seconds == BOOT_A_HARD_LIFETIME_SECONDS == 900
    text = rendered.text
    for phrase in (
        "max_hourly_usd` = $3.00",
        "max_estimated_metered_cost_usd` = $6.00",
        "account_balance_floor_usd` = $50.00",
        "account_balance_alert_usd` = $75.00",
        "ceiling = 7200",
        "billing_cutoff_margin_seconds` = 3600",
        "authorizes nothing",
    ):
        assert phrase in text, phrase
    assert "Why the preview will refuse" not in text


def test_an_unconfigured_policy_renders_a_refusal_not_a_request() -> None:
    """The refusal path must not depend solely on the committed spend.toml
    happening to say state="unconfigured" today -- it has to hold for the
    state itself, independent of the committed file's current contents."""
    rendered = render_boot_a_request(
        SpendPolicy(state="unconfigured"), load_placement_table(PLACEMENT)
    )

    assert rendered.refused
    assert "REFUSED" in rendered.text
    assert rendered.card is None and rendered.hard_lifetime_seconds is None
    assert "python -m operations.pod.cli" not in rendered.text


def test_a_configured_policy_renders_the_drill_from_its_own_numbers() -> None:
    placement = load_placement_table(PLACEMENT)
    rendered = render_boot_a_request(configured(), placement)

    assert not rendered.refused
    card = cheapest_card(placement)
    assert rendered.card is card
    assert card.hourly_usd == min(profile.hourly_usd for profile in placement.card_profiles)
    assert rendered.hard_lifetime_seconds == BOOT_A_HARD_LIFETIME_SECONDS == 900
    # 900 s at the cheapest reviewed rate, rounded up to the cent -- matching
    # production's own explicit ROUND_UP (`_cents`), so an inflated quote is
    # caught rather than waved through by a `>=` that any overcharge satisfies.
    expected = (card.hourly_usd * Decimal(900) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_UP
    )
    assert rendered.estimated_pod_cost_usd == expected
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


def test_the_pod_request_validates_once_the_project_lead_supplies_four_values() -> None:
    """``hard_deadline`` is a value the project lead supplies too -- ``pod_request``"""

    card = cheapest_card(load_placement_table(PLACEMENT))
    hard_deadline = (utc_now().replace(microsecond=0)).isoformat().replace("+00:00", "Z")
    raw = pod_request(
        card,
        image="registry.example/verbatus@sha256:" + "a" * 64,
        volume_id="volume-1",
        repository_commit="b" * 40,
        hard_deadline=hard_deadline,
    )
    raw["metadata"] = {"VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS": "3600"}
    assert raw["hard_deadline"] == hard_deadline
    raw["docker_start_cmd"] = tuple(raw["docker_start_cmd"])  # type: ignore[arg-type]
    # `pod_request` renders the RFC3339 string the JSON file carries;
    # `PodCreateRequest` itself takes a parsed `datetime`, exactly as
    # `cli._request` parses it before construction.
    raw["hard_deadline"] = datetime.fromisoformat(hard_deadline.replace("Z", "+00:00"))

    request = PodCreateRequest(**raw)  # type: ignore[arg-type]

    assert request.gpu_type == card.gpu_type_id
    bootstrap = json.loads(
        request.docker_start_cmd[request.docker_start_cmd.index("--bootstrap-command-json") + 1]
    )
    assert bootstrap[:4] == ["python", "-m", "operations.pod.bootstrap_main", "--hold-only"]
    assert "--volume-mount-path" in bootstrap and "--report-path" in bootstrap


def test_the_rendered_request_loads_through_cli_request_once_every_value_is_supplied(
    tmp_path: Path,
) -> None:
    """What `README.md` and the printed command both promise is runnable must
    actually load through the exact loader the printed command invokes -- not
    just construct ``PodCreateRequest`` directly, which would miss a field
    ``cli._request`` refuses (e.g. an unfilled ``hard_deadline`` placeholder).
    """

    placement = load_placement_table(PLACEMENT)
    now = utc_now()
    deadline = (now + timedelta(seconds=BOOT_A_HARD_LIFETIME_SECONDS + 100)).replace(microsecond=0)
    hard_deadline = deadline.isoformat().replace("+00:00", "Z")
    rendered = render_boot_a_request(
        configured(),
        placement,
        image="registry.example/verbatus@sha256:" + "a" * 64,
        volume_id="volume-1",
        repository_commit="b" * 40,
        hard_deadline=hard_deadline,
        now=now,
    )
    assert not rendered.refused

    printed = rendered.text.split("```json\n", 1)[1].split("\n```", 1)[0]
    # The launch seals the real margin from the spend policy on every create,
    # so the printed placeholder is a non-blank string `_request` must accept.
    assert json.loads(printed)["metadata"] == {
        "VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS": "<the sealed policy value>"
    }
    path = tmp_path / "boot-a.json"
    path.write_text(printed, encoding="utf-8")

    request = _request(path)

    assert isinstance(request, PodCreateRequest)
    assert request.hard_deadline.isoformat().replace("+00:00", "Z") == hard_deadline


def test_a_hard_deadline_shorter_than_the_drills_own_lifetime_is_refused() -> None:
    now = utc_now()
    too_soon = (now + timedelta(seconds=10)).isoformat().replace("+00:00", "Z")

    with pytest.raises(ValueError, match="at least .* seconds from now"):
        render_boot_a_request(
            configured(),
            load_placement_table(PLACEMENT),
            hard_deadline=too_soon,
            now=now,
        )


def test_a_past_hard_deadline_is_refused() -> None:
    now = utc_now()
    past = (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")

    with pytest.raises(ValueError, match="must be in the future"):
        render_boot_a_request(
            configured(), load_placement_table(PLACEMENT), hard_deadline=past, now=now
        )


def test_placeholders_stay_visible_until_supplied() -> None:
    raw = pod_request(
        cheapest_card(load_placement_table(PLACEMENT)),
        image=None,
        volume_id=None,
        repository_commit=None,
    )

    assert raw["image"] == raw["volume_id"] == raw["repository_commit"] == "<not yet supplied>"


def test_a_request_with_no_volume_is_refused_before_any_provider_is_reached(
    tmp_path: Path,
) -> None:
    """Every run needs a network volume; the unsupplied placeholder must not launch."""

    raw = pod_request(
        cheapest_card(load_placement_table(PLACEMENT)),
        image="registry.example/verbatus@sha256:" + "a" * 64,
        volume_id=None,
        repository_commit="b" * 40,
        hard_deadline=utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    raw["metadata"] = {"VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS": "3600"}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="placeholder"):
        _request(path)

    raw["volume_id"] = "  <not yet supplied>"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="placeholder"):
        _request(path)


def test_a_request_naming_any_other_mount_path_is_refused(tmp_path: Path) -> None:
    raw = pod_request(
        cheapest_card(load_placement_table(PLACEMENT)),
        image="registry.example/verbatus@sha256:" + "a" * 64,
        volume_id="volume-1",
        repository_commit="b" * 40,
        hard_deadline=utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        volume_mount_path="/workspace/elsewhere",
    )
    raw["metadata"] = {"VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS": "3600"}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="volume_mount_path must be"):
        _request(path)


def test_main_exits_zero_on_the_committed_policy(capsys: pytest.CaptureFixture[str]) -> None:
    status = main(["--spend", str(COMMITTED_SPEND), "--placement", str(PLACEMENT)])

    out = capsys.readouterr().out
    assert status == 0
    assert "REFUSED" not in out
    assert "RTX A5000" in out


def test_main_exits_two_on_an_uncommitted_unconfigured_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Against a policy file this test writes itself, so the refusal path keeps
    its own coverage now that the committed config/spend.toml is configured."""
    spend = tmp_path / "spend.toml"
    spend.write_text(f'schema = "{SPEND_SCHEMA}"\nstate = "unconfigured"\n', encoding="utf-8")

    status = main(["--spend", str(spend), "--placement", str(PLACEMENT)])

    assert status == 2
    assert "REFUSED" in capsys.readouterr().out


def test_main_refuses_an_unreadable_spend_policy_instead_of_raising(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`SpendRefusal` is a `PodRuntimeError`, not a `ValueError`.

    A missing or malformed `config/spend.toml` is the ordinary way this
    command is run wrong, and must answer with the REFUSED page every other
    unrenderable configuration gets, not a traceback.
    """

    spend = tmp_path / "spend.toml"
    spend.write_text(
        f'schema = "{SPEND_SCHEMA}"\nstate = "configured"\nmax_hourly', encoding="utf-8"
    )

    status = main(["--spend", str(spend), "--placement", str(PLACEMENT)])

    assert status == 2
    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert "python -m operations.pod.cli" not in out


def test_main_refuses_a_missing_spend_policy_instead_of_raising(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(["--spend", str(tmp_path / "absent.toml"), "--placement", str(PLACEMENT)])

    assert status == 2
    assert "REFUSED" in capsys.readouterr().out


def test_main_exits_zero_on_a_configured_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spend = tmp_path / "spend.toml"
    spend.write_text(
        configured_spend_toml(shutdown_deadline_seconds=5),
        encoding="utf-8",
    )

    status = main(["--spend", str(spend), "--placement", str(PLACEMENT), "--volume-id", "vol-9"])

    out = capsys.readouterr().out
    assert status == 0
    assert '"volume_id": "vol-9"' in out
    assert "--record-fixture" in out


def test_the_drill_request_names_the_timer_capability_as_the_project_leads_to_deliver() -> None:
    """The one thing that stops the drill dead, said where the reader decides.

    The pod-side timer refuses to construct without its provider capability,
    and a timer that cannot construct cannot close the pod -- the container
    exits and the pod stays EXITED and billing. Nothing in the tracked tree can
    deliver that value (`metadata` refuses credential-shaped keys), so it
    belongs in "what only the project lead supplies" rather than being discovered on a
    billing card. Named by its factory rather than by the vendor's variable,
    because provider vocabulary stays inside the adapter.
    """

    rendered = render_boot_a_request(configured(), load_placement_table(PLACEMENT))

    supplies = rendered.text.split("## What only the project lead supplies", 1)[1].split(
        "## The command", 1
    )[0]
    assert "timer_context_from_environment" in supplies
    assert "EXITED" in supplies
    assert "metadata" in supplies
