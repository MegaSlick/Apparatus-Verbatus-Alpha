"""The Coniector on the fixture: on by default, each page reconstructed from its own
text, a join only on consecutive pages, and what is not made says why.

The trees are the fixture's page-read scenarios (`conftest.build_page_tree`), taken
through the real Recensor and Archetypus, then this stage and the Armarium. The
fixture's replies are `[[reconstruction_answer]]` in `proof/skeleton_fixture.toml`.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from common.contracts.stages import CONIECTOR
from common.page_review import held_by_recensor
from common.reconstruction import load_reconstruction_policy
from common.reconstruction_records import (
    CALL_FAILED,
    CALL_KIND,
    CHAIR_ABSENT,
    LABEL,
    PLAN_KIND,
    RECONSTRUCTION_KIND,
)
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE, verify_final_seal
from conftest import (
    advance_held_recensor,
    build_page_tree,
    floor_models_config,
    load_stage,
    programs_through,
    run_stage,
)

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
    text = re.sub(r'^mode = "(on|off)"$', f'mode = "{mode}"', text, count=1, flags=re.M)
    if consecutive:
        text = text.replace("pages_are_consecutive = false", "pages_are_consecutive = true")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "reconstruction.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _run(base: Path, scenario: str, **options) -> tuple[Path, dict]:
    root, options = build_page_tree(base, scenario, **options)
    for program in AFTER_PERLECTOR:
        # A Recensor that holds anything is advanced, as a person would to export
        # with every hold named; no later stage runs past it otherwise.
        if program != AFTER_PERLECTOR[0] and held_by_recensor(RunTree(root, RUN_ID)):
            advance_held_recensor(root, RUN_ID)
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
    base = tmp_path_factory.mktemp("off")
    return _run(base, "happy", reconstruction_config=_config(base / "config-r", mode="off"))


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


def test_the_committed_default_runs_the_reconstructor():
    assert load_reconstruction_policy().mode == "on"


def test_off_the_plan_asks_nothing_and_no_act_carries_a_reconstruction(off):
    root, _options = off
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
    # The fixture's two pieces read alike, so their order is pinned by the act each
    # names; the joined text's order is proved on distinct pieces in
    # `common/test_reconstruction_records.py`.
    rows = [json.loads(line) for line in _members(root)["acts.jsonl"].splitlines()]
    act_ids = {row["act_key"]: row["act_id"] for row in rows}
    assert join["payload"]["act_ids"] == [act_ids["p1:2"], act_ids["p2:1"]]
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


def test_a_run_whose_chair_is_absent_makes_nothing_and_names_the_absence(tmp_path):
    base = tmp_path / "absent"
    root, options = build_page_tree(
        base,
        "happy",
        reconstruction_config=_config(base / "config-r"),
        models_config=floor_models_config(base / "models", 3),
    )
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
    by_keys = _by_keys(root)
    assert sorted(by_keys) == [("p1:1",), ("p1:2",), ("p2:1",)]
    for record in by_keys.values():
        assert [reason["code"] for reason in record["payload"]["not_made"]] == [CHAIR_ABSENT]
        assert record["payload"]["maker"]["chair_state"] == "absent"
    calls = _records(root, CALL_KIND)
    assert sorted(call["payload"]["page_ordinal"] for call in calls) == [1, 2]
    assert {call["payload"]["parse_state"] for call in calls} == {"not-asked"}


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
    from operations.serving.chat_request import EngineSignalRefusal

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
    from common.stage import reading_acts
    from conftest import page_context
    from operations.serving.assembly import SERVING_READER

    # Opened as the Coniector, whose seal check reads the Perlector's alone, so the
    # recompute itself is what meets the tampered record. The serving reader is
    # the one the stage opens with, so a live call is read again.
    context = page_context(
        root, RUN_ID, "happy", options, stage=CONIECTOR, serving_reader=SERVING_READER
    )
    return verified_reconstructions(context, reading_acts(context))


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


def _roster_context(*, present: bool):
    from common.chairs.models import ChairIdentity

    # Only the chair's type is read; the retained bytes are on disk as named.
    identity = ChairIdentity.__new__(ChairIdentity) if present else object()
    return SimpleNamespace(
        registry=SimpleNamespace(resolve=lambda _role: identity),
        input_ref=lambda path: {
            "relative_path": path,
            "sha256": {"raw": "a", "call": "b"}[path] * 64,
        },
    )


def _asked_payload(asked: dict) -> dict:
    return {**{name: asked[name] for name in asked}, "call": CALL}


def _refused_for_capacity():
    stage = load_stage("4b_coniector", "run")
    chair = _live_chair(_Client(reply="unused"))
    small = SimpleNamespace(**{**vars(chair.row()), "max_model_len": 64})
    chair.row = lambda: small
    asked = stage._ask(chair, CALL, "the prompt", load_reconstruction_policy(), 8192, "page 1")
    assert chair.client.requests == []
    return asked


def _failed_call():
    from operations.serving.chat_request import EngineSignalRefusal

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
    return stage._ask(
        _live_chair(_Client(error=refusal)),
        CALL,
        "the prompt",
        load_reconstruction_policy(),
        8192,
        "page 1",
    )


def test_what_the_stage_records_for_a_call_not_asked_is_what_the_verifier_accepts():
    from common.reconstruction_records import REQUEST_OVER_CAPACITY, _require_not_asked_evidence

    over = _refused_for_capacity()
    assert [problem["code"] for problem in over["problems"]] == [REQUEST_OVER_CAPACITY]
    assert over["capacity"]["capacity"]["fits"] is False
    failed = _failed_call()
    assert [problem["code"] for problem in failed["problems"]] == [CALL_FAILED]
    context = _roster_context(present=True)
    for asked in (over, failed):
        _require_not_asked_evidence(context, _asked_payload(asked), "page 1")


def test_a_row_that_cannot_measure_a_request_stops_the_stage_rather_than_recording_it():
    from common.request_capacity import RequestCapacityRefusal

    stage = load_stage("4b_coniector", "run")
    chair = _live_chair(_Client(reply="unused"))
    unmeasurable = SimpleNamespace(**{**vars(chair.row()), "max_model_len": None})
    chair.row = lambda: unmeasurable
    with pytest.raises(RequestCapacityRefusal, match="no positive max_model_len"):
        stage._ask(chair, CALL, "the prompt", load_reconstruction_policy(), 8192, "page 1")


@pytest.mark.parametrize(
    "asked, change, present, refusal",
    [
        (
            _refused_for_capacity,
            {"capacity": None},
            True,
            "refused for capacity with no record that it did not fit",
        ),
        (_failed_call, {"failure": None}, True, "failed with no failure or admission recorded"),
        (
            _failed_call,
            {
                "failure": None,
                "capacity": None,
                "problems": [{"code": CHAIR_ABSENT, "detail": "x"}],
            },
            True,
            "says whether its chair is absent against the roster",
        ),
        (
            _failed_call,
            {"failure": {"raw_response_ref": {"relative_path": "raw", "sha256": "e" * 64}}},
            True,
            "retained bytes that are not on disk as named",
        ),
        (_failed_call, {}, False, "says whether its chair is absent against the roster"),
    ],
    ids=["capacity-unrecorded", "failure-unrecorded", "absent-against-roster", "bytes", "roster"],
)
def test_a_call_not_asked_without_its_evidence_is_refused(asked, change, present, refusal):
    from common.contracts.errors import FatalAccounting
    from common.reconstruction_records import _require_not_asked_evidence

    payload = {**_asked_payload(asked()), **change}
    with pytest.raises(FatalAccounting, match=refusal):
        _require_not_asked_evidence(_roster_context(present=present), payload, "page 1")


# --- a retained call asked otherwise is not adopted ----------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        lambda payload: payload["call"].update(context=["p2:1"]),
        lambda payload: payload.update(prompt_sha256="0" * 64),
        lambda payload: payload.update(serving_mode="live"),
        lambda payload: payload["maker"]["resolved_revision"].update(value="0" * 64),
    ],
    ids=["call", "prompt", "serving-mode", "maker"],
)
def test_a_retained_call_asked_otherwise_is_not_adopted(unconsecutive, tmp_path, change):
    from conftest import _stage_records, _write_record

    source, options = unconsecutive
    root = tmp_path / "runs"
    shutil.copytree(source, root)
    stage = root / RUN_ID / "4b_coniector"
    # The stage is resumed after its calls were sealed and before anything else
    # was: its reconstructions and seal are gone, its calls retained.
    for kind in (RECONSTRUCTION_KIND, "stage-seal"):
        shutil.rmtree(stage / "artifacts" / kind)
    (stage / "manifest.json").unlink()
    [(path, record)] = [
        (path, record)
        for path, record in _stage_records(root, RUN_ID, "4b_coniector", CALL_KIND)
        if record["payload"]["page_ordinal"] == 1
    ]
    change(record["payload"])
    _write_record(path, record)
    result = run_stage(root, RUN_ID, "happy", CONIECTOR_PROGRAM, **options)
    assert result.returncode != 0
    assert "retained reconstruction call was asked from another plan" in result.stderr, (
        result.stderr
    )
    assert "is not adopted" in result.stderr


# --- a live chair, end to end over the fake served chair -----------------------------------

TIER = "generic-48gb"


def _vllm_row(identity, tier: str) -> dict:
    """A proven `kind = "vllm"` row for the reconstructor, in the live seam's shape
    (`pipeline/test_live_reading_seam_e2e.py`); every figure is a test value."""
    from operations.serving.config import chair_preflight_identity_digest, profile_preflight_digest

    row = {
        "kind": "vllm",
        "recipe": identity.serving_recipe,
        "chair": "reconstructor",
        "tier": tier,
        "host": "127.0.0.1",
        "port": 8310,
        "served_model_id": "served-reconstructor",
        "dtype": "bfloat16",
        "seed": 7,
        "required_packages": {"vllm": "0.test"},
        "max_model_len": 16384,
        "max_num_seqs": 1,
        "max_num_batched_tokens": 256,
        "gpu_memory_utilization": "0.85",
        "min_pixels": 1,
        "max_pixels": 1806336,
        "patch_size": 16,
        "merge_size": 2,
        "enable_prefix_caching": True,
        "enforce_eager": False,
        "trust_remote_code": False,
        "enable_tower_connector_lora": False,
        "max_lora_rank": 16,
        "generation_config": "vllm",
        "preflight_state": "proven",
        "startup_timeout_seconds": 3,
        "poll_interval_seconds": 1,
        "request_timeout_seconds": 30,
        "readiness_probe": {
            "kind": "chat-completions",
            "request_json": '{"messages":[{"role":"user","content":"READY"}],"max_tokens":4}',
        },
        "preflight_identity_digest": chair_preflight_identity_digest(identity),
    }
    row["preflight_digest"] = profile_preflight_digest(row)
    return row


def _toml_profile(row: dict) -> str:
    def value(item):
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, int):
            return str(item)
        return f"'{item}'" if '"' in item else f'"{item}"'

    lines = ["[[profiles]]"]
    lines += [f"{key} = {value(item)}" for key, item in row.items() if not isinstance(item, dict)]
    for name, table in ((key, item) for key, item in row.items() if isinstance(item, dict)):
        lines.append(f"[profiles.{name}]")
        lines += [f"{key} = {value(item)}" for key, item in table.items()]
    return "\n".join(lines) + "\n"


def _live_reconstructor_catalogue(catalogue: Path, models: Path) -> None:
    """The catalogue with the reconstructor's fixture rows served live."""
    from common.chairs.registry import ChairRegistry
    from operations.serving.config import load_serving_recipes

    identity = ChairRegistry.from_toml(str(models)).resolve("reconstructor")
    text = catalogue.read_text(encoding="utf-8")
    for tier in ("generic-24gb", "generic-48gb", "generic-80gb-plus"):
        fixture = (
            '[[profiles]]\nkind = "fixture"\nrecipe = "fake-reconstructor-v0"\n'
            f'chair = "reconstructor"\ntier = "{tier}"\n'
            "description = \"offline walking-skeleton fixture for the Coniector's reconstructor "
            'chair"\n'
        )
        assert text.count(fixture) == 1, tier
        text = text.replace(fixture, _toml_profile(_vllm_row(identity, tier)))
    catalogue.write_text(text, encoding="utf-8")
    load_serving_recipes(catalogue)


