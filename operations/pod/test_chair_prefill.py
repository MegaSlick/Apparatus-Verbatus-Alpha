"""The background chair-cache fill that overlaps UV_ENVIRONMENT."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from common.chairs.conftest import (
    RecordingFetcher,
    config_of,
    hf_chair,
    pin_snapshot,
    registry_for,
    write_snapshot,
)
from common.chairs.errors import DigestMismatchRefusal, DiskSpaceRefusal

from .chair_prefill import ChairCachePrefill, PrefillChairs, in_stage_need_order

FREE = 10**9


class PerRoleFetcher(RecordingFetcher):
    """Each role pins its own bytes; optionally blocks until released."""

    def __init__(self, files_by_role, *, gate: threading.Event | None = None):
        super().__init__({})
        self.files_by_role = files_by_role
        self.gate = gate

    def fetch(self, identity, destination: Path, paths):
        if self.gate is not None:
            assert self.gate.wait(timeout=10)
        self.files = self.files_by_role[identity.role]
        super().fetch(identity, destination, paths)


def _world(tmp_path: Path, roles, *, gate=None):
    files_by_role = {role: {"weights.bin": f"{role} weights\n".encode()} for role in roles}
    chairs = {}
    for role in roles:
        remote = write_snapshot(tmp_path / "remote" / role, files_by_role[role])
        chairs[role] = hf_chair(role, pin_snapshot(remote, tmp_path / "manifests" / f"{role}.json"))
    fetcher = PerRoleFetcher(files_by_role, gate=gate)
    registry = registry_for(config_of(tmp_path, chairs, witness_floor=0), tmp_path, fetcher)
    return registry, fetcher


def _prefill(registry, roles, *, free=FREE, deferred=()):
    chairs = tuple(registry.resolve(role) for role in roles)
    return ChairCachePrefill(
        lambda: PrefillChairs(registry, chairs, tuple(deferred)),
        free_bytes=lambda _path: free,
        filesystem_key=lambda _path: 1,
    )


def test_chairs_are_ordered_by_the_stage_that_first_needs_them() -> None:
    assert in_stage_need_order(
        [
            "reconstructor",
            "annotator",
            "perlector",
            "attestator_2",
            "secondary_proposer",
            "attestator_1",
            "designator_surya",
        ]
    ) == [
        "designator_surya",
        "secondary_proposer",
        "attestator_1",
        "attestator_2",
        "perlector",
        "reconstructor",
        "annotator",
    ]


def test_the_fill_runs_in_the_background_and_its_record_names_each_verified_copy(
    tmp_path: Path,
) -> None:
    gate = threading.Event()
    registry, fetcher = _world(tmp_path, ("attestator_1", "perlector"), gate=gate)
    prefill = _prefill(registry, ("attestator_1", "perlector"))

    prefill.start("uv-environment", {1: 0})
    assert prefill.registry is None, "a running fill hands over no registry"
    gate.set()
    record = prefill.wait()

    assert fetcher.roles == ["attestator_1", "perlector"]
    assert record["started_at_step"] == "uv-environment"
    assert [item["chair"] for item in record["filled"]] == ["attestator_1", "perlector"]
    for item in record["filled"]:
        assert Path(item["root"]).is_dir()
        assert item["root"].endswith(item["manifest_digest"])
    assert record["deferred"] == []
    assert prefill.registry is registry


def test_a_second_start_and_a_second_wait_do_not_copy_again(tmp_path: Path) -> None:
    registry, fetcher = _world(tmp_path, ("attestator_1",))
    prefill = _prefill(registry, ("attestator_1",))

    prefill.start("uv-environment")
    first = prefill.wait()
    prefill.start("model-store")

    assert prefill.wait() == first
    assert fetcher.roles == ["attestator_1"]


def test_no_fill_was_started_in_this_process_after_a_resume(tmp_path: Path) -> None:
    registry, _ = _world(tmp_path, ("attestator_1",))

    assert _prefill(registry, ("attestator_1",)).wait() is None


def test_a_chair_that_does_not_fit_beside_the_environment_build_is_left_for_preflight(
    tmp_path: Path,
) -> None:
    registry, fetcher = _world(tmp_path, ("attestator_1", "perlector"))
    size = len(b"attestator_1 weights\n")
    # Room for the uv bytes and the first chair only.
    prefill = _prefill(registry, ("attestator_1", "perlector"), free=1000 + size + 1)

    prefill.start("uv-environment", {1: 1000, 2: 10**12})
    record = prefill.wait()

    assert fetcher.roles == ["attestator_1"]
    assert [item["chair"] for item in record["filled"]] == ["attestator_1"]
    (deferred,) = record["deferred"]
    assert deferred["chair"] == "perlector"
    assert "no room beside the environment build" in deferred["reason"]


def test_a_cache_already_present_needs_no_room(tmp_path: Path) -> None:
    registry, fetcher = _world(tmp_path, ("attestator_1",))
    registry.ensure(registry.resolve("attestator_1"))
    prefill = _prefill(registry, ("attestator_1",), free=0)

    prefill.start("uv-environment")
    record = prefill.wait()

    assert fetcher.roles == ["attestator_1"], "the present cache is verified, not copied"
    assert [item["chair"] for item in record["filled"]] == ["attestator_1"]


def test_a_fill_that_finds_no_room_evicts_nothing_and_is_deferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, _ = _world(tmp_path, ("attestator_1",))
    seen: list[bool] = []

    def ensure(identity, *, evict=True):
        seen.append(evict)
        raise DiskSpaceRefusal(identity.role, "container disk too small")

    monkeypatch.setattr(registry, "ensure", ensure)
    prefill = _prefill(registry, ("attestator_1",))

    prefill.start("uv-environment")
    record = prefill.wait()

    assert seen == [False]
    assert record["filled"] == []
    assert record["deferred"] == [
        {"chair": "attestator_1", "reason": "chair 'attestator_1': container disk too small"}
    ]


def test_a_copy_that_refuses_is_raised_by_the_step_that_waits(tmp_path: Path) -> None:
    registry, fetcher = _world(tmp_path, ("attestator_1", "perlector"))
    fetcher.files_by_role["attestator_1"] = {"weights.bin": b"a flipped byte lands here!!\n"}
    prefill = _prefill(registry, ("attestator_1", "perlector"))

    prefill.start("uv-environment")

    with pytest.raises(DigestMismatchRefusal) as caught:
        prefill.wait()
    assert caught.value.chair == "attestator_1"
    assert "weights.bin" in str(caught.value)
    with pytest.raises(DigestMismatchRefusal):
        prefill.wait()


def test_a_prefill_that_cannot_be_planned_defers_everything_and_fails_nothing() -> None:
    def prepare():
        raise RuntimeError("roster unreadable")

    prefill = ChairCachePrefill(prepare)
    prefill.start("uv-environment")
    record = prefill.wait()

    assert record["filled"] == []
    assert record["deferred"] == [
        {"chair": "*", "reason": "the prefill could not be planned: roster unreadable"}
    ]


def test_chairs_the_plan_left_out_are_recorded_as_deferred(tmp_path: Path) -> None:
    registry, fetcher = _world(tmp_path, ("attestator_1",))
    prefill = _prefill(
        registry,
        (),
        deferred=({"chair": "attestator_1", "reason": "store source not ready"},),
    )

    prefill.start("uv-environment")
    record = prefill.wait()

    assert fetcher.roles == []
    assert record["deferred"] == [{"chair": "attestator_1", "reason": "store source not ready"}]
