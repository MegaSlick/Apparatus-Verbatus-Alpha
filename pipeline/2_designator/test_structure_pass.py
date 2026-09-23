"""The Designator's live structure pass, proven offline end to end (SPEC_D §1, §2, §5).

Nothing here starts a pod, opens a socket, or loads a model. The run tree is
built by the real Door, Exemplar and Ink Map programs as subprocesses, and then
`run.py`'s own `main` is called in this process with the fake endpoint from
`operations/serving/fakes.py` behind it -- so what is proved is the stage's
wiring: which pass the sealed catalogue selects, what is sent per page, what
each answer does to the page, what the minted rows carry, and that the
consumer-side verifier `common/stage.py::expected_acts` (D3) accepts what this
producer wrote.

The selector is deliberately not a flag on this stage. A run is live because
the serving-recipe row sealed into its `config_digest` says `kind = "vllm"` for
the resolved structure chair, so these tests build a catalogue whose
`designator_structure` rows are live and let the run bind it exactly as a real
one would. Every other chair keeps its fixture row.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _test_support import load_designator

import common.stage as stage_contract
from common import chandra_layout, structure_answer
from common.chairs.registry import ChairRegistry
from common.chandra_presentation import (
    STRUCTURE_REQUEST_IMAGE_KIND,
    STRUCTURE_REQUEST_IMAGE_SCHEMA,
)
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal
from common.contracts.identities import act_bindings
from common.contracts.identities import verify as verify_identity
from common.contracts.serving import (
    CHAIR_CALL_RECORD_SCHEMA,
    CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA,
)
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR
from common.decoding import load_decoding_policy
from common.fixture_identity import page_identity
from common.imaging import dimensions
from common.imaging_ports import scale_to_fit_chandra
from common.request_capacity import DECLARED_ANSWER_BOUND_TOKENS
from common.runtree.store import RECEIPTS_DIR, RunTree
from common.stage import (
    EXIT_COMPLETE,
    EXIT_HELD,
    STRUCTURE_ANSWER_KIND,
    STRUCTURE_ANSWER_RECORD_SCHEMA,
    STRUCTURE_ANSWER_RECORD_SCHEMA_V2,
    STRUCTURE_ANSWER_RECORD_SCHEMA_V3,
    expected_acts,
    load_fixture,
    open_stage_context,
    stage_parser,
)
from operations.serving.client import ChairClient
from operations.serving.config import (
    ServingConfigInputs,
    chair_preflight_identity_digest,
    load_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.fakes import (
    FakeEndpoint,
    FakeLauncher,
    FakePackages,
    FakeRegistry,
    ScriptedAnswer,
    scripted_prompt_too_long,
    scripted_structure_answer,
    scripted_structure_refusal,
    structure_answer_body,
    structure_blank_page_body,
    structure_layout_block,
)
from operations.serving.http import request_body
from operations.serving.manager import ServingManager, StageContextReceiptPublisher
from operations.serving.residency import FileResidencyLease
from operations.submit import gate, submit

ROOT = Path(__file__).resolve().parents[2]
DOOR_CLI = ROOT / "pipeline" / "1_exemplar" / "door.py"
EXEMPLAR_CLI = ROOT / "pipeline" / "1_exemplar" / "run.py"
INK_MAP_CLI = ROOT / "pipeline" / "1_ink_map" / "run.py"
ATTESTATORES_CLI = ROOT / "pipeline" / "3_attestatores" / "run.py"
MODELS_CONFIG = ROOT / "config" / "models.toml"
FIXTURE_CATALOGUE = ROOT / "config" / "serving_recipes.toml"
FIXTURE_PAGES = ROOT / "proof" / "fixtures" / "synthetic-two-page-v0"
RUN_ID = "r"
TIER = "generic-48gb"
SERVED_MODEL_ID = "designator-structure-under-test"

# The synthetic fixture's own ink, in page pixels (`proof/synthetic_pages.py`).
PAGE_ONE_ACTS = (
    ({"x": 20, "y": 20, "w": 160, "h": 80}, "SYNTHETIC ACT ONE alpha beta gamma"),
    ({"x": 20, "y": 120, "w": 160, "h": 100}, "SYNTHETIC ACT TWO delta epsilon zeta eta"),
)
PAGE_TWO_ACTS = (({"x": 20, "y": 20, "w": 160, "h": 60}, "SYNTHETIC ACT THREE theta iota"),)
SCRIPTED_TEXTS = tuple(text for _bounds, text in PAGE_ONE_ACTS + PAGE_TWO_ACTS)

designator = load_designator("designator_structure_pass_under_test")
structure_pass = designator.structure_pass


# --- building the run and the catalogue ---------------------------------------


def _structure_identity():
    return ChairRegistry.from_toml(str(MODELS_CONFIG)).resolve("designator_structure")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return "{ " + ", ".join(f'"{k}" = {_toml_value(v)}' for k, v in value.items()) + " }"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _live_row(identity) -> dict[str, Any]:
    """One `kind = "vllm"` row for the fixture roster's own structure chair.

    `preflight_state = "proven"` with the two real digests, because the manager
    refuses to launch an unproven row and these tests exercise the manager the
    production factory would build. The proof is a test fixture in a tmp
    directory; no catalogue in the repository is edited.
    """
    row: dict[str, Any] = {
        "kind": "vllm",
        "recipe": identity.serving_recipe,
        "chair": identity.role,
        "tier": TIER,
        "host": "127.0.0.1",
        "port": 8107,
        "served_model_id": SERVED_MODEL_ID,
        "dtype": "bfloat16",
        "seed": 0,
        "required_packages": {"vllm": "0.test"},
        "max_model_len": 4096,
        "max_num_seqs": 1,
        "max_num_batched_tokens": 512,
        "gpu_memory_utilization": "0.58",
        "min_pixels": 3136,
        "max_pixels": 1806336,
        # The chair's own vision-encoder geometry, as the shipped real
        # catalogue states it for Chandra: without it nothing can say what one
        # page image costs this chair in prompt tokens, and `ask_page` refuses
        # by name rather than counting against a default
        # (`common/request_capacity.py`).
        "patch_size": 16,
        "merge_size": 2,
        "enable_prefix_caching": True,
        "enforce_eager": False,
        "trust_remote_code": False,
        "enable_tower_connector_lora": False,
        "max_lora_rank": 64,
        "generation_config": "vllm",
        "preflight_state": "proven",
        "startup_timeout_seconds": 3,
        "poll_interval_seconds": 1,
        "request_timeout_seconds": 30,
        "readiness_probe": {
            "kind": "chat-completions",
            "request_json": '{"messages":[{"role":"user","content":"READY"}],"max_tokens":4}',
        },
    }
    row["preflight_identity_digest"] = chair_preflight_identity_digest(identity)
    row["preflight_digest"] = profile_preflight_digest(row)
    return row


def _live_catalogue(destination: Path) -> Path:
    """The committed fixture catalogue with its structure-chair rows made live.

    The three `designator_structure` fixture rows come first in the committed
    file; they are replaced by one live row at `TIER`, and every other chair's
    rows follow unchanged. The committed file is never touched.
    """
    source = FIXTURE_CATALOGUE.read_text(encoding="utf-8")
    marker = '[[profiles]]\nkind = "fixture"\nrecipe = "fake-designator-v0"'
    head, *designator_rows = source.split(marker)
    assert len(designator_rows) == 3, "the fixture catalogue no longer carries three structure rows"
    tail = designator_rows[-1]
    tail = tail[tail.index("\n[[profiles]]") :]
    row = _live_row(_structure_identity())
    body = "\n".join(f"{key} = {_toml_value(value)}" for key, value in row.items())
    path = destination / "serving_recipes_live_designator.toml"
    path.write_text(f"{head}[[profiles]]\n{body}\n{tail}", encoding="utf-8")
    return path


def _chain(root: Path, catalogue: Path, *extra: str) -> None:
    for program in (DOOR_CLI, EXEMPLAR_CLI, INK_MAP_CLI):
        result = subprocess.run(
            [
                sys.executable,
                str(program),
                "--run-root",
                str(root),
                "--run-id",
                RUN_ID,
                "--scenario",
                "happy",
                "--serving-recipes-config",
                str(catalogue),
                *extra,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program.name}: {result.stderr}"


@pytest.fixture(scope="module")
def chained_run(tmp_path_factory) -> tuple[Path, Path]:
    """One Door-through-Ink-Map tree under the live catalogue, copied per test."""
    base = tmp_path_factory.mktemp("live-designator")
    catalogue = _live_catalogue(base)
    root = base / "runs"
    _chain(root, catalogue)
    return root, catalogue


@pytest.fixture()
def live_run(chained_run, tmp_path: Path) -> tuple[Path, Path]:
    template, catalogue = chained_run
    root = tmp_path / "runs"
    shutil.copytree(template, root)
    return root, catalogue


@pytest.fixture(scope="module")
def real_template(tmp_path_factory) -> Path:
    """One real submission of the fixture pages, carried to the Ink Map's seal
    under the committed fixture catalogue."""
    base = tmp_path_factory.mktemp("real-designator-template")
    approved = base / "approved-storage"
    source = approved / "submitted-pages"
    source.mkdir(parents=True)
    for name in ("page-1.png", "page-2.png"):
        shutil.copyfile(FIXTURE_PAGES / name, source / name)
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(approved)]
    policy_path = base / "data-gate-policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    ledger = approved / "submission-ledger.json"
    submit.submit(source, ledger, policy_path=policy_path)
    root = approved / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(DOOR_CLI),
            "--run-root",
            str(root),
            "--run-id",
            RUN_ID,
            "--submission-folder",
            str(source),
            "--submission-manifest",
            str(ledger),
            "--data-gate-policy",
            str(policy_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    for program in (EXEMPLAR_CLI, INK_MAP_CLI):
        result = subprocess.run(
            [sys.executable, str(program), "--run-root", str(root), "--run-id", RUN_ID],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program.name}: {result.stderr}"
    return root


# --- driving the stage -----------------------------------------------------------


class _TreeBlobs:
    """`FakeEndpoint`'s response-as-arrival probe, pointed at the real run tree."""

    def __init__(self, root: Path) -> None:
        self._tree = RunTree(root, RUN_ID)

    def has(self, sha256: str) -> bool:
        return self._tree.resolve(self._tree.blob_path(DESIGNATOR, sha256)).exists()


def _serving_factory(
    endpoint: FakeEndpoint, catalogue: Path, log_root: Path, lock: Path, decoding: Path
):
    """The `(context, chair, tier) -> ChairClient` seam `main` injects against.

    Deliberately close to `structure_pass.default_serving_factory`: the same
    manager, the same real `StageContextReceiptPublisher`, the same
    `retain_chair_bytes` into the stage's own blob area, the same receipt
    re-read through the tree. Only the launcher, the transport and the package
    inspector are fakes -- the three things that would otherwise need a card.
    """
    policy, decoding_sha256 = load_decoding_policy(decoding)
    recipes = load_serving_recipes(catalogue)

    def factory(context, chair, tier) -> ChairClient:
        manager = ServingManager(
            registry=FakeRegistry({chair.role: chair}, log_root),
            recipes=recipes,
            config_inputs=ServingConfigInputs.from_record(dict(context.serving_config_inputs)),
            launcher=FakeLauncher(endpoint),
            http=endpoint,
            receipt_publisher=StageContextReceiptPublisher(context),
            log_root=log_root,
            package_inspector=FakePackages({"vllm": "0.test"}),
            residency_lease=FileResidencyLease(lock),
        )
        return ChairClient(
            manager=manager,
            identity=chair,
            tier=tier,
            retain=lambda data: structure_pass.retain_chair_bytes(context, data),
            decoding_config_sha256=decoding_sha256,
            record_temperature=structure_pass.executable_temperature(policy),
            read_receipt=lambda reference: context.tree.read_run_receipt(dict(reference)),
        )

    return factory


def _argv(root: Path, catalogue: Path, *extra: str) -> list[str]:
    return [
        str(ROOT / "pipeline" / "2_designator" / "run.py"),
        "--run-root",
        str(root),
        "--run-id",
        RUN_ID,
        "--scenario",
        "happy",
        "--serving-recipes-config",
        str(catalogue),
        *extra,
    ]


def _run_designator(
    root: Path,
    catalogue: Path,
    tmp_path: Path,
    monkeypatch,
    answers: list[ScriptedAnswer],
    *,
    argv: tuple[str, ...] = ("--placement-tier", TIER),
    decoding: Path = ROOT / "config" / "decoding.toml",
) -> tuple[FakeEndpoint, int]:
    """Run the real stage in this process against a scripted endpoint."""
    endpoint = FakeEndpoint(
        served_model_id=SERVED_MODEL_ID,
        blob_store=_TreeBlobs(root),
        assert_retained_before_next_request=True,
    )
    endpoint.script(*answers)
    factory = _serving_factory(
        endpoint, catalogue, tmp_path / "logs", tmp_path / "pod-gpu.lock", decoding
    )
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        _argv(root, catalogue, *argv, "--decoding-config", str(decoding)),
    )
    return endpoint, designator.main(serving_factory=factory)


def _answer(acts, page_w: int = 200, page_h: int = 260, **fields: Any) -> ScriptedAnswer:
    """One scripted structure answer in Chandra's layout grammar.

    Built by the shared fake's own builder and read back through
    `common/chandra_layout.py` before this file sees it, so a fixture whose
    rectangles do not survive the round trip fails in the builder rather than
    as an unexplained hold three stages downstream. The normalized boxes come
    from `operations/serving/fakes.py`'s `structure_box_1000`, which searches
    the 0-1000 grid and checks every candidate through
    `common.structure_answer.to_page_bounds` itself. This file used to carry a
    second private copy of that search (`_box`), which is exactly the drift the
    one shared builder exists to prevent -- two inverses of one converter,
    either free to stop agreeing with it.
    """
    return scripted_structure_answer(acts, page_w, page_h, **fields)


