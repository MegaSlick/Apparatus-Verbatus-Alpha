"""The Perlector reads each chair's current testimonium, never a superseded one.

`pipeline/3_attestatores/run.py` cannot itself produce a second attempt for one
(act, chair) today -- every attempt is hardcoded to ordinal 1 -- so this forges
the second attempt directly onto the tree, the same technique
`pipeline/orchestrator/test_orchestrator_acceptance.py` already uses to exercise a
chair-identity boundary no live producer reaches yet either.
"""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.alignment import markup_text_view
from common.chairs.registry import ChairRegistry
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import ATTESTATORES, DESIGNATOR, PERLECTOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
MODELS_CONFIG = ROOT / "config" / "models.toml"


def _load_perlector():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("perlector_testimonia_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()


class _Context:
    def __init__(self, tree):
        self.tree = tree
        self.run = tree.read_run()
        self.registry = ChairRegistry.from_toml(MODELS_CONFIG)

    def input_ref(self, relative_path):
        return {
            "relative_path": relative_path,
            "sha256": digest_bytes(self.tree.read_bytes(relative_path)),
        }

    @property
    def witness_chairs(self):
        return list(self.run["witness_chairs"])


@pytest.fixture
def run_with_a_superseded_attempt(tmp_path):
    """A real happy-path run, with one chair's testimonium given a second, later
    attempt that disagrees with its first — forged directly, since no CLI path
    produces a second attempt today."""
    root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-root",
            str(root),
            "--run-id",
            "superseded-attempt",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    tree = RunTree(root, "superseded-attempt")
    entries = tree.build_manifest(ATTESTATORES)["artifacts"]
    first = next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in entries
        if entry["kind"] == "testimonium"
        and tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])["outcome"]
        == "read"
    )
    act_id = first["subject_id"]
    chair = first["payload"]["chair"]

    second = copy.deepcopy(first)
    second["payload"]["attempt_ordinal"] = 2
    second["outcome"] = "failed"
    second["payload"]["reported"] = "a later, failed re-read"
    second["payload"]["reason"] = "the chair returned no usable report on the second attempt"
    second["attempt_id"] = attempt_id(act_id, f"read:{chair}", 2)
    second["artifact_id"] = artifact_id(ATTESTATORES, "testimonium", act_id, second["attempt_id"])
    second["self_hash"] = self_hash(second)
    path = tree.resolve(f"3_attestatores/artifacts/testimonium/{second['artifact_id']}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(second))
    tree.write_manifest(ATTESTATORES)

    return _Context(tree), act_id, chair


def _proposal_regions(context, act_id):
    return sorted(
        [
            context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
            and entry["subject_id"] == act_id
            and context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])["payload"][
                "origin"
            ]
            == "proposal"
        ],
        key=lambda record: record["payload"]["attempt_ordinal"],
    )


def test_a_superseded_attempt_does_not_still_count_as_current_evidence(
    run_with_a_superseded_attempt,
):
    context, act_id, chair = run_with_a_superseded_attempt
    testimonia = perlector.testimonia_of(context, act_id, _proposal_regions(context, act_id))

    by_chair = {record["payload"]["chair"]: record for record in testimonia}
    assert len(testimonia) == len(by_chair), "more than one record survived for the same chair"
    assert by_chair[chair]["payload"]["attempt_ordinal"] == 2
    assert by_chair[chair]["outcome"] == "failed"


def test_the_witness_read_filter_excludes_a_chair_whose_current_attempt_failed(
    run_with_a_superseded_attempt,
):
    """The failure mode named in the finding: `pipeline/4_perlector/run.py::main`
    builds its witness-covered region set by filtering `testimonia_of(...)` to
    `outcome == "read"`. Before this fix, a since-superseded `read` attempt for
    this chair would still pass that filter even though the chair's current
    attempt is `failed` — this is the exact filter, exercised directly."""
    context, act_id, chair = run_with_a_superseded_attempt
    testimonia = perlector.testimonia_of(context, act_id, _proposal_regions(context, act_id))
    read_chairs = {
        record["payload"]["chair"] for record in testimonia if record["outcome"] == "read"
    }
    assert chair not in read_chairs


