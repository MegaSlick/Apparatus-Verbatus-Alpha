"""Thin local command surface for the shared create/adopt/close gate.

It deliberately requires an explicit, untracked provider factory.  The tracked
repository neither contains a credential nor chooses a provider account, GPU, or
spend ceiling.  Invoking a factory that can make a live request remains a
separately authorized action.
"""

from __future__ import annotations

import argparse
import importlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from . import notify_hooks, supervise
from .arming import ControllerArmer
from .fixture import FixtureRecorder
from .launch import LaunchResult, LaunchState, PodRuntime, phraseless
from .lease import LeaseStore
from .models import PodCreateRequest, require_utc, utc_now
from .notify_bridge import Notifier, shell_notifier, silent
from .preflight import PlacementRefusal, load_placement_table
from .provider import PodProvider
from .shutdown import VerifiedShutdown
from .spend import load_spend_policy


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verbatus gated pod launcher")
    parser.add_argument(
        "--provider-factory", required=True, help="untracked module:callable returning PodProvider"
    )
    parser.add_argument(
        "--controller-armer-factory",
        # Required for `create` and `adopt`, which arm two controllers, and
        # checked as such below. `close` arms nothing -- it is the verb an
        # operator reaches for when a pod is billing and something is already
        # wrong, and making it demand an untracked arming factory it would never
        # call would be one more thing to get right in exactly that moment.
        required=False,
        help=(
            "untracked module:callable returning a durable two-controller handshake -- "
            "normally operations.pod.controller_armer.ChannelControllerArmer, given a "
            "report channel over this launch's volume and the argv that starts "
            "operations.pod.supervise; ObservingControllerArmer beside it performs the "
            "identical read, never arms, and files what it saw, which is what a first "
            "authorized boot runs"
        ),
    )
    parser.add_argument("--spend", type=Path, default=Path("config/spend.toml"))
    parser.add_argument(
        "--placement",
        type=Path,
        default=Path("config/pod_placement.toml"),
        help=(
            "the reviewed card table a create is held to: the request's gpu_type must "
            "name one of its rows and that row's reviewed price must fit under "
            "max_hourly_usd, before any provider call"
        ),
    )
    parser.add_argument("--leases", type=Path, required=True)
    parser.add_argument("--provider-name", required=True)
    parser.add_argument(
        "--notify",
        action="store_true",
        help=(
            "send notification-only spend warnings, and a launch/close line, through "
            "operations/notify/notify.sh; off by default so nothing pages a phone "
            "unasked, and delivery never changes a launch or close decision either way"
        ),
    )
    parser.add_argument(
        "--record-fixture",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "append every provider exchange this launch sees -- method, path, request "
            "body, status, response body -- to PATH as JSON lines, credential-shaped "
            "fields and credential-shaped values scrubbed, so a drill boot leaves a "
            "replayable fixture behind. The "
            "provider must be able to record its own exchanges; the fake cannot"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="preview then optionally create a guarded pod")
    create.add_argument("--request", type=Path, required=True)
    adopt = subparsers.add_parser("adopt", help="preview then optionally adopt a guarded pod")
    adopt.add_argument("--pod-id", required=True)
    adopt.add_argument(
        "--request",
        type=Path,
        required=True,
        help="expected immutable pod/timer contract and hard deadline",
    )
    close = subparsers.add_parser(
        "close",
        help=(
            "close one live lease now, through the same verified path the supervisor "
            "uses; no preview and no typed phrase, because this verb stops spending "
            "rather than starting it"
        ),
    )
    close.add_argument("--lease", required=True, help="exact lease id to close")
    close.add_argument(
        "--reason",
        default="operator asked for an immediate close",
        help="what to record in the close report and the durable lease",
    )
    args = parser.parse_args(argv)
    if args.command in {"create", "adopt"} and not args.controller_armer_factory:
        # A printed refusal record, not `parser.error`'s usage text on stderr.
        # Every other refusal this surface makes is one JSON object on stdout
        # with a state, a green flag and a detail, and exit 2 for "nothing was
        # paid"; a caller or a log that reads those records should not have to
        # learn a second, unparseable shape for one of them (GOVERNANCE 2 --
        # a refusal that only argparse can explain is a refusal half lost).
        return _refused(
            f"{args.command} arms two controllers and requires --controller-armer-factory; "
            "no provider factory was loaded and no paid action occurred"
        )

    recorder: FixtureRecorder | None = None
    fixture_note: str | None = None
    try:
        provider = _provider(args.provider_factory)
    except Exception as error:  # noqa: BLE001 -- every factory failure is named, never a traceback
        # `_provider` raises far more than the two types this once caught: an
        # unimportable module raises `ModuleNotFoundError`, a name the module
        # does not carry raises `AttributeError`, and the factory itself raises
        # whatever it likes when it is called. Each of those used to leave the
        # `close` path with a traceback, no close, and no durable record, for a
        # lease whose pod may be billing at that moment.
        detail = (
            f"--provider-factory {args.provider_factory!r} could not be loaded and called: "
            f"{type(error).__name__}: {error}"
        )
        if args.command == "close":
            return _close_not_attempted(
                args,
                f"{detail}; no provider was reached, so nothing was terminated and the pod this "
                "lease names may still be billing -- go and look at the provider console, then "
                "close again with a factory that loads",
            )
        # For `create` and `adopt` the refusal shape is unchanged: nothing was
        # loaded, so nothing was paid, and exit 2 says exactly that.
        return _refused(f"{detail}; no paid action occurred")
    if args.record_fixture is not None:
        try:
            recorder = _record_fixture(provider, args.record_fixture)
        except Exception as error:  # noqa: BLE001 -- an optional recorder never decides a close
            # `FixtureRecorder` opens and chmods the path, so construction
            # raises `OSError` on an unwritable directory or a bad mode; the
            # provider's own `record_exchanges` may raise anything at all.
            detail = (
                str(error)
                if isinstance(error, ValueError)
                else (
                    f"--record-fixture {args.record_fixture} could not be attached: "
                    f"{type(error).__name__}: {error}"
                )
            )
            if args.command != "close":
                # `operations/pod/README.md`: a provider that cannot record its
                # own exchanges "refuses the flag by name before any preview".
                return _refused(detail)
            # ...but not for `close`. That verb exists for the moment a pod is
            # billing and something has already gone wrong, and refusing to
            # stop the meter because the evidence recorder could not be
            # attached would trade money and a live pod for a fixture nobody
            # asked for in that moment. The fact is recorded in the close
            # record instead of raised (GOVERNANCE 2).
            fixture_note = f"--record-fixture was not honoured: {detail}"
    # Wired before the preview, because the preview itself observes a balance:
    # a launch run with --notify should page the phone for the reading its own
    # spend gate made, not only for later ones.
    try:
        balance_wiring = _wire_balance_notify(provider, enabled=args.notify)
    except Exception as error:  # noqa: BLE001 -- notification is never fatal, on any verb
        # `--notify` is notification-only by ruling: a phone that cannot be
        # reached may not decide a launch, and it certainly may not abort a
        # close and leave a pod billing. The failure is written into the record
        # the same way an unwired seam already is.
        balance_wiring = _BalanceWiring(
            f"--notify could not wire balance notifications: {type(error).__name__}: {error}"
        )
    try:
        if args.command == "close":
            # No preview, no balance reading, no typed phrase, and no arming
            # seam: this verb stops spending rather than starting it, and it
            # exists for the moment when a pod is billing and something has
            # already gone wrong.
            return _close_command(
                args, provider, balance_wiring=balance_wiring, fixture_note=fixture_note
            )
        try:
            placement = load_placement_table(args.placement)
        except PlacementRefusal as error:
            # The card allowlist is a gate, so an unreadable table refuses the
            # launch rather than quietly turning the gate off.
            return _refused(
                f"the reviewed card table {args.placement} could not be read: "
                f"{error}; no paid action occurred"
            )
        runtime = PodRuntime(
            provider,
            provider_name=args.provider_name,
            spend_policy=load_spend_policy(args.spend),
            lease_root=args.leases,
            placement_table=placement,
            controller_armer=_controller_armer(args.controller_armer_factory),
            notifier=_notifier(args.notify),
        )
        request = _request(args.request)
        if args.command == "create":
            preview = runtime.preview_create(request)
        else:
            preview = runtime.preview_adopt(args.pod_id, expected=request)
        # The order is the gate: nobody can type the phrase without having been
        # shown the price and ceilings it names.
        previewed = _record(_confirmable_only(preview))
        if args.notify:
            # The preview already observed a balance, so this is the record
            # that says whether that reading reached a phone -- and it is the
            # last record printed on every path that refuses here.
            previewed["balance_notification"] = balance_wiring.to_record()
        print(json.dumps(previewed, sort_keys=True, indent=2), flush=True)
        if (
            preview.state is not LaunchState.PREVIEW
            or preview.preview is None
            or not preview.preview.assessment.allowed
        ):
            # The same exit-status rule as the post-confirmation exit below: a
            # preview refusal that observed a real pod (an adopt inspection) must
            # not read as "nothing exists".
            return 3 if _observed_something(preview) else 2
        confirmation = _typed_confirmation(preview.preview.confirmation_phrase)
        try:
            if args.command == "create":
                result = runtime.create(request, confirmation=confirmation)
            else:
                result = runtime.adopt(args.pod_id, expected=request, confirmation=confirmation)
        except KeyboardInterrupt:
            # An interrupt mid-action may have left a pod and a pending lease.
            # Say so before dying: an exit that looks like a plain abort is how a
            # billing pod goes unwatched.
            print(
                json.dumps(
                    {
                        "state": "interrupted",
                        "detail": (
                            "interrupted during the paid action; a pod and a pending lease "
                            "may exist -- inspect the leases directory and the provider "
                            "console now"
                        ),
                        "leases_root": str(args.leases),
                    },
                    sort_keys=True,
                    indent=2,
                ),
                flush=True,
            )
            raise
        record = _record(result)
        if args.record_fixture is not None:
            record["record_fixture"] = str(args.record_fixture)
        if args.notify:
            record["balance_notification"] = balance_wiring.to_record()
            try:
                _notify_launch_and_close(record, result, request, runtime.spend_policy)
            except Exception as error:  # noqa: BLE001 -- contained so the record below still prints (rule 7)
                # `notify_hooks` promises never to raise; contained here anyway so a
                # future bug in that promise cannot take this printed record with it --
                # the record naming the pod and lease is the one artifact rule 7 exists
                # to protect, and it must still print even when notification breaks.
                detail = f"notification raised and was contained: {error!r}"
                if len(detail) > 160:
                    detail = f"{detail[:160]} (reason truncated at 160 characters)"
                record["notification_error"] = detail
        print(json.dumps(record, sort_keys=True, indent=2))
        # Exit status alone must never read as "nothing happened": 0 is guarded
        # success, 2 is a refusal that made no paid action, 3 means a pod or lease
        # exists (or existed and a close was attempted) -- go and look.
        if result.green:
            return 0
        return 3 if _observed_something(result) else 2
    finally:
        if recorder is not None:
            # The append-mode handle the drill's evidence is written through.
            # Every line was already flushed and fsynced under the recorder's
            # own lock, so this loses nothing -- it closes the descriptor on
            # the way out rather than leaving it to interpreter exit.
            #
            # Contained, and said out loud: an exception raised in a `finally`
            # replaces the return value it is unwinding past, so a failing
            # descriptor close would have discarded the exit status of a
            # created pod or a completed close and raised in its place. The
            # record above is already printed; this is the note beside it.
            try:
                recorder.close()
            except Exception as error:  # noqa: BLE001 -- never past the record it would replace
                print(
                    json.dumps(
                        {
                            "state": "record-fixture-close-failed",
                            "green": False,
                            "detail": (
                                f"the --record-fixture handle {args.record_fixture} could not be "
                                f"closed: {type(error).__name__}: {error}; every line it wrote "
                                "was already flushed and fsynced, and the record above stands"
                            ),
                        },
                        sort_keys=True,
                        indent=2,
                    ),
                    flush=True,
                )


