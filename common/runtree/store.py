"""The run tree: immutable artifacts, atomic publication, and honest reuse.

The tree is the evidence. Everything a stage learned is a file in it, and the whole
of resume, rerun, and accounting rests on three properties this module is
responsible for:

  Artifacts are immutable.   Once published, bytes never change. A second publish
                             of identical bytes is a no-op that reports `reused`; a
                             second publish of *different* bytes under the same
                             identity is refused before anything is written.
  Publication is atomic.     Temp file in the same directory, then os.replace. A
                             crash leaves either the old file or the new one, never
                             a half-written artifact that a resume would trust.
  Manifests are rebuildable. manifest.json is an inventory derived from the
                             artifacts on disk, never the only evidence that
                             something happened. Delete it and it comes back
                             identical; disagree with it and the artifacts win.

`run.json` is the immutable authority for what this run *is*: its source pages,
its configured witness chairs, its configuration digest, its adapter recipes. It
deliberately does not predeclare acts — the Designator's proposal seal is the
downstream expected-act authority, because acts are discovered and pages are given.

Reusing a run id whose source, configuration, or adapter recipes have changed fails
before any write. That is spec 01's third test, and it is the difference between a
resumed run and a corrupted one.

`receipts/sha256/` is the one thing here that is not a stage artifact. Serving receipts
and approval records both carry a real moment, so `envelope.py`'s docstring already
rules them out of a stage artifact; publishing either one there would break "repeating
the identical command leaves every byte unchanged". They are written content-addressed
under the run root, outside every stage's directory and out of every stage manifest,
and a stage payload carries only its digest-checked reference plus the immutable facts
it needs.
"""

import errno
import json
import os
import stat
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from common.chairs.receipts import receipt_record, validate_receipt
from common.contracts.approval import ApprovalRecordReference, validate_approval_record
from common.contracts.canonical import (
    SCHEMA_LABEL,
    canonical_bytes,
    digest_bytes,
    is_sha256,
    self_hash,
    self_hash_refusal,
    verify_self_hash,
)
from common.contracts.envelope import (
    digest_ref,
    validate_envelope,
    validate_input_refs,
    verify_input_bytes,
)
from common.contracts.errors import (
    ApprovalRefusal,
    ContractError,
    IncompatibleReuse,
    SchemaRefusal,
)
from common.contracts.identities import validate_run_id
from common.contracts.stages import DOOR, writing_directory
from common.corpus_register import empty_register, validate_register_bytes
from common.durability import sync_directory

RUN_FILE: Final = "run.json"
MANIFEST_FILE: Final = "manifest.json"
DOOR_MANIFEST_FILE: Final = "manifest-door.json"
INDEX_FILE: Final = "index.json"
ARTIFACTS_DIR: Final = "artifacts"
BLOBS_DIR: Final = "blobs/sha256"
RECEIPTS_DIR: Final = "receipts/sha256"
RECENSOR_PARTITION_RECEIPT_FILE: Final = "run-health/recensor-partition-receipt.json"
# Written by the serving launcher while a stage runs, never by this store; named
# so `inventory_scope()` covers every path any code writes in the tree.
SERVING_LOGS_DIR: Final = "serving-logs"

# The facts a run id is bound to. Changing any of them means this is a different
# run wearing an old name, and reuse is refused rather than resumed.
_BOUND_FIELDS: Final = (
    "source_manifest",
    "config_digest",
    "adapter_recipes",
    "witness_chairs",
    "corpus_frame_membership",
    "register_digest",
    "register_required",
)
_INGRESS_FIELD: Final = "ingress"

# What a filesystem that will not hard-link answers with.
_NO_HARD_LINKS: Final = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS})
# A run tree may be damaged or hostile (fetched, resumed), so every whole-file
# read is bounded, and the manifest walk is bounded in entries too.
_MAX_MANIFEST_ARTIFACT_BYTES: Final = 64 * 1024 * 1024
_MAX_MANIFEST_WALK_ENTRIES: Final = 100_000
# Two read ceilings, one per kind of file.  `MAX_RECORD_READ_BYTES` bounds a
# JSON record (the largest legitimate one, a 100,000-entry manifest, is tens of
# MiB); it is public so a caller reading a record through `read_bytes` can ask
# for it by name.  `_MAX_TREE_READ_BYTES` bounds a page blob: half again the
# 128 MiB decoded-page bound in `pipeline/1_exemplar/image_formats.py`, and
# below `MAX_FETCH_OBJECT_BYTES` in `operations/operator/surface.py`, since a
# read ceiling above the fetch ceiling could never fire on fetched bytes.
# `operations/operator/test_surface.py` pins that ordering.
MAX_RECORD_READ_BYTES: Final = _MAX_MANIFEST_ARTIFACT_BYTES
_MAX_TREE_READ_BYTES: Final = 192 * 1024 * 1024
_DIRECTORY_OPEN_FLAGS: Final = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)
_FILE_OPEN_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
_RENDER_SETTINGS_FIELD: Final = "render_settings"
# Recorded by name, not only folded into `config_digest`, so a reader holding the
# tree alone can name the policy bytes that governed the run.
_SEALED_CONFIG_DIGESTS_FIELD: Final = "sealed_config_digests"
# The commit of the code that created this run, so a fetched tree can say which
# code produced it.  Not a bound field: a run id names inputs and configuration,
# not a build, and binding it would refuse every resume after a fix.  The commit
# behind each later stage is in the orchestrator's timing journal, outside the tree.
_REPOSITORY_COMMIT_FIELD: Final = "repository_commit"


class PublishResult:
    """What happened when an artifact was published, so callers can say so.

    `reused` is the interesting one: it is how a resumed run proves it did not
    rewrite work it had already done, which is spec 01's fourth test.
    """

    __slots__ = ("relative_path", "reused")

    def __init__(self, relative_path: str, reused: bool):
        self.relative_path = relative_path
        self.reused = reused

    def __repr__(self) -> str:
        return f"PublishResult({self.relative_path!r}, reused={self.reused})"


class RunReceiptReference:
    """A digest-checked reference to a non-deterministic receipt under one run.

    The receipt itself includes a timestamp and endpoint and is therefore never a
    stage artifact.  A stage carries only this reference plus the immutable model
    identity/revision that produced its reading.
    """

    __slots__ = ("relative_path", "sha256")

    def __init__(self, relative_path: str, sha256: str):
        self.relative_path = relative_path
        self.sha256 = sha256

    def to_record(self) -> dict[str, str]:
        return {"relative_path": self.relative_path, "sha256": self.sha256}

    def __repr__(self) -> str:
        return f"RunReceiptReference({self.relative_path!r}, sha256={self.sha256!r})"


