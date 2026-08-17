"""Spec 02, test 2 — Verification.

"A fixture snapshot with one flipped byte fails **naming the file**; a complete
match passes; an extra file fails; a partial cache re-fetches exactly the missing
files. Network is mocked, and the spec says plainly what that means: this
measures the call the mock received, not Hugging Face's behaviour."

Two more clauses from the same section are checked here, because nothing else
would catch them: verification covers the *whole* fetched snapshot, and "a failed
verification leaves the previously verified snapshot untouched".

Every fetch below goes through `RecordingFetcher`, the one seam. No test in this
file asserts anything about Hugging Face; each asserts what the registry asked
the seam for, and what it did with what came back.
"""

import json

import pytest

from common.chairs.errors import DigestMismatchRefusal, UnresolvedChairRefusal
from common.chairs.manifests import (
    build_manifest,
    file_digest,
    manifest_digest,
    read_manifest,
    write_manifest,
)
from common.chairs.registry import CACHE_DESCRIPTOR
from common.contracts.canonical import canonical_bytes, digest_bytes

from .conftest import (
    RecordingFetcher,
    config_of,
    hf_chair,
    pin_snapshot,
    registry_for,
    write_snapshot,
)


def test_file_digest_streams_the_snapshot_file_in_bounded_chunks(tmp_path, monkeypatch):
    """Large model weights are hashed in bounded chunks, never loaded whole.

    ``Path.read_bytes`` is blocked outright, and every read on the opened handle
    is bounded, so a regression to *either* whole-file shape — ``read_bytes()``
    or ``open().read()`` — fails by name. The bound is judged from the requested
    size, not the bytes returned, so the fixture file can stay small.
    """
    weights = tmp_path / "weights.bin"
    weights.write_bytes(b"fixture weights\n")

    def whole_file_read_would_be_a_memory_regression(_path):
        pytest.fail("snapshot digest read the entire file into memory")

    monkeypatch.setattr(type(weights), "read_bytes", whole_file_read_would_be_a_memory_regression)

    # Far above hashlib.file_digest's 256 KiB chunk, far below any real weights file.
    bound = 4 * 1024 * 1024

    class BoundedHandle:
        """Proxy that refuses any single read larger than the bound.

        Deliberately carries no ``getbuffer``: ``hashlib.file_digest`` takes the
        whole buffer at once down that path, which is exactly the shape refused.
        """

        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self._handle.__exit__(*exc_info)

        def readable(self):
            return self._handle.readable()

        def readinto(self, buffer):
            if len(buffer) > bound:
                pytest.fail("snapshot digest read the entire file into memory")
            return self._handle.readinto(buffer)

        def read(self, size=-1):
            if size is None or size < 0 or size > bound:
                pytest.fail("snapshot digest read the entire file into memory")
            return self._handle.read(size)

    real_open = type(weights).open

    def bounded_open(self, *args, **kwargs):
        return BoundedHandle(real_open(self, *args, **kwargs))

    monkeypatch.setattr(type(weights), "open", bounded_open)

    assert file_digest(weights, "attestator_1", "weights.bin") == digest_bytes(b"fixture weights\n")


# --- A complete match ---------------------------------------------------------------


def test_a_complete_match_verifies_and_fetches_exactly_the_pinned_paths(hf_world):
    identity = hf_world.identity()
    snapshot = hf_world.registry.ensure(identity)

    assert snapshot.identity == identity
    assert snapshot.manifest_digest == hf_world.pin
    # Exactly the pinned paths, in sorted order, and nothing besides.
    assert hf_world.fetcher.calls == [("attestator_1", ("config.json", "nested/weights.bin"))]
    assert (snapshot.root / CACHE_DESCRIPTOR).is_file()


