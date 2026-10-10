from __future__ import annotations

import argparse
import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from common.chairs.config import parse_models_config
from common.chairs.models import ChairIdentity, is_sha256
from common.chairs.receipts import validate_receipt
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import ContractError
from common.contracts.serving import SERVING_CONFIG_INPUTS_SCHEMA, SERVING_LAUNCH_AUDIT_SCHEMA
from common.sealed_config import parse_sealed_toml
from operations.pod.durable import exclusive_write

from .config import (
    FixtureProfile,
    InProcessProfile,
    ServingProfile,
    ServingRecipes,
    SubprocessProfile,
    UnsupportedProfile,
    chair_preflight_identity_digest,
    parse_serving_recipes,
    profile_preflight_digest,
)
from .errors import ServingConfigurationError
from .recordgold_smoke import (
    DAI_WITNESS_ADAPTER,
    RECORDGOLD_SMOKE_MAX_CER,
    RECORDGOLD_SMOKE_PROFILE,
    RECORDGOLD_SMOKE_RECORD,
    RecordGoldSmokeRecord,
    RecordGoldSmokeRefusal,
    committed_recordgold_bytes,
    fetch_recordgold_smoke_page,
    score_recordgold_answer,
)
from .smoke import page_witness_edit_distance
from .witness import is_page_witness

DESCRIPTION = """Verify a real-silicon preflight and render profile proof candidates.

This command never edits the serving catalogue.  It turns durable runtime
evidence into the exact identity and profile digests a reviewer may stamp on
the one tier that was measured.  Normal serving remains unable to launch an
unproven row.
"""

SCHEMA = "serving-qualification-candidates.v2"
QUALIFICATION_PURPOSE = "preflight-qualification"
# Catalogue row kinds a stage runs itself, never served; preflight places each
# chair under its row's kind. An `unsupported` row placed at a tier makes the
# preflight red, so a report qualify accepts never holds one.
UNSERVED_KINDS = frozenset({"subprocess", "in-process"})
# What a smoke receipt says its chair read (`smoke.py` writes `smoke_page`).
# Every served chair reads the golden page, except the DAI chair, whose smoke
# reads the pinned RecordGold record (`recordgold_smoke.py`). A receipt that
# names neither is refused.
GOLDEN_PAGE = "golden-page"
RECORDGOLD_PAGE = "recordgold-record"


class QualificationRefusal(ValueError):
    """The supplied evidence cannot justify a profile proof mark."""


