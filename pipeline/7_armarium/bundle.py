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

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from armarium_export import ARMARIUM_ARCHIVE_NAME, verify_delivered_bundle  # noqa: E402

from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import artifact_id  # noqa: E402
from common.contracts.stages import ARMARIUM  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import EXIT_COMPLETE, run_stage, stage_parser  # noqa: E402

EXTRACTION_NAME = "bundle"


def sealed_bundle(tree: RunTree) -> tuple[bytes, dict]:
    """The exact bytes the `export` artifact references, checked against its digest."""
    # Absence is asked about separately, before reading: a `try` around the read would
    # report "there is no export here" for "the export does not verify", hiding a
    # tampered blob behind the more reassuring of the two sentences.
    address = artifact_id(ARMARIUM, "export", "export", None)
    relative = tree.artifact_path(ARMARIUM, "export", address)
    if not tree.resolve(relative).is_file():
        raise ContractError(
            "this run has no sealed armarium/export artifact; the bundle is published "
            "from a job the Armarium has already run over, and a job with none has "
            "nothing to publish"
        )
    record = tree.read_artifact(ARMARIUM, "export", address)
    reference = record["payload"].get("bundle", {}).get("reference")
    declared = record["payload"].get("bundle", {}).get("sha256")
    if not isinstance(reference, dict) or not isinstance(declared, str):
        raise ContractError("the export artifact names no sealed product bundle")
    if record.get("inputs") != [reference]:
        raise ContractError(
            "the sealed product bundle reference is not the export artifact's sole "
            "digest-checked input; publication refuses an unverified link in the product chain"
        )
    relative_path = reference.get("relative_path")
    if not isinstance(relative_path, str):
        raise ContractError("the export artifact names no sealed product bundle")
    if relative_path != tree.blob_path(ARMARIUM, declared):
        raise ContractError(
            "the sealed product bundle reference does not occupy the Armarium's "
            "content-addressed blob path; publication refuses a package substituted by path"
        )
    try:
        data = tree.read_bytes(relative_path)
    except OSError as error:
        raise ContractError(
            f"the sealed product bundle at {relative_path} could not be read"
        ) from error
    if digest_bytes(data) != declared or reference.get("sha256") != declared:
        raise ContractError(
            "the sealed product bundle no longer matches the digest its export "
            "artifact recorded; nothing may be published over changed bytes"
        )
    return data, record["payload"]