def _blank_page_answer(**fields: Any) -> ScriptedAnswer:
    """The chair's answer for a page it read as blank: one `Blank-Page` block.

    Under the retired JSON contract this was `{"acts": []}`. The vendor grammar
    has no empty-list shape -- an answer with no `<div>` in it is refused as
    `no-layout-blocks` rather than read as an empty page -- and `Blank-Page` is
    the label the carried prompt itself offers for exactly this.
    """
    return scripted_structure_answer(
        (), 200, 260, body=structure_blank_page_body(), expect_proposals=(), **fields
    )


def _happy_answers() -> list[ScriptedAnswer]:
    return [_answer(PAGE_ONE_ACTS), _answer(PAGE_TWO_ACTS)]


# --- reading the tree back ---------------------------------------------------------


def _artifacts(root: Path, stage: str, kind: str) -> list[dict[str, Any]]:
    tree = RunTree(root, RUN_ID)
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind
    ]


def _by_page_ordinal(records: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {record["payload"]["page_ordinal"]: record for record in records}


def _seal(root: Path) -> dict[str, Any]:
    (seal,) = _artifacts(root, DESIGNATOR, "proposal-seal")
    return seal


def _open(root: Path, catalogue: Path, stage: str, *extra: str):
    args = stage_parser("structure pass test").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            RUN_ID,
            "--scenario",
            "happy",
            "--serving-recipes-config",
            str(catalogue),
            *extra,
        ]
    )
    return open_stage_context(args, stage)


def _receipts(root: Path) -> list[dict[str, Any]]:
    directory = root / RUN_ID / RECEIPTS_DIR
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.rglob("*.json"))
    ]


def _designator_artifact_text(root: Path) -> str:
    """Every byte of every Designator artifact, for the no-text grep."""
    directory = root / RUN_ID / "2_designator" / "artifacts"
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(directory.rglob("*.json")))


# --- the selector: the sealed row kind, and nothing else ---------------------------


def _mode_arguments(catalogue: Path, tier: str | None):
    placement = ROOT / "config" / "pod_placement.toml"
    context = SimpleNamespace(
        serving_config_inputs={
            "schema": "serving-config-inputs.v1",
            "serving_recipes_sha256": digest_bytes(Path(catalogue).read_bytes()),
            "pod_placement_sha256": digest_bytes(placement.read_bytes()),
        },
        registry=ChairRegistry.from_toml(str(MODELS_CONFIG)),
    )
    args = SimpleNamespace(serving_recipes_config=str(catalogue), placement_tier=tier)
    return context, args


def test_the_committed_fixture_catalogue_selects_the_fixture_pass():
    """No run that seals the committed catalogue can reach the live pass."""
    for tier in (None, TIER):
        context, args = _mode_arguments(FIXTURE_CATALOGUE, tier)
        mode, identity = structure_pass.structure_serving_mode(context, args)
        assert mode == "fixture"
        assert identity.role == "designator_structure"


def test_a_live_row_selects_the_live_pass_only_with_the_measured_tier(chained_run):
    _root, catalogue = chained_run
    context, args = _mode_arguments(catalogue, TIER)
    assert structure_pass.structure_serving_mode(context, args)[0] == "live"
    _, no_tier = _mode_arguments(catalogue, None)
    with pytest.raises(ContractError, match="placement-tier"):
        structure_pass.structure_serving_mode(context, no_tier)


def test_a_catalogue_that_is_not_the_sealed_one_is_refused(chained_run, tmp_path: Path):
    _root, catalogue = chained_run
    substitute = tmp_path / "substituted.toml"
    substitute.write_bytes(Path(catalogue).read_bytes() + b"\n# a byte that moved\n")
    context, args = _mode_arguments(catalogue, TIER)
    args.serving_recipes_config = str(substitute)
    with pytest.raises(ContractError, match="serving configuration was refused"):
        structure_pass.structure_serving_mode(context, args)


# --- the page-identity pin (SPEC_D §5) ----------------------------------------------


def test_every_sealed_fixture_page_subject_equals_the_fixture_derived_identity(chained_run):
    """`pages[n]["subject_id"]` and `page_identity(fixture, n)` are one string.

    The fixture act loop names a sealed page by the Exemplar's own subject now,
    and this is the equality that keeps every fixture seal row byte-identical.
    """
    root, _catalogue = chained_run
    fixture = load_fixture(str(ROOT / "proof"))
    pages = [
        record for record in _artifacts(root, EXEMPLAR, "page") if record["outcome"] == "sealed"
    ]
    assert len(pages) == 2
    for page in pages:
        assert page["subject_id"] == page_identity(fixture, page["payload"]["ordinal"])


# --- a live pass, end to end -------------------------------------------------------


def test_a_live_pass_mints_the_chairs_rectangles_and_the_seal_verifies_downstream(
    live_run, tmp_path, monkeypatch
):
    root, catalogue = live_run
    endpoint, exit_code = _run_designator(root, catalogue, tmp_path, monkeypatch, _happy_answers())

    assert exit_code == EXIT_COMPLETE
    # One whole-page call per sealed page: one `user` turn and no system turn
    # (v2, matching Chandra's own inference code), the native RGB/grid-28 image
    # as the one image, and no generation knobs of the stage's own.
    assert len(endpoint.requests) == 2
    for request in endpoint.requests:
        messages = request["messages"]
        assert [message["role"] for message in messages] == ["user"]
        # Image block first, then the instruction -- upstream Chandra's own
        # trained order, and the token sequence the chat template emits.
        assert messages[0]["content"][0]["type"] == "image_url"
        assert messages[0]["content"][1]["type"] == "text"
        # Chandra's own 12,384-token bound against what this row leaves: the
        # fixture pages are 200x260 and cost 48 image tokens, the prompt is the
        # measured 593 (the carried vendor prompt, v3), and the live row states
        # `max_model_len` 4,096. The row is what binds, so no bound goes on the
        # wire and the engine's own budget -- the same quantity, measured by
        # the component that holds the tokenizer -- governs, exactly as before.
        assert DECLARED_ANSWER_BOUND_TOKENS["designator_structure"] > 4096 - 48 - 593
        assert "max_tokens" not in request
        # Thinking mode closed, whichever of the two shipped chat templates the
        # engine resolves (`common/chair_wire.py`).
        assert request["chat_template_kwargs"] == {"enable_thinking": False}
        assert request["temperature"] == 1
    tree = RunTree(root, RUN_ID)
    request_images = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_REQUEST_IMAGE_KIND))
    assert set(request_images) == {1, 2}

    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    assert set(answers) == {1, 2}
    for ordinal, expected in ((1, PAGE_ONE_ACTS), (2, PAGE_TWO_ACTS)):
        payload = answers[ordinal]["payload"]
        assert payload["schema"] == STRUCTURE_ANSWER_RECORD_SCHEMA_V3
        assert payload["parse_state"] == "parsed"
        assert payload["parse_outcome"] is None
        assert payload["disposition"] == "detected"
        assert payload["finish_reason"] == "stop"
        assert payload["served_model_id"] == SERVED_MODEL_ID
        assert payload["act_count"] == len(expected)
        assert [act["raw_bounds"] for act in payload["acts"]] == [b for b, _t in expected]
        assert [act["text_digest"] for act in payload["acts"]] == [
            structure_answer.text_digest(text) for _b, text in expected
        ]
        assert payload["findings"] == []
        assert payload["decoding"]["policy"] == "structure"
        assert payload["decoding"]["temperature"] == 1
        assert payload["prompt_version"] == "verbatus-structure-prompt.v3"
        # Every block the answer carried became a proposal, so the record's own
        # denominator reconciles with nothing left over.
        assert payload["block_count"] == len(expected)
        assert payload["blocks_without_proposal"] == []
        assert payload["answer_schema"] == "chandra-layout-html.v1"
        assert payload["text_view"] == "chandra-layout-text.v1"
        assert payload["vendor"]["repository"] == "github.com/datalab-to/chandra"
        assert payload["vendor"]["commit"] == "d4f7467435aa4137d9539f000ddf0b7ced3eb43f"
        presentation = tree.read_artifact_reference(
            payload["presentation_ref"],
            stage=DESIGNATOR,
            kind=STRUCTURE_REQUEST_IMAGE_KIND,
            subject_id=payload["page_id"],
        )
        evidence = presentation["payload"]
        assert evidence["schema"] == STRUCTURE_REQUEST_IMAGE_SCHEMA
        assert evidence["source_image_ref"]["sha256"] != evidence["presented"]["image_sha256"]
        assert evidence["presented"]["transform"]["bounds"] == {
            "x": 0,
            "y": 0,
            "w": 200,
            "h": 260,
        }
        resize = evidence["presented"]["transform"]["resize"]
        assert (resize["target_width_px"], resize["target_height_px"]) == (196, 252)
        assert payload["capacity"]["images"][0]["width"] == 196
        assert payload["capacity"]["images"][0]["height"] == 252
        request_url = endpoint.requests[ordinal - 1]["messages"][0]["content"][0]["image_url"][
            "url"
        ]
        request_bytes = base64.b64decode(request_url.partition(",")[2])
        assert dimensions(request_bytes) == (196, 252)
        assert digest_bytes(request_bytes) == evidence["presented"]["image_sha256"]
        # These fixtures declare no `data-label`, so the vendor parser's own
        # `block` default is what the vocabulary field carries, `label_declared`
        # is false, and the digest and length stay null -- "the chair offered no
        # label", spelled honestly.
        assert [act["label_vocabulary"] for act in payload["acts"]] == ["block"] * len(expected)
        assert not any(act["label_declared"] for act in payload["acts"])
        assert all(act["label_digest"] is None for act in payload["acts"])
        assert all(act["nested_bbox_count"] == 0 for act in payload["acts"])
        # The retained bytes, the custody binding and the call record all exist
        # under the digests the record names.
        for name in ("raw_response_ref", "custody_ref", "call_record_ref"):
            reference = payload[name]
            data = tree.read_bytes(reference["relative_path"])
            assert digest_bytes(data) == reference["sha256"], name
        call_record = json.loads(tree.read_bytes(payload["call_record_ref"]["relative_path"]))
        assert call_record["schema"] == CHAIR_CALL_RECORD_SCHEMA
        assert call_record["chair"] == "designator_structure"
        assert (
            call_record["decoding_config_sha256"] == payload["decoding"]["decoding_config_sha256"]
        )
        assert call_record["image_sha256s"] == [evidence["presented"]["image_sha256"]]
        # Response-as-arrival: the retained blob is the exact wire body.
        assert (
            json.loads(tree.read_bytes(payload["raw_response_ref"]["relative_path"]))["model"]
            == SERVED_MODEL_ID
        )
        # The posture the consumer will check (D3): the same engine call on the
        # answer, the status and the seal.
        assert payload["provenance"]["engine_call"]["decoding_policy"] == "structure"

    statuses = _by_page_ordinal(_artifacts(root, DESIGNATOR, "structure-status"))
    for ordinal in (1, 2):
        payload = statuses[ordinal]["payload"]
        assert payload["state"] == "scanned"
        assert payload["structure_evidence"] == "detected"
        assert payload["structure_answer_ref"]["relative_path"].endswith(
            answers[ordinal]["artifact_id"] + ".json"
        )

    regions = _artifacts(root, DESIGNATOR, "region")
    by_key = {record["payload"]["act_key"]: record for record in regions}
    assert set(by_key) == {"proposal:1:0", "proposal:1:1", "proposal:2:0"}
    expected_bounds = {
        "proposal:1:0": PAGE_ONE_ACTS[0][0],
        "proposal:1:1": PAGE_ONE_ACTS[1][0],
        "proposal:2:0": PAGE_TWO_ACTS[0][0],
    }
    for key, record in by_key.items():
        payload = record["payload"]
        assert payload["raw_bounds"] == expected_bounds[key]
        assert payload["origin"] == "proposal"
        assert payload["padding"] is not None
        verify_identity(
            record["subject_id"],
            "act",
            act_bindings(payload["transform"]["source_page_id"], "proposal", payload["raw_bounds"]),
        )
        assert payload["provenance"]["engine_call"]["schema"] == "structure-chair-call.v1"

    groups = {
        record["payload"]["act_key"]: record["payload"]
        for record in _artifacts(root, DESIGNATOR, "act-group")
    }
    assert set(groups) == set(by_key)
    for key, payload in groups.items():
        assert payload["declared_bounds"] == expected_bounds[key]
        assert payload["continuation"] is None
    # The fixture's own ink lies under the scripted rectangles, so the scan
    # corroborates every one of them independently.
    assert {payload["structure_evidence"] for payload in groups.values()} == {"detected"}

    seal = _seal(root)
    rows = seal["payload"]["expected_acts"]
    assert [row["act_key"] for row in rows] == ["proposal:1:0", "proposal:1:1", "proposal:2:0"]
    assert {row["outcome"] for row in rows} == {"proposed"}
    assert seal["payload"]["provenance"]["engine_call"]["call_kind"] == "chat-completions"

    # No `fixture://` receipt anywhere: the one receipt is the served chair's.
    receipts = _receipts(root)
    assert [receipt["chair"] for receipt in receipts] == ["designator_structure"]
    assert not receipts[0]["endpoint"].startswith("fixture://")

    # No act text in any Designator artifact (SPEC_D §4); the custody blob is
    # the one permitted home for it.
    artifacts_text = _designator_artifact_text(root)
    for text in SCRIPTED_TEXTS:
        assert text not in artifacts_text
    assert any(
        text in tree.read_bytes(answers[1]["payload"]["raw_response_ref"]["relative_path"]).decode()
        for text in SCRIPTED_TEXTS[:2]
    )

    # The consumer-side verifier (D3) accepts every row against the answer it
    # came from, at the very boundary the Attestatores open the run under.
    acts = expected_acts(_open(root, catalogue, ATTESTATORES, "--placement-tier", TIER))
    assert [row["act_key"] for row in acts] == [row["act_key"] for row in rows]


