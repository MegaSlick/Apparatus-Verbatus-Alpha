"""Synthetic-only candidate adapter used to exercise the instrument's interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .models import CandidateRequest, CandidateResponse, Condition, OutputStatus, ResolvedIdentity


@dataclass(frozen=True, slots=True)
class FakeReply:
    """A scripted response with no relation to any real page or model."""

    status: OutputStatus = OutputStatus.COMPLETE
    text: str | None = ""
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
            observed_prompt_sha256=reply.prompt_digest_override or request.prompt_format_sha256,
            observed_dossier_sha256=reply.dossier_digest_override or request.dossier.wire_sha256,
            observed_delivery_sha256=reply.delivery_digest_override or request.delivery_sha256,
        )
