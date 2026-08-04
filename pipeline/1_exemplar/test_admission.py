"""The one admission module: by bytes, from one configured list, in a closed set.

Spec 03's test 1 lives here — correct magic and decodable bytes are admitted; the
wrong extension over the right bytes is admitted under the *detected* type; the
right extension over the wrong bytes is refused and named — and so does the half of
test 2 that is about the reason vocabulary itself. The other half, an all-refused
folder exiting loud, needs a run tree and is in `test_door.py`.
"""

import struct
import tomllib

import pytest
from admission import (
    ADMIT,
    ADMIT_OR_FAN_OUT,
    GAP,
    RENDER_PAGES,
    SNIFFABLE_FORMATS,
    AdmissionOutcome,
    FormatPolicyRefusal,
    RefusalReason,
    classify_detected_format,
    duplicate_reason,
    gap_detail,
    inspect_source,
    load_format_policy,
    reason,
    reason_code,
)
from image_formats import MAX_DIMENSION, MAX_SOURCE_BYTES
from synthetic_sources import (
    gif,
    heic,
    jpeg,
    multi_page_tiff,
    png,
    png_container,
    single_gray_page_pdf,
    tiff,
)

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

POLICY = load_format_policy()


# --- The admission list is configuration, and it is checked ----------------------


def test_the_shipped_admission_list_says_what_the_ledger_says_it_says():
    """Ruling 2026-08-04, item 2 rewrote the whole ledger: nothing is rejected, so
    there is no `refuse` action left to check. PNG/JPEG are single-image rasters;
    TIFF is usually one but can structurally hold more, so it is fanned out
    on-demand; PDF is a container of pages, always fanned out and rasterised. GIF
    and HEIC are sniffable with no reader built yet — a named gap, never a
    decision about the file."""
    assert POLICY["png"] == POLICY["jpeg"] == ADMIT
    assert POLICY["tiff"] == ADMIT_OR_FAN_OUT
    assert POLICY["pdf"] == RENDER_PAGES
    assert POLICY["gif"] == GAP
    assert POLICY["heic"] == GAP


def test_the_admission_list_covers_exactly_the_formats_the_door_can_detect(tmp_path):
    """A format with no row would be admitted or gapped by omission — the silent
    drift that let the old door accept `.gif` on one path and refuse it on another."""
    assert set(POLICY) == SNIFFABLE_FORMATS
    short = tmp_path / "short.toml"
    short.write_text('[format]\npng = "admit"\n', encoding="utf-8")
    with pytest.raises(FormatPolicyRefusal, match="Missing"):
        load_format_policy(short)


def test_the_admission_list_refuses_a_format_it_cannot_detect(tmp_path):
    rows = "\n".join(f'{name} = "gap"' for name in sorted(SNIFFABLE_FORMATS))
    path = tmp_path / "extra.toml"
    path.write_text(f'[format]\n{rows}\nwebp = "gap"\n', encoding="utf-8")
    with pytest.raises(FormatPolicyRefusal, match="Unknown"):
        load_format_policy(path)


def test_the_admission_list_refuses_an_action_outside_its_closed_set(tmp_path):
    rows = "\n".join(
        f'{name} = "{"maybe" if name == "png" else "gap"}"' for name in sorted(SNIFFABLE_FORMATS)
    )
    path = tmp_path / "bad-action.toml"
    path.write_text(f"[format]\n{rows}\n", encoding="utf-8")
    with pytest.raises(FormatPolicyRefusal, match="not one of"):
        load_format_policy(path)


def test_the_admission_list_no_longer_accepts_the_retired_refuse_action(tmp_path):
    """Ruling 2026-08-04, item 2 deleted `refuse` outright — a format nothing here
    can read gets `gap` instead. A policy file still spelling the retired word is
    exactly the same shape of error as an invented one."""
    rows = "\n".join(
        f'{name} = "{"refuse" if name == "gif" else POLICY[name]}"'
        for name in sorted(SNIFFABLE_FORMATS)
    )
    path = tmp_path / "retired-refuse.toml"
    path.write_text(f"[format]\n{rows}\n", encoding="utf-8")
    with pytest.raises(FormatPolicyRefusal, match="not one of"):
        load_format_policy(path)


