"""`review` reads a run tree without changing it; `advance` binds the seal the operator confirmed."""

from __future__ import annotations

import errno
import io
import json
import os
import shutil
import struct
import types
import zipfile
from dataclasses import asdict
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ApprovalRefusal, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ARMARIUM
from common.runtree.store import RunTree
from operations.operator import advance, cli, review, review_text, surface
from operations.operator.errors import ErrorCode, OperatorError
from operations.operator.review import ReviewProjection

_EMPTY_VIEW = ReviewProjection("reviewed", (), (), (), (), None, ())

ROOT = Path(__file__).resolve().parents[2]
_EXPORT_REF = {"relative_path": "7_armarium/artifacts/export/art_test.json", "sha256": "e" * 64}


def _make_run(
    orchestrated_run, tmp_path: Path, *, scenario: str = "page-unbroken"
) -> tuple[Path, str]:
    held = scenario == "page-review"
    root = orchestrated_run(
        tmp_path / "runs", "reviewed", scenario, 3 if held else 0, past_held_recensor=held
    )
    return root, "reviewed"


def _boundary_digest(run_root: Path, run_id: str, stage: str = "armarium") -> str:
    return advance.sealed_boundary(RunTree(run_root, run_id), stage)[1]


def test_read_surface_walks_stage_records_seals_census_pages_and_crops(
    orchestrated_run, tmp_path: Path
):
    run_root, run_id = _make_run(orchestrated_run, tmp_path)

    projected = review.ReadOnlyRun(run_root, run_id).projection()

    assert {row["stage"] for row in projected.boundaries} == {
        "door",
        "exemplar",
        "ink-map",
        "designator",
        "attestatores",
        "perlector",
        "recensor",
        "archetypus",
        "coniector",
        "armarium",
    }
    assert all(row["sealed"] and len(row["seal_digest"]) == 64 for row in projected.boundaries)
    assert all("census" in row for row in projected.boundaries)
    tree = RunTree(run_root, run_id)
    # Coverage, not cardinality: a walk that silently skipped a stage or
    # dropped an artifact is the failure this surface exists to prevent, and
    # "more rows than boundaries" would not catch it.
    expected = {
        (stage, row["artifact_id"])
        for stage in review.STAGES
        for row in tree.build_manifest(stage, verify_inputs=False)["artifacts"]
    }
    projected_ids = {(row["stage"], row["artifact_id"]) for row in projected.stage_records}
    assert expected == projected_ids, "the review walk dropped an artifact the manifest lists"
    assert all(
        row["stage"] == row["record"]["stage"]
        and row["artifact_id"] == row["record"]["artifact_id"]
        and row["kind"] == row["record"]["kind"]
        and row["subject_id"] == row["record"]["subject_id"]
        and row["outcome"] == row["record"]["outcome"]
        and row["record_ref"]["sha256"]
        == digest_bytes(tree.read_bytes(row["record_ref"]["relative_path"]))
        for row in projected.stage_records
    )
    assert len(projected.pages) == 2
    assert all(
        row["image_path"].startswith("1_exemplar/blobs/sha256/")
        and "image_data_url" not in row
        and row["record_ref"]["relative_path"].startswith("7_armarium/artifacts/export/")
        for row in projected.pages
    )
    # The fixture delivers two acts on page 1 and one on page 2, each cut by the
    # Perlector from the page it read. Without this count the `all(...)` below
    # is vacuously true over an empty tuple, so a projection that dropped the
    # whole delivered list would still pass the assertion written to catch
    # exactly that loss.
    assert len(projected.acts) == 3
    assert all(
        act["crops"]
        and all(crop["image_path"].startswith("4_perlector/blobs/sha256/") for crop in act["crops"])
        and "image_data_url" not in act["crops"][0]
        and act["record_ref"]["relative_path"].startswith("7_armarium/artifacts/export/")
        for act in projected.acts
    )
    # A complete fixture produces no review rows. `in (None, ())` accepted both
    # answers and so could not tell the two apart: `()` means Armarium's bundle
    # was read and held no review list, `None` means there was no bundle to
    # read at all. Measured against this fixture the answer is `()`; pinning it
    # is what makes a populated list silently becoming `None` a failure.
    assert projected.review_items == ()


def test_held_armarium_review_rows_keep_their_bundle_and_export_record_trace(
    orchestrated_run, tmp_path: Path
):
    run_root, run_id = _make_run(orchestrated_run, tmp_path, scenario="page-review")

    projected = review.ReadOnlyRun(run_root, run_id).projection()

    assert projected.review_items
    for item in projected.review_items:
        assert item["bundle_path"].startswith("7_armarium/blobs/sha256/")
        assert item["member"] == "review-items.jsonl"
        assert item["line"] > 0
        assert item["record_ref"]["relative_path"].startswith("7_armarium/artifacts/export/")
        assert isinstance(item["row"], dict)


def test_unsealed_boundary_is_refused_before_an_advance_record_is_written(tmp_path: Path):
    tree = RunTree.create(
        tmp_path,
        "unsealed",
        source_manifest=[{"relative_path": "page.png", "sha256": "a" * 64, "ordinal": 1}],
        config_digest="b" * 64,
        adapter_recipes={"designator": "fixture"},
        witness_chairs=["attestator_1"],
    )

    with pytest.raises(ApprovalRefusal, match="no stored stage-seal"):
        advance.record_advance(tree, "designator", reason="reviewed", expected_digest="c" * 64)

    assert not (tree.root / "receipts").exists()


def test_the_trigger_refuses_an_unsealed_boundary_before_writing(tmp_path):
    tree = RunTree.create(
        tmp_path,
        "unsealed-trigger",
        source_manifest=[{"relative_path": "page.png", "sha256": "a" * 64, "ordinal": 1}],
        config_digest="b" * 64,
        adapter_recipes={"designator": "fixture"},
        witness_chairs=["attestator_1"],
    )

    with pytest.raises(OperatorError) as excinfo:
        advance.trigger_advance(
            tmp_path,
            tree.run_id,
            "designator",
            reason="not actually sealed",
            expected_digest="c" * 64,
        )

    assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
    assert not (tree.root / "receipts").exists()


def test_the_advance_refuses_a_wrong_digest_over_a_boundary_that_still_verifies(
    orchestrated_run, tmp_path
):
    """Isolate the digest comparison from the seal verification beside it.

    `test_advance_modes.py`'s reseal test accepts either refusal message,
    because forging a census both moves the digest and breaks verification. So
    the digest comparison could be deleted and that test would stay green on
    the verification branch alone. Here the sealed boundary is untouched and
    still verifies; only the supplied digest is wrong, so this refusal can come
    from nothing else.
    """
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    current = _boundary_digest(run_root, run_id)
    wrong = "c" * 64
    assert wrong != current

    with pytest.raises(OperatorError) as refusal:
        advance.trigger_advance(
            run_root,
            run_id,
            "armarium",
            reason="operator reviewed a boundary and named the wrong digest",
            expected_digest=wrong,
        )

    assert refusal.value.code == ErrorCode.ADVANCE_REFUSED
    assert "changed after it was shown for confirmation" in (refusal.value.detail or "")
    # The boundary was never disturbed, so it still verifies afterwards.
    assert advance.sealed_boundary(tree, "armarium")[1] == current
    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before


