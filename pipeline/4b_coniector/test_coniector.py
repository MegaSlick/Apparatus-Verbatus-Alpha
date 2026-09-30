"""The Coniector on the fixture: off by default, each page reconstructed from its own
text, a join only on consecutive pages, and what is not made says why.

The trees are the fixture's page-read scenarios (`conftest.build_page_tree`), taken
through the real Recensor and Archetypus, then this stage and the Armarium. The
fixture's replies are `[[reconstruction_answer]]` in `proof/skeleton_fixture.toml`.
"""

from __future__ import annotations

import json
import shutil
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from common.contracts.stages import CONIECTOR
from common.reconstruction import load_reconstruction_policy
from common.reconstruction_records import (
    CALL_FAILED,
    CALL_KIND,
    CHAIR_ABSENT,
    LABEL,
    NOT_APPLICABLE_ACT_READ,
    PLAN_KIND,
    RECONSTRUCTION_KIND,
)
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE, verify_final_seal
from conftest import build_page_tree, load_stage, programs_through, run_stage

RUN_ID = "r"
CONIECTOR_PROGRAM = "pipeline/4b_coniector/run.py"
AFTER_PERLECTOR = (
    "pipeline/5_recensor/run.py",
    "pipeline/6_archetypus/run.py",
    CONIECTOR_PROGRAM,
    "pipeline/7_armarium/run.py",
)


def _config(directory: Path, *, mode: str = "on", consecutive: bool = False) -> Path:
    text = Path("config/reconstruction.toml").read_text(encoding="utf-8")
    text = text.replace('mode = "off"', f'mode = "{mode}"')
    if consecutive:
        text = text.replace("pages_are_consecutive = false", "pages_are_consecutive = true")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "reconstruction.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _run(base: Path, scenario: str, **options) -> tuple[Path, dict]:
    root, options = build_page_tree(base, scenario, **options)
    for program in AFTER_PERLECTOR:
        result = run_stage(root, RUN_ID, scenario, program, **options)
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"
    return root, options


def _records(root: Path, kind: str) -> list[dict]:
    tree = RunTree(root, RUN_ID)
    return [
        tree.read_artifact(CONIECTOR, entry["kind"], entry["artifact_id"])
        for entry in tree.build_manifest(CONIECTOR)["artifacts"]
        if entry["kind"] == kind
    ]


def _by_keys(root: Path) -> dict[tuple[str, ...], dict]:
    return {
        tuple(record["payload"]["act_keys"]): record
        for record in _records(root, RECONSTRUCTION_KIND)
    }


