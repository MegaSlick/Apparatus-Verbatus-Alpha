"""The Exemplar door over synthetic byte sources and a sealed local ledger.

The tests exercise the actual RunTree records rather than an in-memory admission
summary.  Every image/PDF byte is created at test time; no real source material is
read or checked in.
"""

import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import door
import pytest
from admission import RefusalReason, load_format_policy, reason_code
from door import SourceEntry, expand_sources, process_sources
from image_formats import MAX_SOURCE_BYTES, validate_png
from PIL import Image
from synthetic_sources import (
    blank_pages_pdf,
    png,
    single_gray_page_pdf,
    tiff,
    two_page_pdf,
)

from common.contracts.approval import (
    ApprovalRecordReference,
    approval_gated_real_ingress_record,
    build_approval_record,
    synthetic_fixture_ingress_record,
)
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash, verify_self_hash
from common.contracts.errors import ApprovalRefusal, ContractError
from common.contracts.stages import DESIGNATOR, DOOR, EXEMPLAR
from common.runtree.store import RunTree
from common.stage import StageContext
from operations.submit import gate, submit

POLICY = load_format_policy()
RECIPES = {"door": "fake-door-v0", "exemplar": "fake-exemplar-v0"}
CHAIRS = ["attestator_1", "attestator_2", "attestator_3"]
ROOT = Path(__file__).resolve().parents[2]
EXEMPLAR_CLI = ROOT / "pipeline" / "1_exemplar" / "run.py"
DESIGNATOR_CLI = ROOT / "pipeline" / "2_designator" / "run.py"


def jpeg(width: int = 5, height: int = 4, *, trailing: bytes = b"") -> bytes:
    """A decoder-backed JPEG, optionally with normal scanner suffix bytes."""
    output = BytesIO()
    Image.new("RGB", (width, height), (22, 44, 66)).save(output, format="JPEG")
    return output.getvalue() + trailing


def multipage_tiff() -> bytes:
    """Two distinct ordinary TIFF pages, made only for this test process."""
    output = BytesIO()
    first = Image.new("L", (4, 3), 19)
    second = Image.new("L", (2, 5), 231)
    first.save(output, format="TIFF", save_all=True, append_images=[second])
    return output.getvalue()


def animated_gif() -> bytes:
    """Two ordinary synthetic frames, distinct enough to prove neither is lost."""
    output = BytesIO()
    first = Image.new("RGB", (4, 3), (17, 17, 17))
    second = Image.new("RGB", (4, 3), (221, 221, 221))
    first.save(
        output,
        format="GIF",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )
    return output.getvalue()


def open_door(tmp_path, sources, *, run_id="r1", ingress=None):
    """A real tree/context writing the door's own artifacts."""
    tree = RunTree.create(
        tmp_path / "runs",
        run_id,
        source_manifest=[
            {
                "relative_path": source.declared_path,
                "sha256": source.declared_sha256,
                "ordinal": source.ordinal,
                **({"bytes": source.declared_size} if source.declared_size is not None else {}),
                **(
                    {"ledger_sha256": source.ledger_sha256}
                    if source.ledger_sha256 is not None
                    else {}
                ),
                **(
                    {"container_page_index": source.container_page_index}
                    if source.container_page_index is not None
                    else {}
                ),
            }
            for source in sources
        ],
        config_digest="c" * 64,
        adapter_recipes=RECIPES,
        witness_chairs=CHAIRS,
        ingress=ingress or synthetic_fixture_ingress_record(),
    )
    return tree, StageContext(
        tree=tree,
        run=tree.read_run(),
        fixture={},
        scenario="test",
        stage=DOOR,
        adapter_revision=RECIPES[DOOR],
        args=None,
        registry=None,
    )


def admissions(tree) -> dict[int, dict]:
    """Door records by page ordinal, read back through the RunTree contract."""
    records = {}
    for entry in tree.build_manifest(DOOR)["artifacts"]:
        if entry["kind"] != "admission":
            continue
        record = tree.read_artifact(DOOR, "admission", entry["artifact_id"])
        records[record["payload"]["ordinal"]] = record
    return records


def reader(files: dict[str, bytes]):
    def read_bytes(path: str) -> bytes:
        try:
            return files[path]
        except KeyError as error:
            raise OSError("synthetic source is absent") from error

    return read_bytes


def test_correct_bytes_admit_even_when_the_filename_extension_is_wrong(tmp_path):
    data = jpeg()
    source = SourceEntry(1, "archive-id-01.png", digest_bytes(data))
    tree, context = open_door(tmp_path, [source])

    assert process_sources(
        context, tree, [source], reader({source.declared_path: data}), policy=POLICY
    )
    context.finish(DOOR)

    record = admissions(tree)[1]
    assert record["outcome"] == "admitted"
    assert record["payload"]["declared_path"] == "archive-id-01.png"
    assert record["payload"]["declared_sha256"] == digest_bytes(data)


