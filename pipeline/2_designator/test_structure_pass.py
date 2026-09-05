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

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _test_support import load_designator

from common import structure_answer
from common.chairs.registry import ChairRegistry
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import act_bindings
from common.contracts.identities import verify as verify_identity
from common.contracts.serving import CHAIR_CALL_RECORD_SCHEMA
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR
from common.decoding import load_decoding_policy
from common.fixture_identity import page_identity
from common.runtree.store import RECEIPTS_DIR, RunTree
from common.stage import (
    EXIT_COMPLETE,
    EXIT_HELD,
    STRUCTURE_ANSWER_KIND,
    STRUCTURE_ANSWER_RECORD_SCHEMA,
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
)
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
    monkeypatch.setattr(sys, "argv", _argv(root, catalogue, *argv))
    return endpoint, designator.main(serving_factory=factory)


def _box(bounds: dict[str, int], page_w: int, page_h: int) -> list[int]:
    """The normalized box whose page-pixel conversion is exactly `bounds`.

    The inverse of `structure_answer.to_page_bounds`, found by search rather
    than by a second formula so the test cannot agree with the converter by
    sharing its arithmetic: every candidate is checked through the converter
    itself.
    """
    for x0 in range(1001):
        if x0 * page_w // 1000 == bounds["x"]:
            break
    for y0 in range(1001):
        if y0 * page_h // 1000 == bounds["y"]:
            break
    right, bottom = bounds["x"] + bounds["w"] - 1, bounds["y"] + bounds["h"] - 1
    for x1 in range(1001):
        if min(page_w - 1, (x1 * page_w + 999) // 1000 - 1) == right:
            break
    for y1 in range(1001):
        if min(page_h - 1, (y1 * page_h + 999) // 1000 - 1) == bottom:
            break
    box = [x0, y0, x1, y1]
    assert structure_answer.to_page_bounds(box, page_w, page_h) == bounds
    return box


def _answer(acts, page_w: int = 200, page_h: int = 260, **fields: Any) -> ScriptedAnswer:
    body = {
        "schema": structure_answer.STRUCTURE_ANSWER_SCHEMA,
        "acts": [{"box_1000": _box(bounds, page_w, page_h), "text": text} for bounds, text in acts],
    }
    fields.setdefault("finish_reason", "stop")
    return ScriptedAnswer(content=json.dumps(body), **fields)


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
    # One whole-page call per sealed page, the sealed prompt first, the sealed
    # page bytes as the one image, no generation knobs of the stage's own.
    assert len(endpoint.requests) == 2
    for request in endpoint.requests:
        messages = request["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["content"][0]["type"] == "text"
        assert messages[1]["content"][1]["type"] == "image_url"
        assert "max_tokens" not in request
        assert request["temperature"] == 0
    tree = RunTree(root, RUN_ID)

    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    assert set(answers) == {1, 2}
    for ordinal, expected in ((1, PAGE_ONE_ACTS), (2, PAGE_TWO_ACTS)):
        payload = answers[ordinal]["payload"]
        assert payload["schema"] == STRUCTURE_ANSWER_RECORD_SCHEMA
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
        assert payload["decoding"]["temperature"] == 0
        assert payload["prompt_version"] == "verbatus-structure-prompt.v1"
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
    """Every witness keeps its own pass (Tyrel, 2026-09-02).

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


def test_a_zero_act_answer_cuts_the_page_into_fallback_tiles(live_run, tmp_path, monkeypatch):
    root, catalogue = live_run
    _endpoint, exit_code = _run_designator(
        root, catalogue, tmp_path, monkeypatch, [_answer(PAGE_ONE_ACTS), _answer(())]
    )
    assert exit_code == EXIT_COMPLETE
    statuses = _by_page_ordinal(_artifacts(root, DESIGNATOR, "structure-status"))
    assert statuses[2]["payload"]["state"] == "scanned"
    assert statuses[2]["payload"]["structure_evidence"] == "fallback-tiles"
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    assert answers[2]["payload"]["disposition"] == "fallback-tiles"
    assert answers[2]["payload"]["act_count"] == 0
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


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (
            "# Page\n\nSome markdown the chair wrote instead of JSON.",
            "structure-answer-invalid-json",
        ),
        (
            json.dumps({"schema": "verbatus-structure-answer.v1", "acts": [], "note": "x"}),
            "structure-answer-unverified-response-schema",
        ),
        (
            json.dumps({"schema": "verbatus-structure-answer.v1"}),
            "structure-answer-missing-act-list",
        ),
        (
            json.dumps(
                {
                    "schema": "verbatus-structure-answer.v1",
                    "acts": [{"box_1000": [10, 10, 5, 5], "text": "x"}],
                }
            ),
            "structure-answer-malformed-act-geometry",
        ),
    ],
)
def test_an_answer_the_contract_refuses_holds_the_page_by_its_outcome(
    live_run, tmp_path, monkeypatch, content, code
):
    root, catalogue = live_run
    _endpoint, exit_code = _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [_answer(PAGE_ONE_ACTS), ScriptedAnswer(content=content, finish_reason="stop")],
    )
    assert exit_code == EXIT_HELD
    _assert_page_two_held(root, code)
    answers = _by_page_ordinal(_artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND))
    payload = answers[2]["payload"]
    assert payload["parse_state"] == "refused"
    assert payload["parse_outcome"] == code.removeprefix("structure-answer-")
    assert payload["acts"] == [] and payload["act_count"] == 0


def test_a_truncated_body_that_does_not_parse_holds_as_cut_off_not_a_parse_refusal(
    live_run, tmp_path, monkeypatch
):
    """The cut-off stop word wins over the parse outcome: SPEC_D S1.4 places

    the `finish_reason in ENGINE_STOP_CUT_OFF` row above the parse-refusal
    rows, and it applies "parsed or not". A body the engine truncated
    mid-object is exactly the failure this measurement exists to name --
    the small `max_model_len` a whole-page transcription can overrun -- and
    it must not be recorded as `structure-answer-invalid-json`, which would
    blame the chair's JSON rather than the context window.
    """
    root, catalogue = live_run
    truncated = '{"schema": "verbatus-structure-answer.v1", "acts": [{"box_1000"'
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
    assert payload["parse_state"] == "refused"
    assert payload["finish_reason"] == "length"
    assert payload["act_count"] == 0


def test_an_unrecognized_stop_word_over_a_body_that_does_not_parse_is_still_refused_by_name(
    live_run, tmp_path, monkeypatch
):
    """The unnameable stop word is fatal whether or not the body parsed --

    not folded silently into `structure-answer-invalid-json` just because
    the JSON also happened to be unreadable.
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
                ScriptedAnswer(content="not json at all", finish_reason="abort"),
            ],
        )
    assert not _artifacts(root, DESIGNATOR, STRUCTURE_ANSWER_KIND)


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


def test_a_recovery_from_a_real_submission_is_refused_by_name(real_template, tmp_path, monkeypatch):
    """A real ingress has no fixture, and `recovery_pass` reads a recrop's

    geometry from the fixture's declared rectangle. Refuse before that read
    is ever attempted, by this stage's own named reason, rather than let the
    generic fixture accessor's real-submission refusal (common/stage.py)
    stand in for it. Checked ahead of `--act`/`--recovery-request` validation
    so this refusal fires even when neither flag is given.
    """
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
    with pytest.raises(ContractError, match="bounded recovery from a real submission is not built"):
        designator.main()
    assert not (root / RUN_ID / "2_designator").exists()


def test_a_non_zero_sealed_structure_temperature_is_refused_before_any_chair_starts(
    tmp_path, monkeypatch
):
    """The seam executes 0 only; a sealed value it cannot carry is a named refusal,
    never a silent zero on every call record."""
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

    def factory(context, chair, tier):  # pragma: no cover - must never be reached
        raise AssertionError("a chair was started under a temperature the seam cannot execute")

    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        _argv(root, catalogue, "--placement-tier", TIER, "--decoding-config", str(decoding)),
    )
    with pytest.raises(ContractError, match="cannot be executed as sealed"):
        designator.main(serving_factory=factory)
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
    """Absent by ruling (2026-08-12); a live run writes no fixture receipt for one."""
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
    record: dict[str, Any] = dict.fromkeys(designator._STRUCTURE_ANSWER_FIELDS)
    record["acts"] = []
    record["findings"] = []
    record["decoding"] = dict.fromkeys(designator._STRUCTURE_ANSWER_DECODING_FIELDS)
    return record


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
    be a picker (GOVERNANCE 3). It used to record the tie as `model-only`,
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
    discarded every other page's answer, which is the lost act GOALS 1 puts
    above everything.

    Held, not repaired and not silently minted. The bytes themselves are not
    lost — the client retained them and the call record before custody was
    reached — so what the refusal costs is the binding that proves which call
    they came from, and a rectangle minted without it would be attributed to a
    call nothing ties it to (GOVERNANCE 6). The record still publishes what the
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
