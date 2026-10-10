"""The queue runner end to end on the CPU: witness arms against the fake server, a CPU arm,
a flaky and a broken arm, the schedule's cuts on a fake clock, the copy and its digests,
the pod's end, SIGTERM, and the Mac's watch and fetch. Synthetic pages only."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from operations.bakeoff import queue_runner as Q
from operations.bakeoff import witness_run as W
from operations.bakeoff.test_bakeoff_runner import FAKE, _free_port, _pages

ROOT = Path(__file__).resolve().parents[2]
POD_DELETE = ROOT / "operations" / "pod" / "pod_delete.sh"
EXAMPLES = Path(__file__).with_name("queue")

# A stand-in arm: writes a cache record in the page schema for each page it is given.
CPU_ARM = """
import argparse, json, os, sys, time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--pages"); p.add_argument("--out"); p.add_argument("--label")
p.add_argument("--limit", type=int); p.add_argument("--sleep", type=float, default=0)
p.add_argument("--fail", action="store_true"); p.add_argument("--fail-once")
p.add_argument("--pid-file"); p.add_argument("--folder"); p.add_argument("--threads")
p.add_argument("--meet", nargs=2, help="write the first file, then wait for the second")
a = p.parse_args()
if a.pid_file:
    Path(a.pid_file).write_text(str(os.getpid()))
if a.meet:
    Path(a.meet[0]).write_text("here")
    deadline = time.monotonic() + 20
    while not Path(a.meet[1]).exists():
        if time.monotonic() > deadline:
            sys.exit(6)
        time.sleep(0.05)
time.sleep(a.sleep)
if a.fail:
    sys.exit(3)
if a.fail_once and not Path(a.fail_once).exists():
    Path(a.fail_once).write_text("failed once")
    sys.exit(4)
folder = Path(a.out) / (a.folder or a.label)
folder.mkdir(parents=True, exist_ok=True)
for page in sorted(Path(a.pages).glob("*.tif"))[: a.limit or None]:
    record = {
        "schema": "bakeoff-witness-page.v1", "model": a.label, "arm": "cpu-test",
        "repo": None, "revision": None, "weights": None,
        "server": {"url": None, "argv": sys.argv}, "page": page.stem,
        "source_file": page.name, "source_sha256": "0" * 64, "units": [],
        "text": "Le dix mai", "empty": False, "finish_reason": "stop", "loop": False,
        "loop_reasons": [], "seconds": 0.0, "error": None, "written": "now",
    }
    (folder / (page.stem + ".json")).write_text(json.dumps(record))
"""


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class FakeNotifier:
    def __init__(self, on_message=None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.on_message = on_message

    def __call__(self, kind: str, message: str):
        self.sent.append((kind, message))
        if self.on_message:
            self.on_message(message)
        return type("Outcome", (), {"delivered": True, "suppressed": False, "detail": ""})()


class Recording(Q.Queue):
    """Keeps a copy of every status the queue writes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[dict] = []

    def write_status(self) -> None:
        super().write_status()
        self.seen.append(json.loads((self.m.out / "status.json").read_text()))


@pytest.fixture
def bench(tmp_path):
    pages = tmp_path / "pages"
    _pages(pages, 3)
    script = tmp_path / "cpu_arm.py"
    script.write_text(CPU_ARM)
    guard = tmp_path / "guard"
    guard.mkdir()
    return {
        "tmp": tmp_path,
        "pages": pages,
        "out": tmp_path / "cache",
        "home": tmp_path / "home",
        "script": script,
        "guard": guard,
        "env": {**os.environ, "RUNPOD_POD_ID": "testpod", "POD_GUARD_DIR": str(guard)},
    }


def _cpu_arm(bench, name, *extra, **fields):
    command = [sys.executable, str(bench["script"]), "--pages", str(bench["pages"])]
    command += ["--out", str(bench["out"]), "--label", name, *extra]
    return {"name": name, "time_box_min": 10, "cut": "never", "command": command, **fields}


def _witness_arm(bench, model, label):
    weights = bench["tmp"] / "weights" / label
    weights.mkdir(parents=True, exist_ok=True)
    (weights / "config.json").write_text("{}")
    command = [sys.executable, "-m", "operations.bakeoff.witness_run", "run", "--model", model]
    command += ["--label", label, "--pages", str(bench["pages"]), "--out", str(bench["out"])]
    command += ["--port", str(_free_port()), "--max-num-seqs", "2", "--startup-timeout", "60"]
    command += ["--vllm-cmd", sys.executable, str(FAKE), "--weights", str(weights)]
    return {"name": label, "time_box_min": 20, "cut": "never", "command": command}


def _marker(path: Path) -> list[str]:
    return [sys.executable, "-c", f"import pathlib; pathlib.Path({str(path)!r}).write_text('x')"]


def _manifest(bench, arms, **extra):
    data = {
        "schema": Q.QUEUE_SCHEMA,
        "name": "test-queue",
        "pages": str(bench["pages"]),
        "out": str(bench["out"]),
        "sync_to": str(bench["home"]),
        "smoke_pages": 2,
        "arms": arms,
        **extra,
    }
    return Q.parse_manifest(data)


def _events(out: Path) -> list[dict]:
    return [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]


def _fresh_heartbeat(bench) -> None:
    (bench["guard"] / "heartbeat-testpod").write_text("")