def _forge_attempt(tree, first, act_id, chair, *, sealed_ordinal, payload_ordinal):
    """Publish a second testimonium whose sealed identity and payload ordinal may
    deliberately disagree, and return the forged record."""
    forged = copy.deepcopy(first)
    forged["payload"]["attempt_ordinal"] = payload_ordinal
    forged["payload"]["reported"] = "a reading nobody sealed under this ordinal"
    forged["attempt_id"] = attempt_id(act_id, f"read:{chair}", sealed_ordinal)
    forged["artifact_id"] = artifact_id(ATTESTATORES, "testimonium", act_id, forged["attempt_id"])
    forged["self_hash"] = self_hash(forged)
    path = tree.resolve(f"3_attestatores/artifacts/testimonium/{forged['artifact_id']}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(forged))
    tree.write_manifest(ATTESTATORES)
    return forged


@pytest.fixture
def happy_run(tmp_path):
    root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-root",
            str(root),
            "--run-id",
            "attempt-binding",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "attempt-binding")
    first = next(
        record
        for record in (
            tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
            for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
            if entry["kind"] == "testimonium"
        )
        if record["outcome"] == "read"
    )
    return _Context(tree), first


def test_an_ordinal_its_own_sealed_identity_does_not_bind_cannot_decide_what_is_current(
    happy_run,
):
    """The envelope binds `artifact_id` to an opaque well-formed `attempt_id` and
    stops there: it never re-derives that token from the subject, operation and
    ordinal it is supposed to bind. `attempt_ordinal` sits in the payload, outside
    that derivation, and it is the field that decides which record is current.

    So the one field the whole retention ruling turns on was the one field nothing
    recomputed. Here the sealed identity says attempt 1 and the payload says 3; the
    record is a perfectly valid envelope, and it used to outrank the reading that
    actually happened."""
    context, first = happy_run
    act_id, chair = first["subject_id"], first["payload"]["chair"]
    _forge_attempt(context.tree, first, act_id, chair, sealed_ordinal=1, payload_ordinal=3)

    with pytest.raises(FatalAccounting, match="does not derive from"):
        perlector.testimonia_of(context, act_id, _proposal_regions(context, act_id))


def test_a_manufactured_far_ordinal_cannot_leapfrog_the_attempt_that_happened(happy_run):
    """Deriving the identity honestly for ordinal 99 is three lines with this
    repository's own API, so the derivation check alone does not close it. Attempts
    are append-only and never reused, so their ordinals are the contiguous run
    1..N; a gap is an attempt that is no longer here, and refusing the gap is what
    stops a manufactured attempt 99 sitting beside attempt 1 and winning."""
    context, first = happy_run
    act_id, chair = first["subject_id"], first["payload"]["chair"]
    _forge_attempt(context.tree, first, act_id, chair, sealed_ordinal=99, payload_ordinal=99)

    with pytest.raises(FatalAccounting, match="contiguous run"):
        perlector.testimonia_of(context, act_id, _proposal_regions(context, act_id))


def test_perlector_names_a_testimonium_with_missing_provenance(happy_run):
    context, first = happy_run
    missing = copy.deepcopy(first)
    del missing["payload"]["provenance"]
    missing["self_hash"] = self_hash(missing)
    path = context.tree.resolve(
        context.tree.artifact_path(ATTESTATORES, "testimonium", first["artifact_id"])
    )
    path.write_bytes(canonical_bytes(missing))
    context.tree.write_manifest(ATTESTATORES)

    with pytest.raises(SchemaRefusal, match="model provenance is not an object"):
        perlector.testimonia_of(
            context,
            first["subject_id"],
            _proposal_regions(context, first["subject_id"]),
        )


