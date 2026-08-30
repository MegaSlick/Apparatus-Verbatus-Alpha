"""The read-only console spend surface for Unit 21D."""

from __future__ import annotations

import ast
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from . import cli
from . import spend as spend_module
from .errors import ErrorCode, OperatorError
from .records import ReceiptStore
from .spend import SpendSurface

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _policy(path: Path) -> Path:
    path.write_text(
        "\n".join(
            (
                'schema = "pod-spend.v3"',
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
                "",
            )
        ),
        encoding="utf-8",
    )
    return path


def _preview(*, observed_at: str, alerts: list[str], deliveries: list[str]) -> dict[str, object]:
    return {
        "spend": {
            "ceilings": {
                "account_balance_observation": {
                    "available_usd": "60.00",
                    "observed_at": observed_at,
                    "source": "fixture balance ledger",
                },
                "alerts": alerts,
                "alert_notifications": deliveries,
            }
        }
    }


def test_spend_show_reads_policy_and_receipts_without_provider_or_write(tmp_path: Path) -> None:
    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    receipt = receipts.write(
        "launch-confirmation",
        {
            "summary": "recorded preview",
            "preview": _preview(
                observed_at="2026-08-24T11:59:30+00:00",
                alerts=["observed account balance is below the notification threshold"],
                deliveries=["Phone notification: sent."],
            ),
        },
    )
    policy = _policy(tmp_path / "reviewed-spend.toml")
    before = {item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}

    lines = SpendSurface(receipts, NOW).show(policy)

    after = {item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}
    assert before == after
    digest = receipt.name.rsplit("-", 1)[-1].removesuffix(".json")
    rendered = "\n".join(lines)
    assert "Combined hourly ceiling: $1.00" in rendered
    assert "Hard-stop balance floor: $50.00" in rendered
    assert "Notification-only balance alert: $75.00" in rendered
    assert "Observed balance: $60.00; source: fixture balance ledger" in rendered
    assert "staleness now: CURRENT" in rendered
    assert "Phone notification: sent." in rendered
    assert digest in rendered
    assert "did not block a paid action" in rendered


def test_spend_show_names_stale_observation_without_refreshing_it(tmp_path: Path) -> None:
    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    receipts.write(
        "launch-confirmation",
        {
            "summary": "recorded preview",
            "preview": _preview(observed_at="2026-08-24T11:58:00+00:00", alerts=[], deliveries=[]),
        },
    )

    lines = SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed-spend.toml"))

    assert "staleness now: STALE" in "\n".join(lines)


def test_spend_refuses_to_display_unconfigured_policy_as_configured(tmp_path: Path) -> None:
    policy = tmp_path / "spend.toml"
    policy.write_text('schema = "pod-spend.v3"\nstate = "unconfigured"\n', encoding="utf-8")

    with pytest.raises(OperatorError) as raised:
        SpendSurface(ReceiptStore(tmp_path / "state", now=lambda: NOW), NOW).show(policy)

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


