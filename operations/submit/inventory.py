"""Reading a submitted folder without following anything out of it.

One reader, used twice: `submit.py` inventories a folder to seal a manifest, and
`pipeline/1_exemplar/door.py` reads the same folder to admit its bytes. A second
walker would be a second set of rules about what a submission *is*, and the drift
between two such sets is the defect this whole spec exists to kill.

**Every open is anchored to a directory descriptor and refuses to follow a link.**
A submitted folder is untrusted local material: a symlink inside it points at
something the submitter did not submit, and reading that thing would put bytes into
a sealed corpus that nobody chose to give us. `O_NOFOLLOW | O_DIRECTORY`, `dir_fd`
relative opens, and an `lstat` before every decision are what make "this file is
under this folder" true at the moment of reading rather than at the moment of
listing.

**A source that cannot be read is a failure of the whole inventory, not a gap in
it.** A per-file refusal is the *door's* job and needs bytes to refuse; an
inventory that quietly omitted a file it could not open would shrink the
denominator the Armarium's census later reconciles against, which is exactly the
silent loss GOVERNANCE 2 forbids.
"""

import hashlib
import os
import stat
from pathlib import Path
from typing import Final, NamedTuple

from common.contracts.errors import ContractError

# Read in chunks so an oversized source is hashed without ever being held whole.
# Its refusal still needs an exact digest — the run's source manifest names every
# submitted file, refused ones included — so it is streamed to the hash rather than
# read wholesale only to discover it was too big.
_CHUNK: Final = 1024 * 1024


class SubmittedSource(NamedTuple):
    """One regular file found under a submitted folder.

    `data` is `None` exactly when the file is larger than `max_bytes`: the digest
    is still exact, so the source stays in the run's denominator and is refused by
    name rather than vanishing.
    """

    relative_path: str
    sha256: str
    size: int
    data: bytes | None


class SubmissionInputError(ContractError):
    """A folder could not be inventoried without lying about what is in it."""


def read_submission(folder: Path, *, max_bytes: int) -> list[SubmittedSource]:
    """Every regular file under `folder`, sorted by path, with its exact bytes."""
    root = Path(folder)
    if root.is_symlink():
        raise SubmissionInputError(
            "the submitted folder is a symlink; a submission cannot be entered by redirect"
        )
    if not root.is_dir():
        raise SubmissionInputError(f"{root} is not a directory")
    descriptor = _open_directory(root)
    try:
        sources = _walk(descriptor, "", max_bytes)
    finally:
        os.close(descriptor)
    return sorted(sources, key=lambda source: source.relative_path)


def _open_directory(path: Path | str, parent_descriptor: int | None = None) -> int:
    """Open a directory without following its final component."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise SubmissionInputError(
            "this platform cannot open a directory without following links; the submit "
            "door refuses rather than reading a folder it cannot bound"
        )
    flags = os.O_RDONLY | no_follow | directory | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = (
            os.open(path, flags)
            if parent_descriptor is None
            else os.open(path, flags, dir_fd=parent_descriptor)
        )
    except OSError as error:
        raise SubmissionInputError(
            "a submitted directory could not be opened without following a redirect"
        ) from error
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise SubmissionInputError("a submitted path is not a directory when opened")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_regular_file(name: str, parent_descriptor: int) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise SubmissionInputError(
            "this platform cannot open a file without following links; the submit door "
            "refuses rather than reading bytes from somewhere else"
        )
    flags = os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise SubmissionInputError(
            "a submitted source could not be opened without following a redirect"
        ) from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SubmissionInputError("a submitted source is not a regular file when opened")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _walk(directory_descriptor: int, prefix: str, max_bytes: int) -> list[SubmittedSource]:
    sources: list[SubmittedSource] = []
    try:
        names = sorted(os.listdir(directory_descriptor))
    except OSError as error:
        raise SubmissionInputError("a submitted directory could not be listed") from error
    for name in names:
        relative_path = f"{prefix}/{name}" if prefix else name
        try:
            details = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except OSError as error:
            raise SubmissionInputError(
                "a submitted entry could not be inspected without following a redirect"
            ) from error
        if stat.S_ISLNK(details.st_mode):
            raise SubmissionInputError(
                f"the submission contains a symlink ({relative_path}); only plain files "
                "and directories may be submitted"
            )
        if stat.S_ISDIR(details.st_mode):
            child = _open_directory(name, directory_descriptor)
            try:
                sources.extend(_walk(child, relative_path, max_bytes))
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(details.st_mode):
            raise SubmissionInputError(
                f"the submission contains a non-regular entry ({relative_path}); the door "
                "cannot bind bytes it cannot read as a file"
            )
        descriptor = _open_regular_file(name, directory_descriptor)
        try:
            data, digest, size = _read_once(descriptor, max_bytes)
        except OSError as error:
            raise SubmissionInputError(
                "a submitted source could not be read for its digest; the door will not "
                "invent a source record without bytes"
            ) from error
        finally:
            os.close(descriptor)
        sources.append(SubmittedSource(relative_path, digest, size, data))
    return sources


def _read_once(descriptor: int, max_bytes: int) -> tuple[bytes | None, str, int]:
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    size = 0
    while chunk := os.read(descriptor, _CHUNK):
        digest.update(chunk)
        size += len(chunk)
        if size <= max_bytes:
            chunks.append(chunk)
    return (b"".join(chunks) if size <= max_bytes else None, digest.hexdigest(), size)
