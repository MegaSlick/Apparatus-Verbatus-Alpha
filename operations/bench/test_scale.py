from __future__ import annotations

from operations.bench.scale import run_scale


def test_scale_runner_refuses_a_smaller_substitute(tmp_path):
    try:
        run_scale(tmp_path / "scale", shards=1, pages_per_shard=1)
    except ValueError as error:
        assert "fixed at 10 shards" in str(error)
    else:
        raise AssertionError("scale runner accepted a smaller cardinality")
