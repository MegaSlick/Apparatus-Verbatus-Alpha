from __future__ import annotations

from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.decoding import (
    load_decoding_policy,
    variance_experiment_id,
    variance_pass_artifact_id,
    variance_pass_attempt_id,
)


def test_shipped_decoding_policy_declares_a_zero_temperature_record_and_variance_shape():
    policy, digest = load_decoding_policy()
    assert policy["reading_of_record"] == {"temperature": 0}
    assert policy["variance_experiment"] == {
        "label": "variance.v1",
        "seed": 20260820,
        "passes": 2,
    }
    assert len(digest) == 64


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 1\n'
            '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 2\n',
            "temperature 0",
        ),
        (
            'schema = "decoding.v1"\n[reading_of_record]\ntemperature = false\n'
            '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 2\n',
            "temperature 0",
        ),
        (
            'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 0\n'
            '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 1\n',
            "at least 2",
        ),
    ],
)
def test_decoding_policy_refuses_a_non_record_posture_or_retry_shaped_experiment(
    tmp_path: Path, body: str, message: str
):
    path = tmp_path / "decoding.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ContractError, match=message) as refusal:
        load_decoding_policy(path)
    assert "No run or stage artifact was written" in str(refusal.value)
    assert "Restore or correct the decoding file and retry" in str(refusal.value)


def test_each_variance_pass_has_its_own_artifact_identity_under_the_sealed_plan():
    policy, _digest = load_decoding_policy()

    first = variance_pass_artifact_id("perlector", "variance-lectio", "act_1", policy, 1)
    second = variance_pass_artifact_id("perlector", "variance-lectio", "act_1", policy, 2)

    assert first != second
    assert variance_pass_attempt_id(policy, 1) != variance_pass_attempt_id(policy, 2)
    changed_seed = {**policy, "variance_experiment": {**policy["variance_experiment"], "seed": 7}}
    changed_count = {
        **policy,
        "variance_experiment": {**policy["variance_experiment"], "passes": 3},
    }
    assert variance_experiment_id(policy) != variance_experiment_id(changed_seed)
    assert variance_experiment_id(policy) != variance_experiment_id(changed_count)


@pytest.mark.parametrize("ordinal", [0, 3, True])
def test_a_variance_pass_cannot_escape_the_sealed_pass_count(ordinal):
    policy, _digest = load_decoding_policy()

    with pytest.raises(ContractError, match="sealed range"):
        variance_pass_attempt_id(policy, ordinal)


@pytest.mark.parametrize(
    "change",
    [
        {"label": "", "seed": 20260820, "passes": 2},
        {"label": "variance.v1", "seed": True, "passes": 2},
        {"label": "variance.v1", "seed": -1, "passes": 2},
        {"label": "variance.v1", "seed": 20260820, "passes": True},
    ],
)
def test_variance_identity_refuses_a_policy_the_loader_would_refuse(change):
    policy, _digest = load_decoding_policy()
    policy["variance_experiment"] = change

    with pytest.raises(ContractError, match="decoding variance_experiment"):
        variance_experiment_id(policy)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"\xff", "not UTF-8"),
        (b'schema = "decoding.v1"\n[', "not valid TOML"),
    ],
)
def test_decoding_policy_parse_refusals_name_the_actual_cause(tmp_path, body, message):
    path = tmp_path / "decoding.toml"
    path.write_bytes(body)

    with pytest.raises(ContractError, match=message) as refusal:
        load_decoding_policy(path)
    assert "No run or stage artifact was written" in str(refusal.value)
    assert "Restore or correct the decoding file and retry" in str(refusal.value)


def test_a_variance_pass_can_never_become_an_act_s_reading_of_record():
    """The two identity spaces do not meet, and the shared reader is why.

    A variance experiment and a reading of record can look at the very same act.
    What must never happen is that one of the experiment's passes is read back
    as the act's established reading -- that would be a picker assembled out of
    identities (GOVERNANCE 3), and it would be one nobody wrote on purpose.

    `common.stage.latest_attempt` is the one place "current" is derived, and it
    recomputes the attempt identity from the subject, the operation and the
    ordinal beside it. A variance pass's attempt identity is folded from the
    sealed experiment instead -- its label, seed and pass count -- so it does
    not recompute as `(act, "perlegere", n)` and the shared reader refuses it
    rather than ranking it against the readings.
    """
    from common.contracts.errors import FatalAccounting
    from common.contracts.identities import artifact_id as artifact_identity
    from common.contracts.identities import attempt_id
    from common.stage import latest_attempt

    policy, _digest = load_decoding_policy()
    act_id = "act_" + "0" * 16

    # Same act, same ordinal, two identities that share nothing.
    assert variance_pass_attempt_id(policy, 1) != attempt_id(act_id, "perlegere", 1)
    assert variance_pass_artifact_id(
        "perlector", "perlectio", act_id, policy, 1
    ) != artifact_identity("perlector", "perlectio", act_id, attempt_id(act_id, "perlegere", 1))

    reading = {
        "artifact_id": artifact_identity(
            "perlector", "perlectio", act_id, attempt_id(act_id, "perlegere", 1)
        ),
        "attempt_id": attempt_id(act_id, "perlegere", 1),
        "subject_id": act_id,
        "payload": {"attempt_ordinal": 1},
    }
    assert latest_attempt([reading], "reading", operation="perlegere") is reading

    # The same act, one ordinal further on, carrying an experimental pass's
    # sealed identity. Filed under a reading's kind it would outrank the reading
    # above on ordinal alone; the identity check is what stops it.
    smuggled = {
        "artifact_id": variance_pass_artifact_id("perlector", "perlectio", act_id, policy, 2),
        "attempt_id": variance_pass_attempt_id(policy, 2),
        "subject_id": act_id,
        "payload": {"attempt_ordinal": 2},
    }
    with pytest.raises(FatalAccounting, match="does not derive from"):
        latest_attempt([reading, smuggled], "reading", operation="perlegere")
