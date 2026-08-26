"""The read-only console view of Unit 22's spend records."""

from __future__ import annotations

import ast
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
                'schema = "pod-spend.v2"',
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
    policy.write_text('schema = "pod-spend.v2"\nstate = "unconfigured"\n', encoding="utf-8")

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
    """Unequal counts mean the record kept no pairing, and the screen may not invent one.

    `operations/pod/launch.py:_record_spend_notifications` appends a delivery
    line only for an alert episode it actually attempted, so a skipped episode
    shifts every later outcome down one index rather than leaving a hole. Reading
    position as identity across that shift would print the second alert's
    delivery outcome under the first alert's name — a claim about a delivery that
    nothing measured (GOVERNANCE 10). Both lists still reach the screen, neither
    dropped (GOVERNANCE 2).
    """

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
    # The single outcome is never shown as belonging to either named alert.
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
    """A stamp with no offset parses to a naive datetime; subtracting it raises TypeError.

    The observation fields are free text out of a saved record, which is why the
    surface guards them at all. Catching only `ValueError` let one malformed
    stamp take the ceilings, the floor and every sound observation down with it.
    """

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
    """`cli._print` keeps newlines so a three-part refusal stays three parts.

    That makes a newline inside a recorded value a way to print a line carrying
    no receipt digest, in the shape of a policy line this surface vouched for.
    """

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    preview = _preview(observed_at="2026-08-24T11:59:30+00:00", alerts=[], deliveries=[])
    preview["spend"]["ceilings"]["account_balance_observation"]["source"] = (
        "ledger\n- Hard-stop balance floor: $0.00 (policy SHA-256 forged)"
    )
    receipts.write("launch-confirmation", {"summary": "recorded preview", "preview": preview})

    lines = SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml"))

    assert not [line for line in lines if "\n" in line]
    assert "$0.00" in "\n".join(lines)  # the bytes are shown, GOVERNANCE 4
    assert len([line for line in lines if line.startswith("- Hard-stop balance floor:")]) == 1


def test_the_spend_module_imports_no_route_to_a_paid_action_or_a_write() -> None:
    """The money boundary as a property of the module, not of one call path.

    A reader can confirm today that `show` reaches no provider and writes
    nothing. This fails the day an import or a call gives it one, which is the
    part a later reader would otherwise have to re-derive by hand.
    """

    tree = ast.parse(Path(spend_module.__file__).read_text(encoding="utf-8"))
    project_imports = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("operations.")
        for alias in node.names
    }
    assert project_imports == {
        ("operations.pod.spend", "MAX_BALANCE_OBSERVATION_AGE_SECONDS"),
        ("operations.pod.spend", "load_spend_policy"),
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
    """A file the store will refuse to read, left where a receipt belongs."""

    store.receipts.mkdir(parents=True, exist_ok=True)
    planted = store.receipts / name
    planted.write_text("{}\n", encoding="utf-8")
    return planted


def test_one_unreadable_receipt_does_not_take_the_whole_spend_view_with_it(
    tmp_path: Path,
) -> None:
    """A damaged file names itself; it does not delete the ceilings and the floor.

    `records_of_kind` raises on the first record that will not read, so before
    this the screen lost the reviewed policy, the hard-stop floor and every
    sound observation because one file in the directory was unreadable — the
    partial result vanishing behind a refusal rather than being shown as
    partial (GOVERNANCE 2). An unreadable receipt of a kind this view never
    projects stays `status`'s account to give, not this one's.
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
    """Receipt filenames are digests, so their sort order carries no time at all.

    A money history listed in that order invites its last line to be read as
    its latest. The pair below is chosen so digest order and time order
    disagree, which is the only arrangement that can tell the two apart.
    """

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
    """Amount, source and stamp were shown only if all three were strings.

    An observation whose amount was saved as a number therefore vanished with
    its source and its stamp, and vanished silently — on the one screen that
    exists to account for observed money (GOVERNANCE 2).
    """

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
    """Skipping it dropped its delivery outcome too, one warning short of the record."""

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
    """A launch confirmation records a paid action that was taken.

    Dropping it because its ceilings will not read shows fewer confirmed paid
    actions than the receipts hold, which is the one thing a money screen may
    not do quietly.
    """

    receipts = ReceiptStore(tmp_path / "state", now=lambda: NOW)
    receipt = receipts.write(
        "launch-confirmation",
        {"summary": "recorded preview", "preview": {"spend": {"allowed": True}}},
    )

    rendered = "\n".join(SpendSurface(receipts, NOW).show(_policy(tmp_path / "reviewed.toml")))

    assert f"{receipt.name}: its saved preview carries no readable spend ceilings" in rendered
    assert "Recorded balance observations: none" in rendered