def qualification_candidates(
    *,
    report_path: str | Path,
    evidence_root: str | Path,
    models_config: str | Path,
    recipes_config: str | Path,
    placement_config: str | Path,
    recordgold_record: RecordGoldSmokeRecord = RECORDGOLD_SMOKE_RECORD,
    recordgold_fetch: Callable[[str], bytes] = committed_recordgold_bytes,
) -> dict[str, object]:
    """Return proof candidates bound to one measured tier and its artifacts.

    ``recordgold_record`` and ``recordgold_fetch`` name the RecordGold record
    the DAI chair's smoke read and where its pinned bytes come from: by
    default the record pinned in ``recordgold_smoke.py`` and the copy committed
    beside it, so qualification needs no network. The gold transcription is
    read into memory to re-score the retained answer and is never written.
    """

    report_bytes = _read_bytes(report_path, "bootstrap report")
    report = _json_object(report_bytes, "bootstrap report")
    bootstrap = _bootstrap_record(report)
    if bootstrap.get("color") != "green":
        raise QualificationRefusal("bootstrap report is not green")
    completed = bootstrap.get("completed")
    if not isinstance(completed, list) or "preflight" not in completed:
        raise QualificationRefusal("bootstrap report does not record a completed preflight")
    receipts = _object(bootstrap.get("receipts"), "bootstrap receipts")
    preflight = _object(receipts.get("preflight"), "bootstrap preflight receipt")
    if preflight.get("color") != "green" or preflight.get("issues") != []:
        raise QualificationRefusal("preflight receipt is not green and issue-free")
    if preflight.get("assembly_proven") is not True:
        raise QualificationRefusal("preflight did not prove a real serving assembly")
    tier = preflight.get("placement_tier")
    if not isinstance(tier, str) or not tier:
        raise QualificationRefusal("preflight does not name one measured placement tier")
    golden_page_sha256 = preflight.get("golden_page_sha256")
    if not is_sha256(golden_page_sha256):
        raise QualificationRefusal("preflight does not carry a valid golden-page digest")

    recipes_bytes = _read_bytes(recipes_config, "serving recipes")
    placement_bytes = _read_bytes(placement_config, "placement table")
    models_bytes = _read_bytes(models_config, "model roster")
    config_inputs = _object(preflight.get("serving_config_inputs"), "serving config inputs")
    try:
        recipes_raw, recipes_sha256 = parse_sealed_toml(recipes_bytes, "serving recipes")
        _, placement_sha256 = parse_sealed_toml(placement_bytes, "placement table")
        models_raw, models_sha256 = parse_sealed_toml(models_bytes, "model roster")
    except ContractError as error:
        raise QualificationRefusal(f"serving configuration cannot be parsed: {error}") from error
    expected_inputs = {
        "schema": SERVING_CONFIG_INPUTS_SCHEMA,
        "serving_recipes_sha256": recipes_sha256,
        "pod_placement_sha256": placement_sha256,
    }
    if config_inputs != expected_inputs:
        raise QualificationRefusal("preflight serving inputs do not match the supplied files")

    try:
        recipes = parse_serving_recipes(
            recipes_raw,
            source_path=recipes_config,
            source_sha256=expected_inputs["serving_recipes_sha256"],
        )
    except ServingConfigurationError as error:
        raise QualificationRefusal(f"serving recipes are invalid: {error}") from error
    # A parsed catalogue keeps its rows in file order, so each typed profile's
    # raw row (the input to its preflight digest) sits at the same index.
    raw_rows = {
        id(profile): row
        for profile, row in zip(recipes.profiles, recipes_raw["profiles"], strict=True)
    }

    try:
        models = parse_models_config(models_raw, source_path=models_config)
    except ContractError as error:
        raise QualificationRefusal(f"model roster is invalid: {error}") from error
    identities = {
        role: identity
        for role, identity in models.chairs.items()
        if isinstance(identity, ChairIdentity)
    }
    # Qualification is per chair: it covers exactly the chairs this preflight
    # placed, which is the whole roster or the narrowed selection an operator
    # asked for. A chair its stage runs itself has a placement in its row's kind
    # and no smoke receipt, and is never a candidate.
    placements = _rows_by_chair(preflight.get("placements"), "placements")
    selected = _placed_chairs(placements, models.chairs, identities)
    profiles = {role: _profile_at_tier(recipes, identities[role], tier) for role in selected}
    unserved_states = {
        role: profile.kind for role, profile in profiles.items() if profile.kind in UNSERVED_KINDS
    }
    served = {role: identities[role] for role in selected if role not in unserved_states}
    if not served:
        raise QualificationRefusal("preflight placed no chair that is served at this tier")
    smoke_rows = preflight.get("smoke_receipts")
    if not isinstance(smoke_rows, list):
        raise QualificationRefusal("preflight smoke receipts are not a list")
    by_chair: dict[str, list[Mapping[str, object]]] = {}
    for raw in smoke_rows:
        smoke = _object(raw, "smoke receipt")
        chair = smoke.get("chair")
        if not isinstance(chair, str) or not chair:
            raise QualificationRefusal("smoke receipt does not name a chair")
        by_chair.setdefault(chair, []).append(smoke)
    if set(by_chair) != set(served):
        raise QualificationRefusal(
            "smoke receipts do not cover exactly the placed served chairs: "
            f"expected={sorted(served)}, observed={sorted(by_chair)}"
        )
    _verify_cache_receipts(
        preflight.get("cache_receipts"), {role: identities[role] for role in selected}
    )
    _verify_placements(
        placements, {role: identities[role] for role in selected}, tier, unserved_states
    )
    _verify_subprocess_receipts(
        preflight.get("subprocess_receipts"),
        {
            role: raw_rows[id(profile)]
            for role, profile in profiles.items()
            if isinstance(profile, SubprocessProfile)
        },
    )

    root = Path(evidence_root)
    gold_text: str | None = None
    candidates: list[dict[str, object]] = []
    for role, identity in sorted(served.items()):
        matches = by_chair[role]
        if len(matches) != 1:
            raise QualificationRefusal(f"chair {role!r} has {len(matches)} smoke receipts")
        smoke = matches[0]
        profile = profiles[role]
        row = dict(raw_rows[id(profile)])
        if not isinstance(profile, ServingProfile) or profile.preflight_state != "unproven":
            raise QualificationRefusal(
                f"chair {role!r} tier {tier!r} is not one unproven vLLM profile"
            )
        smoke_page = smoke.get("smoke_page")
        recordgold: tuple[RecordGoldSmokeRecord, str] | None = None
        if smoke_page == RECORDGOLD_PAGE:
            # Only the chair whose adapter is DAI's reads the record; every
            # other chair that claims to has smoked a page the qualifier does
            # not accept from it.
            if identity.witness_adapter != DAI_WITNESS_ADAPTER:
                raise QualificationRefusal(
                    f"chair {role!r} is not a RecordGold reader but smoked the RecordGold record"
                )
            if gold_text is None:
                gold_text = _pinned_gold_text(recordgold_record, recordgold_fetch)
            recordgold = (recordgold_record, gold_text)
        elif smoke_page != GOLDEN_PAGE:
            raise QualificationRefusal(f"chair {role!r} smoked an unknown page {smoke_page!r}")
        _verify_smoke(
            smoke,
            identity=identity,
            tier=tier,
            served_model_id=row["served_model_id"],
            golden_page_sha256=golden_page_sha256,
            config_inputs=config_inputs,
            evidence_root=root,
            recordgold=recordgold,
        )
        identity_digest = chair_preflight_identity_digest(identity)
        row["preflight_identity_digest"] = identity_digest
        row["preflight_state"] = "proven"
        profile_digest = profile_preflight_digest(row)
        if recordgold is None:
            page_read: dict[str, object] = {
                "page_witness_reference": dict(
                    _object(smoke.get("page_witness_reference"), "page witness reference")
                ),
            }
        else:
            # The record is public and pinned, so the candidate names it and
            # the measured rate; there is no retained witness artifact to cite.
            page_read = {
                "page_witness_reference": None,
                "recordgold_record": dict(_object(smoke["recordgold_record"], "RecordGold pins")),
                "recordgold_page_sha256": smoke["recordgold_page_sha256"],
                "character_error_rate": smoke["character_error_rate"],
                "character_error_rate_threshold": smoke["character_error_rate_threshold"],
            }
        candidates.append(
            {
                "chair": role,
                "recipe": identity.serving_recipe,
                "tier": tier,
                "preflight_identity_digest": identity_digest,
                "preflight_digest": profile_digest,
                "smoke_page": smoke_page,
                **page_read,
                "page_witness_sha256": smoke["page_witness_sha256"],
                "smoke_fixture_output_sha256": smoke["smoke_fixture_output_sha256"],
                "service_receipt_reference": dict(
                    _object(smoke.get("receipt_reference"), "service receipt reference")
                ),
                "serving_launch_audit_reference": dict(
                    _object(
                        smoke.get("serving_launch_audit_reference"),
                        "serving launch audit reference",
                    )
                ),
                "serving_evidence_reference": dict(
                    _object(
                        smoke.get("serving_evidence_reference"),
                        "serving evidence reference",
                    )
                ),
            }
        )

    environment = _object(preflight.get("environment"), "preflight environment")
    return {
        "schema": SCHEMA,
        "report_sha256": digest_bytes(report_bytes),
        "source_inputs": {
            "models_config_sha256": models_sha256,
            **expected_inputs,
        },
        "measured": {
            "placement_tier": tier,
            "gpu": environment.get("gpu"),
            "cuda_version": environment.get("cuda_version"),
            "driver_version": environment.get("driver_version"),
            "compute_capability": environment.get("compute_capability"),
            "vram_gib": environment.get("vram_gib"),
            "dtype": environment.get("dtype"),
            "golden_page_sha256": golden_page_sha256,
        },
        "preflight_chairs": sorted(selected),
        "candidates": candidates,
    }


