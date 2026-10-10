"""Regression tests for terminal accounting guards.

Each test supplies the smallest synthetic contradiction that reaches one real
stage entry point.  The contradiction is deliberately impossible through normal
publication: that is precisely why the terminal boundary must reject it rather
than relying on an earlier stage never to produce it.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.armarium_formats import ArmariumFormats
from common.background import load_background_config, resolve_background_policy
from common.chairs.registry import ChairRegistry
from common.contracts.approval import synthetic_fixture_ingress_record
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.outcomes import ArmariumCategory
from common.contracts.stages import ARCHETYPUS, ARMARIUM, CONIECTOR, DOOR, EXEMPLAR, INK_MAP
from common.reading_annotations import read_doubt_marks
from common.reconstruction import load_reconstruction_policy
from common.reconstruction_records import PLAN_KIND, plan_payload
from common.residual_ink import (
    ink_map_page,
    load_coverage_audit_config,
    resolve_coverage_audit_policy,
)
from common.runtree.store import RunTree
from common.sealed_config import read_sealed_toml
from common.stage import (
    EXIT_FATAL,
    EXIT_HELD,
    StageContext,
    load_fixture,
    require_sealed_config,
    run_config_bindings,
)
from conftest import load_stage

ROOT = Path(__file__).resolve().parents[2]
EXEMPLAR_CLI = ROOT / "pipeline" / "1_exemplar" / "run.py"


INK_MAP_RUN = load_stage("1_ink_map")


def _parser_stub():
    """The stage parser seam for a direct, synthetic entry-point test."""
    return SimpleNamespace(parse_args=lambda: SimpleNamespace())


def _accepted_review() -> dict:
    """A fresh accepted Recensor review for terminal tests."""
    return {
        "artifact_id": "art_accepted",
        "outcome": "accepted",
        # The coverage facts a page review writes, so the double is a review a
        # producer can emit.
        "payload": {
            "coverage": {
                "under_witnessed": False,
                "unresolved_chairs": 0,
                "shortfalls": {"failed": 0, "truncated": 0, "unaligned": 0},
            },
            # A review names its holds, its flags and its queue place: none when clean.
            "hold_codes": [],
            "flag_codes": [],
            "review_priority": None,
        },
    }


class _RecordingContext:
    """Just enough sealed-context surface for terminal-only stage paths."""

    stage = ARMARIUM
    retain = StageContext.retain

    def __init__(self) -> None:
        self.published: list[dict] = []
        self.sealed = False
        self.finished = False
        self.fixture = {"fixture_id": "synthetic-terminal-guard-v0"}
        self.scenario = "synthetic-terminal-guard"
        self.config_digest = "a" * 64
        self.run = {"source_manifest": [], "self_hash": "d" * 64}
        self.registry = SimpleNamespace(config=object())
        self.required_configs: list[tuple[str, str]] = []
        self.witness_chairs: list[str] = []
        self.witness_floor = 0
        self.armarium_formats = ArmariumFormats(
            ("text-bundle", "acts-database", "jsonl", "review-items"),
            False,
        )
        self.blobs: dict[str, bytes] = {}
        # The sealed configuration paths the export reads for its
        # `claims.not_measured` block: the Designator geometry file whose
        # `provenance` says whether their numbers were ever calibrated, and the
        # Perlector audit policy whose `round_cap` decides whether an uncertain
        # span was reachable at all. The shipped files, because this synthetic
        # context stands in for a run sealed under them -- a stand-in that named
        # invented paths would make the block's own honesty untestable here.
        config = ROOT / "config"
        self.args = SimpleNamespace(
            designator_geometry_config=config / "designator_geometry.toml",
            ink_map_config=config / "ink_map.toml",
            perlector_protocol_config=config / "perlector_protocol.toml",
            alignment_config=config / "alignment.toml",
            reconstruction_config=config / "reconstruction.toml",
            review_config=config / "review.toml",
        )
        self.perlector_audit_config_path = config / "perlector_audit.toml"
        self.page_accounting_config_path = config / "page_accounting.toml"
        # Every synthetic row's Perlectio reads "Jean Roy" with no doubt unless a
        # test stores another under the row's reference.
        self.perlectiones: dict[str, dict] = {}
        # Read for the [truncation] provenance the not-measured geometry row discloses.
        self.perlector_protocol_config_path = self.args.perlector_protocol_config
        # Mirror the real context's named point-of-use seals.  The terminal
        # paths read every file named below; recording their digests here makes the
        # double refuse drift or an unsealed name instead of bypassing that
        # boundary.
        self.sealed_config_digests = {
            "designator-geometry": read_sealed_toml(self.args.designator_geometry_config, "config")[
                1
            ],
            "ink-map": read_sealed_toml(self.args.ink_map_config, "config")[1],
            "perlector-audit": read_sealed_toml(self.perlector_audit_config_path, "config")[1],
            "perlector-protocol": read_sealed_toml(self.args.perlector_protocol_config, "config")[
                1
            ],
            "alignment": read_sealed_toml(self.args.alignment_config, "config")[1],
            "reconstruction": load_reconstruction_policy(self.args.reconstruction_config).sha256,
            "review": read_sealed_toml(self.args.review_config, "config")[1],
            "page-accounting": read_sealed_toml(self.page_accounting_config_path, "config")[1],
        }
        # The Coniector plans no call over this synthetic run; the export proves its one plan.
        self.coniector_plan = {
            "artifact_id": "coniector-plan",
            "payload": plan_payload(
                load_reconstruction_policy(self.args.reconstruction_config), []
            ),
        }

        # Build the mapped page with the same policies, measures, and canonical
        # projection as Ink Map. The tiny page is uniformly paper-colored, so
        # it truthfully contains no retained or edge ink while still carrying
        # every field the closed producer record requires.
        width, height = 8, 2
        self.sealed_page_dimensions = (width, height)
        rows = [bytearray([230] * width) for _ in range(height)]
        background_config = load_background_config(self.args.ink_map_config)
        coverage_config = load_coverage_audit_config(self.args.ink_map_config)
        background_policy = resolve_background_policy(background_config, width, height)
        coverage_policy = resolve_coverage_audit_policy(coverage_config, width, height)
        measured = ink_map_page(
            width,
            height,
            rows,
            background_policy=background_policy,
            coverage_policy=coverage_policy,
        )

        def read_bytes(relative_path: str) -> bytes:
            if relative_path not in self.blobs:
                raise AssertionError(f"the stage read an unstored blob path: {relative_path}")
            return self.blobs[relative_path]

        def put_blob(_stage: str, data: bytes):
            relative_path = "synthetic/armarium-export.zip"
            self.blobs[relative_path] = data
            return digest_bytes(data), SimpleNamespace(relative_path=relative_path)

        # The export's page-level hold is derived from one Ink Map row per
        # sealed page, and the stage reads them with no capability sniff. This
        # double therefore answers the manifest walk the real tree answers: one
        # `mapped` page 1 finding, carrying real `ink-runs.v2`-shaped evidence
        # rather than a placeholder, and no reading regions to release anything
        # with.
        self.ink_map_records = {
            "page-1": {
                "artifact_id": "page-1",
                "outcome": "mapped",
                "payload": {
                    "page_ordinal": 1,
                    "ink_measurable": True,
                    "background": {
                        **measured["background"],
                        "config_sha256": self.sealed_config_digests["ink-map"],
                    },
                    "edge": INK_MAP_RUN.artifact_finding(measured["edge"]),
                    "edge_findings": measured["edge_findings"],
                },
            }
        }

        def build_manifest(stage: str, **_options) -> dict:
            if stage == CONIECTOR:
                return {"artifacts": [{"kind": PLAN_KIND, "artifact_id": "coniector-plan"}]}
            if stage != INK_MAP:
                # An empty manifest is the truthful answer, not a swallowed
                # lookup: the code under test polls this method for stages this
                # synthetic context deliberately stores nothing for (the
                # Exemplar, for example), and an empty inventory is what an
                # unpopulated stage really has. `read_artifact` below refuses
                # instead because it is only ever called with an artifact id
                # that must already exist.
                return {"artifacts": []}
            return {
                "artifacts": [
                    {"kind": "ink-map", "artifact_id": artifact_id}
                    for artifact_id in self.ink_map_records
                ]
            }

        def read_artifact(stage: str, kind: str, artifact_id: str) -> dict:
            if stage == CONIECTOR and kind == PLAN_KIND:
                return self.coniector_plan
            if stage != INK_MAP or kind != "ink-map":
                raise AssertionError(f"the stage read an unstored artifact: {stage}/{kind}")
            return self.ink_map_records[artifact_id]

        self.tree = SimpleNamespace(
            read_bytes=read_bytes,
            put_blob=put_blob,
            build_manifest=build_manifest,
            read_artifact=read_artifact,
            # This run stores no operator review decision.
            review_decision_records=lambda: [],
            read_run=lambda: self.run,
            read_artifact_reference=lambda reference, **_where: {
                "payload": self.perlectiones.get(reference["relative_path"], _perlectio("Jean Roy"))
            },
        )

    def publish(self, **record) -> None:
        self.published.append(record)

    def finish(self) -> None:
        self.finished = True

    def seal_boundary(self) -> None:
        self.sealed = True

    def require_sealed_config(self, name: str, observed_sha256: str) -> None:
        require_sealed_config(
            self.sealed_config_digests, name, observed_sha256, "synthetic terminal context"
        )
        self.required_configs.append((name, observed_sha256))

    def artifact_ref(self, stage: str, kind: str, identity: str) -> dict[str, str]:
        return {
            "relative_path": f"synthetic/{stage}/{kind}/{identity}.json",
            "sha256": "a" * 64,
        }

    def input_ref(self, relative_path: str) -> dict[str, str]:
        if relative_path not in self.blobs:
            raise AssertionError(f"the stage referenced an unstored blob path: {relative_path}")
        return {
            "relative_path": relative_path,
            "sha256": digest_bytes(self.blobs[relative_path]),
        }


def _sealed_page_census(context: _RecordingContext) -> dict[int, dict]:
    """The page shape of this context's generated synthetic measurement."""
    return {
        1: {
            "outcome": "sealed",
            "_pixel_dimensions": context.sealed_page_dimensions,
        }
    }


