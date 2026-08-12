#!/usr/bin/env python3
"""Prepare an immutable review candidate and bind reports to its exact commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def clean_candidate(root: Path, candidate: str) -> tuple[str, str]:
    resolved = git(root, "rev-parse", "HEAD")
    expected = git(root, "rev-parse", f"{candidate}^{{commit}}")
    if resolved != expected:
        raise ValueError(f"HEAD is {resolved}, not candidate {expected}")
    dirty = git(root, "status", "--porcelain")
    if dirty:
        raise ValueError("the candidate tree is dirty")
    return resolved, git(root, "rev-parse", f"{resolved}^{{tree}}")


def prepare(root: Path, base: str) -> dict[str, str]:
    candidate, tree = clean_candidate(root, "HEAD")
    base_sha = git(root, "rev-parse", f"{base}^{{commit}}")
    subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", base_sha, candidate],
        check=True,
        capture_output=True,
    )
    return {"candidate": candidate, "base": base_sha, "tree": tree}


def require_ancestor(root: Path, base: str, candidate: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", base, candidate],
        check=True,
        capture_output=True,
    )


def receipt(
    root: Path, candidate: str, base: str, reviewer: str, report: Path
) -> tuple[Path, dict[str, str]]:
    candidate_sha, tree = clean_candidate(root, candidate)
    base_sha = git(root, "rev-parse", f"{base}^{{commit}}")
    require_ancestor(root, base_sha, candidate_sha)
    if report.is_symlink() or not report.is_file():
        raise ValueError("the review report is not a regular file")
    data = report.read_bytes()
    if not data.strip():
        raise ValueError("the review report is empty")
    if candidate_sha.encode() not in data:
        raise ValueError("the review report does not name the full candidate SHA")
    slug = re.sub(r"[^a-z0-9]+", "-", reviewer.lower()).strip("-")
    if not slug:
        raise ValueError("the reviewer name has no usable characters")
    report_sha256 = hashlib.sha256(data).hexdigest()
    record = {
        "reviewer": reviewer,
        "candidate": candidate_sha,
        "base": base_sha,
        "tree": tree,
        "report": str(report.resolve()),
        "report_sha256": report_sha256,
    }
    output = (
        root / "workbench" / "raw" / "reviews" / candidate_sha / f"{slug}-{report_sha256[:12]}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output)
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
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"candidate: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
