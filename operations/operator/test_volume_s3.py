"""The network-volume target, exercised against an injected client only.

Nothing in this file builds a real boto3 client, reads a real credential, or
reaches a network. What is tested is the classification this adapter does — which
answers mean "absent", which mean "broken", and what it records so a later check
can verify a file — because those are the decisions that decide whether an
unverified file can be counted as sent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from operations.pod.transfer import ChecksummedTransfer

from .errors import ErrorCode, OperatorError
from .test_surface import _manifest, _spend_policy, _surface
from .volume_s3 import (
    SHA256_METADATA_KEY,
    S3VolumeTarget,
    VolumeSpec,
    VolumeTransferRefusal,
    build_client,
)

EXPECTED_SYNTHETIC_PAGE_SHA256 = "b08f1bf7a42942ccf7e5fa645f6b0ed50cf5caa4ebfe1e8bcbbbc1c5effbdac4"


class FakeS3Client:
    """The smallest thing shaped like the two boto3 calls this adapter makes."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.head_error: Exception | None = None
        self.upload_error: Exception | None = None
        self.drop_metadata = False
        self.uploads: list[str] = []

    def head_object(self, *, Bucket: str, Key: str):  # noqa: N803 - boto3's own names
        del Bucket
        if self.head_error is not None:
            raise self.head_error
        if Key not in self.objects:
            raise _client_error("404", 404)
        payload, metadata = self.objects[Key]
        return {"ContentLength": len(payload), "Metadata": {} if self.drop_metadata else metadata}

    def upload_fileobj(self, *, Fileobj, Bucket: str, Key: str, ExtraArgs=None, Config=None):  # noqa: N803
        del Bucket, Config
        if self.upload_error is not None:
            raise self.upload_error
        self.uploads.append(Key)
        self.objects[Key] = (
            Fileobj.read(),
            dict((ExtraArgs or {}).get("Metadata", {})),
        )


class _ResponseError(Exception):
    def __init__(self, response: dict) -> None:
        super().__init__(str(response))
        self.response = response


def _client_error(code: str, status: int) -> _ResponseError:
    return _ResponseError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}})


def _spec() -> VolumeSpec:
    return VolumeSpec(datacenter_id="EU-CZ-1", volume_id="fixture-volume-id")


def test_the_endpoint_lowercases_the_datacenter_and_the_region_does_not() -> None:
    """RunPod's own inconsistency, preserved rather than tidied into a bug."""

    spec = _spec()

    assert spec.endpoint_url == "https://s3api-eu-cz-1.runpod.io/"
    assert spec.datacenter_id == "EU-CZ-1"
    assert "EU-CZ-1" in spec.describe() and "fixture-volume-id" in spec.describe()


@pytest.mark.parametrize("bad", ("eu-cz-1", "EUCZ1", "", "EU-CZ", "EU_CZ_1"))
def test_a_datacenter_that_is_not_the_documented_shape_is_refused(bad: str) -> None:
    with pytest.raises(VolumeTransferRefusal):
        VolumeSpec(datacenter_id=bad, volume_id="v")


def test_a_missing_storage_credential_is_refused_before_any_client_is_built() -> None:
    with pytest.raises(VolumeTransferRefusal, match="RUNPOD_S3_ACCESS_KEY"):
        build_client(_spec(), environ={})


def test_an_absent_object_is_absent_and_a_broken_credential_is_not(tmp_path: Path) -> None:
    """The distinction the whole adapter turns on.

    Reading `AccessDenied` as "absent" would turn one wrong key into a full,
    silent re-upload on every single run, with nothing verified at the end.
    """

    client = FakeS3Client()
    target = S3VolumeTarget(_spec(), client=client)

    assert target.inspect("volume/page.bin") is None

    client.head_error = _client_error("AccessDenied", 403)
    with pytest.raises(VolumeTransferRefusal):
        target.inspect("volume/page.bin")

    client.head_error = _client_error("InternalError", 500)
    with pytest.raises(VolumeTransferRefusal):
        target.inspect("volume/page.bin")

    client.head_error = RuntimeError("the connection dropped")
    with pytest.raises(VolumeTransferRefusal):
        target.inspect("volume/page.bin")