def test_the_attestatores_read_a_live_seal_under_their_own_fixture_rows(
    live_run, tmp_path, monkeypatch
):
    """Every witness keeps its own pass.

    The Attestatores stage is untouched by this unit: it runs as the real
    program over a tree the live Designator produced, under the same catalogue
    (its own rows still fixture), and nothing in it reaches the structure
    chair -- the fake endpoint saw exactly the Designator's two page calls and
    no other, and no Attestatores record carries a structure-chair call.
    """
    root, catalogue = live_run
    endpoint, exit_code = _run_designator(root, catalogue, tmp_path, monkeypatch, _happy_answers())
    assert exit_code == EXIT_COMPLETE
    result = subprocess.run(
        [
            sys.executable,
            str(ATTESTATORES_CLI),
            "--run-root",
            str(root),
            "--run-id",
            RUN_ID,
            "--scenario",
            "happy",
            "--serving-recipes-config",
            str(catalogue),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode in (EXIT_COMPLETE, EXIT_HELD), result.stderr
    assert len(endpoint.requests) == 2
    testimonia = _artifacts(root, ATTESTATORES, "testimonium")
    assert testimonia
    for record in testimonia:
        provenance = record["payload"]["provenance"]
        assert provenance["chair"] != "designator_structure"
        assert "engine_call" not in provenance
    # The Attestatores' production client still binds `reading_of_record`, not
    # the structure section -- pinned at the source, since this unit does not
    # own that file and must not have moved it.
    source = ATTESTATORES_CLI.read_text(encoding="utf-8")
    assert 'record_temperature=policy["reading_of_record"]["temperature"]' in source
    assert '["structure"]' not in source


# --- what each answer does to the page (SPEC_D §1.4) ---------------------------------


def test_a_blank_page_answer_cuts_the_page_into_fallback_tiles(live_run, tmp_path, monkeypatch):
    root, catalogue = live_run
    _endpoint, exit_code = _run_designator(
        root, catalogue, tmp_path, monkeypatch, [_answer(PAGE_ONE_ACTS), _blank_page_answer()]
    )
    assert exit_code == EXIT_COMPLETE
    statuses = _by_page_ordinal(_artifacts(root, DESIGNATOR, "structure-status"))
    assert statuses[2]["payload"]["state"] == "scanned"
    assert statuses[2]["payload"]["structure_evidence"] == "fallback-tiles"
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    assert answers[2]["payload"]["disposition"] == "fallback-tiles"
    assert answers[2]["payload"]["act_count"] == 0
    # The `Blank-Page` block is *retained*, not dropped: it is the one block the
    # answer carried, it proposed nothing for a named reason, and the grammar's
    # own finding says the vendor would have discarded it here
    # (`chandra_layout`'s second departure). A page the chair declared blank is
    # therefore distinguishable in the record from a page it never answered.
    payload = answers[2]["payload"]
    assert payload["block_count"] == 1
    assert [block["reason"] for block in payload["blocks_without_proposal"]] == ["blank-page"]
    assert payload["blocks_without_proposal"][0]["blank_page"] is True
    assert payload["blocks_without_proposal"][0]["label_vocabulary"] == "Blank-Page"
    assert [f["kind"] for f in payload["findings"]] == ["blank-page-retained"]
    (fallback,) = _artifacts(root, DESIGNATOR, "page-fallback")
    assert fallback["payload"]["page_ordinal"] == 2
    assert fallback["payload"]["page_bounds"] == {"x": 0, "y": 0, "w": 200, "h": 260}
    # Live wording, not the fixture sentence: page 2's own ink scan grouped
    # ink (proven above by "detected" on the happy path), so the record must
    # name what actually happened here -- the chair returned no act -- rather
    # than claim the scan found nothing.
    assert fallback["payload"]["reason"] == designator._FALLBACK_REASON_LIVE
    rows = {row["act_key"]: row for row in _seal(root)["payload"]["expected_acts"]}
    assert rows["page-fallback:2"]["outcome"] == "proposed"
    assert len(rows["page-fallback:2"]["evidence"]) == fallback["payload"]["tile_count"]
    acts = expected_acts(_open(root, catalogue, ATTESTATORES, "--placement-tier", TIER))
    assert sorted(row["act_key"] for row in acts) == sorted(rows)


# --- blocks that propose nothing, and the findings that say why -----------------


def _mixed_block_answer() -> ScriptedAnswer:
    """One page whose four blocks exercise all three no-proposal reasons.

    Ordinal 0 places; 1 declares a `data-bbox` nobody can read; 2 declares none
    at all; 3 is a `Blank-Page`. One page, so the ordinals in the record, in
    `blocks_without_proposal` and in every finding are all in the same
    namespace and can be joined without a second numbering scheme.
    """
    blocks = [
        structure_layout_block(
            bounds=PAGE_TWO_ACTS[0][0], page_w=200, page_h=260, text="placed", label="Text"
        ),
        structure_layout_block(data_bbox="ten 20 30 40", text="unreadable box", label="Text"),
        structure_layout_block(text="no box at all", label="Text"),
        structure_layout_block(data_bbox="0 0 1000 1000", text="", label="Blank-Page"),
    ]
    return scripted_structure_answer(
        (),
        200,
        260,
        body="\n".join(blocks),
        expect_proposals=(PAGE_TWO_ACTS[0][0],),
    )


def test_a_block_with_no_usable_box_is_recorded_and_never_minted(live_run, tmp_path, monkeypatch):
    """The rule the vendor's own parser breaks, held to end to end.

    `chandra/output.py::parse_layout` prints "defaulting to full image" and
    substitutes `[0, 0, 1, 1]` for a bbox it cannot read -- a rectangle a few
    pixels wide in the page's corner, published as though the model had drawn
    it, with the print going to a stdout nobody retains. Here the block is kept
    with its geometry unresolved, the reason named, and **no crop cut from it**:
    the run mints exactly the one rectangle that resolved, and the three that
    did not are in the record with the reason each fell out for.

    The blank block is in the same answer on purpose. `Blank-Page` and
    `malformed-bbox` both reach `block_page_bounds` returning `None`, and this
    is what says the record still tells them apart -- a page reported as blank
    is a different fact from a box nobody could read, and folding either into
    the other would be a reading substituted for a measurement.
    """
    root, catalogue = live_run
    _endpoint, exit_code = _run_designator(
        root, catalogue, tmp_path, monkeypatch, [_answer(PAGE_ONE_ACTS), _mixed_block_answer()]
    )
    assert exit_code == EXIT_COMPLETE
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    payload = answers[2]["payload"]
    assert payload["disposition"] == "detected"
    # Four blocks came back; one proposed, and the other three are named.
    assert payload["block_count"] == 4
    assert payload["act_count"] == 1
    assert [act["ordinal"] for act in payload["acts"]] == [0]
    assert [(b["ordinal"], b["reason"]) for b in payload["blocks_without_proposal"]] == [
        (1, "malformed-bbox"),
        (2, "no-bbox"),
        (3, "blank-page"),
    ]
    # Each unminted block keeps its own text as a digest and a length, so what
    # the chair wrote there is provable against the retained bytes without the
    # record carrying a word of it.
    assert [b["text_length"] for b in payload["blocks_without_proposal"]] == [14, 13, 0]
    assert all(b["text_digest"] for b in payload["blocks_without_proposal"])
    # The findings name the same ordinals, and the malformed box is published as
    # a digest -- never as the bytes the chair wrote into the attribute. Both
    # `malformed-bbox` findings are here: the grammar raises one for a box it
    # could not read *and* one for a box that was never there, and a record
    # keeping only the last would drop a block's whole account of itself while
    # still counting the block.
    assert [(f["kind"], f["ordinal"]) for f in payload["findings"]] == [
        ("malformed-bbox", 1),
        ("malformed-bbox", 2),
        ("blank-page-retained", 3),
    ]
    unreadable, absent, _blank = payload["findings"]
    assert unreadable["data_bbox_digest"] == structure_answer.text_digest("ten 20 30 40")
    assert unreadable["data_bbox_truncated"] is False
    assert unreadable["reason"] == "components are not plain decimal integers"
    # A null digest is the structural signal for "the chair drew no box at all",
    # told apart from "it drew one nobody could read" without reading the reason.
    assert absent["data_bbox_digest"] is None
    assert absent["reason"] == "no data-bbox attribute"
    assert "ten 20 30 40" not in json.dumps(payload)
    # And exactly one crop was cut on the page: the block that placed.
    on_page_two = [
        record["payload"]
        for record in _artifacts(root, DESIGNATOR, "region")
        if record["payload"]["transform"]["source_page_ordinal"] == 2
    ]
    assert [region["act_key"] for region in on_page_two] == ["proposal:2:0"]
    assert on_page_two[0]["raw_bounds"] == dict(PAGE_TWO_ACTS[0][0])
    assert not _artifacts(root, DESIGNATOR, "page-fallback")


def test_a_page_whose_every_block_failed_to_place_is_tiled_and_says_so(
    live_run, tmp_path, monkeypatch
):
    """No rectangle proposed, so the page is covered by predetermined crops.

    The alternative would be holding it, which costs every act on the page
    until a reviewer looks (goal 2: a missed act is worse than a poorly read
    one). What `fallback-tiles` claims is "no rectangle was proposed", and that
    is true here -- so the disposition is honest and the record carries the
    part it does not say: two blocks came back, both unplaceable, with their
    reasons and their text digests beside them. A reader can tell this page
    from a blank one without opening the blob.
    """
    root, catalogue = live_run
    unplaceable = "\n".join(
        [
            structure_layout_block(data_bbox="9 9", text="too few components", label="Text"),
            structure_layout_block(data_bbox="600 600 100 100", text="inverted", label="Text"),
        ]
    )
    _endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [
            _answer(PAGE_ONE_ACTS),
            scripted_structure_answer((), 200, 260, body=unplaceable, expect_proposals=()),
        ],
    )
    assert exit_code == EXIT_COMPLETE
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    payload = answers[2]["payload"]
    assert payload["disposition"] == "fallback-tiles"
    assert (payload["block_count"], payload["act_count"]) == (2, 0)
    assert [b["reason"] for b in payload["blocks_without_proposal"]] == ["malformed-bbox"] * 2
    # The two boxes failed for two different reasons, and the grammar's own
    # refusal reason says which -- arity against ordering, not one word for both.
    reasons = [f["reason"] for f in payload["findings"] if f["kind"] == "malformed-bbox"]
    assert reasons == ["expected 4 space-separated components, found 2", "x1 <= x0 or y1 <= y0"]
    (fallback,) = _artifacts(root, DESIGNATOR, "page-fallback")
    assert fallback["payload"]["page_ordinal"] == 2
    statuses = _by_page_ordinal(_artifacts(root, DESIGNATOR, "structure-status"))
    assert statuses[2]["payload"]["structure_evidence"] == "fallback-tiles"


def test_ink_the_answer_wrote_outside_every_block_is_counted_and_named(
    live_run, tmp_path, monkeypatch
):
    """The finding that stops a missed act arriving under a successful status.

    A model that answered one act as a top-level `<p>`, or wrote a line between
    two divs, produces words that reach no block, no page text and no span --
    and, without this, a page that parses clean, mints its other rectangles and
    reports `findings == []`. goal 2 rates that worst and principle 2 forbids
    it. The count travels, not the text: the words are in the retained bytes,
    and a Designator artifact publishes no reading of them.
    """
    root, catalogue = live_run
    stray = "an act the chair wrote outside every block"
    body = "\n".join(
        [
            structure_layout_block(
                bounds=PAGE_TWO_ACTS[0][0], page_w=200, page_h=260, text="placed", label="Text"
            ),
            f"<p>{stray}</p>",
        ]
    )
    _endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [
            _answer(PAGE_ONE_ACTS),
            scripted_structure_answer(
                (), 200, 260, body=body, expect_proposals=(PAGE_TWO_ACTS[0][0],)
            ),
        ],
    )
    assert exit_code == EXIT_COMPLETE
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    payload = answers[2]["payload"]
    assert payload["disposition"] == "detected"
    (finding,) = [f for f in payload["findings"] if f["kind"] == "content-outside-blocks"]
    # Non-whitespace characters, which is what the text view would have read.
    assert finding["characters"] == len(stray.replace(" ", ""))
    assert stray not in json.dumps(payload)
    # The block that did place is unaffected: the finding names a gap, it does
    # not repair one (principle 3).
    assert payload["act_count"] == 1


# --- a held page ----------------------------------------------------------------


def _assert_page_two_held(root: Path, reason_code: str) -> None:
    statuses = _by_page_ordinal(_artifacts(root, DESIGNATOR, "structure-status"))
    assert statuses[2]["payload"]["state"] == "held"
    assert statuses[2]["payload"]["reason_code"] == reason_code
    assert statuses[2]["payload"]["structure_evidence"] is None
    assert "structure_answer_ref" in statuses[2]["payload"]
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    assert answers[2]["outcome"] == "held"
    assert answers[2]["payload"]["disposition"] == "held"
    assert answers[2]["payload"]["reason_code"] == reason_code
    # A held page is not tiled and nothing is cut on it; its ink reconciles as
    # conservation residual, held from the moment it exists.
    regions = _artifacts(root, DESIGNATOR, "region")
    assert {r["payload"]["transform"]["source_page_ordinal"] for r in regions} == {1}
    assert not _artifacts(root, DESIGNATOR, "page-fallback")
    rows = {row["act_key"]: row for row in _seal(root)["payload"]["expected_acts"]}
    assert "residual:2:0" in rows
    assert rows["residual:2:0"]["outcome"] == "held"


def test_the_repetition_measure_separates_the_live_runs_looping_pages():
    """The floor is set from four real RecordGold pages from a live run.

    Two completed and two spent their whole context on one repeated fragment.
    The measured shares were 0.004 and 0.005 against 0.067 and 0.305, so the
    floor sits an order of magnitude clear of both sides. These registers do
    repeat their formulae, and a page of genuine repeated phrasing must keep
    reading as a page.
    """
    healthy = (
        '<div data-bbox="1 2 3 4" data-label="Text"><p>'
        + ("Le dixieme jour d'Aoust mil sept cent vingt deux a este baptise " * 12)
        + "</p></div>"
    )
    assert structure_pass.repetition_share(healthy) < structure_pass.DEGENERATE_REPETITION_SHARE

    # Page 1's actual shape: a fragment emitted until the context ran out.
    looped = (
        "<div><p>Le Septieme Aoust mil sept cens vingt deux a este inhume"
        + (". J" * 4000)
        + "</p></div>"
    )
    assert structure_pass.repetition_share(looped) >= structure_pass.DEGENERATE_REPETITION_SHARE

    # Page 3's shape: a whole phrase rather than two characters.
    phrase = "<div><p>" + ("de l'Eglise, " * 1600) + "</p></div>"
    assert structure_pass.repetition_share(phrase) >= structure_pass.DEGENERATE_REPETITION_SHARE

    # A positive control at each end. One character repeated makes every window
    # identical and scores exactly 1.0. A fifteen-character phrase repeated
    # instead yields fifteen distinct windows and scores about 0.07 -- which is
    # page 3's own magnitude, its `de l\'Église, ` being thirteen characters,
    # and why the floor sits well under that rather than near 1.
    assert structure_pass.repetition_share("a" * 600) == 1.0
    phrase_share = structure_pass.repetition_share("abcdefghijklmno" * 40)
    assert structure_pass.DEGENERATE_REPETITION_SHARE < phrase_share < 0.1

    # Too short to ask the question of. The floor is a count of windows, and
    # an answer under it is not judged either way.
    assert structure_pass.repetition_share("aaaa") == 0.0
    assert structure_pass.repetition_share("ab" * 20) == 0.0