def test_the_trigger_refuses_an_advance_that_names_no_reviewed_digest(orchestrated_run, tmp_path):
    """A caller handing over `None` or an empty digest is refused.

    Either would otherwise bind the advance to whatever seal happened to be
    current, which is the substitution the typed confirmation exists to
    prevent.
    """
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}

    for absent in (None, ""):
        with pytest.raises(OperatorError) as excinfo:
            advance.trigger_advance(
                run_root,
                run_id,
                "armarium",
                reason="no digest was ever confirmed",
                expected_digest=absent,
            )
        assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
        assert "no reviewed stage-seal digest" in (excinfo.value.detail or "")

    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before


def test_an_explicitly_blank_advance_timestamp_is_refused_not_replaced_with_now(
    orchestrated_run, tmp_path
):
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}

    with pytest.raises(ApprovalRefusal, match="no timestamp"):
        advance.record_advance(
            tree,
            "armarium",
            reason="reviewed",
            timestamp="",
            expected_digest=_boundary_digest(run_root, run_id),
        )

    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before


def test_an_advance_binds_the_seal_digest_the_operator_confirmed(orchestrated_run, tmp_path):
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    _, observed_digest = advance.sealed_boundary(tree, "armarium")

    reference = advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="operator reviewed the sealed Armarium boundary",
        expected_digest=observed_digest,
    )

    after = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    assert len(after - before) == 1
    record = advance.verify_advance(tree, "armarium", reference)
    seal, digest = advance.sealed_boundary(tree, "armarium")
    assert record["subject_ids"] == ["stage-boundary:armarium"]
    assert record["target_version_hash"] == digest == observed_digest
    assert digest == digest_bytes(
        tree.read_bytes(tree.artifact_path("armarium", "stage-seal", seal["artifact_id"]))
    )


def test_an_advance_reason_is_bounded_before_it_can_amplify_a_record():
    with pytest.raises(ApprovalRefusal, match="reason exceeds"):
        advance.validate_advance_reason("x" * (advance.MAX_ADVANCE_REASON_CHARACTERS + 1))


def test_a_boundary_that_stopped_verifying_is_refused_before_a_record_is_written(
    orchestrated_run, tmp_path
):
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    reviewed_digest = _boundary_digest(run_root, run_id, "attestatores")
    receipts_before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    witnessed = next(
        row
        for row in tree.build_manifest("attestatores", verify_inputs=False)["artifacts"]
        if row["kind"] not in {"stage-seal", "decode-environment"}
    )
    tree.resolve(witnessed["relative_path"]).unlink()

    # The seal bytes did not move, so the digest the operator confirmed is
    # still the current one: nothing about equality refuses this advance.
    assert advance.stored_boundary(tree, "attestatores")[1] == reviewed_digest

    with pytest.raises(OperatorError) as excinfo:
        advance.trigger_advance(
            run_root,
            run_id,
            "attestatores",
            reason="the seal no longer witnesses what is on disk",
            expected_digest=reviewed_digest,
        )

    assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
    assert "no longer verifies against the run tree" in (excinfo.value.detail or "")
    assert {
        path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")
    } == receipts_before


def test_an_unreadable_stage_seal_is_a_named_refusal_not_an_unexpected_error(
    orchestrated_run, tmp_path, monkeypatch
):
    """A seal that cannot be read refuses the advance by name, not as an unexpected error."""

    run_root, run_id = _make_run(orchestrated_run, tmp_path)

    def _unreadable(*_arguments, **_keywords):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(cli, "boundary_summary", _unreadable)

    with pytest.raises(OperatorError) as excinfo:
        cli._advance_with_confirmation(run_root, run_id, "armarium", reason="reviewed")

    assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
    assert "Input/output error" in (excinfo.value.detail or "")


def test_credential_free_environment_catches_secrets_beyond_the_named_provider_prefixes():
    """The Perlector chair is contractually swappable to any vendor model.

    A scrubber that only knew today's four provider prefixes would silently
    stop protecting the boundary the day a differently-named vendor
    credential is wired in. The generic name-shape marker this reuses from
    `operations.pod.models` is what already keeps the same class of secret
    out of a durable controller receipt.
    """
    # Values are short and unstructured on purpose: only the env var *name* is
    # ever inspected, and a long value shaped like a real key would be exactly
    # the payload this repository's own ingress scanner exists to reject from
    # a committed test file (`.githooks/check_ingress.py`'s literal-credential
    # rule) — irony a fixture value should not have to test.
    scrubbed = surface.credential_free_environment(
        {
            "OPENAI_API_KEY": "x",
            "SOME_SERVICE_SECRET": "x",
            "VERBATUS_LAUNCH_TOKEN": "x",
            "SAFE": "yes",
        }
    )
    assert scrubbed == {"SAFE": "yes"}


def test_review_marks_invalid_and_advance_refuses_when_a_sealed_inventory_lost_evidence(
    orchestrated_run, tmp_path
):
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    reviewed_digest = _boundary_digest(run_root, run_id, "attestatores")
    receipts_before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    testimony = next(
        row
        for row in tree.build_manifest("attestatores", verify_inputs=False)["artifacts"]
        if row["kind"] == "page-testimonium"
    )
    tree.resolve(testimony["relative_path"]).unlink()

    # The provenance walk reads every stage record, so a deleted artifact
    # refuses the whole projection with the exact file named.
    with pytest.raises(OperatorError) as review_error:
        review.ReadOnlyRun(run_root, run_id).projection()
    assert review_error.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE
    assert testimony["relative_path"] in (review_error.value.detail or "")

    with pytest.raises(OperatorError) as advance_error:
        advance.trigger_advance(
            run_root,
            run_id,
            "attestatores",
            reason="must not advance damaged evidence",
            expected_digest=reviewed_digest,
        )
    assert advance_error.value.code == ErrorCode.ADVANCE_REFUSED
    assert "inventory no longer matches disk" in (advance_error.value.detail or "")
    assert {
        path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")
    } == receipts_before


def _tree_serving(tmp_path: Path, data: bytes) -> types.SimpleNamespace:
    """A stand-in run tree whose one blob really is on disk.

    The projection measures a file before it reads it, so a `read_bytes` stub
    that returned bytes from nowhere would no longer be exercising the path the
    review takes. The projection also checks a claimed path against the
    content-addressed one before any read, so the stand-in answers `blob_path`
    the way a real tree lays its blobs out.
    """

    blob = tmp_path / "blob"
    blob.write_bytes(data)
    return types.SimpleNamespace(
        resolve=lambda _path: blob,
        blob_path=lambda stage, value: (
            f"{'1_exemplar' if stage == 'exemplar' else '2_designator'}/blobs/sha256/{value}"
        ),
    )


def test_review_image_rows_refuse_bytes_that_do_not_match_their_sealed_digest(tmp_path: Path):
    tree = _tree_serving(tmp_path, b"changed page bytes")
    page = {
        "ordinal": 1,
        "page_id": "pg_example",
        "outcome": "sealed",
        "image_path": "1_exemplar/blobs/sha256/" + "0" * 64,
        "image_sha256": "0" * 64,
    }
    act = {
        "act_id": "act_example",
        "act_key": "a1",
        "category": "delivered",
        "source_regions": [
            {
                "source_page_ordinal": 1,
                "region_id": "rgn_example",
                "image_path": "2_designator/blobs/sha256/" + "0" * 64,
                "image_sha256": "0" * 64,
            }
        ],
    }

    with pytest.raises(OperatorError) as page_error:
        review._image_row(tree, page, _EXPORT_REF)
    assert "bytes have digest" in (page_error.value.detail or "")
    assert "page 1" in (page_error.value.detail or "")
    with pytest.raises(OperatorError) as crop_error:
        review._act_row(tree, act, _EXPORT_REF)
    assert "bytes have digest" in (crop_error.value.detail or "")


