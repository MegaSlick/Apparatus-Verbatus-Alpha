"""The Perlector build bake-off queue (queue/perlector-fed-96gb.toml): its pins and serving
shape against the repository's own configuration, and a whole run of it against the fake
server on a synthetic run tree. No pod, no GPU, no real page."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

from operations.bakeoff import arms as A
from operations.bakeoff import queue_runner as Q
from operations.bakeoff import weights as W
from operations.bakeoff.test_bakeoff_fed_arm import SERVED, make_run_tree
from operations.bakeoff.test_bakeoff_queue import FakeNotifier
from operations.bakeoff.test_bakeoff_runner import FAKE, _free_port, _pages

MANIFEST = Path(__file__).with_name("queue") / "perlector-fed-96gb.toml"
ORDER = ["bf16-a", "bf16-b", "fp8", "fp8-mtp3", "nvfp4", "qwen35"]
PRIVATE = "/workspace/private"


def _arms() -> list[dict]:
    return tomllib.loads(MANIFEST.read_text())["arms"]


def _flag(command: list[str], flag: str) -> str | None:
    return command[command.index(flag) + 1] if flag in command else None


def test_the_manifest_validates_and_dry_runs(capsys):
    assert Q.main(["validate", "--manifest", str(MANIFEST)]) == 0
    assert Q.main(["run", "--manifest", str(MANIFEST), "--dry-run"]) == 0
    assert "--limit 2" in capsys.readouterr().out


def test_arms_are_in_the_planned_order_and_the_unproven_ones_only_cut_themselves():
    arms = _arms()
    assert [a["name"] for a in arms] == ORDER
    assert [a["cut"] for a in arms[:3]] == ["never"] * 3
    assert {a["name"]: a["cut"] for a in arms[3:]} == {
        "fp8-mtp3": "behind-schedule", "nvfp4": "behind-schedule", "qwen35": "behind-schedule",
    }  # fmt: skip
    assert not any("after" in a for a in arms)


def test_every_arm_replays_the_run_sealed_under_the_runs_own_name_on_the_pipelines_row():
    pins = W.pins()
    for arm in _arms():
        command = arm["command"]
        assert command[2:4] == ["operations.bakeoff.fed_arm", "run"]
        assert _flag(command, "--label") == arm["name"]
        assert _flag(command, "--sampling") == "sealed"
        assert _flag(command, "--run-tree") == f"{PRIVATE}/runs/RUN_ID"
        # The name is the run's: it is never given, so fed_arm asks for the recorded one.
        assert "--model-name" not in command and "--accept-new-model-name" not in command
        row = A.serving_row("perlector", "generic-80gb-plus", _flag(command, "--recipe"))
        assert _flag(command, "--max-num-seqs") == str(row["max_num_seqs"]) == "4"
        assert float(_flag(command, "--gpu-memory-utilization")) == float(
            row["gpu_memory_utilization"]
        )
        assert _flag(command, "--concurrency") == str(row["max_num_seqs"])
        assert "--max-num-batched-tokens" not in command  # the row's own
        fetched = arm["prepare"][-1]
        assert arm["prepare"][3] == "fetch"
        pin = pins.get(fetched) or A.chair_identity("perlector")  # bf16 is the chair's pin
        assert (_flag(command, "--repo"), _flag(command, "--revision")) == (
            pin["repo"], pin["revision"],
        )  # fmt: skip
        assert _flag(command, "--weights") == f"{PRIVATE}/model-store/hf/{fetched}"
    by = {a["name"]: _flag(a["command"], "--recipe") for a in _arms()}
    assert by == {
        "bf16-a": None, "bf16-b": None, "qwen35": None,
        "fp8": "unproven-real-perlector-fp8", "fp8-mtp3": "unproven-real-perlector-fp8-mtp3",
        "nvfp4": "unproven-real-perlector-nvfp4",
    }  # fmt: skip
    revisions = {a["name"]: _flag(a["command"], "--revision")[:8] for a in _arms()}
    assert revisions == {
        "bf16-a": "1d4bf0f2", "bf16-b": "1d4bf0f2", "fp8": "017b9c7a", "fp8-mtp3": "017b9c7a",
        "nvfp4": "482ca0f3", "qwen35": "fc05daec",
    }  # fmt: skip


def _quantization(recipe: str | None) -> dict:
    row = A.serving_row("perlector", "generic-80gb-plus", recipe)
    if row.get("quantization") == "fp8":
        return {"quantization_config": {"quant_method": "fp8"}}
    if row.get("quantization"):
        return {
            "quantization_config": {"quant_method": "modelopt", "quant_algo": "MIXED_PRECISION"}
        }
    return {}


def test_the_whole_queue_runs_against_the_fake_server_on_a_synthetic_run_tree(tmp_path):
    """The shipped manifest with only its paths moved: the run tree and snapshots are
    synthetic, the server is the fake, the weights fetch is a no-op."""
    tree = make_run_tree(tmp_path / "RUN_ID", pages=(("Le dix mai", "Le dix mai"),
                                                       ("Le onze juin", "BARE-TEST")))  # fmt: skip
    pages, out, home = tmp_path / "pages", tmp_path / "cache", tmp_path / "home"
    _pages(pages, 2)
    for stem in ("p000", "p001"):  # the queue's page list: the run's two stems
        (pages / f"{stem}.tif").rename(pages / f"p{int(stem[1:]) + 1:03d}.tif")
    data = tomllib.loads(MANIFEST.read_text())
    data.update(pages=str(pages), out=str(out), sync_to=str(home), end_pod="none")
    for arm in data["arms"]:
        command = [str(c) for c in arm["command"]]
        recipe = _flag(command, "--recipe")
        snapshot = tmp_path / "weights" / arm["name"]
        snapshot.mkdir(parents=True)
        (snapshot / "config.json").write_text(json.dumps(_quantization(recipe)))
        command[0] = sys.executable
        for flag, value in (
            ("--run-tree", str(tree)), ("--weights", str(snapshot)), ("--out", str(out)),
        ):  # fmt: skip
            command[command.index(flag) + 1] = value
        command += ["--vllm-cmd", sys.executable, str(FAKE), "--port", str(_free_port()),
                    "--startup-timeout", "60"]  # fmt: skip
        arm["command"] = command
        arm["prepare"] = [sys.executable, "-c", "pass"]
    queue = Q.Queue(
        Q.parse_manifest(data),
        notifier=FakeNotifier(),
        environ={"RUNPOD_POD_ID": "testpod", "PATH": "/usr/bin:/bin"},
        poll_seconds=0.05,
    )
    assert queue.run() == 0
    status = json.loads((out / "status.json").read_text())
    assert status["state"] == "done"
    assert {a["label"]: a["status"] for a in status["finished_arms"]} == dict.fromkeys(ORDER, "ok")
    for label in ORDER:
        clean = json.loads((out / label / "p001.json").read_text())
        bare = json.loads((out / label / "p002.json").read_text())
        # Asked under the run's name, the run's own request, byte for byte.
        assert clean["model_name"] == SERVED and clean["request"]["matches_run"] is True
        assert clean["setup"]["sampling_name"] == "sealed" and clean["error"] is None
        assert bare["parse_state"] == "parsed" and bare["repaired"] is True
    recipes = {
        label: json.loads((out / label / "p001.json").read_text())["setup"]["recipe"]
        for label in ORDER
    }
    assert recipes["fp8-mtp3"] == "unproven-real-perlector-fp8-mtp3" and recipes["bf16-a"] is None
    assert (home / "fp8" / "p001.json").is_file()
