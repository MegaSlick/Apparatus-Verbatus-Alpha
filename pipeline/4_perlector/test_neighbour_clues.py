"""Neighbour clues: the acts before and after the one being read, as witness clues only.

The reader is shown every witness's reading of the neighbouring acts so it knows
where its own act starts and stops, and is told to transcribe only its own ink.
These tests pin that the clues stay clues: they reach the prompt and the dossier
and nothing that establishes, compares or audits this act's text; unprimed arms
see none; and an act's dossier does not depend on any other act's reading.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal
from common.contracts.stages import PERLECTOR
from common.runtree.store import RunTree
from conftest import load_stage, programs_through, run_stage, stage_artifacts

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "config" / "perlector_protocol.toml"

perlector = load_stage("4_perlector")
dossier_module = perlector.dossier_module
prompts = perlector.prompts
protocol = perlector.protocol
combined = perlector.combined
annotations = perlector.annotations

# One witness's reading of act a2 in the fixture, misspelt so it appears nowhere
# else in the run: attestator_3 reports "epsiIon".
SENTINEL = "epsiIon"


def _run(root: Path, *args: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-id",
            "r",
            "--run-root",
            str(root),
            *args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="module")
def fed_run(tmp_path_factory):
    """One whole fixture run with a fed Pass A, so every arm and Pass C exist."""
    root = tmp_path_factory.mktemp("neighbours") / "runs"
    _run(root, "--blind-read", "fed")
    return RunTree(root, "r")


def _by_act(tree: RunTree, kind: str) -> dict[str, dict]:
    return {
        record["payload"]["act_key"]: record for record in stage_artifacts(tree, PERLECTOR, kind)
    }


def _sealed_protocol() -> dict:
    return protocol.load(PROTOCOL)[0]


# --- the cap and the page break, without a run ---------------------------------


def test_each_neighbour_reading_is_cut_to_the_sealed_cap_at_the_edge_that_touches_this_act():
    act = {"act_id": "act_x", "act_key": "x"}
    witnesses = [
        {
            "witness_label": "w1",
            "outcome": "read",
            "reported": "abcdefgh",
            "reported_basis": "own-report",
            "testimonium_ref": {},
        },
        {
            "witness_label": "w2",
            "outcome": "read",
            "reported": "abc",
            "reported_basis": "own-report",
            "testimonium_ref": {},
        },
        {
            "witness_label": "w3",
            "outcome": "failed",
            "reported": None,
            "reported_basis": "none",
            "testimonium_ref": {},
        },
    ]
    before = dossier_module.neighbour_entry(
        act, side="preceding", same_page=True, witnesses=witnesses, characters_per_row=3
    )
    after = dossier_module.neighbour_entry(
        act, side="following", same_page=False, witnesses=witnesses, characters_per_row=3
    )
    assert [(row["reported"], row["shown"]) for row in before["witnesses"]] == [
        ("fgh", "tail"),
        ("abc", "whole"),
        (None, None),
    ]
    assert [(row["reported"], row["shown"]) for row in after["witnesses"]] == [
        ("abc", "head"),
        ("abc", "whole"),
        (None, None),
    ]
    # Every witness is carried, never one chair's reading alone.
    assert [row["witness_label"] for row in after["witnesses"]] == ["w1", "w2", "w3"]
    dossier_module.assert_no_order_bearing_field({"neighbours": {"preceding": before}})


def test_neighbours_cross_page_breaks_and_are_null_at_either_end():
    """The Designator's act sequence decides the neighbours, whatever page they sit on."""
    expected = [
        {"act_id": "a1", "act_key": "k1", "page_id": "p1"},
        {"act_id": "a2", "act_key": "k2", "page_id": "p1"},
        {"act_id": "a3", "act_key": "k3", "page_id": "p2"},
    ]
    run = SimpleNamespace(
        expected=expected,
        declared_order={act["act_id"]: index for index, act in enumerate(expected)},
        protocol_config=_sealed_protocol(),
        # a3 continues onto p3; a2 sits on p1 alone.
        neighbour_clues={
            "a1": ({"p1"}, [], None),
            "a2": ({"p1"}, [], None),
            "a3": ({"p2", "p3"}, [], None),
        },
    )
    first = perlector._neighbours(run, expected[0], pages={"p1"})
    last = perlector._neighbours(run, expected[2], pages={"p2", "p3"})
    middle = perlector._neighbours(run, expected[1], pages={"p1"})
    assert first["preceding"] is None and first["following"]["act_key"] == "k2"
    assert last["following"] is None and last["preceding"]["act_key"] == "k2"
    assert last["preceding"]["same_page"] is False
    assert middle["preceding"]["same_page"] is True
    assert middle["following"]["same_page"] is False
    # A continuation act shares the page it continues onto with the act after it.
    assert perlector._neighbours(run, expected[1], pages={"p1", "p2"})["following"]["same_page"]