def test_review_keeps_an_unsealed_page_visible_with_its_reason():
    row = review._image_row(
        types.SimpleNamespace(),
        {"ordinal": 2, "page_id": None, "outcome": "refused", "reason": "decoder refused"},
        _EXPORT_REF,
    )

    assert row == {
        "ordinal": 2,
        "page_id": None,
        "outcome": "refused",
        "reason": "decoder refused",
        "record_ref": _EXPORT_REF,
        "image_path": None,
        "image_sha256": None,
    }


def test_review_refuses_an_armarium_projection_that_omits_an_accounting_list():
    export_id = artifact_id(ARMARIUM, "export", "export", None)
    record = {
        "artifact_id": export_id,
        "payload": {"pages": []},
        "record_ref": _EXPORT_REF,
    }
    tree = types.SimpleNamespace(
        artifact_path=lambda stage, kind, value: _EXPORT_REF["relative_path"],
    )
    stage_records = [
        {
            "stage": ARMARIUM,
            "kind": "export",
            "artifact_id": export_id,
            "relative_path": _EXPORT_REF["relative_path"],
            "record": record,
            "record_ref": _EXPORT_REF,
            "payload": record["payload"],
        }
    ]
    with pytest.raises(OperatorError) as excinfo:
        review._armarium_payload(tree, stage_records)
    assert "delivered value is missing or" in (excinfo.value.detail or "")


def test_unreadable_tree_recovery_never_instructs_an_evidence_repair():
    rendered = OperatorError(ErrorCode.CONSOLE_TREE_UNREADABLE).render()

    assert "Preserve the run tree unchanged" in rendered
    assert "never edit the damaged evidence in place" in rendered
    assert "repair the named evidence" not in rendered


def test_review_refuses_bundle_bytes_that_do_not_match_the_export_reference(tmp_path: Path):
    blob = tmp_path / "bundle-blob"
    blob.write_bytes(b"changed bundle")
    tree = types.SimpleNamespace(
        resolve=lambda _path: blob,
        blob_path=lambda stage, value: f"7_armarium/blobs/sha256/{value}",
    )
    payload = {
        "bundle": {
            "reference": {
                "relative_path": "7_armarium/blobs/sha256/" + "0" * 64,
                "sha256": "0" * 64,
            },
            "sha256": "0" * 64,
        }
    }

    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload, _EXPORT_REF)
    assert "but its bytes have digest" in (excinfo.value.detail or "")


def test_review_hashes_and_parses_one_bundle_handle(tmp_path: Path):
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("review-items.jsonl", b"{}\n")
    data = bundle.getvalue()
    digest = digest_bytes(data)

    class OneOpenBundle:
        def __init__(self):
            self.opens = 0

        def open(self, mode):
            assert mode == "rb"
            self.opens += 1
            assert self.opens == 1
            return io.BytesIO(data)

    source = OneOpenBundle()
    tree = types.SimpleNamespace(
        resolve=lambda _path: source,
        blob_path=lambda stage, value: f"7_armarium/blobs/sha256/{value}",
    )
    payload = {
        "bundle": {
            "reference": {
                "relative_path": f"7_armarium/blobs/sha256/{digest}",
                "sha256": digest,
            },
            "sha256": digest,
        }
    }

    items, total = review._review_items(tree, payload, _EXPORT_REF)
    assert source.opens == 1
    assert total == 1 and items[0]["line"] == 1


def _review_bundle_payload(data: bytes, tmp_path: Path) -> tuple[types.SimpleNamespace, dict]:
    digest = digest_bytes(data)
    blob = tmp_path / "bundle-blob"
    blob.write_bytes(data)
    tree = types.SimpleNamespace(
        resolve=lambda _path: blob,
        blob_path=lambda stage, value: f"7_armarium/blobs/sha256/{value}",
    )
    payload = {
        "bundle": {
            "reference": {
                "relative_path": f"7_armarium/blobs/sha256/{digest}",
                "sha256": digest,
            },
            "sha256": digest,
        }
    }
    return tree, payload


def test_review_refuses_a_row_larger_than_its_input_limit(tmp_path: Path):
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("review-items.jsonl", b"x" * (review.MAX_REVIEW_ITEM_BYTES + 1))
    tree, payload = _review_bundle_payload(bundle.getvalue(), tmp_path)

    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload, _EXPORT_REF)

    assert "line 1 exceeds" in (excinfo.value.detail or "")


def test_no_archive_member_can_be_both_this_name_and_a_directory():
    """Why the directory branch beside the size limit carries no test of its own.

    `ZipInfo.is_dir()` is decided by a trailing separator, and the member filter
    above accepts only the exact name `review-items.jsonl`. The two conditions
    are therefore mutually exclusive: no bundle can reach that branch. It is
    kept as a defensive check and given its own sentence -- a defensive check
    that names the wrong fault is worse than none -- but the state it guards is
    unconstructible, and a test claiming to reach it would be claiming more than
    is true.
    """

    for spelling in ("review-items.jsonl", "review-items.jsonl/"):
        member = zipfile.ZipInfo(spelling)
        assert not (member.filename == review._REVIEW_ITEMS_MEMBER and member.is_dir())


def test_review_pages_across_boundaries_without_loss(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(review, "REVIEW_PAGE_SIZE", 2)
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("review-items.jsonl", b"{}\n" * 5)
    tree, payload = _review_bundle_payload(bundle.getvalue(), tmp_path)

    seen = []
    for page in range(1, 4):
        items, total = review._review_items(tree, payload, _EXPORT_REF, review_page=page)
        assert total == 5
        assert items is not None and len(items) <= review.REVIEW_PAGE_SIZE
        if page == 1:
            lines = review_text.render(
                {
                    "run_id": "reviewed",
                    "review_items": items,
                    "review_items_total": total,
                    "review_page": page,
                    "review_page_size": review.REVIEW_PAGE_SIZE,
                }
            )
            assert "Review queue (5) — page 1, items 1-2" in lines
            assert any("--review-page 2" in line for line in lines)
        seen.extend(item["line"] for item in items)
    assert seen == list(range(1, 6))


def test_projection_selects_a_later_review_page(orchestrated_run, tmp_path: Path, monkeypatch):
    run_root, run_id = _make_run(orchestrated_run, tmp_path, scenario="page-review")
    monkeypatch.setattr(review, "REVIEW_PAGE_SIZE", 2)
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            "review-items.jsonl", b'{"reason":"one"}\n{"reason":"two"}\n{"reason":"three"}\n'
        )
    queue_tree, queue_payload = _review_bundle_payload(bundle.getvalue(), tmp_path)
    read_items = review._review_items

    def use_queue(_tree, _payload, export_ref, *, review_page):
        return read_items(queue_tree, queue_payload, export_ref, review_page=review_page)

    monkeypatch.setattr(review, "_review_items", use_queue)
    projected = review.ReadOnlyRun(run_root, run_id).projection(review_page=2)

    assert projected.review_page == 2
    assert projected.review_page_size == 2
    assert projected.review_items_total == 3
    assert [item["line"] for item in projected.review_items] == [3]
    assert "Review queue (3) — page 2, items 3-3" in review_text.render(asdict(projected))


def test_projection_refuses_review_page_before_one(tmp_path: Path):
    with pytest.raises(OperatorError) as excinfo:
        review.ReadOnlyRun(tmp_path, "absent").projection(review_page=0)
    assert excinfo.value.code == ErrorCode.INVALID_COMMAND
    assert "review page must be at least 1" in (excinfo.value.detail or "")


