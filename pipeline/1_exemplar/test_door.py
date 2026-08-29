"""The Exemplar door over synthetic byte sources and a sealed local ledger.

The tests exercise the actual RunTree records rather than an in-memory admission
summary.  Every image/PDF byte is created at test time; no real source material is
read or checked in.
"""

import ast
import gc
import inspect
import json
import os
import struct
import subprocess
import sys
import weakref
import zlib
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from textwrap import dedent

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

import common.imaging as common_imaging
from common.chairs import load_models_toml
from common.contracts.approval import synthetic_fixture_ingress_record
from common.contracts.canonical import (
    canonical_bytes,
    digest_bytes,
    digest_of,
    self_hash,
    verify_self_hash,
)
from common.contracts.errors import ContractError, IncompatibleReuse
from common.contracts.identities import physical_page_id
from common.contracts.stages import DESIGNATOR, DOOR, EXEMPLAR
from common.corpus_register import members_of
from common.runtree.store import RunTree
from common.stage import (
    DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH,
    DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH,
    StageContext,
    require_triage_modes,
    run_sealed_config_digests,
)
from operations.submit import gate, submit
from operations.triage import instrument, producer
from operations.triage.instrument import load_config as instrument_config
from operations.triage.instrument import producer_recipe

POLICY = load_format_policy()


def test_an_unreadable_corpus_register_refusal_names_what_it_promises(tmp_path):
    """The wording only. What it *claims* about the tree is checked end to end below.

    This calls the helper alone, with no run root and no tree, so nothing here
    observes the ordering the message asserts. On its own it proves only that a
    helper raises with the wording the helper itself contains.
    """
    with pytest.raises(
        ContractError,
        match=(
            "could not be read before run creation; no run or admission record was written; "
            "provide a readable canonical register and retry"
        ),
    ):
        door._read_corpus_register(str(tmp_path / "missing-register.json"))


def _sealed_binding_digests() -> dict[str, str]:
    """The configuration digests every `_real_bindings` caller has to supply.

    Read exactly as the door reads them, from one read each, so a test never seals
    a name under bytes nothing parsed. Kept in one helper because the argument list
    is the shape the fixture path's `run_config_bindings` has to match: the F-S5
    defect was one map growing an entry the other did not.
    """
    return {
        "pdf_render_config_sha256": door.render_config.load_pdf_render_binding(
            minimum_dpi=door.pdf_render.MIN_RENDER_DPI
        ).config_sha256,
        "data_handling_config_sha256": gate.load_policy_binding().config_sha256,
        "designator_padding_config_sha256": door._padding_config_digest(
            DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH
        ),
        "designator_geometry_config_sha256": door._geometry_config_digest(
            DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH
        ),
    }


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


def test_source_expansion_refuses_paths_that_alias_on_default_apfs():
    """A cross-host ledger must not acquire different ordinal-to-file meaning.

    Exact-string uniqueness permits both spellings on Linux, while default APFS
    resolves each pair to one pathname. Refuse before calling the byte reader, so
    the host filesystem never gets to choose which row's evidence was opened.
    """
    data = png(2, 2)
    rows = [
        {"relative_path": path, "sha256": digest_bytes(data), "bytes": len(data)}
        for path in ("Page.PNG", "page.png")
    ]

    with pytest.raises(ContractError, match="alias on default APFS"):
        expand_sources(
            rows,
            lambda _path: pytest.fail("colliding paths must refuse before a source is read"),
            POLICY,
        )


def test_pdf_cleanup_failure_does_not_mask_a_security_refusal(monkeypatch):
    """Cleanup evidence is retained without replacing the refusal in flight."""
    data = single_gray_page_pdf()
    digest = digest_bytes(data)
    source = SourceEntry(
        1,
        "register.pdf",
        digest,
        container_page_index=0,
        declared_size=len(data),
        detected_format="pdf",
    )

    class Opened:
        def __init__(self):
            self.handle = BytesIO(data)

        def assert_unchanged(self, *, expected_sha256):
            assert expected_sha256 == digest

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(door.pdf_render, "open_document", lambda _handle: object())
    monkeypatch.setattr(
        door,
        "decide",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ContractError("security refusal")),
    )

    def fail_close(_document):
        raise door.pdf_render.PdfRefusal(RefusalReason.CORRUPT, "native cleanup failed")

    monkeypatch.setattr(door.pdf_render, "close_document", fail_close)

    with pytest.raises(ContractError, match="security refusal") as refused:
        process_sources(
            object(),
            object(),
            [source],
            lambda _path: pytest.fail("a streamed PDF must not use the raster reader"),
            policy=POLICY,
            pdf_settings=object(),
            open_source=lambda _path: Opened(),
        )

    assert refused.value.__notes__ == ["PDF cleanup also failed: corrupt: native cleanup failed"]


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


def test_triage_producer_recipe_is_the_third_bound_document_path(tmp_path):
    """Validated recipe bytes supply the digest that the Door later binds."""
    from operations.triage.instrument import load_config, producer_recipe

    data = png(4, 3)
    source_digest = digest_bytes(data)
    row = door.triage_manifest.make_row(
        corpus_id="parish-a",
        source_frame_sha256=source_digest,
        frame={"width": 4, "height": 3},
        split=door.triage_manifest.make_split(
            [
                door.triage_manifest.make_part(
                    {"x": 0, "y": 0, "w": 4, "h": 3},
                    {"x": 0, "y": 0, "w": 4, "h": 3},
                    0,
                    colour_mode="keep",
                )
            ]
        ),
        re_shoot_cluster_id=None,
        confidence=0,
        mode="manual",
        actor={"kind": "producer", "identity": "triage-instrument", "revision": "v1"},
        human_override=False,
    )
    manifest_path = tmp_path / "manifest.json"
    recipe_path = tmp_path / "recipe.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": door.triage_manifest.MANIFEST_SCHEMA,
                "corpus_id": "parish-a",
                "records": [row],
            }
        ),
        encoding="utf-8",
    )
    recipe_path.write_text(json.dumps(producer_recipe(load_config())), encoding="utf-8")

    rows, clusters, digests = door.load_triage_decisions(
        manifest_path, producer_recipe_path=recipe_path
    )
    assert rows == {source_digest: row}
    assert clusters == {}
    assert digests == {
        "triage-decision-manifest": digest_bytes(manifest_path.read_bytes()),
        "triage-producer-recipe": digest_bytes(recipe_path.read_bytes()),
    }


def test_a_producer_authored_manifest_cannot_drop_its_producer_recipe(tmp_path):
    data = png(4, 3)
    source_digest = digest_bytes(data)
    row = door.triage_manifest.make_row(
        corpus_id="parish-a",
        source_frame_sha256=source_digest,
        frame={"width": 4, "height": 3},
        split=door.triage_manifest.make_split(
            [
                door.triage_manifest.make_part(
                    {"x": 0, "y": 0, "w": 4, "h": 3},
                    {"x": 0, "y": 0, "w": 4, "h": 3},
                    0,
                    colour_mode="keep",
                )
            ]
        ),
        re_shoot_cluster_id=None,
        confidence=0,
        mode="manual",
        actor={"kind": "producer", "identity": "triage-instrument", "revision": "v1"},
        human_override=False,
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": door.triage_manifest.MANIFEST_SCHEMA,
                "corpus_id": "parish-a",
                "records": [row],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="producer rows.*no triage producer recipe"):
        door.load_triage_decisions(manifest_path)


def test_a_missing_triage_producer_recipe_path_is_a_named_read_refusal(tmp_path):
    """A requested recipe path must refuse by name when it cannot be read."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"schema": door.triage_manifest.MANIFEST_SCHEMA, "corpus_id": "parish-a", "records": []}
        ),
        encoding="utf-8",
    )
    missing = tmp_path / "nonexistent-recipe.json"
    with pytest.raises(ContractError, match="^the triage producer recipe could not be read$"):
        door.load_triage_decisions(manifest_path, producer_recipe_path=missing)


def test_a_triage_document_symlink_is_not_followed(tmp_path):
    target = tmp_path / "recipe-target.json"
    target.write_text(json.dumps(producer_recipe(instrument_config())), encoding="utf-8")
    redirected = tmp_path / "recipe.json"
    redirected.symlink_to(target)
    with pytest.raises(ContractError, match="without following path redirects"):
        door._read_triage_document(redirected, "triage producer recipe")


def test_a_triage_document_does_not_follow_an_intermediate_directory_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "recipe.json").write_text(
        json.dumps(producer_recipe(instrument_config())), encoding="utf-8"
    )
    redirected = tmp_path / "redirected"
    redirected.symlink_to(target, target_is_directory=True)
    with pytest.raises(ContractError, match="without following path redirects"):
        door._read_triage_document(redirected / "recipe.json", "triage producer recipe")


def test_a_triage_document_is_bounded_before_json_deserialization(tmp_path, monkeypatch):
    monkeypatch.setattr(door, "MAX_TRIAGE_DOCUMENT_BYTES", 64)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * 65)
    with pytest.raises(ContractError, match="64-byte document bound"):
        door._read_triage_document(oversized, "triage producer recipe")


def test_a_triage_document_path_replacement_cannot_change_the_opened_bytes(tmp_path, monkeypatch):
    recipe_path = tmp_path / "recipe.json"
    original = json.dumps(producer_recipe(instrument_config())).encode("utf-8")
    recipe_path.write_bytes(original)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b"not the opened document")
    real_open = door.os.open

    def open_then_replace(path, flags, *, dir_fd=None):
        descriptor = real_open(path, flags, dir_fd=dir_fd)
        if path == recipe_path.name and not flags & door.os.O_DIRECTORY:
            replacement.replace(recipe_path)
        return descriptor

    monkeypatch.setattr(door.os, "open", open_then_replace)
    raw, _document = door._read_triage_document(recipe_path, "triage producer recipe")
    assert raw == original


def test_a_non_json_triage_producer_recipe_names_its_exact_parse_failure(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"schema": door.triage_manifest.MANIFEST_SCHEMA, "corpus_id": "parish-a", "records": []}
        ),
        encoding="utf-8",
    )
    bad_recipe = tmp_path / "recipe.json"
    bad_recipe.write_text("not JSON", encoding="utf-8")
    with pytest.raises(ContractError, match="the triage producer recipe is not valid UTF-8 JSON"):
        door.load_triage_decisions(manifest_path, producer_recipe_path=bad_recipe)


@pytest.mark.parametrize(
    "ambiguous",
    [
        b'{"schema":"triage-producer-recipe.v1","schema":"other"}',
        b'{"nested":' + b"[" * 20_000 + b"]" * 20_000 + b"}",
    ],
)
def test_ambiguous_or_pathologically_nested_triage_json_is_a_named_refusal(tmp_path, ambiguous):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"schema": door.triage_manifest.MANIFEST_SCHEMA, "corpus_id": "parish-a", "records": []}
        ),
        encoding="utf-8",
    )
    bad_recipe = tmp_path / "recipe.json"
    bad_recipe.write_bytes(ambiguous)
    # A duplicate member refuses at parse time. Pathological nesting refuses at
    # parse time when the interpreter's C recursion headroom runs out first, or
    # at closed-schema validation when the parser happens to survive the depth;
    # either way it is a named ContractError, never an escaping crash, which is
    # the guarantee this test exists for.
    with pytest.raises(
        ContractError,
        match="the triage producer recipe is (not valid UTF-8 JSON|invalid)",
    ):
        door.load_triage_decisions(manifest_path, producer_recipe_path=bad_recipe)


def test_a_malformed_triage_producer_recipe_is_refused_by_name(tmp_path):
    """A recipe that fails its closed schema names validation, not JSON parsing."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"schema": door.triage_manifest.MANIFEST_SCHEMA, "corpus_id": "parish-a", "records": []}
        ),
        encoding="utf-8",
    )
    bad_recipe = tmp_path / "recipe.json"
    bad_recipe.write_text(json.dumps({"schema": "not-the-real-schema"}), encoding="utf-8")
    with pytest.raises(ContractError, match="the triage producer recipe is invalid"):
        door.load_triage_decisions(manifest_path, producer_recipe_path=bad_recipe)


