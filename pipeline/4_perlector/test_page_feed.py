"""The page feed and the page prompt, proven on synthetic page Testimonia."""

from __future__ import annotations

import copy
import io
import json
import random
import re
from pathlib import Path
from types import SimpleNamespace

import dossier
import page_feed
import page_overlay
import page_prompt
import prompts
import protocol
import pytest
from PIL import Image, ImageColor, ImageDraw

from common.churro_document import churro_system_prompt
from common.contracts.canonical import code_digest, digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.native_witness import CHURRO_OUTPUT_TOKENS, derive_churro_capture
from common.request_capacity import (
    PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST,
    RequestCapacityRefusal,
    page_request_capacity,
)
from common.witness_regime import pseudonym_for

ROOT = Path(__file__).resolve().parents[2]
PAGE = (2550, 3300)
RENDER = {
    "source_page_id": "page-1",
    "source_page_ordinal": 1,
    "source": {"relative_path": "0_exemplar/p.png", "sha256": "1" * 64},
    "image_path": "4_perlector/blobs/r.png",
    "image_sha256": "2" * 64,
    "transform": {
        "operation": "downscale-for-page-context",
        "source_dimensions": {"w": 2550, "h": 3300},
        "target_dimensions": {"w": 1978, "h": 2560},
        "maximum_edge": 2560,
        "resampler": "pillow-lanczos",
    },
    "reason": "legible-ink",
}


def _ref(tag: str) -> dict[str, str]:
    return {
        "relative_path": f"2_designator/artifacts/{tag}.json",
        "sha256": digest_bytes(tag.encode()),
    }


class _Blobs:
    """A run tree's byte store: retained raw responses by content-addressed path."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def retain(self, raw: bytes) -> dict[str, str]:
        sha = digest_bytes(raw)
        path = f"3_attestatores/blobs/sha256/{sha}"
        self.files[path] = raw
        return {"relative_path": path, "sha256": sha}

    def read_bytes(self, path: str) -> bytes:
        return self.files[path]


def _capture(adapter: str, ref: dict, parser: str, text: str) -> dict:
    return {
        "schema": "attestatores-model-view.v1",
        "adapter": adapter,
        "view": {},
        "raw_response_ref": ref,
        "transport_stop_reason": "stop",
        "stop_reason": "stop",
        "findings": [],
        "parse": {"state": "parsed", "parser": parser, "text": text},
    }


def chandra_testimonium(blobs: _Blobs, blocks: list[tuple[str, str, str]], outcome="read"):
    """`blocks` are `(data-bbox, data-label, text)` in Chandra's order."""
    html = "".join(
        f'<div data-bbox="{bbox}" data-label="{label}"><p>{text}</p></div>'
        for bbox, label, text in blocks
    ).encode()
    from common.chandra_layout import parse_layout_html

    page_text = parse_layout_html(html)["page_text"]
    ref = blobs.retain(html)
    return {
        "outcome": outcome,
        "payload": {"native_capture": _capture("chandra.v1", ref, "html", page_text)},
    }


def churro_testimonium(blobs: _Blobs, body: str, outcome="read"):
    system = churro_system_prompt("registry-v0.3.0")
    raw = body.encode()
    derived = derive_churro_capture(raw, "stop", parser="xml", system_prompt=system)
    capture = {
        "schema": "attestatores-model-view.v1",
        "adapter": "churro.v1",
        "view": {
            "prompt": {"system": system},
            "generation": {"max_new_tokens": CHURRO_OUTPUT_TOKENS},
        },
        "raw_response_ref": blobs.retain(raw),
        "transport_stop_reason": "stop",
        **derived,
    }
    return {"outcome": outcome, "payload": {"native_capture": capture}}