def test_every_source_gets_a_named_record_even_when_nothing_admits(tmp_path):
    sources = [
        SourceEntry(1, "not-an-image.png", digest_bytes(b"plain text")),
        SourceEntry(2, "missing-scan.tif", "0" * 64),
    ]
    tree, context = open_door(tmp_path, sources)
    assert (
        process_sources(
            context, tree, sources, reader({"not-an-image.png": b"plain text"}), policy=POLICY
        )
        == 0
    )
    report_path = door.publish_refusal_report(context)
    context.finish(DOOR)

    with pytest.raises(ContractError, match="Private named refusal report"):
        door.require_some_admitted(0, tree, report_path)
    records = admissions(tree)
    assert set(records) == {1, 2}
    assert records[1]["payload"]["declared_path"] == "not-an-image.png"
    assert records[2]["payload"]["declared_path"] == "missing-scan.tif"
    assert reason_code(records[1]["payload"]["reason"]) is RefusalReason.UNRECOGNIZED_FORMAT
    assert reason_code(records[2]["payload"]["reason"]) is RefusalReason.UNREADABLE
    report = next(
        tree.read_artifact(DOOR, "refusal-report", entry["artifact_id"])
        for entry in tree.build_manifest(DOOR)["artifacts"]
        if entry["kind"] == "refusal-report"
    )
    assert report_path == next(
        entry["relative_path"]
        for entry in tree.build_manifest(DOOR)["artifacts"]
        if entry["kind"] == "refusal-report"
    )
    assert verify_self_hash(report["payload"])
    assert report["payload"]["refusals"] == [
        {
            "declared_path": "not-an-image.png",
            "ordinal": 1,
            "reason": records[1]["payload"]["reason"],
        },
        {
            "declared_path": "missing-scan.tif",
            "ordinal": 2,
            "reason": records[2]["payload"]["reason"],
        },
    ]


def test_jpeg_trailing_bytes_are_admitted_not_called_corruption(tmp_path):
    data = jpeg(trailing=b"scanner-padding-after-eoi")
    source = SourceEntry(1, "microfilm-123.jpg", digest_bytes(data))
    tree, context = open_door(tmp_path, [source])

    assert (
        process_sources(
            context, tree, [source], reader({source.declared_path: data}), policy=POLICY
        )
        == 1
    )
    context.finish(DOOR)
    assert admissions(tree)[1]["outcome"] == "admitted"


def test_an_oversized_decoder_alarm_does_not_abort_later_source_accounting(tmp_path):
    huge = tiff(100_000, 2_000, tag_type=4, strip_bytes=1)
    ordinary = png(3, 2)
    sources = [
        SourceEntry(1, "oversized.tif", digest_bytes(huge), container_page_index=0),
        SourceEntry(2, "ordinary.png", digest_bytes(ordinary)),
    ]
    tree, context = open_door(tmp_path, sources)

    assert (
        process_sources(
            context,
            tree,
            sources,
            reader({"oversized.tif": huge, "ordinary.png": ordinary}),
            policy=POLICY,
        )
        == 1
    )
    context.finish(DOOR)

    records = admissions(tree)
    assert reason_code(records[1]["payload"]["reason"]) is RefusalReason.UNSUPPORTED_VARIANT
    assert records[2]["outcome"] == "admitted"


def test_pdf_and_multipage_tiff_fan_out_and_seal_lossless_page_blobs(tmp_path):
    pdf = two_page_pdf()
    tiff = multipage_tiff()
    files = {"iphone-scan.pdf": pdf, "microfilm.tif": tiff}
    sources = expand_sources(
        [
            {"relative_path": path, "sha256": digest_bytes(data), "bytes": len(data)}
            for path, data in files.items()
        ],
        reader(files),
        POLICY,
    )
    assert [(source.declared_path, source.container_page_index) for source in sources] == [
        ("iphone-scan.pdf", 0),
        ("iphone-scan.pdf", 1),
        ("microfilm.tif", 0),
        ("microfilm.tif", 1),
    ]
    tree, context = open_door(tmp_path, sources)

    assert process_sources(context, tree, sources, reader(files), policy=POLICY) == 4
    context.finish(DOOR)

    records = admissions(tree)
    assert len(records) == 4
    assert {
        record["payload"]["rendered_from"]["container_format"] for record in records.values()
    } == {
        "pdf",
        "tiff",
    }
    for record in records.values():
        payload = record["payload"]
        assert record["outcome"] == "admitted"
        assert payload["sha256"] != payload["rendered_from"]["container_sha256"]
        assert validate_png(tree.read_bytes(payload["stored_at"])).format == "png"


