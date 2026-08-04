"""Builders the chair test files share, so each spec test can stand on its own.

Spec 02 names nine tests. Seven of them live here, one file each, named for the
test they discharge, because a reader checking the spec against the suite should
not have to work out which assertion inside a long function covers which clause.
That split only stays readable if the fixture plumbing is written once — hence
this file rather than the same forty lines copied into seven.

Nothing here resolves, fetches, or verifies anything. It builds `models.toml`
files, snapshot directories, manifest artifacts and serving details, and hands
them back; every assertion about behaviour belongs in the test that makes it.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from common.chairs.config import parse_models_config
from common.chairs.manifests import build_manifest, manifest_digest, write_manifest
from common.chairs.models import ModelsConfig, ServingDetails
from common.chairs.registry import ChairRegistry

HF_REVISION = "a" * 40
LICENSE_NOTE = "fixture placeholder; not a real model, so no model license applies"
SERVING_RECIPE = "fixture-recipe-v0"


def hf_chair(role: str, digest_manifest: str, **overrides: Any) -> dict[str, Any]:
    """A well-formed `huggingface` chair table, before any deliberate damage."""
    table = {
        "state": "configured",
        "source": "huggingface",
        "repo": f"fixture-org/{role}",
        "revision": HF_REVISION,
        "digest_manifest": digest_manifest,
        "manifest": f"manifests/{role}.json",
        "serving_recipe": SERVING_RECIPE,
        "license_note": LICENSE_NOTE,
    }
    table.update(overrides)
    return table


def local_chair(role: str, digest_manifest: str, **overrides: Any) -> dict[str, Any]:
    """A well-formed `local-repository` chair table: a path, and no revision."""
    table = {
        "state": "configured",
        "source": "local-repository",
        "path": role,
        "digest_manifest": digest_manifest,
        "manifest": f"manifests/{role}.json",
        "serving_recipe": SERVING_RECIPE,
        "license_note": LICENSE_NOTE,
    }
    table.update(overrides)
    return table


def absent_chair(reason: str = "no such witness is configured for alpha") -> dict[str, Any]:
    return {"state": "absent", "reason": reason}


def write_snapshot(root: Path, files: dict[str, bytes]) -> Path:
    """Materialize one snapshot's files under `root` and return it."""
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def pin_snapshot(snapshot_root: Path, manifest_path: Path) -> str:
    """Build a snapshot's manifest artifact, write it, and return its pin."""
    manifest = build_manifest(snapshot_root)
    pin = write_manifest(manifest, manifest_path)
    assert pin == manifest_digest(manifest)
    return pin


def config_of(
    tmp_path: Path, chairs: dict[str, dict[str, Any]], *, witness_floor: int = 1, **top: Any
) -> ModelsConfig:
    """Parse a `models.toml`-shaped mapping, rooted at `tmp_path/models.toml`."""
    raw: dict[str, Any] = {"witness_floor": witness_floor, "chairs": chairs}
    raw.update(top)
    return parse_models_config(raw, source_path=tmp_path / "models.toml")


def write_models_toml(
    tmp_path: Path, chairs: dict[str, dict[str, Any]], *, witness_floor: int = 1, **top: Any
) -> Path:
    """The same shape, but through a real TOML file on disk.

    Some tests need `load_models_toml`'s own reading of the file rather than the
    in-memory parser, because the file is the artifact an operator edits.
    """
    lines: list[str] = [f"witness_floor = {witness_floor}"]
    for key, value in top.items():
        lines.append(f"{key} = {_toml_value(value)}")
    for role, fields in chairs.items():
        lines.append("")
        lines.append(f"[chairs.{role}]")
        for key, value in fields.items():
            lines.append(f"{key} = {_toml_value(value)}")
    path = tmp_path / "models.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class RecordingFetcher:
    """The one network seam, offline, keeping the log the tests actually assert on.

    Two things are measured through it and neither is Hugging Face's behaviour:
    which chair a fetch was requested for, and exactly which paths were asked for.
    Spec 02, test 2 says this plainly — "this measures the call the mock
    received" — and test 7 leans on the same log to show no *other* chair was ever
    reached for while a refusal was being handled.
    """

    def __init__(self, files: dict[str, bytes], *, fail: Exception | None = None):
        self.files = files
        self.fail = fail
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    @property
    def roles(self) -> list[str]:
        return [role for role, _ in self.calls]

    def fetch(self, identity, destination: Path, paths: tuple[str, ...]) -> None:
        self.calls.append((identity.role, paths))
        if self.fail is not None:
            raise self.fail
        for relative in paths:
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.files[relative])


def registry_for(
    config: ModelsConfig, tmp_path: Path, fetcher: RecordingFetcher | None = None
) -> ChairRegistry:
    return ChairRegistry(
        config, manifest_root=tmp_path, cache_root=tmp_path / "cache", fetcher=fetcher
    )