def test_an_uploaded_file_reads_back_with_the_digest_it_was_tagged_with(tmp_path: Path) -> None:
    client = FakeS3Client()
    target = S3VolumeTarget(_spec(), client=client)
    source = tmp_path / "page.bin"
    source.write_bytes(b"a synthetic page\n")

    with source.open("rb") as handle:
        target.put_file(
            "volume/page.bin",
            handle,
            expected_sha=EXPECTED_SYNTHETIC_PAGE_SHA256,
        )
    observed = target.inspect("volume/page.bin")

    assert observed is not None
    assert observed.size == source.stat().st_size
    assert observed.sha256 == EXPECTED_SYNTHETIC_PAGE_SHA256
    assert (
        client.objects["volume/page.bin"][1][SHA256_METADATA_KEY] == EXPECTED_SYNTHETIC_PAGE_SHA256
    )


def test_an_injected_client_still_gets_the_documented_transfer_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = object()
    monkeypatch.setattr(
        "operations.operator.volume_s3.default_transfer_config",
        lambda: configured,
    )

    target = S3VolumeTarget(_spec(), client=FakeS3Client())

    assert target.transfer_config is configured


def test_an_object_without_our_digest_is_refused_and_not_overwritten(tmp_path: Path) -> None:
    """Unknown ownership cannot be converted into permission to overwrite."""

    client = FakeS3Client()
    target = S3VolumeTarget(_spec(), client=client)
    source = tmp_path / "page.bin"
    source.write_bytes(b"a synthetic page\n")
    with source.open("rb") as handle:
        target.put_file(
            "volume/page.bin",
            handle,
            expected_sha=EXPECTED_SYNTHETIC_PAGE_SHA256,
        )
    client.drop_metadata = True

    with pytest.raises(VolumeTransferRefusal, match="was not overwritten"):
        target.inspect("volume/page.bin")

    assert client.uploads == ["volume/page.bin"]


def test_a_head_response_of_the_wrong_shape_is_a_named_refusal_not_a_traceback() -> None:
    """A remote server's metadata shape is not ours to assume."""

    class WrongShapeClient(FakeS3Client):
        def head_object(self, *, Bucket: str, Key: str):  # noqa: N803 - boto3's own names
            del Bucket, Key
            return {"ContentLength": 12, "Metadata": "not a mapping"}

    with pytest.raises(VolumeTransferRefusal, match="was not overwritten"):
        S3VolumeTarget(_spec(), client=WrongShapeClient()).inspect("volume/page.bin")


def test_an_error_response_of_the_wrong_shape_is_not_read_as_absence() -> None:
    client = FakeS3Client()
    client.head_error = _ResponseError({"Error": "not a mapping", "ResponseMetadata": 404})

    with pytest.raises(VolumeTransferRefusal):
        S3VolumeTarget(_spec(), client=client).inspect("volume/page.bin")


def test_head_metadata_keys_are_read_case_insensitively() -> None:
    client = FakeS3Client()
    client.objects["volume/page.bin"] = (
        b"payload",
        {SHA256_METADATA_KEY.upper(): "a" * 64},
    )

    observed = S3VolumeTarget(_spec(), client=client).inspect("volume/page.bin")

    assert observed is not None
    assert observed.sha256 == "a" * 64


