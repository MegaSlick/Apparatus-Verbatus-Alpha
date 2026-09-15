from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from common.chairs.config import load_models_toml
from common.chairs.models import ChairIdentity
from common.contracts.canonical import canonical_bytes, digest_bytes
from operations.pod.test_bootstrap_main import PROVEN_TIER, _serving_workspace

from .config import parse_serving_recipes
from .qualify import QualificationRefusal, qualification_candidates
from .qualify import main as qualification_main

HASH = "a" * 64


def _write_artifact(root: Path, kind: str, value: dict[str, object]) -> dict[str, str]:
    data = canonical_bytes(value)
    digest = digest_bytes(data)
    relative = f"{kind}/sha256/{digest}.json"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"relative_path": relative, "sha256": digest}


def _qualification_fixture(tmp_path: Path) -> tuple[dict[str, Path], dict[str, object]]:
    ws, _ = _serving_workspace(tmp_path, preflight_state="unproven")
    evidence_root = ws.volume / "preflight" / "qualification"
    recipes_config = ws.repository / "config" / "serving_recipes.toml"
    recipes_bytes = recipes_config.read_bytes()
    placement_bytes = ws.placement_config.read_bytes()
    config_inputs = {
        "schema": "serving-config-inputs.v1",
        "serving_recipes_sha256": digest_bytes(recipes_bytes),
        "pod_placement_sha256": digest_bytes(placement_bytes),
    }
    models = load_models_toml(ws.models_config)
    smoke_receipts = []
    cache_receipts = []
    placements = []
    for role, identity in sorted(models.chairs.items()):
        if not isinstance(identity, ChairIdentity):
            continue
        cache_receipts.append(
            {
                "chair": role,
                "manifest_digest": identity.digest_manifest,
                "repaired_once": False,
                "root": f"/runpod-volume/models/{role}",
            }
        )
        placements.append(
            {
                "chair": role,
                "configured_serving_recipe": identity.serving_recipe,
                "tier": PROVEN_TIER,
                "state": "planned",
            }
        )
        service_receipt = {
            "schema": "chair-serving-receipt.v1",
            "chair": role,
            "source": identity.source,
            "resolved": identity.source_reference,
            "revision": identity.receipt_revision,
            "revision_kind": identity.receipt_revision_kind,
            "digest_manifest": identity.digest_manifest,
            "tokenizer_revision": identity.receipt_revision,
            "seed": 0,
            "context_cap": 2048,
            "pixel_cap": 1024,
            "engine": "vllm",
            "engine_version": "0.test",
            "dtype": "bfloat16",
            "adapter_identity": None,
            "endpoint": f"http://127.0.0.1:{8100 + len(smoke_receipts)}",
            "started_at": "2026-09-15T12:00:00Z",
        }
        receipt_ref = _write_artifact(evidence_root, "receipts", service_receipt)
        audit = {
            "schema": "serving-launch-audit.v1",
            "chair": role,
            "launch_purpose": "preflight-qualification",
            "configuration_inputs": config_inputs,
            "primary_identity": identity.to_record(),
            "profile": {
                "recipe": identity.serving_recipe,
                "tier": PROVEN_TIER,
                "preflight_state": "unproven",
            },
        }
        audit_ref = _write_artifact(evidence_root, "launch-audits", audit)
        evidence = {
            "schema": "serving-evidence.v1",
            "receipt_reference": receipt_ref,
            "launch_audit_reference": audit_ref,
        }
        evidence_ref = _write_artifact(evidence_root, "serving-evidence", evidence)
        smoke_receipts.append(
            {
                "chair": role,
                "served_engine": "vllm 0.test",
                "utilization": [{"gpu_percent": "50", "cpu_percent": "10"}],
                "supplied_fixture_sha256": HASH,
                "smoke_fixture_response_sha256": "b" * 64,
                "smoke_fixture_output_sha256": "c" * 64,
                "smoke_service_request_count": 1,
                "smoke_fixture_request_count": 1,
                "service_receipt": service_receipt,
                "receipt_reference": receipt_ref,
                "serving_launch_audit": audit,
                "serving_launch_audit_reference": audit_ref,
                "serving_evidence_reference": evidence_ref,
            }
        )
    preflight = {
        "color": "green",
        "issues": [],
        "assembly_proven": True,
        "placement_tier": PROVEN_TIER,
        "golden_page_sha256": HASH,
        "serving_config_inputs": config_inputs,
        "environment": {
            "gpu": "measured GPU",
            "cuda_version": "13.0",
            "driver_version": "580",
            "compute_capability": [12, 0],
            "vram_gib": "96",
            "dtype": "bfloat16",
        },
        "cache_receipts": cache_receipts,
        "placements": placements,
        "smoke_receipts": smoke_receipts,
    }
    wrapper = {
        "schema": "pod-bootstrap-result.v1",
        "state": "bootstrap-green",
        "at": "2026-09-15T12:00:00Z",
        "bootstrap": {
            "color": "green",
            "completed": ["preflight"],
            "receipts": {"preflight": preflight},
            "failure_step": None,
            "detail": None,
            "remediation": None,
        },
    }
    report = ws.volume / "bootstrap-report.json"
    report.write_text(json.dumps(wrapper), encoding="utf-8")
    paths = {
        "report": report,
        "evidence": evidence_root,
        "models": ws.models_config,
        "recipes": recipes_config,
        "placement": ws.placement_config,
    }
    return paths, wrapper