def dai_testimonium(blobs: _Blobs, records: list[tuple[dict, str]], outcome="read"):
    """`records` are `(bounds, text)` in the detector's order; the page text joins
    them in a different order, as the Attestatores join them by act."""
    joined, spans = "", [None] * len(records)
    for index in reversed(range(len(records))):
        text = records[index][1]
        joined += "\n" if joined else ""
        spans[index] = {"start": len(joined), "end": len(joined) + len(text)}
        joined += text
    captures = [
        _capture("dai.v1", blobs.retain(text.encode()), "text", text) for _bounds, text in records
    ]
    observed = [
        {"ordinal": index, "bounds": bounds, "bounds_source": "native", "span": spans[index]}
        for index, (bounds, _text) in enumerate(records)
    ]
    return {
        "outcome": outcome,
        "payload": {"payload": joined, "unit_captures": captures, "observed": observed},
    }


CHURRO_XML = (
    "<HistoricalDocument><Page><Header><Line>Registre 1780</Line></Header>"
    "<Body><Line>Le vingt mai</Line><Line>a été baptisé Pierre</Line></Body>"
    "<Footer><Line>12</Line></Footer></Page></HistoricalDocument>"
)


def witnesses(blobs: _Blobs, *, regime="named"):
    rows = [
        (
            "attestator_1",
            "chandra.v1",
            chandra_testimonium(
                blobs,
                [
                    ("100 50 900 120", "Page-Header", "Registre 1780"),
                    ("100 150 900 400", "Text", "Le vingt mai a été baptisé Pierre"),
                    ("not a box", "Text", "marge"),
                ],
            ),
        ),
        (
            "attestator_2",
            "dai.v1",
            dai_testimonium(
                blobs,
                [
                    ({"x": 255, "y": 495, "w": 2040, "h": 825}, "Le vingt may"),
                    ({"x": 255, "y": 1400, "w": 2040, "h": 300}, "baptisé Pierre"),
                ],
            ),
        ),
        ("attestator_3", "churro.v1", churro_testimonium(blobs, CHURRO_XML)),
    ]
    return [
        {
            "chair": chair,
            "witness_label": chair
            if regime == "named"
            else pseudonym_for(chair, run_id="run-1", config_digest="c" * 64),
            "adapter": adapter,
            "testimonium": testimonium,
            "testimonium_ref": _ref(f"t-{chair}"),
        }
        for chair, adapter, testimonium in rows
    ]


def surya(lines: int = 3, blocks: int = 2):
    line_band, block_band = 3000 // lines, 3000 // blocks
    return {
        "census_ref": _ref("census"),
        "lines": [
            {
                "box_px": {"x": 255, "y": 150 + line_band * index, "w": 2000, "h": line_band - 5},
                "ref": _ref(f"l{index}"),
            }
            for index in range(lines)
        ],
        # Given out of reading order: ids follow `position`, never list order.
        "blocks": [
            {
                "box_px": {
                    "x": 255,
                    "y": 150 + block_band * index,
                    "w": 2040,
                    "h": block_band - 10,
                },
                "label": "Text",
                "position": blocks - index,
                "ref": _ref(f"b{index}"),
            }
            for index in range(blocks)
        ],
    }


def switches(**changes):
    table = copy.deepcopy(protocol.load(ROOT / "config" / "perlector_protocol.toml")[0]["feed"])
    table.update(changes)
    return table


def feed_for(
    blobs,
    *,
    rows=None,
    recipe="unproven-real-perlector",
    regime="named",
    render=RENDER,
    census=None,
    **changes,
):
    return page_feed.build_page_feed(
        page_id="page-1",
        page_ordinal=1,
        page_size=PAGE,
        feed_switches=switches(**changes),
        witness_regime=regime,
        witnesses=rows if rows is not None else witnesses(blobs, regime=regime),
        surya=census if census is not None else surya(),
        page_render=render,
        serving_recipe=recipe,
        read_bytes=blobs.read_bytes,
    )


# --- the protocol's feed table ----------------------------------------------------


def test_the_shipped_protocol_reads_by_act_with_every_feed_input_on():
    sealed, _digest = protocol.load(ROOT / "config" / "perlector_protocol.toml")
    assert sealed["reading_unit"] == "act"
    assert sealed["feed"] == {
        "page_image": "legible",
        "witnesses": "all",
        "witness_units": "own",
        "witness_coordinates": True,
        "surya_lines": True,
        "surya_blocks": True,
        "crops": "off",
        "page_overlay": "off",
    }


