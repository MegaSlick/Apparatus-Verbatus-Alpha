"""RunPod's S3-compatible network-volume API — the only file that knows boto3.

Everything here is one `operations.pod.transfer.TransferTarget` and one
`operations.pod.controller_armer.TimerReportChannel`, and nothing more, so
`surface.upload` never learns that an S3 client exists and the armer never
learns which vendor's volume it is reading. Same split, and the same reason, as
`operations/pod/provider_runpod.py` against `provider.py`. The digest checking,
resume journal and refusal-to-overwrite all stay in `ChecksummedTransfer`,
which already does them for every target.

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
  files (over 10,000) or large amounts of data (over 10GB)". The transfer target
  never lists — `inspect` is one `HeadObject` per file — so that limit is avoided
  rather than worked around; `S3VolumeObjectReader` does list, one run prefix at
  a time, and bounds the walk at `MAX_LISTED_KEYS` rather than trusting the
  documented figure.

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

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Final, Mapping

from operations.pod.transfer import RemoteObject

SHA256_METADATA_KEY: Final = "verbatus-sha256"
# 64 MiB parts: comfortably inside the documented 500MB-per-part ceiling, and at
# S3's 10,000-part limit still enough for a single file of roughly 625 GiB. The
# multipart threshold matches, so anything smaller goes as one `PutObject` and
# stays inside the documented "<500MB" single-shot limit.
PART_BYTES: Final = 64 * 1024 * 1024
MAX_CONCURRENCY: Final = 4
# What `S3VolumeReadChannel` will pull into memory for one object. The pod
# report it exists to read is a few hundred bytes; a megabyte is already far
# past anything this channel should believe, and the bytes come off a volume a
# pod writes, so their size is untrusted input like their content.
MAX_READ_BYTES: Final = 1024 * 1024
# `S3VolumeObjectReader`: one run tree's listing is bounded, and each object is
# streamed in chunks under the caller's own per-object bound.
MAX_LISTED_KEYS: Final = 100_000
# S3's own page ceiling is 1,000 keys, so MAX_LISTED_KEYS bounds a well-behaved
# listing to well under 100 pages. A page that adds no keys never trips the key
# bound at all, so the walk needs its own, more generous page bound to refuse a
# listing that is not making progress.
MAX_LISTED_PAGES: Final = 1_000
FETCH_CHUNK_BYTES: Final = 1024 * 1024

# `Error.Code` values that mean "no such object" rather than "something is wrong".
# botocore reports a bare `HeadObject` 404 as "404"; `NoSuchKey` and `NotFound`
# appear from `GetObject` and from some S3-compatible servers. Anything outside
# this set — `AccessDenied`, `SignatureDoesNotMatch`, `InvalidAccessKeyId`, a 5xx —
# is a failure and must never be read as "absent, so upload it". That reading
# would turn one broken credential into a full silent re-upload on every run.
_ABSENT_CODES: Final = frozenset({"404", "NoSuchKey", "NotFound"})

_DATACENTER = re.compile(r"[A-Z]{2,4}-[A-Z]{2}-[0-9]{1,2}")

# The upload-only credentials, named once. They are read here, and they are the
# names every child-process environment must have stripped of them, so a third
# credential added to this transfer without the strippers learning of it is the
# failure this single definition exists to prevent.
TRANSFER_ACCESS_KEY_ENV: Final = "RUNPOD_S3_ACCESS_KEY"
TRANSFER_SECRET_KEY_ENV: Final = "RUNPOD_S3_SECRET_KEY"
TRANSFER_CREDENTIAL_ENV: Final = frozenset({TRANSFER_ACCESS_KEY_ENV, TRANSFER_SECRET_KEY_ENV})


class VolumeTransferRefusal(RuntimeError):
    """This target cannot be built or cannot answer; never a silent 'absent'."""


@dataclass(frozen=True, slots=True)
class VolumeSpec:
    """One network volume, named exactly as RunPod's own documentation names it."""

    datacenter_id: str
    volume_id: str
    access_key_env: str = TRANSFER_ACCESS_KEY_ENV
    secret_key_env: str = TRANSFER_SECRET_KEY_ENV

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
        from botocore.config import Config
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
        # Explicit bounds, so a stalled endpoint becomes a named refusal rather
        # than a terminal that prints nothing indefinitely. total_max_attempts
        # counts the initial request.
        config=Config(
            connect_timeout=30,
            read_timeout=120,
            retries={"total_max_attempts": 3, "mode": "standard"},
        ),
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
            default_transfer_config() if transfer_config is None else transfer_config
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
        # Checked for being a mapping, not merely truthy — the same reasoning as
        # `_means_absent` below, applied to the *same server's* response. `or {}`
        # substitutes for `None` and passes a string or a list straight through to
        # `.get`, and this line sits outside the `try` above, so the resulting
        # `AttributeError` escapes as a traceback rather than as this module's own
        # `VolumeTransferRefusal`. Hardened in one of the two readers when it was
        # found; this is the other. Found by the Opus read of this branch.
        metadata = head.get("Metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        normalized_metadata = {
            name.lower(): value for name, value in metadata.items() if isinstance(name, str)
        }
        recorded = normalized_metadata.get(SHA256_METADATA_KEY)
        size = head.get("ContentLength")
        if not isinstance(recorded, str) or not isinstance(size, int):
            # The object is present, so `None` would authorize the transfer layer
            # to overwrite it. Missing metadata is unknown ownership, not absence.
            raise VolumeTransferRefusal(
                f"network-volume object {key!r} exists without the digest and size "
                "evidence Verbatus needs; it was not overwritten"
            )
        return RemoteObject(sha256=recorded, size=size)

    def put_file(self, key: str, source: BinaryIO, *, expected_sha: str) -> None:
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
            source.seek(0)
        except OSError as error:
            raise VolumeTransferRefusal(
                f"the source for {key!r} could not be read while sending it: {error}"
            ) from error
        extra = {"Metadata": {SHA256_METADATA_KEY: expected_sha}}
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
                f"sending {key!r} failed while reading the source or writing to the network "
                f"volume; nothing was verified as sent: {error}"
            ) from error
        except Exception as error:
            raise VolumeTransferRefusal(
                f"the network volume refused or dropped the upload of {key!r}: {error}"
            ) from error


