"""Writer and read-back seams enforce the same closed native intake contract."""

import ast
import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.chairs import ChairRegistry
from common.chairs.models import AbsentChair
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


def _is_call_statement(statement: ast.stmt, name: str) -> bool:
    """A bare or assigned call to `name`, which running the block must execute."""
    value = statement.value if isinstance(statement, (ast.Assign, ast.Expr)) else None
    return (
        isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == name
    )


TESTIMONIUM_KINDS = {"testimonium", "page-testimonium"}


def _publish_lines(node: ast.AST) -> list[int]:
    """Only Testimonium writes. An act-attachment carries no adapter presentation.

    A dynamic or non-literal `kind` is counted, so a write this proof cannot
    classify fails loudly rather than slipping past it.
    """
    lines = []
    for child in ast.walk(node):
        if (
            not isinstance(child, ast.Call)
            or not isinstance(child.func, ast.Attribute)
            or child.func.attr != "publish"
        ):
            continue
        kinds = [keyword.value for keyword in child.keywords if keyword.arg == "kind"]
        if len(kinds) != 1 or not isinstance(kinds[0], ast.Constant):
            lines.append(child.lineno)
        elif kinds[0].value in TESTIMONIUM_KINDS:
            lines.append(child.lineno)
    return lines


def _child_blocks(statement: ast.stmt):
    for field in ("body", "orelse", "finalbody"):
        block = getattr(statement, field, None)
        if block:
            yield block
    for handler in getattr(statement, "handlers", []):
        if handler.body:
            yield handler.body


def _undominated_publishes(statements: list[ast.stmt], name: str) -> list[int]:
    """Publish lines this block can reach without first executing a `name` call.

    Dominance is per block, not per function: the page writer validates and
    publishes inside its page/chair loop, which is correct. What must never
    exist is a publish reachable down a path where the reconciliation sits in a
    branch that did not run. (`test_page_join.py` runs the fuller write scan
    over the same tree.)
    """
    validated = False
    undominated: list[int] = []
    for statement in statements:
        if _is_call_statement(statement, name):
            validated = True
            continue
        if not _publish_lines(statement):
            continue
        if validated:
            continue
        blocks = list(_child_blocks(statement))
        if not blocks:
            undominated.extend(_publish_lines(statement))
            continue
        for block in blocks:
            undominated.extend(_undominated_publishes(block, name))
    return undominated


@pytest.mark.parametrize(
    "writer",
    ("publish_attempt", "publish_page_testimonia_and_attachments"),
)
def test_each_testimonium_writer_reconciles_adapter_evidence_before_publication(writer):
    """A later tally refusal cannot undo immutable evidence already published.

    Source order alone was too weak a proof: it is satisfied by a reconciliation
    sitting inside a branch that never runs while the publish below it does.
    What must hold is that no publish is *reachable* without the reconciliation
    having executed first on that path.
    """
    tree = ast.parse(Path(attestatores.__file__).read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == writer
    )

    assert _publish_lines(function), f"{writer} no longer publishes; this proof watches nothing"
    undominated = _undominated_publishes(function.body, "validate_testimonium_presentation")

    assert undominated == [], (
        f"{writer} can reach context.publish at line(s) {undominated} without first "
        "reconciling its adapter-derived presentation; that evidence would be immutable "
        "before anything checked it"
    )


def test_dai_uncertainty_tokens_reach_a_closed_testimonium_verbatim():
    """Uncertainty markers must survive every boundary, not only UTF-8 parsing."""
    adapter = attestatores.witness_adapters.resolve_runnable_adapter("dai.v1")
    raw = "[UNCERTAIN]  ſ [CROSSED_OUT]".encode("utf-8")
    parsed = adapter.parse(raw)
    presented = _base()["presented"]
    observed = adapter.observe(presented, parsed)

    record = attestatores.testimonium_payload(
        chair="attestator_2",
        act_key="a1",
        ordinal=1,
        regions=[],
        provenance={},
        format_capabilities=attestatores.DEFAULT_FORMAT_CAPABILITIES,
        native_payload=parsed,
        witness_reported=None,
        health=attestatores.content_health(parsed, completed=True),
        presented=presented,
        observed=observed,
        outcome="read",
    )

    assert record["payload"] == raw.decode("utf-8")
    # The `reported` compatibility projection is retired: the retained payload
    # is the coverage text directly, so there is no second field restating it.
    assert "reported" not in record
    assert record["observed"][0]["span"] == {"start": 0, "end": len(record["payload"])}


