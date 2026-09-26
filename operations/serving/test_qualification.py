from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from common.chairs.config import load_models_toml
from common.chairs.models import ChairIdentity
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.sealed_config import parse_sealed_toml
from operations.pod.test_bootstrap_main import PROVEN_TIER, _serving_workspace

from .config import parse_serving_recipes
from .qualify import QualificationRefusal, _verified_artifact_bytes, qualification_candidates
from .qualify import main as qualification_main

HASH = "a" * 64
PAGE_WITNESS = "qualification-witness-0123456789abcdef"


def _write_artifact(root: Path, kind: str, value: dict[str, object]) -> dict[str, str]:
    data = canonical_bytes(value)
    digest = digest_bytes(data)
    relative = f"{kind}/sha256/{digest}.json"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"relative_path": relative, "sha256": digest}


def _write_bytes_artifact(root: Path, kind: str, data: bytes) -> dict[str, str]:
    digest = digest_bytes(data)
    relative = f"{kind}/sha256/{digest}.txt"
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
        "serving_recipes_sha256": parse_sealed_toml(recipes_bytes, "recipes")[1],
        "pod_placement_sha256": parse_sealed_toml(placement_bytes, "placement")[1],
    }
    models = load_models_toml(ws.models_config)
    profile_rows = tomllib.loads(recipes_bytes.decode("utf-8"))["profiles"]
    witness_bytes = PAGE_WITNESS.encode("ascii")
    witness_ref = _write_bytes_artifact(evidence_root, "page-witnesses", witness_bytes)
    witness_sha256 = digest_bytes(witness_bytes)
    expected_output_sha256 = digest_bytes(canonical_bytes([f"PAGE-WITNESS: {PAGE_WITNESS}"]))
    smoke_receipts = []
    cache_receipts = []
    placements = []
    for role, identity in sorted(models.chairs.items()):
        if not isinstance(identity, ChairIdentity):
            continue
        served_model_id = next(
            row["served_model_id"]
            for row in profile_rows
            if row["chair"] == role and row["tier"] == PROVEN_TIER
        )
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
                "served_model_id": served_model_id,
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
                "smoke_fixture_output_sha256": expected_output_sha256,
                "fixture_response_sha256": "b" * 64,
                "resolved_identity": identity.to_record(),
                "resolved_revision": identity.receipt_revision,
                "resolved_revision_kind": identity.receipt_revision_kind,
                "served_model_id": served_model_id,
                "page_witness_sha256": witness_sha256,
                "page_witness_matches": True,
                "page_witness_reference": witness_ref,
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
    for candidate in candidates:
        reference = candidate["page_witness_reference"]
        witness_bytes = (paths["evidence"] / reference["relative_path"]).read_bytes()
        assert witness_bytes == PAGE_WITNESS.encode("ascii")
        assert digest_bytes(witness_bytes) == reference["sha256"]
        assert reference["sha256"] == candidate["page_witness_sha256"]
        assert candidate["smoke_fixture_output_sha256"] == digest_bytes(
            canonical_bytes(["PAGE-WITNESS: " + witness_bytes.decode("ascii")])
        )
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


def test_bootstrap_witness_evidence_is_accepted_by_the_qualifier(tmp_path: Path) -> None:
    """Exercise the real bootstrap producer shape through the offline verifier.

    The injected GPU probe cannot mint production runtime provenance, so this
    test supplies only that one paid-hardware fact after asserting the producer
    correctly left it false. The page, witness artifact, smoke receipts, and
    referenced serving artifacts all come from ``_build_preflight`` unchanged.
    """

    from operations.pod.bootstrap_main import _build_preflight, build_parser, resolve_plan
    from operations.pod.test_bootstrap_main import Clock, _argv, _environ, _preflight_seams

    ws, identities = _serving_workspace(tmp_path, preflight_state="unproven")
    plan = resolve_plan(build_parser().parse_args(_argv(ws)), _environ(Clock()))
    seams, _http, _launcher = _preflight_seams(tmp_path, identities)
    preflight = _build_preflight(plan, seams)()
    assert preflight["assembly_proven"] is False
    preflight["assembly_proven"] = True
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
    ws.report_path.write_text(json.dumps(wrapper), encoding="utf-8")

    candidates = qualification_candidates(
        report_path=ws.report_path,
        evidence_root=plan.preflight_root,
        models_config=ws.models_config,
        recipes_config=plan.serving_recipes_config,
        placement_config=ws.placement_config,
    )["candidates"]

    assert isinstance(candidates, list) and len(candidates) == len(identities)


@pytest.mark.parametrize(
    ("schema", "state"),
    [
        ("pod-bootstrap-result.v1", "bootstrap-red"),
        ("pod-bootstrap-result.v1", None),
        ("pod-bootstrap-hold.v1", "terminated"),
        ("pod-bootstrap-hold.v1", None),
    ],
)
def test_qualification_refuses_contradictory_wrapper_state(
    tmp_path: Path, schema: str, state: str | None
) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    wrapper.update(schema=schema, state=state)
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    with pytest.raises(QualificationRefusal, match="wrapper state"):
        _qualify(paths)