def _placed_chairs(
    placed: Mapping[str, object],
    roster: Mapping[str, object],
    identities: Mapping[str, ChairIdentity],
) -> list[str]:
    """The configured chairs the preflight placed, in roster order."""
    strangers = sorted(set(placed) - set(roster))
    if strangers:
        raise QualificationRefusal(f"placements name chairs outside the roster: {strangers}")
    return [role for role in identities if role in placed]


def _bootstrap_record(report: Mapping[str, object]) -> Mapping[str, object]:
    if report.get("schema") in {"pod-bootstrap-hold.v1", "pod-bootstrap-result.v1"}:
        expected_state = (
            "holding" if report["schema"] == "pod-bootstrap-hold.v1" else "bootstrap-green"
        )
        if report.get("state") != expected_state:
            raise QualificationRefusal("bootstrap wrapper state does not record success")
        return _object(report.get("bootstrap"), "bootstrap result")
    raise QualificationRefusal("input is not a bootstrap result or hold report")


def _profile_at_tier(
    recipes: ServingRecipes, identity: ChairIdentity, tier: str
) -> ServingProfile | InProcessProfile | SubprocessProfile | FixtureProfile | UnsupportedProfile:
    try:
        return recipes.for_identity(identity, tier)
    except ServingConfigurationError as error:
        raise QualificationRefusal(str(error)) from error