def test_a_target_that_never_returns_our_digest_cannot_be_called_complete(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    client.drop_metadata = True
    source, manifest = _manifest(tmp_path)

    with pytest.raises(VolumeTransferRefusal, match="exists without the digest"):
        ChecksummedTransfer(
            source_root=source,
            submission_manifest=manifest,
            target=S3VolumeTarget(_spec(), client=client),
            prefix="volume",
            journal_path=tmp_path / "journal.json",
        ).resume()

    assert client.uploads == ["volume/page-one.bin"]


def test_a_target_check_failure_reaches_the_three_part_upload_error(tmp_path: Path) -> None:
    surface = _surface(tmp_path)
    source, manifest = _manifest(tmp_path)
    client = FakeS3Client()
    client.objects["volume/page-one.bin"] = (b"unknown prior bytes", {})

    with pytest.raises(OperatorError) as failure:
        surface.upload(
            source,
            sealed_manifest=manifest,
            target=S3VolumeTarget(_spec(), client=client),
        )

    assert failure.value.code is ErrorCode.UPLOAD_PARTIAL
    assert client.uploads == []


def test_a_contradictory_access_denied_404_is_not_absence() -> None:
    client = FakeS3Client()
    client.head_error = _client_error("AccessDenied", 404)

    with pytest.raises(VolumeTransferRefusal):
        S3VolumeTarget(_spec(), client=client).inspect("volume/page.bin")


class _BrokenHandle:
    """A handle that fails mid-read, the one way `put_file` can still see a bad source.

    Refusing a symlink or a missing file is no longer this class's job: the
    caller opens and verifies the source exactly once, with `O_NOFOLLOW`,
    before `put_file` ever sees it (`operations.pod.transfer.TransferTarget`'s
    own docstring), and that refusal is covered by
    `operations/pod/test_transfer.py`'s
    `test_open_verified_regular_file_refuses_a_symlink_leaf_directly`. What is
    still this class's job is turning a failure reading the handle it was
    given into a named `VolumeTransferRefusal`, not a raw exception.
    """

    def read(self, size: int = -1) -> bytes:
        raise OSError("injected read failure")

    def seek(self, _offset: int) -> int:
        return 0


def test_a_read_failure_on_the_handle_is_a_named_refusal() -> None:
    target = S3VolumeTarget(_spec(), client=FakeS3Client())

    with pytest.raises(VolumeTransferRefusal, match="reading the source or writing"):
        target.put_file("volume/broken.bin", _BrokenHandle(), expected_sha="0" * 64)


def test_an_os_error_from_the_upload_names_source_or_network_without_guessing(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    client.upload_error = OSError("connection reset")
    source = tmp_path / "page.bin"
    source.write_bytes(b"payload")

    with source.open("rb") as handle:
        with pytest.raises(VolumeTransferRefusal, match="reading the source or writing"):
            S3VolumeTarget(_spec(), client=client).put_file(
                "volume/page.bin", handle, expected_sha="0" * 64
            )


def test_upload_through_the_surface_sends_only_files_named_by_the_sealed_record(
    tmp_path: Path,
) -> None:
    """The injected client exercises the real target adapter without network access."""

    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    source, manifest = _manifest(tmp_path)
    (source / "not-in-the-sealed-record.bin").write_bytes(b"never sent\n")
    client = FakeS3Client()

    receipt = surface.upload(
        source, sealed_manifest=manifest, target=S3VolumeTarget(_spec(), client=client)
    )

    assert receipt.is_file()
    assert sorted(client.uploads) == ["volume/page-one.bin", "volume/page-two.bin"]
    assert client.objects["volume/page-one.bin"][0] == (source / "page-one.bin").read_bytes()
    assert client.objects["volume/page-two.bin"][0] == (source / "page-two.bin").read_bytes()
    assert any("zero GPU-hours" in line for line in messages)
    payload = surface.receipts.read(surface._descriptor_receipt("upload"))["payload"]
    assert payload["state"] == "complete"
    assert "fixture volume" not in payload["summary"]


def test_naming_a_volume_says_what_will_be_contacted_before_anything_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    source, manifest = _manifest(tmp_path)
    # Explicitly absent, not ambiently absent: this test's refusal must come
    # from missing credentials, the same refusal a real operator would see,
    # not merely from whatever happens to be unset in the environment this
    # suite runs in. With both variables actually exported and boto3 installed,
    # an unguarded test here would build a real client and reach the network
    # before the assertion below ever ran.
    monkeypatch.delenv("RUNPOD_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("RUNPOD_S3_SECRET_KEY", raising=False)

    with pytest.raises(OperatorError) as refusal:
        surface.upload(source, sealed_manifest=manifest, volume=_spec())

    assert refusal.value.code is ErrorCode.UPLOAD_VOLUME_UNAVAILABLE

    assert any("https://s3api-eu-cz-1.runpod.io/" in line for line in messages)
    assert any("Nothing outside that sealed record is read or sent." in line for line in messages)
    assert any("zero GPU-hours" in line for line in messages)


def test_a_rehearsal_with_no_volume_named_still_uses_the_local_fixture(tmp_path: Path) -> None:
    messages: list[str] = []
    surface = _surface(tmp_path, output=messages)
    source, manifest = _manifest(tmp_path)
    _spend_policy(tmp_path)

    surface.upload(source, sealed_manifest=manifest)

    assert any("fixture volume" in line for line in messages)
    assert not any("runpod.io" in line for line in messages)
