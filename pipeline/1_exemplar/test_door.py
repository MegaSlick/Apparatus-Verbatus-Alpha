"""The Exemplar door over synthetic byte sources and a sealed local ledger.

The tests exercise the actual RunTree records rather than an in-memory admission
summary.  Every image/PDF byte is created at test time; no real source material is
read or checked in.
"""

import gc
import json
import os
import struct
import subprocess
import sys
import weakref
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
    content_page_pdf,
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


def synthetic_decoder_image(format_name: str) -> bytes:
    """Generate ordinary bytes for a door-level installed-codec assertion."""
    output = BytesIO()
    Image.new("RGB", (7, 5), (17, 34, 51)).save(output, format=format_name)
    return output.getvalue()


def multipage_tiff() -> bytes:
    """Two distinct ordinary TIFF pages, made only for this test process."""
    output = BytesIO()
    first = Image.new("L", (4, 3), 19)
    second = Image.new("L", (2, 5), 231)
    first.save(output, format="TIFF", save_all=True, append_images=[second])
    return output.getvalue()


def corrupt_later_tiff_page() -> bytes:
    """A real two-page TIFF whose second ImageWidth value points past EOF."""
    data = bytearray(multipage_tiff())
    endian = "<" if data[:2] == b"II" else ">"
    (first_ifd,) = struct.unpack_from(endian + "I", data, 4)
    (first_entries,) = struct.unpack_from(endian + "H", data, first_ifd)
    next_ifd_at = first_ifd + 2 + first_entries * 12
    (second_ifd,) = struct.unpack_from(endian + "I", data, next_ifd_at)
    second_image_width_entry = second_ifd + 2
    struct.pack_into(endian + "I", data, second_image_width_entry + 4, 2)
    struct.pack_into(endian + "I", data, second_image_width_entry + 8, len(data) + 999_999)
    return bytes(data)


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


def truncated_animated_gif() -> bytes:
    """A real animated GIF cut off inside its second frame's image descriptor.

    Walk the block structure rather than guessing an offset: the byte that makes
    Pillow raise a bare `IndexError` out of `n_frames` sits one past the byte that
    produces an orderly `struct.error`, so a hard-coded length would be a fixture
    that stops proving anything the moment the encoder changes.
    """
    data = animated_gif()
    position = 13
    if data[10] & 0x80:
        position += 3 * (2 ** ((data[10] & 7) + 1))
    descriptors = []
    while position < len(data):
        marker = data[position]
        if marker == 0x2C:
            descriptors.append(position)
            position += 10
            if data[position - 1] & 0x80:
                position += 3 * (2 ** ((data[position - 1] & 7) + 1))
            position += 1
        elif marker == 0x21:
            position += 2
        else:
            break
        while position < len(data) and data[position]:
            position += data[position] + 1
        position += 1
    assert len(descriptors) == 2, "the fixture is meant to be a two-frame GIF"
    return data[: descriptors[1] + 9]


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


class _Sentinel:
    """An ordinary object, which unlike `bytes` can be weak-referenced."""


class _TrackedBytes(bytes):
    """`bytes` carrying a weak-referenceable sentinel that dies exactly when it does.

    Neither `bytes` nor a `bytes` subclass can be weak-referenced directly — a
    variable-length built-in subtype cannot gain `__weakref__` — so liveness is
    watched through an attribute hanging off the body instead.
    """

    def __new__(cls, data: bytes) -> "_TrackedBytes":
        body = super().__new__(cls, data)
        body.sentinel = _Sentinel()
        return body


def test_the_raster_body_cache_holds_one_source_at_a_time(tmp_path):
    """The cache retained the complete bytes of every distinct raster path for the
    whole call, so peak memory grew with the number of raster sources in the
    submission rather than with the largest one — the guarantee this door's docstring
    makes for a reel, granted on the PDF path and given straight back on this one.

    Liveness is watched from inside the reader, because that is the only moment the
    two implementations differ: both drop everything when the call returns. One slot
    loses no re-read avoidance, since `expand_sources` numbers row by row and a
    path's ordinals therefore arrive together.
    """
    pages = {f"scan-{index}.png": png(3, 2 + index) for index in range(1, 5)}
    sources = [
        SourceEntry(index, name, digest_bytes(data))
        for index, (name, data) in enumerate(sorted(pages.items()), start=1)
    ]
    tree, context = open_door(tmp_path, sources)

    handed_out: list[weakref.ref] = []
    reads: list[str] = []
    live_at_each_read: list[int] = []

    def read_bytes(path: str) -> _TrackedBytes:
        gc.collect()
        live_at_each_read.append(sum(1 for ref in handed_out if ref() is not None))
        reads.append(path)
        body = _TrackedBytes(pages[path])
        handed_out.append(weakref.ref(body.sentinel))
        return body

    assert process_sources(context, tree, sources, read_bytes, policy=POLICY) == len(pages)

    assert reads == sorted(pages), f"a source was re-read or skipped: {reads}"
    assert max(live_at_each_read) <= 1, (
        f"more than one raster body was retained at once: {live_at_each_read}"
    )


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