def test_every_decoder_reported_animation_frame_fans_out_once(tmp_path):
    data = animated_gif()
    files = {"archive-animation.gif": data}
    sources = expand_sources(
        [
            {"relative_path": path, "sha256": digest_bytes(data), "bytes": len(data)}
            for path in files
        ],
        reader(files),
        POLICY,
    )
    assert [(source.declared_path, source.container_page_index) for source in sources] == [
        ("archive-animation.gif", 0),
        ("archive-animation.gif", 1),
    ]

    tree, context = open_door(tmp_path, sources)
    assert process_sources(context, tree, sources, reader(files), policy=POLICY) == 2
    context.finish(DOOR)

    records = admissions(tree)
    assert [
        records[ordinal]["payload"]["rendered_from"]["container_page_index"]
        for ordinal in sorted(records)
    ] == [0, 1]
    assert records[1]["payload"]["sha256"] != records[2]["payload"]["sha256"]


# `tiff_lzw` and `tiff_adobe_deflate` are what real flatbed and archival scanning
# software writes by default; `packbits` is the baseline TIFF 6.0 compression; and
# `group4` is CCITT fax, which is what microfilm and bitonal register scans arrive
# as. This is the gap one lane left open and named as the thing it was least sure
# about — every page still got an ordinal there, but only an uncompressed directory
# actually rendered, so a compressed page reached the Exemplar as a named alarm and
# not as pixels. It is closed by using a decoder that reads these codecs rather than
# by hand-writing four decompressors.
TIFF_COMPRESSIONS = ["tiff_lzw", "tiff_adobe_deflate", "packbits", "group4"]


def compressed_multipage_tiff(compression: str) -> bytes:
    """Two distinct TIFF pages under one of the compressions scanners produce."""
    output = BytesIO()
    mode = "1" if compression == "group4" else "L"
    first = Image.new("L", (4, 3), 19).convert(mode)
    second = Image.new("L", (2, 5), 231).convert(mode)
    first.save(
        output,
        format="TIFF",
        save_all=True,
        append_images=[second],
        compression=compression,
    )
    return output.getvalue()


@pytest.mark.parametrize("compression", TIFF_COMPRESSIONS)
def test_a_compressed_multipage_tiff_fans_out_and_every_page_reaches_real_pixels(
    tmp_path, compression
):
    """ "TIFF 100% must work" is not satisfied by an ordinal with no pixels behind it.

    A page that fans out to an ordinal and then refuses is still a page nobody
    reads, which is GOALS 1 failing quietly rather than loudly. So this asserts the
    whole way through: two ordinals, two admitted outcomes, two distinct sealed PNG
    blobs, and the second page's real geometry — not merely that the door noticed
    there were two directories.
    """
    data = compressed_multipage_tiff(compression)
    files = {f"flatbed-{compression}.tif": data}
    sources = expand_sources(
        [
            {"relative_path": path, "sha256": digest_bytes(data), "bytes": len(data)}
            for path in files
        ],
        reader(files),
        POLICY,
    )
    assert [source.container_page_index for source in sources] == [0, 1]

    tree, context = open_door(tmp_path, sources)
    assert process_sources(context, tree, sources, reader(files), policy=POLICY) == 2
    context.finish(DOOR)

    records = admissions(tree)
    assert {record["outcome"] for record in records.values()} == {"admitted"}
    pixels = [validate_png(tree.read_bytes(records[o]["payload"]["stored_at"])) for o in (1, 2)]
    assert [(page.width, page.height) for page in pixels] == [(4, 3), (2, 5)]
    assert records[1]["payload"]["sha256"] != records[2]["payload"]["sha256"]


def test_a_single_page_tiff_is_sealed_as_its_own_untouched_bytes(tmp_path):
    """The common TIFF is one image, and the Exemplar seals the submitted bytes.

    A TIFF is *usually* one page, unlike a PDF, and re-encoding an ordinary scan on
    the way in would spend the Exemplar's immutability (GOVERNANCE 4) for nothing.
    The check that matters is the last assertion: the stored blob is byte-identical
    to what was submitted, not merely an image of the same size.
    """
    data = tiff(6, 5)
    files = {"register-page.tif": data}
    sources = expand_sources(
        [
            {"relative_path": path, "sha256": digest_bytes(data), "bytes": len(data)}
            for path in files
        ],
        reader(files),
        POLICY,
    )
    assert [(source.declared_path, source.container_page_index) for source in sources] == [
        ("register-page.tif", None)
    ]

    tree, context = open_door(tmp_path, sources)
    assert process_sources(context, tree, sources, reader(files), policy=POLICY) == 1
    context.finish(DOOR)

    payload = admissions(tree)[1]["payload"]
    assert "rendered_from" not in payload
    assert payload["sha256"] == digest_bytes(data)
    assert tree.read_bytes(payload["stored_at"]) == data


