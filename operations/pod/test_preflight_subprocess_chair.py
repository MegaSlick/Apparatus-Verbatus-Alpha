"""Preflight of a chair its stage runs as a subprocess (Surya).

Never served and never on the card: preflight checks its weights against the
pinned manifest, then runs the chair's own runner once on the golden page, on
the CPU, and records what that run measured.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from common.chairs.config import load_models_toml
from operations.pod.preflight import (
    GpuProfile,
    PreflightRunner,
    SmokeResult,
    UtilizationSample,
    load_placement_table,
)
from operations.serving.config import load_serving_recipes
from operations.serving.errors import ServingConfigurationError

ROOT = Path(__file__).resolve().parents[2]

TIERS = ("generic-24gb", "generic-48gb", "generic-80gb-plus")


def _catalogue(tmp_path: Path) -> Path:
    """The shipped fixture catalogue with the Surya chair's rows as subprocess rows."""
    shipped = (ROOT / "config" / "serving_recipes.toml").read_text(encoding="utf-8")
    fixture_rows = "".join(
        f'\n[[profiles]]\nkind = "fixture"\nrecipe = "fake-surya-v0"\nchair = "designator_surya"\n'
        f'tier = "{tier}"\n'
        'description = "offline walking-skeleton fixture for the Surya detector chair"\n'
        for tier in TIERS
    )
    subprocess_rows = "".join(
        f'\n[[profiles]]\nkind = "subprocess"\nrecipe = "fake-surya-v0"\n'
        f'chair = "designator_surya"\ntier = "{tier}"\nengine = "surya"\n'
        'environment = "operations/serving/surya"\ndevice = "cpu"\nthreads = 2\n'
        'startup_timeout_seconds = 300\nseconds_per_page = 60\nrequired_packages = { "surya-ocr" = "0.22.1", torch = "2.14.0" }\n'
        for tier in TIERS
    )
    assert fixture_rows in shipped
    path = tmp_path / "serving_recipes.toml"
    path.write_text(shipped.replace(fixture_rows, subprocess_rows), encoding="utf-8")
    return path


class Cache:
    def __init__(self) -> None:
        self.verified: list[str] = []

    def verify(self, identity):  # type: ignore[no-untyped-def]
        self.verified.append(identity.role)
        return {"manifest_digest": identity.digest_manifest, "root": f"/store/{identity.role}"}


class Smoke:
    def __init__(self) -> None:
        self.read_by: list[str] = []

    def read(self, identity, fixture, placement):  # type: ignore[no-untyped-def]
        self.read_by.append(identity.role)
        return SmokeResult(
            shape_valid=True,
            nonempty=True,
            format_valid=True,
            receipt={"fixture": fixture.name, "tier": placement.identifier},
            utilization=(UtilizationSample(Decimal("71"), Decimal("31")),),
        )


GOLDEN_PAGE = ROOT / "proof/fixtures/synthetic-two-page-v0/page-1.png"
MEASURED = {
    "versions": {
        "surya_ocr": "0.22.1",
        "torch": "2.14.0",
        "python": "3.12.3",
        "cpu_capability": "AVX512",
        "machine": "x86_64",
    },
    "engine_version": "surya-ocr 0.22.1; torch 2.14.0; cpu AVX512 on x86_64",
    "golden_page": {"lines": 3, "blocks": 1, "reading_order": "surya-order-head"},
}


def _run(tmp_path: Path, checker, golden_page: Path = GOLDEN_PAGE):
    cache, smoke = Cache(), Smoke()
    runner = PreflightRunner(
        load_models_toml(ROOT / "config" / "models.toml"),
        load_placement_table(ROOT / "config/pod_placement.toml"),
        cache,
        smoke,
        golden_page,
        serving_recipes=load_serving_recipes(_catalogue(tmp_path)),
        subprocess_checker=checker,
    )
    report = runner.run(
        GpuProfile("synthetic", "12.4", "550", (8, 0), Decimal("48"), Decimal("100"), "bfloat16")
    )
    return report, cache, smoke


def test_a_subprocess_chair_runs_once_on_the_golden_page_and_is_never_smoke_read(
    tmp_path,
):
    checked: list[tuple[str, str, Path, Path]] = []

    def checker(identity, profile, weights_root, golden_page):  # type: ignore[no-untyped-def]
        checked.append((identity.role, profile.environment, weights_root, golden_page))
        return MEASURED

    report, cache, smoke = _run(tmp_path, checker)
    placement = {item.chair: item for item in report.placements}["designator_surya"]
    assert (placement.state, placement.tier, placement.residency) == (
        "subprocess",
        "generic-48gb",
        None,
    )
    assert "designator_surya" in cache.verified
    assert "designator_surya" not in smoke.read_by
    assert checked == [
        (
            "designator_surya",
            "operations/serving/surya",
            Path("/store/designator_surya"),
            GOLDEN_PAGE,
        )
    ]
    assert not [issue for issue in report.issues if issue.chair == "designator_surya"]
    # The measured versions and CPU are in the report, beside the chair they belong to.
    assert report.to_record()["subprocess_receipts"] == [
        {"chair": "designator_surya", "environment": "operations/serving/surya", **MEASURED}
    ]


def test_an_environment_that_does_not_answer_its_pins_turns_preflight_red(tmp_path):
    def checker(identity, profile, weights_root, golden_page):  # type: ignore[no-untyped-def]
        raise ServingConfigurationError("Surya's environment reports surya-ocr 0.22.0")

    report, _cache, _smoke = _run(tmp_path, checker)
    assert report.color == "red"
    (issue,) = [issue for issue in report.issues if issue.chair == "designator_surya"]
    assert issue.code == "subprocess-environment-unready"
    assert "uv sync --locked --project operations/serving/surya" in issue.remediation


def test_a_missing_golden_page_runs_nothing_and_is_already_red(tmp_path):
    def checker(identity, profile, weights_root, golden_page):  # type: ignore[no-untyped-def]
        raise AssertionError("nothing may run without the golden page")

    report, _cache, _smoke = _run(tmp_path, checker, tmp_path / "missing.png")
    assert report.color == "red"
    assert "proof-fixture-missing" in {issue.code for issue in report.issues}
    assert report.to_record()["subprocess_receipts"] == []


def test_the_production_check_runs_the_runner_on_the_golden_page(tmp_path, monkeypatch):
    from operations.pod import preflight
    from operations.serving import surya_detector
    from operations.serving.fakes import InProcessSurya

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "weights.bin").write_bytes(b"w")
    seen = []

    def run(profile, weights_root, pages, sizes, identity):  # type: ignore[no-untyped-def]
        seen.append((weights_root, sorted(pages), sizes))
        return InProcessSurya([], [])(profile, weights_root, pages, sizes, identity)

    monkeypatch.setattr(surya_detector, "run_surya_subprocess", run)
    recipes = load_serving_recipes(_catalogue(tmp_path))
    identity = load_models_toml(ROOT / "config" / "models.toml").chairs["designator_surya"]
    profile = recipes.for_identity(identity, "generic-48gb")
    measured = preflight.check_subprocess_environment(identity, profile, bundle, GOLDEN_PAGE)
    assert seen == [(bundle, [1], {1: (200, 260)})]
    assert measured["versions"]["surya_ocr"] == "0.22.1"
    assert measured["golden_page"] == {
        "lines": 0,
        "blocks": 0,
        "reading_order": "surya-order-head",
    }
