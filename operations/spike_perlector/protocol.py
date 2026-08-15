"""Bind declared runs to the exact committed pre-measurement protocol artifact."""

from __future__ import annotations

from pathlib import Path

from .encoding import is_sha256, sha256_bytes
from .errors import MatrixRefusal

# Updated only in the same reviewable change as the protocol document itself.
#
# Re-pinned 2026-08-11 for Tyrel's two binding survival rulings: every selected
# act is read every time, and a missing-text reference cannot end its life. The
# protocol previously said blank and unresolved references were excluded from
# the matrix and that one unproved adapter delivery invalidated the measurement
# immediately. It now states the implemented contract: those acts receive every
# planned Perlector read but no invented CER/WER denominator; failed deliveries
# are retained while later reads continue; a Testimonium binds its exact act,
# crop and delivery-attempt state; and incomplete delivery evidence cannot
# publish. These are the settled survival semantics, not a new picker or score.
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
# Re-pinned 2026-08-15 for the four readiness-audit repairs, all decided before
# results exist. The README's opening disclaimer states that this instrument has
# no evaluation image, transcription, model call, pod, or reading-quality number
# and is “not a result,” so this is pre-measurement correction rather than post-hoc
# metric selection. The session corrected the real adjective to `allographic-v1`,
# predeclared `graphemic-v1` because folding two glyph forms of one letter serves
# comparison against the ink, narrowed Tyrel's run approval to prove-before-scale,
# spend, and disclosure, bound data-gate approval to the repository's sole current
# policy identity/revision, added closed coded limitations plus an unpublishable
# refusal, and made stale provenance claims self-contained. The protocol's measure
# definitions remain pinned and fail closed after this deliberate re-pin.
PREDECLARED_PROTOCOL_SHA256 = "48a558402b53c8404a38860beadf181b980c8322843e0abfd85e7814fd39ab8a"


def protocol_document_sha256() -> str:
    """Return the digest of the committed protocol document, not a caller assertion."""

    path = Path(__file__).with_name("README.md")
    try:
        payload = path.read_bytes()
    except OSError as error:
        # An absent or unreadable protocol document is the same refusal as a
        # changed one: the run cannot show which protocol it ran under. Raised
        # by name because every caller holds on `MatrixRefusal`, and a bare
        # `OSError` out of a gate is not caught by any of them.
        raise MatrixRefusal(
            f"the committed Spec 05 protocol document cannot be read at {path.name}: {error}"
        ) from error
    return sha256_bytes(payload)


def require_predeclared_protocol() -> str:
    """Refuse a declared run if its committed protocol is no longer the pinned artifact."""

    # The pin itself first. A malformed or miscased constant can never equal a
    # measured digest, so the run would refuse with "the protocol differs" and
    # send the reader to diff a document that had not changed.
    if not is_sha256(PREDECLARED_PROTOCOL_SHA256):
        raise MatrixRefusal(
            "the predeclared Spec 05 protocol pin is not a lowercase SHA-256: "
            f"{PREDECLARED_PROTOCOL_SHA256!r}"
        )
    observed = protocol_document_sha256()
    if observed != PREDECLARED_PROTOCOL_SHA256:
        raise MatrixRefusal(
            "the committed Spec 05 protocol document differs from its predeclared digest: "
            f"observed {observed}, predeclared {PREDECLARED_PROTOCOL_SHA256}"
        )
    # The measured digest, not the constant it matched. They are equal here by
    # the check above; returning the measurement keeps the value a reading.
    return observed
