"""Spec 01's seven acceptance tests, driven over the real pipeline.

Meta-invariant #86, verbatim: "A fix proven only on a fixture is not proven."
Load-bearing tests drive REAL producers — real CLIs, real argv, real subprocesses —
over REAL sealed artifacts. Nothing here imports a stage and calls its main(); every
run below shells out exactly as the operator would, so a stage that only works when
imported would fail here rather than pass.

Meta-invariant #88: no test reports success over an empty population. Every loop
asserts an exact expected count.
"""

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_STORED, BadZipFile, ZipFile

import pytest

from common.chairs import ChairIdentity, load_models_toml
from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of, self_hash
from common.contracts.envelope import build_envelope, validate_envelope, verify_input_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import act_id as derive_act_id
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import (
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    PERLECTOR,
    RECENSOR,
    STAGE_DIRECTORIES,
)
from common.imaging import PNG_SIGNATURE, decode_grayscale_png
from common.runtree.store import RunTree
from common.stage import (
    EXIT_FATAL,
    load_fixture,
    open_context,
    page_identity,
    residual_act_ordinal,
    run_config_bindings,
    stage_parser,
)

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE = "synthetic-two-page-v0"

# Each of these is the digest of a whole run tree's relative-path -> file-digest
# inventory, per spec 02's test 9. They are re-pinned in the commit that changes
# what a run writes, and never loosened: "nothing changed" must not be satisfiable
# by a run that is internally consistent but no longer the run these tests
# describe, so the new value belongs in that commit and nowhere else. Which past
# change moved them, and why, is in that commit beside the change itself.
#
# Re-pinned again for the System 03 rebuild. The file count remains fixed, but door
# admissions, Exemplar pages/census, and Armarium export rows now retain original
# filename/digest linkage; the Designator independently verifies that census before
# it creates proposals. Recomputed against the real orchestrator.
#
# Re-pinned for ruling 14 and the final System 03 check. The run authority now carries
# its PDF target, the fixed decoder route is no longer disguised as configuration,
# and every delivered source region retains its complete crop transform. These are
# deliberate record changes, so the whole-tree pins change with them.
# Re-pinned for the round-two merge. The recovery policy values are unchanged,
# but its corrected explanatory text changes the policy file hash deliberately
# sealed into run.json and therefore every downstream artifact config digest.
# Re-pinned again for the CodeRabbit pass on PR #17: an admitted door source now
# records `admitted_source_sha256`, the digest this door actually computed for the
# submitted file. Duplicate accounting used to group on `declared_sha256`, which a
# `SourceEntry` may legally omit — so a legal admission made the duplicate report
# fatal, and fatal *after* every admission had already been published. The artifact
# count is unchanged (42 and 46 below); only the bytes of the admission payloads
# moved, which is what a deliberate record change looks like here.
# Re-pinned once more on 2026-08-05: the shipped PDF render target moved from 400 to
# 300 DPI on Tyrel's instruction, so every PDF-derived page is a different — and
# deliberately different — set of pixels. Artifact counts are again unchanged.
#
# Re-pinned again the same day for a *comment* in `config/pdf_render.toml`, which is
# worth stating plainly because it surprises people: the config digest sealed into
# `run.json` covers the file's bytes, so its prose is part of the configuration a run
# is bound to. Correcting an arithmetic error in an explanatory comment therefore moves
# every downstream artifact digest and these two whole-tree pins with them. That is the
# seal behaving correctly — a run records exactly the configuration text it ran under —
# but it means a documentation-only edit to anything under `config/` lands here.
#
# Re-pinned 2026-08-05 for the same reason a third time, this time a comment in
# `config/recovery.toml`: it still asked Tyrel to confirm which mechanism his "stop at 3"
# ruling governed, months after ruling #18 answered it (he blessed both, and set the
# run-level threshold at more than two). A stale question standing in a merged config is
# how a settled decision gets re-litigated, so the prose was corrected and these two pins
# moved with it. Nothing about the pipeline's behaviour changed.
#
# Moved for the System 09 (Recensor) merge — 42 → 43 and 46 → 47 — for four
# deliberate, recorded changes, not behavioural drift in the pinned scenarios:
#
#   - Every Recensor `review` payload now carries `continuation` (the Recensor's own
#     authoritative continuation link, derived from region evidence rather than
#     trusted from the Designator's `has_continuation` seal flag) and `page_coverage`
#     (the residual-ink finding for every page this act's regions were cut from).
#   - Every `recovery-request` payload now names its `recovery_kind` and that kind's
#     own budget counters, so a page-level allowance can never be spent as a crop.
#   - `config/recovery.toml`'s prose changed, and the new `config/hard_failure.toml`
#     is now sealed into `config_digest` alongside it — the run-level cap is
#     run-bound configuration exactly as the recovery budget is.
#   - The one extra file in each tree is the scoped Recensor partition receipt at
#     `run-health/recensor-partition-receipt.json`, recomputed from the artifacts on
#     disk rather than from any stage manifest.
#
# Moved twice more within the same build, with neither file count changing: a
# `config/hard_failure.toml` prose correction (the ruled threshold is exact, not a
# tunable ceiling) moved its sealed config digest, and the `recovery-requested`
# review shape gained the `page_coverage` field every other shape already carried
# (the happy scenario requests no recovery, so only the review pin moved).
#
# Moved once more for the audit-repair pass that removed two dead `/out/report.md`
# citations from `config/hard_failure.toml`'s comments (audit-c.md F1, audit-d.md
# F7): the config digest covers the file's bytes including its prose, so even a
# citation the code never reads moves every downstream artifact digest. File
# counts and scenario behaviour are unchanged.
#
# Recomputed against the real orchestrator for this merge. PNG entries bind to
# decoded pixels and dimensions rather than one zlib build's compressed bytes;
# every non-image entry remains byte-bound.
#
# Moved once more, same pattern as the two entries above: a stage-09 pre-push
# CodeRabbit pass dropped a `door.py:278-300` line-number citation from
# `config/hard_failure.toml`'s comments (a stable reference stays; a line range
# that drifts as the file is edited does not). File counts and scenario
# behaviour are unchanged; only the sealed config digest moved.
#
# Re-pinned for System 08 (the Perlector build): `run_config_bindings` now folds spec
# 08's run-level witness-context regime, its Lectio-nuda sampling rate, and the digest
# of the new `config/witness_context.toml` declaration into `config_digest` -- the same
# pattern `pdf_target_dpi_override` already used, extended to the two new sealed facts a
# Perlector run carries. That changes every artifact's `config_digest` under both
# scenarios even at the unchanged defaults (`named`, `0`), which is the deliberate
# record change this comment's own rule anticipates. The Perlector also now writes one
# downscaled page-render blob per distinct page an act's regions touch (the dossier's
# page-render reference, spec 08) -- two additional files under both scenarios, since
# both `happy` and `review` touch the same two synthetic pages. File counts move from
# 42 to 44 (happy) and 46 to 48 (review); nothing else about either scenario's shape
# changed.
#
# Re-pinned again in the same build for `proof/build_fixture.py`: two new declared
# scenarios (`engine-truncated-reading`, `no-readable-text-reading`), a second
# `reading_failure` row, and the new `stop_reason` table the truncation detector reads.
# `config_digest` seals the entire parsed fixture dict, not only the scenario a run
# actually chose (`common/stage.py::run_config_bindings`'s `"fixture": fixture`
# binding), so declaring data for scenarios neither `happy` nor `review` uses still
# moves both pins. File counts are unchanged again; only the sealed configuration text
# is bigger.
#
# And re-pinned once more when the two independent builds of System 08 were merged.
# Every payload change is additive and none of them moves a file: each Perlectio now
# carries the declared prompt it was produced through, span-level dissent beside the
# per-chair booleans, and gap evidence naming the Testimonium it came from; the
# page-context render caps its long edge instead of dividing by two and records its
# whole transform; `nuda_approval_ref` joined the sealed configuration. File counts
# stay at 44 and 48 -- the same two page-context blobs, different bytes inside the
# records that reference them.
#
# Re-pinned by the merge audit after restoring the named dossier's model identity
# and resolved provenance, and after binding each page-context render plus its
# sealed source page into the Perlectio's direct inputs. File counts remain 44 and
# 48; the changed bytes are the evidence the reading now honestly retains.
#
# Re-pinned again by the D-7 fix: `prompts.prompt_evidence` now folds a digest of
# the builder's own source (`builder_sha256`) into every prompt record, so a
# later edit to a recipe's builder changes what the record claims about itself
# instead of silently invalidating an old one nothing could detect. File counts
# remain 44 and 48; only the prompt record's bytes changed.
# **These two pins are platform-dependent as of this branch, and that is a finding
# rather than a property of this test.** Diagnosed 2026-08-11 after they failed on
# macOS while passing in a Linux chamber and in CI, on the identical commit:
#
#   - the divergence begins at the Perlector and everything downstream inherits it;
#     stages 1-3 are byte-identical;
#   - it is `dossier.page_renders[0].image_sha256`, a PNG this branch newly binds
#     into the Perlectio's sealed inputs — `main` has no `page_renders` at all;
#   - the two renders' **pixels are identical** (same `Image.tobytes()` digest);
#     only the PNG container differs, 364 bytes against 366, because the macOS and
#     Linux Pillow wheels carry different zlib builds. Same Pillow 12.3.0 both sides.
#
# So ARCHITECTURE invariant 3 holds — the image shown to a model *is* reproducible
# from the Exemplar and the recorded transforms — while the run tree's sealed
# identity is not, because it binds the encoded container rather than the image.
# Two platforms reading the same Exemplar produce different Archetypus and Armarium
# digests, and a run verified across platforms would refuse legitimate reuse.
#
# **Deliberately not re-pinned.** Re-pinning for macOS would break CI, and picking
# either platform's bytes decides by accident a question that should be decided on
# purpose: whether a run's identity binds pixels or bytes. Carried to Tyrel in
# workbench/raw/stage-prs/THE_ONE_DECISION.md.
#
# Re-pinned for the rebase of the System 08 build onto the merged System 09 tree:
# both movements above are now in one tree, so the counts are 45 (happy) and 49
# (review) -- main's Recensor partition receipt plus this branch's two page-render
# blobs per scenario -- and both digests were recomputed from real orchestrator
# runs on the merged tree under `semantic_snapshot_digest` (decoded pixels for
# PNG entries, bytes for everything else). The Perlector's Pillow-written
# page-render blobs decode through Pillow in that helper; the project's minimal
# filter-0 decoder still covers every fixture page.
#
# Moved once more by the pre-push CodeRabbit round: the Lectio nuda dossier's
# region rows no longer carry `witness_covered` (coverage is witness-derived and
# the baseline saw no witnesses), `builder_sha256` binds the whole prompt module
# rather than one function's source, and the witness-context declaration gate
# tightened. File counts are unchanged at 45 and 47+2; both digests re-measured
# from real orchestrator runs.
#
# And once more for the second re-review round: both snapshot decoder paths now
# reduce a PNG to the same grayscale-sample digest (the Pillow fallback was
# hashing mode+raw bytes, so one image could carry two identities), and the
# real-submission door digest gained the four spec-08 settings. Counts
# unchanged; digests re-measured.
#
# And once more after PR #31's CI failed on exactly the hazard this block
# documents: the page-render blobs were written by Pillow, whose Linux wheels
# bundle a different zlib than macOS ships, so the blob bytes — and the
# content-addressed digests sealed into every downstream artifact — differed
# per platform. The render now goes through
# `common.imaging.encode_grayscale_png_deterministic` (filter 0, stored-block
# DEFLATE, every byte fixed by the spec), so these two values are the same on
# every machine that can run the suite. Counts unchanged; digests re-measured.
#
# Re-pinned for spec 10, the Archetypus rebuild, and this one *is* a behaviour change:
# every established record now carries `text_hash`, `text_status`, `annotations` and
# `evidence_ref`, and each run writes one genuinely new file, `6_archetypus/index.json`
# — a rebuildable summary reconciled 1:1 against the acts the Recensor accepted. Both
# trees therefore gain one file each and every downstream digest moves with the
# payload shape. A deliberate record change, not drift.
#
# Re-pinned for the rebase onto the merged System 08 tree (PR #31): the counts are
# 46 (happy) and 50 (review) -- the merged tree's 45/49 plus this branch's
# index.json per run -- and both digests were re-measured from real orchestrator
# runs under `semantic_snapshot_digest`.
#
# Moved once more by the pre-push CodeRabbit round: the index's count field is
# now named `record_count` for what it actually holds (records summarized, with
# `validate_index` proving the tie to the Recensor's accepted set), which
# changes `index.json`'s bytes under both scenarios. Counts unchanged at 46 and
# 50; both digests re-measured from real orchestrator runs.
#
# Re-pinned for the System 06 (Designator) deepening. The Designator now writes five
# new artifacts per happy run and stays proportionally larger under recovery: one
# `secondary-provenance` record (the secondary-proposer chair, resolved and addressed
# every run rather than left unaddressed the day the roster is enabled), one
# `act-group` per proposed act (real geometric grouping evidence, reconciled against
# the declared act bounds — 2 in the shared fixture), and one `conservation` record per
# sealed page this run reached (independent ink-vs-crop reconciliation — 2 pages). Every
# proposal region's crop bounds also changed: capture padding is now genuinely applied
# and clamped to the page edge before cutting, so `region` payloads carry different
# transform bounds, a `raw_bounds`/`padding` provenance pair, and a `transform_digest`
# that were not there before. On this branch's original base the counts moved from
# 42/46 to 47/51 (both scenarios gain the same five kinds; the review scenario's
# recovery loop does not add or remove any of them, since conservation and act-group
# evidence are produced only in `initial_pass`). The merged tree's counts are
# re-measured in the rebase entry below, never carried by arithmetic.
# Re-pinned again for the System 06 deepening's second pass. File counts stay at
# 47/51 — no scenario here produces a conservation residual, so the new
# residual-holding-act mechanism (`common.stage.residual_act_ordinal`,
# `_verify_residual_act_rows`) never fires and adds no artifact to either
# scenario. What moved: every proposal region's `padding` field now carries a
# `provenance` sub-object (`geometry.load_padding_config` / `cut_region`),
# stating plainly that the shipped basis-point values are carried forward from
# a third-party corpus and have not been calibrated against this project's own
# pages — a fact that used to live only in a comment in
# `config/designator_padding.toml` and now travels with the evidence itself.
# That new field, present on every region artifact in both scenarios, is a
# deliberate record change and moves both whole-tree pins with it.
#
# Re-pinned once more when the two independent builds of System 06 were merged.
# Two changes moved these, both deliberate. Counts go 47/51 to 49/53: the
# Designator now publishes one `structure-status` record per sealed page (2 per
# run, both scenarios) so that "the structure pass ran here and succeeded" is a
# record rather than the absence of one. And `config/designator_padding.toml` is
# now sealed into `run.json`'s `config_digest` — padding decides how many pixels
# a witness is shown, so a run reusing an id across a padding change would hold
# two geometries under one name — which moves every downstream artifact digest
# with it, exactly as the pdf_render binding above does.
#
# Re-pinned again for the audit-repair pass: `structure-status`'s success
# `state` was "marked-out", colliding with GLOSSARY's Designator entry (which
# already owns that verb for the stage as a whole) and with the Recensor's own,
# different "marked out" fact about an act. Renamed to "scanned" — counts
# unchanged, only these two artifacts' bytes move.
#
# Re-pinned for the rebase of the System 06 build onto the post-#33 main
# (Systems 08, 10 and 12 merged underneath it): the counts are 53 (happy)
# and 57 (review) — main's 46/50 plus this branch's seven per scenario (five
# Designator evidence kinds from the first deepening plus two
# `structure-status` records) — and both digests were recomputed from real
# orchestrator runs on the merged tree under `semantic_snapshot_digest`.
# The fixture also now declares the union of both sides' scenarios
# (main's confirmed-blank/blank-with-dissent beside this branch's
# structure-failure), which enters `config_digest` and moves every
# downstream artifact digest with it.
#
# Re-pinned for CodeRabbit round 1 on the stage-06 candidate. Every conservation
# record now attributes its independent scan's background source and value. A
# held structure pass can therefore keep null structure evidence while the later
# conservation measurement states the threshold it actually used. Counts stay
# 53/57; both digests were re-measured from real orchestrator runs through this
# module's `orchestrate` and `semantic_snapshot_digest` helpers.
#
# Re-pinned for CodeRabbit round 2. The padding calibration harness now makes
# its caller state whether the supplied gold set belongs to this corpus, and
# the shipped padding config's generation note records that requirement. The
# config is sealed byte-for-byte, so this explanatory correction deliberately
# moves every artifact's config digest while leaving the 53/57 counts intact.
# Both values below were measured from fresh real runs through this module's
# `orchestrate` and `semantic_snapshot_digest` helpers.
#
# Re-pinned for CodeRabbit round 3 after adding the scenario-only ink-free page
# that drives a minted page-fallback act through the real witness and Perlector
# programs. The full parsed fixture is part of `config_digest`, so declaring the
# new page and scenario moves every artifact in happy and review even though that
# page is inactive in both. Counts remain 53/57. Both values were measured from
# fresh runs through the same two helpers, never derived arithmetically.
#
# Re-pinned for the post-#34 System 07 rebase. Testimonia now retain append-only
# attempts with native payloads, explicit non-reading outcomes, deterministic
# channel health, and an independent attempt tally; the fixture also declares
# the merged Designator and Attestatores scenario set. Those deliberate record
# and sealed-fixture changes move both trees. Fresh real runs through this
# module's `orchestrate` and `semantic_snapshot_digest` helpers measured 53 files
# for happy and 57 for review; no file count moved.
#
# Re-pinned for the audit F2 follow-up. The two scenario-specific testimony rows
# carrying `witness_reported` and `format_capabilities` moved from `happy` to the
# dedicated `witness-capabilities` scenario. The reference run now measures
# dissent against all three chairs, while the capability distinction remains
# exercised in its own real orchestrator run. Because the full parsed fixture is
# sealed into every run's `config_digest`, both pins move even though review does
# not use those declarations. Fresh runs through this module's `orchestrate` and
# `semantic_snapshot_digest` helpers measured 53 files for happy (exit 0) and 57
# for review (exit 3); no file count moved.
#
# Re-pinned for System 11: formats.toml is now a sealed run configuration and
# Armarium writes one content-addressed, self-verifying external product bundle.
# The added blob changes each inventory count by one; the changed configuration
# digest deliberately changes every dependent artifact byte.
#
# Re-pinned for the rebase onto post-#35 main. Armarium's bundle adds one file
# to each of main's measured trees, moving happy from 53 to 54 files and review
# from 57 to 58. Fresh real runs through this module's `orchestrate` and
# `semantic_snapshot_digest` helpers measured the values below; happy exited 0
# and review exited 3. These replace the branch's old raw-snapshot pins rather
# than carrying forward its pre-main reason for leaving them platform-specific.
#
# Re-pinned for platform-independent Armarium identity. The bundle entry now
# binds its named members, reducing `acts.sqlite` to its logical schema and rows,
# and the dependent manifest fields bind that semantic digest. It therefore
# measures the exported data rather than SQLite's library-specific container
# stamp at header bytes 96-99:
# https://www.sqlite.org/fileformat.html#the_database_header. All unrelated
# members and manifest fields remain byte-bound. Both values below were measured
# from fresh real orchestrator runs with 54 files/exit 0 and 58 files/exit 3.
#
# Re-pinned for the Unicode-version verification repair. `acts.sqlite` now
# records the build interpreter's Unicode database version beside the normalizer
# revision, so that new logical metadata deliberately moves the semantic bundle
# identity. Fresh real runs again measured 54 files/exit 0 and 58 files/exit 3.
#
# Re-pinned to exclude the two version-local parts of that database from the
# semantic identity: the `export_metadata.unidata_version` environment stamp and
# `act_search.derived_search_text` plus `derived_text_sha256`, its Unicode-version-
# local derived projection. They remain honestly recorded in the product and are
# checked by the verifier's version-aware path added in the preceding repair.
# Binding them here would give every platform a different pin, which is the defect
# this pin fixes rather than a property to preserve. Every literal row and stable
# field -- including `normalizer_revision`, `derived_from_canonical_sha256`, and
# `derived_kind` -- remains bound.
#
# Re-pinned for Stage SM on current main. `run_config_bindings` now seals the
# exact `config/serving_recipes.toml` and `config/pod_placement.toml` bytes into
# `config_digest`, so every dependent artifact truthfully changes identity even
# though serving assembly writes no new file into these offline fixture runs.
# Fresh real orchestrator runs measured 54 files for happy (exit 0) and 58 files
# for review (exit 3); the counts and digests below came from those same trees.
#
# Re-pinned for Stage SM CodeRabbit round 1 after correcting
# `pod_placement.toml`'s square-image arithmetic. Its 1344, 1792 and 2304 are
# longest-edge pixel caps, not total counts; the corrected comment says that
# misread as totals they would describe roughly 37x37, 42x42 and exactly 48x48
# images, not one 36x36 example. The file is sealed byte-for-byte, so this comment-only config
# correction moves every dependent artifact. Fresh real runs again measured 54
# files for happy (exit 0) and 58 for review (exit 3).
HAPPY_RUN_TREE_DIGEST = "c130b5b44a62cacbbfb550ef6555cd9e79f2ad568335a8fecc4f0a53f5c88098"
REVIEW_RUN_TREE_DIGEST = "14f3857580d043e9ce59585f9d20c78e6bf97b39ea68af13bec4e4bda433fa6e"