def test_full_queue_smoke_then_run_overlap_retry_sync_and_guard_release(bench):
    arms = [
        _witness_arm(bench, "chandra", "chandra"),
        {**_witness_arm(bench, "churro", "churro"), "prepare": _marker(bench["tmp"] / "prep")},
        _witness_arm(bench, "chandra", "chandra-b"),
        _cpu_arm(bench, "cpu-lines", "--sleep", "0.5", gpu=False),
        _cpu_arm(bench, "flaky", "--fail-once", str(bench["tmp"] / "flaky-once")),
        _cpu_arm(bench, "broken", "--fail"),
    ]
    _fresh_heartbeat(bench)
    notifier, deletes = FakeNotifier(), []
    queue = Recording(
        _manifest(bench, arms),
        notifier=notifier,
        runner=lambda argv: deletes.append(argv) or 0,
        environ=bench["env"],
        poll_seconds=0.05,
    )
    assert queue.run() == 0

    out = bench["out"]
    status = json.loads((out / "status.json").read_text())
    assert status["schema"] == "bakeoff-queue-status.v1" and status["state"] == "done"
    assert status["pod_id"] == "testpod" and status["phase"] == "end-pod"
    assert {a["label"]: a["status"] for a in status["finished_arms"]} == {
        "chandra": "ok",
        "churro": "ok",
        "chandra-b": "ok",
        "cpu-lines": "ok",
        "flaky": "ok",
        "broken": "failed",
    }
    assert all(a["pages"] == 3 for a in status["finished_arms"] if a["status"] == "ok")
    assert "broken: smoke failed (exit 3)" in status["errors"]
    assert status["end_action"] == "its guard was asked to delete it"

    # Smoke before the full run for each arm, retries after every first pass, then the end.
    phases = [(s["arm"], s["phase"]) for s in queue.seen]
    for label in ("chandra", "churro", "chandra-b"):
        assert phases.index((label, "smoke")) < phases.index((label, "run"))
    assert phases.index(("chandra", "smoke")) < phases.index(("flaky", "retry"))
    assert phases.index(("broken", "retry")) < phases.index((None, "sync"))
    assert phases[-1] == (None, "end-pod")
    smokes = [s for s in queue.seen if s["arm"] == "chandra" and s["phase"] == "smoke"]
    assert smokes and all(s["pages_total"] == 2 for s in smokes)
    runs = [s for s in queue.seen if s["arm"] == "chandra" and s["phase"] == "run"]
    assert runs and all(s["pages_total"] == 3 for s in runs)

    # The smoke carries --limit; churro's preparation ran while chandra's command ran.
    events = _events(out)
    starts = [e for e in events if e["event"] == "queue-command-start"]
    smoke = next(e for e in starts if e["arm"] == "chandra" and e["phase"] == "smoke")
    assert smoke["argv"][-2:] == ["--limit", "2"]
    prep = next(e for e in starts if e["arm"] == "churro" and e["phase"] == "prepare")
    chandra_done = next(
        e
        for e in events
        if e["event"] == "queue-command-end" and e["arm"] == "chandra" and e["phase"] == "smoke"
    )
    assert prep["t"] < chandra_done["t"]
    assert (bench["tmp"] / "prep").exists()
    assert any(e["event"] == "server-ready" and e["model"] == "chandra" for e in events)

    # Every ping once; the queue's name leads each one; the end is a done event.
    messages = [m for _, m in notifier.sent]
    assert len(messages) == len(set(messages))
    assert all(m.startswith("test-queue: ") for m in messages)
    assert sum("started (" in m for m in messages) == 6
    assert sum("ok:" in m for m in messages) == 5
    assert notifier.sent[-1][0] == "queue-done" and "guard" in notifier.sent[-1][1]
    # The queue handles a failed arm itself: milestones, never a decision.
    assert [k for k, m in notifier.sent if "broken" in m] == ["milestone"] * 3
    assert sum("arm failed: broken" in m for _, m in notifier.sent) == 2
    assert "decision" not in [k for k, _ in notifier.sent]
    assert len(status["pings"]) == len(set(status["pings"]))

    # The copy, its digests and DONE.json, identical in both places.
    done = json.loads((out / "DONE.json").read_text())
    assert done == json.loads((bench["home"] / "DONE.json").read_text())
    assert done["verified"] and done["queue"] == "test-queue" and done["pod_id"] == "testpod"
    assert done["digests"]["chandra/p000.json"] == Q.sha256_file(out / "chandra" / "p000.json")
    assert "status.json" not in done["digests"] and done["files"] == len(done["digests"])
    assert done["status"]["arms"] == {"ok": 5, "failed": 1}

    # A fresh guard heartbeat: the deadline moves to now and the guard does the delete.
    deadline = int((bench["guard"] / "deadline-testpod").read_text())
    assert abs(deadline - time.time()) < 120
    assert "queue test-queue ended done" in (bench["guard"] / "released-testpod").read_text()
    assert deletes == []


def test_schedule_cuts_overrun_and_install_rules_on_a_fake_clock(bench):
    clock = FakeClock()

    def advance(message: str) -> None:
        if "alpha started" in message:
            clock.t += 50 * 60

    notifier = FakeNotifier(advance)
    fail = [sys.executable, "-c", "raise SystemExit(5)"]
    arms = [
        _cpu_arm(bench, "alpha"),
        _cpu_arm(bench, "bravo", cut="overrun", time_box_min=30),
        _cpu_arm(bench, "delta", cut="behind-schedule"),
        _cpu_arm(bench, "charlie", cut="overrun", time_box_min=60),
        _cpu_arm(bench, "echo", cut="never"),
        _cpu_arm(bench, "foxtrot", cut="install-failed", install=fail),
        _cpu_arm(bench, "golf", cut="never", install=fail),
    ]
    queue = Q.Queue(
        _manifest(bench, arms, end_pod="none"),
        clock=clock,
        notifier=notifier,
        environ=bench["env"],
        poll_seconds=0.05,
    )
    assert queue.run() == 0
    status = json.loads((bench["out"] / "status.json").read_text())
    assert [s["arm"] for s in status["skipped"]] == ["bravo", "delta", "foxtrot"]
    assert {a["label"]: a["status"] for a in status["finished_arms"]} == {
        "alpha": "ok",
        "charlie": "ok",
        "echo": "ok",
        "golf": "failed",
    }
    messages = [m for _, m in notifier.sent]
    assert sum("past its 10 min box" in m for m in messages) == 1
    assert sum(" skipped: " in m for m in messages) == 3
    assert any("golf install failed; retried at the end" in m for m in messages)
    events = [e["event"] for e in _events(bench["out"])]
    assert events.count("queue-overrun") == 1 and events.count("queue-arm-skipped") == 3
    assert status["end_action"] == "kept (end_pod = none)"
    assert not (bench["guard"] / "deadline-testpod").exists()


def test_an_arm_that_cannot_start_is_retried_reported_and_the_queue_still_ends(bench):
    """A typo in a program path is that arm's error: retried, reported, never the end."""
    env_dump = bench["tmp"] / "arm-env.json"
    dump = [
        sys.executable,
        "-c",
        f"import json, os, sys; open({str(env_dump)!r}, 'w').write(json.dumps(sorted(os.environ)))",
    ]
    arms = [
        {**_cpu_arm(bench, "typo"), "command": ["/no/such/program", "--label", "typo"]},
        {**_cpu_arm(bench, "envcheck"), "command": [*dump, "--label", "envcheck"]},
        _cpu_arm(bench, "good"),
    ]
    notifier, deletes = FakeNotifier(), []
    env = {**bench["env"], "RUNPOD_API_KEY": "key-not-real", "NTFY_TOPIC": "topic-not-real"}
    queue = Q.Queue(
        _manifest(bench, arms),
        notifier=notifier,
        runner=lambda argv: deletes.append(argv) or 0,
        environ=env,
        poll_seconds=0.05,
    )
    assert queue.run() == 0
    status = json.loads((bench["out"] / "status.json").read_text())
    assert {a["label"]: a["status"] for a in status["finished_arms"]} == {
        "good": "ok",
        "typo": "failed",
        "envcheck": "failed",
    }
    assert any(e.startswith("typo: crashed (FileNotFoundError") for e in status["errors"])
    assert (bench["home"] / "DONE.json").is_file() and deletes
    # The arms never see the pod's API key or the phone topic.
    names = json.loads(env_dump.read_text())
    assert "RUNPOD_API_KEY" not in names and "NTFY_TOPIC" not in names
    assert "HF_HUB_OFFLINE" in names
    blob = (bench["out"] / "events.jsonl").read_text() + (bench["out"] / "status.json").read_text()
    assert "key-not-real" not in blob and "topic-not-real" not in blob


