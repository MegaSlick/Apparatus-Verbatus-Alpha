"""The content-addressed cache and the never-re-fetch request ledger.

`SPEC.md` §5.1 ("Resumable / never re-fetch") is two stores with different keys:

  cache/<response-sha256>.jpg      the bytes themselves, addressed by their own
                                    digest — two identifiers that return the same
                                    bytes share one file, for free.
  cache/requests/<request-key>.json  a marker that a given request has already
                                    been *answered*, addressed by the request's
                                    own inputs (`sha256(kind||identifier||region||
                                    size||rotation||quality||format)`).

The request-key store is the one `fetch.py` actually consults before issuing a
network call: `load_request_record` returning non-`None` means "do not ask the
server this question again." The response store is where the answer's bytes
live; a request record and a response file are written only after a fetch has
*fully* completed, so a run killed mid-body leaves neither — the request will be
retried, not silently treated as answered (`SPEC.md` §5.1's "an interrupt loses
at most one in-flight body").

Both writes are atomic creates, never overwrites: `_write_new_file` hard-links a
completed temp file onto its destination, which raises `FileExistsError`
atomically if the destination is already there — the one race a sequential,
single-connection fetcher still has to guard against is its own crash-and-resume,
not concurrency, but "atomic" here means "survives being killed between the
write and the rename," not "safe under concurrent writers."
"""

import errno
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from common.contracts.canonical import canonical_bytes, digest_bytes, is_sha256

from . import CorpusRefusal

CACHE_REFUSAL_REASONS = frozenset(
    {
        "malformed-digest",
        "duplicate-request-record",
        "no-hard-link-support",
    }
)

# `os.link` on a cache root that cannot hold hard links (EPERM, EOPNOTSUPP,
# ENOSYS — a filesystem with them disabled, or one that never supports them)
# is a constraint on the cache root itself, not on the one file being written.
# Any other `OSError` (ENOSPC, EIO, ...) keeps its native diagnostics and is
# left to propagate unchanged — this set names only the case that has its own
# recovery story ("point --cache-root at a filesystem that supports hard
# links").
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS})


class CacheUnusable(CorpusRefusal):
    """The cache root itself cannot hold the store — not one page's problem."""


# The general request-key formula from `SPEC.md` §5.1 covers every kind of
# request this package issues, not only image fetches. `info.json` has no
# region/size/rotation/quality/format of its own, so it fills those fields with
# the fixed sentinel `"info"` rather than omitting them — one formula, one
# function, every request kind keyed the same way.
INFO_SENTINEL = "info"


def compute_request_key(
    *,
    kind: str,
    identifier: str,
    region: str,
    size: str,
    rotation: str,
    quality: str,
    format: str,
) -> str:
    """`sha256` over the closed tuple that identifies one request, canonically."""
    payload = {
        "kind": kind,
        "identifier": identifier,
        "region": region,
        "size": size,
        "rotation": rotation,
        "quality": quality,
        "format": format,
    }
    return digest_bytes(canonical_bytes(payload))


def _require_sha256(value: str, what: str) -> str:
    if not is_sha256(value):
        raise CorpusRefusal(
            f"malformed-digest: {what} {value!r} is not a lowercase sha256 hex digest"
        )
    return value


def body_path(cache_root: Path, response_sha256: str) -> Path:
    """Where a response's content-addressed bytes live, given their own digest."""
    _require_sha256(response_sha256, "response digest")
    return Path(cache_root) / f"{response_sha256}.jpg"


def owner_path(cache_root: Path, response_sha256: str) -> Path:
    """Where the first *verified* claim on a response digest is recorded.

    Written only after a page has passed every check in `fetch.py:fetch_page`
    (decode, dimensions, EXIF, region) — never from the raw request record, which
    is written as soon as the bytes are down but before any of that verification
    runs. `write_new_file` makes the first writer permanent: whichever identifier
    claims a digest first, across any number of runs, owns it forever, regardless
    of the order a later run happens to revisit identifiers in.
    """
    _require_sha256(response_sha256, "response digest")
    return Path(cache_root) / "owners" / f"{response_sha256}.json"


def request_record_path(cache_root: Path, request_key: str) -> Path:
    """Where the never-re-fetch marker for one request lives."""
    _require_sha256(request_key, "request key")
    return Path(cache_root) / "requests" / f"{request_key}.json"


def load_request_record(cache_root: Path, request_key: str) -> dict[str, Any] | None:
    """The recorded answer to `request_key`, or `None` if it has never been asked."""
    path = request_record_path(cache_root, request_key)
    if not path.exists():
        return None
    return json.loads(path.read_bytes())


def write_new_file(path: Path, data: bytes) -> bool:
    """Write `data` to `path` only if `path` does not already exist, atomically.

    Returns `True` if this call created the file, `False` if it already existed
    (in which case `data` was NOT written — for the content-addressed cache the
    two are guaranteed identical because the path is a digest of the content, but
    for a request record the existing file is the answer of record and this
    function never overwrites it).

    Writes to a temp file in the same directory, then hard-links the temp file
    onto the destination: `os.link` raises `FileExistsError` atomically if the
    destination is already there, which `os.replace` would not — it silently
    overwrites. The temp file is always removed afterward, whichever branch ran.

    Raises `CacheUnusable` (`"no-hard-link-support"`) if the cache root's
    filesystem refuses hard links outright (EPERM/EOPNOTSUPP/ENOSYS) — that is
    a constraint on the cache root, not on this one file, and the caller should
    let it propagate rather than treat it as one failed write. Any other
    `OSError` (ENOSPC, EIO, ...) propagates unchanged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".partial")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, path)
            return True
        except FileExistsError:
            return False
        except OSError as error:
            if error.errno in _NO_HARD_LINKS:
                raise CacheUnusable(
                    f"no-hard-link-support: the cache root at {path.parent} is on a "
                    f"filesystem that refuses hard links ({error.strerror}); cache entries "
                    "are published by atomic link so a partly written file can never take "
                    "its final name, and the cache root has to be on a filesystem that "
                    "supports it"
                ) from error
            raise
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def store_response_body(cache_root: Path, body: bytes) -> str:
    """Store `body` content-addressed under `cache/<sha256>.jpg`; return its digest.

    Idempotent: a second page whose response is byte-identical to an earlier
    one's writes nothing new and returns the same digest — the mechanism that
    makes `duplicate-page-bytes` detectable at all.
    """
    digest = digest_bytes(body)
    path = body_path(cache_root, digest)
    if not path.exists():
        write_new_file(path, body)
    return digest


def write_request_record(cache_root: Path, request_key: str, record: dict[str, Any]) -> None:
    """Record that `request_key` has been fully answered — atomically, once.

    Call this only after a fetch has completed in full (the whole body read and,
    for image bodies, stored via `store_response_body`); never on a request that
    raised partway through. That ordering is what makes an interrupt mid-body
    leave no request record: the write this function performs is the only one
    that exists, and it never runs until there is a complete answer to record.
    """
    path = request_record_path(cache_root, request_key)
    data = canonical_bytes(record)
    if not write_new_file(path, data):
        raise CorpusRefusal(
            f"duplicate-request-record: {request_key!r} already has a recorded answer — "
            "never re-fetch means never re-record either; the caller should have checked "
            "load_request_record first"
        )


def already_answered(cache_root: Path, request_key: str) -> bool:
    """Whether `request_key` has a recorded answer — the never-re-fetch gate."""
    return load_request_record(cache_root, request_key) is not None
