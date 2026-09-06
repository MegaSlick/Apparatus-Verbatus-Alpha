"""Which card a create may rent, enforced where the money is spent.

`PodCreateRequest.gpu_type` is free text that goes straight to the provider.
Until this gate existed nothing in the launch path ever read
`config/pod_placement.toml` -- `PlacementTable.price_for` had no production
caller anywhere in the tree -- so the only mechanical bound on *which* card got
rented was `max_hourly_usd` against the price the provider happened to quote. A
typo, a wrong tier, or a card whose quoted price undercut its reviewed one was a
launch.

The reviewed table used here is the repository's own `config/pod_placement.toml`,
not a fixture: the point of the gate is that the shipped table is the allowlist.
The spend policies are test policies -- `config/spend.toml` is Tyrel's and stays
unconfigured.

Nothing here reaches a network, a provider account, or money.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from . import cli
from .controller_armer import ChannelControllerArmer, default_supervisor_argv
from .fake_provider import FakeProvider
from .launch import LaunchState, PodRuntime
from .models import PodCreateRequest
from .preflight import load_placement_table
from .spend import SpendPolicy

# Resolved from this file, not from the cwd. These tests assert against the
# repository's own shipped table -- that is the point of the gate -- and a
# bare relative path finds it only when pytest happens to be started from the
# repository root. Run from anywhere else it would silently read some other
# file, or none.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REVIEWED_TABLE = REPOSITORY_ROOT / "config" / "pod_placement.toml"
SPEND_POLICY_PATH = REPOSITORY_ROOT / "config" / "spend.toml"

# The Stage 1 card, by the exact string `boot_a_request.py` renders into the
# request -- the row's `gpu_type_id`, which is the only spelling the API is
# ever sent -- and its reviewed price in the table above.
A5000 = "NVIDIA RTX A5000"
A5000_ROW_NAME = "RTX A5000"
# The most expensive reviewed row: listed, and far above the Stage 1 ceiling.
BLACKWELL = "NVIDIA RTX PRO 6000 Blackwell Server Edition"


def policy(*, max_hourly: str) -> SpendPolicy:
    """A configured test policy. `config/spend.toml` is Tyrel's and is untouched."""

    return SpendPolicy(
        state="configured",
        max_hourly_usd=Decimal(max_hourly),
        max_estimated_metered_cost_usd=Decimal("5.00"),
        account_balance_floor_usd=Decimal("50.00"),
        account_balance_alert_usd=Decimal("75.00"),
        hard_lifetime_seconds=3600,
        laptop_heartbeat_timeout_seconds=30,
        shutdown_poll_interval_seconds=1,
        shutdown_deadline_seconds=8,
        billing_cutoff_margin_seconds=3600,
    )


class _SilentChannel:
    """Answers every read with a proven absence; nothing here reads anything."""

    def read(self, key: str) -> bytes | None:
        del key
        return None


def request(gpu_type: str) -> PodCreateRequest:
    return PodCreateRequest(
        name="card-allowlist-drill",
        gpu_type=gpu_type,
        image="registry.example/verbatus@sha256:" + "a" * 64,
        template="pinned-template",
        volume_id="test-volume",
        volume_mount_path="/workspace/private",
        docker_start_cmd=(
            "python",
            "-m",
            "operations.pod.pod_timer",
            "--timer-factory",
            "untracked.timer:factory",
            "--bootstrap-command-json",
            json.dumps(["python", "-m", "operations.pod.bootstrap_main", "--hold"]),
            "--report-path",
            "/workspace/private/pod-report.json",
        ),
        hard_deadline=datetime.now(UTC) + timedelta(seconds=300),
        repository_commit="b" * 40,
    )


def runtime(tmp_path: Path, provider: FakeProvider, *, max_hourly: str) -> PodRuntime:
    return PodRuntime(
        provider,
        provider_name="fake",
        spend_policy=policy(max_hourly=max_hourly),
        lease_root=tmp_path / "leases",
        placement_table=load_placement_table(REVIEWED_TABLE),
        controller_armer=armer(),
    )


def armer() -> ChannelControllerArmer:
    """The real armer, over a channel that answers every read with an absence.

    Its own preflight checks that the supervisor argv it would start is
    runnable, so the argv is the one `controller_armer.default_supervisor_argv`
    builds. Nothing here ever starts it: no preview reaches the arming step.
    """

    return ChannelControllerArmer(
        channel=_SilentChannel(),
        supervisor_argv=default_supervisor_argv(
            provider_factory="untracked.drill_provider:factory",
            spend=SPEND_POLICY_PATH,
        ),
    )


