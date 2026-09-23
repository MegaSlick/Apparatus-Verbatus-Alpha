"""principle 5's executable half: every export format's clean text hashes to the
established record's own text hash.

Spec 10, test 4: "every export format carries a canonical clean-text field, and
that field's hash equals the record's text hash per act, mechanically." The old
pipeline could never have passed this — the audit found the established text was
decided *twice*, so there was no single record for an export format to agree
with in the first place. This drives the real orchestrator end to end and checks
the internal `export` artifact the Armarium publishes: its `delivered[i]["text"]`
field must be byte-identical to, and therefore hash-identical to, the Archetypus
record's own `text` and `text_hash`.

Rendered displays (brackets, sigla) are not built by any stage yet — spec 10
names that as the Armarium's future business at export time — so the
render -> strip -> hash round-trip half of test 4 is proven separately, as a
schema-sufficiency demonstration, in `pipeline/6_archetypus/test_annotations.py`.

What this does **not** itself prove: that every *packaged* literal-text format
(text-bundle, acts-database, jsonl) carries the same characters. All three ship
today as members inside the single `export` artifact kind, so a guard keyed on
artifact *kind* would never see a new one land (F090) — this module's own guard
used to be keyed that way and could not fire. The guard below instead reads the
packaged manifest's own `formats.formats` list, which does grow the moment a
format is added or removed, and the cross-format identity claim itself is proven
by `pipeline/7_armarium/armarium_export.py::_compare_literal_projections`, built
at export time and verified again at read time, with unit coverage in
`pipeline/7_armarium/test_armarium_export.py`. When a sixth format joins
`common.armarium_formats.KNOWN_FORMATS`, the assertion below is where it must be
named, and its failure mode is a missing format, never a silent pass over the
ones that already exist.
"""

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from common.contracts.canonical import digest_of
from common.contracts.identities import artifact_id
from common.contracts.stages import ARCHETYPUS, ARMARIUM, RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE = "synthetic-two-page-v0"

# Both cases drive the complete orchestrator end to end; the everyday fast
# gate excludes them and the full gate (and CI) runs them.
pytestmark = pytest.mark.full


def orchestrate(root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    """Run the pipeline the way a person would, and return the whole result.

    Deliberately not imported from `test_orchestrator_acceptance.py`:
    `pipeline/test_stage_import_boundaries.py` refuses any stage file that
    imports `pipeline` by its dotted path, so this small helper is duplicated
    rather than shared across that boundary.
    """
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            scenario,
            "--run-id",
            run_id,
            "--run-root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        # Generous for the two-page fixture; a hung recovery loop should fail
        # this test with captured output, not block CI until an outer watchdog.
        timeout=600,
    )


def export_of(tree: RunTree) -> dict:
    return tree.read_artifact(ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None))[
        "payload"
    ]


@pytest.mark.parametrize(("scenario", "exit_code"), [("happy", 0), ("review", 3)])
def test_every_delivered_export_text_hashes_to_its_archetypus_record(tmp_path, scenario, exit_code):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", scenario)
    # The scenario's own exit, not "either": a held happy run or a completed
    # review run is a wrong result this test must not read past.
    assert result.returncode == exit_code, result.stderr
    tree = RunTree(root, "r")
    # A sanity check on the artifact *kinds* the Armarium publishes -- stable by
    # design, since every literal-text format ships as a member inside the one
    # `export` kind rather than as a kind of its own (F090: this shape is
    # exactly why a kind-keyed guard can never see a new format arrive).
    all_kinds = {entry["kind"] for entry in tree.build_manifest(ARMARIUM)["artifacts"]}
    boundary_kinds = {"decode-environment", "stage-seal"}
    assert all_kinds & boundary_kinds == boundary_kinds
    produced_kinds = all_kinds - boundary_kinds
    assert produced_kinds == {"export", "manifest-entry"}

    export = export_of(tree)
    assert export["delivered"], f"the {scenario!r} scenario must deliver at least one act"

    # The guard that actually fires on a new format: the packaged manifest's own
    # `formats.formats` selection, read out of the sealed bundle rather than
    # inferred from artifact kinds. A format landing in `config/formats.toml`'s
    # default selection that this test does not yet name here fails loudly,
    # instead of shipping under the umbrella of the one `export` kind.
    bundle_bytes = tree.read_bytes(export["bundle"]["reference"]["relative_path"])
    with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as archive:
        packaged_manifest = json.loads(archive.read("EXPORT_MANIFEST.json"))
    expected_formats = {"text-bundle", "acts-database", "jsonl", "review-items", "salvage-tier"}
    produced_formats = set(packaged_manifest["formats"]["formats"])
    assert produced_formats == expected_formats, (
        f"the exported bundle selects formats {sorted(produced_formats)}; a new format "
        "must be added to this projection-identity test's expectations (and, if it is a "
        "literal-text format, proven identical by _compare_literal_projections) before "
        "it ships"
    )

    for delivered in export["delivered"]:
        established = tree.read_artifact(
            ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", delivered["act_id"])
        )
        payload = established["payload"]
        # The one canonical clean-text field this export format carries.
        assert delivered["text"] == payload["text"]
        # And its hash mechanically equals the record's own text hash — the
        # projection-identity claim, proven rather than asserted by construction.
        assert digest_of(delivered["text"]) == payload["text_hash"]
        # The record's word about its own text travels with the text: one
        # status, everywhere, exactly as principle 5 holds the characters.
        assert delivered["text_status"] == payload["text_status"]
    # The whole delivered set, not a sample: a projection check that skipped an
    # act would pass while that act's export carried a different reading. These
    # comparisons — not the loop above, which visits whatever "delivered"
    # happens to hold — are the only lines that prove no act was passed over,
    # and the length checks are what a bare set comparison would forgive: an
    # act delivered twice collapses to one set member and a consumer counts it
    # twice while the suite reports success.
    delivered_ids = [item["act_id"] for item in export["delivered"]]
    archetypus_ids = [
        entry["subject_id"]
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    ]
    # A census from a different stage's records: the acts the Recensor
    # accepted. An omission shared by the Archetypus and the export cannot
    # also delete the Recensor's sealed review, so agreeing with this set is
    # not the run agreeing with itself. (Completeness against the *fixture* is
    # the acceptance suite's job, via its pinned file counts and digests.)
    recensor_accepted_ids = [
        entry["subject_id"]
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    ]
    assert len(delivered_ids) == len(set(delivered_ids))
    assert len(recensor_accepted_ids) == len(set(recensor_accepted_ids))
    assert len(delivered_ids) == len(archetypus_ids) == len(recensor_accepted_ids)
    assert set(delivered_ids) == set(archetypus_ids) == set(recensor_accepted_ids)