def test_duplicate_files_are_an_alarm_but_identical_pages_inside_one_pdf_are_not(tmp_path):
    data = png(3, 2)
    sources = [
        SourceEntry(1, "source-a.png", digest_bytes(data)),
        SourceEntry(2, "source-b.png", digest_bytes(data)),
    ]
    tree, context = open_door(tmp_path, sources)
    assert (
        process_sources(
            context,
            tree,
            sources,
            reader({"source-a.png": data, "source-b.png": data}),
            policy=POLICY,
        )
        == 1
    )
    context.finish(DOOR)
    assert reason_code(admissions(tree)[2]["payload"]["reason"]) is RefusalReason.DUPLICATE


def test_expansion_ordinals_are_stable_by_filename_and_page_index():
    pdf = two_page_pdf()
    tiff = multipage_tiff()
    files = {"z.png": png(), "b.tif": tiff, "a.pdf": pdf}
    rows = [
        {"relative_path": path, "sha256": digest_bytes(data), "bytes": len(data)}
        for path, data in files.items()
    ]
    first = expand_sources(rows, reader(files), POLICY)
    second = expand_sources(list(reversed(rows)), reader(files), POLICY)
    assert first == second
    assert [(item.ordinal, item.declared_path, item.container_page_index) for item in first] == [
        (1, "a.pdf", 0),
        (2, "a.pdf", 1),
        (3, "b.tif", 0),
        (4, "b.tif", 1),
        (5, "z.png", None),
    ]


def test_real_run_bindings_change_with_a_renderer_recipe_before_a_page_is_written(monkeypatch):
    class Models:
        witness_chairs = ("attestator_1",)
        adapter_recipes = {"door": "synthetic-door-v0"}

        @staticmethod
        def to_record():
            return {"models": "synthetic"}

    ledger = {
        "files": [{"relative_path": "scan.pdf", "sha256": "a" * 64, "bytes": 12}],
        "self_hash": "b" * 64,
    }
    reference = ApprovalRecordReference("receipts/sha256/" + "c" * 64 + ".json", "c" * 64)
    settings = door.render_config.load_pdf_render_settings(
        minimum_dpi=door.pdf_render.MIN_RENDER_DPI
    )
    baseline = door._real_bindings(
        Models(),
        ledger,
        {"policy": "synthetic"},
        reference,
        POLICY,
        settings,
        door.load_recovery_policy(),
    )
    altered_pdf_recipe = dict(door.pdf_render.renderer_recipe(settings), dpi=301)
    monkeypatch.setattr(door.pdf_render, "renderer_recipe", lambda _settings: altered_pdf_recipe)
    changed = door._real_bindings(
        Models(),
        ledger,
        {"policy": "synthetic"},
        reference,
        POLICY,
        settings,
        door.load_recovery_policy(),
    )

    assert baseline["config_digest"] != changed["config_digest"]