def orchestrate(
    run_root: Path,
    run_id: str,
    scenario: str,
    *,
    models_config: Path | None = None,
    recovery_config: Path | None = None,
    hard_failure_config: Path | None = None,
    nuda_per_mille: int | None = None,
    nuda_approval_ref: str | None = None,
) -> subprocess.CompletedProcess:
    """Run the pipeline the way a person would, and return the whole result."""
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        FIXTURE,
        "--scenario",
        scenario,
        "--run-id",
        run_id,
        "--run-root",
        str(run_root),
    ]
    if models_config is not None:
        command.extend(("--models-config", str(models_config)))
    if recovery_config is not None:
        command.extend(("--recovery-config", str(recovery_config)))
    if hard_failure_config is not None:
        command.extend(("--hard-failure-config", str(hard_failure_config)))
    if nuda_per_mille is not None:
        command.extend(("--nuda-per-mille", str(nuda_per_mille)))
    if nuda_approval_ref is not None:
        command.extend(("--nuda-approval-ref", nuda_approval_ref))
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def invoke_stage(
    run_root: Path, run_id: str, scenario: str, program: str, **extra
) -> subprocess.CompletedProcess:
    """Run one real stage program against a staged synthetic run tree."""
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(run_root),
        "--run-id",
        run_id,
        "--scenario",
        scenario,
    ]
    for key, value in extra.items():
        command.extend((f"--{key.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


@pytest.mark.parametrize(
    "program",
    (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
        "pipeline/6_archetypus/run.py",
        "pipeline/7_armarium/run.py",
    ),
)
def test_only_attestatores_accepts_the_shared_chair_argument(tmp_path, program):
    """A stage must never report success while ignoring an operator's chair."""
    root = tmp_path / "runs"

    result = invoke_stage(root, "r", "happy", program, chair="attestator_1")

    assert result.returncode == EXIT_FATAL
    assert "ContractError" in result.stderr
    assert "--chair is implemented only by the Attestatores" in result.stderr
    assert not root.exists()


def _run_through_designator(root: Path, run_id: str = "r", scenario: str = "happy") -> None:
    """Run Door, Exemplar, and Designator, refusing a partial setup loudly."""
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
    ):
        result = invoke_stage(root, run_id, scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"


def run_through_recensor(
    run_root: Path, run_id: str, scenario: str = "happy", *, allow_held: bool = False
) -> None:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = invoke_stage(run_root, run_id, scenario, program)
        expected = {0, 3} if allow_held else {0}
        assert result.returncode in expected, f"{program}: {result.stderr}"


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _sqlite_logical_digest(data: bytes) -> str:
    """Bind a SQLite member to its schema and rows, not its library header."""
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise ValueError(
            "pragma_table_list is unavailable: this interpreter reports "
            f"sqlite3.sqlite_version={sqlite3.sqlite_version}; SQLite 3.37.0 or newer is required"
        )
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(data)
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise ValueError(f"SQLite integrity check failed: {integrity!r}")

        schema_sql = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                "ORDER BY type, name, tbl_name, sql"
            )
        ]
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM pragma_table_list "
                "WHERE schema = 'main' AND type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables = {}
        for table_name in table_names:
            escaped_table = table_name.replace('"', '""')
            quoted_table = f'"{escaped_table}"'
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})")]
            if table_name == "act_search":
                excluded = {"derived_search_text", "derived_text_sha256"}
                if not excluded <= set(columns):
                    raise ValueError(
                        "act_search no longer carries the version-local derived columns "
                        "this pin excludes; update the exclusion deliberately"
                    )
                columns = [column for column in columns if column not in excluded]
            quoted_columns = []
            for column in columns:
                escaped_column = column.replace('"', '""')
                quoted_columns.append(f'"{escaped_column}"')
            query = f"SELECT {', '.join(quoted_columns)} FROM {quoted_table}"
            parameters = ()
            if table_name == "export_metadata":
                if "key" not in columns:
                    raise ValueError(
                        "export_metadata has no 'key' column, so the unidata_version "
                        "stamp cannot be excluded from this pin"
                    )
                query += ' WHERE "key" != ?'
                parameters = ("unidata_version",)
            query += " ORDER BY " + ", ".join(str(index) for index in range(1, len(columns) + 1))
            tables[table_name] = {
                "columns": columns,
                "rows": connection.execute(query, parameters).fetchall(),
            }

        return digest_of(
            {
                "schema_sql": schema_sql,
                # `pragma_table_list ... type = 'table'` leaves out the FTS5
                # virtual table and its shadow tables: they are SQLite's own
                # index over `act_search.derived_search_text`, and that column is
                # itself excluded above as Unicode-version-local. Binding the
                # index while excluding what it indexes would put the same
                # version-local bytes back into the pin under another name. Their
                # *schema* stays bound in `schema_sql`, and the fold they encode
                # is checked by the verifier's version-aware recomputation
                # (`armarium_export._verify_search_fold_claim`), not here.
                "tables": tables,
                "application_id": connection.execute("PRAGMA application_id").fetchone()[0],
                "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            }
        )
    except sqlite3.DatabaseError as error:
        raise ValueError("the Armarium SQLite member is unreadable") from error
    finally:
        connection.close()


def _armarium_bundle_semantics(data: bytes) -> tuple[str, dict[str, str]] | None:
    """Return the semantic bundle digest and its derived-hash replacements."""
    if not data.startswith(b"PK\x03\x04"):
        return None
    try:
        with ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or not {"EXPORT_MANIFEST.json", "acts.sqlite"} <= set(
                names
            ):
                return None
            manifest_data = archive.read("EXPORT_MANIFEST.json")
            manifest = json.loads(manifest_data)
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema") != "armarium-export-manifest.v1"
                or canonical_bytes(manifest) != manifest_data
                or manifest.get("self_hash") != self_hash(manifest)
            ):
                return None

            member_names = set(names) - {"EXPORT_MANIFEST.json"}
            listed = manifest.get("members")
            if not isinstance(listed, list):
                return None
            member_rows = {}
            for row in listed:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"path", "sha256", "bytes"}
                    or not isinstance(row.get("path"), str)
                    or row["path"] in member_rows
                ):
                    return None
                member_rows[row["path"]] = row
            if set(member_rows) != member_names:
                return None
            member_data = {name: archive.read(name) for name in member_names}
            if any(
                row.get("sha256") != digest_bytes(member_data[name])
                or row.get("bytes") != len(member_data[name])
                for name, row in member_rows.items()
            ):
                return None

            database_data = member_data["acts.sqlite"]
            logical_database_digest = _sqlite_logical_digest(database_data)
            semantic_manifest = deepcopy(manifest)
            database_rows = [
                row for row in semantic_manifest["members"] if row["path"] == "acts.sqlite"
            ]
            database_rows[0]["sha256"] = logical_database_digest
            semantic_manifest["self_hash"] = self_hash(semantic_manifest)
            semantic_manifest_data = canonical_bytes(semantic_manifest)

            member_inventory = {}
            for name in names:
                if name == "acts.sqlite":
                    member_inventory[name] = logical_database_digest
                elif name == "EXPORT_MANIFEST.json":
                    # This member's SQLite digest and its own self-hash are
                    # consequences of the container bytes. Every other field in
                    # it, and every other member, remains byte-bound.
                    member_inventory[name] = digest_bytes(semantic_manifest_data)
                else:
                    member_inventory[name] = digest_bytes(member_data[name])
    except (BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    return digest_of(member_inventory), {
        digest_bytes(data): digest_of(member_inventory),
        manifest["self_hash"]: semantic_manifest["self_hash"],
    }


def _replace_semantic_digests(value, replacements: dict[str, str]):
    """Replace only exact digest tokens and content-addressed path components."""
    if isinstance(value, str):
        if value in replacements:
            return replacements[value]
        for raw_digest, semantic_digest in replacements.items():
            suffix = f"/{raw_digest}"
            if value.endswith(suffix):
                return f"{value[: -len(raw_digest)]}{semantic_digest}"
        return value
    if isinstance(value, dict):
        return {key: _replace_semantic_digests(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_semantic_digests(item, replacements) for item in value]
    return value


def _semantic_export_artifact(data: bytes, replacements: dict[str, str]) -> bytes | None:
    """Reduce the Armarium export envelope's bundle bindings semantically."""
    try:
        record = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    bundle = payload.get("bundle", {}) if isinstance(payload, dict) else {}
    if (
        record.get("stage") != ARMARIUM
        or record.get("kind") != "export"
        or bundle.get("format") != "zip"
        or bundle.get("sha256") not in replacements
        or canonical_bytes(record) != data
        or record.get("self_hash") != self_hash(record)
    ):
        return None
    semantic = _replace_semantic_digests(record, replacements)
    semantic["self_hash"] = self_hash(semantic)
    return canonical_bytes(semantic)


def _semantic_armarium_manifest(data: bytes, replacements: dict[str, str]) -> bytes | None:
    """Reduce only derived bundle/export digests in the stage inventory."""
    try:
        manifest = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(manifest, dict)
        or manifest.get("stage") != ARMARIUM
        or manifest.get("schema") != "skeleton.v1"
        or canonical_bytes(manifest) != data
        or not any(blob in replacements for blob in manifest.get("blobs", []))
    ):
        return None
    return canonical_bytes(_replace_semantic_digests(manifest, replacements))


def semantic_snapshot(root: Path) -> dict[str, str]:
    """Run-tree inventory with platform-written containers reduced to data.

    PNG blobs bind decoded pixels. The Armarium bundle binds its named member
    inventory, with ``acts.sqlite`` reduced to a deterministic schema-and-row
    dump; the package manifest and run-tree manifest bind the corresponding
    semantic digest rather than derivative container hashes. Everything else
    remains byte-bound. The ordinary ``snapshot`` stays byte-exact for all resume
    and no-write assertions.
    """
    files = [(path, path.read_bytes()) for path in sorted(root.rglob("*")) if path.is_file()]
    bundle_paths = {}
    replacements = {}
    for path, data in files:
        semantics = _armarium_bundle_semantics(data)
        if semantics is None:
            continue
        semantic_digest, bundle_replacements = semantics
        bundle_paths[path] = semantic_digest
        replacements.update(bundle_replacements)

    semantic_files = {}
    for path, data in files:
        semantic = _semantic_export_artifact(data, replacements)
        if semantic is not None:
            semantic_files[path] = semantic
            replacements[digest_bytes(data)] = digest_bytes(semantic)

    for path, data in files:
        semantic = _semantic_armarium_manifest(data, replacements)
        if semantic is not None:
            semantic_files[path] = semantic

    inventory = {}
    for path, data in files:
        relative = str(path.relative_to(root))
        if path in bundle_paths:
            raw_digest = digest_bytes(data)
            semantic_digest = bundle_paths[path]
            if relative.endswith(raw_digest):
                relative = f"{relative[: -len(raw_digest)]}{semantic_digest}"
            inventory[relative] = semantic_digest
        elif path in semantic_files:
            inventory[relative] = digest_bytes(semantic_files[path])
        elif data.startswith(PNG_SIGNATURE):
            try:
                width, height, rows = decode_grayscale_png(data)
                pixel_digest = digest_bytes(b"".join(rows))
            except ValueError:
                # The Perlector's page-render blobs are written by Pillow, whose
                # adaptive PNG filters the project's own minimal decoder refuses
                # by design. The pin still binds pixels, not compressor bytes —
                # only the decoder differs.
                from io import BytesIO

                from PIL import Image

                with Image.open(BytesIO(data)) as image:
                    grayscale = image.convert("L")
                    width, height = grayscale.size
                    pixel_digest = digest_bytes(grayscale.tobytes())
            inventory[relative] = digest_of(
                {
                    "width": width,
                    "height": height,
                    "pixel_sha256": pixel_digest,
                }
            )
        else:
            inventory[relative] = digest_bytes(data)
    return inventory


def semantic_snapshot_digest(root: Path) -> str:
    """The canonical content pin for the full relative run-tree inventory."""
    return digest_of(semantic_snapshot(root))


def test_semantic_snapshot_digest_binds_png_pixels_not_compressor_bytes(tmp_path, monkeypatch):
    """A different valid DEFLATE stream must not rename the measured run."""
    import common.imaging as imaging

    image_path = tmp_path / "blob-with-no-extension"
    rows = [bytearray([0, 127, 255]), bytearray([255, 127, 0])]
    image_path.write_bytes(imaging.encode_grayscale_png(3, 2, rows))
    raw_before = snapshot(tmp_path)
    semantic_before = semantic_snapshot_digest(tmp_path)

    compress = imaging.zlib.compress
    monkeypatch.setattr(
        imaging.zlib,
        "compress",
        lambda data, *args, **kwargs: compress(data, level=0),
    )
    image_path.write_bytes(imaging.encode_grayscale_png(3, 2, rows))

    assert snapshot(tmp_path) != raw_before
    assert semantic_snapshot_digest(tmp_path) == semantic_before


def _acceptance_sqlite(
    path: Path,
    text: str,
    *,
    unidata_version: str = "15.1.0",
    derived_search_text: str = "original derived text",
    derived_from_canonical_sha256: str | None = None,
) -> bytes:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version=1")
        connection.executescript(
            """
            CREATE TABLE export_metadata (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE acts (act_id TEXT PRIMARY KEY, text TEXT NOT NULL);
            CREATE TABLE act_search (
                rowid INTEGER PRIMARY KEY,
                act_id TEXT UNIQUE NOT NULL REFERENCES acts(act_id),
                derived_search_text TEXT NOT NULL,
                derived_text_sha256 TEXT NOT NULL,
                derived_from_canonical_sha256 TEXT NOT NULL,
                normalizer_revision TEXT NOT NULL,
                derived_kind TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO export_metadata VALUES (?, ?)",
            (
                ("normalizer_revision", "armarium-textnorm-v1"),
                ("unidata_version", unidata_version),
            ),
        )
        connection.execute("INSERT INTO acts VALUES ('a1', ?)", (text,))
        if derived_from_canonical_sha256 is None:
            derived_from_canonical_sha256 = digest_bytes(text.encode("utf-8"))
        connection.execute(
            "INSERT INTO act_search VALUES (1, 'a1', ?, ?, ?, ?, ?)",
            (
                derived_search_text,
                digest_bytes(derived_search_text.encode("utf-8")),
                derived_from_canonical_sha256,
                "armarium-textnorm-v1",
                "search-fold",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return path.read_bytes()


def test_sqlite_pin_reducer_refuses_a_renamed_excluded_column(tmp_path):
    """An exclusion dependency must fail by name, not silently re-platform the pin."""
    path = tmp_path / "renamed-column.sqlite"
    _acceptance_sqlite(path, "original row")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "ALTER TABLE act_search RENAME COLUMN derived_search_text "
            "TO renamed_derived_search_text"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="act_search"):
        _sqlite_logical_digest(path.read_bytes())


def test_sqlite_pin_reducer_names_the_version_when_pragma_table_list_is_unavailable(monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 0))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.36.0")

    with pytest.raises(
        ValueError,
        match=r"sqlite3\.sqlite_version=3\.36\.0; SQLite 3\.37\.0 or newer is required",
    ):
        _sqlite_logical_digest(b"not reached")


def _write_acceptance_bundle_tree(root: Path, database_data: bytes, damage=None) -> None:
    """Write a whole run tree around one bundle, optionally damaged from the inside.

    ``damage`` mutates the package manifest *after* it is written and before the tree
    is addressed, so everything outside the archive -- the blob's content-addressed
    name, the export artifact's digests and self-hash, the stage manifest -- is
    rebuilt consistently around the damaged bytes. That is the tree a forger leaves,
    and it is the only one in which the reducer's own integrity guards are the thing
    under test rather than a stale filename.
    """
    members = {"acts.sqlite": database_data, "acts.jsonl": b'{"act_id":"a1"}\n'}
    package_manifest = {
        "schema": "armarium-export-manifest.v1",
        "members": [
            {"path": name, "sha256": digest_bytes(content), "bytes": len(content)}
            for name, content in sorted(members.items())
        ],
    }
    package_manifest["self_hash"] = self_hash(package_manifest)
    if damage is not None:
        damage(package_manifest)
    members["EXPORT_MANIFEST.json"] = canonical_bytes(package_manifest)
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED) as archive:
        for name, content in sorted(members.items()):
            archive.writestr(name, content)
    bundle_data = buffer.getvalue()
    bundle_digest = digest_bytes(bundle_data)
    bundle_relative = f"7_armarium/blobs/sha256/{bundle_digest}"

    bundle_path = root / bundle_relative
    bundle_path.parent.mkdir(parents=True)
    bundle_path.write_bytes(bundle_data)
    export = {
        "schema": "skeleton.v1",
        "stage": ARMARIUM,
        "kind": "export",
        "payload": {
            "bundle": {
                "format": "zip",
                "sha256": bundle_digest,
                "manifest_self_hash": package_manifest["self_hash"],
                "reference": {"relative_path": bundle_relative, "sha256": bundle_digest},
            },
            "unrelated": "remains-byte-bound",
        },
        "inputs": [{"relative_path": bundle_relative, "sha256": bundle_digest}],
    }
    export["self_hash"] = self_hash(export)
    export_data = canonical_bytes(export)
    export_path = root / "7_armarium/artifacts/export/example.json"
    export_path.parent.mkdir(parents=True)
    export_path.write_bytes(export_data)
    stage_manifest = {
        "schema": "skeleton.v1",
        "stage": ARMARIUM,
        "run_id": "r",
        "artifacts": [
            {
                "kind": "export",
                "relative_path": "7_armarium/artifacts/export/example.json",
                "sha256": digest_bytes(export_data),
            }
        ],
        "blobs": [bundle_digest],
    }
    (root / "7_armarium/manifest.json").write_bytes(canonical_bytes(stage_manifest))


def test_semantic_snapshot_digest_binds_sqlite_rows_not_library_header(tmp_path):
    """Version-local database fields cannot rename a run; a literal row can."""
    database = _acceptance_sqlite(tmp_path / "database.sqlite", "original row")
    version_local = _acceptance_sqlite(
        tmp_path / "version-local.sqlite",
        "original row",
        unidata_version="16.0.0",
        derived_search_text="different version-local derived text",
    )
    doctored = version_local[:96] + b"\xff\xff\xff\xff" + version_local[100:]
    original_root = tmp_path / "original"
    doctored_root = tmp_path / "doctored"
    changed_root = tmp_path / "changed"
    _write_acceptance_bundle_tree(original_root, database)
    _write_acceptance_bundle_tree(doctored_root, doctored)
    changed = _acceptance_sqlite(
        tmp_path / "changed.sqlite",
        "changed row",
        derived_from_canonical_sha256=digest_bytes(b"original row"),
    )
    _write_acceptance_bundle_tree(changed_root, changed)

    assert snapshot(original_root) != snapshot(doctored_root)
    assert semantic_snapshot_digest(original_root) == semantic_snapshot_digest(doctored_root)
    assert semantic_snapshot_digest(original_root) != semantic_snapshot_digest(changed_root)


def test_semantic_snapshot_refuses_damaged_persisted_integrity_fields(tmp_path):
    """Integrity damage stays byte-bound instead of being normalized out of the pin.

    The two bundle-internal cases are the ones the reduction would otherwise *erase*:
    it recomputes the package manifest's `self_hash` and overwrites the `acts.sqlite`
    member row's `sha256` with the logical digest, so without the reducer's own
    integrity guards a manifest lying about either would reduce to exactly the same
    pin as an honest one. Both trees are written whole, so the blob's content address,
    the export artifact and the stage manifest all agree with the damaged bytes and
    nothing incidental distinguishes them.
    """
    database = _acceptance_sqlite(tmp_path / "database.sqlite", "original row")
    original_root = tmp_path / "original"
    _write_acceptance_bundle_tree(original_root, database)
    original_semantic = semantic_snapshot_digest(original_root)

    manifest_hash_root = tmp_path / "manifest-self-hash"
    member_digest_root = tmp_path / "member-digest"
    export_hash_root = tmp_path / "export-self-hash"

    def damage_manifest_hash(manifest: dict) -> None:
        manifest["self_hash"] = "b" * 64

    def damage_database_member_digest(manifest: dict) -> None:
        row = next(item for item in manifest["members"] if item["path"] == "acts.sqlite")
        row["sha256"] = "d" * 64
        manifest["self_hash"] = self_hash(
            {key: value for key, value in manifest.items() if key != "self_hash"}
        )

    _write_acceptance_bundle_tree(manifest_hash_root, database, damage=damage_manifest_hash)
    _write_acceptance_bundle_tree(
        member_digest_root, database, damage=damage_database_member_digest
    )
    shutil.copytree(original_root, export_hash_root)
    export_path = export_hash_root / "7_armarium/artifacts/export/example.json"
    export = json.loads(export_path.read_bytes())
    export["self_hash"] = "c" * 64
    export_path.write_bytes(canonical_bytes(export))

    for root in (manifest_hash_root, member_digest_root, export_hash_root):
        assert snapshot(root) != snapshot(original_root)
        assert semantic_snapshot_digest(root) != original_semantic


def export_of(tree: RunTree) -> dict:
    return tree.read_artifact(ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None))[
        "payload"
    ]


@pytest.fixture(scope="module")
def happy_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("happy")
    result = orchestrate(root, "r", "happy")
    assert result.returncode == 0, result.stderr
    return root, RunTree(root, "r")


@pytest.fixture(scope="module")
def review_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("review")
    result = orchestrate(root, "r", "review")
    # Exit 3 is "accounted, holdable" — the run reached honest terminal states and
    # one act is held. A zero here would be the vacuous green this project exists
    # to notice.
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r")


# --- 1. The happy path runs offline, and every reference resolves --------------


def test_the_happy_path_runs_and_establishes_both_acts(happy_run):
    _, tree = happy_run
    export = export_of(tree)
    assert export["aggregate"]["status"] == "complete"
    assert export["aggregate"]["reasons"] == []
    assert len(export["delivered"]) == 2
    assert export["non_delivered"] == []
    assert {item["category"] for item in export["delivered"]} == {"delivered"}


def test_every_input_reference_in_the_run_resolves_and_matches_its_digest(happy_run):
    """The whole traceability claim in one assertion: every artifact names the
    bytes it was derived from, and every one of those references is real."""
    _, tree = happy_run
    checked = 0
    for stage in (DOOR, EXEMPLAR, DESIGNATOR, ATTESTATORES, PERLECTOR, RECENSOR, ARCHETYPUS):
        for entry in tree.build_manifest(stage)["artifacts"]:
            record = tree.read_artifact(stage, entry["kind"], entry["artifact_id"])
            for reference in record["inputs"]:
                verify_input_bytes(reference, tree.read_bytes(reference["relative_path"]))
                checked += 1
    assert checked >= 20, f"only {checked} references checked; the run looks too thin"


def test_every_expected_act_has_exactly_one_terminal_category(happy_run):
    _, tree = happy_run
    export = export_of(tree)
    entries = [
        tree.read_artifact(ARMARIUM, "manifest-entry", entry["artifact_id"])
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "manifest-entry"
    ]
    assert len(entries) == export["expected_acts"] == 2
    assert len({entry["subject_id"] for entry in entries}) == 2


def test_the_final_export_keeps_each_original_filename_and_digest_link(happy_run):
    """Every exported crop names its original source file/frame, not just the pages list."""
    _, tree = happy_run
    export = export_of(tree)
    source_by_ordinal = {row["ordinal"]: row for row in tree.read_run()["source_manifest"]}
    assert len(export["pages"]) == len(source_by_ordinal) == 2
    pages_by_ordinal = {page["ordinal"]: page for page in export["pages"]}
    for page in export["pages"]:
        source = source_by_ordinal[page["ordinal"]]
        assert page["declared_path"] == source["relative_path"]
        assert page["declared_sha256"] == source["sha256"]
        assert page["page_id"]
    for delivered in export["delivered"]:
        assert delivered["source_regions"]
        perlectio = tree.read_artifact_reference(
            delivered["perlectio_ref"],
            stage=PERLECTOR,
            kind="perlectio",
            subject_id=delivered["act_id"],
        )
        assert delivered["dissent_ref"] == delivered["perlectio_ref"]
        assert len(delivered["witnesses"]) == 3
        assert {witness["chair"] for witness in delivered["witnesses"]} == {
            "attestator_1",
            "attestator_2",
            "attestator_3",
        }
        assert all("reported" not in witness for witness in delivered["witnesses"])
        assert perlectio["artifact_id"] == delivered["perlectio_ref"]["relative_path"].split("/")[
            -1
        ].removesuffix(".json")
        for witness in delivered["witnesses"]:
            testimony = tree.read_artifact_reference(
                witness["testimonium_ref"],
                stage=ATTESTATORES,
                kind="testimonium",
                subject_id=delivered["act_id"],
            )
            assert testimony["payload"]["chair"] == witness["chair"]
            assert testimony["payload"]["provenance"] == witness["provenance"]
        for region in delivered["source_regions"]:
            source = source_by_ordinal[region["source_page_ordinal"]]
            page = pages_by_ordinal[region["source_page_ordinal"]]
            assert region["source_page_id"] == page["page_id"]
            assert region["declared_path"] == source["relative_path"]
            assert region["declared_sha256"] == source["sha256"]
            assert region["structure_provenance"]["chair"] == "designator_structure"


def test_a_genuinely_empty_testimonium_counts_as_a_witnessed_read(tmp_path):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "genuinely-empty-witness")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    empty = next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
        and entry["outcome"] == "genuinely-empty"
        and entry["subject_id"]
    )
    assert empty["payload"]["chair"] == "attestator_3"
    assert empty["payload"]["content_health"] == {
        "native_type": "string",
        "encoding": "utf-8-json-native",
        "recordable": True,
        "empty": True,
        "blank": True,
        "truncated": False,
        "characters": 0,
        "truncation_basis": "trusted-response-boundary",
    }
    assert empty["payload"]["payload"] == ""
    assert empty["payload"]["witness_reported"] is None
    reading = next(
        tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio" and entry["subject_id"] == empty["subject_id"]
    )
    assert all(region["witness_covered"] for region in reading["payload"]["basis"]["regions"])
    assert export_of(tree)["aggregate"]["status"] == "complete"


def test_an_ink_free_page_fallback_is_witnessed_and_read_end_to_end(tmp_path):
    """A Designator-minted act reaches both reader stages without fixture-key lookup failure."""
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "ink-free-page")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")

    testimonia = []
    for artifact in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if artifact["kind"] != "testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "testimonium", artifact["artifact_id"])
        if record["payload"]["act_key"] == "page-fallback:3":
            testimonia.append(record)
    assert len(testimonia) == 3
    assert all(record["outcome"] == "genuinely-empty" for record in testimonia)
    assert all(record["payload"]["regions"] for record in testimonia)

    reading = next(
        tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio"
        and tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])["payload"]["act_key"]
        == "page-fallback:3"
    )
    assert reading["outcome"] == "no-readable-text"
    assert reading["payload"]["text"] == ""
    assert all(region["witness_covered"] for region in reading["payload"]["basis"]["regions"])

    entry = next(
        row for row in export_of(tree)["non_delivered"] if row["act_key"] == "page-fallback:3"
    )
    assert entry["act_id"] == reading["subject_id"]
    assert entry["category"] == "confirmed-blank"


def test_a_shortened_resealed_proposal_denominator_stops_the_first_consumer(tmp_path):
    """The fixture's a2 cannot silently disappear from the downstream denominator."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
    ):
        result = invoke_stage(root, "r", "happy", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    tree = RunTree(root, "r")
    seal_id = artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal")
    path = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal-seal", seal_id))
    seal = json.loads(path.read_text(encoding="utf-8"))
    seal["payload"]["expected_acts"] = seal["payload"]["expected_acts"][:1]
    seal["payload"]["count"] = 1
    seal["payload"]["self_hash"] = self_hash(seal["payload"])
    seal["self_hash"] = self_hash(seal)
    path.write_bytes(canonical_bytes(seal))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == 2
    assert "does not reconcile to every synthetic act" in result.stderr
    assert snapshot(root) == before


def _designator_context_for(root: Path, run_id: str, scenario: str):
    """A real Designator `StageContext` over an already-created run tree.

    Opened the way `pipeline/2_designator/run.py`'s own CLI would open one,
    not fabricated — the same seam `test_recovery_idempotency.py` uses. This
    is enough to publish a well-formed `hold` artifact with `context.publish`,
    which is all these tests need: the actual minting logic under test lives
    in `common.stage._verify_residual_act_rows`, exercised by real subprocess
    consumers below, per meta-invariant #86.
    """
    args = stage_parser("test-only residual denominator context").parse_args(
        ["--run-root", str(root), "--run-id", run_id, "--scenario", scenario]
    )
    return open_context(args, DESIGNATOR)


def _mint_test_residual_row(
    context,
    page_id: str,
    page_ordinal: int,
    index: int,
    bounds: dict,
    *,
    hold_bounds: dict | None = None,
    conservation_ref: dict[str, str] | None = None,
) -> dict:
    """Publish one residual-shaped `hold` and return its expected-act seal row.

    Mirrors `pipeline/2_designator/run.py::hold_residual_act` exactly enough to
    exercise `common.stage`'s verification of it. `hold_bounds`, when different
    from `bounds`, lets a test forge a hold whose recorded facts do not match
    the identity the row claims. `conservation_ref`, when given, is the real
    on-disk conservation record `_patch_conservation_with_extra_residual`
    below prepared to actually carry this residual, so the hold references it
    exactly as the real `hold_residual_act` does — needed only by the test that
    exercises `_verify_residual_traces_to_conservation`; every other test here
    is refused before that check ever runs and stays independent of a real
    conservation residual, the *denominator* check being what they exercise.
    """
    ordinal = residual_act_ordinal(index)
    act_id = derive_act_id(page_id, ordinal, bounds)
    hold = context.publish(
        kind="hold",
        subject_id=act_id,
        outcome="held",
        inputs=[conservation_ref] if conservation_ref is not None else [],
        payload={
            "act_key": f"residual:{page_ordinal}:{index}",
            "page_ordinal": page_ordinal,
            "residual_ordinal": ordinal,
            "residual_bounds": hold_bounds if hold_bounds is not None else bounds,
            "residual_pixel_count": bounds["w"] * bounds["h"],
            "reason": "test-minted residual hold",
        },
    )
    context.finish()
    return {
        "act_id": act_id,
        "act_key": f"residual:{page_ordinal}:{index}",
        "page_id": page_id,
        "page_ordinal": page_ordinal,
        "has_continuation": False,
        "outcome": "held",
        "evidence": [context.input_ref(hold.relative_path)],
    }


def _patch_conservation_with_extra_residual(
    tree: RunTree, page_id: str, bounds: dict, pixel_count: int
) -> dict[str, str]:
    """Append one residual component to a page's real, on-disk conservation record.

    Conservation is a once-only artifact (`context.publish` may not produce a
    second one for the same page), so a test that needs a *real* reconciliation
    pass to have found a given residual edits the sealed bytes directly, the
    same way `_reseal_with_extra_row` extends the seal — and returns the
    digest-checked reference a hold can then cite honestly, exactly as
    `pipeline/2_designator/run.py::hold_residual_act` does for a genuine one.
    """
    conservation_id = artifact_id(DESIGNATOR, "conservation", page_id)
    relative_path = tree.artifact_path(DESIGNATOR, "conservation", conservation_id)
    path = tree.resolve(relative_path)
    record = json.loads(path.read_text(encoding="utf-8"))
    components = record["payload"]["residual_components"]
    assert not components, (
        f"page {page_id} already reconciles {len(components)} residual component(s); "
        "these tests mint their added residual at index 0"
    )
    components.append({"bounds": bounds, "pixel_count": pixel_count, "review_priority": "low"})
    record["payload"]["residual_pixel_count"] += pixel_count
    record["payload"]["total_ink_pixel_count"] += pixel_count
    record["self_hash"] = self_hash(record)
    data = canonical_bytes(record)
    path.write_bytes(data)
    return {"relative_path": relative_path, "sha256": digest_bytes(data)}


def _reseal_with_extra_row(tree: RunTree, row: dict, *, include_hold_evidence: bool = True) -> None:
    """Append one expected-act row to the real, on-disk proposal seal.

    Recomputes both self-hashes exactly as the precedent shortened-denominator
    test above does, so this stays a well-formed, digest-checked artifact —
    the seal's OWN identity does not change, only the append-only inventory
    a real second `hold` publish already added to the tree.
    """
    seal_id = artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal")
    path = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal-seal", seal_id))
    seal = json.loads(path.read_text(encoding="utf-8"))
    seal["payload"]["expected_acts"].append(row)
    seal["payload"]["count"] = len(seal["payload"]["expected_acts"])
    if include_hold_evidence:
        seal["inputs"].extend(row["evidence"])
    seal["payload"]["self_hash"] = self_hash(seal["payload"])
    seal["self_hash"] = self_hash(seal)
    path.write_bytes(canonical_bytes(seal))


def test_a_well_formed_residual_act_extends_the_denominator_and_the_first_consumer_accepts_it(
    tmp_path,
):
    """A conservation residual's held act is not a fixture act, and is accepted anyway.

    `expected_acts`'s floor is still every fixture act; a residual is the one
    kind of *additional* row it may carry, verified against its own hold
    record rather than trusted because the seal says so.
    """
    root = tmp_path / "runs"
    _run_through_designator(root)

    tree = RunTree(root, "r")
    context = _designator_context_for(root, "r", "happy")
    page_id = page_identity(context.fixture, 1)
    bounds = {"x": 1, "y": 1, "w": 2, "h": 2}
    conservation_ref = _patch_conservation_with_extra_residual(tree, page_id, bounds, 4)
    row = _mint_test_residual_row(context, page_id, 1, 0, bounds, conservation_ref=conservation_ref)
    _reseal_with_extra_row(tree, row)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == 0, result.stderr
    testimonia = [
        record
        for record in artifacts(tree, ATTESTATORES, "testimonium")
        if record["payload"]["act_key"] == "residual:1:0"
    ]
    # Held from the moment it exists: every configured chair still gets an
    # explicit not-run, exactly as any other held act, and never a read —
    # nothing witnessed this ink and this stage may not manufacture a witness.
    assert len(testimonia) == 3
    assert {record["outcome"] for record in testimonia} == {"not-run"}


def test_a_self_consistent_residual_with_no_matching_conservation_component_is_refused(tmp_path):
    """A residual must trace to the reconciliation pass that found it, not merely
    be internally self-consistent.

    The hold below recomputes its own identity correctly — `residual_ordinal`
    and `residual_bounds` agree with the act id the seal row claims, exactly as
    the well-formed case above. What is missing is any conservation record
    that actually reconciled this rectangle as residual ink: the hold carries
    no evidence reference at all. Before `_verify_residual_traces_to_conservation`
    existed, this was accepted anyway — a residual invented from nothing, so
    long as whoever invented it also recomputed the identity correctly.
    """
    root = tmp_path / "runs"
    _run_through_designator(root)

    tree = RunTree(root, "r")
    context = _designator_context_for(root, "r", "happy")
    page_id = page_identity(context.fixture, 1)
    row = _mint_test_residual_row(context, page_id, 1, 0, {"x": 1, "y": 1, "w": 2, "h": 2})
    _reseal_with_extra_row(tree, row)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == EXIT_FATAL
    assert "does not reference exactly one conservation" in result.stderr


def test_a_residual_whose_bounds_do_not_match_its_own_conservation_record_is_refused(tmp_path):
    """The conservation record the hold references must actually carry this residual.

    The hold's own ordinal and bounds still recompute the claimed identity
    correctly, and it references a real conservation record — but at that
    residual's ordinal, the referenced record's own `residual_components` name
    a different rectangle. Self-consistency plus a reference is not the same
    as a reference that actually corroborates the claim.
    """
    root = tmp_path / "runs"
    _run_through_designator(root)

    tree = RunTree(root, "r")
    context = _designator_context_for(root, "r", "happy")
    page_id = page_identity(context.fixture, 1)
    claimed_bounds = {"x": 1, "y": 1, "w": 2, "h": 2}
    recorded_bounds = {"x": 50, "y": 50, "w": 2, "h": 2}
    conservation_ref = _patch_conservation_with_extra_residual(tree, page_id, recorded_bounds, 4)
    row = _mint_test_residual_row(
        context, page_id, 1, 0, claimed_bounds, conservation_ref=conservation_ref
    )
    _reseal_with_extra_row(tree, row)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == EXIT_FATAL
    assert "does not carry at that ordinal" in result.stderr


def test_a_residual_act_claiming_to_be_proposed_is_refused(tmp_path):
    """A residual may only ever be `held`; it was never a structural proposal."""
    root = tmp_path / "runs"
    _run_through_designator(root)

    tree = RunTree(root, "r")
    context = _designator_context_for(root, "r", "happy")
    page_id = page_identity(context.fixture, 1)
    row = _mint_test_residual_row(context, page_id, 1, 0, {"x": 1, "y": 1, "w": 2, "h": 2})
    row["outcome"] = "proposed"
    _reseal_with_extra_row(tree, row)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == EXIT_FATAL
    assert "is not 'held'" in result.stderr


def test_a_residual_act_claiming_a_continuation_is_refused(tmp_path):
    """A residual has no declared continuation to claim."""
    root = tmp_path / "runs"
    _run_through_designator(root)

    tree = RunTree(root, "r")
    context = _designator_context_for(root, "r", "happy")
    page_id = page_identity(context.fixture, 1)
    row = _mint_test_residual_row(context, page_id, 1, 0, {"x": 1, "y": 1, "w": 2, "h": 2})
    row["has_continuation"] = True
    _reseal_with_extra_row(tree, row)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == EXIT_FATAL
    assert "has no declared continuation to claim" in result.stderr


def test_a_residual_act_whose_hold_bounds_do_not_verify_is_refused(tmp_path):
    """A residual's identity must recompute from its own hold record, not be trusted.

    The seal row's `act_id` is derived from one rectangle; the hold record
    published beside it names a different one. A reader that trusted the seal
    row alone would never notice — this is exactly the forged-evidence shape
    `_verify_residual_act_rows` exists to catch.
    """
    root = tmp_path / "runs"
    _run_through_designator(root)

    tree = RunTree(root, "r")
    context = _designator_context_for(root, "r", "happy")
    page_id = page_identity(context.fixture, 1)
    row = _mint_test_residual_row(
        context,
        page_id,
        1,
        0,
        {"x": 1, "y": 1, "w": 2, "h": 2},
        hold_bounds={"x": 9, "y": 9, "w": 2, "h": 2},
    )
    _reseal_with_extra_row(tree, row)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == EXIT_FATAL
    assert "does not verify against the residual ordinal and bounds" in result.stderr


def test_a_residual_act_with_no_hold_record_is_refused(tmp_path):
    """An extra act is not accounted for merely because the seal names it."""
    root = tmp_path / "runs"
    _run_through_designator(root)

    tree = RunTree(root, "r")
    context = _designator_context_for(root, "r", "happy")
    page_id = page_identity(context.fixture, 1)
    ordinal = residual_act_ordinal(0)
    bounds = {"x": 1, "y": 1, "w": 2, "h": 2}
    row = {
        "act_id": derive_act_id(page_id, ordinal, bounds),
        "act_key": "residual:1:0",
        "page_id": page_id,
        "page_ordinal": 1,
        "has_continuation": False,
        "outcome": "held",
        "evidence": [],
    }
    _reseal_with_extra_row(tree, row, include_hold_evidence=False)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == EXIT_FATAL
    assert "published no hold record" in result.stderr


def test_a_conservation_residual_the_seal_never_minted_is_refused(tmp_path):
    """The other direction: a residual the denominator was never told about.

    Every test above checks a seal row against the reconciliation that should
    have produced it. That direction cannot see the row that was never written.
    Here the Designator's own conservation record declares unclaimed ink and the
    proposal seal names no act for it — no forged hold, no extra row, nothing to
    be caught as unaccounted evidence, because a residual that never became a
    hold leaves no artifact behind. Before this check the run reconciled
    perfectly and exited `complete` over ink the stage itself measured and no
    crop claimed, which is exactly what GOVERNANCE 2 refuses.
    """
    root = tmp_path / "runs"
    _run_through_designator(root)

    tree = RunTree(root, "r")
    context = _designator_context_for(root, "r", "happy")
    page_id = page_identity(context.fixture, 1)
    _patch_conservation_with_extra_residual(tree, page_id, {"x": 1, "y": 1, "w": 9, "h": 9}, 81)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == EXIT_FATAL
    assert "accounts for no held act for" in result.stderr


def test_recensor_refuses_duplicate_witness_attempt_ordinals_instead_of_selecting_one(tmp_path):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        result = invoke_stage(root, "r", "happy", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    tree = RunTree(root, "r")
    original = next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium" and entry["outcome"] == "read"
    )
    act_id = original["subject_id"]
    chair = original["payload"]["chair"]
    forged = json.loads(json.dumps(original))
    forged_attempt = attempt_id(act_id, f"read:{chair}", 2)
    forged["attempt_id"] = forged_attempt
    forged["artifact_id"] = artifact_id(ATTESTATORES, "testimonium", act_id, forged_attempt)
    # A new artifact identity with the old semantic ordinal is an ambiguity, not
    # an attempt 2 that the Recensor is allowed to choose among.
    forged["self_hash"] = self_hash(forged)
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", forged["artifact_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(forged))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 2
    assert "duplicate attempt ordinal" in result.stderr
    assert snapshot(root) == before


def test_designator_refuses_a_commandless_recovery_recrop(tmp_path):
    root = tmp_path / "runs"
    # This must be a real outstanding request.  An empty tree would fail later
    # for lack of any Recensor record even if the CLI guard disappeared, making
    # the test a check of an error spelling rather than a check that the request
    # argument is required to authorize a crop.
    run_through_recensor(root, "r", "review", allow_held=True)
    tree = RunTree(root, "r")
    act_id = next(
        tree.read_artifact(RECENSOR, "recovery-request", entry["artifact_id"])["subject_id"]
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "recovery-request"
    )
    before = snapshot(root)

    result = invoke_stage(
        root,
        "r",
        "review",
        "pipeline/2_designator/run.py",
        operation="recover",
        act=act_id,
    )
    assert result.returncode == 2
    assert "exact Recensor recovery request" in result.stderr
    assert snapshot(root) == before


def test_designator_refuses_a_standalone_next_recovery_request(tmp_path):
    """Only the latest Recensor review can authorize its exact request.

    First make the legitimate first recrop and reread, then add a syntactically
    sound second request.  It has the right act, next ordinal, policy, and new
    Perlectio input, but no current review names it.  Before the cross-stage
    check, direct invocation of the crop author accepted this unreviewed request.
    """
    root = tmp_path / "runs"
    run_through_recensor(root, "r", "review", allow_held=True)
    tree = RunTree(root, "r")
    original = next(
        tree.read_artifact(RECENSOR, "recovery-request", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "recovery-request"
    )
    act_id = original["subject_id"]
    assert (
        invoke_stage(
            root,
            "r",
            "review",
            "pipeline/2_designator/run.py",
            operation="recover",
            act=act_id,
            recovery_request=original["artifact_id"],
        ).returncode
        == 0
    )
    assert (
        invoke_stage(
            root,
            "r",
            "review",
            "pipeline/4_perlector/run.py",
            act=act_id,
        ).returncode
        == 0
    )

    latest_reading = max(
        (
            tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
            for entry in tree.build_manifest(PERLECTOR)["artifacts"]
            if entry["kind"] == "perlectio" and entry["subject_id"] == act_id
        ),
        key=lambda record: record["payload"]["attempt_ordinal"],
    )
    reading_path = tree.artifact_path(PERLECTOR, "perlectio", latest_reading["artifact_id"])
    reading_ref = {
        "relative_path": reading_path,
        "sha256": digest_bytes(tree.read_bytes(reading_path)),
    }
    forged = json.loads(json.dumps(original))
    forged_attempt = attempt_id(act_id, "recover", 2)
    forged["attempt_id"] = forged_attempt
    forged["artifact_id"] = artifact_id(RECENSOR, "recovery-request", act_id, forged_attempt)
    forged["inputs"] = [reading_ref]
    forged["payload"]["attempt_ordinal"] = 2
    forged["payload"]["budget_used"] = 1
    forged["payload"]["perlectio_ref"] = reading_ref
    forged["self_hash"] = self_hash(forged)
    forged_path = tree.resolve(
        tree.artifact_path(RECENSOR, "recovery-request", forged["artifact_id"])
    )
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_bytes(canonical_bytes(forged))
    before = snapshot(root)

    result = invoke_stage(
        root,
        "r",
        "review",
        "pipeline/2_designator/run.py",
        operation="recover",
        act=act_id,
        recovery_request=forged["artifact_id"],
    )
    assert result.returncode == 2
    assert "not the exact current Recensor request" in result.stderr
    assert snapshot(root) == before


def test_designator_refuses_a_current_recovery_review_with_a_different_policy(tmp_path):
    """The review's policy is evidence, not a decorative copy of the request's."""
    root = tmp_path / "runs"
    run_through_recensor(root, "r", "review", allow_held=True)
    tree = RunTree(root, "r")
    request = next(
        tree.read_artifact(RECENSOR, "recovery-request", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "recovery-request"
    )
    review_entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "recovery-requested"
    )
    review_path = tree.resolve(review_entry["relative_path"])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["payload"]["recovery_policy"] = {"not": "the run-bound policy"}
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))
    before = snapshot(root)

    result = invoke_stage(
        root,
        "r",
        "review",
        "pipeline/2_designator/run.py",
        operation="recover",
        act=request["subject_id"],
        recovery_request=request["artifact_id"],
    )
    assert result.returncode == 2
    assert "run-bound policy" in result.stderr
    assert snapshot(root) == before


