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


def test_the_committed_policy_renders_the_a5000_drill_under_the_ledger_ceilings() -> None:
    """`config/spend.toml` is configured since 2026-09-06 (standing ledger §8-§9):
    the committed file renders the Boot A drill on the cheapest reviewed card
    under exactly the ceilings Tyrel approved. The refusal path keeps its own
    coverage in the unconfigured-policy tests below."""
    placement = load_placement_table(PLACEMENT)
    rendered = render_boot_a_request(load_spend_policy(COMMITTED_SPEND), placement)

    assert not rendered.refused
    assert rendered.card is cheapest_card(placement)
    assert rendered.card.name == "RTX A5000"
    assert rendered.hard_lifetime_seconds == BOOT_A_HARD_LIFETIME_SECONDS == 900
    text = rendered.text
    for phrase in (
        "max_hourly_usd` = $0.40",
        "max_estimated_metered_cost_usd` = $2.00",
        "account_balance_floor_usd` = $50.00",
        "account_balance_alert_usd` = $75.00",
        "ceiling = 14400",
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


def test_the_pod_request_validates_once_tyrel_supplies_his_four_values() -> None:
    """``hard_deadline`` is a value Tyrel supplies too -- ``pod_request``

    carries no runtime that fills it in, unlike ``metadata``'s billing-cutoff
    margin, which the launch seals from the spend policy on its own. This
    supplies all four (image, volume id, repository commit, hard deadline)
    exactly the way the rendered request asks for them, rather than patching
    hard_deadline and metadata past the placeholders the rendering leaves.
    """

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
    """The document `README.md` and the printed command both promise is

    runnable as printed must actually load through the exact loader the
    printed command invokes -- not just construct ``PodCreateRequest``
    directly, which would miss a field ``cli._request`` refuses (e.g. the
    ``hard_deadline`` placeholder that used to reach `PodCreateRequest`
    unfilled and unrefused-by-name)."""

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

    raw = pod_request(
        rendered.card,
        image="registry.example/verbatus@sha256:" + "a" * 64,
        volume_id="volume-1",
        repository_commit="b" * 40,
        hard_deadline=hard_deadline,
    )
    # The metadata placeholder the rendering prints is deliberately left as it
    # stands: it is the one field the earlier version of this test replaced,
    # which meant the printed document was never the document that was loaded.
    # The launch seals the real margin from the spend policy on every create,
    # so the placeholder is a non-blank string `_request` must accept.
    assert raw["metadata"] == {
        "VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS": "<the sealed policy value>"
    }
    path = tmp_path / "boot-a.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

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
    spend.write_text('schema = "pod-spend.v3"\nstate = "unconfigured"\n', encoding="utf-8")

    status = main(["--spend", str(spend), "--placement", str(PLACEMENT)])

    assert status == 2
    assert "REFUSED" in capsys.readouterr().out


def test_main_refuses_an_unreadable_spend_policy_instead_of_raising(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`SpendRefusal` is a `PodRuntimeError`, not a `ValueError`.

    A missing or malformed `config/spend.toml` is the ordinary way this
    command is run wrong, and it used to answer with a traceback rather than
    the REFUSED page every other unrenderable configuration gets.
    """

    spend = tmp_path / "spend.toml"
    spend.write_text('schema = "pod-spend.v3"\nstate = "configured"\nmax_hourly', encoding="utf-8")

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