def test_admitting_a_format_nothing_can_verify_is_refused_at_load(tmp_path):
    """The honest cost of the `.heic` gap. Turning that line to "admit" is one
    edit, and on its own it would admit unverified bytes under a name claiming they
    were checked — so it refuses, and says what is actually missing."""
    rows = "\n".join(
        f'{name} = "{"admit" if name == "heic" else POLICY[name]}"'
        for name in sorted(SNIFFABLE_FORMATS)
    )
    path = tmp_path / "heic-admitted.toml"
    path.write_text(f"[format]\n{rows}\n", encoding="utf-8")
    with pytest.raises(FormatPolicyRefusal, match="no structural validator"):
        load_format_policy(path)


def test_admit_or_fan_out_is_refused_for_a_format_other_than_tiff(tmp_path):
    """Only TIFF can structurally hold more than one image under one file; giving
    another admitted format this action would ask the door for a renderer that
    does not exist and never will for a single-image format."""
    rows = "\n".join(
        f'{name} = "{"admit-or-fan-out" if name == "png" else POLICY[name]}"'
        for name in sorted(SNIFFABLE_FORMATS)
    )
    path = tmp_path / "png-admit-or-fan-out.toml"
    path.write_text(f"[format]\n{rows}\n", encoding="utf-8")
    with pytest.raises(FormatPolicyRefusal, match="only TIFF"):
        load_format_policy(path)


def test_render_pages_is_refused_for_a_format_other_than_pdf(tmp_path):
    rows = "\n".join(
        f'{name} = "{"render-pages" if name == "png" else POLICY[name]}"'
        for name in sorted(SNIFFABLE_FORMATS)
    )
    path = tmp_path / "png-render-pages.toml"
    path.write_text(f"[format]\n{rows}\n", encoding="utf-8")
    with pytest.raises(FormatPolicyRefusal, match="renders pages unconditionally"):
        load_format_policy(path)


def test_an_unreadable_admission_list_is_a_failed_check_not_an_empty_one(tmp_path):
    missing = tmp_path / "nothing-here.toml"
    with pytest.raises(FormatPolicyRefusal, match="could not be read"):
        load_format_policy(missing)


def test_the_shipped_admission_list_parses_as_the_one_table_it_claims_to_be():
    with open("config/admitted_formats.toml", "rb") as handle:
        assert set(tomllib.load(handle)) == {"format"}


def test_a_format_the_table_has_no_opinion_on_is_refused_rather_than_admitted():
    """Unreachable while `load_format_policy` requires full coverage, and kept as
    the fail-closed catch. Unknown is never "fine" — but it is no longer named
    `REFUSED_FORMAT` either, since that whole category is retired; it reads as the
    same `UNSUPPORTED_VARIANT` a genuine pipeline gap does."""
    assert classify_detected_format("webp", POLICY) is RefusalReason.UNSUPPORTED_VARIANT
    assert classify_detected_format(None, POLICY) is RefusalReason.UNRECOGNIZED_FORMAT


def test_a_gap_format_classifies_as_the_gap_action_itself():
    """`classify_detected_format` hands back the literal `gap` action — like
    `render-pages` and `admit-or-fan-out` — so the caller decides how to word the
    refusal; `inspect_source` is what actually turns it into `UNSUPPORTED_VARIANT`."""
    assert classify_detected_format("gif", POLICY) == GAP
    assert classify_detected_format("heic", POLICY) == GAP


# --- Test 1: admission by bytes, never by extension ------------------------------