def test_page_image_use_refuses_bytes_swapped_after_artifact_verification():
    original = b"sealed page bytes"
    swapped = b"different page bytes"

    class _SwappedTree:
        def read_bytes(self, _relative_path):
            return swapped

    context = type("Context", (), {"tree": _SwappedTree()})()
    page = {
        "payload": {
            "image_path": "1_exemplar/blobs/sha256/" + digest_bytes(original),
            "source_sha256": digest_bytes(original),
        }
    }

    with pytest.raises(SchemaRefusal, match="changed between artifact verification and image use"):
        attestatores._verified_page_bytes(context, page)


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
    """Writer and consumer seams both exclude recovery crops from witness basis."""
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
    """Region-ref tests cannot use DAI's distinct ``adapter-crop`` shape."""
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        if record["payload"]["presented"]["kind"] == "region":
            return record
    raise AssertionError("the fixture has no region-kind Testimonium")


def test_a_continuation_act_states_which_of_its_crops_the_derived_layer_omits(tmp_path):
    """One page-space presentation must name a continuation crop it cannot describe."""
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
            assert presented["kind"] == "adapter-crop"
            assert "region_ref" not in presented
    assert single != continuation


def test_page_native_geometry_stays_with_page_witnesses_and_inside_witness_views(tmp_path):
    """Native page-space geometry may ride only records owned by a page witness.

    Unit 10C's coverage design lets a page witness's act view restate its
    page-space geometry (boxes may exceed that record's one-crop presentation);
    every other record's observed boxes must stay inside the exact presentation
    the witness was shown, and no act-scoped chair may carry native geometry.
    """
    tree = _happy_run(tmp_path, "native-page-scope")
    native = []
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] not in {"testimonium", "page-testimonium"}:
            continue
        record = tree.read_artifact(ATTESTATORES, entry["kind"], entry["artifact_id"])
        payload = record["payload"]
        presented = payload["presented"]
        page_witness_view = entry["kind"] == "testimonium" and payload.get("page_witness") is True
        for observation in payload["observed"]:
            if observation["bounds_source"] == "native":
                native.append((entry["kind"], payload["chair"]))
                assert entry["kind"] == "page-testimonium" or page_witness_view
            if presented and not page_witness_view:
                outer = presented["transform"]["bounds"]
                inner = observation["bounds"]
                assert outer["x"] <= inner["x"]
                assert outer["y"] <= inner["y"]
                assert outer["x"] + outer["w"] >= inner["x"] + inner["w"]
                assert outer["y"] + outer["h"] >= inner["y"] + inner["h"]
    assert ("page-testimonium", "attestator_1") in native
    assert all(chair in {"attestator_1", "attestator_3"} for _kind, chair in native)


def test_a_page_presentation_naming_another_page_s_blob_is_refused_at_the_tally_seam(tmp_path):
    """A real digest-bound page blob cannot stand in for a different named page."""
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
    happened"; attempted testimony must carry its receipt (GOVERNANCE 6)."""
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


