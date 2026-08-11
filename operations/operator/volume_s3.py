"""RunPod's S3-compatible network-volume API — the only file that knows boto3.

Everything here is one `operations.pod.transfer.TransferTarget` and nothing more,
so `surface.upload` never learns that an S3 client exists. Same split, and the
same reason, as `operations/pod/provider_runpod.py` against `provider.py`. The
digest checking, resume journal and refusal-to-overwrite all stay in
`ChecksummedTransfer`, which already does them for every target.

**Sources, fetched and read on 2026-08-09** — `https://docs.runpod.io/storage/s3-api`,
quoted rather than paraphrased where the detail is load-bearing:

- endpoint: one per datacenter, `https://s3api-DATACENTER.runpod.io/`, with the
  datacenter **lowercased** in the hostname (`EU-CZ-1` → `s3api-eu-cz-1.runpod.io`).
- region: the `--region` value is the datacenter ID in its **uppercase** form
  (`EU-CZ-1`). So the hostname is lowercased and the region is not; that
  inconsistency is RunPod's and is preserved here rather than tidied away.
- bucket: the network volume ID.
- credential: a dedicated **S3 API key**, separate from the RunPod API key. The
  access key looks like `user_***` and the secret like `rps_***`.
- client construction: exactly the documented `boto3.client("s3", ...)` example,
  with no `addressing_style` or `signature_version` override — the example is the
  only evidence available and second-guessing it would be inventing a fact.
- supported: `PutObject`, `GetObject`, `HeadObject`, `HeadBucket`, `CopyObject`,
  `DeleteObject`, `ListBuckets`, `ListObjects`, `ListObjectsV2` and the full
  multipart set. Not supported: `DeleteObjects`, bucket create/delete, ACLs,
  policies, versioning, tagging, encryption, object locking.
- limits: `PutObject` is for objects "<500MB"; a multipart part may not exceed
  500MB; maximum request clock skew is one hour.
- `ListObjects` "may take a long time when used on a directory containing many
  files (over 10,000) or large amounts of data (over 10GB)". This class never
  lists — `inspect` is one `HeadObject` per file — so the limit is avoided rather
  than worked around.

**Stated as unconfirmed rather than asserted:**

1. *Whether user metadata round-trips.* `put_file` writes each file's SHA-256 as S3
   user metadata on upload and reads it back with `HeadObject`. Both operations
   are documented as supported; the page says nothing either way about user
   metadata surviving. If RunPod drops it, `HeadObject` returns no digest and this
   class refuses the transfer. It must not report a present object as absent:
   `ChecksummedTransfer` would then overwrite bytes it had no evidence it owned.
2. *ETag is deliberately not used for that check.* A multipart ETag is not a
   whole-file MD5 even on AWS, the sealed manifest carries SHA-256 rather than
   MD5, and an integrity check built on a value whose definition is unclear is not
   an integrity check.
3. **Nothing in this file has ever run against a real endpoint**, authenticated or
   otherwise, from this chamber or any other. Its logic is tested against an
   injected fake client; its network behaviour is untested. boto3 is imported
   lazily, so `upload` without `--network-volume` does not construct a client or
   read storage credentials.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any, BinaryIO, Final, Mapping

from operations.pod.transfer import RemoteObject

SHA256_METADATA_KEY: Final = "verbatus-sha256"
BLOCK_BYTES: Final = 1024 * 1024

# 64 MiB parts: comfortably inside the documented 500MB-per-part ceiling, and at
# S3's 10,000-part limit still enough for a single file of roughly 625 GiB. The
# multipart threshold matches, so anything smaller goes as one `PutObject` and
# stays inside the documented "<500MB" single-shot limit.
PART_BYTES: Final = 64 * 1024 * 1024
MAX_CONCURRENCY: Final = 4

# `Error.Code` values that mean "no such object" rather than "something is wrong".
# botocore reports a bare `HeadObject` 404 as "404"; `NoSuchKey` and `NotFound`
# appear from `GetObject` and from some S3-compatible servers. Anything outside
# this set — `AccessDenied`, `SignatureDoesNotMatch`, `InvalidAccessKeyId`, a 5xx —
# is a failure and must never be read as "absent, so upload it". That reading
# would turn one broken credential into a full silent re-upload on every run.
_ABSENT_CODES: Final = frozenset({"404", "NoSuchKey", "NotFound"})

_DATACENTER = re.compile(r"[A-Z]{2,4}-[A-Z]{2}-[0-9]{1,2}")


class VolumeTransferRefusal(RuntimeError):
    """This target cannot be built or cannot answer; never a silent 'absent'."""


@dataclass(frozen=True, slots=True)
class VolumeSpec:
    """One network volume, named exactly as RunPod's own documentation names it."""

    datacenter_id: str
    volume_id: str
    access_key_env: str = "RUNPOD_S3_ACCESS_KEY"
    secret_key_env: str = "RUNPOD_S3_SECRET_KEY"

    def __post_init__(self) -> None:
        if not _DATACENTER.fullmatch(self.datacenter_id):
            # Deliberately a shape check and not a list of known datacenters: a
            # hardcoded list goes stale silently, and would refuse a datacenter
            # RunPod had since added.
            raise VolumeTransferRefusal(
                f"datacenter id {self.datacenter_id!r} is not RunPod's documented "
                "uppercase form, for example 'EU-CZ-1'"
            )
        if not self.volume_id or not self.volume_id.strip():
            raise VolumeTransferRefusal("network volume id must be non-blank")

    @property
    def endpoint_url(self) -> str:
        return f"https://s3api-{self.datacenter_id.lower()}.runpod.io/"

    def describe(self) -> str:
        """What the operator is told before anything leaves this computer."""

        return (
            f"network volume {self.volume_id} in datacenter {self.datacenter_id} "
            f"({self.endpoint_url})"
        )