def _write(tmp_path, old: str, new: str):
    shipped = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    assert old in shipped
    path = tmp_path / "perlector_protocol.toml"
    path.write_text(shipped.replace(old, new, 1), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('reading_unit = "act"', 'reading_unit = "region"', "reading_unit"),
        ('crops = "off"', 'crops = "on-request"', "crops"),
        ('page_image = "legible"', 'page_image = "tiny"', "page_image"),
        ('witness_units = "own"', 'witness_units = "lines"', "witness_units"),
        ("surya_lines = true", 'surya_lines = "yes"', "surya_lines"),
        ('witnesses = "all"', 'witnesses = "some"', "witnesses"),
        ('witnesses = "all"', 'witnesses = ["a", "a"]', "witnesses"),
        ('page_overlay = "off"', 'page_overlay = "circles"', "page_overlay"),
        ('page_image = "legible"', 'page_image = "off"', None),
        ("surya_blocks = true", "surya_blocks = true\nsurya_words = true", "closed schema"),
        ("surya_blocks = true\n", "", "closed schema"),
    ],
)
def test_the_feed_table_refuses_an_unknown_key_or_value(tmp_path, old, new, message):
    path = _write(tmp_path, old, new)
    if message is None:
        # page_image off loads alone, but not with the overlay on: it is drawn on the render.
        protocol.load(path)
        path.write_text(
            path.read_text().replace('page_overlay = "off"', 'page_overlay = "boxes"'),
            encoding="utf-8",
        )
        message = "page_overlay draws on a copy of the page render"
    with pytest.raises(ContractError, match=message):
        protocol.load(path)


def test_a_page_reading_protocol_with_a_witness_subset_loads(tmp_path):
    path = _write(tmp_path, 'reading_unit = "act"', 'reading_unit = "page"')
    path.write_text(
        path.read_text().replace('witnesses = "all"', 'witnesses = ["attestator_1"]'),
        encoding="utf-8",
    )
    sealed, _digest = protocol.load(path)
    assert (sealed["reading_unit"], sealed["feed"]["witnesses"]) == ("page", ["attestator_1"])


# --- units, ids and the record ------------------------------------------------------


def test_the_default_feed_shows_each_witness_in_its_own_units():
    blobs = _Blobs()
    feed = feed_for(blobs)
    assert feed["schema"] == "perlector-page-feed.v1"
    assert feed["reading_unit"] == "page"
    assert [row["letter"] for row in feed["witnesses"]] == ["A", "B", "C"]
    assert feed["page_size"] == {"w": 2550, "h": 3300}
    chandra, dai, churro = feed["witnesses"]
    # DAI's units are marked as its detector's records; the prompt never says so.
    assert [row["unit_kind"] for row in feed["witnesses"]] == [
        "layout-block",
        "detector-record",
        "line",
    ]
    assert [unit["id"] for unit in chandra["units"]] == ["A1", "A2", "A3"]
    assert chandra["units"][0] == {
        "id": "A1",
        "ordinal": 0,
        "box_px": {"x": 255, "y": 165, "w": 2040, "h": 231},
        "box_1000": [100, 50, 900, 120],
        "label": "Page-Header",
        "text": "Registre 1780",
    }
    # A malformed box is still a unit, with no box.
    assert chandra["units"][2]["box_px"] is None and chandra["units"][2]["text"] == "marge"
    # DAI in its detector's order, whatever order its page text joined them in.
    assert [unit["text"] for unit in dai["units"]] == ["Le vingt may", "baptisé Pierre"]
    assert dai["units"][0]["box_1000"] == [100, 150, 900, 400]
    assert [(unit["label"], unit["text"]) for unit in churro["units"]] == [
        ("Header", "Registre 1780"),
        ("Body", "Le vingt mai"),
        ("Body", "a été baptisé Pierre"),
        ("Footer", "12"),
    ]
    assert all(unit["box_px"] is None for unit in churro["units"])
    assert [line["id"] for line in feed["surya"]["lines"]] == ["L1", "L2", "L3"]
    # Blocks follow Surya's reading order, not the order they were handed in.
    assert [block["id"] for block in feed["surya"]["blocks"]] == ["S1", "S2"]
    assert feed["surya"]["blocks"][0]["box_px"]["y"] == 1650
    assert feed["answer_measure"] == {"longest_witness_characters": 51, "act_entries": 3}
    page_feed.verify_feed_digest(feed)