def _declared_answers() -> list[str]:
    import tomllib

    rows = tomllib.loads(Path("proof/skeleton_fixture.toml").read_text(encoding="utf-8"))[
        "reconstruction_answer"
    ]
    return [
        row["answer"]
        for ordinal in (1, 2)
        for row in rows
        if row["scenario"] == "happy"
        and row["page_ordinal"] == ordinal
        and row["pages_are_consecutive"] is False
    ]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """The `unconsecutive` run with its reconstructor served live: a real ChairClient and
    ServingManager over a scripted endpoint, the stage's own `main` in process."""
    from operations.serving.assembly import retain_chair_bytes
    from operations.serving.client import ChairClient
    from operations.serving.config import ServingConfigInputs, load_serving_recipes
    from operations.serving.fakes import FakeEndpoint, FakeLauncher, FakePackages, ScriptedAnswer
    from operations.serving.manager import ServingManager, StageContextReceiptPublisher
    from operations.serving.residency import FileResidencyLease

    base = tmp_path_factory.mktemp("live")
    repository = Path(__file__).resolve().parents[2]
    catalogue = base / "live-models" / "serving_recipes.toml"
    catalogue.parent.mkdir(parents=True)
    catalogue.write_bytes((repository / "config" / "serving_recipes.toml").read_bytes())
    _live_reconstructor_catalogue(catalogue, repository / "config" / "models.toml")
    roster = {"serving_recipes_config": catalogue}
    root, options = build_page_tree(
        base, "happy", reconstruction_config=_config(base / "config-r"), **roster
    )
    for program in AFTER_PERLECTOR[:2]:
        result = run_stage(root, RUN_ID, "happy", program, **options)
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"
    endpoint = FakeEndpoint(served_model_id="served-reconstructor")
    endpoint.script(
        *(ScriptedAnswer(content=answer, finish_reason="stop") for answer in _declared_answers())
    )

    def factory(context, identity, tier, *, decoding_policy, decoding_config_sha256):
        manager = ServingManager(
            registry=context.registry,
            recipes=load_serving_recipes(context.args.serving_recipes_config),
            config_inputs=ServingConfigInputs.from_record(dict(context.serving_config_inputs)),
            launcher=FakeLauncher(endpoint),
            http=endpoint,
            receipt_publisher=StageContextReceiptPublisher(context),
            log_root=base / "serving-logs",
            package_inspector=FakePackages({"vllm": "0.test"}),
            residency_lease=FileResidencyLease(base / "pod-gpu.lock"),
            producer=CONIECTOR_PROGRAM,
        )
        return ChairClient(
            manager=manager,
            identity=identity,
            tier=tier,
            retain=lambda data: retain_chair_bytes(context, data),
            decoding_config_sha256=decoding_config_sha256,
            decoding_policy=decoding_policy,
            read_receipt=context.tree.read_run_receipt,
        )

    stage = load_stage("4b_coniector", "run")
    argv = [CONIECTOR_PROGRAM, "--run-root", str(root), "--run-id", RUN_ID, "--scenario", "happy"]
    for name, value in {**options, "placement_tier": TIER}.items():
        argv += [f"--{name.replace('_', '-')}", str(value)]
    original = sys.argv
    sys.argv = argv
    try:
        assert stage.main(serving_factory=factory) == EXIT_COMPLETE
    finally:
        sys.argv = original
    assert len(endpoint.requests) == 2
    return root, options


