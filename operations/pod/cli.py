"""Thin local command surface for the shared create/adopt gate.

It deliberately requires an explicit, untracked provider factory.  The tracked
repository neither contains a credential nor chooses a provider account, GPU, or
spend ceiling.  Invoking a factory that can make a live request remains a
separately authorized action.
"""

from __future__ import annotations

import argparse
import importlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .arming import ControllerArmer
from .launch import LaunchResult, LaunchState, PodRuntime, phraseless
from .models import PodCreateRequest, require_utc
from .notify_bridge import Notifier, shell_notifier, silent
from .provider import PodProvider
from .spend import load_spend_policy


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verbatus gated pod launcher")
    parser.add_argument(
        "--provider-factory", required=True, help="untracked module:callable returning PodProvider"
    )
    parser.add_argument(
        "--controller-armer-factory",
        required=True,
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
    parser.add_argument("--leases", type=Path, required=True)
    parser.add_argument("--provider-name", required=True)
    parser.add_argument(
        "--notify",
        action="store_true",
        help=(
            "send notification-only spend warnings through operations/notify/notify.sh; "
            "off by default so nothing pages a phone unasked, and delivery never changes "
            "a launch decision either way"
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
    args = parser.parse_args(argv)

    provider = _provider(args.provider_factory)
    runtime = PodRuntime(
        provider,
        provider_name=args.provider_name,
        spend_policy=load_spend_policy(args.spend),
        lease_root=args.leases,
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
    print(json.dumps(_record(_confirmable_only(preview)), sort_keys=True, indent=2), flush=True)
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
    print(json.dumps(_record(result), sort_keys=True, indent=2))
    # Exit status alone must never read as "nothing happened": 0 is guarded
    # success, 2 is a refusal that made no paid action, 3 means a pod or lease
    # exists (or existed and a close was attempted) -- go and look.
    if result.green:
        return 0
    return 3 if _observed_something(result) else 2


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