def test_a_second_ensure_over_a_complete_cache_verifies_again_and_fetches_nothing(hf_world):
    """The re-verification GOVERNANCE 6 asks for has to be survivable.

    A registry that leaves its own bookkeeping inside the snapshot directory
    passes the first verification and then refuses every one after it, because
    its own marker file is an unpinned extra. The descriptor is excluded by name
    from the comparison, and this is the test that says so.
    """
    identity = hf_world.identity()
    first = hf_world.registry.ensure(identity)
    second = hf_world.registry.ensure(identity)

    assert second.root == first.root
    assert second.manifest_digest == first.manifest_digest
    assert len(hf_world.fetcher.calls) == 1, "a complete verified cache is not re-fetched"


# --- One flipped byte, a missing file, an extra file --------------------------------


def test_one_flipped_byte_fails_naming_that_file(hf_world):
    identity = hf_world.identity()
    hf_world.fetcher.files["nested/weights.bin"] = b"one corrupted byte lands here\n"

    with pytest.raises(DigestMismatchRefusal) as caught:
        hf_world.registry.ensure(identity)
    assert "nested/weights.bin" in str(caught.value)
    assert caught.value.chair == "attestator_1"


def test_a_file_the_fetch_never_produced_fails_naming_it(hf_world):
    identity = hf_world.identity()

    class Partial(RecordingFetcher):
        def fetch(self, identity, destination, paths):
            super().fetch(identity, destination, tuple(p for p in paths if p != "config.json"))

    hf_world.registry.fetcher = Partial(hf_world.files)
    with pytest.raises(DigestMismatchRefusal) as caught:
        hf_world.registry.ensure(identity)
    assert "config.json" in str(caught.value)
    assert "missing" in str(caught.value)


def test_an_extra_file_is_a_mismatch_not_a_shrug(hf_world):
    """Verification covers the whole fetched snapshot: a file that arrived but is
    not pinned is exactly as unverifiable as one whose bytes changed."""
    identity = hf_world.identity()

    class Generous(RecordingFetcher):
        def fetch(self, identity, destination, paths):
            super().fetch(identity, destination, paths)
            (destination / "z-unpinned.json").write_bytes(b"{}\n")

    hf_world.registry.fetcher = Generous(hf_world.files)
    with pytest.raises(DigestMismatchRefusal) as caught:
        hf_world.registry.ensure(identity)
    assert "z-unpinned.json" in str(caught.value)
    assert "extra" in str(caught.value)


def test_a_file_of_the_right_digest_but_the_wrong_size_is_refused(tmp_path):
    """Size and digest are both pinned, so a manifest row that agrees with the
    bytes on only one of them is a manifest that has been edited."""
    files = {"weights.bin": b"fixture weights\n"}
    write_snapshot(tmp_path / "remote", files)
    manifest = build_manifest(tmp_path / "remote")
    tampered = [dict(row.to_record(), size=999) for row in manifest.rows]
    manifest_path = tmp_path / "manifests" / "attestator_1.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(canonical_bytes(tampered))
    pin = digest_bytes(manifest_path.read_bytes())

    config = config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", pin)})
    fetcher = RecordingFetcher(files)
    registry = registry_for(config, tmp_path, fetcher)

    with pytest.raises(DigestMismatchRefusal) as caught:
        registry.ensure(registry.resolve("attestator_1"))
    assert "size" in str(caught.value)


# --- A partial cache re-fetches exactly the missing files ---------------------------


def test_a_partial_cache_re_fetches_exactly_the_missing_files(hf_world):
    """The registry computes the missing set and asks for precisely that.

    This is the whole of the claim: the seam is handed one tuple of paths, and
    the assertion is on that tuple. The seam has no skip-if-present logic of its
    own, so a registry that simply re-requested everything would fail here.
    """
    identity = hf_world.identity()
    snapshot = hf_world.registry.ensure(identity)
    (snapshot.root / "nested/weights.bin").unlink()

    hf_world.registry.ensure(identity)

    assert hf_world.fetcher.calls == [
        ("attestator_1", ("config.json", "nested/weights.bin")),
        ("attestator_1", ("nested/weights.bin",)),
    ]