def build_client(spec: VolumeSpec, environ: Mapping[str, str] | None = None) -> Any:
    """The documented boto3 client for one network volume, and nothing more.

    boto3 is imported here rather than at module scope so an operator who never
    asks for a network volume never constructs its client or reads credentials.
    """

    # Credentials before the import, deliberately: a missing key is the far more
    # common and far more actionable failure, and an operator who forgot to export
    # one should not be told to go and install a package instead.
    env = os.environ if environ is None else environ
    access_key = _credential(env, spec.access_key_env)
    secret_key = _credential(env, spec.secret_key_env)
    try:
        import boto3
    except ImportError as error:
        raise VolumeTransferRefusal(
            "sending to a network volume needs the project's boto3 dependency, but it "
            "is missing from this installation; reinstall Verbatus before retrying"
        ) from error
    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=spec.datacenter_id,
        endpoint_url=spec.endpoint_url,
    )


def _credential(environ: Mapping[str, str], variable: str) -> str:
    value = environ.get(variable, "")
    if not value:
        raise VolumeTransferRefusal(
            f"the environment variable {variable} holds no value; the network-volume key "
            "is read from the environment only, never from a file in this repository"
        )
    return value


class S3VolumeTarget:
    """`operations.pod.transfer.TransferTarget` against a RunPod network volume.

    `client` is injectable for the same reason `RunPodProvider`'s transport is: a
    seam with no override is a seam nothing can exercise. Every test in this
    repository injects one; nothing here has ever built a real one.
    """

    def __init__(
        self,
        spec: VolumeSpec,
        *,
        client: Any | None = None,
        environ: Mapping[str, str] | None = None,
        transfer_config: Any | None = None,
    ) -> None:
        self.spec = spec
        self.client = build_client(spec, environ) if client is None else client
        # After the client, never before: a missing credential is the failure the
        # operator can act on, and it must not be masked by boto3's absence.
        self.transfer_config = (
            default_transfer_config()
            if transfer_config is None and client is None
            else transfer_config
        )

    def inspect(self, key: str) -> RemoteObject | None:
        """Present *and* carrying the digest we recorded for it. See docstring note 1."""

        try:
            head = self.client.head_object(Bucket=self.spec.volume_id, Key=key)
        except Exception as error:
            if _means_absent(error):
                return None
            raise VolumeTransferRefusal(
                f"the network volume refused or could not answer a check for {key!r}: {error}"
            ) from error
        metadata = head.get("Metadata") or {}
        recorded = metadata.get(SHA256_METADATA_KEY)
        size = head.get("ContentLength")
        if not isinstance(recorded, str) or not isinstance(size, int):
            # The object is present, so `None` would authorize the transfer layer
            # to overwrite it. Missing metadata is unknown ownership, not absence.
            raise VolumeTransferRefusal(
                f"network-volume object {key!r} exists without the digest and size "
                "evidence Verbatus needs; it was not overwritten"
            )
        return RemoteObject(sha256=recorded, size=size)

    def put_file(self, key: str, source: BinaryIO) -> None:
        """Send the exact bytes behind this already-opened handle, tagged with their digest.

        `upload_fileobj` rather than `upload_file`: the caller has already opened
        and verified this handle once (see `TransferTarget.put_file`'s docstring),
        so this seam reads only from it and never re-resolves the source by name.
        boto3's managed transfer still switches to multipart above the configured
        threshold, which is what keeps a file past RunPod's documented single-
        `PutObject` limit working without a second code path here.

        `upload_fileobj` takes the same `ExtraArgs`/`Config` as `upload_file` and
        needs only a binary-mode, readable file object — no path, no seekability
        requirement beyond what this handle already gives it. Confirmed 2026-08-11
        against `https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/upload_fileobj.html`.
        """

        try:
            digest = hashlib.sha256()
            for block in iter(lambda: source.read(BLOCK_BYTES), b""):
                digest.update(block)
            source.seek(0)
        except OSError as error:
            raise VolumeTransferRefusal(
                f"the source for {key!r} could not be read while sending it: {error}"
            ) from error
        extra = {"Metadata": {SHA256_METADATA_KEY: digest.hexdigest()}}
        try:
            if self.transfer_config is None:
                self.client.upload_fileobj(
                    Fileobj=source, Bucket=self.spec.volume_id, Key=key, ExtraArgs=extra
                )
            else:
                self.client.upload_fileobj(
                    Fileobj=source,
                    Bucket=self.spec.volume_id,
                    Key=key,
                    ExtraArgs=extra,
                    Config=self.transfer_config,
                )
        except OSError as error:
            raise VolumeTransferRefusal(
                f"the source for {key!r} could not be read while sending it: {error}"
            ) from error
        except Exception as error:
            raise VolumeTransferRefusal(
                f"the network volume refused or dropped the upload of {key!r}: {error}"
            ) from error


