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
its configured witness seats, its configuration digest, its adapter recipes. It
deliberately does not predeclare acts — the Designator's proposal seal is the
downstream expected-act authority, because acts are discovered and pages are given.

Reusing a run id whose source, configuration, or adapter recipes have changed fails
before any write. That is spec 01's third test, and it is the difference between a
resumed run and a corrupted one.
"""

import os
from pathlib import Path
from typing import Any, Final

from common.contracts.canonical import (
    SCHEMA_LABEL,
    canonical_bytes,
    digest_bytes,
    self_hash,
    verify_self_hash,
)
from common.contracts.envelope import validate_envelope
from common.contracts.errors import IncompatibleReuse, SchemaRefusal
from common.contracts.identities import validate_run_id
from common.contracts.stages import writing_directory

RUN_FILE: Final = "run.json"
MANIFEST_FILE: Final = "manifest.json"
ARTIFACTS_DIR: Final = "artifacts"
BLOBS_DIR: Final = "blobs/sha256"

# The facts a run id is bound to. Changing any of them means this is a different
# run wearing an old name, and reuse is refused rather than resumed.
_BOUND_FIELDS: Final = ("source_manifest", "config_digest", "adapter_recipes", "witness_seats")


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


class RunTree:
    """One run's directory, and the only writer to it."""

    def __init__(self, root: Path, run_id: str):
        self.run_id = validate_run_id(run_id)
        self.root = Path(root) / run_id

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
        witness_seats: list[str],
    ) -> "RunTree":
        """Open a run, creating it if new and refusing an incompatible reuse.

        The refusal happens before any directory is created for a *new* run and
        before any artifact is touched for an existing one, so a rejected reuse
        leaves the tree exactly as it found it.
        """
        tree = cls(root, run_id)
        authority = {
            "schema": SCHEMA_LABEL,
            "run_id": tree.run_id,
            "source_manifest": sorted(
                source_manifest, key=lambda page: (page.get("ordinal", 0), page.get("sha256", ""))
            ),
            "config_digest": config_digest,
            "adapter_recipes": dict(sorted(adapter_recipes.items())),
            "witness_seats": sorted(witness_seats),
        }
        authority["self_hash"] = self_hash(authority)

        run_file = tree.root / RUN_FILE
        if run_file.exists():
            existing = tree.read_run()
            differing = [field for field in _BOUND_FIELDS if existing[field] != authority[field]]
            if differing:
                raise IncompatibleReuse(
                    f"run {run_id!r} already exists and is bound to different "
                    f"{', '.join(differing)}; a run id names one set of inputs and "
                    "one configuration, so this is a different run wearing an old "
                    "name. Nothing was written"
                )
            return tree

        tree.root.mkdir(parents=True, exist_ok=True)
        _atomic_write(run_file, canonical_bytes(authority))
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
        if not resolved.is_relative_to(self.root.resolve()):
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

    def _publish_bytes(self, relative: str, data: bytes) -> PublishResult:
        target = self.resolve(relative)
        if target.exists():
            existing = target.read_bytes()
            if existing == data:
                return PublishResult(relative, reused=True)
            raise IncompatibleReuse(
                f"{relative} already holds different bytes. Artifacts are immutable: "
                "the same identity may not describe two different things, and the "
                "existing file was not touched"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, data)
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
        prefixes = [RUN_FILE]
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


def _read_json(path: Path) -> Any:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SchemaRefusal(f"{path} could not be read as an artifact: {error}") from error


def _refuse_path_component(value: Any, what: str) -> None:
    if not isinstance(value, str) or not value:
        raise SchemaRefusal(f"{what} is empty")
    if "/" in value or "\\" in value or value in (".", "..") or value.startswith("."):
        raise SchemaRefusal(
            f"{what} {value!r} is not a single plain path component; a stage that "
            "can name a directory can write outside its own"
        )
