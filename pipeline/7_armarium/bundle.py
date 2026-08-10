"""Take the sealed export bundle out of the run tree and put it where it was asked for.

`run.py` builds the product, verifies it, and seals it as a content-addressed blob
referenced by the `export` artifact. That is the right home for it -- the run's own
record then says an export happened and names its digest -- but a blob inside a run
tree has not left the pipeline, and GOALS 4 is that everything read leaves it. This
program is the last step: read the sealed blob, verify it again from the outside, and
publish it to an operator-chosen destination.

**It is a reader, not a second writer.** It builds nothing, projects nothing and
touches no text. Every byte it publishes came out of the run tree and is checked
against the digest the `export` artifact recorded, so there is no path here by which a
second version of an established reading could reach a deliverable.

**Publish is all-or-nothing, and an existing destination is refused rather than
merged into.** The pattern -- refuse the target, build under a hidden sibling name,
rename into place, remove the sibling on any failure -- was read at the window
(`local/export_clean_workspaces.py::_atomic_target`) and is reasoned rather than
carried: the failure it prevents is a destination holding half of one publication and
half of another, which is exactly the state nobody can later tell apart from a
complete one. `os.replace` is used instead of the window's `rename` because it is the
same atomic operation with the semantics stated in the name.

    python pipeline/7_armarium/bundle.py --run-root <dir> --run-id <id> --out <dest>

The destination gets `armarium-export.zip`, byte-identical to the sealed blob, and
`bundle/`, the extraction that verification produced -- so the product can be read
without a zip tool and the two can be compared.
"""

import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from armarium_export import ARMARIUM_ARCHIVE_NAME, verify_export_bundle  # noqa: E402

from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import artifact_id  # noqa: E402
from common.contracts.stages import ARMARIUM  # noqa: E402
from common.stage import EXIT_COMPLETE, open_context, run_stage, stage_parser  # noqa: E402

ARCHIVE_NAME = ARMARIUM_ARCHIVE_NAME
EXTRACTION_NAME = "bundle"


def sealed_bundle(context) -> tuple[bytes, dict]:
    """The exact bytes the `export` artifact references, checked against its digest."""
    # Absence is asked about separately, before reading. "There is no export here"
    # and "the export does not verify" are different facts about the run, and a
    # `try` around the read would report the first for the second -- hiding a
    # tampered blob behind the more reassuring of the two sentences.
    address = artifact_id(ARMARIUM, "export", "export", None)
    relative = context.tree.artifact_path(ARMARIUM, "export", address)
    if not context.tree.resolve(relative).is_file():
        raise ContractError(
            "this run has no sealed armarium/export artifact; the bundle is published "
            "from a job the Armarium has already run over, and a job with none has "
            "nothing to publish"
        )
    record = context.tree.read_artifact(ARMARIUM, "export", address)
    reference = record["payload"].get("bundle", {}).get("reference")
    declared = record["payload"].get("bundle", {}).get("sha256")
    if not isinstance(reference, dict) or not isinstance(declared, str):
        raise ContractError("the export artifact names no sealed product bundle")
    relative_path = reference.get("relative_path")
    if not isinstance(relative_path, str):
        raise ContractError("the export artifact names no sealed product bundle")
    data = context.tree.read_bytes(relative_path)
    if digest_bytes(data) != declared or reference.get("sha256") != declared:
        raise ContractError(
            "the sealed product bundle no longer matches the digest its export "
            "artifact recorded; nothing may be published over changed bytes"
        )
    return data, record["payload"]


def publish(context, out_dir: Path) -> dict:
    """Verify the sealed bundle and put it at `out_dir`, atomically or not at all.

    The destination is *reserved* with `mkdir` before any of the read, verify and
    write work below runs, rather than merely checked and trusted. A plain
    existence check followed by that work leaves a real window in which a second
    publish -- or anything else -- can create the destination in between; `mkdir`
    is atomic at the filesystem level, so there is no such window, and it refuses
    a symlink (broken or not) at this path exactly as the check it replaces did.
    """
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_dir.mkdir()
    except OSError as error:
        raise ContractError(
            f"{out_dir} already exists; an export destination is never reused or merged "
            "into, so a half-written publication can never be mistaken for a whole one"
        ) from error
    try:
        data, payload = sealed_bundle(context)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{out_dir.name}.publishing-", dir=str(out_dir.parent))
        )
        try:
            # Verified into the staging directory, from the bytes alone, exactly as a
            # recipient with no run tree would: if it does not survive that, it is not a
            # product to publish and the destination stays absent.
            manifest = verify_export_bundle(data, staging / EXTRACTION_NAME)
            (staging / ARCHIVE_NAME).write_bytes(data)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        # `out_dir` was reserved empty above, and `replace` atomically swaps an
        # empty directory for `staging` -- the same atomic operation the module
        # docstring already names, now also the one thing that ever populates the
        # reservation.
        os.replace(staging, out_dir)
    except BaseException:
        # Every failure above re-raises before `os.replace` runs, so `out_dir` is
        # still exactly the empty reservation created at the top of this
        # function. Remove it so a failed publish leaves no destination at all,
        # not an empty one -- the guarantee this function has always documented.
        with contextlib.suppress(OSError):
            out_dir.rmdir()
        raise
    return {
        "archive": ARCHIVE_NAME,
        "extraction": EXTRACTION_NAME,
        "sha256": digest_bytes(data),
        "status": manifest["claims"]["status"],
        "unit_count": manifest["claims"]["terminal_ledger"]["unit_count"],
        "unresolved": len(manifest["claims"]["partial_reasons"]),
        "aggregate_status": payload["aggregate"]["status"],
    }


def main() -> int:
    parser = stage_parser(__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="the destination for the export bundle")
    args = parser.parse_args()
    context = open_context(args, ARMARIUM)
    summary = publish(context, Path(args.out))
    print(
        f"export bundle published to {args.out}: {summary['status']}, "
        f"{summary['unit_count']} accounted units, {summary['unresolved']} unresolved"
    )
    for key in ("archive", "extraction", "sha256", "aggregate_status"):
        print(f"  {key}: {summary[key]}")
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