def _approved_submission(tmp_path, files: dict[str, bytes]):
    """Create synthetic source files, policy/approval, and the local filename ledger."""
    approved = tmp_path / "approved"
    source = approved / "source"
    source.mkdir(parents=True)
    for path, data in files.items():
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(approved)]
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps(
            build_approval_record(
                subject_ids=["data-handling-policy"],
                action="data-gate",
                reason="synthetic System 03 proof only",
                target_version_hash=gate.policy_hash(policy),
                timestamp="2026-08-04T12:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    ledger_path = approved / "source-ledger.json"
    ledger = submit.submit(
        source,
        ledger_path,
        approval_record=approval_path,
        policy_path=policy_path,
    )
    return approved, source, policy, policy_path, approval_path, ledger_path, ledger


def _run_real_door(
    monkeypatch, *, run_root, source, policy_path, approval_path, ledger_path, run_id
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "door.py",
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--submission-folder",
            str(source),
            "--submission-manifest",
            str(ledger_path),
            "--approval-record",
            str(approval_path),
            "--data-gate-policy",
            str(policy_path),
        ],
    )
    return door.main()


def test_real_door_binds_the_local_filename_ledger_to_every_run_page(tmp_path, monkeypatch):
    files = {"FS-1234.png": png(4, 3), "iPhone/BATCH-7.pdf": single_gray_page_pdf()}
    approved, source, policy, policy_path, approval, ledger_path, ledger = _approved_submission(
        tmp_path, files
    )
    run_root = approved / "runs"

    assert (
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="real-ledger",
        )
        == 0
    )

    tree = RunTree(run_root, "real-ledger")
    run = tree.read_run()
    assert {row["ledger_sha256"] for row in run["source_manifest"]} == {ledger["self_hash"]}
    assert {
        (row["relative_path"], row["sha256"], row["bytes"]) for row in run["source_manifest"]
    } == {(row["relative_path"], row["sha256"], row["bytes"]) for row in ledger["files"]}
    for record in admissions(tree).values():
        assert record["payload"]["ledger_sha256"] == ledger["self_hash"]
        assert (
            record["payload"]["data_gate_approval_ref"] == run["ingress"]["data_gate_approval_ref"]
        )

    before = {
        path.relative_to(run_root): path.read_bytes()
        for path in run_root.rglob("*")
        if path.is_file()
    }
    assert (
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="real-ledger",
        )
        == 0
    )
    after = {
        path.relative_to(run_root): path.read_bytes()
        for path in run_root.rglob("*")
        if path.is_file()
    }
    assert after == before

    sealed = subprocess.run(
        [
            sys.executable,
            str(EXEMPLAR_CLI),
            "--run-root",
            str(run_root),
            "--run-id",
            "real-ledger",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert sealed.returncode == 0, sealed.stderr
    pages = [
        tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
    ]
    assert {page["payload"]["ledger_sha256"] for page in pages} == {ledger["self_hash"]}
    assert run["ingress"]["data_gate_policy_hash"] == gate.policy_hash(policy)

    before_designator = tree.build_manifest(DESIGNATOR)
    boundary = subprocess.run(
        [
            sys.executable,
            str(DESIGNATOR_CLI),
            "--run-root",
            str(run_root),
            "--run-id",
            "real-ledger",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert boundary.returncode == 2
    assert "filename-ledger boundary reconciled" in boundary.stderr
    assert tree.build_manifest(DESIGNATOR) == before_designator


def test_changed_transfer_bytes_raise_a_digest_alarm_under_the_original_filename(
    tmp_path, monkeypatch
):
    approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-4321.png": png(4, 3)}
    )
    (source / "FS-4321.png").write_bytes(png(5, 3))

    with pytest.raises(ContractError, match="digest-mismatch"):
        _run_real_door(
            monkeypatch,
            run_root=approved / "runs",
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="changed-copy",
        )
    record = admissions(RunTree(approved / "runs", "changed-copy"))[1]
    assert record["payload"]["declared_path"] == "FS-4321.png"
    assert reason_code(record["payload"]["reason"]) is RefusalReason.DIGEST_MISMATCH


def test_extra_copy_absent_from_the_filename_ledger_stops_before_a_run_is_created(
    tmp_path, monkeypatch
):
    approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-1.png": png()}
    )
    (source / "unledgered.png").write_bytes(png())
    with pytest.raises(ContractError, match="absent from its self-hashed filename ledger"):
        _run_real_door(
            monkeypatch,
            run_root=approved / "runs",
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="unexpected-copy",
        )
    assert not (approved / "runs" / "unexpected-copy" / "run.json").exists()


def test_a_real_run_root_inside_its_submission_folder_is_refused_before_inventory(
    tmp_path, monkeypatch
):
    approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-1.png": png()}
    )
    with pytest.raises(ContractError, match="run root cannot live inside the submitted folder"):
        _run_real_door(
            monkeypatch,
            run_root=source / "runs",
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="contained-run-root",
        )
    assert not (source / "runs").exists()


def test_real_input_refuses_before_opening_a_source_when_approval_evidence_is_missing(tmp_path):
    opened: list[str] = []
    data = png()
    source = SourceEntry(1, "real.png", digest_bytes(data))
    reference = ApprovalRecordReference(f"receipts/sha256/{'a' * 64}.json", "a" * 64)
    policy = gate.load_policy()
    tree, context = open_door(
        tmp_path,
        [source],
        ingress=approval_gated_real_ingress_record(gate.policy_hash(policy), reference),
    )

    with pytest.raises(ApprovalRefusal, match="could not be read"):
        process_sources(
            context,
            tree,
            [source],
            lambda path: opened.append(path) or data,
            policy=POLICY,
            data_policy=policy,
        )
    assert opened == []


def test_a_real_submission_requires_the_local_filename_ledger(tmp_path, monkeypatch):
    approved, source, _policy, policy_path, approval, _ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-2.png": png()}
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "door.py",
            "--run-root",
            str(approved / "runs"),
            "--run-id",
            "no-ledger",
            "--submission-folder",
            str(source),
            "--approval-record",
            str(approval),
            "--data-gate-policy",
            str(policy_path),
        ],
    )
    with pytest.raises(ContractError, match="requires --submission-manifest"):
        door.main()