def test_a_looping_answer_is_held_as_degenerate_not_as_a_cut_off():
    """A loop and a page too dense to finish want opposite repairs.

    Reporting the loop as a cut-off sends a reader to the token budget, which
    is the wrong place to look: the retained responses showed
    `. J. J. J.` 5,376 times.
    """
    looped = ". J" * 4000
    dense = "".join(
        f'<div data-bbox="1 {i} 3 4" data-label="Text"><p>acte numero {i} du registre</p></div>'
        for i in range(60)
    )
    assert structure_pass._finish_reason_disposition("length", looped) == (
        structure_pass.HELD_DEGENERATE
    )
    assert structure_pass._finish_reason_disposition("length", dense) == (
        structure_pass.HELD_CUT_OFF
    )
    # No answer to read is still a cut-off, never a guess at degeneration.
    assert structure_pass._finish_reason_disposition("length", None) == structure_pass.HELD_CUT_OFF
    # A completed answer is this chair's word for the page, whatever it repeats.
    assert structure_pass._finish_reason_disposition("stop", looped) is None


def test_a_cut_off_answer_holds_the_page_even_though_it_parsed(live_run, tmp_path, monkeypatch):
    root, catalogue = live_run
    endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [_answer(PAGE_ONE_ACTS), _answer(PAGE_TWO_ACTS, finish_reason="length")],
    )
    assert exit_code == EXIT_HELD
    _assert_page_two_held(root, "structure-answer-cut-off")
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    payload = answers[2]["payload"]
    assert payload["parse_state"] == "parsed"
    assert payload["finish_reason"] == "length"
    assert payload["act_count"] == 1
    # The bytes are retained under custody whatever the page's disposition.
    tree = RunTree(root, RUN_ID)
    retained = tree.read_bytes(payload["raw_response_ref"]["relative_path"])
    assert digest_bytes(retained) == payload["raw_response_ref"]["sha256"]
    assert len(endpoint.requests) == 2


def test_an_invalid_structure_answer_gets_one_bounded_coverage_retry(
    live_run, tmp_path, monkeypatch
):
    root, catalogue = live_run
    endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [
            _answer(PAGE_ONE_ACTS),
            scripted_structure_refusal("no-layout-blocks"),
            _answer(PAGE_TWO_ACTS),
        ],
    )
    assert exit_code == EXIT_COMPLETE
    assert len(endpoint.requests) == 3
    attempts = [
        row
        for row in _artifacts(root, DESIGNATOR, "structure-attempt")
        if row["subject_id"]
        == _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))[2]["subject_id"]
    ]
    assert len(attempts) == 2
    attempts.sort(key=lambda row: row["payload"]["attempt_ordinal"])
    assert [request["seed"] for request in endpoint.requests] == [0, 0, 1]
    assert [row["payload"]["attempt_seed"] for row in attempts] == [0, 1]
    assert len({row["payload"]["request_sha256"] for row in attempts}) == 2
    assert attempts[0]["payload"]["attempts"] == []
    final = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))[2]["payload"]
    assert final["attempt_ordinal"] == 2 and len(final["attempts"]) == 2
    assert attempts[1]["payload"]["attempts"] == final["attempts"][:1]


def test_resume_keeps_the_published_attempt_and_uses_its_next_deterministic_seed(
    live_run, tmp_path, monkeypatch
):
    root, catalogue = live_run
    original = designator._recoverable_structure_outcome

    def interrupt_after_first_refusal(answer):
        if answer.reason_code == "structure-answer-no-layout-blocks":
            raise RuntimeError("interrupted after first refused attempt")
        return original(answer)

    with monkeypatch.context() as patcher:
        patcher.setattr(
            designator,
            "_recoverable_structure_outcome",
            interrupt_after_first_refusal,
        )
        with pytest.raises(RuntimeError, match="first refused attempt"):
            _run_designator(
                root,
                catalogue,
                tmp_path,
                patcher,
                [_answer(PAGE_ONE_ACTS), scripted_structure_refusal("no-layout-blocks")],
            )

    before = [
        row
        for row in _artifacts(root, DESIGNATOR, "structure-attempt")
        if row["payload"]["page_ordinal"] == 2
    ]
    assert len(before) == 1
    endpoint, exit_code = _run_designator(
        root, catalogue, tmp_path, monkeypatch, [_answer(PAGE_TWO_ACTS)]
    )
    assert exit_code == EXIT_COMPLETE
    assert [request["seed"] for request in endpoint.requests] == [1]
    attempts = [
        row
        for row in _artifacts(root, DESIGNATOR, "structure-attempt")
        if row["payload"]["page_ordinal"] == 2
    ]
    attempts.sort(key=lambda row: row["payload"]["attempt_ordinal"])
    assert attempts[0] == before[0]
    assert [row["payload"]["attempt_seed"] for row in attempts] == [0, 1]


def test_resume_terminalizes_a_retained_nonretryable_attempt_without_starting_a_chair(
    live_run, tmp_path, monkeypatch
):
    root, catalogue = live_run
    original = designator._publish_structure_answer

    def interrupt_after_page_two(context, page_record, answer):
        if answer.ordinal == 2:
            raise RuntimeError("interrupted after immutable attempt")
        return original(context, page_record, answer)

    with monkeypatch.context() as patcher:
        patcher.setattr(designator, "_publish_structure_answer", interrupt_after_page_two)
        with pytest.raises(RuntimeError, match="interrupted after immutable attempt"):
            _run_designator(
                root,
                catalogue,
                tmp_path,
                patcher,
                [_answer(PAGE_ONE_ACTS), _answer(PAGE_TWO_ACTS, finish_reason="length")],
            )

    attempts_before = _artifacts(root, DESIGNATOR, "structure-attempt")
    endpoint, exit_code = _run_designator(root, catalogue, tmp_path, monkeypatch, [])
    assert exit_code == EXIT_HELD
    assert endpoint.requests == []
    assert _artifacts(root, DESIGNATOR, "structure-attempt") == attempts_before
    final = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))[2]["payload"]
    assert final["attempt_ordinal"] == len(final["attempts"]) == 1


def test_legacy_structure_answer_v1_resumes_without_attempt_fields(tmp_path, monkeypatch):
    catalogue = _live_catalogue(tmp_path)
    decoding = tmp_path / "legacy-decoding.toml"
    decoding.write_text(
        'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 0\n'
        '[variance_experiment]\nlabel = "variance.v1"\nseed = 20260820\npasses = 2\n'
        "[structure]\ntemperature = 1\n",
        encoding="utf-8",
    )
    root = tmp_path / "legacy-runs"
    _chain(root, catalogue, "--decoding-config", str(decoding))
    original = designator._publish_structure_answer

    def interrupt_after_legacy_source(context, page_record, answer):
        reference = original(context, page_record, answer)
        if answer.ordinal == 1:
            raise RuntimeError("convert completed page to legacy fixture")
        return reference

    with monkeypatch.context() as patcher:
        patcher.setattr(designator, "_publish_structure_answer", interrupt_after_legacy_source)
        with pytest.raises(RuntimeError, match="legacy fixture"):
            _run_designator(
                root,
                catalogue,
                tmp_path,
                patcher,
                [_answer(PAGE_ONE_ACTS)],
                decoding=decoding,
            )

    tree = RunTree(root, RUN_ID)
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    answer_row = answers[1]
    attempt_row = next(
        row
        for row in _artifacts(root, DESIGNATOR, "structure-attempt")
        if row["subject_id"] == answer_row["subject_id"]
    )
    answer_path = tree.resolve(
        tree.artifact_path(DESIGNATOR, STRUCTURE_ANSWER_KIND, answer_row["artifact_id"])
    )
    envelope = json.loads(answer_path.read_text(encoding="utf-8"))
    payload = envelope["payload"]
    payload["schema"] = STRUCTURE_ANSWER_RECORD_SCHEMA
    for field in (
        "attempt_ordinal",
        "attempts",
        "attempt_seed",
        "attempt_policy",
        "presentation_ref",
    ):
        payload.pop(field)
    envelope["self_hash"] = self_hash(envelope)
    answer_path.write_bytes(canonical_bytes(envelope))

    tree.resolve(
        tree.artifact_path(DESIGNATOR, "structure-attempt", attempt_row["artifact_id"])
    ).unlink()

    endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [_answer(PAGE_TWO_ACTS)],
        decoding=decoding,
    )
    assert exit_code == EXIT_COMPLETE
    assert len(endpoint.requests) == 1
    resumed = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    assert resumed[1]["payload"]["schema"] == STRUCTURE_ANSWER_RECORD_SCHEMA
    assert "attempts" not in resumed[1]["payload"]
    assert resumed[2]["payload"]["schema"] == STRUCTURE_ANSWER_RECORD_SCHEMA_V3
    assert resumed[2]["payload"]["attempt_policy"] == {
        "max_attempts": 1,
        "seed_schedule": "fixed-base",
    }


def test_structure_answer_v2_resume_keeps_its_direct_page_presentation(
    live_run, tmp_path, monkeypatch
):
    """A sealed v2 attempt remains readable without acquiring v3 evidence fields."""
    root, catalogue = live_run
    original = designator._publish_structure_answer

    def interrupt_after_first_page(context, page_record, answer):
        reference = original(context, page_record, answer)
        if answer.ordinal == 1:
            raise RuntimeError("convert completed page to v2 fixture")
        return reference

    with monkeypatch.context() as patcher:
        patcher.setattr(designator, "_publish_structure_answer", interrupt_after_first_page)
        with pytest.raises(RuntimeError, match="v2 fixture"):
            _run_designator(
                root,
                catalogue,
                tmp_path,
                patcher,
                [_answer(PAGE_ONE_ACTS)],
            )

    tree = RunTree(root, RUN_ID)
    answer_row = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))[1]
    attempt_row = next(
        row
        for row in _artifacts(root, DESIGNATOR, "structure-attempt")
        if row["subject_id"] == answer_row["subject_id"]
    )
    attempt_path = tree.resolve(
        tree.artifact_path(DESIGNATOR, "structure-attempt", attempt_row["artifact_id"])
    )
    attempt_envelope = json.loads(attempt_path.read_text(encoding="utf-8"))
    presentation_path = attempt_envelope["payload"]["presentation_ref"]["relative_path"]
    call_record = json.loads(
        tree.read_bytes(attempt_envelope["payload"]["call_record_ref"]["relative_path"])
    )
    source_ref = attempt_envelope["inputs"][0]
    source_bytes = tree.read_bytes(source_ref["relative_path"])
    source_width, source_height = dimensions(source_bytes)
    current_capacity = attempt_envelope["payload"]["capacity"]
    legacy_capacity = structure_pass.page_capacity(
        SimpleNamespace(
            **{
                key: current_capacity[key]
                for key in (
                    "recipe",
                    "chair",
                    "tier",
                    "max_model_len",
                    "min_pixels",
                    "max_pixels",
                    "patch_size",
                    "merge_size",
                )
            }
        ),
        source_width,
        source_height,
    )
    legacy_request = structure_pass.page_request(
        source_bytes,
        source_ref["sha256"],
        temperature=attempt_envelope["payload"]["decoding"]["temperature"],
        capacity=legacy_capacity,
    )
    temperature = attempt_envelope["payload"]["decoding"]["temperature"]
    request_sha256 = digest_bytes(
        request_body(
            {
                **legacy_request.generation_sent,
                "messages": list(legacy_request.messages),
            },
            model_id=call_record["served_model_id"],
            seed=call_record["generation_sent"]["seed"],
            deterministic=temperature == 0,
            temperature=temperature,
        )
    )
    call_record["image_sha256s"] = [source_ref["sha256"]]
    call_record["request_sha256"] = request_sha256
    call_record["capacity"] = legacy_capacity
    call_digest, call_blob = tree.put_blob(DESIGNATOR, canonical_bytes(call_record))
    legacy_call_ref = {"relative_path": call_blob.relative_path, "sha256": call_digest}
    attempt_envelope["payload"]["schema"] = STRUCTURE_ANSWER_RECORD_SCHEMA_V2
    attempt_envelope["payload"].pop("presentation_ref")
    attempt_envelope["payload"]["call_record_ref"] = legacy_call_ref
    attempt_envelope["payload"]["request_sha256"] = request_sha256
    attempt_envelope["payload"]["capacity"] = legacy_capacity
    attempt_envelope["inputs"] = attempt_envelope["inputs"][:1]
    attempt_envelope["self_hash"] = self_hash(attempt_envelope)
    attempt_bytes = canonical_bytes(attempt_envelope)
    attempt_path.write_bytes(attempt_bytes)
    attempt_ref = {
        "relative_path": tree.artifact_path(
            DESIGNATOR, "structure-attempt", attempt_row["artifact_id"]
        ),
        "sha256": digest_bytes(attempt_bytes),
    }

    answer_path = tree.resolve(
        tree.artifact_path(
            DESIGNATOR,
            STRUCTURE_ANSWER_KIND,
            answer_row["artifact_id"],
        )
    )
    answer_envelope = json.loads(answer_path.read_text(encoding="utf-8"))
    answer_envelope["payload"]["schema"] = STRUCTURE_ANSWER_RECORD_SCHEMA_V2
    answer_envelope["payload"].pop("presentation_ref")
    answer_envelope["payload"]["call_record_ref"] = legacy_call_ref
    answer_envelope["payload"]["request_sha256"] = request_sha256
    answer_envelope["payload"]["capacity"] = legacy_capacity
    answer_envelope["payload"]["attempts"] = [attempt_ref]
    answer_envelope["inputs"] = answer_envelope["inputs"][:1]
    answer_envelope["self_hash"] = self_hash(answer_envelope)
    answer_path.write_bytes(canonical_bytes(answer_envelope))
    tree.resolve(presentation_path).unlink()

    endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [_answer(PAGE_TWO_ACTS)],
    )
    assert exit_code == EXIT_COMPLETE
    assert len(endpoint.requests) == 1
    resumed = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    assert resumed[1]["payload"]["schema"] == STRUCTURE_ANSWER_RECORD_SCHEMA_V2
    assert "presentation_ref" not in resumed[1]["payload"]
    assert resumed[2]["payload"]["schema"] == STRUCTURE_ANSWER_RECORD_SCHEMA_V3