def test_the_cap_and_the_page_render_bound_are_sealed_and_the_fragment_is_pinned(tmp_path):
    sealed = _sealed_protocol()
    assert sealed["neighbours"]["characters_per_row"] == 800
    assert sealed["neighbours"]["fragment"] == protocol.NEIGHBOUR_FRAGMENT
    assert sealed["page_context"] == {"maximum_edge": 2560, "covered_page_edge": 1024}
    shipped = PROTOCOL.read_text(encoding="utf-8")
    for edited, refusal in (
        (shipped.replace("characters_per_row = 800", "characters_per_row = 0"), "neighbours"),
        (shipped.replace("never copy a neighbour's text", "copy a neighbour's text"), "fragment"),
        (shipped.replace("maximum_edge = 2560", "maximum_edge = 0"), "page_context"),
    ):
        assert edited != shipped
        path = tmp_path / "protocol.toml"
        path.write_text(edited, encoding="utf-8")
        with pytest.raises(ContractError, match=refusal):
            protocol.load(path)


def test_an_unprimed_arm_is_shown_no_neighbour_reading():
    primed = {"testimonia": [{"reported": "x"}], "neighbours": {"preceding": None}, "regions": []}
    assert "neighbours" not in combined._unprimed(primed)


@pytest.mark.parametrize(
    "raw",
    [
        "le vingt deux du mois [[?]]",
        "le vingt deux du mois [[?]]\n",
        "le vingt deux du mois [[?]].",
        "le vingt deux du mois [[?]] ;»\n",
    ],
)
def test_ink_past_the_crop_marked_at_the_end_is_a_trailing_gap(raw):
    """What the instruction asks for: a zero-width gap at the end, visible, carrying no
    text, even when the reader closes the line or the sentence after the mark."""
    text, assessment = annotations.read_doubt_marks(raw)
    gaps = [{"position": "trailing", "start": 22, "end": 22, "witness_evidence": []}]
    assert assessment["gaps"] == gaps
    annotations.validate_annotations(
        {"text": text, "uncertain_spans": [], "gaps": gaps}, outcome="read"
    )


def test_a_mark_followed_by_more_words_stays_internal():
    _text, assessment = annotations.read_doubt_marks("le vingt [[?]] du mois")
    assert assessment["gaps"][0]["position"] == "internal"


def test_every_arm_is_told_to_stop_at_the_crop_edge_and_only_primed_arms_get_clues():
    """The crop-edge rule lives in the transcription instruction every arm is sent; the
    neighbour fragment speaks only about the clues."""
    assert "stop at the edge and write [[?]]" in prompts.TRANSCRIPTION_INSTRUCTION
    assert "[[?]]" not in protocol.NEIGHBOUR_FRAGMENT
    unprimed = {"witness_regime": "named", "act_key": "a1", "testimonia": []}
    rendered = prompts.build_prompt("unproven-real-perlector", "perlector", unprimed, None)
    assert prompts.TRANSCRIPTION_INSTRUCTION in rendered
    assert "neighbouring_acts" not in rendered