def test_two_byte_identical_pages_inside_one_container_are_both_kept(tmp_path):
    """Two blank pages of a scanned book are two pages, not one page and a duplicate.

    The duplicate rule and the fan-out rule meet here and could contradict each
    other: a scanned volume routinely holds several byte-identical blank or ruled
    pages, and collapsing the second into "already admitted as source-1" loses a
    page that genuinely exists — GOALS 1, in the place the old door failed.

    The test beside this one is named for this case and never exercised it: its body
    submits two identical PNG *files* and stops there, so the half of its name about
    pages inside one container was covered by nothing.
    """
    data = blank_pages_pdf(2, width=8, height=6)
    files = {"scanned-volume.pdf": data}
    sources = expand_sources(
        [
            {"relative_path": path, "sha256": digest_bytes(data), "bytes": len(data)}
            for path in files
        ],
        reader(files),
        POLICY,
    )
    assert [source.container_page_index for source in sources] == [0, 1]

    tree, context = open_door(tmp_path, sources)
    assert process_sources(context, tree, sources, reader(files), policy=POLICY) == 2
    context.finish(DOOR)

    records = admissions(tree)
    assert {record["outcome"] for record in records.values()} == {"admitted"}
    # The two pages render to identical pixels, deliberately: blobs are
    # content-addressed, so both admissions reference one stored blob. Two
    # ordinals, two admissions, one blob — and no duplicate refusal anywhere.
    assert records[1]["payload"]["sha256"] == records[2]["payload"]["sha256"]


def test_a_second_copy_of_one_container_is_a_duplicate_file_not_two_lost_pages(tmp_path):
    """The same two rules meeting from the other side.

    Pages of one file are never duplicates of each other; two copies of one file
    under different names are. A two-page PDF submitted twice produces four slots:
    two admitted pages and two named duplicates. Neither rule may quietly become
    the other.
    """
    data = two_page_pdf()
    files = {"scan-1.pdf": data, "scan-2.pdf": data}
    sources = expand_sources(
        [
            {"relative_path": path, "sha256": digest_bytes(payload), "bytes": len(payload)}
            for path, payload in files.items()
        ],
        reader(files),
        POLICY,
    )
    assert len(sources) == 4

    tree, context = open_door(tmp_path, sources)
    assert process_sources(context, tree, sources, reader(files), policy=POLICY) == 2
    context.finish(DOOR)

    records = admissions(tree)
    assert [records[ordinal]["outcome"] for ordinal in sorted(records)] == [
        "admitted",
        "admitted",
        "refused",
        "refused",
    ]
    for ordinal in (3, 4):
        assert reason_code(records[ordinal]["payload"]["reason"]) is RefusalReason.DUPLICATE
        assert records[ordinal]["payload"]["declared_path"] == "scan-2.pdf"


def test_two_identical_broken_sources_are_each_told_the_truth_about_themselves(tmp_path):
    """A refused source is never the "first admission" a later duplicate names.

    The duplicate reason says "identical content already admitted as source-N". If a
    second copy of a corrupt file were given that reason, the record would assert an
    admission that never happened (GOVERNANCE 10), and the census would read "one
    corrupt file, one duplicate" when the truth is two corrupt files, each needing
    the same fix.

    **What actually protects this is the order of the two checks**, not the line that
    registers the digest — both were broken in turn to find out, and only reordering
    the duplicate check above the refusal check changed this test's outcome. Refusing
    a source on its own merits before ever consulting `seen_sources` is the property
    being asserted here.
    """
    data = b"not an image at all"
    sources = [
        SourceEntry(1, "broken-a.png", digest_bytes(data)),
        SourceEntry(2, "broken-b.png", digest_bytes(data)),
    ]
    tree, context = open_door(tmp_path, sources)
    assert (
        process_sources(
            context,
            tree,
            sources,
            reader({"broken-a.png": data, "broken-b.png": data}),
            policy=POLICY,
        )
        == 0
    )
    context.finish(DOOR)

    records = admissions(tree)
    for ordinal in (1, 2):
        assert (
            reason_code(records[ordinal]["payload"]["reason"]) is RefusalReason.UNRECOGNIZED_FORMAT
        )


