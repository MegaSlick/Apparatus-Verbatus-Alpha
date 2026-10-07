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
from operations.pod.test_bootstrap_main import PROVEN_TIER, SURYA_CHAIR, _serving_workspace

from .config import parse_serving_recipes
from .qualify import QualificationRefusal, _verified_artifact_bytes, qualification_candidates
from .qualify import main as qualification_main
from .recordgold_smoke import (
    DAI_WITNESS_ADAPTER,
    RECORDGOLD_SMOKE_MAX_CER,
    RECORDGOLD_SMOKE_PROFILE,
    fetch_recordgold_smoke_page,
    score_recordgold_answer,
)
from .test_recordgold_smoke import TEST_GOLD_TEXT, record_pin

HASH = "a" * 64
PAGE_WITNESS = "ABEFGHJMNRTYabdefghijmnqrty23456789ABEFGHJM"
# The DAI chair's smoke page: a pinned test record answered from memory, and
# the digest of the PNG the pod would hand the chair.
TEST_RECORD, TEST_FETCH = record_pin()
TEST_RECORD_PAGE_SHA256 = digest_bytes(
    fetch_recordgold_smoke_page(TEST_RECORD, fetch=TEST_FETCH).png
)


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


def _replace_smoke_response(
    paths: dict[str, Path], smoke: dict[str, object], response: dict[str, object]
) -> None:
    response_bytes = canonical_bytes(response)
    reference = _write_bytes_artifact(paths["evidence"], "smoke-responses", response_bytes)
    smoke["smoke_response_reference"] = reference
    smoke["smoke_fixture_response_sha256"] = digest_bytes(response_bytes)
    smoke["fixture_response_sha256"] = digest_bytes(response_bytes)


def _qualification_fixture(
    tmp_path: Path,
    *,
    answer: str | None = None,
    recordgold_answer: str | None = None,
    dai_smoke_page: str = "recordgold-record",
    adjust_workspace=None,
) -> tuple[dict[str, Path], dict[str, object]]:
    ws, _ = _serving_workspace(tmp_path, preflight_state="unproven")
    if adjust_workspace is not None:
        adjust_workspace(ws)
    paths = {
        "report": ws.volume / "bootstrap-report.json",
        "evidence": ws.volume / "preflight" / "qualification",
        "models": ws.models_config,
        "recipes": ws.repository / "config" / "serving_recipes.toml",
        "placement": ws.placement_config,
    }
    return paths, _write_report(
        paths,
        PROVEN_TIER,
        answer=answer,
        recordgold_answer=recordgold_answer,
        dai_smoke_page=dai_smoke_page,
    )