def test_armarium_rechecks_a_corpus_seal_tampered_after_designator(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    identity = artifact_id(EXEMPLAR, "seal", "corpus-seal")
    path = tree.resolve(tree.artifact_path(EXEMPLAR, "seal", identity))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["page_count"] = 999
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    before = snapshot(root)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/7_armarium/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "valid self-hashed census" in result.stderr
    assert snapshot(root) == before


def test_armarium_rechecks_sealed_pixels_tampered_after_designator(tmp_path):
    """The final export has its own pixel boundary, not only a census boundary."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    page = next(
        tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
        and tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])["outcome"] == "sealed"
    )
    tree.resolve(page["payload"]["image_path"]).write_bytes(b"altered after Designator")
    before = snapshot(root)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/7_armarium/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "changed under a sealed reference" in result.stderr
    assert snapshot(root) == before


def test_armarium_refuses_an_archetypus_record_orphaned_beside_a_held_act(tmp_path):
    """The mirror of "an accepted act must have an Archetypus": a held/refused act
    must NOT have one. pipeline/6_archetypus/run.py's own guard already refuses to
    establish a held act, so a record here can only exist by writing straight to
    the tree — exactly the class of forgery this checks, the same way every other
    tamper test in this file writes an artifact no stage would ever produce and
    proves the next real stage still refuses it."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "refused-page").returncode == 3
    tree = RunTree(root, "r")

    review = next(
        record
        for record in artifacts(tree, RECENSOR, "review")
        if record["payload"]["act_key"] == "a2"
    )
    act_id = review["subject_id"]
    assert review["outcome"] == "held-for-review"

    run = tree.read_run()
    payload = {
        "act_id": act_id,
        "act_key": "a2",
        "page_id": "pg_0000000000000000",
        "text": "FORGED TEXT NO STAGE WROTE",
        "status": "established",
        "regions": [],
        "provenance": {},
        "dissent_ref": "art_0000000000000000",
        "recensor_ref": review["artifact_id"],
    }
    payload["self_hash"] = self_hash(payload)
    envelope = build_envelope(
        run_id="r",
        artifact_id=artifact_id(ARCHETYPUS, "archetypus", act_id),
        subject_id=act_id,
        stage=ARCHETYPUS,
        kind="archetypus",
        outcome="established",
        config_digest=run["config_digest"],
        adapter_revision=run["adapter_recipes"][ARCHETYPUS],
        inputs=[],
        payload=payload,
    )
    path = tree.resolve(tree.artifact_path(ARCHETYPUS, "archetypus", envelope["artifact_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(envelope))
    tree.write_manifest(ARCHETYPUS)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/7_armarium/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "refused-page",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "may not also be established" in result.stderr


def test_the_seal_carries_an_outcome_and_a_derived_continuation_for_every_act(happy_run):
    """The seal entry is the handoff contract: every entry names its Designator
    outcome, and `has_continuation` reports the regions actually cut, never the
    declaration — a claim of a continuation nothing holds is how half an act gets
    delivered as the act."""
    _, tree = happy_run
    seal = tree.read_artifact(
        DESIGNATOR, "proposal-seal", artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)
    )["payload"]
    by_key = {entry["act_key"]: entry for entry in seal["expected_acts"]}
    assert by_key["a1"]["outcome"] == "proposed"
    assert by_key["a2"]["outcome"] == "proposed"
    assert by_key["a1"]["has_continuation"] is False
    assert by_key["a2"]["has_continuation"] is True


def test_the_run_used_no_network_and_no_model(happy_run):
    """The adapters are all fakes, declared as such. A run that had reached a real
    model would carry a resolved identity that was not a `fake-*` recipe."""
    _, tree = happy_run
    run = tree.read_run()
    config = load_models_toml(ROOT / "config" / "models.toml")
    fixture = load_fixture(str(ROOT / "proof"))
    bindings = run_config_bindings(config, fixture, "happy")
    assert run["config_digest"] == bindings["config_digest"]
    assert run["witness_chairs"] == list(config.witness_chairs)
    assert run["adapter_recipes"] == dict(config.adapter_recipes)
    recipes = run["adapter_recipes"]
    assert len(recipes) == 8
    assert all(revision.startswith("fake-") for revision in recipes.values())
    # Every configured chair is a local-repository fixture: nothing here can have
    # reached Hugging Face, because no live chair names a repo at all.
    assert {
        chair.source for chair in config.chairs.values() if isinstance(chair, ChairIdentity)
    } == {"local-repository"}


def test_the_config_digest_still_binds_the_scenario_as_well_as_the_chairs(happy_run):
    """Spec 02 moved the roster into `config/models.toml`; it did not move the
    scenario out of the run's configuration digest. The two are distinct runs,
    and the digest has to say so before spec 01's third test below can refuse
    one under the other's run id *before any write*."""
    _, tree = happy_run
    config = load_models_toml(ROOT / "config" / "models.toml")
    fixture = load_fixture(str(ROOT / "proof"))

    happy = run_config_bindings(config, fixture, "happy")["config_digest"]
    review = run_config_bindings(config, fixture, "review")["config_digest"]

    assert tree.read_run()["config_digest"] == happy
    assert happy != review
    # And the fixture is in there too: same scenario, one changed act, new digest.
    altered = json.loads(json.dumps(fixture))
    altered["act"][0]["text"] = "SOMETHING ELSE ENTIRELY"
    assert run_config_bindings(config, altered, "happy")["config_digest"] != happy


def test_an_explicit_absent_witness_is_a_visible_dead_and_counts_against_floor(
    tmp_path, absent_third_chair_config
):
    """Exercise absence through real stage programs, not only the config parser."""
    models_config = absent_third_chair_config

    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy", models_config=models_config)
    assert result.returncode == 3, result.stderr
    tree = RunTree(root, "r")
    testimonia = [
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
    ]
    absent_records = [
        record for record in testimonia if record["payload"]["chair"] == "attestator_3"
    ]
    assert len(absent_records) == 2
    assert all(record["outcome"] == "dead" for record in absent_records)
    for record in absent_records:
        provenance = record["payload"]["provenance"]
        assert provenance["chair_state"] == "absent"
        assert provenance["receipt_ref"] is None
        assert record["inputs"] == []
        assert record["payload"]["regions"] == []
        assert provenance["absence"] == {
            "role": "attestator_3",
            "state": "absent",
            "reason": "fixture test removes this witness without replacing it",
        }
    export = export_of(tree)
    assert export["aggregate"]["status"] == "partial"
    assert all(item["under_witnessed"] is True for item in export["non_delivered"])
    assert all(
        item["witness_coverage"]["by_outcome"] == {"read": 2, "dead": 1}
        for item in export["non_delivered"]
    )
    assert all(
        item["witness_coverage"]["by_class"] == {"completed": 2, "unresolved": 0, "failed": 1}
        for item in export["non_delivered"]
    )
    assert tree.read_run()["witness_chairs"] == ["attestator_1", "attestator_2", "attestator_3"]


def test_an_absent_witness_on_a_held_act_is_also_dead_not_not_run(
    tmp_path, absent_third_chair_config
):
    """A dead witness is dead independent of the act's own state.

    "Held" here is the Designator's own outcome — `refused-page` holds a2 because
    its continuation page never sealed — not the Recensor's later
    `held-for-review` category. Before spec 07's repair every chair on a held act
    was recorded `not-run` whether it was configured or explicitly absent, which
    is the collapse this guards against: holding the act does not turn an
    unreachable witness into a merely unasked one, and a live chair on the same
    act must stay `not-run` rather than being swept into the same word.
    """
    models_config = absent_third_chair_config
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "refused-page", models_config=models_config)
    assert result.returncode == 3, result.stderr
    tree = RunTree(root, "r")
    testimonia = [
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
    ]
    held = [record for record in testimonia if record["payload"]["act_key"] == "a2"]
    by_chair = {record["payload"]["chair"]: record for record in held}
    assert set(by_chair) == {"attestator_1", "attestator_2", "attestator_3"}
    assert by_chair["attestator_3"]["outcome"] == "dead"
    assert "chair is explicitly absent" in by_chair["attestator_3"]["payload"]["reason"]
    assert by_chair["attestator_1"]["outcome"] == "not-run"
    assert by_chair["attestator_2"]["outcome"] == "not-run"


def test_a_structured_testimonium_is_retained_here_and_refused_by_name_downstream(tmp_path):
    """The known edge of the Testimonium split, pinned rather than left to be found.

    Spec 07 requires `payload` to be the witness's native output, verbatim, never
    coerced into a shared body schema — so a witness whose real output is an object
    lands here as an object. The Perlector's current dissent comparison still reads
    a textual `reported` field, and this stage deliberately projects that only for a
    *textual* native payload: picking a field out of a structured one to stand in
    for the whole would be the coercion the spec refuses, one step further on.

    So the pipeline cannot yet carry a structured witness end to end, and this test
    exists to say exactly how it fails: a named `SchemaRefusal` at the Perlector
    boundary naming the chair, with the Testimonium retained intact behind it —
    never a reading assembled from part of a payload, and never a silent skip.
    Removing this test is the Perlector owner's to do, once its reader consumes
    `payload` natively; until then it is the honest record of a gap.
    """
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "structured-witness")

    assert result.returncode == 2, result.stderr
    assert "carries no text to compare" in result.stderr
    assert "attestator_1" in result.stderr

    tree = RunTree(root, "r")
    structured = next(
        record
        for record in artifacts(tree, ATTESTATORES, "testimonium")
        if record["payload"]["chair"] == "attestator_1" and record["payload"]["act_key"] == "a1"
    )
    assert structured["outcome"] == "read"
    assert structured["payload"]["payload"] == {
        "tokens": ["μ", "beta"],
        "layout": {"line": 4},
        "uncertain": True,
    }
    assert "reported" not in structured["payload"], (
        "no field of a structured payload may be promoted to stand in for the whole"
    )


def test_an_unknown_attestatores_tally_holds_an_orchestrated_rerun(tmp_path):
    """A damaged independent count cannot hide behind an old complete export."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    tally_path = tree.resolve(tree.manifest_path(ATTESTATORES))
    tally_path.write_bytes(b"{")
    before = snapshot(root)

    result = orchestrate(root, "r", "happy")

    assert result.returncode == 3
    assert "UNKNOWN" in result.stderr
    assert "run r: held; its reason is on stderr above" in result.stdout
    assert snapshot(root) == before


def test_perlector_refuses_a_tampered_testimonium_model_provenance(tmp_path):
    """#42 at the handoff: a sealed-looking witness cannot change its model pin."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    entry = next(
        entry
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium"
    )
    path = tree.resolve(entry["relative_path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["provenance"]["resolved_revision"] = {
        "kind": "digest-manifest",
        "value": "0" * 64,
    }
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/4_perlector/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "SchemaRefusal" in result.stderr
    assert "resolved revision" in result.stderr


def test_a_perlectio_retains_digest_checked_testimonia_it_used(tmp_path):
    """Changing a witness record after reading must stop the next real consumer."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        result = invoke_stage(root, "r", "happy", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    tree = RunTree(root, "r")
    testimony = next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium" and entry["outcome"] == "read"
    )
    path = tree.resolve(tree.artifact_path(ATTESTATORES, "testimonium", testimony["artifact_id"]))
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["payload"]["payload"] = "changed after Perlectio"
    changed["self_hash"] = self_hash(changed)
    path.write_bytes(canonical_bytes(changed))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 2
    assert "changed under a sealed reference" in result.stderr
    assert snapshot(root) == before


def test_recensor_refuses_a_completed_perlectio_without_an_object_region_basis(tmp_path):
    """A resealed malformed payload is an accounting refusal, never a traceback."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        result = invoke_stage(root, "r", "happy", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    tree = RunTree(root, "r")
    entry = next(
        entry
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio" and entry["outcome"] == "read"
    )
    path = tree.resolve(entry["relative_path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["basis"] = []
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/5_recensor/run.py")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "no object basis" in result.stderr
    assert snapshot(root) == before


def test_archetypus_refuses_a_resealed_completed_perlectio_without_an_object_basis(tmp_path):
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    review_entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    review_path = tree.resolve(review_entry["relative_path"])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    old_ref = review["payload"]["perlectio_ref"]
    reading_path = tree.resolve(old_ref["relative_path"])
    reading = json.loads(reading_path.read_text(encoding="utf-8"))
    reading["payload"]["basis"] = []
    reading["self_hash"] = self_hash(reading)
    reading_path.write_bytes(canonical_bytes(reading))
    new_ref = {
        "relative_path": old_ref["relative_path"],
        "sha256": digest_bytes(reading_path.read_bytes()),
    }
    review["inputs"] = [
        new_ref if reference == old_ref else reference for reference in review["inputs"]
    ]
    review["payload"]["perlectio_ref"] = new_ref
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))

    result = invoke_stage(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "no object basis" in result.stderr
    # The tampered act has no record. Deliberately NOT `snapshot == before`:
    # the stage publishes act by act, so whether the *other* act's record was
    # sealed before this refusal depends only on loop order, and asserting
    # nothing was written would pin an ordering coincidence as a contract.
    assert not tree.has_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", review["subject_id"])
    )


def test_archetypus_refuses_a_newer_unreviewed_perlectio(tmp_path):
    """A newer reading must be reviewed, never silently ignored or substituted."""
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    # Choose a1 explicitly: it has an accepted review in the happy trace.
    review = next(
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    act_id = review["subject_id"]
    original = tree.read_artifact(
        PERLECTOR,
        "perlectio",
        review["payload"]["perlectio_ref"]["relative_path"].split("/")[-1].removesuffix(".json"),
    )
    forged = json.loads(json.dumps(original))
    forged_attempt = attempt_id(act_id, "perlegere", 2)
    forged["attempt_id"] = forged_attempt
    forged["artifact_id"] = artifact_id(PERLECTOR, "perlectio", act_id, forged_attempt)
    forged["payload"]["attempt_ordinal"] = 2
    forged["payload"]["text"] = "UNREVIEWED REPLACEMENT"
    forged["self_hash"] = self_hash(forged)
    forged_path = tree.resolve(tree.artifact_path(PERLECTOR, "perlectio", forged["artifact_id"]))
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_bytes(canonical_bytes(forged))

    result = invoke_stage(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 2
    assert "newer Perlectio" in result.stderr


def test_archetypus_refuses_to_call_an_accepted_empty_reading_blank_without_proof(tmp_path):
    """An accepted reading is not itself evidence that the page was blank.

    Tyrel ruled blank pages ordinary, and also distinguished them from unread ink.
    The outcome algebra therefore leaves silence unresolved until the Recensor
    retains a blank proof.  Acceptance alone must not manufacture that proof.
    """
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    review_entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    review_path = tree.resolve(review_entry["relative_path"])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    old_ref = review["payload"]["perlectio_ref"]
    reading_path = tree.resolve(old_ref["relative_path"])
    reading = json.loads(reading_path.read_text(encoding="utf-8"))
    reading["payload"]["text"] = ""
    reading["self_hash"] = self_hash(reading)
    reading_path.write_bytes(canonical_bytes(reading))
    new_ref = {
        "relative_path": old_ref["relative_path"],
        "sha256": digest_bytes(reading_path.read_bytes()),
    }
    review["inputs"] = [
        new_ref if reference == old_ref else reference for reference in review["inputs"]
    ]
    review["payload"]["perlectio_ref"] = new_ref
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))
    act_id = review["subject_id"]

    result = invoke_stage(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 2
    assert "requires its evidence reference" in result.stderr
    assert not tree.has_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", act_id)
    )


def _forge_blank_proof(tree: RunTree, act_id: str) -> dict[str, str]:
    """A standalone artifact standing in for a real Recensor blank proof.

    Deliberately a *distinct* artifact rather than a reference already in the
    review's inputs: reusing `perlectio_ref` or a region blob would coincidentally
    satisfy the Armarium's input reconciliation even if this stage's own inputs
    were wrong, which is the gap the caller below exists to close.
    """
    run = tree.read_run()
    payload = {"note": "a hypothetical blank-proof artifact"}
    payload["self_hash"] = self_hash(payload)
    envelope = build_envelope(
        run_id=tree.run_id,
        artifact_id=artifact_id(RECENSOR, "blank-proof", act_id),
        subject_id=act_id,
        stage=RECENSOR,
        kind="blank-proof",
        outcome="accepted",
        config_digest=run["config_digest"],
        adapter_revision=run["adapter_recipes"][RECENSOR],
        inputs=[],
        payload=payload,
    )
    relative = tree.artifact_path(RECENSOR, "blank-proof", envelope["artifact_id"])
    path = tree.resolve(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(envelope))
    return {"relative_path": relative, "sha256": digest_bytes(path.read_bytes())}


def test_archetypus_establishes_no_readable_text_once_the_review_retains_real_blank_proof(
    tmp_path,
):
    """The success path `_no_readable_text_evidence` exists for, exercised for real.

    No producer in this build writes `no_readable_text_evidence_ref` today
    (HANDOFF.md's named cross-stage gap), so this forges a blank proof onto an
    accepted review's own inputs the same way the sibling refusal test above
    forges an empty reading -- standing in for whatever real blank-proof
    artifact a future Recensor contract produces.

    The point is twofold: prove the constructor's success path actually writes
    the record spec 10 describes, not only that its refusal paths fire; and
    prove the record it writes remains exportable through the (frozen,
    off-limits this round) Armarium -- an Archetypus record that cannot survive
    its own consumer is not established, whatever its own schema says.
    """
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    review_entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    review_path = tree.resolve(review_entry["relative_path"])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    old_ref = review["payload"]["perlectio_ref"]
    reading_path = tree.resolve(old_ref["relative_path"])
    reading = json.loads(reading_path.read_text(encoding="utf-8"))
    reading["payload"]["text"] = ""
    reading["self_hash"] = self_hash(reading)
    reading_path.write_bytes(canonical_bytes(reading))
    new_ref = {
        "relative_path": old_ref["relative_path"],
        "sha256": digest_bytes(reading_path.read_bytes()),
    }

    act_id = review["subject_id"]
    evidence_ref = _forge_blank_proof(tree, act_id)

    review["inputs"] = [
        new_ref if reference == old_ref else reference for reference in review["inputs"]
    ] + [evidence_ref]
    review["payload"]["perlectio_ref"] = new_ref
    review["payload"]["no_readable_text_evidence_ref"] = evidence_ref
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))

    result = invoke_stage(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 0, result.stderr
    record = tree.read_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", act_id)
    )["payload"]
    assert record["text"] == ""
    assert record["text_status"] == "no_readable_text"
    assert record["status"] == "established"
    assert record["evidence_ref"] == evidence_ref

    export_result = invoke_stage(root, "r", "happy", "pipeline/7_armarium/run.py")
    assert export_result.returncode == 0, export_result.stderr
    # Surviving the consumer means being *in* its export, not merely not
    # crashing it: a delivered set that silently dropped the blank act would
    # exit 0 too. The export row carries the record's established empty text;
    # the evidence reference lives on the record itself, asserted above.
    export = export_of(tree)
    blank = next(item for item in export["delivered"] if item["act_id"] == act_id)
    assert blank["text"] == ""


