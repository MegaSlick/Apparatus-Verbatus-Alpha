"""The Perlector's chair left serving for the Coniector, over one run tree.

Both stages run their own `main` here, one after the other as the orchestrator
runs them, against one scripted endpoint: the card both stage processes share.
The Perlector is told the Coniector runs next (`--hand-off-to-coniector`); the
reconstructor's sealed row shares the Perlector's service, and the run's roster
binds the reconstructor to the Perlector's own bytes, so the Coniector takes the
running service over instead of starting the model again, and stops it before
its own seal. Nothing here starts a pod, opens a socket or loads a model.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from common.chairs.registry import ChairRegistry
from common.contracts.stages import CONIECTOR, PERLECTOR
from common.decoding import load_decoding_policy
from common.reconstruction_records import CALL_KIND, RECONSTRUCTION_KIND, verified_reconstructions
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE, reading_acts
from conftest import _stage_records, load_stage, page_context, programs_through, run_stage
from operations.serving.assembly import SERVING_READER, retain_chair_bytes
from operations.serving.client import ChairClient
from operations.serving.config import (
    ServingConfigInputs,
    chair_preflight_identity_digest,
    load_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.fakes import FakeEndpoint, FakeLauncher, FakePackages, ScriptedAnswer
from operations.serving.manager import ServingManager, StageContextReceiptPublisher
from operations.serving.residency import FileResidencyLease

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "r"
TIER = "generic-48gb"
SERVED = "shared-27b"
FIXTURE = tomllib.loads((ROOT / "proof" / "skeleton_fixture.toml").read_text(encoding="utf-8"))
PAGE_ANSWERS = [
    row["answer"]
    for ordinal in (1, 2)
    for row in FIXTURE["page_answer"]
    if row["scenario"] == "happy" and row["page_ordinal"] == ordinal
]
RECONSTRUCTION_ANSWERS = [
    row["answer"]
    for ordinal in (1, 2)
    for row in FIXTURE["reconstruction_answer"]
    if row["scenario"] == "happy"
    and row["page_ordinal"] == ordinal
    and row["pages_are_consecutive"] is False
]


def _shared_roster(directory: Path) -> Path:
    """The fixture roster with the reconstructor bound to the Perlector's own bytes,
    as the real roster binds it to the Perlector's checkpoint."""
    directory.mkdir(parents=True)
    for name in ("model-fixtures", "manifests"):
        shutil.copytree(ROOT / "config" / name, directory / name)
    text = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    chairs = tomllib.loads(text)["chairs"]
    perlector = chairs["perlector"]
    start = text.index("[chairs.reconstructor]\n")
    end = text.index("\n[", start + 1)
    section = (
        "[chairs.reconstructor]\n"
        'state = "configured"\n'
        'source = "local-repository"\n'
        f'path = "{perlector["path"]}"\n'
        f'digest_manifest = "{perlector["digest_manifest"]}"\n'
        f'manifest = "{perlector["manifest"]}"\n'
        f'serving_recipe = "{chairs["reconstructor"]["serving_recipe"]}"\n'
        f'license_note = "{perlector["license_note"]}"\n'
    )
    path = directory / "models.toml"
    path.write_text(text[:start] + section + text[end + 1 :], encoding="utf-8")
    return path


def _row(identity, **extra: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": "vllm",
        "recipe": identity.serving_recipe,
        "chair": identity.role,
        "tier": TIER,
        **extra,
        "host": "127.0.0.1",
        "port": 8106,
        "served_model_id": SERVED,
        "dtype": "bfloat16",
        "seed": 0,
        "required_packages": {"vllm": "0.test"},
        "max_model_len": 16384,
        "max_num_seqs": 1,
        "max_num_batched_tokens": 512,
        "gpu_memory_utilization": "0.58",
        "min_pixels": 3136,
        "max_pixels": 1806336,
        "patch_size": 16,
        "merge_size": 2,
        "enable_prefix_caching": True,
        "enforce_eager": False,
        "trust_remote_code": False,
        "generation_config": "vllm",
        "preflight_state": "proven",
        "startup_timeout_seconds": 3,
        "poll_interval_seconds": 1,
        "request_timeout_seconds": 30,
        "preflight_identity_digest": chair_preflight_identity_digest(identity),
    }
    row["readiness_probe"] = {
        "kind": "chat-completions",
        "request_json": '{"messages":[{"role":"user","content":"READY"}],"max_tokens":4}',
    }
    row["preflight_digest"] = profile_preflight_digest(row)
    return row


def _toml(row: dict[str, Any]) -> str:
    def value(item: object) -> str:
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, int):
            return str(item)
        if isinstance(item, dict):
            return "{ " + ", ".join(f'"{k}" = {value(v)}' for k, v in item.items()) + " }"
        return json.dumps(item)

    return "[[profiles]]\n" + "".join(f"{key} = {value(item)}\n" for key, item in row.items())


