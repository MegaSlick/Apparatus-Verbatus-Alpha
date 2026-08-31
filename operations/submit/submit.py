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
storage-root check is enforced here before a single byte is hashed — and again at
the door, on the door's own admission loop, because "material lives only where the
policy names" has to be true of the door and not only of whatever ran before it.

**Cut 2026-08-09, per Tyrel's ruling that session.** A submission used to also
require a current data-gate approval-record artifact, verified here before a byte
was hashed and named in the sealed manifest's `authorized_by` field. His ruling:
none of this material ever reaches git regardless of any such sign-off, so the
requirement bought nothing and is gone; the manifest no longer carries an
authorization reference.

**Upload completion is explicit and sealed.** The manifest is built entirely in
memory, then written once, atomically. A crash at any point before that final
rename leaves no manifest at all; there is no intermediate state that could be
mistaken for a completed submission.

**Filenames are citation links, not a leak to discard.** The self-hashed manifest
always carries each submitted path and digest. A refusal that identifies a source
writes the source path and reason to a private, self-hashed refusal report under an
approved storage root. Terminal output gives the count and report location; it does
not print image bytes. This separates the immutable record from a captured terminal
without breaking traceability.

**There is no ordinary deletion command here.** Whole-run disposal is permitted
only when the run is dead/broken or complete/exported. This local tool has no sealed
authority for either condition, so `purge()` refuses rather than pretending a
manifest cleanup is a retention decision. `cleanup.py` remains the synthetic-drill
verifier; it makes observable claims only.

    python operations/submit/submit.py --source <folder> --manifest-out <path>
"""

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Final, Literal, NoReturn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common.contracts.canonical import (  # noqa: E402
    canonical_bytes,
    digest_bytes,
    self_hash,
    verify_self_hash,
)
from common.contracts.errors import ContractError  # noqa: E402
from operations.submit import gate, inventory  # noqa: E402

SCHEMA: Final = "submission-manifest.v1"
REFUSAL_REPORT_SCHEMA: Final = "submission-refusal-report.v0"

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

# Every field `log()` may carry. The immutable records carry filename linkage;
# terminal presentation carries only counts, digests, and report locations. Image
# bytes are never terminal output.
_LOG_FIELDS: Final = frozenset({"files", "bytes", "digest", "removed", "status"})
_LOG_EVENTS: Final = frozenset({"submission sealed", "submission refused"})
_LOG_STATUSES: Final = frozenset({"refusal-report-written"})


class SubmitRefusal(ContractError):
    """A folder could not be walked, or the gate refused it. Nothing was written."""


class ExistingRecordRefusal(SubmitRefusal):
    """An immutable target already holds different sealed evidence."""


class SubmissionRefusal(SubmitRefusal):
    """A refused local submission with its private report location and count."""

    def __init__(self, message: str, *, report_path: Path, refusal_count: int):
        super().__init__(message)
        self.report_path = Path(report_path)
        self.refusal_count = refusal_count


def log(event: str, **fields: Any) -> None:
    """One structured line. Refuses to print any field outside the allowed set."""
    unexpected = sorted(set(fields) - _LOG_FIELDS)
    if unexpected:
        raise SubmitRefusal(
            f"log() was asked to carry field(s) {unexpected}, outside its allowed set "
            f"{sorted(_LOG_FIELDS)}; filename linkage belongs in the sealed record, and image "
            "bytes may never reach terminal output"
        )
    if event not in _LOG_EVENTS:
        raise SubmitRefusal(
            "log event is outside the closed operational vocabulary; arbitrary event "
            "text could carry image bytes or an unaccounted presentation claim"
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


def build_manifest(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The sealed, self-hashed submission manifest.

    Not yet a run's `source_manifest`: ordinals and any PDF page fan-out are the
    door's decision, made when it actually opens these files — this manifest only
    ever names what arrived.

    **Cut 2026-08-09.** This used to also carry `authorized_by`, a digest-checked
    reference to the data-gate approval record that admitted the corpus. That
    requirement is gone (schema bumped to v1 accordingly); the manifest is purely
    the filename-to-digest ledger.
    """
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "files": sorted(entries, key=lambda entry: entry["relative_path"]),
    }
    manifest["self_hash"] = self_hash(manifest)
    return validate_manifest(manifest)


