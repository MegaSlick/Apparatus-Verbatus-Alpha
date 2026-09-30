"""A single orchestrated run combining a page break, re-shoots, and a clean page.

The serving transport is scripted; the orchestrator and every stage program use
their production entry points and the run tree is read through its verifiers.
"""

from __future__ import annotations

import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from test_structure_chair_e2e import (
    RUN_ID,
    TIER,
    ReaderWorld,
    StructureWorld,
    WitnessWorld,
    artifacts,
    witness_scripts,
    write_catalogue,
)

from common.chairs.registry import ChairRegistry
from common.contracts.canonical import digest_bytes
from common.contracts.identities import physical_page_id
from common.contracts.outcomes import ArmariumCategory
from common.contracts.stages import DESIGNATOR, PERLECTOR
from common.corpus_register import append_records, empty_register, register_digest
from common.decoding import load_decoding_policy
from common.physical_act_partition import CROSS_CAPTURE_READ_NOT_BUILT
from common.runtree.store import RunTree
from common.stage import EXIT_HELD, verify_final_seal
from conftest import load_stage
from operations.serving.fakes import InProcessSurya, scripted_structure_answer
from operations.submit import gate, submit
from proof.synthetic_pages import PAGE_BREAK_PAGES, render_page

ROOT = Path(__file__).resolve().parents[1]
orchestrator = load_stage("orchestrator")
designator = load_stage("2_designator")
attestatores = load_stage("3_attestatores")
perlector = load_stage("4_perlector")
armarium_export = load_stage("7_armarium", "armarium_export", isolate_path=True)

# The first two pages are the existing page-break fixture. The next two are
# distinct captures of one physical page; the last is an unrelated clean page.
PAGES = (
    *PAGE_BREAK_PAGES,
    {
        "ordinal": 3,
        "width": 200,
        "height": 260,
        "acts": ({"ordinal": 3, "bounds": {"x": 20, "y": 75, "w": 160, "h": 80}, "ink": 60},),
    },
    {
        "ordinal": 4,
        "width": 200,
        "height": 260,
        "acts": ({"ordinal": 3, "bounds": {"x": 25, "y": 80, "w": 150, "h": 80}, "ink": 70},),
    },
    {
        "ordinal": 5,
        "width": 200,
        "height": 260,
        "acts": ({"ordinal": 4, "bounds": {"x": 20, "y": 75, "w": 160, "h": 80}, "ink": 110},),
    },
)
ACTS_BY_PAGE = {
    page["ordinal"]: tuple(
        (act["bounds"], f"REHEARSAL PAGE {page['ordinal']} ACT {index}")
        for index, act in enumerate(page["acts"])
    )
    for page in PAGES
}
KEYS = {
    f"proposal:{page}:{index}" for page, acts in ACTS_BY_PAGE.items() for index in range(len(acts))
}
HELD_KEYS = {"proposal:3:0", "proposal:4:0"}
HEAD, TAIL = "proposal:1:1", "proposal:2:0"


def _submission(work: Path) -> tuple[Path, Path, Path, Path, dict[int, bytes]]:
    approved = work / "approved-storage"
    source = approved / "submitted-pages"
    source.mkdir(parents=True)
    pages = {page["ordinal"]: render_page(page) for page in PAGES}
    for ordinal, data in pages.items():
        (source / f"page-{ordinal}.png").write_bytes(data)
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(approved)]
    policy_path = work / "data-gate-policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    ledger = approved / "submission-ledger.json"
    submit.submit(source, ledger, policy_path=policy_path)
    return approved, source, ledger, policy_path, pages


def _register(work: Path, pages: dict[int, bytes]) -> Path:
    physical_page = physical_page_id("rehearsal", "volume-1", "leaf-3")
    register = work / "register.json"
    append_records(
        register,
        [
            {
                "kind": "physical-page",
                "corpus_id": "rehearsal",
                "volume_id": "volume-1",
                "designation": "leaf-3",
                "physical_page_id": physical_page,
                "appending_run": "rehearsal",
            },
            {
                "kind": "membership",
                "physical_page_id": physical_page,
                "members": sorted(digest_bytes(pages[ordinal]) for ordinal in (3, 4)),
                "predecessor": None,
                "appending_run": "rehearsal",
            },
        ],
        expected_digest=register_digest(empty_register()),
    )
    return register


