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
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .arming import ControllerArmer
from .launch import LaunchResult, LaunchState, PodRuntime
from .models import PodCreateRequest, require_utc
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
        help="untracked module:callable returning a durable two-controller handshake",
    )
    parser.add_argument("--spend", type=Path, default=Path("config/spend.toml"))
    parser.add_argument("--leases", type=Path, required=True)
    parser.add_argument("--provider-name", required=True)
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
    )
    request = _request(args.request)
    if args.command == "create":
        preview = runtime.preview_create(request)
    else:
        preview = runtime.preview_adopt(args.pod_id, expected=request)
    # The order is the gate: nobody can type the phrase without having been
    # shown the price and ceilings it names.
    print(json.dumps(_record(preview), sort_keys=True, indent=2), flush=True)
    if (
        preview.state is not LaunchState.PREVIEW
        or preview.preview is None
        or not preview.preview.assessment.allowed
    ):
        return 2
    confirmation = _typed_confirmation(preview.preview.confirmation_phrase)
    if args.command == "create":
        result = runtime.create(request, confirmation=confirmation)
    else:
        result = runtime.adopt(args.pod_id, expected=request, confirmation=confirmation)
    print(json.dumps(_record(result), sort_keys=True, indent=2))
    return 0 if result.green else 2


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