def _shared_catalogue(directory: Path, models: Path) -> Path:
    """The fixture catalogue with the Perlector and the reconstructor served live at
    the test tier, the reconstructor's row sharing the Perlector's service."""
    registry = ChairRegistry.from_toml(str(models))
    text = (ROOT / "config" / "serving_recipes.toml").read_text(encoding="utf-8")
    for role, extra in (("perlector", {}), ("reconstructor", {"shares_service_with": "perlector"})):
        identity = registry.resolve(role)
        pattern = re.compile(
            r'\[\[profiles\]\]\nkind = "fixture"\nrecipe = "'
            + re.escape(identity.serving_recipe)
            + r'"\nchair = "'
            + role
            + r'"\ntier = "'
            + TIER
            + r'"\ndescription = "[^"\n]*"\n'
        )
        replacement = _toml(_row(identity, **extra))
        text, count = pattern.subn(lambda _match, row=replacement: row, text)
        assert count == 1, role
    path = directory / "serving_recipes.toml"
    path.write_text(text, encoding="utf-8")
    load_serving_recipes(path)
    return path


@pytest.fixture(scope="module")
def through_attestatores(tmp_path_factory) -> tuple[Path, dict[str, str]]:
    base = tmp_path_factory.mktemp("shared-service")
    models = _shared_roster(base / "config")
    catalogue = _shared_catalogue(base / "config", models)
    options = {"models_config": str(models), "serving_recipes_config": str(catalogue)}
    root = base / "runs"
    for program in programs_through("attestatores"):
        result = run_stage(root, RUN_ID, "happy", program, **options)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return root, options


class _Card:
    """One pod's card: the endpoint every stage process reaches, its lease and the
    hand-off record beside it."""

    def __init__(self, directory: Path, root: Path) -> None:
        self.directory = directory
        self.endpoint = FakeEndpoint(served_model_id=SERVED)
        self.lease = directory / "pod-gpu.lock"
        self.hand_off = directory / "pod-gpu.hand-off.json"
        self.scope = str(RunTree(root, RUN_ID).root)
        self.launchers: list[FakeLauncher] = []

    def manager(self, context, producer: str) -> ServingManager:
        launcher = FakeLauncher(self.endpoint)
        self.launchers.append(launcher)
        return ServingManager(
            registry=context.registry,
            recipes=load_serving_recipes(context.args.serving_recipes_config),
            config_inputs=ServingConfigInputs.from_record(dict(context.serving_config_inputs)),
            launcher=launcher,
            http=self.endpoint,
            receipt_publisher=StageContextReceiptPublisher(context),
            log_root=self.directory / "serving-logs",
            package_inspector=FakePackages({"vllm": "0.test"}),
            residency_lease=FileResidencyLease(self.lease),
            hand_off_path=self.hand_off,
            service_scope=self.scope,
            producer=producer,
        )

    def client(self, context, identity, tier, policy, policy_sha256, producer) -> ChairClient:
        return ChairClient(
            manager=self.manager(context, producer),
            identity=identity,
            tier=tier,
            retain=lambda data: retain_chair_bytes(context, data),
            decoding_config_sha256=policy_sha256,
            decoding_policy=policy,
            read_receipt=context.tree.read_run_receipt,
        )

    @property
    def launches(self) -> int:
        return sum(len(launcher.calls) for launcher in self.launchers)


def _argv(program: str, root: Path, options: dict[str, str], *extra: str) -> list[str]:
    argv = [str(ROOT / program), "--run-root", str(root), "--run-id", RUN_ID]
    for name, value in {**options, "placement_tier": TIER}.items():
        argv += [f"--{name.replace('_', '-')}", value]
    return [*argv, *extra]


def _run_perlector(card: _Card, root: Path, options, monkeypatch, *extra: str) -> int:
    policy, policy_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))

    def factory(context, identity, tier) -> ChairClient:
        return card.client(
            context, identity, tier, policy, policy_sha256, "pipeline/4_perlector/run.py"
        )

    monkeypatch.setattr(sys, "argv", _argv("pipeline/4_perlector/run.py", root, options, *extra))
    return load_stage("4_perlector").main(serving_factory=factory)


def _run_coniector(card: _Card, root: Path, options, monkeypatch) -> int:
    def factory(context, identity, tier, *, decoding_policy, decoding_config_sha256):
        return card.client(
            context,
            identity,
            tier,
            decoding_policy,
            decoding_config_sha256,
            "pipeline/4b_coniector/run.py",
        )

    monkeypatch.setattr(sys, "argv", _argv("pipeline/4b_coniector/run.py", root, options))
    return load_stage("4b_coniector").main(serving_factory=factory)


