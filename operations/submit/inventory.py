"""Reading a submitted folder without following anything out of it.

One reader, used by both: `submit.py` inventories a folder to seal a manifest, and
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

**Proving it once is not enough, because the walk ends.** `read_submission`
establishes that no component of a submitted path was a link *at the moment it
enumerated the folder*, then closes every descriptor. The bytes that become the
sealed Exemplar are read later, by the door, and a plain second open by path would
follow a link planted in between — undoing the proof at exactly the point it
matters. `open_submission_source` is the same anchored walk offered as a reopen,
and it hands back the live descriptor rather than a path, so the digest, the format
sniff and PDFium's own reads are all of one file. `assert_unchanged` closes the
other half: anchoring proves *which* file, and the before/after `fstat` proves the
file did not move under the reader while it was being read.

**A source that cannot be read is a failure of the whole inventory, not a gap in
it.** A per-file refusal is the *door's* job and needs bytes to refuse; an
inventory that quietly omitted a file it could not open would shrink the
denominator the Armarium's census later reconciles against, which is exactly the
silent loss GOVERNANCE 2 forbids.

**A refusal here says what happened and not what it was called.** The messages used
to interpolate the offending entry's submitted relative path, and `submit.py`'s CLI
prints every one of them to stderr — so a rejected submission emitted a declared
path into the channel a runner captures, which the data-handling policy's logging
rule excludes. The name still exists: it rides on the exception as `entry`, for the
submit door's approved private refusal report.

**The walk is bounded in four directions, not one.** `max_bytes` bounded a single
file's retained bytes and nothing else: a submission's file count, its aggregate
retained bytes, its directory depth and its entries per directory were all
attacker-shaped. Depth was the sharpest — 2,000 nested directories escaped as a
`RecursionError` and CPython's exit 1 rather than a named refusal, past the
`ContractError` handler entirely.
"""

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Final, Iterator, NamedTuple

from common.contracts.errors import ContractError

# Read in chunks so an oversized source is hashed without ever being held whole.
# Its refusal still needs an exact digest — the run's source manifest names every
# submitted file, refused ones included — so it is streamed to the hash rather than
# read wholesale only to discover it was too big.
_CHUNK: Final = 1024 * 1024

# What a submission may be, in aggregate. One scanned register volume is hundreds of
# pages in a shallow tree; these are far above anything real and far below what
# exhausts a machine. A bound nobody can reach is still the difference between a
# named refusal and an out-of-memory kill nobody can read afterwards.
#
# `MAX_SUBMITTED_BYTES` counts *retained* bytes, so it is inert for `submit.py`,
# which asks for `max_bytes=0` and holds no content at all, and live for the
# pipeline door, which asks for 64 MiB per file and is the caller that actually
# accumulates a corpus in memory. That asymmetry is the point: the bound sits where
# the memory does. The other three bind both callers.
MAX_SUBMITTED_FILES: Final = 100_000
MAX_SUBMITTED_BYTES: Final = 8 * 1024 * 1024 * 1024
MAX_DIRECTORY_DEPTH: Final = 64
MAX_DIRECTORY_ENTRIES: Final = 100_000


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


class OpenedSubmissionSource:
    """One submitted file held through an anchored, no-follow descriptor.

    The caller uses ``handle`` for every read of the source, then calls
    :meth:`assert_unchanged` before it treats derived bytes as evidence.  Keeping
    the descriptor open is essential for container readers such as PDFium: a
    pathname may be replaced between a pre-read digest and a later decoder open.
    """

    __slots__ = ("handle", "_descriptor", "_entry", "_before")

    def __init__(
        self,
        handle: BinaryIO,
        descriptor: int,
        entry: str,
        before: tuple[int, int, int, int, int],
    ) -> None:
        self.handle = handle
        self._descriptor = descriptor
        self._entry = entry
        self._before = before

    def assert_unchanged(self) -> None:
        """Refuse a source modified in place while this descriptor was in use."""
        try:
            after = _open_stream_metadata(os.fstat(self._descriptor))
        except OSError as error:
            raise SubmissionInputError(
                "a submitted source could not be rechecked after reading; the door will not "
                "bind evidence to a file it can no longer identify",
                entry=self._entry,
            ) from error
        if after != self._before:
            raise SubmissionInputError(
                "a submitted source changed while it was being read; the door refuses to "
                "bind evidence to moving bytes",
                entry=self._entry,
            )


class RawPathReference(NamedTuple):
    """A non-UTF-8 submitted name, retained without putting surrogates in JSON."""

    display: str
    raw_bytes_hex: str

    @classmethod
    def from_relative_path(cls, relative_path: str) -> "RawPathReference":
        raw = relative_path.encode("utf-8", "surrogateescape")
        return cls(display=raw.decode("utf-8", "backslashreplace"), raw_bytes_hex=raw.hex())

    def to_refusal_record(self, reason: str) -> dict[str, str]:
        return {
            "relative_path": self.display,
            "relative_path_encoding": "raw-bytes-hex",
            "relative_path_bytes_hex": self.raw_bytes_hex,
            "reason": reason,
        }


