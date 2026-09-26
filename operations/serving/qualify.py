from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from common.chairs.config import parse_models_config
from common.chairs.models import ChairIdentity, is_sha256
from common.chairs.receipts import validate_receipt
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import ContractError
from common.contracts.serving import SERVING_CONFIG_INPUTS_SCHEMA
from common.sealed_config import parse_sealed_toml
from operations.pod.durable import exclusive_write

from .config import (
    chair_preflight_identity_digest,
    parse_serving_recipes,
    profile_preflight_digest,
)
from .errors import ServingConfigurationError

DESCRIPTION = """Verify a real-silicon preflight and render profile proof candidates.

This command never edits the serving catalogue.  It turns durable runtime
evidence into the exact identity and profile digests a reviewer may stamp on
the one tier that was measured.  Normal serving remains unable to launch an
unproven row.
"""

SCHEMA = "serving-qualification-candidates.v1"
QUALIFICATION_PURPOSE = "preflight-qualification"


class QualificationRefusal(ValueError):
    """The supplied evidence cannot justify a profile proof mark."""


def qualification_candidates(
    *,
    report_path: str | Path,
    evidence_root: str | Path,
    models_config: str | Path,
    recipes_config: str | Path,
    placement_config: str | Path,
) -> dict[str, object]:
    """Return proof candidates bound to one measured tier and its artifacts."""

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
        parse_serving_recipes(
            recipes_raw,
            source_path=recipes_config,
            source_sha256=expected_inputs["serving_recipes_sha256"],
        )
    except ServingConfigurationError as error:
        raise QualificationRefusal(f"serving recipes are invalid: {error}") from error
    rows = recipes_raw.get("profiles")
    if not isinstance(rows, list):  # parse_serving_recipes already names the ordinary case
        raise QualificationRefusal("serving recipes have no profile rows")

    try:
        models = parse_models_config(models_raw, source_path=models_config)
    except ContractError as error:
        raise QualificationRefusal(f"model roster is invalid: {error}") from error
    identities = {
        role: identity
        for role, identity in models.chairs.items()
        if isinstance(identity, ChairIdentity)
    }
    adapters = sorted(
        role for role, identity in identities.items() if identity.adapter_of is not None
    )
    if adapters:
        raise QualificationRefusal(
            "adapter qualification is unsupported: an adapter proof must bind its resolved "
            "base checkpoint as well as the adapter; no candidates were emitted for "
            + ", ".join(adapters)
        )
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
    if set(by_chair) != set(identities):
        raise QualificationRefusal(
            "smoke receipts do not cover exactly the configured chairs: "
            f"expected={sorted(identities)}, observed={sorted(by_chair)}"
        )
    _verify_cache_receipts(preflight.get("cache_receipts"), identities)
    _verify_placements(preflight.get("placements"), identities, tier)

    root = Path(evidence_root)
    candidates: list[dict[str, object]] = []
    for role, identity in sorted(identities.items()):
        matches = by_chair[role]
        if len(matches) != 1:
            raise QualificationRefusal(f"chair {role!r} has {len(matches)} smoke receipts")
        smoke = matches[0]
        profile_rows = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("recipe") == identity.serving_recipe
            and row.get("chair") == role
            and row.get("tier") == tier
        ]
        if len(profile_rows) != 1:
            raise QualificationRefusal(
                f"chair {role!r} resolves to {len(profile_rows)} raw profile rows at tier {tier!r}"
            )
        row = dict(profile_rows[0])
        if row.get("kind") != "vllm" or row.get("preflight_state") != "unproven":
            raise QualificationRefusal(
                f"chair {role!r} tier {tier!r} is not one unproven vLLM profile"
            )
        _verify_smoke(
            smoke,
            identity=identity,
            tier=tier,
            served_model_id=row["served_model_id"],
            golden_page_sha256=golden_page_sha256,
            config_inputs=config_inputs,
            evidence_root=root,
        )
        identity_digest = chair_preflight_identity_digest(identity)
        row["preflight_identity_digest"] = identity_digest
        row["preflight_state"] = "proven"
        profile_digest = profile_preflight_digest(row)
        candidates.append(
            {
                "chair": role,
                "recipe": identity.serving_recipe,
                "tier": tier,
                "preflight_identity_digest": identity_digest,
                "preflight_digest": profile_digest,
                "page_witness_reference": dict(
                    _object(smoke.get("page_witness_reference"), "page witness reference")
                ),
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
        "candidates": candidates,
    }


def _bootstrap_record(report: Mapping[str, object]) -> Mapping[str, object]:
    if report.get("schema") in {"pod-bootstrap-hold.v1", "pod-bootstrap-result.v1"}:
        expected_state = (
            "holding" if report["schema"] == "pod-bootstrap-hold.v1" else "bootstrap-green"
        )
        if report.get("state") != expected_state:
            raise QualificationRefusal("bootstrap wrapper state does not record success")
        return _object(report.get("bootstrap"), "bootstrap result")
    raise QualificationRefusal("input is not a bootstrap result or hold report")


def _verify_smoke(
    smoke: Mapping[str, object],
    *,
    identity: ChairIdentity,
    tier: str,
    served_model_id: str,
    golden_page_sha256: str,
    config_inputs: Mapping[str, object],
    evidence_root: Path,
) -> None:
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
    if (
        not 32 <= len(witness) <= 128
        or not witness
        or not all(character.isalnum() or character in "-_" for character in witness)
    ):
        raise QualificationRefusal(f"chair {identity.role!r} page witness artifact is malformed")
    if digest_bytes(witness_bytes) != smoke["page_witness_sha256"]:
        raise QualificationRefusal(
            f"chair {identity.role!r} witness digest disagrees with its artifact"
        )
    expected_output_sha256 = digest_bytes(canonical_bytes([f"PAGE-WITNESS: {witness}"]))
    if smoke["smoke_fixture_output_sha256"] != expected_output_sha256:
        raise QualificationRefusal(
            f"chair {identity.role!r} output did not contain the retained page witness exactly"
        )
    page_read = {
        "resolved_identity": identity.to_record(),
        "resolved_revision": identity.receipt_revision,
        "resolved_revision_kind": identity.receipt_revision_kind,
        "served_model_id": served_model_id,
        "fixture_response_sha256": smoke["smoke_fixture_response_sha256"],
    }
    if smoke.get("page_witness_matches") is not True:
        raise QualificationRefusal(f"chair {identity.role!r} did not match the golden-page witness")
    for field, expected in page_read.items():
        if smoke.get(field) != expected:
            raise QualificationRefusal(
                f"chair {identity.role!r} page-read evidence disagrees at {field}"
            )
    for field in ("smoke_service_request_count", "smoke_fixture_request_count"):
        value = smoke.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise QualificationRefusal(f"chair {identity.role!r} has no valid {field}")

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
    if audit_artifact.get("schema") != "serving-launch-audit.v1":
        raise QualificationRefusal(f"chair {identity.role!r} launch audit has the wrong schema")
    if audit_artifact.get("chair") != identity.role:
        raise QualificationRefusal(f"chair {identity.role!r} launch audit names another chair")
    if audit_artifact.get("launch_purpose") != QUALIFICATION_PURPOSE:
        raise QualificationRefusal(f"chair {identity.role!r} was not launched for qualification")
    if audit_artifact.get("configuration_inputs") != config_inputs:
        raise QualificationRefusal(f"chair {identity.role!r} launch used different configuration")
    if audit_artifact.get("primary_identity") != expected_identity:
        raise QualificationRefusal(f"chair {identity.role!r} launch used a different identity")
    profile = _object(audit_artifact.get("profile"), "launch audit profile")
    if (
        profile.get("recipe") != identity.serving_recipe
        or profile.get("tier") != tier
        or profile.get("served_model_id") != served_model_id
        or profile.get("preflight_state") != "unproven"
    ):
        raise QualificationRefusal(f"chair {identity.role!r} launch audit names another profile")


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
        raise QualificationRefusal("cache receipts do not cover exactly the configured chairs")
    for role, rows in receipts.items():
        if len(rows) != 1 or rows[0].get("manifest_digest") != identities[role].digest_manifest:
            raise QualificationRefusal(f"chair {role!r} cache receipt does not match its manifest")


def _verify_placements(
    raw_placements: object, identities: Mapping[str, ChairIdentity], tier: str
) -> None:
    placements = _rows_by_chair(raw_placements, "placements", selected=set(identities))
    if set(placements) != set(identities):
        raise QualificationRefusal("placements do not cover exactly the configured chairs")
    for role, rows in placements.items():
        if (
            len(rows) != 1
            or rows[0].get("configured_serving_recipe") != identities[role].serving_recipe
            or rows[0].get("tier") != tier
            or rows[0].get("state") != "planned"
        ):
            raise QualificationRefusal(f"chair {role!r} was not planned on the measured tier")


def _rows_by_chair(
    value: object, label: str, *, selected: set[str] | None = None
) -> dict[str, list[Mapping[str, object]]]:
    if not isinstance(value, list):
        raise QualificationRefusal(f"{label} are not a list")
    rows: dict[str, list[Mapping[str, object]]] = {}
    for raw in value:
        row = _object(raw, label[:-1])
        chair = row.get("chair")
        if not isinstance(chair, str) or not chair:
            raise QualificationRefusal(f"{label[:-1]} does not name a chair")
        if selected is None or chair in selected:
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
