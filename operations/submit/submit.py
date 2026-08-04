"""The submit door: a local folder in, a checksummed and sealed manifest out.

**What this does, and what it deliberately does not.** It walks a folder through
`inventory.py`, computes a sha256 per file, and writes one sealed, self-hashed
manifest — nothing more. It does not decode, sniff, or judge a single byte of image
content: that is admission, and admission belongs to the pipeline door
(`pipeline/1_exemplar/door.py`) and its one format policy. And it does not transfer
anything to a pod: spec 04 owns "checksummed and resumable", and until a pod exists
there is nothing to transfer to.

**This is the first real boundary in the chain.** The pipeline door's fixture CLI
only ever sees declared synthetic pages; a folder handed to *this* tool is never a
fixture, by construction, because it never goes near `load_fixture`. So the
data-handling gate is enforced here before a single byte is hashed — and again at
the door, on the door's own admission loop, because "the door refuses real input
without a current approval" has to be true of the door and not only of whatever ran
before it.

**Upload completion is explicit and sealed.** The manifest is built entirely in
memory, then written once, atomically. A crash at any point before that final
rename leaves no manifest at all; there is no intermediate state that could be
mistaken for a completed submission.

**Logging never carries a name, a path, or a byte of content** — the data-handling
policy's logging rule made mechanical: `log()` refuses any field outside a small
allowed set of counts, digests, and status words.

**And neither does a refusal.** `log()` was airtight and it was not the only
output: `main()` printed every `ContractError` to stderr verbatim, and those
messages carried the submitted folder's path and the relative path of an offending
entry — so an ordinary rejected invocation emitted exactly the values the policy
excludes, into the channel a shell runner, a CI job or a service manager captures
by default. Three seats found it independently and it reproduced on an empty
folder, a symlink and a FIFO. The refusal *reason* is what an operator needs and
it is what they get; the name belongs in the accounted record, not in the log.
`inventory.SubmissionInputError.entry` is how a refusal still carries the name for
a caller with an approved place to write it — nothing here has one yet, which is a
question in the gate package rather than a decision made here.

**The distinction is submitted material, not every path.** A message naming
`config/data_handling_policy.json` or the approval record the *operator* passed on
the command line is naming their own configuration, not a declared filename out of
a submission, and redacting it would leave an operator unable to tell which of
their own files failed to load. The rule is about the material that arrived.

    python operations/submit/submit.py --source <folder> --manifest-out <path> \
        --approval-record <path>
"""

import argparse
import glob
import os
import sys
from pathlib import Path
from typing import Any, Final, NamedTuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from operations.submit import gate, inventory  # noqa: E402

SCHEMA: Final = "submission-manifest.v0"

# The manifest names every submitted file, whatever its size — a source too large
# for the door to admit is still a source that arrived, and it must stay in the
# denominator. This tool needs no file's *content* to write that, so it retains
# none: the digest is streamed and exact either way.
#
# This replaces a `MAX_RETAINED_BYTES = 64 MiB` that was a second, independent copy
# of `image_formats.MAX_SOURCE_BYTES` — the same number kept by hand in two places,
# which is the shape of drift this spec exists to kill, and which two seats flagged.
# `operations/submit/` may not import the pipeline (the dependency points one way),
# so the copy could not simply be shared; retaining nothing removes the need for it.
RETAIN_NO_BYTES: Final = 0

# Every field `log()` may carry. A count, a digest, or a status word — never a
# filename, a declared path, or image bytes, which is what the data-handling
# policy's logging rule actually requires.
_LOG_FIELDS: Final = frozenset({"files", "bytes", "digest", "removed", "status"})
_LOG_EVENTS: Final = frozenset({"submission sealed", "cleanup"})
_LOG_STATUSES: Final = frozenset({"target-absent", "target-present"})


class SubmitRefusal(ContractError):
    """A folder could not be walked, or the gate refused it. Nothing was written."""