def _refused(detail: str) -> int:
    """This surface's one refusal shape: a JSON record on stdout, exit 2.

    Exit 2 is "refused, and nothing was paid". Every refusal that reaches a
    caller before a provider is asked for anything prints through here, so a
    log or a script reading these records never meets a second shape.
    """

    print(
        json.dumps(
            {"state": "refused", "green": False, "detail": detail},
            sort_keys=True,
            indent=2,
        ),
        flush=True,
    )
    return 2


_CLOSE_NOT_ATTEMPTED = "CLOSE NOT ATTEMPTED"
"""The prefix a close that never reached the provider prints, in one place.

The sibling of `UNVERIFIED CLOSE`, and deliberately a different phrase: that one
means a close ran and could not be verified, this one means no close was tried at
all. Both are exit 3 and neither may read as "nothing was paid" -- the lease
names a pod that may be billing while the operator reads the record.
"""


def _close_not_attempted(args: argparse.Namespace, detail: str) -> int:
    """Every `close` that fails before the provider is reached, in one shape.

    Exit 3, not 2. Exit 2 is this surface's "refused, and nothing was paid", and
    it is a lie about a lease: the lease exists because a paid action created
    it, and the pod it names is not stopped by this command failing to run. The
    honest status is "go and look", and the durable record is written for the
    same reason every other close outcome writes one (GOVERNANCE 2).
    """

    return _print_close_record(
        {
            "state": "refused",
            "green": False,
            "lease_id": args.lease,
            "detail": f"{_CLOSE_NOT_ATTEMPTED}: {detail}",
            "close": None,
            "lease_phase": None,
        },
        3,
        leases_root=args.leases,
        lease_id=args.lease,
    )