def test_triage_digest_mismatch_is_a_named_door_refusal(tmp_path):
    """A manifest row is evidence about submitted master bytes, never a hint."""
    data = png(4, 3)
    actual = digest_bytes(data)
    declared = "a" * 64
    row = door.triage_manifest.make_row(
        corpus_id="parish-a",
        source_frame_sha256=declared,
        frame={"width": 4, "height": 3},
        split=door.triage_manifest.make_split(
            [
                door.triage_manifest.make_part(
                    {"x": 0, "y": 0, "w": 4, "h": 3},
                    {"x": 0, "y": 0, "w": 4, "h": 3},
                    0,
                    colour_mode="keep",
                )
            ]
        ),
        re_shoot_cluster_id=None,
        confidence=4,
        mode="auto",
        actor={"kind": "model", "identity": "triage", "revision": "r1"},
        human_override=False,
    )
    source = SourceEntry(
        1,
        "frame.png",
        actual,
        container_page_index=0,
        triage_row=row,
        triage_part_index=0,
        source_frame_index=0,
    )
    tree, context = open_door(tmp_path, [source])
    assert process_sources(context, tree, [source], reader({"frame.png": data}), policy=POLICY) == 0
    record = admissions(tree)[1]
    assert record["outcome"] == "refused"
    assert reason_code(record["payload"]["reason"]) == RefusalReason.DIGEST_MISMATCH


@pytest.mark.parametrize(
    ("which", "expected"),
    [
        ("manifest", "triage decision manifest"),
        ("clusters", "triage re-shoot cluster records"),
    ],
)
def test_a_malformed_triage_document_names_its_role_effect_and_remedy(tmp_path, which, expected):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": door.triage_manifest.MANIFEST_SCHEMA,
                "corpus_id": "parish-a",
                "records": [],
            }
        ),
        encoding="utf-8",
    )
    clusters_path = tmp_path / "clusters.json"
    clusters_path.write_text("{}", encoding="utf-8")
    (manifest_path if which == "manifest" else clusters_path).write_bytes(b"\xffnot-json")

    with pytest.raises(ContractError, match=f"{expected} is not valid UTF-8 JSON") as refused:
        door.load_triage_decisions(
            manifest_path,
            clusters_path if which == "clusters" else None,
        )
    assert "no run was created" in str(refused.value)
    assert "export valid UTF-8 JSON and retry" in str(refused.value)


def test_a_triage_document_is_bounded_before_json_decoding(tmp_path, monkeypatch):
    decision_path = tmp_path / "manifest.json"
    decision_path.write_bytes(b"123456789")
    monkeypatch.setattr(door, "MAX_TRIAGE_DOCUMENT_BYTES", 8)

    with pytest.raises(ContractError, match="8-byte document bound"):
        door.load_triage_decisions(decision_path)


def test_a_triage_document_symlink_is_not_followed_at_the_read_boundary(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps(
            {
                "schema": door.triage_manifest.MANIFEST_SCHEMA,
                "corpus_id": "parish-a",
                "records": [],
            }
        ),
        encoding="utf-8",
    )
    redirected = tmp_path / "manifest.json"
    redirected.symlink_to(outside)

    # The surviving fd-anchored walk refuses the redirect at open time with its
    # own wording; the symlink is still never followed and no run is created.
    with pytest.raises(ContractError, match="without following path redirects") as refused:
        door.load_triage_decisions(redirected)

    assert "regular file" in str(refused.value)


