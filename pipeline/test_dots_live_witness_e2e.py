"""dots.mocr read live, through the serving fakes, on the one page routed to it.

The roster seats dots.mocr (`attestator_4`) by `[witness_routing]`
(`conftest.dots_models_config`); the fixture's `dots-table` scenario makes
page 2 a table page. The Door through the Designator run as real programs on
fixture rows; the Attestatores run their own `main` in process, every witness
served live through `operations/serving/fakes.py` as in
`pipeline/test_live_reading_seam_e2e.py`, whose driver this module reuses.

What it shows: dots.mocr is asked once, for page 2 only, in its vendor's
message shape and sampling; its answer is captured in its own grammar with its
boxes mapped back onto the page; and while its prompt has no measured token
count the request is refused by name, never sent on a guess.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import DOTS_CHAIR, dots_models_config, programs_through

ROOT = Path(__file__).resolve().parents[1]

import test_live_reading_seam_e2e as seam  # noqa: E402
from test_live_reading_seam_e2e import (  # noqa: E402
    CHANDRA_PAGE_ONE,
    CHANDRA_PAGE_TWO,
    CHURRO_PAGE_ONE,
    CHURRO_PAGE_TWO,
    DAI_ACT_ONE,
    DAI_ACT_TWO,
    TIER,
    TIERS,
    WitnessWorld,
    _toml_profile,
    _vllm_row,
    attestatores,
    stage_argv,
)

from common import dots_layout, request_capacity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.stages import ATTESTATORES  # noqa: E402
from common.decoding import load_decoding_policy  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import EXIT_COMPLETE  # noqa: E402
from operations.serving.config import (  # noqa: E402
    chair_preflight_identity_digest,
    load_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.fakes import ScriptedAnswer  # noqa: E402

RUN_ID = "r"
DOTS = DOTS_CHAIR
WITNESSES = ("attestator_1", "attestator_2", "attestator_3", DOTS)
DOTS_ANSWER = json.dumps(
    [
        {
            "bbox": [20, 20, 176, 78],
            "category": "Text",
            "text": "SYNTHETIC ACT TWO delta epsilon zeta eta",
        }
    ]
)
# dots.mocr's sampling as its vendor's command line sends it, over vLLM's
# defaults for the fields it does not send.
DOTS_DECODING = """
[chair_decoding.attestator_4]
# dots-studio/dots.mocr, read the way the vendor's `dots_mocr/parser.py` reads a
# page: `inference_with_vllm` sends temperature and top_p and nothing else.
temperature = 0.1
top_p = 1.0
top_k = 0
min_p = 0.0
repetition_penalty = 1.0
source = "https://github.com/rednote-hilab/dots.mocr/blob/23f3e5612fb8066d4034d5ecfc8f33a9243533eb/dots_mocr/parser.py"
revision = "23f3e5612fb8066d4034d5ecfc8f33a9243533eb"
verification = "test stand-in written beside the vendor card (operations/bakeoff/cards/dots-mocr.md)"
"""


def _decoding(directory: Path) -> Path:
    path = directory / "decoding.toml"
    path.write_text(
        (ROOT / "config" / "decoding.toml").read_text(encoding="utf-8") + DOTS_DECODING,
        encoding="utf-8",
    )
    return path


def _catalogue(path: Path, models: Path) -> Path:
    """Fixture rows for the Designator's chairs, live rows for the four witnesses."""
    registry = ChairRegistry.from_toml(str(models))
    rows = [
        {
            "kind": "fixture",
            "recipe": recipe,
            "chair": chair,
            "tier": tier,
            "description": "offline walking-skeleton fixture row",
        }
        for chair, recipe in (
            ("secondary_proposer", "fake-secondary-proposer-v0"),
            ("designator_surya", "fake-surya-v0"),
            ("reconstructor", "fake-reconstructor-v0"),
        )
        for tier in TIERS
    ]
    for index, chair in enumerate((*WITNESSES, "perlector")):
        identity = registry.resolve(chair)
        for tier in TIERS:
            row = _vllm_row(
                recipe=identity.serving_recipe, chair=chair, tier=tier, port=8500 + index
            )
            if chair == DOTS:
                # Room for the vendor's whole 16,384-token answer beside the page,
                # and Qwen2-VL's 14-pixel patches.
                row["max_model_len"] = 32768
                row["patch_size"] = 14
            row["preflight_identity_digest"] = chair_preflight_identity_digest(identity)
            row["preflight_digest"] = profile_preflight_digest(row)
            rows.append(row)
    path.write_text(
        'schema = "serving-recipes.v1"\n\n' + "\n".join(_toml_profile(row) for row in rows),
        encoding="utf-8",
    )
    load_serving_recipes(path)
    return path


def _argv(run_root: Path, catalogue: Path, models: Path, decoding: Path, tier=None) -> list[str]:
    argv = stage_argv(run_root, catalogue, placement_tier=tier)
    for flag, value in (
        ("--models-config", models),
        ("--decoding-config", decoding),
        ("--scenario", "dots-table"),
    ):
        argv[argv.index(flag) + 1] = str(value)
    return argv


