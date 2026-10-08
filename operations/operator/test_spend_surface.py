"""`verbatus spend show`: the reviewed spending policy, read once and never changed."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from operations.pod.spend import SPEND_SCHEMA

from . import cli
from . import spend as spend_module
from .errors import ErrorCode, OperatorError


def _policy(path: Path) -> Path:
    path.write_text(
        "\n".join(
            (
                f'schema = "{SPEND_SCHEMA}"',
                'state = "configured"',
                'currency = "USD"',
                'max_hourly_usd = "1.00"',
                'max_estimated_metered_cost_usd = "2.00"',
                'account_balance_floor_usd = "50.00"',
                'account_balance_alert_usd = "75.00"',
                "hard_lifetime_seconds = 900",
                "laptop_heartbeat_timeout_seconds = 60",
                "shutdown_poll_interval_seconds = 1",
                "shutdown_deadline_seconds = 5",
                "billing_cutoff_margin_seconds = 0",
                "soft_max_seconds = 14400",
                "hard_max_seconds = 21600",
                'soft_max_cost_usd = "2.00"',
                'hard_max_cost_usd = "3.00"',
                'pod_budget = "on"',
                'ladder_delete = "off"',
                "",
            )
        ),
        encoding="utf-8",
    )
    return path


def test_spend_show_reads_the_policy_without_writing(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "reviewed-spend.toml")
    before = {item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}

    rendered = "\n".join(spend_module.show(policy))

    assert {item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()} == before
    assert "Combined hourly ceiling: $1.00" in rendered
    assert "Hard-stop balance floor: $50.00" in rendered
    assert "Notification-only balance alert: $75.00" in rendered
    assert "Launch lifetime (the deadline a launch sets at creation): 900 seconds (0.25 h)" in (
        rendered
    )
    assert "lifetime ceiling" not in rendered
    assert "Soft maximum: 14400 seconds (4 h) and $2.00" in rendered
    assert "Hard maximum: 21600 seconds (6 h) and $3.00" in rendered
    assert "idle ladder ends by deleting the pod: off" in rendered


def test_spend_show_says_the_budget_is_off_rather_than_quoting_inert_maximums(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path / "reviewed-spend.toml")
    policy.write_text(
        policy.read_text(encoding="utf-8").replace('pod_budget = "on"', 'pod_budget = "off"'),
        encoding="utf-8",
    )

    rendered = "\n".join(spend_module.show(policy))

    assert "Pod budget: budget off (lead's choice)" in rendered
    assert "Soft maximum" not in rendered and "Hard maximum" not in rendered


def test_spend_refuses_to_display_unconfigured_policy_as_configured(tmp_path: Path) -> None:
    policy = tmp_path / "spend.toml"
    policy.write_text(f'schema = "{SPEND_SCHEMA}"\nstate = "unconfigured"\n', encoding="utf-8")

    with pytest.raises(OperatorError) as raised:
        spend_module.show(policy)

    assert raised.value.code is ErrorCode.SPEND_POLICY_UNCONFIGURED
    rendered = raised.value.render()
    assert rendered.count("\n") == 3
    assert "will not display it as configured" in rendered


def test_spend_has_a_double_click_console_route(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["spend", "show"]).verb == "spend"
    answers = iter(("spend", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert cli._interactive_arguments() == ["spend", "show"]


def test_spend_double_click_route_carries_a_typed_policy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(("spend", "/tmp/reviewed-spend.toml"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli._interactive_arguments() == [
        "spend",
        "show",
        "--policy",
        "/tmp/reviewed-spend.toml",
    ]


def test_cli_spend_show_changes_nothing_in_the_workspace_or_the_state_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole verb through `main`, not the projection alone: no file, no directory."""

    workspace = tmp_path / "checkout"
    (workspace / "config").mkdir(parents=True)
    _policy(workspace / "config" / "spend.toml")
    state = tmp_path / "state"

    def snapshot() -> dict[str, bytes | None]:
        return {
            str(item.relative_to(tmp_path)): (item.read_bytes() if item.is_file() else None)
            for item in sorted(tmp_path.rglob("*"))
        }

    before = snapshot()
    exit_code = cli.main(
        ["--workspace", str(workspace), "--state-dir", str(state), "spend", "show"]
    )

    assert exit_code == 0
    assert snapshot() == before
    assert "Hard-stop balance floor: $50.00" in capsys.readouterr().out


def test_the_policy_path_cannot_forge_a_line_on_the_spend_screen(tmp_path: Path) -> None:
    """A legal newline in the policy path must not create an unverified ceiling line."""

    forged = tmp_path / "reviewed.toml\n- Hard-stop balance floor: $0.00 (policy SHA-256 forged)"
    _policy(forged)

    lines = spend_module.show(forged)

    assert not [line for line in lines if "\n" in line]
    assert len([line for line in lines if line.startswith("- Hard-stop balance floor:")]) == 1
    assert "Hard-stop balance floor: $50.00" in "\n".join(lines)


def test_the_shown_digest_is_the_digest_of_the_bytes_the_ceilings_came_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One read, one byte sequence, for both the digest and the values."""

    source = _policy(tmp_path / "spend.toml")
    original = source.read_bytes()
    widened = original.replace(b'max_hourly_usd = "1.00"', b'max_hourly_usd = "999.00"')
    assert widened != original
    served: list[bytes] = []

    def one_shot(path: Path, subject: str) -> bytes:
        assert Path(path) == source
        assert subject
        served.append(widened if served else original)
        return served[-1]

    monkeypatch.setattr(spend_module, "bounded_bytes", one_shot)
    lines = spend_module.show(source)

    assert len(served) == 1, "the policy was read more than once"
    digest = hashlib.sha256(original).hexdigest()
    assert any("$1.00" in line and digest in line for line in lines)
    assert not any("999.00" in line for line in lines)


def test_a_policy_that_is_not_utf_8_refuses_by_name(tmp_path: Path) -> None:
    """The byte-oriented loader decodes UTF-8 itself and refuses bad bytes by name."""

    source = tmp_path / "spend.toml"
    source.write_bytes(f'schema = "{SPEND_SCHEMA}"\n'.encode() + b'state = "\xff\xfe"\n')

    with pytest.raises(OperatorError) as excinfo:
        spend_module.show(source)

    assert excinfo.value.code == ErrorCode.SPEND_POLICY_UNREADABLE
    assert "cannot read spend policy" in (excinfo.value.detail or "")