def _close_command(
    args: argparse.Namespace,
    provider: PodProvider,
    *,
    balance_wiring: "_BalanceWiring",
    fixture_note: str | None = None,
) -> int:
    """`close --lease <id>`: the supervisor's own close path, asked for on purpose.

    Until this verb existed a live pod could be closed only by its sealed hard
    lifetime, by a supervisor tick that happened to see a non-`RUNNING`
    provider state, or by the provider's console -- `operations/pod/README.md`
    and the first-live-tests plan both name that gap. `supervise.close_lease_now`
    does the work, so this is the same `VerifiedShutdown` standard the
    supervisor holds a pod to and not a second implementation of it.

    The spend policy is required, and for one reason: the shutdown controller's
    poll interval, deadline and billing-cutoff margin are reviewed policy
    values, and a close driven on invented timings is not the close this
    repository verifies. It is not a spend gate here -- no ceiling is consulted,
    because stopping a meter is not a paid action. Both ways that policy can
    fail -- unreadable, and unconfigured -- exit 3 through `_close_not_attempted`
    rather than 2: the lease names a pod this command did not stop, and a status
    meaning "nothing was paid" would be false about it.

    Every outcome, refusals included, leaves a durable record beside the lease
    through `supervise._write_final_record` (GOVERNANCE 2): the operator who
    reaches for this verb is usually looking at a terminal that is about to be
    closed, and a refusal that existed only in that scrollback would be lost.
    `supervise.main` has written one per run since it landed; this is the same
    record, from the same function, for the other driver of the same close.

    ``balance_wiring`` is carried into the record rather than dropped. A close
    consults no ceiling, but a vendor adapter that observes its account balance
    while terminating or capturing cost still pages the phone through the hook
    `main` attached before dispatching here, and a ping that was sent, refused,
    or never wired is a fact about this close.
    """

    leases_root: Path = args.leases
    lease_id: str = args.lease
    try:
        # Before `LeaseStore` is handed a path built from it. `--lease
        # ../../somewhere` names no lease this root can hold, and neither does
        # the id of a lease file someone renamed; both are refused here rather
        # than interpolated into a path and discovered afterwards. The durable
        # record is filed under no lease id, since there is no valid one.
        supervise.require_lease_id(lease_id)
    except supervise.SuperviseRefusal as refusal:
        return _print_close_record(
            {
                "state": "refused",
                "green": False,
                "lease_id": lease_id,
                "detail": refusal.detail,
            },
            refusal.exit_code,
            leases_root=leases_root,
            lease_id=None,
        )
    # Both policy refusals below exit 3, not 2. The lease id is syntactically a
    # lease id by this point, so the record this command refuses over is one a
    # paid action wrote, and the pod it names is not stopped by an unreadable
    # timings file. "Nothing was touched" is true and is not the whole truth:
    # exit 2 would say "nothing was paid" about a meter that may be running.
    try:
        policy = load_spend_policy(args.spend)
    except Exception as error:  # noqa: BLE001 -- a named refusal, never a traceback
        return _close_not_attempted(
            args,
            f"spend policy {args.spend} could not be read: {error}; nothing was touched, and "
            f"the pod lease {lease_id} names may still be billing -- go and look, then close "
            "again under the reviewed policy this lease was launched with",
        )
    if not policy.configured:
        return _close_not_attempted(
            args,
            f"spend policy {args.spend} is unconfigured, so this close would run on invented "
            "shutdown timings; supply the reviewed policy this lease was launched under. "
            f"Nothing was touched, and the pod lease {lease_id} names may still be billing -- "
            "go and look",
        )
    # Narrowing, not a check: a configured policy carries every one of these
    # (`SpendPolicy.__post_init__` refuses one that does not).
    assert policy.shutdown_deadline_seconds is not None
    assert policy.shutdown_poll_interval_seconds is not None
    assert policy.billing_cutoff_margin_seconds is not None
    shutdown = VerifiedShutdown(
        provider,
        timeout_seconds=float(policy.shutdown_deadline_seconds),
        poll_seconds=float(policy.shutdown_poll_interval_seconds),
        billing_cutoff_margin_seconds=policy.billing_cutoff_margin_seconds,
    )
    try:
        result, exit_code = supervise.close_lease_now(
            store=LeaseStore(Path(leases_root) / f"{lease_id}.json"),
            leases_root=Path(leases_root),
            lease_id=lease_id,
            provider_name=args.provider_name,
            shutdown=shutdown,
            reason=args.reason,
        )
    except supervise.SuperviseRefusal as refusal:
        return _print_close_record(
            {
                "state": "refused",
                "green": False,
                "lease_id": lease_id,
                "detail": refusal.detail,
            },
            refusal.exit_code,
            leases_root=leases_root,
            lease_id=lease_id,
        )
    close = result.close_report
    detail = result.detail
    if (close is not None and not close.verified) or result.state == "close-unverified":
        # The one word the operator surface reserves for this, in the record
        # and in the exit status alike: an unverified close never reads as
        # zero, and never reads as "nothing more to do".
        #
        # The second half of that test is the lease that *already* sits in
        # `close-unverified` when this verb is run again. That path makes no
        # provider call, so it carries no close report of its own -- and it is
        # reached by exactly the operator checking whether an earlier close
        # finished. A bare "lease already reached terminal phase", with the
        # word UNVERIFIED nowhere in the record, is how an unverified close
        # gets read as a finished one.
        detail = f"UNVERIFIED CLOSE: {detail}"
    record: dict[str, object] = {
        "state": result.state,
        "green": result.green,
        "lease_id": lease_id,
        "detail": detail,
        "close": close.to_record() if close is not None else None,
        "lease_phase": result.lease.phase if result.lease is not None else None,
    }
    if fixture_note is not None:
        record["record_fixture"] = fixture_note
    if args.notify:
        record["balance_notification"] = balance_wiring.to_record()
        if close is not None:
            record["close_notification"] = _notify_close_line(lease_id, result)
    return _print_close_record(record, exit_code, leases_root=leases_root, lease_id=lease_id)