CONFIGURED_SPEND_TOML = "\n".join(
    [
        'schema = "pod-spend.v3"',
        'state = "configured"',
        'currency = "USD"',
        'max_hourly_usd = "1.00"',
        'max_estimated_metered_cost_usd = "5.00"',
        'account_balance_floor_usd = "50.00"',
        'account_balance_alert_usd = "75.00"',
        "hard_lifetime_seconds = 3600",
        "laptop_heartbeat_timeout_seconds = 30",
        "shutdown_poll_interval_seconds = 1",
        "shutdown_deadline_seconds = 8",
        "billing_cutoff_margin_seconds = 3600",
        "",
    ]
)
"""A configured test policy for the `cli.main` drills. `config/spend.toml` is
Tyrel's and stays unconfigured."""


def write_request(tmp_path: Path, ask: PodCreateRequest) -> Path:
    """The request JSON `cli.py create` reads, written from a built request."""

    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "name": ask.name,
                "gpu_type": ask.gpu_type,
                "image": ask.image,
                "template": ask.template,
                "volume_id": ask.volume_id,
                "volume_mount_path": ask.volume_mount_path,
                "docker_start_cmd": list(ask.docker_start_cmd),
                "hard_deadline": ask.hard_deadline.isoformat(),
                "repository_commit": ask.repository_commit,
            }
        ),
        encoding="utf-8",
    )
    return path


def provider_for(card: str) -> FakeProvider:
    """A fake whose price sheet would happily sell the card under test."""

    return FakeProvider({card: (Decimal("0.10"), Decimal("0.05"))})


def test_an_unlisted_card_is_refused_before_any_provider_call(tmp_path: Path) -> None:
    """The refusal names the card and every reviewed row, and nothing is asked.

    "Before any provider call" is asserted literally: `FakeProvider` records
    every verb it is asked for, including the unpriced `estimate` read, and the
    list is empty.
    """

    provider = provider_for("fake-48gb")

    result = runtime(tmp_path, provider, max_hourly="1.00").preview_create(request("fake-48gb"))

    assert result.state is LaunchState.REFUSED_CARD
    assert "'fake-48gb' is not a reviewed card" in result.detail
    assert A5000_ROW_NAME in result.detail and A5000 in result.detail
    assert result.preview is None
    assert provider.calls == [], "a refused card still reached the provider"


def test_a_reviewed_card_above_the_ceiling_is_refused_by_its_reviewed_price(
    tmp_path: Path,
) -> None:
    """Listed is not enough: the row's own price must fit the policy.

    This is the half the existing ceiling cannot do. The fake would quote
    $0.10/h for this card, and the old gate -- which only ever sees the quoted
    price -- would have allowed a $1.99/h row straight through.
    """

    provider = provider_for(BLACKWELL)

    result = runtime(tmp_path, provider, max_hourly="0.40").preview_create(request(BLACKWELL))

    assert result.state is LaunchState.REFUSED_CARD
    assert "'RTX PRO 6000 Blackwell'" in result.detail
    assert "$1.99/h" in result.detail and "$0.40" in result.detail
    assert provider.calls == []


def test_the_stage_one_card_passes_the_gate_to_the_provider(tmp_path: Path) -> None:
    """$0.27/h under a $0.40/h ceiling: reviewed, affordable, and priced.

    The preview is reached, which means the estimate was asked for -- the gate
    let this create through to the provider rather than merely failing to
    refuse it.
    """

    provider = provider_for(A5000)

    result = runtime(tmp_path, provider, max_hourly="0.40").preview_create(request(A5000))

    assert result.state is LaunchState.PREVIEW, result.detail
    assert result.preview is not None and result.preview.assessment.allowed
    assert [verb for verb, _ in provider.calls] == ["estimate", "observe_account_balance"]


def test_the_ceiling_is_net_of_the_volume_the_provider_quotes(tmp_path: Path) -> None:
    """A pod that fits alone and not beside its volume is refused, by name.

    $0.27 fits under $0.30; $0.27 plus a $0.05/h volume does not. The first
    check (before any provider call) cannot know the volume rate, so this
    refusal comes from the second one, immediately after the estimate and still
    before anything is created.
    """

    provider = provider_for(A5000)

    result = runtime(tmp_path, provider, max_hourly="0.30").preview_create(request(A5000))

    assert result.state is LaunchState.REFUSED_CARD
    assert "less the volume's $0.05/h" in result.detail
    assert "$0.25/h this policy leaves for the pod" in result.detail
    assert [verb for verb, _ in provider.calls] == ["estimate"]
    assert not provider.create_requests