def test_bad_row_on_later_page_refuses_first_page(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(review, "REVIEW_PAGE_SIZE", 2)
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("review-items.jsonl", b"{}\n{}\nnot json\n")
    tree, payload = _review_bundle_payload(bundle.getvalue(), tmp_path)

    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload, _EXPORT_REF, review_page=1)
    assert "line 3" in (excinfo.value.detail or "")


def test_review_page_byte_allowance_names_the_first_excess_row(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(review, "MAX_REVIEW_PAGE_BYTES", 15)
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("review-items.jsonl", b'{"x":1}\n{"x":2}\n{"x":3}\n')
    tree, payload = _review_bundle_payload(bundle.getvalue(), tmp_path)

    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload, _EXPORT_REF)
    assert "review-items.jsonl line 3" in (excinfo.value.detail or "")
    assert "review page 1 past 15 bytes" in (excinfo.value.detail or "")


def test_review_row_limit_excludes_line_ending(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(review, "MAX_REVIEW_ITEM_BYTES", 2)
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("review-items.jsonl", b"{}\r\n")
    tree, payload = _review_bundle_payload(bundle.getvalue(), tmp_path)

    items, total = review._review_items(tree, payload, _EXPORT_REF)
    assert total == 1
    assert items[0]["line"] == 1


def test_review_refuses_cr_only_rows_by_name(tmp_path: Path):
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("review-items.jsonl", b"{}\r{}\r")
    tree, payload = _review_bundle_payload(bundle.getvalue(), tmp_path)

    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload, _EXPORT_REF)
    assert "review-items.jsonl line 1" in (excinfo.value.detail or "")
    assert "bare carriage return" in (excinfo.value.detail or "")


def test_review_page_past_end_refuses_but_empty_queue_is_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(review, "REVIEW_PAGE_SIZE", 2)
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("review-items.jsonl", b"{}\n{}\n")
    tree, payload = _review_bundle_payload(bundle.getvalue(), tmp_path)
    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload, _EXPORT_REF, review_page=2)
    assert excinfo.value.code == ErrorCode.INVALID_COMMAND
    assert "past the end" in (excinfo.value.detail or "")

    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("review-items.jsonl", b"")
    tree, payload = _review_bundle_payload(empty.getvalue(), tmp_path)
    items, total = review._review_items(tree, payload, _EXPORT_REF)
    assert items == () and total == 0
    assert "Review queue (0) — page 1, empty" in review_text.render(
        {
            "run_id": "reviewed",
            "review_items": items,
            "review_items_total": total,
            "review_page": 1,
            "review_page_size": review.REVIEW_PAGE_SIZE,
        }
    )


def test_a_row_that_is_not_json_names_the_line_it_is_on(tmp_path: Path):
    """A refusal that names only the bundle cannot be acted on.

    A bad row without a position leaves the operator to find it by hand.
    """

    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("review-items.jsonl", b'{"reason":"first"}\n{ not json\n')
    tree, payload = _review_bundle_payload(bundle.getvalue(), tmp_path)

    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload, _EXPORT_REF)

    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "line 2" in (excinfo.value.detail or "")
    assert "not valid JSON" in (excinfo.value.detail or "")


def test_a_row_that_is_not_an_object_names_the_line_it_is_on(tmp_path: Path):
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("review-items.jsonl", b'{"reason":"first"}\n[1, 2]\n')
    tree, payload = _review_bundle_payload(bundle.getvalue(), tmp_path)

    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload, _EXPORT_REF)

    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "line 2" in (excinfo.value.detail or "")
    assert "is not an object" in (excinfo.value.detail or "")


def test_a_run_with_no_armarium_export_is_told_so_rather_than_counted(tmp_path: Path):
    """Zero matches is a missing export, not a tally of duplicates.

    A run halted before Armarium has no export to review at all; "appeared 0
    times" described the arithmetic instead of the condition.
    """

    tree = RunTree(tmp_path / "runs", "reviewed")

    with pytest.raises(OperatorError) as excinfo:
        review._armarium_payload(tree, [])

    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE
    detail = excinfo.value.detail or ""
    assert "no Armarium export record" in detail
    assert "no completed export" in detail
    assert "appeared" not in detail


def test_review_refuses_ambiguous_duplicate_review_members(tmp_path: Path):
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("review-items.jsonl", b'{"reason":"first"}\n')
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("review-items.jsonl", b'{"reason":"second"}\n')
    tree, payload = _review_bundle_payload(bundle.getvalue(), tmp_path)

    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload, _EXPORT_REF)

    assert "more than one review-items.jsonl" in (excinfo.value.detail or "")


def test_a_bad_run_id_is_named_as_such_by_advance_and_review(tmp_path):
    """A mistyped or escaping run id is a refused request, not an unclassified fault.

    `RunTree.__init__` validates the run id and refuses one that resolves
    outside the run root, both as `ContractError`. Built above the guard, a
    typed `--run-id My-Run` reached the blanket handler and told the operator
    to photograph the message and find help over a capital letter, and an
    attempted escape from the approved run root reported itself the same way.
    """
    for act in (
        lambda: cli._advance_with_confirmation(
            tmp_path, "My-Run", "armarium", reason="typed with a capital"
        ),
        lambda: cli._review(tmp_path, "My-Run"),
    ):
        with pytest.raises(OperatorError) as excinfo:
            act()
        # `INVALID_COMMAND`, not a verb-specific refusal: nothing was read and
        # nothing was changed, and a tree-unreadable code would send the
        # operator to preserve evidence that was never opened.
        assert excinfo.value.code == ErrorCode.INVALID_COMMAND
        assert "My-Run" in (excinfo.value.detail or "")
        assert "could not classify" not in excinfo.value.render()
        assert "Preserve the run tree" not in excinfo.value.render()


def test_review_refuses_an_act_whose_export_row_lost_its_crop_list():
    """Absent and empty are different facts and may not share an answer.

    `row.get("source_regions", [])` projected an act whose crop list had gone
    missing as an act with no crops. The operator would see the text, see no
    image, and have no way to tell "this act records no crop" from "the record
    of what I would be approving against the ink is gone".
    """
    with pytest.raises(OperatorError) as excinfo:
        review._act_row(
            types.SimpleNamespace(), {"act_id": "act_example", "act_key": "a1"}, _EXPORT_REF
        )
    assert "source_regions value is missing" in (excinfo.value.detail or "")

    # An act that genuinely records an empty crop list is still projected. It
    # carries a witness basis because a delivered export row does, and one
    # without it is refused by `_normalised_act_row` as a damaged record rather
    # than recovered from the run tree.
    projected = review._act_row(
        types.SimpleNamespace(),
        {"act_id": "act_example", "act_key": "a1", "source_regions": [], "witnesses": []},
        _EXPORT_REF,
    )
    assert projected["crops"] == []

    # A non-delivered act is the one shape whose writer never records the
    # field (pipeline/7_armarium/run.py builds review entries without it), so
    # only there absent is the record as written — while a present non-list
    # is still refused.
    held = review._act_row(
        types.SimpleNamespace(),
        {"act_id": "act_example", "act_key": "a1"},
        _EXPORT_REF,
        requires_crops=False,
    )
    assert held["crops"] == []
    with pytest.raises(OperatorError):
        review._act_row(
            types.SimpleNamespace(),
            {"act_id": "act_example", "act_key": "a1", "source_regions": "gone"},
            _EXPORT_REF,
            requires_crops=False,
        )