def _notify_close_line(lease_id: str, result: "supervise.SuperviseResult") -> str:
    """The same close line `create` sends, from the same hook, contained the same way."""

    close = result.close_report
    assert close is not None
    billed_seconds: object = "unknown"
    if result.lease is not None:
        billed_seconds = (close.cutoff_at - result.lease.created_at).total_seconds()
    try:
        return notify_hooks.notify_close(
            lease_id=lease_id,
            verified_state=close.state.value,
            billed_seconds=billed_seconds,
        ).line()
    except Exception as error:  # noqa: BLE001 -- contained so the close record still prints
        detail = f"notification raised and was contained: {error!r}"
        return detail if len(detail) <= 160 else f"{detail[:160]} (reason truncated)"


def _print_close_record(
    record: dict[str, object],
    exit_code: int,
    *,
    leases_root: Path,
    lease_id: str | None,
) -> int:
    """Print the close record, and leave the same outcome on disk.

    GOVERNANCE 2: a refusal that exists only in a terminal has been lost. The
    durable copy is `supervise._write_final_record`, the same per-run file the
    supervisor driver writes, so both drivers of this one close path file their
    outcome in one place and one format.

    A record that cannot be written is said out loud in the printed record and
    never raised: the printed record naming the lease and the close is the
    artifact that matters most in this moment, and losing it to a failing
    durable write would be the worse of the two failures.
    """

    try:
        path = supervise._write_final_record(
            leases_root,
            lease_id,
            exit_code=exit_code,
            state=str(record.get("state", "unknown")),
            detail=str(record.get("detail", "")),
            now=utc_now(),
        )
        record["final_record"] = str(path)
    except Exception as error:  # noqa: BLE001 -- contained so the record below still prints
        record["final_record"] = f"could not be written: {error}"
    print(json.dumps(record, sort_keys=True, indent=2), flush=True)
    return exit_code