def test_a_cache_holding_a_file_the_pin_does_not_name_is_refused_before_any_refetch(hf_world):
    identity = hf_world.identity()
    snapshot = hf_world.registry.ensure(identity)
    (snapshot.root / "z-unpinned.json").write_bytes(b"{}\n")
    hf_world.fetcher.calls.clear()

    with pytest.raises(DigestMismatchRefusal) as caught:
        hf_world.registry.ensure(identity)
    assert "z-unpinned.json" in str(caught.value)
    assert hf_world.fetcher.calls == []


# --- A failed verification leaves the previously verified snapshot untouched --------


def test_a_failed_verification_leaves_the_previously_verified_snapshot_untouched(hf_world):
    identity = hf_world.identity()
    verified = hf_world.registry.ensure(identity)
    before = {
        path.name: path.read_bytes() for path in sorted(verified.root.rglob("*")) if path.is_file()
    }

    # The cache is complete, so the corrupted byte has to arrive on a re-fetch of
    # a file the cache lost — the shape a real interrupted download leaves.
    (verified.root / "config.json").unlink()
    hf_world.fetcher.files["config.json"] = b"corrupted on the way back\n"
    with pytest.raises(DigestMismatchRefusal):
        hf_world.registry.ensure(identity)

    surviving = {
        path.name: path.read_bytes() for path in sorted(verified.root.rglob("*")) if path.is_file()
    }
    assert surviving == {name: data for name, data in before.items() if name != "config.json"}
    assert not any(
        entry.name.startswith(".attestator_1.candidate-")
        for entry in hf_world.registry.cache_root.iterdir()
    ), "a refused candidate snapshot is removed rather than left beside the cache"


def test_a_failed_fetch_leaves_the_previously_verified_snapshot_untouched(hf_world):
    identity = hf_world.identity()
    verified = hf_world.registry.ensure(identity)
    kept = (verified.root / "config.json").read_bytes()

    (verified.root / "config.json").unlink()
    hf_world.fetcher.fail = RuntimeError("the connection went away mid-download")
    with pytest.raises(UnresolvedChairRefusal):
        hf_world.registry.ensure(identity)

    hf_world.fetcher.fail = None
    assert hf_world.registry.ensure(identity).root == verified.root
    assert (verified.root / "config.json").read_bytes() == kept


# --- The manifest artifact is the thing the pin names -------------------------------


def test_the_pin_names_the_manifest_artifact_s_exact_canonical_bytes(tmp_path):
    """Not "a JSON file that happens to parse to the same rows": the pin is the
    digest of the artifact, so a second serialization of the same content is a
    different artifact and is refused before verification is even considered."""
    write_snapshot(tmp_path / "snapshot", {"weights.bin": b"fixture bytes\n"})
    manifest_path = tmp_path / "manifests" / "attestator_1.json"
    pin = pin_snapshot(tmp_path / "snapshot", manifest_path)

    assert digest_bytes(manifest_path.read_bytes()) == pin
    assert read_manifest(manifest_path, expected_digest=pin, chair="attestator_1").rows

    manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    with pytest.raises(DigestMismatchRefusal, match="canonical artifact bytes"):
        read_manifest(manifest_path, expected_digest=pin, chair="attestator_1")


def test_a_manifest_whose_digest_is_not_the_pin_is_refused(tmp_path):
    write_snapshot(tmp_path / "snapshot", {"weights.bin": b"fixture bytes\n"})
    manifest_path = tmp_path / "manifests" / "attestator_1.json"
    pin_snapshot(tmp_path / "snapshot", manifest_path)

    with pytest.raises(DigestMismatchRefusal, match="expected digest"):
        read_manifest(manifest_path, expected_digest="0" * 64, chair="attestator_1")