class S3VolumeReadChannel:
    """`operations.pod.controller_armer.TimerReportChannel` over one network volume.

    A `GetObject` beside `S3VolumeTarget`'s `HeadObject`, sharing this file's
    client construction and its one classifier, `_means_absent`. It is what
    lets the laptop read the report the pod wrote through its volume mount --
    the single unobserved assumption the whole two-controller arming rests on.

    Deliberately not a method on `S3VolumeTarget`: that class is one
    `TransferTarget` and nothing more, and a third verb on it would put a read
    the transfer layer never makes inside the seam the transfer layer is
    checked against.

    The contract is the channel's, not this adapter's convenience: `read`
    returns bytes, or `None` **only** for a positively absent object. A refused
    credential, a 5xx, a dropped connection, a body it cannot read or one past
    the size bound all raise, because arming reads `None` as "the pod has not
    written its report yet" and would poll a broken credential to its bound and
    then close a pod that was fine.

    Note 3 of this module's docstring applies here in full: nothing in this
    class has ever run against a real endpoint.
    """

    def __init__(
        self,
        spec: VolumeSpec,
        *,
        client: Any | None = None,
        environ: Mapping[str, str] | None = None,
        max_bytes: int = MAX_READ_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise VolumeTransferRefusal("network-volume read bound must be positive")
        self.spec = spec
        self.client = build_client(spec, environ) if client is None else client
        self.max_bytes = int(max_bytes)

    def read(self, key: str) -> bytes | None:
        """The object's bytes, or `None` when the volume proved it is not there."""

        try:
            response = self.client.get_object(Bucket=self.spec.volume_id, Key=key)
        except Exception as error:
            if _means_absent(error):
                return None
            raise VolumeTransferRefusal(
                f"the network volume refused or could not answer a read of {key!r}: {error}"
            ) from error
        body = response.get("Body") if isinstance(response, Mapping) else None
        if body is None or not callable(getattr(body, "read", None)):
            # Checked rather than assumed, for the same reason `_means_absent`
            # checks its nested mappings: the response comes from a remote
            # server, and an AttributeError here would escape as a traceback
            # rather than as this module's own named refusal.
            raise VolumeTransferRefusal(
                f"the network volume answered a read of {key!r} without a readable body"
            )
        try:
            payload = _read_bounded(body, self.max_bytes + 1)
        except Exception as error:
            raise VolumeTransferRefusal(
                f"the network volume dropped the body of {key!r} while it was being read; "
                f"nothing was read completely: {error}"
            ) from error
        finally:
            closer = getattr(body, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # pragma: no cover - a failed close hides no evidence
                    pass
        if len(payload) > self.max_bytes:
            raise VolumeTransferRefusal(
                f"the object at {key!r} is larger than the {self.max_bytes}-byte bound this "
                "channel reads; it was not read, and it is not evidence of anything"
            )
        return payload


class S3VolumeObjectReader:
    """The list-and-fetch seam `surface.fetch_run` brings a run tree home through.

    `ListObjectsV2` under one prefix, paginated to the end, and one streamed
    `GetObject` per key into a local file -- beside `S3VolumeReadChannel`'s
    single bounded read and `S3VolumeTarget`'s `HeadObject`, sharing the client
    construction and the one classifier, `_means_absent`. It knows nothing
    about run trees: which keys belong to a run, what each must digest to, and
    whether a local copy may be touched are `surface.fetch_run`'s questions.

    Both verbs fail closed. A listing the volume cannot finish (a truncated
    page with no continuation token, a non-list `Contents`, a key that is not
    a string) is a refusal, never a shorter run. A key the listing named that
    `GetObject` then reports absent is a refusal too: an object that vanished
    between the two calls is not one to skip. Bytes go to a temporary file in
    the destination's own directory and are renamed into place only once the
    body reached EOF inside the caller's bound, so a dropped connection never
    leaves a short file wearing a real name.

    `ListObjects` "may take a long time" past 10,000 objects or 10 GB
    (RunPod's documented limit, module docstring); a run tree of a few pages
    is far inside that, and `MAX_LISTED_KEYS` together with `MAX_LISTED_PAGES`
    refuses one that is not -- by key count, and by page count so a listing
    that answers truncated pages with no new keys is refused instead of
    walked forever -- rather than walking it forever. Note 3 of the module
    docstring applies: nothing here has run against a real endpoint.
    """

    def __init__(
        self,
        spec: VolumeSpec,
        *,
        client: Any | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.spec = spec
        self.client = build_client(spec, environ) if client is None else client

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        """Every key under `prefix`, in the order the volume listed them."""

        if not prefix or not prefix.endswith("/"):
            raise VolumeTransferRefusal(
                f"a run prefix must be non-empty and end in '/', not {prefix!r}"
            )
        keys: list[str] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        pages = 0
        while True:
            pages += 1
            if pages > MAX_LISTED_PAGES:
                raise VolumeTransferRefusal(
                    f"more than {MAX_LISTED_PAGES} pages listing {prefix!r} without reaching "
                    "the end; that is not the shape of one run tree and was not walked further"
                )
            request: dict[str, Any] = {"Bucket": self.spec.volume_id, "Prefix": prefix}
            if token is not None:
                request["ContinuationToken"] = token
            try:
                page = self.client.list_objects_v2(**request)
            except Exception as error:
                raise VolumeTransferRefusal(
                    f"the network volume refused or could not answer a listing of {prefix!r}: "
                    f"{error}"
                ) from error
            if not isinstance(page, Mapping):
                raise VolumeTransferRefusal(
                    f"the network volume answered a listing of {prefix!r} with no page"
                )
            contents = page.get("Contents", [])
            if not isinstance(contents, list):
                raise VolumeTransferRefusal(
                    f"the network volume listed {prefix!r} as something other than a list"
                )
            for entry in contents:
                key = entry.get("Key") if isinstance(entry, Mapping) else None
                if not isinstance(key, str) or not key.startswith(prefix):
                    raise VolumeTransferRefusal(
                        f"the network volume listed an object under {prefix!r} with no usable "
                        "key; the listing is not evidence of the run"
                    )
                keys.append(key)
                if len(keys) > MAX_LISTED_KEYS:
                    raise VolumeTransferRefusal(
                        f"more than {MAX_LISTED_KEYS} objects under {prefix!r}; that is not the "
                        "shape of one run tree and was not walked further"
                    )
            truncated = page.get("IsTruncated")
            if not isinstance(truncated, bool):
                # A page with no usable `IsTruncated` used to read exactly like
                # `False` -- complete -- so a client answer missing or
                # malforming the one field this reader trusts to know it has
                # seen everything could end the walk early and let `fetch-run`
                # record "verified" over a listing that was never proven whole.
                raise VolumeTransferRefusal(
                    f"the network volume answered a listing of {prefix!r} with no usable "
                    "IsTruncated flag; a listing this reader cannot tell complete from "
                    "partial is not evidence of the run"
                )
            if not truncated:
                return tuple(keys)
            token = page.get("NextContinuationToken")
            if not isinstance(token, str) or not token:
                raise VolumeTransferRefusal(
                    f"the network volume truncated the listing of {prefix!r} without a "
                    "continuation token; a partial listing is not a run"
                )
            if token in seen_tokens:
                raise VolumeTransferRefusal(
                    f"the network volume repeated a continuation token listing {prefix!r}; "
                    "the listing is not making progress and was not walked further"
                )
            seen_tokens.add(token)

    def fetch_to(self, key: str, destination: Path, *, max_bytes: int) -> int:
        """Stream one object into `destination`; the byte count, or a refusal.

        The caller has already decided `destination` may be written (it does
        not exist, or `surface.fetch_run` will compare bytes afterwards). This
        writes a temporary file beside it and renames only after the body's
        EOF arrived inside `max_bytes`.
        """

        if max_bytes <= 0:
            raise VolumeTransferRefusal("network-volume fetch bound must be positive")
        try:
            response = self.client.get_object(Bucket=self.spec.volume_id, Key=key)
        except Exception as error:
            if _means_absent(error):
                raise VolumeTransferRefusal(
                    f"the network volume listed {key!r} and then reported it absent; an object "
                    "that vanished mid-fetch is not one to skip"
                ) from error
            raise VolumeTransferRefusal(
                f"the network volume refused or could not answer a read of {key!r}: {error}"
            ) from error
        body = response.get("Body") if isinstance(response, Mapping) else None
        if body is None or not callable(getattr(body, "read", None)):
            raise VolumeTransferRefusal(
                f"the network volume answered a read of {key!r} without a readable body"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        written = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                while True:
                    chunk = body.read(FETCH_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise VolumeTransferRefusal(
                            f"the network volume answered {key!r} with a non-bytes body chunk"
                        )
                    written += len(chunk)
                    if written > max_bytes:
                        raise VolumeTransferRefusal(
                            f"the object at {key!r} is larger than the {max_bytes}-byte bound; "
                            "it was not kept"
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except VolumeTransferRefusal:
            _unlink_quietly(temporary)
            raise
        except Exception as error:
            _unlink_quietly(temporary)
            raise VolumeTransferRefusal(
                f"the network volume dropped the body of {key!r} while it was being read; "
                f"nothing was kept: {error}"
            ) from error
        finally:
            closer = getattr(body, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # pragma: no cover - a failed close hides no evidence
                    pass
        return written


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_bounded(body: Any, limit: int) -> bytes:
    """Accumulate up to ``limit`` bytes, tolerating short reads.

    `read(amount)` is free to return fewer bytes than asked for, and a single
    call that did so would silently truncate a report -- which the armer would
    then refuse as unparseable and close a pod over. The loop ends on EOF or at
    the limit, and the caller decides what a full buffer means.
    """

    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = body.read(remaining)
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError("the network volume answered with a non-bytes body chunk")
        # A stream is free to ignore `amount` and hand back more than asked;
        # truncating here is what keeps `remaining` from going negative and
        # the returned buffer from exceeding `limit` even against a
        # misbehaving body.
        bounded = bytes(chunk)[:remaining]
        chunks.append(bounded)
        remaining -= len(bounded)
    return b"".join(chunks)


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
    # Each nested value checked for being a mapping, not merely truthy. `or {}`
    # substitutes for `None` and for anything falsey, and passes a *string* or a
    # list straight through to `.get`, which raises `AttributeError` — out of the
    # one function whose whole job is to classify an exception. A classifier that
    # raises while classifying fails in the direction this docstring says it must
    # not: the caller never reaches its fail-closed answer at all. The response
    # comes from a remote server, so its shape is not ours to assume.
    # Found by CodeRabbit.
    error_detail = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = str((error_detail if isinstance(error_detail, Mapping) else {}).get("Code", ""))
    status = (metadata if isinstance(metadata, Mapping) else {}).get("HTTPStatusCode")
    # A bare 404 is the shape botocore uses for HeadObject. If a server supplies
    # a contradictory named error (for example AccessDenied with a 404 status),
    # preserve the failure instead of treating it as permission to overwrite.
    return code in _ABSENT_CODES or (not code and status == 404)


__all__ = [
    "FETCH_CHUNK_BYTES",
    "MAX_CONCURRENCY",
    "MAX_LISTED_KEYS",
    "MAX_LISTED_PAGES",
    "MAX_READ_BYTES",
    "PART_BYTES",
    "SHA256_METADATA_KEY",
    "S3VolumeObjectReader",
    "S3VolumeReadChannel",
    "S3VolumeTarget",
    "VolumeSpec",
    "VolumeTransferRefusal",
    "build_client",
    "default_transfer_config",
]
