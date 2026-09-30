"""The atomic all-capture presentation, `common/cross_capture_autopsia.py`."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.contracts.canonical import digest_bytes, digest_of  # noqa: E402
from common.contracts.errors import SchemaRefusal  # noqa: E402
from common.cross_capture_autopsia import (  # noqa: E402
    OVER_CAPACITY,
    assemble_reader_input,
    atomic_delivered_pixels,
    build_autopsia,
    build_autopsia_from_run,
    invoke_one_logical_read,
    over_capacity_reason,
    validate_autopsia,
)
from common.physical_act_partition import source_ledger_from_run  # noqa: E402

A, B = "a" * 64, "b" * 64
REF = {"relative_path": "blobs/x", "sha256": "c" * 64}
# The stand-in transport returns path bytes, making each fixture reference
# independently digest-verifiable without filesystem state.
READ_BYTES = str.encode


def blob(path):
    return {"relative_path": path, "sha256": digest_bytes(READ_BYTES(path))}


def view(capture, suffix):
    return {
        "view_id": f"view-{suffix}",
        "physical_page_id": "ppg_fixture",
        "source_sha256": capture,
        "page_ids": [f"pg_{suffix}"],
        "local_act_ids": [f"act_{suffix}"],
        "region_refs": [blob(f"blobs/crop-{suffix}")],
        "page_render_refs": [blob(f"blobs/page-{suffix}")],
        "alignment_ref": f"alignment-{suffix}",
        "visibility_evidence_refs": [blob(f"blobs/visibility-{suffix}")],
    }


def autopsia(views=None):
    return build_autopsia(
        logical_act_id="pac_fixture",
        partition_ref=REF,
        required_capture_sha256s=[A, B],
        views=views or [view(A, "a"), view(B, "b")],
    )


def test_complete_capture_set_is_canonical_and_shuffle_invariant():
    assert autopsia() == autopsia([view(B, "b"), view(A, "a")])
    assert validate_autopsia(autopsia())["member_conservation"]["required_count"] == 2


def test_every_capture_pixel_arrives_in_one_atomic_delivery():
    delivered = atomic_delivered_pixels(autopsia(), read_bytes=READ_BYTES, max_images=6)
    assert delivered == {
        "region_images": [b"blobs/crop-a", b"blobs/crop-b"],
        "page_render_images": [b"blobs/page-a", b"blobs/page-b"],
    }


class RecordingReader:
    def __init__(self):
        self.calls = []

    def read(self, dossier, *, pass_kind, delivered_pixels):
        self.calls.append((dossier, pass_kind, delivered_pixels))
        return {"text": "joint ink", "stop_reason": None}


def test_one_logical_read_receives_all_pixels_testimony_and_prior_in_one_call():
    reader = RecordingReader()
    dossier = {
        "logical_act_id": "pac_fixture",
        "testimonia": [{"capture": A}, {"capture": B}],
        "prior_draft": {"text": "prior"},
    }
    delivered, pixels, result = invoke_one_logical_read(
        reader,
        autopsia=autopsia(),
        dossier=dossier,
        read_bytes=READ_BYTES,
        max_images=6,
        pass_kind="perlectio",
    )
    assert result["text"] == "joint ink"
    assert len(reader.calls) == 1
    seen, kind, received = reader.calls[0]
    assert kind == "perlectio"
    assert seen == delivered
    assert received == pixels
    assert seen["testimonia"] == dossier["testimonia"]
    assert seen["prior_draft"] == dossier["prior_draft"]
    assert seen["cross_capture_autopsia"] == autopsia()
    assert received["region_images"] == [b"blobs/crop-a", b"blobs/crop-b"]


def test_the_reader_receives_a_dossier_digest_sealing_the_delivered_fields():
    reader = RecordingReader()
    body = {"testimonia": []}
    delivered, _pixels, _result = invoke_one_logical_read(
        reader,
        autopsia=autopsia(),
        dossier={**body, "dossier_digest": digest_of(body)},
        read_bytes=READ_BYTES,
        max_images=6,
        pass_kind="perlectio",
    )
    sealed = {key: value for key, value in delivered.items() if key != "dossier_digest"}
    assert delivered["dossier_digest"] == digest_of(sealed)
    assert reader.calls[0][0] == delivered


def test_the_assembled_presentation_carries_every_capture_not_one():
    """Renamed to what it checks. It ran one identical assembly four times.

    The loop variable never reached `assemble_reader_input`, so the four
    iterations made the same single call and the name promised per-arm coverage
    the body did not provide. The genuine per-arm proof is
    `test_clustered_logical_passes_make_one_establishing_call_and_no_capture_local_calls`
    below, which drives the arms through the recording reader.
    """
    dossier = {"testimonia": [], "prior_draft": {"text": "prior"}}
    delivered, pixels = assemble_reader_input(
        autopsia=autopsia(),
        dossier=dossier,
        read_bytes=READ_BYTES,
        max_images=6,
    )
    assert delivered["cross_capture_autopsia"]["required_capture_sha256s"] == [A, B]
    assert len(pixels["region_images"]) == 2


def test_unmeasured_or_insufficient_capacity_holds_before_any_read():
    for capacity in (None, 3):
        with pytest.raises(SchemaRefusal, match=OVER_CAPACITY):
            atomic_delivered_pixels(
                autopsia(), read_bytes=lambda _: pytest.fail("read"), max_images=capacity
            )


def test_capture_drop_or_preference_field_is_refused():
    with pytest.raises(SchemaRefusal, match="required and delivered"):
        autopsia([view(A, "a")])
    bad = view(A, "a")
    bad["selected_view"] = True
    # Pinned to the preference screen's own message, not the closed-schema
    # refusal `_view` would also raise on this row (it also has an unknown
    # field): a test that accepts either passes even if the preference screen
    # is deleted and only the shape check remains.
    with pytest.raises(SchemaRefusal, match=r"forbidden preference field 'selected_view'"):
        autopsia([bad, view(B, "b")])


@pytest.mark.parametrize(
    ("required", "views", "match"),
    [
        (None, [view(A, "a"), view(B, "b")], "required_capture_sha256s"),
        ([A, B], None, "views"),
    ],
)
def test_malformed_collection_boundaries_are_named_schema_refusals(required, views, match):
    with pytest.raises(SchemaRefusal, match=match):
        build_autopsia(
            logical_act_id="pac_fixture",
            partition_ref=REF,
            required_capture_sha256s=required,
            views=views,
        )


def test_source_ledger_is_derived_from_manifest_not_proposals():
    assert source_ledger_from_run(
        {"source_manifest": [{"sha256": A}, {"sha256": A}, {"sha256": B}]}
    ) == {A, B}
    # The refusal names the capture that is missing, not merely that one is.
    # An operator's next act is to fetch a photograph, and a logical act over
    # several captures does not say which one unless the sentence does.
    with pytest.raises(SchemaRefusal, match=f"cluster-member-absent.*{B}") as absent:
        build_autopsia_from_run(
            run={"source_manifest": [{"sha256": A}]},
            logical_act_id="pac_fixture",
            partition_ref=REF,
            required_capture_sha256s=[A, B],
            views=[view(A, "a"), view(B, "b")],
        )
    # Only the absent one: naming a capture the run did supply would send the
    # operator after a photograph that is already here.
    assert A not in str(absent.value)


def test_the_preference_screen_walks_a_deep_payload_instead_of_the_interpreter_stack():
    """An untrusted payload must reach a named refusal, never a RecursionError.

    Both entries to this screen run it before any shape check closes the value:
    `build_autopsia` screens `views` before `_view` validates a row, and
    `assemble_reader_input` screens a reader dossier checked only for being a
    dict. So the walk's input is arbitrary caller data, and a recursive walk
    over it exhausted the stack at a few thousand levels -- a crash carrying no
    statement of what was wrong, where the sibling screen in
    `common.corpus_register` had already been made iterative for this reason.
    """
    deep = {"leaf": 1}
    for _ in range(50_000):
        deep = {"nested": deep}

    # Clean to the bottom: the screen walks the whole depth, finds no forbidden
    # field, and hands the row on to the shape check, which is what refuses it.
    # Raised through the public door, so this pins the screen the callers
    # actually reach rather than the private helper behind it.
    with pytest.raises(SchemaRefusal, match="view is not its closed schema"):
        build_autopsia(
            logical_act_id="pac_fixture",
            partition_ref=REF,
            required_capture_sha256s=[A],
            views=[deep],
        )

    # And a forbidden field buried at the bottom is still found and named.
    buried = {"winner": "capture-a"}
    for _ in range(50_000):
        buried = {"nested": buried}
    with pytest.raises(SchemaRefusal, match="forbidden preference field 'winner'"):
        build_autopsia(
            logical_act_id="pac_fixture",
            partition_ref=REF,
            required_capture_sha256s=[A],
            views=[buried],
        )


def test_a_view_image_that_no_longer_matches_its_digest_is_refused():
    """A path is not evidence that its current bytes still match the sealed crop."""
    reader = RecordingReader()
    with pytest.raises(SchemaRefusal, match="changed under a sealed reference"):
        atomic_delivered_pixels(
            autopsia(), read_bytes=lambda path: b"tampered " + READ_BYTES(path), max_images=6
        )
    with pytest.raises(SchemaRefusal, match="changed under a sealed reference"):
        invoke_one_logical_read(
            reader,
            autopsia=autopsia(),
            dossier={"testimonia": []},
            read_bytes=lambda path: b"tampered " + READ_BYTES(path),
            max_images=6,
            pass_kind="perlectio",
        )
    assert reader.calls == []


def test_an_unreadable_view_image_is_a_named_refusal_not_a_bare_os_error():
    def missing(_path):
        raise OSError("no such blob")

    with pytest.raises(SchemaRefusal, match="could not be read"):
        atomic_delivered_pixels(autopsia(), read_bytes=missing, max_images=6)


def test_over_capacity_is_answerable_before_it_is_a_refusal():
    """Capacity must be measurable before a producer commits to a reader call."""
    assert over_capacity_reason(autopsia(), 4) is None
    assert OVER_CAPACITY in over_capacity_reason(autopsia(), 3)
    assert OVER_CAPACITY in over_capacity_reason(autopsia(), None)


def test_a_late_preference_field_is_refused_before_the_reader_is_called():
    reader = RecordingReader()
    with pytest.raises(SchemaRefusal, match="forbidden preference field"):
        invoke_one_logical_read(
            reader,
            autopsia=autopsia(),
            dossier={"testimonia": [], "winner_capture": A},
            read_bytes=READ_BYTES,
            max_images=6,
            pass_kind="perlectio",
        )
    assert reader.calls == []


# --- Security review: path-handling on every digest-bound reference -------


@pytest.mark.parametrize("escaping_path", ["../outside", "a/../../outside", "/etc/passwd"])
def test_a_traversal_partition_ref_is_refused_before_any_reader_call(escaping_path):
    with pytest.raises(SchemaRefusal, match="is not a canonical run-relative path"):
        build_autopsia(
            logical_act_id="pac_fixture",
            partition_ref={"relative_path": escaping_path, "sha256": "c" * 64},
            required_capture_sha256s=[A, B],
            views=[view(A, "a"), view(B, "b")],
        )


def test_a_traversal_region_ref_is_refused_before_any_reader_call():
    escaping_view = {
        **view(A, "a"),
        "region_refs": [{"relative_path": "../outside", "sha256": "c" * 64}],
    }
    with pytest.raises(SchemaRefusal, match="is not a canonical run-relative path"):
        build_autopsia(
            logical_act_id="pac_fixture",
            partition_ref=REF,
            required_capture_sha256s=[A, B],
            views=[escaping_view, view(B, "b")],
        )
