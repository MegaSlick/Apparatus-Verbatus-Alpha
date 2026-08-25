"""The two-capture cluster path, from 19A correspondence through 19B dissent.

The canonical orchestrator fixtures intentionally remain image-local.  This
module owns the separate fixture that proves a physical act represented by two
captures reaches one atomic establishing call.  Its reader inspects the
delivered crop bytes; green text therefore cannot be produced while silently
dropping either active capture.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from combined import run_logical_passes  # noqa: E402

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of  # noqa: E402
from common.contracts.identities import act_id, physical_page_id  # noqa: E402
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
from common.physical_act_partition import (  # noqa: E402
    append_correspondence_proposal,
    build_correspondence_proposal,
    build_physical_act_partition,
)

FIXTURE = Path(__file__).with_name("fixtures") / "two-capture-leaf-cluster.json"


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