def test_recording_context_validates_a_config_before_recording_it() -> None:
    context = _RecordingContext()
    name = "designator-geometry"
    digest = context.sealed_config_digests[name]

    context.require_sealed_config(name, digest)
    assert context.required_configs == [(name, digest)]

    with pytest.raises(
        ContractError, match="configuration changed between this run's binding check"
    ):
        context.require_sealed_config(name, "0" * 64)
    with pytest.raises(
        ContractError,
        match="synthetic terminal context sealed no digest for the unsealed-test-name configuration",
    ):
        context.require_sealed_config("unsealed-test-name", digest)
    assert context.required_configs == [(name, digest)]


def _all_refused_door_tree(root: Path) -> RunTree:
    """Publish one synthetically refused source without the Door's earlier gate.

    ``process_sources`` normally belongs to a Door run that calls
    ``require_some_admitted`` afterwards.  The Exemplar nevertheless has its own
    terminal guard: a resumed or tampered tree can contain only valid refusal
    artifacts, and the Exemplar must not seal that empty corpus.
    """
    fixture = load_fixture(str(ROOT / "proof"))
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    bindings = run_config_bindings(registry.config, fixture, "happy")
    tree = RunTree.create(
        root,
        "all-refused",
        source_manifest=[
            {
                "ordinal": 1,
                "relative_path": "synthetic/unreadable.bin",
                "sha256": "a" * 64,
            }
        ],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
        ingress=synthetic_fixture_ingress_record(),
        sealed_config_digests=bindings["sealed_config_digests"],
    )
    context = StageContext(
        tree=tree,
        run=tree.read_run(),
        fixture=fixture,
        scenario="happy",
        stage=DOOR,
        adapter_revision=bindings["adapter_recipes"][DOOR],
        args=None,
        registry=registry,
    )
    context.publish(
        kind="admission",
        subject_id="source-1",
        outcome="refused",
        payload={
            "ordinal": 1,
            "declared_path": "synthetic/unreadable.bin",
            "declared_sha256": "a" * 64,
            "reason": "corrupt: deliberately invalid synthetic bytes",
        },
    )
    context.seal_boundary()
    context.finish()
    return tree