@pytest.mark.act_path
def test_combined_rehearsal_accounts_for_every_act_and_verifies_export(tmp_path, monkeypatch):
    """Both re-shoot captures are held, because reading across a physical page's
    captures is not built yet; the remaining five acts are delivered and exported.
    """
    approved, source, ledger, policy, pages = _submission(tmp_path)
    register = _register(tmp_path, pages)
    catalogue = write_catalogue(
        tmp_path / "serving_recipes_live.toml",
        ChairRegistry.from_toml(str(ROOT / "config" / "models.toml")),
    )
    _policy, decoding_sha256 = load_decoding_policy(str(ROOT / "config" / "decoding.toml"))
    structure = StructureWorld(
        catalogue,
        tmp_path / "structure-world",
        [
            scripted_structure_answer(ACTS_BY_PAGE[ordinal], 200, 260)
            for ordinal in sorted(ACTS_BY_PAGE)
        ],
        # The fixture declares no Surya detections for these pages.
        surya=InProcessSurya((), ()),
    )
    witnesses = WitnessWorld(
        catalogue,
        decoding_sha256,
        tmp_path / "witness-world",
        witness_scripts([ACTS_BY_PAGE[ordinal] for ordinal in sorted(ACTS_BY_PAGE)]),
    )
    reader = ReaderWorld(catalogue, tmp_path / "reader", finish_reason="stop")
    injected = {
        "pipeline/2_designator/run.py": (
            designator,
            {"serving_factory": structure.factory, "surya_runner": structure.surya},
        ),
        "pipeline/3_attestatores/run.py": (attestatores, {"serving_factory": witnesses.factory}),
        "pipeline/4_perlector/run.py": (perlector, {"serving_factory": reader.factory}),
    }
    real_subprocess_run = subprocess.run

    def run_stage(command, **kwargs):
        program = str(Path(command[2]).relative_to(ROOT))
        if program not in injected:
            return real_subprocess_run(command, **kwargs)
        module, seams = injected[program]
        original_argv, sys.argv = sys.argv, [str(command[2]), *command[3:]]
        try:
            return SimpleNamespace(returncode=module.main(**seams))
        finally:
            sys.argv = original_argv

    monkeypatch.setattr(orchestrator.subprocess, "run", run_stage)
    original_argv, sys.argv = (
        sys.argv,
        [
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--run-id",
            RUN_ID,
            "--run-root",
            str(approved / "runs"),
            "--submission-folder",
            str(source),
            "--submission-manifest",
            str(ledger),
            "--data-gate-policy",
            str(policy),
            "--corpus-register",
            str(register),
            "--models-config",
            str(ROOT / "config/models.toml"),
            "--serving-recipes-config",
            str(catalogue),
            "--placement-tier",
            TIER,
        ],
    )
    try:
        exit_code = orchestrator.main()
    finally:
        sys.argv = original_argv
    assert exit_code == EXIT_HELD

    root = approved / "runs"
    tree = RunTree(root, RUN_ID)
    proposals = artifacts(root, DESIGNATOR, "region")
    assert len(proposals) == len(KEYS) == 7
    assert {row["payload"]["act_key"] for row in proposals} == KEYS
    (candidate,) = artifacts(root, DESIGNATOR, "continuation-candidate")
    assert [row["act_key"] for row in candidate["payload"]["acts_a"]] == [HEAD]
    assert [row["act_key"] for row in candidate["payload"]["acts_b"]] == [TAIL]

    export = verify_final_seal(tree)
    assert export["outcome"] == ArmariumCategory.HELD_FOR_REVIEW.value
    payload = export["payload"]
    assert payload["expected_acts"] == len(KEYS)
    delivered = {row["act_key"]: row["text"] for row in payload["delivered"]}
    held = {row["act_key"]: row for row in payload["non_delivered"]}
    assert len(payload["delivered"]) == len(delivered) == 5
    assert len(payload["non_delivered"]) == len(held) == 2
    assert set(delivered) == KEYS - HELD_KEYS
    assert set(held) == HELD_KEYS
    assert all(row["category"] == ArmariumCategory.HELD_FOR_REVIEW.value for row in held.values())
    not_run = [
        row for row in artifacts(root, PERLECTOR, "perlectio") if row["outcome"] == "not-run"
    ]
    assert {row["payload"]["act_key"] for row in not_run} == HELD_KEYS
    assert all(row["payload"]["hold"]["code"] == CROSS_CAPTURE_READ_NOT_BUILT for row in not_run)
    # Two reader calls for each of the five delivered acts, none for the held captures:
    # Pass A runs only under --blind-read fed or saved, neither of which this run sets.
    assert len(reader.endpoint.requests) == 10
    assert set(delivered).isdisjoint(held)
    aggregate = payload["aggregate"]
    assert aggregate["status"] == "partial"
    assert aggregate["by_category"] == {
        ArmariumCategory.DELIVERED.value: 5,
        ArmariumCategory.HELD_FOR_REVIEW.value: 2,
    }
    assert len(aggregate["reasons"]) == 3
    assert any(
        reason.startswith("continuation join join-1-2-0 (reconstructed)")
        for reason in aggregate["reasons"]
    )
    assert {reason for reason in aggregate["reasons"] if reason.startswith("act proposal:")} == {
        f"act {key} is held-for-review" for key in HELD_KEYS
    }

    bundle = tree.read_bytes(payload["bundle"]["reference"]["relative_path"])
    manifest = armarium_export.verify_export_bundle(bundle, tmp_path / "verified-export")
    assert manifest["claims"]["status"] == "partial"
    with ZipFile(BytesIO(bundle)) as archive:
        (readings,) = [name for name in archive.namelist() if name.endswith("readings.txt")]
        lines = archive.read(readings).decode("utf-8").split("\n")
    reconstructed = json.loads(lines[lines.index("reconstructed_text:") + 1])
    assert reconstructed == delivered[HEAD] + "\n" + delivered[TAIL]
