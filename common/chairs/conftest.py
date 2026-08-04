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
    """The chair protocol's second implementation: in-memory, offline, no fetch.

    Spec 02 asks for "a deterministic fake honoring the same protocol, for the
    skeleton and every offline test", living beside the tests rather than in
    `proof/` — `proof/` holds fixture *data*, and a fake implementation is code.
    It sits in this file, the one module in the package that only ever loads
    under pytest, so that nothing shipped can reach it: a fake answering under a
    configured chair's name is the exact failure the whole framework is built to
    refuse, and the surest way to prevent it is to leave no import path to one.

    Independence, in the sense spec 02 defines: it does not import, subclass or
    delegate to `ChairRegistry`. It shares the configuration parser, because the
    two implementations are meant to enforce one contract on a pin rather than
    two that happen to agree, and it shares the receipt builder for the same
    reason. Everything the protocol actually names — resolution, verification,
    and what a receipt is issued for — it does itself.

    `calls` is the log the parameterized skeleton test reads to prove the stages
    really ran against this implementation and not quietly against the real one.
    """

    def __init__(self, config_path: str | Path, snapshot_root: Path):
        from common.chairs.config import load_models_toml

        self.config = load_models_toml(config_path)
        self.snapshot_root = Path(snapshot_root)
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        self.calls: list[tuple[str, str]] = []

    def resolve(self, role: str):
        from common.chairs.errors import UnresolvedChairRefusal

        self.calls.append(("resolve", role))
        configured = self.config.chairs.get(role)
        if configured is None:
            raise UnresolvedChairRefusal(role, "role is not present in this deterministic chair")
        return configured

    def ensure(self, identity):
        from common.chairs.models import VerifiedSnapshot

        self.calls.append(("ensure", identity.role))
        self._require_current(identity)
        return VerifiedSnapshot(
            identity=identity, root=self.snapshot_root, manifest_digest=identity.digest_manifest
        )

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
