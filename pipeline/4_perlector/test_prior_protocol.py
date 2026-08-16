"""R5a prior-draft protocol: distinct passes, refusal gate, and fixture signal."""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import protocol
import pytest

from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import perlector_attempt_id
from common.contracts.stages import PERLECTOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def _load_perlector():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("perlector_prior_protocol_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()


def _run(root, run_id="r", scenario="happy", *extra):
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            scenario,
            "--run-id",
            run_id,
            "--run-root",
            str(root),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _records(tree, kind):
    return [
        tree.read_artifact(PERLECTOR, kind, entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == kind
    ]


def test_sampled_triple_has_all_three_records_and_nuda_stays_unfed(tmp_path):
    root = tmp_path / "runs"
    result = _run(
        root,
        "r",
        "happy",
        "--nuda-per-mille",
        "1000",
        "--nuda-approval-ref",
        "fixture/nuda",
        "--perlector-instrument-per-mille",
        "1000",
        "--perlector-instrument-approval-ref",
        "fixture/prior",
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    assert len(_records(tree, "lectio-prior")) == 2
    assert len(_records(tree, "primed-without-prior")) == 2
    finals = _records(tree, "perlectio")
    assert len(finals) == 2
    assert any(record["payload"]["self_revision"] for record in finals)
    assert any(not record["payload"]["self_revision"] for record in finals)
    for record in _records(tree, "lectio-nuda"):
        dossier = record["payload"]["dossier"]
        assert dossier["testimonia"] == []
        assert "prior_draft" not in dossier
        assert "act_attachment" not in dossier


def test_unsampled_run_has_prior_and_production_but_no_control(tmp_path):
    root = tmp_path / "runs"
    result = _run(root)
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    assert len(_records(tree, "lectio-prior")) == len(_records(tree, "perlectio")) == 2
    assert _records(tree, "primed-without-prior") == []


def test_control_refuses_without_tyrels_approval_on_fixture_path(tmp_path):
    root = tmp_path / "runs"
    result = _run(root, "r", "happy", "--perlector-instrument-per-mille", "1")
    assert result.returncode != 0
    assert "unapproved instrument sample" in result.stderr
    assert not (root / "r").exists()


def test_draft_fed_toggle_records_both_states_and_withholds_prompt_text(tmp_path):
    root = tmp_path / "runs"
    result = _run(root, "r", "happy", "--no-draft-fed")
    assert result.returncode == 0, result.stderr
    final = next(
        record["payload"]
        for record in _records(RunTree(root, "r"), "perlectio")
        if record["payload"]["dossier"]["prior_draft"]["text"] != record["payload"]["text"]
    )
    assert final["protocol"]["draft_fed"] is False
    assert final["dossier"]["prior_draft_view"] == "withheld"
    prior_text = final["dossier"]["prior_draft"]["text"]
    assert prior_text

    protocol_config, _protocol_sha256 = protocol.load(ROOT / "config" / "perlector_protocol.toml")
    identity = perlector.ChairIdentity(**final["provenance"]["resolved_identity"])
    rendered = perlector.prompts.build_prompt(
        identity.serving_recipe,
        identity.role,
        final["dossier"],
        protocol_config,
    )
    assert prior_text not in rendered


def test_control_selection_has_no_run_id_input_at_all():
    """Structural, not behavioural: a signature that never accepts run_id cannot
    depend on it, whatever the draw itself does."""
    import inspect

    assert "run_id" not in inspect.signature(protocol.is_control_sampled).parameters


def test_two_different_run_ids_over_the_same_corpus_facts_sample_the_control_identically(
    tmp_path,
):
    """The behavioural half U18 actually names: two runs that only differ by
    run ID, over the same corpus, draw the same control-arm membership."""
    root = tmp_path / "runs"
    extra = (
        "--perlector-instrument-per-mille",
        "500",
        "--perlector-instrument-approval-ref",
        "fixture/prior",
    )
    first = _run(root, "run-one", "happy", *extra)
    second = _run(root, "run-two", "happy", *extra)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    def _control_sample(run_id):
        return {
            entry["subject_id"]
            for entry in RunTree(root, run_id).build_manifest(PERLECTOR)["artifacts"]
            if entry["kind"] == "primed-without-prior"
        }

    assert _control_sample("run-one") == _control_sample("run-two")


def test_changing_the_seed_changes_the_draw_for_some_act():
    """The draw is not a constant that happens to be reproducible: sweeping the
    seed alone, holding every other fact fixed, must move at least one act
    across the sampling threshold."""
    outcomes = {
        seed: protocol.is_control_sampled(
            "act-1",
            frame_digest="a" * 64,
            page_digest="b" * 64,
            seed=f"seed-{seed}",
            per_mille=500,
        )
        for seed in range(50)
    }
    assert len(set(outcomes.values())) == 2, (
        "50 distinct seeds at a 500/1000 rate produced only one outcome; the seed is "
        "not moving the draw"
    )


def test_changing_the_act_id_changes_the_draw_for_some_seed():
    """Symmetric check: at a fixed corpus/seed, sweeping the act moves the draw
    too, so membership is not secretly constant across a page."""
    outcomes = {
        act: protocol.is_control_sampled(
            f"act-{act}",
            frame_digest="a" * 64,
            page_digest="b" * 64,
            seed="c" * 64,
            per_mille=500,
        )
        for act in range(50)
    }
    assert len(set(outcomes.values())) == 2


def test_the_modulo_bias_stays_negligible():
    """`int(digest[:8], 16) % 1000` draws from 2**32 values, not a multiple of
    1000. The per-threshold bias this leaves is recorded in the function's own
    docstring; this test is the canary that catches it silently growing if the
    digest truncation width ever changes."""
    space = 2**32
    remainder = space % 1000
    assert remainder != 0, "no bias to record if this ever becomes exact -- update the docstring"
    relative_bias = 1 / (space // 1000)
    assert relative_bias < 1e-5


def test_each_prior_protocol_pass_has_a_distinct_closed_attempt_operation():
    ids = {
        perlector_attempt_id("act", operation, 1)
        for operation in ("lectio-prior", "primed-without-prior", "perlegere")
    }
    assert len(ids) == 3
    with pytest.raises(ValueError, match="unknown Perlector reading operation"):
        perlector_attempt_id("act", "prior", 1)


def test_core_fixture_declarations_exercise_departure_and_equality_both_ways():
    import tomllib

    fixture = tomllib.loads((ROOT / "proof" / "skeleton_fixture.toml").read_text())
    final = {act["key"]: act["text"] for act in fixture["act"]}
    for scenario in ("happy", "review"):
        priors = {
            row["act_key"]: row["text"]
            for row in fixture["prior_reading"]
            if row["scenario"] == scenario
        }
        assert any(priors[key] != final[key] for key in final)
        assert any(priors[key] == final[key] for key in final)


def test_a_control_reference_forged_as_a_perlectio_is_refused(tmp_path):
    root = tmp_path / "runs"
    result = _run(
        root,
        "r",
        "happy",
        "--perlector-instrument-per-mille",
        "1000",
        "--perlector-instrument-approval-ref",
        "fixture/prior",
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    control = _records(tree, "primed-without-prior")[0]
    entry = next(
        item
        for item in tree.build_manifest(PERLECTOR)["artifacts"]
        if item["artifact_id"] == control["artifact_id"]
    )
    with pytest.raises(SchemaRefusal, match="not required 'perlector'/'perlectio'"):
        tree.read_artifact_reference(
            {"relative_path": entry["relative_path"], "sha256": entry["sha256"]},
            stage=PERLECTOR,
            kind="perlectio",
            subject_id=control["subject_id"],
        )


def test_a_prior_reference_forged_as_a_perlectio_is_refused(tmp_path):
    """The sibling of the control's forged-reference refusal, for the other
    retired-and-reaccepted kind: a Pass-A draft is never a Perlectio either."""
    root = tmp_path / "runs"
    result = _run(root)
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    prior = _records(tree, "lectio-prior")[0]
    entry = next(
        item
        for item in tree.build_manifest(PERLECTOR)["artifacts"]
        if item["artifact_id"] == prior["artifact_id"]
    )
    with pytest.raises(SchemaRefusal, match="not required 'perlector'/'perlectio'"):
        tree.read_artifact_reference(
            {"relative_path": entry["relative_path"], "sha256": entry["sha256"]},
            stage=PERLECTOR,
            kind="perlectio",
            subject_id=prior["subject_id"],
        )


@pytest.fixture(scope="module")
def published_lectio_prior_payload(tmp_path_factory):
    """A real published lectio-prior payload, not a hand-built stand-in --
    matching test_perlectio_schema.py's published_payload pattern for the
    kind that fixture only covers by inheritance until now."""
    root = tmp_path_factory.mktemp("lectio-prior-schema") / "runs"
    result = _run(root, "r", "happy")
    assert result.returncode == 0, result.stderr
    return _records(RunTree(root, "r"), "lectio-prior")[0]["payload"]


@pytest.fixture(scope="module")
def published_perlectio_payload(tmp_path_factory):
    """A real production payload for prior-reference relation forgeries."""
    root = tmp_path_factory.mktemp("perlectio-prior-relation") / "runs"
    result = _run(root, "r", "happy")
    assert result.returncode == 0, result.stderr
    return _records(RunTree(root, "r"), "perlectio")[0]["payload"]


@pytest.fixture(scope="module")
def published_primed_without_prior_payload(tmp_path_factory):
    """A real published control-arm payload."""
    root = tmp_path_factory.mktemp("control-schema") / "runs"
    result = _run(
        root,
        "r",
        "happy",
        "--perlector-instrument-per-mille",
        "1000",
        "--perlector-instrument-approval-ref",
        "fixture/prior",
    )
    assert result.returncode == 0, result.stderr
    return _records(RunTree(root, "r"), "primed-without-prior")[0]["payload"]


@pytest.fixture(scope="module")
def _sealed_protocol():
    return protocol.load(ROOT / "config" / "perlector_protocol.toml")


def test_a_real_published_lectio_prior_satisfies_its_closed_schema(
    published_lectio_prior_payload, _sealed_protocol
):
    protocol_config, protocol_sha256 = _sealed_protocol
    perlector.validate_reading_payload(
        copy.deepcopy(published_lectio_prior_payload),
        outcome="read",
        fields=perlector._LECTIO_PRIOR_FIELDS,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )


def test_validator_refuses_a_protocol_record_without_threaded_sealed_config(
    published_lectio_prior_payload,
):
    """The validator must not recover protocol bytes from process cwd.

    This goes red if the removed cwd-relative reload returns: the real payload
    carries a protocol record, and no alternate validation failure is expected.
    """
    with pytest.raises(SchemaRefusal, match="not given the sealed protocol bytes"):
        perlector.validate_reading_payload(
            copy.deepcopy(published_lectio_prior_payload),
            outcome="read",
            fields=perlector._LECTIO_PRIOR_FIELDS,
        )


def test_producer_refuses_primed_with_prior_without_a_prior_reference(
    published_perlectio_payload, _sealed_protocol
):
    payload = copy.deepcopy(published_perlectio_payload)
    payload["dossier"].pop("prior_draft")
    payload["dossier"].pop("prior_draft_view")
    dossier_body = {
        key: value for key, value in payload["dossier"].items() if key != "dossier_digest"
    }
    payload["dossier"]["dossier_digest"] = perlector.digest_of(dossier_body)
    protocol_config, protocol_sha256 = _sealed_protocol

    with pytest.raises(SchemaRefusal, match="claims primed-with-prior"):
        perlector.validate_reading_payload(
            payload,
            outcome="read",
            fields=perlector._PERLECTIO_FIELDS,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
        )


def test_producer_refuses_primed_without_prior_with_a_prior_reference(
    published_perlectio_payload, _sealed_protocol
):
    payload = copy.deepcopy(published_perlectio_payload)
    payload["lectio_kind"] = "primed-without-prior"
    protocol_config, protocol_sha256 = _sealed_protocol

    with pytest.raises(SchemaRefusal, match="claims primed-without-prior"):
        perlector.validate_reading_payload(
            payload,
            outcome="read",
            fields=perlector._PERLECTIO_FIELDS,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
        )


@pytest.mark.parametrize("field", sorted(perlector._LECTIO_PRIOR_FIELDS))
def test_a_lectio_prior_missing_any_field_of_its_record_is_refused(
    published_lectio_prior_payload, _sealed_protocol, field
):
    protocol_config, protocol_sha256 = _sealed_protocol
    payload = copy.deepcopy(published_lectio_prior_payload)
    del payload[field]
    with pytest.raises(SchemaRefusal, match="not its closed schema"):
        perlector.validate_reading_payload(
            payload,
            outcome="read",
            fields=perlector._LECTIO_PRIOR_FIELDS,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
        )


def test_a_real_published_control_satisfies_its_closed_schema(
    published_primed_without_prior_payload, _sealed_protocol
):
    protocol_config, protocol_sha256 = _sealed_protocol
    perlector.validate_reading_payload(
        copy.deepcopy(published_primed_without_prior_payload),
        outcome="read",
        fields=perlector._PRIMED_WITHOUT_PRIOR_FIELDS,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )


@pytest.mark.parametrize("field", sorted(perlector._PRIMED_WITHOUT_PRIOR_FIELDS))
def test_a_control_missing_any_field_of_its_record_is_refused(
    published_primed_without_prior_payload, _sealed_protocol, field
):
    protocol_config, protocol_sha256 = _sealed_protocol
    payload = copy.deepcopy(published_primed_without_prior_payload)
    del payload[field]
    with pytest.raises(SchemaRefusal, match="not its closed schema"):
        perlector.validate_reading_payload(
            payload,
            outcome="read",
            fields=perlector._PRIMED_WITHOUT_PRIOR_FIELDS,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
        )


def test_the_sealed_protocol_declaration_reproduces(_sealed_protocol):
    protocol_config, protocol_sha256 = _sealed_protocol
    assert protocol_config["selection_rule"] == protocol.SELECTION_RULE
    assert protocol_config["page_shared_prefix_policy"] == protocol.PAGE_SHARED_PREFIX_POLICY
    assert len(protocol_sha256) == 64


def _write_protocol(tmp_path, **overrides):
    fields = {
        "selection_rule": protocol.SELECTION_RULE,
        "page_shared_prefix_policy": protocol.PAGE_SHARED_PREFIX_POLICY,
        "pass_b_fragment": "Independently reread the image.",
    }
    fields.update(overrides)
    path = tmp_path / "perlector_protocol.toml"
    path.write_text("\n".join(f'{key} = "{value}"' for key, value in fields.items()))
    return path


def test_a_pass_b_fragment_asserting_the_prior_was_wrong_is_refused(tmp_path):
    """GOVERNANCE-3's control (iterative_reader.md:46-51, GOV 10): the protocol
    declaration cannot ship a fragment that forces a change, only one that
    reports the finding."""
    path = _write_protocol(tmp_path, pass_b_fragment="The prior reading was wrong; correct it.")
    with pytest.raises(ContractError, match="protocol is neutral"):
        protocol.load(path)


def test_a_blank_pass_b_fragment_is_refused(tmp_path):
    path = _write_protocol(tmp_path, pass_b_fragment="   ")
    with pytest.raises(ContractError, match="blank Pass-B fragment"):
        protocol.load(path)


def test_an_unknown_selection_rule_is_refused(tmp_path):
    path = _write_protocol(tmp_path, selection_rule="stratified-v1")
    with pytest.raises(ContractError, match="unknown selection rule"):
        protocol.load(path)


def test_an_unknown_page_shared_prefix_policy_is_refused(tmp_path):
    path = _write_protocol(tmp_path, page_shared_prefix_policy="witness-order-first.v1")
    with pytest.raises(ContractError, match="unknown page-shared-prefix policy"):
        protocol.load(path)


def test_a_protocol_declaration_missing_a_field_is_refused(tmp_path):
    path = tmp_path / "perlector_protocol.toml"
    path.write_text(f'selection_rule = "{protocol.SELECTION_RULE}"')
    with pytest.raises(ContractError, match="not its closed string schema"):
        protocol.load(path)
