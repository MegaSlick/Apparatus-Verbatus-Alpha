"""Preflight of a chair its stage runs as a subprocess (Surya).

Never served and never on the card: preflight checks its weights against the
pinned manifest and its own environment's versions, and reads no golden page
through it.
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
        'timeout_seconds = 600\nrequired_packages = { "surya-ocr" = "0.22.1", torch = "2.14.0" }\n'
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
        return {"manifest_digest": identity.digest_manifest}


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


def _run(tmp_path: Path, checker):
    cache, smoke = Cache(), Smoke()
    runner = PreflightRunner(
        load_models_toml(ROOT / "config" / "models.toml"),
        load_placement_table(ROOT / "config/pod_placement.toml"),
        cache,
        smoke,
        ROOT / "proof/fixtures/synthetic-two-page-v0/page-1.png",
        serving_recipes=load_serving_recipes(_catalogue(tmp_path)),
        subprocess_checker=checker,
    )
    report = runner.run(
        GpuProfile("synthetic", "12.4", "550", (8, 0), Decimal("48"), Decimal("100"), "bfloat16")
    )
    return report, cache, smoke


def test_a_subprocess_chair_is_checked_for_weights_and_environment_and_never_smoke_read(
    tmp_path,
):
    checked: list[tuple[str, str]] = []

    def checker(identity, profile):  # type: ignore[no-untyped-def]
        checked.append((identity.role, profile.environment))
        return {"surya_ocr": "0.22.1", "torch": "2.14.0", "python": "3.12.3"}

    report, cache, smoke = _run(tmp_path, checker)
    placement = {item.chair: item for item in report.placements}["designator_surya"]
    assert (placement.state, placement.tier, placement.residency) == (
        "subprocess",
        "generic-48gb",
        None,
    )
    assert "designator_surya" in cache.verified
    assert "designator_surya" not in smoke.read_by
    assert checked == [("designator_surya", "operations/serving/surya")]
    assert not [issue for issue in report.issues if issue.chair == "designator_surya"]


def test_an_environment_that_does_not_answer_its_pins_turns_preflight_red(tmp_path):
    def checker(identity, profile):  # type: ignore[no-untyped-def]
        raise ServingConfigurationError("Surya's environment reports surya-ocr 0.22.0")

    report, _cache, _smoke = _run(tmp_path, checker)
    assert report.color == "red"
    (issue,) = [issue for issue in report.issues if issue.chair == "designator_surya"]
    assert issue.code == "subprocess-environment-unready"
    assert "uv sync --frozen --project operations/serving/surya" in issue.remediation