@pytest.mark.parametrize(
    ("region", "message"),
    (
        ({}, "no payload and page-space transform"),
        ({"payload": "not-an-object"}, "no payload and page-space transform"),
        (
            {"payload": {"region_id": "r1", "image_path": "p", "image_sha256": "s"}},
            "no payload and page-space transform",
        ),
        (
            {
                "payload": {
                    "region_id": "r1",
                    "image_path": "p",
                    "transform": {"source_page_id": "page-1", "source_page_ordinal": 1},
                }
            },
            r"lacks the field\(s\) \['image_sha256'\]",
        ),
        (
            {
                "payload": {
                    "region_id": "r1",
                    "image_path": "p",
                    "image_sha256": "s",
                    "transform": {"source_page_id": "page-1"},
                }
            },
            r"lacks the field\(s\) \['source_page_ordinal'\]",
        ),
    ),
)
def test_a_sealed_region_missing_its_presentation_fields_is_named_not_indexed(region, message):
    """`validate_testimonium_presentation` treats manifest regions as untrusted.

    It reads them straight out of the Designator manifest to reconcile a record,
    without the crop-lineage verification the writer's caller performs. A raw
    KeyError there would be the validation seam failing to say what is wrong
    with the evidence it was asked to judge.
    """
    with pytest.raises(SchemaRefusal, match=message):
        attestatores.presentation_for_region(region)


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
    """Digest-bound pixels cannot supply a forged region identity."""
    tree = _happy_run(tmp_path, "unknown-region-ref")
    context = _Context(tree)
    testimony = _region_testimonium(tree)
    forged = copy.deepcopy(testimony)
    forged["payload"]["presented"]["region_ref"] = {"region_id": "rgn_" + "0" * 16}
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="no unique sealed Designator region"):
        attestatores.validate_testimonium_presentation(context, forged)


def test_a_region_ref_matching_two_manifest_rows_is_not_treated_as_unique(tmp_path, monkeypatch):
    """A region identity must resolve to exactly one sealed manifest row."""
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
    """Quantization belongs beside native adapter data, never in derived facts."""
    for mutate in (
        lambda payload: payload.update({"quantization": "round-half-up"}),
        lambda payload: payload["presented"].update({"quantization": "round-half-up"}),
        lambda payload: payload["observed"][0].update({"quantization": "round-half-up"}),
    ):
        payload = _base()
        mutate(payload)
        with pytest.raises(SchemaRefusal, match="closed|unknown field"):
            attestatores.validate_testimonium_payload(payload)


# ------------------- the two fields only a live reading writes ----------------
#
# `serving_call_ref` names the `chair-call-record.v1` blob for the one request
# this attempt came from, and `native_capture` is the adapter's own retained
# model view of the response (SPEC_A section 2.3). Both are optional and are
# written only by the live pass, so a fixture Testimonium is byte-for-byte what
# it always was; what follows closes them at the writer, which is the same
# validator the tally read-back uses.

_BLOB_PREFIX = "3_attestatores/blobs/sha256/"


def _blob_ref(seed: str) -> dict[str, str]:
    digest = digest_bytes(seed.encode("utf-8"))
    return {"relative_path": _BLOB_PREFIX + digest, "sha256": digest}


def _live_capture(reference: dict[str, str], *, stop: str = "stop") -> dict[str, object]:
    return {
        "schema": "attestatores-model-view.v1",
        "adapter": "chandra.v1",
        "view": {"prompt": {"instruction": "read"}},
        "raw_response_ref": reference,
        "transport_stop_reason": stop,
        "stop_reason": stop,
        "findings": [],
        "parse": {"state": "parsed", "parser": "json", "text": "native bytes remain elsewhere"},
    }


def test_a_live_act_record_may_name_its_retained_response_call_and_model_view():
    payload = _base()
    reference = _blob_ref("live response bytes")
    payload["raw_response_ref"] = reference
    payload["raw_response_kind"] = "model-output"
    payload["serving_call_ref"] = _blob_ref("call record bytes")
    payload["native_capture"] = _live_capture(reference)
    assert attestatores.validate_testimonium_payload(payload) is payload


def test_a_serving_call_reference_without_a_retained_response_is_refused():
    """A request with no retained answer is not evidence of a reading."""
    payload = _base()
    payload["serving_call_ref"] = _blob_ref("call record bytes")
    with pytest.raises(SchemaRefusal, match="retains no response"):
        attestatores.validate_testimonium_payload(payload)