def test_the_real_door_seals_bmp_webp_avif_and_a_generic_decoder_fallback(tmp_path):
    """Format routing is not enough: every route must reach a sealed admission.

    This uses the Door's normal `process_sources`/RunTree publication path rather
    than an in-memory decoder result.  PPM deliberately has no sniffer branch, so
    its row is the generic decoder fallback proof.
    """
    formats = [
        ("BMP", "bitmap.bmp", "bmp"),
        ("WEBP", "lossless.webp", "webp"),
        ("AVIF", "phone-export.avif", "avif"),
        ("PPM", "portable-pixmap.ppm", "ppm"),
    ]
    files = {path: synthetic_decoder_image(encoder) for encoder, path, _ in formats}
    sources = [
        SourceEntry(ordinal, path, digest_bytes(files[path]))
        for ordinal, (_, path, _) in enumerate(formats, start=1)
    ]
    tree, context = open_door(tmp_path, sources)

    assert process_sources(context, tree, sources, reader(files), policy=POLICY) == len(sources)
    context.finish(DOOR)

    records = admissions(tree)
    assert [
        (records[ordinal]["outcome"], records[ordinal]["payload"]["geometry"])
        for ordinal in sorted(records)
    ] == [
        ("admitted", {"width": 7, "height": 5}),
    ] * len(sources)
    assert [records[ordinal]["payload"].get("rendered_from") for ordinal in sorted(records)] == [
        None
    ] * len(sources)
    for ordinal, (_, path, _) in enumerate(formats, start=1):
        payload = records[ordinal]["payload"]
        assert payload["sha256"] == digest_bytes(files[path])
        assert tree.read_bytes(payload["stored_at"]) == files[path]


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
        SourceEntry(1, "oversized.tif", digest_bytes(huge)),
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


def test_a_directoryless_classic_tiff_keeps_its_ordinal_and_is_named_corrupt(tmp_path):
    """The offset-0 TIFF gap: a real file must never vanish from the census.

    Before the fix, `expand_sources` fanned this source to zero ordinals -- not
    admitted, not refused, absent even from the run's source_manifest, exactly the
    silent loss GOVERNANCE 2 forbids. The file beside it must be unaffected
    (harvest #2: per-file, never per-folder).
    """
    import struct as _struct

    corrupt_tiff = b"II*\x00" + _struct.pack("<I", 0) + b"\x00" * 4
    files = {"corrupt-no-ifd.tif": corrupt_tiff, "good.png": png(4, 3)}
    sources = expand_sources(
        [
            {"relative_path": path, "sha256": digest_bytes(data), "bytes": len(data)}
            for path, data in files.items()
        ],
        reader(files),
        POLICY,
    )
    assert [source.declared_path for source in sources] == ["corrupt-no-ifd.tif", "good.png"]
    tree, context = open_door(tmp_path, sources)

    assert process_sources(context, tree, sources, reader(files), policy=POLICY) == 1
    context.finish(DOOR)

    records = admissions(tree)
    assert len(records) == 2
    assert records[1]["payload"]["declared_path"] == "corrupt-no-ifd.tif"
    assert records[1]["outcome"] == "refused"
    assert reason_code(records[1]["payload"]["reason"]) is RefusalReason.CORRUPT
    assert records[2]["payload"]["declared_path"] == "good.png"
    assert records[2]["outcome"] == "admitted"


