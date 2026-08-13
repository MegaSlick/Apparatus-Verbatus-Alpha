#!/usr/bin/env python3
"""Prepare an immutable review candidate and bind reports to its exact commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from pathlib import Path

GIT_ENV = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()


def clean_candidate(root: Path, candidate: str) -> tuple[str, str]:
    resolved = git(root, "rev-parse", "HEAD")
    expected = git(root, "rev-parse", f"{candidate}^{{commit}}")
    if resolved != expected:
        raise ValueError(f"HEAD is {resolved}, not candidate {expected}")
    dirty = git(root, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        raise ValueError("the candidate tree is dirty")
    return resolved, git(root, "rev-parse", f"{resolved}^{{tree}}")


def prepare(root: Path, base: str) -> dict[str, str]:
    candidate, tree = clean_candidate(root, "HEAD")
    base_sha = resolve_base(root, base)
    require_ancestor(root, base_sha, candidate)
    return {"candidate": candidate, "base": base_sha, "tree": tree}


def require_ancestor(root: Path, base: str, candidate: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", base, candidate],
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )
    if result.returncode == 1:
        raise ValueError(f"base {base} is not an ancestor of candidate {candidate}")
    if result.returncode:
        detail = result.stderr.strip() or f"git exited {result.returncode}"
        raise ValueError(f"could not verify base {base} against candidate {candidate}: {detail}")


def resolve_base(root: Path, base: str) -> str:
    try:
        return git(root, "rev-parse", f"{base}^{{commit}}")
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or f"git exited {error.returncode}"
        raise ValueError(f"base {base!r} is unknown or is not a commit: {detail}") from error


def read_regular_file(path: Path, noun: str) -> bytes:
    """Read one regular, non-symlink file through the descriptor we validate."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ValueError(f"safe {noun} reads require O_NOFOLLOW support")
    flags = os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"the {noun} cannot be opened safely: {error}") from error
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"the {noun} is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def report_names(data: bytes, label: str, identity: str) -> bool:
    """Require a labelled, complete identity rather than a SHA substring.

    The optional carriage return matters: `$` in multiline mode matches before the `\\n`,
    so a report saved with CRLF endings leaves a `\\r` between the SHA and the line end.
    Without this the receipt refuses a report that does name its candidate, and blames
    the missing SHA rather than the line ending.
    """
    expression = (
        rb"(?m)^" + re.escape(label.encode()) + rb":[ \t]*" + identity.encode() + rb"[ \t]*\r?$"
    )
    return re.search(expression, data) is not None


def fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_immutable(path: Path, data: bytes, noun: str) -> None:
    """Durably publish bytes once, or prove the existing immutable copy matches."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ValueError(f"safe {noun} publication requires O_NOFOLLOW support")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(temporary, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read_regular_file(path, noun) != data:
                raise ValueError(f"an immutable {noun} already exists at {path}") from None
        fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            else:
                fsync_directory(path.parent)


def receipt(
    root: Path, candidate: str, base: str, reviewer: str, report: Path
) -> tuple[Path, dict[str, str]]:
    candidate_sha, tree = clean_candidate(root, candidate)
    base_sha = resolve_base(root, base)
    require_ancestor(root, base_sha, candidate_sha)
    data = read_regular_file(report, "review report")
    if not data.strip():
        raise ValueError("the review report is empty")
    if not report_names(data, "Candidate", candidate_sha):
        raise ValueError("the review report must name the exact full Candidate SHA")
    if not report_names(data, "Base", base_sha):
        raise ValueError("the review report must name the exact full Base SHA")
    slug = re.sub(r"[^a-z0-9]+", "-", reviewer.lower()).strip("-")
    if not slug:
        raise ValueError("the reviewer name has no usable characters")
    report_sha256 = hashlib.sha256(data).hexdigest()
    directory = root / "workbench" / "raw" / "reviews" / candidate_sha
    snapshot = directory / f"{slug}-{report_sha256}.md"
    record = {
        "reviewer": reviewer,
        "candidate": candidate_sha,
        "base": base_sha,
        "tree": tree,
        "report": str(snapshot.absolute()),
        "report_sha256": report_sha256,
    }
    output = directory / f"{slug}-{report_sha256}.json"
    encoded = (json.dumps(record, indent=2) + "\n").encode()
    publish_immutable(snapshot, data, "review-report snapshot")
    publish_immutable(output, encoded, "review receipt")
    return output, record


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--root", type=Path, default=Path.cwd())
    commands = result.add_subparsers(dest="command", required=True)
    preparing = commands.add_parser("prepare")
    preparing.add_argument("--base", default="origin/main")
    recording = commands.add_parser("receipt")
    recording.add_argument("--candidate", required=True)
    recording.add_argument("--base", required=True)
    recording.add_argument("--reviewer", required=True)
    recording.add_argument("--report", required=True, type=Path)
    return result


def main() -> int:
    arguments = parser().parse_args()
    root = arguments.root.resolve()
    try:
        if arguments.command == "prepare":
            print(json.dumps(prepare(root, arguments.base), indent=2))
        else:
            output, record = receipt(
                root,
                arguments.candidate,
                arguments.base,
                arguments.reviewer,
                arguments.report,
            )
            print(json.dumps({"receipt": str(output), **record}, indent=2))
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or f"git exited {error.returncode}"
        print(f"candidate: git command failed: {detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"candidate: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