def validate_manifest(record: Any) -> dict[str, Any]:
    """Validate one self-hashed local filename ledger without logging or I/O.

    The submit door and a later ingress may both need this exact check.  It keeps
    the filename-to-digest ledger one closed shape: a non-empty, path-sorted set of
    submitted files.
    """
    if not isinstance(record, dict):
        raise SubmitRefusal("submission manifest is not an object")
    if set(record) != {"schema", "files", "self_hash"}:
        raise SubmitRefusal("submission manifest has an unexpected shape")
    if record["schema"] != SCHEMA:
        raise SubmitRefusal("submission manifest has an unsupported schema")
    if not verify_self_hash(record):
        raise SubmitRefusal("submission manifest fails its self-hash")
    files = record["files"]
    if not isinstance(files, list) or not files:
        raise SubmitRefusal("submission manifest names no submitted files")
    paths: list[str] = []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"relative_path", "sha256", "bytes"}:
            raise SubmitRefusal("submission manifest has an invalid file row")
        path, digest, size = entry["relative_path"], entry["sha256"], entry["bytes"]
        if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
            raise SubmitRefusal("submission manifest has an unsafe declared path")
        if not _is_sha256(digest):
            raise SubmitRefusal("submission manifest has a file row without a lowercase sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise SubmitRefusal(
                "submission manifest has a file row without a non-negative byte count"
            )
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise SubmitRefusal("submission manifest file rows are not sorted unique declared paths")
    return record


def load_manifest(path: Path) -> dict[str, Any]:
    """Load a canonical, self-hashed local filename ledger without terminal output."""
    try:
        data = Path(path).read_bytes()
        record = json.loads(data.decode("utf-8"))
        if canonical_bytes(record) != data:
            raise SubmitRefusal("submission manifest is not canonical JSON")
    except SubmitRefusal:
        raise
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as error:
        raise SubmitRefusal("submission manifest could not be read as canonical JSON") from error
    return validate_manifest(record)


def build_refusal_report(records: list[dict[str, str]]) -> dict[str, Any]:
    """One private, self-hashed record of refused source names and reason codes."""
    report = {
        "schema": REFUSAL_REPORT_SCHEMA,
        "refusals": sorted(records, key=lambda record: record["relative_path"]),
    }
    report["self_hash"] = self_hash(report)
    return report


def _write_refusal_report(path: Path, records: list[dict[str, str]]) -> Path:
    """Write private immutable refusal evidence without losing a later refusal.

    The ordinary location remains easy for an operator to find on its first use.
    If a distinct later refusal already occupies it, the new self-hashed record
    gets a content-addressed sibling instead of being overwritten or discarded.
    """
    report = build_refusal_report(records)
    data = canonical_bytes(report)
    try:
        atomic_create(path, data)
    except ExistingRecordRefusal:
        fallback = _content_addressed_report_path(path, report["self_hash"])
        atomic_create(fallback, data)
        return fallback
    return path


def _content_addressed_report_path(path: Path, report_hash: str) -> Path:
    """A sibling location whose name is bound to the self-hashed report bytes."""
    return path.with_name(f"{path.stem}.{report_hash}{path.suffix}")


