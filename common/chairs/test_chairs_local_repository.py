"""Spec 02, test 5 — Local-repository chair.

"Path resolution under the model root, escape attempts refused, digest
verification, no network."

The clause behind it is ARCHITECTURE's, not CLAUDE.md's Quarantine: weights are
never vendored, a locally trained checkpoint is *called* like any other model
from its own model repository, and trained weights are "a candidate, never a
privileged inheritance". A local chair is therefore an ordinary chair that happens
to sit on this filesystem — pinned by digest manifest exactly like a fetched one,
and holding no revision at all, because it has no git commit to name.
"""

import pytest

from common.chairs.errors import ConfigurationRefusal, DigestMismatchRefusal, LocalPathRefusal
from common.chairs.registry import ChairRegistry, resolve_local_path

from .conftest import (
    RecordingFetcher,
    config_of,
    local_chair,
    pin_snapshot,
    write_snapshot,
)

FILES = {"config.json": b'{"fixture": "perlector"}\n', "nested/weights.bin": b"fixture weights\n"}


def _world(tmp_path, *, path: str = "perlector", files: dict[str, bytes] | None = None):
    """A model root with one snapshot in it, pinned by its own manifest."""
    model_root = tmp_path / "model-fixtures"
    snapshot = write_snapshot(model_root / "perlector", dict(files or FILES))
    pin = pin_snapshot(snapshot, tmp_path / "manifests" / "perlector.json")
    config = config_of(
        tmp_path,
        {"perlector": local_chair("perlector", pin, path=path)},
        model_root="model-fixtures",
    )
    fetcher = RecordingFetcher({})
    registry = ChairRegistry(config, manifest_root=tmp_path, fetcher=fetcher)
    return registry, snapshot, fetcher


# --- Resolution and verification under the model root --------------------------------


def test_a_complete_local_repository_verifies_against_its_pin(tmp_path):
    registry, snapshot, _ = _world(tmp_path)
    verified = registry.ensure(registry.resolve("perlector"))

    assert verified.root == snapshot.resolve()
    assert verified.identity.revision is None
    assert verified.manifest_digest == verified.identity.digest_manifest


def test_a_local_repository_round_trip_never_touches_the_network(tmp_path):
    """Not "no network was reachable": no fetch was requested at all. The one
    seam records every call, and for a local chair the log stays empty."""
    registry, _, fetcher = _world(tmp_path)
    registry.ensure(registry.resolve("perlector"))
    assert fetcher.calls == []


def test_one_flipped_byte_under_the_model_root_fails_naming_that_file(tmp_path):
    registry, snapshot, _ = _world(tmp_path)
    (snapshot / "nested/weights.bin").write_bytes(b"different weights entirely\n")

    with pytest.raises(DigestMismatchRefusal) as caught:
        registry.ensure(registry.resolve("perlector"))
    assert "nested/weights.bin" in str(caught.value)


def test_an_extra_file_under_the_model_root_fails_naming_it(tmp_path):
    registry, snapshot, _ = _world(tmp_path)
    (snapshot / "z-unpinned.json").write_bytes(b"{}\n")

    with pytest.raises(DigestMismatchRefusal, match="extra file"):
        registry.ensure(registry.resolve("perlector"))


def test_a_missing_file_under_the_model_root_fails_naming_it(tmp_path):
    """A local chair has nowhere to re-fetch from, so a gap is a refusal outright
    rather than the partial-cache path a Hugging Face chair takes."""
    registry, snapshot, _ = _world(tmp_path)
    (snapshot / "config.json").unlink()

    with pytest.raises(DigestMismatchRefusal, match="missing file"):
        registry.ensure(registry.resolve("perlector"))


def test_a_path_naming_a_directory_that_does_not_exist_is_refused(tmp_path):
    registry, snapshot, _ = _world(tmp_path, path="not-here")
    with pytest.raises(LocalPathRefusal, match="cannot resolve"):
        registry.ensure(registry.resolve("perlector"))