def _verify_smoke(
    smoke: Mapping[str, object],
    *,
    identity: ChairIdentity,
    tier: str,
    served_model_id: str,
    golden_page_sha256: str,
    config_inputs: Mapping[str, object],
    evidence_root: Path,
    recordgold: tuple[RecordGoldSmokeRecord, str] | None = None,
) -> None:
    """Check one chair's smoke receipt against its artifacts and the page it read.

    ``recordgold`` is the pinned record and its gold transcription when this
    chair read the RecordGold record; ``None`` when it read the golden page.
    """
    if not isinstance(smoke.get("served_engine"), str) or not smoke["served_engine"]:
        raise QualificationRefusal(f"chair {identity.role!r} has no served engine")
    utilization = smoke.get("utilization")
    if not isinstance(utilization, list) or not utilization:
        raise QualificationRefusal(f"chair {identity.role!r} has no utilization samples")
    if any(
        not isinstance(sample, dict)
        or set(sample) != {"gpu_percent", "cpu_percent"}
        or not all(isinstance(value, str) and value for value in sample.values())
        for sample in utilization
    ):
        raise QualificationRefusal(f"chair {identity.role!r} has malformed utilization samples")
    for field in (
        "supplied_fixture_sha256",
        "smoke_fixture_response_sha256",
        "smoke_fixture_output_sha256",
        "page_witness_sha256",
    ):
        if not is_sha256(smoke.get(field)):
            raise QualificationRefusal(f"chair {identity.role!r} has no valid {field}")
    witness: str | None = None
    if recordgold is None:
        if smoke["supplied_fixture_sha256"] != golden_page_sha256:
            raise QualificationRefusal(f"chair {identity.role!r} smoked a different golden page")
        witness_ref = _object(smoke.get("page_witness_reference"), "page witness reference")
        witness_bytes = _verified_artifact_bytes(evidence_root, witness_ref, "page witness")
        try:
            witness = witness_bytes.decode("ascii")
        except UnicodeDecodeError as error:
            raise QualificationRefusal(
                f"chair {identity.role!r} page witness artifact is not ASCII"
            ) from error
        if not is_page_witness(witness):
            raise QualificationRefusal(
                f"chair {identity.role!r} page witness artifact is malformed"
            )
        if digest_bytes(witness_bytes) != smoke["page_witness_sha256"]:
            raise QualificationRefusal(
                f"chair {identity.role!r} witness digest disagrees with its artifact"
            )
    else:
        _verify_recordgold_page(smoke, identity, recordgold[0])
    response_ref = _object(smoke.get("smoke_response_reference"), "smoke response reference")
    response_bytes = _verified_artifact_bytes(evidence_root, response_ref, "smoke response")
    if digest_bytes(response_bytes) != smoke["smoke_fixture_response_sha256"]:
        raise QualificationRefusal(
            f"chair {identity.role!r} response digest disagrees with its artifact"
        )
    response = _json_object(response_bytes, "smoke response")
    choices = response.get("choices")
    if (
        response.get("model") != served_model_id
        or not isinstance(choices, list)
        or len(choices) != 1
    ):
        raise QualificationRefusal(
            f"chair {identity.role!r} smoke response has a different model or output shape"
        )
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    answer = message.get("content") if isinstance(message, dict) else None
    if "page_witness_edit_distance" not in smoke:
        raise QualificationRefusal(
            f"chair {identity.role!r} smoke receipt records no page_witness_edit_distance; "
            "re-run preflight"
        )
    if recordgold is None:
        assert witness is not None
        distance = page_witness_edit_distance(answer, witness) if isinstance(answer, str) else None
        if distance is None:
            raise QualificationRefusal(
                f"chair {identity.role!r} output was not a near transcription of the retained "
                "page witness"
            )
        recorded_distance = smoke.get("page_witness_edit_distance")
        if type(recorded_distance) is not int or recorded_distance != distance:
            raise QualificationRefusal(f"chair {identity.role!r} page-read edit distance disagrees")
        if distance != 0:
            raise QualificationRefusal(
                f"chair {identity.role!r} read the page witness with edit distance {distance}; "
                "only an exact read can prove a profile row"
            )
    else:
        _verify_recordgold_read(smoke, identity, answer, recordgold[1])
    if smoke["smoke_fixture_output_sha256"] != digest_bytes(canonical_bytes([answer])):
        raise QualificationRefusal(
            f"chair {identity.role!r} output digest disagrees with its artifact"
        )
    page_read = {
        "resolved_identity": identity.to_record(),
        "resolved_revision": identity.receipt_revision,
        "resolved_revision_kind": identity.receipt_revision_kind,
        "served_model_id": served_model_id,
        "fixture_response_sha256": smoke["smoke_fixture_response_sha256"],
    }
    if smoke.get("page_witness_matches") is not True:
        raise QualificationRefusal(
            f"chair {identity.role!r} did not match the golden-page witness"
            if recordgold is None
            else f"chair {identity.role!r} did not record a passing RecordGold read"
        )
    for field, expected in page_read.items():
        if smoke.get(field) != expected:
            raise QualificationRefusal(
                f"chair {identity.role!r} page-read evidence disagrees at {field}"
            )
    count = smoke.get("smoke_fixture_request_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise QualificationRefusal(
            f"chair {identity.role!r} has no valid smoke_fixture_request_count"
        )

    receipt_ref = _object(smoke.get("receipt_reference"), "service receipt reference")
    audit_ref = _object(smoke.get("serving_launch_audit_reference"), "launch audit reference")
    evidence_ref = _object(smoke.get("serving_evidence_reference"), "evidence reference")
    receipt_artifact = _verified_artifact(evidence_root, receipt_ref, "service receipt")
    audit_artifact = _verified_artifact(evidence_root, audit_ref, "serving launch audit")
    evidence_artifact = _verified_artifact(evidence_root, evidence_ref, "serving evidence")
    if receipt_artifact != _object(smoke.get("service_receipt"), "embedded service receipt"):
        raise QualificationRefusal(f"chair {identity.role!r} service receipt artifact disagrees")
    if audit_artifact != _object(smoke.get("serving_launch_audit"), "embedded launch audit"):
        raise QualificationRefusal(f"chair {identity.role!r} launch audit artifact disagrees")
    if evidence_artifact != {
        "schema": "serving-evidence.v1",
        "receipt_reference": receipt_ref,
        "launch_audit_reference": audit_ref,
    }:
        raise QualificationRefusal(f"chair {identity.role!r} serving evidence is misbound")

    expected_identity = identity.to_record()
    try:
        validated_receipt = validate_receipt(dict(receipt_artifact))
    except ContractError as error:
        raise QualificationRefusal(
            f"chair {identity.role!r} service receipt is invalid: {error}"
        ) from error
    expected_receipt_identity = {
        "chair": identity.role,
        "source": identity.source,
        "resolved": identity.source_reference,
        "revision": identity.receipt_revision,
        "revision_kind": identity.receipt_revision_kind,
        "digest_manifest": identity.digest_manifest,
    }
    if any(validated_receipt.get(key) != value for key, value in expected_receipt_identity.items()):
        raise QualificationRefusal(f"chair {identity.role!r} service receipt identity changed")
    served_engine = " ".join((validated_receipt["engine"], validated_receipt["engine_version"]))
    if smoke["served_engine"] != served_engine:
        raise QualificationRefusal(f"chair {identity.role!r} served-engine claim changed")
    if audit_artifact.get("schema") != SERVING_LAUNCH_AUDIT_SCHEMA:
        raise QualificationRefusal(f"chair {identity.role!r} launch audit has the wrong schema")
    if audit_artifact.get("chair") != identity.role:
        raise QualificationRefusal(f"chair {identity.role!r} launch audit names another chair")
    if audit_artifact.get("launch_purpose") != QUALIFICATION_PURPOSE:
        raise QualificationRefusal(f"chair {identity.role!r} was not launched for qualification")
    if audit_artifact.get("configuration_inputs") != config_inputs:
        raise QualificationRefusal(f"chair {identity.role!r} launch used different configuration")
    if audit_artifact.get("chair_identity") != expected_identity:
        raise QualificationRefusal(f"chair {identity.role!r} launch used a different identity")
    profile = _object(audit_artifact.get("profile"), "launch audit profile")
    if (
        profile.get("recipe") != identity.serving_recipe
        or profile.get("tier") != tier
        or profile.get("served_model_id") != served_model_id
        or profile.get("preflight_state") != "unproven"
    ):
        raise QualificationRefusal(f"chair {identity.role!r} launch audit names another profile")