def test_a_runtime_with_no_reviewed_table_enforces_no_allowlist(tmp_path: Path) -> None:
    """The seam is opt-in at the library, and `cli.py` opts in by default.

    Offline drills and tests construct `PodRuntime` with no table and are
    unaffected; the check below is what makes the two behaviours distinguishable
    rather than accidental.
    """

    provider = provider_for("fake-48gb")
    unguarded = PodRuntime(
        provider,
        provider_name="fake",
        spend_policy=policy(max_hourly="1.00"),
        lease_root=tmp_path / "leases",
        controller_armer=armer(),
    )

    assert unguarded.preview_create(request("fake-48gb")).state is LaunchState.PREVIEW


def test_the_cli_holds_a_create_to_the_reviewed_table_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shipped reviewed table is the allowlist, named the way the default names it.

    `--placement` is passed here only so the table is resolved from the
    repository root rather than from whatever directory pytest was started in;
    the value is `cli.py`'s own default file.

    Exit 2 is this surface's "refused, and nothing was paid"; the fake never
    saw a verb.
    """

    provider = provider_for("fake-48gb")
    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(CONFIGURED_SPEND_TOML, encoding="utf-8")
    request_path = write_request(tmp_path, request("fake-48gb"))
    monkeypatch.setattr(cli, "_provider", lambda _reference: provider)
    monkeypatch.setattr(cli, "_controller_armer", lambda _reference: armer())
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("a refused card asked for a confirmation")
    )

    exit_code = cli.main(
        [
            "--provider-factory",
            "unused:factory",
            "--controller-armer-factory",
            "unused:factory",
            "--spend",
            str(spend_path),
            "--leases",
            str(tmp_path / "leases"),
            "--provider-name",
            "fake",
            "--placement",
            str(REVIEWED_TABLE),
            "create",
            "--request",
            str(request_path),
        ]
    )

    assert exit_code == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["state"] == LaunchState.REFUSED_CARD.value
    assert "is not a reviewed card" in printed["detail"]
    assert printed["preview"] is None
    assert provider.calls == []


def test_the_human_row_name_is_not_an_allowlist_entry(tmp_path: Path) -> None:
    """`gpu_type_id` is the allowlist; `name` is prose for the operator.

    `boot_a_request.py` renders `"gpu_type": card.gpu_type_id`, and that string
    is the only spelling of a card this repository has ever put in a request
    body. Accepting `"RTX A5000"` too would allowlist a value no provider is
    known to take: a create could pass this gate and then fail at the API, or
    -- the worse outcome -- be resolved to some other card. So the gate matches
    what is actually sent, and the refusal still prints both columns so the
    operator can see which string to use.
    """

    provider = provider_for(A5000_ROW_NAME)

    result = runtime(tmp_path, provider, max_hourly="1.00").preview_create(request(A5000_ROW_NAME))

    assert result.state is LaunchState.REFUSED_CARD
    assert f"gpu_type {A5000_ROW_NAME!r} is not a reviewed card" in result.detail
    assert A5000 in result.detail, "the refusal must name the spelling that does work"
    assert provider.calls == []


def test_an_unreadable_placement_table_refuses_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate fails closed: a table that cannot be parsed is not an empty allowlist.

    This is the branch that decides whether a damaged or truncated
    `config/pod_placement.toml` turns the card gate off. It must not: exit 2 is
    "refused, and nothing was paid", and the fake is asked for no verb at all --
    not even the unpriced estimate.
    """

    provider = provider_for(A5000)
    damaged = tmp_path / "damaged-placement.toml"
    damaged.write_text('schema = "pod-placement.v1"\n[[tiers]\n', encoding="utf-8")
    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(CONFIGURED_SPEND_TOML, encoding="utf-8")
    request_path = write_request(tmp_path, request(A5000))
    monkeypatch.setattr(cli, "_provider", lambda _reference: provider)
    monkeypatch.setattr(cli, "_controller_armer", lambda _reference: armer())
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: pytest.fail("an unreadable table asked for a phrase")
    )

    exit_code = cli.main(
        [
            "--provider-factory",
            "unused:factory",
            "--controller-armer-factory",
            "unused:factory",
            "--spend",
            str(spend_path),
            "--leases",
            str(tmp_path / "leases"),
            "--provider-name",
            "fake",
            "--placement",
            str(damaged),
            "create",
            "--request",
            str(request_path),
        ]
    )

    assert exit_code == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["state"] == "refused"
    assert str(damaged) in str(printed["detail"])
    assert "no paid action occurred" in str(printed["detail"])
    assert provider.calls == [], "an unreadable table still reached the provider"