def test_a_pdf_with_a_bounded_transport_preamble_still_routes_and_admits(tmp_path):
    """The Door routes the same preamble PDF that PDFium can actually open."""
    data = b"\n\n" + single_gray_page_pdf()
    files = {"transfer-wrapped-scan.pdf": data}
    sources = expand_sources(
        [
            {"relative_path": path, "sha256": digest_bytes(bytes_), "bytes": len(bytes_)}
            for path, bytes_ in files.items()
        ],
        reader(files),
        POLICY,
    )
    assert [(source.declared_path, source.container_page_index) for source in sources] == [
        ("transfer-wrapped-scan.pdf", 0)
    ]
    tree, context = open_door(tmp_path, sources)

    assert process_sources(context, tree, sources, reader(files), policy=POLICY) == 1
    context.finish(DOOR)
    assert admissions(tree)[1]["outcome"] == "admitted"


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


def test_a_source_with_no_declared_digest_still_reaches_a_duplicate_report(tmp_path):
    """`SourceEntry.declared_sha256` defaults to `None`, and `decide` and
    `process_sources` both treat a missing declared digest as acceptable — so a run
    could legally admit one and then die in `publish_duplicate_report`, which grouped
    on exactly that optional field. It failed *after* every admission was published,
    which is the worst moment to discover it. Duplicate accounting groups on the
    digest the door itself computed instead."""
    data, other = png(3, 2), png(4, 2)
    sources = [
        SourceEntry(1, "undeclared-a.png", None),
        SourceEntry(2, "undeclared-b.png", None),
        SourceEntry(3, "distinct.png", None),
    ]
    tree, context = open_door(tmp_path, sources)
    assert (
        process_sources(
            context,
            tree,
            sources,
            reader({"undeclared-a.png": data, "undeclared-b.png": data, "distinct.png": other}),
            policy=POLICY,
        )
        == 3
    )

    report = door.publish_duplicate_report(context)
    context.finish(DOOR)

    assert report is not None, "the two identical sources were not reported as duplicates"
    entry = next(
        item
        for item in tree.build_manifest(DOOR)["artifacts"]
        if item["kind"] == "duplicate-report"
    )
    duplicate = json.loads(tree.read_bytes(entry["relative_path"]).decode("utf-8"))["payload"]
    assert duplicate["duplicate_source_count"] == 1
    assert [group["source_sha256"] for group in duplicate["groups"]] == [digest_bytes(data)]
    assert [source["declared_path"] for source in duplicate["groups"][0]["sources"]] == [
        "undeclared-a.png",
        "undeclared-b.png",
    ]


@pytest.mark.parametrize("damaged", [b'["a list", 1]', b'"a bare string"', b"17"])
def test_the_refusal_census_survives_an_artifact_that_decodes_to_a_non_object(
    tmp_path, monkeypatch, damaged
):
    """`_refusal_census` promises that nothing in it may raise, because it runs only
    on the failure path to describe a failure that already happened. It indexed
    `record["outcome"]`, so a record decoding to a JSON list, string or number raised
    `TypeError` — which the `except` did not name, replacing "the door admitted
    nothing" with something about JSON. That is the exact substitution the docstring
    rejects: the primary failure masked by a secondary one.

    Damage is injected between the manifest walk and the re-read, because that is the
    only way the two disagree — `build_manifest` validates each envelope, so a file
    already broken on disk is caught one branch earlier. A tree being damaged while
    the failure path reads it is precisely what this function is written for.
    """
    data = png(3, 2)
    sources = [SourceEntry(1, "only.png", digest_bytes(data))]
    tree, context = open_door(tmp_path, sources)
    assert process_sources(context, tree, sources, reader({"only.png": data}), policy=POLICY) == 1
    context.finish(DOOR)

    # Only the admission record is damaged. `build_manifest` verifies referenced blob
    # bytes through this same method, so damaging everything would trip the earlier
    # "census could not be read" branch instead of the one under test.
    admission_path = next(
        entry["relative_path"]
        for entry in tree.build_manifest(DOOR)["artifacts"]
        if entry["kind"] == "admission"
    )
    sound = tree.read_bytes
    monkeypatch.setattr(
        tree,
        "read_bytes",
        lambda relative_path: damaged if relative_path == admission_path else sound(relative_path),
    )

    total, census = door._refusal_census(tree)

    assert total == 1
    assert census == {"unreadable record": 1}


