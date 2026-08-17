"""Prompt fidelity (invariant #49): byte-exact per-chair prompts, and a fail
closed refusal for any recipe with no registered builder.
"""

from pathlib import Path

import prompts
import protocol
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _dossier():
    return {
        "act_key": "a1",
        "witness_regime": "named",
        "testimonia": [
            {
                "witness_label": "attestator_1",
                "model_name": "fixture/attestator-1",
                "resolved_provenance": {"resolved_revision": "fixture-v1"},
                "training_domain": "a synthetic fixture witness",
                "outcome": "read",
                "reported": "alpha beta gamma",
            }
        ],
    }


def test_the_fixture_recipe_reproduces_its_declared_template_byte_for_byte():
    built = prompts.build_prompt("fake-perlector-v0", "perlector", _dossier())
    assert built == (
        "page_shared_prefix_policy: page-shared-prefix-first.v1\n"
        "witness_regime: named\n"
        "testimonia:\n"
        "  - attestator_1 (a synthetic fixture witness): 'alpha beta gamma'; "
        'model=\'fixture/attestator-1\'; provenance={"resolved_revision":"fixture-v1"}\n'
        "role: perlector\n"
        "act: a1"
    )


# --- The Pass-B prompt that actually carries the prior draft (R5a) ------------
#
# The declared-template test above renders a dossier with no prior draft and no
# sealed protocol, so until these tests nothing anywhere asserted the bytes of
# the one prompt that puts a prior reading in front of the reader. A builder
# that dropped the neutral fragment, or presented the draft as authoritative,
# reproduced from every record and passed every check: `prompt_evidence` only
# proves the recorded digest matches whatever the builder currently emits.
# R5a's contract asked for the neutrality constraint in the builder AND its
# fidelity test; this is the second half.


def _sealed_protocol_config():
    protocol_config, _sha256 = protocol.load(ROOT / "config" / "perlector_protocol.toml")
    return protocol_config


def _fed_dossier():
    return _dossier() | {
        "prior_draft": {
            "reference": {"relative_path": "4_perlector/artifacts/x.json", "sha256": "0" * 64},
            "text": "SYNTHETIC ACT ONE alpha beta ganna",
        },
        "prior_draft_view": "fed",
    }


def test_the_fed_pass_b_prompt_reproduces_its_declared_template_byte_for_byte():
    built = prompts.build_prompt(
        "fake-perlector-v0", "perlector", _fed_dossier(), _sealed_protocol_config()
    )
    assert built == (
        "page_shared_prefix_policy: page-shared-prefix-first.v1\n"
        "witness_regime: named\n"
        "testimonia:\n"
        "  - attestator_1 (a synthetic fixture witness): 'alpha beta gamma'; "
        'model=\'fixture/attestator-1\'; provenance={"resolved_revision":"fixture-v1"}\n'
        "prior_draft:\n"
        "SYNTHETIC ACT ONE alpha beta ganna\n"
        "This is a prior reading. It may be correct, incomplete, or wrong. Independently "
        "reread the image, preserve what the ink supports, and change only what the image "
        "justifies.\n"
        "role: perlector\n"
        "act: a1"
    )


def test_the_withheld_pass_b_prompt_carries_neither_the_draft_nor_its_fragment():
    withheld = _fed_dossier() | {"prior_draft_view": "withheld"}
    built = prompts.build_prompt(
        "fake-perlector-v0", "perlector", withheld, _sealed_protocol_config()
    )
    assert built == prompts.build_prompt(
        "fake-perlector-v0", "perlector", _dossier(), _sealed_protocol_config()
    )
    assert "prior_draft" not in built
    assert protocol.PASS_B_FRAGMENT not in built


def test_the_prior_draft_is_never_the_last_word_before_the_reader_is_addressed():
    """Ordering is part of what the prompt asserts. The neutral fragment sits
    immediately after the draft, so the draft is never the final instruction
    the reader sees about it -- GOVERNANCE 3's 'no picker' in prompt bytes,
    not only in the dossier guard."""
    lines = prompts.build_prompt(
        "fake-perlector-v0", "perlector", _fed_dossier(), _sealed_protocol_config()
    ).splitlines()
    draft_line = lines.index("SYNTHETIC ACT ONE alpha beta ganna")
    assert lines[draft_line - 1] == "prior_draft:"
    assert lines[draft_line + 1] == protocol.PASS_B_FRAGMENT


