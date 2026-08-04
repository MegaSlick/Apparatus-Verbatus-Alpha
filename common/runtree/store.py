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

import json
import os
import tempfile
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
    self_hash,
    verify_self_hash,
)
from common.contracts.envelope import validate_envelope
from common.contracts.errors import ApprovalRefusal, IncompatibleReuse, SchemaRefusal
from common.contracts.identities import validate_run_id
from common.contracts.stages import writing_directory

RUN_FILE: Final = "run.json"
MANIFEST_FILE: Final = "manifest.json"
ARTIFACTS_DIR: Final = "artifacts"
BLOBS_DIR: Final = "blobs/sha256"
RECEIPTS_DIR: Final = "receipts/sha256"

# The facts a run id is bound to. Changing any of them means this is a different
# run wearing an old name, and reuse is refused rather than resumed.
_BOUND_FIELDS: Final = ("source_manifest", "config_digest", "adapter_recipes", "witness_chairs")
_INGRESS_FIELD: Final = "ingress"
_RENDER_SETTINGS_FIELD: Final = "render_settings"


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
        ingress: dict[str, Any] | None = None,
        render_settings: dict[str, Any] | None = None,
    ) -> "RunTree":
        """Open a run, creating it if new and refusing an incompatible reuse.

        The refusal happens before any directory is created for a *new* run and
        before any artifact is touched for an existing one, so a rejected reuse
        leaves the tree exactly as it found it.
        """
        tree = cls(root, run_id)
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
        repeated = sorted({ordinal for ordinal in ordinals if ordinals.count(ordinal) > 1})
        if repeated:
            raise SchemaRefusal(
                f"source pages declare ordinal(s) {repeated} more than once; an "
                "ordinal names one page, so a repeat leaves the run unable to say "
                "how many pages it was given"
            )
        authority = {
            "schema": SCHEMA_LABEL,
            "run_id": tree.run_id,
            "source_manifest": sorted(
                source_manifest, key=lambda page: (page.get("ordinal", 0), page.get("sha256", ""))
            ),
            "config_digest": config_digest,
            "adapter_recipes": dict(sorted(adapter_recipes.items())),
            "witness_chairs": sorted(witness_chairs),
        }
        if ingress is not None:
            authority[_INGRESS_FIELD] = ingress
        if render_settings is not None:
            if not isinstance(render_settings, dict) or not render_settings:
                raise SchemaRefusal("run render_settings must be a non-empty object when supplied")
            authority[_RENDER_SETTINGS_FIELD] = render_settings
        authority["self_hash"] = self_hash(authority)

        run_file = tree.root / RUN_FILE
        tree.root.mkdir(parents=True, exist_ok=True)
        try:
            _atomic_create(run_file, canonical_bytes(authority))
        except FileExistsError:
            existing = tree.read_run()
            # `.get`, not `[...]`: a run.json missing a bound field used to raise a
            # bare KeyError out of the reuse check, which is a traceback and an
            # exit 1 where the contract promises a named refusal. A missing field
            # is also not "equal" to anything, so it lands in `differing` and is
            # refused with the reason spelled out.
            if existing.get("schema") != authority["schema"]:
                raise IncompatibleReuse(
                    f"run {run_id!r} was written under schema "
                    f"{existing.get('schema')!r} and this is {authority['schema']!r}; "
                    "the two describe different shapes and cannot share a tree"
                ) from None
            optional_bound_fields = tuple(
                field
                for field in (_INGRESS_FIELD, _RENDER_SETTINGS_FIELD)
                if field in authority or field in existing
            )
            bound_fields = _BOUND_FIELDS + optional_bound_fields
            differing = [
                field
                for field in bound_fields
                if field not in existing or existing[field] != authority[field]
            ]
            if differing:
                raise IncompatibleReuse(
                    f"run {run_id!r} already exists and is bound to different "
                    f"{', '.join(differing)}; a run id names one set of inputs and "
                    "one configuration, so this is a different run wearing an old "
                    "name. Nothing was written"
                ) from None
            return tree
        return tree

    def read_run(self) -> dict[str, Any]:
        """The run authority, refused if it was edited after it was sealed."""
        run_file = self.root / RUN_FILE
        if not run_file.exists():
            raise IncompatibleReuse(
                f"no {RUN_FILE} under {self.root}: there is no run here to read, and "
                "a stage that wrote into one anyway would be writing into nothing"
            )
        record = _read_json(run_file)
        if not verify_self_hash(record):
            raise IncompatibleReuse(
                f"{run_file} fails its own self-hash: the run authority was edited "
                "after it was sealed, so nothing in this tree can be trusted against it"
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
        return f"{writing_directory(stage)}/{MANIFEST_FILE}"

    def receipt_path(self, digest: str) -> str:
        """The one content-addressed location for a validated receipt-backed record."""
        if not _is_sha256(digest):
            raise SchemaRefusal(f"receipt digest {digest!r} is not a lowercase sha256")
        return f"{RECEIPTS_DIR}/{digest}.json"

    def resolve(self, relative_path: str) -> Path:
        """A path inside this run tree, refusing anything that leaves it.

        Input references are relative so a run tree stays movable and verifiable;
        this is where that promise is enforced rather than assumed.
        """
        if relative_path.startswith("/") or ".." in relative_path.split("/"):
            raise SchemaRefusal(f"{relative_path!r} escapes the run tree")
        resolved = (self.root / relative_path).resolve()
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
        record = _read_json(self.resolve(self.artifact_path(stage, kind, artifact_id)))
        return validate_envelope(record)

    def read_bytes(self, relative_path: str) -> bytes:
        return self.resolve(relative_path).read_bytes()

    def has_artifact(self, stage: str, kind: str, artifact_id: str) -> bool:
        return self.resolve(self.artifact_path(stage, kind, artifact_id)).exists()

    # --- Manifests: derived, never the only evidence ---------------------------

    def build_manifest(self, stage: str) -> dict[str, Any]:
        """Walk the stage's artifacts and describe what is actually there.

        Derived from the tree every time it is called, so it cannot drift from
        what the tree holds. That is why a manifest is never evidence on its own:
        if it disagrees with the artifacts, the artifacts are right and the
        manifest was stale.
        """
        stage_root = self.resolve(writing_directory(stage))
        entries: list[dict[str, Any]] = []
        artifacts_root = stage_root / ARTIFACTS_DIR
        if artifacts_root.exists():
            for path in sorted(artifacts_root.rglob("*.json")):
                record = validate_envelope(_read_json(path))
                entries.append(
                    {
                        "artifact_id": record["artifact_id"],
                        "kind": record["kind"],
                        "subject_id": record["subject_id"],
                        "outcome": record["outcome"],
                        "relative_path": str(path.relative_to(self.root)),
                        "sha256": digest_bytes(path.read_bytes()),
                    }
                )
        blobs_root = stage_root / BLOBS_DIR
        blobs = sorted(entry.name for entry in blobs_root.iterdir()) if blobs_root.exists() else []
        return {
            "schema": SCHEMA_LABEL,
            "run_id": self.run_id,
            "stage": stage,
            "artifacts": sorted(entries, key=lambda entry: entry["artifact_id"]),
            "blobs": blobs,
        }

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
        prefixes = [RUN_FILE, f"{RECEIPTS_DIR}/"]
        for directory in sorted(set(_all_writing_directories())):
            prefixes.append(f"{directory}/{ARTIFACTS_DIR}/")
            prefixes.append(f"{directory}/{BLOBS_DIR}/")
            prefixes.append(f"{directory}/{MANIFEST_FILE}")
        return tuple(prefixes)


def _all_writing_directories() -> list[str]:
    from common.contracts.stages import WRITING_DIRECTORIES

    return list(WRITING_DIRECTORIES.values())


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
    """
    temporary = _write_temporary(target, data)
    try:
        os.link(temporary, target)
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


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SchemaRefusal(f"{path} could not be read as an artifact: {error}") from error


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _receipt_reference(value: RunReceiptReference | dict[str, str]) -> RunReceiptReference:
    if isinstance(value, RunReceiptReference):
        return value
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        raise SchemaRefusal("run receipt reference must contain exactly relative_path and sha256")
    relative, digest = value["relative_path"], value["sha256"]
    if not isinstance(relative, str) or not relative:
        raise SchemaRefusal("run receipt reference has no relative_path")
    if not _is_sha256(digest):
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
    if not _is_sha256(value.sha256):
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