def _notify_launch_and_close(
    record: dict[str, object],
    result: LaunchResult,
    request: PodCreateRequest,
    spend_policy: object,
) -> None:
    """Gated behind ``--notify``, exactly like the existing spend-alert bridge.

    Best-effort in both directions: a failed ping is recorded in the printed
    record's own notification fields (GOVERNANCE 2 -- nothing lost silently)
    and never raised, so a broken phone can never turn a green launch or an
    already-decided close into something this command reports differently.
    """

    lease_id = result.lease_path.stem if result.lease_path else "unknown-lease"
    if result.green:
        outcome = notify_hooks.notify_launch(
            lease_id=lease_id,
            card=request.gpu_type or "unknown",
            max_hourly_usd=getattr(spend_policy, "max_hourly_usd", None),
        )
        record["launch_notification"] = outcome.line()
    close = result.close_report
    if close is not None:
        # What this number is, exactly: pod creation to the *billing cutoff*
        # the close verified against -- not the moment the pod stopped, which
        # nothing here measured. `CloseReport` carries no observed stop time,
        # and the cutoff can sit up to `billing_cutoff_margin_seconds` past
        # the absence observation, so calling this "ran Ns" reported a figure
        # no instrument took (GOVERNANCE 10). It is named for what it is.
        billed_seconds: object = "unknown"
        if result.record is not None:
            billed_seconds = (close.cutoff_at - result.record.created_at).total_seconds()
        outcome = notify_hooks.notify_close(
            lease_id=lease_id,
            verified_state=close.state.value,
            billed_seconds=billed_seconds,
        )
        record["close_notification"] = outcome.line()