def test_duplicate_files_are_admitted_and_sealed_in_a_private_operator_report(tmp_path, capsys):
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
        == 2
    )
    report = door.publish_duplicate_report(context)
    context.finish(DOOR)
    records = admissions(tree)
    assert {record["outcome"] for record in records.values()} == {"admitted"}
    assert records[2]["payload"]["duplicate_of"] == {
        "first_declared_path": "source-a.png",
        "first_ordinal": 1,
        "source_sha256": digest_bytes(data),
    }
    assert records[1]["payload"]["stored_at"] == records[2]["payload"]["stored_at"]
    assert report is not None
    entry = next(
        item
        for item in tree.build_manifest(DOOR)["artifacts"]
        if item["kind"] == "duplicate-report"
    )
    duplicate = json.loads(tree.read_bytes(entry["relative_path"]).decode("utf-8"))["payload"]
    assert duplicate["duplicate_source_count"] == 1
    assert duplicate["duplicate_ordinal_count"] == 1
    assert duplicate["groups"] == [
        {
            "source_sha256": digest_bytes(data),
            "first_declared_path": "source-a.png",
            "first_ordinal": 1,
            "sources": [
                {"declared_path": "source-a.png", "ordinals": [1]},
                {"declared_path": "source-b.png", "ordinals": [2]},
            ],
        }
    ]
    door._announce_duplicate_report(tree, report)
    summary = capsys.readouterr().err
    assert "1 duplicate source(s) admitted across 1 page ordinal(s)" in summary
    assert "source-a.png" not in summary
    assert "source-b.png" not in summary


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


def test_a_real_door_run_names_and_binds_its_non_fake_implementation_revision(monkeypatch):
    class Models:
        witness_chairs = ("attestator_1",)
        adapter_recipes = {"door": "fake-door-v0"}

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
    assert baseline["adapter_recipes"]["door"] == door.REAL_DOOR_ADAPTER_REVISION
    assert baseline["adapter_recipes"]["door"] != "fake-door-v0"

    monkeypatch.setattr(door, "REAL_DOOR_ADAPTER_REVISION", "exemplar-door-test-change")
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


def test_real_pdf_replaced_after_its_hash_seals_the_opened_original(tmp_path, monkeypatch):
    """PDFium is never given a pathname it can resolve for itself.

    The first PDFium open is expansion's page count. The second is admission,
    after the real door has streamed the source digest. Both must receive an
    already-open stream: `pypdfium2` resolves and reopens a path it is handed, so
    a pathname replaced at that moment used to make it seal the replacement's
    600-pixel page while writing the original's digest.

    What this pins is the *shape* of what PDFium receives, plus the sealed result.
    That the stream is the very descriptor the digest came from — a second
    anchored open would be symlink-safe and still separate the hash from the
    pixels — is pinned by the test below, which replaces the name in the one
    window this one cannot reach.
    """
    original = content_page_pdf(b"", width=72, height=72)
    replacement = content_page_pdf(b"", width=144, height=72)
    approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
        tmp_path, {"register.pdf": original}
    )
    replacement_path = tmp_path / "replacement.pdf"
    replacement_path.write_bytes(replacement)
    original_open_document = door.pdf_render.open_document
    settings = door.render_config.load_pdf_render_settings(
        ROOT / "config" / "pdf_render.toml",
        minimum_dpi=door.pdf_render.MIN_RENDER_DPI,
    )
    opened_original = original_open_document(original)
    try:
        original_width = door.pdf_render.render_page(opened_original, 0, settings).width
    finally:
        door.pdf_render.close_document(opened_original)

    inputs = []

    def replace_before_admission_open(pdf_input, *args, **kwargs):
        inputs.append(pdf_input)
        if len(inputs) == 2:
            os.replace(replacement_path, source / "register.pdf")
        return original_open_document(pdf_input, *args, **kwargs)

    monkeypatch.setattr(door.pdf_render, "open_document", replace_before_admission_open)

    assert (
        _run_real_door(
            monkeypatch,
            run_root=approved / "runs",
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="anchored-pdf",
        )
        == 0
    )

    record = admissions(RunTree(approved / "runs", "anchored-pdf"))[1]
    assert len(inputs) == 2
    assert all(not isinstance(pdf_input, (str, Path)) for pdf_input in inputs)
    assert all(
        all(hasattr(pdf_input, method) for method in ("read", "readinto", "seek", "tell"))
        for pdf_input in inputs
    )
    assert (source / "register.pdf").read_bytes() == replacement
    assert record["outcome"] == "admitted"
    assert record["payload"]["admitted_source_sha256"] == digest_bytes(original)
    assert record["payload"]["geometry"]["width"] == original_width
    assert record["payload"]["geometry"]["width"] != original_width * 2


