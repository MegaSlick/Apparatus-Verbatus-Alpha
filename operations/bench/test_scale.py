from __future__ import annotations

import pytest

import operations.bench.scale as scale
from operations.bench.scale import cleanup_scale, run_scale


def test_scale_runner_refuses_a_smaller_substitute(tmp_path):
    try:
        run_scale(tmp_path / "scale", shards=1, pages_per_shard=1)
    except ValueError as error:
        assert "fixed at 10 shards" in str(error)
    else:
        raise AssertionError("scale runner accepted a smaller cardinality")


def test_scale_runner_creates_resumes_censuses_and_cleans_up_at_small_cardinality(
    tmp_path, monkeypatch
):
    """The 10x1,000 host invocation timed out mid-run with no prior smoke test

    under it (TERRA_BUILD_REPORT.md). This exercises the same create/resume/
    export/cleanup path the host command drives, at a size this chamber can
    actually finish, so a latent bug in the logic itself — as opposed to its
    wall-clock cost — is not resting on an unverified refusal-only test.
    """
    monkeypatch.setattr(scale, "SHARDS", 2)
    monkeypatch.setattr(scale, "PAGES_PER_SHARD", 3)
    root = tmp_path / "scale-smoke"

    result = run_scale(root, shards=2, pages_per_shard=3)

    assert result["schema"] == "r7b-runtree-scale-result.v1"
    assert result["state"] == "measured"
    assert result["artifact_count"] == 6
    assert result["disk_bytes"] > 0
    assert result["inodes"] > 0
    for field in ("create_seconds", "resume_seconds", "manifest_export_seconds", "wall_seconds"):
        assert result[field] >= 0
    assert (root / "aggregate-census.json").exists()

    with pytest.raises(FileExistsError, match="scale root already exists"):
        run_scale(root, shards=2, pages_per_shard=3)

    cleanup_scale(root)
    assert not root.exists()
