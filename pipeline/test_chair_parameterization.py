"""The whole pipeline runs over both chair implementations.

`common/chairs/test_chairs_contract_suite.py` runs the chair protocol against
both implementations; this runs every stage program over the fixture, once
through the production `ChairRegistry` and once through the chair tests' own
`DeterministicChairRegistry`. It shows the stages run against either
implementation of the chair interface, and nothing about any model.

The stages are loaded and called in-process, because the injection seam is a
`main(registry_factory=...)` keyword. It is deliberately not a command-line
option: a `--registry fake` flag would be a live route to a fake answering under
a configured chair's name.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from common.chairs import (
    ChairIdentity,
    ChairRegistry,
    ServingDetails,
    exercise_contract,
)
from common.chairs.conftest import DeterministicChairRegistry
from conftest import load_stage, stage_programs

ROOT = Path(__file__).resolve().parents[1]
MODELS_CONFIG = ROOT / "config" / "models.toml"
FIXTURE_ROOT = ROOT / "proof"
# Every stage opens its context through the seam, including the pure consumers:
# they resolve identities while validating the provenance their producers wrote,
# so the whole skeleton has to receive the same implementation rather than
# quietly switching back to the production registry halfway down.
CHAIRS_THE_SKELETON_CALLS = {
    "attestator_1",
    "attestator_2",
    "attestator_3",
    "perlector",
}


def _invoke(module, monkeypatch, arguments: list[str], registry_factory) -> int:
    monkeypatch.setattr(sys, "argv", [str(module.__file__), *arguments])
    return module.main(registry_factory=registry_factory)


@pytest.mark.parametrize("implementation", ("registry", "deterministic"))
def test_the_full_skeleton_runs_over_both_chair_implementations(
    tmp_path, monkeypatch, implementation
):
    fake = DeterministicChairRegistry(MODELS_CONFIG)
    registry_factory = ChairRegistry.from_toml if implementation == "registry" else lambda _: fake
    arguments = [
        "--run-root",
        str(tmp_path / "runs"),
        "--run-id",
        f"{implementation}-seats",
        "--scenario",
        "page-unbroken",
        "--fixture-root",
        str(FIXTURE_ROOT),
        "--models-config",
        str(MODELS_CONFIG),
    ]

    for name, program in stage_programs().items():
        module = load_stage(Path(program).parent.name, Path(program).stem)
        assert _invoke(module, monkeypatch, arguments, registry_factory) == 0, (
            f"{name} did not complete over the {implementation} implementation"
        )

    tested = fake if implementation == "deterministic" else ChairRegistry.from_toml(MODELS_CONFIG)
    identity = tested.resolve("attestator_1")
    assert isinstance(identity, ChairIdentity)
    assert (
        exercise_contract(
            tested,
            role=identity.role,
            expected_identity=identity,
            serving=_details(identity),
        ).identity
        == identity
    )
    if implementation == "deterministic":
        # Both implementations agree, so a stage that fell back to the
        # production registry would still pass; the fake's own call log shows
        # it answered every chair the stages call, through all three methods.
        assert CHAIRS_THE_SKELETON_CALLS <= {role for _, role in fake.calls}
        assert {method for method, _ in fake.calls} == {"resolve", "ensure", "receipt"}


def _details(identity: ChairIdentity) -> ServingDetails:
    return ServingDetails(
        tokenizer_revision=identity.receipt_revision,
        seed=0,
        context_cap=4096,
        pixel_cap=52_000,
        engine=identity.serving_recipe,
        engine_version="fixture-v0",
        dtype="fixture",
        adapter_identity=None,
        endpoint="fixture://parameterization-test",
        started_at="2026-08-03T00:00:00Z",
    )