def test_the_pdf_pdfium_renders_is_the_descriptor_the_digest_was_taken_from(tmp_path, monkeypatch):
    """One anchored open, not two: the hash and the pixels are the same bytes.

    A door that streamed the digest through one verified open and then made a
    second verified open for PDFium would be symlink-safe and still wrong — an
    `os.replace` in the gap between them writes the original's digest beside the
    replacement's pixels, and every check downstream would agree with itself.
    The window is closed by holding one descriptor, so this replaces the name at
    exactly the moment the digest is finished and expects the already-open inode
    to be what gets rendered.
    """
    original = content_page_pdf(b"", width=72, height=72)
    replacement = content_page_pdf(b"", width=144, height=72)
    approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
        tmp_path, {"register.pdf": original}
    )
    replacement_path = tmp_path / "replacement.pdf"
    replacement_path.write_bytes(replacement)
    settings = door.render_config.load_pdf_render_settings(
        ROOT / "config" / "pdf_render.toml",
        minimum_dpi=door.pdf_render.MIN_RENDER_DPI,
    )
    opened_original = door.pdf_render.open_document(original)
    try:
        original_width = door.pdf_render.render_page(opened_original, 0, settings).width
    finally:
        door.pdf_render.close_document(opened_original)

    original_digest_stream = door._source_digest_stream
    replacements = 0

    def replace_the_name_the_moment_its_digest_is_taken(handle):
        nonlocal replacements
        result = original_digest_stream(handle)
        os.replace(replacement_path, source / "register.pdf")
        replacements += 1
        return result

    monkeypatch.setattr(
        door, "_source_digest_stream", replace_the_name_the_moment_its_digest_is_taken
    )

    assert (
        _run_real_door(
            monkeypatch,
            run_root=approved / "runs",
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="one-descriptor-pdf",
        )
        == 0
    )

    record = admissions(RunTree(approved / "runs", "one-descriptor-pdf"))[1]
    assert replacements == 1
    assert (source / "register.pdf").read_bytes() == replacement
    assert record["outcome"] == "admitted"
    assert record["payload"]["admitted_source_sha256"] == digest_bytes(original)
    assert record["payload"]["geometry"]["width"] == original_width
    assert record["payload"]["geometry"]["width"] != original_width * 2


def test_real_pdf_rewritten_during_render_is_refused_before_blob_publication(tmp_path, monkeypatch):
    """An open inode that changes in place cannot become an Exemplar page."""
    original = content_page_pdf(b"", width=72, height=72)
    replacement = content_page_pdf(b"", width=144, height=72)
    approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
        tmp_path, {"register.pdf": original}
    )
    original_open_document = door.pdf_render.open_document
    calls = 0

    def rewrite_before_admission_open(pdf_input, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            # This is deliberately not an atomic rename: the descriptor survives,
            # but its bytes have changed after the ledger comparison.
            (source / "register.pdf").write_bytes(replacement)
        return original_open_document(pdf_input, *args, **kwargs)

    monkeypatch.setattr(door.pdf_render, "open_document", rewrite_before_admission_open)

    with pytest.raises(ContractError, match="the door admitted nothing"):
        _run_real_door(
            monkeypatch,
            run_root=approved / "runs",
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="rewritten-pdf",
        )

    record = admissions(RunTree(approved / "runs", "rewritten-pdf"))[1]
    assert calls == 2
    assert record["outcome"] == "refused"
    assert reason_code(record["payload"]["reason"]) is RefusalReason.DIGEST_MISMATCH
    assert record["inputs"] == []
    assert "stored_at" not in record["payload"]


def test_real_raster_redirected_after_inventory_is_refused_and_recorded(tmp_path, monkeypatch):
    """The bounded raster reader must keep the same no-follow boundary as PDF."""
    approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
        tmp_path, {"register.png": png(4, 3)}
    )
    outside = tmp_path / "outside.png"
    outside.write_bytes(png(7, 5))
    original_inventory = door.inventory.read_submission

    def inventory_then_redirect(folder, *, max_bytes):
        found = original_inventory(folder, max_bytes=max_bytes)
        submitted = source / "register.png"
        submitted.unlink()
        submitted.symlink_to(outside)
        return found

    monkeypatch.setattr(door.inventory, "read_submission", inventory_then_redirect)

    with pytest.raises(ContractError, match="the door admitted nothing"):
        _run_real_door(
            monkeypatch,
            run_root=approved / "runs",
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="redirected-raster",
        )

    record = admissions(RunTree(approved / "runs", "redirected-raster"))[1]
    assert record["outcome"] == "refused"
    assert record["payload"]["declared_path"] == "register.png"
    assert reason_code(record["payload"]["reason"]) is RefusalReason.UNREADABLE
    assert record["inputs"] == []


