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

    python operations/submit/submit.py --source <folder> --manifest-out <path> \
        --approval-record <path>
"""

import argparse
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
# denominator. The inventory keeps bytes only up to this bound; the digest is exact
# either way.
MAX_RETAINED_BYTES: Final = 64 * 1024 * 1024

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
    """Every regular file under `source`, sorted, hashed. No format sniffing."""
    sources = inventory.read_submission(source, max_bytes=MAX_RETAINED_BYTES)
    if not sources:
        raise SubmitRefusal(
            f"{source} contains no files to submit; an empty folder is a loud failure, "
            "never a green submission with nothing in it"
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


def _atomic_write(target: Path, data: bytes) -> None:
    """Temp file in the same directory, flushed, then replaced — never a partial
    file at the target path, mirroring `common/runtree/store.py`'s own writer."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


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

    entries = walk_folder(source)
    manifest = build_manifest(entries, authorized_by=reference.to_record())
    data = canonical_bytes(manifest)
    _atomic_write(manifest_out, data)
    log("submission sealed", files=len(entries), digest=digest_bytes(data))
    return manifest


def purge(manifest_out: Path) -> CleanupReport:
    """The cleanup drill's removal half: remove the sealed manifest and any stray
    temp file beside it, then report what the filesystem actually shows afterward.

    Never a claim of forensic unrecoverability from storage media, snapshots, or
    provider backups — no filesystem check can establish that (GOVERNANCE 10). This
    only ever reports what a directory listing says after the removal, and
    `cleanup.verify_synthetic_cleanup` is what turns that report into a pass or a
    failure against declared, measurable bounds.
    """
    removed_temp = 0
    if manifest_out.parent.exists():
        for candidate in manifest_out.parent.glob(f".{manifest_out.name}.tmp-*"):
            candidate.unlink()
            removed_temp += 1
    if manifest_out.exists():
        manifest_out.unlink()

    remaining = (
        tuple(sorted(str(path) for path in manifest_out.parent.glob(f".{manifest_out.name}.tmp-*")))
        if manifest_out.parent.exists()
        else ()
    )
    report = CleanupReport(
        target_removed=not manifest_out.exists(),
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
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