def publish(tree: RunTree, out_dir: Path) -> dict:
    """Verify the sealed bundle and put it at `out_dir`, atomically or not at all.

    The destination is *reserved* with `mkdir` before the read, verify and write work
    below, not merely checked: an existence check followed by slow work leaves a real
    window for a second publish to create the destination in between, and `mkdir` is
    atomic at the filesystem level. It refuses a symlink at this path too, broken or
    not, exactly as the check it replaces did.
    """
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_dir.mkdir()
    except OSError as error:
        raise ContractError(
            f"could not reserve export destination {out_dir}: {error}; an export destination "
            "is never reused or merged into, so a half-written publication can never be "
            "mistaken for a whole one"
        ) from error
    try:
        data, payload = sealed_bundle(tree)
        aggregate = payload.get("aggregate")
        if not isinstance(aggregate, dict) or not isinstance(aggregate.get("status"), str):
            raise ContractError(
                "the export artifact records no aggregate status; a product may not be "
                "published under a summary its own run never wrote"
            )
        staging = Path(
            tempfile.mkdtemp(prefix=f".{out_dir.name}.publishing-", dir=str(out_dir.parent))
        )
        try:
            # Verified into the staging directory, from the bytes alone, exactly as a
            # recipient with no run tree would: if it does not survive that, it is not a
            # product to publish and the destination stays absent.
            #
            # `verify_delivered_bundle` rather than `verify_export_bundle`: the latter
            # checks that the package is internally whole and stops there, so a product
            # whose acts.jsonl and acts.sqlite carried different readings of one act
            # passed it while its own manifest claimed `identity_verified_across` those
            # formats. The build checked that; this, the gate the product actually
            # leaves through, did not.
            manifest = verify_delivered_bundle(data, staging / EXTRACTION_NAME)
            if aggregate != manifest.get("aggregate"):
                raise ContractError(
                    "the delivered bundle's aggregate disagrees with the sealed export artifact; "
                    "nothing was published because the run tree and package no longer describe "
                    "one result; restore the immutable run tree from an intact copy before "
                    "retrying publication"
                )
            run = tree.read_run()
            expected_run_binding = {
                "fixture_id": payload.get("fixture_id"),
                "scenario": payload.get("scenario"),
                "config_digest": run.get("config_digest"),
            }
            if manifest.get("run") != expected_run_binding:
                # A clean-machine verifier has no external record to compare these
                # labels with. The publisher does: fixture/scenario come from the
                # immutable export artifact, while config_digest comes from run.json.
                # This is an internal-consistency check, not a signature over either
                # file; authenticity beyond the run-tree immutability contract would
                # require an external trust root this package does not claim to have.
                raise ContractError(
                    "the delivered bundle's run binding disagrees with the sealed export "
                    "artifact or run configuration digest; nothing was published because the "
                    "package is internally inconsistent with this run tree; restore the "
                    "immutable run tree from an intact copy before retrying publication"
                )
            bundle_record = payload.get("bundle")
            if (
                not isinstance(bundle_record, dict)
                or bundle_record.get("manifest_self_hash") != manifest.get("self_hash")
                or bundle_record.get("claims_status") != manifest.get("claims", {}).get("status")
            ):
                raise ContractError(
                    "the delivered bundle's manifest identity or status disagrees with the "
                    "sealed export artifact; nothing was published because the package and its "
                    "run-tree envelope are not one sealed result; restore the immutable run "
                    "tree from an intact copy before retrying publication"
                )
            verification = manifest.get("verification", {})
            search_fold_verification = verification.get("search_fold")
            if (
                search_fold_verification is not None
                and search_fold_verification.get("status") != "verified"
            ):
                # The clean verifier can honestly report that a version-dependent
                # recomputation was unavailable. Publication cannot turn that honest
                # non-measurement into a successful terminal claim: a resealer can
                # select this branch by changing only export_metadata.unidata_version.
                raise ContractError(
                    "the delivered bundle's search-fold recomputation was not run; "
                    "a declined terminal measurement is a publication refusal, so nothing was "
                    "published; use a verifier whose Unicode database matches the package's "
                    "recorded version and retry"
                )
            (staging / ARMARIUM_ARCHIVE_NAME).write_bytes(data)
            # `mkdtemp` creates at 0o700, so the published directory's permissions
            # would otherwise be whatever the staging call happened to make them —
            # narrower than the surrounding tree and unrelated to the operator's
            # umask, because `os.replace` moves the directory rather than creating
            # one at the destination. A published product a recipient cannot read
            # is not published. Set from the umask the way `mkdir` would, so the
            # bundle looks like every other directory this operator makes rather
            # than like a temporary one. Found by CodeRabbit.
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(staging, 0o777 & ~umask)
            # The one thing that ever populates the reservation, and it does so
            # atomically. Inside this try so a rename failure also cleans the staging
            # directory rather than orphaning it beside the empty reservation.
            os.replace(staging, out_dir)
        except BaseException:
            try:
                shutil.rmtree(staging)
            except OSError as cleanup_error:
                print(
                    f"warning: could not remove staging directory {staging}: {cleanup_error}",
                    file=sys.stderr,
                )
            raise
    except BaseException:
        # Every failure above re-raises before `os.replace`, so `out_dir` is still the
        # empty reservation. Removing it is what makes a failed publish leave no
        # destination at all rather than an empty one.
        try:
            out_dir.rmdir()
        except OSError as cleanup_error:
            print(
                f"warning: the empty destination {out_dir} remains: {cleanup_error}",
                file=sys.stderr,
            )
        raise
    # What the clean-machine pass actually did, carried out of it rather than
    # discarded. `search_fold` is the one check that can honestly decline to run --
    # a package built under a different Unicode database keeps its digest and
    # coverage checks and skips the recomputation -- and an operator told only
    # "published: complete" would never learn which of the two happened.
    return {
        "archive": ARMARIUM_ARCHIVE_NAME,
        "extraction": EXTRACTION_NAME,
        "sha256": digest_bytes(data),
        "status": manifest["claims"]["status"],
        "unit_count": manifest["claims"]["terminal_ledger"]["unit_count"],
        "unresolved": len(manifest["claims"]["partial_reasons"]),
        "aggregate_status": aggregate["status"],
        "projection_identity": verification.get("projection_identity", {}).get(
            "status", "not recorded"
        ),
        "search_fold": verification.get("search_fold", {}).get(
            "status", "not-run-no-acts-database-in-this-package"
        ),
    }


def main() -> int:
    parser = stage_parser(__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="the destination for the export bundle")
    args = parser.parse_args()
    # Publication is a reader of a completed run, not a stage resuming under the
    # configuration currently on disk. `read_run` verifies the sealed authority;
    # every artifact and input read below remains checked against that authority.
    tree = RunTree(Path(args.run_root), args.run_id)
    tree.read_run()
    summary = publish(tree, Path(args.out))
    print(
        f"export bundle published to {args.out}: {summary['status']}, "
        f"{summary['unit_count']} accounted units, {summary['unresolved']} unresolved"
    )
    for key in (
        "archive",
        "extraction",
        "sha256",
        "aggregate_status",
        "projection_identity",
        "search_fold",
    ):
        print(f"  {key}: {summary[key]}")
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