def _confirmable_only(result: LaunchResult) -> LaunchResult:
    """Withhold a still-spendable challenge from every refused preview record."""

    preview = result.preview
    if preview is None or preview.assessment.allowed:
        return result
    return replace(result, preview=phraseless(preview))


def _observed_something(result: LaunchResult) -> bool:
    """True when the result names a real pod, a durable lease, or a close.

    A create refused for an open lease names no lease of its own -- the lease it
    found belongs to another action and saying otherwise would put a stranger's
    path in this result. The state is the evidence: it is reached only where this
    lease root holds an open paid action or could not be proved clear of one, so
    it exits "go and look" rather than "nothing happened".
    """

    return (
        result.state is LaunchState.REFUSED_ACTIVE_LEASE
        or result.record is not None
        or result.lease_path is not None
        or result.close_report is not None
    )


def _provider(reference: str) -> PodProvider:
    if reference.count(":") != 1:
        raise ValueError("provider factory must be module:callable")
    module_name, name = reference.split(":", 1)
    factory = getattr(importlib.import_module(module_name), name)
    provider = factory()
    if not isinstance(provider, PodProvider):
        raise TypeError("provider factory did not return the seven-verb PodProvider seam")
    return provider


def _record_fixture(provider: PodProvider, path: Path) -> FixtureRecorder:
    """Attach the recorder before the preview, or refuse before anything is paid.

    Duck-typed on ``record_exchanges`` so this surface names no vendor: an
    adapter that owns an HTTP transport routes it through the recorder; a
    provider with no such method -- the fake -- cannot honour the flag, and
    saying so here is better than a launch that silently records nothing.
    """

    attach = getattr(provider, "record_exchanges", None)
    if not callable(attach):
        raise ValueError(
            f"--record-fixture needs a provider that can record its exchanges; "
            f"{type(provider).__name__} cannot"
        )
    recorder = FixtureRecorder(path)
    attach(recorder)
    return recorder