def test_a_symlink_planted_after_the_walk_refuses_only_its_own_source(tmp_path, monkeypatch):
    """Per-file, never per-folder: the redirected source is the only casualty.

    The digest check already stops the *wrong content* from being sealed either
    way — that is not what this proves. What distinguishes the anchored door from
    the one before it is *when* the redirect is refused: at the reopen itself
    (`unreadable`, proving the target's bytes were never read at all) rather than
    after they were read and hashed (`digest-mismatch`, proving they were). The
    file beside it is untouched and still admits, and no run artifact anywhere
    carries a byte of the material that was never submitted.
    """
    outside_secret = tmp_path / "outside-secret.bin"
    outside_secret.write_bytes(b"NEVER SUBMITTED")

    approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-1.png": png(5, 5), "FS-2.png": png(4, 3)}
    )
    run_root = approved / "runs"
    original_read_submission = door.inventory.read_submission

    def swap_after_the_safe_walk(folder, *, max_bytes):
        found = original_read_submission(folder, max_bytes=max_bytes)
        target = folder / "FS-1.png"
        target.unlink()
        target.symlink_to(outside_secret)
        return found

    monkeypatch.setattr(door.inventory, "read_submission", swap_after_the_safe_walk)

    assert (
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="symlink-race",
        )
        == 0
    )

    records = admissions(RunTree(run_root, "symlink-race"))
    swapped, intact = records[1], records[2]
    assert swapped["payload"]["declared_path"] == "FS-1.png"
    assert swapped["outcome"] == "refused"
    assert reason_code(swapped["payload"]["reason"]) is RefusalReason.UNREADABLE
    assert intact["payload"]["declared_path"] == "FS-2.png"
    assert intact["outcome"] == "admitted"

    for path in run_root.rglob("*"):
        if path.is_file():
            assert b"NEVER SUBMITTED" not in path.read_bytes()


def test_a_forged_nul_byte_manifest_row_refuses_only_itself(tmp_path, monkeypatch):
    """A hand-forged ledger row cannot crash admission for every file beside it.

    No real directory listing can ever produce a NUL byte in a name, so this row
    could only reach the door through a manifest built by hand rather than by
    `submit.py`. Before this fix, `os.open`'s bare `ValueError` for an embedded
    NUL was not caught anywhere between `inventory.open_submission_source` and
    `door.expand_sources`'s own except clause, so it escaped as an uncaught
    traceback and admitted nothing in the same run -- the same "one bad name
    breaks the whole folder" shape this module already fixed once for directory
    depth (harvest #2: per-file, never per-folder).
    """
    approved, source, _policy, policy_path, approval, ledger_path, ledger = _approved_submission(
        tmp_path, {"good.png": png(4, 3)}
    )
    forged = dict(ledger)
    forged["files"] = sorted(
        [*ledger["files"], {"relative_path": "a\x00b", "sha256": "0" * 64, "bytes": 5}],
        key=lambda row: row["relative_path"],
    )
    del forged["self_hash"]
    forged["self_hash"] = self_hash(forged)
    ledger_path.write_bytes(canonical_bytes(forged))

    assert (
        _run_real_door(
            monkeypatch,
            run_root=approved / "runs",
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="forged-nul-row",
        )
        == 0
    )

    records = admissions(RunTree(approved / "runs", "forged-nul-row"))
    assert len(records) == 2
    forged_record = next(r for r in records.values() if r["payload"]["declared_path"] == "a\x00b")
    good_record = next(r for r in records.values() if r["payload"]["declared_path"] == "good.png")
    assert forged_record["outcome"] == "refused"
    assert reason_code(forged_record["payload"]["reason"]) is RefusalReason.UNREADABLE
    assert good_record["outcome"] == "admitted"


