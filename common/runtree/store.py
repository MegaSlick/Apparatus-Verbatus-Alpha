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

# At module level, not deferred inside the receipt-backed record methods. The
# dependencies are real — the store is the one writer and reader for these records,
# refusing invalid values at both ends — so they belong where a reader of the imports
# can see them. Neither contract imports this module, so there is no cycle to dodge,
# and a deferred import that exists only to hide a layer from the eye is a layer nobody
# can check.
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
from common.contracts.envelope import validate_envelope, validate_input_refs, verify_input_bytes
from common.contracts.errors import (
    ApprovalRefusal,
    ContractError,
    IncompatibleReuse,
    SchemaRefusal,
)
from common.contracts.identities import validate_run_id
from common.contracts.stages import DOOR, writing_directory
from common.corpus_register import empty_register, validate_register_bytes

RUN_FILE: Final = "run.json"
MANIFEST_FILE: Final = "manifest.json"
DOOR_MANIFEST_FILE: Final = "manifest-door.json"
INDEX_FILE: Final = "index.json"
ARTIFACTS_DIR: Final = "artifacts"
BLOBS_DIR: Final = "blobs/sha256"
RECEIPTS_DIR: Final = "receipts/sha256"
RECENSOR_PARTITION_RECEIPT_FILE: Final = "run-health/recensor-partition-receipt.json"

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

