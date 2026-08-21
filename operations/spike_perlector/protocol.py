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
# metric selection. The four repairs: (1) un-parked rule-13 decisions — corrected
# the real adjective to `allographic-v1`, predeclared `graphemic-v1` because
# folding two glyph forms of one letter serves comparison against the ink, and
# narrowed Tyrel's run approval to prove-before-scale, spend, and disclosure;
# (2) bound data-gate approval to the repository's sole current policy
# identity/revision; (3) added closed coded limitations plus an unpublishable
# refusal; (4) made stale provenance claims self-contained. The protocol's measure
# definitions remain pinned and fail closed after this deliberate re-pin.
#
# Re-pinned 2026-08-15 to finish repair (4), which the pass above applied to three
# of four ruling citations. Section 7's denominator argument still cited "ruling 3"
# by number with a verbatim quotation, which is the exact pattern that pass removed
# elsewhere: a reader outside this repository cannot check a ledger that is not in
# it. It now states the ruling's substance and date inline, as the other three do,
# and all four say the ledger is *untracked* rather than outside the repository
# tree — `workbench/` is in the working tree, only gitignored. Prose only: the
# unbounded `(S + D + I) / N` denominator, its comparison with the OCR-D bounded
# form, and every bound, profile, and scoring rule are byte-identical.
#
# Re-pinned 2026-08-15 while closing the readiness audit's four decided
# report-only findings. Section 1 now records the session-written durable
# engineering-declaration artifact and the authorization evidence retained on a
# publishable run. Section 8 restores the complete enumeration of Tyrel's reserved
# judgments by naming the declaration that the pipeline is proven. These are prose
# corrections only: no measure, profile, bound, score, or result moved, and the
# opening disclaimer still records that no result exists.
#
# Re-pinned 2026-08-20, twice. First (commit dc4ab85c, recorded here after the
# fact — that re-pin landed without its paragraph, against this ledger's own
# convention): Tyrel ruled the training base starts fresh on 3.8, so Section 2's
# stock base moved from the 9B model to Qwen/Qwen3.8-27B. Second (this change):
# the provenance sentence carried the old ruling's 2026-08-05 date forward with
# the new model — a false attribution in the one sentence that exists to make
# provenance verifiable — caught by the 2026-08-20 trust audit. It now names the
# 2026-08-20 ruling and the supersession. The ruling's record:
# workbench/standing/TYREL_RULINGS_2026-08-20_EVENING.md (untracked). Prose and
# roster provenance only: no measure, profile, bound, score, or result moved.
PREDECLARED_PROTOCOL_SHA256 = "61dc492686a1db79a9125f4d2779549c43d05393174240800e11fa3bcdb6ee8a"


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
