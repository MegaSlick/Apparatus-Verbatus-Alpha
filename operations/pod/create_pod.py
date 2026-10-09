"""Create one guarded pod on the hand route with a global volume attached.

The hand route (`operations/pod/README.md`) starts a pod with the guard's start
command and nothing else from the managed runtime. `runpodctl`, the connector
and REST cannot attach a RunPod global volume, so this is the hand route's
create: one `podFindAndDeployOnDemand` mutation over the same transport the
balance observer uses, then the pod's answer checked against what was asked.

    START=$(sh operations/pod/pod_start_command.sh off <sha>) &&
    .venv/bin/python -m operations.pod.create_pod --name verbatus-bakeoff-w \\
      --gpu "NVIDIA A40" --image runpod/pytorch:1.4.0-cu1300-torch2130-ubuntu2404 \\
      --container-disk-gb 100 --disk-gb 100 --min-vcpu 16 --cuda 13.0 \\
      --global-volume <id> --global-mount /workspace/global --start-command "$START"

The account key is read from the shell variable `runpodctl` uses
(`provider_runpod.ACCOUNT_KEY_ENVIRONMENT`), else from runpodctl's own config
file (`provider_runpod.RUNPODCTL_CONFIG`); never a command line, never printed. Nothing is a
default: the global volume, its mount and the disk are named every time. The
pod's own disk (`--disk-gb`, deleted with the pod) or a network volume
(`--network-volume`) sits at /workspace/private, where the guard keeps its
records; the global volume is object storage and holds results only.

Exit 0 when the pod reports every mount asked for; 2 when nothing was created;
3 when a pod was created but does not report the mounts: its id is printed,
and it is deleted by hand before anything else.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from .models import POD_VOLUME_MOUNT_PATH, GlobalVolumeMount, ProviderFailure
from .provider_runpod import (
    ACCOUNT_KEY_ENVIRONMENT,
    GRAPHQL_PATH,
    POD_CREATE_MUTATION,
    HttpTransport,
    created_pod_from_graphql,
    global_volume_of,
    graphql_transport_from_environment,
    pod_create_input,
)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m operations.pod.create_pod", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--gpu", required=True, help="RunPod's GPU type id, e.g. 'NVIDIA A40'")
    parser.add_argument("--image", required=True)
    parser.add_argument("--container-disk-gb", type=int, required=True)
    disk = parser.add_mutually_exclusive_group(required=True)
    disk.add_argument(
        "--disk-gb",
        type=int,
        help=f"the pod's own disk at {POD_VOLUME_MOUNT_PATH}, deleted with the pod",
    )
    disk.add_argument("--network-volume", help=f"a network volume at {POD_VOLUME_MOUNT_PATH}")
    parser.add_argument("--global-volume", help="the global volume's id (console, Storage)")
    parser.add_argument(
        "--global-mount", help="where the global volume mounts, e.g. /workspace/global"
    )
    parser.add_argument("--start-command", required=True, help="pod_start_command.sh's output")
    parser.add_argument("--min-vcpu", type=int, help="refuse a machine with fewer vCPUs")
    parser.add_argument("--datacenter", help="one datacenter id; omitted, any with stock")
    parser.add_argument(
        "--cuda",
        action="append",
        default=[],
        metavar="VERSION",
        help="a host CUDA version the image needs, e.g. 13.0; repeat or comma-separate for more",
    )
    parser.add_argument("--ports", default="22/tcp")
    parser.add_argument("--dry-run", action="store_true", help="print the input; create nothing")
    return parser.parse_args(argv)


def build_input(args: argparse.Namespace) -> dict[str, object]:
    """The mutation input from the options, refusing a half-named global volume."""

    if (args.global_volume is None) != (args.global_mount is None):
        raise ValueError("--global-volume and --global-mount go together")
    global_volume = (
        GlobalVolumeMount(args.global_volume, args.global_mount)
        if args.global_volume is not None
        else None
    )
    if not args.start_command.strip():
        raise ValueError("--start-command is empty: the pod would start without its guard")
    for label, value in (
        ("--container-disk-gb", args.container_disk_gb),
        ("--disk-gb", args.disk_gb),
    ):
        if value is not None and value <= 0:
            raise ValueError(f"{label} must be a positive number of gigabytes")
    return pod_create_input(
        name=args.name,
        image=args.image,
        gpu_type=args.gpu,
        container_disk_gb=args.container_disk_gb,
        docker_args=args.start_command,
        network_volume_id=args.network_volume,
        persistent_disk_gb=args.disk_gb,
        volume_mount_path=POD_VOLUME_MOUNT_PATH,
        global_volume=global_volume,
        ports=args.ports or None,
        min_vcpu_count=args.min_vcpu,
        data_center_id=args.datacenter,
        allowed_cuda_versions=cuda_versions(args.cuda),
    )


def cuda_versions(given: Sequence[str]) -> list[str]:
    """`--cuda 13.0 --cuda 12.9` or `--cuda 13.0,12.9`, as one list; a blank is refused."""

    versions = [part.strip() for item in given for part in item.split(",")]
    if any(not part for part in versions):
        raise ValueError("--cuda takes versions like 13.0, not a blank")
    return versions


def create(transport: HttpTransport, variables: dict[str, object]) -> dict[str, object]:
    response = transport.request(
        "POST", GRAPHQL_PATH, {"query": POD_CREATE_MUTATION, "variables": {"input": variables}}
    )
    if response.status != 200:
        raise ProviderFailure(
            f"RunPod GraphQL create returned HTTP {response.status}: "
            f"{response.body[:300].decode('utf-8', 'replace')}"
        )
    return created_pod_from_graphql(response.body)


def mount_problems(variables: dict[str, object], pod: dict[str, object]) -> list[str]:
    """What the pod reports differently from the mounts asked for; empty when it matches."""

    problems = []
    if pod.get("volumeMountPath") != variables["volumeMountPath"]:
        problems.append(
            f"volumeMountPath is {pod.get('volumeMountPath')!r}, not {variables['volumeMountPath']!r}"
        )
    if "volumeInGb" in variables and pod.get("volumeInGb") != variables["volumeInGb"]:
        problems.append(f"volumeInGb is {pod.get('volumeInGb')!r}, not {variables['volumeInGb']!r}")
    mounts = variables.get("objectMounts")
    if mounts:
        try:
            observed = global_volume_of(pod).as_object_mount()
        except (ProviderFailure, ValueError) as error:
            problems.append(str(error))
        else:
            if observed != mounts[0]:  # type: ignore[index]
                problems.append(f"objectStores reports {observed}, not {mounts[0]}")  # type: ignore[index]
    return problems


def main(argv: Sequence[str] | None = None, transport: HttpTransport | None = None) -> int:
    args = parse_args(argv)
    try:
        variables = build_input(args)
    except ValueError as error:
        print(f"create_pod: {error}; nothing was created", file=sys.stderr)
        return 2
    if args.dry_run:
        print(json.dumps(variables, indent=1))
        return 0
    if transport is None:
        transport = graphql_transport_from_environment()
        if transport is None:
            print(
                f"create_pod: {ACCOUNT_KEY_ENVIRONMENT} is not set; nothing was created",
                file=sys.stderr,
            )
            return 2
    try:
        pod = create(transport, variables)
    except ProviderFailure as error:
        # A GraphQL error answers before anything is placed; an HTTP failure after
        # the POST is the one case where the console must be checked.
        print(f"create_pod: {error}", file=sys.stderr)
        return 2
    print(json.dumps(pod, indent=1, default=str))
    problems = mount_problems(variables, pod)
    if problems:
        print(
            f"create_pod: pod {pod.get('id')} was created but does not report the mounts asked "
            f"for ({'; '.join(problems)}); delete it before anything else",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