def test_archetypus_refuses_a_blank_proof_that_is_the_reading_itself(tmp_path):
    """A reading is never evidence of its own silence.

    Unlike `perlectio_ref` and `recensor_ref`, `evidence_ref` is never read,
    stage-checked or kind-checked -- no `blank-proof` artifact kind exists yet
    to check it against (HANDOFF.md's named gap). Without this refusal, naming
    the accepted (now-emptied) Perlectio itself as `no_readable_text_evidence_ref`
    passes: the reading whose silence is in question stands in as proof of it,
    defeating HANDOFF.md's whole argument for the field ("An accepted review is
    evidence that the Recensor accepted a reading; it is not evidence that the
    page was blank"). Reproduces audit-d finding F4's measurement.
    """
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    review_entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    review_path = tree.resolve(review_entry["relative_path"])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    old_ref = review["payload"]["perlectio_ref"]
    reading_path = tree.resolve(old_ref["relative_path"])
    reading = json.loads(reading_path.read_text(encoding="utf-8"))
    reading["payload"]["text"] = ""
    reading["self_hash"] = self_hash(reading)
    reading_path.write_bytes(canonical_bytes(reading))
    new_ref = {
        "relative_path": old_ref["relative_path"],
        "sha256": digest_bytes(reading_path.read_bytes()),
    }

    review["inputs"] = [
        new_ref if reference == old_ref else reference for reference in review["inputs"]
    ]
    review["payload"]["perlectio_ref"] = new_ref
    review["payload"]["no_readable_text_evidence_ref"] = new_ref
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))

    result = invoke_stage(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "never evidence of its own silence" in result.stderr
    # A refused act leaves no record behind for a downstream stage to treat as
    # output; the refusal and a published Archetypus cannot coexist.
    assert not tree.has_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", review["subject_id"])
    )


