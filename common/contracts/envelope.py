"""The envelope every artifact wears, and the refusals at every handoff.

Deliberately small. The envelope carries what a *consumer* needs in order to decide
whether it may act on the thing at all — schema, identities, outcome, what produced
it, and what it was derived from — and the stage payload carries everything else. A
fat envelope becomes a second schema competing with the stage's own, which is the
drift spec 01 is trying to avoid by having one executable authority.

The input references are the load-bearing part. Each names a path and the sha256 of
the bytes at that path, so a consumer can prove that what it is reading is what the
producer wrote. That is ARCHITECTURE's second and third invariants made checkable:
every region traces back to the Exemplar, and the exact image shown to a model is
reproducible from the Exemplar plus the recorded transforms.

No timestamps. Two identical runs must produce identical bytes, or "repeating the
identical command leaves all artifact bytes unchanged" is not testable. When a
moment genuinely matters it lives in an approval record or a run receipt, both of
which are honestly non-deterministic and neither of which is a stage artifact.
"""

from typing import Any, Final

from .canonical import (
    SCHEMA_LABEL,
    digest_bytes,
    self_hash,
    self_hash_refusal,
    verify_self_hash,
)
from .errors import ReservedKindRefusal, SchemaRefusal
from .identities import artifact_id, is_well_formed
from .outcomes import classify, require_approval
from .stages import STAGES

_REQUIRED: Final = (
    "schema",
    "run_id",
    "artifact_id",
    "attempt_id",
    "subject_id",
    "stage",
    "kind",
    "outcome",
    "config_digest",
    "producer",
    "inputs",
    "payload",
    "self_hash",
)

_OPTIONAL: Final = ("approval_ref",)

# These names belong to producer branches that have not yet defined their
# payloads.  Refuse them at the envelope boundary rather than relying on their
# current absence from a run tree (which would be a vacuous promise).
_RESERVED_KINDS: Final = frozenset()


def build_envelope(
    *,
    run_id: str,
    artifact_id: str,
    subject_id: str,
    stage: str,
    kind: str,
    outcome: str,
    config_digest: str,
    adapter_revision: str,
    inputs: list[dict[str, str]],
    payload: dict[str, Any],
    attempt: str | None = None,
    approval_ref: str | None = None,
) -> dict[str, Any]:
    """Assemble an envelope and refuse it here rather than at the next stage.

    Validating on the way out as well as on the way in is deliberate: a producer
    that writes an artifact nobody can read has already lost the work, and finding
    out at the consumer means the evidence of what went wrong is a stage away.
    """
    envelope: dict[str, Any] = {
        "schema": SCHEMA_LABEL,
        "run_id": run_id,
        "artifact_id": artifact_id,
        # The artifact id is derived from this exact attempt.  Keeping the
        # binding beside it is what lets a consumer recompute the id rather than
        # accepting any well-formed-looking string under a trusted path.
        "attempt_id": attempt,
        "subject_id": subject_id,
        "stage": stage,
        "kind": kind,
        "outcome": outcome,
        "config_digest": config_digest,
        "producer": {"stage": stage, "adapter_revision": adapter_revision},
        "inputs": sorted(
            inputs, key=lambda ref: (ref.get("relative_path", ""), ref.get("sha256", ""))
        ),
        "payload": payload,
    }
    if approval_ref is not None:
        envelope["approval_ref"] = approval_ref
    # An artifact id is stable across a stage's payload by design — attempts
    # append under a derived identity, while the payload remains the evidence of
    # what happened.  Its own hash closes the otherwise unsealed half: a changed
    # payload cannot be mistaken for the immutable artifact the consumer was
    # handed.  Direct input references still establish lineage; this catches an
    # altered artifact before a stage can reinterpret it.
    envelope["self_hash"] = self_hash(envelope)
    validate_envelope(envelope)
    return envelope