def test_two_acts_of_one_page_share_the_prefix_the_record_claims_they_do():
    """`page_shared_prefix_policy` rides on every prompt record. Measured, not
    assumed: the shared prefix is real and starts with the policy line, and it
    ends where the first act-scoped line (the act's own testimonia) begins.
    That is the whole of what this builder's ordering buys; the record names a
    policy, and this is the check that the policy is not just a label."""
    config = _sealed_protocol_config()
    first = prompts.build_prompt("fake-perlector-v0", "perlector", _dossier(), config)
    second_dossier = _dossier() | {"act_key": "a2"}
    second_dossier["testimonia"] = [_dossier()["testimonia"][0] | {"reported": "delta epsilon"}]
    second = prompts.build_prompt("fake-perlector-v0", "perlector", second_dossier, config)

    shared = []
    for left, right in zip(first.splitlines(), second.splitlines(), strict=False):
        if left != right:
            break
        shared.append(left)
    assert shared == [
        "page_shared_prefix_policy: page-shared-prefix-first.v1",
        "witness_regime: named",
        "testimonia:",
    ]
    # And the act's own identity is last, not second, so nothing act-scoped
    # sits ahead of the shared lines.
    assert first.splitlines()[-1] == "act: a1"


def test_the_same_dossier_and_recipe_always_produce_the_same_bytes():
    first = prompts.build_prompt("fake-perlector-v0", "perlector", _dossier())
    second = prompts.build_prompt("fake-perlector-v0", "perlector", _dossier())
    assert first == second


def test_an_unregistered_recipe_refuses_rather_than_falling_back_to_a_default():
    """The load-bearing behaviour: a fine-tuned candidate served under a recipe
    with no declared builder must never silently render through some other
    chair's template."""
    with pytest.raises(ValueError, match="no declared prompt builder"):
        prompts.build_prompt("some-unregistered-recipe-v7", "perlector", _dossier())


def test_the_refusal_is_not_vacuous_it_actually_distinguishes_recipes():
    """Prove the guard can go red: a recipe that *is* registered must not
    raise, so the refusal above is really about the missing entry."""
    prompts.build_prompt("fake-perlector-v0", "perlector", _dossier())  # does not raise
    with pytest.raises(ValueError, match="no declared prompt builder"):
        prompts.build_prompt("fake-perlector-v1", "perlector", _dossier())


def test_prompt_evidence_binds_the_builders_own_bytes_not_only_its_name(monkeypatch):
    """D-7: the recipe name pins *which* builder ran only by convention -- a
    future edit to `_fake_perlector_v0` must change what the record claims
    about itself, or an old record's `rendered_sha256` would be unreproducible
    with nothing to say why."""
    from common.chairs.models import ChairIdentity

    chair = ChairIdentity(
        role="perlector",
        source="local-repository",
        repo=None,
        path="perlector",
        revision=None,
        digest_manifest="0" * 64,
        manifest="manifests/perlector.json",
        adapter_of=None,
        serving_recipe="fake-perlector-v0",
        license_note="fixture identity only; no model weights or model license apply",
    )
    dossier = _dossier() | {"dossier_digest": "d" * 64}
    before = prompts.prompt_evidence(chair, dossier)
    # The claim is module-wide on purpose: a builder renders through helpers,
    # so the digest binds every line of prompt-building code, not one
    # function's source. A monkeypatched builder is not a source edit and must
    # NOT move it -- only the rendered bytes move.
    from pathlib import Path

    from common.contracts.canonical import digest_bytes

    module_bytes = Path(prompts.__file__).resolve().read_bytes()
    assert before["builder_sha256"] == digest_bytes(module_bytes), (
        "builder_sha256 must be the digest of the whole prompt module's source"
    )

    def _mutated_builder(chair_role, dossier, protocol_config):
        return prompts._fake_perlector_v0(chair_role, dossier, protocol_config) + "\n"

    monkeypatch.setitem(prompts._BUILDERS, "fake-perlector-v0", _mutated_builder)
    after = prompts.prompt_evidence(chair, dossier)
    assert after["builder_sha256"] == before["builder_sha256"], (
        "a runtime monkeypatch is not a source edit; the module digest binds bytes on disk"
    )
    assert after["rendered_sha256"] != before["rendered_sha256"]


def test_the_default_protocols_policy_literal_agrees_with_the_protocol_pin():
    """prompts.py is self-digesting (its bytes are builder_sha256), so the
    duplicate literal cannot be replaced with an import without moving every
    pin. The agreement is pinned here instead: if either side changes its
    spelling, this fails and a person reconciles the two on purpose."""
    assert (
        prompts._DEFAULT_PROTOCOL["page_shared_prefix_policy"] == protocol.PAGE_SHARED_PREFIX_POLICY
    )