def test_box_1000_rounds_half_to_even_from_page_pixels():
    # 1 px on a 2,000 px page is 0.5 on the grid; 3 px is 1.5.
    assert page_feed.box_1000({"x": 1, "y": 3, "w": 2, "h": 2}, (2000, 2000)) == [0, 2, 2, 2]


def test_a_witness_that_did_not_read_is_a_row_with_its_outcome_and_no_units():
    blobs = _Blobs()
    rows = witnesses(blobs)
    rows[1]["testimonium"] = {"outcome": "failed", "payload": {}}
    feed = feed_for(blobs, rows=rows)
    assert feed["witnesses"][1]["outcome"] == "failed"
    assert feed["witnesses"][1]["units"] == []
    text = page_prompt.build_page_prompt("unproven-real-perlector", feed)
    assert "witness B (attestator_2): failed, no units" in text


def test_a_page_testimonium_with_no_native_capture_is_refused_by_name():
    blobs = _Blobs()
    rows = witnesses(blobs)
    rows[0]["testimonium"] = {"outcome": "read", "payload": {"payload": "joined text"}}
    with pytest.raises(SchemaRefusal, match="no native capture"):
        feed_for(blobs, rows=rows)


def test_a_dai_page_text_that_disagrees_with_a_retained_response_is_refused():
    blobs = _Blobs()
    rows = witnesses(blobs)
    rows[1]["testimonium"]["payload"]["payload"] = "Le vingt MAY\nbaptisé Pierre"
    with pytest.raises(SchemaRefusal, match="span the record states"):
        feed_for(blobs, rows=rows)


def test_a_retained_response_whose_bytes_changed_is_refused():
    blobs = _Blobs()
    rows = witnesses(blobs)
    path = rows[0]["testimonium"]["payload"]["native_capture"]["raw_response_ref"]["relative_path"]
    blobs.files[path] = b'<div data-bbox="1 1 2 2" data-label="Text">forged</div>'
    with pytest.raises(SchemaRefusal, match="bytes changed"):
        feed_for(blobs, rows=rows)


def test_witness_record_order_changes_no_id_and_no_byte():
    blobs = _Blobs()
    rows = witnesses(blobs)
    feed = feed_for(blobs, rows=rows)
    for seed in range(5):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        assert feed_for(blobs, rows=shuffled) == feed


def test_a_blinded_feed_names_no_chair_and_letters_follow_sorted_pseudonyms():
    blobs = _Blobs()
    feed = feed_for(blobs, regime="blinded")
    labels = [row["witness_label"] for row in feed["witnesses"]]
    assert labels == sorted(labels)
    assert all(row["chair"] is None for row in feed["witnesses"])
    by_label = {row["witness_label"]: row for row in witnesses(_Blobs(), regime="blinded")}
    adapters = [by_label[label]["adapter"] for label in labels]
    for row, adapter in zip(feed["witnesses"], adapters, strict=True):
        assert (
            row["units"][0]["text"]
            == {
                "chandra.v1": "Registre 1780",
                "dai.v1": "Le vingt may",
                "churro.v1": "Registre 1780",
            }[adapter]
        )
    text = page_prompt.build_page_prompt("unproven-real-perlector", feed)
    assert not re.search(r"attestator|chandra|churro|dai", text, re.I)


def test_the_named_regime_requires_the_chair_as_its_label():
    blobs = _Blobs()
    rows = witnesses(blobs)
    rows[0]["witness_label"] = "someone-else"
    with pytest.raises(SchemaRefusal, match="named regime"):
        feed_for(blobs, rows=rows)


