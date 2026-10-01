"""Writer and read-back seams enforce the same closed native intake contract."""

import ast
import copy
from pathlib import Path

import pytest

from common.chairs import ChairRegistry
from common.contracts.canonical import digest_bytes, self_hash
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR
from common.imaging import dimensions
from common.native_witness import record_presentations, unpresented_region_ids
from common.runtree.store import RunTree
from conftest import load_stage, run_stage, run_through

ROOT = Path(__file__).resolve().parents[2]
ATTESTATORES_PROGRAM = "pipeline/3_attestatores/run.py"


attestatores = load_stage("3_attestatores")


def _base():
    return {
        "chair": "attestator_1",
        "act_key": "page-1",
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
        "scope": "page",
        "page_ordinal": 1,
        "page_role": "primary",
        "unjoined_act_attempts": [],
    }


def test_unknown_field_is_refused_at_the_page_writer_validator():
    page = _base()
    page["untrusted"] = True
    with pytest.raises(SchemaRefusal, match="closed"):
        attestatores.validate_page_testimonium_payload(page)


def _page_records(tree, ordinal=None):
    return [
        record
        for record in (
            tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
            for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
            if entry["kind"] == "page-testimonium"
        )
        if ordinal is None or record["payload"]["attempt_ordinal"] == ordinal
    ]


def test_a_page_the_perlector_was_shown_closes_its_witness_layer(tmp_path):
    """A reading is established over the testimony it was shown; a new attempt
    there would supersede it, so a pass that would append one is refused before
    anything is written."""
    root = tmp_path / "runs"
    run_through(root, "closed-layer", "happy", "perlector")
    tree = RunTree(root, "closed-layer")
    before = len(_page_records(tree))

    result = run_stage(root, "closed-layer", "happy", ATTESTATORES_PROGRAM, attempt_ordinal=2)

    assert result.returncode == attestatores.EXIT_HELD, result.stderr
    assert "witness layer is closed: a whole pass at ordinal 2" in result.stderr
    assert "to witness these pages again, start a new run" in result.stderr
    assert len(_page_records(tree)) == before
    assert _page_records(tree, ordinal=2) == []


def _is_call_statement(statement: ast.stmt, name: str) -> bool:
    """A bare or assigned call to `name`, which running the block must execute."""
    value = statement.value if isinstance(statement, (ast.Assign, ast.Expr)) else None
    return (
        isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == name
    )