def _pinned_gold_text(record: RecordGoldSmokeRecord, fetch: Callable[[str], bytes]) -> str:
    """The pinned record's gold transcription, verified against the pin, or a refusal.

    The crop is fetched and checked against its pinned digest too, so a
    qualifier whose committed copy has drifted refuses by name rather than
    scoring against text the pin does not vouch for.
    """
    try:
        return fetch_recordgold_smoke_page(record, fetch=fetch).text
    except RecordGoldSmokeRefusal as refusal:
        raise QualificationRefusal(
            f"the pinned RecordGold record cannot be verified: {refusal}"
        ) from refusal


def _verify_recordgold_page(
    smoke: Mapping[str, object], identity: ChairIdentity, record: RecordGoldSmokeRecord
) -> None:
    """The receipt names the pinned record, and the page it was sent is that record.

    The pod verified the fetched crop against the pin, re-encoded it as PNG and
    refused to send any other bytes (``VisionSmokeCall._verify_recordgold_page``);
    the receipt carries that page's digest under ``recordgold_page_sha256`` and
    the digest of what was sent under ``supplied_fixture_sha256``. The PNG is not
    re-encoded here to compare: two Pillow builds need not compress alike, and a
    refusal on that difference would say nothing about the chair.
    """
    if smoke.get("recordgold_record") != record.to_record():
        raise QualificationRefusal(
            f"chair {identity.role!r} smoked a RecordGold record other than the pinned "
            f"{record.record_id}"
        )
    if not is_sha256(smoke.get("recordgold_page_sha256")):
        raise QualificationRefusal(f"chair {identity.role!r} has no valid recordgold_page_sha256")
    if smoke["supplied_fixture_sha256"] != smoke["recordgold_page_sha256"]:
        raise QualificationRefusal(
            f"chair {identity.role!r} was sent a page other than the verified RecordGold record"
        )
    if smoke["page_witness_sha256"] != record.text_sha256:
        raise QualificationRefusal(
            f"chair {identity.role!r} RecordGold transcription digest disagrees with the pin"
        )