class RunTree:
    """One run's directory, and the only writer to it."""

    def __init__(self, root: Path, run_id: str):
        self.run_id = validate_run_id(run_id)
        # Resolved apart from its run-id child, so a run-id symlink cannot become
        # the root every later containment check protects.
        requested_root = Path(root).resolve()
        candidate = requested_root / self.run_id
        resolved = candidate.resolve()
        if not resolved.is_relative_to(requested_root):
            raise SchemaRefusal(
                "run id resolves outside the requested run root; a run tree may not be "
                "redirected through a symlink"
            )
        # One resolved spelling (`/private/tmp`, never `/tmp`) for exact comparisons.
        self.root = resolved
        self._root_identity: tuple[int, int] | None = None
        if self.root.exists():
            self._bind_root_identity()
        self._config_digest: str | None = None

    # --- Creation and the run authority ---------------------------------------

    @classmethod
    def create(
        cls,
        root: Path,
        run_id: str,
        *,
        source_manifest: list[dict[str, Any]],
        config_digest: str,
        adapter_recipes: dict[str, str],
        witness_chairs: list[str],
        register_bytes: bytes | None = None,
        ingress: dict[str, Any] | None = None,
        render_settings: dict[str, Any] | None = None,
        sealed_config_digests: dict[str, str] | None = None,
        repository_commit: str | None = None,
    ) -> "RunTree":
        """Open a run, creating it if new and refusing an incompatible reuse.

        The refusal happens before any directory is created for a *new* run and
        before any artifact is touched for an existing one, so a rejected reuse
        leaves the tree exactly as it found it.
        """
        tree = cls(root, run_id)
        # Not stored until the authority accepts it: storing first would write a
        # foreign register into an existing run on the way to refusing it.
        snapshot = empty_register() if register_bytes is None else register_bytes
        register_required = register_bytes is not None
        validate_register_bytes(snapshot)
        snapshot_digest = digest_bytes(snapshot)
        # Refused here, where a manifest enters a run.  A repeated ordinal would let
        # the Armarium's set-based page census balance with a page lost.
        ordinals = [page.get("ordinal") for page in source_manifest]
        if any(not isinstance(ordinal, int) or isinstance(ordinal, bool) for ordinal in ordinals):
            raise SchemaRefusal(
                "every source page must declare an integer ordinal: a run cannot "
                "account for pages it cannot count"
            )
        # Every producer counts pages from one; nothing here can have written less.
        non_positive = sorted({ordinal for ordinal in ordinals if ordinal < 1})
        if non_positive:
            raise SchemaRefusal(
                f"source pages declare ordinal(s) {non_positive}; a source page ordinal "
                "is its position in the submission and is counted from one, so a run "
                "cannot say which page a value below it names"
            )
        ordinal_counts = Counter(ordinals)
        repeated = sorted(ordinal for ordinal, count in ordinal_counts.items() if count > 1)
        if repeated:
            raise SchemaRefusal(
                f"source pages declare ordinal(s) {repeated} more than once; an "
                "ordinal names one page, so a repeat leaves the run unable to say "
                "how many pages it was given"
            )
        # Membership is derived from the manifest: accepting a caller-supplied
        # value could let different page sets claim the same corpus frame.
        membership = _default_corpus_frame_membership(source_manifest)
        _validate_corpus_frame_membership(membership)
        authority = {
            "schema": SCHEMA_LABEL,
            "run_id": tree.run_id,
            "source_manifest": sorted(
                source_manifest, key=lambda page: (page.get("ordinal", 0), page.get("sha256", ""))
            ),
            "config_digest": config_digest,
            "adapter_recipes": dict(sorted(adapter_recipes.items())),
            "witness_chairs": sorted(witness_chairs),
            "corpus_frame_membership": membership,
            "register_digest": snapshot_digest,
            "register_required": register_required,
        }
        if ingress is not None:
            authority[_INGRESS_FIELD] = ingress
        if render_settings is not None:
            if not isinstance(render_settings, dict) or not render_settings:
                raise SchemaRefusal("run render_settings must be a non-empty object when supplied")
            authority[_RENDER_SETTINGS_FIELD] = render_settings
        if sealed_config_digests is not None:
            if not isinstance(sealed_config_digests, dict) or not sealed_config_digests:
                raise SchemaRefusal(
                    "run sealed_config_digests must be a non-empty object when supplied"
                )
            if any(
                not isinstance(name, str) or not name or not is_sha256(digest)
                for name, digest in sealed_config_digests.items()
            ):
                raise SchemaRefusal(
                    "every sealed configuration digest must be a lowercase sha256 recorded "
                    "under a named policy; an unnamed or malformed one names nothing a "
                    "point of use could ask for"
                )
            authority[_SEALED_CONFIG_DIGESTS_FIELD] = dict(sorted(sealed_config_digests.items()))
        if repository_commit is not None:
            if not _is_full_commit(repository_commit):
                raise SchemaRefusal(
                    "a run's repository_commit must be forty lowercase hexadecimal "
                    "characters; a short or decorated revision names a commit only against "
                    "the repository that resolved it, which a fetched tree no longer has"
                )
            authority[_REPOSITORY_COMMIT_FIELD] = repository_commit
        authority["self_hash"] = self_hash(authority)

        run_file = tree.root / RUN_FILE
        tree.root.parent.mkdir(parents=True, exist_ok=True)
        # Serialized on the resolved parent itself, so no lock pathname is
        # predictable.  The snapshot is published before run.json, so a failed
        # blob write cannot leave an authority sealing evidence that never arrived.
        with _run_creation_lock(tree.root.parent):
            tree.root.mkdir(parents=True, exist_ok=True)
            # Bound before any write when __init__ found no root, and re-verified
            # against the identity __init__ bound when it did.
            tree._bind_root_identity()
            if run_file.exists():
                _verify_compatible_reuse(tree, run_id, authority)
                _verify_register_snapshot_present(tree, snapshot_digest, snapshot)
                return tree
            tree.put_blob(DOOR, snapshot)
            try:
                _atomic_create(run_file, canonical_bytes(authority))
            except FileExistsError:
                # A writer ignoring the advisory lock won the race; its authority
                # must pass the same reuse check.
                _verify_compatible_reuse(tree, run_id, authority)
                _verify_register_snapshot_present(tree, snapshot_digest, snapshot)
        return tree

    def read_run(self) -> dict[str, Any]:
        """The run authority, refused unless its self-hash verifies its current contents."""
        run_file = self.root / RUN_FILE
        if not run_file.exists():
            raise IncompatibleReuse(
                f"no {RUN_FILE} under {self.root}: there is no run here to read, and "
                "a stage that wrote into one anyway would be writing into nothing"
            )
        record = _read_json(run_file)
        if not verify_self_hash(record):
            unhashable = self_hash_refusal(record)
            if unhashable is not None:
                raise IncompatibleReuse(
                    f"{run_file} fails its own self-hash: {unhashable}. Nothing in this "
                    "tree can be trusted against it"
                )
            raise IncompatibleReuse(
                f"{run_file} fails its own self-hash: the run authority was edited "
                "after it was sealed, so nothing in this tree can be trusted against it"
            )
        if record.get("schema") != SCHEMA_LABEL:
            raise IncompatibleReuse(
                f"{run_file} declares schema {record.get('schema')!r}, not {SCHEMA_LABEL!r}; "
                "an old run cannot be reinterpreted under a new evidence contract"
            )
        if record.get("run_id") != self.run_id:
            raise IncompatibleReuse(
                f"{run_file} belongs to run {record.get('run_id')!r}, not {self.run_id!r}"
            )
        return record

    # --- Paths -----------------------------------------------------------------

    def artifact_path(self, stage: str, kind: str, artifact_id: str) -> str:
        _refuse_path_component(kind, "kind")
        _refuse_path_component(artifact_id, "artifact id")
        return f"{writing_directory(stage)}/{ARTIFACTS_DIR}/{kind}/{artifact_id}.json"

    def blob_path(self, stage: str, digest: str) -> str:
        _refuse_path_component(digest, "blob digest")
        return f"{writing_directory(stage)}/{BLOBS_DIR}/{digest}"

    def manifest_path(self, stage: str) -> str:
        # Door and Exemplar share a directory; separate manifests keep either one's
        # record of a completion seal from being erased by the other's write.
        filename = DOOR_MANIFEST_FILE if stage == DOOR else MANIFEST_FILE
        return f"{writing_directory(stage)}/{filename}"

    def index_path(self, stage: str) -> str:
        """The stage-local, rebuildable derived index path.

        An index is an inventory, never evidence; the store owns its path so a
        stage cannot invent an untracked side file.  Door and Exemplar share one
        directory, so `write_index` refuses a stage whose directory is shared.
        """
        return f"{writing_directory(stage)}/{INDEX_FILE}"

    def serving_log_path(self, stage: str) -> str:
        """Where the serving launcher writes this stage's engine logs, inside the tree.

        Owned here so the launcher and `inventory_scope()` derive the directory
        from `writing_directory` alike; a stage spelling it for itself put logs
        outside the scope, and `fetch-run` then refused the whole tree.
        """
        return f"{writing_directory(stage)}/{SERVING_LOGS_DIR}"

    def receipt_path(self, digest: str) -> str:
        """The one content-addressed location for a validated receipt-backed record."""
        if not is_sha256(digest):
            raise SchemaRefusal(f"receipt digest {digest!r} is not a lowercase sha256")
        return f"{RECEIPTS_DIR}/{digest}.json"

    def recensor_partition_receipt_path(self) -> str:
        """The current derived partition receipt at the Recensor boundary."""
        return RECENSOR_PARTITION_RECEIPT_FILE

    def resolve(self, relative_path: str) -> Path:
        """A path inside this run tree, refusing anything that leaves it.

        Input references are relative so a run tree stays movable and verifiable;
        this is where that promise is enforced rather than assumed.
        """
        if relative_path.startswith("/") or ".." in relative_path.split("/"):
            raise SchemaRefusal(f"{relative_path!r} escapes the run tree")
        try:
            resolved = (self.root / relative_path).resolve()
        except (OSError, RuntimeError, ValueError) as error:
            # A symlink loop (RuntimeError, or OSError) or an unrepresentable path
            # (ValueError) is a path the tree cannot resolve, not a crash.
            raise SchemaRefusal(
                f"{relative_path!r} could not be resolved inside the run tree: {error}"
            ) from error
        # Not a string prefix, which would accept the sibling `.../r1-scratch`.
        if not resolved.is_relative_to(self.root):
            raise SchemaRefusal(f"{relative_path!r} resolves outside the run tree")
        return resolved

    # --- Publication -----------------------------------------------------------

    def publish_artifact(self, envelope: dict[str, Any]) -> PublishResult:
        """Publish one artifact. Immutable, atomic, and honest about reuse."""
        validate_envelope(envelope)
        if envelope["run_id"] != self.run_id:
            raise SchemaRefusal(
                f"artifact belongs to run {envelope['run_id']!r}, not {self.run_id!r}"
            )
        relative = self.artifact_path(envelope["stage"], envelope["kind"], envelope["artifact_id"])
        return self._publish_bytes(relative, canonical_bytes(envelope))

    def put_blob(self, stage: str, data: bytes) -> tuple[str, PublishResult]:
        """Store bytes under their own digest. Content-addressed, so reuse is free."""
        digest = digest_bytes(data)
        return digest, self._publish_bytes(self.blob_path(stage, digest), data)

    def write_run_receipt(self, receipt) -> tuple[RunReceiptReference, PublishResult]:
        """Store one validated serving receipt outside stage artifacts.

        Receipts record a serving moment, so they never reach a stage manifest.
        Content-addressed: an identical write is reused, a different receipt gets
        its own reference.  Validated on the way in as well as out, because an
        invalid record found at the reader has already lost its moment.
        """
        record = receipt_record(receipt)
        data = canonical_bytes(record)
        digest = digest_bytes(data)
        result = self._publish_bytes(self.receipt_path(digest), data)
        return RunReceiptReference(result.relative_path, digest), result

    def write_approval_record(
        self, record: dict[str, Any]
    ) -> tuple[ApprovalRecordReference, PublishResult]:
        """Store one validated approval record outside stage artifacts.

        An approval records a human act at a moment, so it is stored like a
        serving receipt, validated (schema and self-hash) before any path is made.
        """
        validated = validate_approval_record(record)
        data = canonical_bytes(validated)
        digest = digest_bytes(data)
        result = self._publish_bytes(self.receipt_path(digest), data)
        return ApprovalRecordReference(result.relative_path, digest), result

    def write_recensor_partition_receipt(self, record: dict[str, Any]) -> PublishResult:
        """Atomically replace the derived current Recensor partition receipt.

        Unlike an artifact, it changes after a bounded recovery, so it is replaced
        in place; it stays inside `inventory_scope()` because reviewers recompute
        denominators from it.

        A replace, not a compare-and-swap: of two racing Recensor passes the last
        write wins.  The race is bounded, not fixed.  `expected_act_count` is
        sealed by the Designator before the Recensor runs, so a write changing it
        is refused.  A stale write can under-state completeness but never claim
        it, because the reviews it cites are append-only and an act's class only
        moves toward resolution.  Concurrent Recensor passes are still unsafe.
        """
        from common.recensor_receipt import validate_recensor_partition_receipt

        checked = validate_recensor_partition_receipt(record)
        if checked["run_id"] != self.run_id or checked["config_digest"] != self._run_authority():
            raise SchemaRefusal("Recensor partition receipt does not belong to this run authority")
        relative = self.recensor_partition_receipt_path()
        target = self.resolve(relative)
        data = canonical_bytes(checked)
        if target.exists():
            existing = _existing_partition_receipt(target, relative)
            if existing is not None and (
                existing["run_id"] == checked["run_id"]
                and existing["config_digest"] == checked["config_digest"]
                and existing["expected_act_count"] != checked["expected_act_count"]
            ):
                raise SchemaRefusal(
                    "Recensor partition receipt would change its expected_act_count from "
                    f"{existing['expected_act_count']} to {checked['expected_act_count']} under "
                    "the same run authority; the proposal-act denominator is sealed once and "
                    "cannot legitimately differ between two passes over the same run"
                )
            try:
                # A file longer than `data` cannot be the same receipt.
                if _read_bytes_bounded(target, max_bytes=len(data)) == data:
                    return PublishResult(relative, reused=True)
            except SchemaRefusal:
                pass
            except FileNotFoundError:
                # Gone between `exists()` above and here: nothing to reuse or
                # refuse, so publish it, as `_publish_bytes` does at the same seam.
                pass
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, data)
        return PublishResult(relative, reused=False)

    def read_recensor_partition_receipt(self) -> dict[str, Any]:
        """Read the current derived receipt only when it binds to this run."""
        from common.recensor_receipt import validate_recensor_partition_receipt

        path = self.resolve(self.recensor_partition_receipt_path())
        try:
            record = _read_json(path)
        except OSError as error:  # pragma: no cover - _read_json already refuses
            raise SchemaRefusal(f"Recensor partition receipt could not be read: {error}") from error
        checked = validate_recensor_partition_receipt(record)
        if checked["run_id"] != self.run_id or checked["config_digest"] != self._run_authority():
            raise SchemaRefusal("Recensor partition receipt does not belong to this run authority")
        return checked

    def read_run_receipt(self, reference: RunReceiptReference | dict[str, str]) -> dict[str, Any]:
        """Read a receipt only when both its reference and bytes still verify.

        Three checks, because each catches a different lie: the path must be the
        one its own digest names, the bytes there must hash to that digest, and
        the record must still be a whole receipt (#42 — tampered or wrong-schema
        provenance is refused, never repaired).
        """
        parsed = _receipt_reference(reference)
        data = self._read_receipt_bytes(
            parsed.relative_path,
            parsed.sha256,
            SchemaRefusal,
            label="run receipt",
            reference_label="receipt",
        )
        try:
            return validate_receipt(json.loads(data.decode("utf-8")))
        except (UnicodeDecodeError, ValueError) as error:
            raise SchemaRefusal(
                f"run receipt {parsed.relative_path} could not be read: {error}"
            ) from error

    def read_approval_record(self, reference: ApprovalRecordReference) -> dict[str, Any]:
        """Read an approval only through its typed, checked receipt reference.

        The content address verifies that the bytes remain the exact record named
        by the caller; the approval validator then verifies the record's schema
        and self-hash.  Both are necessary: a valid digest proves only that the
        bytes were not changed after this reference was made, not that they ever
        formed an approval record.
        """
        parsed = _approval_record_reference(reference)
        data = self._read_receipt_bytes(
            parsed.relative_path,
            parsed.sha256,
            ApprovalRefusal,
            label="approval record",
            reference_label="approval record",
        )
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ApprovalRefusal(
                f"approval record {parsed.relative_path} could not be read: {error}"
            ) from error
        return validate_approval_record(decoded)

    def _read_receipt_bytes(
        self,
        relative_path: str,
        sha256: str,
        refusal: type[SchemaRefusal],
        *,
        label: str,
        reference_label: str,
    ) -> bytes:
        """The bytes at a receipt reference, refused unless path, presence and digest all agree.

        A missing file is a named refusal like any other bad provenance, never a
        bare `FileNotFoundError`.
        """
        expected_path = self.receipt_path(sha256)
        if relative_path != expected_path:
            raise refusal(
                f"{reference_label} reference {relative_path!r} is not its content-addressed "
                f"path {expected_path!r}"
            )
        try:
            data = self.read_bytes(relative_path)
        except OSError as error:
            raise refusal(f"{label} {relative_path} could not be read: {error}") from error
        actual = digest_bytes(data)
        if actual != sha256:
            raise refusal(
                f"{label} {relative_path} has digest {actual}, not the reference digest {sha256}"
            )
        return data

    def _publish_bytes(self, relative: str, data: bytes) -> PublishResult:
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            _atomic_create(target, data)
        except FileExistsError:
            try:
                # A file longer than `data` is already different.
                existing: bytes | None = _read_bytes_bounded(target, max_bytes=len(data))
            except SchemaRefusal:
                existing = None
            except OSError as error:
                raise IncompatibleReuse(
                    f"{relative} appeared while it was being published and could not be read; "
                    "the immutable write was not replaced"
                ) from error
            if existing == data:
                return PublishResult(relative, reused=True)
            raise IncompatibleReuse(
                f"{relative} already holds different bytes. Artifacts are immutable: "
                "the same identity may not describe two different things, and the "
                "existing file was not touched"
            ) from None
        return PublishResult(relative, reused=False)

    # --- Reading ----------------------------------------------------------------

    def read_artifact(self, stage: str, kind: str, artifact_id: str) -> dict[str, Any]:
        record, _ = self.read_artifact_snapshot(stage, kind, artifact_id)
        return record

    def read_artifact_snapshot(
        self, stage: str, kind: str, artifact_id: str
    ) -> tuple[dict[str, Any], bytes]:
        """Read and validate one artifact, retaining the exact bytes decoded.

        Callers that publish a digest beside decoded fields must derive both
        from one filesystem read.  Returning the bytes from that read prevents
        a concurrent replacement from pairing one record body with another
        record's immutable address.
        """

        relative = self.artifact_path(stage, kind, artifact_id)
        with _naming(relative):
            record, artifact_bytes = _read_json_with_bytes(self.resolve(relative))
            record = validate_envelope(record)
            self._verify_artifact_run(record)
        if (
            record["stage"] != stage
            or record["kind"] != kind
            or record["artifact_id"] != artifact_id
        ):
            raise SchemaRefusal(
                "artifact contents do not match the stage, kind, and identity requested by "
                f"their path {relative!r}"
            )
        self._verify_artifact_path(relative, record)
        self._verify_artifact_inputs(record)
        return record, artifact_bytes

    def read_artifact_reference(
        self,
        reference: dict[str, str],
        *,
        stage: str,
        kind: str,
        subject_id: str | None = None,
    ) -> dict[str, Any]:
        """Read an artifact through a producer's digest-checked input reference.

        An artifact id is an address, not enough evidence that a consumer saw the
        same bytes the producer saw.  Semantic handoffs therefore retain the
        ordinary input reference and resolve it here: its path, bytes, envelope,
        and declared producer all have to agree before a later stage can use it.
        """
        validate_input_refs([reference])
        relative_path = reference["relative_path"]
        try:
            data = self.read_bytes(relative_path)
        except OSError as error:
            raise SchemaRefusal(
                f"referenced artifact {relative_path!r} could not be read: {error}"
            ) from error
        verify_input_bytes(reference, data)
        try:
            record = validate_envelope(json.loads(data.decode("utf-8")))
        except (UnicodeDecodeError, ValueError) as error:
            raise SchemaRefusal(
                f"referenced artifact {relative_path!r} is not valid JSON evidence: {error}"
            ) from error
        self._verify_artifact_run(record)
        self._verify_artifact_path(relative_path, record)
        self._verify_artifact_inputs(record)
        if record["stage"] != stage or record["kind"] != kind:
            raise SchemaRefusal(
                f"referenced artifact {relative_path!r} is {record['stage']!r}/"
                f"{record['kind']!r}, not required {stage!r}/{kind!r}"
            )
        if subject_id is not None and record["subject_id"] != subject_id:
            raise SchemaRefusal(
                f"referenced artifact {relative_path!r} names subject {record['subject_id']!r}, "
                f"not required {subject_id!r}"
            )
        return record

    def read_bytes(self, relative_path: str, *, max_bytes: int | None = None) -> bytes:
        """One file's bytes, under the blob-sized tree ceiling unless told otherwise.

        A caller that knows it is reading a JSON record rather than a page blob
        passes `max_bytes=MAX_RECORD_READ_BYTES`, so the bytes it is about to
        hand to `json.loads` -- which costs several times their size again in
        parsed objects -- are bounded by what a record can legitimately be and
        not by what an image can (G13).
        """
        return _read_bytes_bounded(self.resolve(relative_path), max_bytes=max_bytes)

    def has_artifact(self, stage: str, kind: str, artifact_id: str) -> bool:
        return self.resolve(self.artifact_path(stage, kind, artifact_id)).exists()

    # --- Manifests: derived, never the only evidence ---------------------------

    def build_manifest(self, stage: str, *, verify_inputs: bool = True) -> dict[str, Any]:
        """Walk the stage's artifacts and describe what is actually there.

        Derived from the tree every time it is called, so it cannot drift from
        what the tree holds. That is why a manifest is never evidence on its own:
        if it disagrees with the artifacts, the artifacts are right and the
        manifest was stale.

        ``verify_inputs=False`` skips only ``_verify_artifact_inputs``, so an
        artifact whose upstream blob changed is still listed.  It is for a caller
        whose own boundary owns lineage, never for speed.
        """
        # An empty directory must not make a missing run authority look like an empty run.
        self._run_authority()
        entries: list[dict[str, Any]] = []
        artifacts_root = self._inventory_directory(stage, ARTIFACTS_DIR)
        if artifacts_root is not None:
            # The whole walk first, so a content failure cannot hide a structural one.
            members = list(self._walk_artifact_json(artifacts_root))
            for relative_path in members:
                # One read supplies the verified record and its digest.
                record, artifact_bytes = self._read_manifest_artifact(relative_path)
                with _naming(relative_path):
                    record = validate_envelope(record)
                    self._verify_artifact_run(record)
                self._verify_artifact_path(relative_path, record)
                # Door and Exemplar share a directory; a manifest lists its own producer.
                if record["stage"] != stage:
                    continue
                if verify_inputs:
                    self._verify_artifact_inputs(record)
                entries.append(
                    {
                        "artifact_id": record["artifact_id"],
                        "kind": record["kind"],
                        "subject_id": record["subject_id"],
                        "outcome": record["outcome"],
                        "relative_path": relative_path,
                        "sha256": digest_bytes(artifact_bytes),
                    }
                )
        blobs_root = self._inventory_directory(stage, BLOBS_DIR)
        blobs = [] if blobs_root is None else list(self._walk_blobs(blobs_root))
        return {
            "schema": SCHEMA_LABEL,
            "run_id": self.run_id,
            "stage": stage,
            "artifacts": sorted(entries, key=lambda entry: entry["artifact_id"]),
            "blobs": blobs,
        }

    def _inventory_directory(self, stage: str, subdirectory: str) -> Path | None:
        """Return an unredirected inventory directory, or ``None`` if it is absent.

        Every existing ancestor must be a plain directory; otherwise a broken
        parent could make evidence below it look like an honestly empty inventory.
        """
        relative = f"{writing_directory(stage)}/{subdirectory}"
        # For its diagnostic only; safety comes from the no-follow descriptor walk.
        resolved = self.resolve(relative)
        descriptor = self._open_relative_fd(
            relative,
            directory=True,
            missing_ok=True,
            purpose=f"stage inventory {relative!r}",
        )
        if descriptor is None:
            return None
        os.close(descriptor)
        return resolved

    def _walk_artifact_json(self, directory: Path) -> Iterator[str]:
        """Yield artifact paths while refusing every uninspectable tree entry.

        The walk is iterative so filesystem depth cannot exhaust Python's call
        stack. Its component-wise order matches ``sorted(rglob("*.json"))``;
        ``endswith`` preserves that glob's match for a file named exactly ``.json``.
        A ``.json`` directory is legal only at the kind level, where store-created
        kinds may carry that suffix. Special files are rejected before opening
        because reading a FIFO would block indefinitely.
        """
        relative_root = str(directory.relative_to(self.root))
        start_fd = self._open_relative_fd(
            relative_root,
            directory=True,
            purpose=f"artifact inventory {relative_root!r}",
        )
        assert start_fd is not None
        start_identity = _inode_identity(os.fstat(start_fd))
        walked: dict[tuple[int, int], str] = {start_identity: relative_root}
        # fd, relative directory, ordered names, next index, ancestor identities.
        stack: list[tuple[int, str, list[str], int, frozenset[tuple[int, int]]]] = []
        examined = 0
        try:
            self._push_listing(stack, start_fd, relative_root, frozenset({start_identity}))
            while stack:
                directory_fd, relative_directory, names, index, ancestors = stack[-1]
                if index == len(names):
                    self._require_directory_identity(relative_directory, directory_fd)
                    os.close(directory_fd)
                    stack.pop()
                    continue
                name = names[index]
                stack[-1] = (directory_fd, relative_directory, names, index + 1, ancestors)
                examined += 1
                if examined > _MAX_MANIFEST_WALK_ENTRIES:
                    raise SchemaRefusal(
                        f"artifact inventory exceeds the {_MAX_MANIFEST_WALK_ENTRIES}-entry "
                        "manifest walk limit"
                    )
                relative_path = f"{relative_directory}/{name}"
                # For its diagnostic only, as in `_inventory_directory`.
                self.resolve(relative_path)
                before = self._entry_lstat(directory_fd, name, relative_path)
                if stat.S_ISLNK(before.st_mode):
                    self._raise_manifest_symlink(relative_path, ancestors)
                if stat.S_ISDIR(before.st_mode):
                    if relative_directory != relative_root and name.endswith(".json"):
                        raise SchemaRefusal(
                            f"{relative_path!r} is named as an artifact but is a directory, "
                            "so it cannot be read as artifact bytes"
                        )
                    child_fd = self._open_child_fd(
                        directory_fd, name, before, relative_path, directory=True
                    )
                    identity = _inode_identity(os.fstat(child_fd))
                    if identity in ancestors:
                        os.close(child_fd)
                        raise SchemaRefusal(
                            f"{relative_path!r} is a symlink cycle back to a directory "
                            "already being walked"
                        )
                    if identity in walked:
                        os.close(child_fd)
                        raise SchemaRefusal(
                            f"{relative_path!r} and {walked[identity]!r} are the same directory: "
                            "a manifest may not describe one artifact at two paths"
                        )
                    walked[identity] = relative_path
                    self._push_listing(stack, child_fd, relative_path, ancestors | {identity})
                elif relative_directory == relative_root:
                    raise SchemaRefusal(
                        f"{relative_path!r} is not a directory where an artifact kind "
                        "directory must be, so it cannot disappear from the manifest walk"
                    )
                elif name.endswith(".json"):
                    if not stat.S_ISREG(before.st_mode):
                        raise SchemaRefusal(
                            f"{relative_path!r} is neither a directory nor a regular file, "
                            "so it cannot be read as an artifact"
                        )
                    yield relative_path
        finally:
            for directory_fd, *_ in stack:
                try:
                    os.close(directory_fd)
                except OSError:
                    pass

    def _push_listing(
        self,
        stack: list[tuple[int, str, list[str], int, frozenset[tuple[int, int]]]],
        directory_fd: int,
        relative_directory: str,
        ancestors: frozenset[tuple[int, int]],
    ) -> None:
        """List an opened directory onto the walk stack, closing it if the listing fails."""
        try:
            names = self._listing_fd(directory_fd, relative_directory)
        except BaseException:
            os.close(directory_fd)
            raise
        stack.append((directory_fd, relative_directory, names, 0, ancestors))

    def _walk_blobs(self, directory: Path) -> Iterator[str]:
        """Yield addressable regular blobs in name order.

        Non-digest names include same-directory publication residue and are not
        inventory members. Blob contents are verified when consumed rather than
        during every manifest rebuild because they may be full page images.
        """
        relative_root = str(directory.relative_to(self.root))
        directory_fd = self._open_relative_fd(
            relative_root,
            directory=True,
            purpose=f"blob inventory {relative_root!r}",
        )
        assert directory_fd is not None
        try:
            names = self._listing_fd(directory_fd, relative_root)
            for name in names:
                relative_path = f"{relative_root}/{name}"
                self.resolve(relative_path)
                before = self._entry_lstat(directory_fd, name, relative_path)
                if stat.S_ISLNK(before.st_mode):
                    self._raise_manifest_symlink(relative_path, frozenset())
                if not is_sha256(name):
                    continue
                if not stat.S_ISREG(before.st_mode):
                    raise SchemaRefusal(
                        f"{relative_path!r} is named as a content-addressed blob but is not a "
                        "regular file"
                    )
                blob_fd = self._open_child_fd(
                    directory_fd, name, before, relative_path, directory=False
                )
                os.close(blob_fd)
                yield name
            self._require_directory_identity(relative_root, directory_fd)
        finally:
            os.close(directory_fd)

    def _bind_root_identity(self) -> None:
        """Bind this object to the run directory it opened, by device and inode."""
        try:
            descriptor = os.open(self.root, _DIRECTORY_OPEN_FLAGS)
        except OSError as error:
            raise SchemaRefusal(
                f"run root {self.root} could not be opened without links: {error}"
            ) from error
        try:
            identity = _inode_identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        if self._root_identity is not None and identity != self._root_identity:
            raise SchemaRefusal(
                f"run root {self.root} is no longer the directory this RunTree opened; "
                "its device or inode changed"
            )
        self._root_identity = identity

    def _open_root_fd(self) -> int:
        """Open the bound run root without following a replacement link."""
        try:
            descriptor = os.open(self.root, _DIRECTORY_OPEN_FLAGS)
        except OSError as error:
            raise SchemaRefusal(
                f"run root {self.root} could not be opened without links: {error}"
            ) from error
        try:
            identity = _inode_identity(os.fstat(descriptor))
        except OSError as error:
            os.close(descriptor)
            raise SchemaRefusal(
                f"run root {self.root} could not be identified after it was opened: {error}"
            ) from error
        if self._root_identity is None:
            self._root_identity = identity
        elif identity != self._root_identity:
            os.close(descriptor)
            raise SchemaRefusal(
                f"run root {self.root} is no longer the directory this RunTree opened; "
                "its device or inode changed"
            )
        return descriptor

    def _open_relative_fd(
        self,
        relative_path: str,
        *,
        directory: bool,
        purpose: str,
        missing_ok: bool = False,
    ) -> int | None:
        """Open one run-relative object through no-follow component descriptors."""
        components = Path(relative_path).parts
        parent_fd = self._open_root_fd()
        try:
            for index, component in enumerate(components):
                current_relative = str(Path(*components[: index + 1]))
                final = index == len(components) - 1
                try:
                    before = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    if missing_ok:
                        os.close(parent_fd)
                        return None
                    raise SchemaRefusal(
                        f"{current_relative!r} disappeared while opening {purpose}"
                    ) from None
                except OSError as error:
                    raise SchemaRefusal(
                        f"{current_relative!r} could not be inspected while opening "
                        f"{purpose}: {error}"
                    ) from error
                if stat.S_ISLNK(before.st_mode):
                    self._raise_manifest_symlink(current_relative, frozenset())
                needs_directory = not final or directory
                if needs_directory and not stat.S_ISDIR(before.st_mode):
                    raise SchemaRefusal(
                        f"{purpose} cannot be reached because {current_relative!r} "
                        "is not a directory"
                    )
                if final and not directory and not stat.S_ISREG(before.st_mode):
                    raise SchemaRefusal(
                        f"{current_relative!r} is no longer a regular artifact file after "
                        "the manifest walk, so it cannot be read as artifact bytes"
                    )
                opened = self._open_child_fd(
                    parent_fd,
                    component,
                    before,
                    current_relative,
                    directory=needs_directory,
                )
                os.close(parent_fd)
                parent_fd = opened
            return parent_fd
        except BaseException:
            try:
                os.close(parent_fd)
            except OSError:
                pass
            raise

    def _open_child_fd(
        self,
        parent_fd: int,
        name: str,
        before: os.stat_result,
        relative_path: str,
        *,
        directory: bool,
    ) -> int:
        """Open one checked child and prove the name still denotes that inode."""
        flags = _DIRECTORY_OPEN_FLAGS if directory else _FILE_OPEN_FLAGS
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            operation = "could not be listed or opened" if directory else "could not be opened"
            raise SchemaRefusal(
                f"{relative_path!r} changed or {operation} without following links: {error}"
            ) from error
        try:
            after = os.fstat(descriptor)
        except OSError as error:
            os.close(descriptor)
            raise SchemaRefusal(
                f"{relative_path!r} could not be identified after it was opened: {error}"
            ) from error
        if _inode_identity(after) != _inode_identity(before) or stat.S_IFMT(
            after.st_mode
        ) != stat.S_IFMT(before.st_mode):
            os.close(descriptor)
            raise SchemaRefusal(
                f"{relative_path!r} changed device, inode, or file type between inspection "
                "and open; the manifest refuses the replacement"
            )
        return descriptor

    def _entry_lstat(self, directory_fd: int, name: str, relative_path: str) -> os.stat_result:
        try:
            return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise SchemaRefusal(
                f"{relative_path!r} could not be inspected during the manifest walk: {error}"
            ) from error

    def _listing_fd(self, directory_fd: int, relative_directory: str) -> list[str]:
        """List one opened directory and refuse names that default APFS conflates."""
        try:
            names = []
            with os.scandir(directory_fd) as listing:
                for entry in listing:
                    names.append(entry.name)
                    if len(names) > _MAX_MANIFEST_WALK_ENTRIES:
                        raise SchemaRefusal(
                            f"{relative_directory!r} exceeds the "
                            f"{_MAX_MANIFEST_WALK_ENTRIES}-entry manifest walk limit"
                        )
        except OSError as error:
            raise SchemaRefusal(
                f"{relative_directory!r} could not be listed while this stage's manifest "
                f"was being built: {error}"
            ) from error
        names.sort()
        folded: dict[str, str] = {}
        for name in names:
            collision = folded.get(name.casefold())
            if collision is not None and collision != name:
                raise SchemaRefusal(
                    f"{relative_directory!r} contains case-variant names {collision!r} and "
                    f"{name!r}; default APFS stores them as one name, so this inventory "
                    "cannot preserve both"
                )
            folded[name.casefold()] = name
        return names

    def _require_directory_identity(self, relative_path: str, descriptor: int) -> None:
        """Refuse a directory renamed away or replaced while its entries were read."""
        expected = _inode_identity(os.fstat(descriptor))
        reopened = self._open_relative_fd(
            relative_path,
            directory=True,
            purpose=f"manifest directory {relative_path!r}",
        )
        assert reopened is not None
        try:
            if _inode_identity(os.fstat(reopened)) != expected:
                raise SchemaRefusal(
                    f"{relative_path!r} changed device or inode while its manifest entries "
                    "were being walked"
                )
        finally:
            os.close(reopened)

    def _raise_manifest_symlink(
        self, relative_path: str, ancestors: frozenset[tuple[int, int]]
    ) -> None:
        """Refuse a link, retaining the most specific safe diagnostic available."""
        try:
            resolved = self.resolve(relative_path)
        except SchemaRefusal:
            raise
        try:
            target = resolved.stat()
        except OSError:
            target = None
        if target is not None and _inode_identity(target) in ancestors:
            raise SchemaRefusal(
                f"{relative_path!r} is a symlink cycle back to a directory already being walked"
            )
        raise SchemaRefusal(
            f"{relative_path!r} is a link to {str(resolved)!r}: a manifest reads only the "
            "files and directories the store itself wrote, never an alias"
        )

    def _read_manifest_artifact(self, relative_path: str) -> tuple[Any, bytes]:
        """Read one bounded regular artifact through a no-follow descriptor chain."""
        resolved = self.resolve(relative_path)
        lexical = self.root / relative_path
        if resolved != lexical or lexical.is_symlink():
            raise SchemaRefusal(
                f"{relative_path!r} is a link to {str(resolved)!r}: a manifest reads the "
                "artifact file the store itself wrote, never an alias"
            )
        descriptor = self._open_relative_fd(
            relative_path,
            directory=False,
            purpose=f"manifest artifact {relative_path!r}",
        )
        assert descriptor is not None
        try:
            size = os.fstat(descriptor).st_size
            if size > _MAX_MANIFEST_ARTIFACT_BYTES:
                raise SchemaRefusal(
                    f"{relative_path!r} is {size} bytes, above the "
                    f"{_MAX_MANIFEST_ARTIFACT_BYTES}-byte manifest artifact limit"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read(_MAX_MANIFEST_ARTIFACT_BYTES + 1)
            if len(data) > _MAX_MANIFEST_ARTIFACT_BYTES:
                raise SchemaRefusal(
                    f"{relative_path!r} grew above the "
                    f"{_MAX_MANIFEST_ARTIFACT_BYTES}-byte manifest artifact limit while read"
                )
        except OSError as error:
            raise SchemaRefusal(f"{lexical} could not be read as an artifact: {error}") from error
        finally:
            os.close(descriptor)
        return _decode_json_bytes(data, lexical), data

    def _verify_artifact_path(self, relative_path: str, record: dict[str, Any]) -> None:
        """Require a sealed artifact to live under the path its own fields derive.

        A manifest is rebuilt by walking a directory, while a consumer may ask for
        one exact artifact path.  Both routes must agree about what the bytes are;
        otherwise a syntactically valid envelope can be copied below a different
        producer directory and acquire an identity it never had.
        """
        expected = self.artifact_path(record["stage"], record["kind"], record["artifact_id"])
        if relative_path != expected:
            raise SchemaRefusal(
                f"artifact at {relative_path!r} does not occupy its derived path {expected!r}"
            )

    def _verify_artifact_run(self, record: dict[str, Any]) -> None:
        """Bind every read route to the run tree whose authority is being used.

        The run id alone is not that binding: it is caller-supplied, and two runs
        in two roots may both be `run1`, so an artifact copied from one would be
        accepted by the other.  The `config_digest` every stage publishes is the
        authority's own binding to its inputs, so it is compared too.

        Integrity, not authentication: every input to the hash is inside the
        record, so this proves no author.
        """
        if record["run_id"] != self.run_id:
            raise SchemaRefusal(
                f"artifact belongs to run {record['run_id']!r}, not {self.run_id!r}"
            )
        authority = self._run_authority()
        if record["config_digest"] != authority:
            raise SchemaRefusal(
                f"artifact was produced under configuration {record['config_digest']!r} and this "
                f"run is bound to {authority!r}; two runs may share a name, so the name alone "
                "does not say an artifact belongs here"
            )

    def _run_authority(self) -> str:
        """This tree's sealed `config_digest`.

        Read once and kept, because it cannot change under a run: `read_run` refuses
        an authority that fails its own self-hash, and `create` refuses incompatible
        reuse. Artifact readers run only after creation; a missing or unreadable
        authority therefore makes evidence unreadable rather than disabling its
        run binding.
        """
        if self._config_digest is None:
            config_digest = self.read_run().get("config_digest")
            if not is_sha256(config_digest):
                raise IncompatibleReuse(
                    "run.json has no lowercase sha256 config_digest, so no artifact in this "
                    "tree can be bound to its run authority"
                )
            self._config_digest = config_digest
        return self._config_digest

    def _verify_artifact_inputs(self, record: dict[str, Any]) -> None:
        """Verify each direct input before a consumer may reinterpret this artifact.

        Input references form the handoff chain.  Validating only their shape at
        publication lets an input be edited later and every downstream manifest
        still look complete; its recorded digest is useful only if a reader
        checks it against the bytes again.
        """
        for reference in record["inputs"]:
            try:
                data = self.read_bytes(reference["relative_path"])
            except OSError as error:
                raise SchemaRefusal(
                    f"artifact input {reference['relative_path']!r} could not be read: {error}"
                ) from error
            verify_input_bytes(reference, data)

    def write_manifest(self, stage: str) -> PublishResult:
        """Publish the derived manifest.

        Rewritable on purpose, unlike an artifact: it is an inventory of a growing
        directory, so a stage that publishes a second artifact must be able to
        record it. Nothing may treat it as the evidence that the artifact exists.
        """
        manifest = self.build_manifest(stage)
        relative = self.manifest_path(stage)
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, canonical_bytes(manifest))
        return PublishResult(relative, reused=False)

    def write_index(self, stage: str, index: dict[str, Any]) -> PublishResult:
        """Atomically replace a stage's derived index.

        Rewritable on purpose, exactly as `write_manifest` is: an index is
        regenerated from the immutable records it summarizes on every run, so a
        deleted or stale one repairs itself. The caller remains responsible for
        proving the rows reconcile against those records before anything treats
        the index as accounting.
        """
        if not isinstance(index, dict):
            raise SchemaRefusal("a derived stage index must be an object")
        # Like every read route: no summary file for a tree without a valid run.json.
        self._run_authority()
        directory = writing_directory(stage)
        if _all_writing_directories().count(directory) > 1:
            raise SchemaRefusal(
                f"stage {stage!r} shares run-tree directory {directory!r} with another "
                "producer, so one index file cannot account for both; give the index a "
                "stage-qualified name before either stage writes one"
            )
        try:
            data = canonical_bytes(index)
        except (TypeError, ValueError, RecursionError) as error:
            # Floats and cycles raise TypeError or ValueError, outside the
            # ContractError family; json's C encoder can still recurse.
            raise SchemaRefusal(
                f"a derived stage index must be canonically serializable: {error}"
            ) from error
        relative = self.index_path(stage)
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, data)
        return PublishResult(relative, reused=False)

    def read_index(self, stage: str) -> dict[str, Any]:
        """Read a derived index as JSON; reconciliation belongs to its stage.

        Behind the run authority like every other read route: a tree whose
        `run.json` is missing or corrupt must not hand out a complete-looking
        index to a consumer that reads nothing else.
        """
        self._run_authority()
        value = _read_json(self.resolve(self.index_path(stage)))
        if not isinstance(value, dict):
            raise SchemaRefusal("a derived stage index is not an object")
        return value

    def manifest_agrees_with_disk(self, stage: str) -> bool:
        """True when the stored manifest still describes what the tree holds."""
        stored_path = self.resolve(self.manifest_path(stage))
        if not stored_path.exists():
            return False
        return _read_json(stored_path) == self.build_manifest(stage)

    def inventory_scope(self) -> tuple[str, ...]:
        """Every path prefix this store is able to write.

        Every managed path any code writes must fall inside this scope; a static
        test beside this module reads the writers from source and compares.  That
        includes `<stage>/serving-logs/`, written by the serving launcher, which
        `fetch-run` would otherwise refuse.
        """
        prefixes = [RUN_FILE, f"{RECEIPTS_DIR}/", RECENSOR_PARTITION_RECEIPT_FILE]
        for directory in sorted(set(_all_writing_directories())):
            prefixes.append(f"{directory}/{ARTIFACTS_DIR}/")
            prefixes.append(f"{directory}/{BLOBS_DIR}/")
            prefixes.append(f"{directory}/{MANIFEST_FILE}")
            prefixes.append(f"{directory}/{INDEX_FILE}")
            # In scope, never inventoried as evidence: nothing records its digest.
            prefixes.append(f"{directory}/{SERVING_LOGS_DIR}/")
        prefixes.append(f"{writing_directory(DOOR)}/{DOOR_MANIFEST_FILE}")
        return tuple(prefixes)


