"""F-new-1 (audit finding, mutation-of-mechanisms pass): `require_corpus_frame_shard`
had no test anywhere in the suite -- not its refusal branch, not its point-of-use
TOCTOU recheck.  A run whose page count exceeds the sealed shard limit, or whose
config bytes changed between binding and the run-creation check, is exactly what
the brief's tampering battery asks for ("the shard limit bypassed by editing config
bytes after binding"); this file gives it a falsifiable test that fails red if the
call to `require_corpus_frame_shard` is removed from `pipeline/1_exemplar/door.py`,
or if either of its two checks is weakened.

Sonnet audit-and-repair seat 1, R0.
"""

from __future__ import annotations

import pytest

from common.contracts.errors import ContractError
from common.stage import (
    DEFAULT_CORPUS_FRAME_CONFIG_PATH,
    load_corpus_frame_policy,
    require_corpus_frame_shard,
)

REAL_DIGEST = load_corpus_frame_policy(DEFAULT_CORPUS_FRAME_CONFIG_PATH)[1]


def test_a_page_count_above_the_sealed_shard_limit_is_refused():
    """The shard boundary is a hard-failure unit, not merely sealed and ignored.

    Before this test, no test anywhere in the suite drove `require_corpus_frame_
    shard` past its limit (the fixture only ever has 2 pages); the enforcement
    call in `pipeline/1_exemplar/door.py` could be deleted entirely and the full
    suite would stay green.
    """
    policy, _ = load_corpus_frame_policy(DEFAULT_CORPUS_FRAME_CONFIG_PATH)
    limit = policy["max_pages_per_shard"]
    with pytest.raises(ContractError, match="shard limit"):
        require_corpus_frame_shard(limit + 1, {"corpus-frame-shard": REAL_DIGEST})


def test_a_page_count_at_the_sealed_shard_limit_is_admitted():
    """The boundary is inclusive: exactly the sealed limit is not itself a refusal."""
    policy, _ = load_corpus_frame_policy(DEFAULT_CORPUS_FRAME_CONFIG_PATH)
    require_corpus_frame_shard(policy["max_pages_per_shard"], {"corpus-frame-shard": REAL_DIGEST})


def test_a_shard_config_that_changed_since_binding_is_refused_before_use():
    """Point-of-use recheck: a `corpus-frame-shard` digest that no longer matches
    the config bytes on disk must refuse before the page-count comparison runs at
    all, exactly as `designator-padding`'s `require_sealed_config` refuses a
    config that changed between a run's binding check and the read that used it.
    """
    with pytest.raises(ContractError, match="changed between run binding"):
        require_corpus_frame_shard(1, {"corpus-frame-shard": "0" * 64})


def test_a_run_missing_the_sealed_shard_digest_entirely_is_refused():
    """A `sealed_config_digests` mapping that names no shard entry at all must
    refuse rather than silently treat every observed digest as unbound.
    """
    with pytest.raises(ContractError, match="changed between run binding"):
        require_corpus_frame_shard(1, {})