def _verify_recordgold_read(
    smoke: Mapping[str, object], identity: ChairIdentity, answer: object, gold: str
) -> None:
    """Re-score the retained answer against the gold; the receipt must agree and pass."""
    score = score_recordgold_answer(answer, gold) if isinstance(answer, str) else None
    if score is None:
        raise QualificationRefusal(
            f"chair {identity.role!r} output could not be measured against the RecordGold "
            "transcription"
        )
    recorded_distance = smoke.get("page_witness_edit_distance")
    if type(recorded_distance) is not int or recorded_distance != score.edits:
        raise QualificationRefusal(f"chair {identity.role!r} page-read edit distance disagrees")
    if smoke.get("character_error_rate") != str(score.character_error_rate):
        raise QualificationRefusal(
            f"chair {identity.role!r} RecordGold character error rate disagrees"
        )
    if smoke.get("character_error_rate_threshold") != str(RECORDGOLD_SMOKE_MAX_CER):
        raise QualificationRefusal(
            f"chair {identity.role!r} RecordGold threshold is not the pinned "
            f"{RECORDGOLD_SMOKE_MAX_CER}"
        )
    if smoke.get("reference_units") != score.reference_units:
        raise QualificationRefusal(f"chair {identity.role!r} RecordGold reference units disagree")
    if smoke.get("normalization_profile") != {
        "profile_id": RECORDGOLD_SMOKE_PROFILE.profile_id,
        "digest": RECORDGOLD_SMOKE_PROFILE.digest,
    }:
        raise QualificationRefusal(
            f"chair {identity.role!r} RecordGold read was not scored under the corpus's "
            "normalisation profile"
        )
    if not score.passed:
        raise QualificationRefusal(
            f"chair {identity.role!r} read the RecordGold record at character error rate "
            f"{score.character_error_rate}, past the {RECORDGOLD_SMOKE_MAX_CER} pass line"
        )


