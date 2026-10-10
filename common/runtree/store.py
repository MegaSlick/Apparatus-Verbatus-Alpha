"""The run tree: immutable artifacts, atomic publication, and honest reuse.

The tree is the evidence. Everything a stage learned is a file in it, and the whole
of resume, rerun, and accounting rests on three properties this module is
responsible for:

  Artifacts are immutable.   Once published, bytes never change. A second publish
                             of identical bytes is a no-op that reports `reused`; a
                             second publish of *different* bytes under the same
                             identity is refused: the existing file is not
                             touched and nothing is left behind.
  Publication is atomic.     An artifact, blob, receipt or run.json is written to a
                             synced temporary file in the same directory and
                             hard-linked to its unused name; manifests, indexes and
                             the partition receipt are replaced with os.replace. A
                             crash never leaves a half-written file that a resume
                             would trust.
  Manifests are rebuildable. manifest.json is an inventory derived from the
                             artifacts on disk, never the only evidence that
                             something happened. Delete it and it comes back
                             identical; disagree with it and the artifacts win.

`run.json` is the immutable authority for what this run *is*: its source pages,
its configured witness chairs, its configuration digest, its adapter recipes. It
deliberately does not predeclare acts — the Perlector's page readings count them,
because acts are discovered and pages are given.

Reusing a run id whose source, configuration, or adapter recipes have changed fails
before any write: that is the difference between a resumed run and a corrupted one.

`receipts/sha256/` is the one thing here that is not a stage artifact. Serving receipts
and approval records both carry a real moment, so publishing either one as a stage
artifact would make a repeat of the identical command change bytes. They are written
content-addressed under the run root, outside every stage's directory and out of every
stage manifest, and a stage payload carries only its digest-checked reference plus the
immutable facts it needs.
"""

import errno
import json
import os
import stat
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from common import armarium_formats
from common.chairs.models import is_hf_revision
from common.chairs.receipts import receipt_record, validate_receipt
from common.contracts.approval import (
    SCHEMA_V1,
    ApprovalRecordReference,
    parse_ingress_record,
    validate_approval_record,
)
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
    portable_spelling,
    read_verified,
    validate_envelope,
    validate_input_refs,
)
from common.contracts.errors import (
    ApprovalRefusal,
    ContractError,
    IncompatibleReuse,
    SchemaRefusal,
)
from common.contracts.identities import validate_run_id
from common.contracts.stages import ARMARIUM, DOOR, writing_directory
from common.corpus_register import empty_register, validate_register_bytes
from common.durability import (
    HardLinkUnsupported,
    PublishedUnsettled,
    atomic_create,
    atomic_replace,
    is_temporary_name,
    is_unpublished_blob_temporary,
)
from common.sealed_config import SEAL_METHOD, SEAL_METHOD_FIELD, require_seal_method

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
# Beside a stage's engine logs, one empty note per launch audit the stage stored,
# named by the audit blob's digest, so a watcher finds the launches without reading
# the stage's other blobs. Operational, like the logs: never inventoried as evidence.
LAUNCH_AUDIT_NOTE_PREFIX: Final = "launch-audit-"

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
# `operations/operator/test_surface.py` pins that ordering.  The Armarium's blob
# directory holds only the export archive, which is read under its own limit,
# `MAX_EXPORT_ARCHIVE_BYTES` in `common/armarium_formats.py`.
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
# A replay run's source (`common.replay`): which run's stages it imports as sealed
# and answers its model calls from. Bound, so no other run may be opened under it.
_REPLAY_FIELD: Final = "replay"


