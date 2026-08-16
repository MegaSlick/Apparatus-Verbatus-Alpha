"""R0 falsification tests: reserved kind names and the closed-ordinal confidence rule.

Written blind, from /out/R0_CONTRACT_NOTE.md (v2) and the resolved v2.1 stack, before
the R0 build chamber runs. Every test here must fail RED on the chamber's base commit
(main 176b09e) because the refusal it checks is not yet built.

Kind table (R0_CONTRACT_NOTE.md "Kind-by-kind table"):
    lectio-prior, primed-without-prior         DEFERRED -> R5a
    audit-draft, audit-finding                 DEFERRED -> R5b
    raw-proposal, occlusion (U7 kinds)         DEFERRED -> R2
    Closed-ordinal confidence rule             EXERCISED narrowly (R0 owns it)

"Refusals, not absences" (brief priority 3): the point of R0 is not that these kinds
happen to be unused today (they are: nothing produces them), it is that the pipeline
actively REFUSES an attempt to mint one this early. A test that only checks that no
such kind appears in a run tree would pass vacuously on a base that never tried to
write one at all, which proves nothing. These tests instead attempt the write, through
the real validation surface, and require a named refusal.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from common.contracts.envelope import build_envelope
from common.contracts.errors import ContractError
from common.contracts.stages import DESIGNATOR, PERLECTOR

# Every kind name the contract note defers past R0, by name, mapped to a stage whose
# outcome vocabulary it could plausibly ride (so `classify()` inside `build_envelope`
# does not itself refuse the envelope for an unrelated reason first). The mapping is
# this test's own judgment call about which producer would mint each kind — the
# contract note assigns producing *branches*, not stages — recorded here and in the
# report as the strictest defensible reading available before R2/R5a/R5b exist.
DEFERRED_KINDS = (
    (PERLECTOR, "read", "lectio-prior"),
    (PERLECTOR, "read", "primed-without-prior"),
    (PERLECTOR, "read", "audit-draft"),
    (PERLECTOR, "read", "audit-finding"),
    (DESIGNATOR, "proposed", "raw-proposal"),
    (DESIGNATOR, "proposed", "occlusion"),
)


@pytest.mark.parametrize("stage,outcome,kind", DEFERRED_KINDS)
def test_a_deferred_kind_name_is_not_yet_an_accepted_kind(stage, outcome, kind):
    """R0_CONTRACT_NOTE.md kind table: a deferred kind name is refused, not merely absent.

    On the base commit `build_envelope` has no kind allowlist at all -- any string is
    accepted as long as the (stage, outcome) pair classifies -- so this call currently
    SUCCEEDS and returns a well-formed envelope. That is the RED failure: nothing
    refuses a would-be producer of an R2/R5a/R5b kind from minting one through R0's own
    stage-neutral envelope builder before those branches exist to define what the kind
    even means.
    """
    with pytest.raises(ContractError, match=kind):
        build_envelope(
            run_id="r0-reserved-kind-probe",
            artifact_id="art_0000000000000000",
            subject_id="act_0000000000000000",
            stage=stage,
            kind=kind,
            outcome=outcome,
            config_digest="0" * 64,
            adapter_revision="fixture-v0",
            inputs=[],
            payload={"probe": "R0 reserved-kind falsification test"},
        )


# --- Closed-ordinal confidence (three_stage_reading_design.md v2.1 §2, U5) ------
#
# "Confidence is a closed ordinal (canonical artifacts refuse floats)." Floats are
# ALREADY refused everywhere by `common/contracts/canonical.py::_refuse_floats`, so a
# test that only checks a float is rejected would pass today for a reason that has
# nothing to do with a *closed ordinal* -- it is just the generic no-floats-anywhere
# rule. The R0-owned rule this note asks for is narrower and does not exist yet: a
# witness's self-reported confidence must be one of a named, closed set of levels, not
# merely "not a float". An arbitrary out-of-set string or a raw integer both currently
# pass straight through `pipeline/3_attestatores/run.py`'s handling of
# `witness_reported` untouched -- nothing there checks the *value*, only that the JSON
# shape is recordable.

ROOT = Path(__file__).resolve().parents[2]


def _stage_module(name: str, path: Path):
    """Load one numeric-directory stage program by an unpolluted, unique name.

    Never a bare ``import run`` — this repository's stage directories are not
    packages, and several of them define a module literally named ``run``. A plain
    import would risk resolving to whichever same-named module Python's import
    cache already holds from an earlier test file in the same session, silently
    testing the wrong stage. Mirrors `pipeline/orchestrator/test_terminal_guards.py`'s
    own `_stage_module` helper.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attestatores_run = _stage_module(
    "r0_reserved_kinds_attestatores", ROOT / "pipeline" / "3_attestatores" / "run.py"
)


def test_a_witness_confidence_value_outside_the_closed_ordinal_set_is_refused():
    """A confidence claim outside the closed ordinal set must be a named refusal.

    `format_capabilities_for`/`prepared_response` in the Attestatores stage program are
    where a witness's self-report is validated today, and today they validate only that
    `witness_reported` is JSON-safe -- never that `confidence` names one of a closed,
    predeclared set of ordinal levels. This out-of-set value is nonsense on its face
    ("more confident than confident itself") and is exactly what a closed ordinal
    exists to exclude; on the base commit it is retained verbatim.
    """
    row = {
        "payload": "SYNTHETIC ACT ONE alpha beta gamma",
        "witness_reported": {"confidence": "more-confident-than-confident-itself"},
    }
    native_payload, witness_reported, capabilities, health, recording_problem = (
        attestatores_run.prepared_response(row)
    )
    assert recording_problem is not None, (
        "a witness_reported.confidence value outside any closed ordinal set was accepted "
        "as a normal reading instead of being refused; R0 owns a contracts-level closed-"
        "ordinal validation rule for confidence and none is wired into the Attestatores "
        "self-report path yet"
    )
    # Strengthened at the chain-end CodeRabbit pass: the refusal must be the
    # confidence rule's own, not some unrelated recording problem.
    assert "confidence" in recording_problem


def test_a_witness_confidence_integer_ordinal_outside_any_declared_scale_is_refused():
    """A bare out-of-range integer confidence is refused the same way a bad string is.

    A closed ordinal is a fixed, named set of levels -- not "any integer", which is
    exactly the unbounded scale a closed ordinal exists to rule out. `_native_problem`
    (via `prepared_response`) accepts any plain int today with no range or membership
    check at all.
    """
    row = {
        "payload": "SYNTHETIC ACT ONE alpha beta gamma",
        "witness_reported": {"confidence": 999_999},
    }
    native_payload, witness_reported, capabilities, health, recording_problem = (
        attestatores_run.prepared_response(row)
    )
    assert recording_problem is not None, (
        "an integer confidence value with no declared closed-ordinal scale was accepted "
        "verbatim; R0's closed-ordinal confidence rule is not enforced on the base commit"
    )
    assert "confidence" in recording_problem


def test_a_nested_confidence_cannot_bypass_the_closed_ordinal_rule():
    """The rule applies to confidence claims anywhere in retained self-report JSON."""
    row = {
        "payload": "SYNTHETIC ACT ONE alpha beta gamma",
        "witness_reported": {"metadata": {"confidence": 999_999}},
    }
    *_, recording_problem = attestatores_run.prepared_response(row)
    assert recording_problem is not None, (
        "a confidence ordinal nested below witness_reported.metadata bypassed the "
        "top-level closed-set check"
    )
    assert "confidence" in recording_problem