@pytest.mark.parametrize("damage", ["missing", "forged", "out-of-order", "extra"])
def test_structure_attempt_consumer_refuses_missing_forged_or_out_of_order_history(
    damage, monkeypatch
):
    monkeypatch.setattr(stage_contract, "validate_serving_provenance", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        stage_contract,
        "verify_structure_attempt_call",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        stage_contract,
        "load_decoding_policy",
        lambda _path: (
            {
                "schema": "decoding.v2",
                "reading_of_record": {"temperature": 0},
                "variance_experiment": {"label": "v", "seed": 1, "passes": 2},
                "structure": {
                    "temperature": 1,
                    "recovery_seed_schedule": "base-plus-attempt-ordinal-minus-one",
                    "recovery_max_attempts": 3,
                },
            },
            "d" * 64,
        ),
    )
    page_id = "page_" + "1" * 16
    policy = {
        "max_attempts": 3,
        "seed_schedule": "base-plus-attempt-ordinal-minus-one",
    }
    decoding = {
        "policy": "structure",
        "temperature": 1,
        "decoding_config_sha256": "d" * 64,
    }
    first_ref = {"relative_path": "2_designator/a1.json", "sha256": "1" * 64}
    second_ref = {"relative_path": "2_designator/a2.json", "sha256": "2" * 64}
    first = {
        "schema": STRUCTURE_ANSWER_RECORD_SCHEMA_V2,
        "page_id": page_id,
        "page_ordinal": 1,
        "attempt_ordinal": 1,
        "attempt_seed": 7,
        "attempt_policy": policy,
        "attempts": [],
        "decoding": decoding,
    }
    second = {
        **first,
        "attempt_ordinal": 2,
        "attempt_seed": 8,
        "attempts": [first_ref],
    }
    rows = {
        first_ref["relative_path"]: {
            "attempt_id": designator.attempt_id(page_id, "structure", 1),
            "subject_id": page_id,
            "payload": first,
        },
        second_ref["relative_path"]: {
            "attempt_id": designator.attempt_id(page_id, "structure", 2),
            "subject_id": page_id,
            "payload": second,
        },
    }
    monkeypatch.setattr(stage_contract, "_stage_records", lambda *_args: list(rows.values()))

    class FakeTree:
        def read_artifact_reference(self, reference, **_expected):
            try:
                return rows[reference["relative_path"]]
            except KeyError as error:
                raise SchemaRefusal("missing attempt") from error

    terminal = {**second, "attempts": [first_ref, second_ref]}
    context = SimpleNamespace(tree=FakeTree(), args=SimpleNamespace(decoding_config="unused"))
    # Positive control: every damage case starts from this accepted exact chain.
    stage_contract._verify_structure_attempt_chain(context, terminal, page_id)
    expected = {
        "missing": "no bounded exact attempt ledger",
        "forged": "does not bind its identity",
        "out-of-order": "does not bind its identity",
        "extra": "stage manifest contains 3",
    }[damage]
    if damage == "missing":
        terminal["attempts"] = [first_ref]
    elif damage == "forged":
        rows[second_ref["relative_path"]] = {
            **rows[second_ref["relative_path"]],
            "attempt_id": designator.attempt_id("page_" + "2" * 16, "structure", 2),
        }
    else:
        if damage == "out-of-order":
            terminal["attempts"] = [second_ref, first_ref]
        else:
            rows["2_designator/a3.json"] = {
                "attempt_id": designator.attempt_id(page_id, "structure", 3),
                "subject_id": page_id,
                "payload": {
                    **second,
                    "attempt_ordinal": 3,
                    "attempt_seed": 9,
                    "attempts": [first_ref, second_ref],
                },
            }

    with pytest.raises(FatalAccounting, match=expected):
        stage_contract._verify_structure_attempt_chain(context, terminal, page_id)


def test_real_denominator_indexes_structure_attempts_and_decoding_once(monkeypatch):
    pages = ["page_" + "1" * 16, "page_" + "2" * 16]
    answers = [
        {"subject_id": page_id, "payload": {"schema": STRUCTURE_ANSWER_RECORD_SCHEMA_V2}}
        for page_id in pages
    ]
    attempt_rows = [{"subject_id": page_id, "payload": {}} for page_id in pages]
    calls = {"answers": 0, "attempts": 0, "decoding": 0}

    def stage_records(_tree, _stage, kind):
        if kind == STRUCTURE_ANSWER_KIND:
            calls["answers"] += 1
            return answers
        if kind == stage_contract.STRUCTURE_ATTEMPT_KIND:
            calls["attempts"] += 1
            return attempt_rows
        raise AssertionError(f"unexpected stage-record kind {kind!r}")

    sealed = ({"structure": {}}, "d" * 64)

    def load_decoding(_path):
        calls["decoding"] += 1
        return sealed

    received = []

    def verify_chain(_context, _payload, page_id, *, attempts_by_page, sealed_decoding):
        received.append((page_id, attempts_by_page, sealed_decoding))

    monkeypatch.setattr(stage_contract, "_stage_records", stage_records)
    monkeypatch.setattr(stage_contract, "load_decoding_policy", load_decoding)
    monkeypatch.setattr(stage_contract, "_verify_structure_attempt_chain", verify_chain)
    monkeypatch.setattr(stage_contract, "_designator_records_by_subject", lambda *_args: {})
    monkeypatch.setattr(stage_contract, "exemplar_page_ids", lambda _context: {})
    monkeypatch.setattr(stage_contract, "_verify_minted_act_rows", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        stage_contract,
        "_verify_every_conservation_residual_is_accounted",
        lambda *_args, **_kw: None,
    )

    context = SimpleNamespace(tree=object(), args=SimpleNamespace(decoding_config="unused"))
    stage_contract._verify_real_act_denominator(context, [], {})

    assert calls == {"answers": 1, "attempts": 1, "decoding": 1}
    assert [item[0] for item in received] == pages
    assert received[0][1] is received[1][1]
    assert received[0][2] is received[1][2] is sealed
    assert {page_id: rows for page_id, rows in received[0][1].items()} == {
        page_id: [row] for page_id, row in zip(pages, attempt_rows, strict=True)
    }


@pytest.mark.parametrize("case", ["prior-retry", "held", "fallback"])
def test_shared_attempt_call_verifier_refuses_a_digest_valid_call_from_another_attempt(
    live_run, tmp_path, monkeypatch, case
):
    root, catalogue = live_run
    if case == "prior-retry":
        answers = [
            _answer(PAGE_ONE_ACTS),
            scripted_structure_refusal("no-layout-blocks"),
            _answer(PAGE_TWO_ACTS),
        ]
    elif case == "held":
        answers = [
            _answer(PAGE_ONE_ACTS),
            scripted_prompt_too_long(
                max_model_len=2048,
                requested_tokens=3619,
                prompt_tokens=2044,
                completion_tokens=1575,
            ),
        ]
    else:
        answers = [_blank_page_answer(), _answer(PAGE_TWO_ACTS)]
    _endpoint, _exit_code = _run_designator(root, catalogue, tmp_path, monkeypatch, answers)
    attempts = _artifacts(root, DESIGNATOR, "structure-attempt")
    if case == "prior-retry":
        same_page = sorted(
            (row for row in attempts if row["payload"]["page_ordinal"] == 2),
            key=lambda row: row["payload"]["attempt_ordinal"],
        )
        target, foreign = same_page
    elif case == "held":
        target = next(row for row in attempts if row["payload"]["page_ordinal"] == 2)
        foreign = next(row for row in attempts if row["payload"]["page_ordinal"] == 1)
    else:
        target = next(row for row in attempts if row["payload"]["page_ordinal"] == 1)
        foreign = next(row for row in attempts if row["payload"]["page_ordinal"] == 2)
    forged = {**target["payload"], "call_record_ref": foreign["payload"]["call_record_ref"]}
    context = _open(root, catalogue, ATTESTATORES, "--placement-tier", TIER)
    with pytest.raises(ContractError, match="retained call record|response evidence"):
        stage_contract.verify_structure_attempt_call(
            context,
            forged,
            target["subject_id"],
            attempt_inputs=target["inputs"],
        )


@pytest.mark.parametrize("mismatch", ["image", "temperature"])
def test_v3_attempt_refuses_a_digest_valid_call_with_wrong_image_or_temperature(
    live_run, tmp_path, monkeypatch, mismatch
):
    root, catalogue = live_run
    _endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        _happy_answers(),
    )
    assert exit_code == EXIT_COMPLETE
    target = _by_page_ordinal(_artifacts(root, DESIGNATOR, "structure-attempt"))[1]
    tree = RunTree(root, RUN_ID)
    call = json.loads(tree.read_bytes(target["payload"]["call_record_ref"]["relative_path"]))
    context = _open(root, catalogue, ATTESTATORES, "--placement-tier", TIER)
    stage_contract.verify_structure_attempt_call(
        context,
        target["payload"],
        target["subject_id"],
        attempt_inputs=target["inputs"],
    )
    if mismatch == "image":
        source_ref = target["inputs"][0]
        assert call["image_sha256s"] != [source_ref["sha256"]]
        call["image_sha256s"] = [source_ref["sha256"]]
    else:
        expected_temperature = target["payload"]["decoding"]["temperature"]
        assert call["generation_sent"]["temperature"] == expected_temperature
        # Keep the forged call inside the canonical wire vocabulary: floats
        # are refused before the attempt verifier can test the mismatch.
        call["generation_sent"]["temperature"] = 0 if expected_temperature != 0 else 1
    digest, blob = tree.put_blob(DESIGNATOR, canonical_bytes(call))
    forged = {
        **target["payload"],
        "call_record_ref": {"relative_path": blob.relative_path, "sha256": digest},
    }
    with pytest.raises(ContractError, match="disagrees with its retained call record"):
        stage_contract.verify_structure_attempt_call(
            context,
            forged,
            target["subject_id"],
            attempt_inputs=target["inputs"],
        )


@pytest.mark.parametrize("case", ["prior-retry", "held", "fallback"])
def test_downstream_consumer_verifies_every_attempt_including_nonproposal_pages(
    live_run, tmp_path, monkeypatch, case
):
    root, catalogue = live_run
    if case == "prior-retry":
        answers = [
            _answer(PAGE_ONE_ACTS),
            scripted_structure_refusal("no-layout-blocks"),
            _answer(PAGE_TWO_ACTS),
        ]
        target_ordinal = 2
        expected_verifications = 2
    elif case == "held":
        answers = [
            _answer(PAGE_ONE_ACTS),
            scripted_prompt_too_long(
                max_model_len=2048,
                requested_tokens=3619,
                prompt_tokens=2044,
                completion_tokens=1575,
            ),
        ]
        target_ordinal = 2
        expected_verifications = 1
    else:
        answers = [_blank_page_answer(), _answer(PAGE_TWO_ACTS)]
        target_ordinal = 1
        expected_verifications = 1
    _endpoint, _exit_code = _run_designator(root, catalogue, tmp_path, monkeypatch, answers)
    target_page = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))[
        target_ordinal
    ]["subject_id"]
    verified: list[str] = []
    original = stage_contract.verify_structure_attempt_call

    def record_verification(context, payload, page_id, **kwargs):
        verified.append(page_id)
        return original(context, payload, page_id, **kwargs)

    monkeypatch.setattr(
        stage_contract,
        "verify_structure_attempt_call",
        record_verification,
    )
    expected_acts(_open(root, catalogue, ATTESTATORES, "--placement-tier", TIER))
    assert verified.count(target_page) == expected_verifications


@pytest.mark.parametrize("outcome", ["no-layout-blocks", "blocks-not-at-top-level"])
def test_an_answer_the_grammar_refuses_holds_the_page_by_its_outcome(
    live_run, tmp_path, monkeypatch, outcome
):
    """The two refusals an answer's *shape* can reach, and they are told apart.

    `no-layout-blocks` is an answer with no `<div>` in it at all -- prose, most
    likely. `blocks-not-at-top-level` is an answer that does have divs, every
    one nested inside a wrapper the vendor's own `recursive=False` would find
    nothing in either. Only the second is a model that answered in the right
    grammar and the wrong envelope, and telling them apart is what lets a first
    real reading say which happened without a person opening the blob.

    The grammar's other four outcomes are properties of the wire bytes rather
    than of a body a `ScriptedAnswer` can carry, and they are measured where
    they happen, in `common/test_chandra_layout.py`
    (`operations/serving/fakes.py::_STRUCTURE_REFUSALS` names the split).
    """
    root, catalogue = live_run
    code = f"structure-answer-{outcome}"
    _endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [
            _answer(PAGE_ONE_ACTS),
            scripted_structure_refusal(outcome),
            scripted_structure_refusal(outcome),
            scripted_structure_refusal(outcome),
        ],
    )
    assert exit_code == EXIT_HELD
    _assert_page_two_held(root, code)
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    payload = answers[2]["payload"]
    assert payload["parse_state"] == "refused"
    assert payload["parse_outcome"] == outcome
    assert payload["acts"] == [] and payload["act_count"] == 0
    # A refused answer read no blocks, so both denominators are zero and
    # neither list can be carrying a block the other one lost.
    assert payload["block_count"] == 0
    assert payload["blocks_without_proposal"] == []
    attempts = [
        row
        for row in _artifacts(root, DESIGNATOR, "structure-attempt")
        if row["subject_id"] == answers[2]["subject_id"]
    ]
    attempts.sort(key=lambda row: row["payload"]["attempt_ordinal"])
    assert [row["payload"]["attempt_seed"] for row in attempts] == [0, 1, 2]
    assert [row["payload"]["attempt_ordinal"] for row in attempts] == [1, 2, 3]
    assert payload["attempt_ordinal"] == len(payload["attempts"]) == 3
    last_reference = payload["attempts"][-1]
    tree = RunTree(root, RUN_ID)
    assert (
        digest_bytes(tree.read_bytes(last_reference["relative_path"])) == last_reference["sha256"]
    )
    assert attempts[-1]["payload"] == {**payload, "attempts": payload["attempts"][:-1]}