def test_a_witness_with_no_reading_and_a_withheld_clue_render_as_words():
    witness = {"witness_label": "w1", "reported": None, "shown": None}
    preceding = {"act_key": "a0", "same_page": True, "witnesses": [witness], "unavailable": None}
    following = {
        "act_key": "a2",
        "same_page": False,
        "witnesses": [],
        "unavailable": "the Designator held this act; no witness read it",
    }
    dossier = {"neighbours": {"preceding": preceding, "following": following}}
    block = prompts.neighbour_block(dossier, _sealed_protocol())
    assert "    - w1: no reading" in block and "None" not in block
    assert "  following: a2 (another page)\n    witness readings unavailable" in block


def test_a_neighbours_defective_witness_records_withhold_its_clue_and_say_why(monkeypatch):
    """The act beside it is still read; the clue is withheld with the reason, not dropped."""
    act = {"act_id": "a2", "act_key": "k2", "page_id": "p1", "outcome": "proposed"}
    run = SimpleNamespace(neighbour_clues={}, context=None)

    def broken(context, act_id):
        raise SchemaRefusal("a tampered region")

    monkeypatch.setattr(perlector, "act_regions", broken)
    pages, witnesses, unavailable = perlector._neighbour_clue(run, act)
    assert (pages, witnesses) == ({"p1"}, [])
    assert "SchemaRefusal: a tampered region" in unavailable
    held = {**act, "act_id": "a3", "outcome": "held"}
    assert perlector._neighbour_clue(run, held)[2].startswith("the Designator held")


def test_neighbour_clues_are_refused_where_the_act_has_no_witnesses():
    with pytest.raises(SchemaRefusal, match="no testimonia"):
        dossier_module.build_dossier(
            SimpleNamespace(witness_context_config_path="x"),
            act_id="a1",
            act_key="k1",
            regions=[],
            testimonia=[],
            regime="named",
            page_renders=[],
            witness_context={},
            neighbours={"preceding": None, "following": None},
        )


# --- a whole run: clues reach the prompt and nothing else -----------------------


@pytest.mark.act_path
def test_the_first_and_last_acts_have_one_neighbour_each_with_every_witness(fed_run):
    readings = _by_act(fed_run, "perlectio")
    first = readings["a1"]["payload"]["dossier"]["neighbours"]
    last = readings["a2"]["payload"]["dossier"]["neighbours"]
    assert first["preceding"] is None and last["following"] is None
    assert first["following"]["act_key"] == "a2" and last["preceding"]["act_key"] == "a1"
    assert first["following"]["same_page"] is True
    labels = {row["witness_label"] for row in readings["a1"]["payload"]["dossier"]["testimonia"]}
    assert {row["witness_label"] for row in first["following"]["witnesses"]} == labels
    assert len(labels) == 3