class SubmissionInputError(ContractError):
    """A folder could not be inventoried without lying about what is in it.

    `str(...)` never carries a submitted name or path, because `submit.py`'s CLI
    prints it to stderr and the data-handling policy's logging rule excludes exactly
    those values from operational output. `entry` holds the submitted relative path
    when one is what went wrong, for a caller with an approved place to record it.
    """

    def __init__(self, message: str, *, entry: str | RawPathReference | None = None):
        super().__init__(message)
        self.entry = entry


def refusal_record(entry: str | RawPathReference, reason: str) -> dict[str, str]:
    """Project an inventory error into the private, canonical refusal report."""
    if isinstance(entry, RawPathReference):
        return entry.to_refusal_record(reason)
    return {"relative_path": entry, "reason": reason}


class _Budget:
    """What one whole submission may consume, checked as the walk proceeds."""

    __slots__ = ("files", "retained")

    def __init__(self) -> None:
        self.files = 0
        self.retained = 0

    def admit(self, size: int, retained: int, *, entry: str) -> None:
        self.files += 1
        if self.files > MAX_SUBMITTED_FILES:
            raise SubmissionInputError(
                f"the submission holds more than {MAX_SUBMITTED_FILES} files; the door "
                "refuses a submission it cannot bound rather than reading until it stops",
                entry=entry,
            )
        self.retained += retained
        if self.retained > MAX_SUBMITTED_BYTES:
            raise SubmissionInputError(
                f"the submission's retained bytes exceed the {MAX_SUBMITTED_BYTES}-byte "
                "aggregate limit; the per-file limit bounds one source, not a corpus",
                entry=entry,
            )


def read_submission(folder: Path, *, max_bytes: int) -> list[SubmittedSource]:
    """Every regular file under `folder`, sorted by path, with its exact bytes."""
    descriptor = _open_submission_root(folder)
    try:
        sources = _walk(descriptor, "", max_bytes, _Budget(), depth=0)
    finally:
        os.close(descriptor)
    return sorted(sources, key=lambda source: source.relative_path)


@contextmanager
def open_submission_source(folder: Path, relative_path: str) -> Iterator[OpenedSubmissionSource]:
    """Yield one source through the same anchored walk used by the inventory.

    ``read_submission`` deliberately closes every descriptor after producing the
    filename ledger.  A later consumer must not rebuild an ordinary ``Path`` from
    that ledger and open it outside the no-follow boundary: an atomic replacement
    in the interval could make a digest describe one PDF while a decoder seals
    another.  This context manager recreates the descriptor-relative walk and
    keeps the final regular-file descriptor alive for the consumer's whole use.
    """
    components = _source_components(relative_path)
    root = _open_submission_root(folder)
    directories = [root]
    descriptor: int | None = None
    handle: BinaryIO | None = None
    try:
        parent = root
        for component in components[:-1]:
            child = _open_directory(component, parent, entry=relative_path)
            directories.append(child)
            parent = child
        descriptor = _open_regular_file(components[-1], parent, entry=relative_path)
        try:
            before = _open_stream_metadata(os.fstat(descriptor))
            # Keep descriptor ownership here.  PDFium retains a Python stream for
            # the document's lifetime, so closing this handle early would turn the
            # secure stream into an unreliable source halfway through rendering.
            handle = os.fdopen(descriptor, "rb", closefd=False)
        except OSError as error:
            raise SubmissionInputError(
                "a submitted source could not be prepared for a stable read",
                entry=relative_path,
            ) from error
        try:
            yield OpenedSubmissionSource(handle, descriptor, relative_path, before)
        finally:
            handle.close()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for directory in reversed(directories):
            os.close(directory)


def _open_submission_root(folder: Path) -> int:
    """Validate and open a submission root for an anchored descendant walk."""
    root = Path(folder)
    if root.is_symlink():
        raise SubmissionInputError(
            "the submitted folder is a symlink; a submission cannot be entered by redirect"
        )
    if not root.is_dir():
        raise SubmissionInputError("the submitted folder is not a directory")
    return _open_directory(root)


def _source_components(relative_path: str) -> tuple[str, ...]:
    """Accept only the canonical slash-relative names an inventory can emit."""
    if not isinstance(relative_path, str) or not relative_path:
        raise SubmissionInputError(
            "a requested submitted source has no plain relative filename",
            entry=relative_path if isinstance(relative_path, str) else None,
        )
    components = tuple(relative_path.split("/"))
    if any(component in {"", ".", ".."} for component in components):
        raise SubmissionInputError(
            "a requested submitted source is not a plain relative filename",
            entry=relative_path,
        )
    return components


