"""Synthetic-only candidate adapter used to exercise the instrument's interface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .models import CandidateRequest, CandidateResponse, Condition, OutputStatus, ResolvedIdentity


@dataclass(frozen=True, slots=True)
class FakeReply:
    """A scripted response with no relation to any real page or model."""

    status: OutputStatus = OutputStatus.COMPLETE
    text: str | None = "alpha beta"
    elapsed_ms: float | None = 1.0
    cost_usd: float | None = 0.0
    prompt_digest_override: str | None = None
    dossier_digest_override: str | None = None
    delivery_digest_override: str | None = None


@dataclass(slots=True)
class FakeCandidate:
    """A transparent in-memory fake; its request log is a prompt/modality test oracle."""

    _identity: ResolvedIdentity
    replies: Mapping[tuple[str, Condition], FakeReply] = field(default_factory=dict)
    requests: list[CandidateRequest] = field(default_factory=list)

    @property
    def identity(self) -> ResolvedIdentity:
        return self._identity

    def read(self, request: CandidateRequest) -> CandidateResponse:
        self.requests.append(request)
        reply = self.replies.get(
            (request.dossier.opaque_act_id, request.dossier.condition), FakeReply()
        )
        return CandidateResponse(
            status=reply.status,
            text=reply.text,
            elapsed_ms=reply.elapsed_ms,
            cost_usd=reply.cost_usd,
            # `is None`, not `or`: an override of `""` is falsy, so `or` handed
            # back the correct digest and a test written to prove the runner
            # refuses a blank observed receipt was proving the opposite.
            observed_prompt_sha256=(
                request.prompt_format_sha256
                if reply.prompt_digest_override is None
                else reply.prompt_digest_override
            ),
            observed_dossier_sha256=(
                request.dossier.wire_sha256
                if reply.dossier_digest_override is None
                else reply.dossier_digest_override
            ),
            observed_delivery_sha256=(
                request.delivery_sha256
                if reply.delivery_digest_override is None
                else reply.delivery_digest_override
            ),
        )