def test_review_refuses_a_bundle_it_cannot_follow_instead_of_showing_an_empty_queue():
    """A broken bundle reference must not read as "nothing needs your attention".

    Review items are the acts the pipeline could not settle. Returning `None`
    for a malformed reference gave the operator the same screen as an empty
    queue, so a lost queue and an empty queue were indistinguishable on the
    one surface a person reads. Armarium always records a
    bundle object in its export payload, so an absent bundle is refused too,
    not read as a run with nothing to review.
    """
    tree = types.SimpleNamespace(read_bytes=lambda _path: b"")

    for broken, expected in (
        ({}, "has no bundle"),
        ({"bundle": "not an object"}, "has no bundle"),
        ({"bundle": {}}, "no immutable reference"),
        ({"bundle": {"reference": {"sha256": "0" * 64}}}, "has no single digest"),
        ({"bundle": {"reference": {"relative_path": 7}}}, "has no single digest"),
    ):
        with pytest.raises(OperatorError) as excinfo:
            review._review_items(tree, broken, _EXPORT_REF)
        assert expected in (excinfo.value.detail or ""), broken


def test_review_refuses_a_bundle_member_whose_declared_size_is_false(tmp_path: Path):
    """A lying zip header must become a refusal, not a short queue."""
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("review-items.jsonl", b'{"reason":"real"}\n' * 4096)
    data = bytearray(bundle.getvalue())
    entry = data.rfind(b"PK\x01\x02")
    struct.pack_into("<I", data, entry + 24, 8)  # central-directory uncompressed size

    tree, payload = _review_bundle_payload(bytes(data), tmp_path)
    with pytest.raises(OperatorError) as excinfo:
        review._review_items(tree, payload, _EXPORT_REF)
    assert "could not be read" in (excinfo.value.detail or "")


def test_verify_advance_detects_a_boundary_that_changed_after_it_was_advanced(
    orchestrated_run, tmp_path
):
    """Digest binding must be proven against a boundary that actually changed."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    reference = advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="operator reviewed the sealed Armarium boundary",
        expected_digest=_boundary_digest(run_root, run_id),
    )
    advance.verify_advance(tree, "armarium", reference)

    seal, _ = advance.sealed_boundary(tree, "armarium")
    record = tree.read_artifact("armarium", "stage-seal", seal["artifact_id"])
    record["payload"] = {
        **record["payload"],
        "census": [
            *record["payload"]["census"],
            {"kind": "probe", "outcome": "sealed", "count": 1},
        ],
    }
    record["self_hash"] = self_hash(record)
    tree.resolve(tree.artifact_path("armarium", "stage-seal", seal["artifact_id"])).write_bytes(
        canonical_bytes(record)
    )

    with pytest.raises(ApprovalRefusal, match="boundary changed after it was advanced"):
        advance.verify_advance(tree, "armarium", reference)


def test_two_advance_records_for_one_boundary_are_both_persisted_and_both_visible(
    orchestrated_run, tmp_path
):
    """Append-only means the second advance is a new record, not a silent overwrite.

    Both must actually reach a human: the review projection is the one
    surface a person reads, so a second advance decision that does not
    appear there is a silent loss, even though the
    bytes are safely on disk.
    """
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    digest = _boundary_digest(run_root, run_id)
    first = advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="first reviewer signed off",
        expected_digest=digest,
    )
    second = advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="second reviewer signed off independently",
        expected_digest=digest,
    )
    assert first.relative_path != second.relative_path

    projected = review.ReadOnlyRun(run_root, run_id).projection()
    paths = {record["relative_path"] for record in projected.advance_records}
    assert paths == {first.relative_path, second.relative_path}
    assert all(
        record["subject_ids"] == ["stage-boundary:armarium"] for record in projected.advance_records
    )


def test_review_refuses_an_advance_record_copied_under_a_false_content_address(
    orchestrated_run, tmp_path
):
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    reference = advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="reviewed once",
        expected_digest=_boundary_digest(run_root, run_id),
    )
    false_path = tree.root / "receipts" / "sha256" / f"{'0' * 64}.json"
    false_path.write_bytes(tree.read_bytes(reference.relative_path))

    with pytest.raises(OperatorError) as excinfo:
        review.ReadOnlyRun(run_root, run_id).projection()

    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE
    assert f"recorded {'0' * 64}: the bytes changed under a sealed reference" in (
        excinfo.value.detail
    )


def test_review_refuses_an_in_tree_symlink_at_a_receipt_address(orchestrated_run, tmp_path):
    """An immutable receipt is a regular file, not an alias to equivalent bytes."""

    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    reference = advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="reviewed before the receipt path was replaced",
        expected_digest=_boundary_digest(run_root, run_id),
    )
    receipt = tree.root / reference.relative_path
    alias = tree.root / "same-receipt-bytes.json"
    alias.write_bytes(receipt.read_bytes())
    receipt.unlink()
    receipt.symlink_to(alias)

    with pytest.raises(OperatorError) as excinfo:
        review.ReadOnlyRun(run_root, run_id).projection()

    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "not an immutable regular file" in (excinfo.value.detail or "")


def test_operator_advance_requires_exact_confirmation_of_the_observed_digest(
    orchestrated_run, tmp_path, monkeypatch, capsys
):
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: "not that boundary")

    with pytest.raises(OperatorError) as excinfo:
        cli._advance_with_confirmation(
            run_root,
            run_id,
            "armarium",
            reason="reviewed in review",
        )

    assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before
    assert "seal digest" in capsys.readouterr().out


def test_a_confirmed_operator_advance_reports_its_record(
    orchestrated_run, tmp_path, monkeypatch, capsys
):
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    cli._advance_with_confirmation(
        run_root,
        run_id,
        "armarium",
        reason="reviewed in review",
    )

    projected = review.ReadOnlyRun(run_root, run_id).projection()
    assert len(projected.advance_records) == 1
    assert projected.advance_records[0]["reason"] == "reviewed in review"
    assert "Advance record:" in capsys.readouterr().out


def test_the_advance_verb_records_a_confirmed_advance(
    orchestrated_run, tmp_path, monkeypatch, capsys
):
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    monkeypatch.setattr(cli, "_typed_advance_confirmation", lambda phrase: phrase)

    result = cli.main(
        [
            "--workspace",
            str(ROOT),
            "advance",
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--stage",
            "armarium",
            "--reason",
            "reviewed through the operator verb",
        ]
    )

    assert result == 0
    assert "Advance record:" in capsys.readouterr().out
    projected = review.ReadOnlyRun(run_root, run_id).projection()
    assert [row["reason"] for row in projected.advance_records] == [
        "reviewed through the operator verb"
    ]


def test_a_confirmed_digest_changed_before_the_append_is_refused_without_a_record(
    orchestrated_run, tmp_path
):
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    seal, reviewed_digest = advance.sealed_boundary(tree, "armarium")
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}
    record = tree.read_artifact("armarium", "stage-seal", seal["artifact_id"])
    record["payload"] = {
        **record["payload"],
        "census": [*record["payload"]["census"], {"kind": "changed", "count": 1}],
    }
    record["self_hash"] = self_hash(record)
    tree.resolve(tree.artifact_path("armarium", "stage-seal", seal["artifact_id"])).write_bytes(
        canonical_bytes(record)
    )

    with pytest.raises(OperatorError) as excinfo:
        advance.trigger_advance(
            run_root,
            run_id,
            "armarium",
            reason="reviewed before the change",
            expected_digest=reviewed_digest,
        )

    assert excinfo.value.code == ErrorCode.ADVANCE_REFUSED
    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before


def test_a_boundary_changed_during_the_append_is_retained_but_not_reported_as_success(
    orchestrated_run, tmp_path, monkeypatch
):
    """A newly stale immutable record is a refusal with a recovery location."""

    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    reviewed_digest = _boundary_digest(run_root, run_id)
    original_record_advance = advance.record_advance

    def reseal_after_append(*args, **kwargs):
        result = original_record_advance(*args, **kwargs)
        seal, _digest = advance.sealed_boundary(tree, "armarium")
        record = tree.read_artifact("armarium", "stage-seal", seal["artifact_id"])
        record["payload"] = {
            **record["payload"],
            "census": [*record["payload"]["census"], {"kind": "post-append-change", "count": 1}],
        }
        record["self_hash"] = self_hash(record)
        tree.resolve(tree.artifact_path("armarium", "stage-seal", seal["artifact_id"])).write_bytes(
            canonical_bytes(record)
        )
        return result

    monkeypatch.setattr(advance, "record_advance", reseal_after_append)

    with pytest.raises(OperatorError) as refusal:
        advance.trigger_advance(
            run_root,
            run_id,
            "armarium",
            reason="operator reviewed the pre-append boundary",
            expected_digest=reviewed_digest,
        )

    assert refusal.value.code == ErrorCode.ADVANCE_REFUSED
    assert "advance record receipts/sha256/" in (refusal.value.detail or "")
    assert "was written, but the stage seal changed" in (refusal.value.detail or "")
    projected = review.ReadOnlyRun(run_root, run_id).projection().advance_records
    stale = [
        row for row in projected if row["reason"] == "operator reviewed the pre-append boundary"
    ]
    assert len(stale) == 1
    assert stale[0]["boundary_current"] is False


# --- The trigger: a hostile run tree can lie to a person, never to evidence. -----------


def test_a_path_traversal_image_reference_in_the_run_tree_is_refused_not_read(
    orchestrated_run, tmp_path
):
    """A poisoned Armarium export cannot make review read outside the run tree."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    export_id = artifact_id(ARMARIUM, "export", "export", None)
    record = tree.read_artifact(ARMARIUM, "export", export_id)
    pages = list(record["payload"]["pages"])
    pages[0] = {**pages[0], "image_path": "../../../etc/passwd"}
    record["payload"] = {**record["payload"], "pages": pages}
    record["self_hash"] = self_hash(record)
    tree.resolve(tree.artifact_path(ARMARIUM, "export", export_id)).write_bytes(
        canonical_bytes(record)
    )

    with pytest.raises(OperatorError) as excinfo:
        review.ReadOnlyRun(run_root, run_id).projection()
    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE


@pytest.mark.hostile_local
def test_hostile_projection_content_reaches_the_terminal_only_as_inert_escaped_text(
    tmp_path, monkeypatch, capsys
):
    """A malicious filename or review reason can misinform, never execute or corrupt.

    The plain view escapes every control character as each value reaches a
    line (`review_text.inert`), and `--json` prints `json.dumps` text, which
    escapes every control byte to a literal ``\\u00XX`` sequence.
    """
    hostile = ReviewProjection(
        run_id="hostile",
        stage_records=(),
        boundaries=(),
        pages=(),
        acts=({"act_id": "a1", "act_key": "x\x1b[2Jwiped", "category": "baptism", "crops": []},),
        review_items=({"reason": "adversarial\x1b]0;pwned\x07 escape sequence"},),
        review_items_total=1,
        advance_records=(),
    )
    monkeypatch.setattr(
        cli,
        "ReadOnlyRun",
        lambda root, run_id: types.SimpleNamespace(projection=lambda **_kwargs: hostile),
    )

    cli._review(tmp_path, "hostile")

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "wiped" in out
    assert "pwned" in out


def test_an_advance_naming_a_stage_that_is_not_one_writes_nothing(orchestrated_run, tmp_path):
    """The subject is derived from a closed stage list, never from the request."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    before = {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")}

    with pytest.raises(ApprovalRefusal, match="unknown stage"):
        advance.record_advance(tree, "../../etc", reason="reviewed", expected_digest="c" * 64)

    assert {path.name for path in (tree.root / "receipts" / "sha256").glob("*.json")} == before


def test_a_symlink_inside_the_run_tree_pointing_outside_it_is_refused_not_followed(
    orchestrated_run, tmp_path
):
    """Containment holds for review's own reads, not only for manifest walks.

    Review reads a page image, an act crop and the receipt directory, and a
    symlink is the case a `..` check alone does not answer: the stored
    reference is an ordinary-looking relative path and the escape lives on disk.
    """
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    export_id = artifact_id(ARMARIUM, "export", "export", None)
    record = tree.read_artifact(ARMARIUM, "export", export_id)

    outside = tmp_path / "outside-the-tree.png"
    outside.write_bytes(b"\x89PNG not evidence")
    link = tree.root / "smuggled.png"
    link.symlink_to(outside)

    pages = list(record["payload"]["pages"])
    pages[0] = {**pages[0], "image_path": "smuggled.png"}
    record["payload"] = {**record["payload"], "pages": pages}
    record["self_hash"] = self_hash(record)
    tree.resolve(tree.artifact_path(ARMARIUM, "export", export_id)).write_bytes(
        canonical_bytes(record)
    )

    with pytest.raises(OperatorError) as excinfo:
        review.ReadOnlyRun(run_root, run_id).projection()
    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE


def test_a_symlinked_receipts_directory_is_refused_not_walked(orchestrated_run, tmp_path: Path):
    """The receipt walk is not a manifest walk, so it must assert its own containment.

    `receipts/sha256` sits outside `_inventory_directory`'s protection (a receipt
    is not a stage artifact), and every record found under it is still checked
    for content-addressed self-consistency. That check alone does not stop a
    redirected directory: an attacker who can only swap in a symlink, but who
    also controls the decoy directory's contents, can name their own file
    whatever its own hash is and satisfy that check trivially. A fabricated
    "advance" record would then read as a real approval decision.
    """
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    # Inside the run tree: `RunTree.resolve` then accepts the target, so the
    # explicit symlink refusal is the only thing that can reject it.
    decoy = tree.root / "decoy-receipts"
    decoy.mkdir()
    (tree.root / "receipts").mkdir(exist_ok=True)
    existing = tree.root / "receipts" / "sha256"
    if existing.is_dir():
        shutil.rmtree(existing)
    existing.symlink_to(decoy)

    with pytest.raises(OperatorError) as excinfo:
        review.ReadOnlyRun(run_root, run_id).projection()
    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "receipts/sha256" in excinfo.value.detail
    # The refusal must be the link check, not the containment check.
    assert "is a link" in excinfo.value.detail


def test_an_advance_whose_boundary_later_changed_is_named_stale_where_a_person_reads(
    orchestrated_run, tmp_path
):
    """A moved boundary's advance is still shown, and named stale where a person reads it."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    advance.trigger_advance(
        run_root,
        run_id,
        "armarium",
        reason="reviewed the sealed boundary",
        expected_digest=_boundary_digest(run_root, run_id),
    )

    fresh = review.ReadOnlyRun(run_root, run_id).projection().advance_records
    assert [row["boundary_stage"] for row in fresh] == ["armarium"]
    assert fresh[0]["boundary_current"] is True and fresh[0]["boundary_note"] is None

    seal, _ = advance.sealed_boundary(tree, "armarium")
    record = tree.read_artifact("armarium", "stage-seal", seal["artifact_id"])
    record["payload"] = {
        **record["payload"],
        "census": [
            *record["payload"]["census"],
            {"kind": "probe", "outcome": "sealed", "count": 1},
        ],
    }
    record["self_hash"] = self_hash(record)
    tree.resolve(tree.artifact_path("armarium", "stage-seal", seal["artifact_id"])).write_bytes(
        canonical_bytes(record)
    )

    stale = review.ReadOnlyRun(run_root, run_id).projection().advance_records
    assert len(stale) == 1  # still shown, never dropped
    assert stale[0]["boundary_current"] is False
    assert "changed after this advance was recorded" in stale[0]["boundary_note"]


