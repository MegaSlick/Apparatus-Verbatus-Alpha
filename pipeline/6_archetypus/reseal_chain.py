"""The one reseal chain the stage's test forgeries go through.

Three test files used to rebuild the same sequence independently: patch the
review's inputs, patch `payload["perlectio_ref"]`, recompute `self_hash`, write
canonical bytes. When the review or Perlectio envelope gains a bound field, one
shared chain moves every forgery with it — three private copies would keep
sealing the old shape, and their refusal-message tests would keep passing
against a record no stage would ever write.

Test support, not stage code: `run.py` never imports this. Test modules in this
directory import it by name (pytest puts the directory on `sys.path` for them).
"""

import json

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.stages import RECENSOR


def repoint_review(tree, review: dict, forged_ref: dict) -> None:
    """Rewrite an accepted review's perlectio_ref to a forged reference, sealed."""
    review_path = tree.resolve(tree.artifact_path(RECENSOR, "review", review["artifact_id"]))
    old_ref = review["payload"]["perlectio_ref"]
    assert old_ref in review["inputs"], (
        "the review does not list its own perlectio_ref as a direct input, so this "
        "reseal would repoint the payload without repointing the inputs — the forged "
        "review would then fail at the retained-reference check instead of at the "
        "refusal the calling test names"
    )
    review["inputs"] = [
        forged_ref if reference == old_ref else reference for reference in review["inputs"]
    ]
    review["payload"]["perlectio_ref"] = forged_ref
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))


def reseal_reviewed_reading(tree, review: dict, mutate) -> str:
    """Mutate the reviewed Perlectio's payload and reseal the chain around it.

    Returns the review's subject act id, for tests that need to find the
    record the tampered reading establishes (or fails to).
    """
    old_ref = review["payload"]["perlectio_ref"]
    reading_path = tree.resolve(old_ref["relative_path"])
    reading = json.loads(reading_path.read_text(encoding="utf-8"))
    mutate(reading["payload"])
    # Reseal the nested payload hash too, when the producer carries one, so a
    # later stage-side check of it cannot make every forgery here fail at the
    # hash instead of at the refusal the calling test names.
    if "self_hash" in reading["payload"]:
        reading["payload"]["self_hash"] = self_hash(reading["payload"])
    reading["self_hash"] = self_hash(reading)
    reading_path.write_bytes(canonical_bytes(reading))
    repoint_review(
        tree,
        review,
        {
            "relative_path": old_ref["relative_path"],
            "sha256": digest_bytes(reading_path.read_bytes()),
        },
    )
    return review["subject_id"]