def test_an_oversized_source_is_named_too_large_without_ever_being_read(tmp_path):
    """A file past the admission limit is refused from its recorded size alone.

    The real submission path keeps an over-size file's digest and byte count and
    deliberately drops its bytes, so this branch is what stands between a four-
    gigabyte file and an attempt to hold it in memory. The reader here raises if it
    is called at all, which is the assertion that matters.
    """

    def refuse_to_read(relative_path: str) -> bytes:
        raise AssertionError(f"{relative_path} was read despite its recorded size")

    source = SourceEntry(1, "enormous.tif", "0" * 64, None, MAX_SOURCE_BYTES + 1, None)
    tree, context = open_door(tmp_path, [source])

    assert process_sources(context, tree, [source], refuse_to_read, policy=POLICY) == 0
    context.finish(DOOR)

    payload = admissions(tree)[1]["payload"]
    assert reason_code(payload["reason"]) is RefusalReason.TOO_LARGE
    assert payload["declared_path"] == "enormous.tif"


def test_a_page_container_declared_without_a_page_index_is_refused_not_guessed_at(tmp_path):
    """A stale or hand-built manifest must not silently seal page one of a document.

    A PDF reaching `decide` with no page index means the expansion that assigns one
    ordinal per page did not happen for it. Guessing page zero would seal the first
    page of a document and lose the rest with nothing to show for them.
    """
    data = two_page_pdf()
    source = SourceEntry(1, "iphone-scan.pdf", digest_bytes(data))

    decision = door.decide(data, source, POLICY)

    assert decision.outcome == "refused"
    assert reason_code(decision.reason) is RefusalReason.UNSUPPORTED_VARIANT
    assert "must be declared with a page index" in decision.reason


def test_a_page_index_on_a_one_frame_image_refuses_without_asserting_a_falsehood(tmp_path):
    """The mirror case, and the wording is the point.

    An ordinary PNG carrying a page index is a manifest that disagrees with the
    file. The refusal says what was actually observed — declared with a page index,
    decoder reports one frame — rather than claiming the file is damaged, which it
    is not.
    """
    data = png(3, 2)
    source = SourceEntry(1, "register-page.png", digest_bytes(data), container_page_index=0)

    decision = door.decide(data, source, POLICY)

    assert decision.outcome == "refused"
    assert reason_code(decision.reason) is RefusalReason.UNSUPPORTED_VARIANT
    assert "decoder reports one frame" in decision.reason


def test_a_container_page_whose_bytes_changed_in_transfer_is_a_digest_alarm(tmp_path):
    """`decide` re-checks the ledger digest itself, before it renders anything.

    `process_sources` checks it first, so this is defence in depth — and the kind
    that is worth having, because `decide` is the function that turns bytes into
    sealed pixels. A caller reaching it directly with a changed copy must be told
    the copy changed, not handed a page rendered from it.
    """
    data = two_page_pdf()
    source = SourceEntry(1, "iphone-scan.pdf", "0" * 64, container_page_index=0)

    decision = door.decide(data, source, POLICY)

    assert decision.outcome == "refused"
    assert reason_code(decision.reason) is RefusalReason.DIGEST_MISMATCH


def test_a_caller_owned_folder_is_never_the_declared_synthetic_fixture_root(tmp_path):
    """`--fixture-root` may not be the flag that turns the data-handling gate off.

    Ruling 2026-08-04, item 1: fixture status comes from the declared fixture
    manifest, never from a caller flag, a filename suffix or a folder name. The
    accepting half of this guard is exercised by every fixture run in the suite;
    the refusing half — the half that is the guard — was exercised by nothing.
    """
    caller_owned = tmp_path / "definitely-synthetic"
    caller_owned.mkdir()

    with pytest.raises(ContractError, match="not the declared synthetic fixture root"):
        door.declared_synthetic_fixture_root(str(caller_owned))

    assert door.declared_synthetic_fixture_root(str(ROOT / "proof")) == (ROOT / "proof").resolve()


def test_the_loud_failure_names_the_reasons_rather_than_counting_anonymously(tmp_path):
    """An anonymous "unsupported" counter is the door defect this replaced.

    The terminal may not carry filenames — that is the data-handling policy, and
    the private report is where the names are. What it must carry is *which* alarms
    fired and how many of each, because "3 refused" tells an operator nothing about
    whether the pipeline is broken or the transfer was.
    """
    broken = b"not an image at all"
    sources = [
        SourceEntry(1, "one.png", digest_bytes(broken)),
        SourceEntry(2, "two.tif", "0" * 64),
    ]
    tree, context = open_door(tmp_path, sources)
    assert process_sources(context, tree, sources, reader({"one.png": broken}), policy=POLICY) == 0
    report = door.publish_refusal_report(context)
    context.finish(DOOR)

    with pytest.raises(ContractError) as caught:
        door.require_some_admitted(0, tree, report)

    message = str(caught.value)
    assert "unrecognized-format: 1" in message
    assert "unreadable: 1" in message
    assert "2 source(s) submitted" in message
    assert "one.png" not in message and "two.tif" not in message


