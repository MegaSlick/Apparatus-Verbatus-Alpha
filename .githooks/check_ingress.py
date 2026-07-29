#!/usr/bin/env python3
"""Reject recognized credential forms and repository-sized payloads from Git.

The pre-commit hook scans the exact index state. CI scans every commit reachable
from HEAD, so add-then-delete does not make a leaked key or corpus file disappear.
Only the Python standard library and Git are required.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys
import tomllib
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

NORMAL_MAX_BYTES = 1_048_576
FIXTURE_MAX_BYTES = 25 * 1_048_576
FIXTURE_TOTAL_MAX_BYTES = 100 * 1_048_576
MANIFEST_PATH = "proof/fixtures.toml"
FIXTURE_ROOT = "proof/fixtures/"
PRIVATE_README = "private/README.md"

MEDIA = {
    "image/jpeg": ((".jpg", ".jpeg"), (b"\xff\xd8\xff",)),
    "image/png": ((".png",), (b"\x89PNG\r\n\x1a\n",)),
    "image/tiff": ((".tif", ".tiff"), (b"II*\x00", b"MM\x00*")),
}

SENSITIVE_NAMES = {
    ".env",
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "ntfy.conf",
    "service-account.json",
}
SENSITIVE_SUFFIXES = (".jks", ".key", ".keystore", ".p12", ".pfx")
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template")

LFS_HEADER = b"version https://git-lfs.github.com/spec/v1"

SECRET_PATTERNS = (
    (
        "private-key",
        re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    ),
    ("runpod-api-key", re.compile(rb"\brpa_[A-Za-z0-9_-]{20,}\b")),
    ("runpod-s3-secret", re.compile(rb"\brps_[A-Za-z0-9_-]{20,}\b")),
    ("aws-access-key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("github-token", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("gitlab-token", re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("google-api-key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("openai-api-key", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic-api-key", re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("huggingface-token", re.compile(rb"\bhf_[A-Za-z0-9]{20,}\b")),
    ("google-oauth-secret", re.compile(rb"\bGOCSPX-[A-Za-z0-9_-]{20,}")),
    # `xoxe` is a refresh token and `xapp` an app-level token; both are as
    # usable as the bot token the original class covered.
    ("slack-token", re.compile(rb"\bxox[baeprs]-[A-Za-z0-9-]{20,}\b")),
    ("slack-token", re.compile(rb"\bxapp-[0-9]-[A-Za-z0-9-]{20,}\b")),
    # A webhook URL is a bearer credential in one string: whoever holds it can
    # post as the app. Pasting one into a note is an ordinary mistake.
    (
        "slack-webhook",
        re.compile(
            rb"https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]{20,}"
        ),
    ),
    ("stripe-live-key", re.compile(rb"\bsk_live_[A-Za-z0-9]{20,}\b")),
    ("stripe-restricted-key", re.compile(rb"\brk_live_[A-Za-z0-9]{20,}\b")),
    ("stripe-webhook-secret", re.compile(rb"\bwhsec_[A-Za-z0-9]{20,}\b")),
    ("pypi-token", re.compile(rb"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,}")),
    # A signed bearer token carries its own authority until it expires, and
    # the payload is only base64: it leaks the claims as well as the access.
    (
        "json-web-token",
        re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    # The credential-url rule below is anchored to HTTP. A database URI leaks
    # exactly the same way and is the likelier paste in this project.
    (
        "credential-uri",
        re.compile(
            rb"\b(?:postgres|postgresql|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss"
            rb"|amqp|amqps|ftp|ftps|sftp|ssh|s3|clickhouse)://[^/\s:@]+:[^@\s/]{8,}@[^\s/]+",
            re.I,
        ),
    ),
    # An ntfy topic is unauthenticated by design: the name IS the whole
    # credential, for reading and for forging. The old repository leaked its
    # topic into six committed paths, mostly as pasted working command lines.
    (
        "ntfy-topic",
        re.compile(rb"\bNTFY_TOPIC[\t ]*=[\t ]*[\"']?[A-Za-z0-9_-]{6,}"),
    ),
    # The URL form is how the old leak actually happened: a working command
    # line pasted whole. Anchored to this project's topic prefix so ntfy's own
    # documentation URLs (ntfy.sh/publish, ntfy.sh/docs) stay committable.
    ("ntfy-topic", re.compile(rb"\bntfy\.sh/verbatus[A-Za-z0-9_-]{4,}")),
    (
        "credential-url",
        re.compile(rb"https?://[^/\s:@]+:[^@\s/]{8,}@[^\s/]+", re.I),
    ),
)

# Path/rule/digest triples exempting one exact byte sequence at one exact path.
# Not a placeholder exemption: the same bytes elsewhere, or any newly invented
# topic at the same path, stay blocked. Full digests keep the 12-character
# display fingerprints out of the trust boundary.
#
# It is empty, and that is the intended resting state. Two triples lived here
# briefly to cover synthetic values in intermediate working commits that were
# never pushed; the tree those commits produced does not contain them, so the
# exemptions were dead the moment the work was assembled. A digest nobody can
# resolve back to a string is unauditable by construction, and an exemption
# nobody can audit inside a credential scanner is worse than no exemption at
# all. An empty set is readable at a glance. Anything added here must arrive
# with the reason, and must fail the scan when removed.
DECLARED_SECRET_FIXTURES = frozenset()

# The key half tolerates a vendor prefix joined by `_`, `-` or `.`. An
# earlier `\b` anchor was defeated by the commonest real spelling there is:
# `aws_secret_access_key`, where the underscore is a word character and so no
# boundary exists before `secret`. The trailing lookahead keeps the key from
# ending mid-word, and the assignment operator must still follow immediately,
# which is what keeps ordinary prose out.
GENERIC_ASSIGNMENT = re.compile(
    rb"""(?ix)
    (?P<key_quote>["']?)
    (?<![A-Za-z0-9])
    (?:[A-Za-z0-9]+[_.-]){0,4}
    (?:
        runpod_api_key|api[_-]?key|access[_-]?key|access[_-]?token|auth[_-]?token|
        session[_-]?token|refresh[_-]?token|bearer[_-]?token|
        client[_-]?secret|secret[_-]?key|secret[_-]?access[_-]?key|
        private[_-]?key|password|passwd|token|secret
    )(?![A-Za-z0-9])(?P=key_quote)
    [\t ]*(?::|=)[\t ]*["']?
    (?P<value>[A-Za-z0-9_./+=-]{20,})
    """
)


class ScanFailure(RuntimeError):
    """The check itself could not make a trustworthy decision."""


FILE_KINDS = (
    (stat.S_ISFIFO, "fifo"),
    (stat.S_ISSOCK, "socket"),
    (stat.S_ISCHR, "character device"),
    (stat.S_ISBLK, "block device"),
    (stat.S_ISDIR, "directory"),
    (stat.S_ISLNK, "symbolic link"),
)


def file_kind(mode: int) -> str:
    for predicate, name in FILE_KINDS:
        if predicate(mode):
            return name
    return "unknown file type"


def measured_read(path: Path, max_bytes: int | None) -> tuple[int, bytes | None]:
    """Return a regular file's size, and its bytes only if it is small enough.

    The size comes from the same descriptor that would be read, so a file that
    grows between the decision and the read cannot smuggle its payload in. A
    file past `max_bytes` is refused on its size alone, and pulling it into
    memory to say so would crash the scanner instead of producing the clean
    oversize diagnosis it exists to produce.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ScanFailure(f"{path} is not a regular file ({file_kind(info.st_mode)})")
        if max_bytes is not None and info.st_size > max_bytes:
            return info.st_size, None
        with open(fd, "rb", closefd=False) as handle:
            return info.st_size, handle.read()
    finally:
        os.close(fd)


def read_regular_file(path: Path) -> bytes:
    """Read a path only after the opened descriptor proves it is a regular file.

    A FIFO at a scanned path blocks a plain open forever when no writer
    exists, and this scanner runs from `pre-commit` and from CI: that is not a
    failed scan but a session or a build that never finishes and never says
    why. `O_NONBLOCK` makes the open itself return, and judging the type from
    `fstat` on the descriptor rather than from the name closes the race where
    a regular file is swapped for a FIFO between the check and the read.
    A device that reads cleanly is refused too: a successful read is not
    evidence that a regular file was scanned.
    """
    _, data = measured_read(path, None)
    return data


@dataclass(frozen=True)
class Blob:
    path: str
    oid: str
    mode: str
    kind: str = "blob"
    data: bytes | None = None
    # Set only for worktree entries whose bytes were deliberately not read.
    size: int | None = None


@dataclass(frozen=True)
class Fixture:
    path: str
    sha256: str
    size: int
    media_type: str
    source: str
    reason: str


@dataclass(frozen=True, order=True)
class Issue:
    path: str
    rule: str
    detail: str
    context: str = ""


def git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        command = "git " + " ".join(args)
        error = result.stderr.decode("utf-8", "replace").strip()
        raise ScanFailure(f"{command} failed ({result.returncode}): {error}")
    return result.stdout


# The size cache holds one integer per reachable object id. Blob payloads are
# much larger, so their cache has a separate aggregate byte budget. An entry
# count is the wrong bound here: 65 tiny, frequently reused blobs make a
# 64-entry LRU miss forever, while 64 one-MiB blobs consume the whole intended
# allowance. The byte-budgeted LRU can retain every small blob that fits and
# never retains a single blob larger than the entire allowance.
BLOB_CACHE_MAX_BYTES = 64 * 1_048_576


@lru_cache(maxsize=None)
def blob_size(oid: str) -> int:
    raw = git("cat-file", "-s", oid).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ScanFailure(f"Git returned a non-numeric size for object {oid[:12]}") from exc


class BlobDataCache:
    """Least-recently-used Git blob cache bounded by retained payload bytes."""

    def __init__(self, max_bytes: int):
        if max_bytes < 0:
            raise ValueError("blob cache byte limit must be non-negative")
        self.max_bytes = max_bytes
        self.bytes_used = 0
        self._entries: OrderedDict[str, bytes] = OrderedDict()

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def get(self, oid: str) -> bytes:
        try:
            data = self._entries.pop(oid)
        except KeyError:
            pass
        else:
            # Reinsert the same object at the MRU end. Its payload was already
            # counted, so a repeated OID must not grow the byte accounting.
            self._entries[oid] = data
            return data

        data = git("cat-file", "blob", oid)
        size = len(data)
        if size > self.max_bytes:
            # The caller still receives the bytes it requested, but the cache
            # cannot retain a payload larger than its whole memory allowance.
            return data

        while self._entries and self.bytes_used + size > self.max_bytes:
            _, evicted = self._entries.popitem(last=False)
            self.bytes_used -= len(evicted)

        self._entries[oid] = data
        self.bytes_used += size
        return data


_BLOB_DATA_CACHE = BlobDataCache(BLOB_CACHE_MAX_BYTES)


def blob_data(oid: str) -> bytes:
    return _BLOB_DATA_CACHE.get(oid)


def entry_size(entry: Blob) -> int:
    if entry.size is not None:
        return entry.size
    return len(entry.data) if entry.data is not None else blob_size(entry.oid)


def entry_data(entry: Blob) -> bytes:
    if entry.data is not None:
        return entry.data
    if entry.size is not None:
        # Refused on size, so its bytes were deliberately never read. Asking
        # for them anyway is a caller defect, not something to answer with an
        # empty payload that would scan clean.
        raise ScanFailure(f"{entry.path} was refused on size; its bytes were not read")
    return blob_data(entry.oid)


def index_tree() -> dict[str, Blob]:
    entries = {}
    for record in git("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
        except ValueError as exc:
            raise ScanFailure("Git returned a malformed index record") from exc
        path = os.fsdecode(raw_path)
        if stage != "0":
            raise ScanFailure(f"the index contains an unresolved merge entry at {path}")
        kind = "commit" if mode == "160000" else "blob"
        entries[path] = Blob(path, oid, mode, kind)
    return entries


def commit_tree(commit: str) -> dict[str, Blob]:
    entries = {}
    for record in git("ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
        except ValueError as exc:
            raise ScanFailure(f"Git returned a malformed tree record for {commit[:12]}") from exc
        path = os.fsdecode(raw_path)
        entries[path] = Blob(path, oid, mode, kind)
    return entries


def working_tree() -> dict[str, Blob]:
    """Read tracked and unignored untracked files exactly as they are on disk."""
    entries = {}
    records = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for raw_path in records.split(b"\0"):
        if not raw_path:
            continue
        path = os.fsdecode(raw_path)
        source = Path(path)
        try:
            mode = os.lstat(source).st_mode
            if stat.S_ISLNK(mode):
                entries[path] = Blob(
                    path, "worktree", "120000", data=os.fsencode(os.readlink(source))
                )
            elif stat.S_ISREG(mode):
                # No legal payload exceeds the fixture ceiling, so a file
                # above it is refused on size alone and never read: the file
                # too big to accept was the one being pulled into memory.
                size, data = measured_read(source, FIXTURE_MAX_BYTES)
                entries[path] = Blob(path, "worktree", "100644", data=data, size=size)
            elif stat.S_ISDIR(mode):
                entries[path] = Blob(path, "worktree", "160000", "commit")
            else:
                # A FIFO, socket or device used to fall through every branch
                # and the run still reported passed. GOVERNANCE 2: a partial
                # result is visibly partial, so it is named and it blocks.
                entries[path] = Blob(path, "worktree", "000000", file_kind(mode))
        except FileNotFoundError:
            # A tracked path missing from disk is a working-tree deletion, not
            # a payload to scan. The staged/index mode still inspects what Git
            # records.
            continue
    return entries


def repository_paths() -> list[str]:
    records = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return [os.fsdecode(raw) for raw in records.split(b"\0") if raw]


def path_issues(paths, context: str) -> list[Issue]:
    issues = []
    for path in paths:
        raw_path = os.fsencode(path)
        fingerprint = hashlib.sha256(raw_path).hexdigest()[:12]
        label = f"<repository-path:{fingerprint}>"
        if any(ord(char) < 32 or ord(char) == 127 for char in path):
            issues.append(
                Issue(
                    label,
                    "control-path",
                    "path contains an ASCII control character",
                    context,
                )
            )
        issues.extend(secret_issues(label, raw_path, context))
    return issues


def fixture_media(data: bytes) -> str | None:
    for media_type, (_, signatures) in MEDIA.items():
        if any(data.startswith(signature) for signature in signatures):
            return media_type
    return None


def is_binary(data: bytes) -> bool:
    if fixture_media(data) or b"\0" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def sensitive_filename(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    if name.startswith(".env.") and name.endswith(SAFE_ENV_SUFFIXES):
        return False
    return name in SENSITIVE_NAMES or name.startswith(".env.") or name.endswith(SENSITIVE_SUFFIXES)


def secret_issues(path: str, data: bytes, context: str) -> list[Issue]:
    issues = []
    seen = set()
    for rule, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(data):
            value = match.group(0)
            digest = hashlib.sha256(value).hexdigest()
            fingerprint = digest[:12]
            if (path, rule, digest) in DECLARED_SECRET_FIXTURES:
                continue
            key = (rule, fingerprint)
            if key in seen:
                continue
            seen.add(key)
            line = data.count(b"\n", 0, match.start()) + 1
            issues.append(
                Issue(
                    path,
                    rule,
                    f"possible credential at line {line}; fingerprint {fingerprint}",
                    context,
                )
            )

    for match in GENERIC_ASSIGNMENT.finditer(data):
        value = match.group("value")
        fingerprint = hashlib.sha256(value).hexdigest()[:12]
        key = ("literal-credential", fingerprint)
        if key in seen:
            continue
        seen.add(key)
        line = data.count(b"\n", 0, match.start()) + 1
        issues.append(
            Issue(
                path,
                "literal-credential",
                f"credential literal at line {line}; fingerprint {fingerprint}",
                context,
            )
        )
    return issues


def parse_manifest(
    entries: dict[str, Blob], context: str
) -> tuple[dict[str, Fixture], list[Issue]]:
    manifest_blob = entries.get(MANIFEST_PATH)
    if manifest_blob is None:
        return {}, []
    if manifest_blob.kind != "blob":
        return {}, [Issue(MANIFEST_PATH, "manifest", "manifest is not a file", context)]
    try:
        parsed = tomllib.loads(entry_data(manifest_blob).decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return {}, [Issue(MANIFEST_PATH, "manifest", f"cannot parse manifest: {exc}", context)]
    if parsed.get("version") != 1:
        return {}, [Issue(MANIFEST_PATH, "manifest", "version must be 1", context)]
    raw_fixtures = parsed.get("fixture", [])
    if not isinstance(raw_fixtures, list):
        return {}, [Issue(MANIFEST_PATH, "manifest", "fixture must be an array of tables", context)]

    fixtures = {}
    issues = []
    for number, raw in enumerate(raw_fixtures, start=1):
        label = f"fixture entry {number}"
        if not isinstance(raw, dict):
            issues.append(Issue(MANIFEST_PATH, "manifest", f"{label} is not a table", context))
            continue
        required = ("path", "sha256", "bytes", "media_type", "source", "reason")
        missing = [key for key in required if key not in raw]
        if missing:
            issues.append(
                Issue(
                    MANIFEST_PATH,
                    "manifest",
                    f"{label} is missing {', '.join(missing)}",
                    context,
                )
            )
            continue
        path = raw["path"]
        digest = raw["sha256"]
        size = raw["bytes"]
        media_type = raw["media_type"]
        source = raw["source"]
        reason = raw["reason"]
        if not all(isinstance(value, str) for value in (path, digest, media_type, source, reason)):
            issues.append(
                Issue(MANIFEST_PATH, "manifest", f"{label} has a non-string field", context)
            )
            continue
        normalized = PurePosixPath(path)
        valid_path = (
            path.startswith(FIXTURE_ROOT)
            and not normalized.is_absolute()
            and ".." not in normalized.parts
            and normalized.as_posix() == path
        )
        if not valid_path:
            issues.append(
                Issue(MANIFEST_PATH, "manifest", f"{label} has an invalid fixture path", context)
            )
            continue
        if path in fixtures:
            issues.append(Issue(MANIFEST_PATH, "manifest", f"duplicate entry for {path}", context))
            continue
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            issues.append(Issue(MANIFEST_PATH, "manifest", f"{label} has invalid bytes", context))
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            issues.append(Issue(MANIFEST_PATH, "manifest", f"{label} has invalid sha256", context))
            continue
        if media_type not in MEDIA:
            issues.append(
                Issue(MANIFEST_PATH, "manifest", f"{label} has unsupported media_type", context)
            )
            continue
        extensions, _ = MEDIA[media_type]
        if normalized.suffix.lower() not in extensions:
            issues.append(
                Issue(
                    MANIFEST_PATH,
                    "manifest",
                    f"{label} extension does not match {media_type}",
                    context,
                )
            )
            continue
        if not source.strip() or not reason.strip():
            issues.append(
                Issue(MANIFEST_PATH, "manifest", f"{label} needs source and reason", context)
            )
            continue
        fixtures[path] = Fixture(path, digest, size, media_type, source, reason)
    return fixtures, issues


def scan_tree(entries: dict[str, Blob], context: str) -> list[Issue]:
    fixtures, issues = parse_manifest(entries, context)
    issues.extend(path_issues(entries, context))
    fixture_total = 0

    for path, fixture in fixtures.items():
        entry = entries.get(path)
        if entry is None:
            issues.append(
                Issue(path, "fixture", "manifest entry points to a missing file", context)
            )
            continue
        if entry.kind != "blob":
            issues.append(Issue(path, "fixture", "fixture is not a Git blob", context))
            continue
        actual_size = entry_size(entry)
        fixture_total += actual_size
        if actual_size > FIXTURE_MAX_BYTES:
            issues.append(
                Issue(
                    path,
                    "fixture-size",
                    f"{actual_size} bytes exceeds the {FIXTURE_MAX_BYTES}-byte fixture limit",
                    context,
                )
            )
            continue
        data = entry_data(entry)
        actual_digest = hashlib.sha256(data).hexdigest()
        actual_media = fixture_media(data)
        if actual_size != fixture.size:
            issues.append(Issue(path, "fixture", "byte count does not match manifest", context))
        if actual_digest != fixture.sha256:
            issues.append(Issue(path, "fixture", "SHA-256 does not match manifest", context))
        if actual_media != fixture.media_type:
            issues.append(
                Issue(path, "fixture", "file signature does not match media_type", context)
            )

    if fixture_total > FIXTURE_TOTAL_MAX_BYTES:
        issues.append(
            Issue(
                MANIFEST_PATH,
                "fixture-total",
                f"{fixture_total} bytes exceeds the {FIXTURE_TOTAL_MAX_BYTES}-byte total limit",
                context,
            )
        )

    for path, entry in entries.items():
        if path.startswith("private/") and path != PRIVATE_README:
            # `.gitignore` prevents an ordinary add, but `git add -f` bypasses
            # it. The directory's contract is stronger: local material never
            # enters history, whether or not its content resembles a secret.
            issues.append(
                Issue(path, "private-path", "local private material may not enter Git", context)
            )
        if sensitive_filename(path):
            issues.append(Issue(path, "sensitive-filename", "credential-prone filename", context))
        if entry.mode == "120000":
            issues.append(
                Issue(
                    path,
                    "symlink",
                    "symbolic links are not allowed; targets vary by machine and can escape the tree",
                    context,
                )
            )
            continue
        if entry.kind != "blob":
            if entry.kind in ("commit", "tree", "tag"):
                rule = "unsupported-object"
                detail = f"tracked Git object has type {entry.kind}"
            else:
                rule = "unscannable"
                detail = f"path is a {entry.kind} and cannot be scanned as a file"
            issues.append(Issue(path, rule, detail, context))
            continue

        size = entry_size(entry)
        fixture = fixtures.get(path)
        limit = FIXTURE_MAX_BYTES if fixture else NORMAL_MAX_BYTES
        if size > limit:
            if not fixture:
                issues.append(
                    Issue(
                        path,
                        "oversize",
                        f"{size} bytes exceeds the {NORMAL_MAX_BYTES}-byte repository limit",
                        context,
                    )
                )
            continue

        data = entry_data(entry)
        issues.extend(secret_issues(path, data, context))
        if data.startswith(LFS_HEADER):
            issues.append(Issue(path, "git-lfs", "Git LFS pointers are not allowed", context))
        if is_binary(data) and not fixture:
            issues.append(
                Issue(
                    path,
                    "binary",
                    "binary payload is not hash-bound in proof/fixtures.toml",
                    context,
                )
            )
    return issues


def unique_issues(issues: list[Issue]) -> list[Issue]:
    unique = {}
    for issue in issues:
        key = (issue.path, issue.rule, issue.detail)
        unique.setdefault(key, issue)
    return sorted(unique.values())


def scan_history(revision: str) -> list[Issue]:
    shallow = git("rev-parse", "--is-shallow-repository").strip()
    if shallow == b"true":
        raise ScanFailure(
            "history scan requires a complete clone; fetch full history before retrying"
        )
    if shallow != b"false":
        raise ScanFailure("Git could not determine whether the repository is shallow")
    commits = git("rev-list", revision).decode("ascii").splitlines()
    if not commits:
        raise ScanFailure(f"revision {revision!r} resolved to no commits")
    issues = []
    for commit in commits:
        issues.extend(scan_tree(commit_tree(commit), commit[:12]))
        message = git("show", "-s", "--format=%B", commit)
        issues.extend(secret_issues("<commit-message>", message, commit[:12]))
    return unique_issues(issues)


def scan_ref_object(revision: str) -> list[Issue]:
    """Scan every annotated-tag object in a ref's peel chain.

    `git rev-list` peels tags to commits and therefore never exposes annotated
    tag messages. Tags are immutable under the local policy, so letting one
    leave with a credential in its message would preserve the secret in the
    exact object the guard then refuses to delete.
    """
    raw_oid = git("rev-parse", "--verify", revision).strip()
    try:
        oid = raw_oid.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ScanFailure(f"Git returned a non-ASCII object id for {revision!r}") from exc

    issues = []
    seen = set()
    while True:
        if oid in seen:
            raise ScanFailure(f"tag object chain for {revision!r} contains a cycle")
        seen.add(oid)

        raw_kind = git("cat-file", "-t", oid).strip()
        try:
            kind = raw_kind.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ScanFailure(f"Git returned a non-ASCII object type for {oid[:12]}") from exc
        if kind == "commit":
            return unique_issues(issues)
        if kind != "tag":
            raise ScanFailure(
                f"ref object {oid[:12]} has unsupported type {kind!r}; expected tag or commit"
            )

        data = git("cat-file", "tag", oid)
        issues.extend(secret_issues("<annotated-tag>", data, oid[:12]))
        first_line = data.split(b"\n", 1)[0]
        match = re.fullmatch(rb"object ([0-9a-f]{40}|[0-9a-f]{64})", first_line)
        if match is None:
            raise ScanFailure(f"annotated tag {oid[:12]} has no valid object header")
        oid = match.group(1).decode("ascii")


def scan_audit_fields(data: bytes) -> list[Issue]:
    """Scan the two NUL-delimited fields written into an audit receipt."""
    fields = data.split(b"\0")
    if len(fields) != 3 or fields[-1]:
        raise ScanFailure("audit input must contain exactly two NUL-delimited fields")
    return secret_issues("<audit-receipt>", b"\n".join(fields[:2]), "receipt")


def scan_ref_fields(data: bytes) -> list[Issue]:
    """Scan one or more NUL-delimited Git ref names without echoing them."""
    fields = data.split(b"\0")
    if len(fields) < 2 or fields[-1]:
        raise ScanFailure("ref input must contain one or more NUL-delimited fields")
    issues = []
    nonempty = 0
    for value in fields[:-1]:
        if not value:
            continue
        nonempty += 1
        fingerprint = hashlib.sha256(value).hexdigest()[:12]
        issues.extend(secret_issues(f"<git-ref:{fingerprint}>", value, "ref"))
    if not nonempty:
        raise ScanFailure("ref input contains no ref name")
    return issues


def scan_audit_receipt(path: Path) -> list[Issue]:
    """Validate a complete receipt and scan all of its text for credentials."""
    data = read_regular_file(path)
    issues = secret_issues("<audit-receipt>", data, "receipt")

    def malformed(detail: str) -> list[Issue]:
        return issues + [Issue("<audit-receipt>", "receipt-shape", detail, "receipt")]

    if b"\r" in data or not data.endswith(b"\n"):
        return malformed("receipt must use LF lines and end with a newline")
    lines = data.split(b"\n")[:-1]
    if len(lines) < 3:
        return malformed("receipt header is incomplete")
    if re.fullmatch(rb"commit:  [0-9a-f]{40}|commit:  [0-9a-f]{64}", lines[0]) is None:
        return malformed("receipt has no valid commit header")
    if not lines[1].startswith(b"branch:  ") or not lines[1][9:]:
        return malformed("receipt has no branch header")
    if not lines[2].startswith(b"audited: ") or not lines[2][9:]:
        return malformed("receipt has no audited-range header")

    blocks = lines[3:]
    if len(blocks) % 4:
        return malformed("receipt ends inside a reviewer record")
    for offset in range(0, len(blocks), 4):
        separator, auditor, when, finding = blocks[offset : offset + 4]
        if separator:
            return malformed("reviewer records must be separated by one blank line")
        if not auditor.startswith(b"auditor: ") or not auditor[9:]:
            return malformed("reviewer record has no auditor")
        if (
            re.fullmatch(
                rb"when:    [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
                when,
            )
            is None
        ):
            return malformed("reviewer record has no valid timestamp")
        if not finding.startswith(b"finding: ") or not finding[9:]:
            return malformed("reviewer record has no finding")
    return issues


def safe_output(text: str, label: str) -> str:
    """Redact a whole output field if it contains credential-shaped text."""
    data = os.fsencode(text)
    if not secret_issues("<output>", data, "output"):
        return text
    fingerprint = hashlib.sha256(data).hexdigest()[:12]
    return f"<redacted-{label}:{fingerprint}>"


def report(issues: list[Issue], limit: int = 100) -> int:
    if not issues:
        print("Ingress check passed for the requested scope.")
        return 0
    print("BLOCKED: repository ingress policy failed.", file=sys.stderr)
    shown = issues if limit == 0 else issues[:limit]
    for issue in shown:
        path = safe_output(issue.path, "metadata")
        context = safe_output(issue.context, "context")
        detail = safe_output(issue.detail, "detail")
        where = f" ({context})" if context else ""
        print(f"  {path}{where}: [{issue.rule}] {detail}", file=sys.stderr)
    if len(shown) < len(issues):
        # The remainder used to be unreachable: a count, and no way to see
        # what it counted.
        print(
            f"  ...and {len(issues) - len(shown)} more issue(s); "
            "re-run with --max-findings 0 to list them all",
            file=sys.stderr,
        )
    print(
        "Recognized credentials are forbidden; repository trees must also satisfy payload policy.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="scan the exact Git index state")
    mode.add_argument(
        "--worktree",
        action="store_true",
        help="scan tracked and unignored untracked working files",
    )
    mode.add_argument("--history", metavar="REV", help="scan every commit reachable from REV")
    mode.add_argument(
        "--ref-object",
        metavar="REV",
        help="scan annotated-tag messages in REV's object peel chain",
    )
    mode.add_argument("--message-file", metavar="PATH", help="scan one commit message file")
    mode.add_argument(
        "--file",
        metavar="PATH",
        action="append",
        help="scan an arbitrary file for credentials; repeat for more than one file",
    )
    mode.add_argument(
        "--stdin-file",
        action="store_true",
        help="credential-scan arbitrary file bytes from standard input before persisting them",
    )
    mode.add_argument(
        "--audit-fields",
        action="store_true",
        help="scan exactly two NUL-delimited audit receipt fields from standard input",
    )
    mode.add_argument(
        "--ref-fields",
        action="store_true",
        help="scan one or more NUL-delimited Git ref names from standard input",
    )
    mode.add_argument(
        "--audit-receipt",
        metavar="PATH",
        help="validate and credential-scan a complete audit receipt",
    )
    mode.add_argument(
        "--paths",
        action="store_true",
        help="check repository paths for characters unsafe to line-oriented policies",
    )
    parser.add_argument(
        "--max-findings",
        type=int,
        default=100,
        metavar="N",
        help="print at most N findings; 0 prints every finding",
    )
    args = parser.parse_args(argv)
    if args.max_findings < 0:
        parser.error("--max-findings must be 0 or greater")
    try:
        if args.staged:
            issues = scan_tree(index_tree(), "index")
        elif args.worktree:
            issues = scan_tree(working_tree(), "worktree")
        elif args.message_file:
            data = read_regular_file(Path(args.message_file))
            issues = secret_issues("<commit-message>", data, "message")
        elif args.file:
            issues = []
            for path in args.file:
                issues.extend(secret_issues(path, read_regular_file(Path(path)), "file"))
        elif args.stdin_file:
            issues = secret_issues("<standard-input>", sys.stdin.buffer.read(), "buffer")
        elif args.ref_object:
            issues = scan_ref_object(args.ref_object)
        elif args.audit_fields:
            issues = scan_audit_fields(sys.stdin.buffer.read())
        elif args.ref_fields:
            issues = scan_ref_fields(sys.stdin.buffer.read())
        elif args.audit_receipt:
            issues = scan_audit_receipt(Path(args.audit_receipt))
        elif args.paths:
            issues = path_issues(repository_paths(), "worktree")
        else:
            issues = scan_history(args.history)
        return report(unique_issues(issues), args.max_findings)
    except (OSError, ScanFailure) as exc:
        error = safe_output(str(exc), "error")
        print(f"BLOCKED: ingress check could not run: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