def test_triage_split_count_refuses_before_quadratic_geometry_validation(tmp_path, monkeypatch):
    decision_path = tmp_path / "manifest.json"
    decision_path.write_text(
        json.dumps(
            {
                "schema": door.triage_manifest.MANIFEST_SCHEMA,
                "corpus_id": "parish-a",
                "records": [{"split": {"parts": [{}, {}, {}]}}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(door, "MAX_TRIAGE_DERIVATIVE_PAGES", 2)

    with pytest.raises(ContractError, match="more than 2 derivative pages") as refused:
        door.load_triage_decisions(decision_path)

    assert "before pairwise geometry validation" in str(refused.value)
    assert "export one configured shard" in str(refused.value)


def test_triage_cluster_members_are_bounded_before_set_expansion(tmp_path, monkeypatch):
    decision_path = tmp_path / "manifest.json"
    decision_path.write_text(
        json.dumps(
            {
                "schema": door.triage_manifest.MANIFEST_SCHEMA,
                "corpus_id": "parish-a",
                "records": [],
            }
        ),
        encoding="utf-8",
    )
    clusters_path = tmp_path / "clusters.json"
    clusters_path.write_text(
        json.dumps({"opening": {"member_frame_sha256": ["a", "b", "c"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(door, "MAX_TRIAGE_DERIVATIVE_PAGES", 2)

    with pytest.raises(ContractError, match="more than 2 member references") as refused:
        door.load_triage_decisions(decision_path, clusters_path)

    assert "before set expansion" in str(refused.value)
    assert "clusters for one configured shard" in str(refused.value)


def test_split_render_uses_the_deterministic_common_encoder(monkeypatch):
    """Same-process repetition alone would pass even for a Pillow-chosen framing;
    the claim under test is that a *different* PNG-writing zlib build still yields
    identical bytes, which is what `common.imaging.encode_image_deterministic`
    exists to guarantee (`common/test_imaging_determinism.py`'s own zlib-shim
    pattern). Without varying the encoder, this test cannot tell the project-owned
    encoder from Pillow's own writer choosing whatever its bundled zlib does.

    The zlib shim alone did not do that. `encode_image_deterministic` writes stored
    DEFLATE blocks through `_deterministic_stored_deflate` and never calls
    `zlib.compress`, and neither does Pillow's PNG writer, which uses a compression
    object. So the shim changed neither render and the test passed no matter which
    encoder ran. The encoder call is spied on directly now, and the shim covers
    `compressobj` too, so a replacement writer would be caught by both halves.
    Found by CodeRabbit."""
    calls: list[bytes] = []
    genuine_encode = common_imaging.encode_image_deterministic

    def spy(image):
        encoded = genuine_encode(image)
        calls.append(encoded)
        return encoded

    monkeypatch.setattr(common_imaging, "encode_image_deterministic", spy)

    data = jpeg(6, 4)
    part = door.triage_manifest.make_part(
        {"x": 0, "y": 0, "w": 6, "h": 4},
        {"x": 1, "y": 0, "w": 4, "h": 4},
        0,
        colour_mode="grayscale",
    )
    first, _, first_contract = door.render_raster_page(data, 0, part)
    assert validate_png(first).width == 4
    assert first_contract["deterministic_encoder"] == "common.imaging.encode_image_deterministic-v1"
    # The bytes the Door sealed are this encoder's return value, not a writer that
    # merely produced something a PNG validator accepts.
    assert calls == [first]

    genuine_compress = zlib.compress
    genuine_compressobj = zlib.compressobj
    try:
        zlib.compress = lambda payload, level=-1, **kwargs: genuine_compress(payload, 6)
        zlib.compressobj = lambda *args, **kwargs: genuine_compressobj(6)
        shimmed, _, shimmed_contract = door.render_raster_page(data, 0, part)
    finally:
        zlib.compress = genuine_compress
        zlib.compressobj = genuine_compressobj

    assert shimmed == first, "a different zlib compression level renamed the sealed derivative"
    assert shimmed_contract == first_contract
    assert calls == [first, first]


def test_content_aware_shards_do_not_cut_split_pairs_or_clusters():
    split_row = {"re_shoot_cluster_id": None}
    cluster_row = {"re_shoot_cluster_id": "opening-7"}
    split = [
        SourceEntry(1, "a.jpg", "a" * 64, 0, triage_row=split_row, triage_part_index=0),
        SourceEntry(2, "a.jpg", "a" * 64, 1, triage_row=split_row, triage_part_index=1),
        SourceEntry(3, "b.jpg", "b" * 64, 0, triage_row=cluster_row, triage_part_index=0),
        SourceEntry(4, "c.jpg", "c" * 64, 0, triage_row=cluster_row, triage_part_index=0),
    ]
    shards = door.content_aware_shards(split, max_pages_per_shard=2)
    assert [[source.ordinal for source in shard] for shard in shards] == [[1, 2], [3, 4]]
    with pytest.raises(ContractError, match="content-aware shard refusal"):
        door.content_aware_shards(split[:2], max_pages_per_shard=1)
    with pytest.raises(ContractError, match="content-aware shard refusal"):
        door.content_aware_shards(split[2:], max_pages_per_shard=1)


def test_re_shoot_cluster_admits_every_member_and_records_no_canonical(tmp_path):
    first, second = png(4, 3), png(4, 3, rows=(b"\x00" + b"\x63" * 4) * 3)
    first_digest, second_digest = digest_bytes(first), digest_bytes(second)

    def row(digest):
        return door.triage_manifest.make_row(
            corpus_id="parish-a",
            source_frame_sha256=digest,
            frame={"width": 4, "height": 3},
            split=door.triage_manifest.make_split(
                [
                    door.triage_manifest.make_part(
                        {"x": 0, "y": 0, "w": 4, "h": 3},
                        {"x": 0, "y": 0, "w": 4, "h": 3},
                        0,
                        colour_mode="keep",
                    )
                ]
            ),
            re_shoot_cluster_id="opening-7",
            confidence=3,
            mode="semi",
            actor={"kind": "model", "identity": "triage", "revision": "r1"},
            human_override=False,
        )

    rows = {first_digest: row(first_digest), second_digest: row(second_digest)}
    cluster = {
        "schema": door.triage_manifest.CLUSTER_SCHEMA,
        "corpus_id": "parish-a",
        "cluster_id": "opening-7",
        "member_frame_sha256": [first_digest, second_digest],
        "split_count": 1,
    }
    sources = door.expand_sources(
        [
            {"relative_path": "a.png", "sha256": first_digest},
            {"relative_path": "b.png", "sha256": second_digest},
        ],
        reader({"a.png": first, "b.png": second}),
        POLICY,
        triage_rows=rows,
        triage_clusters={"opening-7": cluster},
    )
    tree, context = open_door(tmp_path, sources)
    assert (
        process_sources(
            context,
            tree,
            sources,
            reader({"a.png": first, "b.png": second}),
            policy=POLICY,
        )
        == 2
    )
    report = door.publish_cluster_report(context)
    assert report is not None
    payload = json.loads(tree.read_bytes(report).decode("utf-8"))["payload"]
    assert payload["clusters"][0]["cluster_id"] == "opening-7"
    assert {member["source_frame_sha256"] for member in payload["clusters"][0]["members"]} == {
        first_digest,
        second_digest,
    }
    assert "canonical" not in json.dumps(payload)


@pytest.mark.parametrize("bad_bytes", [True, False, -1, "5", 5.0])
def test_a_row_with_no_non_negative_byte_count_is_a_contract_error(bad_bytes):
    """`validate_manifest` already refuses this for a real ledger; this proves the
    guard inside expand_sources itself actually fires too, for a caller that
    reaches it some other way.
    """
    rows = [{"relative_path": "a.png", "sha256": "0" * 64, "bytes": bad_bytes}]
    with pytest.raises(ContractError, match="no non-negative byte count"):
        expand_sources(rows, reader({}), POLICY)


def test_real_run_bindings_change_with_a_renderer_recipe_before_a_page_is_written(monkeypatch):
    class Models:
        # The full configured roster: `_real_bindings` now validates the real
        # witness-context declaration, which describes these three chairs, and
        # a narrower stub roster would refuse the declaration as unaddressed.
        witness_chairs = ("attestator_1", "attestator_2", "attestator_3")
        adapter_recipes = {"door": "synthetic-door-v0"}

        @staticmethod
        def to_record():
            return {"models": "synthetic"}

    ledger = {
        "files": [{"relative_path": "scan.pdf", "sha256": "a" * 64, "bytes": 12}],
        "self_hash": "b" * 64,
    }
    settings = door.render_config.load_pdf_render_settings(
        minimum_dpi=door.pdf_render.MIN_RENDER_DPI
    )
    baseline = door._real_bindings(
        Models(),
        ledger,
        POLICY,
        settings,
        door.load_recovery_policy(),
        door.load_hard_failure_policy(),
        **_sealed_binding_digests(),
    )
    altered_pdf_recipe = dict(door.pdf_render.renderer_recipe(settings), dpi=301)
    monkeypatch.setattr(door.pdf_render, "renderer_recipe", lambda _settings: altered_pdf_recipe)
    changed = door._real_bindings(
        Models(),
        ledger,
        POLICY,
        settings,
        door.load_recovery_policy(),
        door.load_hard_failure_policy(),
        **_sealed_binding_digests(),
    )

    assert baseline["config_digest"] != changed["config_digest"]


def test_real_run_bindings_refuse_a_configured_witness_without_an_adapter():
    models = load_models_toml(ROOT / "config" / "models.toml")
    chairs = dict(models.chairs)
    chairs["attestator_1"] = replace(
        chairs["attestator_1"], witness_adapter=None, witness_scope=None
    )
    models = replace(models, chairs=chairs)
    ledger = {
        "files": [{"relative_path": "scan.pdf", "sha256": "a" * 64, "bytes": 12}],
        "self_hash": "b" * 64,
    }
    settings = door.render_config.load_pdf_render_settings(
        minimum_dpi=door.pdf_render.MIN_RENDER_DPI
    )

    with pytest.raises(
        ContractError, match="chair 'attestator_1' has no witness_adapter"
    ) as caught:
        door._real_bindings(
            models,
            ledger,
            POLICY,
            settings,
            door.load_recovery_policy(),
            door.load_hard_failure_policy(),
            **_sealed_binding_digests(),
        )

    message = str(caught.value)
    assert "no native boundary to run" in message
    assert "Add witness_adapter and witness_scope" in message


def test_a_real_door_run_names_and_binds_its_non_fake_implementation_revision(monkeypatch):
    class Models:
        # The full configured roster: `_real_bindings` now validates the real
        # witness-context declaration, which describes these three chairs, and
        # a narrower stub roster would refuse the declaration as unaddressed.
        witness_chairs = ("attestator_1", "attestator_2", "attestator_3")
        adapter_recipes = {"door": "fake-door-v0"}

        @staticmethod
        def to_record():
            return {"models": "synthetic"}

    ledger = {
        "files": [{"relative_path": "scan.pdf", "sha256": "a" * 64, "bytes": 12}],
        "self_hash": "b" * 64,
    }
    settings = door.render_config.load_pdf_render_settings(
        minimum_dpi=door.pdf_render.MIN_RENDER_DPI
    )
    baseline = door._real_bindings(
        Models(),
        ledger,
        POLICY,
        settings,
        door.load_recovery_policy(),
        door.load_hard_failure_policy(),
        **_sealed_binding_digests(),
    )
    assert baseline["adapter_recipes"]["door"] == door.REAL_DOOR_ADAPTER_REVISION
    assert baseline["adapter_recipes"]["door"] != "fake-door-v0"

    monkeypatch.setattr(door, "REAL_DOOR_ADAPTER_REVISION", "exemplar-door-test-change")
    changed = door._real_bindings(
        Models(),
        ledger,
        POLICY,
        settings,
        door.load_recovery_policy(),
        door.load_hard_failure_policy(),
        **_sealed_binding_digests(),
    )
    assert baseline["config_digest"] != changed["config_digest"]


def test_a_real_door_run_binds_the_hard_failure_policy_before_any_page_is_written():
    """The run-level cap is run-bound configuration, exactly as recovery is.

    A closed list of what counts as a hard failure decides whether a run may keep
    invoking stages. Editing that list mid-run and reinterpreting failures already
    on disk is the same class of mistake as editing the recovery budget mid-run,
    so it is sealed into `config_digest` and a changed policy is a different run.
    """

    class Models:
        # The full configured roster: `_real_bindings` now validates the real
        # witness-context declaration, which describes these three chairs, and
        # a narrower stub roster would refuse the declaration as unaddressed.
        witness_chairs = ("attestator_1", "attestator_2", "attestator_3")
        adapter_recipes = {"door": "synthetic-door-v0"}

        @staticmethod
        def to_record():
            return {"models": "synthetic"}

    ledger = {
        "files": [{"relative_path": "scan.pdf", "sha256": "a" * 64, "bytes": 12}],
        "self_hash": "b" * 64,
    }
    settings = door.render_config.load_pdf_render_settings(
        minimum_dpi=door.pdf_render.MIN_RENDER_DPI
    )
    recovery = door.load_recovery_policy()
    baseline = door._real_bindings(
        Models(),
        ledger,
        POLICY,
        settings,
        recovery,
        door.load_hard_failure_policy(),
        **_sealed_binding_digests(),
    )
    changed = door._real_bindings(
        Models(),
        ledger,
        POLICY,
        settings,
        recovery,
        {
            "config_sha256": "d" * 64,
            "threshold": 2,
            "kinds": [("perlector", "failed")],
            "reason_kinds": [],
        },
        **_sealed_binding_digests(),
    )

    assert baseline["config_digest"] != changed["config_digest"]


def _approved_submission(tmp_path, files: dict[str, bytes]):
    """Create synthetic source files, an approved-root policy, and the filename ledger."""
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
    ledger_path = approved / "source-ledger.json"
    ledger = submit.submit(
        source,
        ledger_path,
        policy_path=policy_path,
    )
    return approved, source, policy, policy_path, ledger_path, ledger


def _run_real_door(
    monkeypatch,
    *,
    run_root,
    source,
    policy_path,
    ledger_path,
    run_id,
    corpus_register=None,
    extra=(),
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
            "--data-gate-policy",
            str(policy_path),
            *(["--corpus-register", str(corpus_register)] if corpus_register is not None else []),
            *extra,
        ],
    )
    return door.main()


def test_real_door_binds_the_local_filename_ledger_to_every_run_page(tmp_path, monkeypatch):
    files = {"FS-1234.png": png(4, 3), "iPhone/BATCH-7.pdf": single_gray_page_pdf()}
    approved, source, _policy, policy_path, ledger_path, ledger = _approved_submission(
        tmp_path, files
    )
    run_root = approved / "runs"

    assert (
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=policy_path,
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
    assert run["ingress"] == {"mode": "real"}
    for record in admissions(tree).values():
        assert record["payload"]["ledger_sha256"] == ledger["self_hash"]
        assert "data_gate_approval_ref" not in record["payload"]

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
    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
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
    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
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
    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
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
    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
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

    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
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
    approved, source, _policy, policy_path, ledger_path, ledger = _approved_submission(
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

    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
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
    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
        tmp_path, files
    )
    run_root = approved / "runs"

    assert (
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=policy_path,
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
    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
        tmp_path, files
    )
    run_root = approved / "runs"

    assert (
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=policy_path,
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
    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-4321.png": png(4, 3)}
    )
    (source / "FS-4321.png").write_bytes(png(5, 3))

    with pytest.raises(ContractError, match="digest-mismatch"):
        _run_real_door(
            monkeypatch,
            run_root=approved / "runs",
            source=source,
            policy_path=policy_path,
            ledger_path=ledger_path,
            run_id="changed-copy",
        )
    record = admissions(RunTree(approved / "runs", "changed-copy"))[1]
    assert record["payload"]["declared_path"] == "FS-4321.png"
    assert reason_code(record["payload"]["reason"]) is RefusalReason.DIGEST_MISMATCH


def test_extra_copy_absent_from_the_filename_ledger_stops_before_a_run_is_created(
    tmp_path, monkeypatch
):
    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-1.png": png()}
    )
    (source / "unledgered.png").write_bytes(png())
    with pytest.raises(ContractError, match="absent from its self-hashed filename ledger"):
        _run_real_door(
            monkeypatch,
            run_root=approved / "runs",
            source=source,
            policy_path=policy_path,
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
    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-1.png": png(4, 3), "FS-2.png": png(5, 5)}
    )
    (source / "FS-1.png").unlink()

    assert (
        _run_real_door(
            monkeypatch,
            run_root=approved / "runs",
            source=source,
            policy_path=policy_path,
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


def test_an_unreadable_corpus_register_really_does_leave_no_run_behind(tmp_path, monkeypatch):
    """The claim the refusal makes, checked against the tree rather than the string.

    `_read_corpus_register` is evaluated as an argument to `RunTree.create`, so
    the read precedes creation today — but nothing observed that. Move the read
    below the create and the unit test above still passes while the door leaves a
    run behind after telling the operator nothing was written.
    """
    _approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-1.png": png()}
    )
    run_root = _approved / "runs"

    with pytest.raises(ContractError, match="no run or admission record was written"):
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=policy_path,
            ledger_path=ledger_path,
            run_id="no-register",
            corpus_register=tmp_path / "missing-register.json",
        )

    assert not (run_root / "no-register" / "run.json").exists(), "a run was created anyway"
    assert not (run_root / "no-register").exists() or not list(
        (run_root / "no-register").rglob("*.json")
    ), "the refused run left records behind"


def test_an_unapproved_run_root_is_named_before_its_run_authority_is_read(tmp_path, monkeypatch):
    """The storage gate runs before the run-level cap, so no run.json is opened.

    The cap check used to run first, in `main`, against the typed run root. For a
    real submission that meant opening and self-hash-verifying a run authority in
    a directory the data-handling policy never approved — the exact read the gate
    exists to stop — and an operator who mistyped the root onto an unapproved
    volume that happened to hold a run.json was told the run was halted rather
    than that the root is not an approved location.
    """
    _approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-1.png": png()}
    )
    outside = tmp_path / "unapproved"
    (outside / "halted-elsewhere").mkdir(parents=True)
    # A run.json the cap check would have opened. It is deliberately not a valid
    # authority: if anything reads it, the failure will not be the gate's.
    (outside / "halted-elsewhere" / "run.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ContractError, match="run root is outside every approved storage root"):
        _run_real_door(
            monkeypatch,
            run_root=outside,
            source=source,
            policy_path=policy_path,
            ledger_path=ledger_path,
            run_id="halted-elsewhere",
        )


def test_a_real_run_root_inside_its_submission_folder_is_refused_before_inventory(
    tmp_path, monkeypatch
):
    _approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-1.png": png()}
    )
    with pytest.raises(ContractError, match="run root cannot live inside the submitted folder"):
        _run_real_door(
            monkeypatch,
            run_root=source / "runs",
            source=source,
            policy_path=policy_path,
            ledger_path=ledger_path,
            run_id="contained-run-root",
        )
    assert not (source / "runs").exists()


def test_a_real_submission_requires_the_local_filename_ledger(tmp_path, monkeypatch):
    approved, source, _policy, policy_path, _ledger_path, _ledger = _approved_submission(
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


def test_a_wholly_refused_door_does_not_publish_a_completion_seal(tmp_path):
    """Failure evidence remains, but a fatal close cannot wear the happy-path seal."""
    broken = b"not an image at all"
    source = SourceEntry(1, "one.png", digest_bytes(broken))
    tree, context = open_door(tmp_path, [source])
    admitted = process_sources(
        context,
        tree,
        [source],
        reader({"one.png": broken}),
        policy=POLICY,
    )

    with pytest.raises(ContractError, match="the door admitted nothing"):
        door._finish_door_run(context, tree, admitted)

    kinds = [entry["kind"] for entry in tree.build_manifest(DOOR)["artifacts"]]
    assert "refusal-report" in kinds
    assert "stage-seal" not in kinds


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


def test_real_bindings_seal_designator_padding_alongside_the_shard_knob(monkeypatch):
    """F-S5 (audit finding): `_real_bindings`'s `sealed_config_digests` must name
    `designator-padding` exactly as `run_config_bindings` (the fixture path) does,
    not only `corpus-frame-shard`.

    Before this audit's fix, `_real_bindings` returned
    `sealed_config_digests = {"corpus-frame-shard": ...}` only. The padding
    config's bytes were already folded into the overall `config_digest`, but the
    NAMED point-of-use-recheck entry was missing -- so a real Designator run
    reaching `context.require_sealed_config("designator-padding", ...)`
    (`pipeline/2_designator/run.py`) over real ingress would refuse every time
    with "this context sealed no digest for the designator-padding configuration",
    the day R2 lands a real structure pass. The fixture and real paths must expose
    the same `sealed_config_digests` shape.
    """

    class Models:
        witness_chairs = ("attestator_1", "attestator_2", "attestator_3")
        adapter_recipes = {"door": "synthetic-door-v0"}

        @staticmethod
        def to_record():
            return {"models": "synthetic"}

    ledger = {
        "files": [{"relative_path": "scan.pdf", "sha256": "a" * 64, "bytes": 12}],
        "self_hash": "b" * 64,
    }
    settings = door.render_config.load_pdf_render_settings(
        minimum_dpi=door.pdf_render.MIN_RENDER_DPI
    )
    supplied = _sealed_binding_digests()
    padding_digest = supplied["designator_padding_config_sha256"]
    geometry_digest = supplied["designator_geometry_config_sha256"]
    recovery = door.load_recovery_policy()
    bindings = door._real_bindings(
        Models(),
        ledger,
        POLICY,
        settings,
        recovery,
        door.load_hard_failure_policy(),
        **supplied,
    )
    sealed = bindings["sealed_config_digests"]
    assert sealed.get("designator-padding") == padding_digest, (
        f"_real_bindings()'s sealed_config_digests is {sorted(sealed)}, missing a "
        "'designator-padding' entry bound to the exact digest passed in; the fixture "
        "path's run_config_bindings() already seals this name (F-S5)"
    )
    assert sealed.get("designator-geometry") == geometry_digest, (
        f"_real_bindings()'s sealed_config_digests is {sorted(sealed)}, missing a "
        "'designator-geometry' entry bound to the exact digest passed in; the Designator's "
        "point-of-use recheck (pipeline/2_designator/run.py) requires this name on every "
        "run, so a real run without it refuses unconditionally (same class as F-S5)"
    )
    assert "corpus-frame-shard" in sealed, (
        "the pre-existing corpus-frame-shard entry must survive this fix, not be replaced"
    )
    # The sealing family (audit S3/S6, CodeRabbit CF01). Each of these has a point
    # of use on the real route: the door renders with the PDF policy it parsed, the
    # storage-root gate ran under the data-handling policy it loaded, and the
    # Designator recovery pass and the orchestrator's dispatch both work from the
    # recovery budget. A real run whose door sealed none of them would refuse at
    # the point of use with "sealed no digest" -- the F-S5 shape again.
    assert sealed.get("pdf-render") == supplied["pdf_render_config_sha256"], (
        f"_real_bindings()'s sealed_config_digests is {sorted(sealed)}, missing a "
        "'pdf-render' entry bound to the digest of the bytes the settings were parsed "
        "from; without it the door cannot prove what it rendered under (audit S6)"
    )
    assert sealed.get("recovery") == recovery["config_sha256"], (
        f"_real_bindings()'s sealed_config_digests is {sorted(sealed)}, missing a "
        "'recovery' entry; the Recensor, the Designator recovery pass and the "
        "orchestrator all require this name at their point of use (audit S3)"
    )
    assert sealed.get("data-handling") == supplied["data_handling_config_sha256"], (
        f"_real_bindings()'s sealed_config_digests is {sorted(sealed)}, missing a "
        "'data-handling' entry naming the caller-selected policy that gated admission "
        "(CodeRabbit CF01)"
    )
    triage_modes = ROOT / "config" / "triage_modes.toml"
    assert sealed.get("triage-modes") == digest_bytes(triage_modes.read_bytes()), (
        f"_real_bindings()'s sealed_config_digests is {sorted(sealed)}, missing a "
        "'triage-modes' entry for the mode vocabulary a real triage manifest uses"
    )
    require_triage_modes(sealed, triage_modes)


def test_real_submission_rechecks_triage_modes_before_expanding_triage_geometry():
    """The named real-path seal is used before a manifest can shape source pages."""
    implementation = inspect.getsource(door.real_submission)
    assert implementation.index("require_triage_modes") < implementation.index(
        "sources = expand_sources"
    )


def test_real_bindings_refuse_an_unapproved_prior_control_before_run_creation():
    """The real ingress path shares the fixture path's approval refusal."""

    class Models:
        witness_chairs = ("attestator_1", "attestator_2", "attestator_3")
        adapter_recipes = {"door": "synthetic-door-v0"}

        @staticmethod
        def to_record():
            return {"models": "synthetic"}

    ledger = {
        "files": [{"relative_path": "scan.pdf", "sha256": "a" * 64, "bytes": 12}],
        "self_hash": "b" * 64,
    }
    settings = door.render_config.load_pdf_render_settings(
        minimum_dpi=door.pdf_render.MIN_RENDER_DPI
    )
    with pytest.raises(ContractError, match="is not an approval record"):
        door._real_bindings(
            Models(),
            ledger,
            POLICY,
            settings,
            door.load_recovery_policy(),
            door.load_hard_failure_policy(),
            **_sealed_binding_digests(),
            perlector_instrument_per_mille=1,
        )


@pytest.mark.parametrize(
    "submission,denominator",
    [
        (door.fixture_submission, "pages"),
        (door.real_submission, "sources"),
    ],
)
def test_each_door_path_enforces_the_shard_limit_at_run_creation(submission, denominator):
    """F-new-1's helper tests must also pin both production call sites.

    Sonnet's four tests invoked ``require_corpus_frame_shard`` directly; deleting
    both calls from the Door left all four green. This AST assertion binds the
    already behavior-tested helper to each ingress denominator without needing a
    1,001-page fixture.
    """
    tree = ast.parse(dedent(inspect.getsource(submission)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "require_corpus_frame_shard"
    ]
    assert len(calls) == 1, (
        f"{submission.__name__} must enforce the sealed shard boundary exactly once"
    )
    assert ast.unparse(calls[0].args[0]) == f"len({denominator})"
    assert ast.unparse(calls[0].args[1]) == "bindings['sealed_config_digests']"


def test_a_real_admission_names_the_data_handling_policy_that_governed_it(tmp_path, monkeypatch):
    """CodeRabbit CF01: which caller-selected policy admitted this material.

    Both entry points expose the policy as a flag, so "the current policy" is
    whichever file the invoker names. `config/README.md` said outright that nothing
    bound a run to the policy version governing it, which left later evidence
    unable to establish which file's storage roots the corpus was admitted under —
    a real gap even though the gate itself works from one in-memory record.

    The run now names it. Not an approval record: nothing here refuses a submission
    for want of a sign-off, and the per-run approval requirement cut on 2026-08-09
    stays cut. This is provenance, which GOVERNANCE 6 asks travel with the record.
    """
    files = {"FS-9001.png": png(4, 3)}
    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
        tmp_path, files
    )
    run_root = approved / "runs"
    assert (
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=policy_path,
            ledger_path=ledger_path,
            run_id="named-policy",
        )
        == 0
    )

    run = RunTree(run_root, "named-policy").read_run()
    assert run["sealed_config_digests"]["data-handling"] == digest_bytes(policy_path.read_bytes())
    assert run["sealed_config_digests"]["pdf-render"] == digest_bytes(
        (ROOT / "config" / "pdf_render.toml").read_bytes()
    )
    assert run["sealed_config_digests"]["recovery"] == door.load_recovery_policy()["config_sha256"]


def test_reusing_a_run_id_under_a_changed_data_handling_policy_is_refused(tmp_path, monkeypatch):
    """Bound, not merely recorded: a second policy is a different run.

    The two policies here name the same storage roots, so the gate admits the same
    folder under both. What differs is the document — and until it was bound, the
    same run id could hold material admitted under two of them with nothing in the
    tree saying so.
    """
    files = {"FS-9002.png": png(4, 3)}
    approved, source, policy, policy_path, ledger_path, _ledger = _approved_submission(
        tmp_path, files
    )
    run_root = approved / "runs"
    assert (
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=policy_path,
            ledger_path=ledger_path,
            run_id="one-policy",
        )
        == 0
    )
    before = {
        path.relative_to(run_root): digest_bytes(path.read_bytes())
        for path in run_root.rglob("*")
        if path.is_file()
    }

    edited = dict(policy, policy_version=f"{policy['policy_version']}-second")
    second_policy = tmp_path / "policy-second.json"
    second_policy.write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(ContractError, match="config_digest|sealed_config_digests"):
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=second_policy,
            ledger_path=ledger_path,
            run_id="one-policy",
        )
    after = {
        path.relative_to(run_root): digest_bytes(path.read_bytes())
        for path in run_root.rglob("*")
        if path.is_file()
    }
    assert after == before, "the refusal must land before anything is written"


def test_the_fixture_run_authority_records_every_digest_its_stages_will_ask_for(
    tmp_path, monkeypatch
):
    """The run names the policies it sealed, under the names points of use ask for.

    The map in `run.json` and the one `run_config_bindings` computes are the same
    map. F-S5 was the two drifting apart on the real route; recording it makes that
    drift a refusal at `open_context` rather than a "sealed no digest" surprise at
    whichever stage reached the point of use first.
    """
    from common.chairs.registry import ChairRegistry
    from common.stage import load_fixture, run_config_bindings

    run_root = tmp_path / "runs"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "door.py",
            "--run-root",
            str(run_root),
            "--run-id",
            "sealed-map",
            "--fixture-root",
            str(ROOT / "proof"),
        ],
    )
    assert door.main() == 0

    run = RunTree(run_root, "sealed-map").read_run()
    expected = run_config_bindings(
        ChairRegistry.from_toml(str(ROOT / "config" / "models.toml")).config,
        load_fixture(str(ROOT / "proof")),
        "happy",
    )
    assert run["sealed_config_digests"] == expected["sealed_config_digests"]
    # Real ingress seals one name more; the fixture route is not gated, so a
    # data-handling entry here would name a check that never happened.
    assert "data-handling" not in run["sealed_config_digests"]
    assert {"pdf-render", "recovery"} <= set(run["sealed_config_digests"])


# --- Split-page provenance and refusal contracts ------------------------------


def test_a_master_this_encoder_would_convert_is_a_named_page_refusal(tmp_path):
    """A 16-bit master under `keep` refuses as a page, not as a crash.

    `common/test_imaging_determinism.py` proves the render refuses it; this
    proves the door turns that into its ordinary named per-page alarm, so the
    frame is visibly refused with a reason an operator can act on instead of
    reaching the Exemplar as silently 8-bit pixels.
    """
    image = Image.new("I;16", (4, 2))
    for x in range(4):
        for y in range(2):
            image.putpixel((x, y), (x * 9973 + y) % 65535)
    output = BytesIO()
    image.save(output, format="TIFF", compression="raw")
    master = output.getvalue()
    digest = digest_bytes(master)
    row = door.triage_manifest.make_row(
        corpus_id="parish-a",
        source_frame_sha256=digest,
        frame={"width": 4, "height": 2},
        split=door.triage_manifest.make_split(
            [
                door.triage_manifest.make_part(
                    {"x": 0, "y": 0, "w": 4, "h": 2},
                    {"x": 0, "y": 0, "w": 4, "h": 2},
                    0,
                    colour_mode="keep",
                )
            ]
        ),
        re_shoot_cluster_id=None,
        confidence=4,
        mode="auto",
        actor={"kind": "model", "identity": "triage", "revision": "r1"},
        human_override=False,
    )
    sources = door.expand_sources(
        [{"relative_path": "frame.tif", "sha256": digest}],
        reader({"frame.tif": master}),
        POLICY,
        triage_rows={digest: row},
    )
    tree, context = open_door(tmp_path, sources)
    assert (
        process_sources(context, tree, sources, reader({"frame.tif": master}), policy=POLICY) == 0
    )
    record = admissions(tree)[1]
    assert record["outcome"] == "refused"
    assert reason_code(record["payload"]["reason"]) == RefusalReason.UNSUPPORTED_VARIANT
    assert "colour_mode 'keep'" in record["payload"]["reason"]
    assert "Declare the conversion this page needs" in record["payload"]["reason"]


def test_a_split_derivative_records_a_palette_master_and_the_rgb_it_was_sealed_in():
    """The lossless case where the decoded mode differs from the encoded one: the
    deterministic encoder expands P to RGB pixel-for-pixel, so colour_mode "keep"
    is honoured and the record must still say the master was a palette image. The
    test below described this case in its docstring and then built an RGB master,
    so nothing exercised palette provenance at all. Found by CodeRabbit."""
    palette = Image.new("P", (6, 4))
    palette.putpalette([10, 20, 30] + [0, 0, 0] * 255)
    output = BytesIO()
    palette.save(output, format="PNG")
    part = door.triage_manifest.make_part(
        {"x": 0, "y": 0, "w": 6, "h": 4},
        {"x": 0, "y": 0, "w": 6, "h": 4},
        0,
        colour_mode="keep",
    )

    _bytes, _geometry, contract = door.render_raster_page(output.getvalue(), 0, part)

    assert contract["source_mode"] == "P"
    assert contract["output"] == {"codec": "png", "color_mode": "RGB"}
    assert contract["mode_transform"] == "triage-region-crop-rotate-convert-to-rgb"


def test_a_split_derivative_records_its_master_mode_and_the_mode_it_was_sealed_in():
    """The record must retain the decoded master mode and bands, not placeholders.
    The whole-page and split branches share this provenance constraint."""
    output = BytesIO()
    Image.new("RGB", (6, 4), (10, 20, 30)).save(output, format="PNG")
    part = door.triage_manifest.make_part(
        {"x": 0, "y": 0, "w": 6, "h": 4},
        {"x": 0, "y": 0, "w": 6, "h": 4},
        0,
        colour_mode="grayscale",
    )
    _bytes, _geometry, contract = door.render_raster_page(output.getvalue(), 0, part)
    assert contract["source_mode"] == "RGB"
    assert contract["source_bands"] == ["R", "G", "B"]
    assert contract["output"] == {"codec": "png", "color_mode": "L"}
    assert contract["mode_transform"] == "triage-region-crop-rotate-convert-to-l"


def test_a_re_run_triage_manifest_is_a_different_run_wearing_an_old_id(tmp_path):
    """The immutable denominator's *content*, not only its length.

    A triage pass re-run between two attempts at one run id can move a gutter
    without changing the part count, so `source_manifest` — paths, digests,
    ordinals, part indices — is byte-identical and `RunTree.create` would accept
    the reuse. The geometry that produced the pixels is therefore bound like
    every other fact that shaped them, and the swap refuses at the run authority
    rather than surviving until a changed row happens to reach an
    already-published admission.
    """

    class Models:
        witness_chairs = ("attestator_1", "attestator_2", "attestator_3")
        adapter_recipes = {"door": "fake-door-v0"}

        @staticmethod
        def to_record():
            return {"models": "synthetic"}

    ledger = {
        "files": [{"relative_path": "spread.jpg", "sha256": "a" * 64, "bytes": 12}],
        "self_hash": "b" * 64,
    }
    settings = door.render_config.load_pdf_render_settings(
        minimum_dpi=door.pdf_render.MIN_RENDER_DPI
    )

    def bindings(triage_digests):
        return door._real_bindings(
            Models(),
            ledger,
            POLICY,
            settings,
            door.load_recovery_policy(),
            door.load_hard_failure_policy(),
            triage_document_digests=triage_digests,
            **_sealed_binding_digests(),
        )

    first = bindings({"triage-decision-manifest": "c" * 64})
    again = bindings({"triage-decision-manifest": "c" * 64})
    moved_gutter = bindings({"triage-decision-manifest": "d" * 64})
    gained_clusters = bindings(
        {"triage-decision-manifest": "c" * 64, "triage-re-shoot-clusters": "e" * 64}
    )
    gained_producer_recipe = bindings(
        {"triage-decision-manifest": "c" * 64, "triage-producer-recipe": "f" * 64}
    )
    none_at_all = bindings({})

    assert first["config_digest"] == again["config_digest"]
    assert (
        len(
            {
                first["config_digest"],
                moved_gutter["config_digest"],
                gained_clusters["config_digest"],
                gained_producer_recipe["config_digest"],
                none_at_all["config_digest"],
            }
        )
        == 5
    )

    source_manifest = [
        {
            "relative_path": "spread.jpg",
            "sha256": "a" * 64,
            "bytes": 12,
            "ordinal": 1,
            "container_page_index": 0,
        }
    ]
    created = RunTree.create(
        tmp_path,
        "triage-reuse",
        source_manifest=source_manifest,
        config_digest=first["config_digest"],
        adapter_recipes=first["adapter_recipes"],
        witness_chairs=first["witness_chairs"],
        sealed_config_digests=first["sealed_config_digests"],
    )
    unchanged = RunTree.create(
        tmp_path,
        "triage-reuse",
        source_manifest=source_manifest,
        config_digest=again["config_digest"],
        adapter_recipes=again["adapter_recipes"],
        witness_chairs=again["witness_chairs"],
        sealed_config_digests=again["sealed_config_digests"],
    )
    assert unchanged.read_run() == created.read_run()
    with pytest.raises(IncompatibleReuse, match="different config_digest"):
        RunTree.create(
            tmp_path,
            "triage-reuse",
            source_manifest=source_manifest,
            config_digest=moved_gutter["config_digest"],
            adapter_recipes=moved_gutter["adapter_recipes"],
            witness_chairs=moved_gutter["witness_chairs"],
            sealed_config_digests=moved_gutter["sealed_config_digests"],
        )


def test_content_aware_shards_impose_no_shard_ceiling_of_their_own():
    """The sealed page cap is the policy; the shard count is its consequence.

    An implicit count ceiling would refuse a valid larger corpus independently of
    the sealed page cap. A caller with an external ceiling must pass it explicitly.
    """
    sources = [
        SourceEntry(index, f"{index:03d}.jpg", f"{index:03d}".zfill(64)) for index in range(1, 9)
    ]

    assert len(door.content_aware_shards(sources, max_pages_per_shard=2)) == 4
    with pytest.raises(ContractError, match="shard count is exhausted"):
        door.content_aware_shards(sources, max_pages_per_shard=2, max_shards=3)


@pytest.mark.parametrize(
    ("max_pages", "max_shards"),
    [(True, None), (1.5, None), ("2", None), (2, True), (2, 1.5), (2, "3")],
)
def test_content_aware_shard_limits_are_positive_integer_counts(max_pages, max_shards):
    sources = [SourceEntry(1, "one.jpg", "a" * 64)]

    with pytest.raises(ContractError, match="non-positive or non-integer") as refused:
        door.content_aware_shards(
            sources,
            max_pages_per_shard=max_pages,
            max_shards=max_shards,
        )
    assert "no shard plan was returned" in str(refused.value)
    assert "pass positive integer limits" in str(refused.value)


def test_a_re_shoot_cluster_that_would_straddle_the_submitted_shard_is_refused(tmp_path):
    """The seam a *production* run can actually place is the operator's folder cut.

    Nothing in the tree partitions a corpus into shards; `content_aware_shards`
    only plans seams for its caller, and a split pair cannot straddle a folder cut
    because both halves come from one file. A cluster can, so source expansion must
    refuse an incomplete cluster independently of whether the planner was used.
    """
    first, second = png(4, 3), png(4, 3, rows=(b"\x00" + b"\x63" * 4) * 3)
    first_digest, second_digest = digest_bytes(first), digest_bytes(second)

    def row(digest):
        return door.triage_manifest.make_row(
            corpus_id="parish-a",
            source_frame_sha256=digest,
            frame={"width": 4, "height": 3},
            split=door.triage_manifest.make_split(
                [
                    door.triage_manifest.make_part(
                        {"x": 0, "y": 0, "w": 4, "h": 3},
                        {"x": 0, "y": 0, "w": 4, "h": 3},
                        0,
                        colour_mode="keep",
                    )
                ]
            ),
            re_shoot_cluster_id="opening-7",
            confidence=3,
            mode="semi",
            actor={"kind": "model", "identity": "triage", "revision": "r1"},
            human_override=False,
        )

    rows = {first_digest: row(first_digest), second_digest: row(second_digest)}
    cluster = {
        "schema": door.triage_manifest.CLUSTER_SCHEMA,
        "corpus_id": "parish-a",
        "cluster_id": "opening-7",
        "member_frame_sha256": [first_digest, second_digest],
        "split_count": 1,
    }
    with pytest.raises(ContractError, match="would cross this submitted shard") as crossing:
        door.expand_sources(
            [{"relative_path": "a.png", "sha256": first_digest}],
            reader({"a.png": first}),
            POLICY,
            triage_rows=rows,
            triage_clusters={"opening-7": cluster},
        )
    assert "no source expansion was returned" in str(crossing.value)
    assert "submit every cluster member in the same shard" in str(crossing.value)

    with pytest.raises(ContractError, match="no supplied cluster record") as unresolved:
        door.expand_sources(
            [
                {"relative_path": "a.png", "sha256": first_digest},
                {"relative_path": "b.png", "sha256": second_digest},
            ],
            reader({"a.png": first, "b.png": second}),
            POLICY,
            triage_rows=rows,
            triage_clusters={},
        )
    assert "cluster cannot be reconciled" in str(unresolved.value)
    assert "supply the matching corpus-scoped cluster record" in str(unresolved.value)


# The insert covers the middle of the frame at its own angle; the page around it is
# the exact complement, decomposed into four axis-aligned rectangles. Unit 5's
# validator proves the partition, so these numbers are the whole geometry claim.
_TAPED_FRAME = {"width": 64, "height": 48}
_TAPED_INSERT = {"x": 20, "y": 12, "w": 24, "h": 16}
_TAPED_PAGE_PARTS = (
    {"x": 0, "y": 0, "w": 64, "h": 12},
    {"x": 0, "y": 12, "w": 20, "h": 16},
    {"x": 44, "y": 12, "w": 20, "h": 16},
    {"x": 0, "y": 28, "w": 64, "h": 20},
)


def _taped_split():
    """One rotated insert part plus the four page parts that complete the frame."""
    parts = [
        door.triage_manifest.make_part(
            _TAPED_INSERT,
            {"x": 0, "y": 0, "w": _TAPED_INSERT["w"], "h": _TAPED_INSERT["h"]},
            3_500,
            colour_mode="keep",
        )
    ]
    parts += [
        door.triage_manifest.make_part(
            region,
            {"x": 0, "y": 0, "w": region["w"], "h": region["h"]},
            0,
            colour_mode="keep",
        )
        for region in _TAPED_PAGE_PARTS
    ]
    return door.triage_manifest.make_split(parts)


def _taped_frames():
    frames = []
    for index, tone in enumerate((90, 160)):
        image = Image.new("L", (_TAPED_FRAME["width"], _TAPED_FRAME["height"]), tone)
        # A little structure so the instrument's signature grid is not uniform.
        image.paste(255 - tone, (20, 12, 44, 28))
        encoded = BytesIO()
        image.save(encoded, format="PNG")
        frames.append(producer.SubmittedFrame(f"{index}.png", encoded.getvalue()))
    return frames


def _taped_confirmation(frames):
    """A confirmation over the taped pair, traced to the real Unit 6A instrument."""
    config = instrument_config()
    proxies = [instrument.build_proxies_from_bytes(item.data, config) for item in frames]
    evidence, evidence_manifest = instrument.candidate_evidence(proxies, config)
    recipe = instrument.producer_recipe(config)
    digests = sorted(digest_bytes(item.data) for item in frames)
    confirmation = {
        "schema": producer.CONFIRMATION_SCHEMA,
        "corpus_id": "parish-a",
        "appending_run": "triage-taped-1",
        "authority": {"kind": "fixture", "identity": "taped-insert-fixture", "revision": "v1"},
        "instrument_config_sha256": evidence_manifest["instrument_config_sha256"],
        "evidence_manifest_sha256": digest_of(evidence_manifest),
        "clusters": [
            {
                "pages": [
                    {
                        "volume_id": "v1",
                        "designation": "opening-taped",
                        "member_frame_sha256": digests,
                    }
                ],
                "evidence_pairs": [digests],
            }
        ],
    }
    return confirmation, recipe, evidence_manifest, evidence


def test_synthetic_63_64_65_plus_66_closes_instrument_confirmation_register_and_door(
    tmp_path: Path,
):
    """The final DoD-3/4/5 walk: three linked frames, one independent frame, no loss."""
    frames = []
    for name, tone in (("63", 70), ("64", 100), ("65", 130), ("66", 160)):
        image = Image.new("L", (64, 48), tone)
        image.paste(255 - tone, (8, 8, 24, 24))
        encoded = BytesIO()
        image.save(encoded, format="PNG")
        frames.append(producer.SubmittedFrame(f"{name}.png", encoded.getvalue()))
    config = instrument_config()
    proxies = [instrument.build_proxies_from_bytes(item.data, config) for item in frames]
    evidence, evidence_manifest = instrument.candidate_evidence(proxies, config)
    recipe = instrument.producer_recipe(config)
    digests_by_name = {item.path: digest_bytes(item.data) for item in frames}
    linked = sorted(digests_by_name[name] for name in ("63.png", "64.png", "65.png"))
    independent = digests_by_name["66.png"]
    confirmation = {
        "schema": producer.CONFIRMATION_SCHEMA,
        "corpus_id": "parish-a",
        "appending_run": "triage-63-66-final",
        "authority": {"kind": "fixture", "identity": "synthetic-63-66", "revision": "v1"},
        "instrument_config_sha256": evidence_manifest["instrument_config_sha256"],
        "evidence_manifest_sha256": digest_of(evidence_manifest),
        "clusters": [
            {
                "pages": [
                    {
                        "volume_id": "v1",
                        "designation": "opening-31-left",
                        "member_frame_sha256": linked,
                    },
                    {
                        "volume_id": "v1",
                        "designation": "opening-31-right",
                        "member_frame_sha256": [digests_by_name["65.png"]],
                    },
                ],
                "evidence_pairs": [sorted(linked[:2])],
            }
        ],
    }
    register_path = tmp_path / "register.json"
    produced, _register_head = producer.commit_confirmed_production(
        frames,
        corpus_id="parish-a",
        mode="auto",
        confirmation=confirmation,
        instrument_recipe=recipe,
        evidence_manifest=evidence_manifest,
        evidence_records=evidence,
        register_path=register_path,
        manifest_path=tmp_path / "manifest.json",
        clusters_path=tmp_path / "clusters.json",
        authority_path=tmp_path / "confirmation.json",
        max_pages_per_shard=3,
    )
    assert len(produced.manifest["records"]) == 4
    assert set(produced.rows_by_digest) == set(digests_by_name.values())
    assert produced.rows_by_digest[independent]["re_shoot_cluster_id"] is None
    cluster_id, cluster = next(iter(produced.clusters.items()))
    assert cluster["member_frame_sha256"] == linked
    assert all(
        produced.rows_by_digest[digest]["re_shoot_cluster_id"] == cluster_id for digest in linked
    )
    register_bytes = register_path.read_bytes()
    left_page = physical_page_id("parish-a", "v1", "opening-31-left")
    right_page = physical_page_id("parish-a", "v1", "opening-31-right")
    assert members_of(register_bytes, left_page) == linked
    assert members_of(register_bytes, right_page) == [digests_by_name["65.png"]]

    sources = door.expand_sources(
        [{"relative_path": item.path, "sha256": digests_by_name[item.path]} for item in frames],
        reader({item.path: item.data for item in frames}),
        POLICY,
        triage_rows=produced.rows_by_digest,
        triage_clusters=produced.clusters,
    )
    shards = door.content_aware_shards(sources, max_pages_per_shard=3)
    assert [[source.ordinal for source in shard] for shard in shards] == [[1, 2, 3], [4]]


def test_a_taped_insert_proposal_survives_produce_validation_and_the_door_fan_out():
    """Unit 5's own structural case, carried end to end without a frame-level crop.

    A document taped over the page at its own angle has no single gutter for
    auto-split and no global deskew that straightens both surfaces. The proposal is
    one rotated part for the insert and four axis-aligned parts for the page around
    it. This asserts the whole path: the producer binds it to submitted bytes, Unit
    5's validator proves it partitions the frame, and the Door fans one ordinal out
    per part while keeping the confirmed cluster whole.
    """
    frames = _taped_frames()
    digests = [digest_bytes(item.data) for item in frames]
    proposals = {
        item.path: door.triage_manifest.make_row(
            corpus_id="parish-a",
            source_frame_sha256=digest,
            frame=dict(_TAPED_FRAME),
            split=_taped_split(),
            re_shoot_cluster_id=None,
            confidence=0,
            mode="manual",
            # A deterministic offline producer cannot see a taped insert; the
            # geometry is an operator's structural proposal, bound to these bytes.
            actor={"kind": "human", "identity": "operator", "revision": None},
            human_override=True,
        )
        for item, digest in zip(frames, digests, strict=True)
    }
    confirmation, recipe, evidence_manifest, evidence = _taped_confirmation(frames)
    produced = producer.produce(
        frames,
        corpus_id="parish-a",
        mode="manual",
        confirmation=confirmation,
        instrument_recipe=recipe,
        evidence_manifest=evidence_manifest,
        evidence_records=evidence,
        transcribed_rows_by_path=proposals,
        max_pages_per_shard=10,
    )
    rows = produced.rows_by_digest
    assert len(produced.clusters) == 1
    cluster_id, cluster = next(iter(produced.clusters.items()))
    assert cluster["split_count"] == 5
    for digest in digests:
        parts = rows[digest]["split"]["parts"]
        assert len(parts) == 5
        assert [part["rotation"]["rotation_millidegrees"] for part in parts].count(0) == 4
        assert rows[digest]["re_shoot_cluster_id"] == cluster_id

    sources = door.expand_sources(
        [
            {"relative_path": item.path, "sha256": digest}
            for item, digest in zip(frames, digests, strict=True)
        ],
        reader({item.path: item.data for item in frames}),
        POLICY,
        triage_rows=rows,
        triage_clusters=produced.clusters,
    )
    assert [source.triage_part_index for source in sources] == [0, 1, 2, 3, 4] * 2
    assert {source.ordinal for source in sources} == set(range(1, 11))

    # The cluster spans every one of those ten ordinals, so no seam inside it is
    # legal: one shard holds it or the submission is refused.
    assert len(door.content_aware_shards(sources, max_pages_per_shard=10)) == 1
    with pytest.raises(ContractError, match="content-aware shard refusal"):
        door.content_aware_shards(sources, max_pages_per_shard=5)


def test_the_producer_measures_a_cluster_span_in_door_ordinals_not_in_frames():
    """Two taped frames are ten Door ordinals, and a five-page cap cannot hold them.

    A span counted in frames would have called this cluster two pages and passed it
    to a Door that then has no legal seam anywhere inside it — the whole submission
    refused, at the stage that can no longer explain why.
    """
    frames = _taped_frames()
    proposals = {
        item.path: door.triage_manifest.make_row(
            corpus_id="parish-a",
            source_frame_sha256=digest_bytes(item.data),
            frame=dict(_TAPED_FRAME),
            split=_taped_split(),
            re_shoot_cluster_id=None,
            confidence=0,
            mode="manual",
            actor={"kind": "human", "identity": "operator", "revision": None},
            human_override=True,
        )
        for item in frames
    }
    confirmation, recipe, evidence_manifest, evidence = _taped_confirmation(frames)
    with pytest.raises(producer.ProducerRefusal, match="cluster-span-over-cap"):
        producer.produce(
            frames,
            corpus_id="parish-a",
            mode="manual",
            confirmation=confirmation,
            instrument_recipe=recipe,
            evidence_manifest=evidence_manifest,
            evidence_records=evidence,
            transcribed_rows_by_path=proposals,
            max_pages_per_shard=5,
        )


def test_a_submitted_frame_with_no_triage_row_is_refused_and_so_is_an_extra_row():
    """The Door's half of Unit 6B's coverage invariant, in both directions.

    The producer proves exact coverage over what it was handed; the Door proves it
    again over what was actually submitted, because the two sets are only the same
    if nothing was added between them. A submitted frame with no row would be a
    frame fanned out with no declared geometry — silently, since every other row
    still expands. There is no corpus-scoped sharding yet to explain away a row
    naming a frame outside the submission, so the reverse direction is refused too:
    a manifest's rows must exactly match what was submitted.
    """
    submitted, absent = png(4, 3), png(4, 3, rows=None, bit_depth=8, color_type=2)
    submitted_digest, absent_digest = digest_bytes(submitted), digest_bytes(absent)

    def row(digest, width, height):
        return door.triage_manifest.make_row(
            corpus_id="parish-a",
            source_frame_sha256=digest,
            frame={"width": width, "height": height},
            split=door.triage_manifest.make_split(
                [
                    door.triage_manifest.make_part(
                        {"x": 0, "y": 0, "w": width, "h": height},
                        {"x": 0, "y": 0, "w": width, "h": height},
                        0,
                        colour_mode="keep",
                    )
                ]
            ),
            re_shoot_cluster_id=None,
            confidence=0,
            mode="manual",
            actor={"kind": "producer", "identity": "operations.triage.producer", "revision": "r1"},
            human_override=False,
        )

    with pytest.raises(ContractError, match="no row for a submitted source frame"):
        door.expand_sources(
            [{"relative_path": "a.png", "sha256": submitted_digest}],
            reader({"a.png": submitted}),
            POLICY,
            triage_rows={absent_digest: row(absent_digest, 4, 3)},
        )

    with pytest.raises(ContractError, match="naming no submitted source frame"):
        door.expand_sources(
            [{"relative_path": "a.png", "sha256": submitted_digest}],
            reader({"a.png": submitted}),
            POLICY,
            triage_rows={
                submitted_digest: row(submitted_digest, 4, 3),
                absent_digest: row(absent_digest, 4, 3),
            },
        )


def test_a_legal_seam_between_byte_identical_split_files_is_not_mistaken_for_a_pair():
    """A pair is one declared path's parts, not every adjacent copy of its digest."""
    row = {"re_shoot_cluster_id": None}
    digest = "a" * 64
    sources = [
        SourceEntry(1, "copy-a.jpg", digest, 0, triage_row=row, triage_part_index=0),
        SourceEntry(2, "copy-a.jpg", digest, 1, triage_row=row, triage_part_index=1),
        SourceEntry(3, "copy-b.jpg", digest, 0, triage_row=row, triage_part_index=0),
        SourceEntry(4, "copy-b.jpg", digest, 1, triage_row=row, triage_part_index=1),
    ]

    shards = door.content_aware_shards(sources, max_pages_per_shard=2)

    assert [[source.ordinal for source in shard] for shard in shards] == [[1, 2], [3, 4]]


def test_nested_cluster_spans_remain_whole_without_inventing_a_winner():
    rows = {
        "outer": {"re_shoot_cluster_id": "outer"},
        "inner": {"re_shoot_cluster_id": "inner"},
        "none": {"re_shoot_cluster_id": None},
    }
    cluster_by_ordinal = {
        1: "none",
        2: "outer",
        3: "inner",
        4: "none",
        5: "none",
        6: "inner",
        7: "outer",
        8: "none",
    }
    sources = [
        SourceEntry(
            ordinal,
            f"{ordinal}.jpg",
            f"{ordinal:064x}",
            0,
            triage_row=rows[cluster_by_ordinal[ordinal]],
            triage_part_index=0,
        )
        for ordinal in range(1, 9)
    ]

    shards = door.content_aware_shards(sources, max_pages_per_shard=6)

    assert [[source.ordinal for source in shard] for shard in shards] == [
        [1],
        [2, 3, 4, 5, 6, 7],
        [8],
    ]


def test_unicode_and_separator_like_relative_paths_have_exact_stable_ordinals():
    files = {
        "é.png": png(4, 3),
        "e\u0301.png": png(5, 3),
        "a\\b.png": png(6, 3),
        "a/b.png": png(7, 3),
    }
    rows = [
        {"relative_path": path, "sha256": digest_bytes(data), "bytes": len(data)}
        for path, data in reversed(list(files.items()))
    ]

    first = expand_sources(rows, reader(files), POLICY)
    second = expand_sources(list(reversed(rows)), reader(files), POLICY)

    assert first == second
    assert [source.declared_path for source in first] == [
        "a/b.png",
        "a\\b.png",
        "e\u0301.png",
        "é.png",
    ]


def _single_part_triage_row(
    master: bytes,
    *,
    frame: tuple[int, int],
    rotation_millidegrees: int = 0,
    colour_mode: str = "rgb",
    cluster_id: str | None = None,
):
    width, height = frame
    return door.triage_manifest.make_row(
        corpus_id="parish-a",
        source_frame_sha256=digest_bytes(master),
        frame={"width": width, "height": height},
        split=door.triage_manifest.make_split(
            [
                door.triage_manifest.make_part(
                    {"x": 0, "y": 0, "w": width, "h": height},
                    {"x": 0, "y": 0, "w": width, "h": height},
                    rotation_millidegrees,
                    colour_mode=colour_mode,
                )
            ]
        ),
        re_shoot_cluster_id=cluster_id,
        confidence=4,
        mode="auto",
        actor={"kind": "model", "identity": "triage", "revision": "r1"},
        human_override=False,
    )


def test_triage_rows_reconcile_exactly_with_the_submitted_shard():
    submitted = png(4, 3)
    absent = png(5, 3)
    rows = {
        digest_bytes(submitted): _single_part_triage_row(submitted, frame=(4, 3)),
        digest_bytes(absent): _single_part_triage_row(absent, frame=(5, 3)),
    }

    with pytest.raises(ContractError, match="1 row.*no submitted source frame") as refused:
        expand_sources(
            [{"relative_path": "submitted.png", "sha256": digest_bytes(submitted)}],
            reader({"submitted.png": submitted}),
            POLICY,
            triage_rows=rows,
        )
    assert "no source expansion was returned" in str(refused.value)
    assert "exactly match the submitted shard" in str(refused.value)


def test_a_missing_triage_row_names_the_loss_it_prevents_and_the_remedy():
    submitted = png(4, 3)

    with pytest.raises(ContractError, match="no row for a submitted source frame") as refused:
        expand_sources(
            [{"relative_path": "submitted.png", "sha256": digest_bytes(submitted)}],
            reader({"submitted.png": submitted}),
            POLICY,
            triage_rows={},
        )
    assert "disappear from the post-split census" in str(refused.value)
    assert "one row for every submitted frame digest" in str(refused.value)


def test_cluster_records_without_a_decision_manifest_are_not_ignored():
    submitted = png(4, 3)

    with pytest.raises(ContractError, match="without a decision manifest") as refused:
        expand_sources(
            [{"relative_path": "submitted.png", "sha256": digest_bytes(submitted)}],
            reader({"submitted.png": submitted}),
            POLICY,
            triage_clusters={},
        )
    assert "no ordinals were assigned" in str(refused.value)
    assert "supply the matching triage decision manifest" in str(refused.value)


def _triage_decision(master: bytes, row: dict):
    source = SourceEntry(
        1,
        "frame.img",
        digest_bytes(master),
        0,
        triage_row=row,
        triage_part_index=0,
        source_frame_index=0,
    )
    return door.decide(master, source, POLICY)


@pytest.mark.parametrize("declared_frame", [(5, 4), (7, 4), (6, 3), (6, 5)])
def test_the_frame_boundary_refuses_a_one_pixel_mismatch_on_every_edge(declared_frame):
    master = jpeg(6, 4)
    row = _single_part_triage_row(master, frame=declared_frame)

    decision = _triage_decision(master, row)

    assert decision.outcome == "refused"
    assert "frame dimensions do not match" in decision.reason
    assert "could omit or shift source pixels" in decision.reason
    assert "regenerate the row against the stored raster dimensions" in decision.reason


def test_exact_frame_equality_is_checked_before_a_part_rotation_changes_output_geometry():
    master = jpeg(6, 4)
    row = _single_part_triage_row(master, frame=(6, 4), rotation_millidegrees=90_000)

    decision = _triage_decision(master, row)

    assert decision.outcome == "admitted"
    assert decision.geometry == (4, 6)


def test_exif_orientation_is_metadata_not_an_unrecorded_frame_transform():
    image = Image.new("RGB", (6, 4), (10, 20, 30))
    exif = Image.Exif()
    exif[274] = 6  # display rotated 90 degrees clockwise; stored raster remains 6x4
    output = BytesIO()
    image.save(output, format="JPEG", exif=exif)
    master = output.getvalue()

    raw_frame = _single_part_triage_row(master, frame=(6, 4))
    display_frame = _single_part_triage_row(master, frame=(4, 6))

    assert _triage_decision(master, raw_frame).outcome == "admitted"
    refused = _triage_decision(master, display_frame)
    assert refused.outcome == "refused"
    assert "frame dimensions do not match" in refused.reason


def test_cluster_report_keeps_refused_members_and_parts_visible(tmp_path):
    admitted_master = png(4, 3)
    high_precision = Image.new("I;16", (4, 3))
    high_precision_output = BytesIO()
    high_precision.save(high_precision_output, format="TIFF", compression="raw")
    refused_master = high_precision_output.getvalue()
    cluster_id = "opening-7"
    rows = {
        digest_bytes(admitted_master): _single_part_triage_row(
            admitted_master, frame=(4, 3), colour_mode="keep", cluster_id=cluster_id
        ),
        digest_bytes(refused_master): _single_part_triage_row(
            refused_master, frame=(4, 3), colour_mode="keep", cluster_id=cluster_id
        ),
    }
    cluster = {
        "schema": door.triage_manifest.CLUSTER_SCHEMA,
        "corpus_id": "parish-a",
        "cluster_id": cluster_id,
        "member_frame_sha256": sorted(rows),
        "split_count": 1,
    }
    files = {"a.png": admitted_master, "b.tif": refused_master}
    sources = expand_sources(
        [{"relative_path": path, "sha256": digest_bytes(data)} for path, data in files.items()],
        reader(files),
        POLICY,
        triage_rows=rows,
        triage_clusters={cluster_id: cluster},
    )
    tree, context = open_door(tmp_path, sources)
    assert process_sources(context, tree, sources, reader(files), policy=POLICY) == 1

    report_path = door.publish_cluster_report(context)
    assert report_path is not None
    payload = json.loads(tree.read_bytes(report_path))["payload"]
    members = payload["clusters"][0]["members"]
    assert len(members) == 2
    assert {page["outcome"] for member in members for page in member["pages"]} == {
        "admitted",
        "refused",
    }
    assert "canonical" not in json.dumps(payload)
    assert "winner" not in json.dumps(payload)


def test_an_undecodable_split_frame_keeps_every_declared_page_ordinal(tmp_path):
    master = b"not a decodable raster"
    digest = digest_bytes(master)
    row = door.triage_manifest.make_row(
        corpus_id="parish-a",
        source_frame_sha256=digest,
        frame={"width": 6, "height": 4},
        split=door.triage_manifest.make_split(
            [
                door.triage_manifest.make_part(
                    {"x": 0, "y": 0, "w": 3, "h": 4},
                    {"x": 0, "y": 0, "w": 3, "h": 4},
                    0,
                    colour_mode="keep",
                ),
                door.triage_manifest.make_part(
                    {"x": 3, "y": 0, "w": 3, "h": 4},
                    {"x": 0, "y": 0, "w": 3, "h": 4},
                    0,
                    colour_mode="keep",
                ),
            ]
        ),
        re_shoot_cluster_id=None,
        confidence=1,
        mode="auto",
        actor={"kind": "model", "identity": "triage", "revision": "r1"},
        human_override=False,
    )
    sources = expand_sources(
        [{"relative_path": "broken.img", "sha256": digest, "bytes": len(master)}],
        reader({"broken.img": master}),
        POLICY,
        triage_rows={digest: row},
    )
    assert [(source.ordinal, source.triage_part_index) for source in sources] == [(1, 0), (2, 1)]

    tree, context = open_door(tmp_path, sources)
    assert (
        process_sources(context, tree, sources, reader({"broken.img": master}), policy=POLICY) == 0
    )
    records = admissions(tree)
    assert set(records) == {1, 2}
    assert all(record["outcome"] == "refused" for record in records.values())


def test_the_door_seals_the_same_triage_modes_file_its_point_of_use_check_reads(tmp_path):
    """Binding and point-of-use checks must resolve the same triage-modes bytes.

    Both sides now resolve `DEFAULT_TRIAGE_MODES_CONFIG_PATH`, so this holds the
    weaker remaining coupling: that the binding digest and the point-of-use check
    still agree about the bytes, whatever that constant later names. Drift would
    otherwise compare a run against bytes that did not govern it.
    """

    class Models:
        witness_chairs = ("attestator_1", "attestator_2", "attestator_3")
        adapter_recipes = {"door": "fake-door-v0"}

        @staticmethod
        def to_record():
            return {"models": "synthetic"}

    ledger = {
        "files": [{"relative_path": "spread.jpg", "sha256": "a" * 64, "bytes": 12}],
        "self_hash": "b" * 64,
    }
    bindings = door._real_bindings(
        Models(),
        ledger,
        POLICY,
        door.render_config.load_pdf_render_settings(minimum_dpi=door.pdf_render.MIN_RENDER_DPI),
        door.load_recovery_policy(),
        door.load_hard_failure_policy(),
        triage_document_digests={"triage-decision-manifest": "c" * 64},
        **_sealed_binding_digests(),
    )
    require_triage_modes(bindings["sealed_config_digests"])
    edited = tmp_path / "triage_modes.toml"
    edited.write_text(
        "[manual]\nreview_at_or_below_confidence = 3\n"
        "[semi]\nreview_at_or_below_confidence = 4\n"
        "[auto]\nreview_at_or_below_confidence = 4\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="changed between run binding") as refusal:
        require_triage_modes(bindings["sealed_config_digests"], edited)
    assert bindings["sealed_config_digests"]["triage-modes"] in str(refusal.value)
    assert digest_bytes(edited.read_bytes()) in str(refusal.value)


def test_a_run_sealed_before_the_repair_is_refused_by_name_not_by_key_error():
    """A run authority missing the modes seal names the binding fault.

    Missing bindings and changed files require distinct operator actions, so this
    path must not collapse into a bare KeyError or the drift refusal.
    """
    pre_repair_authority = {
        "sealed_config_digests": {
            "designator-padding": "a" * 64,
            "designator-geometry": "b" * 64,
            "alignment": "c" * 64,
            "corpus-frame-shard": "d" * 64,
            "pdf-render": "e" * 64,
            "recovery": "f" * 64,
            "hard-failure": "0" * 64,
            "data-handling": "1" * 64,
        }
    }
    sealed = run_sealed_config_digests(pre_repair_authority)
    assert "triage-modes" not in sealed
    with pytest.raises(ContractError, match="sealed no digest for the triage modes") as refusal:
        require_triage_modes(sealed)
    assert "changed between run binding" not in str(refusal.value)


def test_a_triage_producer_recipe_without_its_manifest_is_refused_at_the_real_door(
    tmp_path, monkeypatch
):
    """The recipe records how a decision manifest was produced; alone it decides nothing.

    Accepting it alone would seal a document into `config_digest` that governed no input
    to this run — a reproducibility claim about a step that never touched these bytes.
    Driven through the real door rather than a stub so the refusal is proven to arrive
    before anything is read, not merely to exist in the source.
    """
    approved, source, _policy, policy_path, ledger_path, _ledger = _approved_submission(
        tmp_path, {"FS-1234.png": png(4, 3)}
    )
    recipe_path = approved / "recipe.json"
    recipe_path.write_text(
        json.dumps(producer_recipe(instrument_config())),
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="producer recipe requires a triage decision manifest"):
        _run_real_door(
            monkeypatch,
            run_root=approved / "runs",
            source=source,
            policy_path=policy_path,
            ledger_path=ledger_path,
            run_id="recipe-without-manifest",
            extra=["--triage-producer-recipe", str(recipe_path)],
        )


def _triage_documents(folder, ledger):
    """A one-row triage manifest for a submitted 4x3 PNG, plus its recipe."""
    corpus_id = "parish-a"
    source_digest = ledger["files"][0]["sha256"]
    row = door.triage_manifest.make_row(
        corpus_id=corpus_id,
        source_frame_sha256=source_digest,
        frame={"width": 4, "height": 3},
        split=door.triage_manifest.make_split(
            [
                door.triage_manifest.make_part(
                    {"x": 0, "y": 0, "w": 4, "h": 3},
                    {"x": 0, "y": 0, "w": 4, "h": 3},
                    0,
                    colour_mode="keep",
                )
            ]
        ),
        re_shoot_cluster_id=None,
        confidence=0,
        mode="manual",
        actor={"kind": "producer", "identity": "triage-instrument", "revision": "v1"},
        human_override=False,
    )
    manifest_path = folder / "triage-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": door.triage_manifest.MANIFEST_SCHEMA,
                "corpus_id": corpus_id,
                "records": [row],
            }
        ),
        encoding="utf-8",
    )
    recipe_path = folder / "triage-recipe.json"
    recipe_path.write_text(json.dumps(producer_recipe(instrument_config())), encoding="utf-8")
    return manifest_path, recipe_path


def test_the_real_door_seals_and_proves_triage_modes_on_a_run_that_carries_geometry(
    tmp_path, monkeypatch
):
    """A real submission proves the modes seal before triage geometry is used.

    Triage manifests arrive only on the real path, so source-order inspection cannot
    prove that the check executes over geometry-bearing input.
    """
    approved, source, _policy, policy_path, ledger_path, ledger = _approved_submission(
        tmp_path, {"FS-1234.png": png(4, 3)}
    )
    manifest_path, recipe_path = _triage_documents(approved, ledger)
    run_root = approved / "runs"
    assert (
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=policy_path,
            ledger_path=ledger_path,
            run_id="triage-geometry",
            extra=[
                "--triage-decision-manifest",
                str(manifest_path),
                "--triage-producer-recipe",
                str(recipe_path),
            ],
        )
        == 0
    )
    run = RunTree(run_root, "triage-geometry").read_run()
    sealed = run_sealed_config_digests(run)
    assert sealed["triage-modes"] == digest_bytes(
        (Path(door.ROOT) / "config" / "triage_modes.toml").read_bytes()
    )
    require_triage_modes(sealed)
    # The fixed run-authority shape binds recipe bytes only through config_digest and
    # intentionally exposes no triage_document_digests field.
    assert "triage_document_digests" not in run