def test_the_copy_waits_for_a_skipped_arms_preparation(bench):
    """A preparation still writing into the cache must finish before the copy and digests."""
    clock = FakeClock()
    marker = bench["out"] / "late" / "prepared.txt"
    slow = [
        sys.executable,
        "-c",
        f"import pathlib, time; time.sleep(2); pathlib.Path({str(marker)!r}).write_text('done')",
    ]
    arms = [
        _cpu_arm(bench, "first"),
        {**_cpu_arm(bench, "late", cut="overrun", time_box_min=5), "prepare": slow},
    ]
    notifier = FakeNotifier(lambda m: "first started" in m and setattr(clock, "t", clock.t + 3600))
    queue = Q.Queue(
        _manifest(bench, arms, end_pod="none"),
        clock=clock,
        notifier=notifier,
        environ=bench["env"],
        poll_seconds=0.05,
    )
    assert queue.run() == 0
    done = json.loads((bench["out"] / "DONE.json").read_text())
    assert done["verified"] and "late/prepared.txt" in done["digests"]
    assert [
        s["arm"] for s in json.loads((bench["out"] / "status.json").read_text())["skipped"]
    ] == ["late"]


def _ends(events, name):
    """Event positions (the file's own order) of an arm's command starts and its end."""
    starts = [i for i, e in enumerate(events) if e["event"] == "queue-command-start"
              and e["arm"] == name and e["phase"] in ("smoke", "run", "retry")]  # fmt: skip
    end = next(
        i for i, e in enumerate(events) if e["event"] == "queue-arm-end" and e["arm"] == name
    )
    return starts, end


def test_cpu_arms_run_at_once_dependants_wait_and_the_copy_waits_for_every_lane(bench):
    """Two CPU arms that can only finish together (each waits for the other's marker), a
    dependant that writes its pages elsewhere, a GPU arm beside them, then the copy."""
    tmp = bench["tmp"]
    meet = [tmp / "left-here", tmp / "right-here"]
    arms = [
        _cpu_arm(bench, "gpu-a"),
        _cpu_arm(bench, "left", "--meet", *map(str, meet), gpu=False, threads=2),
        _cpu_arm(bench, "right", "--meet", *map(str, meet[::-1]), gpu=False, threads=2),
        _cpu_arm(
            bench,
            "child",
            "--folder",
            "_lines/child",
            gpu=False,
            after=["left"],
            writes="_lines/child",
            threads=2,
        ),  # fmt: skip
    ]
    queue = Recording(
        _manifest(bench, arms, end_pod="none", cpu_threads=4),
        notifier=FakeNotifier(),
        environ=bench["env"],
        poll_seconds=0.05,
    )
    assert queue.run() == 0
    status = json.loads((bench["out"] / "status.json").read_text())
    assert {a["label"]: a["status"] for a in status["finished_arms"]} == dict.fromkeys(
        ("gpu-a", "left", "right", "child"), "ok"
    )
    assert next(a for a in status["finished_arms"] if a["label"] == "child")["pages"] == 3
    events = _events(bench["out"])
    child_starts, _ = _ends(events, "child")
    _, left_end = _ends(events, "left")
    assert min(child_starts) > left_end
    # Two CPU arms at once show in the status beside the GPU arm.
    assert any(len(s["cpu_arms"]) == 2 for s in queue.seen)
    sync = next(i for i, e in enumerate(events) if e["event"] == "queue-sync")
    assert sync > max(_ends(events, name)[1] for name in ("gpu-a", "left", "right", "child"))
    done = json.loads((bench["out"] / "DONE.json").read_text())
    assert "_lines/child/p002.json" in done["digests"] and done["verified"]


def test_a_failed_or_skipped_dependency_skips_its_dependants_with_the_reason(bench):
    fail = [sys.executable, "-c", "raise SystemExit(5)"]
    arms = [
        _cpu_arm(bench, "broken", "--fail", gpu=False),
        _cpu_arm(bench, "flaky", "--fail-once", str(bench["tmp"] / "flaky-once"), gpu=False),
        _cpu_arm(bench, "no-install", cut="install-failed", install=fail),
        _cpu_arm(bench, "needs-broken", gpu=False, after=["broken"]),
        _cpu_arm(bench, "needs-flaky", after=["flaky"]),
        _cpu_arm(bench, "needs-skipped", gpu=False, after=["no-install"]),
    ]
    queue = Q.Queue(
        _manifest(bench, arms, end_pod="none", cpu_threads=4),
        notifier=FakeNotifier(),
        environ=bench["env"],
        poll_seconds=0.05,
    )
    assert queue.run() == 0
    status = json.loads((bench["out"] / "status.json").read_text())
    assert {a["label"]: a["status"] for a in status["finished_arms"]} == {
        "broken": "failed",
        "flaky": "ok",
        "needs-flaky": "ok",  # deferred behind flaky's retry, then run
    }
    assert {s["arm"]: s["reason"] for s in status["skipped"]} == {
        "no-install": "install failed",
        "needs-skipped": "needs no-install, which was skipped",
        "needs-broken": "needs broken, which failed",
    }
    events = _events(bench["out"])
    assert not any(e.get("arm") == "needs-broken" and e["event"] == "queue-command-start"
                   for e in events)  # fmt: skip


def test_pick_keeps_order_within_the_budget_and_never_holds_the_card():
    def arm(name, gpu=False, threads=4):
        return Q.ArmSpec(name, 10, "never", gpu, ("x",), threads=threads)

    arms = (arm("big", threads=8), arm("a"), arm("b"), arm("card", gpu=True), arm("c"))
    assert Q.pick(arms, [0, 1, 2, 3, 4], True, [], 14) == [0, 1, 3]  # b waits; c behind it
    assert Q.pick(arms, [1, 2, 3], False, [8], 14) == [1]
    assert Q.pick(arms, [0, 1], True, [], None) == [0]  # no budget: one CPU arm at a time
    assert Q.pick(arms, [0], True, [], 4) == [0]  # larger than the budget, alone
    assert Q.cpu_budget("auto", 16) == 14 and Q.cpu_budget(None, 64) is None


def test_the_plan_puts_cpu_arms_beside_the_card(bench):
    arms = [
        {**_cpu_arm(bench, "g1"), "time_box_min": 30},
        {**_cpu_arm(bench, "lines"), "gpu": False, "time_box_min": 60, "threads": 4},
        {**_cpu_arm(bench, "other"), "gpu": False, "time_box_min": 60, "threads": 4},
        {**_cpu_arm(bench, "reader"), "time_box_min": 10, "after": ["lines"]},
    ]
    manifest = _manifest(bench, arms, cpu_threads="auto")
    assert Q.plan(manifest, 8) == {"gpu_end": 70, "cpu_end": 60, "gpu_wait": 30, "end": 70}
    assert Q.plan(manifest, None)["cpu_end"] == 120


def test_pages_done_counts_only_the_queue_pages_without_errors(bench):
    queue = Q.Queue(_manifest(bench, [_cpu_arm(bench, "a")]), environ=bench["env"])
    queue.page_list = sorted(bench["pages"].glob("*.tif"))
    folder = bench["out"] / "a"
    folder.mkdir(parents=True)
    (folder / "p000.json").write_text(json.dumps({"error": None}))
    (folder / "p001.json").write_text(json.dumps({"error": "timeout"}))
    (folder / "stray-old-page.json").write_text(json.dumps({"error": None}))
    (folder / "run.json").write_text(json.dumps({"error": None}))
    queue.lanes["gpu"] = {"arm": "a", "index": 0, "phase": "run", "t0": 0.0, "wall0": 0.0}
    snap = queue.snapshot()
    assert (snap["pages_done"], snap["pages_total"]) == (1, 3)
    queue.lanes["gpu"]["phase"] = "smoke"
    snap = queue.snapshot()
    assert (snap["pages_done"], snap["pages_total"]) == (1, 2)