def test_a_live_reconstruction_is_verified_against_the_request_its_engine_was_sent(live):
    root, options = live
    verified = _verified(root, options)
    assert {call["serving_mode"] for call in verified["calls"].values()} == {"live"}
    assert all(call["engine_call"] is not None for call in verified["calls"].values())
    assert sorted(record["act_keys"][0] for record in verified["acts"].values()) == [
        "p1:1",
        "p1:2",
        "p2:1",
    ]
    assert all(record["made"] for record in verified["acts"].values())


def _forge_retained_request(payload: dict, root: Path) -> None:
    """Rewrite the retained call record to name another request, as a forger would."""
    from common.contracts.canonical import digest_bytes

    reference = payload["engine_call"]["call_record_ref"]
    record = json.loads((root / RUN_ID / reference["relative_path"]).read_bytes())
    record["request_sha256"] = "0" * 64
    data = json.dumps(record).encode("utf-8")
    digest = digest_bytes(data)
    path = root / RUN_ID / reference["relative_path"].replace(reference["sha256"], digest)
    path.write_bytes(data)
    payload["engine_call"]["call_record_ref"] = {
        "relative_path": reference["relative_path"].replace(reference["sha256"], digest),
        "sha256": digest,
    }


@pytest.mark.parametrize(
    "change, refusal",
    [
        (_forge_retained_request, "answered for a request other than this prompt"),
        (
            lambda payload, _root: payload["capacity"].update(
                max_tokens=payload["capacity"]["max_tokens"] - 1
            ),
            "answered for a request other than this prompt",
        ),
        (
            lambda payload, _root: payload.update(
                reply_text=payload["reply_text"].replace(
                    '"reconstruction":"gamma"', '"reconstruction":"delta"'
                )
            ),
            "holds a reply other than the one its engine returned",
        ),
        (
            lambda payload, _root: payload.update(serving_mode="fixture"),
            "is a fixture reply carrying live call evidence",
        ),
    ],
    ids=["request-sha256", "request-cap", "reply", "fixture-with-engine-call"],
)
def test_the_recompute_refuses_a_forged_live_call(live, tmp_path, change, refusal):
    from common.contracts.errors import FatalAccounting

    source, options = live
    root = tmp_path / "runs"
    shutil.copytree(source, root)
    _tamper(root, CALL_KIND, 1, lambda payload: change(payload, root))
    with pytest.raises(FatalAccounting, match=refusal):
        _verified(root, options)