def test_a_truncated_body_holds_as_cut_off_even_though_the_grammar_reads_it(
    live_run, tmp_path, monkeypatch
):
    """The cut-off stop word wins over the parse outcome: SPEC_D S1.4 places

    the `finish_reason in ENGINE_STOP_CUT_OFF` row above the parse-refusal
    rows, and it applies "parsed or not". A body the engine truncated
    mid-block is exactly the failure this measurement exists to name --
    the small `max_model_len` a whole-page transcription can overrun -- and
    it must not be recorded as a refusal of the chair's shape, which would
    blame the answer rather than the context window.

    Under Chandra's layout grammar the truncated body *does* read: an unclosed
    block is closed and its bytes kept, with an `unclosed-block` finding
    (`chandra_layout`'s `finish`). So this now proves the stronger half of the
    row -- the page is held on the stop word while its answer parsed cleanly
    enough to have proposed a rectangle -- which the retired JSON contract
    could not distinguish, since a cut object was also invalid JSON.
    """
    root, catalogue = live_run
    truncated = structure_answer_body(PAGE_TWO_ACTS, 200, 260)
    truncated = truncated[: truncated.index("</div>")]
    _endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [_answer(PAGE_ONE_ACTS), ScriptedAnswer(content=truncated, finish_reason="length")],
    )
    assert exit_code == EXIT_HELD
    _assert_page_two_held(root, "structure-answer-cut-off")
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    payload = answers[2]["payload"]
    assert payload["parse_state"] == "parsed"
    assert payload["finish_reason"] == "length"
    # The block it did send is recorded in full, and nothing was minted from it.
    assert payload["act_count"] == 1
    assert [f["kind"] for f in payload["findings"]] == ["unclosed-block"]
    assert not _artifacts(root, DESIGNATOR, "page-fallback")


def test_an_unrecognized_stop_word_over_a_body_that_does_not_parse_is_still_refused_by_name(
    live_run, tmp_path, monkeypatch
):
    """The unnameable stop word is fatal whether or not the body parsed --

    not folded silently into `structure-answer-no-layout-blocks` just because
    the body also happened to carry no block the grammar could read.
    """
    root, catalogue = live_run
    with pytest.raises(ContractError, match="finish_reason 'abort'"):
        _run_designator(
            root,
            catalogue,
            tmp_path,
            monkeypatch,
            [
                _answer(PAGE_ONE_ACTS),
                ScriptedAnswer(content="no layout block at all", finish_reason="abort"),
            ],
        )
    # The refused page has no record at all: an unnameable stop word is the
    # run's refusal, never that page's published outcome. Page 1 is a different
    # page's fact and keeps the answer it got -- it is published as it arrives,
    # so a call this run already paid for stays visible and a resume reuses it
    # instead of asking again (principle 2, and the Designator's resume rule).
    assert sorted(_by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))) == [1]


def test_a_body_the_client_cannot_read_holds_the_page_as_unusable(live_run, tmp_path, monkeypatch):
    root, catalogue = live_run
    body = json.dumps({"model": SERVED_MODEL_ID, "choices": []}).encode()
    _endpoint, exit_code = _run_designator(
        root, catalogue, tmp_path, monkeypatch, [_answer(PAGE_ONE_ACTS), ScriptedAnswer(body=body)]
    )
    assert exit_code == EXIT_HELD
    _assert_page_two_held(root, "structure-call-unusable")
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    payload = answers[2]["payload"]
    assert payload["parse_state"] == "refused"
    assert payload["parse_outcome"] is None
    assert payload["call_problem"] is not None
    assert payload["finish_reason"] is None


# --- the ink tripwire runs at the page's own threshold ------------------------


def _photographed_page():
    """A page shaped like a photographed leaf: a dark surround, paper spread
    over forty-five grey levels with its peak at the top of them, and one small
    mark of real ink in the middle.

    Built here rather than taken from a fixture because every fixture page in
    this repository has flat paper and a 140-level separation, which is exactly
    the shape that hid this defect: at flat paper the floor margin and the
    page's own margin select the identical pixels.
    """
    width, height = 200, 160
    rows = [bytearray([0] * width) for _ in range(height)]
    for y in range(20, height - 20):
        row = rows[y]
        for x in range(20, width - 20):
            # Paper over sixty grey levels with its peak at 220. The `min` is
            # what gives the population a mode: a flat spread would leave the
            # inferred paper decided by whichever value the argmax reached
            # first, and this test would be about tie-breaking.
            row[x] = min(220, 175 + ((x * 7 + y * 11) % 65))
    for y in range(70, 80):
        for x in range(90, 110):
            rows[y][x] = 30  # the only writing on the page
    return width, height, rows


def _analysis_at(margin: int):
    import structure

    width, height, rows = _photographed_page()
    import grouping_config

    policy = grouping_config.resolve_background_policy(
        grouping_config.load_grouping_config(
            Path(__file__).resolve().parents[2] / "config" / "designator_grouping.toml"
        ),
        width,
        height,
    )
    evidence = structure.infer_background_evidence(width, height, rows, background_policy=policy)
    resolved = evidence["ink_margin"] if margin is None else margin
    return evidence, {
        "background": evidence["background"],
        "ink_margin": resolved,
        "rows": rows,
        "components": structure.label_components(
            structure.ink_pixels(
                width, height, rows, background=evidence["background"], margin=resolved
            ),
            gap_tolerance_px=3,
        ),
    }


def test_the_ink_tripwire_tests_a_rectangle_at_the_pages_own_threshold():
    """The reader's finding, pinned.

    At the floor margin the threshold sits inside this page's own paper
    population, so a rectangle over blank paper touches "ink", every rectangle a
    chair could draw touches ink, and the `model-only` signal cannot fire at all
    -- it is true by construction rather than by measurement. At the margin the
    page derives for itself the paper is paper: a rectangle over blank paper
    returns False and one over the writing returns True.

    The margin is taken from the shipped inference rather than written out, so
    this is the threshold a run would actually use on a page of this shape.
    """
    designator = load_designator("designator_touches_ink")
    blank = {"x": 30, "y": 30, "w": 40, "h": 20}
    writing = {"x": 88, "y": 68, "w": 24, "h": 14}

    evidence, derived = _analysis_at(None)
    assert evidence["source"] == "inferred-interior-mode"
    assert derived["ink_margin"] > designator.structure.PRIMARY_MARGIN

    _evidence, at_floor = _analysis_at(designator.structure.PRIMARY_MARGIN)
    assert designator.structure_pass.touches_ink(blank, at_floor) is True
    assert designator.structure_pass.touches_ink(writing, at_floor) is True

    assert designator.structure_pass.touches_ink(blank, derived) is False
    assert designator.structure_pass.touches_ink(writing, derived) is True


def test_the_ink_tripwire_returns_false_on_a_page_with_no_background():
    """`ink_margin` is `None` exactly where `background` is, and the background
    check above it is what stops the subtraction from ever seeing that `None`."""
    designator = load_designator("designator_touches_ink_unmeasured")
    analysis = {"background": None, "ink_margin": None, "rows": [], "components": []}
    assert (
        designator.structure_pass.touches_ink({"x": 0, "y": 0, "w": 5, "h": 5}, analysis) is False
    )


def test_rectangles_touching_none_of_the_scanned_ink_hold_the_page(live_run, tmp_path, monkeypatch):
    """The coordinate-space tripwire: the scan found ink, the chair drew on paper."""
    root, catalogue = live_run
    blank_strip = {"x": 20, "y": 102, "w": 160, "h": 14}  # between the two acts on page 1
    _endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [_answer(((blank_strip, "nothing is written here"),)), _answer(PAGE_TWO_ACTS)],
    )
    assert exit_code == EXIT_HELD
    statuses = _by_page_ordinal(_artifacts(root, DESIGNATOR, "structure-status"))
    assert statuses[1]["payload"]["state"] == "held"
    assert statuses[1]["payload"]["reason_code"] == "structure-answer-no-ink-overlap"
    assert statuses[2]["payload"]["state"] == "scanned"
    regions = _artifacts(root, DESIGNATOR, "region")
    assert {r["payload"]["transform"]["source_page_ordinal"] for r in regions} == {2}
    rows = {row["act_key"] for row in _seal(root)["payload"]["expected_acts"]}
    assert "proposal:2:0" in rows
    assert any(key.startswith("residual:1:") for key in rows)


def test_a_rectangle_that_touches_ink_is_not_tripped_by_one_that_does_not(
    live_run, tmp_path, monkeypatch
):
    """Not a threshold: one rectangle on ink is enough, and the paper one is minted too."""
    root, catalogue = live_run
    blank_strip = {"x": 20, "y": 102, "w": 160, "h": 14}
    _endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [_answer((PAGE_ONE_ACTS[0], (blank_strip, "margin"))), _answer(PAGE_TWO_ACTS)],
    )
    groups = {
        record["payload"]["act_key"]: record["payload"]["structure_evidence"]
        for record in _artifacts(root, DESIGNATOR, "act-group")
    }
    assert groups["proposal:1:0"] == "detected"
    assert groups["proposal:1:1"] == "model-only"
    # Act two's ink is unclaimed, so it is a held residual, not a lost act.
    assert exit_code == EXIT_HELD
    rows = {row["act_key"] for row in _seal(root)["payload"]["expected_acts"]}
    assert any(key.startswith("residual:1:") for key in rows)


def test_two_rectangles_over_one_ink_group_are_shared_detection_on_both(
    live_run, tmp_path, monkeypatch
):
    root, catalogue = live_run
    upper = {"x": 20, "y": 120, "w": 160, "h": 50}
    lower = {"x": 20, "y": 170, "w": 160, "h": 50}
    _endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [
            _answer((PAGE_ONE_ACTS[0], (upper, "upper half"), (lower, "lower half"))),
            _answer(PAGE_TWO_ACTS),
        ],
    )
    groups = {
        record["payload"]["act_key"]: record["payload"]
        for record in _artifacts(root, DESIGNATOR, "act-group")
    }
    assert groups["proposal:1:0"]["structure_evidence"] == "detected"
    assert groups["proposal:1:1"]["structure_evidence"] == "shared-detection"
    assert groups["proposal:1:2"]["structure_evidence"] == "shared-detection"
    assert groups["proposal:1:1"]["detected_bounds"] == groups["proposal:1:2"]["detected_bounds"]
    assert exit_code == EXIT_COMPLETE


def test_a_duplicate_rectangle_mints_once_and_is_recorded_as_a_finding(
    live_run, tmp_path, monkeypatch
):
    root, catalogue = live_run
    first, second = PAGE_ONE_ACTS
    _endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [_answer((first, (first[0], "the same act again"), second)), _answer(PAGE_TWO_ACTS)],
    )
    assert exit_code == EXIT_COMPLETE
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    payload = answers[1]["payload"]
    assert payload["act_count"] == 3
    assert payload["findings"] == [{"kind": "duplicate-rectangle", "ordinals": [0, 1]}]
    keys = sorted(row["act_key"] for row in _seal(root)["payload"]["expected_acts"])
    assert keys == ["proposal:1:0", "proposal:1:2", "proposal:2:0"]
    acts = expected_acts(_open(root, catalogue, ATTESTATORES, "--placement-tier", TIER))
    assert len(acts) == 3


def test_an_unrecognized_engine_stop_word_is_refused_by_name(live_run, tmp_path, monkeypatch):
    root, catalogue = live_run
    with pytest.raises(ContractError, match="finish_reason 'abort'"):
        _run_designator(
            root,
            catalogue,
            tmp_path,
            monkeypatch,
            [_answer(PAGE_ONE_ACTS, finish_reason="abort"), _answer(PAGE_TWO_ACTS)],
        )
    assert not _artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND)


def test_a_prompt_too_long_400_is_refused_by_name_and_never_read_as_a_cut_off(
    live_run, tmp_path, monkeypatch
):
    """The failure SPEC_D 7 names, in the shape vLLM actually gives it.

    `scripted_structure_cut_off` scripts the *answer*-side truncation: HTTP 200,
    `finish_reason="length"`, a body cut mid-object. A prompt too long for the
    context is a different wire event -- HTTP 400, no choices at all -- and the
    two must not be read as one thing: a 400 read as a length stop would hold
    the page as `structure-answer-cut-off`, asserting that a chair answered and
    was cut off when no chair answered at all.

    The paid response becomes one immutable, terminal held attempt. It is not
    retried: HTTP and source-integrity failures are evidence to retain, not
    structural coverage variants to sample around.
    """

    root, catalogue = live_run
    refusal = scripted_prompt_too_long(
        max_model_len=2048,
        requested_tokens=3619,
        prompt_tokens=2044,
        completion_tokens=1575,
    )
    endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [_answer(PAGE_ONE_ACTS), refusal],
    )
    assert exit_code == EXIT_HELD
    assert len(endpoint.requests) == 2
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    payload = answers[2]["payload"]
    assert payload["reason_code"] == structure_pass.HELD_CALL_UNUSABLE
    assert payload["call_problem"] == "CHAIR_RESPONSE_HTTP_ERROR"
    assert payload["finish_reason"] is None
    assert payload["attempt_ordinal"] == len(payload["attempts"]) == 1
    assert payload["call_record_ref"] is not None
    assert payload["raw_response_ref"] is not None
    # The bytes reached disk before the refusal was raised: the engine's own
    # account of why it refused is the artefact a rented card exists to
    # produce, and it used to be discarded here.
    tree = RunTree(root, RUN_ID)
    retained = tree.read_bytes(tree.blob_path(DESIGNATOR, digest_bytes(refusal.body)))
    assert b"maximum context length is 2048" in retained
    call_record = json.loads(tree.read_bytes(payload["call_record_ref"]["relative_path"]))
    assert call_record["response_status"] == 400
    assert call_record["parse_problem"] == "CHAIR_RESPONSE_HTTP_ERROR"