def _controller_armer(reference: str) -> ControllerArmer:
    if reference.count(":") != 1:
        raise ValueError("controller armer factory must be module:callable")
    module_name, name = reference.split(":", 1)
    armer = getattr(importlib.import_module(module_name), name)()
    if not callable(getattr(armer, "preflight", None)) or not callable(getattr(armer, "arm", None)):
        raise TypeError("controller armer factory did not return the two-controller arming seam")
    return armer


def _request(path: Path) -> PodCreateRequest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read pod request {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("pod request must be a JSON object")
    allowed = {
        "name",
        "gpu_type",
        "image",
        "volume_id",
        "volume_mount_path",
        "docker_start_cmd",
        "hard_deadline",
        "repository_commit",
        "template",
        "metadata",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"pod request has unknown field(s) {unknown}")
    command = raw.get("docker_start_cmd")
    metadata = raw.get("metadata", {})
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError("docker_start_cmd must be an array of strings")
    if not isinstance(metadata, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in metadata.items()
    ):
        raise ValueError("metadata must map strings to strings")
    return PodCreateRequest(
        name=raw.get("name"),
        gpu_type=raw.get("gpu_type"),
        image=raw.get("image"),
        volume_id=raw.get("volume_id"),
        volume_mount_path=raw.get("volume_mount_path"),
        docker_start_cmd=tuple(command),
        hard_deadline=_timestamp(raw.get("hard_deadline")),
        repository_commit=raw.get("repository_commit"),
        template=raw.get("template"),
        metadata=metadata,
    )


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("hard_deadline must be an RFC3339 UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("hard_deadline must be an RFC3339 UTC string") from error
    try:
        return require_utc(parsed, "hard_deadline")
    except ValueError as error:
        raise ValueError("hard_deadline must be UTC") from error


@dataclass
class _BalanceWiring:
    """Whether balance notifications are wired, and what every ping did.

    Both halves are printed in the launch record. GOVERNANCE 2: a phone that
    was asked for and could not be wired, and a ping that was refused on sight
    or never delivered, are facts about this launch and are written down beside
    the pod and the lease rather than left in a return value nobody reads.
    """

    detail: str
    lines: list[str] = field(default_factory=list)

    def to_record(self) -> dict[str, object]:
        return {"wiring": self.detail, "sent": list(self.lines)}


def _wire_balance_notify(provider: PodProvider, *, enabled: bool) -> _BalanceWiring:
    """Put the phone hook on the provider's balance source, behind ``--notify``.

    Duck-typed on ``set_balance_notify`` for the same reason ``--record-fixture``
    is duck-typed on ``record_exchanges``: the provider comes from an untracked
    ``--provider-factory`` that this repository never constructs, so the only
    place the host CLI can reach a vendor adapter is a named method on the
    object the factory returned. This is what makes ``--notify`` the single
    gate for all three notification moments -- launch, close, and every
    account-balance observation -- rather than two of them.

    A provider that cannot take the hook is *recorded*, never refused: ruling
    (b) makes this seam tracking plus notifications only, and a launch that
    failed because a phone could not be reached would be exactly the new
    enforcement that ruling forbids.
    """

    if not enabled:
        return _BalanceWiring("not requested: --notify was not passed")
    try:
        attach = getattr(provider, "set_balance_notify", None)
    except Exception as error:  # noqa: BLE001 -- a vendor property may compute, and may fail
        return _BalanceWiring(
            f"--notify could not reach a balance-notification seam on "
            f"{type(provider).__name__}: {type(error).__name__}: {error}"
        )
    if not callable(attach):
        return _BalanceWiring(
            f"--notify was passed, but {type(provider).__name__} has no balance-notification "
            "seam; launch and close still page the phone, balance observations do not"
        )
    wiring = _BalanceWiring("pending")

    def notify(balance: Decimal, spend_rate: Decimal | None) -> notify_hooks.NotifyOutcome:
        # Read off the module at call time, so a test that replaces
        # `notify_hooks.notify_balance` replaces what the provider calls.
        try:
            outcome = notify_hooks.notify_balance(
                balance_usd=balance, spend_rate_usd_per_hr=spend_rate
            )
        except Exception as error:  # noqa: BLE001 -- "never raised" is the promise; contain it here too
            detail = f"the balance notification raised and was contained: {error!r}"
            if len(detail) > 160:
                detail = f"{detail[:160]} (reason truncated at 160 characters)"
            outcome = notify_hooks.NotifyOutcome(True, False, detail)
        wiring.lines.append(outcome.line())
        return outcome

    try:
        attach(notify)
    except Exception as error:  # noqa: BLE001 -- an unreachable phone never decides a close
        return _BalanceWiring(
            f"--notify could not wire balance notifications: {type(error).__name__}: {error}"
        )
    wiring.detail = "wired: every account-balance observation this launch makes pages the phone"
    return wiring


def _notifier(enabled: bool) -> Notifier:
    """Off unless the operator asked for it on this invocation.

    The spend floor is enforced by the runtime; a warning is notification-only,
    and `operations/notify`'s topic is a bearer secret. Without ``--notify``, a
    preview never reaches a phone.
    """

    return shell_notifier() if enabled else silent


def _typed_confirmation(phrase: str) -> str | None:
    """Ask after preview; EOF is a refusal rather than an automation side door."""

    try:
        return input(f"Type exactly {phrase!r} to continue with this paid action: ")
    except EOFError:
        return None


def _record(result: LaunchResult) -> dict[str, object]:
    return {
        "state": result.state.value,
        "green": result.green,
        "detail": result.detail,
        "preview": result.preview.to_record() if result.preview else None,
        "pod_id": result.record.pod_id if result.record else None,
        "lease_path": str(result.lease_path) if result.lease_path else None,
        "owner_token": "recorded locally" if result.owner_token else None,
        "close": result.close_report.to_record() if result.close_report else None,
        "controller_arming": result.controller_arming.to_record()
        if result.controller_arming
        else None,
        "controller_readiness": (
            result.controller_readiness.to_record() if result.controller_readiness else None
        ),
    }


if __name__ == "__main__":  # pragma: no cover - command wrapper
    raise SystemExit(main())