# What a filesystem that will not hard-link answers with. Named so `_atomic_create`
# can say which setup fact is wrong instead of letting a bare OSError about `link`
# escape as a traceback.
_NO_HARD_LINKS: Final = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS})
# A manifest walks files that may have been damaged or replaced outside the
# store. Bound both axes before accepting them as an inventory: one malformed
# artifact must not be able to allocate the process out of existence, and an
# attacker-created directory forest must not grow the walk without limit.
_MAX_MANIFEST_ARTIFACT_BYTES: Final = 64 * 1024 * 1024
_MAX_MANIFEST_WALK_ENTRIES: Final = 100_000
_DIRECTORY_OPEN_FLAGS: Final = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)
_FILE_OPEN_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
_RENDER_SETTINGS_FIELD: Final = "render_settings"
# The digest of each configuration file this run sealed, under the name its point
# of use asks for (`common/stage.py::require_sealed_config`). Recorded in the
# authority, not only folded into `config_digest`, so a reader holding the tree
# alone can *name* the policy bytes that governed the run instead of only being
# able to test a candidate file against a hash of everything at once.
_SEALED_CONFIG_DIGESTS_FIELD: Final = "sealed_config_digests"


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
        # Resolve the requested base separately from its run-id child.  A run-id
        # symlink must not redirect a new run outside the caller's approved root:
        # once that escape became ``self.root``, every later containment check
        # would faithfully protect the wrong tree.
        requested_root = Path(root).resolve()
        candidate = requested_root / self.run_id
        resolved = candidate.resolve()
        if not resolved.is_relative_to(requested_root):
            raise SchemaRefusal(
                "run id resolves outside the requested run root; a run tree may not be "
                "redirected through a symlink"
            )
        # Resolved once, here, so every later comparison is against one spelling.
        # `relative_to` is exact rather than semantic: on macOS a caller passing
        # `/tmp` produces the real `/private/tmp` spelling consistently.
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
    ) -> "RunTree":
        """Open a run, creating it if new and refusing an incompatible reuse.

        The refusal happens before any directory is created for a *new* run and
        before any artifact is touched for an existing one, so a rejected reuse
        leaves the tree exactly as it found it.
        """
        tree = cls(root, run_id)
        # Validated and digested here, but not *stored* until the run authority
        # has accepted it below. Storing it first would put a foreign register's
        # bytes into an existing run's blob store on the way to refusing that
        # very register as an incompatible reuse — writing into a tree while
        # telling the operator nothing was written.
        snapshot = empty_register() if register_bytes is None else register_bytes
        register_required = register_bytes is not None
        validate_register_bytes(snapshot)
        snapshot_digest = digest_bytes(snapshot)
        # An ordinal names one page. Two rows carrying the same one do not describe
        # a duplicate page — they make the run's page count ambiguous before
        # anything has been read. That matters because the Armarium's page census
        # reconciles itself against these ordinals *as a set*: a repeat silently
        # reduces two declared pages to one, so a run that lost one of them still
        # balances and still reports `complete`. Four reviewers filed the
        # lost-page defect this check's absence recreates one level down.
        #
        # Refused here, at the one place a manifest enters a run, rather than
        # defended at each of the places it is later read.
        ordinals = [page.get("ordinal") for page in source_manifest]
        if any(not isinstance(ordinal, int) or isinstance(ordinal, bool) for ordinal in ordinals):
            raise SchemaRefusal(
                "every source page must declare an integer ordinal: a run cannot "
                "account for pages it cannot count"
            )
        # And a page number, not merely an integer. Every producer in this tree
        # counts from one -- the Door's expansion increments before it assigns,
        # and the fixture declarations follow it -- so a zero or negative ordinal
        # is a manifest nothing here can legitimately have written. Sealed
        # unchecked it would enter the corpus frame membership and, through it,
        # gold's re-derivation of the same frame, giving the run a page numbering
        # that no other part of the system agrees with. Refused at the one place
        # a manifest enters a run, beside the checks above, rather than defended
        # wherever an ordinal is later read.
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
        authority["self_hash"] = self_hash(authority)

        run_file = tree.root / RUN_FILE
        tree.root.parent.mkdir(parents=True, exist_ok=True)
        # Creators serialize on the already-resolved parent directory itself, so
        # no predictable lock pathname is introduced.  A new run publishes the
        # immutable snapshot first and run.json last: a failed blob write can no
        # longer leave a trusted authority sealing evidence that never arrived.
        with _run_creation_lock(tree.root.parent):
            tree.root.mkdir(parents=True, exist_ok=True)
            # The root did not exist when __init__ resolved it, so bind device and
            # inode here, before any write goes through it. Every later descriptor
            # this tree opens is checked against the directory bound at this line.
            tree._bind_root_identity()
            if run_file.exists():
                _verify_compatible_reuse(tree, run_id, authority)
                _verify_register_snapshot_present(tree, snapshot_digest, snapshot)
                return tree
            tree.put_blob(DOOR, snapshot)
            try:
                _atomic_create(run_file, canonical_bytes(authority))
            except FileExistsError:
                # A non-cooperating writer can still race the advisory lock. Its
                # authority must pass the same complete reuse check before this
                # caller can proceed.
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
            # Bare run authority has no envelope boundary to name an unhashable
            # value; without a computed digest, this boundary cannot claim when
            # the malformed content arose.
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
        # Door and Exemplar share their evidence directory, but their manifests
        # are producer inventories. Giving Door a stage-qualified filename
        # preserves both inventories, which matters when either one names a
        # completion seal that later disappears. A shared manifest would let an
        # Exemplar write erase the Door's deletion trigger.
        filename = DOOR_MANIFEST_FILE if stage == DOOR else MANIFEST_FILE
        return f"{writing_directory(stage)}/{filename}"

    def index_path(self, stage: str) -> str:
        """The stage-local, rebuildable derived index path.

        An index has the same standing as a manifest: an inventory made from
        immutable artifacts, never the evidence that an artifact exists. The
        store owns the path and the atomic rewrite so stage code cannot invent
        an untracked side file beside the evidence it summarizes.

        Door and Exemplar share one physical directory (`writing_directory`), so
        `index_path(DOOR)` and `index_path(EXEMPLAR)` collide. Their producer
        manifests have stage-qualified paths, but an index holds whatever its
        caller builds with no `build_manifest`-style stage filter — so
        `write_index` refuses any stage whose directory has more than one
        producer, rather than letting the second stage's index silently erase
        the first stage's rows.
        """
        return f"{writing_directory(stage)}/{INDEX_FILE}"

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
            # `Path.resolve` reports a filesystem-level symlink loop as a
            # RuntimeError (and some platforms report an OSError), and rejects
            # paths the OS cannot represent with ValueError, before the containment
            # comparison below can run. Those are still paths the run tree cannot
            # safely resolve, not interpreter failures a stage should surface as
            # tracebacks.
            raise SchemaRefusal(
                f"{relative_path!r} could not be resolved inside the run tree: {error}"
            ) from error
        # is_relative_to, not a string prefix: with a root of `.../r1`, a prefix
        # test would happily accept the sibling directory `.../r1-scratch`.
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

        Receipts are intentionally non-deterministic records of a serving moment,
        so this writer never reaches ``publish_artifact`` or a stage manifest. The
        record is canonical and content-addressed, making an identical write reuse
        its bytes while a different receipt receives its own immutable reference.

        The store validates on the way in and again on the way out, for the same
        reason `build_envelope` does: an invalid record that reached the tree has
        already lost the moment it described, and finding out at the reader means
        the evidence of what went wrong is a stage away.
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

        An approval records a human act at a moment, so it has the same receipt
        shape as a serving receipt rather than a stage artifact.  The contract
        validator checks both the declared approval-record schema and its own
        hash before any path is made; canonical bytes and the existing immutable
        writer then make an identical record reuse its receipt and a changed one
        receive a different content-addressed reference.
        """
        validated = validate_approval_record(record)
        data = canonical_bytes(validated)
        digest = digest_bytes(data)
        result = self._publish_bytes(self.receipt_path(digest), data)
        return ApprovalRecordReference(result.relative_path, digest), result

    def write_recensor_partition_receipt(self, record: dict[str, Any]) -> PublishResult:
        """Atomically replace the derived current Recensor partition receipt.

        Unlike an artifact, the receipt legitimately changes after a bounded
        recovery: the immutable review and request evidence stays beside it, while
        this one record describes the current partition reconstructed from that
        evidence. It is therefore replaced in place rather than published as a new
        immutable object — and it is still inside `inventory_scope()`, because a
        record a reviewer recomputes denominators from may not be a file nothing
        accounts for.

        This is a replace, not a compare-and-swap: two Recensor passes racing on
        one shared run tree could write in either order and the last one wins.
        The race is bounded here rather than fixed. The proposal-act denominator
        `expected_act_count` recomputes is sealed by the Designator before the
        Recensor ever runs, so it cannot legitimately differ between two honest
        passes over the same run; a write that would change it is a different,
        inconsistent claim about the same sealed denominator, and is refused.

        What a race can still do — replace a receipt from a later, more-resolved
        pass with one from an earlier pass over the same denominator — cannot
        manufacture the failure GOVERNANCE 2 and ARCHITECTURE invariant 6 forbid.
        Every review this receipt cites is itself immutable and append-only, and
        an act's classification only ever moves toward resolution
        (`common/contracts/outcomes.py` has no transition back from a
        COMPLETED-class review), so an honestly computed receipt can under-state
        a run's completeness but never claim completeness the on-disk reviews do
        not independently back. A stale write is a confusing audit artifact, not
        a false "complete". Two Recensor passes must still not run concurrently
        against one run tree; nothing here makes that safe, only makes one
        particular inconsistency loud instead of silent.
        """
        from common.recensor_receipt import validate_recensor_partition_receipt

        checked = validate_recensor_partition_receipt(record)
        if checked["run_id"] != self.run_id or checked["config_digest"] != self._run_authority():
            raise SchemaRefusal("Recensor partition receipt does not belong to this run authority")
        relative = self.recensor_partition_receipt_path()
        target = self.resolve(relative)
        data = canonical_bytes(checked)
        if target.exists():
            # An unreadable or invalid receipt is treated as absent, not as a
            # reason to refuse the valid one being written. This record is
            # **derived** — the paragraph above says so, and the immutable review
            # and request evidence it is reconstructed from sits beside it
            # untouched — so a torn write, a truncated file or a receipt from an
            # older schema left the run permanently unable to record a partition
            # it could recompute perfectly well. GOVERNANCE 4 protects evidence;
            # this is not evidence, and refusing here protected nothing while
            # blocking recovery. The `expected_act_count` refusal below still
            # applies whenever the existing receipt *is* valid, because that is a
            # real disagreement about a sealed denominator rather than damage.
            # Found by CodeRabbit.
            try:
                existing = validate_recensor_partition_receipt(_read_json(target))
            # `TypeError` is among them because strict canonicalization raises it: a
            # receipt damaged with a float reaches `verify_self_hash` →
            # `canonical_bytes` → `_refuse_floats`, which is a `TypeError` and not a
            # `ContractError`. Without it the sentence this block exists to make
            # true — invalid is treated as absent — was false for one whole class of
            # damage, and it is the same escape route found on the stage-05 branch
            # the same night: strict canonicalization refusing outside the governed
            # vocabulary. Found by the Opus read of this branch.
            # `_read_json` already translates `OSError`, `ValueError`, and its
            # `UnicodeDecodeError` subclass into `SchemaRefusal`/`ContractError`.
            # `RecursionError` remains separate because `json.loads` can raise it
            # for a deeply nested damaged file and `_read_json` does not translate
            # it. These are the three live classes at this boundary.
            except (ContractError, TypeError, RecursionError) as error:
                existing = None
                # Treated as absent, but never *silently* absent. A torn or
                # truncated receipt means a process died mid-write or the disk
                # misbehaved — a fact about this run's health, visible at
                # exactly this moment and nowhere afterwards, because the next
                # line overwrites it. Discarding it without a word would leave
                # an auditor a clean receipt and no reason to look further,
                # which is the shape GOVERNANCE 2 forbids. The path is a
                # run-tree relative path, never a submitted filename, so this
                # channel is open to it (`common/exemplar_boundary.py` records
                # why that distinction matters). Found by CodeRabbit.
                print(
                    f"warning: the existing Recensor partition receipt at {relative} could "
                    f"not be read as a valid receipt and is being replaced "
                    f"({type(error).__name__}: {error}). This means a previous write did "
                    f"not complete; the receipt is derived and is being rebuilt, but the "
                    f"interruption itself is worth investigating.",
                    file=sys.stderr,
                )
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
                if target.read_bytes() == data:
                    return PublishResult(relative, reused=True)
            except FileNotFoundError:
                # Gone between `exists()` above and here. Nothing to reuse and
                # nothing to refuse: fall through and publish it, which is what
                # `_publish_bytes` does at the same seam. Found by CodeRabbit.
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
        expected_path = self.receipt_path(parsed.sha256)
        if parsed.relative_path != expected_path:
            raise SchemaRefusal(
                f"receipt reference {parsed.relative_path!r} is not its content-addressed path "
                f"{expected_path!r}"
            )
        # A reference whose file is gone is a provenance failure, not a crash. Without
        # this, a valid-looking reference to a removed receipt ended the stage with a
        # bare FileNotFoundError instead of a named refusal — and #42 is about refusing
        # provenance, which includes provenance that is no longer there. Found by
        # CodeRabbit on pull request 16.
        try:
            data = self.read_bytes(parsed.relative_path)
        except OSError as error:
            raise SchemaRefusal(
                f"run receipt {parsed.relative_path} could not be read: {error}"
            ) from error
        actual = digest_bytes(data)
        if actual != parsed.sha256:
            raise SchemaRefusal(
                f"run receipt {parsed.relative_path} has digest {actual}, not the reference "
                f"digest {parsed.sha256}"
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
        expected_path = self.receipt_path(parsed.sha256)
        if parsed.relative_path != expected_path:
            raise ApprovalRefusal(
                f"approval record reference {parsed.relative_path!r} is not its content-addressed "
                f"path {expected_path!r}"
            )
        try:
            data = self.read_bytes(parsed.relative_path)
        except OSError as error:
            raise ApprovalRefusal(
                f"approval record {parsed.relative_path} could not be read: {error}"
            ) from error
        actual = digest_bytes(data)
        if actual != parsed.sha256:
            raise ApprovalRefusal(
                f"approval record {parsed.relative_path} has digest {actual}, not the reference "
                f"digest {parsed.sha256}"
            )
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ApprovalRefusal(
                f"approval record {parsed.relative_path} could not be read: {error}"
            ) from error
        return validate_approval_record(decoded)

    def _publish_bytes(self, relative: str, data: bytes) -> PublishResult:
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            _atomic_create(target, data)
        except FileExistsError:
            try:
                existing = target.read_bytes()
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

    def read_bytes(self, relative_path: str) -> bytes:
        return self.resolve(relative_path).read_bytes()

    def has_artifact(self, stage: str, kind: str, artifact_id: str) -> bool:
        return self.resolve(self.artifact_path(stage, kind, artifact_id)).exists()

    # --- Manifests: derived, never the only evidence ---------------------------

    def build_manifest(self, stage: str, *, verify_inputs: bool = True) -> dict[str, Any]:
        """Walk the stage's artifacts and describe what is actually there.

        Derived from the tree every time it is called, so it cannot drift from
        what the tree holds. That is why a manifest is never evidence on its own:
        if it disagrees with the artifacts, the artifacts are right and the
        manifest was stale.

        ``verify_inputs=False`` still applies the envelope, run, and path checks,
        but not ``_verify_artifact_inputs``. An artifact whose upstream blob has
        since changed is then listed and reads as present. That option is for a
        caller whose own boundary owns lineage -- one already verifying the
        chain, or one deliberately reading a tree it is about to refuse -- and
        never for speed. A caller that wants a manifest it can trust about
        lineage leaves it alone.
        """
        # A manifest is a read route too. In particular, an empty directory must
        # not make a missing run authority look like an empty, trustworthy run.
        self._run_authority()
        entries: list[dict[str, Any]] = []
        artifacts_root = self._inventory_directory(stage, ARTIFACTS_DIR)
        if artifacts_root is not None:
            # Validate the whole walk before reading bytes so structural failures
            # cannot be hidden by an earlier artifact-content failure.
            members = list(self._walk_artifact_json(artifacts_root))
            for relative_path in members:
                # One filesystem read supplies both the record that is verified
                # and its digest. Re-resolving here also closes the replacement
                # window opened by collecting the complete walk first. The
                # _naming wrapper attributes a refusal to the exact member that
                # raised it, for the operator-facing review surface.
                record, artifact_bytes = self._read_manifest_artifact(relative_path)
                with _naming(relative_path):
                    record = validate_envelope(record)
                    self._verify_artifact_run(record)
                self._verify_artifact_path(relative_path, record)
                # Door and Exemplar deliberately share one physical directory:
                # a Door admission is part of what Exemplar must account for.
                # Their manifests still describe producer inventories, not every
                # neighboring JSON file under that directory.
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
        # Keep the spelling check for a useful out-of-tree diagnostic, but never
        # rely on it for safety. The descriptor walk below opens every component
        # relative to its already-open parent with O_NOFOLLOW and compares the
        # opened object to the lstat result by device and inode.
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
            try:
                start_names = self._listing_fd(start_fd, relative_root)
            except BaseException:
                os.close(start_fd)
                raise
            stack.append(
                (
                    start_fd,
                    relative_root,
                    start_names,
                    0,
                    frozenset({start_identity}),
                )
            )
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
                # Preserve explicit containment diagnostics. Security comes from
                # the dir-fd operations below; this resolved spelling is not used.
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
                    try:
                        child_names = self._listing_fd(child_fd, relative_path)
                    except BaseException:
                        os.close(child_fd)
                        raise
                    stack.append(
                        (
                            child_fd,
                            relative_path,
                            child_names,
                            0,
                            ancestors | {identity},
                        )
                    )
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

        The run id alone is not that binding. It is caller-supplied and kept boring
        on purpose so an operator can type it and find the run again
        (`identities.validate_run_id`), and `--run-root` and `--run-id` are
        independent flags — so two runs in two roots may both be `run1` and nothing
        in a name comparison can tell their artifacts apart. Demonstrated: a
        `perlectio` from one `run1` dropped into the other `run1` was accepted by
        every generic read route, and that run's manifest, review and export all
        reconciled around a reading produced under a different configuration. A tree
        restored from a partial backup is enough to produce it.

        The `config_digest` is the authority's own binding to the source manifest,
        model roster and adapter recipes the run was created with, and every stage
        publishes the one it opened. Comparing it here closes the gap for every
        artifact rather than only for the Door admissions and Exemplar pages that
        were already checked against it by hand at their own boundaries.

        This is integrity, not authentication, and the distinction is worth keeping
        straight: the self-hash and this check together prove a record was not
        edited by anything unaware of the scheme, and that it belongs to this run's
        configuration. Neither proves who wrote it — every input to the hash is
        inside the record, so anything holding this repository's own API can seal a
        forgery. Nothing here should be read as claiming otherwise.
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
        # Behind the run authority like `write_manifest` (through `build_manifest`)
        # and `read_index`: a tree with no valid `run.json` must not gain a
        # summary file nothing can bind to it.
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
            # canonical_bytes refuses floats (and anything unserializable) with
            # TypeError, and a circular structure is named by its float-walk's
            # own cycle check (TypeError) or by json's (ValueError) — all outside
            # the ContractError family a stage classifies. RecursionError stays
            # caught even though that walk no longer recurses: json's C encoder
            # under it still does, and this boundary is not the place to bet on
            # a bound one module away. A later caller's measured ratio or
            # self-referencing row must be a named refusal, not a traceback.
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

        Harvest invariant #13: the inventory's scope can never silently
        under-cover — every managed output path any code can write must resolve
        inside the inventory scope, and adding a managed path without extending
        the scope fails a static drift test, loudly, naming the path. The test
        beside this module reads the writers from source and compares.
        """
        prefixes = [RUN_FILE, f"{RECEIPTS_DIR}/", RECENSOR_PARTITION_RECEIPT_FILE]
        for directory in sorted(set(_all_writing_directories())):
            prefixes.append(f"{directory}/{ARTIFACTS_DIR}/")
            prefixes.append(f"{directory}/{BLOBS_DIR}/")
            prefixes.append(f"{directory}/{MANIFEST_FILE}")
            prefixes.append(f"{directory}/{INDEX_FILE}")
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
    """Temp file in the same directory, flushed, then replaced.

    Same directory because os.replace is only atomic within one filesystem. The
    fsync is what makes the guarantee survive power loss rather than only process
    death, which matters because a half-written artifact that a resume trusts is
    exactly the failure the sealed tree exists to prevent.
    """
    temporary = _write_temporary(target, data)
    try:
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_create(target: Path, data: bytes) -> None:
    """Publish immutable bytes only when their final name does not yet exist.

    A hard link is an atomic create on the target filesystem.  The temporary is
    fully written and synced first, then either acquires the final name or raises
    ``FileExistsError`` without replacing the competing writer's bytes.

    **The run root must therefore be on a hard-link-capable filesystem**, which most
    are and some are not — an exFAT or FAT32 volume, a few network mounts, and some
    container bind mounts reject ``os.link`` outright.  Those refuse with ``EPERM``,
    ``EOPNOTSUPP`` or ``ENOSYS``, which escaped from here as a bare ``OSError`` and
    surfaced as a traceback naming ``link`` rather than as a statement about where
    the run root was put.  Named instead, because it is a setup fact the operator
    can act on.  A plain ``O_EXCL`` write is deliberately not substituted: it would
    publish the final name before the bytes were in it, and no-partial-publication
    is the guarantee this function exists for.
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


def _read_json_with_bytes(path: Path) -> tuple[Any, bytes]:
    # `RecursionError` beside the two obvious ones because it is the same fact —
    # this file could not be read — arriving by a route the tuple did not name.
    # `json`'s scanner recurses per nesting level, so a deeply nested artifact
    # raised it straight through every caller: a stage that should have refused
    # the file and held instead died with a traceback, and the manifest walk that
    # reads every artifact in a directory made one such file enough to stop the
    # whole stage. The exact depth this fires at is the scanner's own, not a
    # number this file should claim: it depends on the interpreter's recursion
    # limit and the C accelerator's own tolerance, both environment facts rather
    # than this project's. The regression test drives it at a depth deep enough
    # to be unambiguous on any of them (30,000) rather than pin one that would
    # not reproduce elsewhere. A second, shallower band of the same failure can
    # still reach `verify_self_hash`'s own recursive walk after this guard has
    # already let a shallower-but-still-deep file through; that band is caught
    # where it happens, in `common/contracts/canonical.py`.
    #
    # Both merged branches split this reader the same way and named the halves
    # differently; one tuple-returning body survives, under this name, and it
    # decodes and returns the exact same bytes a caller may later digest.
    try:
        data = path.read_bytes()
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
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        raise SchemaRefusal("run receipt reference must contain exactly relative_path and sha256")
    relative, digest = value["relative_path"], value["sha256"]
    if not isinstance(relative, str) or not relative:
        raise SchemaRefusal("run receipt reference has no relative_path")
    if not is_sha256(digest):
        raise SchemaRefusal("run receipt reference has no lowercase sha256")
    return RunReceiptReference(relative, digest)


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