def test_a_symlinked_pdf_is_never_handed_to_pdfium_to_count_or_render(tmp_path, monkeypatch):
    """The PDF path is the sharper half of the same gap: PDFium parses whatever it
    opens, in `expand_sources`'s page count *before* any digest is even computed.

    A one-page declared PDF swapped for a two-page PDF at some other path proves
    whether PDFium ever touched the swap target: if it had, this source would fan
    out to two ordinals. It must not — the redirect is refused at the reopen,
    before `pdf_render.count_pages` is ever called on it, so exactly one ordinal
    exists for the declared document, refused, and the file beside it is
    unaffected. Proof by page count, not by reason code alone.
    """
    swap_target = tmp_path / "outside-two-page.pdf"
    swap_target.write_bytes(two_page_pdf())

    approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
        tmp_path, {"SCAN.pdf": single_gray_page_pdf(), "OTHER.png": png(4, 3)}
    )
    run_root = approved / "runs"
    original_read_submission = door.inventory.read_submission

    def swap_after_the_safe_walk(folder, *, max_bytes):
        found = original_read_submission(folder, max_bytes=max_bytes)
        target = folder / "SCAN.pdf"
        target.unlink()
        target.symlink_to(swap_target)
        return found

    monkeypatch.setattr(door.inventory, "read_submission", swap_after_the_safe_walk)

    assert (
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="pdf-symlink-race",
        )
        == 0
    )

    records = admissions(RunTree(run_root, "pdf-symlink-race"))
    assert len(records) == 2
    pdf_record = next(r for r in records.values() if r["payload"]["declared_path"] == "SCAN.pdf")
    png_record = next(r for r in records.values() if r["payload"]["declared_path"] == "OTHER.png")
    assert pdf_record["outcome"] == "refused"
    assert reason_code(pdf_record["payload"]["reason"]) is RefusalReason.UNREADABLE
    assert png_record["outcome"] == "admitted"


def test_a_corrupt_later_tiff_page_keeps_its_good_earlier_page_and_other_sources(
    tmp_path, monkeypatch
):
    files = {"bad-volume.tiff": corrupt_later_tiff_page(), "good-page.png": png(4, 3)}
    approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
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
            run_id="corrupt-tiff-isolated",
        )
        == 0
    )

    records = admissions(RunTree(run_root, "corrupt-tiff-isolated"))
    assert len(records) == 3
    assert records[1]["payload"]["declared_path"] == "bad-volume.tiff"
    assert records[1]["outcome"] == "admitted"
    assert records[2]["payload"]["declared_path"] == "bad-volume.tiff"
    assert records[2]["outcome"] == "refused"
    assert records[3]["payload"]["declared_path"] == "good-page.png"
    assert records[3]["outcome"] == "admitted"


def test_a_truncated_animated_gif_cannot_erase_another_sources_admission(tmp_path, monkeypatch):
    """The same isolation the corrupt-TIFF regression above proves, for the class of
    decoder fault that was still escaping after it.

    A round narrowed `decode_raster`'s catch set to exclude `IndexError` on the
    argument that only this project's own code raises it. Pillow raises it too, from
    `GifImagePlugin._seek` while `n_frames` counts frames — and this file is counted
    inside `expand_sources`, before any admission exists, so the escape did not just
    lose this source: it aborted the expansion and every other source in the
    submission with it. Two sources in, two records out is the whole assertion."""
    files = {"broken-animation.gif": truncated_animated_gif(), "good-page.png": png(4, 3)}
    approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
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
            run_id="truncated-gif-isolated",
        )
        == 0
    )

    records = admissions(RunTree(run_root, "truncated-gif-isolated"))
    assert len(records) == 2
    by_name = {record["payload"]["declared_path"]: record for record in records.values()}
    assert by_name["broken-animation.gif"]["outcome"] == "refused"
    assert by_name["good-page.png"]["outcome"] == "admitted"


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


def test_a_ledgered_file_absent_from_the_folder_keeps_its_ordinal_and_is_named(
    tmp_path, monkeypatch
):
    """The denominator starts at what was *submitted*, not at what survived.

    A file the sealed ledger names and the folder no longer holds is the door's
    ordinary per-source alarm: it keeps its ordinal, is refused by name, and the
    source beside it still admits. It may not vanish into a smaller corpus that
    later looks complete (GOVERNANCE 2), and it may not abort the whole census.
    """
    approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-1.png": png(4, 3), "FS-2.png": png(5, 5)}
    )
    (source / "FS-1.png").unlink()

    assert (
        _run_real_door(
            monkeypatch,
            run_root=approved / "runs",
            source=source,
            policy_path=policy_path,
            approval_path=approval,
            ledger_path=ledger_path,
            run_id="vanished-source",
        )
        == 0
    )

    records = admissions(RunTree(approved / "runs", "vanished-source"))
    assert len(records) == 2
    assert records[1]["payload"]["declared_path"] == "FS-1.png"
    assert records[1]["outcome"] == "refused"
    assert reason_code(records[1]["payload"]["reason"]) is RefusalReason.UNREADABLE
    assert records[2]["payload"]["declared_path"] == "FS-2.png"
    assert records[2]["outcome"] == "admitted"