def test_the_feed_names_no_preference_among_witnesses():
    dossier.assert_no_order_bearing_field(feed_for(_Blobs()))


# --- every switch -------------------------------------------------------------------


def _changed(**change):
    blobs = _Blobs()
    base = feed_for(blobs)
    other = feed_for(blobs, **change)
    return (
        base,
        other,
        page_prompt.build_page_prompt("unproven-real-perlector", base),
        page_prompt.build_page_prompt("unproven-real-perlector", other),
    )


def test_each_switch_is_recorded_and_moves_the_prompt_and_the_digest():
    for change in (
        {"witness_units": "flat"},
        {"witness_coordinates": False},
        {"surya_lines": False},
        {"surya_blocks": False},
        {"witnesses": ["attestator_1", "attestator_3"]},
    ):
        base, other, _base_text, other_text = _changed(**change)
        ((key, value),) = change.items()
        assert other["switches"][key] == value
        assert other["feed_digest"] != base["feed_digest"]
        assert other["prompt"]["rendered_sha256"] == digest_bytes(other_text.encode())
        assert other["prompt"]["rendered_sha256"] != base["prompt"]["rendered_sha256"]


def test_page_image_off_shows_no_render_and_says_so():
    blobs = _Blobs()
    feed = feed_for(blobs, render=None, page_image="off")
    assert feed["page_render"] is None
    assert "page image: not shown" in page_prompt.build_page_prompt("unproven-real-perlector", feed)
    with pytest.raises(SchemaRefusal, match="no page image"):
        feed_for(blobs, page_image="off")
    with pytest.raises(SchemaRefusal, match="full page"):
        feed_for(blobs, page_image="full")


def test_flat_units_join_each_witness_in_its_own_order_with_no_box():
    base, flat, _base_text, text = _changed(witness_units="flat")
    for row, own in zip(flat["witnesses"], base["witnesses"], strict=True):
        (unit,) = row["units"]
        assert unit["id"] == f"{row['letter']}1"
        assert unit["box_px"] is None and unit["box_1000"] is None and unit["label"] is None
        assert row["unit_kind"] == "page-text"
        assert unit["text"] == "\n".join(u["text"] for u in own["units"] if u["text"])
    assert "\nA2 " not in text and "\nA1 " in text
    # How the units are shown does not move the answer reserve.
    assert flat["answer_measure"] == base["answer_measure"]


def test_coordinates_off_hides_every_witness_box_but_keeps_the_sealed_geometry():
    base, other, base_text, text = _changed(witness_coordinates=False)
    assert "A1 [100,50,900,120] (Page-Header)" in base_text
    assert "A1 (Page-Header)" in text
    for row in other["witnesses"]:
        assert all(unit["box_1000"] is None for unit in row["units"])
    assert other["witnesses"][0]["units"][0]["box_px"] == base["witnesses"][0]["units"][0]["box_px"]
    # Surya's detections are their boxes, so they are not a witness coordinate.
    assert "L1 [" in text


def test_surya_switches_remove_their_ids_from_feed_and_prompt():
    _base, other, base_text, text = _changed(surya_lines=False)
    assert other["surya"]["lines"] == [] and "\nL1 [" not in text and "\nL1 [" in base_text
    _base, other, _base_text, text = _changed(surya_blocks=False)
    assert other["surya"]["blocks"] == [] and "\nS1 [" not in text
    blobs = _Blobs()
    both_off = page_feed.build_page_feed(
        page_id="page-1",
        page_ordinal=1,
        page_size=PAGE,
        feed_switches=switches(surya_lines=False, surya_blocks=False),
        witness_regime="named",
        witnesses=witnesses(blobs),
        surya=None,
        page_render=RENDER,
        serving_recipe="unproven-real-perlector",
        read_bytes=blobs.read_bytes,
    )
    assert both_off["surya"] is None
    with pytest.raises(SchemaRefusal, match="no Surya census"):
        page_feed.build_page_feed(
            page_id="page-1",
            page_ordinal=1,
            page_size=PAGE,
            feed_switches=switches(),
            witness_regime="named",
            witnesses=witnesses(blobs),
            surya=None,
            page_render=RENDER,
            serving_recipe="unproven-real-perlector",
            read_bytes=blobs.read_bytes,
        )