def _launch_audits(root: Path, stage: str) -> list[dict[str, Any]]:
    blobs = root / RUN_ID / stage / "blobs" / "sha256"
    audits: list[dict[str, Any]] = []
    for path in sorted(blobs.iterdir()) if blobs.is_dir() else ():
        try:
            value = json.loads(path.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("schema") == "serving-launch-audit.v2":
            audits.append(value)
    return audits


@pytest.fixture()
def run_root(through_attestatores, tmp_path) -> tuple[Path, dict[str, str]]:
    template, options = through_attestatores
    root = tmp_path / "runs"
    shutil.copytree(template, root)
    return root, options


def test_the_coniector_takes_over_the_perlectors_chair_and_stops_it(
    run_root, tmp_path, monkeypatch
):
    root, options = run_root
    card = _Card(tmp_path, root)
    card.endpoint.script(
        *(ScriptedAnswer(content=answer, finish_reason="stop") for answer in PAGE_ANSWERS)
    )
    monkeypatch.chdir(ROOT)

    assert _run_perlector(card, root, options, monkeypatch, "--hand-off-to-coniector") == 0

    [process] = card.launchers[0].processes
    assert process.poll() is None, "the Perlector's chair was stopped, not handed off"
    assert card.hand_off.is_file()
    # The Perlector's seal is written with its chair still serving.
    assert _stage_records(root, RUN_ID, "4_perlector", "stage-seal")

    card.endpoint.script(
        *(ScriptedAnswer(content=answer, finish_reason="stop") for answer in RECONSTRUCTION_ANSWERS)
    )
    assert _run_coniector(card, root, options, monkeypatch) == EXIT_COMPLETE

    assert card.launches == 1, "the Coniector started the model again"
    assert process.poll() is not None, "the taken-over chair was not stopped"
    assert not card.hand_off.exists()
    [perlector_audit] = _launch_audits(root, "4_perlector")
    [adopted] = _launch_audits(root, "4b_coniector")
    assert adopted["chair"] == "reconstructor"
    assert adopted["launch_purpose"] == "adopted"
    assert adopted["started_at"] == perlector_audit["started_at"]
    assert adopted["command"]["argv_sha256"] == perlector_audit["command"]["argv_sha256"]
    assert adopted["adoption"]["from_chair"] == PERLECTOR
    calls = [record for _path, record in _stage_records(root, RUN_ID, "4b_coniector", CALL_KIND)]
    assert len(calls) == 2
    tree = RunTree(root, RUN_ID)
    for record in calls:
        receipt = tree.read_run_receipt(record["payload"]["maker"]["receipt_ref"])
        assert receipt["chair"] == "reconstructor"
        assert receipt["started_at"] == perlector_audit["started_at"]
    made = [
        record["payload"]["made"]
        for _path, record in _stage_records(root, RUN_ID, "4b_coniector", RECONSTRUCTION_KIND)
    ]
    assert made and all(made)
    assert _stage_records(root, RUN_ID, "4b_coniector", "stage-seal")
    # What the Armarium recomputes accepts a reconstruction made by a taken-over service.
    context = page_context(
        root, RUN_ID, "happy", options, stage=CONIECTOR, serving_reader=SERVING_READER
    )
    verified = verified_reconstructions(context, reading_acts(context))
    assert {call["serving_mode"] for call in verified["calls"].values()} == {"live"}


def test_without_the_coniector_next_the_perlector_stops_its_chair_before_the_seal(
    run_root, tmp_path, monkeypatch
):
    root, options = run_root
    card = _Card(tmp_path, root)
    card.endpoint.script(
        *(ScriptedAnswer(content=answer, finish_reason="stop") for answer in PAGE_ANSWERS)
    )
    monkeypatch.chdir(ROOT)

    assert _run_perlector(card, root, options, monkeypatch) == 0

    [process] = card.launchers[0].processes
    assert process.poll() is not None
    assert not card.hand_off.exists()


def test_a_coniector_that_sends_nothing_stops_the_chair_left_for_it(
    run_root, tmp_path, monkeypatch
):
    """The Perlector reads nothing it can parse, so the Coniector has no call to send;
    it still stops the chair left serving for it, before its own seal."""
    root, options = run_root
    card = _Card(tmp_path, root)
    card.endpoint.script(*(ScriptedAnswer(content="not an answer", finish_reason="stop"),) * 2)
    monkeypatch.chdir(ROOT)

    assert _run_perlector(card, root, options, monkeypatch, "--hand-off-to-coniector") == 0
    [process] = card.launchers[0].processes
    assert process.poll() is None

    assert _run_coniector(card, root, options, monkeypatch) == EXIT_COMPLETE

    assert card.launches == 1
    assert process.poll() is not None
    assert not card.hand_off.exists()
    assert _launch_audits(root, "4b_coniector") == []
