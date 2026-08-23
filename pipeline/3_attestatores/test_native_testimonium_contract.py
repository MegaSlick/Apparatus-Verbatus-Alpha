"""Writer-side closure and reread rule tests for the native intake contract."""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.chairs import ChairRegistry
from common.contracts.canonical import digest_bytes, self_hash
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR
from common.imaging import dimensions
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def _load_attestatores():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("attestatores_native_contract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attestatores = _load_attestatores()


def _base(*, page=False):
    payload = {
        "chair": "attestator_1",
        "act_key": "a1",
        "attempt_ordinal": 1,
        "regions": [],
        "provenance": {},
        "format_capabilities": {},
        "payload": "native bytes remain elsewhere",
        "witness_reported": None,
        "content_health": {},
        "unpresented_regions": [],
        "presented": {
            "kind": "page",
            "source_page_id": "page-1",
            "source_page_ordinal": 1,
            "image_path": "1_exemplar/blobs/sha256/" + "0" * 64,
            "image_sha256": "0" * 64,
            "transform": {
                "operation": "whole",
                "source_page_id": "page-1",
                "source_page_ordinal": 1,
                "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
            },
        },
        "observed": [
            {
                "ordinal": 0,
                "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
                "bounds_source": "presented",
                "span": None,
            }
        ],
    }
    if page:
        payload.update(
            {
                "scope": "page",
                "page_ordinal": 1,
                "page_role": "primary",
                "unjoined_act_attempts": [],
            }
        )
    return payload


def test_unknown_field_is_refused_at_both_act_and_page_writer_validators():
    act = _base()
    act["untrusted"] = True
    with pytest.raises(SchemaRefusal, match="closed"):
        attestatores.validate_testimonium_payload(act)
    page = _base(page=True)
    page["untrusted"] = True
    with pytest.raises(SchemaRefusal, match="closed"):
        attestatores.validate_page_testimonium_payload(page)


def test_rederived_reread_rule_names_new_ink_as_a_recovery_request():
    with pytest.raises(ContractError, match="New testimony after a reading is refused; new INK"):
        attestatores.require_open_witness_layer(
            frozenset({"act-1"}), {"act_id": "act-1", "act_key": "a1"}, "a reread"
        )


def _attempt(outcome):
    return attestatores.Attempt(
        outcome=outcome,
        native_payload=None,
        witness_reported=None,
        format_capabilities=None,
        health={},
        reason="fixture" if outcome != "read" else None,
    )


def test_a_page_witness_shown_pixels_that_all_come_back_failed_is_still_attempted():
    """A page chair attempted (shown pixels) on every act but every response was
    unusable must not collapse into the same `presented: {}` fact as a chair
    never shown an image at all -- those are held acts, refused pages, and
    absent chairs, not attempted-and-failed (GOVERNANCE 2)."""
    acts = [{"act_id": "a1"}, {"act_id": "a2"}]
    attempts_by_pair = {
        ("a1", "attestator_1"): _attempt("failed"),
        ("a2", "attestator_1"): _attempt("failed"),
    }
    assert attestatores.page_witness_attempted(acts, "attestator_1", attempts_by_pair) is True


def test_a_page_witness_never_run_on_any_act_is_not_attempted():
    acts = [{"act_id": "a1"}, {"act_id": "a2"}]
    attempts_by_pair = {
        ("a1", "attestator_1"): _attempt("not-run"),
        ("a2", "attestator_1"): _attempt("dead"),
    }
    assert attestatores.page_witness_attempted(acts, "attestator_1", attempts_by_pair) is False


def test_a_page_witness_with_one_failed_and_one_unread_act_is_still_attempted():
    acts = [{"act_id": "a1"}, {"act_id": "a2"}]
    attempts_by_pair = {
        ("a1", "attestator_1"): _attempt("not-run"),
        ("a2", "attestator_1"): _attempt("failed"),
    }
    assert attestatores.page_witness_attempted(acts, "attestator_1", attempts_by_pair) is True


class _Context:
    def __init__(self, tree):
        self.tree = tree
        self.run = tree.read_run()
        self.registry = ChairRegistry.from_toml(ROOT / "config/models.toml")

    def input_ref(self, relative_path):
        return {
            "relative_path": relative_path,
            "sha256": digest_bytes(self.tree.read_bytes(relative_path)),
        }


def test_a_resealed_tally_record_cannot_retroactively_claim_a_recovery_crop(tmp_path):
    """The same wall `pipeline/4_perlector/run.py::validate_testimonium_regions`
    enforces must also hold at the writer/tally seam
    (`validate_testimonium_presentation`), which is the validator this slice
    added and which a Testimonium's own tally read-back actually calls."""
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "review",
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "witness-boundary-writer",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, result.stderr
    tree = RunTree(tmp_path / "runs", "witness-boundary-writer")
    context = _Context(tree)
    recovery = next(
        tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region"
        and tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])["payload"]["origin"]
        == "recovery"
    )
    act_id = recovery["subject_id"]
    testimony = next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium" and entry["subject_id"] == act_id
    )
    forged = copy.deepcopy(testimony)
    transform = recovery["payload"]["transform"]
    forged["payload"]["presented"] = {
        "kind": "region",
        "source_page_id": transform["source_page_id"],
        "source_page_ordinal": transform["source_page_ordinal"],
        "image_path": recovery["payload"]["image_path"],
        "image_sha256": recovery["payload"]["image_sha256"],
        "transform": transform,
        "region_ref": {"region_id": recovery["payload"]["region_id"]},
    }
    forged["payload"]["observed"] = [
        {
            "ordinal": 0,
            "bounds": transform["bounds"],
            "bounds_source": "presented",
            "span": None,
        }
    ]
    forged["inputs"] = [context.input_ref(recovery["payload"]["image_path"])]
    forged["self_hash"] = self_hash(forged)

    with pytest.raises(SchemaRefusal, match="recovery region cannot be presented"):
        attestatores.validate_testimonium_presentation(context, forged)