def test_the_witness_switch_shows_only_named_chairs_of_the_roster():
    _base, other, _base_text, text = _changed(witnesses=["attestator_3"])
    assert [row["witness_label"] for row in other["witnesses"]] == ["attestator_3"]
    assert other["witnesses"][0]["letter"] == "A"
    assert "attestator_1" not in text
    with pytest.raises(SchemaRefusal, match="not in this page's roster"):
        feed_for(_Blobs(), witnesses=["attestator_9"])


# --- the prompt ---------------------------------------------------------------------


def test_the_page_prompt_renders_units_compactly_with_the_instruction_last():
    blobs = _Blobs()
    text = page_prompt.build_page_prompt("unproven-real-perlector", feed_for(blobs))
    lines = text.split("\n")
    assert lines[0] == "page 1"
    assert 'A2 [100,50,900,120] (Text) "Le vingt mai a été baptisé Pierre"' not in lines
    assert 'A2 [100,150,900,400] (Text) "Le vingt mai a été baptisé Pierre"' in lines
    assert 'C1 (Header) "Registre 1780"' in lines
    assert "S1 [100,500,900,952] (Text)" in lines
    assert lines[-1] == page_prompt.PAGE_READING_INSTRUCTION


def test_the_fixture_recipe_renders_the_same_inputs_without_the_instruction():
    blobs = _Blobs()
    feed = feed_for(blobs, recipe="fake-perlector-v0")
    real = page_prompt.build_page_prompt("unproven-real-perlector", feed)
    assert real == page_prompt.build_page_prompt("fake-perlector-v0", feed) + "\n" + (
        page_prompt.PAGE_READING_INSTRUCTION
    )
    assert feed["prompt"]["instruction_sha256"] is None
    with pytest.raises(ValueError, match="no declared page prompt builder"):
        page_prompt.build_page_prompt("some-other-recipe", feed)


def test_the_page_instruction_keeps_the_act_instructions_doubt_marks_word_for_word():
    marks = (
        "Transcribe the ink exactly as it is written on the page. Do not modernize spelling, "
        "expand abbreviations, or correct the scribe. ",
        "Where ink cannot be read, write [[?]] in its place. Where a reading is uncertain, "
        "write it as [[reading]], or as [[reading|other|other]] to add other possible readings.",
    )
    for sentence in marks:
        assert sentence in prompts.TRANSCRIPTION_INSTRUCTION
        assert sentence in page_prompt.PAGE_READING_INSTRUCTION
    example = page_prompt.PAGE_READING_INSTRUCTION.split("in this form: ", 1)[1]
    assert set(json.loads(example)) == {"acts", "set_aside"}


def test_the_prompt_evidence_names_the_builder_the_instruction_and_the_bytes():
    blobs = _Blobs()
    feed = feed_for(blobs)
    text = page_prompt.build_page_prompt("unproven-real-perlector", feed)
    assert feed["prompt"] == {
        "serving_recipe": "unproven-real-perlector",
        "builder_sha256": page_prompt.BUILDER_SHA256,
        "rendered_sha256": digest_bytes(text.encode()),
        "instruction_sha256": digest_bytes(page_prompt.PAGE_READING_INSTRUCTION.encode()),
    }


def test_the_page_builder_digest_is_pinned_where_its_carried_rate_is_sealed():
    source = (Path(page_prompt.__file__)).read_text(encoding="utf-8")
    assert code_digest(source) == page_prompt.BUILDER_SHA256
    assert PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST == page_prompt.BUILDER_SHA256
    edited = source.replace("Read this page from its ink.", "Read this page.", 1)
    assert code_digest(edited) != page_prompt.BUILDER_SHA256


# --- the dense page against the served row --------------------------------------------