def test_archetypus_refuses_a_blank_proof_that_is_the_readings_own_crop(tmp_path):
    """The same circularity as the reading-itself case, one step further out.

    An accepted review's inputs are the reading plus every crop that reading
    read, so a reference to the very image the reading failed to read passes
    the direct-input check. Without this refusal it would seal as proof the
    page was blank — the ink whose reading is in question standing as evidence
    of its own silence.
    """
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    review_entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    review_path = tree.resolve(review_entry["relative_path"])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    old_ref = review["payload"]["perlectio_ref"]
    reading_path = tree.resolve(old_ref["relative_path"])
    reading = json.loads(reading_path.read_text(encoding="utf-8"))
    # A crop the reading read: an input of the reading that the review also
    # lists directly (the review's inputs are the reading plus its crops).
    crop_ref = next(
        (reference for reference in reading["inputs"] if reference in review["inputs"]),
        None,
    )
    assert crop_ref is not None, (
        "this test needs a crop the reading read that the review also lists directly; "
        "the review's inputs are the reading plus its crops"
    )
    reading["payload"]["text"] = ""
    reading["self_hash"] = self_hash(reading)
    reading_path.write_bytes(canonical_bytes(reading))
    new_ref = {
        "relative_path": old_ref["relative_path"],
        "sha256": digest_bytes(reading_path.read_bytes()),
    }

    review["inputs"] = [
        new_ref if reference == old_ref else reference for reference in review["inputs"]
    ]
    review["payload"]["perlectio_ref"] = new_ref
    review["payload"]["no_readable_text_evidence_ref"] = crop_ref
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))

    result = invoke_stage(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "input of the accepted Perlectio itself" in result.stderr
    assert not tree.has_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", review["subject_id"])
    )


