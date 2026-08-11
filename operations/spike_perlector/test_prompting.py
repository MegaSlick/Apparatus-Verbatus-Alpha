import pytest

from operations.spike_perlector.errors import PromptFidelityRefusal
from operations.spike_perlector.models import Condition, dossier_for
from operations.spike_perlector.prompting import PromptFormat, PromptRegistry
from operations.spike_perlector.testkit import evaluation_act, identity, prompt_format, registry


def test_registry_passes_the_declared_format_byte_for_byte():
    candidate = identity("synthetic-a", 1)
    raw = b"\x00exact\xffformat\n"
    prompt = prompt_format(candidate, raw)
    registered = PromptRegistry({candidate.candidate_key: prompt})
    request = registered.request_for(
        candidate, dossier_for(evaluation_act(), Condition.LECTIO_NUDA)
    )
    assert request.prompt_format_bytes == raw
    assert request.prompt_format_sha256 == prompt.declared_sha256


def test_prompt_digest_tampering_is_refused():
    candidate = identity("synthetic-a", 1)
    raw = b"exact"
    with pytest.raises(PromptFidelityRefusal, match="do not match"):
        PromptFormat(
            candidate_key=candidate.candidate_key,
            source_ref=candidate.source_ref,
            revision=candidate.revision,
            artifact_digest=candidate.artifact_digest,
            raw_bytes=raw,
            declared_sha256="0" * 64,
            source_evidence_sha256="1" * 64,
            modality_contract="image-and-testimonia-v1",
        )


def test_unknown_candidate_cannot_fall_back_to_another_prompt_format():
    known = identity("synthetic-known", 1)
    unknown = identity("synthetic-unknown", 2)
    with pytest.raises(PromptFidelityRefusal, match="no declared prompt"):
        registry(known).request_for(
            unknown, dossier_for(evaluation_act(), Condition.WITNESS_PRIMED)
        )


def test_prompt_format_binds_its_model_revision_and_digest():
    candidate = identity("synthetic-a", 1)
    changed = identity("synthetic-a", 1, source_ref="synthetic/a-other")
    with pytest.raises(PromptFidelityRefusal, match="does not bind"):
        registry(candidate).request_for(
            changed, dossier_for(evaluation_act(), Condition.LECTIO_NUDA)
        )