def _scripts() -> dict[str, list[ScriptedAnswer]]:
    return {
        "attestator_1": [
            ScriptedAnswer(content=CHANDRA_PAGE_ONE, finish_reason="stop"),
            ScriptedAnswer(content=CHANDRA_PAGE_TWO, finish_reason="stop"),
        ],
        # The detector's three records, in its order: a1, a2, a2's continuation.
        "attestator_2": [
            ScriptedAnswer(content=answer, finish_reason="stop")
            for answer in (DAI_ACT_ONE, DAI_ACT_TWO, "zeta eta")
        ],
        "attestator_3": [
            ScriptedAnswer(content=CHURRO_PAGE_ONE, finish_reason="stop"),
            ScriptedAnswer(content=CHURRO_PAGE_TWO, finish_reason="stop"),
        ],
        DOTS: [ScriptedAnswer(content=DOTS_ANSWER, finish_reason="stop")],
    }


def _witness(work: Path, *, measured_prompt: bool) -> tuple[RunTree, WitnessWorld]:
    models = dots_models_config(work / "models")
    decoding = _decoding(work)
    catalogue = _catalogue(work / "serving_recipes.toml", models)
    run_root = work / "runs"
    for program in programs_through("designator"):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(ROOT / program),
                *_argv(run_root, catalogue, models, decoding),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == EXIT_COMPLETE, f"{program}: {result.stderr}"
    policy, decoding_sha256 = load_decoding_policy(str(decoding))
    patch = pytest.MonkeyPatch()
    # The serving fakes' clients send the sealed decoding row; this run's carries dots.mocr's.
    patch.setattr(seam, "shipped_decoding_policy", lambda: (policy, decoding_sha256))
    if measured_prompt:
        # A stand-in count, not a measurement: no tokenizer runs offline. It is
        # bound to the exact prompt text, as a measured count is.
        patch.setattr(
            request_capacity,
            "MEASURED_PROMPT_TOKENS",
            {
                **request_capacity.MEASURED_PROMPT_TOKENS,
                DOTS: (
                    request_capacity.SealedPromptTokens(
                        tokens=300,
                        prompt_digest=request_capacity.prompt_digest(dots_layout.prompt_text()),
                        repo="dots-studio/dots.mocr",
                        revision="e539fbb52280393adc081b289ec597430a0f9031",
                    ),
                ),
            },
        )
    witnesses = WitnessWorld(catalogue, decoding_sha256, work / "witness-world", _scripts())
    original = sys.argv
    sys.argv = [attestatores.__file__, *_argv(run_root, catalogue, models, decoding, TIER)]
    try:
        assert attestatores.main(serving_factory=witnesses.factory) == EXIT_COMPLETE
    finally:
        sys.argv = original
        patch.undo()
    return RunTree(run_root, RUN_ID), witnesses


def _pages(tree: RunTree, chair: str) -> dict[int, dict]:
    return {
        record["payload"]["page_ordinal"]: record
        for record in (
            tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
            for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
            if entry["kind"] == "page-testimonium"
        )
        if record["payload"]["chair"] == chair
    }


def test_dots_is_served_once_for_its_routed_page_in_its_vendors_shape(tmp_path):
    tree, witnesses = _witness(tmp_path, measured_prompt=True)
    pages = _pages(tree, DOTS)
    assert set(pages) == {2}, "dots.mocr has a record on the routed page only"
    record = pages[2]
    assert record["outcome"] == "read"
    payload = record["payload"]
    assert payload["payload"] == "SYNTHETIC ACT TWO delta epsilon zeta eta"
    assert payload["native_capture"]["parse"]["parser"] == "layout-json"
    assert "serving_call_ref" in payload
    (observed,) = payload["observed"]
    assert observed == {
        "ordinal": 0,
        "bounds": {"x": 20, "y": 20, "w": 160, "h": 61},
        "bounds_source": "native",
        "span": {"start": 0, "end": 40},
    }
    # One reading request, after the readiness probe: the image, then the
    # vendor's placeholder and prompt, in one user turn, at its sampling.
    requests = [body for body in witnesses.endpoints[DOTS].requests if "messages" in body]
    reading = [body for body in requests if body["messages"][0]["content"] != "READY"]
    assert len(reading) == 1
    (message,) = reading[0]["messages"]
    assert message["role"] == "user"
    assert [part["type"] for part in message["content"]] == ["image_url", "text"]
    assert message["content"][1]["text"] == dots_layout.prompt_text()
    assert reading[0]["temperature"] == 0.1 and reading[0]["top_p"] == 1.0
    assert reading[0]["max_tokens"] == dots_layout.MAX_COMPLETION_TOKENS
    # Every other witness reads both pages, as it always does.
    for chair in ("attestator_1", "attestator_2", "attestator_3"):
        assert set(_pages(tree, chair)) == {1, 2}, chair


def test_with_no_measured_prompt_cost_the_request_is_refused_by_name_and_never_sent(tmp_path):
    tree, witnesses = _witness(tmp_path, measured_prompt=False)
    record = _pages(tree, DOTS)[2]
    assert record["outcome"] == "failed"
    assert "no measured prompt-token count" in record["payload"]["reason"]
    requests = [body for body in witnesses.endpoints[DOTS].requests if "messages" in body]
    assert all(body["messages"][0]["content"] == "READY" for body in requests)
