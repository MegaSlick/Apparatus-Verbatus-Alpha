import pytest

from operations.spike_perlector.errors import MatrixRefusal
from operations.spike_perlector.protocol import (
    PREDECLARED_PROTOCOL_SHA256,
    protocol_document_sha256,
    require_predeclared_protocol,
)


def test_predeclared_protocol_pin_matches_the_committed_protocol_document():
    assert protocol_document_sha256() == PREDECLARED_PROTOCOL_SHA256


def test_an_unreadable_protocol_document_refuses_by_name_not_as_an_os_error(monkeypatch):
    """A caller holding on `MatrixRefusal` would not have caught a bare `OSError`.

    The same defect class this branch already fixed in `gates.py`, where strict
    canonicalization's `TypeError` left a gate unconverted.
    """

    def unreadable(self):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr("pathlib.Path.read_bytes", unreadable)
    with pytest.raises(MatrixRefusal, match="cannot be read at README.md"):
        protocol_document_sha256()


def test_a_protocol_mismatch_names_both_digests_rather_than_asserting_one(monkeypatch):
    """A refusal that names neither digest cannot be acted on by the reader."""

    monkeypatch.setattr(
        "operations.spike_perlector.protocol.protocol_document_sha256",
        lambda: "0" * 64,
    )
    with pytest.raises(MatrixRefusal) as refusal:
        require_predeclared_protocol()
    assert "0" * 64 in str(refusal.value)
    assert PREDECLARED_PROTOCOL_SHA256 in str(refusal.value)
