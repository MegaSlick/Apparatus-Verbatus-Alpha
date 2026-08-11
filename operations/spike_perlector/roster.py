"""The settled candidate roster constraints, separate from per-candidate adapters."""

from __future__ import annotations

from dataclasses import dataclass

from .encoding import canonical_json_bytes, is_sha256, sha256_bytes
from .errors import CandidateRosterRefusal
from .models import DeliveryMode, ResolvedIdentity, repository_of

STOCK_BASE_SOURCE = "Qwen/Qwen3.5-9B"
FORBIDDEN_ATTESTATOR_SOURCE = "Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR"


def validate_perlector_candidate(identity: ResolvedIdentity) -> None:
    """Apply the structural no-self-witness rule at every execution boundary.

    **The comparison is normalized, because this is a structural rule and not a
    string match.** GOVERNANCE 3 and CLAUDE.md hard rule 8 say the Perlector reads
    and never picks among witnesses; a witness sitting in the candidate roster is
    that rule broken at the root. Compared exactly, a trailing space, a capital
    letter, or a `@revision` pin walked the Attestator straight through the one
    check standing in its way — and the refusal that did not fire is invisible.
    Found by CodeRabbit on the rebased branch.
    """

    if repository_of(identity.source_ref) == repository_of(FORBIDDEN_ATTESTATOR_SOURCE):
        raise CandidateRosterRefusal(
            "the Teklia RecordGold ATR model is an Attestator and cannot be a Perlector candidate"
        )


@dataclass(frozen=True, slots=True)
class CandidateRoster:
    """The three required candidates; this validates a roster, it never selects one."""

    stock_base: ResolvedIdentity
    vendor_unaltered: ResolvedIdentity
    trained_checkpoint: ResolvedIdentity
    vendor_unaltered_evidence_sha256: str
    checkpoint_repository_evidence_sha256: str

    def identities(self) -> tuple[ResolvedIdentity, ...]:
        return (self.stock_base, self.vendor_unaltered, self.trained_checkpoint)

    def private_record(self) -> dict[str, object]:
        """Private role bindings that a run-plan approval names by digest."""

        return {
            "schema": "spec05-candidate-roster.v1",
            "stock_base": self.stock_base.private_record(),
            "vendor_unaltered": self.vendor_unaltered.private_record(),
            "trained_checkpoint": self.trained_checkpoint.private_record(),
            "vendor_unaltered_evidence_sha256": self.vendor_unaltered_evidence_sha256,
            "checkpoint_repository_evidence_sha256": self.checkpoint_repository_evidence_sha256,
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.private_record()))

    def validate(self) -> None:
        """Refuse the structurally disqualified witness model and duplicate identities."""

        identities = self.identities()
        # Normalized, like every other source comparison in this method and for
        # the reason `repository_of` gives: a `source_ref` is typed by hand, so
        # the settled stock base arrives as `"Qwen/Qwen3.5-9B "`,
        # `"qwen/qwen3.5-9b"` or `"Qwen/Qwen3.5-9B@main"`. A raw compare refused
        # the correct model for a trailing space while the checks below it, on
        # the same field, accepted it.
        if repository_of(self.stock_base.source_ref) != repository_of(STOCK_BASE_SOURCE):
            raise CandidateRosterRefusal(
                f"stock base must be the settled {STOCK_BASE_SOURCE!r} candidate"
            )
        for identity in identities:
            validate_perlector_candidate(identity)
        candidate_keys = [identity.candidate_key for identity in identities]
        slots = [identity.public_slot for identity in identities]
        artifacts = [identity.artifact_digest for identity in identities]
        # Normalized, for the reason `repository_of` gives: three roles whose
        # `source_ref`s differ only by a trailing space or an `@revision` pin are
        # three names for one model, and this refusal exists to stop exactly that
        # roster. Three lines above the normalized check and still a raw compare
        # until the Opus read of this branch executed it.
        sources = [repository_of(identity.source_ref) for identity in identities]
        if len(set(candidate_keys)) != len(candidate_keys) or set(slots) != {1, 2, 3}:
            raise CandidateRosterRefusal(
                "candidate roster requires three distinct identities and slots"
            )
        if len(set(artifacts)) != len(artifacts) or len(set(sources)) != len(sources):
            raise CandidateRosterRefusal(
                "candidate roles must name three distinct source repositories and artifacts"
            )
        if self.stock_base.delivery is not DeliveryMode.LOCAL:
            raise CandidateRosterRefusal("the settled stock base is a local Perlector candidate")
        if self.vendor_unaltered.delivery is not DeliveryMode.EXTERNAL:
            raise CandidateRosterRefusal(
                "the unaltered vendor candidate must use the external interface"
            )
        if self.trained_checkpoint.delivery is not DeliveryMode.LOCAL:
            raise CandidateRosterRefusal(
                "Tyrel's trained checkpoint must resolve from its own local repository"
            )
        if not is_sha256(self.vendor_unaltered_evidence_sha256):
            raise CandidateRosterRefusal("vendor candidate has no unaltered-model evidence digest")
        if not is_sha256(self.checkpoint_repository_evidence_sha256):
            raise CandidateRosterRefusal("checkpoint has no own-repository evidence digest")