def test_a_wrong_model_response_is_one_terminal_attempt_with_observed_model_retained(
    live_run, tmp_path, monkeypatch
):
    root, catalogue = live_run
    endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [
            _answer(PAGE_ONE_ACTS),
            ScriptedAnswer(
                content="a foreign reading that cannot become structure",
                finish_reason="stop",
                model="foreign-model",
            ),
        ],
    )
    assert exit_code == EXIT_HELD
    assert len(endpoint.requests) == 2
    payload = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))[2]["payload"]
    assert payload["reason_code"] == structure_pass.HELD_CALL_UNUSABLE
    assert payload["call_problem"] == "CHAIR_RESPONSE_MODEL_MISMATCH"
    assert payload["attempt_ordinal"] == len(payload["attempts"]) == 1
    tree = RunTree(root, RUN_ID)
    call_record = json.loads(tree.read_bytes(payload["call_record_ref"]["relative_path"]))
    assert call_record["response_status"] == 200
    assert call_record["served_model_id"] == SERVED_MODEL_ID
    assert call_record["response_model"] == "foreign-model"
    assert call_record["parse_problem"] == "CHAIR_RESPONSE_MODEL_MISMATCH"


def test_a_transport_timeout_is_retained_once_and_resume_never_resends_it(
    live_run, tmp_path, monkeypatch
):
    root, catalogue = live_run
    original = designator._publish_structure_answer

    def interrupt_before_timeout_terminal(context, page_record, answer):
        if answer.ordinal == 2:
            raise RuntimeError("interrupted after timeout attempt")
        return original(context, page_record, answer)

    with monkeypatch.context() as patcher:
        patcher.setattr(
            designator,
            "_publish_structure_answer",
            interrupt_before_timeout_terminal,
        )
        with pytest.raises(RuntimeError, match="timeout attempt"):
            _run_designator(
                root,
                catalogue,
                tmp_path,
                patcher,
                [
                    _answer(PAGE_ONE_ACTS),
                    ScriptedAnswer(transport_failure="whole-call deadline exceeded"),
                ],
            )
    attempts_before = _artifacts(root, DESIGNATOR, "structure-attempt")
    timeout_attempts = [row for row in attempts_before if row["payload"]["page_ordinal"] == 2]
    assert len(timeout_attempts) == 1
    attempt = timeout_attempts[0]["payload"]
    assert attempt["reason_code"] == structure_pass.HELD_CALL_UNUSABLE
    assert attempt["call_problem"] == "CHAIR_TRANSPORT_FAILURE"
    assert attempt["raw_response_ref"] is None
    tree = RunTree(root, RUN_ID)
    call = json.loads(tree.read_bytes(attempt["call_record_ref"]["relative_path"]))
    assert call["schema"] == CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA
    assert call["request_sha256"] == attempt["request_sha256"]
    assert call["generation_sent"]["seed"] == attempt["attempt_seed"] == 0
    assert call["transport_problem"]["response_completion"] == "unknown"

    resumed, exit_code = _run_designator(root, catalogue, tmp_path, monkeypatch, [])
    assert exit_code == EXIT_HELD
    assert resumed.requests == []
    assert _artifacts(root, DESIGNATOR, "structure-attempt") == attempts_before
    terminal = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))[2]["payload"]
    assert terminal["call_record_ref"] == attempt["call_record_ref"]
    assert terminal["attempt_ordinal"] == len(terminal["attempts"]) == 1
    expected_acts(_open(root, catalogue, ATTESTATORES, "--placement-tier", TIER))


# --- the refusals before any chair starts ---------------------------------------------


def test_a_real_submission_under_the_fixture_catalogue_is_refused_by_name(
    real_template, tmp_path, monkeypatch
):
    root = tmp_path / "runs"
    shutil.copytree(real_template, root)
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ROOT / "pipeline" / "2_designator" / "run.py"),
            "--run-root",
            str(root),
            "--run-id",
            RUN_ID,
        ],
    )
    with pytest.raises(ContractError, match="may not be marked out by the fixture structure chair"):
        designator.main()
    assert not (root / RUN_ID / "2_designator").exists()


def test_a_real_recovery_requires_the_same_explicit_request_identity(
    real_template, tmp_path, monkeypatch
):
    """Real ingress reaches recovery validation; it is not route-refused."""
    root = tmp_path / "runs"
    shutil.copytree(real_template, root)
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ROOT / "pipeline" / "2_designator" / "run.py"),
            "--run-root",
            str(root),
            "--run-id",
            RUN_ID,
            "--operation",
            "recover",
        ],
    )
    with pytest.raises(ContractError, match="must name the act"):
        designator.main()
    assert not (root / RUN_ID / "2_designator").exists()


def test_a_non_zero_sealed_structure_temperature_is_admitted_for_the_serving_seam(
    tmp_path, monkeypatch
):
    """The serving seam owns and sends the sealed structural posture."""
    catalogue = _live_catalogue(tmp_path)
    decoding = tmp_path / "decoding.toml"
    decoding.write_text(
        'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 0\n'
        '[variance_experiment]\nlabel = "variance.v1"\nseed = 20260820\npasses = 2\n'
        "[structure]\ntemperature = 0.7\n",
        encoding="utf-8",
    )
    policy, _digest = load_decoding_policy(decoding)
    assert policy["structure"] == {"temperature": 0.7}
    root = tmp_path / "runs"
    _chain(root, catalogue, "--decoding-config", str(decoding))

    assert designator.structure_pass.executable_temperature(policy) == 0.7
    assert not (root / RUN_ID / "2_designator" / "artifacts").exists()


_ABSENT_SECONDARY = """[chairs.secondary_proposer]
state = \"absent\"
reason = \"no secondary proposer is configured for the offline walking skeleton\"
"""

_CONFIGURED_SECONDARY = """[chairs.secondary_proposer]
state = \"configured\"
source = \"local-repository\"
path = \"designator_structure\"
digest_manifest = \"{digest_manifest}\"
manifest = \"manifests/designator_structure.json\"
serving_recipe = \"fake-designator-v0\"
license_note = \"fixture identity only; no model weights or model license apply\"
"""


def test_a_configured_secondary_proposer_is_refused_on_the_live_path(tmp_path, monkeypatch):
    """Absent by ruling; a live run writes no fixture receipt for one."""
    import tomllib

    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    live = MODELS_CONFIG.read_text(encoding="utf-8")
    assert _ABSENT_SECONDARY in live
    digest_manifest = tomllib.loads(live)["chairs"]["designator_structure"]["digest_manifest"]
    models = config_root / "models.toml"
    models.write_text(
        live.replace(
            _ABSENT_SECONDARY, _CONFIGURED_SECONDARY.format(digest_manifest=digest_manifest)
        ),
        encoding="utf-8",
    )
    catalogue = _live_catalogue(tmp_path)
    root = tmp_path / "runs"
    _chain(root, catalogue, "--models-config", str(models))
    endpoint = FakeEndpoint(served_model_id=SERVED_MODEL_ID)
    factory = _serving_factory(
        endpoint, catalogue, tmp_path / "logs", tmp_path / "lock", ROOT / "config" / "decoding.toml"
    )
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        _argv(root, catalogue, "--placement-tier", TIER, "--models-config", str(models)),
    )
    with pytest.raises(
        ContractError, match="secondary proposer chair 'secondary_proposer' is configured"
    ):
        designator.main(serving_factory=factory)
    assert endpoint.requests == []
    assert _receipts(root) == []


# --- the fixture pass is the fixture pass ----------------------------------------------


def test_the_fixture_catalogue_runs_the_fixture_pass_with_no_answer_and_no_call(
    tmp_path, monkeypatch
):
    """Under the committed catalogue nothing of the live path appears on disk.

    Shape, not bytes: the acceptance pins are the byte measurement and are
    re-pinned by the host (adding `[structure]` to `config/decoding.toml` moves
    every fixture run's `config_digest`). What this pins is that the fixture
    pass writes no structure-answer, no engine call, no answer reference, and
    the one `fixture://` receipt it always wrote.
    """
    root = tmp_path / "runs"
    _chain(root, FIXTURE_CATALOGUE)
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(sys, "argv", _argv(root, FIXTURE_CATALOGUE))

    def factory(context, chair, tier):  # pragma: no cover - must never be reached
        raise AssertionError("the fixture pass built a live client")

    assert designator.main(serving_factory=factory) == EXIT_COMPLETE
    assert not _artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND)
    for status in _artifacts(root, DESIGNATOR, "structure-status"):
        assert "structure_answer_ref" not in status["payload"]
        assert "engine_call" not in status["payload"]["provenance"]
    seal = _seal(root)
    assert "engine_call" not in seal["payload"]["provenance"]
    assert [row["act_key"] for row in seal["payload"]["expected_acts"]] == ["a1", "a2"]
    receipts = _receipts(root)
    assert [receipt["chair"] for receipt in receipts] == ["designator_structure"]
    assert receipts[0]["endpoint"].startswith("fixture://")


# --- the record's own closed field set -----------------------------------------


def _minimal_answer_record() -> dict[str, Any]:
    """A record with exactly the declared field names and nothing checked but names.

    Built from the constant itself rather than written out again: the point of
    this test is that a field *outside* the set refuses, and a hand-copied
    second list of the set would drift from the one the validator uses and
    start proving something else.
    """
    record: dict[str, Any] = dict.fromkeys(designator._STRUCTURE_ANSWER_V1_FIELDS)
    record["schema"] = STRUCTURE_ANSWER_RECORD_SCHEMA
    record["acts"] = []
    record["blocks_without_proposal"] = []
    record["findings"] = []
    record["decoding"] = dict.fromkeys(designator._STRUCTURE_ANSWER_DECODING_FIELDS)
    record["vendor"] = dict.fromkeys(designator._STRUCTURE_ANSWER_VENDOR_FIELDS)
    return record


# --- the pre-send capacity check ---------------------------------------------


