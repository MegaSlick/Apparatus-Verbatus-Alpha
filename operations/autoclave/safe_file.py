#!/usr/bin/env python3
"""Move bytes into and out of `/out` without ever following its last path component.

`/out` is the one host path a chamber agent can write, so everything the launcher
reads back from it is untrusted input, and everything it writes into it is a write to
a name the agent controls. Testing a path and then acting on it leaves a window: the
agent is *running* while that happens, so it can pass the test as a regular file and
be acted on as a symlink — into any file this user can read or write.

Closing that needs the path opened once, with the kernel refusing to follow a final
symlink. `O_NOFOLLOW` is that, and no POSIX shell can ask for it. Python can, and this
repository already requires python3 everywhere — the hooks, the static gate and the
suite are all python3, so this adds no dependency the launcher did not already have.

The shell tests in the launcher stay in front of these calls. They say *what* is
wrong, in a sentence an operator can act on; this says *no* whatever happens.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def worktree_path_for_ref(root: Path, ref: str) -> Path | None:
    """Return an exact checked-out path without line-delimiting Git path bytes."""
    result = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"],
        stdout=subprocess.PIPE,
        check=True,
    )
    wanted = os.fsencode(ref)
    matches: list[Path] = []
    for record in result.stdout.split(b"\0\0"):
        fields = record.split(b"\0")
        path = next((field[9:] for field in fields if field.startswith(b"worktree ")), None)
        branch = next((field[7:] for field in fields if field.startswith(b"branch ")), None)
        if branch == wanted and path is not None:
            matches.append(Path(os.fsdecode(path)))
    if len(matches) > 1:
        raise OSError(f"{ref} is checked out in more than one worktree")
    return matches[0] if matches else None


def open_regular(path: Path, flags: int, mode: int = 0o600):
    """Open once, refuse a link or anything that is not a regular file, keep the fd.

    `O_NONBLOCK` matters before the `fstat`: opening a FIFO for reading otherwise
    waits forever for a writer, so an agent could hang the launcher by leaving one in
    the slot rather than by pointing it anywhere. On a regular file the flag has no
    effect at all.
    """
    descriptor = os.open(
        path,
        flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        mode,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"{path} is not a regular file")
        return os.fdopen(descriptor, "rb" if flags == os.O_RDONLY else "wb")
    except BaseException:
        os.close(descriptor)
        raise


def main() -> int:
    """Move untrusted bytes, or inspect worktree occupancy without flattening paths.

    `read SLOT` reads *out of* `/out`: the slot is the agent's and is opened with
    `O_NOFOLLOW`.

    `write SOURCE SLOT` writes *into* `/out`: the slot is the agent's and is opened
    with `O_NOFOLLOW`; the source is a path the session chose and is opened normally.
    Refusing to follow a link there would break the ordinary way of pointing at a
    standing brief, and would refuse it with a message naming the wrong file.

    `bundle SLOT REF` opens the untrusted output slot once and gives that descriptor to
    `git bundle create - REF`, so Git never resolves the slot path itself.

    `worktree ROOT REF occupied|tree` parses Git's NUL-delimited worktree records.
    It emits only a fixed word or object ID, so even a path containing a newline never
    crosses the shell boundary.
    """
    try:
        command = sys.argv[1]
        source = Path(sys.argv[2])
        if command == "read" and len(sys.argv) == 3:
            with open_regular(source, os.O_RDONLY) as reading:
                shutil.copyfileobj(reading, sys.stdout.buffer)
            return 0
        if command == "write" and len(sys.argv) == 4:
            with (
                open(source, "rb") as reading,
                open_regular(Path(sys.argv[3]), os.O_WRONLY | os.O_CREAT) as writing,
            ):
                # The launcher prints the drawer's brief path, so using that path as
                # the next dispatch input is an ordinary workflow. Opening it again
                # with O_TRUNC would empty the inode underneath the already-open read
                # descriptor. Compare the opened objects, not their path spellings;
                # aliases and hard links are the same case.
                if os.path.samestat(os.fstat(reading.fileno()), os.fstat(writing.fileno())):
                    return 0
                os.ftruncate(writing.fileno(), 0)
                shutil.copyfileobj(reading, writing)
            return 0
        if command == "bundle" and len(sys.argv) == 4:
            with open_regular(source, os.O_WRONLY | os.O_CREAT | os.O_TRUNC) as writing:
                subprocess.run(
                    ["git", "bundle", "create", "-", "--end-of-options", sys.argv[3]],
                    stdout=writing,
                    check=True,
                )
            return 0
        if command == "worktree" and len(sys.argv) == 5:
            mode = sys.argv[4]
            if mode not in {"occupied", "tree"}:
                raise OSError(f"unknown worktree mode: {mode}")
            path = worktree_path_for_ref(source, sys.argv[3])
            if path is None:
                print("absent")
                return 0
            if mode == "occupied":
                print("occupied")
                return 0
            if mode == "tree":
                tree = subprocess.run(
                    ["git", "-C", str(path), "write-tree"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree) is None:
                    raise OSError("git write-tree returned an invalid object ID")
                print(f"tree:{tree}")
                return 0
    except (IndexError, OSError, subprocess.CalledProcessError) as failure:
        # The message names the path and the errno and nothing else. A refusal that
        # quoted what it found would print the very bytes the refusal exists to
        # withhold.
        print(f"safe-file: {failure}", file=sys.stderr)
        return 1
    print(
        "usage: safe_file.py read SLOT | write SOURCE SLOT | bundle SLOT REF | "
        "worktree ROOT REF occupied|tree",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