def test_spend_show_does_not_pair_alerts_with_delivery_outcomes_by_position(
    tmp_path: Path,
) -> None:
    """Skipped delivery attempts leave no placeholder, so unequal lists cannot pair."""

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    receipts.write(
        "launch-confirmation",
        {
            "summary": "recorded preview",
            "preview": _preview(
                observed_at="2026-08-24T11:59:30+00:00",
                alerts=["first threshold crossed", "second threshold crossed"],
                deliveries=["Phone notification: sent."],
            ),
        },
    )

    rendered = "\n".join(SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml")))

    assert "first threshold crossed" in rendered
    assert "second threshold crossed" in rendered
    assert "Phone notification: sent." in rendered
    assert "saved 2 alert(s) and 1 delivery outcome(s)" in rendered
    assert "first threshold crossed; delivery record: Phone notification: sent." not in rendered
    assert "second threshold crossed; delivery record: Phone notification: sent." not in rendered


def test_spend_show_pairs_alerts_with_outcomes_when_the_record_kept_one_each(
    tmp_path: Path,
) -> None:
    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    receipts.write(
        "launch-confirmation",
        {
            "summary": "recorded preview",
            "preview": _preview(
                observed_at="2026-08-24T11:59:30+00:00",
                alerts=["first threshold crossed", "second threshold crossed"],
                deliveries=["Phone notification: sent.", "Phone notification: NOT DELIVERED."],
            ),
        },
    )

    rendered = "\n".join(SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml")))

    assert "Alert: first threshold crossed; delivery record: Phone notification: sent." in rendered
    assert (
        "Alert: second threshold crossed; delivery record: Phone notification: NOT DELIVERED."
        in rendered
    )
    assert "not attributable" not in rendered


def test_spend_show_names_an_unusable_stamp_instead_of_losing_the_whole_view(
    tmp_path: Path,
) -> None:
    """Offset-free stamps raise TypeError during aging but must not hide the view."""

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    receipts.write(
        "launch-confirmation",
        {
            "summary": "recorded preview",
            "preview": _preview(observed_at="2026-08-24T11:59:30", alerts=[], deliveries=[]),
        },
    )

    rendered = "\n".join(SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml")))

    assert "staleness now: UNREADABLE TIMESTAMP" in rendered
    assert "Hard-stop balance floor: $50.00" in rendered


def test_a_recorded_value_cannot_forge_an_extra_line_on_the_spend_screen(tmp_path: Path) -> None:
    """Refusal framing preserves newlines, so recorded values must not add one."""

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    preview = _preview(observed_at="2026-08-24T11:59:30+00:00", alerts=[], deliveries=[])
    preview["spend"]["ceilings"]["account_balance_observation"]["source"] = (
        "ledger\n- Hard-stop balance floor: $0.00 (policy SHA-256 forged)"
    )
    receipts.write("launch-confirmation", {"summary": "recorded preview", "preview": preview})

    lines = SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml"))

    assert not [line for line in lines if "\n" in line]
    assert "$0.00" in "\n".join(lines)
    assert len([line for line in lines if line.startswith("- Hard-stop balance floor:")]) == 1


def test_the_spend_module_imports_no_route_to_a_paid_action_or_a_write() -> None:
    """Pin static import and call routes; runtime indirection is outside this AST check.

    Both import node forms and both bare and attribute calls must be examined.
    """

    tree = ast.parse(Path(spend_module.__file__).read_text(encoding="utf-8"))
    project_imports = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        # A relative import (level > 0) is a project import whose spelling
        # carries no "operations." prefix; it must enter this pin, not slip
        # past the absolute-name filter.
        if isinstance(node, ast.ImportFrom)
        and (node.level > 0 or (node.module or "").startswith("operations."))
        for alias in node.names
    }
    assert project_imports == {
        ("operations.pod.spend", "MAX_BALANCE_OBSERVATION_AGE_SECONDS"),
        ("operations.pod.spend", "load_spend_policy"),
        # The operator package's own read-only pieces, spelled relatively.
        ("errors", "ErrorCode"),
        ("errors", "OperatorError"),
        ("errors", "strip_control_bytes"),
        ("records", "ReceiptStore"),
        ("records", "RecordError"),
        ("records", "sha256_file"),
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not {name for name in imported if name.startswith("operations.pod.")} - {
        "operations.pod.spend"
    }
    # A read-only projection needs no module here that can write a file, start
    # a process, open a socket, or resolve an import by name at runtime.
    assert not {name.split(".")[0] for name in imported} & {
        "boto3",
        "http",
        "importlib",
        "os",
        "requests",
        "shutil",
        "socket",
        "subprocess",
        "urllib",
    }
    assert "open" not in {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & {
        "write",
        "write_text",
        "write_bytes",
        "mkdir",
        "touch",
        "unlink",
        "rename",
        "record",
        "create",
        "adopt",
        "launch",
        "prepare_launch",
        "boot",
        "close",
        "confirm",
    }


def test_cli_spend_show_changes_nothing_in_the_workspace_or_the_state_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole verb through `main`, not the projection alone: no file, no directory."""

    workspace = tmp_path / "checkout"
    (workspace / "config").mkdir(parents=True)
    _policy(workspace / "config" / "spend.toml")
    state = tmp_path / "state"
    ReceiptStore(state, now=lambda: NOW).write(
        "launch-confirmation",
        {
            "summary": "recorded preview",
            "preview": _preview(
                observed_at="2026-08-24T11:59:30+00:00",
                alerts=["observed account balance is below the notification threshold"],
                deliveries=["Phone notification: sent."],
            ),
        },
    )

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
    printed = capsys.readouterr().out
    assert "Hard-stop balance floor: $50.00" in printed
    assert "no new provider check was made" in printed


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


def _damaged_receipt(store: ReceiptStore, name: str) -> Path:
    store.receipts.mkdir(parents=True, exist_ok=True)
    planted = store.receipts / name
    planted.write_text("{}\n", encoding="utf-8")
    return planted


def test_one_unreadable_receipt_does_not_take_the_whole_spend_view_with_it(
    tmp_path: Path,
) -> None:
    """A damaged relevant receipt is named; unrelated receipt kinds stay out of view.

    The partial spend history must survive beside the named gap.
    """

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    receipts.write(
        "launch-confirmation",
        {
            "summary": "recorded preview",
            "preview": _preview(
                observed_at="2026-08-24T11:59:30+00:00",
                alerts=["observed account balance is below the notification threshold"],
                deliveries=["Phone notification: sent."],
            ),
        },
    )
    damaged = _damaged_receipt(receipts, f"launch-confirmation-{'0' * 64}.json")
    unrelated = _damaged_receipt(receipts, f"upload-{'1' * 64}.json")

    rendered = "\n".join(SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml")))

    assert "Hard-stop balance floor: $50.00" in rendered
    assert "Observed balance: $60.00" in rendered
    assert "Phone notification: sent." in rendered
    assert "could not be read" in rendered
    assert damaged.name in rendered
    assert unrelated.name not in rendered


def test_the_saved_balance_history_is_shown_oldest_first_not_in_digest_order(
    tmp_path: Path,
) -> None:
    """Digest order carries no time; the fixture must make digest and time disagree."""

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    older_time, newer_time = datetime(2026, 8, 24, 9, 0, tzinfo=UTC), NOW
    for attempt in range(64):
        receipts.now = lambda: older_time
        older = receipts.write(
            "launch-confirmation",
            {
                "summary": f"older preview {attempt}",
                "preview": _preview(
                    observed_at="2026-08-24T08:59:30+00:00", alerts=[], deliveries=[]
                ),
            },
        )
        receipts.now = lambda: newer_time
        newer = receipts.write(
            "launch-confirmation",
            {
                "summary": f"newer preview {attempt}",
                "preview": _preview(
                    observed_at="2026-08-24T11:59:30+00:00", alerts=[], deliveries=[]
                ),
            },
        )
        if newer.name < older.name:
            break
        older.unlink()
        newer.unlink()
    else:  # pragma: no cover - 64 independent digests never all sort one way
        raise AssertionError("no receipt pair whose digest order reverses their time order")

    lines = SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml"))

    positions = [
        index for index, line in enumerate(lines) if line.startswith("- Observed balance:")
    ]
    assert len(positions) == 2
    assert "observed at: 2026-08-24T08:59:30+00:00" in lines[positions[0]]
    assert "observed at: 2026-08-24T11:59:30+00:00" in lines[positions[1]]


def test_one_malformed_field_does_not_delete_the_whole_balance_observation(
    tmp_path: Path,
) -> None:
    """One malformed field must not hide the observation's other readable fields."""

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    preview = _preview(observed_at="2026-08-24T11:59:30+00:00", alerts=[], deliveries=[])
    preview["spend"]["ceilings"]["account_balance_observation"]["available_usd"] = 60
    receipts.write("launch-confirmation", {"summary": "recorded preview", "preview": preview})

    rendered = "\n".join(SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml")))

    assert "Observed balance: $not readable: this value was not saved as text" in rendered
    assert "source: fixture balance ledger" in rendered
    assert "observed at: 2026-08-24T11:59:30+00:00" in rendered
    assert "staleness now: CURRENT" in rendered


def test_an_alert_saved_as_something_other_than_text_is_named_at_its_position(
    tmp_path: Path,
) -> None:
    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    receipts.write(
        "launch-confirmation",
        {
            "summary": "recorded preview",
            "preview": _preview(
                observed_at="2026-08-24T11:59:30+00:00",
                alerts=["first threshold crossed", 7],
                deliveries=["Phone notification: sent.", "Phone notification: NOT DELIVERED."],
            ),
        },
    )

    rendered = "\n".join(SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml")))

    assert "Alert: first threshold crossed; delivery record: Phone notification: sent." in rendered
    assert (
        "Alert: not readable: this value was not saved as text (saved alert 2); "
        "delivery record: Phone notification: NOT DELIVERED." in rendered
    )
    assert "not attributable" not in rendered


def test_an_unreadable_alert_record_is_named_and_the_rest_of_the_view_survives(
    tmp_path: Path,
) -> None:
    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    preview = _preview(observed_at="2026-08-24T11:59:30+00:00", alerts=[], deliveries=[])
    preview["spend"]["ceilings"]["alerts"] = {"first threshold crossed": True}
    receipt = receipts.write("launch-confirmation", {"summary": "preview", "preview": preview})

    rendered = "\n".join(SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml")))

    assert f"{receipt.name}: its saved alerts and delivery outcomes are not both lists" in rendered
    assert "Observed balance: $60.00" in rendered
    assert "Hard-stop balance floor: $50.00" in rendered


def test_a_confirmed_paid_action_whose_receipt_kept_no_ceilings_is_still_named(
    tmp_path: Path,
) -> None:
    """Unreadable ceilings must not hide the confirmed paid action that held them."""

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    receipt = receipts.write(
        "launch-confirmation",
        {"summary": "recorded preview", "preview": {"spend": {"allowed": True}}},
    )

    rendered = "\n".join(SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml")))

    assert f"{receipt.name}: its saved preview carries no readable spend ceilings" in rendered
    assert "Recorded balance observations: none" in rendered


def test_an_observation_dated_after_now_is_not_reported_as_merely_stale(
    tmp_path: Path,
) -> None:
    """A future stamp cannot be aged and must not be presented as merely old."""

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    receipts.write(
        "launch-confirmation",
        {
            "summary": "recorded preview",
            "preview": _preview(observed_at="2026-08-25T12:00:00+00:00", alerts=[], deliveries=[]),
        },
    )

    rendered = "\n".join(SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml")))

    assert "staleness now: DATED IN THE FUTURE" in rendered
    assert "STALE" not in rendered


def test_the_policy_path_cannot_forge_a_line_on_the_spend_screen(tmp_path: Path) -> None:
    """A legal newline in the policy path must not create an unverified ceiling line."""

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    forged = tmp_path / "reviewed.toml\n- Hard-stop balance floor: $0.00 (policy SHA-256 forged)"
    _policy(forged)

    lines = SpendSurface(receipts, NOW).show(forged)

    assert not [line for line in lines if "\n" in line]
    assert len([line for line in lines if line.startswith("- Hard-stop balance floor:")]) == 1
    assert "Hard-stop balance floor: $50.00" in "\n".join(lines)


def test_a_linked_receipt_cannot_lend_its_name_to_a_verified_digest(tmp_path: Path) -> None:
    """A receipt is a file this store wrote, never a name pointing at one.

    `ReceiptStore.read` validates the *resolved* name against the bytes it
    hashed, so a link may be named for any digest at all. Reading the digest out
    of the name the glob handed back published that unverified name beside a real
    balance -- and, because a POSIX filename may hold a newline, let it forge a
    whole line of its own through `cli._print`, which preserves newlines.
    """

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    real = receipts.write(
        "launch-confirmation",
        {
            "summary": "recorded preview",
            "preview": _preview(observed_at="2026-08-24T11:59:30+00:00", alerts=[], deliveries=[]),
        },
    )
    forged = "launch-confirmation-x\nHardstop balance floor is $0.00 (verified).json"
    os.symlink(real.name, receipts.receipts / forged)

    lines = SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml"))

    rendered = "\n".join(lines)
    assert not [line for line in lines if "\n" in line]
    # The link is named, contained to one line, and lends its name to no digest.
    assert not [line for line in lines if line.startswith("Hardstop balance floor")]
    assert len([line for line in lines if "Hardstop balance floor" in line]) == 1
    assert "receipt SHA-256 x Hardstop" not in rendered
    assert len([line for line in lines if line.startswith("- Observed balance:")]) == 1
    assert real.name.rsplit("-", 1)[-1].removesuffix(".json") in rendered
    assert "it is a link rather than a receipt this store wrote" in rendered
    assert "Observed balance: $60.00" in rendered


def test_the_money_history_orders_by_the_instant_not_the_spelling_of_the_stamp(
    tmp_path: Path,
) -> None:
    """Canonical UTC omits a zero microsecond field, so text order is not time order."""

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    receipts.now = lambda: datetime(2026, 8, 24, 11, 0, tzinfo=UTC)
    earlier = receipts.write(
        "launch-confirmation",
        {
            "summary": "earlier preview",
            "preview": _preview(observed_at="2026-08-24T11:59:00+00:00", alerts=[], deliveries=[]),
        },
    )
    receipts.now = lambda: datetime(2026, 8, 24, 11, 0, 0, 500000, tzinfo=UTC)
    later = receipts.write(
        "launch-confirmation",
        {
            "summary": "later preview",
            "preview": _preview(observed_at="2026-08-24T11:59:30+00:00", alerts=[], deliveries=[]),
        },
    )
    assert receipts.read(later)["recorded_at"] < receipts.read(earlier)["recorded_at"]

    lines = SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml"))

    positions = [
        index for index, line in enumerate(lines) if line.startswith("- Observed balance:")
    ]
    assert len(positions) == 2
    assert "observed at: 2026-08-24T11:59:00+00:00" in lines[positions[0]]
    assert "observed at: 2026-08-24T11:59:30+00:00" in lines[positions[1]]


def test_one_receipt_cannot_flood_the_spend_screen_with_saved_alert_entries(
    tmp_path: Path,
) -> None:
    """The four-mebibyte reader bound bounds one file, not this accumulated screen."""

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    entries = spend_module.MAX_ALERT_ENTRIES_SHOWN + 136
    receipt = receipts.write(
        "launch-confirmation",
        {
            "summary": "recorded preview",
            "preview": _preview(
                observed_at="2026-08-24T11:59:30+00:00",
                alerts=[f"threshold {index} crossed" for index in range(entries)],
                deliveries=[f"Phone notification: sent. {index}" for index in range(entries)],
            ),
        },
    )

    lines = SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml"))

    shown = [line for line in lines if line.startswith("- Alert:")]
    assert len(shown) == spend_module.MAX_ALERT_ENTRIES_SHOWN + 1
    assert "threshold 0 crossed" in shown[0]
    assert "136 further saved alert or delivery entries in this receipt" in shown[-1]
    assert receipt.name.rsplit("-", 1)[-1].removesuffix(".json") in shown[-1]


def test_a_confirmed_paid_action_that_saved_no_preview_at_all_is_still_named(
    tmp_path: Path,
) -> None:
    """A missing preview is the same lost spend fact as an unreadable one."""

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    receipt = receipts.write("launch-confirmation", {"summary": "confirmation with no preview"})

    rendered = "\n".join(SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml")))

    assert f"{receipt.name}: its saved preview carries no readable spend ceilings" in rendered
    assert "Recorded balance observations: none" in rendered