@pytest.mark.act_path
def test_a_neighbours_reading_reaches_only_the_neighbour_clues(fed_run):
    """The sentinel is a2's witness text; a1's record carries it only under `neighbours`."""
    reading = _by_act(fed_run, "perlectio")["a1"]
    payload = reading["payload"]
    assert SENTINEL in json.dumps(payload["dossier"]["neighbours"])
    stripped = json.loads(json.dumps(payload))
    del stripped["dossier"]["neighbours"]
    assert SENTINEL not in json.dumps(stripped)
    for field in ("basis", "dissent", "gaps", "text"):
        assert SENTINEL not in json.dumps(payload[field])
    neighbour_refs = [
        row["testimonium_ref"] for row in payload["dossier"]["neighbours"]["following"]["witnesses"]
    ]
    # Lineage: the reading's inputs name what it was shown, and the dossier's
    # neighbour rows name them as clues, never as its witness basis.
    assert neighbour_refs and all(ref in reading["inputs"] for ref in neighbour_refs)
    assert neighbour_refs == perlector.neighbour_testimonium_refs(payload["dossier"])
    assert not [row for row in payload["basis"]["testimonia"] if row["reference"] in neighbour_refs]
    # No consumer takes them as a1's evidence: its review and established record
    # name none of them.
    for stage, kind in (("recensor", "review"), ("archetypus", "archetypus")):
        records = stage_artifacts(fed_run, stage, kind, reading["subject_id"])
        assert records
        for ref in neighbour_refs:
            assert ref["relative_path"] not in json.dumps(records)
    # Pass C's frozen inputs for a1 never see it either.
    for kind in ("audit-draft", "audit-finding"):
        record = _by_act(fed_run, kind)["a1"]
        assert SENTINEL not in json.dumps(record["payload"])
        assert not [ref for ref in neighbour_refs if ref in record["inputs"]]
    # In the prompt it sits inside the neighbour block and nowhere else.
    config = _sealed_protocol()
    rendered = prompts.build_prompt(
        "unproven-real-perlector", "perlector", payload["dossier"], config
    )
    block = prompts.neighbour_block(payload["dossier"], config)
    assert SENTINEL in block and SENTINEL not in rendered.replace(block, "")
    assert rendered.index("testimonia:") < rendered.index(block) < rendered.index("role:")
    assert block.endswith(protocol.NEIGHBOUR_FRAGMENT)


@pytest.mark.act_path
def test_the_blind_pass_carries_no_neighbour_text(fed_run):
    prior = _by_act(fed_run, "lectio-prior")["a1"]
    assert "neighbours" not in prior["payload"]["dossier"]
    assert SENTINEL not in json.dumps(prior["payload"])


def _dossiers_built(monkeypatch, root: Path, *args: str, refused: bool = False) -> dict[str, dict]:
    """Run the Perlector in process over a run tree and keep every dossier it built.

    Kept at build time because a narrowed pass with no sibling reading stops later,
    at Pass C's page denominator; the dossier is complete before that.
    """
    built: dict[str, dict] = {}
    real = dossier_module.build_dossier

    def recording(context, **kwargs):
        value = real(context, **kwargs)
        built[kwargs["act_key"]] = value
        return value

    with monkeypatch.context() as patch:
        patch.setattr(dossier_module, "build_dossier", recording)
        patch.chdir(ROOT)
        patch.setattr(
            sys,
            "argv",
            [
                str(ROOT / "pipeline/4_perlector/run.py"),
                "--run-root",
                str(root),
                "--run-id",
                "r",
                "--scenario",
                "happy",
                *args,
            ],
        )
        if refused:
            with pytest.raises(FatalAccounting, match="has no Perlectio"):
                perlector.main()
        else:
            assert perlector.main() == 0
    return built


@pytest.mark.act_path
def test_an_acts_dossier_does_not_depend_on_its_siblings_readings(tmp_path, monkeypatch):
    """Built alone, with no other act's Perlectio anywhere, a2's dossier and prompt are the same.

    Parallel calls are the same question (`test_live_perlector.py`'s serial-against-
    batched byte comparison covers the dossiers too): neighbour clues come from sealed
    Attestatores records, never from another act's reading.
    """
    for name in ("whole", "alone"):
        for program in programs_through("attestatores"):
            result = run_stage(tmp_path / name, "r", "happy", program)
            assert result.returncode == 0, f"{program}: {result.stderr}"
    whole = _dossiers_built(monkeypatch, tmp_path / "whole")
    # A second pass over the same tree rebuilds every dossier with a1's reading sealed.
    assert set(_by_act(RunTree(tmp_path / "whole", "r"), "perlectio")) == {"a1", "a2"}
    again = _dossiers_built(monkeypatch, tmp_path / "whole")
    alone = _dossiers_built(
        monkeypatch, tmp_path / "alone", "--act", whole["a2"]["act_id"], refused=True
    )
    assert set(alone) == {"a2"}
    assert not stage_artifacts(RunTree(tmp_path / "alone", "r"), PERLECTOR, "perlectio")
    assert alone["a2"]["neighbours"]["preceding"]["act_key"] == "a1"
    config = _sealed_protocol()
    rendered = {
        prompts.build_prompt("unproven-real-perlector", "perlector", built["a2"], config)
        for built in (whole, again, alone)
    }
    assert len(rendered) == 1
    assert whole["a2"] == again["a2"] == alone["a2"]