def _write_report(
    paths: dict[str, Path],
    tier: str,
    *,
    answer: str | None = None,
    recordgold_answer: str | None = None,
    roles: set[str] | None = None,
    dai_smoke_page: str = "recordgold-record",
) -> dict[str, object]:
    """Write a green bootstrap report for a preflight that placed `roles` (all by default).

    Every served chair answers the golden page with `answer`, except the DAI
    chair, which (as the real preflight has it) read the pinned test record
    and answered `recordgold_answer`; `dai_smoke_page = "golden-page"` makes
    it read the golden page like the rest instead.
    """

    evidence_root = paths["evidence"]
    recipes_bytes = paths["recipes"].read_bytes()
    placement_bytes = paths["placement"].read_bytes()
    config_inputs = {
        "schema": "serving-config-inputs.v2",
        "serving_recipes_sha256": parse_sealed_toml(recipes_bytes, "recipes")[1],
        "pod_placement_sha256": parse_sealed_toml(placement_bytes, "placement")[1],
    }
    models = load_models_toml(paths["models"])
    profile_rows = tomllib.loads(recipes_bytes.decode("utf-8"))["profiles"]
    witness_bytes = PAGE_WITNESS.encode("ascii")
    witness_ref = _write_bytes_artifact(evidence_root, "page-witnesses", witness_bytes)
    witness_sha256 = digest_bytes(witness_bytes)
    if answer is None:
        answer = f"PAGE-WITNESS: {PAGE_WITNESS}"
    if recordgold_answer is None:
        recordgold_answer = TEST_GOLD_TEXT
    smoke_receipts = []
    cache_receipts = []
    placements = []
    subprocess_receipts = []
    for role, identity in sorted(models.chairs.items()):
        if not isinstance(identity, ChairIdentity):
            continue
        if roles is not None and role not in roles:
            continue
        row = next(row for row in profile_rows if row["chair"] == role and row["tier"] == tier)
        kind = row["kind"]
        if kind in ("subprocess", "in-process"):
            # A chair its stage runs itself is never served: preflight verifies
            # its weights and places it (and runs a subprocess chair's own
            # runner once on the golden page), so it has no smoke receipt.
            cache_receipts.append(
                {
                    "chair": role,
                    "manifest_digest": identity.digest_manifest,
                    "root": f"/runpod-volume/models/{role}",
                }
            )
            placements.append(
                {
                    "chair": role,
                    "configured_serving_recipe": identity.serving_recipe,
                    "tier": tier,
                    "state": kind,
                }
            )
            if kind == "subprocess":
                # Its own runner read the golden page once, in its own environment.
                subprocess_receipts.append(
                    {
                        "chair": role,
                        "environment": row["environment"],
                        "versions": {
                            package.replace("-", "_"): pin
                            for package, pin in row["required_packages"].items()
                        },
                    }
                )
            continue
        served_model_id = row["served_model_id"]
        reads_recordgold = (
            identity.witness_adapter == DAI_WITNESS_ADAPTER
            and dai_smoke_page == "recordgold-record"
        )
        chair_answer = recordgold_answer if reads_recordgold else answer
        response_bytes = canonical_bytes(
            {"model": served_model_id, "choices": [{"message": {"content": chair_answer}}]}
        )
        response_ref = _write_bytes_artifact(evidence_root, "smoke-responses", response_bytes)
        cache_receipts.append(
            {
                "chair": role,
                "manifest_digest": identity.digest_manifest,
                "root": f"/runpod-volume/models/{role}",
            }
        )
        placements.append(
            {
                "chair": role,
                "configured_serving_recipe": identity.serving_recipe,
                "tier": tier,
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
            "schema": "serving-launch-audit.v2",
            "chair": role,
            "launch_purpose": "preflight-qualification",
            "configuration_inputs": config_inputs,
            "chair_identity": identity.to_record(),
            "profile": {
                "recipe": identity.serving_recipe,
                "tier": tier,
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
        if reads_recordgold:
            score = score_recordgold_answer(chair_answer, TEST_GOLD_TEXT)
            assert score is not None
            page_read: dict[str, object] = {
                "smoke_page": "recordgold-record",
                "supplied_fixture_sha256": TEST_RECORD_PAGE_SHA256,
                "page_witness_sha256": TEST_RECORD.text_sha256,
                "page_witness_matches": score.passed,
                "page_witness_edit_distance": score.edits,
                "recordgold_record": TEST_RECORD.to_record(),
                "recordgold_page_sha256": TEST_RECORD_PAGE_SHA256,
                "character_error_rate": str(score.character_error_rate),
                "character_error_rate_threshold": str(RECORDGOLD_SMOKE_MAX_CER),
                "reference_units": score.reference_units,
                "normalization_profile": {
                    "profile_id": RECORDGOLD_SMOKE_PROFILE.profile_id,
                    "digest": RECORDGOLD_SMOKE_PROFILE.digest,
                },
            }
        else:
            page_read = {
                "smoke_page": "golden-page",
                "supplied_fixture_sha256": HASH,
                "page_witness_sha256": witness_sha256,
                "page_witness_matches": True,
                "page_witness_edit_distance": 0,
                "page_witness_reference": witness_ref,
            }
        smoke_receipts.append(
            {
                "chair": role,
                "served_engine": "vllm 0.test",
                "utilization": [{"gpu_percent": "50", "cpu_percent": "10"}],
                **page_read,
                "smoke_fixture_response_sha256": digest_bytes(response_bytes),
                "smoke_fixture_output_sha256": digest_bytes(canonical_bytes([chair_answer])),
                "fixture_response_sha256": digest_bytes(response_bytes),
                "smoke_response_reference": response_ref,
                "resolved_identity": identity.to_record(),
                "resolved_revision": identity.receipt_revision,
                "resolved_revision_kind": identity.receipt_revision_kind,
                "served_model_id": served_model_id,
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
        "placement_tier": tier,
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
        "subprocess_receipts": subprocess_receipts,
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
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    return wrapper


def _qualify(paths: dict[str, Path]) -> dict[str, object]:
    return qualification_candidates(
        report_path=paths["report"],
        evidence_root=paths["evidence"],
        models_config=paths["models"],
        recipes_config=paths["recipes"],
        placement_config=paths["placement"],
        recordgold_record=TEST_RECORD,
        recordgold_fetch=TEST_FETCH,
    )


def _dai_smoke(wrapper: dict[str, object]) -> dict[str, object]:
    """The DAI chair's smoke receipt in a written report."""
    smokes = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"]  # type: ignore[index]
    (smoke,) = [smoke for smoke in smokes if smoke["chair"] == "attestator_2"]
    return smoke


def test_green_qualification_renders_marks_for_only_the_measured_tier(tmp_path: Path) -> None:
    paths, _ = _qualification_fixture(tmp_path)

    record = _qualify(paths)

    assert record["schema"] == "serving-qualification-candidates.v2"
    candidates = record["candidates"]
    assert isinstance(candidates, list) and len(candidates) == 5
    assert {item["tier"] for item in candidates} == {PROVEN_TIER}
    golden = [item for item in candidates if item["smoke_page"] == "golden-page"]
    (dai,) = [item for item in candidates if item["smoke_page"] == "recordgold-record"]
    assert dai["chair"] == "attestator_2" and len(golden) == 4
    for candidate in golden:
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
    # The five served chairs; the Surya subprocess row has no proof state at all.
    assert SURYA_CHAIR not in {item["chair"] for item in candidates}
    assert (
        sum(getattr(profile, "preflight_state", None) == "proven" for profile in parsed.profiles)
        == 5
    )


REAL_CONFIG = Path(__file__).resolve().parents[2] / "config"


def _real_catalogue_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "report": tmp_path / "bootstrap-report.json",
        "evidence": tmp_path / "evidence",
        "models": REAL_CONFIG / "models-real.toml",
        "recipes": REAL_CONFIG / "serving_recipes_real.toml",
        "placement": REAL_CONFIG / "pod_placement.toml",
    }


def _real_rows_at(tier: str) -> dict[str, str]:
    raw = tomllib.loads((REAL_CONFIG / "serving_recipes_real.toml").read_text(encoding="utf-8"))
    return {row["chair"]: row["kind"] for row in raw["profiles"] if row["tier"] == tier}


@pytest.mark.parametrize("tier", ["generic-24gb", "generic-48gb", "generic-80gb-plus"])
def test_the_real_catalogue_qualifies_every_chair_a_green_preflight_served_at_each_tier(
    tmp_path: Path, tier: str
) -> None:
    # A green preflight at a small tier leaves out the chairs that tier cannot
    # serve; what it did smoke must still yield one candidate per served chair.
    kinds = _real_rows_at(tier)
    placed = {chair for chair, kind in kinds.items() if kind != "unsupported"}
    paths = _real_catalogue_paths(tmp_path)
    _write_report(paths, tier, roles=placed)

    record = _qualify(paths)

    assert record["preflight_chairs"] == sorted(placed)
    assert {item["chair"] for item in record["candidates"]} == {
        chair for chair, kind in kinds.items() if kind == "vllm"
    }
    assert {item["tier"] for item in record["candidates"]} == {tier}


def test_a_narrowed_preflight_qualifies_only_the_chairs_it_smoked(tmp_path: Path) -> None:
    paths = _real_catalogue_paths(tmp_path)
    _write_report(paths, "generic-24gb", roles={"attestator_1"})

    record = _qualify(paths)

    assert [item["chair"] for item in record["candidates"]] == ["attestator_1"]


def test_an_unsupported_chair_placed_in_a_report_claiming_green_is_refused(
    tmp_path: Path,
) -> None:
    # Preflight reports such a placement red; a report that claims green anyway
    # names a served chair with no smoke, and nothing is emitted.
    paths = _real_catalogue_paths(tmp_path)
    wrapper = _write_report(paths, "generic-24gb", roles={"attestator_1"})
    preflight = wrapper["bootstrap"]["receipts"]["preflight"]
    preflight["placements"].append(
        {
            "chair": "perlector",
            "configured_serving_recipe": "unproven-real-perlector",
            "tier": "generic-24gb",
            "state": "unservable-at-tier",
        }
    )
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    with pytest.raises(QualificationRefusal, match="do not cover exactly"):
        _qualify(paths)


def test_qualification_refuses_a_preflight_that_served_nothing(tmp_path: Path) -> None:
    paths = _real_catalogue_paths(tmp_path)
    _write_report(paths, "generic-24gb", roles={"designator_surya"})
    with pytest.raises(QualificationRefusal, match="no chair that is served"):
        _qualify(paths)


def test_a_parsed_catalogue_keeps_one_profile_per_row_in_file_order() -> None:
    """Qualification pairs each typed profile with its raw row by position."""

    shipped = Path(__file__).resolve().parents[2] / "config" / "serving_recipes.toml"
    raw = tomllib.loads(shipped.read_text(encoding="utf-8"))

    parsed = parse_serving_recipes(raw)

    assert [(p.recipe, p.chair, p.tier) for p in parsed.profiles] == [
        (row["recipe"], row["chair"], row["tier"]) for row in raw["profiles"]
    ]


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
        recordgold_record=seams.recordgold_record,
        recordgold_fetch=seams.recordgold_fetch,
    )["candidates"]

    assert isinstance(candidates, list) and len(candidates) == len(identities)
    # The DAI chair read the record the seams pinned; the producer's receipt
    # passes the verifier with no witness artifact to cite.
    (dai,) = [item for item in candidates if item["smoke_page"] == "recordgold-record"]
    assert dai["chair"] == "attestator_2"
    assert dai["page_witness_reference"] is None
    assert dai["recordgold_record"] == seams.recordgold_record.to_record()


# --- The DAI chair's RecordGold receipt ------------------------------------------


def test_the_dai_chair_s_recordgold_read_is_accepted_within_the_pinned_threshold(
    tmp_path: Path,
) -> None:
    """A slipped but passing read of the pinned record proves the DAI row; the
    candidate names the record and the measured rate, never the text."""
    slipped = TEST_GOLD_TEXT.replace("Pierre", "Piere")
    paths, _ = _qualification_fixture(tmp_path, recordgold_answer=slipped)
    score = score_recordgold_answer(slipped, TEST_GOLD_TEXT)
    assert score is not None and score.passed and score.edits == 1

    record = _qualify(paths)

    (dai,) = [item for item in record["candidates"] if item["chair"] == "attestator_2"]
    assert dai["smoke_page"] == "recordgold-record"
    assert dai["page_witness_reference"] is None
    assert dai["recordgold_record"] == TEST_RECORD.to_record()
    assert dai["recordgold_page_sha256"] == TEST_RECORD_PAGE_SHA256
    assert dai["page_witness_sha256"] == TEST_RECORD.text_sha256
    assert dai["character_error_rate"] == str(score.character_error_rate)
    assert dai["character_error_rate_threshold"] == str(RECORDGOLD_SMOKE_MAX_CER)
    assert dai["smoke_fixture_output_sha256"] == digest_bytes(canonical_bytes([slipped]))
    assert TEST_GOLD_TEXT not in json.dumps(record)
    assert len(record["candidates"]) == 5


def test_the_dai_chair_may_still_prove_its_row_with_an_exact_golden_page_read(
    tmp_path: Path,
) -> None:
    """A DAI receipt that read the golden page is held to the golden-page rule;
    the RecordGold path is an alternative the pod takes, not a relaxation."""
    paths, _ = _qualification_fixture(tmp_path, dai_smoke_page="golden-page")

    record = _qualify(paths)

    assert all(item["smoke_page"] == "golden-page" for item in record["candidates"])
    assert len(record["candidates"]) == 5


def test_a_chair_that_is_not_the_dai_reader_may_not_smoke_the_recordgold_record(
    tmp_path: Path,
) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smoke = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"][0]  # type: ignore[index]
    assert smoke["chair"] == "attestator_1" and smoke["smoke_page"] == "golden-page"
    smoke["smoke_page"] = "recordgold-record"
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match="'attestator_1' is not a RecordGold reader"):
        _qualify(paths)


def test_a_dai_read_past_the_pass_line_is_refused_even_when_preflight_claims_green(
    tmp_path: Path,
) -> None:
    mangled = TEST_GOLD_TEXT[:40]
    score = score_recordgold_answer(mangled, TEST_GOLD_TEXT)
    assert score is not None and not score.passed
    paths, wrapper = _qualification_fixture(tmp_path, recordgold_answer=mangled)
    # The fixture wrote what the pod would: a receipt that records the failing
    # read honestly. Claiming a pass on top of it is refused the same way.
    with pytest.raises(QualificationRefusal, match="past the 0.15 pass line"):
        _qualify(paths)
    _dai_smoke(wrapper)["page_witness_matches"] = True
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    with pytest.raises(QualificationRefusal, match="past the 0.15 pass line"):
        _qualify(paths)


def _other_record() -> dict[str, object]:
    return {**TEST_RECORD.to_record(), "record_id": "00000000-0000-0000-0000-00000000beef"}


@pytest.mark.parametrize(
    ("field", "bad_value", "refusal"),
    [
        ("smoke_page", "some-other-page", "smoked an unknown page 'some-other-page'"),
        ("recordgold_record", _other_record(), "a RecordGold record other than the pinned"),
        ("recordgold_page_sha256", "f" * 64, "a page other than the verified RecordGold record"),
        ("supplied_fixture_sha256", "f" * 64, "a page other than the verified RecordGold record"),
        ("page_witness_sha256", "e" * 64, "transcription digest disagrees with the pin"),
        ("page_witness_edit_distance", 3, "page-read edit distance disagrees"),
        ("character_error_rate", "0.0500", "character error rate disagrees"),
        ("character_error_rate_threshold", "0.5000", "threshold is not the pinned 0.15"),
        ("reference_units", 7, "reference units disagree"),
        ("normalization_profile", {"profile_id": "other"}, "normalisation profile"),
        ("page_witness_matches", False, "did not record a passing RecordGold read"),
    ],
)
def test_qualification_refuses_each_disagreeing_recordgold_fact_by_name(
    tmp_path: Path, field: str, bad_value: object, refusal: str
) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    _dai_smoke(wrapper)[field] = bad_value
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match=refusal):
        _qualify(paths)


def test_qualification_refuses_when_the_pinned_record_s_own_copy_has_drifted(
    tmp_path: Path,
) -> None:
    """The gold the answer is scored against must itself pass the pin."""
    paths, _ = _qualification_fixture(tmp_path)
    _, drifted_fetch = record_pin(text=TEST_GOLD_TEXT[:-1] + "s")

    with pytest.raises(QualificationRefusal, match="pinned RecordGold record cannot be verified"):
        qualification_candidates(
            report_path=paths["report"],
            evidence_root=paths["evidence"],
            models_config=paths["models"],
            recipes_config=paths["recipes"],
            placement_config=paths["placement"],
            recordgold_record=TEST_RECORD,
            recordgold_fetch=drifted_fetch,
        )


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


def test_an_in_process_chair_needs_no_smoke_receipt_and_is_never_a_candidate(
    tmp_path: Path,
) -> None:
    paths, _ = _qualification_fixture(tmp_path)
    candidates = _qualify(paths)["candidates"]
    assert len(candidates) == 5
    assert "secondary_proposer" not in {item["chair"] for item in candidates}


def test_an_in_process_chair_must_be_placed_in_process(tmp_path: Path) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    for placement in wrapper["bootstrap"]["receipts"]["preflight"]["placements"]:
        if placement["chair"] == "secondary_proposer":
            placement["state"] = "planned"
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    with pytest.raises(QualificationRefusal, match="'secondary_proposer' was not planned"):
        _qualify(paths)


def test_a_subprocess_chair_needs_its_runner_s_receipt(tmp_path: Path) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    preflight = wrapper["bootstrap"]["receipts"]["preflight"]
    (receipt,) = preflight["subprocess_receipts"]
    assert receipt["chair"] == SURYA_CHAIR
    receipt["environment"] = "operations/serving/elsewhere"
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    with pytest.raises(QualificationRefusal, match="does not name its row's environment"):
        _qualify(paths)
    preflight["subprocess_receipts"] = []
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    with pytest.raises(QualificationRefusal, match="run as subprocesses"):
        _qualify(paths)


def test_a_subprocess_receipt_must_measure_the_row_s_pinned_packages(tmp_path: Path) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    (receipt,) = wrapper["bootstrap"]["receipts"]["preflight"]["subprocess_receipts"]
    receipt["versions"]["torch"] = "2.14.0+cu130"
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    _qualify(paths)
    receipt["versions"]["surya_ocr"] = "0.22.0"
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    with pytest.raises(QualificationRefusal, match="ran surya-ocr '0.22.0'.*pins '0.22.1'"):
        _qualify(paths)
    del receipt["versions"]
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")
    with pytest.raises(QualificationRefusal, match="measured no versions"):
        _qualify(paths)


def test_qualification_refuses_record_without_producer_wrapper(tmp_path: Path) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    paths["report"].write_text(json.dumps(wrapper["bootstrap"]), encoding="utf-8")
    with pytest.raises(QualificationRefusal, match="not a bootstrap result or hold report"):
        _qualify(paths)


def test_qualification_refuses_a_roster_that_declares_an_adapter_chair(tmp_path: Path) -> None:
    paths, _ = _qualification_fixture(tmp_path)
    models = paths["models"].read_text(encoding="utf-8")
    models = models.replace(
        "[chairs.attestator_1]\n",
        '[chairs.attestator_1]\nadapter_of = "attestator_2"\n',
        1,
    )
    paths["models"].write_text(models, encoding="utf-8")

    with pytest.raises(QualificationRefusal, match="adapter_of is not supported"):
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


def test_qualification_refuses_a_response_digest_that_disagrees_with_its_artifact(
    tmp_path: Path,
) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smoke = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"][0]  # type: ignore[index]
    smoke["smoke_fixture_response_sha256"] = "e" * 64  # type: ignore[index]
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match="response digest disagrees"):
        _qualify(paths)