def test_archetypus_refuses_a_blank_proof_over_a_reading_that_has_text(tmp_path):
    """Two upstream claims that contradict each other are never quietly one claim.

    A review carrying a blank proof says this act held no readable ink; the
    reading it accepted says otherwise, in characters. Consulting the evidence
    reference only where the stage's own derivation has already reached
    `no_readable_text` reads past the Recensor's finding everywhere else, so the
    contradiction resolves in favour of whichever claim the derivation reaches
    first and leaves no trace of the other (GOVERNANCE 2).
    """
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    review_entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    review_path = tree.resolve(review_entry["relative_path"])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    act_id = review["subject_id"]
    reading = json.loads(
        tree.resolve(review["payload"]["perlectio_ref"]["relative_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert reading["payload"]["text"].strip(), "this act must carry real text for the conflict"

    evidence_ref = _forge_blank_proof(tree, act_id)
    review["inputs"] = review["inputs"] + [evidence_ref]
    review["payload"]["no_readable_text_evidence_ref"] = evidence_ref
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))

    result = invoke_stage(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "reconciliation failure" in result.stderr
    assert not tree.has_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", act_id)
    )


def test_archetypus_refuses_a_crop_the_run_tree_cannot_read(tmp_path):
    """A named crop that is not there is an accounting failure, not a traceback.

    The reference is built by hashing the bytes on disk, so a reading naming a
    crop this tree does not hold arrives as `OSError` — outside the family
    `run_stage` classifies. Unnamed, it takes every other act's record with it
    under exit 1, which the orchestrator does not recognise as a stage outcome.
    """
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    review_entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    review_path = tree.resolve(review_entry["relative_path"])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    old_ref = review["payload"]["perlectio_ref"]
    reading_path = tree.resolve(old_ref["relative_path"])
    reading = json.loads(reading_path.read_text(encoding="utf-8"))
    # The envelope's own `inputs` still name the real crops, so the reading
    # verifies; only the basis this stage reads its regions from is repointed.
    for region in reading["payload"]["basis"]["regions"]:
        region["image_path"] = tree.blob_path(DESIGNATOR, "0" * 64)
    reading["self_hash"] = self_hash(reading)
    reading_path.write_bytes(canonical_bytes(reading))
    new_ref = {
        "relative_path": old_ref["relative_path"],
        "sha256": digest_bytes(reading_path.read_bytes()),
    }
    review["inputs"] = [
        new_ref if reference == old_ref else reference for reference in review["inputs"]
    ]
    review["payload"]["perlectio_ref"] = new_ref
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))

    result = invoke_stage(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "which this run tree cannot read" in result.stderr
    # The classified refusal must not leave a record behind either.
    assert not tree.has_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", review["subject_id"])
    )


def test_armarium_refuses_a_newer_perlectio_than_the_established_one(tmp_path):
    """A completed Archetypus cannot hide a reading appended after its review."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    original = next(
        tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio"
    )
    act_id = original["subject_id"]
    forged = json.loads(json.dumps(original))
    forged_attempt = attempt_id(act_id, "perlegere", 2)
    forged["attempt_id"] = forged_attempt
    forged["artifact_id"] = artifact_id(PERLECTOR, "perlectio", act_id, forged_attempt)
    forged["payload"]["attempt_ordinal"] = 2
    forged["payload"]["text"] = "UNREVIEWED EXPORT REPLACEMENT"
    forged["self_hash"] = self_hash(forged)
    forged_path = tree.resolve(tree.artifact_path(PERLECTOR, "perlectio", forged["artifact_id"]))
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_bytes(canonical_bytes(forged))

    result = invoke_stage(root, "r", "happy", "pipeline/7_armarium/run.py")
    assert result.returncode == 2
    assert "newer Perlectio" in result.stderr


def test_armarium_refuses_a_resealed_archetypus_text_that_disagrees_with_its_parent(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    entry = next(
        entry
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    )
    path = tree.resolve(entry["relative_path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["text"] = "ALTERED ESTABLISHED TEXT"
    record["payload"]["self_hash"] = self_hash(record["payload"])
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/7_armarium/run.py")
    assert result.returncode == 2
    assert "does not exactly preserve the Perlectio" in result.stderr
    assert snapshot(root) == before


def test_armarium_refuses_two_established_records_instead_of_selecting_one(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    original = next(
        tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    )
    act_id = original["subject_id"]
    forged = json.loads(json.dumps(original))
    forged_attempt = attempt_id(act_id, "establish", 2)
    forged["attempt_id"] = forged_attempt
    forged["artifact_id"] = artifact_id(ARCHETYPUS, "archetypus", act_id, forged_attempt)
    forged["self_hash"] = self_hash(forged)
    path = tree.resolve(tree.artifact_path(ARCHETYPUS, "archetypus", forged["artifact_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(forged))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/7_armarium/run.py")
    assert result.returncode == 2
    assert "carries 2 Archetypus records" in result.stderr
    assert snapshot(root) == before


# --- 2. Repeating the identical command changes nothing ------------------------


def test_repeating_the_identical_command_leaves_every_byte_unchanged(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    before = snapshot(root)

    # R0 adds two retained page Testimonia and two derived act attachments to
    # the happy walking skeleton; repeatability still compares every byte.
    assert len(before) == 58
    assert semantic_snapshot_digest(root) == HAPPY_RUN_TREE_DIGEST
    assert orchestrate(root, "r", "happy").returncode == 0
    after = snapshot(root)

    assert after == before
    assert semantic_snapshot_digest(root) == HAPPY_RUN_TREE_DIGEST


def test_repeating_the_identical_command_with_nuda_enabled_leaves_every_byte_unchanged(tmp_path):
    """The byte-identical-rerun property above is only ever exercised at the
    default --nuda-per-mille 0. Lectio nuda's sampling rule is a deterministic
    hash threshold over (run_id, act_id), never `random` -- this is the test
    that actually drives two runs of the identical command with nuda turned on
    and proves the property still holds rather than trusting the docstring's
    word for it (audit finding: coverage gap, `nuda.py`)."""
    root = tmp_path / "runs"
    assert (
        orchestrate(
            root, "r", "happy", nuda_per_mille=1000, nuda_approval_ref="test/nuda"
        ).returncode
        == 0
    )
    before = snapshot(root)
    assert any("lectio-nuda" in path for path in before), "the run must actually have sampled nuda"

    assert (
        orchestrate(
            root, "r", "happy", nuda_per_mille=1000, nuda_approval_ref="test/nuda"
        ).returncode
        == 0
    )
    after = snapshot(root)

    assert after == before


def test_repeating_the_review_scenario_also_changes_nothing(tmp_path):
    """The scenario with a recovery loop in it is the one that can most easily
    append on every run — which it did, until the reading attempt stopped being a
    count of invocations."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "review").returncode == 3
    before = snapshot(root)

    # R0 adds the same four retained page/attachment artifacts before review's
    # recovery loop; its append-only invariant is unchanged.
    assert len(before) == 62
    assert semantic_snapshot_digest(root) == REVIEW_RUN_TREE_DIGEST
    assert orchestrate(root, "r", "review").returncode == 3
    assert snapshot(root) == before
    assert semantic_snapshot_digest(root) == REVIEW_RUN_TREE_DIGEST


# --- 3. An incompatible run id fails before writing ----------------------------


