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
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

COPY_BLOCK_BYTES = 1024 * 1024


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


def _drawer_ancestor(source: Path, drawer_descriptor: int) -> tuple[int, tuple[str, ...]] | None:
    """Return an opened alias of ``drawer`` and the lexical suffix below it.

    A case-insensitive filesystem or a symlink outside the drawer can give the
    same directory another spelling.  Compare opened directory identities, not
    strings, and keep the matching descriptor for the later no-follow walk.
    """

    drawer_stat = os.fstat(drawer_descriptor)
    source_absolute = Path(os.path.abspath(source))
    suffix: tuple[str, ...] = (source_absolute.name,)
    ancestor = source_absolute.parent
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    while True:
        try:
            candidate = os.open(ancestor, directory_flags)
        except OSError:
            candidate = -1
        if candidate >= 0:
            if os.path.samestat(os.fstat(candidate), drawer_stat):
                return candidate, suffix
            os.close(candidate)
        if ancestor.parent == ancestor:
            return None
        suffix = (ancestor.name, *suffix)
        ancestor = ancestor.parent


def open_write_source(source: Path, drawer: Path):
    """Open a dispatch source without trusting agent-writable drawer components.

    Sources outside the drawer may be the session's standing-brief symlinks;
    sources beneath it may follow no component. Containment is directory
    identity, not spelling, so case variants and an outside alias to the drawer
    receive the untrusted treatment too.
    """

    drawer_absolute = Path(os.path.abspath(drawer))
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        drawer_descriptor = os.open(drawer_absolute, directory_flags)
        try:
            match = _drawer_ancestor(source, drawer_descriptor)
            if match is None:
                # Outside the drawer the path is the session's own, so its open
                # happens after this guard. Inside the guard, an ordinary
                # permission error or vanished standing brief was rewritten as
                # "cannot safely open ... beneath <drawer>", and the launcher then
                # blamed an agent-controlled symlink -- sending the operator to
                # inspect the chamber's drawer over a fault on their own file.
                external = True
            else:
                external = False
                directory, relative_parts = match
            if not external:
                try:
                    for component in relative_parts[:-1]:
                        child = os.open(component, directory_flags, dir_fd=directory)
                        os.close(directory)
                        directory = child
                    descriptor = os.open(
                        relative_parts[-1],
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                        dir_fd=directory,
                    )
                finally:
                    os.close(directory)
        finally:
            os.close(drawer_descriptor)
    except OSError as error:
        raise OSError(f"cannot safely open {source} beneath {drawer}: {error}") from error
    if external:
        return open(source, "rb")
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"{source} is not a regular file")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def byte_limit(value: str) -> int:
    """Parse the launcher's bounded decimal byte limits without integer surprises."""

    if re.fullmatch(r"[1-9][0-9]{0,9}", value) is None:
        raise OSError("the byte limit is not a positive bounded decimal integer")
    return int(value)


def copy_bounded(reading, writing, limit: int) -> None:
    """Copy at most ``limit`` bytes and refuse a file that changes size while read."""

    expected = os.fstat(reading.fileno()).st_size
    if expected > limit:
        raise OSError(f"input is larger than the {limit}-byte limit")
    copied = 0
    while block := reading.read(min(COPY_BLOCK_BYTES, limit - copied + 1)):
        if copied + len(block) > limit:
            raise OSError(f"input grew beyond the {limit}-byte limit while being read")
        writing.write(block)
        copied += len(block)
    if copied != expected:
        raise OSError("input changed size while being read")


def main() -> int:
    """Move untrusted bytes, or inspect worktree occupancy without flattening paths.

    `read SLOT` reads *out of* `/out`: the slot is the agent's and is opened with
    `O_NOFOLLOW`.

    `write SOURCE SLOT` writes *into* `/out`: the slot is the agent's and is opened
    with `O_NOFOLLOW`. A source outside that drawer is a path the session chose and
    may be a standing-brief symlink. A source inside the drawer is agent-writable,
    so it receives the same regular-file/no-follow treatment as every other drawer
    input. This distinction remains true while a dispatch waits for its lock.

    `bundle SLOT REF` opens the untrusted output slot once and gives that descriptor to
    `git bundle create - REF`, so Git never resolves the slot path itself.

    `worktree ROOT REF occupied|tree` parses Git's NUL-delimited worktree records.
    It emits only a fixed word or object ID, so even a path containing a newline never
    crosses the shell boundary.
    """
    try:
        command = sys.argv[1]
        source = Path(sys.argv[2])
        if command == "read" and len(sys.argv) == 4:
            limit = byte_limit(sys.argv[3])
            with open_regular(source, os.O_RDONLY) as reading:
                copy_bounded(reading, sys.stdout.buffer, limit)
            return 0
        if command == "write" and len(sys.argv) == 5:
            limit = byte_limit(sys.argv[4])
            destination = Path(sys.argv[3])
            with open_write_source(source, destination.parent) as reading:
                if os.fstat(reading.fileno()).st_size > limit:
                    raise OSError(f"input is larger than the {limit}-byte limit")
                with open_regular(destination, os.O_WRONLY | os.O_CREAT) as writing:
                    # The launcher prints the drawer's brief path, so using that path as
                    # the next dispatch input is an ordinary workflow. Opening it again
                    # with O_TRUNC would empty the inode underneath the already-open read
                    # descriptor. Compare the opened objects, not their path spellings;
                    # aliases and hard links are the same case.
                    if os.path.samestat(os.fstat(reading.fileno()), os.fstat(writing.fileno())):
                        return 0
                    os.ftruncate(writing.fileno(), 0)
                    copy_bounded(reading, writing, limit)
            return 0
        if command == "bundle" and len(sys.argv) == 4:
            with open_regular(source, os.O_WRONLY | os.O_CREAT | os.O_TRUNC) as writing:
                subprocess.run(
                    ["git", "bundle", "create", "-", "--end-of-options", sys.argv[3]],
                    stdout=writing,
                    check=True,
                )
            return 0
        if command == "retain" and len(sys.argv) == 4:
            destination = Path(sys.argv[3])
            with open_regular(source, os.O_RDONLY) as retained:
                os.fsync(retained.fileno())
            os.replace(source, destination)
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return 0
        if command == "bundle-tip" and len(sys.argv) == 5:
            reference = sys.argv[3]
            expected = sys.argv[4]
            with tempfile.TemporaryDirectory(prefix="autoclave-bundle-check.") as repository:
                subprocess.run(["git", "-C", repository, "init", "--bare", "--quiet"], check=True)
                subprocess.run(
                    [
                        "git",
                        "-C",
                        repository,
                        "fetch",
                        "--quiet",
                        str(source),
                        f"{reference}:refs/autoclave/verified",
                    ],
                    check=True,
                )
                actual = subprocess.run(
                    ["git", "-C", repository, "rev-parse", "refs/autoclave/verified"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if actual != expected:
                    raise OSError("retained bundle resolved to the wrong commit")
            print("recoverable")
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
        "usage: safe_file.py read SLOT MAX_BYTES | write SOURCE SLOT MAX_BYTES | "
        "bundle SLOT REF | "
        "retain SOURCE DESTINATION | "
        "bundle-tip BUNDLE REF EXPECTED | "
        "worktree ROOT REF occupied|tree",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