def test_unpresented_regions_must_be_a_unique_list_of_region_ids():
    for bad in ("rgn_1", [""], ["rgn_1", "rgn_1"], [1]):
        payload = _base()
        payload["unpresented_regions"] = bad
        with pytest.raises(SchemaRefusal, match="unique list of region ids"):
            attestatores.validate_testimonium_payload(payload)


def test_a_record_with_no_presentation_at_all_cannot_name_an_unpresented_region():
    """`presented: {}` is a chair that was never shown an image. Naming a crop
    its presentation does not speak for would claim a presentation exists."""
    payload = _base()
    payload["presented"] = {}
    payload["observed"] = []
    payload["unpresented_regions"] = ["rgn_0123456789abcdef"]
    with pytest.raises(SchemaRefusal, match="cannot name regions"):
        attestatores.validate_testimonium_payload(payload)


def _happy_run(tmp_path, run_id):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            run_id,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return RunTree(tmp_path / "runs", run_id)


def _region_testimonium(tree):
    """Return a fixture Testimonium that presents a sealed Designator region.

    DAI's independently re-derived presentation is deliberately an
    ``adapter-crop``.  The tests below exercise the distinct 10B wall that
    applies only when a record claims ``kind == \"region\"``.
    """
    return next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
        and tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])["payload"][
            "presented"
        ]["kind"]
        == "region"
    )


def test_a_continuation_act_states_which_of_its_crops_the_derived_layer_omits(tmp_path):
    """0C ruled a continuation's second page is evidence. Its crop is bound by
    digest in `regions`/`inputs`, but `presented` binds one image recipe and
    `observed` boxes live in that one page's pixel space, so the far-side ink has
    no derived geometry in this record and cannot acquire any. Every chair's
    Testimonium for the continuation act therefore names the crop its
    presentation does not speak for, live in the pinned happy run."""
    tree = _happy_run(tmp_path, "continuation-scope")
    regions = [
        tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region"
    ]
    by_act: dict[str, list] = {}
    for region in regions:
        by_act.setdefault(region["subject_id"], []).append(region)
    continuation = next(act_id for act_id, rows in by_act.items() if len(rows) == 2)
    single = next(act_id for act_id, rows in by_act.items() if len(rows) == 1)
    second_crop = sorted(
        by_act[continuation], key=lambda region: region["payload"]["attempt_ordinal"]
    )[1]["payload"]["region_id"]

    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        expected = [second_crop] if entry["subject_id"] == continuation else []
        assert record["payload"]["unpresented_regions"] == expected, entry["artifact_id"]
        presented = record["payload"]["presented"]
        if presented["kind"] == "region":
            assert presented["region_ref"]["region_id"] != second_crop
        else:
            # An adapter crop is a different image kind; it must never borrow
            # the region-only identity field to make this assertion pass.
            assert presented["kind"] == "adapter-crop"
            assert "region_ref" not in presented
    assert single != continuation


def test_a_page_presentation_naming_another_page_s_blob_is_refused_at_the_tally_seam(tmp_path):
    """The wall wired into `validate_testimonium_presentation`: a self-consistent
    record whose presented blob is a real, digest-bound sealed page -- the wrong
    one. Its boxes would be validated against the page it names and read as that
    page's geometry."""
    tree = _happy_run(tmp_path, "page-blob-forgery")
    context = _Context(tree)
    pages = [
        tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
    ]
    first, second = sorted(pages, key=lambda page: page["payload"]["ordinal"])[:2]
    testimony = next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
    )
    forged = copy.deepcopy(testimony)
    width, height = dimensions(tree.read_bytes(second["payload"]["image_path"]))
    forged["payload"]["presented"] = {
        "kind": "page",
        "source_page_id": first["subject_id"],
        "source_page_ordinal": first["payload"]["ordinal"],
        "image_path": second["payload"]["image_path"],
        "image_sha256": second["payload"]["source_sha256"],
        "transform": {
            "operation": "whole",
            "source_page_id": first["subject_id"],
            "source_page_ordinal": first["payload"]["ordinal"],
            "bounds": {"x": 0, "y": 0, "w": width, "h": height},
        },
    }
    forged["payload"]["observed"] = []
    forged["payload"]["unpresented_regions"] = []
    forged["inputs"] = [context.input_ref(second["payload"]["image_path"])]
    forged["self_hash"] = self_hash(forged)

    with pytest.raises(SchemaRefusal, match="not the sealed page it claims"):
        attestatores.validate_testimonium_presentation(context, forged)