def test_a_path_naming_a_file_rather_than_a_snapshot_directory_is_refused(tmp_path):
    registry, snapshot, _ = _world(tmp_path, path="perlector/config.json")
    with pytest.raises(LocalPathRefusal, match="not a snapshot directory"):
        registry.ensure(registry.resolve("perlector"))


# --- Escape attempts --------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/etc/models", "~/models", "../outside", "perlector/../../outside", "..", ""]
)
def test_an_absolute_or_traversing_path_is_refused_when_the_config_is_read(tmp_path, path):
    """Refused at parse time, before anything looks at a filesystem: a pin that
    can name a directory outside the model root is already wrong on paper."""
    with pytest.raises(ConfigurationRefusal):
        config_of(
            tmp_path,
            {"perlector": local_chair("perlector", "d" * 64, path=path)},
            model_root="model-fixtures",
        )


def test_a_symlink_escape_is_refused_against_the_real_filesystem(tmp_path):
    """The string-shape check cannot see this one: `escape-link` traverses
    nothing and is not absolute. Only resolving it against the real filesystem
    shows it leaving the model root."""
    outside = write_snapshot(tmp_path / "outside", {"weights.bin": b"somewhere else\n"})
    model_root = tmp_path / "model-fixtures"
    write_snapshot(model_root / "perlector", dict(FILES))
    (model_root / "escape-link").symlink_to(outside, target_is_directory=True)
    pin = pin_snapshot(outside, tmp_path / "manifests" / "perlector.json")

    config = config_of(
        tmp_path,
        {"perlector": local_chair("perlector", pin, path="escape-link")},
        model_root="model-fixtures",
    )
    registry = ChairRegistry(config, manifest_root=tmp_path)

    with pytest.raises(LocalPathRefusal, match="escapes model_root"):
        registry.ensure(registry.resolve("perlector"))


def test_a_symlinked_subdirectory_inside_the_snapshot_is_refused(tmp_path):
    """The escape one level down: the snapshot root is legitimate, and a
    directory inside it points at bytes the model root does not hold."""
    outside = write_snapshot(tmp_path / "outside", {"weights.bin": b"somewhere else\n"})
    model_root = tmp_path / "model-fixtures"
    snapshot = write_snapshot(model_root / "perlector", {"config.json": b"{}\n"})
    (snapshot / "nested").symlink_to(outside, target_is_directory=True)
    pin = pin_snapshot(tmp_path / "outside", tmp_path / "manifests" / "perlector.json")

    config = config_of(
        tmp_path, {"perlector": local_chair("perlector", pin)}, model_root="model-fixtures"
    )
    registry = ChairRegistry(config, manifest_root=tmp_path)

    with pytest.raises(DigestMismatchRefusal, match="symlink directory"):
        registry.ensure(registry.resolve("perlector"))


def test_a_manifest_path_that_escapes_its_own_root_is_refused(tmp_path):
    """`manifest` is resolved under the config file's directory for the same
    reason `path` is resolved under the model root."""
    with pytest.raises(ConfigurationRefusal, match="safe relative POSIX path"):
        config_of(
            tmp_path,
            {"perlector": local_chair("perlector", "d" * 64, manifest="../elsewhere.json")},
            model_root="model-fixtures",
        )


def test_resolve_local_path_refuses_an_identity_that_is_not_a_local_chair(tmp_path):
    from .conftest import hf_chair

    identity = config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", "d" * 64)}).chairs[
        "attestator_1"
    ]
    with pytest.raises(LocalPathRefusal, match="non-local identity"):
        resolve_local_path(identity, tmp_path)


def test_a_model_root_that_is_not_a_directory_is_refused(tmp_path):
    registry, snapshot, _ = _world(tmp_path)
    identity = registry.resolve("perlector")
    with pytest.raises(LocalPathRefusal, match="not a directory"):
        resolve_local_path(identity, tmp_path / "no-such-root")