def _members(root: Path) -> dict[str, bytes]:
    tree = RunTree(root, RUN_ID)
    export = verify_final_seal(tree)
    data = tree.read_bytes(export["payload"]["bundle"]["reference"]["relative_path"])
    with ZipFile(BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


@pytest.fixture(scope="module")
def off(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("off"), "happy")


@pytest.fixture(scope="module")
def unconsecutive(tmp_path_factory):
    base = tmp_path_factory.mktemp("unconsecutive")
    return _run(base, "happy", reconstruction_config=_config(base / "config-r"))


@pytest.fixture(scope="module")
def consecutive(tmp_path_factory):
    base = tmp_path_factory.mktemp("consecutive")
    return _run(base, "happy", reconstruction_config=_config(base / "config-r", consecutive=True))


@pytest.fixture(scope="module")
def reviewed(tmp_path_factory):
    base = tmp_path_factory.mktemp("page-review")
    return _run(base, "page-review", reconstruction_config=_config(base / "config-r"))


def test_off_by_default_the_plan_asks_nothing_and_no_act_carries_a_reconstruction(off):
    root, _options = off
    assert load_reconstruction_policy().mode == "off"
    (plan,) = _records(root, PLAN_KIND)
    assert plan["payload"]["mode"] == "off" and plan["payload"]["calls"] == []
    assert _records(root, CALL_KIND) == [] and _records(root, RECONSTRUCTION_KIND) == []
    assert "coniector.jsonl" not in _members(root)


def test_unconsecutive_pages_are_each_reconstructed_from_their_own_text(unconsecutive):
    root, _options = unconsecutive
    (plan,) = _records(root, PLAN_KIND)
    assert plan["payload"]["calls"] == [
        {"page_ordinal": 1, "subjects": ["p1:1", "p1:2"], "chains": [], "context": []},
        {"page_ordinal": 2, "subjects": ["p2:1"], "chains": [], "context": []},
    ]
    by_keys = _by_keys(root)
    assert sorted(by_keys) == [("p1:1",), ("p1:2",), ("p2:1",)]
    one = by_keys[("p1:1",)]["payload"]
    assert one["made"] and one["label"] == LABEL
    assert one["reconstruction_raw"] == "SYNTHETIC ACT ONE alpha beta gamma"
    assert one["maker"]["kind"] == "model" and one["maker"]["chair"] == "reconstructor"
    assert one["maker"]["receipt_ref"] is not None
    # A finding is a flag; the act it names is reconstructed as asked.
    two = by_keys[("p1:2",)]["payload"]
    assert two["findings"] == [
        {"code": "cut-at-page-break", "reason": "the act runs on past the page"}
    ]
    assert two["reconstruction_raw"] == "SYNTHETIC ACT TWO delta epsilon zeta eta"


def test_the_prompt_is_text_only_and_the_reply_is_recorded_as_given(unconsecutive):
    root, _options = unconsecutive
    calls = {record["payload"]["page_ordinal"]: record for record in _records(root, CALL_KIND)}
    assert calls[1]["outcome"] == "answered"
    payload = calls[1]["payload"]
    assert payload["shown"] == ["p1:1", "p1:2"]
    assert payload["parse_state"] == "parsed" and payload["serving_mode"] == "fixture"
    assert json.loads(payload["reply_text"])["acts"][0]["act"] == "p1:1"


def test_consecutive_pages_join_an_act_across_the_break_asked_on_its_last_page(consecutive):
    root, _options = consecutive
    (plan,) = _records(root, PLAN_KIND)
    assert plan["payload"]["calls"] == [
        {"page_ordinal": 1, "subjects": ["p1:1"], "chains": [], "context": ["p2:1"]},
        {"page_ordinal": 2, "subjects": [], "chains": [["p1:2", "p2:1"]], "context": ["p1:2"]},
    ]
    join = _by_keys(root)[("p1:2", "p2:1")]
    assert join["subject_id"] == join["payload"]["act_ids"][0]
    assert join["payload"]["unit"] == "join" and join["payload"]["continues"] is True
    assert join["payload"]["reconstruction_raw"] == (
        "SYNTHETIC ACT TWO delta epsilon zeta eta SYNTHETIC ACT TWO delta epsilon zeta eta"
    )


def test_what_is_not_made_says_why_and_the_diplomatic_is_still_delivered(reviewed):
    root, _options = reviewed
    by_keys = _by_keys(root)
    missing = by_keys[("p1:1",)]
    assert missing["outcome"] == "not-made" and missing["payload"]["made"] is False
    assert [reason["code"] for reason in missing["payload"]["not_made"]] == [
        "departure-span-not-found"
    ]
    assert missing["payload"]["findings"] == [{"code": "inconsistent", "reason": "no such word"}]
    assert by_keys[("p1:2",)]["payload"]["made"] is True
    assert [reason["code"] for reason in by_keys[("p2:1",)]["payload"]["not_made"]] == [
        "reply-malformed"
    ]
    export = verify_final_seal(RunTree(root, RUN_ID))
    delivered = {act["act_key"]: act["text"] for act in export["payload"]["delivered"]}
    assert delivered["p1:1"] == "SYNTHETIC ACT ONE alpha beta gamma"


def test_an_act_read_run_plans_nothing_and_says_why(tmp_path):
    root = tmp_path / "runs"
    config = _config(tmp_path / "config-r")
    for program in (*programs_through("archetypus"), CONIECTOR_PROGRAM):
        result = run_stage(root, RUN_ID, "happy", program, reconstruction_config=config)
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"
    (plan,) = _records(root, PLAN_KIND)
    assert plan["payload"]["reading_unit"] == "act"
    assert plan["payload"]["not_applicable"] == NOT_APPLICABLE_ACT_READ
    assert plan["payload"]["calls"] == []


def test_a_run_whose_chair_is_absent_makes_nothing_and_names_the_absence(tmp_path):
    base = tmp_path / "absent"
    root, options = build_page_tree(base, "happy", reconstruction_config=_config(base / "config-r"))
    models = Path(options["models_config"])
    text = models.read_text(encoding="utf-8")
    start = text.index("[chairs.reconstructor]")
    end = text.index("\n\n", start)
    models.write_text(
        text[:start]
        + '[chairs.reconstructor]\nstate = "absent"\nreason = "not served in this test"'
        + text[end:],
        encoding="utf-8",
    )
    # A changed roster is another run: read it again from the Door under this one.
    shutil.rmtree(root)
    for program in (*programs_through("perlector"), *AFTER_PERLECTOR):
        result = run_stage(root, RUN_ID, "happy", program, **options)
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"
    for record in _records(root, RECONSTRUCTION_KIND):
        assert [reason["code"] for reason in record["payload"]["not_made"]] == [CHAIR_ABSENT]
        assert record["payload"]["maker"]["chair_state"] == "absent"


def test_a_resumed_stage_republishes_the_same_records(unconsecutive, tmp_path):
    source, options = unconsecutive
    root = tmp_path / "runs"
    shutil.copytree(source, root)
    tree = RunTree(root, RUN_ID)
    before = {entry["artifact_id"]: entry for entry in tree.build_manifest(CONIECTOR)["artifacts"]}
    # The stage is sealed; a resumed run reuses identical bytes and seals again.
    result = run_stage(root, RUN_ID, "happy", CONIECTOR_PROGRAM, **options)
    assert result.returncode == EXIT_COMPLETE, result.stderr
    after = {entry["artifact_id"]: entry for entry in tree.build_manifest(CONIECTOR)["artifacts"]}
    for identifier, entry in before.items():
        if entry["kind"] not in ("stage-seal", "decode-environment"):
            assert after[identifier]["sha256"] == entry["sha256"]


# --- the live call, with a fake chair ------------------------------------------------------


class _Client:
    def __init__(self, reply=None, error=None):
        self.requests, self.reply, self.error = [], reply, error

    def read(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            parse_problem=None,
            content=self.reply,
            finish_reason="stop",
            raw_response_ref={"relative_path": "raw", "sha256": "a" * 64},
            call_record_ref={"relative_path": "call", "sha256": "b" * 64},
            request_sha256="c" * 64,
            receipt_ref={"relative_path": "receipt", "sha256": "d" * 64},
            served_model_id="reconstructor-qwen3.8-27b",
            response_sha256="a" * 64,
        )


def _live_chair(client):
    row = SimpleNamespace(
        recipe="r",
        chair="reconstructor",
        tier="t",
        max_model_len=65536,
        min_pixels=65536,
        max_pixels=5299200,
        patch_size=16,
        merge_size=2,
    )
    return SimpleNamespace(present=True, live=True, row=lambda: row, client=client, start=None)


CALL = {"page_ordinal": 1, "subjects": ["p1:1"], "chains": [], "context": []}


def test_a_live_call_sends_one_text_turn_thinking_off_with_its_capacity():
    stage = load_stage("4b_coniector", "run")
    client = _Client(reply='{"acts": [], "joins": []}')
    asked = stage._ask(
        _live_chair(client), CALL, "the prompt", load_reconstruction_policy(), 8192, "page 1"
    )
    (request,) = client.requests
    assert [dict(message) for message in request.messages] == [
        {"role": "user", "content": "the prompt"}
    ]
    assert request.image_sha256s == ()
    assert dict(request.generation_sent)["chat_template_kwargs"] == {"enable_thinking": False}
    assert asked["capacity"]["capacity"]["prompt_tokens_basis"] == "all-text-per-byte"
    assert asked["reply_text"] == '{"acts": [], "joins": []}'


def test_a_live_call_that_fails_leaves_no_reply_and_names_its_retained_bytes():
    from common.chat_request import EngineSignalRefusal

    stage = load_stage("4b_coniector", "run")
    refusal = EngineSignalRefusal(
        "NO_CHOICES",
        "no choices",
        raw_response_ref={"relative_path": "raw", "sha256": "a" * 64},
        call_record_ref={"relative_path": "call", "sha256": "b" * 64},
        request_sha256="c" * 64,
        receipt_ref={"relative_path": "receipt", "sha256": "d" * 64},
        served_model_id="m",
    )
    asked = stage._ask(
        _live_chair(_Client(error=refusal)),
        CALL,
        "the prompt",
        load_reconstruction_policy(),
        8192,
        "page 1",
    )
    assert asked["reply_text"] is None
    assert [problem["code"] for problem in asked["problems"]] == [CALL_FAILED]
    assert asked["failure"]["raw_response_ref"] == {"relative_path": "raw", "sha256": "a" * 64}


def test_the_armarium_refuses_an_export_without_the_coniector_seal(off, tmp_path):
    source, options = off
    root = tmp_path / "runs"
    shutil.copytree(source, root)
    shutil.rmtree(root / RUN_ID / "4b_coniector")
    result = run_stage(root, RUN_ID, "happy", "pipeline/7_armarium/run.py", **options)
    assert result.returncode != 0
    assert "side branch coniector has no stage-seal" in result.stderr


# --- what the Armarium's recompute refuses -------------------------------------------------


def _tamper(root: Path, kind: str, keys_or_page, change) -> None:
    from common.contracts.canonical import digest_bytes
    from conftest import _stage_records, _write_record

    for path, record in _stage_records(root, RUN_ID, "4b_coniector", kind):
        payload = record["payload"]
        if payload.get("act_keys") == keys_or_page or payload.get("page_ordinal") == keys_or_page:
            before = digest_bytes(path.read_bytes())
            change(payload)
            _write_record(path, record)
            after = digest_bytes(path.read_bytes())
            # A forger rewrites what names the record too, so the recompute is what refuses.
            for named_path, named in _stage_records(
                root, RUN_ID, "4b_coniector", RECONSTRUCTION_KIND
            ):
                if named["payload"]["call_ref"]["sha256"] == before:
                    named["payload"]["call_ref"]["sha256"] = after
                    for reference in named["inputs"]:
                        if reference["sha256"] == before:
                            reference["sha256"] = after
                    _write_record(named_path, named)
            return
    raise AssertionError(f"no {kind} record for {keys_or_page!r}")


def _verified(root: Path, options: dict):
    from common.reconstruction_records import verified_reconstructions
    from common.stage import READING_UNIT_PAGE, reading_acts
    from conftest import page_context

    # Opened as the Coniector, whose seal check reads the Perlector's alone, so the
    # recompute itself is what meets the tampered record.
    context = page_context(root, RUN_ID, "happy", options, stage=CONIECTOR)
    return verified_reconstructions(context, READING_UNIT_PAGE, reading_acts(context))


@pytest.mark.parametrize(
    "kind, key, change, refusal",
    [
        (
            RECONSTRUCTION_KIND,
            ["p1:1"],
            lambda payload: payload.update(reconstruction_raw="SYNTHETIC ACT ONE invented"),
            "is not the one its call's reply gives",
        ),
        (
            CALL_KIND,
            1,
            lambda payload: payload.update(
                reply_text=payload["reply_text"].replace(
                    '"reconstruction":"gamma"', '"reconstruction":"delta"'
                )
            ),
            "reply the fixture never declared",
        ),
        (
            CALL_KIND,
            1,
            lambda payload: payload["maker"]["resolved_revision"].update(value="0" * 64),
            "maker other than the run's reconstructor chair",
        ),
        (
            CALL_KIND,
            2,
            lambda payload: payload.update(
                reply_text=None,
                finish_reason=None,
                stop_reason=None,
                parse_state="not-asked",
                problems=[{"code": CHAIR_ABSENT, "detail": "forged"}],
            ),
            "receipt exactly when it was not asked",
        ),
    ],
    ids=["reconstruction", "reply", "maker", "not-asked"],
)
def test_the_recompute_refuses_a_forged_record(unconsecutive, tmp_path, kind, key, change, refusal):
    from common.contracts.errors import FatalAccounting

    source, options = unconsecutive
    root = tmp_path / "runs"
    shutil.copytree(source, root)
    _tamper(root, kind, key, change)
    with pytest.raises(FatalAccounting, match=refusal):
        _verified(root, options)


def test_a_call_refused_for_capacity_is_verified_with_the_record_it_was_refused_on():
    from common.chairs.models import ChairIdentity
    from common.reconstruction_records import REQUEST_OVER_CAPACITY, _require_not_asked_evidence

    # The roster's chair is configured; only its type is read here.
    present = ChairIdentity.__new__(ChairIdentity)
    context = SimpleNamespace(registry=SimpleNamespace(resolve=lambda _role: present))
    payload = {
        "reply_text": None,
        "finish_reason": None,
        "stop_reason": None,
        "engine_call": None,
        "failure": None,
        # The shape the stage records a refusal in.
        "capacity": {"capacity": {"fits": False}, "answer_reserve": None, "max_tokens": None},
        "problems": [{"code": REQUEST_OVER_CAPACITY, "detail": "too large"}],
    }
    _require_not_asked_evidence(context, payload, "page 1")