def _verified_artifact(
    root: Path, reference: Mapping[str, object], label: str
) -> Mapping[str, object]:
    return _json_object(_verified_artifact_bytes(root, reference, label), label)


def _verified_artifact_bytes(root: Path, reference: Mapping[str, object], label: str) -> bytes:
    if set(reference) != {"relative_path", "sha256"} or not is_sha256(reference.get("sha256")):
        raise QualificationRefusal(f"{label} reference is malformed")
    relative = reference.get("relative_path")
    if not isinstance(relative, str):
        raise QualificationRefusal(f"{label} reference path is malformed")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise QualificationRefusal(f"{label} reference escapes the evidence root")
    path = root.joinpath(*pure.parts)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise QualificationRefusal(f"cannot inspect {label} artifact path: {error}") from error
    if not resolved_path.is_relative_to(resolved_root):
        raise QualificationRefusal(f"{label} reference escapes the evidence root")
    candidate = root
    try:
        if candidate.is_symlink():
            raise QualificationRefusal(f"{label} artifact path contains a symlink")
        for part in pure.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise QualificationRefusal(f"{label} artifact path contains a symlink")
        leaf = path.lstat()
    except QualificationRefusal:
        raise
    except OSError as error:
        raise QualificationRefusal(f"cannot inspect {label} artifact path: {error}") from error
    if not stat.S_ISREG(leaf.st_mode):
        raise QualificationRefusal(f"{label} artifact is not a regular file")
    data = _read_bytes(path, label)
    if digest_bytes(data) != reference["sha256"]:
        raise QualificationRefusal(f"{label} artifact digest does not match its reference")
    return data


def _verify_cache_receipts(raw_receipts: object, identities: Mapping[str, ChairIdentity]) -> None:
    receipts = _rows_by_chair(raw_receipts, "cache receipts")
    if set(receipts) != set(identities):
        raise QualificationRefusal("cache receipts do not cover exactly the placed chairs")
    for role, rows in receipts.items():
        if len(rows) != 1 or rows[0].get("manifest_digest") != identities[role].digest_manifest:
            raise QualificationRefusal(f"chair {role!r} cache receipt does not match its manifest")


