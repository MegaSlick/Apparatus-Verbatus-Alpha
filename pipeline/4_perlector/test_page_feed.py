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
ROSTER = ["attestator_1", "attestator_2", "attestator_3"]
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


def _record(outcome: str, payload: dict) -> dict:
    return {
        "kind": "page-testimonium",
        "subject_id": "page-1",
        "outcome": outcome,
        "payload": payload,
    }


def _sealed(blobs: _Blobs, rows: list[dict]) -> list[dict]:
    """Each row's testimonium written to the byte store, its ref naming those bytes."""
    return [
        {**row, "testimonium_ref": blobs.retain(json.dumps(row["testimonium"]).encode())}
        for row in rows
    ]


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
    return _record(outcome, {"native_capture": _capture("chandra.v1", ref, "html", page_text)})


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
    return _record(outcome, {"native_capture": capture})


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
    return _record(outcome, {"payload": joined, "unit_captures": captures, "observed": observed})


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
    roster=None,
    **changes,
):
    rows = _sealed(blobs, rows if rows is not None else witnesses(blobs, regime=regime))
    return page_feed.build_page_feed(
        page_id="page-1",
        page_ordinal=1,
        page_size=PAGE,
        feed_switches=switches(**changes),
        witness_regime=regime,
        roster=roster if roster is not None else [row["chair"] for row in rows],
        witnesses=rows,
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
    rows[1]["testimonium"] = _record("failed", {})
    feed = feed_for(blobs, rows=rows)
    assert feed["witnesses"][1]["outcome"] == "failed"
    assert feed["witnesses"][1]["units"] == []
    text = page_prompt.build_page_prompt("unproven-real-perlector", feed)
    assert "witness B (attestator_2): failed, no units" in text


def test_a_page_testimonium_with_no_native_capture_is_refused_by_name():
    blobs = _Blobs()
    rows = witnesses(blobs)
    rows[0]["testimonium"] = _record("read", {"payload": "joined text"})
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


def test_flat_units_keep_every_unit_on_the_feed_and_render_one_line_per_witness():
    base, flat, _base_text, text = _changed(witness_units="flat")
    for row, own in zip(flat["witnesses"], base["witnesses"], strict=True):
        # The accounting reads the same units, ids, sealed boxes and unit kind.
        assert row["unit_kind"] == own["unit_kind"]
        assert [(u["id"], u["box_px"], u["text"]) for u in row["units"]] == [
            (u["id"], u["box_px"], u["text"]) for u in own["units"]
        ]
        assert all(unit["box_1000"] is None for unit in row["units"])
        first, last = row["units"][0]["id"], row["units"][-1]["id"]
        joined = json.dumps("\n".join(u["text"] for u in own["units"] if u["text"]))
        assert f"\n{first}-{last} {json.loads(joined)!r}" not in text
        assert f"\n{first}-{last} " + json.dumps(json.loads(joined), ensure_ascii=False) in text
    assert "\nA2 " not in text and "\nA1-A3 " in text
    assert "box_1000" not in text.split("surya lines")[0]
    # How the units are shown does not move the answer reserve.
    assert flat["answer_measure"] == base["answer_measure"]


def test_coordinates_off_hides_every_witness_box_but_keeps_the_sealed_geometry():
    base, other, base_text, text = _changed(witness_coordinates=False)
    assert 'A1 [100,50,900,120] ("Page-Header")' in base_text
    assert 'A1 ("Page-Header")' in text
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
        roster=ROSTER,
        witnesses=_sealed(blobs, witnesses(blobs)),
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
            roster=ROSTER,
            witnesses=_sealed(blobs, witnesses(blobs)),
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
    assert 'A2 [100,50,900,120] ("Text") "Le vingt mai a été baptisé Pierre"' not in lines
    assert 'A2 [100,150,900,400] ("Text") "Le vingt mai a été baptisé Pierre"' in lines
    assert 'C1 ("Header") "Registre 1780"' in lines
    assert 'S1 [100,500,900,952] ("Text")' in lines
    assert lines[-1] == page_prompt.page_reading_instruction(feed_for(_Blobs()))


def test_the_fixture_recipe_renders_the_same_inputs_without_the_instruction():
    blobs = _Blobs()
    feed = feed_for(blobs, recipe="fake-perlector-v0")
    real = page_prompt.build_page_prompt("unproven-real-perlector", feed)
    assert real == page_prompt.build_page_prompt("fake-perlector-v0", feed) + "\n" + (
        page_prompt.page_reading_instruction(feed)
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
    for feed in (feed_for(_Blobs()), feed_for(_Blobs(), render=None, page_image="off")):
        instruction = page_prompt.page_reading_instruction(feed)
        for sentence in marks:
            assert sentence in prompts.TRANSCRIPTION_INSTRUCTION
            assert sentence in instruction
        example = instruction.split("in this form: ", 1)[1]
        assert set(json.loads(example)) == {"acts", "set_aside"}


def test_the_prompt_evidence_names_the_builder_the_instruction_and_the_bytes():
    blobs = _Blobs()
    feed = feed_for(blobs)
    text = page_prompt.build_page_prompt("unproven-real-perlector", feed)
    assert feed["prompt"] == {
        "serving_recipe": "unproven-real-perlector",
        "builder_sha256": page_prompt.BUILDER_SHA256,
        "rendered_sha256": digest_bytes(text.encode()),
        "instruction_sha256": digest_bytes(page_prompt.page_reading_instruction(feed).encode()),
    }


def test_the_page_builder_digest_is_pinned_where_its_carried_rate_is_sealed():
    source = (Path(page_prompt.__file__)).read_text(encoding="utf-8")
    assert code_digest(source) == page_prompt.BUILDER_SHA256
    assert PERLECTOR_PAGE_PROMPT_TEMPLATE_DIGEST == page_prompt.BUILDER_SHA256
    edited = source.replace("Read this page from its ink", "Read this page", 1)
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
        # 4,960 image + 22,759 prompt (4,418 bytes of id, box and coordinate rows at one
        # token each) + 6,903 answer = 34,622: over 32k, inside 65k.
        ({}, False),
        # Without Surya's 140 rows: 31,526.
        ({"surya_lines": False, "surya_blocks": False}, True),
        # One line per witness, Surya still shown: 32,053.
        ({"witness_units": "flat"}, True),
        # The overlay's second image costs another 4,960 tokens: 39,648.
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
            roster=[],
            witnesses=[],
            surya=surya(),
            page_render=RENDER,
            serving_recipe="unproven-real-perlector",
        )


# --- nothing a witness said is absent -------------------------------------------------


def test_chandra_text_outside_its_blocks_is_a_unit_of_its_own_with_the_finding():
    blobs = _Blobs()
    rows = witnesses(blobs)
    html = (
        b'<div data-bbox="100 50 900 120" data-label="Text"><p>Registre</p></div>\n'
        b"  en marge   : 12  \n<p>signature</p>"
    )
    from common.chandra_layout import parse_layout_html

    capture = _capture(
        "chandra.v1", blobs.retain(html), "html", parse_layout_html(html)["page_text"]
    )
    rows[0]["testimonium"] = _record("read", {"native_capture": capture})
    feed = feed_for(blobs, rows=rows)
    chandra = feed["witnesses"][0]
    assert chandra["units"][-1] == {
        "id": "A2",
        "ordinal": None,
        "box_px": None,
        "box_1000": None,
        "label": page_feed.OUTSIDE_UNITS_LABEL,
        "text": "en marge : 12\nsignature",
    }
    assert "content-outside-blocks" in [finding["kind"] for finding in chandra["findings"]]
    text = page_prompt.build_page_prompt("unproven-real-perlector", feed)
    assert 'A2 ("outside units") "en marge : 12\\nsignature"' in text.split("\n")
    # Counted in the witness's text, not as an act-sized block.
    assert sum(len(unit["text"]) for unit in chandra["units"]) == len("Registre") + len(
        "en marge : 12\nsignature"
    )
    assert page_feed.answer_measure([("chandra.v1", chandra["units"])], surya_blocks=0) == {
        "longest_witness_characters": 31,
        "act_entries": 1,
    }
    assert "findings" not in text


def test_churro_page_text_outside_its_sections_is_a_unit_of_its_own_with_the_finding():
    blobs = _Blobs()
    rows = witnesses(blobs)
    xml = CHURRO_XML.replace("<Page>", "<Page>Folio  3 ").replace(
        "</Body>", "</Body><Note>en marge</Note>"
    )
    rows[2]["testimonium"] = churro_testimonium(blobs, xml)
    feed = feed_for(blobs, rows=rows)
    churro = feed["witnesses"][2]
    assert [(unit["label"], unit["text"]) for unit in churro["units"]][-2:] == [
        ("Footer", "12"),
        (page_feed.OUTSIDE_UNITS_LABEL, "Folio 3\nen marge"),
    ]
    assert churro["units"][-1]["ordinal"] is None
    assert "page-text-outside-sections" in [finding["kind"] for finding in churro["findings"]]


def test_a_witness_with_nothing_outside_its_units_has_no_extra_unit():
    feed = feed_for(_Blobs())
    for row in feed["witnesses"]:
        assert all(unit["label"] != page_feed.OUTSIDE_UNITS_LABEL for unit in row["units"])
    assert feed["witnesses"][1]["findings"] == []


# --- provenance -------------------------------------------------------------------------


def test_a_testimonium_that_is_not_the_record_its_ref_holds_is_refused():
    blobs = _Blobs()
    rows = _sealed(blobs, witnesses(blobs))
    rows[1]["testimonium"] = {**rows[1]["testimonium"], "outcome": "failed", "payload": {}}
    with pytest.raises(SchemaRefusal, match="not the record its reference"):
        page_feed.build_page_feed(
            page_id="page-1",
            page_ordinal=1,
            page_size=PAGE,
            feed_switches=switches(),
            witness_regime="named",
            roster=ROSTER,
            witnesses=rows,
            surya=surya(),
            page_render=RENDER,
            serving_recipe="unproven-real-perlector",
            read_bytes=blobs.read_bytes,
        )


def test_a_testimonium_of_another_page_or_with_no_outcome_is_refused_by_name():
    blobs = _Blobs()
    rows = witnesses(blobs)
    rows[0]["testimonium"]["subject_id"] = "page-2"
    with pytest.raises(SchemaRefusal, match="page-testimonium of page 'page-1'"):
        feed_for(blobs, rows=rows)
    rows = witnesses(blobs)
    del rows[2]["testimonium"]["outcome"]
    with pytest.raises(SchemaRefusal, match="records no outcome"):
        feed_for(blobs, rows=rows)
    with pytest.raises(SchemaRefusal, match="records no outcome"):
        page_feed.witness_reading(
            {"payload": {}}, adapter="dai.v1", page_size=PAGE, read_bytes=None
        )


def test_every_roster_chair_needs_a_row_and_every_row_a_roster_chair():
    blobs = _Blobs()
    with pytest.raises(SchemaRefusal, match=r"\['attestator_4'\] of the sealed roster have no row"):
        feed_for(blobs, roster=[*ROSTER, "attestator_4"])
    with pytest.raises(SchemaRefusal, match="not in the sealed page-witness roster"):
        feed_for(blobs, roster=ROSTER[:2])
    # A roster chair the switch hides still needs its row.
    with pytest.raises(SchemaRefusal, match="have no row"):
        feed_for(blobs, rows=witnesses(blobs)[:2], roster=ROSTER, witnesses=["attestator_1"])


def test_a_dai_record_with_no_integer_ordinal_is_refused():
    blobs = _Blobs()
    rows = witnesses(blobs)
    rows[1]["testimonium"]["payload"]["observed"][0]["ordinal"] = "0"
    with pytest.raises(SchemaRefusal, match="no integer ordinal"):
        feed_for(blobs, rows=rows)


# --- the answer reserve counts acts, not lines ------------------------------------------


def test_a_200_line_churro_only_page_is_reserved_on_its_likely_acts_under_the_cap():
    blobs = _Blobs()
    lines = "".join(
        f"<Line>le vingt deux mai a été baptisé Pierre fils de {index}</Line>"
        for index in range(200)
    )
    churro = churro_testimonium(
        blobs, f"<HistoricalDocument><Page><Body>{lines}</Body></Page></HistoricalDocument>"
    )
    row = {
        "chair": "attestator_3",
        "witness_label": "attestator_3",
        "adapter": "churro.v1",
        "testimonium": churro,
        "testimonium_ref": _ref("churro"),
    }
    feed = feed_for(blobs, rows=[row], census=surya(lines=200, blocks=20))
    assert len(feed["witnesses"][0]["units"]) == 200
    # Surya's twenty blocks, not Churro's two hundred lines.
    assert feed["answer_measure"]["act_entries"] == 20
    admitted = page_request_capacity(
        _perlector_row(65536),
        image_sizes=page_feed.request_image_sizes(feed),
        prompt_text=page_prompt.build_page_prompt("unproven-real-perlector", feed),
        template_digest=page_prompt.BUILDER_SHA256,
        answer_measure=feed["answer_measure"],
        page_max_tokens=12288,
    )
    assert admitted["answer_reserve"]["tokens"] < 12288


def test_the_act_count_is_the_most_of_surya_blocks_dai_records_and_chandra_blocks():
    units = [{"ordinal": index, "text": "x"} for index in range(4)]
    outside = [{"ordinal": None, "text": "y"}]
    rows = [("chandra.v1", units + outside), ("dai.v1", units[:2]), ("churro.v1", units * 50)]
    assert page_feed.answer_measure(rows, surya_blocks=3) == {
        "longest_witness_characters": 200,
        "act_entries": 4,
    }
    assert page_feed.answer_measure(rows, surya_blocks=9)["act_entries"] == 9
    assert page_feed.answer_measure([("churro.v1", units)], surya_blocks=0)["act_entries"] == 0


# --- the prompt says what is shown and nothing else ---------------------------------------


def test_the_instruction_names_only_the_inputs_the_feed_shows():
    default = page_prompt.page_reading_instruction(feed_for(_Blobs()))
    assert "Read this page from its ink in the page image." in default
    assert "the witness units, the detected lines and the detected blocks above" in default.lower()
    no_image = page_prompt.page_reading_instruction(
        feed_for(_Blobs(), render=None, page_image="off")
    )
    assert "No page image is shown" in no_image
    assert "page image shows" not in no_image and "in the page image" not in no_image
    no_surya = page_prompt.page_reading_instruction(
        feed_for(_Blobs(), surya_lines=False, surya_blocks=False)
    )
    assert "detected" not in no_surya and "every unit its ink covers" in no_surya
    flat = page_prompt.page_reading_instruction(feed_for(_Blobs(), witness_units="flat"))
    assert "cited by the range of ids before its text" in flat
    assert "range of ids" not in default


def test_non_act_text_is_read_as_other_and_set_aside_is_for_ink_not_read():
    instruction = page_prompt.page_reading_instruction(feed_for(_Blobs()))
    assert "a page number or a marginal note that is not an entry, is read too" in instruction
    assert 'kind "other"' in instruction
    assert "Set aside only an id whose ink you do not read at all" in instruction
    assert "printed page number" not in instruction
    example = json.loads(instruction.split("in this form: ", 1)[1])
    assert [act["kind"] for act in example["acts"]] == ["act", "other"]
    assert example["acts"][1]["label"] == "page number"
    assert example["set_aside"] == [{"id": "L19", "reason": "not text"}]


def test_labels_are_json_strings_so_a_newline_or_parenthesis_cannot_forge_a_row():
    blobs = _Blobs()
    census = surya()
    label = 'Text")\nS9 [0,0,1000,1000] ("Text'
    census["blocks"][0]["label"] = label
    feed = feed_for(blobs, census=census)
    text = page_prompt.build_page_prompt("unproven-real-perlector", feed)
    assert "\nS9 " not in text
    (row,) = [line for line in text.split("\n") if line.startswith("S2 ")]
    assert row == f"S2 [100,45,900,497] ({json.dumps(label)})"


def test_the_unit_kind_and_findings_are_never_rendered():
    blobs = _Blobs()
    feed = feed_for(blobs)
    text = page_prompt.build_page_prompt("unproven-real-perlector", feed)
    assert "layout-block" not in text and "detector-record" not in text
    changed = copy.deepcopy(feed)
    for row in changed["witnesses"]:
        row["unit_kind"] = "detector-record"
        row["findings"] = [{"kind": "anything"}]
    assert page_prompt.build_page_prompt("unproven-real-perlector", changed) == text


def test_a_full_page_box_is_the_whole_grid():
    assert page_feed.box_1000({"x": 0, "y": 0, "w": 2550, "h": 3300}, PAGE) == [0, 0, 1000, 1000]
    assert page_feed.box_1000({"x": 2549, "y": 3299, "w": 1, "h": 1}, PAGE) == [
        1000,
        1000,
        1000,
        1000,
    ]


# --- overlay determinism, colours and label placement -----------------------------------


def test_every_overlay_colour_is_distinct_and_a_letter_past_them_is_refused():
    colours = [
        *page_overlay.WITNESS_COLOURS,
        page_overlay.SURYA_LINE_COLOUR,
        page_overlay.SURYA_BLOCK_COLOUR,
        page_overlay.LABEL_TEXT_COLOUR,
    ]
    assert len({ImageColor.getrgb(colour) for colour in colours}) == len(colours)
    letter = chr(ord("A") + len(page_overlay.WITNESS_COLOURS))
    with pytest.raises(SchemaRefusal, match="would repeat one"):
        page_overlay._source_colour(f"witness-{letter}")


def test_every_id_character_has_a_5x7_glyph():
    assert set(page_overlay.GLYPHS) == set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    for glyph in page_overlay.GLYPHS.values():
        assert len(glyph) == 7 and all(len(row) == 5 and set(row) <= {".", "#"} for row in glyph)
    assert len({glyph for glyph in page_overlay.GLYPHS.values()}) == len(page_overlay.GLYPHS)


def test_labels_of_witness_units_and_surya_detections_on_one_box_do_not_overlap():
    box = [400, 400, 520, 440]
    plan = {
        "dimensions": {"w": 1978, "h": 2560},
        "label_scale": 3,
        "drawn": [
            {"id": "A12", "source": "witness-A", "box": box},
            {"id": "B7", "source": "witness-B", "box": box},
            {"id": "L104", "source": "surya-line", "box": box},
            {"id": "S9", "source": "surya-block", "box": box},
        ],
    }
    placed = page_overlay.label_boxes(plan)
    for index, first in enumerate(placed):
        for second in placed[index + 1 :]:
            assert not page_overlay._overlaps(first, second)
    # The first sits at the box's top-left corner.
    assert placed[0][:2] == (400, 400)


def test_the_overlay_is_written_by_the_project_encoder():
    from common.imaging import encode_image_deterministic

    _feed, png = _overlaid(_Blobs())
    with Image.open(io.BytesIO(png)) as image:
        assert encode_image_deterministic(image.convert("RGB")) == png


def test_the_overlay_is_byte_identical_when_redrawn_in_a_fresh_process(tmp_path):
    import subprocess
    import sys

    blobs = _Blobs()
    feed, png = _overlaid(blobs)
    render = blobs.read_bytes(feed["page_render"]["image_path"])
    (tmp_path / "render.png").write_bytes(render)
    plan = {
        key: value for key, value in feed["overlay"].items() if key not in page_overlay._RECORD_ONLY
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    script = (
        "import json, sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(ROOT)!r}); sys.path.insert(0, {str(Path(page_overlay.__file__).parent)!r}); "
        "import page_overlay; from common.contracts.canonical import digest_bytes; "
        f"d = Path({str(tmp_path)!r}); "
        "print(digest_bytes(page_overlay.draw_page_overlay((d / 'render.png').read_bytes(), "
        "json.loads((d / 'plan.json').read_text()))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=tmp_path
    )
    assert result.stdout.strip() == feed["overlay"]["image_sha256"] == digest_bytes(png)