def _qualify(paths: dict[str, Path]) -> dict[str, object]:
    return qualification_candidates(
        report_path=paths["report"],
        evidence_root=paths["evidence"],
        models_config=paths["models"],
        recipes_config=paths["recipes"],
        placement_config=paths["placement"],
    )


def test_green_qualification_renders_marks_for_only_the_measured_tier(tmp_path: Path) -> None:
    paths, _ = _qualification_fixture(tmp_path)

    record = _qualify(paths)

    assert record["schema"] == "serving-qualification-candidates.v1"
    candidates = record["candidates"]
    assert isinstance(candidates, list) and len(candidates) == 5
    assert {item["tier"] for item in candidates} == {PROVEN_TIER}
    raw = tomllib.loads(paths["recipes"].read_text(encoding="utf-8"))
    by_key = {(item["recipe"], item["chair"], item["tier"]): item for item in candidates}
    for row in raw["profiles"]:
        key = (row["recipe"], row["chair"], row["tier"])
        if key in by_key:
            row["preflight_state"] = "proven"
            row["preflight_identity_digest"] = by_key[key]["preflight_identity_digest"]
            row["preflight_digest"] = by_key[key]["preflight_digest"]
    parsed = parse_serving_recipes(raw)
    assert sum(profile.preflight_state == "proven" for profile in parsed.profiles) == 5  # type: ignore[attr-defined]


def test_qualification_refuses_a_normal_launch_audit(tmp_path: Path) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smoke = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"][0]  # type: ignore[index]
    audit = smoke["serving_launch_audit"]  # type: ignore[index]
    audit["launch_purpose"] = "normal"  # type: ignore[index]
    audit_ref = _write_artifact(paths["evidence"], "launch-audits", audit)  # type: ignore[arg-type]
    smoke["serving_launch_audit_reference"] = audit_ref  # type: ignore[index]
    receipt_ref = smoke["receipt_reference"]  # type: ignore[index]
    evidence = {
        "schema": "serving-evidence.v1",
        "receipt_reference": receipt_ref,
        "launch_audit_reference": audit_ref,
    }
    smoke["serving_evidence_reference"] = _write_artifact(  # type: ignore[index]
        paths["evidence"], "serving-evidence", evidence
    )
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match="was not launched for qualification"):
        _qualify(paths)


def test_qualification_refuses_partial_or_red_evidence(tmp_path: Path) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    preflight = wrapper["bootstrap"]["receipts"]["preflight"]  # type: ignore[index]
    preflight["smoke_receipts"].pop()  # type: ignore[index]
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    with pytest.raises(QualificationRefusal, match="cover exactly"):
        _qualify(paths)

    preflight["color"] = "red"  # type: ignore[index]
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    with pytest.raises(QualificationRefusal, match="not green"):
        _qualify(paths)


def test_qualification_refuses_changed_source_or_artifact_bytes(tmp_path: Path) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    paths["placement"].write_bytes(paths["placement"].read_bytes() + b"\n")
    with pytest.raises(QualificationRefusal, match="inputs do not match"):
        _qualify(paths)

    artifact_case = tmp_path / "artifact"
    artifact_case.mkdir()
    paths, wrapper = _qualification_fixture(artifact_case)
    smoke = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"][0]  # type: ignore[index]
    ref = smoke["receipt_reference"]  # type: ignore[index]
    (paths["evidence"] / ref["relative_path"]).write_text("{}", encoding="utf-8")  # type: ignore[index]
    with pytest.raises(QualificationRefusal, match="digest does not match"):
        _qualify(paths)


def test_qualification_cli_writes_once_and_never_replaces_a_candidate(tmp_path: Path) -> None:
    paths, _ = _qualification_fixture(tmp_path)
    output = tmp_path / "qualification.json"
    argv = [
        "--report",
        str(paths["report"]),
        "--evidence-root",
        str(paths["evidence"]),
        "--models-config",
        str(paths["models"]),
        "--serving-recipes-config",
        str(paths["recipes"]),
        "--placement-config",
        str(paths["placement"]),
        "--output",
        str(output),
    ]

    assert qualification_main(argv) == 0
    retained = output.read_bytes()
    assert qualification_main(argv) == 0
    output.write_bytes(b"different retained candidate")
    assert qualification_main(argv) == 2
    assert output.read_bytes() == b"different retained candidate"
    assert retained != output.read_bytes()