@pytest.mark.parametrize("change", ["model", "output-shape"])
def test_qualification_refuses_a_response_with_a_different_model_or_output_shape(
    tmp_path: Path, change: str
) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smoke = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"][0]  # type: ignore[index]
    reference = smoke["smoke_response_reference"]  # type: ignore[index]
    response = json.loads((paths["evidence"] / reference["relative_path"]).read_bytes())
    if change == "model":
        response["model"] = "another-served-model"
    else:
        response["choices"].append(response["choices"][0])
    _replace_smoke_response(paths, smoke, response)
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match="different model or output shape"):
        _qualify(paths)


def test_qualification_refuses_a_missing_smoke_response_reference(tmp_path: Path) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smoke = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"][0]  # type: ignore[index]
    del smoke["smoke_response_reference"]  # type: ignore[index]
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match="smoke response reference"):
        _qualify(paths)


def test_qualification_refuses_an_output_digest_that_does_not_match_the_witness(
    tmp_path: Path,
) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smoke = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"][0]  # type: ignore[index]
    smoke["smoke_fixture_output_sha256"] = "e" * 64  # type: ignore[index]
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match="output digest disagrees"):
        _qualify(paths)


def test_qualification_accepts_internal_witness_whitespace_with_raw_digest(tmp_path: Path) -> None:
    answer = f"PAGE-WITNESS: {PAGE_WITNESS[:8]} \t{PAGE_WITNESS[8:20]}\n{PAGE_WITNESS[20:]}"
    paths, _ = _qualification_fixture(tmp_path, answer=answer)

    record = _qualify(paths)

    assert all(
        item["smoke_fixture_output_sha256"] == digest_bytes(canonical_bytes([answer]))
        for item in record["candidates"]
        if item["smoke_page"] == "golden-page"
    )