def test_reusing_a_run_id_with_a_changed_configuration_fails_before_writing(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    before = snapshot(root)

    # The scenario is part of the run's configuration digest, so the same run id
    # under a different scenario is a different run wearing an old name. Both
    # scenarios declare the *same* two source pages, so the source manifest
    # cannot be what catches this — only the config digest can, and this is the
    # assertion that says so.
    result = orchestrate(root, "r", "review")

    assert result.returncode != 0
    assert "IncompatibleReuse" in result.stderr
    assert "config_digest" in result.stderr, (
        "the refusal must name the changed binding; catching this later, on "
        "artifact immutability, is a refusal several stages after the first write"
    )
    assert snapshot(root) == before, "a refused reuse must leave the tree untouched"
    # And refused by the door — the stage that binds the run id — rather than by
    # some later stage discovering it cannot overwrite an artifact.
    assert "1_exemplar/door.py" in result.stderr


# --- 4. Resume reuses valid artifacts without rewriting them -------------------


def test_an_interrupted_run_resumes_without_rewriting_what_survived(tmp_path):
    """Interrupt for real: delete everything from the Perlector onward, as though
    the process died mid-run, then run the same command again."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    complete = snapshot(root)

    for stage_directory in ("4_perlector", "5_recensor", "6_archetypus", "7_armarium"):
        shutil.rmtree(root / "r" / stage_directory)
    survivors = snapshot(root)
    assert len(survivors) < len(complete)

    assert orchestrate(root, "r", "happy").returncode == 0
    resumed = snapshot(root)

    # Everything that survived is byte-identical: resume reused it rather than
    # redoing it. And the finished tree is identical to the uninterrupted one.
    for path, digest in survivors.items():
        assert resumed[path] == digest, f"{path} was rewritten on resume"
    assert resumed == complete


# --- 5. The review scenario preserves the whole history ------------------------


def artifacts(tree: RunTree, stage: str, kind: str) -> list[dict]:
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind
    ]


def test_the_recovered_act_keeps_one_identity_across_two_regions(review_run):
    """ARCHITECTURE invariant 1, driven end to end rather than unit-tested: act
    identity survives recropping, and the region identity does not."""
    _, tree = review_run
    regions = [
        record
        for record in artifacts(tree, DESIGNATOR, "region")
        if record["payload"]["act_key"] == "a1"
    ]
    assert len(regions) == 2
    assert len({record["subject_id"] for record in regions}) == 1
    assert len({record["payload"]["region_id"] for record in regions}) == 2
    assert {record["payload"]["origin"] for record in regions} == {"proposal", "recovery"}


def test_the_recovery_request_and_both_reading_attempts_survive(review_run):
    _, tree = review_run
    requests = artifacts(tree, RECENSOR, "recovery-request")
    assert len(requests) == 1
    assert requests[0]["payload"]["act_key"] == "a1"

    readings = [
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a1"
    ]
    assert sorted(record["payload"]["attempt_ordinal"] for record in readings) == [1, 2]


def test_both_recensor_outcomes_for_the_recovered_act_survive(review_run):
    """Nothing is lost inside a recovery loop: the request and the acceptance are
    both still there, in order."""
    _, tree = review_run
    reviews = [
        record
        for record in artifacts(tree, RECENSOR, "review")
        if record["payload"]["act_key"] == "a1"
    ]
    assert len(reviews) == 2
    by_ordinal = {record["payload"]["attempt_ordinal"]: record["outcome"] for record in reviews}
    assert by_ordinal == {1: "recovery-requested", 2: "accepted"}


def test_recovery_ink_is_recorded_as_witness_uncovered(review_run):
    """The recrop uncovered ink no witness ever saw. Saying so is the difference
    between a gap in the record and a gap nobody can see."""
    _, tree = review_run
    latest = max(
        (
            record
            for record in artifacts(tree, PERLECTOR, "perlectio")
            if record["payload"]["act_key"] == "a1"
        ),
        key=lambda record: record["payload"]["attempt_ordinal"],
    )
    coverage = [basis["witness_covered"] for basis in latest["payload"]["basis"]["regions"]]
    assert coverage == [True, False]

    # D-15: spec 08 test 1 asks for "a recovery dossier marks witness-uncovered
    # ink" -- the dossier is what a reader is actually shown, not `basis`, and
    # nothing asserted this surface before. Both derive from the same
    # `witnessed_region_ids` set, so they must agree per region.
    dossier_coverage = {
        region["region_id"]: region["witness_covered"]
        for region in latest["payload"]["dossier"]["regions"]
    }
    basis_coverage = {
        region["region_id"]: region["witness_covered"]
        for region in latest["payload"]["basis"]["regions"]
    }
    assert dossier_coverage == basis_coverage
    assert sorted(dossier_coverage.values()) == [False, True]


def test_the_cross_page_act_is_witnessed_on_both_sides_of_the_break(review_run):
    """A continuation is part of the original proposal, not a later attempt. A
    witness shown only the near side would have read half an act while the record
    said it read the act."""
    _, tree = review_run
    reading = next(
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a2"
    )
    regions = reading["payload"]["basis"]["regions"]
    assert len(regions) == 2
    assert all(basis["witness_covered"] for basis in regions)


def test_recovery_stayed_inside_its_budget(review_run):
    _, tree = review_run
    requests = artifacts(tree, RECENSOR, "recovery-request")
    assert len(requests) == 1
    assert requests[0]["payload"]["budget_allowed"] <= 3, "the absolute cap is a ruling"


# --- 6. The held act cannot look complete --------------------------------------


def test_the_held_act_has_no_archetypus_at_all(review_run):
    """The absence is the evidence. An export that showed a held act as delivered
    would have to invent a record that does not exist."""
    _, tree = review_run
    established = artifacts(tree, ARCHETYPUS, "archetypus")
    assert len(established) == 1
    assert established[0]["payload"]["act_key"] == "a1"


def test_the_held_act_appears_in_the_review_output_and_forces_partial(review_run):
    _, tree = review_run
    export = export_of(tree)
    assert export["aggregate"]["status"] == "partial"
    assert len(export["non_delivered"]) == 1
    assert export["non_delivered"][0]["act_key"] == "a2"
    assert export["non_delivered"][0]["category"] == "held-for-review"
    assert len(export["delivered"]) == 1
    assert "act a2 is held-for-review" in export["aggregate"]["reasons"]


def test_no_delivered_entry_carries_a_witness_reading_as_its_text(review_run):
    """GOALS 3: a witness reading is never itself an output. The established text
    must not equal any witness's reported words *by accident of the fixture*
    either, so the fixture deliberately makes two chairs disagree."""
    _, tree = review_run
    export = export_of(tree)
    testimony = {
        record["payload"]["payload"]
        for record in artifacts(tree, ATTESTATORES, "testimonium")
        if record["outcome"] == "read" and record["payload"]["act_key"] == "a1"
    }
    delivered = export["delivered"][0]["text"]
    disagreeing = [reported for reported in testimony if reported != delivered]
    assert len(disagreeing) == 2, "the fixture must keep dissent exercisable"


def test_the_failed_chair_is_visible_in_the_export(review_run):
    """`failed` is a real member of the closed witness vocabulary, driven end to
    end: it reaches the export as a named shortfall rather than as a silence."""
    _, tree = review_run
    export = export_of(tree)
    held = export["non_delivered"][0]
    assert held["under_witnessed"] is True
    assert held["witness_coverage"]["by_outcome"]["failed"] == 1
    assert held["witness_coverage"]["by_class"] == {"completed": 2, "unresolved": 0, "failed": 1}
    assert any("under-witnessed" in reason for reason in export["aggregate"]["reasons"])


def test_the_capability_scenario_leaves_one_chair_uncompared_while_happy_compares_all(
    tmp_path, happy_run
):
    """Capability handling stays live without blinding the reference instrument.

    `pipeline/4_perlector/dissent.py::is_comparable` refuses to diff a witness
    whose format can express uncertainty, because such a format may embed
    alternative-reading markup inline and diffing the markup would count as
    disagreement. It cannot touch the reading — dissent is read-only and computed
    after the fact — so it is not a picker. What it is, is a hole in the
    instrument ARCHITECTURE names for catching a reader that "learned to agree
    with witnesses rather than to read ink."

    Spec 07's fixture declares that capability on chair 2 of act a1 in the
    dedicated `witness-capabilities` scenario. R0 additionally leaves both
    page-witness chairs unknown until R4 provides act-anchored comparison views.
    The reference happy run therefore has those two honest unknown rows while
    its act-scoped chair remains comparable.
    """
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "witness-capabilities")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    reading = next(
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a1"
    )
    by_chair = {row["chair"]: row for row in reading["payload"]["dissent"]}
    assert set(by_chair) == {"attestator_1", "attestator_2", "attestator_3"}
    assert by_chair["attestator_2"]["compared"] == "unknown"
    assert "cannot be reduced to a plain comparison view" in by_chair["attestator_2"]["reason"]
    assert [row["compared"] for row in reading["payload"]["dissent"]].count("unknown") == 3

    testimonium = next(
        record
        for record in artifacts(tree, ATTESTATORES, "testimonium")
        if record["payload"]["act_key"] == "a1" and record["payload"]["chair"] == "attestator_2"
    )
    assert testimonium["payload"]["format_capabilities"]["can_express_uncertainty"] is True
    # The capability blinds the comparison and nothing else: the outcome, the
    # class, and the coverage count are what they would be without it.
    assert testimonium["outcome"] == "read"
    entry = next(row for row in export_of(tree)["delivered"] if row["act_key"] == "a1")
    assert entry["witness_coverage"]["by_class"] == {
        "completed": 3,
        "unresolved": 0,
        "failed": 0,
    }

    _, happy_tree = happy_run
    happy_reading = next(
        record
        for record in artifacts(happy_tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a1"
    )
    happy_dissent = happy_reading["payload"]["dissent"]
    assert {row["chair"] for row in happy_dissent} == {
        "attestator_1",
        "attestator_2",
        "attestator_3",
    }
    assert {row["chair"] for row in happy_dissent if row["compared"] == "unknown"} == {
        "attestator_1",
        "attestator_3",
    }


def test_a_delivered_act_still_links_back_to_the_exact_ink(review_run):
    _, tree = review_run
    delivered = export_of(tree)["delivered"][0]
    source_by_ordinal = {row["ordinal"]: row for row in tree.read_run()["source_manifest"]}
    assert len(delivered["source_regions"]) == 2
    for region in delivered["source_regions"]:
        assert region["image_sha256"]
        assert region["region_id"].startswith("rgn_")
        assert tree.read_bytes(region["image_path"])
        source = source_by_ordinal[region["source_page_ordinal"]]
        assert region["declared_path"] == source["relative_path"]
        assert region["declared_sha256"] == source["sha256"]


# --- 7. Every one of the seven handoffs refuses corruption ---------------------

# (producer, consumer, the artifact kind that crosses this boundary)
HANDOFF_ARTIFACTS = (
    (DOOR, EXEMPLAR, "admission"),
    (EXEMPLAR, DESIGNATOR, "page"),
    (DESIGNATOR, ATTESTATORES, "region"),
    (ATTESTATORES, PERLECTOR, "testimonium"),
    (PERLECTOR, RECENSOR, "perlectio"),
    (RECENSOR, ARCHETYPUS, "review"),
    (ARCHETYPUS, ARMARIUM, "archetypus"),
)

CONSUMER_PROGRAMS = {
    EXEMPLAR: "pipeline/1_exemplar/run.py",
    DESIGNATOR: "pipeline/2_designator/run.py",
    ATTESTATORES: "pipeline/3_attestatores/run.py",
    PERLECTOR: "pipeline/4_perlector/run.py",
    RECENSOR: "pipeline/5_recensor/run.py",
    ARCHETYPUS: "pipeline/6_archetypus/run.py",
    ARMARIUM: "pipeline/7_armarium/run.py",
}


def one_artifact(tree: RunTree, stage: str, kind: str) -> tuple[Path, dict]:
    entries = [entry for entry in tree.build_manifest(stage)["artifacts"] if entry["kind"] == kind]
    assert entries, f"{stage} produced no {kind} to corrupt"
    entry = entries[0]
    return tree.resolve(entry["relative_path"]), tree.read_artifact(
        stage, kind, entry["artifact_id"]
    )


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_validator_refuses_a_corrupted_schema(happy_run, producer, consumer, kind):
    _, tree = happy_run
    _, record = one_artifact(tree, producer, kind)
    record["schema"] = "skeleton.v99"
    with pytest.raises(SchemaRefusal):
        validate_envelope(record)


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_validator_refuses_a_malformed_identity(happy_run, producer, consumer, kind):
    _, tree = happy_run
    _, record = one_artifact(tree, producer, kind)
    record["artifact_id"] = "art_not_a_real_identity"
    with pytest.raises(SchemaRefusal):
        validate_envelope(record)


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_validator_refuses_duplicate_accounting(happy_run, producer, consumer, kind):
    """A duplicate reference is how one page gets counted twice and a conservation
    check passes over something nobody read."""
    _, tree = happy_run
    _, record = one_artifact(tree, producer, kind)
    assert record["inputs"], (
        f"{producer} {kind} references no input, so this boundary carries nothing "
        "verifiable. Skipping here would be a skip-list, which is how a gap goes "
        "unnoticed (#87) — the producer should name the bytes it acted on"
    )
    record["inputs"] = record["inputs"] + [dict(record["inputs"][0])]
    with pytest.raises(SchemaRefusal):
        validate_envelope(record)


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_validator_refuses_bytes_that_changed_under_a_sealed_reference(
    happy_run, producer, consumer, kind
):
    _, tree = happy_run
    _, record = one_artifact(tree, producer, kind)
    assert record["inputs"], f"{producer} {kind} names no bytes to tamper with"
    reference = record["inputs"][0]
    with pytest.raises(SchemaRefusal):
        verify_input_bytes(reference, b"tampered")


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer,kind", HANDOFF_ARTIFACTS)
def test_each_handoff_corruption_stops_its_named_real_consumer(
    happy_run, tmp_path, producer, consumer, kind
):
    """The validator matrix above is not evidence that a consumer calls it.

    Give every contract edge a fresh complete tree, damage its producer record on
    disk, and invoke the particular downstream program named by the handoff.  A
    generic test that merely calls ``validate_envelope`` can stay green while a
    stage bypasses the boundary entirely; this one cannot.
    """
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    path, record = one_artifact(tree, producer, kind)
    record["schema"] = "skeleton.v99"
    path.write_bytes(canonical_bytes(record))
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", CONSUMER_PROGRAMS[consumer])
    assert result.returncode != 0
    assert "skeleton.v99" in result.stderr or "SchemaRefusal" in result.stderr
    assert snapshot(root) == before


def test_recovery_policy_is_a_run_bound_configuration_not_a_late_local_default(tmp_path):
    """A policy change must refuse the old run before any stage can reinterpret it."""
    root = tmp_path / "runs"
    policy = tmp_path / "recovery.toml"
    policy.write_text((ROOT / "config/recovery.toml").read_text(encoding="utf-8"), encoding="utf-8")
    assert orchestrate(root, "r", "happy", recovery_config=policy).returncode == 0
    before = snapshot(root)

    policy.write_text(
        "absolute_cap = 3\n\n[budget]\nfallback_recrop = 0\npage_level_reread = 1\n",
        encoding="utf-8",
    )
    result = orchestrate(root, "r", "happy", recovery_config=policy)
    assert result.returncode == 2
    assert "different config_digest" in result.stderr
    assert snapshot(root) == before


def test_hard_failure_policy_is_a_run_bound_configuration_not_a_late_local_default(tmp_path):
    """A revised closed list cannot reinterpret an already-sealed run's failures.

    The run-level cap decides whether a run may keep invoking stages at all, so a
    later edit to what counts as a hard failure is exactly as run-shaping as an
    edit to the recovery budget beside it — and refuses the sealed run for the
    same reason, before any stage reads a failure under a list it did not run
    under.
    """
    root = tmp_path / "runs"
    policy = tmp_path / "hard_failure.toml"
    policy.write_text(
        (ROOT / "config/hard_failure.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert orchestrate(root, "r", "happy", hard_failure_config=policy).returncode == 0
    before = snapshot(root)

    policy.write_text(
        'threshold = 2\n\n[[kind]]\nstage = "perlector"\noutcome = "failed"\n',
        encoding="utf-8",
    )
    result = orchestrate(root, "r", "happy", hard_failure_config=policy)
    assert result.returncode == 2
    assert "different config_digest" in result.stderr
    assert snapshot(root) == before


@pytest.mark.full
def test_every_handoff_in_the_contract_is_covered_by_this_table():
    """Meta-invariant #91 — a drift check over an agreement surface. If a handoff
    is added to the contracts and not to this table, the boundary test would
    silently cover six of seven."""
    from common.contracts.stages import HANDOFFS

    assert {(producer, consumer) for producer, consumer, _ in HANDOFF_ARTIFACTS} == set(HANDOFFS)
    assert {consumer for _, consumer, _ in HANDOFF_ARTIFACTS} == set(CONSUMER_PROGRAMS)
    assert len(HANDOFF_ARTIFACTS) == 7


def test_the_run_authority_is_never_rewritten_by_any_stage(happy_run):
    root, tree = happy_run
    stored = json.loads((root / "r" / "run.json").read_text(encoding="utf-8"))
    assert stored == tree.read_run()


def test_a_stage_invoked_before_its_producer_refuses_rather_than_inventing(tmp_path):
    """Order is not a convention here. A stage run out of sequence has nothing to
    read, and must say so instead of producing an empty success."""
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "2_designator" / "run.py"),
            "--run-root",
            str(tmp_path),
            "--run-id",
            "never-created",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "IncompatibleReuse" in result.stderr or "ContractError" in result.stderr


def test_contract_error_is_the_only_way_a_stage_reports_refusal():
    """A stage that crashed with a traceback and exited zero would be the vacuous
    green this project exists to notice."""
    assert issubclass(SchemaRefusal, ContractError)


# --- 8. A refused page cannot vanish, and no act rides out over one -------------
#
# The defect all four reviewers filed first: the Designator skipped any act whose
# page was not sealed, wrote nothing for it anywhere, and sealed a shorter
# expected-act list — so the one conservation check in the pipeline reconciled
# perfectly against a record of the loss's absence. A run that lost a whole page
# reported `status: complete, reasons: []`.


@pytest.fixture(scope="module")
def refused_page_run(tmp_path_factory):
    """Page 2 is refused at the door, so a2's continuation cannot be cut."""
    root = tmp_path_factory.mktemp("refused_page")
    result = orchestrate(root, "r", "refused-page")
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r")


def test_the_orchestrator_relays_the_doors_private_refusal_report(tmp_path):
    """A successful Door can still have a named refusal report for an operator."""
    result = orchestrate(tmp_path / "runs", "r", "refused-page")

    assert result.returncode == 3, result.stderr
    assert "1 door refusal(s); private refusal report:" in result.stderr


@pytest.fixture(scope="module")
def refused_first_page_run(tmp_path_factory):
    """Page 1 — the page both acts live on — is refused at the door."""
    root = tmp_path_factory.mktemp("refused_first_page")
    result = orchestrate(root, "r", "refused-first-page")
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r")


def proposal_seal(tree: RunTree) -> dict:
    return tree.read_artifact(
        DESIGNATOR, "proposal-seal", artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)
    )["payload"]