@pytest.mark.parametrize(
    ("data", "detected", "geometry"),
    [
        (png(2, 3), "png", (2, 3)),
        (jpeg(3, 2), "jpeg", (3, 2)),
        (tiff(4, 5), "tiff", (4, 5)),
    ],
)
def test_correct_bytes_are_admitted_and_their_true_geometry_is_read(data, detected, geometry):
    outcome = inspect_source(data, declared_sha256=None, policy=POLICY)
    assert outcome == AdmissionOutcome("admitted", None, detected, digest_bytes(data), geometry)


def test_the_declared_name_plays_no_part_at_all():
    """A `.txt` of a genuine JPEG and a `.png` of garbage decide identically to the
    same bytes under any other name, because no name is ever passed in. There is no
    parameter here to mislead — which is the point, and this is what would notice a
    suffix creeping back in as a "harmless" extra refusal."""
    assert inspect_source(jpeg(), declared_sha256=None, policy=POLICY).outcome == "admitted"
    outcome = inspect_source(b"not an image", declared_sha256=None, policy=POLICY)
    assert outcome.outcome == "refused"
    assert reason_code(outcome.reason) is RefusalReason.UNRECOGNIZED_FORMAT


def test_a_declared_digest_that_does_not_match_the_bytes_is_refused_and_named():
    outcome = inspect_source(png(), declared_sha256="0" * 64, policy=POLICY)
    assert reason_code(outcome.reason) is RefusalReason.DIGEST_MISMATCH
    assert outcome.digest == digest_bytes(png())


def test_a_single_directory_tiff_is_admitted_directly_unlike_a_multi_page_container():
    """`admit-or-fan-out` behaves exactly like `admit` for the common case — a
    single-directory TIFF is decided here, not fanned out — but a file that
    actually holds more than one directory refuses here rather than being asked
    for as one image, the same shape `test_a_pdf_is_never_admitted_as_one_image_here`
    proves for `render-pages`, just without the `ValueError`: this action's
    common case is the ordinary single-file path, so reaching it with the wrong
    shape of bytes is a refusal, not a caller's programming error."""
    outcome = inspect_source(tiff(6, 5), declared_sha256=None, policy=POLICY)
    assert outcome.outcome == "admitted"
    assert outcome.detected_format == "tiff"

    multi = multi_page_tiff(((6, 5), (4, 3)))
    outcome = inspect_source(multi, declared_sha256=None, policy=POLICY)
    assert outcome.outcome == "refused"
    assert reason_code(outcome.reason) is RefusalReason.UNSUPPORTED_VARIANT
    assert "chains more than one image directory" in outcome.reason


def test_a_pdf_is_never_admitted_as_one_image_here():
    """A container of pages is not one image; the door fans it out. Asking this
    function to decide one is a programming error under the shipped list, which
    always fans PDF out — but only while the list actually asks for the fan-out.
    A list that has no reader for PDF at all still refuses it as an ordinary named
    gap, the same as any other unsupported format."""
    with pytest.raises(ValueError, match="multi-page container"):
        inspect_source(single_gray_page_pdf(), declared_sha256=None, policy=POLICY)

    gapped = {**POLICY, "pdf": GAP}
    outcome = inspect_source(single_gray_page_pdf(), declared_sha256=None, policy=gapped)
    assert reason_code(outcome.reason) is RefusalReason.UNSUPPORTED_VARIANT
    assert "pdf" in outcome.reason


def test_the_pdf_row_is_the_whole_of_the_pdf_decision():
    """`classify_detected_format` is the one authority, and the door reads it rather
    than naming a format itself. Three of four reviewing seats found the same bypass
    — a hardcoded `sniff(data) == "pdf"` fan-out that never consulted this — so this
    pins the table's answer for every action the row can carry."""
    assert classify_detected_format("pdf", {**POLICY, "pdf": GAP}) == GAP
    assert classify_detected_format("pdf", {**POLICY, "pdf": RENDER_PAGES}) == RENDER_PAGES


# --- Test 2: the refusal vocabulary is closed, and every member is exercised ------