def atomic_create(target: Path, data: bytes) -> bool:
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

    Public because `operations/operator/ingest_worker.py` depends on exactly this
    three-way behaviour — created, reused-identical, or `ExistingRecordRefusal` —
    and reads the False as its own refusal, since an entry appearing inside a
    folder it just checked was empty is a concurrent change rather than a retry.
    It reached that through the private name, which left this function looking
    free to change its return convention when it is not. Renaming it is the whole
    of that change; the behaviour is untouched.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    completed = False
    try:
        # `mkstemp`, not a pid-derived name. `open(temporary, "wb")` originally
        # followed a symlink, and O_EXCL|O_NOFOLLOW closed that — but the name was
        # still `.{manifest}.tmp-{pid}`, so it was both guessable *and* reusable: a
        # process that died between the create and the unlink, or a pid the system
        # later handed out again, left that exact name on disk. O_EXCL then refused
        # it, and every later submission to the same manifest path failed with the
        # generic "could not be written" — the one refusal that does not say a stale
        # temporary is the cause. `mkstemp` picks an unpredictable name per attempt,
        # creating it 0o600 with O_CREAT|O_EXCL, so it is at least as strict against
        # a planted link and immune to the stale-name wedge. Same helper, and the
        # same reasoning, as `common/runtree/store.py::_write_temporary`.
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{target.name}.tmp-", dir=target.parent
        )
        temporary = Path(raw_temporary)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            state = _existing_record_state(target, data)
            if state == "matches":
                completed = True
                return False
            if state == "unknown":
                raise ExistingRecordRefusal(
                    "something already exists at that path and could not be read as a regular "
                    "file, so it cannot be shown to seal these bytes. Evidence is never "
                    "overwritten (GOVERNANCE 4): it was not touched. This is not a report that "
                    "the submission changed — a symlink, a directory, or an unreadable entry "
                    "there is a different problem, and it needs looking at rather than a new "
                    "manifest path"
                ) from None
            raise ExistingRecordRefusal(
                "a sealed submission record already exists at that path and seals different "
                "content. Evidence is never overwritten (GOVERNANCE 4): the existing "
                "record was not touched, and a changed submission needs its own path"
            ) from None
        completed = True
        return True
    except OSError as error:
        # A name too long for the filesystem, a full disk, a temp path already taken.
        # Each escaped as a traceback and CPython's exit 1 past `main()`'s handler,
        # printing the manifest path on the way out.
        raise SubmitRefusal(
            "the submission manifest could not be written; nothing was sealed"
        ) from error
    finally:
        # Preserve a primary refusal, but do not call a completed create/reuse
        # successful while its temporary record remains on disk. Calling unlink
        # directly avoids a second, fallible stat call on an over-long temp path.
        # `temporary` stays None when `mkstemp` itself failed, which is the one
        # path where there is nothing on disk to remove.
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError as error:
            if completed:
                raise SubmitRefusal(
                    "the submission manifest was sealed or reused, but its temporary file could "
                    "not be removed; it must not be reported complete"
                ) from error