class PublishResult:
    """What happened when an artifact was published, so callers can say so.

    `reused` is the interesting one: it is how a resumed run proves it did not
    rewrite work it had already done.
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
        # The run id and stages a replay run imports from its source, once read.
        self._imports: tuple[str | None, frozenset[str]] | None = None

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
        # Checked as every later read checks them, so a sealed authority cannot
        # carry a value that leaves the run unreadable.
        if not is_sha256(config_digest):
            raise SchemaRefusal(
                "a run's config_digest must be a lowercase sha256; every artifact is bound "
                "to the run through it, so any other value leaves nothing readable"
            )
        if ingress is not None:
            parse_ingress_record(ingress)
        # A chair is named once, so a roster cannot count one reader twice.
        if (
            not isinstance(witness_chairs, (list, tuple))
            or any(not isinstance(chair, str) or not chair for chair in witness_chairs)
            or len(set(witness_chairs)) != len(witness_chairs)
        ):
            raise SchemaRefusal("witness_chairs must be a list of distinct, non-empty chair names")
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
            authority[SEAL_METHOD_FIELD] = SEAL_METHOD
        if repository_commit is not None:
            if not is_hf_revision(repository_commit):
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
                _verify_register_snapshot_present(tree, snapshot_digest)
                return tree
            tree.put_blob(DOOR, snapshot)
            try:
                _atomic_create(run_file, canonical_bytes(authority))
            except FileExistsError:
                # A writer ignoring the advisory lock won the race; its authority
                # must pass the same reuse check.
                _verify_compatible_reuse(tree, run_id, authority)
                _verify_register_snapshot_present(tree, snapshot_digest)
        return tree

    @classmethod
    def create_replay(
        cls,
        root: Path,
        run_id: str,
        *,
        source: Mapping[str, Any],
        replay: Mapping[str, Any],
        repository_commit: str,
    ) -> "RunTree":
        """Open a new run that replays `source`, a run authority already read and verified.

        The authority is the source's own, with this run's id, the commit of the code
        that replays, and the `replay` block naming the source; everything a stage is
        bound to (pages, configuration, chairs, register) stays the source's, so the
        stages it imports read as sealed. The run directory must not exist yet: a
        replay is always a new run, never written into one.
        """
        from common.replay import validate_replay_block

        tree = cls(root, run_id)
        block = validate_replay_block(replay)
        if (
            source.get("run_id") != block["source_run_id"]
            or source.get("self_hash") != block["source_run_sha256"]
        ):
            raise SchemaRefusal("a replay block must name the run authority it replays")
        if _REPLAY_FIELD in source:
            raise SchemaRefusal(
                f"run {source.get('run_id')!r} is itself a replay; replay the run it names"
            )
        if tree.run_id == block["source_run_id"]:
            raise SchemaRefusal("a replay is a new run, so it takes a run id of its own")
        if not is_hf_revision(repository_commit):
            raise SchemaRefusal(
                "a replay's repository_commit must be forty lowercase hexadecimal characters"
            )
        authority = {
            key: value for key, value in source.items() if key not in ("self_hash", "run_id")
        }
        authority["run_id"] = tree.run_id
        authority[_REPOSITORY_COMMIT_FIELD] = repository_commit
        authority[_REPLAY_FIELD] = block
        authority["self_hash"] = self_hash(authority)
        if tree.root.exists():
            raise IncompatibleReuse(
                f"{tree.root} already exists; a replay writes a new run and never into one"
            )
        tree.root.parent.mkdir(parents=True, exist_ok=True)
        with _run_creation_lock(tree.root.parent):
            tree.root.mkdir(parents=False, exist_ok=False)
            tree._bind_root_identity()
            _atomic_create(tree.root / RUN_FILE, canonical_bytes(authority))
        return tree

    def holds_run_id(self, run_id: Any, stage: Any) -> bool:
        """Whether a record of `stage` naming run `run_id` belongs to this run.

        A run's own records name its id. A replay run also holds the records of
        the stages it imported from its source, which keep the source's id.
        """
        if run_id == self.run_id:
            return True
        source_id, stages = self._replay_imports()
        return source_id is not None and run_id == source_id and stage in stages

    def _replay_imports(self) -> tuple[str | None, frozenset[str]]:
        if self._imports is None:
            block = self.read_run().get(_REPLAY_FIELD)
            if block is None:
                self._imports = (None, frozenset())
            else:
                from common.replay import validate_replay_block

                checked = validate_replay_block(block)
                self._imports = (checked["source_run_id"], frozenset(checked["imported_stages"]))
        return self._imports

    def read_run(self) -> dict[str, Any]:
        """The run authority, refused unless its self-hash verifies its current contents."""
        run_file = self.root / RUN_FILE
        if not run_file.exists():
            raise IncompatibleReuse(
                f"no {RUN_FILE} under {self.root}: there is no run here to read, and "
                "a stage that wrote into one anyway would be writing into nothing"
            )
        record = _read_json(run_file)
        if not isinstance(record, dict):
            raise IncompatibleReuse(
                f"{run_file} is not a JSON object, so it is no run authority and nothing "
                "in this tree can be trusted against it"
            )
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
        from `writing_directory` alike; a directory outside that scope makes
        `fetch-run` refuse the whole tree.
        """
        return f"{writing_directory(stage)}/{SERVING_LOGS_DIR}"

    def note_launch_audit(self, stage: str, digest: str) -> None:
        """Name a launch audit blob of `stage` in its serving-logs directory.

        The note is empty: its name carries the digest, and the blob itself is the
        content-addressed record. An identical note is reused.
        """
        _refuse_path_component(digest, "blob digest")
        self._publish_bytes(
            f"{self.serving_log_path(stage)}/{LAUNCH_AUDIT_NOTE_PREFIX}{digest}", b""
        )

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
                f"{relative_path!r} could not be resolved inside the run tree: "
                f"{type(error).__name__}"
            ) from error
        # Not a string prefix, which would accept the sibling `.../r1-scratch`.
        if not resolved.is_relative_to(self.root):
            raise SchemaRefusal(f"{relative_path!r} resolves outside the run tree")
        return resolved

    # --- Publication -----------------------------------------------------------

    def publish_artifact(self, envelope: dict[str, Any]) -> PublishResult:
        """Publish one artifact. Immutable, atomic, and honest about reuse.

        Refused unless every read route would accept it: an immutable artifact that
        readers refuse could never be replaced, and its stage could never seal.
        """
        validate_envelope(envelope)
        self._verify_artifact_run(envelope)
        self._verify_artifact_inputs(envelope)
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
        write wins.  The race is bounded, not fixed.  `expected_unit_count` is
        sealed by the Perlector's page readings before the Recensor runs, so a
        write changing it is refused when its `pages` name the same
        `reading_ref` for every page, in the same order, as the receipt on disk.
        A write that names another reading for some page (an operator re-read,
        `common.page_path`, makes a new current reading) may change the count;
        which reading is current is the page-read denominator's to prove, not
        this store's.  A stale write can under-state completeness
        but never claim it, because the reviews it cites are append-only and a
        unit's class only moves toward resolution.  Concurrent Recensor passes are still unsafe.
        """
        from common.recensor_receipt import expected_count, validate_recensor_partition_receipt

        checked = validate_recensor_partition_receipt(record)
        if checked["run_id"] != self.run_id or checked["config_digest"] != self._run_authority():
            raise SchemaRefusal("Recensor partition receipt does not belong to this run authority")
        relative = self.recensor_partition_receipt_path()
        target = self.resolve(relative)
        data = canonical_bytes(checked)
        if target.exists():
            existing = _existing_partition_receipt(target)
            if existing is not None and (
                existing["run_id"] == checked["run_id"]
                and existing["config_digest"] == checked["config_digest"]
                and expected_count(existing) != expected_count(checked)
                and _page_readings_bound(existing) == _page_readings_bound(checked)
            ):
                raise SchemaRefusal(
                    "Recensor partition receipt would change its expected_unit_count from "
                    f"{expected_count(existing)} to {expected_count(checked)} under "
                    "the same run authority over the same page readings; the unit "
                    "denominator is sealed by the readings and cannot differ between two "
                    "passes over them"
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(relative, data)
        return PublishResult(relative, reused=False)

    def read_recensor_partition_receipt(self) -> dict[str, Any]:
        """Read the current derived receipt only when it binds to this run."""
        from common.recensor_receipt import validate_recensor_partition_receipt

        path = self.resolve(self.recensor_partition_receipt_path())
        record = _read_json(path)
        checked = validate_recensor_partition_receipt(record)
        if checked["run_id"] != self.run_id or checked["config_digest"] != self._run_authority():
            raise SchemaRefusal("Recensor partition receipt does not belong to this run authority")
        return checked

    def read_run_receipt(self, reference: RunReceiptReference | dict[str, str]) -> dict[str, Any]:
        """Read a receipt only when both its reference and bytes still verify.

        Three checks, because each catches a different lie: the path must be the
        one its own digest names, the bytes there must hash to that digest, and
        the record must still be a whole receipt, since tampered or wrong-schema
        provenance is refused, never repaired.
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
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
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
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise ApprovalRefusal(
                f"approval record {parsed.relative_path} could not be read: {error}"
            ) from error
        return validate_approval_record(decoded)

    def approval_records(self) -> list[tuple[ApprovalRecordReference, dict[str, Any]]]:
        """Every approval record stored in this run, each with its checked reference, in path order.

        Approvals share `receipts/sha256/` with serving receipts. Every file
        there is checked before anything is read from it: its name must be the
        sha256 of its bytes, and its record must be a sound approval record,
        stored as its canonical bytes, or a sound serving receipt. So an edited
        or hand-written approval is
        refused, never skipped as something else, and a valid serving receipt
        is passed over. A name that is no digest is refused even in a run with
        no approval, because only the content-addressed writers write here and
        a renamed record would otherwise vanish from every reader. Only an
        unfinished write's temporary (`common.durability.is_temporary_name`) is
        passed over; any other dot-file is refused like any other stray name.
        """
        directory = self.resolve(RECEIPTS_DIR)
        if not directory.is_dir():
            return []
        found = []
        for path in sorted(directory.iterdir()):
            if is_temporary_name(path.name):
                continue
            relative = f"{RECEIPTS_DIR}/{path.name}"
            digest = path.name.removesuffix(".json")
            if not path.name.endswith(".json") or not is_sha256(digest):
                raise SchemaRefusal(f"{relative} is not a content-addressed receipt")
            data = self._read_receipt_bytes(
                relative, digest, SchemaRefusal, label="receipt", reference_label="receipt"
            )
            try:
                decoded = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as error:
                raise SchemaRefusal(f"receipt {relative} could not be read: {error}") from error
            try:
                record = validate_approval_record(decoded)
            except ApprovalRefusal as refusal:
                try:
                    validate_receipt(decoded)
                except ContractError:
                    raise ApprovalRefusal(
                        f"{relative} is neither a sound approval record nor a sound serving "
                        f"receipt: {refusal}"
                    ) from refusal
                continue
            # Its writer stores canonical bytes, so a reader may cite a decision by
            # the digest of its canonical form and name the same file.
            if canonical_bytes(record) != data:
                raise ApprovalRefusal(
                    f"approval record {relative} is not stored as its canonical bytes"
                )
            found.append((ApprovalRecordReference(relative, digest), record))
        return found

    def review_decision_records(self) -> list[tuple[ApprovalRecordReference, dict[str, Any]]]:
        """Every operator review decision stored in this run: the `approval-record.v1` among
        `approval_records`, so each is digest- and self-hash-checked before it is returned."""
        return [
            (reference, record)
            for reference, record in self.approval_records()
            if record["schema"] == SCHEMA_V1
        ]

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
        ref = {"relative_path": relative_path, "sha256": sha256}
        return read_verified(self._read_record_bytes, ref, label, refusal)

    def _publish_bytes(self, relative: str, data: bytes) -> PublishResult:
        self._require_inventory_path(relative)
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

    def _atomic_write(self, relative: str, data: bytes) -> None:
        self._require_inventory_path(relative)
        _replace_file(self.resolve(relative), data)

    def _require_inventory_path(self, relative: str) -> None:
        if not any(
            relative.startswith(prefix) if prefix.endswith("/") else relative == prefix
            for prefix in self.inventory_scope()
        ):
            raise SchemaRefusal(f"{relative!r} is outside this run tree's inventory scope")

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
        data = read_verified(self._read_record_bytes, reference, "referenced artifact")
        try:
            record = validate_envelope(json.loads(data.decode("utf-8")))
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
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

        The Armarium's blobs (its only blob is the export archive) are read
        under the export archive limit instead.

        A caller that knows it is reading a JSON record rather than a page blob
        passes `max_bytes=MAX_RECORD_READ_BYTES`, so the bytes it is about to
        hand to `json.loads` -- which costs several times their size again in
        parsed objects -- are bounded by what a record can legitimately be and
        not by what an image can.
        """
        path = self.resolve(relative_path)
        if max_bytes is None:
            # The Armarium stores no blob but its export archive (its `run.py`);
            # any other blob put there would be read under the archive limit too.
            archive_blobs = self.root / writing_directory(ARMARIUM) / BLOBS_DIR
            max_bytes = (
                armarium_formats.MAX_EXPORT_ARCHIVE_BYTES
                if path.parent == archive_blobs
                else _MAX_TREE_READ_BYTES
            )
        return _read_bytes_bounded(path, max_bytes=max_bytes)

    def _read_record_bytes(self, relative_path: str) -> bytes:
        """`read_bytes` under the record ceiling, for bytes about to be decoded as JSON."""
        return self.read_bytes(relative_path, max_bytes=MAX_RECORD_READ_BYTES)

    def has_artifact(self, stage: str, kind: str, artifact_id: str) -> bool:
        return self.resolve(self.artifact_path(stage, kind, artifact_id)).exists()

    # --- Manifests: derived, never the only evidence ---------------------------

    def build_manifest(
        self,
        stage: str,
        *,
        verify_inputs: bool = True,
        verified_inputs: set[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Walk the stage's artifacts and describe what is actually there.

        Derived from the tree every time it is called, so it cannot drift from
        what the tree holds. That is why a manifest is never evidence on its own:
        if it disagrees with the artifacts, the artifacts are right and the
        manifest was stale.

        ``verify_inputs=False`` skips only ``_verify_artifact_inputs``, so an
        artifact whose upstream blob changed is still listed.  It is for a caller
        whose own boundary owns lineage, never for speed.

        ``verified_inputs`` shares checked references among the stage manifests
        of one tally. Without it, each build checks its own references anew.
        """
        # An empty directory must not make a missing run authority look like an empty run.
        self._run_authority()
        if verified_inputs is None:
            verified_inputs = set()
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
                    self._verify_artifact_inputs(record, verified_inputs=verified_inputs)
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

        Publisher temporaries are not inventory members. Blob contents are
        verified when consumed because they may be full page images.
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
                    if name != name.casefold() and is_sha256(name.casefold()):
                        raise SchemaRefusal(
                            f"{relative_path!r} is a non-canonical case variant of a sha256"
                        )
                    if not is_unpublished_blob_temporary(name):
                        raise SchemaRefusal(f"{relative_path!r} has a noncanonical content address")
                    if not stat.S_ISREG(before.st_mode):
                        raise SchemaRefusal(f"{relative_path!r} is not a regular blob temporary")
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
        os.close(self._open_root_fd())

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
            spelling = portable_spelling(name)
            collision = folded.get(spelling)
            if collision is not None and collision != name:
                raise SchemaRefusal(
                    f"{relative_directory!r} contains names {collision!r} and {name!r} that "
                    "differ only in case or Unicode normalisation; default APFS stores them "
                    "as one name, so this inventory cannot preserve both"
                )
            folded[spelling] = name
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
        resolved = self.resolve(relative_path)
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
        if not self.holds_run_id(record["run_id"], record["stage"]):
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

    def _verify_artifact_inputs(
        self,
        record: dict[str, Any],
        *,
        verified_inputs: set[tuple[str, str]] | None = None,
    ) -> None:
        """Verify each direct input before a consumer may reinterpret this artifact.

        Input references form the handoff chain.  Validating only their shape at
        publication lets an input be edited later and every downstream manifest
        still look complete; its recorded digest is useful only if a reader
        checks it against the bytes again.
        """
        for reference in record["inputs"]:
            key = (reference["relative_path"], reference["sha256"])
            if verified_inputs is not None and key in verified_inputs:
                continue
            read_verified(self.read_bytes, reference, "artifact input")
            if verified_inputs is not None:
                verified_inputs.add(key)

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
        self._atomic_write(relative, canonical_bytes(manifest))
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
        self._atomic_write(relative, data)
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

        Writers call ``_require_inventory_path`` before publication. That
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