def test_skip_reason_rules():
    def arm(cut, box=20):
        return Q.ArmSpec("a", box, cut, True, ("x",), ("i",))

    assert Q.skip_reason(arm("never"), 500, 30) is None
    assert Q.skip_reason(arm("overrun"), 21, 30) and not Q.skip_reason(arm("overrun"), 19, 30)
    assert Q.skip_reason(arm("behind-schedule"), 31, 30)
    assert not Q.skip_reason(arm("behind-schedule"), 29, 30)
    assert Q.skip_reason(arm("install-failed"), 500, 30) is None


def test_hard_stop_ends_the_arm_skips_the_rest_and_syncs(bench):
    clock = FakeClock()
    arms = [
        _cpu_arm(bench, "long", "--sleep", "30"),
        _cpu_arm(bench, "after"),
    ]
    notifier = FakeNotifier(lambda m: "long started" in m and setattr(clock, "t", clock.t + 3600))
    queue = Q.Queue(
        _manifest(bench, arms, hard_stop_min=30, end_pod="none"),
        clock=clock,
        notifier=notifier,
        environ=bench["env"],
        poll_seconds=0.05,
    )
    started = time.monotonic()
    assert queue.run() == 0
    assert time.monotonic() - started < 25
    status = json.loads((bench["out"] / "status.json").read_text())
    assert status["finished_arms"][0]["status"] == "hard-stopped"
    assert status["skipped"] == [{"arm": "after", "reason": "hard stop reached"}]
    assert (bench["home"] / "DONE.json").is_file()


def _queue_with_cache(bench, notifier, runner, **extra):
    queue = Q.Queue(
        _manifest(bench, [_cpu_arm(bench, "a")], **extra),
        notifier=notifier,
        runner=runner,
        environ=bench["env"],
    )
    (bench["out"] / "a").mkdir(parents=True)
    for index in range(3):
        (bench["out"] / "a" / f"p{index:03d}.json").write_text(json.dumps({"error": None}))
    return queue


def test_own_disk_refuses_to_end_the_pod_when_a_digest_differs(bench, monkeypatch):
    real_copy = Q.copy_tree

    def corrupting_copy(source, target, run=subprocess.run):
        method = real_copy(source, target, run)
        (target / "a" / "p001.json").write_text("changed in transit")
        return method

    monkeypatch.setattr(Q, "copy_tree", corrupting_copy)
    _fresh_heartbeat(bench)
    notifier, deletes = FakeNotifier(), []
    queue = _queue_with_cache(bench, notifier, lambda a: deletes.append(a) or 0, own_disk=True)
    assert queue.finish() == 1
    done = json.loads((bench["out"] / "DONE.json").read_text())
    assert not done["verified"] and done["mismatched"] == ["a/p001.json"]
    assert not (bench["guard"] / "deadline-testpod").exists() and deletes == []
    status = json.loads((bench["out"] / "status.json").read_text())
    assert status["state"] == "failed" and status["end_action"].startswith("NOT ended")
    kinds = [k for k, _ in notifier.sent]
    # The pod kept on its own disk is the one thing the queue cannot settle by itself.
    assert kinds.count("decision") == 1 and kinds[-1] == "queue-done"


def test_without_a_live_guard_pod_delete_is_called(bench):
    beat = bench["guard"] / "heartbeat-testpod"
    beat.write_text("")
    os.utime(beat, (time.time() - 900, time.time() - 900))
    calls = []
    queue = _queue_with_cache(bench, FakeNotifier(), lambda argv: calls.append(argv) or 0)
    assert queue.finish() == 0
    assert calls == [["sh", str(POD_DELETE), "testpod"]]
    assert not (bench["guard"] / "deadline-testpod").exists()
    assert json.loads((bench["out"] / "status.json").read_text())["state"] == "done"


def test_a_failed_pod_delete_is_reported(bench):
    notifier = FakeNotifier()
    queue = _queue_with_cache(bench, notifier, lambda argv: 1)
    assert queue.finish() == 1
    status = json.loads((bench["out"] / "status.json").read_text())
    assert status["state"] == "failed" and status["end_action"] == "pod_delete.sh exit 1"
    assert notifier.sent[-1][0] == "decision"


def test_release_never_moves_an_earlier_deadline_later(tmp_path):
    (tmp_path / "deadline-pod1").write_text("100\n")
    assert Q.release_guard(tmp_path, "pod1", "queue q ended done", 5000) == 100
    assert (tmp_path / "deadline-pod1").read_text() == "100\n"
    assert Q.release_guard(tmp_path, "pod2", "queue q ended done", 5000.7) == 5000
    assert (tmp_path / "deadline-pod2").read_text() == "5000\n"


def test_keep_pod_and_manual_end_pod_keep_the_record(bench):
    queue = _queue_with_cache(bench, FakeNotifier(), lambda argv: 0, end_pod="none")
    assert queue.finish() == 0
    assert not list(bench["guard"].glob("deadline-*"))


def test_sigterm_stops_the_arm_and_exits_143(bench):
    pid_file = bench["tmp"] / "arm.pid"
    arm = _cpu_arm(bench, "sleeper", "--sleep", "120", "--pid-file", str(pid_file))
    manifest = bench["tmp"] / "q.toml"
    manifest.write_text(
        "\n".join(
            [
                f'schema = "{Q.QUEUE_SCHEMA}"',
                'name = "term-test"',
                f"pages = {json.dumps(str(bench['pages']))}",
                f"out = {json.dumps(str(bench['out']))}",
                f"sync_to = {json.dumps(str(bench['home']))}",
                "[[arms]]",
                'name = "sleeper"',
                "time_box_min = 5",
                f"command = {json.dumps(arm['command'])}",
            ]
        )
    )
    env = {k: v for k, v in bench["env"].items() if k != "RUNPOD_POD_ID"}
    runner = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "operations.bakeoff.queue_runner",
            "run",
            "--manifest",
            str(manifest),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert pid_file.exists()
        child = int(pid_file.read_text())
        runner.send_signal(signal.SIGTERM)
        assert runner.wait(timeout=60) == 143
    finally:
        if runner.poll() is None:
            runner.kill()
    status = json.loads((bench["out"] / "status.json").read_text())
    assert status["state"] == "failed" and "stopped by SIGTERM" in status["errors"]
    assert not (bench["home"] / "DONE.json").exists()
    assert _gone(child)


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
        except OSError:
            return True
        if state == "Z":
            return True
        time.sleep(0.1)
    return False