def test_a_page_witness_shown_pixels_carries_the_serving_moment_that_produced_them(tmp_path):
    """One record may not say both "I was shown this image" and "no serving
    happened". The review fixture has the case live: attestator_3's page-2
    Testimonium, whose sole contributing act fails for that chair, is attempted
    (Sonnet's finding 1) and so must carry a receipt (GOVERNANCE 6), exactly as
    the act arm and the Armarium's own read-back already require."""
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "review",
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "page-serving-moment",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, result.stderr
    tree = RunTree(tmp_path / "runs", "page-serving-moment")
    seen_failed_but_presented = False
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "page-testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        payload = record["payload"]
        if payload["provenance"]["chair_state"] != "configured":
            continue
        assert bool(payload["presented"]) == (payload["provenance"]["receipt_ref"] is not None), (
            entry["artifact_id"]
        )
        if record["outcome"] == "failed" and payload["presented"]:
            seen_failed_but_presented = True
    assert seen_failed_but_presented, "the review fixture no longer exercises the case"


def test_a_never_presented_page_witness_is_not_run_and_carries_no_receipt(tmp_path):
    """The absence arm stays distinct from attempted failure at page scope."""
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "ink-free-page-unwitnessed",
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "page-never-presented",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, result.stderr
    tree = RunTree(tmp_path / "runs", "page-never-presented")
    records = [
        tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
        and tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])["payload"][
            "page_ordinal"
        ]
        == 3
    ]
    assert records
    for record in records:
        assert record["outcome"] == "not-run"
        assert record["payload"]["presented"] == {}
        assert record["payload"]["provenance"]["receipt_ref"] is None


def test_a_region_ref_naming_no_sealed_designator_region_is_refused(tmp_path):
    """The branch Sonnet named as logically sound but untested: a presentation
    whose `region_id` matches nothing sealed. Everything else about the record
    stays true, including its digest-bound pixels, so nothing but this lookup
    stands between a forged region identity and a witness basis."""
    tree = _happy_run(tmp_path, "unknown-region-ref")
    context = _Context(tree)
    testimony = _region_testimonium(tree)
    forged = copy.deepcopy(testimony)
    forged["payload"]["presented"]["region_ref"] = {"region_id": "rgn_" + "0" * 16}
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="no unique sealed Designator region"):
        attestatores.validate_testimonium_presentation(context, forged)


def test_a_region_ref_matching_two_manifest_rows_is_not_treated_as_unique(tmp_path, monkeypatch):
    """Exercise the other arm of the same physical-identity lookup refusal."""
    tree = _happy_run(tmp_path, "duplicate-region-ref")
    context = _Context(tree)
    testimony = _region_testimonium(tree)
    original = tree.build_manifest
    designator_manifest = original(DESIGNATOR)
    matching = next(
        entry
        for entry in designator_manifest["artifacts"]
        if entry["kind"] == "region"
        and tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])["payload"]["region_id"]
        == testimony["payload"]["presented"]["region_ref"]["region_id"]
    )

    def duplicated_manifest(stage):
        manifest = original(stage)
        if stage == DESIGNATOR:
            return {**manifest, "artifacts": [*manifest["artifacts"], matching]}
        return manifest

    monkeypatch.setattr(tree, "build_manifest", duplicated_manifest)
    with pytest.raises(SchemaRefusal, match="no unique sealed Designator region"):
        attestatores.validate_testimonium_presentation(context, testimony)


def test_a_declared_quantization_rule_has_nowhere_to_ride_in_this_contract():
    """Consult §3 puts declared quantization in the ADAPTER, beside the raw
    digest, never in the derived waist -- so a Testimonium cannot declare a
    quantization rule at all, and the "declared in one arm, applied in the other"
    forgery has no field to inhabit. Executable rather than asserted: the closed
    schemas refuse the key at the payload, the presentation, and the observation."""
    for mutate in (
        lambda payload: payload.update({"quantization": "round-half-up"}),
        lambda payload: payload["presented"].update({"quantization": "round-half-up"}),
        lambda payload: payload["observed"][0].update({"quantization": "round-half-up"}),
    ):
        payload = _base()
        mutate(payload)
        with pytest.raises(SchemaRefusal, match="closed|unknown field"):
            attestatores.validate_testimonium_payload(payload)