@pytest.mark.parametrize("code", [PAGE_WITNESS[:-1], PAGE_WITNESS[:-2], PAGE_WITNESS + "A"])
def test_qualification_refuses_near_reads_even_when_preflight_is_green(
    tmp_path: Path, code: str
) -> None:
    paths, wrapper = _qualification_fixture(tmp_path, answer=f"PAGE-WITNESS: {code}")
    smokes = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"]  # type: ignore[index]
    for smoke in smokes:
        smoke["page_witness_edit_distance"] = abs(len(code) - len(PAGE_WITNESS))
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match="only an exact read can prove"):
        _qualify(paths)


def test_qualification_names_a_receipt_without_recorded_edit_distance(tmp_path: Path) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smokes = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"]  # type: ignore[index]
    del smokes[0]["page_witness_edit_distance"]
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(
        QualificationRefusal,
        match="smoke receipt records no page_witness_edit_distance; re-run preflight",
    ):
        _qualify(paths)


@pytest.mark.parametrize("recorded_distance", [0.0, False])
def test_qualification_refuses_noninteger_edit_distance(
    tmp_path: Path, recorded_distance: object
) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smokes = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"]  # type: ignore[index]
    smokes[0]["page_witness_edit_distance"] = recorded_distance
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match="page-read edit distance disagrees"):
        _qualify(paths)