# --- the manifest -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema": "other"}, "schema"),
        ({"cut": "sometimes"}, "cut must be one of"),
        ({"command": ["python", "x.py", "--limit", "3"]}, "--limit"),
        ({"command": ["python", "x.py", "--out", "/elsewhere"]}, "not the queue's out"),
        ({"cut": "install-failed"}, "needs an install"),
        ({"time_box": 3}, "unknown keys"),
        ({"command": ["python", "x.py", "--pages", "/other-pages"]}, "not the queue's pages"),
        (
            {
                "command": [
                    "python",
                    "-m",
                    "operations.bakeoff.witness_run",
                    "run",
                    "--model",
                    "dai",
                ]
            },
            "add --label a",
        ),
        (
            {"command": ["python", "-m", "operations.bakeoff.witness_run", "run-all"]},
            "run-all",
        ),
        ({"sync_to": "OUT/home"}, "neither inside the other"),
        ({"after": ["later"]}, "not earlier arms"),
        ({"writes": "../elsewhere"}, "folder inside out"),
        ({"threads": 0}, "whole number"),
        (
            {"prepare": ["python", "-m", "operations.bakeoff.weights", "fetch", "no-such-model"]},
            "unknown weights",
        ),
    ],
)
def test_manifest_refusals(bench, change, message):
    arm = _cpu_arm(bench, "a")
    data = {
        "schema": Q.QUEUE_SCHEMA,
        "name": "q",
        "pages": "/workspace/private/bakeoff-pages",
        "out": str(bench["out"]),
        "sync_to": "/workspace/private/home",
        "arms": [arm],
    }
    data["pages"] = arm["command"][arm["command"].index("--pages") + 1]
    if "sync_to" in change:
        data["sync_to"] = change["sync_to"].replace("OUT", data["out"])
    elif "schema" in change:
        data.update(change)
    else:
        arm.update(change)
    with pytest.raises(Q.ManifestError, match=message):
        Q.parse_manifest(data)


def test_duplicate_arm_names_are_refused(bench):
    with pytest.raises(Q.ManifestError, match="share a name"):
        _manifest(bench, [_cpu_arm(bench, "a"), _cpu_arm(bench, "a")])


