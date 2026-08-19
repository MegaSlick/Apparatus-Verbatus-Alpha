from __future__ import annotations

import json

import pytest

import operations.bench.scale as scale
from operations.bench.scale import cleanup_scale, run_scale


def test_scale_runner_refuses_undersized_without_explicit_smoke_opt_in(tmp_path, monkeypatch):
    # Rebinding these public globals is the mutation under test: the private seal must ignore it.
    monkeypatch.setattr(scale, "SHARDS", 1)
    monkeypatch.setattr(scale, "PAGES_PER_SHARD", 1)

    with pytest.raises(ValueError, match="allow_undersized_smoke=True"):
        run_scale(tmp_path / "scale", shards=1, pages_per_shard=1)


def test_scale_runner_creates_resumes_censuses_and_cleans_up_at_small_cardinality(
    tmp_path, monkeypatch
):
    """The 10x1,000 host invocation timed out mid-run with no prior smoke test

    under it (TERRA_BUILD_REPORT.md). This exercises the same create/resume/
    export/cleanup path the host command drives, at a size this chamber can
    actually finish, so a latent bug in the logic itself — as opposed to its
    wall-clock cost — is not resting on an unverified refusal-only test.
    """
    # Rebinding these public globals is the mutation under test: the private seal must ignore it.
    monkeypatch.setattr(scale, "SHARDS", 2)
    monkeypatch.setattr(scale, "PAGES_PER_SHARD", 3)
    root = tmp_path / "scale-smoke"

    result = run_scale(root, shards=2, pages_per_shard=3, allow_undersized_smoke=True)

    assert result["schema"] == "r7b-runtree-scale-result.v1"
    assert result["state"] == "smoke-undersized"
    assert result["artifact_count"] == 6
    assert result["disk_bytes"] > 0
    assert result["inodes"] > 0
    assert result["create_seconds"] > 0
    assert (
        result["create_seconds"] + result["resume_seconds"] + result["manifest_export_seconds"]
        <= result["wall_seconds"]
    )
    assert (root / "aggregate-census.json").exists()
    census = json.loads((root / "aggregate-census.json").read_bytes())
    assert census["artifact_count"] == 6

    with pytest.raises(FileExistsError, match="scale root already exists"):
        run_scale(root, shards=2, pages_per_shard=3, allow_undersized_smoke=True)

    cleanup_scale(root)
    assert not root.exists()


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

    with pytest.raises(RuntimeError, match="published 5 artifacts; expected 6"):
        run_scale(root, shards=2, pages_per_shard=3, allow_undersized_smoke=True)

    assert not (root / "aggregate-census.json").exists()