def _publish_lines(node: ast.AST) -> list[int]:
    """Only page Testimonium writes.

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
        elif kinds[0].value == "page-testimonium":
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

    What must never exist is a publish reachable down a path where the
    reconciliation sits in a branch that did not run.
    (`test_page_witness_roster.py` runs the fuller write scan over the same tree.)
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
    ("publish_page_testimonium", "publish_detector_page_testimonium"),
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
    adapter = attestatores.witness_adapters.resolve_runnable_adapter("dai.v1")
    raw = "[UNCERTAIN]  ſ [CROSSED_OUT]".encode("utf-8")
    parsed = adapter.parse(raw)
    presented = _base()["presented"]
    observed = adapter.observe(presented, parsed)

    record = attestatores.page_testimonium_payload(
        chair="attestator_2",
        page_ordinal=1,
        ordinal=1,
        provenance={},
        attempt=attestatores.Attempt(
            "read",
            parsed,
            None,
            attestatores.DEFAULT_FORMAT_CAPABILITIES,
            attestatores.content_health(parsed, completed=True),
            None,
        ),
        presented=presented,
        observed=observed,
        unpresented_regions=[],
        testimonium_id="art_0123456789abcdef",
    )

    assert record["payload"] == raw.decode("utf-8")
    # The retained payload is the coverage text directly, so there is no second
    # field restating it.
    assert "reported" not in record
    assert record["observed"][0]["span"] == {"start": 0, "end": len(record["payload"])}


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


def test_unpresented_regions_must_be_a_unique_list_of_region_ids():
    for bad in ("rgn_1", [""], ["rgn_1", "rgn_1"], [1]):
        payload = _base()
        payload["unpresented_regions"] = bad
        with pytest.raises(SchemaRefusal, match="unique list of region ids"):
            attestatores.validate_page_testimonium_payload(payload)


def test_a_record_with_no_presentation_at_all_cannot_name_an_unpresented_region():
    """`presented: {}` is a chair that was never shown an image. Naming a crop
    its presentation does not speak for would claim a presentation exists."""
    payload = _base()
    payload["presented"] = {}
    payload["observed"] = []
    payload["unpresented_regions"] = ["rgn_0123456789abcdef"]
    with pytest.raises(SchemaRefusal, match="cannot name regions"):
        attestatores.validate_page_testimonium_payload(payload)


def _witnessed(tmp_path, run_id, scenario="happy"):
    run_through(tmp_path / "runs", run_id, scenario, "attestatores")
    return RunTree(tmp_path / "runs", run_id)


def test_every_page_record_names_exactly_the_proposals_it_was_not_shown(tmp_path):
    tree = _witnessed(tmp_path, "unpresented-scope")
    proposals = [
        tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region"
    ]
    named = set()
    for record in _page_records(tree):
        on_page = [
            region
            for region in proposals
            if region["payload"]["origin"] == "proposal"
            and region["payload"]["transform"]["source_page_id"] == record["subject_id"]
        ]
        payload = record["payload"]
        assert payload["unpresented_regions"] == unpresented_region_ids(
            record_presentations(payload), on_page
        ), record["artifact_id"]
        named.update(payload["unpresented_regions"])
    # A whole page shows every proposal on it; DAI's record crops show some.
    assert named <= {region["payload"]["region_id"] for region in proposals}


def test_page_native_geometry_stays_with_the_chairs_that_report_it(tmp_path):
    """Native page-space geometry rides only the records of a chair that reports
    it (Chandra) or that the fixture declares a box for (Churro's one row);
    every other box stays inside one image the chair was shown."""
    tree = _witnessed(tmp_path, "native-page-scope")
    native = []
    for record in _page_records(tree):
        payload = record["payload"]
        shown = record_presentations(payload)
        for observation in payload["observed"]:
            if observation["bounds_source"] == "native":
                native.append(payload["chair"])
                continue
            inner = observation["bounds"]
            assert any(
                outer["x"] <= inner["x"]
                and outer["y"] <= inner["y"]
                and outer["x"] + outer["w"] >= inner["x"] + inner["w"]
                and outer["y"] + outer["h"] >= inner["y"] + inner["h"]
                for outer in (image["transform"]["bounds"] for image in shown)
            ), record["artifact_id"]
    assert "attestator_1" in native
    assert set(native) <= {"attestator_1", "attestator_3"}


def test_a_page_presentation_naming_another_page_s_blob_is_refused_at_the_tally_seam(tmp_path):
    tree = _witnessed(tmp_path, "page-blob-forgery")
    context = _Context(tree)
    pages = [
        tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
    ]
    first, second = sorted(pages, key=lambda page: page["payload"]["ordinal"])[:2]
    testimony = next(
        record for record in _page_records(tree) if record["payload"]["chair"] == "attestator_3"
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
    happened"; attempted testimony must carry its receipt."""
    tree = _witnessed(tmp_path, "page-serving-moment", "malformed-witness")
    seen_failed_but_presented = False
    for record in _page_records(tree):
        payload = record["payload"]
        assert bool(payload["presented"]) == (payload["provenance"]["receipt_ref"] is not None), (
            record["artifact_id"]
        )
        if record["outcome"] == "failed" and payload["presented"]:
            seen_failed_but_presented = True
    assert seen_failed_but_presented, "the malformed-witness fixture no longer exercises the case"


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
    """A record crop shown to DAI is untrusted until its presentation is built:
    a raw KeyError would be the seam failing to say what is wrong with it."""
    with pytest.raises(SchemaRefusal, match=message):
        attestatores.presentation_for_region(region)


def test_a_never_presented_page_witness_is_not_run_and_carries_no_receipt(tmp_path):
    tree = _witnessed(tmp_path, "page-never-presented", "ink-free-page-unwitnessed")
    records = [record for record in _page_records(tree) if record["payload"]["page_ordinal"] == 3]
    assert records
    for record in records:
        # DAI's own detector found nothing there, which is its blank testimony.
        expected = "genuinely-empty" if record["payload"]["chair"] == "attestator_2" else "not-run"
        assert record["outcome"] == expected
        assert record["payload"]["presented"] == {}
        assert record["payload"]["provenance"]["receipt_ref"] is None


def test_a_declared_quantization_rule_has_nowhere_to_ride_in_this_contract():
    for mutate in (
        lambda payload: payload.update({"quantization": "round-half-up"}),
        lambda payload: payload["presented"].update({"quantization": "round-half-up"}),
        lambda payload: payload["observed"][0].update({"quantization": "round-half-up"}),
    ):
        payload = _base()
        mutate(payload)
        with pytest.raises(SchemaRefusal, match="closed|unknown field"):
            attestatores.validate_page_testimonium_payload(payload)


_BLOB_PREFIX = "3_attestatores/blobs/sha256/"


def _blob_ref(seed: str) -> dict[str, str]:
    digest = digest_bytes(seed.encode("utf-8"))
    return {"relative_path": _BLOB_PREFIX + digest, "sha256": digest}


def test_a_malformed_retained_model_view_is_refused_at_the_page_writer():
    """A view which is not a retained model view at all cannot ride into a page
    record unexamined. The stop word is not what this proves: the live boundary
    refuses an unreadable engine word itself, before publication
    (`run.py::refuse_unpublishable_stop_word`)."""
    payload = _base()
    reference = _blob_ref("live response bytes")
    payload["native_capture"] = {
        "schema": "not-a-model-view.v9",
        "adapter": "chandra.v1",
        "view": {"prompt": {"instruction": "read"}},
        "raw_response_ref": reference,
        "transport_stop_reason": "stop",
        "stop_reason": "stop",
        "findings": [],
        "parse": {"state": "parsed", "parser": "json", "text": "native bytes remain elsewhere"},
    }
    with pytest.raises(SchemaRefusal, match="retained model-view schema"):
        attestatores.validate_page_testimonium_payload(payload)


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
