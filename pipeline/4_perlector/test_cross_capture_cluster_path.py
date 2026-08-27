"""The two-capture cluster path from correspondence through dissent.

The canonical orchestrator fixtures intentionally remain image-local.  This
This separate fixture proves a physical act represented by two
captures reaches one atomic establishing call.  Its reader inspects the
delivered crop bytes; green text therefore cannot be produced while silently
dropping either active capture.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sqlite3
import sys
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataclasses import replace  # noqa: E402

from combined import run_logical_passes  # noqa: E402

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of  # noqa: E402
from common.contracts.errors import SchemaRefusal  # noqa: E402
from common.contracts.identities import act_id, physical_page_id  # noqa: E402
from common.contracts.outcomes import ArmariumCategory, run_aggregate  # noqa: E402
from common.corpus_register import (  # noqa: E402
    append_records,
    empty_register,
    membership_heads,
    register_digest,
)
from common.cross_capture_autopsia import (  # noqa: E402
    build_autopsia,
    dissent_shell,
)
from common.cross_capture_coverage import (  # noqa: E402
    build_cross_capture_coverage,
    capture_specific_recovery,
)
from common.cross_capture_dissent import (  # noqa: E402
    build_cross_capture_dissent,
    unit20_dissent_input,
)
from common.imaging import encode_grayscale_png  # noqa: E402
from common.physical_act_partition import (  # noqa: E402
    append_correspondence_proposal,
    build_correspondence_proposal,
    build_physical_act_partition,
)

FIXTURE = Path(__file__).with_name("fixtures") / "two-capture-leaf-cluster.json"
ARCHETYPUS_RUN = ROOT / "pipeline" / "6_archetypus" / "run.py"
ARMARIUM_RUN = ROOT / "pipeline" / "7_armarium" / "run.py"
ARMARIUM_DIR = ARMARIUM_RUN.parent
ARMARIUM_EXPORT = ARMARIUM_DIR / "armarium_export.py"


def _module(name: str, path: Path):
    if str(ARMARIUM_DIR) not in sys.path:
        sys.path.insert(0, str(ARMARIUM_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _ref(path: str, content: str) -> dict[str, str]:
    return {"relative_path": path, "sha256": digest_bytes(content.encode())}


def _local_rows(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for capture in fixture["captures"]:
        local_id = act_id(capture["page_id"], "proposal", capture["bounds"])
        rows.append(
            {
                "act_id": local_id,
                "act_key": capture["act_key"],
                "act_class": "proposal",
                "act_bounds": dict(capture["bounds"]),
                "page_id": capture["page_id"],
                "page_ordinal": capture["page_ordinal"],
                "source_sha256": capture["source_sha256"],
                "proposal_refs": [f"proposal:{local_id}"],
            }
        )
    return rows


def _alignment_rows(fixture: dict[str, Any], physical_page: str) -> list[dict[str, str]]:
    return [
        {
            "page_id": capture["page_id"],
            "source_sha256": capture["source_sha256"],
            "physical_page_id": physical_page,
            "alignment_ref": capture["alignment_ref"],
        }
        for capture in fixture["captures"]
    ]


def _register(tmp_path: Path, fixture: dict[str, Any]) -> tuple[Path, str, str]:
    page = fixture["physical_page"]
    physical_page = physical_page_id(page["corpus_id"], page["volume_id"], page["designation"])
    sources = [capture["source_sha256"] for capture in fixture["captures"]]
    retained_sources = set(sources) - {fixture["removed_source_sha256"]}
    (retained_source,) = retained_sources
    first_membership = {
        "kind": "membership",
        "physical_page_id": physical_page,
        "members": [retained_source],
        "predecessor": None,
        "appending_run": "unit19b-fixture-a",
    }
    both_membership = {
        "kind": "membership",
        "physical_page_id": physical_page,
        "members": sorted(sources),
        "predecessor": digest_of(first_membership),
        "appending_run": "unit19b-fixture-b",
    }
    path = tmp_path / "register.json"
    append_records(
        path,
        [
            {
                "kind": "physical-page",
                **page,
                "physical_page_id": physical_page,
            },
            first_membership,
            both_membership,
        ],
        expected_digest=register_digest(empty_register()),
    )
    predecessor = register_digest(path.read_bytes())
    local_rows = _local_rows(fixture)
    proposal = build_correspondence_proposal(
        register=path.read_bytes(),
        register_digest=predecessor,
        discovery_run_id="unit19b-discovery",
        components=[
            {
                "physical_page_id": physical_page,
                "physical_act_id": None,
                "local_acts": local_rows,
                "evidence": ["geometry:unit19b:a-b"],
                "finding": None,
            }
        ],
    )
    append_correspondence_proposal(
        register_path=str(path),
        proposal=proposal,
        discovery_register_digest=predecessor,
    )
    (physical_act_record,) = [
        row for row in proposal["accepted_records"] if row["kind"] == "physical-act"
    ]
    physical_act = physical_act_record["physical_act_id"]
    return path, physical_page, physical_act


def _partition(
    path: Path,
    fixture: dict[str, Any],
    physical_page: str,
    *,
    captures: list[dict[str, Any]],
) -> dict[str, Any]:
    included_sources = {capture["source_sha256"] for capture in captures}
    local_rows = [row for row in _local_rows(fixture) if row["source_sha256"] in included_sources]
    alignments = [
        row
        for row in _alignment_rows(fixture, physical_page)
        if row["source_sha256"] in included_sources
    ]
    register_bytes = path.read_bytes()
    return build_physical_act_partition(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": digest_bytes(b"unit19b proposal seal"),
        },
        local_acts=local_rows,
        capture_alignments=alignments,
        source_ledger=included_sources,
    )


def _autopsia(
    fixture: dict[str, Any], partition: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, bytes]]:
    (logical_act,) = partition["logical_acts"]
    captures_by_source = {capture["source_sha256"]: capture for capture in fixture["captures"]}
    blobs: dict[str, bytes] = {}
    views = []
    for presentation in logical_act["capture_presentations"]:
        capture = captures_by_source[presentation["source_sha256"]]
        crop_ref = _ref(capture["crop_path"], capture["crop_bytes"])
        page_ref = _ref(capture["page_path"], capture["page_bytes"])
        blobs[crop_ref["relative_path"]] = capture["crop_bytes"].encode()
        blobs[page_ref["relative_path"]] = capture["page_bytes"].encode()
        views.append(
            {
                "view_id": f"view:{capture['page_id']}",
                "physical_page_id": presentation["physical_page_id"],
                "source_sha256": presentation["source_sha256"],
                "page_ids": presentation["page_ids"],
                "local_act_ids": presentation["local_act_ids"],
                "region_refs": [crop_ref],
                "page_render_refs": [page_ref],
                "alignment_ref": presentation["alignment_ref"],
                "visibility_evidence_refs": [page_ref],
            }
        )
    partition_bytes = canonical_bytes(partition)
    return (
        build_autopsia(
            logical_act_id=logical_act["logical_act_id"],
            partition_ref={
                "relative_path": "4_perlector/blobs/physical-act-partition.json",
                "sha256": digest_bytes(partition_bytes),
            },
            required_capture_sha256s=[
                source
                for component in logical_act["physical_page_components"]
                for source in component["required_capture_sha256s"]
            ],
            views=views,
        ),
        blobs,
    )


def test_a_deeply_nested_autopsia_view_becomes_a_refusal_not_a_recursion_crash():
    """`_reject_preference` walks `views` before `_view` proves its shape
    (`common/cross_capture_autopsia.py`), so an unvalidated caller can nest it
    past Python's recursion limit. That must become a `SchemaRefusal`, never an
    uncaught `RecursionError` that would crash the whole stage process and take
    every other logical act in the run down with it."""
    nested: Any = "leaf"
    for _ in range(5000):
        nested = {"views": nested}
    with pytest.raises(SchemaRefusal, match="nests too deeply"):
        build_autopsia(
            logical_act_id="pac_0123456789abcdef",
            partition_ref={
                "relative_path": "4_perlector/blobs/physical-act-partition.json",
                "sha256": digest_bytes(b"partition"),
            },
            required_capture_sha256s=[digest_bytes(b"capture")],
            views=[nested],
        )


class _JointReader:
    def __init__(self, established_text: str):
        self.established_text = established_text
        self.calls: list[dict[str, Any]] = []

    def read(self, dossier, *, pass_kind, delivered_pixels):
        assert delivered_pixels["region_images"]
        for crop in delivered_pixels["region_images"]:
            assert self.established_text.encode() in crop
        self.calls.append(
            {
                "dossier": dossier,
                "pass_kind": pass_kind,
                "region_images": list(delivered_pixels["region_images"]),
            }
        )
        return {"text": self.established_text, "stop_reason": None}


def _read(fixture: dict[str, Any], autopsia: dict[str, Any], blobs: dict[str, bytes]):
    reader = _JointReader(fixture["established_text"])
    passes = run_logical_passes(
        reader,
        autopsia=autopsia,
        dossier={"testimonia": []},
        read_bytes=blobs.__getitem__,
        protocol_config={"max_images": 4},
        nuda_sampled=False,
        control_sampled=False,
    )
    return reader, passes


def _contains_winner(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            "winner" in str(key).lower() or _contains_winner(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_winner(item) for item in value)
    return False


def _dissent_for(
    logical_act: dict[str, Any], autopsia: dict[str, Any], perlectio_ref: dict[str, str]
) -> dict[str, Any]:
    """The sibling evidence record for one joint reading, pair-complete.

    Module level so the composition test and the boundary tests below build the
    identical record rather than two slightly different ones; the closed-shape
    and denominator claims themselves are proved in
    `common/test_cross_capture_dissent.py`.
    """
    views = autopsia["views"]
    invocation_ref = _ref("4_perlector/receipts/joint.json", "one reader invocation")
    return build_cross_capture_dissent(
        schema="cross-capture-dissent.v1",
        logical_act_id=logical_act["logical_act_id"],
        perlectio_ref=perlectio_ref,
        partition_ref=autopsia["partition_ref"],
        config_digest="a" * 64,
        model_provenance={"chair": "perlector", "revision": "fixture"},
        reader_invocation_ref=invocation_ref,
        response_observation_digest=digest_bytes(b"joint response observations"),
        views=[
            {
                "view_id": view["view_id"],
                "source_sha256": view["source_sha256"],
                "region_refs": view["region_refs"],
                "visibility_state": "visible",
            }
            for view in views
        ],
        loci=[
            {
                "locus_id": "locus:0",
                "established_span_or_gap_ref": {"start": 0, "end": 5},
                "comparison_state": (
                    "not-comparable" if len(views) == 1 else "different-across-views"
                ),
                "observations": [
                    {
                        "view_id": view["view_id"],
                        "observed_form": ("Maria", "Marta")[offset % 2],
                        "image_region_refs": view["region_refs"],
                        "reason_codes": [] if offset == 0 else ["diagnostic-difference"],
                    }
                    for offset, view in enumerate(views)
                ],
            }
        ],
        pairs=[
            {
                "pair_id": f"pair:{digest_of(sorted(view['view_id'] for view in pair))}",
                "view_ids": [view["view_id"] for view in pair],
                "capture_condition": {"both_unoccluded": True, "comparably_captured": True},
                "same_ink": True,
                "identical_run_configuration": True,
                "act_match_correct": True,
                "finding_codes": [],
            }
            for pair in combinations(views, 2)
        ],
    )


def _full_coverage(logical_act: dict[str, Any], autopsia: dict[str, Any]) -> dict[str, Any]:
    """A complete measured surface for the composed acceptance fixture."""
    alignments = {
        (view["physical_page_id"], view["source_sha256"]): view["alignment_ref"]
        for view in autopsia["views"]
    }
    return build_cross_capture_coverage(
        logical_act_id=logical_act["logical_act_id"],
        components=[
            {
                "physical_page_id": component["physical_page_id"],
                "expected_cells": [[0, 0]],
                "required_capture_sha256s": component["required_capture_sha256s"],
                "captures": [
                    {
                        "source_sha256": source,
                        "alignment_ref": alignments[(component["physical_page_id"], source)],
                        "visibility_state": "visible",
                        "visible_cells": [[0, 0]],
                        "occluded_cells": [],
                        "occlusion_refs": [],
                        "finding_codes": [],
                    }
                    for source in component["required_capture_sha256s"]
                ],
            }
            for component in logical_act["physical_page_components"]
        ],
    )


def _joint_basis_regions(fixture: dict[str, Any], autopsia: dict[str, Any]) -> list[dict[str, Any]]:
    """Retain one complete region-basis row for every crop the joint read saw."""
    captures_by_page = {capture["page_id"]: capture for capture in fixture["captures"]}
    regions = []
    for view in autopsia["views"]:
        (page_id,) = view["page_ids"]
        capture = captures_by_page[page_id]
        for reference in view["region_refs"]:
            regions.append(
                {
                    "region_id": f"rgn_{digest_of(reference)[:16]}",
                    "image_path": reference["relative_path"],
                    "image_sha256": reference["sha256"],
                    "verified_dimensions": {"w": 1, "h": 1},
                    "source_page_ordinal": capture["page_ordinal"],
                    "source_page_id": page_id,
                    "transform": {
                        "operation": "crop",
                        "source_page_ordinal": capture["page_ordinal"],
                        "source_page_id": page_id,
                        "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
                    },
                    "structure_provenance": {"chair": "designator"},
                    "witness_covered": True,
                }
            )
    return sorted(regions, key=lambda region: (region["image_path"], region["region_id"]))


def test_two_capture_leaf_cluster_runs_partition_autopsia_perlectio_and_dissent(tmp_path):
    fixture = _fixture()
    register_path, physical_page, physical_act = _register(tmp_path, fixture)
    captures = fixture["captures"]

    partition = _partition(register_path, fixture, physical_page, captures=captures)
    assert partition["findings"] == []
    assert partition["local_expected_count"] == 2
    assert partition["logical_expected_count"] == 1
    (logical_act,) = partition["logical_acts"]
    assert logical_act["logical_act_id"] == physical_act

    autopsia, blobs = _autopsia(fixture, partition)
    reader, passes = _read(fixture, autopsia, blobs)
    establishing = [call for call in reader.calls if call["pass_kind"] == "perlectio"]
    (establishing_call,) = establishing
    assert len(establishing_call["region_images"]) == 2
    assert passes["perlectio"]["result"]["text"] == fixture["established_text"]
    assert set(autopsia["required_capture_sha256s"]) == {
        capture["source_sha256"] for capture in captures
    }
    assert set(
        establishing_call["dossier"]["cross_capture_autopsia"]["required_capture_sha256s"]
    ) == {capture["source_sha256"] for capture in captures}

    perlectio_bytes = canonical_bytes(passes["perlectio"])
    perlectio_ref = {
        "relative_path": "4_perlector/artifacts/perlectio.json",
        "sha256": digest_bytes(perlectio_bytes),
    }
    shell = dissent_shell(
        perlectio_ref=perlectio_ref,
        autopsia=autopsia,
        reader_invocation_ref={
            "relative_path": "4_perlector/receipts/joint-reader.json",
            "sha256": digest_bytes(b"one joint reader invocation"),
        },
        response_observation_digest=digest_bytes(b"two legible observations"),
    )
    assert shell["perlectio_ref"] == perlectio_ref
    assert {view["source_sha256"] for view in shell["views"]} == {
        capture["source_sha256"] for capture in captures
    }
    assert shell["capture_pairs"] == [sorted(capture["source_sha256"] for capture in captures)]

    membership_head, _active_members = membership_heads(register_path.read_bytes())[physical_page]
    append_records(
        register_path,
        [
            {
                "kind": "retraction",
                "retracts": f"membership:{membership_head}",
                "reason": "unit19b fixture removes one capture to measure evidence coverage",
                "appending_run": "unit19b-fixture-removal",
            }
        ],
        expected_digest=register_digest(register_path.read_bytes()),
    )
    remaining = [
        capture
        for capture in captures
        if capture["source_sha256"] != fixture["removed_source_sha256"]
    ]
    reduced_partition = _partition(register_path, fixture, physical_page, captures=remaining)
    reduced_autopsia, reduced_blobs = _autopsia(fixture, reduced_partition)
    reduced_reader, reduced_passes = _read(fixture, reduced_autopsia, reduced_blobs)

    (reduced_logical_act,) = reduced_partition["logical_acts"]
    assert reduced_logical_act["logical_act_id"] == physical_act
    assert autopsia["member_conservation"]["delivered_count"] == 2
    assert reduced_autopsia["member_conservation"]["delivered_count"] == 1
    (reduced_establishing,) = [
        call for call in reduced_reader.calls if call["pass_kind"] == "perlectio"
    ]
    assert len(reduced_establishing["region_images"]) == 1
    assert reduced_passes["perlectio"]["result"]["text"] == fixture["established_text"]
    assert not _contains_winner(
        {
            "partition": partition,
            "autopsia": autopsia,
            "perlectio": passes["perlectio"],
            "dissent": shell,
            "reduced_partition": reduced_partition,
            "reduced_autopsia": reduced_autopsia,
            "reduced_perlectio": reduced_passes["perlectio"],
        }
    )


def test_composed_two_capture_path_establishes_one_logical_record_and_projects_one_text(tmp_path):
    """The required 19A→19D composition, extending the one cluster fixture."""
    fixture = _fixture()
    register_path, physical_page, _physical_act = _register(tmp_path, fixture)
    partition = _partition(register_path, fixture, physical_page, captures=fixture["captures"])
    (logical_act,) = partition["logical_acts"]
    autopsia, blobs = _autopsia(fixture, partition)
    reader, passes = _read(fixture, autopsia, blobs)
    (joint_call,) = [call for call in reader.calls if call["pass_kind"] == "perlectio"]
    assert len(joint_call["region_images"]) == 2

    archetypus = _module("u19d_archetypus", ARCHETYPUS_RUN)
    armarium = _module("u19d_armarium", ARMARIUM_RUN)
    armarium_export = _module("u19d_armarium_bundle", ARMARIUM_EXPORT)
    ArmariumProjection = armarium_export.ArmariumProjection
    build_armarium_bundle = armarium_export.build_armarium_bundle

    from common.armarium_formats import ArmariumFormats  # noqa: PLC0415

    png_a = encode_grayscale_png(1, 1, [bytearray([20])])
    png_b = encode_grayscale_png(1, 1, [bytearray([40])])
    source_a = fixture["captures"][0]
    source_b = fixture["captures"][1]
    # Capture B's own declared source-page ordinal, read off the fixture rather
    # than asserted here: a source-page ordinal indexes the run's submission
    # manifest, so two distinct captures cannot share one. An ordinal invented
    # by this test makes every downstream page attribution for capture B
    # unfalsifiable -- the census would say one page while the partition row the
    # export derives membership from says another, with nothing comparing them.
    source_b_ordinal = source_b["page_ordinal"]
    assert source_a["page_ordinal"] != source_b_ordinal
    regions = _joint_basis_regions(fixture, autopsia)
    # `perlectio_ref`/`review_ref` are the real digests of the objects they
    # name below, not arbitrary placeholder bytes: `establish_logical_record`
    # checks `accepted_perlectio`/`accepted_review` against these references
    # by digest, so a ref unrelated to the object it names must be refused
    # rather than silently accepted (test_two_capture_leaf_cluster_... proves
    # the mechanism reads all capture pixels; this test also has to prove the
    # copy is bound to what the reference actually names).
    accepted_perlectio = {
        "config_digest": "a" * 64,
        "outcome": "read",
        "payload": {
            "text": passes["perlectio"]["result"]["text"],
            # The dossier the joint reader was actually handed, autopsia and
            # all: `establish_logical_record` proves this record's member and
            # capture provenance against the partition that autopsia names,
            # so a dossier reduced to a bare `logical_act_id` is refused.
            "dossier": {
                "logical_act_id": logical_act["logical_act_id"],
                "cross_capture_autopsia": autopsia,
            },
            "basis": {"regions": regions},
            "provenance": {"chair": "perlector", "revision": "fixture"},
            "reader_invocation_ref": _ref(
                "4_perlector/receipts/joint.json", "one reader invocation"
            ),
            "response_observation_digest": digest_bytes(b"joint response observations"),
            "annotations": [],
            "uncertain_spans": [],
            "gaps": [],
            "self_revision": [],
        },
    }
    perlectio_ref = {
        "relative_path": "4_perlector/artifacts/perlectio/joint.json",
        "sha256": digest_of(accepted_perlectio),
    }
    dissent = _dissent_for(logical_act, autopsia, perlectio_ref)
    dissent_ref = _ref(
        "4_perlector/artifacts/cross-capture-dissent/joint.json", canonical_bytes(dissent).decode()
    )
    unit20_input = unit20_dissent_input(dissent)
    assert unit20_input["pairs"] == dissent["pairs"]
    assert "text" not in unit20_input
    assert not any("variance" in field for field in unit20_input)
    accepted_review = {
        "outcome": "accepted",
        "payload": {
            "perlectio_ref": perlectio_ref,
            "cross_capture_dissent_ref": dissent_ref,
            "cross_capture_coverage": _full_coverage(logical_act, autopsia),
        },
    }
    review_ref = {
        "relative_path": "5_recensor/artifacts/review/joint.json",
        "sha256": digest_of(accepted_review),
    }
    tampered_perlectio = {
        **accepted_perlectio,
        "payload": {**accepted_perlectio["payload"], "text": "a different reading entirely"},
    }
    with pytest.raises(SchemaRefusal):
        archetypus.establish_logical_record(
            partition=partition,
            logical_act=logical_act,
            accepted_perlectio=tampered_perlectio,
            accepted_review=accepted_review,
            perlectio_ref=perlectio_ref,
            recensor_ref=review_ref,
            cross_capture_dissent=dissent,
            cross_capture_dissent_ref=dissent_ref,
        )
    established = archetypus.establish_logical_record(
        partition=partition,
        logical_act=logical_act,
        accepted_perlectio=accepted_perlectio,
        accepted_review=accepted_review,
        perlectio_ref=perlectio_ref,
        recensor_ref=review_ref,
        cross_capture_dissent=dissent,
        cross_capture_dissent_ref=dissent_ref,
    )
    assert established["text"] == fixture["established_text"]
    assert "page_id" not in established and "act_key" not in established
    assert archetypus.build_logical_index([established], run_id="u19d")["record_count"] == 1

    page_citations = {
        source_a["page_id"]: ("capture-a/register.png", digest_bytes(png_a)),
        source_b["page_id"]: ("capture-b/register.png", digest_bytes(png_b)),
    }
    source_regions = []
    for region in regions:
        declared_path, declared_sha256 = page_citations[region["source_page_id"]]
        source_regions.append(
            {
                **region,
                "declared_path": declared_path,
                "declared_sha256": declared_sha256,
            }
        )
    entry = armarium.logical_act_projection_entry(
        established,
        category="delivered",
        source_regions=source_regions,
        witnesses=[{"chair": "attestator_1"}],
    )
    with pytest.raises(SchemaRefusal, match="source regions do not equal.*no crop may vanish"):
        armarium.logical_act_projection_entry(
            established,
            category="delivered",
            source_regions=source_regions[:-1],
            witnesses=[{"chair": "attestator_1"}],
        )
    coverage = {"configured": 1, "floor": 1, "under_witnessed": False, "unresolved_chairs": 0}
    pages = (
        {
            "ordinal": source_a["page_ordinal"],
            "outcome": "sealed",
            "reason": "",
            "declared_path": "capture-a/register.png",
            "declared_sha256": digest_bytes(png_a),
            "page_id": source_a["page_id"],
            "image_path": "1_exemplar/blobs/a.png",
            "image_sha256": digest_bytes(png_a),
        },
        {
            "ordinal": source_b_ordinal,
            "outcome": "sealed",
            "reason": "",
            "declared_path": "capture-b/register.png",
            "declared_sha256": digest_bytes(png_b),
            "page_id": source_b["page_id"],
            "image_path": "1_exemplar/blobs/b.png",
            "image_sha256": digest_bytes(png_b),
        },
    )
    key = entry["act_key"]
    aggregate_basis = {
        "coverage_records": {key: coverage},
        "unaddressed_chairs": [],
        "act_pages": {key: [source_a["page_ordinal"], source_b_ordinal]},
        "act_text_status": {key: "established"},
    }
    aggregate = run_aggregate(
        {key: ArmariumCategory.DELIVERED},
        {key: coverage},
        {page["ordinal"]: page for page in pages},
        unaddressed_chairs=[],
        act_pages=aggregate_basis["act_pages"],
        act_text_status=aggregate_basis["act_text_status"],
    )
    projection = ArmariumProjection(
        fixture_id="u19d-composed",
        scenario="happy",
        config_digest="a" * 64,
        aggregate=aggregate,
        acts=(entry,),
        pages=pages,
        source_manifest=(
            {
                "ordinal": source_a["page_ordinal"],
                "relative_path": "capture-a/register.png",
                "sha256": digest_bytes(png_a),
            },
            {
                "ordinal": source_b_ordinal,
                "relative_path": "capture-b/register.png",
                "sha256": digest_bytes(png_b),
            },
        ),
        expected_acts=1,
        # Both counts, named: the act terminal denominator is the one logical
        # act, and the proposal seal's own two rows travel beside it so the
        # bundle's claim can be reconciled against the seal it came from.
        local_proposal_rows=partition["local_expected_count"],
        witness_chairs=("attestator_1",),
        witness_floor=1,
        aggregate_basis=aggregate_basis,
        ink_map_pages=(
            {"ordinal": source_a["page_ordinal"], "initial_outcome": "mapped", "remeasured": None},
            {"ordinal": source_b_ordinal, "initial_outcome": "mapped", "remeasured": None},
        ),
    )
    assert len(projection.pages) == 2  # page census remains per declared source row
    assert projection.expected_acts == 1  # the shared physical act is counted once
    assert projection.local_proposal_rows == 2  # and both seal rows are still accounted for
    bytes_by_path = {
        "1_exemplar/blobs/a.png": png_a,
        "1_exemplar/blobs/b.png": png_b,
        **{region["image_path"]: b"unused without embedded pixels" for region in source_regions},
    }
    bundle = build_armarium_bundle(
        projection,
        ArmariumFormats(
            ("text-bundle", "acts-database", "jsonl", "review-items", "salvage-tier"), False
        ),
        bytes_by_path.__getitem__,
    )
    from io import BytesIO  # noqa: PLC0415
    from zipfile import ZipFile  # noqa: PLC0415

    with ZipFile(BytesIO(bundle.data)) as archive:
        assert fixture["established_text"] in archive.read("acts.jsonl").decode()
        assert (
            fixture["established_text"]
            in archive.read("text/_source_folder/capture-a/readings.txt").decode()
        )
        readable_a = archive.read("text/_source_folder/capture-a/readings.txt").decode()
        readable_b = archive.read("text/_source_folder/capture-b/readings.txt").decode()
        assert fixture["established_text"] in readable_b
        for readable in (readable_a, readable_b):
            assert "source-page: capture-a/register.png" in readable
            assert "source-page: capture-b/register.png" in readable
        assert archive.read("acts.jsonl").decode().count(fixture["established_text"]) == 1
        database = tmp_path / "acts.sqlite"
        database.write_bytes(archive.read("acts.sqlite"))
    with sqlite3.connect(database) as connection:
        [(sqlite_text,)] = connection.execute("SELECT canonical_clean_text FROM acts").fetchall()
    assert sqlite_text == established["text"]

    verification_root = tmp_path / "duplicate-readable-verification"
    with ZipFile(BytesIO(bundle.data)) as archive:
        archive.extractall(verification_root)
    readable_path = verification_root / "text/_source_folder/capture-a/readings.txt"
    readable_bytes = readable_path.read_bytes()
    readable_path.write_bytes(readable_bytes + b"\n" + readable_bytes)
    with pytest.raises(SchemaRefusal, match="repeats one act inside the same source folder"):
        armarium_export._text_bundle_records(verification_root, list(pages))

    # The bundle's own act denominator, under the name of what was counted.
    # One logical act exported under the manifest's fixed "proposal-seal
    # expected acts" claim would report a number nobody measured -- the seal
    # here holds two rows -- and would drop the count a reader needs to
    # reconcile the bundle against that seal (GOVERNANCE 2, 10; consult §5.2's
    # "the terminal ledger reports both counts explicitly").
    with ZipFile(BytesIO(bundle.data)) as archive:
        manifest = json.loads(archive.read("EXPORT_MANIFEST.json"))
    claim = manifest["claims"]["act_partition"]
    assert claim["denominator"] == "physical-act-partition logical acts"
    assert claim["expected_count"] == 1 and claim["counted"] == 1 and claim["reconciles"]
    assert claim["local_proposal_rows"] == 2
    assert claim["logical_membership"][entry["act_id"]]["member_act_keys"] == sorted(
        member["act_key"] for member in logical_act["member_local_acts"]
    )
    assert sorted(claim["logical_membership"][entry["act_id"]]["member_source_page_ordinals"]) == [
        source_a["page_ordinal"],
        source_b_ordinal,
    ]

    # And a clustered projection that does not say how many seal rows its
    # smaller denominator stands for cannot be exported at all.
    with pytest.raises(SchemaRefusal):
        build_armarium_bundle(
            replace(projection, local_proposal_rows=None),
            ArmariumFormats(("jsonl",), False),
            bytes_by_path.__getitem__,
        )


def _reading_inputs(
    fixture: dict[str, Any],
    logical_act: dict[str, Any],
    autopsia: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """The accepted joint reading and review, each bound to its own digest."""
    accepted_perlectio = {
        "config_digest": "a" * 64,
        "outcome": "read",
        "payload": {
            "text": text,
            "dossier": {
                "logical_act_id": logical_act["logical_act_id"],
                "cross_capture_autopsia": autopsia,
            },
            "basis": {"regions": _joint_basis_regions(fixture, autopsia)},
            "provenance": {"chair": "perlector", "revision": "fixture"},
            "reader_invocation_ref": _ref(
                "4_perlector/receipts/joint.json", "one reader invocation"
            ),
            "response_observation_digest": digest_bytes(b"joint response observations"),
            "annotations": [],
            "uncertain_spans": [],
            "gaps": [],
            "self_revision": [],
        },
    }
    perlectio_ref = {
        "relative_path": "4_perlector/artifacts/perlectio/joint.json",
        "sha256": digest_of(accepted_perlectio),
    }
    dissent = _dissent_for(logical_act, autopsia, perlectio_ref)
    dissent_ref = _ref(
        "4_perlector/artifacts/cross-capture-dissent/joint.json",
        canonical_bytes(dissent).decode(),
    )
    accepted_review = {
        "outcome": "accepted",
        "payload": {
            "perlectio_ref": perlectio_ref,
            "cross_capture_dissent_ref": dissent_ref,
            "cross_capture_coverage": _full_coverage(logical_act, autopsia),
        },
    }
    return {
        "accepted_perlectio": accepted_perlectio,
        "perlectio_ref": perlectio_ref,
        "accepted_review": accepted_review,
        "recensor_ref": {
            "relative_path": "5_recensor/artifacts/review/joint.json",
            "sha256": digest_of(accepted_review),
        },
        "cross_capture_dissent": dissent,
        "cross_capture_dissent_ref": dissent_ref,
    }


def test_logical_establishment_retains_every_joint_autopsia_crop(tmp_path):
    fixture = _fixture()
    register_path, physical_page, _physical_act = _register(tmp_path, fixture)
    partition = _partition(register_path, fixture, physical_page, captures=fixture["captures"])
    (logical_act,) = partition["logical_acts"]
    autopsia, blobs = _autopsia(fixture, partition)
    _reader, passes = _read(fixture, autopsia, blobs)
    inputs = _reading_inputs(fixture, logical_act, autopsia, passes["perlectio"]["result"]["text"])
    archetypus = _module("u19d_complete_region_basis", ARCHETYPUS_RUN)

    incomplete_perlectio = {
        **inputs["accepted_perlectio"],
        "payload": {
            **inputs["accepted_perlectio"]["payload"],
            "basis": {"regions": inputs["accepted_perlectio"]["payload"]["basis"]["regions"][:-1]},
        },
    }
    incomplete_ref = {
        "relative_path": "4_perlector/artifacts/perlectio/incomplete-regions.json",
        "sha256": digest_of(incomplete_perlectio),
    }
    incomplete_dissent = _dissent_for(logical_act, autopsia, incomplete_ref)
    incomplete_dissent_ref = _ref(
        "4_perlector/artifacts/cross-capture-dissent/incomplete-regions.json",
        canonical_bytes(incomplete_dissent).decode(),
    )
    incomplete_review = {
        **inputs["accepted_review"],
        "payload": {
            **inputs["accepted_review"]["payload"],
            "perlectio_ref": incomplete_ref,
            "cross_capture_dissent_ref": incomplete_dissent_ref,
        },
    }
    with pytest.raises(SchemaRefusal, match="region basis does not equal every crop"):
        archetypus.establish_logical_record(
            partition=partition,
            logical_act=logical_act,
            accepted_perlectio=incomplete_perlectio,
            accepted_review=incomplete_review,
            perlectio_ref=incomplete_ref,
            recensor_ref={
                "relative_path": "5_recensor/artifacts/review/incomplete-regions.json",
                "sha256": digest_of(incomplete_review),
            },
            cross_capture_dissent=incomplete_dissent,
            cross_capture_dissent_ref=incomplete_dissent_ref,
        )


def test_logical_establishment_refuses_capacity_and_review_holds_by_their_real_cause(tmp_path):
    fixture = _fixture()
    register_path, physical_page, _physical_act = _register(tmp_path, fixture)
    partition = _partition(register_path, fixture, physical_page, captures=fixture["captures"])
    (logical_act,) = partition["logical_acts"]
    autopsia, blobs = _autopsia(fixture, partition)
    _reader, passes = _read(fixture, autopsia, blobs)
    inputs = _reading_inputs(fixture, logical_act, autopsia, passes["perlectio"]["result"]["text"])
    archetypus = _module("u19d_capacity_review_refusals", ARCHETYPUS_RUN)

    not_run = {
        "config_digest": "a" * 64,
        "outcome": "not-run",
        "payload": {"reason": "cluster-presentation-over-capacity: needs 40 images"},
    }
    with pytest.raises(SchemaRefusal, match="not-run.*capacity hold.*establishes no text"):
        archetypus.establish_logical_record(
            partition=partition,
            logical_act=logical_act,
            **(
                inputs
                | {
                    "accepted_perlectio": not_run,
                    "perlectio_ref": {
                        "relative_path": "4_perlector/artifacts/perlectio/not-run.json",
                        "sha256": digest_of(not_run),
                    },
                }
            ),
        )

    held_review = {
        **inputs["accepted_review"],
        "outcome": "held-for-review",
        "payload": {**inputs["accepted_review"]["payload"], "reason": "occluded-everywhere"},
    }
    with pytest.raises(SchemaRefusal, match="held-for-review.*review item"):
        archetypus.establish_logical_record(
            partition=partition,
            logical_act=logical_act,
            **(
                inputs
                | {
                    "accepted_review": held_review,
                    "recensor_ref": {
                        "relative_path": "5_recensor/artifacts/review/held.json",
                        "sha256": digest_of(held_review),
                    },
                }
            ),
        )


def test_an_occlusion_finding_cannot_hide_inside_a_review_labelled_accepted(tmp_path):
    fixture = _fixture()
    register_path, physical_page, _physical_act = _register(tmp_path, fixture)
    partition = _partition(register_path, fixture, physical_page, captures=fixture["captures"])
    (logical_act,) = partition["logical_acts"]
    autopsia, blobs = _autopsia(fixture, partition)
    _reader, passes = _read(fixture, autopsia, blobs)
    inputs = _reading_inputs(fixture, logical_act, autopsia, passes["perlectio"]["result"]["text"])
    alignments = {
        (view["physical_page_id"], view["source_sha256"]): view["alignment_ref"]
        for view in autopsia["views"]
    }
    occluded = build_cross_capture_coverage(
        logical_act_id=logical_act["logical_act_id"],
        components=[
            {
                "physical_page_id": component["physical_page_id"],
                "expected_cells": [[0, 0]],
                "required_capture_sha256s": component["required_capture_sha256s"],
                "captures": [
                    {
                        "source_sha256": source,
                        "alignment_ref": alignments[(component["physical_page_id"], source)],
                        "visibility_state": "occluded",
                        "visible_cells": [],
                        "occluded_cells": [[0, 0]],
                        "occlusion_refs": [f"occlusion:{source}"],
                        "finding_codes": [],
                    }
                    for source in component["required_capture_sha256s"]
                ],
            }
            for component in logical_act["physical_page_components"]
        ],
    )
    contradictory = {
        **inputs["accepted_review"],
        "payload": {**inputs["accepted_review"]["payload"], "cross_capture_coverage": occluded},
    }
    archetypus = _module("u19d_occlusion_review_refusal", ARCHETYPUS_RUN)
    with pytest.raises(SchemaRefusal, match="occluded-everywhere.*review item"):
        archetypus.establish_logical_record(
            partition=partition,
            logical_act=logical_act,
            **(
                inputs
                | {
                    "accepted_review": contradictory,
                    "recensor_ref": {
                        "relative_path": "5_recensor/artifacts/review/contradictory.json",
                        "sha256": digest_of(contradictory),
                    },
                }
            ),
        )

    unresolved = build_cross_capture_coverage(
        logical_act_id=logical_act["logical_act_id"],
        components=[
            {
                "physical_page_id": component["physical_page_id"],
                "expected_cells": [[0, 0]],
                "required_capture_sha256s": component["required_capture_sha256s"],
                "captures": [
                    {
                        "source_sha256": source,
                        "alignment_ref": alignments[(component["physical_page_id"], source)],
                        "visibility_state": "unresolved",
                        "visible_cells": [],
                        "occluded_cells": [],
                        "occlusion_refs": [],
                        "finding_codes": ["visibility-survey-unavailable"],
                    }
                    for source in component["required_capture_sha256s"]
                ],
            }
            for component in logical_act["physical_page_components"]
        ],
    )
    mislabeled_accepted = {
        **inputs["accepted_review"],
        "payload": {
            **inputs["accepted_review"]["payload"],
            "cross_capture_coverage": unresolved,
        },
    }
    with pytest.raises(SchemaRefusal, match="capture-visibility-unresolved.*review item"):
        archetypus.establish_logical_record(
            partition=partition,
            logical_act=logical_act,
            **(
                inputs
                | {
                    "accepted_review": mislabeled_accepted,
                    "recensor_ref": {
                        "relative_path": "5_recensor/artifacts/review/unresolved.json",
                        "sha256": digest_of(mislabeled_accepted),
                    },
                }
            ),
        )

    armarium = _module("u19d_occlusion_review_export", ARMARIUM_RUN)
    armarium_export = _module("u19d_occlusion_bundle", ARMARIUM_EXPORT)
    ArmariumProjection = armarium_export.ArmariumProjection
    build_armarium_bundle = armarium_export.build_armarium_bundle

    from common.armarium_formats import ArmariumFormats  # noqa: PLC0415

    held_review = {
        "outcome": "held-for-review",
        "payload": {
            **inputs["accepted_review"]["payload"],
            "reason": "every required act surface was measured occluded",
            "cross_capture_coverage": occluded,
        },
    }
    held_ref = {
        "relative_path": "5_recensor/artifacts/review/occluded.json",
        "sha256": digest_of(held_review),
    }
    witness_coverage = {
        "configured": 1,
        "floor": 1,
        "under_witnessed": False,
        "unresolved_chairs": 0,
    }
    altered_review = {
        **held_review,
        "payload": {
            **held_review["payload"],
            "cross_capture_coverage": {**occluded, "act_state": "full"},
        },
    }
    with pytest.raises(SchemaRefusal, match="malformed cross-capture coverage.*review row"):
        armarium.logical_cross_capture_review_entry(
            partition=partition,
            logical_act=logical_act,
            review=altered_review,
            review_ref={
                "relative_path": "5_recensor/artifacts/review/altered-coverage.json",
                "sha256": digest_of(altered_review),
            },
            witness_coverage=witness_coverage,
            witnesses=[{"chair": "attestator_1"}],
        )
    unbound_payload = {
        key: value
        for key, value in held_review["payload"].items()
        if key != "cross_capture_dissent_ref"
    }
    unbound_review = {**held_review, "payload": unbound_payload}
    with pytest.raises(SchemaRefusal, match="missing its Perlectio or cross-capture dissent"):
        armarium.logical_cross_capture_review_entry(
            partition=partition,
            logical_act=logical_act,
            review=unbound_review,
            review_ref={
                "relative_path": "5_recensor/artifacts/review/unbound.json",
                "sha256": digest_of(unbound_review),
            },
            witness_coverage=witness_coverage,
            witnesses=[{"chair": "attestator_1"}],
        )
    entry = armarium.logical_cross_capture_review_entry(
        partition=partition,
        logical_act=logical_act,
        review=held_review,
        review_ref=held_ref,
        witness_coverage=witness_coverage,
        witnesses=[{"chair": "attestator_1"}],
    )
    assert entry["canonical_clean_text"] is None
    assert "occluded-everywhere" in entry["reason"]

    pngs = [encode_grayscale_png(1, 1, [bytearray([shade])]) for shade in (20, 40)]
    pages = tuple(
        {
            "ordinal": capture["page_ordinal"],
            "outcome": "sealed",
            "reason": "",
            "declared_path": f"register/capture-{offset}.png",
            "declared_sha256": digest_bytes(png),
            "page_id": capture["page_id"],
            "image_path": f"1_exemplar/blobs/{offset}.png",
            "image_sha256": digest_bytes(png),
        }
        for offset, (capture, png) in enumerate(zip(fixture["captures"], pngs, strict=True))
    )
    key = entry["act_key"]
    aggregate_basis = {
        "coverage_records": {key: witness_coverage},
        "unaddressed_chairs": [],
        "act_pages": {
            key: sorted(member["page_ordinal"] for member in logical_act["member_local_acts"])
        },
        "act_text_status": {},
    }
    aggregate = run_aggregate(
        {key: ArmariumCategory.HELD_FOR_REVIEW},
        {key: witness_coverage},
        {page["ordinal"]: page for page in pages},
        unaddressed_chairs=[],
        act_pages=aggregate_basis["act_pages"],
        act_text_status={},
    )
    projection = ArmariumProjection(
        fixture_id="u19d-occluded-review",
        scenario="occluded",
        config_digest="a" * 64,
        aggregate=aggregate,
        acts=(entry,),
        pages=pages,
        source_manifest=tuple(
            {
                "ordinal": page["ordinal"],
                "relative_path": page["declared_path"],
                "sha256": page["declared_sha256"],
            }
            for page in pages
        ),
        expected_acts=1,
        local_proposal_rows=partition["local_expected_count"],
        witness_chairs=("attestator_1",),
        witness_floor=1,
        aggregate_basis=aggregate_basis,
        ink_map_pages=tuple(
            {"ordinal": page["ordinal"], "initial_outcome": "mapped", "remeasured": None}
            for page in pages
        ),
    )
    bundle = build_armarium_bundle(
        projection,
        ArmariumFormats(("jsonl", "review-items"), False),
        {page["image_path"]: png for page, png in zip(pages, pngs, strict=True)}.__getitem__,
    )
    from io import BytesIO  # noqa: PLC0415
    from zipfile import ZipFile  # noqa: PLC0415

    with ZipFile(BytesIO(bundle.data)) as archive:
        rows = [
            json.loads(line) for line in archive.read("review-items.jsonl").decode().splitlines()
        ]
    assert len(rows) == 1
    assert rows[0]["act_id"] == logical_act["logical_act_id"]
    assert "occluded-everywhere" in rows[0]["reason"]
    assert {
        (reference["run_relative_path"], reference["sha256"])
        for reference in rows[0]["evidence_refs"]
    } == {(reference["relative_path"], reference["sha256"]) for reference in entry["evidence_refs"]}


def test_dissent_reference_and_views_must_be_the_sibling_of_the_joint_read(tmp_path):
    fixture = _fixture()
    register_path, physical_page, _physical_act = _register(tmp_path, fixture)
    partition = _partition(register_path, fixture, physical_page, captures=fixture["captures"])
    (logical_act,) = partition["logical_acts"]
    autopsia, blobs = _autopsia(fixture, partition)
    _reader, passes = _read(fixture, autopsia, blobs)
    inputs = _reading_inputs(fixture, logical_act, autopsia, passes["perlectio"]["result"]["text"])
    archetypus = _module("u19d_dissent_binding_refusals", ARCHETYPUS_RUN)

    with pytest.raises(SchemaRefusal, match="dissent.*exact bytes.*reference"):
        archetypus.establish_logical_record(
            partition=partition,
            logical_act=logical_act,
            **(
                inputs
                | {
                    "cross_capture_dissent_ref": {
                        "relative_path": "4_perlector/artifacts/cross-capture-dissent/other.json",
                        "sha256": "f" * 64,
                    }
                }
            ),
        )

    body = {
        key: value for key, value in inputs["cross_capture_dissent"].items() if key != "self_hash"
    }
    wandering_views = list(body["views"])
    wandering_views[0] = {
        **wandering_views[0],
        "region_refs": [_ref("2_designator/blobs/other.png", "other capture")],
    }
    wandering_loci = [
        {
            **locus,
            "observations": [
                {
                    **observation,
                    "image_region_refs": wandering_views[0]["region_refs"],
                }
                if observation["view_id"] == wandering_views[0]["view_id"]
                else observation
                for observation in locus["observations"]
            ],
        }
        for locus in body["loci"]
    ]
    wandering = build_cross_capture_dissent(
        **(body | {"views": wandering_views, "loci": wandering_loci})
    )
    wandering_ref = _ref(
        "4_perlector/artifacts/cross-capture-dissent/wandering.json",
        canonical_bytes(wandering).decode(),
    )
    review = {
        **inputs["accepted_review"],
        "payload": {
            **inputs["accepted_review"]["payload"],
            "cross_capture_dissent_ref": wandering_ref,
        },
    }
    with pytest.raises(SchemaRefusal, match="dissent views.*autopsia views"):
        archetypus.establish_logical_record(
            partition=partition,
            logical_act=logical_act,
            **(
                inputs
                | {
                    "accepted_review": review,
                    "recensor_ref": {
                        "relative_path": "5_recensor/artifacts/review/wandering.json",
                        "sha256": digest_of(review),
                    },
                    "cross_capture_dissent": wandering,
                    "cross_capture_dissent_ref": wandering_ref,
                }
            ),
        )

    incomplete_review = {
        **inputs["accepted_review"],
        "payload": {
            **inputs["accepted_review"]["payload"],
            "cross_capture_coverage": {
                **inputs["accepted_review"]["payload"]["cross_capture_coverage"],
                "components": [],
            },
        },
    }
    with pytest.raises(SchemaRefusal, match="malformed cross-capture coverage.*Archetypus"):
        archetypus.establish_logical_record(
            partition=partition,
            logical_act=logical_act,
            **(
                inputs
                | {
                    "accepted_review": incomplete_review,
                    "recensor_ref": {
                        "relative_path": "5_recensor/artifacts/review/incomplete-coverage.json",
                        "sha256": digest_of(incomplete_review),
                    },
                }
            ),
        )


def test_one_active_capture_after_retraction_still_establishes_and_projects_one_text(tmp_path):
    fixture = _fixture()
    register_path, physical_page, physical_act = _register(tmp_path, fixture)
    membership_head, _members = membership_heads(register_path.read_bytes())[physical_page]
    append_records(
        register_path,
        [
            {
                "kind": "retraction",
                "retracts": f"membership:{membership_head}",
                "reason": "retain one active capture for the Unit 19D boundary case",
                "appending_run": "unit19d-one-active-capture",
            }
        ],
        expected_digest=register_digest(register_path.read_bytes()),
    )
    remaining = [
        capture
        for capture in fixture["captures"]
        if capture["source_sha256"] != fixture["removed_source_sha256"]
    ]
    partition = _partition(register_path, fixture, physical_page, captures=remaining)
    (logical_act,) = partition["logical_acts"]
    assert logical_act["logical_act_id"] == physical_act
    assert len(logical_act["member_local_acts"]) == 1
    autopsia, blobs = _autopsia(fixture, partition)
    _reader, passes = _read(fixture, autopsia, blobs)
    inputs = _reading_inputs(fixture, logical_act, autopsia, passes["perlectio"]["result"]["text"])
    assert inputs["cross_capture_dissent"]["pairs"] == []
    archetypus = _module("u19d_one_active_archetypus", ARCHETYPUS_RUN)
    armarium = _module("u19d_one_active_armarium", ARMARIUM_RUN)
    established = archetypus.establish_logical_record(
        partition=partition, logical_act=logical_act, **inputs
    )
    entry = armarium.logical_act_projection_entry(
        established, category="delivered", source_regions=established["regions"], witnesses=[]
    )
    assert entry["act_id"] == physical_act
    assert entry["canonical_clean_text"] == fixture["established_text"]
    assert entry["logical_membership"]["member_local_act_ids"] == [
        logical_act["member_local_acts"][0]["act_id"]
    ]
    recovery = capture_specific_recovery(
        logical_act_id=logical_act["logical_act_id"],
        source_sha256=remaining[0]["source_sha256"],
        page_ordinal=remaining[0]["page_ordinal"],
        ink_confirmed=True,
        page_observation_grant_available=True,
        act_budget_available=True,
    )
    assert recovery == {
        "logical_act_id": physical_act,
        "source_sha256": remaining[0]["source_sha256"],
        "page_ordinal": remaining[0]["page_ordinal"],
        "origin": "ink-confirmed-observation",
        "admitted": True,
        "reason": "this capture's Unit 14B ink-confirmed observation and both bounded grants admit recovery",
    }


def test_a_resealed_logical_record_cannot_forge_identity_or_member_conservation(tmp_path):
    fixture = _fixture()
    register_path, physical_page, _physical_act = _register(tmp_path, fixture)
    partition = _partition(register_path, fixture, physical_page, captures=fixture["captures"])
    (logical_act,) = partition["logical_acts"]
    autopsia, blobs = _autopsia(fixture, partition)
    _reader, passes = _read(fixture, autopsia, blobs)
    archetypus = _module("u19d_resealed_archetypus", ARCHETYPUS_RUN)
    armarium = _module("u19d_resealed_armarium", ARMARIUM_RUN)
    established = archetypus.establish_logical_record(
        partition=partition,
        logical_act=logical_act,
        **_reading_inputs(fixture, logical_act, autopsia, passes["perlectio"]["result"]["text"]),
    )
    second_member = {
        **established["member_local_acts"][0],
        "act_id": "act_ffffffffffffffff",
    }
    variants = (
        {**established, "logical_act_id": "pac_0123456789abcde\u0301"},
        {
            **established,
            "member_local_acts": [{**established["member_local_acts"][0], "page_ordinal": True}],
        },
        {**established, "member_local_acts": [*established["member_local_acts"], second_member]},
        {**established, "physical_page_components": []},
        {**established, "regions": []},
        {**established, "provenance": {}},
    )
    for variant in variants:
        variant["self_hash"] = archetypus.self_hash(variant)
        with pytest.raises(SchemaRefusal):
            archetypus.validate_logical_record(variant)
        with pytest.raises(SchemaRefusal):
            armarium.logical_act_projection_entry(
                variant,
                category="delivered",
                source_regions=variant["regions"],
                witnesses=[],
            )


def test_a_partition_row_cannot_be_stapled_onto_a_reading_that_never_saw_its_captures(tmp_path):
    """The established record may not claim evidence its own reading never received.

    `logical_act` decides what the record says about the ink behind its one
    text -- which captures were required, which local acts are members, which
    physical pages it sits on.  Tied to the reading by `logical_act_id` string
    equality alone, that was the caller's assertion rather than the reading's,
    and a row naming captures the joint autopsia never presented went on
    unchallenged.
    """
    fixture = _fixture()
    register_path, physical_page, _physical_act = _register(tmp_path, fixture)
    archetypus = _module("u19d_archetypus_boundary", ARCHETYPUS_RUN)

    partition = _partition(register_path, fixture, physical_page, captures=fixture["captures"])
    (logical_act,) = partition["logical_acts"]
    autopsia, blobs = _autopsia(fixture, partition)
    _reader, passes = _read(fixture, autopsia, blobs)
    text = passes["perlectio"]["result"]["text"]
    inputs = _reading_inputs(fixture, logical_act, autopsia, text)

    # The honest establishment over the reading that presented both captures.
    established = archetypus.establish_logical_record(
        partition=partition, logical_act=logical_act, **inputs
    )
    assert established["text"] == text
    assert len(established["member_local_acts"]) == 2

    # A row claiming a third capture and a third member is not the row this
    # partition publishes for that logical act, whatever its `logical_act_id`
    # says.  This is the staple: same subject, wider provenance, same evidence.
    (component,) = logical_act["physical_page_components"]
    wider = {
        **logical_act,
        "physical_page_components": [
            {
                **component,
                "required_capture_sha256s": sorted(
                    [*component["required_capture_sha256s"], "c" * 64]
                ),
            }
        ],
        "member_local_acts": [
            *logical_act["member_local_acts"],
            {**logical_act["member_local_acts"][0], "act_id": "act_cccccccccccccccc"},
        ],
    }
    with pytest.raises(SchemaRefusal) as refusal:
        archetypus.establish_logical_record(partition=partition, logical_act=wider, **inputs)
    assert "the row this partition publishes" in str(refusal.value)

    # A different partition, whose bytes this reading's own autopsia does not
    # name, cannot supply the row either -- even a real one over the same act.
    membership_head, _members = membership_heads(register_path.read_bytes())[physical_page]
    append_records(
        register_path,
        [
            {
                "kind": "retraction",
                "retracts": f"membership:{membership_head}",
                "reason": "one capture is withdrawn, so the partition is a different object",
                "appending_run": "unit19d-boundary",
            }
        ],
        expected_digest=register_digest(register_path.read_bytes()),
    )
    narrow = _partition(
        register_path,
        fixture,
        physical_page,
        captures=[
            capture
            for capture in fixture["captures"]
            if capture["source_sha256"] != fixture["removed_source_sha256"]
        ],
    )
    (narrow_row,) = narrow["logical_acts"]
    assert narrow_row["logical_act_id"] == logical_act["logical_act_id"]
    with pytest.raises(SchemaRefusal) as refusal:
        archetypus.establish_logical_record(partition=narrow, logical_act=narrow_row, **inputs)
    assert "not the bytes the joint reading" in str(refusal.value)

    # A reading that names a logical act with no presentation behind it proves
    # nothing about which captures were read.
    without_autopsia = {
        **inputs["accepted_perlectio"],
        "payload": {
            **inputs["accepted_perlectio"]["payload"],
            "dossier": {"logical_act_id": logical_act["logical_act_id"]},
        },
    }
    stripped_ref = {
        "relative_path": inputs["perlectio_ref"]["relative_path"],
        "sha256": digest_of(without_autopsia),
    }
    stripped_review = {"outcome": "accepted", "payload": {"perlectio_ref": stripped_ref}}
    stripped_dissent = _dissent_for(logical_act, autopsia, stripped_ref)
    with pytest.raises(SchemaRefusal) as refusal:
        archetypus.establish_logical_record(
            partition=partition,
            logical_act=logical_act,
            accepted_perlectio=without_autopsia,
            accepted_review=stripped_review,
            perlectio_ref=stripped_ref,
            recensor_ref={
                "relative_path": inputs["recensor_ref"]["relative_path"],
                "sha256": digest_of(stripped_review),
            },
            cross_capture_dissent=stripped_dissent,
            cross_capture_dissent_ref=_ref(
                "dissent.json", canonical_bytes(stripped_dissent).decode()
            ),
        )
    assert "cross-capture autopsia" in str(refusal.value)

    # And the sibling dissent must cite the same partition as the reading.
    elsewhere = {**autopsia, "partition_ref": _ref("blobs/another-partition.json", "elsewhere")}
    wandering = _dissent_for(logical_act, elsewhere, inputs["perlectio_ref"])
    with pytest.raises(SchemaRefusal) as refusal:
        archetypus.establish_logical_record(
            partition=partition,
            logical_act=logical_act,
            **{
                **inputs,
                "cross_capture_dissent": wandering,
                "cross_capture_dissent_ref": _ref(
                    "dissent.json", canonical_bytes(wandering).decode()
                ),
            },
        )
    assert "different partition" in str(refusal.value)


def test_logical_act_export_conserves_each_member_exactly_once(tmp_path):
    """§7.15: one logical act leaves once with closed, non-overlapping membership.

    Nothing refused the duplicate: two member rows and their logical act all
    carry distinct identities and one terminal category each, so every other
    check in the export passes and the same ink leaves three times.
    """
    armarium_export = _module("u19d_conservation_bundle", ARMARIUM_EXPORT)
    ArmariumProjection = armarium_export.ArmariumProjection
    _validate_logical_act_conservation = armarium_export._validate_logical_act_conservation

    fixture = _fixture()
    register_path, physical_page, _physical_act = _register(tmp_path, fixture)
    partition = _partition(register_path, fixture, physical_page, captures=fixture["captures"])
    (logical_act,) = partition["logical_acts"]
    autopsia, blobs = _autopsia(fixture, partition)
    _reader, passes = _read(fixture, autopsia, blobs)
    archetypus = _module("u19d_archetypus_export", ARCHETYPUS_RUN)
    armarium = _module("u19d_armarium_export", ARMARIUM_RUN)
    established = archetypus.establish_logical_record(
        partition=partition,
        logical_act=logical_act,
        **_reading_inputs(fixture, logical_act, autopsia, passes["perlectio"]["result"]["text"]),
    )
    entry = armarium.logical_act_projection_entry(
        established,
        category="delivered",
        source_regions=established["regions"],
        witnesses=[],
    )
    members = entry["logical_membership"]
    assert members["member_local_act_ids"] == sorted(
        member["act_id"] for member in logical_act["member_local_acts"]
    )
    assert entry["act_id"] not in members["member_local_act_ids"]

    duplicate = {
        "act_id": members["member_local_act_ids"][0],
        "act_key": members["member_act_keys"][0],
        "category": ArmariumCategory.HELD_FOR_REVIEW.value,
        "canonical_clean_text": None,
        "reason": "the member capture's own act, exported beside its logical act",
        "source_regions": [],
    }
    projection = ArmariumProjection(
        fixture_id="u19d-duplicate",
        scenario="adversarial",
        config_digest="a" * 64,
        aggregate={},
        acts=(entry, duplicate),
        pages=(),
        source_manifest=(),
        expected_acts=2,
        witness_chairs=(),
        witness_floor=0,
        aggregate_basis={},
        local_proposal_rows=2,
    )
    with pytest.raises(SchemaRefusal) as refusal:
        _validate_logical_act_conservation(
            projection,
            {act["act_id"] for act in projection.acts},
            {act["act_key"] for act in projection.acts},
        )
    assert "beside the logical act" in str(refusal.value)

    # And the other direction: a member act that the declared proposal-seal row
    # count does not cover has vanished from the accounting, and the smaller
    # logical denominator would otherwise close over its absence.
    alone = replace(projection, acts=(entry,), expected_acts=1, local_proposal_rows=3)
    with pytest.raises(SchemaRefusal) as refusal:
        _validate_logical_act_conservation(
            alone,
            {act["act_id"] for act in alone.acts},
            {act["act_key"] for act in alone.acts},
        )
    assert "accounts for 2 local proposal row(s) against a declared 3" in str(refusal.value)

    other_membership = {
        **members,
        "member_local_act_ids": [
            "act_cccccccccccccccc",
            "act_dddddddddddddddd",
        ],
        # Distinct local ids do not make a reused export key a different
        # proposal row. The key collision must be caught across logical acts.
        "member_act_keys": list(members["member_act_keys"]),
    }
    other = {
        **entry,
        "act_id": "pac_ffffffffffffffff",
        "act_key": "logical:pac_ffffffffffffffff",
        "logical_act_id": "pac_ffffffffffffffff",
        "logical_membership": other_membership,
    }
    reused_key = replace(
        projection,
        acts=(entry, other),
        expected_acts=2,
        local_proposal_rows=4,
    )
    with pytest.raises(SchemaRefusal, match="repeats local member id/key.*exactly one"):
        _validate_logical_act_conservation(
            reused_key,
            {act["act_id"] for act in reused_key.acts},
            {act["act_key"] for act in reused_key.acts},
        )

    # Malformed containers are contract refusals, never incidental TypeErrors;
    # source-page attribution may not be silently empty either.
    malformed_values = (
        ("member_local_act_ids", [{}, *members["member_local_act_ids"][1:]]),
        ("member_act_keys", [{}, *members["member_act_keys"][1:]]),
        ("member_source_page_ordinals", [{}]),
        ("member_source_page_ordinals", []),
    )
    for field, value in malformed_values:
        malformed = {
            **entry,
            "logical_membership": {**members, field: value},
        }
        malformed_projection = replace(projection, acts=(malformed,), expected_acts=1)
        with pytest.raises(SchemaRefusal, match="malformed member ids, keys"):
            _validate_logical_act_conservation(
                malformed_projection,
                {malformed["act_id"]},
                {malformed["act_key"]},
            )


def test_the_perlector_read_loop_refuses_a_clustered_partition_rather_than_reading_it(tmp_path):
    """The named gap 19D's composed acceptance rests on, pinned so it cannot go quiet.

    19D wires no production entrypoint to the logical Archetypus/Armarium
    functions, and that is honest only because the stage upstream of them
    refuses to publish a clustered Perlectio at all: `run.py`'s loop presents
    one local act's own regions at a time, so a clustered partition would get
    one capture-local Perlectio per member (§7.9, §7.15).  19B says so in a
    docstring and raises; nothing measured it.  Without this test the refusal
    could be deleted or weakened in a later slice and the *silent* per-member
    read it exists to prevent would arrive with every suite still green.

    When the cross-capture read loop does land, this test is the deliberate
    edit that records it -- which is the point.
    """
    from logical_reading import _refuse_a_partition_this_loop_cannot_read  # noqa: PLC0415

    fixture = _fixture()
    register_path, physical_page, _physical_act = _register(tmp_path, fixture)
    clustered = _partition(register_path, fixture, physical_page, captures=fixture["captures"])
    assert clustered["findings"] == []
    (logical_act,) = clustered["logical_acts"]
    assert logical_act["identity_scope"] == "physical-act"
    assert len(logical_act["member_local_acts"]) == 2

    with pytest.raises(SchemaRefusal) as refusal:
        _refuse_a_partition_this_loop_cannot_read(clustered)
    message = str(refusal.value)
    assert "clustered logical act" in message
    assert logical_act["logical_act_id"] in message


def test_neither_established_stage_dispatches_by_logical_act_yet():
    """The other half of the same recorded gap, at the stages 19D actually touched.

    `establish_logical_record`, `build_logical_index`, the delivered logical
    projection, and the cross-capture review projection exist and are proved
    by the composition fixture above; no `main()` calls one, because no
    clustered Perlectio can be published for them to consume (see the test
    above).  Asserting the absence keeps the two halves of the gap tied
    together: a future slice that wires either stage must flip this test in the
    same change that makes the refusal above reachable, rather than leaving a
    half-wired path nobody notices.
    """
    logical_callables = (
        "establish_logical_record",
        "build_logical_index",
        "logical_act_projection_entry",
        "logical_cross_capture_review_entry",
    )
    for path in (ARCHETYPUS_RUN, ARMARIUM_RUN):
        tree = ast.parse(path.read_text())
        (main,) = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        ]
        called = {
            node.func.id
            for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not called & set(logical_callables), (
            f"{path.name}::main now dispatches by logical act; the composed-acceptance gap "
            "this test records has closed, and the Perlector refusal test above must close "
            "with it"
        )