def test_perlector_refuses_a_missing_witness_before_publishing_any_reading(
    tmp_path, rebind_stage_seal
):
    """A later bad act cannot leave an earlier shortened Perlectio immutable.

    Recensor used to discover this only after Perlector had written the earlier
    act.  Restoring the missing Testimonium then changed the bytes under the
    same Perlectio identity.  The preflight has to cover every requested act
    before the first publication.
    """
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "missing-witness",
                "--scenario",
                "happy",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"

    tree = RunTree(root, "missing-witness")
    missing = next(
        entry
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
        and tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])["payload"][
            "act_key"
        ]
        == "a2"
        and tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])["payload"][
            "chair"
        ]
        == "attestator_3"
    )
    tree.resolve(missing["relative_path"]).unlink()
    tree.write_manifest(ATTESTATORES)
    # The Attestatores seal witnesses the folder it actually left behind, so a
    # rebind is what puts a shortened denominator in front of the Perlector's
    # own witness-floor check rather than in front of the boundary refusal.
    rebind_stage_seal(tree, ATTESTATORES)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/4_perlector/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "missing-witness",
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "shortened witness denominator" in result.stderr
    assert tree.build_manifest(PERLECTOR)["artifacts"] == []


def test_dissent_compares_a_genuinely_empty_witness():
    rows = perlector.dissent_against(
        "visible characters",
        [
            {
                "outcome": "genuinely-empty",
                "payload": {"chair": "attestator_3", "reported": ""},
            }
        ],
    )

    assert rows == [
        {
            "chair": "attestator_3",
            "compared": True,
            "departed": True,
            "departed_raw": True,
            # A witness that read the pixels and found nothing to report departs
            # across the whole reading -- one span covering every character the
            # Perlector established and none of the witness's, which is exactly
            # what "it saw nothing there and we read eighteen characters" means.
            "departures": [
                {
                    "reading_span": {"start": 0, "end": len("visible characters")},
                    "testimonium_span": {"start": 0, "end": 0},
                }
            ],
            "comparison_loss": {"reading_dropped_characters": 0, "witness_dropped_characters": 0},
        }
    ]


def test_an_act_comparison_view_is_sliced_in_the_space_its_span_was_measured_in():
    """F-X3 under the wave's composed span semantics. `witness_span` is stored
    RAW at the one storage point (the attestatores clip-then-translate), so
    the slice comes from the raw report at raw offsets -- and the safety F-X3
    demands (a comparison view a diff may consume without reading markup as
    disagreement) is provided by stripping the SLICE, not by slicing a
    stripped page with offsets from another space.
    """
    page = "<p>SYNTHETIC ACT ONE</p><p>SYNTHETIC ACT TWO</p>"
    stripped = markup_text_view(page)["text"]
    assert stripped == "SYNTHETIC ACT ONESYNTHETIC ACT TWO"

    # Raw spans, as the storage point now records them: each act's slice of
    # the raw page report, tags included in the covered range.
    first = perlector.act_comparison_view(page, {"start": 0, "end": 24})
    second = perlector.act_comparison_view(page, {"start": 24, "end": 48})

    assert first == "SYNTHETIC ACT ONE"
    assert second == "SYNTHETIC ACT TWO"
    # The defect F-X3 closed, restated for the composed contract: the raw
    # slice is never handed onward carrying the markup it cut through.
    assert page[0:24] != first
    for view in (first, second):
        assert "<" not in view and ">" not in view


def test_an_act_comparison_view_past_the_page_reading_is_refused():
    """A span longer than the view it indexes is malformed evidence, not a
    silently truncated slice."""
    with pytest.raises(SchemaRefusal, match="past its own comparison view"):
        perlector.act_comparison_view("<p>alpha</p>", {"start": 0, "end": 99})