def _stable_file_metadata(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """The metadata whose change proves an inventory digest had no stable name."""
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _open_stream_metadata(details: os.stat_result) -> tuple[int, int, int, int, int]:
    """Detect byte-affecting changes without mistaking an unlink for a rewrite.

    An atomic replacement removes this descriptor's name and may update only its
    ctime.  Its already-open inode still holds the exact bytes the door hashed, so
    rejecting it would recreate the pathname race this stream exists to avoid.
    The full inventory check above retains ctime because it is binding a name at
    submission time; the later descriptor-held reader deliberately tracks fields
    that describe its own bytes and mode instead.
    """
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
    )


def _open_directory(
    path: Path | str,
    parent_descriptor: int | None = None,
    *,
    entry: str | None = None,
) -> int:
    """Open a directory without following its final component."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise SubmissionInputError(
            "this platform cannot open a directory without following links; the submit "
            "door refuses rather than reading a folder it cannot bound",
            entry=entry,
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
            "a submitted directory could not be opened without following a redirect", entry=entry
        ) from error
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise SubmissionInputError(
                "a submitted path is not a directory when opened", entry=entry
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_regular_file(name: str, parent_descriptor: int, *, entry: str) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise SubmissionInputError(
            "this platform cannot open a file without following links; the submit door "
            "refuses rather than reading bytes from somewhere else",
            entry=entry,
        )
    flags = os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise SubmissionInputError(
            "a submitted source could not be opened without following a redirect", entry=entry
        ) from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SubmissionInputError(
                "a submitted source is not a regular file when opened", entry=entry
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _walk(
    directory_descriptor: int, prefix: str, max_bytes: int, budget: _Budget, *, depth: int
) -> list[SubmittedSource]:
    if depth > MAX_DIRECTORY_DEPTH:
        # Checked before descending, so a pathological tree is a named refusal
        # rather than a RecursionError escaping past the ContractError handler.
        raise SubmissionInputError(
            f"the submission nests deeper than {MAX_DIRECTORY_DEPTH} directories; a tree "
            "this deep is refused by name rather than by running out of stack",
            entry=prefix or None,
        )
    sources: list[SubmittedSource] = []
    try:
        names = sorted(os.listdir(directory_descriptor))
    except OSError as error:
        raise SubmissionInputError(
            "a submitted directory could not be listed", entry=prefix or None
        ) from error
    if len(names) > MAX_DIRECTORY_ENTRIES:
        raise SubmissionInputError(
            f"a submitted directory holds more than {MAX_DIRECTORY_ENTRIES} entries",
            entry=prefix or None,
        )
    for name in names:
        relative_path = f"{prefix}/{name}" if prefix else name
        try:
            # `os.listdir` surrogate-escapes bytes that are not valid UTF-8, and a
            # surrogate cannot be encoded again — so a submitted name in some other
            # encoding reached `canonical_bytes` and escaped as a UnicodeEncodeError
            # traceback past the ContractError handler, exit 1, with the path
            # printed on the way out. Refused by name instead, and the name itself
            # is not repeated into the message.
            relative_path.encode("utf-8")
        except UnicodeEncodeError as error:
            raise SubmissionInputError(
                "a submitted name is not valid UTF-8; the manifest is canonical JSON "
                "and cannot record a name it cannot encode",
                entry=RawPathReference.from_relative_path(relative_path),
            ) from error
        try:
            details = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except OSError as error:
            raise SubmissionInputError(
                "a submitted entry could not be inspected without following a redirect",
                entry=relative_path,
            ) from error
        if stat.S_ISLNK(details.st_mode):
            raise SubmissionInputError(
                "the submission contains a symlink; only plain files and directories "
                "may be submitted",
                entry=relative_path,
            )
        if stat.S_ISDIR(details.st_mode):
            child = _open_directory(name, directory_descriptor, entry=relative_path)
            try:
                sources.extend(_walk(child, relative_path, max_bytes, budget, depth=depth + 1))
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(details.st_mode):
            raise SubmissionInputError(
                "the submission contains a non-regular entry; the door cannot bind bytes "
                "it cannot read as a file",
                entry=relative_path,
            )
        descriptor = _open_regular_file(name, directory_descriptor, entry=relative_path)
        try:
            before = _stable_file_metadata(os.fstat(descriptor))
            data, digest, size = _read_once(descriptor, max_bytes)
            after = _stable_file_metadata(os.fstat(descriptor))
            if after != before:
                raise SubmissionInputError(
                    "a submitted source changed while it was being read; the door refuses "
                    "to seal a digest over moving bytes",
                    entry=relative_path,
                )
        except OSError as error:
            raise SubmissionInputError(
                "a submitted source could not be read for its digest; the door will not "
                "invent a source record without bytes",
                entry=relative_path,
            ) from error
        finally:
            os.close(descriptor)
        budget.admit(size, len(data) if data is not None else 0, entry=relative_path)
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