@pytest.mark.parametrize(
    "rows",
    [
        {"rows": []},  # an object where the artifact is a bare sorted row list
        [{"path": "a", "sha256": "0" * 64}],  # no size
        [{"path": "a", "sha256": "0" * 64, "size": -1}],
        [{"path": "a", "sha256": "0" * 63, "size": 1}],
        [{"path": "../a", "sha256": "0" * 64, "size": 1}],
        [{"path": "/a", "sha256": "0" * 64, "size": 1}],
        [{"path": ".", "sha256": "0" * 64, "size": 1}],
        [  # "./" also names the directory itself: no parts, same rule as "."
            {"path": "./", "sha256": "0" * 64, "size": 1}
        ],
        [  # not sorted by path
            {"path": "b", "sha256": "0" * 64, "size": 1},
            {"path": "a", "sha256": "0" * 64, "size": 1},
        ],
        [  # the same path twice
            {"path": "a", "sha256": "0" * 64, "size": 1},
            {"path": "a", "sha256": "1" * 64, "size": 1},
        ],
    ],
)
def test_a_malformed_manifest_artifact_is_refused(tmp_path, rows):
    path = tmp_path / "manifest.json"
    path.write_bytes(canonical_bytes(rows))
    with pytest.raises(DigestMismatchRefusal):
        read_manifest(path, expected_digest=digest_bytes(path.read_bytes()), chair="attestator_1")


def test_a_manifest_that_cannot_be_read_at_all_is_refused_naming_the_chair(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_bytes(b"{not json at all")
    with pytest.raises(DigestMismatchRefusal) as caught:
        read_manifest(path, expected_digest="0" * 64, chair="attestator_1")
    assert caught.value.chair == "attestator_1"


def test_a_symlinked_file_inside_a_snapshot_is_refused_rather_than_followed(tmp_path):
    """Hashing through a symlink would let a snapshot verify against bytes that
    are not in it, and that live somewhere nothing pinned."""
    outside = write_snapshot(tmp_path / "outside", {"weights.bin": b"elsewhere\n"})
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "weights.bin").symlink_to(outside / "weights.bin")

    with pytest.raises(DigestMismatchRefusal, match="not a regular file"):
        build_manifest(root)


# --- The production client seam ------------------------------------------------------


def test_the_huggingface_adapter_passes_the_pin_through_to_the_official_client(tmp_path):
    """What the adapter asks the real client for: this repo, this revision, and
    exactly these paths. Nothing about the service's own behaviour is claimed."""
    from common.chairs.registry import HuggingFaceFetcher

    source = write_snapshot(tmp_path / "official-cache", {"nested/model.bin": b"official\n"})

    class OfficialClientFake:
        def __init__(self):
            self.kwargs = None

        def snapshot_download(self, **kwargs):
            self.kwargs = kwargs
            return source

    client = OfficialClientFake()
    config = config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", "d" * 64)})
    identity = config.chairs["attestator_1"]
    destination = tmp_path / "candidate"

    HuggingFaceFetcher(client).fetch(identity, destination, ("nested/model.bin",))

    assert client.kwargs == {
        "repo_id": "fixture-org/attestator_1",
        "revision": "a" * 40,
        "allow_patterns": ["nested/model.bin"],
    }
    assert (destination / "nested/model.bin").read_bytes() == b"official\n"


def test_the_huggingface_adapter_refuses_a_pinned_file_the_client_did_not_return(tmp_path):
    from common.chairs.registry import HuggingFaceFetcher

    source = write_snapshot(tmp_path / "official-cache", {"other.bin": b"not what was asked\n"})

    class OfficialClientFake:
        def snapshot_download(self, **kwargs):
            return source

    config = config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", "d" * 64)})
    with pytest.raises(UnresolvedChairRefusal, match="no requested file"):
        HuggingFaceFetcher(OfficialClientFake()).fetch(
            config.chairs["attestator_1"], tmp_path / "candidate", ("nested/model.bin",)
        )


def test_the_written_manifest_round_trips_through_its_own_reader(tmp_path):
    """The one writer and the one reader agree, which is what lets the pin be a
    constant the artifact must match rather than a value the artifact supplies."""
    root = write_snapshot(tmp_path / "snapshot", {"a.bin": b"a\n", "nested/b.bin": b"b\n"})
    manifest = build_manifest(root)
    path = tmp_path / "manifest.json"
    pin = write_manifest(manifest, path)

    assert json.loads(path.read_text(encoding="utf-8")) == manifest.to_record()
    assert manifest_digest(manifest) == pin
    assert read_manifest(path, expected_digest=pin, chair="attestator_1") == manifest
