from __future__ import annotations

import json
from decimal import Decimal

import pytest

import operations.bench.scale as scale
from common.contracts.canonical import canonical_bytes
from operations.bench.scale import cleanup_scale, run_scale


def test_scale_runner_refuses_undersized_without_explicit_smoke_opt_in(tmp_path):
    with pytest.raises(ValueError, match="allow_undersized_smoke=True"):
        run_scale(tmp_path / "scale", shards=1, pages_per_shard=1)


def test_scale_runner_creates_resumes_censuses_and_cleans_up_at_small_cardinality(
    tmp_path, monkeypatch
):
    """Exercise the same create/resume/export/cleanup path as the full-size run,
    at a size this chamber can actually finish, so a latent logic bug is not
    resting on an unverified refusal-only test.
    """
    root = tmp_path / "scale-smoke"
    real_create = scale.RunTree.create
    resumed_run_ids = []

    def observe_resume(cls, tree_root, run_id, **kwargs):
        if (tree_root / run_id / scale.RUN_FILE).is_file():
            resumed_run_ids.append(run_id)
        return real_create(tree_root, run_id, **kwargs)

    monkeypatch.setattr(scale.RunTree, "create", classmethod(observe_resume))

    result = run_scale(root, shards=2, pages_per_shard=3, allow_undersized_smoke=True)

    assert result["schema"] == "r7b-runtree-scale-result.v1"
    assert result["state"] == "smoke-undersized"
    assert result["artifact_count"] == 6
    assert result["disk_bytes"] > 0
    assert result["inodes"] > 0
    assert Decimal(result["create_seconds"]) > 0
    assert Decimal(result["create_seconds"]) + Decimal(result["resume_seconds"]) + Decimal(
        result["manifest_export_seconds"]
    ) <= Decimal(result["wall_seconds"])
    assert resumed_run_ids == ["bench-scale-01", "bench-scale-02"]
    assert (root / "aggregate-census.json").exists()
    census = json.loads((root / "aggregate-census.json").read_bytes())
    assert census["state"] == "smoke-undersized"
    assert census["shards"] == 2
    assert census["pages_per_shard"] == 3
    assert census["artifact_count"] == 6
    persisted_bytes = (root / "scale-result.json").read_bytes()
    persisted_result = json.loads(persisted_bytes)
    assert persisted_result == result
    assert persisted_bytes == canonical_bytes(result)
    assert result["disk_bytes"] == sum(
        path.stat().st_size for path in root.rglob("*") if path.is_file()
    )
    assert result["inodes"] == sum(1 for _ in root.rglob("*"))

    with pytest.raises(FileExistsError, match="scale root already exists"):
        run_scale(root, shards=2, pages_per_shard=3, allow_undersized_smoke=True)

    cleanup_scale(root)
    assert not root.exists()


def test_cleanup_scale_refuses_a_markerless_non_scale_directory(tmp_path):
    root = tmp_path / "ordinary-directory"
    retained = root / "must-survive.txt"
    root.mkdir()
    retained.write_text("not scale output")

    with pytest.raises(FileNotFoundError, match="aggregate-census.json marker"):
        cleanup_scale(root)

    assert retained.read_text() == "not scale output"


def test_scale_runner_refuses_a_dropped_artifact_before_writing_a_census(tmp_path, monkeypatch):
    real_publish = scale.RunTree.publish_artifact
    publish_calls = 0

    def publish_with_one_drop(tree, envelope):
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 3:
            return None
        return real_publish(tree, envelope)

    monkeypatch.setattr(scale.RunTree, "publish_artifact", publish_with_one_drop)
    root = tmp_path / "scale-shortfall"

    with pytest.raises(
        RuntimeError,
        match=r"published 5 artifacts; expected 6.*non-receipted pages: \[\(1, 3\)\]",
    ):
        run_scale(root, shards=2, pages_per_shard=3, allow_undersized_smoke=True)

    assert not (root / "aggregate-census.json").exists()


def test_scale_runner_refuses_resume_if_a_shard_loses_its_run_authority(tmp_path, monkeypatch):
    root = tmp_path / "scale-missing-run-authority"
    real_perf_counter_ns = scale.time.perf_counter_ns
    sabotage_fired = False

    def remove_authority_between_create_and_resume():
        nonlocal sabotage_fired
        authority = root / "bench-scale-01" / "run.json"
        if not sabotage_fired and authority.is_file():
            authority.unlink()
            sabotage_fired = True
        return real_perf_counter_ns()

    monkeypatch.setattr(scale.time, "perf_counter_ns", remove_authority_between_create_and_resume)

    with pytest.raises(
        FileNotFoundError,
        match="scale resume requires existing RunTree authority run.json",
    ):
        run_scale(root, shards=2, pages_per_shard=3, allow_undersized_smoke=True)

    assert sabotage_fired, "the test must remove the run authority before proving its refusal"
    assert not (root / "aggregate-census.json").exists()