@pytest.mark.parametrize(
    "answer",
    [
        f"PAGE-WITNESS: {PAGE_WITNESS[:-3]}CCC",
        f"PAGE-WITNESS: {PAGE_WITNESS} ",
        f"PAGE-WITNESS: {PAGE_WITNESS}\u200b",
    ],
)
def test_qualification_refuses_other_answer_changes(tmp_path: Path, answer: str) -> None:
    paths, _ = _qualification_fixture(tmp_path, answer=answer)

    with pytest.raises(QualificationRefusal, match="near transcription"):
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


def _rebind(
    evidence_root: Path,
    smoke: dict,
    *,
    receipt: dict | None = None,
    audit: dict | None = None,
) -> None:
    """Rewrite one smoke receipt's serving artifacts consistently, so only the
    field under test disagrees."""

    if receipt is not None:
        smoke["service_receipt"] = receipt
        smoke["receipt_reference"] = _write_artifact(evidence_root, "receipts", receipt)
    if audit is not None:
        smoke["serving_launch_audit"] = audit
        smoke["serving_launch_audit_reference"] = _write_artifact(
            evidence_root, "launch-audits", audit
        )
    smoke["serving_evidence_reference"] = _write_artifact(
        evidence_root,
        "serving-evidence",
        {
            "schema": "serving-evidence.v1",
            "receipt_reference": smoke["receipt_reference"],
            "launch_audit_reference": smoke["serving_launch_audit_reference"],
        },
    )


