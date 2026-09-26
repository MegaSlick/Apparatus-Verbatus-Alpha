"""A configuration's seal covers what it says, never how it is written."""

from pathlib import Path

import pytest

from common.chairs import ChairRegistry
from common.contracts.errors import ContractError
from common.recovery import load_recovery_policy
from common.sealed_config import MAX_CONFIG_BYTES, read_sealed_toml
from common.stage import run_config_bindings

CONFIG = Path(__file__).resolve().parents[1] / "config"
SEALED_FILES = {
    "pdf_render_config_path": "pdf_render.toml",
    "designator_padding_config_path": "designator_padding.toml",
    "designator_grouping_config_path": "designator_grouping.toml",
    "alignment_config_path": "alignment.toml",
    "armarium_formats_config_path": "formats.toml",
    "recovery_config_path": "recovery.toml",
    "hard_failure_config_path": "hard_failure.toml",
    "witness_context_config_path": "witness_context.toml",
    "perlector_protocol_config_path": "perlector_protocol.toml",
    "perlector_audit_config_path": "perlector_audit.toml",
    "serving_recipes_config_path": "serving_recipes.toml",
    "pod_placement_config_path": "pod_placement.toml",
    "corpus_frame_config_path": "corpus_frame.toml",
    "decoding_config_path": "decoding.toml",
    "triage_modes_config_path": "triage_modes.toml",
}


def _bindings(**paths):
    models = ChairRegistry.from_toml(CONFIG / "models.toml").config
    bindings = run_config_bindings(models, {"fixture": "none"}, "test", **paths)
    return {
        name: bindings[name]
        for name in ("config_digest", "sealed_config_digests", "serving_config_inputs")
    }


def test_a_comment_edit_to_any_sealed_config_moves_no_seal(tmp_path):
    paths = {}
    for keyword, name in SEALED_FILES.items():
        edited = tmp_path / name
        edited.write_bytes(b"# a reviewer's note\n\n" + (CONFIG / name).read_bytes() + b"\n# end\n")
        paths[keyword] = edited

    assert _bindings(**paths) == _bindings()


def test_a_value_edit_moves_the_seal(tmp_path):
    recovery = tmp_path / "recovery.toml"
    recovery.write_bytes((CONFIG / "recovery.toml").read_bytes().replace(b"= 1", b"= 0", 1))

    assert (
        load_recovery_policy(recovery)["config_sha256"] != load_recovery_policy()["config_sha256"]
    )
    assert (
        _bindings(recovery_config_path=recovery)["sealed_config_digests"]["recovery"]
        != _bindings()["sealed_config_digests"]["recovery"]
    )


def test_the_seal_ignores_layout_and_key_order_but_not_values(tmp_path):
    def seal(text: str) -> str:
        path = tmp_path / "policy.toml"
        path.write_text(text, encoding="utf-8")
        return read_sealed_toml(path, "test policy")[1]

    base = seal("a = 1\nb = 'x'\n[t]\nc = [1, 2]\n")
    assert seal("# note\nb = 'x'   # why\na = 1\n\n[t]\nc = [ 1, 2 ]\n") == base
    assert seal("a = 2\nb = 'x'\n[t]\nc = [1, 2]\n") != base
    assert seal("a = 1\nb = 'x'\n[t]\nc = [2, 1]\n") != base


def test_an_unknown_key_in_the_recovery_policy_refuses(tmp_path):
    recovery = tmp_path / "recovery.toml"
    recovery.write_bytes((CONFIG / "recovery.toml").read_bytes() + b"\n[budget_override]\nx = 9\n")

    with pytest.raises(ContractError, match="unknown top-level field"):
        load_recovery_policy(recovery)


def test_a_config_read_is_bounded_before_any_toml_work(tmp_path):
    oversized = tmp_path / "policy.toml"
    oversized.write_bytes(b"#" * (MAX_CONFIG_BYTES + 1))

    with pytest.raises(ContractError, match=f"{MAX_CONFIG_BYTES}-byte limit"):
        read_sealed_toml(oversized, "test policy")