class CleanupReport(NamedTuple):
    """What the filesystem shows *after* a cleanup pass — never a claim beyond it.

    `volume_listing` is `None`, not `()`, when there is no volume to check: an
    empty tuple would claim "checked, found nothing", and nothing here has checked
    anything until spec 04's pod volume exists. Unknown is never zero
    (GOVERNANCE 10).
    """

    target_removed: bool
    temp_files_removed: int
    remaining_temp_files: tuple[str, ...]
    volume_listing: tuple[str, ...] | None


def log(event: str, **fields: Any) -> None:
    """One structured line. Refuses to print any field outside the allowed set."""
    unexpected = sorted(set(fields) - _LOG_FIELDS)
    if unexpected:
        raise SubmitRefusal(
            f"log() was asked to carry field(s) {unexpected}, outside its allowed set "
            f"{sorted(_LOG_FIELDS)}; a log line may never carry a name, a path, or image bytes"
        )
    if event not in _LOG_EVENTS:
        raise SubmitRefusal(
            "log event is outside the closed operational vocabulary; arbitrary event "
            "text could carry a submitted name or path"
        )
    for field in ("files", "bytes", "removed"):
        if field in fields and (
            not isinstance(fields[field], int)
            or isinstance(fields[field], bool)
            or fields[field] < 0
        ):
            raise SubmitRefusal(f"log field {field!r} must be a non-negative count")
    if "digest" in fields:
        value = fields["digest"]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise SubmitRefusal("log field 'digest' must be a lowercase sha256")
    if "status" in fields and fields["status"] not in _LOG_STATUSES:
        raise SubmitRefusal("log field 'status' is outside the closed status vocabulary")
    rendered = " ".join(f"{key}={fields[key]}" for key in sorted(fields))
    print(f"{event}: {rendered}" if rendered else event)


def walk_folder(source: Path) -> list[dict[str, Any]]:
    """Every regular file under `source`, sorted, hashed. No format sniffing.

    Not one byte of content is retained. This tool writes a manifest of paths,
    digests and sizes, and never looks at what a file holds — so `max_bytes=0` is
    the honest request, and it also removes the second hand-kept copy of the door's
    64 MiB admission limit that used to live here under another name. The digest is
    streamed and exact whatever the file's size.
    """
    sources = inventory.read_submission(source, max_bytes=RETAIN_NO_BYTES)
    if not sources:
        raise SubmitRefusal(
            "the submitted folder contains no files to submit; an empty folder is a loud "
            "failure, never a green submission with nothing in it"
        )
    return [
        {"relative_path": found.relative_path, "sha256": found.sha256, "bytes": found.size}
        for found in sources
    ]


def build_manifest(
    entries: list[dict[str, Any]], *, authorized_by: dict[str, str]
) -> dict[str, Any]:
    """The sealed, self-hashed submission manifest.

    Not yet a run's `source_manifest`: ordinals and any PDF page fan-out are the
    door's decision, made when it actually opens these files — this manifest only
    ever names what arrived.

    `authorized_by` is the digest-checked reference to the approval record that let
    this corpus in, sealed into the manifest itself and self-hashed alongside
    everything else, so a run tree built from this manifest can always answer
    "which approval admitted this?" without trusting anything outside the manifest.
    """
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "files": sorted(entries, key=lambda entry: entry["relative_path"]),
        "authorized_by": authorized_by,
    }
    manifest["self_hash"] = self_hash(manifest)
    return manifest