def test_the_door_really_refused_page_two_through_its_own_inspection(refused_page_run):
    _, tree = refused_page_run
    refusals = [
        record for record in artifacts(tree, EXEMPLAR, "page") if record["outcome"] == "refused"
    ]
    assert len(refusals) == 1
    assert refusals[0]["payload"]["ordinal"] == 2
    assert "digest" in refusals[0]["payload"]["reason"]


def test_the_page_loss_is_named_and_the_run_is_partial(refused_page_run):
    _, tree = refused_page_run
    export = export_of(tree)
    assert export["aggregate"]["status"] == "partial"
    assert any(
        reason.startswith("page 2 was refused:") for reason in export["aggregate"]["reasons"]
    )
    assert export["aggregate"]["by_page_outcome"] == {"sealed": 1, "refused": 1}


def test_the_act_with_the_lost_continuation_is_held_not_delivered(refused_page_run):
    """`has_continuation` is derived from the regions actually cut, so it may not
    claim a continuation nothing holds — and the act whose far side is on the
    lost page is held rather than delivered as a complete reading of half its ink."""
    _, tree = refused_page_run
    seal = proposal_seal(tree)
    by_key = {entry["act_key"]: entry for entry in seal["expected_acts"]}
    assert set(by_key) == {"a1", "a2"}, "the seal must still name every declared act"
    assert by_key["a1"]["outcome"] == "proposed"
    assert by_key["a2"]["outcome"] == "held"
    assert by_key["a2"]["has_continuation"] is False

    export = export_of(tree)
    assert [item["act_key"] for item in export["delivered"]] == ["a1"]
    assert [item["act_key"] for item in export["non_delivered"]] == ["a2"]
    assert export["non_delivered"][0]["category"] == "held-for-review"


def test_the_hold_is_a_real_artifact_naming_the_lost_page(refused_page_run):
    _, tree = refused_page_run
    holds = artifacts(tree, DESIGNATOR, "hold")
    assert len(holds) == 1
    assert holds[0]["outcome"] == "held"
    assert holds[0]["payload"]["act_key"] == "a2"
    assert "page 2" in holds[0]["payload"]["reason"]
    assert holds[0]["inputs"], "the hold must reference the refusal it rests on"


def test_no_witness_and_no_reading_pretends_to_have_seen_the_held_act(refused_page_run):
    """The held act is not silently skipped: every configured chair records an
    explicit not-run, and the Perlector acknowledges the act without reading it —
    a reading of the near side alone would be a truncation delivered as an output."""
    _, tree = refused_page_run
    testimonia = [
        record
        for record in artifacts(tree, ATTESTATORES, "testimonium")
        if record["payload"]["act_key"] == "a2"
    ]
    assert len(testimonia) == 3
    assert {record["outcome"] for record in testimonia} == {"not-run"}

    readings = [
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a2"
    ]
    assert len(readings) == 1
    assert readings[0]["outcome"] == "not-run"
    assert "text" not in readings[0]["payload"]

    established = artifacts(tree, ARCHETYPUS, "archetypus")
    assert [record["payload"]["act_key"] for record in established] == ["a1"]


def test_the_refused_page_scenario_is_deterministic_on_rerun(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "refused-page").returncode == 3
    before = snapshot(root)
    assert orchestrate(root, "r", "refused-page").returncode == 3
    assert snapshot(root) == before


def test_losing_the_first_page_holds_every_act_and_delivers_nothing(refused_first_page_run):
    """Half one of the defect, driven end to end: an act whose own page was never
    sealed used to disappear from the seal entirely. Now it appears, held, with a
    hold artifact each, and the run is partial with the page loss named.

    Page 2 (a2's continuation page) is sealed but, in this scenario, never has a
    region cut on it at all -- a2 is held entirely on page 1's loss before its
    continuation is ever attempted. Page 2's own real ink therefore reconciles
    as 100% residual, and conservation now mints that residual its own held act
    (`residual:2:0`) rather than leaving it inert inside the conservation
    artifact alone -- a third, independent account of the same underlying loss,
    which is why three holds and three review items are expected rather than two.
    """
    _, tree = refused_first_page_run
    seal = proposal_seal(tree)
    assert len(seal["expected_acts"]) == 3
    assert {entry["act_key"]: entry["outcome"] for entry in seal["expected_acts"]} == {
        "a1": "held",
        "a2": "held",
        "residual:2:0": "held",
    }
    assert artifacts(tree, DESIGNATOR, "region") == [], (
        "no region may be cut for an act that cannot be fully marked out — an "
        "orphan continuation crop would be evidence of an act nothing accounts for"
    )
    assert len(artifacts(tree, DESIGNATOR, "hold")) == 3

    export = export_of(tree)
    assert export["aggregate"]["status"] == "partial"
    assert any(
        reason.startswith("page 1 was refused:") for reason in export["aggregate"]["reasons"]
    )
    assert export["delivered"] == []
    assert [item["category"] for item in export["non_delivered"]] == [
        "held-for-review",
        "held-for-review",
        "held-for-review",
    ]
    entries = [
        entry
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "manifest-entry"
    ]
    assert len(entries) == 3, "conservation: every expected act still has exactly one category"


def test_the_recensor_refuses_a_continuation_claim_with_one_region(tmp_path):
    """Defence in depth for half two: if the seal claims a continuation and the
    tree holds only one proposal region — drift, tampering, or a future bug —
    the Recensor holds the act rather than accepting a half reading."""
    root = tmp_path / "runs"
    for name, program in (
        ("door", "pipeline/1_exemplar/door.py"),
        ("exemplar", "pipeline/1_exemplar/run.py"),
        ("designator", "pipeline/2_designator/run.py"),
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "r",
                "--scenario",
                "happy",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"

    tree = RunTree(root, "r")
    continuations = [
        entry
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region"
        and tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])["payload"]["transform"][
            "source_page_ordinal"
        ]
        == 2
    ]
    assert len(continuations) == 1
    continuation = continuations[0]
    seal_path = tree.resolve(
        tree.artifact_path(
            DESIGNATOR,
            "proposal-seal",
            artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal"),
        )
    )
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    # Model an internally resealed bad producer, not a bare deleted file: all
    # direct references are coherent, but the proposal still asserts that a2
    # continues while only its near-side region remains.  This reaches
    # Recensor's own defence rather than the earlier store-integrity guard.
    seal["inputs"] = [
        reference
        for reference in seal["inputs"]
        if reference["relative_path"] != continuation["relative_path"]
    ]
    for act in seal["payload"]["expected_acts"]:
        if act["act_id"] == continuation["subject_id"]:
            act["evidence"] = [
                reference
                for reference in act["evidence"]
                if reference["relative_path"] != continuation["relative_path"]
            ]
    seal["payload"]["self_hash"] = self_hash(seal["payload"])
    seal["self_hash"] = self_hash(seal)
    seal_path.write_bytes(canonical_bytes(seal))
    tree.resolve(continuations[0]["relative_path"]).unlink()
    tree.write_manifest(DESIGNATOR)

    for name, program in (
        ("attestatores", "pipeline/3_attestatores/run.py"),
        ("perlector", "pipeline/4_perlector/run.py"),
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "r",
                "--scenario",
                "happy",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "5_recensor" / "run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, result.stderr

    reviews = [
        record
        for record in artifacts(tree, RECENSOR, "review")
        if record["payload"]["act_key"] == "a2"
    ]
    assert len(reviews) == 1
    assert reviews[0]["outcome"] == "held-for-review"
    assert "continuation" in reviews[0]["payload"]["reason"]


# --- A reading that did not succeed ---------------------------------------------


@pytest.fixture(scope="module")
def truncated_reading_run(tmp_path_factory):
    """Act a1's reading is `truncated`: a failed-class Perlector outcome that still
    carries the text it managed. That combination is the dangerous one — the
    Recensor used to ask only whether a reading *existed*, and the Archetypus
    copied the text out of whichever reading was latest."""
    root = tmp_path_factory.mktemp("truncated_reading")
    result = orchestrate(root, "r", "truncated-reading")
    assert result.returncode == 3, result.stderr
    return root, RunTree(root, "r")


def test_a_reading_that_did_not_succeed_is_held_and_says_why(truncated_reading_run):
    """GOALS 2 is accuracy against the ink, and GOVERNANCE 2 refuses a loss hidden
    behind a successful status. Text nobody successfully read is neither, so it is
    held — visibly, with the outcome that caused it named in the reason."""
    _, tree = truncated_reading_run
    reviews = [
        record
        for record in artifacts(tree, RECENSOR, "review")
        if record["payload"]["act_key"] == "a1"
    ]
    assert len(reviews) == 1
    assert reviews[0]["outcome"] == "held-for-review"
    assert "truncated" in reviews[0]["payload"]["reason"]


def test_the_truncated_reading_never_becomes_established_text(truncated_reading_run):
    """The whole point: the reading exists and carries text, and none of it reaches
    the one place that turns a reading into the established text."""
    _, tree = truncated_reading_run
    readings = [
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a1"
    ]
    assert len(readings) == 1
    assert readings[0]["outcome"] == "truncated"
    assert readings[0]["payload"]["text"], "the reading must really carry text to be a hazard"

    established = [
        record
        for record in artifacts(tree, ARCHETYPUS, "archetypus")
        if record["payload"]["act_key"] == "a1"
    ]
    assert established == [], "a failed reading may not be established"

    export = export_of(tree)
    assert [item["act_key"] for item in export["non_delivered"]] == ["a1"]
    assert export["aggregate"]["status"] == "partial"


def test_the_act_whose_reading_succeeded_is_still_delivered(truncated_reading_run):
    """Invariant #14 — the refusal must not have bought its strictness by refusing
    good readings too. a2 read cleanly in the same run and is established."""
    _, tree = truncated_reading_run
    export = export_of(tree)
    assert [item["act_key"] for item in export["delivered"]] == ["a2"]
    established = [
        record
        for record in artifacts(tree, ARCHETYPUS, "archetypus")
        if record["payload"]["act_key"] == "a2"
    ]
    assert len(established) == 1
    assert established[0]["outcome"] == "established"


def test_the_recensor_refuses_a_testimonium_from_a_chair_the_run_never_sealed(tmp_path):
    """The mirror of the vanished-chair hole, and the one that counts toward the floor.

    `chair_outcomes` reports every role it finds a testimonium for, and
    `witness_coverage` counts completed-class outcomes without asking where they
    came from. So a testimonium under a role `run.json` never named raised the
    completed count: two real witnesses and one stranger read as three, and
    `under_witnessed` came back False on a run that was genuinely short a witness.
    Found by CodeRabbit on pull request 16.
    """
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "r",
                "--scenario",
                "happy",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"

    # Forge one: an existing testimonium re-published under a chair name the run
    # was never sealed with. Written straight into the tree, because no stage
    # would produce it — that is the point of checking rather than trusting.
    tree = RunTree(root, "r")
    entry = next(
        record
        for record in artifacts(tree, ATTESTATORES, "testimonium")
        if record["payload"]["chair"] == "attestator_1"
    )
    forged = json.loads(json.dumps(entry))
    forged["payload"]["chair"] = "attestator_9"
    forged["attempt_id"] = attempt_id(forged["subject_id"], "read:attestator_9", 1)
    forged["artifact_id"] = artifact_id(
        ATTESTATORES,
        "testimonium",
        forged["subject_id"],
        forged["attempt_id"],
    )
    forged["self_hash"] = self_hash(forged)
    path = tree.resolve(
        f"{STAGE_DIRECTORIES[ATTESTATORES]}/artifacts/testimonium/{forged['artifact_id']}.json"
    )
    path.write_text(json.dumps(forged), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "5_recensor" / "run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "an unsealed chair was accepted into the coverage count"
    assert "attestator_9" in result.stderr
    assert "not sealed with" in result.stderr


def test_armarium_rechecks_the_filename_a_page_was_sealed_under(tmp_path):
    """The last boundary compares each page against `run.json`'s ledger row itself.

    Distinct from the pixel recheck above, and from the corpus-seal recheck: those
    two cover a sealed page's bytes and the census as a whole. This covers the
    filename and digest a page artifact says it came from, which is the link ruling
    1 is about — "we literally need the file name. That is how we link it." A page
    that reaches the export naming a different source than the one submitted is an
    export nobody can trace back, and it is the *refused* pages that nothing else
    would catch: they carry no pixels for the pixel boundary to check.
    """
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    entry = next(
        entry for entry in tree.build_manifest(EXEMPLAR)["artifacts"] if entry["kind"] == "page"
    )
    path = tree.resolve(entry["relative_path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["declared_path"] = "some-other-scan.png"
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    seal_path = tree.resolve(
        tree.artifact_path(
            EXEMPLAR,
            "seal",
            artifact_id(EXEMPLAR, "seal", "corpus-seal"),
        )
    )
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    for reference in seal["inputs"]:
        if reference["relative_path"] == entry["relative_path"]:
            reference["sha256"] = digest_bytes(path.read_bytes())
    seal["self_hash"] = self_hash(seal)
    seal_path.write_bytes(canonical_bytes(seal))
    tree.write_manifest(EXEMPLAR)
    before = snapshot(root)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/7_armarium/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "no longer matches its submitted filename and digest" in result.stderr
    assert snapshot(root) == before