@pytest.mark.parametrize("name", ["example-witness-24gb.toml", "example-reader-96gb.toml"])
def test_example_manifests_validate(name, capsys):
    assert Q.main(["validate", "--manifest", str(EXAMPLES / name)]) == 0
    assert Q.main(["run", "--manifest", str(EXAMPLES / name), "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "--limit 2" in printed and "end pod: delete" in printed


# --- the Mac side -------------------------------------------------------------------


def _fake_ssh(tmp_path: Path) -> str:
    """An `ssh` that runs the remote command locally."""
    ssh = tmp_path / "fake-ssh"
    ssh.write_text('#!/bin/sh\nfor last; do :; done\nexec sh -c "$last"\n')
    ssh.chmod(0o755)
    return f"{ssh} -p 2222 root@203.0.113.9"


def _status(**fields) -> dict:
    base = {
        "queue": "q",
        "state": "running",
        "arm": "chandra",
        "arm_index": 0,
        "arms_total": 3,
        "phase": "smoke",
        "pages_done": 0,
        "pages_total": 2,
        "eta_min": None,
        "behind_min": 0,
        "errors": [],
        "updated": Q.iso(time.time()),
    }
    return {**base, **fields}


def test_watch_prints_changes_only_warns_when_stale_and_exits_on_done(tmp_path):
    status_path = tmp_path / "cache" / "status.json"
    status_path.parent.mkdir()
    # The pod's clock is an hour off; only this machine's clock decides staleness.
    stuck = Q.iso(time.time() - 3600)
    steps = [
        _status(),
        _status(),
        _status(phase="run", pages_done=1, pages_total=3, eta_min=4.2, updated=stuck),
        _status(phase="run", pages_done=1, pages_total=3, eta_min=4.2, updated=stuck),
        _status(phase="run", pages_done=1, pages_total=3, eta_min=4.2, updated=stuck),
        _status(state="done", phase="end-pod", arm=None, arm_index=None),
    ]
    status_path.write_text(json.dumps(steps[0]))
    position, clock = [0], [1000.0]

    def next_step(_seconds):
        position[0] += 1
        clock[0] += 400  # the third and fourth reads leave `updated` unchanged for 800 s
        status_path.write_text(json.dumps(steps[position[0]]))
        if position[0] == len(steps) - 1:
            (status_path.parent / "DONE.json").write_text(
                json.dumps({"verified": True, "files": 9})
            )

    lines: list[str] = []
    code = Q.watch_ssh(
        _fake_ssh(tmp_path),
        str(status_path),
        30,
        sleep=next_step,
        say=lines.append,
        now=lambda: clock[0],
    )
    assert code == 0
    body = [line.split(" ", 1)[1] for line in lines]
    assert body[0].startswith("q running, arm chandra (1/3), smoke, 0/2 pages, eta -")
    assert "run, 1/3 pages, eta 4 min" in body[1]
    assert sum(line.startswith("WARNING") for line in lines) == 1
    assert lines[-1].startswith("queue done: copy verified True, 9 files")
    assert len(lines) == 5


def test_watch_warns_once_when_the_pod_stops_answering(tmp_path):
    clock, lines, reads = [0.0], [], [0]

    def dead_ssh(argv, **_):
        reads[0] += 1
        if reads[0] > 30:
            raise KeyboardInterrupt  # the lead's ctrl-C; the test's way out
        return subprocess.CompletedProcess(argv, 255, "", "Connection refused")

    def sleep(seconds):
        clock[0] += seconds

    with pytest.raises(KeyboardInterrupt):
        Q.watch_ssh(
            "ssh -p 1 root@h",
            "/x/status.json",
            60,
            run=dead_ssh,
            sleep=sleep,
            now=lambda: clock[0],
            say=lines.append,
        )
    warnings = [line for line in lines if line.startswith("WARNING")]
    assert len(warnings) == 1 and "not readable for 11 min" in warnings[0]


def test_watch_exits_1_when_the_queue_failed(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_status(state="failed", errors=["stopped by SIGTERM"])))
    lines: list[str] = []
    assert Q.watch_ssh(_fake_ssh(tmp_path), str(status_path), 1, say=lines.append) == 1
    assert "stopped by SIGTERM" in lines[-1]


def test_watch_ntfy_follows_the_queue_and_never_prints_the_topic():
    topic = "secret-topic-for-test"
    stream = [
        {"event": "open"},
        {"event": "message", "id": "1", "title": "Milestone", "message": "other: unrelated"},
        {"event": "message", "id": "2", "title": "Milestone", "message": "q: chandra started"},
        {"event": "message", "id": "3", "title": Q.DONE_TITLE, "message": "q: queue finished"},
    ]
    urls = []

    class Stream:
        def __enter__(self):
            return [json.dumps(item).encode() + b"\n" for item in stream]

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        urls.append(request.full_url)
        return Stream()

    lines: list[str] = []
    assert Q.watch_ntfy("q", topic=topic, opener=opener, say=lines.append, reconnects=0) == 0
    assert lines == ["Milestone: q: chandra started", f"{Q.DONE_TITLE}: q: queue finished"]
    assert topic in urls[0] and not any(topic in line for line in lines)


def _fake_copy(argv, check=False):
    """rsync or scp from a 'host:path' source, done as a local copy."""
    source = argv[-2].split(":", 1)[1].rstrip("/")
    target = Path(argv[-1])
    if target.exists():
        subprocess.run(["cp", "-R", f"{source}/.", str(target)], check=True)
    else:
        subprocess.run(["cp", "-R", source, str(target)], check=True)
    return subprocess.CompletedProcess(argv, 0)


def test_fetch_copies_and_verifies_every_digest(bench):
    remote = bench["tmp"] / "remote"
    (remote / "a").mkdir(parents=True)
    for index in range(3):
        (remote / "a" / f"p{index:03d}.json").write_text(f'{{"n": {index}}}')
    digests = Q.tree_digests(remote)
    (remote / "DONE.json").write_text(json.dumps({"verified": True, "digests": digests}))
    into = bench["tmp"] / "home-copy"
    lines: list[str] = []
    ssh = "ssh -p 2222 -i key root@203.0.113.9"
    assert Q.fetch(ssh, str(remote), into, run=_fake_copy, say=lines.append) == 0
    assert lines[-1] == "verified 3 of 3 files against DONE.json"

    (into / "a" / "p002.json").write_text("tampered")
    lines.clear()
    assert Q.verify_fetched(into, say=lines.append) == 1
    assert (
        "MISMATCH a/p002.json" in lines and lines[-1] == "verified 2 of 3 files against DONE.json"
    )


def test_split_ssh_finds_the_host_after_valued_options():
    assert Q.split_ssh("ssh -p 2222 -i ~/.ssh/k -o X=1 root@h") == (
        ["ssh", "-p", "2222", "-i", "~/.ssh/k", "-o", "X=1"],
        "root@h",
    )


# --- pod_delete.sh ------------------------------------------------------------------


@pytest.fixture
def delete_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls, curl_argv, curl_configs = (tmp_path / n for n in ("calls", "curl-argv", "curl-config"))
    stubs = {
        "runpodctl": (
            f'printf "%s\\n" "$*" >> "{calls}"\n'
            '[ "$*" = "${FAKE_RUNPODCTL_OK:-}" ] && exit 0\nexit 1\n'
        ),
        "curl": (
            'previous=""\n'
            'for argument in "$@"; do\n'
            f'  [ "$previous" = -K ] && cat "$argument" >> "{curl_configs}"\n'
            '  previous="$argument"\n'
            "done\n"
            f'printf "%s\\n" "$*" >> "{curl_argv}"\n'
            '[ "${FAKE_CURL_FAIL:-}" = yes ] && exit 22\nexit 0\n'
        ),
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\n" + body)
        stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUNPOD_API_KEY": "test-key-not-real",
        "POD_DELETE_INTERVAL": "0",
    }
    return env, calls, curl_argv, curl_configs


def _delete(env, pod="testpod"):
    return subprocess.run(["sh", str(POD_DELETE), pod], env=env, capture_output=True, text=True)


def _lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def test_pod_delete_stops_at_the_first_delete_that_works(delete_env):
    env, calls, curl_argv, _ = delete_env
    assert _delete({**env, "FAKE_RUNPODCTL_OK": "pod delete testpod"}).returncode == 0
    assert _lines(calls) == ["pod delete testpod"] and not curl_argv.exists()


def test_pod_delete_uses_the_api_with_the_key_in_a_config_file(delete_env):
    env, calls, curl_argv, curl_configs = delete_env
    result = _delete(env)
    assert result.returncode == 0
    assert _lines(calls) == ["pod delete testpod", "remove pod testpod"]
    assert "https://api.runpod.io/v2/pods/testpod" in curl_argv.read_text()
    assert "test-key-not-real" not in curl_argv.read_text()
    assert 'header = "Authorization: Bearer test-key-not-real"' in curl_configs.read_text()
    assert "test-key-not-real" not in result.stdout + result.stderr


def test_pod_delete_tries_three_times_then_stops(delete_env):
    env, calls, curl_argv, _ = delete_env
    result = _delete({**env, "FAKE_CURL_FAIL": "yes", "FAKE_RUNPODCTL_OK": "pod stop testpod"})
    assert result.returncode == 3
    assert _lines(calls) == ["pod delete testpod", "remove pod testpod"] * 3 + ["pod stop testpod"]
    assert len(_lines(curl_argv)) == 3


def test_pod_delete_reports_when_nothing_works(delete_env):
    env, calls, _, _ = delete_env
    assert _delete({**env, "FAKE_CURL_FAIL": "yes"}).returncode == 1
    assert _lines(calls)[-2:] == ["pod stop testpod", "stop pod testpod"]


def test_pod_delete_refuses_a_bad_pod_id(delete_env):
    env, calls, _, _ = delete_env
    assert _delete(env, "pod;rm").returncode == 2 and not calls.exists()


# --- the global volume route: copy to object storage, one manifest for both ----------


def _tree(root: Path) -> None:
    (root / "a").mkdir(parents=True)
    (root / "a" / "p000.json").write_text('{"error": null}')
    (root / "top.txt").write_text("x")


def test_copy_falls_back_to_in_place_rsync_when_times_are_refused(tmp_path):
    """An object-storage mount refuses rsync's times and renames; the next form copies."""
    source, target = tmp_path / "src", tmp_path / "dst"
    _tree(source)
    tried = []

    def run(argv, check):
        tried.append(argv[1:3])
        if argv[1] == "-rt":
            raise subprocess.CalledProcessError(23, argv)
        return subprocess.run(argv, check=check)

    assert Q.copy_tree(source, target, run) == "rsync -r --inplace"
    assert tried == [["-rt", f"{source}/"], ["-r", "--inplace"]]
    assert Q.compare_digests(Q.tree_digests(source), target) == []


def test_copy_falls_back_to_python_when_every_rsync_form_fails(tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    _tree(source)

    def run(argv, check):
        raise subprocess.CalledProcessError(23, argv)

    assert Q.copy_tree(source, target, run) == "python copy"
    assert Q.compare_digests(Q.tree_digests(source), target) == []


def test_done_json_is_written_directly_where_a_rename_is_refused(tmp_path, monkeypatch):
    def no_rename(src, dst):
        raise OSError("rename not supported")

    monkeypatch.setattr(Q.os, "replace", no_rename)
    Q.write_json_anywhere(tmp_path / "DONE.json", {"verified": True})
    assert json.loads((tmp_path / "DONE.json").read_text())["verified"] is True


def test_the_command_line_moves_the_copy_to_the_global_mount(bench):
    manifest = _manifest(bench, [_cpu_arm(bench, "a")])

    moved = Q.override_manifest(manifest, "/workspace/global/bakeoff/home", True)

    assert moved.sync_to == Path("/workspace/global/bakeoff/home") and moved.own_disk is True
    assert Q.override_manifest(manifest, None, False) == manifest
    with pytest.raises(Q.ManifestError, match="absolute"):
        Q.override_manifest(manifest, "relative/home", False)
    with pytest.raises(Q.ManifestError, match="separate"):
        Q.override_manifest(manifest, str(bench["out"] / "inside"), False)


@pytest.mark.parametrize(
    "name", ["witness-24gb.toml", "reader-96gb.toml", "perlector-fp8-96gb.toml"]
)
def test_the_bakeoff_manifests_validate_on_the_global_route(name, capsys):
    argv = [
        "--manifest",
        str(EXAMPLES / name),
        "--sync-to",
        "/workspace/global/bakeoff/home",
        "--own-disk",
    ]
    assert Q.main(["validate", *argv]) == 0
    assert Q.main(["run", *argv, "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "-> /workspace/global/bakeoff/home" in printed and "own disk" in printed


def test_keep_pod_makes_the_run_end_with_the_pod_kept(bench, capsys):
    manifest = _manifest(bench, [_cpu_arm(bench, "a")])
    assert Q.override_manifest(manifest, None, False, keep_pod=True).end_pod == "none"
    argv = ["--manifest", str(EXAMPLES / "witness-24gb.toml"), "--keep-pod", "--own-disk"]
    assert Q.main(["run", *argv, "--dry-run"]) == 0
    assert "end pod: none" in capsys.readouterr().out


def _vendor_arm(bench, label, prompt_text):
    prompt = bench["tmp"] / f"{label}-prompt.txt"
    prompt.write_text(prompt_text)
    arm = _witness_arm(bench, "qwen-vendor", label)
    arm["command"] += ["--repo", "Qwen/Qwen3.8-27B", "--prompt-file", str(prompt)]
    return arm


def test_a_smoke_whose_every_page_looped_does_not_start_the_full_run(bench):
    """C7: terminal failures are kept and never resent, but a smoke with no answer at
    all is a failed smoke: the full run would repeat it at the same settings."""
    notifier = FakeNotifier()
    queue = Recording(
        _manifest(bench, [_vendor_arm(bench, "qv", "LOOP: transcribe")], end_pod="none"),
        notifier=notifier,
        environ=bench["env"],
        poll_seconds=0.05,
    )
    queue.run()  # the queue ends; the arm is what is checked
    status = json.loads((bench["out"] / "status.json").read_text())
    (arm,) = status["finished_arms"]
    assert arm["status"] == "failed" and arm["pages"] == 0
    assert arm["failed_pages"] == [
        {"page": f"p{i:03d}", "reasons": ["repetition-loop"]} for i in range(2)
    ]
    phases = [(s["arm"], s["phase"]) for s in queue.seen]
    assert ("qv", "run") not in phases and ("qv", "retry") not in phases
    assert not (bench["out"] / "qv" / "p002.json").exists()  # never sent
    assert any("smoke gave no answer" in e for e in status["errors"])


def test_smoke_verdict_needs_one_answer_and_keeps_terminal_pages(tmp_path):
    folder = tmp_path / "out" / "a"
    folder.mkdir(parents=True)
    pages = [tmp_path / "p0.tif", tmp_path / "p1.tif"]
    terminal = {"error": "loop", "failure": {"terminal": True, "reasons": ["repetition-loop"]}}

    class Q_:
        m = type("M", (), {"out": tmp_path / "out"})()
        _smoke_verdict = Q.Queue._smoke_verdict

    arm = type("Arm", (), {"records": "a"})()
    (folder / "p0.json").write_text(json.dumps(terminal))
    assert Q_()._smoke_verdict(arm, pages) == "failed"  # p1 never written
    (folder / "p1.json").write_text(json.dumps(terminal))
    assert Q_()._smoke_verdict(arm, pages) == "useless"
    (folder / "p1.json").write_text(json.dumps({"error": None}))
    assert Q_()._smoke_verdict(arm, pages) == "ok"  # one looping page does not stop the arm
    (folder / "p1.json").write_text(json.dumps({"error": "HTTP 500"}))
    assert Q_()._smoke_verdict(arm, pages) == "failed"


def test_cpu_retries_stay_within_the_thread_budget_together(bench):
    """C7: two 10-thread CPU arms retried under a 12-thread budget run one after the other,
    not both at 10 threads at once."""
    tmp = bench["tmp"]
    arms = [
        _cpu_arm(bench, name, "--sleep", "0.5", "--fail-once", str(tmp / f"{name}-once"),
                 "--threads", "10", gpu=False)
        for name in ("c1", "c2")
    ]  # fmt: skip
    queue = Recording(
        _manifest(bench, arms, end_pod="none", cpu_threads=12),
        notifier=FakeNotifier(),
        environ=bench["env"],
        poll_seconds=0.05,
    )
    assert queue.run() == 0
    assert all(len(s["cpu_arms"]) <= 1 for s in queue.seen)
    events = _events(bench["out"])
    retries = [e for e in events if e["event"] == "queue-arm-retry"]
    assert sorted(e["arm"] for e in retries) == ["c1", "c2"]
    assert all(e["threads"] == 10 for e in retries)
    ends = {e["arm"]: e["t"] for e in events if e["event"] == "queue-arm-end"}
    starts = {
        e["arm"]: e["t"]
        for e in events
        if e["event"] == "queue-command-start" and e["phase"] == "retry"
    }
    first, second = sorted(starts, key=starts.get)
    assert starts[second] >= ends[first]


def test_cpu_retries_run_in_the_cpu_lane_with_its_whole_thread_share(bench):
    tmp = bench["tmp"]
    arms = [
        _cpu_arm(bench, "g", "--sleep", "1", "--fail-once", str(tmp / "g-once")),
        *(
            _cpu_arm(bench, name, "--fail-once", str(tmp / f"{name}-once"), "--threads", "2",
                     gpu=False)
            for name in ("c1", "c2")
        ),
    ]  # fmt: skip
    queue = Recording(
        _manifest(bench, arms, end_pod="none", cpu_threads=12),
        notifier=FakeNotifier(),
        environ=bench["env"],
        poll_seconds=0.05,
    )
    assert queue.run() == 0
    status = json.loads((bench["out"] / "status.json").read_text())
    assert {a["label"]: a["status"] for a in status["finished_arms"]} == dict.fromkeys(
        ("g", "c1", "c2"), "ok"
    )
    events = _events(bench["out"])
    retries = {e["arm"]: e for e in events if e["event"] == "queue-arm-retry"}
    assert retries["g"]["lane"] == "gpu" and retries["g"]["threads"] is None
    for name in ("c1", "c2"):
        assert retries[name]["lane"] == f"cpu:{name}" and retries[name]["threads"] == 6
        argv = next(
            e["argv"]
            for e in events
            if e["event"] == "queue-command-start" and e["arm"] == name and e["phase"] == "retry"
        )
        assert argv[argv.index("--threads") + 1] == "6"
    # The CPU retries ran beside the card's retry, not after it.
    g_end = next(e["t"] for e in events if e["event"] == "queue-arm-end" and e["arm"] == "g")
    c_starts = [
        e["t"]
        for e in events
        if e["event"] == "queue-command-start" and e["phase"] == "retry" and e["arm"] != "g"
    ]
    assert c_starts and max(c_starts) < g_end
    assert any(
        {c["arm"] for c in s["cpu_arms"]} == {"c1", "c2"} and s["phase"] == "retry"
        for s in queue.seen
    )


def test_with_threads_sets_only_the_threads_value():
    assert Q.with_threads(("x", "--threads", "4", "--y"), 9) == ("x", "--threads", "9", "--y")
    assert Q.with_threads(("x", "--threads=4"), 9) == ("x", "--threads=9")
    assert Q.with_threads(("x",), 9) == ("x",)
    assert Q.with_threads(("x", "--threads", "4"), None) == ("x", "--threads", "4")


def test_each_witness_arm_loads_its_model_once_for_smoke_and_run(bench):
    """The smoke's server is kept and the full run takes it over: one launch per arm,
    where smoke and run each used to start their own."""
    launches = bench["tmp"] / "launches.txt"
    env = {**bench["env"], "FAKE_VLLM_LAUNCHES": str(launches)}
    arms = [_witness_arm(bench, "chandra", "chandra"), _witness_arm(bench, "churro", "churro")]
    queue = Q.Queue(
        _manifest(bench, arms, end_pod="none"),
        notifier=FakeNotifier(),
        environ=env,
        poll_seconds=0.05,
    )
    assert queue.run() == 0
    status = json.loads((bench["out"] / "status.json").read_text())
    assert {a["label"]: a["status"] for a in status["finished_arms"]} == {
        "chandra": "ok",
        "churro": "ok",
    }
    pids = launches.read_text().split()
    assert len(pids) == 2  # was 4: a smoke server and a run server per arm
    events = _events(bench["out"])
    for name in ("chandra", "churro"):
        mine = [e["event"] for e in events if e.get("model") == name]
        assert mine.count("server-start") == 1 and mine.count("server-kept") == 1
        assert mine.count("server-adopted") == 1 and mine.count("server-stopped") == 1
        assert not (bench["out"] / name / "server-handoff.json").exists()
    for pid in map(int, pids):  # every server is gone at the end
        deadline = time.monotonic() + 10
        while W._pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not W._pid_alive(pid)


def test_a_kept_server_no_run_takes_over_is_stopped(bench):
    """A smoke that passes its server on, then a run that never adopts it (its smoke page
    failed here): the queue stops the server itself."""
    launches = bench["tmp"] / "launches.txt"
    env = {**bench["env"], "FAKE_VLLM_LAUNCHES": str(launches)}
    arm = _witness_arm(bench, "chandra", "chandra")
    queue = Q.Queue(
        _manifest(bench, [arm], end_pod="none"),
        notifier=FakeNotifier(),
        environ=env,
        poll_seconds=0.05,
    )
    queue._smoke_verdict = lambda a, pages: "failed"
    assert queue.run() == 0
    events = _events(bench["out"])
    # The retry's smoke finds its pages cached and starts nothing, so nothing else is kept.
    assert [e["event"] for e in events].count("queue-server-released") == 1
    pids = launches.read_text().split()
    assert len(pids) == 1
    for pid in map(int, pids):
        deadline = time.monotonic() + 10
        while W._pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not W._pid_alive(pid)


def test_witness_commands_may_not_carry_the_server_hand_off(bench):
    arm = _witness_arm(bench, "chandra", "chandra")
    arm["command"] += ["--keep-server", "/tmp/x"]
    with pytest.raises(Q.ManifestError, match="--keep-server"):
        _manifest(bench, [arm])


def test_own_disk_with_a_kept_pod_leaves_the_copy_check_to_fetch(bench, monkeypatch):
    def no_read_back(*_args):
        raise AssertionError("the copy on the volume was read back on the pod")

    monkeypatch.setattr(Q, "compare_digests", no_read_back)
    notifier = FakeNotifier()
    queue = _queue_with_cache(bench, notifier, lambda argv: 0, own_disk=True, end_pod="none")
    assert queue.finish() == 0
    done = json.loads((bench["out"] / "DONE.json").read_text())
    assert done["verified"] is None and done["verify"] == "at home, by fetch"
    assert done["files"] == 3 and "a/p000.json" in done["digests"]
    assert any("copy made: 3 files; verify at home" in m for _, m in notifier.sent)
    assert "decision" not in [k for k, _ in notifier.sent]
    status = json.loads((bench["out"] / "status.json").read_text())
    assert status["state"] == "done" and status["end_action"] == "kept (end_pod = none)"

    # At home, fetch checks every file against those digests (here: the cache itself).
    monkeypatch.undo()
    lines: list[str] = []
    assert Q.verify_fetched(bench["out"], say=lines.append) == 0
    assert lines == ["verified 3 of 3 files against DONE.json"]


def test_own_disk_without_keep_pod_still_checks_the_copy_on_the_pod(bench):
    queue = _queue_with_cache(bench, FakeNotifier(), lambda argv: 0, own_disk=True)
    queue.sync()
    done = json.loads((bench["out"] / "DONE.json").read_text())
    assert done["verified"] is True and done["verify"] == "on the pod"


def test_a_terminal_page_is_resent_when_its_timeout_or_concurrency_changes(bench):
    """C7: "the same settings" include the request timeout, concurrency and the server's
    batch settings, so a page that failed under them is sent again once they change."""
    argv = _vendor_arm(bench, "qv", "LOOP: transcribe")["command"][3:]
    argv = [*argv, "--limit", "1"]
    assert W.main(argv) == 0
    record = json.loads((bench["out"] / "qv" / "p000.json").read_text())
    assert record["failure"]["settings"]["request_timeout"] == 1800
    assert W.main(argv) == 0  # the same settings: not resent
    assert W.main([*argv, "--request-timeout", "900"]) == 0  # another timeout: resent
    assert W.main([*argv, "--request-timeout", "900", "--concurrency", "1"]) == 0  # resent
    events = [e["event"] for e in _events(bench["out"])]
    assert events.count("page-not-retried") == 1
    assert events.count("requests-start") == 3


def test_an_arm_whose_every_page_failed_terminally_is_failed_and_blocks_its_dependents(bench):
    """A full run whose pages all ran into a timeout or a loop settles every page but gave
    no answer: it was recorded ok with 0 pages and its dependents ran on nothing."""
    arms = [
        _vendor_arm(bench, "qv", "LOOP: transcribe"),
        _cpu_arm(bench, "child", gpu=False, after=["qv"]),
    ]
    queue = Recording(
        _manifest(bench, arms, end_pod="none"),
        notifier=FakeNotifier(),
        environ=bench["env"],
        poll_seconds=0.05,
    )
    queue._smoke_verdict = lambda arm, pages: "ok"  # as if the smoke pages had answered
    queue.run()
    status = json.loads((bench["out"] / "status.json").read_text())
    qv = next(a for a in status["finished_arms"] if a["label"] == "qv")
    assert qv["status"] == "failed" and qv["pages"] == 0 and len(qv["failed_pages"]) == 3
    assert any("run gave no answer" in e for e in status["errors"])
    assert [s["arm"] for s in status["skipped"]] == ["child"]
    assert queue.outcome["qv"] == "failed"
    assert not [e for e in _events(bench["out"]) if e["event"] == "queue-arm-retry"]


def test_an_arm_with_some_terminal_failures_is_ok_with_failures(tmp_path):
    out = tmp_path / "out"
    (out / "a").mkdir(parents=True)
    terminal = {"error": "loop", "failure": {"terminal": True, "reasons": ["repetition-loop"]}}
    (out / "a" / "p0.json").write_text(json.dumps({"error": None}))
    (out / "a" / "p1.json").write_text(json.dumps(terminal))

    class Q_:
        m = type("M", (), {"out": out})()
        page_list = [tmp_path / "p0.tif", tmp_path / "p1.tif"]
        _run_status = Q.Queue._run_status
        _failed_pages = Q.Queue._failed_pages

    arm = type("Arm", (), {"records": "a"})()
    assert Q_()._run_status(arm) == "ok-with-failures"
    (out / "a" / "p1.json").write_text(json.dumps({"error": None}))
    assert Q_()._run_status(arm) == "ok"
    (out / "a" / "p0.json").write_text(json.dumps(terminal))
    (out / "a" / "p1.json").write_text(json.dumps(terminal))
    assert Q_()._run_status(arm) == "failed"
    # A dependent may build on an arm that answered some pages, not on one that answered none.
    assert "ok-with-failures" in Q.DEPENDENCY_MET and "failed" not in Q.DEPENDENCY_MET