def _atomic_create(target: Path, data: bytes) -> bool:
    """Create the manifest, or reuse an identical one. Never overwrite a different.

    GOVERNANCE 4: evidence is never overwritten. `os.replace` clobbered
    unconditionally, so resubmitting a *changed* folder to the same path replaced a
    valid, self-hashed record of what was previously sealed with a different one —
    no comparison, no warning, no refusal, and nothing on disk retaining the record
    it superseded. "Sealed" then meant only "self-consistent now".

    `os.link` is the same atomic-create-or-fail pattern `common/runtree/store.py`
    uses one layer down, where `RunTree.create` already refuses a changed manifest;
    this record sits upstream of any run tree and had no such protection. Identical
    bytes are a true no-op, so a byte-identical resubmission stays idempotent.
    Returns True when the file was created, False when an identical one was reused.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        # `open(temporary, "wb")` followed a symlink. The temp name is derived from
        # the manifest name and the pid, so it is guessable, and a link planted at it
        # would have taken the manifest bytes wherever it pointed — outside every
        # approved storage root, with `os.link` then failing and the write already
        # done. O_EXCL refuses a name that exists at all; O_NOFOLLOW refuses to
        # follow one that is a link.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if _read_or_none(target) == data:
                return False
            raise SubmitRefusal(
                "a submission manifest already exists at that path and seals different "
                "content. Evidence is never overwritten (GOVERNANCE 4): the existing "
                "record was not touched, and a changed submission needs its own path"
            ) from None
        return True
    except OSError as error:
        # A name too long for the filesystem, a full disk, a temp path already taken.
        # Each escaped as a traceback and CPython's exit 1 past `main()`'s handler,
        # printing the manifest path on the way out.
        raise SubmitRefusal(
            "the submission manifest could not be written; nothing was sealed"
        ) from error
    finally:
        # Nothing in here may raise. `is_symlink()` on an over-length name raises
        # ENAMETOOLONG rather than reporting False, and an exception from a `finally`
        # supersedes the one in flight — so the named `SubmitRefusal` above was
        # demoted to `__context__` and a raw OSError escaped `main()`'s handler as a
        # traceback with exit 1. A cleanup that can destroy its own error report is
        # worse than no cleanup.
        try:
            if temporary.is_symlink() or temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _read_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def submit(
    source: Path,
    manifest_out: Path,
    *,
    approval_record: Path | None,
    policy_path: Path = gate.DEFAULT_POLICY_PATH,
) -> dict[str, Any]:
    """Walk `source`, enforce the gate, and seal a manifest at `manifest_out`.

    The gate is checked *before* a single file is read: a refused submission
    touches no bytes and writes nothing, so a refusal can never leave a partial
    trace that a later run might mistake for progress. The storage-root check is
    part of that — the approved policy decides where real material and its manifest
    may live, and either location outside every approved root is refused before the
    folder is opened.
    """
    policy = gate.load_policy(policy_path)
    if approval_record is None:
        gate.enforce(approval=None, policy=policy)
    _approval, reference = gate.read_external_approval(Path(approval_record), policy)

    roots = gate.approved_storage_roots(policy)
    resolved_source = gate.require_approved_storage_location(source, roots, "submitted folder")
    resolved_manifest = gate.require_approved_storage_location(
        manifest_out, roots, "submission manifest"
    )
    if resolved_manifest.is_relative_to(resolved_source):
        raise SubmitRefusal(
            "the submission manifest cannot be written inside the submitted folder; "
            "otherwise the next inventory includes its own prior output and cannot be idempotent"
        )

    # The *resolved* paths from here on, not the caller's original strings. Checking
    # one path and then opening another is the shape a check-then-use race lives in,
    # and the resolved values were already in hand.
    entries = walk_folder(resolved_source)
    manifest = build_manifest(entries, authorized_by=reference.to_record())
    data = canonical_bytes(manifest)
    _atomic_create(resolved_manifest, data)
    log("submission sealed", files=len(entries), digest=digest_bytes(data))
    return manifest


def _entry_exists(path: Path) -> bool:
    """Whether the directory entry itself is there, dangling symlink included.

    `Path.exists()` follows the link, so a manifest that is a symlink to something
    already gone read as absent: `purge` skipped the unlink, reported
    `target_removed=True` and logged `status=target-absent` while the entry sat on
    disk — and `cleanup._is_absent`, the verifier for the same drill, uses `lstat`
    and calls that same state a failure. Two halves of one drill disagreeing about
    what "gone" means is worse than either answer.
    """
    return path.is_symlink() or path.exists()


def purge(manifest_out: Path, approved_roots: tuple[Path, ...]) -> CleanupReport:
    """The cleanup drill's removal half: remove the sealed manifest and any stray
    temp file beside it, then report what the filesystem actually shows afterward.

    Never a claim of forensic unrecoverability from storage media, snapshots, or
    provider backups — no filesystem check can establish that (GOVERNANCE 10). This
    only ever reports what a directory listing says after the removal, and
    `cleanup.verify_synthetic_cleanup` is what turns that report into a pass or a
    failure against declared, measurable bounds.
    """
    # `submit()` refuses to *write* outside the approved storage roots; deleting
    # outside them was never checked at all. Required rather than optional: a
    # removal path whose safety check is off by default fails open, which is the
    # wrong direction for the one operation that cannot be undone.
    #
    # **The containment check is on the parent directory, not the entry.** Checking
    # the entry would resolve it, and `require_approved_storage_location` refuses a
    # symlink outright — which would refuse to clean up the one case this function
    # was repaired for, a dangling manifest symlink that `cleanup._is_absent` calls
    # a failure. Unlinking a name inside an approved directory removes that name and
    # nothing else; it does not follow the link, and it cannot reach the link's
    # victim. So the question is where the *entry lives*, and that is what is asked.
    directory = gate.require_approved_storage_location(
        manifest_out.parent, approved_roots, "cleanup target directory"
    )
    manifest_out = directory / manifest_out.name

    # Escaped, because a manifest named `batch[1].json` turns `.batch[1].json.tmp-*`
    # into a character class: the glob then matches temp files belonging to some
    # other manifest and misses its own, so `purge` would delete the wrong files and
    # report a drill it did not perform.
    temporary = f".{glob.escape(manifest_out.name)}.tmp-*"
    removed_temp = 0
    if manifest_out.parent.exists():
        for candidate in manifest_out.parent.glob(temporary):
            candidate.unlink()
            removed_temp += 1
    if _entry_exists(manifest_out):
        manifest_out.unlink()

    remaining = (
        tuple(sorted(str(path) for path in manifest_out.parent.glob(temporary)))
        if manifest_out.parent.exists()
        else ()
    )
    report = CleanupReport(
        target_removed=not _entry_exists(manifest_out),
        temp_files_removed=removed_temp,
        remaining_temp_files=remaining,
        # No pod volume exists yet (spec 04); reporting an empty listing here would
        # claim a check that never happened.
        volume_listing=None,
    )
    log(
        "cleanup",
        removed=removed_temp,
        status="target-absent" if report.target_removed else "target-present",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True)
    parser.add_argument("--manifest-out", required=True)
    parser.add_argument(
        "--approval-record",
        help="path to Tyrel's sealed data-gate approval record for the current policy",
    )
    parser.add_argument("--policy", default=str(gate.DEFAULT_POLICY_PATH))
    args = parser.parse_args()

    try:
        submit(
            Path(args.source),
            Path(args.manifest_out),
            approval_record=Path(args.approval_record) if args.approval_record else None,
            policy_path=Path(args.policy),
        )
    except ContractError as error:
        # The reason, never the name. Every refusal that knows a submitted path
        # carries it as `entry` rather than in its message, because this line is
        # what a shell runner, a CI job or a service manager captures — and the
        # data-handling policy's logging rule excludes a declared filename or path
        # from exactly that. **What is missing is the operator's ability to see
        # which entry was rejected**, and there is no approved place to put it yet;
        # that is an open question in the gate package, not a decision made here.
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        if getattr(error, "entry", None) is not None:
            print(
                "the offending entry's submitted path is withheld from this channel by "
                "the data-handling policy's logging rule",
                file=sys.stderr,
            )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
