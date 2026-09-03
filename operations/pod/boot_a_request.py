"""The Boot A drill, rendered as a request Tyrel can read and authorize.

`operations/pod/README.md`'s boot plan splits the first live demonstration in
two. Boot A is the drill: the cheapest reviewed card, a hard lifetime of
about fifteen minutes, `ObservingControllerArmer` (which never arms, so the
launch closes its own pod at once), `bootstrap_main --hold-only`, and
`--record-fixture` on. It buys four facts no offline test can buy -- whether
the pod-written object appears in the volume's S3 view, under which key,
after how long, and whether the pod-scoped key holds delete and billing
rights -- for minutes of a cheap card.

This module renders that request from the sealed spend policy and the
reviewed placement table, so what Tyrel reads is derived from the same
numbers the launch will enforce, not retyped beside them. **It authorizes
nothing.** GOVERNANCE 8 and CLAUDE.md hard rule 2 are one gate per exact
action in the session it happens in; this text is what that gate needs in
order to be given or refused. An unconfigured policy renders a refusal that
names what is missing, never a request with blanks in it.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Sequence

from .models import SpendRefusal, require_utc
from .preflight import CardProfile, PlacementTable, load_placement_table
from .spend import SpendPolicy, load_spend_policy

UTC = timezone.utc

BOOT_A_HARD_LIFETIME_SECONDS = 900
"""The README's "roughly 900": long enough to pull an image and watch one poll
bound, short enough that a stuck close costs minutes, not hours."""

BOOT_A_VOLUME_MOUNT_PATH = "/workspace/private"

_UNSUPPLIED = "<not yet supplied>"


@dataclass(frozen=True, slots=True)
class BootARequest:
    """What was rendered, and whether it is a request at all."""

    text: str
    refused: bool
    card: CardProfile | None
    hard_lifetime_seconds: int | None
    estimated_pod_cost_usd: Decimal | None


def render_boot_a_request(
    policy: SpendPolicy,
    placement: PlacementTable,
    *,
    image: str | None = None,
    volume_id: str | None = None,
    repository_commit: str | None = None,
    hard_deadline: str | None = None,
    now: datetime | None = None,
) -> BootARequest:
    """Render the drill request, or a refusal when the policy cannot back one."""

    if not policy.configured:
        return BootARequest(_refusal(), True, None, None, None)
    # Narrowing, not a check: `SpendPolicy.__post_init__` raises on a configured
    # policy missing any ceiling. Stated as a raise rather than three `assert`s
    # because `python -O` strips asserts, and under -O a policy that reached
    # here short a ceiling would fail on the money path with a TypeError
    # comparing Decimal to None instead of naming what is missing. The rest of
    # this package avoids `assert` for exactly that reason.
    if (
        policy.hard_lifetime_seconds is None
        or policy.max_hourly_usd is None
        or policy.max_estimated_metered_cost_usd is None
    ):
        raise ValueError(
            "a configured spend policy must name hard_lifetime_seconds, max_hourly_usd and "
            "max_estimated_metered_cost_usd; the Boot A request cannot be rendered against a "
            "ceiling that is not there"
        )
    card = cheapest_card(placement)
    lifetime = min(BOOT_A_HARD_LIFETIME_SECONDS, policy.hard_lifetime_seconds)
    pod_cost = _cents(card.hourly_usd * Decimal(lifetime) / Decimal(3600))
    obstacles: list[str] = []
    if card.hourly_usd > policy.max_hourly_usd:
        obstacles.append(
            f"the cheapest reviewed card bills ${card.hourly_usd}/h, above max_hourly_usd "
            f"${policy.max_hourly_usd}; the preview will refuse this drill"
        )
    if pod_cost > policy.max_estimated_metered_cost_usd:
        obstacles.append(
            f"the drill's own pod cost ${pod_cost} exceeds max_estimated_metered_cost_usd "
            f"${policy.max_estimated_metered_cost_usd}; the preview will refuse this drill"
        )
    if hard_deadline is not None:
        _validate_hard_deadline(
            hard_deadline, now=now or datetime.now(UTC), lifetime_seconds=lifetime
        )
    request = pod_request(
        card,
        image=image,
        volume_id=volume_id,
        repository_commit=repository_commit,
        hard_deadline=hard_deadline,
    )
    text = _render(policy, card, lifetime, pod_cost, obstacles, request)
    return BootARequest(text, False, card, lifetime, pod_cost)


def _validate_hard_deadline(value: str, *, now: datetime, lifetime_seconds: int) -> datetime:
    """The same rule ``cli._timestamp`` applies, plus a not-yet-past-lifetime floor.

    Rendered at drill authorization time, not run time -- the document is
    read and authorized later, so this refuses a deadline too close (or
    already past) rather than silently accepting one the drill cannot
    complete within.
    """

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("--hard-deadline must be an RFC3339 UTC string") from error
    try:
        parsed = require_utc(parsed, "--hard-deadline")
    except ValueError as error:
        raise ValueError("--hard-deadline must be UTC") from error
    if parsed <= now:
        raise ValueError("--hard-deadline must be in the future")
    if (parsed - now).total_seconds() < lifetime_seconds:
        raise ValueError(
            f"--hard-deadline must be at least {lifetime_seconds} seconds from now, "
            "the drill's own rendered lifetime"
        )
    return parsed


def cheapest_card(placement: PlacementTable) -> CardProfile:
    """The lowest reviewed hourly price; ties fall to the first reviewed row."""

    if not placement.card_profiles:
        raise ValueError("the placement table has no reviewed card_profile rows to choose from")
    return min(placement.card_profiles, key=lambda profile: profile.hourly_usd)


def pod_request(
    card: CardProfile,
    *,
    image: str | None,
    volume_id: str | None,
    repository_commit: str | None,
    hard_deadline: str | None = None,
    volume_mount_path: str = BOOT_A_VOLUME_MOUNT_PATH,
) -> dict[str, object]:
    """The `cli.py create --request` JSON for the drill, placeholders where Tyrel decides.

    The report paths carry no launch token: `launch._bind_report_path_to_launch`
    folds it into both the timer's own ``--report-path`` and the nested
    bootstrap argv's ``--report-path`` at sealing time, so `bootstrap_main`
    reaches its hold rather than refusing at plan time for a missing token.

    ``hard_deadline`` is the one field with no runtime that fills it in: it
    is rendered as a placeholder telling the reader to hand-supply it,
    because `cli._request` refuses to load this JSON without one -- unlike
    `metadata`'s `VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS`, which the launch
    seals from the spend policy before every create/adopt and so is accurate
    and harmless left as printed.
    """

    hold_only = [
        "python",
        "-m",
        "operations.pod.bootstrap_main",
        "--hold-only",
        "--volume-mount-path",
        volume_mount_path,
        "--report-path",
        f"{volume_mount_path}/bootstrap-hold-only-report.json",
    ]
    return {
        "name": "boot-a-drill",
        "gpu_type": card.gpu_type_id,
        "image": image or _UNSUPPLIED,
        "volume_id": volume_id or _UNSUPPLIED,
        "volume_mount_path": volume_mount_path,
        "docker_start_cmd": [
            "python",
            "-m",
            "operations.pod.pod_timer",
            "--timer-factory",
            "operations.pod.provider_runpod:timer_context_from_environment",
            "--bootstrap-command-json",
            json.dumps(hold_only),
            "--report-path",
            f"{volume_mount_path}/pod-runtime-report.json",
        ],
        "hard_deadline": hard_deadline
        or "<fill in by hand: an RFC3339 UTC timestamp at least this drill's own "
        "lifetime past now -- create will not accept this file until you do>",
        "repository_commit": repository_commit or _UNSUPPLIED,
        # Sealed by the launch from the spend policy at every create/adopt
        # (`PodRuntime._policy_bound_request`); this value is never read from
        # here and needs no hand edit.
        "metadata": {"VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS": "<the sealed policy value>"},
    }


def _refusal() -> str:
    return "\n".join(
        [
            "# Boot A drill -- REFUSED, no request rendered",
            "",
            "`config/spend.toml` is in state `unconfigured`. Nothing below it can be",
            "derived, so this is a refusal rather than a request with blanks in it:",
            "a request whose ceilings are placeholders would read as a plan, and",
            "CLAUDE.md hard rule 1 puts the ceilings and the card in Tyrel's hands.",
            "",
            "What has to exist before this file can render a request:",
            "",
            "- `config/spend.toml` in state `configured`, with every ceiling the",
            "  loader requires (`max_hourly_usd`, `max_estimated_metered_cost_usd`,",
            "  `account_balance_floor_usd`, `account_balance_alert_usd`,",
            "  `hard_lifetime_seconds`, `laptop_heartbeat_timeout_seconds`,",
            "  `shutdown_poll_interval_seconds`, `shutdown_deadline_seconds`,",
            "  `billing_cutoff_margin_seconds`).",
            "- The reviewed card table in `config/pod_placement.toml`, from which the",
            "  cheapest card is taken (already present).",
            "",
            "No pod, lease, preview or provider call results from this refusal.",
            "",
        ]
    )


def _render(
    policy: SpendPolicy,
    card: CardProfile,
    lifetime: int,
    pod_cost: Decimal,
    obstacles: list[str],
    request: dict[str, object],
) -> str:
    minutes = Decimal(lifetime) / Decimal(60)
    lines = [
        "# Boot A drill -- request for authorization",
        "",
        "This text authorizes nothing. It is derived from the sealed spend policy and",
        "the reviewed placement table so that what you read is what the launch will",
        "enforce. Permission is given, or refused, in the session the drill runs in,",
        "for this exact action (GOVERNANCE 8; CLAUDE.md hard rules 1 and 2).",
        "",
        "## What the drill does",
        "",
        f"1. Creates one on-demand pod on the cheapest reviewed card, **{card.name}**",
        f"   (`{card.gpu_type_id}`, {card.vram_gib} GiB, ${card.hourly_usd}/h from the",
        "   reviewed price sheet), with the network volume attached at creation.",
        f"2. Seals a hard lifetime of **{lifetime} seconds** (about {minutes:.0f} minutes):",
        "   the laptop supervisor and the pod-side dead-man timer each enforce it",
        "   independently, whichever fires first.",
        "3. Arms with **`ObservingControllerArmer`** -- the drill armer. It starts the",
        "   laptop supervisor, performs the real read of the pod's report through the",
        "   volume's S3 view, and reports the pod timer acknowledged as **False whatever",
        "   it observes**. `launch._arm_or_close` therefore closes the pod at once.",
        "4. The pod's primary process is the provider-neutral timer, whose mandatory",
        "   bootstrap is **`bootstrap_main --hold-only`**: no bootstrap steps, a",
        "   hold-only journal record, hold to the deadline. It pulls no model.",
        "5. **`--record-fixture` is on**: every provider exchange the launch sees is",
        "   appended, credential-shaped fields and values scrubbed, to a named",
        "   evidence file, so the drill leaves a replayable fixture behind.",
        "",
        "## Expected outcome",
        "",
        "An **immediate close**. The result is green only when exact-pod GET-404, the",
        "independent pod-list absence and non-empty exact-pod billing all agree; a",
        "non-verified close is reported as such and the supervisor keeps guarding",
        "the lease. The drill records: whether the pod-written object appeared in",
        "the S3 view, under which key, after how long; whether the pod-scoped key",
        "holds delete and billing rights; and the account balance the GraphQL",
        "observer read, beside the ceilings.",
        "",
        "## Ceilings the launch will enforce",
        "",
        f"- `max_hourly_usd` = ${policy.max_hourly_usd} (pod plus attached volume)",
        f"- `max_estimated_metered_cost_usd` = ${policy.max_estimated_metered_cost_usd}",
        f"- `account_balance_floor_usd` = ${policy.account_balance_floor_usd} (a reserve that",
        "  must survive the run, tested net of this drill's estimated cost)",
        f"- `account_balance_alert_usd` = ${policy.account_balance_alert_usd} (notification only)",
        f"- `hard_lifetime_seconds` ceiling = {policy.hard_lifetime_seconds}; this drill",
        f"  requests {lifetime}",
        f"- `billing_cutoff_margin_seconds` = {policy.billing_cutoff_margin_seconds}",
        "",
        "## Cost",
        "",
        f"Pod: ${card.hourly_usd}/h x {lifetime}s = **about ${pod_cost}** if it ran the full",
        "lifetime; the expected immediate close makes it less. The attached network",
        "volume's ongoing hourly price is added at preview from the provider factory's",
        "price sheet and stated in the close report; the drill neither deletes nor",
        "retains the volume -- that is a separate decision.",
        "",
    ]
    if obstacles:
        lines += ["## Why the preview will refuse as configured", ""]
        lines += [f"- {obstacle}" for obstacle in obstacles]
        lines += [""]
    lines += [
        "## What only Tyrel supplies",
        "",
        "- In-session permission for this exact drill (and, separately, for Boot B).",
        "- The pinned image digest, the network volume id and the repository commit",
        "  below where they still read as not yet supplied.",
        "- `hard_deadline` below, an RFC3339 UTC timestamp at least this drill's own",
        "  lifetime past whenever it is authorized -- `create` will not accept the",
        "  file until it is filled in by hand.",
        "- The S3 access and secret keys in the launching shell, for the report channel.",
        "- The untracked provider and controller-armer factories the command names.",
        "",
        "`metadata`'s `VERBATUS_BILLING_CUTOFF_MARGIN_SECONDS` needs no hand edit: the",
        "launch seals it from the spend policy before every create or adopt.",
        "",
        "## The command",
        "",
        "```",
        "python -m operations.pod.cli \\",
        "  --provider-factory <untracked module:callable returning RunPodProvider> \\",
        "  --controller-armer-factory <untracked module:callable returning ObservingControllerArmer> \\",
        "  --spend config/spend.toml --leases <lease root for this account> \\",
        "  --provider-name runpod --notify \\",
        "  --record-fixture workbench/raw/boot-a-fixture.jsonl \\",
        "  create --request <path to the JSON below>",
        "```",
        "",
        "It prints the price and ceilings, then asks for the typed phrase; the phrase",
        "is an operational guard, not the live-pod permission.",
        "",
        "## The pod request",
        "",
        "```json",
        json.dumps(request, indent=2, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(lines)


def _cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_UP)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the Boot A drill request from the sealed spend policy",
        allow_abbrev=False,
    )
    parser.add_argument("--spend", type=Path, default=Path("config/spend.toml"))
    parser.add_argument("--placement", type=Path, default=Path("config/pod_placement.toml"))
    parser.add_argument("--image")
    parser.add_argument("--volume-id")
    parser.add_argument("--repository-commit")
    parser.add_argument(
        "--hard-deadline",
        help="RFC3339 UTC timestamp; omit to leave it a fill-in-by-hand placeholder",
    )
    args = parser.parse_args(argv)
    try:
        rendered = render_boot_a_request(
            load_spend_policy(args.spend),
            load_placement_table(args.placement),
            image=args.image,
            volume_id=args.volume_id,
            repository_commit=args.repository_commit,
            hard_deadline=args.hard_deadline,
        )
    # `SpendRefusal` beside `ValueError`, not inside it: the spend policy's
    # own refusal is a `PodRuntimeError`, so an unreadable or invalid
    # `config/spend.toml` -- the one file this command exists to read -- came
    # out as a traceback where the placement table's `PlacementRefusal`, a
    # `ValueError`, printed REFUSED and exited 2. Both are the same fact to a
    # reader: the drill cannot be rendered from what the configuration says.
    except (ValueError, SpendRefusal) as error:
        print(f"# Boot A drill -- REFUSED\n\n{error}\n", end="")
        return 2
    print(rendered.text, end="")
    return 2 if rendered.refused else 0


if __name__ == "__main__":  # pragma: no cover - command wrapper
    raise SystemExit(main())