def default_transfer_config() -> Any | None:
    """boto3's managed-transfer settings, or `None` when boto3 is not installed.

    `None` means "let boto3 choose", which is a weaker setting and never a wrong
    one — and it cannot be reached at all without boto3, since the client could
    not have been built.
    """

    try:
        from boto3.s3.transfer import TransferConfig
    except ImportError:
        return None
    return TransferConfig(
        multipart_threshold=PART_BYTES,
        multipart_chunksize=PART_BYTES,
        max_concurrency=MAX_CONCURRENCY,
    )


def _means_absent(error: BaseException) -> bool:
    """Classify by the response shape, so no botocore import is needed to catch.

    Anything this cannot positively read as "absent" is a failure and is raised —
    the fail-closed direction, because the alternative reads a broken credential
    as an empty volume.
    """

    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return False
    code = str((response.get("Error") or {}).get("Code", ""))
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    # A bare 404 is the shape botocore uses for HeadObject. If a server supplies
    # a contradictory named error (for example AccessDenied with a 404 status),
    # preserve the failure instead of treating it as permission to overwrite.
    return code in _ABSENT_CODES or (not code and status == 404)


__all__ = [
    "MAX_CONCURRENCY",
    "PART_BYTES",
    "SHA256_METADATA_KEY",
    "S3VolumeTarget",
    "VolumeSpec",
    "VolumeTransferRefusal",
    "build_client",
    "default_transfer_config",
]
