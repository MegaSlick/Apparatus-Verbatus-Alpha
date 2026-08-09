from operations.spike_perlector.protocol import (
    PREDECLARED_PROTOCOL_SHA256,
    protocol_document_sha256,
)


def test_predeclared_protocol_pin_matches_the_committed_protocol_document():
    assert protocol_document_sha256() == PREDECLARED_PROTOCOL_SHA256