def _oversized_png() -> bytes:
    """A genuine PNG container declaring a geometry past the admission limits.

    Structurally sound and honestly what it says it is — it is simply larger than
    this door will inspect, which is a documented limit rather than damage.
    """
    header = struct.pack(">IIBBBBB", MAX_DIMENSION + 1, 2, 8, 0, 0, 0, 0)
    return png_container((b"IHDR", header), (b"IEND", b""))


def _exercised() -> dict[RefusalReason, str]:
    """One real refusal per closed-set member, produced by the module under test.

    `UNREADABLE` and `DUPLICATE` are the two the door owns rather than this module:
    a read failure and a second copy of an already-admitted source are both facts
    about a *submission*, not about one file's bytes. They are produced here through
    the same one spelling and proven end to end in `test_door.py`.
    """
    return {
        RefusalReason.EMPTY: inspect_source(b"", declared_sha256=None, policy=POLICY).reason,
        RefusalReason.TOO_LARGE: inspect_source(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_SOURCE_BYTES, declared_sha256=None, policy=POLICY
        ).reason,
        RefusalReason.UNRECOGNIZED_FORMAT: inspect_source(
            b"plain text", declared_sha256=None, policy=POLICY
        ).reason,
        RefusalReason.CORRUPT: inspect_source(
            png()[:-4], declared_sha256=None, policy=POLICY
        ).reason,
        RefusalReason.UNSUPPORTED_VARIANT: inspect_source(
            _oversized_png(), declared_sha256=None, policy=POLICY
        ).reason,
        RefusalReason.DIGEST_MISMATCH: inspect_source(
            png(), declared_sha256="0" * 64, policy=POLICY
        ).reason,
        RefusalReason.DUPLICATE: duplicate_reason(3),
        RefusalReason.UNREADABLE: reason(RefusalReason.UNREADABLE, "no such file"),
    }


def test_every_refusal_reason_in_the_closed_set_is_exercised():
    """An unused member would be an untested refusal path, which is exactly the gap
    invariant #3 exists to close. Asserted rather than trusted."""
    exercised = _exercised()
    assert set(exercised) == set(RefusalReason)
    for code, text in exercised.items():
        assert reason_code(text) is code, f"{code} produced {text!r}"


def test_the_two_refusal_voices_stay_apart():
    """Damaged bytes and an honest variant this door cannot read are different facts
    and different decisions for Tyrel. Collapsing them would tell him a photograph
    was corrupt when the truth is that we cannot read that flavour of it yet."""
    corrupt = inspect_source(png()[:-4], declared_sha256=None, policy=POLICY)
    unsupported = inspect_source(_oversized_png(), declared_sha256=None, policy=POLICY)
    assert reason_code(corrupt.reason) is RefusalReason.CORRUPT
    assert reason_code(unsupported.reason) is RefusalReason.UNSUPPORTED_VARIANT


def test_a_gapped_format_names_the_format_rather_than_counting_anonymously():
    """Audit Q14's actual defect was an anonymous "unsupported" counter. Both
    formats with no reader yet are refused *by name* — and, per ruling
    2026-08-04, item 2, worded as a pipeline gap rather than a decision about the
    file, which `gap_detail` states explicitly."""
    for data, detected in ((gif(), "gif"), (heic(), "heic")):
        outcome = inspect_source(data, declared_sha256=None, policy=POLICY)
        assert reason_code(outcome.reason) is RefusalReason.UNSUPPORTED_VARIANT
        assert detected in outcome.reason
        assert outcome.detected_format == detected
        assert reason(RefusalReason.UNSUPPORTED_VARIANT, gap_detail(detected)) == outcome.reason


def test_a_reason_outside_the_closed_set_is_refused_when_it_is_read_back():
    """The skeleton's free-text reasons are what this spec replaced. A consumer that
    accepted one because it happened to be a string would have replaced nothing."""
    for text in ("page.png does not carry a PNG signature", "", None, 42, "invented: detail"):
        with pytest.raises(ContractError):
            reason_code(text)