def _existing_record_state(path: Path, expected: bytes) -> Literal["matches", "differs", "unknown"]:
    """Compare one held regular-file descriptor without following a redirect.

    Three outcomes, not two. "Could not be compared" is a different fact from
    "seals different content", and only one of them is about the submission. A
    symlink caught by `O_NOFOLLOW`, a directory, an unreadable entry, or a
    platform with no `O_NOFOLLOW` at all all landed in the same `False` as a
    genuine content difference, and the caller then told the operator that the
    existing record sealed something else — sending whoever read it to hunt for
    a difference in content when what was actually sitting at that path was a
    planted link. Both still refuse and neither ever writes; they are told
    apart so the refusal names the problem the operator actually has.

    **POSIX only, and deliberately so.** Without `O_NOFOLLOW` there is no way to
    compare the entry at that path without risking following a redirect to
    somewhere else, so this reports "unknown" and the caller refuses. On such a
    platform a byte-identical resubmission stops being idempotent and is refused
    instead of reused. That is the correct trade and not a gap to close: the
    alternative is a comparison that can be pointed at another file, which is
    the exact attack `atomic_create` exists to refuse. This repository is POSIX
    only regardless — `operations/operator/custody.py` has an OS boundary for
    Linux and macOS alone and refuses to open the console anywhere else — so no
    supported platform reaches this branch.
    """

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        return "unknown"
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0))
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            return "unknown"
        # A successful stat of a regular file is a real comparison: a different
        # length is a content difference, not a failure to look.
        if details.st_size != len(expected):
            return "differs"
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return "matches" if handle.read(len(expected) + 1) == expected else "differs"
    except OSError:
        return "unknown"
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def submit(
    source: Path,
    manifest_out: Path,
    *,
    policy_path: Path = gate.DEFAULT_POLICY_PATH,
    refusal_report_out: Path | None = None,
) -> dict[str, Any]:
    """Walk `source` and seal a manifest at `manifest_out`.

    The storage-root check happens *before* a single file is read: a refused
    submission touches no bytes and writes nothing, so a refusal can never leave a
    partial trace that a later run might mistake for progress. The approved policy
    decides where real material and its manifest may live, and either location
    outside every approved root is refused before the folder is opened.
    """
    policy = gate.load_policy(policy_path)
    roots = gate.approved_storage_roots(policy)
    resolved_source = gate.require_approved_storage_location(source, roots, "submitted folder")
    resolved_manifest = gate.require_approved_storage_location(
        manifest_out, roots, "submission manifest"
    )
    report_target = (
        resolved_manifest.with_suffix(".refusals.json")
        if refusal_report_out is None
        else gate.require_approved_storage_location(
            refusal_report_out, roots, "private refusal report"
        )
    )
    if gate.same_or_inside(resolved_source, resolved_manifest):
        raise SubmitRefusal(
            "the submission manifest cannot be written inside the submitted folder; "
            "otherwise the next inventory includes its own prior output and cannot be idempotent"
        )
    if gate.same_or_inside(resolved_source, report_target):
        raise SubmitRefusal(
            "the private refusal report cannot be written inside the submitted folder; "
            "otherwise a retry inventories the tool-produced report as a submitted source"
        )

    # The *resolved* paths from here on, not the caller's original strings. Checking
    # one path and then opening another is the shape a check-then-use race lives in,
    # and the resolved values were already in hand.
    try:
        entries = walk_folder(resolved_source)
    except ContractError as error:
        entry = getattr(error, "entry", None)
        records = [] if entry is None else [inventory.refusal_record(entry, str(error))]
        try:
            written_report = _write_refusal_report(report_target, records)
        except SubmitRefusal as report_error:
            raise SubmitRefusal(
                "submission was refused and its private refusal report could not be written"
            ) from report_error
        raise SubmissionRefusal(
            f"submission refused: {len(records)} source refusal(s) recorded in private report",
            report_path=written_report,
            refusal_count=len(records),
        ) from error
    manifest = build_manifest(entries)
    data = canonical_bytes(manifest)
    atomic_create(resolved_manifest, data)
    log("submission sealed", files=len(entries), digest=digest_bytes(data))
    return manifest


def purge(manifest_out: Path, approved_roots: tuple[Path, ...]) -> NoReturn:
    """Refuse routine deletion: this tool has no sealed end-of-run authority.

    Synthetic cleanup drills use ``cleanup.verify_synthetic_cleanup`` against
    deliberately-created synthetic paths.  A real manifest/ledger remains until a
    run is dead/broken or complete/exported and whole-run disposal is performed by
    the owning lifecycle operation, not this local submit command.
    """
    del manifest_out, approved_roots
    raise SubmitRefusal(
        "purge is unavailable for submitted material: retain the whole run until its sealed "
        "dead/broken or complete/exported condition permits whole-volume disposal"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True)
    parser.add_argument("--manifest-out", required=True)
    parser.add_argument("--policy", default=str(gate.DEFAULT_POLICY_PATH))
    parser.add_argument(
        "--refusal-report-out",
        help="private approved-root location for a self-hashed refusal report",
    )
    args = parser.parse_args()

    try:
        submit(
            Path(args.source),
            Path(args.manifest_out),
            policy_path=Path(args.policy),
            refusal_report_out=(
                Path(args.refusal_report_out) if args.refusal_report_out is not None else None
            ),
        )
    except ContractError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        if isinstance(error, SubmissionRefusal):
            print(
                f"{error.refusal_count} source refusal(s); private refusal report: "
                f"{error.report_path}",
                file=sys.stderr,
            )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
