"""Over-capacity presentations become explicit holds without killing the stage."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.cross_capture_autopsia import OVER_CAPACITY  # noqa: E402

ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
PROTOCOL = ROOT / "config" / "perlector_protocol.toml"


@pytest.fixture(scope="module")
def over_capacity_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("over-capacity")
    protocol = tmp / "perlector_protocol.toml"
    # Assert the substitution. `replace` returns the text unchanged if the
    # shipped protocol ever stops declaring `max_images = 32`; the run would
    # then use the shipped ceiling of 32, every act would read normally, and the
    # failure would surface far below as "read" != "not-run" -- sending whoever
    # reads it into the capacity-hold logic, which is not where the fault is.
    shipped = PROTOCOL.read_text()
    lowered = shipped.replace("max_images = 32", "max_images = 1")
    assert lowered != shipped, "the shipped protocol no longer declares max_images = 32"
    protocol.write_text(lowered)
    root = tmp / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-id",
            "r",
            "--run-root",
            str(root),
            "--perlector-protocol-config",
            str(protocol),
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    return result, root


def _perlectiones(root):
    return [
        json.loads(path.read_text())
        for path in sorted((root / "r" / "4_perlector" / "artifacts" / "perlectio").iterdir())
    ]


def _reviews(root):
    return [
        json.loads(path.read_text())
        for path in sorted((root / "r" / "5_recensor" / "artifacts" / "review").iterdir())
    ]


def _expected_act_count(root):
    """The Designator's own denominator, independent of anything the Perlector
    or Recensor later publish -- so a fault that drops the same act from both
    downstream collections still shows up here.
    """
    (seal_path,) = sorted((root / "r" / "2_designator" / "artifacts" / "proposal-seal").iterdir())
    payload = json.loads(seal_path.read_text())["payload"]
    assert payload["count"] == len(payload["expected_acts"])
    return payload["count"]


def test_an_over_capacity_presentation_holds_its_act_without_killing_the_stage(over_capacity_run):
    result, root = over_capacity_run
    # Exit 2 is fatal; a capacity hold is a run outcome rather than a crash.
    assert result.returncode != 2, result.stderr
    records = _perlectiones(root)
    assert records, "the Perlector published nothing at all"
    for record in records:
        assert record["outcome"] == "not-run"
        assert record["payload"]["reason"].startswith(OVER_CAPACITY)
        assert "max_images provides 1" in record["payload"]["reason"]
        assert record["payload"]["logical_act_id"]
        assert (
            record["payload"]["cross_capture_autopsia"]["logical_act_id"]
            == record["payload"]["logical_act_id"]
        )
        partition_ref = record["payload"]["cross_capture_autopsia"]["partition_ref"]
        assert partition_ref in record["inputs"]
        assert record["payload"]["basis"] == {"regions": [], "testimonia": []}
        assert record["payload"]["dissent"] == []
        assert "text" not in record["payload"]


def test_no_reader_pass_is_published_for_an_act_that_never_fit(over_capacity_run):
    """Capacity refusal must precede every arm, including the retained prior.

    The lectio-prior assertion is the load-bearing one, because the prior arm is
    universal. The lectio-nuda assertion below is a shape check only: this
    fixture declares no `--nuda-per-mille`, so that directory would be absent
    even with the capacity hold removed entirely, and it cannot fail for the
    reason this test is named for.
    """
    _result, root = over_capacity_run
    stage = root / "r" / "4_perlector" / "artifacts"
    assert not (stage / "lectio-prior").exists()
    assert not (stage / "lectio-nuda").exists()


def test_the_recensor_holds_every_over_capacity_act_and_loses_none_of_them(over_capacity_run):
    """The downstream half of the capacity hold, asserted here because it rests
    on this module's one run rather than on a second fixture pass.

    A review of this path (CodeRabbit, PR #78) read the Recensor's recovery gate
    — which admits only a declared recovery act or an ink-confirmed unclaimed
    observation — and concluded that an act held over capacity therefore stays
    unread *silently*. It does not. `not-run` is a non-COMPLETED reading class,
    so the act falls through the corroboration gate (nothing here is a positive
    claim of absence) into the ordinary hold, its review names the outcome, and
    the run's aggregate is partial. The right route is exactly that hold and not
    bounded recovery: recovery buys coverage of ink nobody has read, and a
    presentation that does not fit one request is not answered by another crop
    of the same act — it is answered by a ceiling a human sets.

    What the act must never do is disappear behind a successful status, so this
    pins the three facts that make it visible: the review outcome and its stated
    reason, the reachability of the capacity sentence from the review's own
    `perlectio_ref`, and a recovery pool that was never spent on a hold recovery
    cannot lift.
    """
    result, root = over_capacity_run
    # 3 is `EXIT_HELD` at the orchestrator: partial, never complete.
    assert result.returncode == 3, result.stderr
    expected_count = _expected_act_count(root)
    reviews = _reviews(root)
    perlectiones = _perlectiones(root)
    # Each output collection is checked against the Designator's independent
    # denominator first, and only then against each other -- so a fault that
    # dropped the same act from both `perlectio` and `review` cannot pass by
    # having the two agree with one another instead of with the input.
    assert len(perlectiones) == expected_count
    assert len(reviews) == expected_count
    assert len(reviews) == len(perlectiones)
    for review in reviews:
        assert review["outcome"] == "held-for-review"
        assert "'not-run'" in review["payload"]["reason"]
        # No presentation was ever delivered, so there is no visibility survey
        # to report -- `None` is "no survey exists", the same fact a
        # Designator-held act's review records, not a survey that came back
        # clean.
        assert review["payload"]["cross_capture_coverage"] is None
        assert review["payload"]["audit_unresolved"] is None
        reading = json.loads(
            (root / "r" / review["payload"]["perlectio_ref"]["relative_path"]).read_text()
        )
        assert reading["outcome"] == "not-run"
        assert reading["payload"]["reason"].startswith(OVER_CAPACITY)
    assert not (root / "r" / "5_recensor" / "artifacts" / "recovery-request").exists()