def _all_writing_directories() -> list[str]:
    from common.contracts.stages import WRITING_DIRECTORIES

    return list(WRITING_DIRECTORIES.values())


@contextmanager
def _run_creation_lock(parent: Path) -> Iterator[None]:
    """Serialize run creation without trusting a writable lock-file name."""
    directory = getattr(os, "O_DIRECTORY", None)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if directory is None or no_follow is None:  # pragma: no cover - supported stores are POSIX
        raise SchemaRefusal(
            "this platform cannot lock the run root through a no-follow directory descriptor"
        )
    try:
        descriptor = os.open(parent, os.O_RDONLY | directory | no_follow)
    except OSError as error:
        raise SchemaRefusal("the requested run root could not be locked safely") from error
    try:
        try:
            import fcntl
        except ImportError as error:  # pragma: no cover - supported stores are POSIX
            raise SchemaRefusal("this platform cannot serialize run creation") from error
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _is_full_commit(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _existing_partition_receipt(target: Path, relative: str) -> dict[str, Any] | None:
    """The stored partition receipt, or `None` when it is unreadable or invalid.

    The receipt is derived, so damage must not block rebuilding it; but the
    damage is reported, because the next write erases the only trace of it.
    `TypeError` and `RecursionError` are how strict canonicalization and a deeply
    nested file refuse; `_read_json` already translates `OSError` and `ValueError`.
    """
    from common.recensor_receipt import validate_recensor_partition_receipt

    try:
        return validate_recensor_partition_receipt(_read_json(target))
    except (ContractError, TypeError, RecursionError) as error:
        print(
            f"warning: the existing Recensor partition receipt at {relative} could "
            f"not be read as a valid receipt and is being replaced "
            f"({type(error).__name__}: {error}). This means a previous write did "
            f"not complete; the receipt is derived and is being rebuilt, but the "
            f"interruption itself is worth investigating.",
            file=sys.stderr,
        )
        return None


def _verify_compatible_reuse(tree: RunTree, run_id: str, authority: dict[str, Any]) -> None:
    existing = tree.read_run()
    # `.get`, not `[...]`: a run.json missing a bound field must become a named
    # refusal instead of a KeyError traceback.
    if existing.get("schema") != authority["schema"]:
        raise IncompatibleReuse(
            f"run {run_id!r} was written under schema {existing.get('schema')!r} and this is "
            f"{authority['schema']!r}; the two describe different shapes and cannot share a tree"
        ) from None
    optional_bound_fields = tuple(
        field
        for field in (_INGRESS_FIELD, _RENDER_SETTINGS_FIELD, _SEALED_CONFIG_DIGESTS_FIELD)
        if field in authority or field in existing
    )
    differing = [
        field
        for field in _BOUND_FIELDS + optional_bound_fields
        if field not in existing or existing[field] != authority.get(field)
    ]
    if differing:
        raise IncompatibleReuse(
            f"run {run_id!r} already exists and is bound to different {', '.join(differing)}; "
            "a run id names one set of inputs and one configuration, so this is a different "
            "run wearing an old name. Nothing was written"
        ) from None


def _verify_register_snapshot_present(tree: RunTree, digest: str, expected: bytes) -> None:
    relative = tree.blob_path(DOOR, digest)
    try:
        observed = tree.read_bytes(relative)
    except OSError as error:
        raise IncompatibleReuse(
            "run.json seals a corpus-register snapshot that is missing or unreadable; an "
            "existing run's immutable evidence is refused rather than silently reconstructed"
        ) from error
    if observed != expected:
        raise IncompatibleReuse(
            "run.json seals corpus-register snapshot bytes that no longer match the accepted "
            "reuse input"
        )


def _atomic_write(target: Path, data: bytes) -> None:
    """Temp file in the same directory, flushed, replaced, then the directory synced.

    Same directory because os.replace is only atomic within one filesystem;
    both the file and its directory entry are synced, so the publication
    survives power loss, not only process death.
    """
    temporary = _write_temporary(target, data)
    try:
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    _sync_published_name(target)


def _atomic_create(target: Path, data: bytes) -> None:
    """Publish immutable bytes only when their final name does not yet exist.

    A hard link is an atomic create: the synced temporary either takes the
    final name or raises ``FileExistsError`` without touching the other
    writer's bytes.  So the run root must be on a hard-link-capable filesystem
    (not exFAT, FAT32 or some network and bind mounts), refused by name
    otherwise.  ``O_EXCL`` is no substitute: it names the file before its bytes
    are in it.
    """
    temporary = _write_temporary(target, data)
    try:
        try:
            os.link(temporary, target)
        except OSError as error:
            if error.errno in _NO_HARD_LINKS:
                raise SchemaRefusal(
                    f"the run root at {target.parent} is on a filesystem that refuses hard "
                    f"links ({error.strerror}); artifacts are published by atomic link so "
                    "that a partly written file can never take its final name, and the run "
                    "root has to be on a filesystem that supports it"
                ) from error
            raise
    finally:
        if temporary.exists():
            temporary.unlink()
    _sync_published_name(target)


def _sync_published_name(target: Path) -> None:
    """Persist the directory entry a publication just created, or refuse by name.

    Strict, unlike the pod-side records: this tree is the evidence, and a
    resume trusts what a publish reported.  The bytes are already published
    when this refuses, so the refusal says so; the repair is to move the run root.
    """

    try:
        sync_directory(target.parent, strict=True)
    except OSError as error:
        raise SchemaRefusal(
            f"the run root at {target.parent} is on a filesystem that will not persist a "
            f"directory entry ({error.strerror}); {target.name} is published but its name "
            "is not proved to survive a power loss, and the run root has to be on a "
            "filesystem that supports it"
        ) from error


def _write_temporary(target: Path, data: bytes) -> Path:
    """Write one unique, synced same-directory temporary and return its path."""
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{target.name}.tmp-", dir=target.parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
    return temporary


@contextmanager
def _naming(relative_path: str) -> Iterator[None]:
    """Add the evidence path while preserving the refusal's concrete class.

    Envelope and identity validators see decoded content, not its filename, but
    the operator-facing repair instruction requires the offending path. Callers
    may catch a specific `SchemaRefusal` subclass, so wrapping must not widen it.
    """

    try:
        yield
    except SchemaRefusal as error:
        message = str(error)
        if message.startswith(f"{relative_path}: "):
            raise
        raise type(error)(f"{relative_path}: {message}") from error


def _read_bytes_bounded(path: Path, *, max_bytes: int | None = None) -> bytes:
    """Read one file with a hard byte ceiling instead of `Path.read_bytes()`'s none.

    Checked before and after the read, because a file can grow in between.
    `max_bytes` defaults to the blob ceiling, read at call time so a test can
    monkeypatch it.  A missing or unreadable file still raises the `OSError`
    subclass `Path.read_bytes()` would, which callers convert to their own
    refusals; only the ceiling raises `SchemaRefusal`.
    """
    if max_bytes is None:
        max_bytes = _MAX_TREE_READ_BYTES
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if size > max_bytes:
            raise SchemaRefusal(
                f"{path} is {size} bytes, above the {max_bytes}-byte tree read limit"
            )
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SchemaRefusal(f"{path} grew above the {max_bytes}-byte tree read limit while read")
    return data


def _read_json_with_bytes(path: Path) -> tuple[Any, bytes]:
    # `RecursionError` too: json's scanner recurses per nesting level, and a
    # deeply nested file must be refused, not a traceback.  Every path here is a
    # record, so the record ceiling applies.
    try:
        data = _read_bytes_bounded(path, max_bytes=MAX_RECORD_READ_BYTES)
        return json.loads(data.decode("utf-8")), data
    except (OSError, ValueError, RecursionError) as error:
        raise SchemaRefusal(f"{path} could not be read as an artifact: {error}") from error


def _decode_json_bytes(data: bytes, path: Path) -> Any:
    """Decode an already-read artifact snapshot under the store's named refusal."""
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise SchemaRefusal(f"{path} could not be read as an artifact: {error}") from error


def _read_json(path: Path) -> Any:
    return _read_json_with_bytes(path)[0]


def _inode_identity(value: os.stat_result) -> tuple[int, int]:
    """The filesystem identity a spelling cannot forge by sharing a prefix."""
    return value.st_dev, value.st_ino


def _default_corpus_frame_membership(source_manifest: list[dict[str, Any]]) -> dict[str, str]:
    """Derive the frame from inspected digests, falling back to declarations.

    Unreadable sources retain their declared digest and ordinal so they remain
    in the denominator; a page with neither digest cannot identify a frame.
    Gold re-derives the frame with the same precedence, so the two boundaries
    must continue to use the same source field.
    """
    pages = []
    for page in sorted(source_manifest, key=lambda page: page.get("ordinal", 0)):
        computed = page.get("computed_sha256")
        if computed is None:
            computed = page.get("sha256")
        if not is_sha256(computed):
            raise SchemaRefusal(
                "corpus frame membership needs an inspected or declared sha256 for "
                "every source page"
            )
        pages.append({"ordinal": page.get("ordinal"), "sha256": computed})
    page_digest = digest_bytes(canonical_bytes(pages))
    return {
        "frame_digest": digest_bytes(canonical_bytes({"pages": pages})),
        "page_digest": page_digest,
        "seed": digest_bytes(canonical_bytes({"page_digest": page_digest, "purpose": "frame"})),
    }


def _validate_corpus_frame_membership(membership: Any) -> None:
    if not isinstance(membership, dict) or set(membership) != {
        "frame_digest",
        "page_digest",
        "seed",
    }:
        raise SchemaRefusal(
            "corpus_frame_membership must be the closed {frame_digest, page_digest, seed} record"
        )
    for field, value in membership.items():
        if not is_sha256(value):
            raise SchemaRefusal(f"corpus_frame_membership.{field} is not a sha256 digest")


def _receipt_reference(value: RunReceiptReference | dict[str, str]) -> RunReceiptReference:
    if isinstance(value, RunReceiptReference):
        return value
    reference = digest_ref(value, "run receipt reference")
    return RunReceiptReference(reference["relative_path"], reference["sha256"])


def _approval_record_reference(value: ApprovalRecordReference) -> ApprovalRecordReference:
    """Validate the typed boundary before using an approval receipt reference."""
    if not isinstance(value, ApprovalRecordReference):
        raise ApprovalRefusal(
            "approval record reference must be an ApprovalRecordReference, not an untyped record"
        )
    if not isinstance(value.relative_path, str) or not value.relative_path:
        raise ApprovalRefusal("approval record reference has no relative_path")
    if not is_sha256(value.sha256):
        raise ApprovalRefusal("approval record reference has no lowercase sha256")
    return value


def _refuse_path_component(value: Any, what: str) -> None:
    if not isinstance(value, str) or not value:
        raise SchemaRefusal(f"{what} is empty")
    if "/" in value or "\\" in value or value in (".", "..") or value.startswith("."):
        raise SchemaRefusal(
            f"{what} {value!r} is not a single plain path component; a stage that "
            "can name a directory can write outside its own"
        )
