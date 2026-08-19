from __future__ import annotations

import json
from decimal import Decimal

import pytest

import operations.bench.scale as scale
from common.contracts.canonical import canonical_bytes
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


def test_cleanup_scale_refuses_a_counterfeit_marker_in_a_non_scale_directory(tmp_path):
    root = tmp_path / "ordinary-directory"
    retained = root / "must-survive.txt"
    root.mkdir()
    retained.write_text("not scale output")
    (root / "aggregate-census.json").write_text('{"schema":"unrelated-census.v1"}')

    with pytest.raises(ValueError, match="not a valid R7b scale census"):
        cleanup_scale(root)

    assert retained.read_text() == "not scale output"


def test_cleanup_scale_refuses_a_census_whose_named_run_lost_its_authority(tmp_path):
    root = tmp_path / "scale-missing-cleanup-authority"
    result = run_scale(root, shards=1, pages_per_shard=1, allow_undersized_smoke=True)
    authority = root / "bench-scale-01" / scale.RUN_FILE
    authority.unlink()

    with pytest.raises(
        FileNotFoundError,
        match="scale cleanup census names a run without its RunTree authority",
    ):
        cleanup_scale(root)

    assert root.is_dir()
    assert json.loads((root / "scale-result.json").read_bytes()) == result


def test_cleanup_scale_refuses_a_missing_scale_result(tmp_path):
    root = tmp_path / "scale-missing-result"
    run_scale(root, shards=1, pages_per_shard=1, allow_undersized_smoke=True)
    (root / "scale-result.json").unlink()

    with pytest.raises(FileNotFoundError, match="scale-result.json"):
        cleanup_scale(root)

    assert root.is_dir()


def test_cleanup_scale_refuses_noncanonical_scale_result_bytes(tmp_path):
    root = tmp_path / "scale-noncanonical-result"
    run_scale(root, shards=1, pages_per_shard=1, allow_undersized_smoke=True)
    result_path = root / "scale-result.json"
    result_path.write_bytes(result_path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match=r"scale-result\.json is not canonical bytes"):
        cleanup_scale(root)

    assert root.is_dir()


@pytest.mark.parametrize(
    ("field", "altered"),
    [
        ("schema", "counterfeit-result.v1"),
        ("state", "measured"),
        ("shards", 2),
        pytest.param("shards", True, id="boolean-shards"),
        ("pages_per_shard", 2),
        ("artifact_count", 2),
    ],
)
def test_cleanup_scale_refuses_a_result_that_disagrees_with_its_census(tmp_path, field, altered):
    root = tmp_path / f"scale-result-{field}-mismatch"
    run_scale(root, shards=1, pages_per_shard=1, allow_undersized_smoke=True)
    result_path = root / "scale-result.json"
    result = json.loads(result_path.read_bytes())
    result[field] = altered
    result_path.write_bytes(canonical_bytes(result))

    with pytest.raises(ValueError, match=rf"scale-result\.json has a {field} mismatch"):
        cleanup_scale(root)

    assert root.is_dir()


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