def _second_armarium_seal(tree: RunTree) -> str:
    """The synthetic reseal must pass validation before display logic is exercised."""
    from common.contracts.identities import attempt_id

    rows = [
        row
        for row in tree.build_manifest(ARMARIUM, verify_inputs=False)["artifacts"]
        if row["kind"] == "stage-seal"
    ]
    assert len(rows) == 1, "the fixture is expected to seal the Armarium exactly once"
    first = tree.read_artifact(ARMARIUM, "stage-seal", rows[0]["artifact_id"])
    attempt = attempt_id(first["subject_id"], "seal", 2)
    # A seal binds the decode-environment record named by its own attempt, so a
    # coherent reseal re-publishes that record under the new attempt too --
    # otherwise the consumer's wrong-decode-environment-name refusal fires
    # before the display logic this helper exists to exercise.
    environment_id = first["payload"]["decode_environment_artifact_id"]
    environment = tree.read_artifact(ARMARIUM, "decode-environment", environment_id)
    second_environment = json.loads(json.dumps(environment))
    second_environment["attempt_id"] = attempt
    second_environment["artifact_id"] = artifact_id(
        ARMARIUM, "decode-environment", environment["subject_id"], attempt
    )
    second_environment["self_hash"] = self_hash(second_environment)
    environment_path = tree.resolve(
        tree.artifact_path(ARMARIUM, "decode-environment", second_environment["artifact_id"])
    )
    environment_path.parent.mkdir(parents=True, exist_ok=True)
    environment_path.write_bytes(canonical_bytes(second_environment))
    second = json.loads(json.dumps(first))
    second["attempt_id"] = attempt
    second["artifact_id"] = artifact_id(ARMARIUM, "stage-seal", first["subject_id"], attempt)
    second["payload"] = {
        **second["payload"],
        "attempt_id": attempt,
        "attempt_ordinal": 2,
        "decode_environment_artifact_id": second_environment["artifact_id"],
        "decode_environment_sha256": digest_bytes(canonical_bytes(second_environment)),
    }
    second["self_hash"] = self_hash(second)
    path = tree.resolve(tree.artifact_path(ARMARIUM, "stage-seal", second["artifact_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(second))
    return second["artifact_id"]


def test_a_resealed_boundary_shows_every_seal_and_names_exactly_one_as_current(
    orchestrated_run, tmp_path: Path
):
    """`current` is a label; superseded seals must remain visible evidence."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    superseded = review.ReadOnlyRun(run_root, run_id).projection()
    before = next(row for row in superseded.boundaries if row["stage"] == ARMARIUM)
    assert [seal["artifact_id"] for seal in before["seals"]] == [before["seal_artifact_id"]]
    assert before["seals"][0]["current"] is True
    assert before["seal_record_ref"] == before["seals"][0]["record_ref"]

    resealed = _second_armarium_seal(tree)

    row = next(
        candidate
        for candidate in review.ReadOnlyRun(run_root, run_id).projection().boundaries
        if candidate["stage"] == ARMARIUM
    )
    assert row["seal_artifact_id"] == resealed, "the later attempt is the one the boundary names"
    assert {seal["artifact_id"] for seal in row["seals"]} == {
        resealed,
        before["seal_artifact_id"],
    }, "the superseded seal is still on the surface a person reads"
    assert [seal["current"] for seal in row["seals"]].count(True) == 1
    assert next(seal for seal in row["seals"] if seal["current"])["artifact_id"] == resealed
    assert all("census" in seal for seal in row["seals"])
    # Each seal carries its own address, so the two are separable in the record
    # rather than two rows sharing one trace back to the current one.
    assert row["seal_record_ref"]["sha256"] != before["seal_record_ref"]["sha256"]
    assert len({seal["record_ref"]["sha256"] for seal in row["seals"]}) == 2
    for seal in row["seals"]:
        assert seal["record_ref"]["sha256"] == digest_bytes(
            tree.read_bytes(seal["record_ref"]["relative_path"])
        )


def test_a_boundary_that_moved_under_two_advances_names_each_one_separately(
    orchestrated_run, tmp_path: Path
):
    """Each advance keeps its own verdict; a stale one remains visible."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    early = advance.trigger_advance(
        run_root,
        run_id,
        ARMARIUM,
        reason="advanced before the boundary was resealed",
        expected_digest=_boundary_digest(run_root, run_id),
    )
    _second_armarium_seal(tree)
    late = advance.trigger_advance(
        run_root,
        run_id,
        ARMARIUM,
        reason="advanced again against the reseal",
        expected_digest=_boundary_digest(run_root, run_id),
    )
    assert early.relative_path != late.relative_path

    records = review.ReadOnlyRun(run_root, run_id).projection().advance_records
    verdicts = {record["relative_path"]: record for record in records}
    assert set(verdicts) == {early.relative_path, late.relative_path}
    assert verdicts[late.relative_path]["boundary_current"] is True
    assert verdicts[late.relative_path]["boundary_note"] is None
    assert verdicts[early.relative_path]["boundary_current"] is False
    assert (
        "changed after this advance was recorded" in verdicts[early.relative_path]["boundary_note"]
    )
    assert all(record["boundary_stage"] == ARMARIUM for record in records)


@pytest.mark.parametrize(
    "evidence",
    ["page", "crop", "bundle", "record"],
    ids=["page-image", "act-crop", "review-bundle", "stage-record"],
)
def test_tampered_evidence_is_refused_naming_the_file_whose_bytes_moved(
    orchestrated_run, tmp_path: Path, evidence: str
):
    """The rendered refusal must name the file; its `__cause__` is not shown."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path, scenario="page-review")
    tree = RunTree(run_root, run_id)
    projected = review.ReadOnlyRun(run_root, run_id).projection()
    if evidence == "page":
        relative = projected.pages[0]["image_path"]
    elif evidence == "crop":
        relative = next(act for act in projected.acts if act["crops"])["crops"][0]["image_path"]
    elif evidence == "bundle":
        assert projected.review_items
        relative = projected.review_items[0]["bundle_path"]
    else:
        relative = next(
            row["record_ref"]["relative_path"]
            for row in projected.stage_records
            if row["kind"] == "stage-seal"
        )
    target = tree.resolve(relative)
    if evidence == "record":
        # Canonical self-hashes ignore trailing whitespace, so change a field to
        # create a semantic tamper rather than alternate JSON serialization.
        record = json.loads(target.read_bytes().decode("utf-8"))
        record["payload"] = {**record["payload"], "census": []}
        target.write_bytes(canonical_bytes(record))
    else:
        target.write_bytes(target.read_bytes() + b"\n")

    with pytest.raises(OperatorError) as raised:
        review.ReadOnlyRun(run_root, run_id).projection()

    assert raised.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert relative in raised.value.detail, "the refusal must name the file whose bytes moved"
    assert relative in raised.value.render(), "and it must survive the render a person reads"


def test_opening_a_run_for_review_changes_no_path_bytes_size_or_mtime(
    orchestrated_run, tmp_path: Path
):
    """The unconfined parent projection must preserve the entire run tree."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path, scenario="page-review")
    root = run_root / run_id

    def census() -> dict[str, object]:
        seen: dict[str, object] = {}
        for path in sorted(root.rglob("*")):
            key = str(path.relative_to(root))
            if path.is_dir():
                seen[key + "/"] = "directory"
            else:
                status = path.stat()
                seen[key] = (digest_bytes(path.read_bytes()), status.st_size, status.st_mtime_ns)
        return seen

    before = census()
    assert before, "the fixture run must have written a tree to walk"

    projected = review.ReadOnlyRun(run_root, run_id).projection()
    assert projected.stage_records and projected.boundaries

    after = census()
    assert set(after) == set(before), "reviewing a run created or removed a path"
    assert after == before, "reviewing a run rewrote evidence it was only meant to read"


def test_a_stage_record_changed_after_inventory_is_not_paired_with_the_old_digest(
    orchestrated_run, tmp_path: Path
):
    """The displayed body and address must describe the same filesystem read."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    manifest_row = next(
        row
        for row in tree.build_manifest(ARMARIUM, verify_inputs=False)["artifacts"]
        if row["kind"] == "export"
    )
    target = tree.resolve(manifest_row["relative_path"])
    changed = json.loads(target.read_bytes().decode("utf-8"))
    changed["payload"] = {**changed["payload"], "scenario": "changed-during-review"}
    changed["self_hash"] = self_hash(changed)
    target.write_bytes(canonical_bytes(changed))

    with pytest.raises(SchemaRefusal, match="changed while the review inventory was being read"):
        review._record_row(tree, ARMARIUM, manifest_row)


def test_export_rows_are_derived_from_the_stage_record_snapshot_not_a_later_reread(
    orchestrated_run,
    tmp_path: Path,
):
    """One projection must not describe two versions of the Armarium export."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    manifest_row = next(
        row
        for row in tree.build_manifest(ARMARIUM, verify_inputs=False)["artifacts"]
        if row["kind"] == "export"
    )
    export_row = review._record_row(tree, ARMARIUM, manifest_row)
    target = tree.resolve(manifest_row["relative_path"])
    changed = json.loads(target.read_bytes().decode("utf-8"))
    changed["payload"] = {**changed["payload"], "scenario": "changed-after-stage-walk"}
    changed["self_hash"] = self_hash(changed)
    target.write_bytes(canonical_bytes(changed))

    payload, record_ref = review._armarium_payload(tree, [export_row])

    assert payload["scenario"] != "changed-after-stage-walk"
    assert payload is export_row["record"]["payload"]
    assert record_ref == export_row["record_ref"]


@pytest.mark.parametrize("evidence", ["page", "crop", "bundle"])
def test_export_references_refuse_a_digest_that_disagrees_with_the_named_bytes(
    orchestrated_run, tmp_path: Path, evidence: str
):
    """Review may not replace a contradictory recorded digest with a fresh one."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path, scenario="page-review")
    tree = RunTree(run_root, run_id)
    export_id = artifact_id(ARMARIUM, "export", "export", None)
    record = tree.read_artifact(ARMARIUM, "export", export_id)
    if evidence == "page":
        pages = json.loads(json.dumps(record["payload"]["pages"]))
        pages[0]["image_sha256"] = "0" * 64
        record["payload"] = {**record["payload"], "pages": pages}
    elif evidence == "crop":
        delivered = json.loads(json.dumps(record["payload"]["delivered"]))
        act = next(row for row in delivered if row["source_regions"])
        act["source_regions"][0]["image_sha256"] = "0" * 64
        record["payload"] = {**record["payload"], "delivered": delivered}
    else:
        bundle = json.loads(json.dumps(record["payload"]["bundle"]))
        bundle["reference"]["sha256"] = "0" * 64
        record["payload"] = {**record["payload"], "bundle": bundle}
    record["self_hash"] = self_hash(record)
    relative = tree.artifact_path(ARMARIUM, "export", export_id)
    tree.resolve(relative).write_bytes(canonical_bytes(record))

    with pytest.raises(OperatorError) as raised:
        review.ReadOnlyRun(run_root, run_id).projection()

    assert raised.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert relative in raised.value.detail
    assert "digest" in raised.value.detail


def test_review_refuses_a_compressed_bundle_member_before_decompressing_it(
    orchestrated_run, tmp_path: Path
):
    """The review bundle is only ever written stored (`build_armarium_bundle`)."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path, scenario="page-review")
    tree = RunTree(run_root, run_id)
    export_id = artifact_id(ARMARIUM, "export", "export", None)
    record = tree.read_artifact(ARMARIUM, "export", export_id)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("review-items.jsonl", b'{"act_id": "a1"}\n' * 4)
    poisoned_digest, _ = tree.put_blob(ARMARIUM, buffer.getvalue())

    bundle = dict(record["payload"]["bundle"])
    bundle["sha256"] = poisoned_digest
    bundle["reference"] = {
        "relative_path": tree.blob_path(ARMARIUM, poisoned_digest),
        "sha256": poisoned_digest,
    }
    record["payload"] = {**record["payload"], "bundle": bundle}
    record["self_hash"] = self_hash(record)
    tree.resolve(tree.artifact_path(ARMARIUM, "export", export_id)).write_bytes(
        canonical_bytes(record)
    )

    with pytest.raises(OperatorError) as excinfo:
        review.ReadOnlyRun(run_root, run_id).projection()

    assert excinfo.value.code == ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "is compressed, not stored" in excinfo.value.detail


@pytest.mark.parametrize(
    "missing", ["pages", "delivered", "non_delivered", "other_readings", "bundle-reference"]
)
def test_review_refuses_a_missing_required_armarium_projection_field(
    orchestrated_run, tmp_path: Path, missing: str
):
    """Absent export evidence is not an empty successful review projection."""
    run_root, run_id = _make_run(orchestrated_run, tmp_path)
    tree = RunTree(run_root, run_id)
    export_id = artifact_id(ARMARIUM, "export", "export", None)
    record = tree.read_artifact(ARMARIUM, "export", export_id)
    payload = dict(record["payload"])
    if missing == "bundle-reference":
        bundle = dict(payload["bundle"])
        bundle.pop("reference")
        payload["bundle"] = bundle
    else:
        payload.pop(missing)
    record["payload"] = payload
    record["self_hash"] = self_hash(record)
    relative = tree.artifact_path(ARMARIUM, "export", export_id)
    tree.resolve(relative).write_bytes(canonical_bytes(record))

    with pytest.raises(OperatorError) as raised:
        review.ReadOnlyRun(run_root, run_id).projection()

    assert raised.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert relative in raised.value.detail
    assert missing.split("-")[0] in raised.value.detail


def test_a_fifo_at_an_image_path_is_refused_rather_than_blocking_review(tmp_path: Path) -> None:
    """Review runs in the operator's process, so a planted FIFO must not hang it."""
    (tmp_path / "run-1" / "images").mkdir(parents=True)
    os.mkfifo(tmp_path / "run-1" / "images" / "page.png")
    tree = RunTree(tmp_path, "run-1")

    with pytest.raises(OperatorError) as failure:
        review._image_digest(tree, "images/page.png", "page image")

    assert failure.value.code is ErrorCode.CONSOLE_TREE_UNREADABLE
    assert "regular file" in str(failure.value.detail)