# --- the published neighbour shape is closed ------------------------------------


def _published(fed_run) -> dict:
    return _by_act(fed_run, "perlectio")["a1"]


_CAP = _sealed_protocol()["neighbours"]["characters_per_row"]


def _refused_with(fed_run, change, match: str) -> None:
    dossier = copy.deepcopy(_published(fed_run)["payload"]["dossier"])
    change(dossier["neighbours"])
    with pytest.raises(SchemaRefusal, match=match):
        perlector._validate_neighbours(dossier, _CAP)


@pytest.mark.act_path
def test_a_neighbour_naming_this_act_is_refused(fed_run):
    own = _published(fed_run)["payload"]["dossier"]["act_id"]
    _refused_with(
        fed_run, lambda neighbours: neighbours["following"].update(act_id=own), "its own act"
    )


@pytest.mark.act_path
def test_a_neighbour_with_an_extra_field_is_refused(fed_run):
    _refused_with(
        fed_run, lambda neighbours: neighbours["following"].update(extra=1), "closed shape"
    )


@pytest.mark.act_path
def test_a_neighbour_ref_outside_the_attestatores_testimonia_is_refused(fed_run):
    def repoint(neighbours):
        neighbours["following"]["witnesses"][0]["testimonium_ref"]["relative_path"] = (
            "4_perlector/artifacts/perlectio/x.json"
        )

    _refused_with(fed_run, repoint, "sealed Attestatores Testimonium")


@pytest.mark.act_path
def test_the_published_neighbours_pass_their_own_validation(fed_run):
    perlector._validate_neighbours(_published(fed_run)["payload"]["dossier"], _CAP)


def _reading(side: str, shown: str, length: int) -> dict:
    witness = {
        "witness_label": "w1",
        "outcome": "read",
        "reported": "x" * length,
        "reported_basis": "own-report",
        "shown": shown,
        "testimonium_ref": {
            "relative_path": perlector._TESTIMONIUM_PREFIX + "x.json",
            "sha256": "0" * 64,
        },
    }
    entry = {
        "act_id": "other",
        "act_key": "k",
        "same_page": True,
        "witnesses": [witness],
        "unavailable": None,
    }
    return {"act_id": "own", "neighbours": {"preceding": None, "following": None, side: entry}}


@pytest.mark.parametrize(
    ("side", "shown", "length"),
    [
        ("following", "whole", _CAP + 1),
        ("preceding", "tail", _CAP - 1),
        ("preceding", "tail", _CAP + 1),
        ("following", "head", _CAP - 1),
        ("following", "tail", _CAP),
        ("preceding", "head", _CAP),
    ],
)
def test_a_reading_not_cut_as_the_sealed_cap_cuts_it_is_refused(side, shown, length):
    with pytest.raises(SchemaRefusal, match="sealed neighbour cap"):
        perlector._validate_neighbours(_reading(side, shown, length), _CAP)


@pytest.mark.parametrize(
    ("side", "shown", "length"),
    [("following", "whole", _CAP), ("preceding", "tail", _CAP), ("following", "head", _CAP)],
)
def test_a_reading_cut_as_the_producer_cuts_it_is_accepted(side, shown, length):
    perlector._validate_neighbours(_reading(side, shown, length), _CAP)
