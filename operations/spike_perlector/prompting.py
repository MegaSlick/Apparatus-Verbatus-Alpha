"""Exact prompt-format registry and byte-fidelity boundary.

The registry stores a candidate's *declared format bytes*, not a made-up generic
chat schema.  A real candidate cannot enter a run until its owner supplies this
record.  The harness can prove what it passed to an adapter, not how an opaque
vendor implementation internally interprets those bytes; that limit is recorded in
the protocol rather than hidden as a stronger claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .encoding import canonical_json_bytes, is_sha256, sha256_bytes
from .errors import PromptFidelityRefusal
from .models import CandidateRequest, Dossier, ResolvedIdentity


@dataclass(frozen=True, slots=True)
class PromptFormat:
    """One exact candidate-owned format declaration, retained in private evidence."""

    candidate_key: str
    source_ref: str
    revision: str
    artifact_digest: str
    raw_bytes: bytes
    declared_sha256: str
    source_evidence_sha256: str
    modality_contract: str

    def __post_init__(self) -> None:
        if not isinstance(self.raw_bytes, bytes) or not self.raw_bytes:
            raise PromptFidelityRefusal("declared prompt-format bytes must be non-empty bytes")
        if sha256_bytes(self.raw_bytes) != self.declared_sha256:
            raise PromptFidelityRefusal("declared prompt-format bytes do not match their SHA-256")
        if not is_sha256(self.artifact_digest):
            raise PromptFidelityRefusal("prompt format has no candidate artifact SHA-256")
        if not is_sha256(self.source_evidence_sha256):
            raise PromptFidelityRefusal("prompt format has no source-evidence SHA-256")
        for name, value in (
            ("candidate_key", self.candidate_key),
            ("source_ref", self.source_ref),
            ("revision", self.revision),
            ("modality_contract", self.modality_contract),
        ):
            if not isinstance(value, str) or not value.strip():
                raise PromptFidelityRefusal(f"prompt format {name} must be non-empty")

    def verify_identity(self, identity: ResolvedIdentity) -> None:
        """Refuse a template recorded for a different model/revision/snapshot."""

        expected = (
            self.candidate_key,
            self.source_ref,
            self.revision,
            self.artifact_digest,
        )
        actual = (
            identity.candidate_key,
            identity.source_ref,
            identity.revision,
            identity.artifact_digest,
        )
        if expected != actual:
            raise PromptFidelityRefusal(
                "declared prompt format does not bind the resolved candidate identity"
            )

    def private_record(self) -> dict[str, str]:
        """A raw-byte-safe declaration snapshot for the private run-plan digest."""

        return {
            "candidate_key": self.candidate_key,
            "source_ref": self.source_ref,
            "revision": self.revision,
            "artifact_digest": self.artifact_digest,
            "declared_sha256": self.declared_sha256,
            "source_evidence_sha256": self.source_evidence_sha256,
            "modality_contract": self.modality_contract,
        }


class PromptRegistry:
    """Closed generic lookup; it never falls back to another candidate's template."""

    def __init__(self, formats: Mapping[str, PromptFormat]) -> None:
        copied = dict(formats)
        if not copied:
            raise PromptFidelityRefusal("prompt registry may not be empty")
        for candidate_key, prompt_format in copied.items():
            if candidate_key != prompt_format.candidate_key:
                raise PromptFidelityRefusal("prompt registry key does not match its declaration")
        self._formats: Mapping[str, PromptFormat] = MappingProxyType(copied)

    def format_for(self, identity: ResolvedIdentity) -> PromptFormat:
        try:
            prompt_format = self._formats[identity.candidate_key]
        except KeyError as error:
            raise PromptFidelityRefusal(
                f"no declared prompt format for candidate {identity.candidate_key!r}"
            ) from error
        prompt_format.verify_identity(identity)
        return prompt_format

    def request_for(self, identity: ResolvedIdentity, dossier: Dossier) -> CandidateRequest:
        """Build a request that carries exactly the registered raw byte sequence."""

        prompt_format = self.format_for(identity)
        return CandidateRequest(
            dossier=dossier,
            prompt_format_bytes=prompt_format.raw_bytes,
            prompt_format_sha256=prompt_format.declared_sha256,
        )

    def snapshot_sha256(self, identities: tuple[ResolvedIdentity, ...]) -> str:
        """Digest the prompt declarations bound to one sealed candidate roster."""

        return sha256_bytes(
            canonical_json_bytes(
                [self.format_for(identity).private_record() for identity in identities]
            )
        )