def _perlector_row(max_model_len: int = 32768):
    return SimpleNamespace(
        recipe="unproven-real-perlector",
        chair="perlector",
        tier="generic-80gb-plus",
        max_model_len=max_model_len,
        min_pixels=65536,
        max_pixels=5299200,
        patch_size=16,
        merge_size=2,
    )


def _dense_page(blobs: _Blobs, *, characters: int = 12_000, acts: int = 20, lines: int = 120):
    """Twenty acts of register French, ~12,000 characters per witness, 120 Surya lines."""
    words = "le vingt deux mai mil sept cent quatre vingt a été baptisé par nous prêtre soussigné".split()
    act_text = []
    per_act = characters // acts
    for index in range(acts):
        text = " ".join(words[(index + step) % len(words)] for step in range(per_act // 5))
        act_text.append(text[:per_act])
    band = 3000 // acts
    chandra = chandra_testimonium(
        blobs,
        [
            (f"100 {50 + index * 45} 900 {90 + index * 45}", "Text", text)
            for index, text in enumerate(act_text)
        ],
    )
    dai = dai_testimonium(
        blobs,
        [
            ({"x": 255, "y": 150 + index * band, "w": 2040, "h": band - 10}, text)
            for index, text in enumerate(act_text)
        ],
    )
    xml_lines = "".join(
        f"<Line>{text[start : start + 60]}</Line>"
        for text in act_text
        for start in range(0, len(text), 60)
    )
    churro = churro_testimonium(
        blobs, f"<HistoricalDocument><Page><Body>{xml_lines}</Body></Page></HistoricalDocument>"
    )
    rows = [
        {
            "chair": chair,
            "witness_label": chair,
            "adapter": adapter,
            "testimonium": record,
            "testimonium_ref": _ref(chair),
        }
        for chair, adapter, record in (
            ("attestator_1", "chandra.v1", chandra),
            ("attestator_2", "dai.v1", dai),
            ("attestator_3", "churro.v1", churro),
        )
    ]
    return rows, surya(lines=lines, blocks=acts)


@pytest.mark.parametrize(
    ("change", "fits_32k"),
    [
        ({}, True),
        ({"surya_lines": False, "surya_blocks": False}, True),
        ({"witness_units": "flat"}, True),
        # The overlay's second image costs another 4,960 tokens: 36,760 needed.
        ({"page_overlay": "boxes"}, False),
    ],
    ids=["default", "no-surya", "flat", "overlay"],
)
def test_a_dense_page_fits_the_served_row_or_refuses_with_its_record(change, fits_32k):
    blobs = _Blobs()
    rows, census = _dense_page(blobs)
    render = _retained_render(blobs)
    feed = feed_for(blobs, rows=rows, census=census, render=render, **change)
    text = page_prompt.build_page_prompt("unproven-real-perlector", feed)
    arguments = dict(
        image_sizes=page_feed.request_image_sizes(feed),
        prompt_text=text,
        template_digest=page_prompt.BUILDER_SHA256,
        answer_measure=feed["answer_measure"],
        page_max_tokens=12288,
    )
    if fits_32k:
        admitted = page_request_capacity(_perlector_row(), **arguments)
        record = admitted["capacity"]
        assert record["fits"] and record["image_prompt_tokens"] == 4960
        assert admitted["max_tokens"] == min(
            12288, 32768 - record["image_prompt_tokens"] - record["prompt_tokens"]
        )
    else:
        with pytest.raises(RequestCapacityRefusal) as refusal:
            page_request_capacity(_perlector_row(), **arguments)
        assert refusal.value.capacity["fits"] is False
    assert page_request_capacity(_perlector_row(65536), **arguments)["capacity"]["fits"]


# --- the page overlay -----------------------------------------------------------------


def _retained_render(blobs: _Blobs) -> dict:
    """A real 1978x2560 render in the byte store, so an overlay can be drawn on it."""
    image = Image.new("RGB", (1978, 2560), "#f4ecd8")
    ImageDraw.Draw(image).line([(200, 300), (1700, 300)], fill="#202020", width=4)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    ref = blobs.retain(buffer.getvalue())
    return {**RENDER, "image_path": ref["relative_path"], "image_sha256": ref["sha256"]}


def _overlaid(blobs: _Blobs, **changes):
    render = _retained_render(blobs)
    feed = feed_for(blobs, render=render, page_overlay="boxes", **changes)
    return feed, page_overlay.overlay_image(feed, blobs.read_bytes)


def test_the_default_feed_draws_no_overlay_and_sends_one_image():
    feed = feed_for(_Blobs())
    assert feed["overlay"] is None
    assert page_feed.request_image_sizes(feed) == [(1978, 2560)]
    assert "second image" not in page_prompt.build_page_prompt("unproven-real-perlector", feed)


def test_the_overlay_is_a_second_image_labelling_every_shown_boxed_id():
    blobs = _Blobs()
    feed, png = _overlaid(blobs)
    overlay = feed["overlay"]
    assert [item["id"] for item in overlay["drawn"]] == [
        "A1",
        "A2",
        "B1",
        "B2",
        "L1",
        "L2",
        "L3",
        "S1",
        "S2",
    ]
    # The unit with no box and Churro's unboxed lines are not drawn.
    assert overlay["colours"] == {
        "surya-block": page_overlay.SURYA_BLOCK_COLOUR,
        "surya-line": page_overlay.SURYA_LINE_COLOUR,
        "witness-A": page_overlay.WITNESS_COLOURS[0],
        "witness-B": page_overlay.WITNESS_COLOURS[1],
    }
    assert overlay["image_sha256"] == digest_bytes(png)
    assert overlay["renderer_sha256"] == page_overlay.RENDERER_SHA256
    # A copy: the clean render is still the first image, unchanged.
    assert feed["page_render"]["image_sha256"] == overlay["source_image_sha256"]
    assert digest_bytes(png) != overlay["source_image_sha256"]
    assert page_feed.request_image_sizes(feed) == [(1978, 2560), (1978, 2560)]
    assert "second image" in page_prompt.build_page_prompt("unproven-real-perlector", feed)
    # A1 is Chandra's [100,50,900,120] block; its outline's corner is the witness colour.
    x0, y0, _x1, _y1 = overlay["drawn"][0]["box"]
    assert (x0, y0) == (198, 128)
    image = Image.open(io.BytesIO(png)).convert("RGB")
    assert image.getpixel((x0 + 40, y0 + 1)) != (0xF4, 0xEC, 0xD8)
    colour = ImageColor.getrgb(page_overlay.WITNESS_COLOURS[0])
    assert image.getpixel((x0 + 400, y0)) == colour


def test_the_overlay_is_byte_identical_for_identical_inputs():
    first_feed, first = _overlaid(_Blobs())
    second_feed, second = _overlaid(_Blobs())
    assert first == second and first_feed == second_feed


@pytest.mark.parametrize(
    ("change", "absent"),
    [
        ({"witness_coordinates": False}, ("A", "B")),
        ({"surya_lines": False}, ("L",)),
        ({"surya_blocks": False}, ("S",)),
        ({"witnesses": ["attestator_3"]}, ("A", "B")),
    ],
)
def test_a_switched_off_source_is_not_drawn(change, absent):
    feed, _png = _overlaid(_Blobs(), **change)
    drawn = [item["id"] for item in feed["overlay"]["drawn"]]
    assert drawn and not [item for item in drawn if item.startswith(absent)]


def test_an_overlay_whose_render_bytes_changed_is_refused():
    blobs = _Blobs()
    feed, _png = _overlaid(blobs)
    blobs.files[feed["page_render"]["image_path"]] = b"not the render"
    with pytest.raises(SchemaRefusal, match="bytes changed"):
        page_overlay.overlay_image(feed, blobs.read_bytes)
    with pytest.raises(SchemaRefusal, match="render's bytes were not given"):
        page_feed.assemble_page_feed(
            page_id="page-1",
            page_ordinal=1,
            page_size=PAGE,
            feed_switches=switches(page_overlay="boxes"),
            witness_regime="named",
            witnesses=[],
            surya=surya(),
            page_render=RENDER,
            serving_recipe="unproven-real-perlector",
        )
