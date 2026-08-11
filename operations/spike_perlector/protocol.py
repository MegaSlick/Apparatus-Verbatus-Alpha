"""Bind declared runs to the exact committed pre-measurement protocol artifact."""

from __future__ import annotations

from pathlib import Path

from .encoding import sha256_bytes
from .errors import MatrixRefusal

# Updated only in the same reviewable change as the protocol document itself.
#
# Re-pinned 2026-08-11 for one correction, and the reason is recorded because a
# re-pin is the one act this constant exists to make deliberate: the dependency
# table attributed `uniseg` to `github.com/rivo/uniseg-python`, which is a 404, and
# `github.com/rivo/uniseg` is a **different project** — a Go library by another
# author. The installed distribution's own metadata names Masaaki Shibata and
# `bitbucket.org/emptypage/uniseg-py`, MIT. CLAUDE.md's Quarantine section requires
# third-party code to be recorded with its real source and licence, and a wrong
# attribution of someone else's MIT-licensed work is a factual error about a third
# party rather than a choice about this instrument's measures. Nothing measured
# changed: no bound, no profile, no scoring rule, no identifier.
#
# **What was deliberately not corrected in the same pass:** `ALLOGRAPHIC_V1` carries
# the identifier `"allographetic-v1"`, which is not a word. That *is* measure-adjacent
# — the identifier is named in this document, in the public finding schema, and is
# one of the two profiles Tyrel selects between before the evaluation manifest opens —
# so it is his to decide and is carried to him rather than folded into this re-pin.
PREDECLARED_PROTOCOL_SHA256 = "c33e5f775722be9965fe17c3e0bd8106d3e69c30560968ff43d40e0dee57515b"


def protocol_document_sha256() -> str:
    """Return the digest of the committed protocol document, not a caller assertion."""

    return sha256_bytes(Path(__file__).with_name("README.md").read_bytes())


def require_predeclared_protocol() -> str:
    """Refuse a declared run if its committed protocol is no longer the pinned artifact."""

    observed = protocol_document_sha256()
    if observed != PREDECLARED_PROTOCOL_SHA256:
        raise MatrixRefusal(
            "the committed Spec 05 protocol document differs from its predeclared digest"
        )
    return PREDECLARED_PROTOCOL_SHA256