def _smoke_field(field: str, value: object):
    def mutate(root: Path, smoke: dict) -> None:
        del root
        smoke[field] = value

    return mutate


def _embedded(field: str):
    def mutate(root: Path, smoke: dict) -> None:
        del root
        smoke[field] = {**smoke[field], "chair": "someone-else"}

    return mutate


def _misbound(root: Path, smoke: dict) -> None:
    smoke["serving_evidence_reference"] = _write_artifact(
        root,
        "serving-evidence",
        {
            "schema": "serving-evidence.v1",
            "receipt_reference": smoke["receipt_reference"],
            "launch_audit_reference": smoke["receipt_reference"],
        },
    )


def _receipt_changed(root: Path, smoke: dict) -> None:
    _rebind(root, smoke, receipt={**smoke["service_receipt"], "chair": "someone-else"})


def _audit_changed(field: str, value: object):
    def mutate(root: Path, smoke: dict) -> None:
        _rebind(root, smoke, audit={**smoke["serving_launch_audit"], field: value})

    return mutate


def _audit_profile_changed(root: Path, smoke: dict) -> None:
    audit = smoke["serving_launch_audit"]
    _rebind(root, smoke, audit={**audit, "profile": {**audit["profile"], "tier": "other-tier"}})


@pytest.mark.parametrize(
    ("mutate", "refusal"),
    [
        (_smoke_field("served_engine", ""), "has no served engine"),
        (_smoke_field("utilization", []), "has no utilization samples"),
        (_smoke_field("utilization", [{"gpu_percent": "50"}]), "malformed utilization samples"),
        (
            _smoke_field("utilization", [{"gpu_percent": "50", "cpu_percent": ""}]),
            "malformed utilization samples",
        ),
        (_smoke_field("supplied_fixture_sha256", "f" * 64), "smoked a different golden page"),
        (_smoke_field("smoke_fixture_request_count", True), "no valid smoke_fixture_request_count"),
        (_embedded("service_receipt"), "service receipt artifact disagrees"),
        (_embedded("serving_launch_audit"), "launch audit artifact disagrees"),
        (_misbound, "serving evidence is misbound"),
        (_receipt_changed, "service receipt identity changed"),
        (_smoke_field("served_engine", "vllm 0.other"), "served-engine claim changed"),
        (_audit_changed("schema", "serving-launch-audit.v0"), "launch audit has the wrong schema"),
        (_audit_changed("chair", "someone-else"), "launch audit names another chair"),
        (_audit_changed("configuration_inputs", {}), "launch used different configuration"),
        (_audit_changed("chair_identity", {}), "launch used a different identity"),
        (_audit_profile_changed, "launch audit names another profile"),
    ],
)
def test_qualification_refuses_each_disagreeing_smoke_fact_by_name(
    tmp_path: Path, mutate, refusal: str
) -> None:
    paths, wrapper = _qualification_fixture(tmp_path)
    smoke = wrapper["bootstrap"]["receipts"]["preflight"]["smoke_receipts"][0]  # type: ignore[index]
    mutate(paths["evidence"], smoke)
    paths["report"].write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(QualificationRefusal, match=refusal):
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
    # The command line has no seam for the RecordGold pin: it verifies the real
    # record's committed copy (`test_recordgold_smoke` covers that copy), so
    # this fixture has the DAI chair read the golden page like the rest.
    paths, _ = _qualification_fixture(tmp_path, dai_smoke_page="golden-page")
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