def validate_envelope(envelope: Any) -> dict[str, Any]:
    """Refuse anything that is not a well-formed envelope for a known stage."""
    if not isinstance(envelope, dict):
        raise SchemaRefusal("artifact is not an object")

    missing = [field for field in _REQUIRED if field not in envelope]
    if missing:
        raise SchemaRefusal(f"artifact is missing required fields {missing}")

    unexpected = sorted(set(envelope) - set(_REQUIRED) - set(_OPTIONAL))
    if unexpected:
        raise SchemaRefusal(
            f"artifact carries unknown top-level field(s) {unexpected}; an envelope is a closed "
            "handoff schema"
        )

    if envelope["schema"] != SCHEMA_LABEL:
        raise SchemaRefusal(
            f"artifact declares schema {envelope['schema']!r}, not {SCHEMA_LABEL!r}; "
            "a consumer that guesses at an unknown schema is how a wrong reading "
            "gets published with confidence"
        )

    stage = envelope["stage"]
    if stage not in STAGES:
        raise SchemaRefusal(f"artifact names stage {stage!r}, which does not exist")

    for field in ("run_id", "kind", "config_digest", "subject_id"):
        if not isinstance(envelope[field], str) or not envelope[field]:
            raise SchemaRefusal(f"artifact field {field!r} is empty or not a string")

    if envelope["kind"] in _RESERVED_KINDS:
        raise ReservedKindRefusal(
            f"artifact kind {envelope['kind']!r} is reserved for its producing branch and is "
            "not accepted by the R0 contract"
        )

    if not is_well_formed(envelope["artifact_id"]):
        raise SchemaRefusal(f"artifact_id {envelope['artifact_id']!r} is malformed")

    attempt = envelope["attempt_id"]
    if attempt is not None and (
        not is_well_formed(attempt)
        or not isinstance(attempt, str)
        or not attempt.startswith("att_")
    ):
        raise SchemaRefusal(
            f"attempt_id {attempt!r} is not an attempt identity or null for a once-only artifact"
        )
    expected_artifact_id = artifact_id(
        envelope["stage"], envelope["kind"], envelope["subject_id"], attempt
    )
    if envelope["artifact_id"] != expected_artifact_id:
        raise SchemaRefusal(
            f"artifact_id {envelope['artifact_id']!r} does not verify against its stage, kind, "
            f"subject, and attempt binding (recomputed {expected_artifact_id!r})"
        )

    producer = envelope["producer"]
    if not isinstance(producer, dict) or set(producer) != {"stage", "adapter_revision"}:
        raise SchemaRefusal(
            "producer is not the closed {stage, adapter_revision} provenance object"
        )
    if producer["stage"] != stage:
        raise SchemaRefusal("producer disagrees with the artifact's stage")
    if (
        not isinstance(producer["adapter_revision"], str)
        or not producer["adapter_revision"].strip()
    ):
        raise SchemaRefusal(
            "producer names no adapter revision; GOVERNANCE 6 — provenance travels "
            "with the record, at the moment it was produced"
        )

    # Fatal rather than a refusal when the outcome is in no set: that is invariant
    # #10's imbalance, and it is not a unit the run may route around.
    classify(stage, envelope["outcome"])
    require_approval(stage, envelope["outcome"], envelope.get("approval_ref"))

    validate_input_refs(envelope["inputs"])

    if not isinstance(envelope["payload"], dict):
        raise SchemaRefusal("payload is not an object")

    if not verify_self_hash(envelope):
        # `verify_self_hash` swallows both `RecursionError` and `TypeError` so
        # every one of its many boolean callers gets a safe refusal instead of a
        # crash. That breadth costs the specific diagnostic here: the serializer
        # already names the exact offending field and value. Current contents
        # that cannot be hashed provide no digest to compare and cannot establish
        # whether they began malformed or changed later. `self_hash_refusal`
        # recomputes once more on this refusal-only path to recover that detail
        # without widening
        # `verify_self_hash`'s contract for its other fourteen-odd callers.
        #
        # The recursion band is named here too rather than falling through to
        # the sentence below: current contents too deep to walk are not
        # checkable on this machine, so no digest comparison can establish a
        # mismatch.
        unhashable = self_hash_refusal(envelope)
        if unhashable is not None:
            raise SchemaRefusal(f"artifact fails its self-hash: {unhashable}")
        raise SchemaRefusal(
            "artifact fails its self-hash: its sealed envelope or payload changed after publication"
        )

    return envelope


def validate_input_refs(inputs: Any) -> None:
    """Every input reference names a path and the digest of the bytes there."""
    if not isinstance(inputs, list):
        raise SchemaRefusal("inputs is not a list")
    # Keyed on the path alone, not on (path, digest). Keying on the pair let one
    # path be listed twice with two different digests — a contradiction, since one
    # file cannot hold two sets of bytes, and one that splits consumers: whichever
    # reference a reader checks first decides what it believes. It also let a page
    # be counted twice in the inputs, which is the double-count this refusal exists
    # to prevent.
    seen: dict[str, str] = {}
    for ref in inputs:
        if not isinstance(ref, dict):
            raise SchemaRefusal("an input reference is not an object")
        path, sha = ref.get("relative_path"), ref.get("sha256")
        if not isinstance(path, str) or not path:
            raise SchemaRefusal("an input reference has no relative_path")
        if path.startswith("/") or ".." in path.split("/"):
            raise SchemaRefusal(
                f"input reference {path!r} escapes the run tree; references are "
                "relative to the run root so a tree stays movable and verifiable"
            )
        if not isinstance(sha, str) or len(sha) != 64 or not _is_hex(sha):
            raise SchemaRefusal(f"input reference {path!r} has no sha256 digest")
        if path in seen:
            conflict = (
                f", with two digests ({seen[path]} and {sha}): one path cannot hold "
                "two sets of bytes"
                if seen[path] != sha
                else ""
            )
            raise SchemaRefusal(f"input reference {path!r} is listed twice{conflict}")
        seen[path] = sha


def verify_input_bytes(ref: dict[str, str], data: bytes) -> None:
    """Refuse bytes that do not match the digest the reference claims for them."""
    actual = digest_bytes(data)
    if actual != ref["sha256"]:
        raise SchemaRefusal(
            f"input {ref['relative_path']} has digest {actual}, but the artifact "
            f"that referenced it recorded {ref['sha256']}: the bytes changed under "
            "a sealed reference, so nothing downstream may act on them"
        )


def _is_hex(value: str) -> bool:
    return all(character in "0123456789abcdef" for character in value)