def test_a_door_with_the_pre_repair_missing_binding_refuses_before_it_expands_geometry(
    tmp_path, monkeypatch
):
    """A missing modes seal refuses before triage rows can shape any source.

    Stripping the current binding provides the invalid authority without reproducing
    an obsolete Door implementation.
    """
    approved, source, _policy, policy_path, ledger_path, ledger = _approved_submission(
        tmp_path, {"FS-1234.png": png(4, 3)}
    )
    manifest_path, recipe_path = _triage_documents(approved, ledger)
    real_bindings = door._real_bindings
    real_expand_sources = door.expand_sources
    expanded: list[int] = []

    def unsealed(*args, **kwargs):
        bindings = real_bindings(*args, **kwargs)
        bindings["sealed_config_digests"].pop("triage-modes")
        return bindings

    def watched_expand(*args, **kwargs):
        expanded.append(1)
        return real_expand_sources(*args, **kwargs)

    monkeypatch.setattr(door, "_real_bindings", unsealed)
    monkeypatch.setattr(door, "expand_sources", watched_expand)
    run_root = approved / "runs"
    with pytest.raises(ContractError, match="sealed no digest for the triage modes"):
        _run_real_door(
            monkeypatch,
            run_root=run_root,
            source=source,
            policy_path=policy_path,
            ledger_path=ledger_path,
            run_id="pre-repair",
            extra=[
                "--triage-decision-manifest",
                str(manifest_path),
                "--triage-producer-recipe",
                str(recipe_path),
            ],
        )
    assert expanded == []
    assert not (run_root / "pre-repair").exists()