def serving_details(**overrides: Any) -> ServingDetails:
    """A complete serving observation: every #41 field, none of them blank."""
    fields: dict[str, Any] = {
        "tokenizer_revision": "f" * 40,
        "seed": 7,
        "context_cap": 8192,
        "pixel_cap": 1_000_000,
        "engine": "fixture-engine",
        "engine_version": "1.0.0",
        "dtype": "bfloat16",
        "adapter_identity": None,
        "endpoint": "http://127.0.0.1:8000",
        "started_at": "2026-08-03T00:00:00Z",
    }
    fields.update(overrides)
    return ServingDetails(**fields)


@pytest.fixture
def hf_world(tmp_path):
    """One configured Hugging Face chair, one absent chair, and a bystander.

    The bystander is not decoration: "no other configured chair is invoked" is a
    vacuous claim against a roster of one, so every refusal test has at least one
    tempting alternative sitting beside the chair that refuses.
    """
    files = {"config.json": b'{"fixture": true}\n', "nested/weights.bin": b"fixture weights\n"}
    write_snapshot(tmp_path / "remote", files)
    pin = pin_snapshot(tmp_path / "remote", tmp_path / "manifests" / "attestator_1.json")
    chairs = {
        "attestator_1": hf_chair("attestator_1", pin),
        "attestator_2": hf_chair("attestator_2", pin, manifest="manifests/attestator_1.json"),
        "attestator_3": absent_chair(),
    }
    config = config_of(tmp_path, chairs, witness_floor=3)
    fetcher = RecordingFetcher(files)
    return HuggingFaceWorld(
        registry=registry_for(config, tmp_path, fetcher),
        fetcher=fetcher,
        files=files,
        pin=pin,
        tmp_path=tmp_path,
    )


class HuggingFaceWorld:
    """What `hf_world` hands a test: the registry, the seam, and the pins."""

    def __init__(self, registry, fetcher, files, pin, tmp_path):
        self.registry = registry
        self.fetcher = fetcher
        self.files = files
        self.pin = pin
        self.tmp_path = tmp_path

    def identity(self, role: str = "attestator_1"):
        return self.registry.resolve(role)


