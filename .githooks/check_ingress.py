#!/usr/bin/env python3
"""Keep credentials and repository-sized payloads out of Git.

The pre-commit hook scans the exact index state. CI scans every commit reachable
from HEAD, so add-then-delete does not make a leaked key or corpus file disappear.
Only the Python standard library and Git are required.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

NORMAL_MAX_BYTES = 1_048_576
FIXTURE_MAX_BYTES = 25 * 1_048_576
FIXTURE_TOTAL_MAX_BYTES = 100 * 1_048_576
MANIFEST_PATH = "proof/fixtures.toml"
FIXTURE_ROOT = "proof/fixtures/"

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
    ("slack-token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("stripe-live-key", re.compile(rb"\bsk_live_[A-Za-z0-9]{20,}\b")),
    (
        "credential-url",
        re.compile(rb"https?://[^/\s:@]+:[^@\s/]{8,}@[^\s/]+", re.I),
    ),
)

GENERIC_ASSIGNMENT = re.compile(
    rb"""(?ix)
    (?P<key_quote>["']?)
    \b(?:
        runpod_api_key|api[_-]?key|access[_-]?token|auth[_-]?token|
        client[_-]?secret|secret[_-]?key|password|passwd|token|secret
    )\b(?P=key_quote)
    [\t ]*(?::|=)[\t ]*["']?
    (?P<value>[A-Za-z0-9_./+=-]{20,})
    """
)

PLACEHOLDERS = (
    b"CHANGEME",
    b"DUMMY",
    b"EXAMPLE",
    b"PLACEHOLDER",
    b"REDACTED",
    b"SAMPLE",
    b"TEST",
    b"YOUR",
)


class ScanFailure(RuntimeError):
    """The check itself could not make a trustworthy decision."""


@dataclass(frozen=True)
class Blob:
    path: str
    oid: str
    mode: str
    kind: str = "blob"
    data: bytes | None = None


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


@lru_cache(maxsize=None)
def blob_size(oid: str) -> int:
    raw = git("cat-file", "-s", oid).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ScanFailure(f"Git returned a non-numeric size for object {oid[:12]}") from exc


@lru_cache(maxsize=None)
def blob_data(oid: str) -> bytes:
    return git("cat-file", "blob", oid)


def entry_size(entry: Blob) -> int:
    return len(entry.data) if entry.data is not None else blob_size(entry.oid)


def entry_data(entry: Blob) -> bytes:
    return entry.data if entry.data is not None else blob_data(entry.oid)


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
        if source.is_symlink():
            entries[path] = Blob(path, "worktree", "120000", data=os.fsencode(os.readlink(source)))
        elif source.is_file():
            entries[path] = Blob(path, "worktree", "100644", data=source.read_bytes())
        elif source.is_dir():
            entries[path] = Blob(path, "worktree", "160000", "commit")
        # A tracked path missing from disk is a working-tree deletion, not a
        # payload to scan. The staged/index mode still inspects what Git records.
    return entries


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


def entropy(value: bytes) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def is_placeholder(value: bytes) -> bool:
    upper = value.upper()
    return any(word in upper for word in PLACEHOLDERS) or len(set(value)) < 6


def secret_issues(path: str, data: bytes, context: str) -> list[Issue]:
    issues = []
    seen = set()
    for rule, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(data):
            value = match.group(0)
            if rule != "private-key" and is_placeholder(value):
                continue
            fingerprint = hashlib.sha256(value).hexdigest()[:12]
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
        if is_placeholder(value) or entropy(value) < 3.0:
            continue
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
                f"high-entropy credential literal at line {line}; fingerprint {fingerprint}",
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
        if sensitive_filename(path):
            issues.append(Issue(path, "sensitive-filename", "credential-prone filename", context))
        if entry.kind != "blob":
            issues.append(
                Issue(
                    path, "unsupported-object", f"tracked Git object has type {entry.kind}", context
                )
            )
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
    return unique_issues(issues)


def report(issues: list[Issue]) -> int:
    if not issues:
        print("Ingress check passed: no secrets, undeclared binaries, or oversized blobs.")
        return 0
    print("BLOCKED: repository ingress policy failed.", file=sys.stderr)
    for issue in issues[:100]:
        where = f" ({issue.context})" if issue.context else ""
        print(f"  {issue.path}{where}: [{issue.rule}] {issue.detail}", file=sys.stderr)
    if len(issues) > 100:
        print(f"  ...and {len(issues) - 100} more issue(s)", file=sys.stderr)
    print("Secrets and corpus-sized payloads must not enter Git history.", file=sys.stderr)
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
    args = parser.parse_args(argv)
    try:
        if args.staged:
            issues = scan_tree(index_tree(), "index")
        elif args.worktree:
            issues = scan_tree(working_tree(), "worktree")
        else:
            issues = scan_history(args.history)
        return report(unique_issues(issues))
    except (OSError, ScanFailure) as exc:
        print(f"BLOCKED: ingress check could not run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