def _verify_subprocess_receipts(
    raw_receipts: object, subprocess_rows: Mapping[str, Mapping[str, object]]
) -> None:
    """Each subprocess chair's runner read the golden page once, in its row's environment."""
    receipts = _rows_by_chair([] if raw_receipts is None else raw_receipts, "subprocess receipts")
    if set(receipts) != set(subprocess_rows):
        raise QualificationRefusal(
            "subprocess receipts do not cover exactly the chairs run as subprocesses: "
            f"expected={sorted(subprocess_rows)}, observed={sorted(receipts)}"
        )
    for role, rows in receipts.items():
        row = subprocess_rows[role]
        if len(rows) != 1 or rows[0].get("environment") != row.get("environment"):
            raise QualificationRefusal(
                f"chair {role!r} subprocess receipt does not name its row's environment"
            )
        _verify_measured_packages(role, rows[0].get("versions"), row.get("required_packages"))


def _verify_measured_packages(role: str, measured: object, required: object) -> None:
    """Each package the row pins, as the run measured it: `surya-ocr` is
    measured as `surya_ocr`, and a local build tag (`2.14.0+cu130`) is the
    same release."""
    if not isinstance(required, Mapping) or not required:
        raise QualificationRefusal(f"chair {role!r} subprocess row pins no packages")
    if not isinstance(measured, Mapping):
        raise QualificationRefusal(f"chair {role!r} subprocess receipt measured no versions")
    for package, pin in required.items():
        found = measured.get(package.replace("-", "_"))
        if not isinstance(found, str) or found.split("+", 1)[0] != pin:
            raise QualificationRefusal(
                f"chair {role!r} ran {package} {found!r}, and its row pins {pin!r}"
            )


def _verify_placements(
    placements: Mapping[str, list[Mapping[str, object]]],
    identities: Mapping[str, ChairIdentity],
    tier: str,
    unserved_states: Mapping[str, str],
) -> None:
    """Each placed chair has exactly one placement, of its recipe, on the measured tier."""
    for role in identities:
        rows = placements[role]
        if (
            len(rows) != 1
            or rows[0].get("configured_serving_recipe") != identities[role].serving_recipe
            or rows[0].get("tier") != tier
            or rows[0].get("state") != unserved_states.get(role, "planned")
        ):
            raise QualificationRefusal(f"chair {role!r} was not planned on the measured tier")


def _rows_by_chair(value: object, label: str) -> dict[str, list[Mapping[str, object]]]:
    if not isinstance(value, list):
        raise QualificationRefusal(f"{label} are not a list")
    rows: dict[str, list[Mapping[str, object]]] = {}
    for raw in value:
        row = _object(raw, label[:-1])
        chair = row.get("chair")
        if not isinstance(chair, str) or not chair:
            raise QualificationRefusal(f"{label[:-1]} does not name a chair")
        rows.setdefault(chair, []).append(row)
    return rows


def _read_bytes(path: str | Path, label: str) -> bytes:
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise QualificationRefusal(f"cannot read {label}: {error}") from error
    if not data:
        raise QualificationRefusal(f"{label} is empty")
    return data


def _json_object(data: bytes, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationRefusal(f"{label} is not JSON: {error}") from error
    return _object(value, label)


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise QualificationRefusal(f"{label} is not an object")
    return value


def _write_output(path: Path, record: Mapping[str, object]) -> None:
    data = canonical_bytes(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        exclusive_write(path, data, strict=True)
    except FileExistsError:
        if path.read_bytes() != data:
            raise QualificationRefusal(
                f"output {path} already exists with different bytes"
            ) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION, allow_abbrev=False)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--models-config", required=True, type=Path)
    parser.add_argument("--serving-recipes-config", required=True, type=Path)
    parser.add_argument("--placement-config", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        record = qualification_candidates(
            report_path=args.report,
            evidence_root=args.evidence_root,
            models_config=args.models_config,
            recipes_config=args.serving_recipes_config,
            placement_config=args.placement_config,
        )
        if args.output is None:
            print(json.dumps(record, sort_keys=True, indent=2))
        else:
            _write_output(args.output, record)
    except (QualificationRefusal, OSError) as error:
        print(f"qualification refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