class DeterministicChairRegistry:
    """The chair protocol's second implementation: offline, no fetch.

    Spec 02 asks for "a deterministic fake honoring the same protocol, for the
    skeleton and every offline test", living beside the tests rather than in
    `proof/` — `proof/` holds fixture *data*, and a fake implementation is code.

    **It is an ordinary module of an ordinary package, and it ships.** An earlier
    version of this docstring said it "only ever loads under pytest, so that
    nothing shipped can reach it"; `pyproject.toml` includes `common.*` wholesale,
    so `import common.chairs.conftest` works in any installed copy. A fake
    answering under a configured chair's name is the exact failure the framework
    exists to refuse, and a claim about packaging that nothing checked was not
    stopping it. The constructor guard below is: outside a pytest session this
    class refuses to exist at all.

    Independence, in the sense spec 02 defines: it does not import, subclass or
    delegate to `ChairRegistry`. It shares the configuration parser, because the
    two implementations are meant to enforce one contract on a pin rather than
    two that happen to agree, and it shares the receipt builder for the same
    reason. Everything the protocol actually names — resolution, verification,
    and what a receipt is issued for — it does itself, down to its own hashing.

    `calls` is the log the parameterized skeleton test reads to prove the stages
    really ran against this implementation and not quietly against the real one.
    """

    def __init__(self, config_path: str | Path):
        from common.chairs.config import load_models_toml

        # `PYTEST_CURRENT_TEST`, not `"pytest" in sys.modules`: this module imports
        # pytest itself, so the module check was true the instant anyone imported
        # this class and refused nothing. The environment variable is set by pytest
        # only while a test's setup, call or teardown is running, which is the only
        # time anything here has business existing.
        if "PYTEST_CURRENT_TEST" not in os.environ:
            raise RuntimeError(
                "DeterministicChairRegistry is a test double and refuses to run outside a "
                "pytest session; a fake answering under a configured chair's name is what "
                "common/chairs exists to prevent"
            )
        self.config = load_models_toml(config_path)
        self._config_dir = Path(config_path).resolve().parent
        self.calls: list[tuple[str, str]] = []

    def resolve(self, role: str):
        from common.chairs.errors import UnresolvedChairRefusal

        self.calls.append(("resolve", role))
        configured = self.config.chairs.get(role)
        if configured is None:
            raise UnresolvedChairRefusal(role, "role is not present in this deterministic chair")
        return configured

    def ensure(self, identity):
        """Verify the pinned snapshot, by its own reading of the manifest artifact.

        It used to return a `VerifiedSnapshot` naming a directory it had created
        itself and copy `identity.digest_manifest` into the answer, so the shared
        contract suite could not tell an implementation that verifies from one
        that asserts — and `exercise_contract` checks only the value's type and
        identity, so an implementation with no verification at all passed it. The
        hashing below is deliberately this class's own rather than
        `manifests.verify_snapshot`: two implementations that call one verifier
        agree because they are one verifier.
        """
        from common.chairs.errors import DigestMismatchRefusal, UnresolvedChairRefusal
        from common.chairs.models import VerifiedSnapshot

        self.calls.append(("ensure", identity.role))
        self._require_current(identity)
        if identity.source != "local-repository":
            raise UnresolvedChairRefusal(
                identity.role,
                "the deterministic chair fetches nothing, so only a local-repository pin "
                "can be verified offline",
            )
        root = self._snapshot_root(identity)
        expected = {row["path"]: row for row in self._manifest_rows(identity)}
        actual = _files_under(root, identity.role)
        for relative in sorted(set(expected) | set(actual)):
            row, path = expected.get(relative), actual.get(relative)
            if row is None:
                raise DigestMismatchRefusal(
                    identity.role, f"snapshot differs at {relative}: extra file"
                )
            if path is None:
                raise DigestMismatchRefusal(
                    identity.role, f"snapshot differs at {relative}: missing file"
                )
            data = path.read_bytes()
            if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise DigestMismatchRefusal(
                    identity.role, f"snapshot differs at {relative}: bytes do not match the pin"
                )
        return VerifiedSnapshot(
            identity=identity, root=root, manifest_digest=identity.digest_manifest
        )

    def _snapshot_root(self, identity) -> Path:
        from common.chairs.errors import LocalPathRefusal

        base = self._config_dir / (self.config.model_root or "")
        root = (base / identity.path).resolve()
        if not root.is_relative_to(base.resolve()) or not root.is_dir():
            raise LocalPathRefusal(
                identity.role, f"local path {identity.path!r} is not a directory under model_root"
            )
        return root

    def _manifest_rows(self, identity) -> list[dict]:
        """Read the manifest artifact and refuse it unless its bytes are the pin."""
        from common.chairs.errors import DigestMismatchRefusal

        path = (self._config_dir / identity.manifest).resolve()
        try:
            data = path.read_bytes()
        except OSError as error:
            raise DigestMismatchRefusal(
                identity.role, f"cannot read manifest {path}: {error}"
            ) from error
        if hashlib.sha256(data).hexdigest() != identity.digest_manifest:
            raise DigestMismatchRefusal(
                identity.role, f"manifest {path} does not hash to the configured pin"
            )
        rows = json.loads(data)
        if not isinstance(rows, list) or not rows:
            raise DigestMismatchRefusal(
                identity.role, "manifest artifact is not a non-empty row list"
            )
        # Row shape, not only list shape. Without this a malformed manifest reached
        # `ensure` and came out as a bare KeyError or TypeError — an error outside
        # the closed taxonomy, from the implementation whose whole job is to reach
        # the same refusals the real registry reaches.
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != {"path", "sha256", "size"}:
                raise DigestMismatchRefusal(
                    identity.role, f"manifest row {index} does not have exactly path, sha256, size"
                )
        return rows

    def receipt(self, identity, serving):
        from common.chairs.receipts import build_receipt

        self.calls.append(("receipt", identity.role))
        self._require_current(identity)
        return build_receipt(identity, serving)

    def _require_current(self, identity):
        """The same refusal the real registry makes, reached independently.

        A fake that accepted any identity handed to it would let the contract
        suite pass over an implementation with no anti-substitution property at
        all, and the suite would then be proving only that both have the right
        method names.
        """
        from common.chairs.errors import UnresolvedChairRefusal
        from common.chairs.models import AbsentChair, ChairIdentity

        configured = self.resolve(identity.role)
        if isinstance(configured, AbsentChair):
            raise UnresolvedChairRefusal(
                identity.role, f"chair is explicitly absent: {configured.reason}"
            )
        if not isinstance(configured, ChairIdentity) or configured != identity:
            raise UnresolvedChairRefusal(
                identity.role,
                "identity differs from the configured pin; ensure and receipt never "
                "accept a neighbouring revision",
            )


def _files_under(root: Path, chair: str) -> dict[str, Path]:
    """Every regular file beneath `root`, refusing a symlink rather than following it.

    Refusing rather than skipping, because `manifests._regular_files` refuses, and
    the two implementations are meant to agree on what a valid snapshot *is*. A
    version of this that quietly skipped symlinks would have let a snapshot the
    real registry rejects pass here, which is the disagreement the contract suite
    exists to catch rather than to contain.
    """
    from common.chairs.errors import DigestMismatchRefusal

    found: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise DigestMismatchRefusal(chair, f"snapshot differs at {relative}: symlink")
        if not path.is_dir():
            found[relative] = path
    return found