def test_the_loud_failure_survives_a_census_it_cannot_read(tmp_path):
    """A damaged record may not replace the failure with a complaint about JSON.

    This path runs only on a bad day, to describe a failure that already happened.
    Masking the primary failure with a secondary one is a worse answer to
    GOVERNANCE 2 than a partial census, so an unreadable record is counted under a
    name that says so and the loud failure still says what it is.
    """
    broken = b"not an image at all"
    source = SourceEntry(1, "one.png", digest_bytes(broken))
    tree, context = open_door(tmp_path, [source])
    assert process_sources(context, tree, [source], reader({"one.png": broken}), policy=POLICY) == 0
    report = door.publish_refusal_report(context)
    context.finish(DOOR)
    entry = next(
        entry for entry in tree.build_manifest(DOOR)["artifacts"] if entry["kind"] == "admission"
    )
    tree.resolve(entry["relative_path"]).write_bytes(b"{ this is not json")

    with pytest.raises(ContractError) as caught:
        door.require_some_admitted(0, tree, report)

    message = str(caught.value)
    assert "the door admitted nothing" in message
    assert "the door's own census could not be read" in message
    assert "Traceback" not in message


def test_the_loud_failure_survives_one_record_it_cannot_make_sense_of(tmp_path):
    """The inner half of the same fallback: the census is read, one row is not.

    Damaging the bytes takes out the whole manifest, so it exercises the outer
    fallback above. This takes out one *record's meaning* while leaving the tree
    structurally sound — a reason outside the closed set, which is precisely the
    free-text refusal this spec replaced. The row is counted under a name that says
    it could not be read, and the other rows still count normally.
    """
    broken = b"not an image at all"
    sources = [
        SourceEntry(1, "one.png", digest_bytes(broken)),
        SourceEntry(2, "two.png", digest_bytes(b"also not an image")),
    ]
    tree, context = open_door(tmp_path, sources)
    assert (
        process_sources(
            context,
            tree,
            sources,
            reader({"one.png": broken, "two.png": b"also not an image"}),
            policy=POLICY,
        )
        == 0
    )
    report = door.publish_refusal_report(context)
    context.finish(DOOR)
    entry = next(
        entry
        for entry in tree.build_manifest(DOOR)["artifacts"]
        if entry["kind"] == "admission" and entry["subject_id"] == "source-1"
    )
    path = tree.resolve(entry["relative_path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["reason"] = "just some free text nobody closed"
    # Preserve the outer transport seal so this exercises the census's closed
    # refusal vocabulary rather than the earlier envelope-integrity guard.
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    report_record = json.loads(tree.read_bytes(report).decode("utf-8"))
    for reference in report_record["inputs"]:
        if reference["relative_path"] == entry["relative_path"]:
            reference["sha256"] = digest_bytes(path.read_bytes())
    report_record["self_hash"] = self_hash(report_record)
    tree.resolve(report).write_bytes(canonical_bytes(report_record))
    tree.write_manifest(DOOR)

    with pytest.raises(ContractError) as caught:
        door.require_some_admitted(0, tree, report)

    message = str(caught.value)
    assert "unreadable record: 1" in message
    assert "unrecognized-format: 1" in message


def test_a_container_that_cannot_be_counted_still_occupies_exactly_one_ordinal(tmp_path):
    """A file that vanishes at expansion time never gets a refusal record at all.

    GOVERNANCE 2: nothing is lost silently. A PDF too damaged to count pages cannot
    be fanned out, so it takes one slot and is refused by name in it — the
    alternative is a submitted file with no outcome anywhere in the run.
    """
    broken_pdf = b"%PDF-1.4\nthis is not a document\n"
    files = {"damaged-scan.pdf": broken_pdf}
    sources = expand_sources(
        [
            {
                "relative_path": path,
                "sha256": digest_bytes(broken_pdf),
                "bytes": len(broken_pdf),
            }
            for path in files
        ],
        reader(files),
        POLICY,
    )
    assert [(source.ordinal, source.declared_path) for source in sources] == [
        (1, "damaged-scan.pdf")
    ]

    tree, context = open_door(tmp_path, sources)
    assert process_sources(context, tree, sources, reader(files), policy=POLICY) == 0
    context.finish(DOOR)

    payload = admissions(tree)[1]["payload"]
    assert payload["declared_path"] == "damaged-scan.pdf"
    assert reason_code(payload["reason"]) is RefusalReason.CORRUPT