def test_exemplar_never_seals_a_corpus_with_only_refused_sources(tmp_path):
    """The Exemplar's own all-refused guard must stop a zero-sealed corpus."""
    tree = _all_refused_door_tree(tmp_path / "runs")

    result = subprocess.run(
        [
            sys.executable,
            str(EXEMPLAR_CLI),
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "all-refused",
            "--fixture-root",
            str(ROOT / "proof"),
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == EXIT_FATAL
    assert "every admitted source failed to seal" in result.stderr
    assert "Traceback" not in result.stderr
    artifacts = tree.build_manifest(EXEMPLAR)["artifacts"]
    assert [entry["outcome"] for entry in artifacts if entry["kind"] == "page"] == ["refused"]
    assert not [entry for entry in artifacts if entry["kind"] == "seal"]


def _reading_row(act_id: str, act_key: str, page_ordinal: int) -> dict:
    """One counted page-read row, as `reading_acts` writes it."""
    return {
        "act_id": act_id,
        "act_key": act_key,
        "kind": "act",
        "class": "reading",
        "page_ordinal": page_ordinal,
        "reading_attempt": 1,
        "hold_codes": [],
        "region_ref": None,
        "perlectio_ref": {
            "relative_path": f"synthetic/perlectio/{act_id}.json",
            "sha256": "c" * 64,
        },
    }


def _perlectio(raw: str) -> dict:
    """The text and doubt fields of a Perlectio that read `raw`."""
    text, report = read_doubt_marks(raw)
    return {
        "text": text,
        "uncertain_spans": report["uncertain_spans"],
        "gaps": report["gaps"],
        "uncertainty_assessment": report,
    }


def test_a_page_over_the_doubt_limit_exports_only_if_every_reading_on_it_was_held(monkeypatch):
    """The export recounts each page as the Perlector does: "Jean Roy fils" all doubtful
    beside "Jean Roy" is 11 of 18, so both readings must carry the page hold."""
    armarium = load_stage("7_armarium")

    def run(hold_codes: list[str]) -> None:
        context = _RecordingContext()
        context.perlectiones["synthetic/perlectio/doubtful.json"] = _perlectio("[[Jean Roy fils]]")
        rows = [_reading_row("doubtful", "p1:1", 1), _reading_row("clean", "p1:2", 1)]
        for row in rows:
            row["hold_codes"] = hold_codes
        _stub_page_export(monkeypatch, armarium, context, rows, ArmariumCategory.HELD_FOR_REVIEW)
        monkeypatch.setattr(armarium, "ink_map_page_rows", lambda *_args: [])
        monkeypatch.setattr(
            armarium,
            "build_armarium_bundle",
            lambda *_args: SimpleNamespace(
                data=b"synthetic bundle",
                manifest={"self_hash": "c" * 64, "claims": {"status": "partial"}},
            ),
        )
        assert armarium.main() == EXIT_HELD

    with pytest.raises(FatalAccounting, match="page-doubt-share-high"):
        run([])
    run(["page-doubt-share-high"])


def _stub_page_export(monkeypatch, armarium, context, rows: list[dict], category) -> None:
    """Reach the page export's terminal accounting with `rows`, each reviewed into `category`.

    The page census, the denominator, the reviews and each row's category are
    stubbed; the projection, the run aggregate and the export record are the
    stage's own.
    """
    review = {**_accepted_review(), "payload": {**_accepted_review()["payload"], "notes": []}}
    ordinals = sorted({row["page_ordinal"] for row in rows})
    monkeypatch.setattr(armarium, "stage_parser", lambda _description: _parser_stub())
    monkeypatch.setattr(armarium, "open_stage_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        armarium,
        "page_census",
        lambda _context: {
            ordinal: {"outcome": "sealed", "_pixel_dimensions": context.sealed_page_dimensions}
            for ordinal in ordinals
        },
    )
    monkeypatch.setattr(
        armarium, "reading_denominator", lambda _context: {"pages": {}, "acts": rows}
    )
    monkeypatch.setattr(
        armarium,
        "current_page_reviews",
        lambda _context, _rows: {row["act_id"]: review for row in rows},
    )
    monkeypatch.setattr(armarium, "continuation_links", lambda *_args: [])
    monkeypatch.setattr(armarium, "current_page_testimonia", lambda *_args: {})
    monkeypatch.setattr(armarium, "_page_category", lambda *_args: (category, None))
    monkeypatch.setattr(armarium, "unaddressed_chairs", lambda _config: ())
    monkeypatch.setattr(armarium, "declared_page_witness_chairs", lambda _context: set())
    monkeypatch.setattr(
        armarium,
        "page_not_measured_basis",
        lambda _context, pages, *_args: {"pages_sealed": len(pages)},
    )
    monkeypatch.setattr(armarium, "page_accounting_rows", lambda *_args: [])
    # The Coniector made nothing over these rows.
    monkeypatch.setattr(
        armarium,
        "verified_reconstructions",
        lambda _context, _rows: {
            "plan": {},
            "acts": {},
            "joins": [],
            "calls": {},
            "refs": {},
            "diplomatic_raw": {},
        },
    )


def test_only_sealed_canary_readings_leave_the_bundle_and_real_canary_named_paths_stay(
    monkeypatch,
):
    armarium = load_stage("7_armarium")
    context = _RecordingContext()
    context.run = {
        "sealed_config_digests": {"canary-ledger": "c" * 64},
        "source_manifest": [
            {"ordinal": 1, "relative_path": "canary/x.jpg", "ledger_sha256": "a" * 64},
            {"ordinal": 2, "relative_path": "bird.jpg", "ledger_sha256": "c" * 64},
        ],
        "self_hash": "d" * 64,
    }
    rows = [_reading_row("real", "p1:1", 1), _reading_row("bird", "p2:1", 2)]
    _stub_page_export(monkeypatch, armarium, context, rows, ArmariumCategory.HELD_FOR_REVIEW)
    monkeypatch.setattr(armarium, "ink_map_page_rows", lambda *_args: [])
    projections = []

    def bundle(projection, *_args):
        projections.append(projection)
        return SimpleNamespace(
            data=b"synthetic bundle",
            manifest={"self_hash": "c" * 64, "claims": {"status": "partial"}},
        )

    monkeypatch.setattr(armarium, "build_armarium_bundle", bundle)
    assert armarium.main() == EXIT_HELD
    (projection,) = projections
    assert [row["act_id"] for row in projection.acts] == ["real"]
    assert [row["ordinal"] for row in projection.pages] == [1]
    assert projection.aggregate["by_category"] == {"held-for-review": 1}
    assert projection.not_measured_basis == {"pages_sealed": 0}
    assert [row["relative_path"] for row in projection.source_manifest] == ["canary/x.jpg"]
    export = next(row["payload"] for row in context.published if row["kind"] == "export")
    assert export["canary"] == {
        "ordinals": [2],
        "acts": [
            {
                "act_id": "bird",
                "act_key": "p2:1",
                "category": "held-for-review",
                "page_ordinals": [2],
            }
        ],
    }
    assert [row["act_id"] for row in export["non_delivered"]] == ["real"]
    assert [row["subject_id"] for row in context.published if row["kind"] == "manifest-entry"] == [
        "real",
        "bird",
    ]


def test_the_stage_reports_the_ledger_status_when_the_run_aggregate_reconciles(monkeypatch):
    """A bundle whose own face says `partial` may not leave under an exit code of 0.

    The ledger folds the aggregate's reasons into its own, so it can only ever
    be the more partial of the two, and the export's outcome and exit follow
    the ledger. The aggregate is stubbed `complete` to reach that case through
    `main`.
    """
    armarium = load_stage("7_armarium")
    context = _RecordingContext()
    rows = [_reading_row("act_blank", "p1:1", 1)]
    _stub_page_export(monkeypatch, armarium, context, rows, ArmariumCategory.CONFIRMED_BLANK)
    monkeypatch.setattr(
        armarium,
        "run_aggregate",
        lambda *_args, **_kwargs: {"status": "complete", "reasons": []},
    )
    monkeypatch.setattr(
        armarium,
        "build_armarium_bundle",
        lambda *_args: SimpleNamespace(
            data=b"synthetic bundle",
            manifest={
                "self_hash": "c" * 64,
                "claims": {"status": "partial", "partial_reasons": ["page 1 is held-for-review"]},
            },
        ),
    )

    assert armarium.main() == EXIT_HELD
    assert context.finished
    export = next(record for record in context.published if record["kind"] == "export")
    assert export["outcome"] == ArmariumCategory.HELD_FOR_REVIEW.value
    assert export["payload"]["bundle"]["claims_status"] == "partial"
    # The aggregate remains its own separate measurement, published unchanged.
    assert export["payload"]["aggregate"]["status"] == "complete"
    expected_digest = digest_bytes(b"synthetic bundle")
    assert export["payload"]["bundle"]["sha256"] == expected_digest
    assert export["payload"]["bundle"]["reference"]["sha256"] == expected_digest


class _ArchetypusTree:
    def __init__(self, records: dict[str, dict]):
        self.records = records

    def read_artifact(self, _stage, _kind, identity):
        return self.records[identity]


@pytest.mark.parametrize(
    ("outcome", "established", "refusal"),
    [
        ("accepted", 0, "carries 0 Archetypus records"),
        ("accepted", 2, "carries 2 Archetypus records"),
        ("held-for-review", 1, "carries an Archetypus record anyway"),
    ],
)
def test_a_reading_exports_only_with_exactly_the_established_record_its_review_allows(
    outcome, established, refusal
):
    """An accepted reading exports its one established record; nothing else may stand in.

    With no record there is no text to deliver, with two there is no rule for
    choosing one, and a reading the Recensor did not accept may not carry one.
    """
    armarium = load_stage("7_armarium")
    records = {
        f"art_{n}": {"artifact_id": f"art_{n}", "outcome": "established", "payload": {}}
        for n in range(established)
    }
    context = SimpleNamespace(tree=_ArchetypusTree(records))
    cache = {
        ARCHETYPUS: {
            "artifacts": [
                {"kind": "archetypus", "subject_id": "act_read", "artifact_id": identity}
                for identity in records
            ]
        }
    }
    row = {**_reading_row("act_read", "p1:1", 1), "disposition": "read"}
    review = {"outcome": outcome, "payload": {"release": None}}
    with pytest.raises(FatalAccounting, match=refusal):
        armarium._page_category(context, row, review, cache)


def test_the_orchestrator_reports_the_armariums_own_terminal_outcome():
    """The run's verdict is the last stage's answer, not a second derivation of it.

    Re-deriving `complete` from `payload["aggregate"]` here undid the stage's own
    repair one level up: the Armarium reports its terminal ledger's status, and the
    orchestrator is what a person actually runs. A bundle saying `partial` on its
    own face printed `complete` and exited 0.
    """
    orchestrator = load_stage("orchestrator")
    reconciled_aggregate = {"status": "complete", "reasons": []}

    status, lines = orchestrator.terminal_report(
        {
            "outcome": ArmariumCategory.DELIVERED.value,
            "payload": {"aggregate": reconciled_aggregate},
        }
    )
    assert (status, lines) == ("complete", [])

    status, lines = orchestrator.terminal_report(
        {
            "outcome": ArmariumCategory.HELD_FOR_REVIEW.value,
            "payload": {"aggregate": reconciled_aggregate},
        }
    )
    assert status == "partial"
    # A partial run always names something; here the aggregate has nothing to name.
    assert lines == [
        "the export bundle's terminal ledger is partial while the run aggregate "
        "reconciled; its unresolved units are named in EXPORT_MANIFEST.json's "
        "claims.partial_reasons"
    ]

    status, lines = orchestrator.terminal_report(
        {
            "outcome": ArmariumCategory.HELD_FOR_REVIEW.value,
            "payload": {
                "aggregate": {"status": "partial", "reasons": ["act a2 is held-for-review"]}
            },
        }
    )
    assert (status, lines) == ("partial", ["act a2 is held-for-review"])