def test_a_real_run_root_inside_its_submission_folder_is_refused_before_inventory(
    tmp_path, monkeypatch
):
    _approved, source, _policy, policy_path, approval, ledger_path, _ledger = _approved_submission(
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


def test_a_second_copy_of_one_container_keeps_all_pages_and_flags_the_source_duplicate(tmp_path):
    """The same two rules meeting from the other side.

    Pages of one file are never duplicates of each other; two copies of one file
    under different names are. A two-page PDF submitted twice produces four slots:
    four admitted pages. The second filename is a duplicate fact, not a refusal;
    neither rule may quietly become the other.
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
    assert process_sources(context, tree, sources, reader(files), policy=POLICY) == 4
    report = door.publish_duplicate_report(context)
    context.finish(DOOR)

    records = admissions(tree)
    assert [records[ordinal]["outcome"] for ordinal in sorted(records)] == ["admitted"] * 4
    for ordinal in (3, 4):
        assert records[ordinal]["payload"]["declared_path"] == "scan-2.pdf"
        assert records[ordinal]["payload"]["duplicate_of"]["first_declared_path"] == "scan-1.pdf"
    assert records[1]["payload"]["stored_at"] == records[3]["payload"]["stored_at"]
    assert records[2]["payload"]["stored_at"] == records[4]["payload"]["stored_at"]
    assert report is not None


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


def test_a_stream_backed_pdf_is_not_refused_by_the_retired_bytes_allocation_cap(
    tmp_path, monkeypatch
):
    """The source stays a stream for both its digest and PDFium open, never bytes.

    The cap is monkeypatched below this tiny synthetic PDF rather than allocating a
    64 MiB fixture. If a PDF were ever routed through the raster allocation guard,
    it would refuse here before the reader is called; the page admission below is
    therefore a direct proof that the real anchored-descriptor route -- the one
    every real submission actually takes -- is exempt.
    """
    folder = tmp_path / "batch"
    folder.mkdir()
    data = single_gray_page_pdf()
    (folder / "microfilm-reel.pdf").write_bytes(data)
    source = SourceEntry(1, "microfilm-reel.pdf", digest_bytes(data), 0, len(data), None, "pdf")

    def unexpected_reader(_relative_path: str) -> bytes:
        raise AssertionError("a stream-backed PDF was read as one bytes object")

    def open_source(relative_path: str):
        return door.inventory.open_submission_source(folder, relative_path)

    tree, context = open_door(tmp_path, [source])
    monkeypatch.setattr(door, "MAX_SOURCE_BYTES", 1)
    assert (
        process_sources(
            context, tree, [source], unexpected_reader, policy=POLICY, open_source=open_source
        )
        == 1
    )
    context.finish(DOOR)
    assert admissions(tree)[1]["outcome"] == "admitted"


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


def test_a_page_index_on_a_one_frame_image_names_door_bookkeeping_disagreement(tmp_path):
    """The mirror case, and the wording is the point.

    An ordinary PNG carrying a page index is an internal manifest/decoder
    disagreement. It must not be labelled an unsupported source variant: the Door
    itself constructed the inconsistent page identity.
    """
    data = png(3, 2)
    source = SourceEntry(1, "register-page.png", digest_bytes(data), container_page_index=0)

    with pytest.raises(ContractError, match="pipeline bookkeeping disagreement"):
        door.decide(data, source, POLICY)


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


def test_a_filename_ledger_byte_count_mismatch_has_its_own_named_alarm(tmp_path):
    """The byte-count check must remain distinct from a later digest comparison."""
    data = png(3, 2)
    source = SourceEntry(
        1,
        "changed-length.png",
        digest_bytes(data),
        declared_size=len(data) + 1,
    )
    tree, context = open_door(tmp_path, [source])

    assert (
        process_sources(
            context, tree, [source], reader({source.declared_path: data}), policy=POLICY
        )
        == 0
    )
    context.finish(DOOR)
    reason = admissions(tree)[1]["payload"]["reason"]
    assert reason_code(reason) is RefusalReason.DIGEST_MISMATCH
    assert "now has" in reason
    assert "recorded in its filename ledger" in reason


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