def test_qualification_accepts_green_hold_report(tmp_path: Path) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    wrapper.update(schema="pod-bootstrap-hold.v1", state="holding")
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    assert len(_qualify(paths)["candidates"]) == 5


def test_qualification_refuses_record_without_producer_wrapper(tmp_path: Path) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    paths["report"].write_text(json.dumps(wrapper["bootstrap"]), encoding="utf-8")
    with pytest.raises(QualificationRefusal, match="not a bootstrap result or hold report"):
        _qualify(paths)


def test_qualification_cannot_stamp_an_adapter_without_binding_its_base(tmp_path: Path) -> None:
    paths, _ = _qualification_fixture(tmp_path)
    models = paths["models"].read_text(encoding="utf-8")
    models = models.replace(
        "[chairs.attestator_1]\n",
        '[chairs.attestator_1]\nadapter_of = "designator_structure"\n',
        1,
    )
    paths["models"].write_text(models, encoding="utf-8")
    assert load_models_toml(paths["models"]).chairs["attestator_1"].adapter_of is not None

    with pytest.raises(QualificationRefusal, match="adapter qualification is unsupported"):
        _qualify(paths)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("page_witness_matches", False),
        ("page_witness_matches", None),
        ("resolved_identity", {"role": "another-chair"}),
        ("resolved_revision", "e" * 40),
        ("resolved_revision_kind", "local-tree"),
        ("served_model_id", "another-model"),
        ("fixture_response_sha256", "e" * 64),
    ],
)
def test_qualification_refuses_inconsistent_page_read_evidence(
    tmp_path: Path, field: str, bad_value: object
) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smoke = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"][0]  # type: ignore[index]
    smoke[field] = bad_value  # type: ignore[index]
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match="golden-page witness|page-read evidence"):
        _qualify(paths)


def test_qualification_refuses_a_witness_digest_that_disagrees_with_its_artifact(
    tmp_path: Path,
) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smoke = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"][0]  # type: ignore[index]
    smoke["page_witness_sha256"] = "e" * 64  # type: ignore[index]
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match="witness digest disagrees"):
        _qualify(paths)


def test_qualification_refuses_an_output_digest_that_does_not_match_the_witness(
    tmp_path: Path,
) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smoke = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"][0]  # type: ignore[index]
    smoke["smoke_fixture_output_sha256"] = "e" * 64  # type: ignore[index]
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match="retained page witness exactly"):
        _qualify(paths)


def test_qualification_refuses_changed_witness_artifact_bytes(tmp_path: Path) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smoke = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"][0]  # type: ignore[index]
    reference = smoke["page_witness_reference"]  # type: ignore[index]
    (paths["evidence"] / reference["relative_path"]).write_bytes(b"changed witness bytes")  # type: ignore[index]

    with pytest.raises(QualificationRefusal, match="artifact digest does not match"):
        _qualify(paths)


@pytest.mark.parametrize("linked_component", ["kind", "sha256"])
def test_qualification_refuses_parent_symlinks_outside_the_evidence_root(
    tmp_path: Path, linked_component: str
) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    data = b"retained artifact"
    digest = digest_bytes(data)
    relative = f"receipts/sha256/{digest}.json"
    if linked_component == "kind":
        (outside / "sha256").mkdir()
        (root / "receipts").symlink_to(outside, target_is_directory=True)
    else:
        (root / "receipts").mkdir()
        (root / "receipts" / "sha256").symlink_to(outside, target_is_directory=True)
    outside_target = outside / ("sha256" if linked_component == "kind" else "") / f"{digest}.json"
    outside_target.write_bytes(data)

    with pytest.raises(QualificationRefusal, match="escapes the evidence root|symlink"):
        _verified_artifact_bytes(
            root, {"relative_path": relative, "sha256": digest}, "service receipt"
        )

    assert outside_target.read_bytes() == data


def test_qualification_refuses_an_equal_bytes_leaf_symlink(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    data = b"retained artifact"
    digest = digest_bytes(data)
    target = root / "receipts" / "sha256" / f"{digest}.json"
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_bytes(data)
    target.symlink_to(outside)

    with pytest.raises(QualificationRefusal, match="escapes the evidence root|symlink"):
        _verified_artifact_bytes(
            root,
            {"relative_path": f"receipts/sha256/{digest}.json", "sha256": digest},
            "service receipt",
        )

    assert outside.read_bytes() == data


def test_qualification_refuses_a_fifo_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "evidence"
    digest = "a" * 64
    target = root / "receipts" / "sha256" / f"{digest}.json"
    target.parent.mkdir(parents=True)
    os.mkfifo(target)

    def fail_read(_path: Path) -> bytes:
        raise AssertionError("the FIFO must be rejected before read_bytes")

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(QualificationRefusal, match="not a regular file"):
        _verified_artifact_bytes(
            root,
            {"relative_path": f"receipts/sha256/{digest}.json", "sha256": digest},
            "service receipt",
        )


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
    paths["placement"].write_bytes(
        paths["placement"].read_bytes().replace(b"batch_size = 1\n", b"batch_size = 9\n", 1)
    )
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