def _page_readings_bound(receipt: Mapping[str, Any]) -> list[Any]:
    """The page readings a partition receipt binds, in page order: what its count is of."""
    rows = receipt.get("pages") or receipt.get("page_reading_refs") or []
    return [row.get("reading_ref") for row in rows if isinstance(row, Mapping)]


def _existing_partition_receipt(target: Path) -> dict[str, Any] | None:
    """The stored derived receipt, or `None` when it cannot be validated."""
    from common.recensor_receipt import validate_recensor_partition_receipt

    try:
        return validate_recensor_partition_receipt(_read_json(target))
    except (ContractError, TypeError, RecursionError):
        return None


def _verify_compatible_reuse(tree: RunTree, run_id: str, authority: dict[str, Any]) -> None:
    # `read_run` has already refused an authority under another schema.
    existing = tree.read_run()
    if _SEALED_CONFIG_DIGESTS_FIELD in existing:
        require_seal_method(existing, f"run {run_id!r}")
    optional_bound_fields = tuple(
        field
        for field in (
            _INGRESS_FIELD,
            _RENDER_SETTINGS_FIELD,
            _SEALED_CONFIG_DIGESTS_FIELD,
            SEAL_METHOD_FIELD,
            _REPLAY_FIELD,
        )
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


def _verify_register_snapshot_present(tree: RunTree, digest: str) -> None:
    what = "run.json's sealed corpus-register snapshot"
    ref = {"relative_path": tree.blob_path(DOOR, digest), "sha256": digest}
    read_verified(tree.read_bytes, ref, what, IncompatibleReuse)


def _replace_file(target: Path, data: bytes) -> None:
    with _run_root_refusals(target):
        atomic_replace(target, data)


def _atomic_create(target: Path, data: bytes) -> None:
    with _run_root_refusals(target):
        atomic_create(target, data)


@contextmanager
def _run_root_refusals(target: Path) -> Iterator[None]:
    """Name the run root's filesystem, not the syscall, when it cannot hold the evidence."""
    try:
        yield
    except HardLinkUnsupported as error:
        raise SchemaRefusal(error.strerror) from error
    except PublishedUnsettled as error:
        raise SchemaRefusal(
            f"the run root at {target.parent} is on a filesystem that will not persist a "
            f"directory entry ({error.strerror}); {target.name} is in the run root but its "
            "name is not proved to survive a power loss, and the run root has to be on a "
            "filesystem that supports it"
        ) from error


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
    monkeypatch it.  Opened without blocking or following a final link (callers
    pass paths `resolve` already checked), and anything but a regular file is
    an `OSError`: a FIFO would otherwise hang the read.
    A missing or unreadable file raises `OSError` too, which callers convert to
    their own refusals; only the ceiling raises `SchemaRefusal`.
    """
    if max_bytes is None:
        max_bytes = _MAX_TREE_READ_BYTES
    descriptor = os.open(path, _FILE_OPEN_FLAGS)
    with os.fdopen(descriptor, "rb") as handle:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise OSError(errno.EINVAL, f"{path.name} is not a regular file")
        size = status.st_size
        if size > max_bytes:
            raise SchemaRefusal(
                f"{path.name} is {size} bytes, above the {max_bytes}-byte tree read limit"
            )
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SchemaRefusal(
            f"{path.name} grew above the {max_bytes}-byte tree read limit while read"
        )
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