def test_a_retained_model_view_naming_another_response_is_refused():
    """One attempt reads one response, and both references must say the same one."""
    payload = _base()
    payload["raw_response_ref"] = _blob_ref("live response bytes")
    payload["raw_response_kind"] = "model-output"
    payload["serving_call_ref"] = _blob_ref("call record bytes")
    payload["native_capture"] = _live_capture(_blob_ref("some other response entirely"))
    with pytest.raises(SchemaRefusal, match="different response blob"):
        attestatores.validate_testimonium_payload(payload)


def test_a_serving_call_reference_outside_this_stage_s_blob_store_is_refused():
    payload = _base()
    payload["raw_response_ref"] = _blob_ref("live response bytes")
    payload["serving_call_ref"] = {
        "relative_path": "4_perlector/blobs/sha256/" + "0" * 64,
        "sha256": "0" * 64,
    }
    with pytest.raises(SchemaRefusal, match="serving_call_ref is not an Attestatores blob"):
        attestatores.validate_testimonium_payload(payload)


def test_a_malformed_retained_model_view_is_refused_at_the_act_writer_too():
    """The shared capture contract closes an act record, not only a page record.

    The stop word is deliberately not what this proves: the shared validator
    checks that only for `churro.v1` (`_validate_churro_capture`), so the live
    boundary refuses an unreadable engine word itself, before publication
    (`run.py::refuse_unpublishable_stop_word`, proven in
    `test_attestatores_live_pass.py`). What this closes here is that a view
    which is not a retained model view at all cannot ride into an act record
    unexamined.
    """
    payload = _base()
    reference = _blob_ref("live response bytes")
    payload["raw_response_ref"] = reference
    payload["native_capture"] = {**_live_capture(reference), "schema": "not-a-model-view.v9"}
    with pytest.raises(SchemaRefusal, match="retained model-view schema"):
        attestatores.validate_testimonium_payload(payload)


# ------------- the serving moment a live provenance record names --------------


class _ProvenanceContext:
    """Just enough `StageContext` for `provenance_for`: it writes one receipt."""

    def __init__(self) -> None:
        self.adapter_revision = "fake-attestatores-v0"
        self.written: list[str] = []

    def write_serving_receipt(self, identity, details):
        self.written.append(identity.role)
        return {"relative_path": "receipts/" + "a" * 64 + ".json", "sha256": "a" * 64}


def _identity(role: str = "attestator_1"):
    return ChairRegistry.from_toml(str(ROOT / "config" / "models.toml")).resolve(role)


def test_a_live_provenance_record_names_the_receipt_the_chair_already_published():
    """The live pass never writes a second, declared receipt over a real one."""
    context = _ProvenanceContext()
    live_receipt = {"relative_path": "receipts/" + "b" * 64 + ".json", "sha256": "b" * 64}
    provenance = attestatores.provenance_for(
        context, _identity(), attempted=True, receipt_ref=live_receipt
    )
    assert provenance["receipt_ref"] == live_receipt
    assert context.written == []
    # The fixture posture is unchanged: no reference in, one declared receipt out.
    fixture = attestatores.provenance_for(context, _identity(), attempted=True)
    assert context.written == ["attestator_1"]
    assert fixture["receipt_ref"]["sha256"] == "a" * 64


def test_a_chair_that_was_never_asked_cannot_carry_a_serving_receipt():
    context = _ProvenanceContext()
    with pytest.raises(ContractError, match="never made"):
        attestatores.provenance_for(
            context,
            _identity(),
            attempted=False,
            receipt_ref={"relative_path": "receipts/x.json", "sha256": "c" * 64},
        )


def test_an_absent_chair_cannot_carry_a_serving_receipt():
    context = _ProvenanceContext()
    absent = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml")).config.chairs[
        "secondary_proposer"
    ]
    assert isinstance(absent, AbsentChair), (
        "this test needs an absent roster entry; secondary_proposer is now configured "
        "in config/models.toml, so provenance_for would fall through to the "
        "configured-return path instead of raising -- pick a different absent entry "
        "or re-derive this test's fixture"
    )
    with pytest.raises(ContractError, match="absent"):
        attestatores.provenance_for(
            context,
            absent,
            attempted=True,
            receipt_ref={"relative_path": "receipts/x.json", "sha256": "c" * 64},
        )