def _capacity_row(**overrides: Any) -> SimpleNamespace:
    """The six fields `ask_page` reads off the sealed row, as the real one states them."""

    fields: dict[str, Any] = {
        "recipe": "unproven-real-designator",
        "chair": "designator_structure",
        "tier": "generic-24gb",
        "max_model_len": 2048,
        "min_pixels": 3136,
        "max_pixels": 1806336,
        "patch_size": 16,
        "merge_size": 2,
        "served_model_id": "designator-structure",
        "seed": 0,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _RefusingClient:
    """A client whose `read` is an assertion failure: nothing may be sent."""

    def __init__(self, profile: SimpleNamespace) -> None:
        self.handle = SimpleNamespace(
            profile=profile,
            receipt_reference={"relative_path": "receipts/r.json", "sha256": "0" * 64},
        )

    def read(self, request):  # pragma: no cover - the point is that it never runs
        raise AssertionError("a request that does not fit the sealed row must never be sent")


def _ask(client, width: int, height: int, monkeypatch):
    target_width, target_height = scale_to_fit_chandra(width, height)
    presented = {
        "image_sha256": "b" * 64,
        "transform": {
            "resize": {
                "target_width_px": target_width,
                "target_height_px": target_height,
            }
        },
    }
    monkeypatch.setattr(
        structure_pass,
        "prepare_page_request_image",
        lambda *_args: (
            b"native Chandra request image",
            presented,
            {"relative_path": "2_designator/artifacts/request-image.json", "sha256": "d" * 64},
        ),
    )
    return structure_pass.ask_page(
        SimpleNamespace(tree=None),
        client,
        {
            "subject_id": "page-1",
            "payload": {
                "ordinal": 1,
                "image_path": "0_door/blobs/source.png",
                "source_sha256": "a" * 64,
            },
        },
        1,
        b"",
        {"width": width, "height": height},
        temperature=0,
        decoding_config_sha256="c" * 64,
        provenance={},
    )


def test_a_page_that_cannot_fit_the_sealed_row_is_held_before_anything_is_sent(monkeypatch):
    """The failure this check exists to move off a billing card.

    An A4 300-dpi page is 2,480x3,508. Against the shipped 24 GB row it costs
    1,715 image tokens; with the measured 593-token structure prompt and a
    1,645-token dense-page answer that is 3,953 against a `max_model_len` of
    2,048. Before this check the request went out and vLLM answered HTTP 400
    with a body the client discarded; now the page is held by name, its record
    is published with the whole arithmetic on it, and `_RefusingClient.read`
    proves nothing was sent. Native presentation is never altered again merely
    to make the request fit.
    """

    answer = _ask(_RefusingClient(_capacity_row()), 2480, 3508, monkeypatch)

    assert answer.disposition == structure_pass.DISPOSITION_HELD
    assert answer.reason_code == structure_pass.HELD_REQUEST_TOO_LARGE
    assert answer.reason_code in structure_pass.STRUCTURE_HELD_CODES
    assert answer.mint == ()
    capacity = answer.record["capacity"]
    target_width, target_height = scale_to_fit_chandra(2480, 3508)
    (image_capacity,) = capacity["images"]
    assert image_capacity["width"] == target_width
    assert image_capacity["height"] == target_height
    assert image_capacity["image_prompt_tokens"] == capacity["image_prompt_tokens"]
    assert capacity["image_prompt_tokens"] == 1715
    assert capacity["prompt_tokens"] == 593
    assert capacity["answer_budget"] == 1645
    assert capacity["need"] == 3953
    assert capacity["headroom"] == 2048 - 3953
    assert capacity["fits"] is False
    # Nothing that describes a response is invented: there was none.
    for field in ("call_record_ref", "raw_response_ref", "custody_ref", "request_sha256"):
        assert answer.record[field] is None
    # And the record is publishable exactly as any other answer is.
    designator._validate_structure_answer_payload(answer.record, terminal=False)


def test_the_same_page_is_admitted_once_the_row_states_a_larger_context(monkeypatch):
    """The counterfactual: only the row changed, and the request goes."""

    client = _RefusingClient(_capacity_row(max_model_len=8192))
    with pytest.raises(AssertionError, match="must never be sent"):
        _ask(client, 2480, 3508, monkeypatch)


def test_the_structure_prompts_measured_token_count_still_matches_the_prompt_that_is_sent():
    """The digest that expires the measured constant, checked where the prompt lives."""

    assert structure_pass.structure_prompt_tokens() == 593


def test_every_live_page_record_carries_the_capacity_it_was_admitted_on(
    live_run, tmp_path, monkeypatch
):
    """The admitted path over the real chain: the arithmetic is published too.

    The fixture pages are 200x260 and cost 48 image tokens on this chair --
    two orders of magnitude under the budget, which is itself why no fixture
    run had ever exercised this arithmetic before it was written down.
    """

    root, catalogue = live_run
    _endpoint, exit_code = _run_designator(root, catalogue, tmp_path, monkeypatch, _happy_answers())
    assert exit_code == EXIT_COMPLETE
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    assert set(answers) == {1, 2}
    for payload in (record["payload"] for record in answers.values()):
        capacity = payload["capacity"]
        assert capacity["schema"] == "verbatus-request-capacity.v1"
        assert capacity["fits"] is True
        assert capacity["image_prompt_tokens"] == 48
        assert capacity["prompt_tokens"] == 593
        assert capacity["answer_budget"] == 1645
        assert capacity["headroom"] == 4096 - (48 + 593 + 1645)
        # The same record reached the retained call record, beside the request.
        tree = RunTree(root, RUN_ID)
        call_record = json.loads(
            tree.read_bytes(payload["call_record_ref"]["relative_path"]).decode("utf-8")
        )
        assert call_record["schema"] == CHAIR_CALL_RECORD_SCHEMA
        assert call_record["capacity"] == capacity


def test_a_field_outside_the_structure_answer_contract_refuses_by_name():
    """A field nobody declared refuses at publication, naming itself.

    `_refuse_text_fields` can only refuse the content names it already knows,
    and `page_text` is not one of them -- the parser computes a whole page's
    joined transcription under exactly that name, and a record that grew a
    field for it would publish the page's reading past every text fence in this
    stage. The closed set is what makes that a refusal on the run that adds the
    field rather than a finding at some later review.
    """
    designator._validate_structure_answer_payload(_minimal_answer_record())

    record = _minimal_answer_record()
    record["page_text"] = "SYNTHETIC ACT ONE alpha beta gamma"
    with pytest.raises(ContractError, match=r"unexpected \['page_text'\]"):
        designator._validate_structure_answer_payload(record)

    record = _minimal_answer_record()
    del record["provenance"]
    with pytest.raises(ContractError, match=r"missing \['provenance'\]"):
        designator._validate_structure_answer_payload(record)


# One page written to raise every finding the layout grammar has: a top-level
# `<div>` with a nested `data-bbox` a descendant carries, character data outside
# every block, a `Blank-Page`, a malformed box, a div wrapped in a `<span>`
# (which the block reader does not see as top level and the deliberately
# different div counter does, so the two counts part), and a block left open at
# the end. Hand-written rather than assembled by a builder: the point is to
# reach the grammar's own edges, and a builder that could produce all of them
# would be a second grammar.
_EVERY_FINDING_PAGE = (
    '<div data-bbox="0 0 100 100" data-label="Text">'
    '<p data-bbox="5 5 50 50">a nested box the vendor deletes</p></div>\n'
    "ink the answer wrote between two blocks\n"
    '<div data-bbox="0 0 1000 1000" data-label="Blank-Page"></div>\n'
    '<div data-bbox="nope" data-label="Text">a box nobody can read</div>\n'
    '<span><div data-bbox="10 10 20 20" data-label="Text">wrapped</div></span>\n'
    '<div data-bbox="200 200 900 900" data-label="Text">left open at the end'
)


def test_every_finding_the_grammar_raises_is_publishable_by_this_stage():
    """The seven declared kinds, produced for real and put through publication.

    Two halves of one claim, and neither proves the other. `_designator_finding`
    refuses a kind this stage has not declared, so a kind the grammar grows
    fails at the page that produces it; and `run.py`'s closed field set refuses
    a *field* nobody declared, independently, without importing this pass's
    table. Four of the seven -- `nested-bbox-retained`, `unclosed-block`,
    `block-count-mismatch` and `duplicate-rectangle` -- are reachable on a real
    page but appear in no end-to-end fixture above, so without this a wrong
    field set for one of them would first be found by a run refusing to publish
    a page it had already paid a card to read.
    """
    parsed = chandra_layout.parse_layout_html(_EVERY_FINDING_PAGE.encode())
    assert not chandra_layout.is_refusal(parsed)
    findings = [structure_pass._designator_finding(f) for f in parsed["findings"]]
    # A duplicate rectangle is this pass's own finding, not the grammar's, so it
    # is produced the way the pass produces it rather than written out here.
    proposals, _without = structure_pass.blocks_to_proposals(parsed, 200, 260)
    _unique, duplicates = structure_pass.dedupe_rectangles(proposals + proposals[:1])
    findings += duplicates
    assert {finding["kind"] for finding in findings} == structure_pass.STRUCTURE_FINDING_KINDS

    record = _minimal_answer_record()
    record["findings"] = findings
    designator._validate_structure_answer_payload(record)


def test_the_producers_finding_kinds_and_the_validators_agree():
    """Two lists written out separately, and neither derived from the other.

    `structure_pass.STRUCTURE_FINDING_KINDS` says what this stage will publish;
    `run._STRUCTURE_ANSWER_FINDING_FIELDS` says what publication accepts. They
    are enumerated in two files on purpose -- a validator that imported the
    producer's table would agree with it by construction -- so this is what
    catches a kind added to one and forgotten in the other, on the commit that
    adds it rather than on the page that raises it.
    """
    assert (
        set(designator._STRUCTURE_ANSWER_FINDING_FIELDS) == structure_pass.STRUCTURE_FINDING_KINDS
    )


def test_the_grammars_finding_kinds_are_reconciled_at_import():
    """The import-time seal, exercised rather than trusted.

    `STRUCTURE_FINDING_KINDS` is written out rather than built from the
    grammar's set, and `_reconcile_finding_kinds` is what makes that a check
    instead of a copy that could drift. Run against a grammar that grew a kind:
    it must refuse, and name both directions of the disagreement.
    """
    structure_pass._reconcile_finding_kinds()  # the real pair agrees

    grown = frozenset(chandra_layout.LAYOUT_FINDING_KINDS) | {"a-kind-the-grammar-grew"}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(chandra_layout, "LAYOUT_FINDING_KINDS", grown)
        with pytest.raises(RuntimeError, match="a-kind-the-grammar-grew"):
            structure_pass._reconcile_finding_kinds()


def test_a_finding_kind_the_grammar_grows_refuses_at_the_page_that_produces_it():
    """The other direction: an undeclared kind never reaches a record.

    `chandra_layout` could add a finding kind without this pass noticing, and a
    finding whose fields nothing here has closed would be published unchecked.
    It refuses instead, naming the kind and the declared set.
    """
    with pytest.raises(ContractError, match="not one of its declared kinds"):
        structure_pass._designator_finding({"kind": "a-kind-nobody-declared", "ordinal": 0})


def test_an_act_entry_that_grew_a_label_again_refuses_before_publication(
    live_run, tmp_path, monkeypatch
):
    """The regression the closed set exists for, over the real chain.

    `label` was published in clear until this branch's review; the act entry's
    field set is what makes putting it back a refusal rather than a quiet
    return. Nothing is published for the run: the refusal is raised before the
    first answer record reaches the tree.
    """
    root, catalogue = live_run
    original = structure_pass._act_record
    monkeypatch.setattr(
        structure_pass,
        "_act_record",
        lambda act: {**original(act), "label": act["label"]},
    )
    with pytest.raises(ContractError, match=r"structure-answer act .*unexpected \['label'\]"):
        _run_designator(
            root,
            catalogue,
            tmp_path,
            monkeypatch,
            [_answer(PAGE_ONE_ACTS), _answer(PAGE_TWO_ACTS)],
        )
    assert not _artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND)


# --- two scanned regions over one rectangle -------------------------------------


def _group(bounds: dict[str, int], rationale: str) -> dict[str, Any]:
    """One scanned group in `grouping.group_page`'s own returned shape."""
    return {
        "bounds": dict(bounds),
        "body_members": [{"bounds": dict(bounds), "pixel_count": bounds["w"] * bounds["h"]}],
        "anchors": [],
        "rationale": rationale,
    }


def test_two_regions_each_covering_half_one_rectangle_are_split_detection_not_model_only():
    """The tie is its own fact: too many regions, not none.

    `model_evidence_blocks` takes the single group covering at least half of a
    rectangle, and two of them is a tie it must not resolve — naming one would
    be a picker (principle 1). It used to record the tie as `model-only`,
    whose rationale says "no region the ink scan found covers half of this
    rectangle", which is the opposite of what happened: a reader of that record
    would conclude the scan found nothing there. `split-detection` says what is
    true, and carries the same null bounds and zero counts, because no single
    measured region stands behind the rectangle either way.

    The groups are built here rather than scanned because the arithmetic is
    what is under test and the fixture page has no such page. The case it
    stands for is ordinary: a group's bounds are the union of its body run and
    its anchors, so a small isolated region can sit inside a larger group's
    bounds, and a rectangle the chair drew around the small one is then covered
    by both.
    """
    inner = {"x": 40, "y": 60, "w": 40, "h": 20}
    analysis = {
        "structure_evidence": "detected",
        "groups": [
            _group(
                {"x": 20, "y": 20, "w": 160, "h": 100}, "single margin anchor seeds one body run"
            ),
            _group(inner, "isolated marginal note: no adjacent body run"),
        ],
    }
    blocks = structure_pass.model_evidence_blocks(analysis, [("proposal:1:0", dict(inner))])
    assert blocks == [
        {
            "structure_evidence": "split-detection",
            "detected_bounds": None,
            "body_member_count": 0,
            "anchor_count": 0,
            "rationale": structure_pass._SPLIT_DETECTION_RATIONALE,
        }
    ]
    assert "no region the ink scan found" not in blocks[0]["rationale"]
    # And the published act-group contract accepts it as a value that measured
    # nothing, which is what `_require_evidence_block` refuses to combine with
    # a region or a member count.
    designator._require_evidence_block(
        {**blocks[0], "declared_bounds": dict(inner)}, "payload under test"
    )


def test_one_region_covering_half_two_rectangles_is_still_shared_detection():
    """The mirror case, unchanged: one region where the chair drew two acts.

    Asserted beside the split so the two ends of the same ambiguity cannot
    drift into one value: `shared-detection` keeps the region it measured,
    `split-detection` has no single region to keep.
    """
    band = {"x": 20, "y": 20, "w": 160, "h": 100}
    analysis = {"structure_evidence": "detected", "groups": [_group(band, "one run")]}
    upper = {"x": 20, "y": 20, "w": 160, "h": 50}
    lower = {"x": 20, "y": 70, "w": 160, "h": 50}
    blocks = structure_pass.model_evidence_blocks(
        analysis, [("proposal:1:0", upper), ("proposal:1:1", lower)]
    )
    assert [block["structure_evidence"] for block in blocks] == [
        "shared-detection",
        "shared-detection",
    ]
    assert all(block["detected_bounds"] == band for block in blocks)


# --- a custody refusal is one page's outcome ------------------------------------


def test_a_custody_refusal_holds_that_page_instead_of_aborting_the_run(
    live_run, tmp_path, monkeypatch
):
    """`retain_chandra_response` refuses; the page is held and the run goes on.

    Custody binds the response bytes to the chair's own serving receipt, and it
    refuses by name for reasons that are reachable on a live path — a receipt
    issued for another chair, a blob whose file is gone, a response that is
    itself a binding record. Uncaught, that `SchemaRefusal` came out of
    `ask_page` as the whole stage's crash: one page's receipt would have
    discarded every other page's answer, which is the lost act goal 2 puts
    above everything.

    Held, not repaired and not silently minted. The bytes themselves are not
    lost — the client retained them and the call record before custody was
    reached — so what the refusal costs is the binding that proves which call
    they came from, and a rectangle minted without it would be attributed to a
    call nothing ties it to (principle 6). The record still publishes what the
    body said, with `custody_problem` naming the refusal and both custody
    references null, so nothing about the failure is inferred from an absence.
    """
    root, catalogue = live_run
    original = structure_pass.retain_chandra_response
    calls: list[int] = []

    def refusing(tree, response, receipt_ref, *, page_id, page_ordinal):
        calls.append(page_ordinal)
        if page_ordinal == 2:
            raise SchemaRefusal("Chandra custody receipt was not issued for chair 'x'")
        return original(tree, response, receipt_ref, page_id=page_id, page_ordinal=page_ordinal)

    monkeypatch.setattr(structure_pass, "retain_chandra_response", refusing)
    _endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [_answer(PAGE_ONE_ACTS), _answer(PAGE_TWO_ACTS)],
    )
    assert exit_code == EXIT_HELD
    assert calls == [1, 2], "both pages were asked; the refusal did not stop the run"
    _assert_page_two_held(root, "structure-response-not-retained")
    payload = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))[2]["payload"]
    # The body is recorded as what it was — a good answer — and held anyway.
    assert payload["parse_state"] == "parsed"
    assert payload["act_count"] == len(PAGE_TWO_ACTS)
    assert payload["custody_problem"] == "Chandra custody receipt was not issued for chair 'x'"
    assert payload["raw_response_ref"] is None
    assert payload["custody_ref"] is None
    # Page one is untouched: its acts are minted and its own custody is intact.
    first = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))[1]["payload"]
    assert first["custody_problem"] is None
    assert first["disposition"] == "detected"
    rows = {row["act_key"] for row in _seal(root)["payload"]["expected_acts"]}
    assert "proposal:1:0" in rows
    assert any(key.startswith("residual:2:") for key in rows)
