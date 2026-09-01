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
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from argparse import Namespace
from copy import deepcopy
from io import BytesIO
from itertools import combinations, product
from math import comb
from pathlib import Path
from zipfile import ZIP_STORED, BadZipFile, ZipFile

import pytest

from common.chairs import ChairIdentity, load_models_toml
from common.contracts.approval import build_approval_record
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
    INK_MAP,
    PERLECTOR,
    RECENSOR,
    STAGE_DIRECTORIES,
    STAGES,
    WRITING_DIRECTORIES,
)
from common.contracts.uncertainty import from_perlectio
from common.fixture_identity import page_identity
from common.hard_failure import load_hard_failure_policy, tally_hard_failures
from common.imaging import PNG_SIGNATURE, decode_grayscale_png
from common.runtree.store import RunTree
from common.stage import (
    DEFAULT_SERVING_RECIPES_CONFIG_PATH,
    EXIT_FATAL,
    EXIT_HELD,
    _decode_environment,
    _validate_decode_environment,
    load_fixture,
    open_context,
    run_config_bindings,
    run_sealed_config_digests,
    stage_parser,
    verify_final_seal,
)
from conftest import rebind_stage_seal_artifact as rebind_stage_seal
from operations.operator import surface, volume_s3
from operations.submit import gate, submit

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE = "synthetic-two-page-v0"
# Spelled out rather than imported from `common.stage`, deliberately. This is the
# value an operator types into `--nuda-approval-ref`, and the acceptance harness
# pins the command a person would run. An import would follow a renamed or
# versioned selector silently; the literal makes that rename a loud failure here,
# which is where a change to operator-facing vocabulary should surface.
NUDA_APPROVAL_SUBJECT = "lectio-nuda-sampling-design.v1"


def _load_recensor():
    path = ROOT / "pipeline/5_recensor/run.py"
    spec = importlib.util.spec_from_file_location("recensor_run_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECENSOR_RUN = _load_recensor()
NO_PAGE_CONSERVATION = RECENSOR_RUN.NO_PAGE_CONSERVATION
NO_PAGE_CONTENT_COVERAGE = RECENSOR_RUN.NO_PAGE_CONTENT_COVERAGE

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
# residual-holding-act mechanism (`pipeline/2_designator/run.py::hold_residual_act`,
# `common.stage::_verify_minted_act_rows`) never fires and adds no artifact to either
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
#
# Re-pinned host-side after the R8 uncertainty build added the canonical
# uncertainty layer to each established Archetypus record. On that build's
# pre-R5b base, fresh real orchestrator runs measured 60 files for happy (exit 0)
# and 65 files for review (exit 3).
#
# R4 audit verified that the apparent Linux/macOS divergence was a snapshot-root
# error, not different recorded bytes. Passing `<runs-root>/r` instead of
# `<runs-root>` removes the `r/` prefix from every relative inventory key and
# reproduces the two alleged Linux digests exactly while every per-file digest
# remains identical. `semantic_snapshot` now refuses that ambiguous call shape,
# and no platform-local value was ever substituted for a host-measured one.
#
# The literals below are re-measured on this change, and each re-measurement
# names what moved them. A mismatch here is evidence about the recorded
# artifacts, never about the platform. Re-measured host-side after rebasing
# R5b onto merged R4, then again on R5b's CR round 1: the sealed audit
# policy's bytes changed (its comment now states the one-round build limit),
# and every Recensor review records the Pass-C verdict as data
# (audit_unresolved) — round 2 closed the recovery-requested review's gap in
# that same field, which moved the review literal alone. R5b itself moved the
# file counts from R4's 60/65 to 64/71 (each act gains an audit draft and an
# audit finding); the counts then held at 64/71 across every one of this
# loop's re-measurements while only digests moved.
#
# Re-measured host-side after rebasing R6 onto merged R5b. R6 changes recorded
# bytes without adding files: witness_span now stores RAW page-text indices
# (translated from the matcher's normalized space at the one storage point),
# every Recensor review gains the coverage fact set, and the review route
# composes testimony/audit/floor causes in stable order. Fresh real runs via
# this module's `orchestrate` and `semantic_snapshot_digest` helpers measured
# 64 files for happy (exit 0) and 71 for review (exit 3) — the counts R5b
# established, unmoved by R6.
#
# Re-pinned for R6 CodeRabbit round 3 because every Recensor review's testimony
# coverage fact replaces per-character uncovered offsets with lossless half-open
# ranges plus their explicit count. That field-shape change moves recorded bytes
# but writes no new files: fresh real runs through this module's own helpers held
# at 64 files for happy (exit 0) and 71 for review (exit 3).
#
# Re-measured for R6 CodeRabbit round 4 finding A after an unmeasured page's
# testimony content coverage changed from the measured-clean shape to None-valued
# `by_chair`/`shortfall` plus a reason. Neither acceptance scenario exercises that
# unavailable shape -- every reviewed page has reported page-witness text -- so
# both digest literals remain byte-identical, with file counts still 64/71.
#
# Re-measured host-side after rebasing R8 onto merged R6. R8 carries the
# canonical uncertainty layer through Archetypus and the Armarium exports:
# recorded bytes move in the acts table, export manifest, and display
# projections, while no new files are written. Fresh real runs via this
# module's `orchestrate` and `semantic_snapshot_digest` helpers measured
# 64 files for happy (exit 0) and 71 for review (exit 3) — the counts R5b
# established, unmoved by R6 and R8 alike.
#
# Re-pinned for R8 CodeRabbit round 2 finding 2. The acts.sqlite column named
# `uncertainty_spans_json` actually stores the complete canonical uncertainty
# layer, so it is now truthfully named `uncertainty_json`. Only that SQLite
# schema identifier changed; fresh runs through this module's own helpers held
# the file counts at 64/71 while the two semantic database identities moved.
#
# Re-pinned for the Sol-S2 repair: every Perlectio's `payload.audit` now carries
# `request_digest` — the digest of the re-proof request the reader actually
# received, or None exactly when no request was delivered (no flags, or a spent
# round_cap). The field is present in every scenario's Perlectio records, so
# recorded bytes move in both trees even though only audit-bearing acts deliver
# a request; no new file is written. Both values were measured twice from fresh
# real runs through this module's own `orchestrate` and
# `semantic_snapshot_digest` helpers; counts held at 64 files for happy
# (exit 0) and 71 for review (exit 3).
#
# Re-measured host-side for the T0 export-honesty repair, and this one is a
# deliberate record change rather than drift. Every delivered `manifest-entry`
# and export row now carries the Archetypus's own `text_status` and its
# transcription annotation layer; the run aggregate's basis carries the per-act
# status it was measured from; and the acts database, JSONL and readable text
# bundle carry both beside each literal (with the semantic annotation row fields
# renamed apart from them, `semantic_annotations` / `semantic_annotation_status`,
# so one word no longer answers for two different layers). Recorded bytes move in
# every Armarium artifact and in the sealed bundle blob; no new file is written.
# Fresh real runs through this module's own `orchestrate` and
# `semantic_snapshot_digest` helpers held the counts at 64 files for happy
# (exit 0) and 71 for review (exit 3).
# (Both values re-measured once more at host integration: the acts row and
# sqlite schema ids were bumped to v2 for the shape change — CR W15's silent-
# rename miss covered in the same bump — and `PRAGMA user_version` moved to 2
# with them, which moves the recorded bytes again. Measured twice from fresh
# runs; counts and exits held at 64/0 and 71/3.)
# (And once more for the review-driven candidate 3: the export manifest and
# sources ids moved to v2 with their own shape changes — the renamed
# annotations claim and the now-required per-outcome text_status.)
# (Final values, measured twice at the exact candidate tree: an interim pair
# was measured before `_armarium_bundle_semantics` below learnt the v2
# manifest id, so the bundle digested on its opaque fallback — the pin must
# always be measured after the LAST byte of the candidate is in place.)
# (Values below are re-measured on THIS branch rebased onto main with the
# reproof theme merged — the combined tree, measured twice, fresh.)
#
# Re-pinned for the Sol-S1 fallback-witness fix. No behaviour in either scenario
# changed and no file moved: what moved is the sealed fixture, which
# `run_config_bindings` folds into `config_digest` whole (`"fixture": fixture`),
# so declaring the ink-free page's three empty witness responses and the new
# `ink-free-page-unwitnessed` scenario moves every artifact digest under happy
# and review even though neither scenario reads a word of it. Both values were
# measured from fresh real runs through this module's own `orchestrate` and
# `semantic_snapshot_digest` helpers, never derived arithmetically; counts held
# at 64 (happy, exit 0) and 71 (review, exit 3).
# (Values below re-measured twice on the fallback replacement branch — main
# with the reproof and export themes merged plus this theme — fresh runs
# through this module's own helpers; counts and exits held at 64/0 and 71/3.)
#
# Re-pinned for Opus-F3: act crops are evidence written during a run, and
# `common/imaging.py::crop_png` now writes them through this project's own
# deterministic encoder on both paths instead of `zlib.compress(level=9)` and
# Pillow's PNG writer. The pixels are unchanged — `semantic_snapshot` already
# binds a PNG's pixels rather than its compressor bytes, and the neighbouring
# `test_semantic_snapshot_digest_binds_png_pixels_not_compressor_bytes` is the
# proof — but a crop blob is content-addressed, so its *path* carries its byte
# digest, and every artifact that names one moves with it. That is exactly the
# property the change buys: those paths and digests are now the same on every
# zlib build rather than only on this one. Fresh real runs through this module's
# own helpers held the file counts at 64 for happy (exit 0) and 71 for review
# (exit 3).
# (Values below re-measured twice on the crops replacement branch — the
# fallback theme's tree plus this theme — fresh runs through this module's
# own helpers; counts and exits held at 64/0 and 71/3.)
#
# Re-measured after act a1's fixture recovery rectangle was repaired from
# 16,16,168,88 to 0,0,200,114. The old rectangle was a strict subset of the
# padded capture rect the proposal had already cut (12,15,188,99), so the
# `review` scenario's one fallback recrop recovered no pixel at all; the
# Designator now refuses such a recrop by name. **Both** digests move, not only
# review's: `run_config_bindings` folds the fixture itself into `config_digest`
# ("the digest of *everything* that shapes this run's behaviour"), so any fixture
# byte moves every scenario's `run.json`. Review's tree moves twice over — its
# recovery crop is a different, larger rectangle with different pixels. No new
# files: fresh real runs via this module's `orchestrate` and
# `semantic_snapshot_digest` helpers held at 64 files for happy (exit 0) and 71
# for review (exit 3).
# (Values below re-measured twice on the coverage replacement branch — the
# crops theme's tree plus this theme — fresh runs through this module's own
# helpers; counts and exits held at 64/0 and 71/3.)
#
# Re-pinned for the T7 config sealing family. Every run authority now records
# `sealed_config_digests` — the digest of each configuration file the run sealed,
# under the name its point of use asks for — so a reader holding only the tree can
# name the policy bytes that governed it rather than only test a candidate file
# against `config_digest`. That moves `run.json`'s recorded bytes in both scenarios
# and writes no new file: fresh real runs through this module's own `orchestrate`
# and `semantic_snapshot_digest` helpers held the counts at 64 files for happy
# (exit 0) and 71 for review (exit 3), unmoved since R5b.
# (Values below re-measured twice on the sealing replacement branch — the
# coverage theme's tree plus this theme — fresh runs through this module's
# own helpers; counts and exits held at 64/0 and 71/3.)
#
# Re-pinned again for the one attempt model. Three things move `run.json` and the
# sealed receipts together: `sealed_config_digests` gains `hard-failure`, the
# sealing family's fourth and last member; the fixture's two reread scenarios
# declare their second attempts on the act-scoped chair, and the fixture is folded
# into `config_digest`, so every scenario's authority moves with it; and the
# offline serving-receipt endpoint is `fixture://offline-chair-runner`, since
# *seat* is harness vocabulary that does not cross into the pipeline (GLOSSARY)
# and that string rides in sealed receipt bytes. Neither reread scenario is
# orchestrated, so no orchestrated tree gains a file: fresh real runs through this
# module's own helpers held the counts at 64 for happy and 71 for review again.
#
# Re-pinned on the cleanup branch. What moved the digests is CONFIGURATION BYTES:
# `config/designator_geometry.toml` and `config/serving_recipes.toml` are both
# digested as raw bytes into `config_digest` (`common/stage.py`), so a comment
# rewritten in either one moves both scenarios' run authority, and the S8 label was
# dissolved in both. `config/models.toml` also changed, but only in comments, which
# its parsed record does not carry.
#
# Two things beyond configuration changed on this branch and NEITHER moves these
# trees, which is a claim rather than an assumption — a stage change that happened
# not to move a pin is exactly the thing a comment should not round down to "no
# stage logic moved". The Attestatores page-join failure reason is recorded bytes,
# but no orchestrated scenario reaches that branch. And
# `pipeline/5_recensor/run.py::blank_corroboration` had its control flow reordered
# so the completed-chair evidence validation runs before the witness_uncovered /
# unresolved-chairs short-circuit: that changes only WHICH path raises
# `FatalAccounting` over a record this pipeline's own writer could not produce, and
# these scenarios publish no such record, so their bytes are untouched.
#
# Counts and exits held at 64/0 and 71/3.
# (Values below re-measured twice at the final candidate — after the last byte
# changed — through this module's own `orchestrate` and `semantic_snapshot_digest`
# helpers, per the Tier-0 loop lesson that a pin measured mid-branch is a pin
# measured against a tree nobody pushed. They moved a second time when the
# CodeRabbit round reworded `config/designator_geometry.toml`'s caveat, which is
# that lesson arriving on schedule: the first measurement was taken before the
# review, and a review that changes a sealed configuration file changes the pins
# with it.)
# Re-pinned once more, for `config/hard_failure.toml` alone. Tyrel confirmed the
# hard-failure cap and declined to re-open its outcome taxonomy, so the file's
# "PROPOSED, NOT YET APPROVED" header became false and was rewritten to record the
# ruling. The file is sealed into every run by its bytes (`sealed_config_digests`
# gained `hard-failure` in the T7 family), so a comment in it moves both scenarios'
# authority. No code changed with it; counts and exits held at 64/0 and 71/3.
# Re-pinned 2026-08-20 for `config/hard_failure.toml` alone, again: the trust
# audit found the taxonomy's rationale still calling pipeline/4_perlector/run.py
# a walking-skeleton stub — a built stage whose only *reader* is the fixture —
# so the comment was corrected and the recorded rule-13 decision on
# `(perlector, failed)` written into it. Comment bytes only; both digests
# re-measured twice through this module's own helpers at the same tree, counts
# and exits held at 64/0 and 71/3.
# Re-pinned in the Unit 18 formal correction pass after `run.json` began binding
# `register_required` separately from `register_digest`. The distinction closes
# the empty-register drift hole: an explicitly supplied empty register can grow,
# so later stages must not mistake its empty digest for "no live register to
# check." The new authority field moves both semantic trees.
#
# Re-pinned 2026-08-21: `proof/skeleton_fixture.toml` gained the
# `continuation-recovery` scenario and act a2's recovery rectangle. The fixture
# declaration is bound into every run's `config_digest`
# (`common/stage.py::run_config_bindings` — "the digest of everything that shapes
# this run's behaviour ... model configuration, fixture, scenario"), so declaring
# a scenario that neither pinned run executes still moves both authorities. That
# is the seal behaving correctly, and it is why the pin moves in a commit that
# changed no stage code.
#
# Moved again the same day, one commit along: the Pass-C audit stopped
# multiplying an act's ACT-LOCAL flags by the number of pages its crop spans, so
# a2's audit draft and finding carry two `testimony-diff` flags rather than four,
# and its sealed re-proof plan two rows rather than four.
#
# Re-pinned in the formal review: `page_witness_count` again counts distinct
# chairs rather than the new `(chair, page)` attachment rows. The continuation
# dossier therefore reports the two witnesses the run configured, not four
# witnesses invented by its two-page span.
#
# Re-pinned for stage-completion seals. Every ordinary pass adds a
# `decode-environment` and `stage-seal`; recovery re-entries add their own pair,
# so review is measured rather than inferred.
#
# Re-pinned in the Phase-2 Sol correction: seals now bind their decode-environment
# bytes and Armarium boundary records no longer claim delivery.
#
# Re-pinned for `triage_modes.toml`, which is sealed into every run and so moves
# both authorities without adding an artifact.
#
# *Not* re-pinned for the Door's cluster report, though a note here once said it was.
# `publish_cluster_report` writes nothing unless some admission carries a
# `triage_link`, and only the real-ingress route ever supplies triage rows to
# `expand_sources`; the fixture route these runs take submits none, so the report
# returns None and no artifact reaches the tree. The pins did move at the merge that
# landed the report, but for the typed render-origin validator and the page-identity
# refusal composed in the same commit. Found by CodeRabbit.
#
# Re-pinned for Unit 10A: adapter names and scopes enter models_digest and
# config_digest, so the pins bind those provenance fields even though artifact
# counts and outcomes do not move.
#
# Re-pinned at each merge that brings two authority-moving branches together. The
# pins below stand for the merged tree and nothing else: every contributing branch
# moved them on its own, so no branch's own pin describes this tree and taking one
# side would be a pin measured against a tree nobody ran. Both values were measured
# through this module's own `orchestrate` and `semantic_snapshot_digest` helpers,
# twice, at two independent run roots, and the file counts below were re-measured
# the same way. The host re-measures at integration.
#
# Re-pinned for Unit 10B, the witness intake contract: the pins bind closed
# witness presentations, observations, and explicit absence/continuation scope
# at canonical run id "r"; the review pin also binds a serving receipt for
# attempted page testimony whose response was unusable — presentation and
# receipt must describe the same event. Changing those facts requires
# remeasurement even when artifact counts and exits stay fixed.
#
# Re-pinned at this merge (10B onto the composed pr tree): every contributing
# branch moved these pins on its own, so no branch's own pin describes this
# tree. Both values measured through this module's own `orchestrate` and
# `semantic_snapshot_digest` helpers, twice, at two independent run roots.
# Re-pinned for Unit 10B: every Testimonium now binds its closed `presented`
# image recipe and `observed` page-pixel geometry. The native marginal fixture
# observation also moves the sealed fixture/config authority. The final absence
# repair records held, refused-page, and absent-chair paths as `presented: {}` /
# `observed: []`, rather than inventing an image no chair saw; that moves the
# review tree but no artifact kind. Counts remain 84/0 and 97/3.
# Sonnet audit seat (10B, seat 2 of 4): the absence repair's page-scope gate
# (`reading`, i.e. WITNESS_READING_OUTCOMES) was narrower than its act-scope
# twin (`attempted`, i.e. ATTEMPTED_WITNESS_OUTCOMES) and collapsed a page
# witness that was genuinely shown pixels and returned an unusable response
# into the same `presented: {}` fact as a chair never shown an image at all
# (GOVERNANCE 2). Confirmed live in this exact fixture: attestator_3's page-2
# Testimonium in the "review" scenario, whose sole contributing act (a2's
# continuation) fails for that chair. Fixed by `page_witness_attempted`
# (`pipeline/3_attestatores/run.py`), gating page-scope `presented` on
# ATTEMPTED_WITNESS_OUTCOMES like the act-scope writer already does. Moves only
# the review tree, back to its pre-absence-repair bytes because that was the
# only record the narrower gate touched; happy is unaffected (no page in it
# has every contributing act fail for a page-scoped chair). Counts unchanged at
# 84/0 and 97/3. Re-measured through this module's own `orchestrate` and
# `semantic_snapshot_digest` helpers at canonical run id "r".
# Opus audit seat (10B, seat 3 of 4): the fixture's disagreeing native
# observation moves, so the sealed fixture/config authority moves with it. The
# box was x 0..20 by y 230..250, which overlapped act a2's own page-1 crop by
# eight pixels each way — genuinely proposed ink, reported as unaccounted only
# because the routing rule asked each act about its own regions alone. With the
# denominator corrected to the page's whole proposal set, that box would have
# produced no finding at all and the disagreeing chair would have stopped
# exercising the rule it exists for; it is now x 0..10 by y 200..240, clear of
# both crops in x. Fixture bytes only: no stage behaviour, artifact kind, count,
# or exit code changes (84/0 and 97/3 hold). Re-measured through this module's
# own `orchestrate` and `semantic_snapshot_digest` helpers at canonical run
# id "r".
# And again, in the same seat, for the continuation-scope statement: every act
# Testimonium gains `unpresented_regions`, naming the bound proposal crops its
# one presented image does not speak for. Empty everywhere except the
# continuation act, where it names the far-side crop that `presented`/`observed`
# structurally cannot describe (their boxes are in the presented page's pixel
# space). A schema field on an existing kind: counts and exits hold at 84/0 and
# 97/3, no artifact kind added. Re-measured through this module's own
# `orchestrate` and `semantic_snapshot_digest` helpers at canonical run id "r".
# Unit 10C re-pin: native/derived containment replaces identity coverage and
# page Testimonia retain the non-verdict partition record. The review-only
# marginal observation routes both page acts through bounded recovery without
# assigning it to either.  The final corrective pass separates its retained
# recovery route from textual shortfall, so fresh canonical-id "r"
# measurements are 84/0 (happy) and 109/3 (review).
# Re-pinned for Unit 12 (Churro as its own stage), at the audit seat. THREE
# things move here and they are different in kind, so they are separated.
#
# 1. `proof/skeleton_fixture.toml` gains eight `[[churro_page_response]]` rows
#    and the `churro-native` scenario. `run_config_bindings` folds the whole
#    fixture declaration into `config_digest`, so this alone moves BOTH pinned
#    authorities even though only `happy` reads any of the new rows.
# 2. `happy` now runs the real Churro page-capture boundary. Its four declared
#    responses reproduce the previous synthetic join text EXACTLY, so no act's
#    reading, span, alignment or dissent row moves; what moves is that each of
#    the four page Testimonia gains a `native_capture` block, and each captured
#    response is written content-addressed as its own retained blob. That is
#    the file-count change: 84 -> 88, four raw responses, one per (page, chair).
#    A capture path no pinned scenario runs is a page-witness mechanism a
#    refactor can disable with every test still green, which is the Sol-S1
#    failure; `happy` runs it now.
# 3. `review` declares no Churro response, keeps the synthetic join, and writes
#    no new file -- its count holds at 109. Its digest still moves, for reason 1
#    alone.
#
# Also in the same seat and moving neither count: the page Testimonium writer now
# validates its own `content_health` (the tally's read-back filters to
# `kind == "testimonium"` and has never seen a page record), post-hoc repetition
# detection inspects the parsed transcription rather than the raw bytes whose
# closing `</output>` tag made every tail window differ, and a page-witness act
# attachment resolves its outcome from the page attempt that produced the record
# it names. None of the three alters a byte in either pinned scenario: happy and
# review carry no repetition, no unparseable capture, and no failed page capture.
#
# Both values below were measured twice, in independent temporary roots, through
# this module's own `orchestrate` and `semantic_snapshot_digest` helpers at
# canonical run id "r", after the last byte of the candidate was in place.
# Counts and exits: 88/0 (happy) and 109/3 (review).
# Sol formal review binds each retained raw Churro response into its page
# Testimonium envelope's `inputs`, so every consumer read verifies the bytes
# against `raw_response_ref` rather than trusting a nested reference it never
# opens. The same four files remain and every payload fact is unchanged; only
# happy's four page-artifact envelopes and their downstream references move.
# Re-measured through this module at canonical run id "r": 88/0. Review has no
# native captures, so its 109 files and digest remain unchanged.
#
# Opus audit seat (10C, seat 3 of 4): the declared fixture gains one scenario,
# `coverage-recovery`, and the single native observation that scenario needs.
# Sonnet's recorded gap was that the coverage-triggered recovery origin had no
# isolated test: in `review` that stimulus sits beside a scenario-declared
# recrop on a1 and a scenario hold on a2, so no assertion there can tell the
# two origins apart. The new scenario declares neither, which makes the
# witness's own unclaimed observation the only thing in it that can ask for a
# recovery or hold an act (`pipeline/5_recensor/test_coverage_recovery_origin.py`).
# `config_digest` binds the WHOLE fixture declaration (`common/stage.py`'s
# `run_config_bindings`), so adding a scenario nobody runs still moves every
# run's digest. Fixture bytes only: no stage behaviour, artifact kind, count or
# exit code changes — 84/0 and 109/3 hold, and each digest below reproduced
# twice in independent temporary roots through this module's own `orchestrate`
# and `semantic_snapshot_digest` helpers at canonical run id "r".
# Unit 11 re-pin: Chandra retains one native JSON response blob for each of its
# two fixture act reads, so both canonical trees gain two content-addressed
# custody artifacts. In `review`, the page partition now carries both those
# captured act boxes and the declared marginal observation: the former attach
# each act witness and keep dissent measurable, while the latter remains an
# unrouted observation for coverage recovery. Fresh canonical-id `r` runs
# measured 86 files/exit 0 for happy and 111 files/exit 3 for review through
# this module's `orchestrate` and `semantic_snapshot_digest` helpers.
# Unit 11 Sonnet re-pin: `ink-free-page`'s minted `page-fallback:3` act declared
# a whole-page Chandra `raw_response` (previous re-pin, below) whose native
# block quantized to a box that both (a) fell outside the sealed page by one
# pixel, holding the run before the Perlector ever established a reading for
# that act, and (b) once the overflow was corrected, fully contained the
# fallback act's own region -- retroactively "witness covering" a recovery
# crop the architecture requires stay visibly under-witnessed
# (`test_an_ink_free_page_fallback_is_read_but_not_retroactively_witness_covered`).
# The row's `raw_response` is removed rather than reshaped: unlike
# `confirmed-blank`/`blank-with-dissent`, this scenario's design is that no
# witness geometry covers the recovery crop. The fixture is bound whole into
# every run's config digest, so both canonical trees change although neither
# happy nor review touches `page-fallback:3`. Measured twice from fresh
# final-candidate runs at canonical run id "r": 86/0 for happy and 111/3 for
# review (counts and exit codes unchanged from the prior re-pin).
#
# Unit 11 prior re-pin: Chandra's scenario-specific inputs now retain native
# layout blocks wherever the scenario overrides its base response, including a
# completed empty response.  The blocks are witness-reported geometry, never a
# whole-page presentation fallback, so blank corroboration and the capability
# scenario each retain the page-witness evidence their assertions require. The
# fixture is bound whole into every run's config digest; therefore both canonical
# trees change although happy and review use only the base Chandra responses.
# Measured twice from fresh final-candidate runs at canonical run id "r": 86/0
# for happy and 111/3 for review.
#
# Unit 11 Opus re-pin (final seat), and the last one in this unit: the durable
# page Testimonium now names the retained responses its own derived geometry was
# quantized from, and the rule that quantized them (`raw_response_refs`,
# `adapter_metadata`). The act-scoped Testimonia already carried both, but they
# are the compatibility bridge Unit 14 deletes, and the page record is the one a
# page-scoped occupant actually produces -- so the record holding the integers
# held no route back to the floats they came from (GOALS 5; ARCHITECTURE
# invariant 3). Two payload fields on the one Chandra page record whose geometry
# is native; a record whose observations are only the presentation echo reports
# no conversion, because none happened. No new artifact, no new blob, no
# behaviour change: counts and exit codes hold at 86/0 and 111/3, and each
# digest below reproduced twice in independent temporary roots through this
# module's own `orchestrate` and `semantic_snapshot_digest` helpers at canonical
# run id "r".
# Phase-2 Sol review re-pin: the placeholder JSON accepted by the offline
# Chandra adapter now carries an explicit fixture schema.  Without that label,
# a real but still-unverified Chandra response that happened to expose the same
# generic `markdown`/`blocks`/`bbox` keys could acquire the fixture's declared
# sealed-page-pixel quantization rule.  The label changes the fixture bytes,
# their retained blob digests, and the config digest bound into every artifact;
# counts and exits remain 86/0 and 111/3.  Both digests below reproduced twice
# from independent canonical-id `r` runs after the correction.
# Superseded, and kept because this log is append-only: the `coverage-recovery`
# entry higher up added fixture bytes alone and so moved every run's digest.
# That candidate changed no stage behavior, artifact kind, count, or exit code;
# its canonical measurements were 84/0 and 109/3, and the entries below have
# since replaced both.
# DAI adds a content-addressed adapter crop, making canonical happy/review
# counts 86/0 and 111/3. Identity-sized views seal ``crop`` because Pillow never
# consults LANCZOS there. Both digests were reproduced twice in independent
# temporary roots at canonical run id "r" after those bindings were final.
# Re-pinned at this merge (13 onto the composed pr tree): the DAI adapter
# joins the composed roster (attestator_2, act scope) with its relabel-proof
# retain wrapper; presentation validation knows all three adapters (Chandra
# and Churro present the exact image they were given, DAI re-derives its
# crop-resize recipe); happy gains attestator_2's two retained DAI act
# responses (88 -> 90) and review likewise (111 -> 113). Measured twice at
# two independent run roots through this module's own helpers.
# Unit 13 final-seat ledger reason: identity-sized DAI views now seal the exact
# `crop` operation that produced them, rather than a resize recipe naming
# LANCZOS even though Pillow returns a copy before consulting the resampler.
# Artifact counts and exits stay 86/0 and 111/3; only the Testimonium transform
# records and their derived bindings move. Both digests below reproduced twice
# in independent temporary roots at canonical run id "r".
# The Ink Map adds five files to both canonical trees: two page records, its
# stage seal, its decode-environment record, and its manifest. Happy therefore
# has 97 files and review 118, with unchanged exits 0 and 3.
# `_semantic_decode_environment` excludes route labels from the portable
# snapshot, so the Ink Map's project-PNG declaration cannot move these pins.
# Both digests reproduced twice in independent roots at canonical run id "r".
# Re-pinned at this merge (9 onto the composed pr tree): the Ink Map stage
# joins the walk between Exemplar and Designator, adding its artifacts to both
# canonical trees (90 -> 95 happy, 113 -> 118 review). Measured twice at two
# independent run roots through this module's own helpers.
# Unit 14A re-pin: the happy tree now carries the retained-native testimony
# seam -- its act attachments explicitly say whether they are comparable, and
# the Perlector dossier records `reported_basis` plus sealed-proposal
# `edge_deltas` rather than a retired `payload.reported` bridge. The Door's own
# decode-environment record is unchanged (it still names both library routes it
# can take, `common/stage.py::_decode_environment`); the semantic reducer
# already makes every stage's decode-environment payload host-observation-only
# in this pin, so it was never part of what moved. Happy remains 92 files and
# exits 0. Both values were measured twice in independent roots by this
# module's `orchestrate` and `semantic_snapshot_digest` helpers at canonical
# run id "r".
# Sol review re-pin: audit location provenance now names only reports whose
# exact comparison produced a frozen testimony-diff location; agreeing
# witnesses are no longer falsely attributed as sources. Multi-page edge-delta
# rows are also normalized to their declared `(ordinal, region_id)` order after
# all page contributions are combined. Counts and exits remain unchanged. The
# two repeatability tests below each measure the candidate before and after an
# identical rerun at canonical run id "r".
# Re-pinned at this merge (14A onto the composed pr tree): attachments say
# whether they are comparable, the reported projection is retired for direct
# native-payload coverage, and page-edge overshoots ride the partition keyed
# to their retained responses. File counts hold at 95/118. Measured twice at
# two independent run roots through this module's own helpers.
# Unit 17 ledger reason: `config/pod_placement.toml` is now sealed into every
# run's `config_digest`. Its final bytes therefore change both scenario trees,
# although they add no artifact: on the composed tree happy remains 95/0 and
# review remains 118/3. Both digests and counts were re-measured twice in
# independent roots through this module's own helpers at canonical run id "r".
# Union re-pin (host, seam of Unit 9 x Unit 14A): each side above measured its
# own tree — Unit 9's carried the ink-map stage without the native-testimony
# seam, Unit 14A's the seam without the stage. The combined tree holds both, so
# neither side's digest can stand; the values below are measured on THIS tree,
# twice each in independent temporary roots at canonical run id "r", through
# this module's own orchestrate and semantic_snapshot_digest helpers. Counts are
# re-measured on the composed tree at unchanged exits.
# Edge findings release only when the retained ink runs are covered by verified
# crops. The v3 export carries one source row per sealed page so clean-machine
# verification derives the same held set, and testimony-diff bases use only
# chairs whose retained text differs. This pin was measured twice in independent
# roots at canonical run id "r".
# this module's own orchestrate and semantic_snapshot_digest helpers. Counts
# stay Unit 9's 97/118 (the seam moves bytes, not files) at unchanged exits.
# Unit 17 ledger reason: `config/pod_placement.toml` is now sealed into every
# run's `config_digest`. Its final bytes therefore change both scenario trees,
# although they add no artifact — on Unit 17's own tree that meant unchanged
# counts 92/113 at new digests.
#
# Union re-pin (host, Unit 17 joins the composed tree): the placement seal
# moves both digests again over the 97/118 trees the union already measured.
# The values below are measured on THIS tree, twice each in independent
# temporary roots at canonical run id "r", through this module's own helpers.
# Unit 14B reconciliation: the initial Ink Map finding is pre-proposal evidence.
# Armarium re-measures its retained runs against verified Designator crops, so
# the fully claimed synthetic ink releases rather than turning happy into a
# false hold. The positive remains real: a page with no such crop stays held.
# Both pins were measured twice in independent roots with this module's
# `orchestrate` and `semantic_snapshot_digest` helpers at run id `r`: happy is
# 97/0 and review is 106/3.
# Unit 14B fix pass 2: that release accidentally made Unit 10C's existing
# witness-observation recovery conditional on the new edge-ink trigger. The
# older path keeps its own origin and fallback-recrop budget; it is not an
# edge-ink request. Restoring it re-adds the second review recovery round, so
# review returns to 118/3 while happy remains 97/0. Both pins were measured at
# canonical run id `r` in two independent roots through the same helpers after
# this final semantic byte: happy reproduced its existing digest and review
# reproduced the replacement below.
# Unit 14B Opus audit: the Armarium's edge hold is now derived, on both sides,
# from one recorded ink-map row per sealed page carried in `sources.json`
# (`armarium-sources.v3`), so the clean-machine verifier recomputes the held set
# from the package's own source graph instead of reading it back out of the
# claim it produced. Every package member except EXPORT_MANIFEST.json used to be
# byte-identical whether or not a page was held, so a manifest built with the
# hold simply dropped verified clean against the very run that had it. Both
# trees gain the evidence, so both digests move; counts and exits are unchanged
# (happy 97/0, review 106/3). Measured twice in independent temporary roots at
# canonical run id "r" through this module's own `orchestrate` and
# `semantic_snapshot_digest` helpers.
# Unit 14B Opus audit, again in both trees: an audit draft's
# `flag_location_basis` named every chair that reported, not the chairs whose
# retained text departs from the reading. On this very fixture both acts raise
# two `testimony-diff` flags and the basis named three chairs -- attestator_1
# agreed with the reading exactly and was recorded as the basis of a flag it
# did not raise (GOVERNANCE 10, consult §4.7: a fact re-derived over a wider
# denominator than its writer counted on). The two producers are now held to
# the same count. Counts and exits unchanged (happy 97/0, review 106/3); both
# re-measured twice in independent roots at run id "r".
# Union re-pin (host, Unit 14B joins the composed tree with Unit 17's
# placement seal): both sides above measured trees missing the other's
# change; the value below is measured on THIS tree, twice, independent
# roots, rid "r", via this module's own helpers.
# Unit 19B Sonnet audit round 2: consult §3.2 step 7 and §7 forbidden shape 19
# named `page_id = page_ids[0]` a representative-singular picker shape and
# required its removal from the audit draft/finding payload; it is now gone
# from `common/perlector_audit.py`'s closed field sets, so the byte this
# digest covers shrinks by that one key on every act's audit chain. No file
# count or exit changes (happy 97/0, review 106/3). Measured twice in
# independent temporary roots at canonical run id "r" through this module's
# own `orchestrate` and `semantic_snapshot_digest` helpers.
# Unit 19B build round 3: `run.py`'s main loop now reads every logical act
# through `combined.run_logical_passes`, fed by `logical_reading.py`'s own
# physical-act partition (`build_run_partition`, `common/physical_act_partition.py`)
# and cross-capture autopsia (`act_autopsia`, `common/cross_capture_autopsia.py`)
# rather than one `build_reader_dossier` call per pass. The partition is one
# run-level artifact, sealed once and published as its own content-addressed
# blob under the Perlector's own stage (consult §9.1's Designator ownership is
# a recorded deviation, not a silent relocation -- see `logical_reading.py`'s
# module docstring) -- one new file per run, not per act. Every act's
# published dossier also carries the new `logical_act_id` and
# `cross_capture_autopsia` fields, and every stored payload carries the new
# `autopsia` field, moving the digest again on every existing file. No test
# scenario in this fixture registers a physical page, so every act still
# resolves as `image-local-singleton` and no reading, flag, or recovery
# decision changes: exits stay 0 (happy) and 3 (review); file counts move by
# exactly the one new partition blob, 97 -> 98 (happy) and 106 -> 107
# (review). Measured twice in independent temporary roots at canonical run id
# "r" through this module's own `orchestrate` and `semantic_snapshot_digest`
# helpers.
# Unit 19B Opus audit: the lectio-prior and lectio-nuda dossiers no longer
# carry `witness_covered` on their region rows. `build_dossier` omits that key
# entirely when it is handed no testimonia, and the pre-19B path built each
# unprimed pass with `testimonia=[]`; the combined path builds one dossier with
# witnesses and strips it per arm, and the strip stopped at `testimonia` --
# leaving the unprimed instrument holding a witness-derived fact about every
# region, which is the same defect an earlier pre-push review round already
# removed once (see the `witness_covered` note further up this comment block).
# Only the lectio-prior/nuda payloads move; file counts and exits are
# unchanged (happy 98/0, review 107/3). Measured twice in independent
# temporary roots at canonical run id "r" through this module's own
# `orchestrate` and `semantic_snapshot_digest` helpers.
# Unit 19B Sol formal review: every cross-capture dossier now binds the physical-
# act partition it cites as a direct envelope input, and multi-page audit
# denominators are canonical page-ID sets rather than ordinal traversal lists.
# The first change moves every reading and its downstream chain; the second
# moves the continuation act's audit chain. No reading, decision, exit, or file
# count changes (happy 98/0). Measured twice in independent temporary roots at
# canonical run id "r" through this module's own helpers.
# Unit 19C adds `cross_capture_coverage` to every Recensor review. With no
# sealed Designator survey, each measured act records the named instrument
# absence as `unresolved`; a Designator hold records `None`. The happy tree
# remains 98 files at exit 0. This digest was reproduced twice from independent
# canonical run-id `r` trees through `semantic_snapshot_digest`.
# Unit 2 re-pin: `config/decoding.toml` is sealed into every run's
# `config_digest`, so both scenario trees legally move without gaining an
# artifact. Happy remains 97 files and exit 0. The value below was measured
# twice in independent roots, rid "r", via this module's `orchestrate` and
# `semantic_snapshot_digest` helpers.
# Re-measured unchanged at Unit 2's audit seat, after the whole-pass resume
# reverted to one shared ordinal and the Perlector's reading ordinal went back
# to its crop-history derivation. Neither revert touches an orchestrated tree:
# on a clean run there is no prior attempt for either derivation to diverge
# over, which is a measurement here and not an inference -- twice more, in two
# further independent roots at rid "r", through the same two helpers.
# Unit 8 re-pin: the Door's computed per-page membership digests now bind the
# inspected source bytes into sealed run authority, rather than permitting two
# genuinely different page sets that share ordinals to name one membership.  The
# per-shard cap's explanatory comment is likewise sealed configuration, so its
# clarified wording also enters `config_digest` and run authority.  These are
# both intended semantic consequences of Unit 8's named deliverable.  Happy
# (97 files, exit 0) and review (106 files, exit 3) were each measured twice,
# in independent temporary roots, at canonical run id "r", through this
# module's own `orchestrate` and `semantic_snapshot_digest` helpers.
# Review only, once more in the same seat: a page witness invoked on every act
# and unusable on all of them now records the serving moment that produced it
# (`provenance_for(..., attempted=attempted_page)`), where the `reading` gate
# left `receipt_ref: None` beside a `presented` block claiming pixels were shown.
# One `receipt_ref` field on attestator_3's page-2 record; no new file, no count
# or exit change (97/3), and happy is untouched at the digest above.
# Audit-round re-pin: each witness-derived flag-location basis row now carries
# the span it accounts for, so the audit draft is bound to its flags by location
# rather than by list length. Draft bytes move and the artifacts referencing them
# follow; no artifact is added or removed, so happy stays 95/0 and review 118/3.
# Both digests were re-measured twice in independent temporary roots through this
# module's own `orchestrate` and `semantic_snapshot_digest` at canonical run id "r".
# Unit 14A audit: that renamed reason said something false. Under the legacy
# page join the outcome carried into an unaligned attachment is THIS ACT'S
# attempt, not the page Testimonium's -- `review`'s attestator_3 has a failed a2
# attempt beside a page-1 Testimonium that read a1 and records `read` -- so the
# record named a non-reading page Testimonium that had in fact read
# (GOVERNANCE 10). The reason now names the attempt in that case
# (`non-reading-act-attempt-<outcome>`) and keeps
# `non-reading-page-testimonium-<outcome>` for a native page capture, which is
# the only path where the page record's own attempt supplies it. One string on
# one attestator_3 attachment row; the review tree is otherwise unchanged at 113
# files and exit 3, and happy never carried that reason so its digest above is
# untouched. Measured twice in independent temporary roots at canonical run id
# "r" by this module's `orchestrate` and `semantic_snapshot_digest` helpers.
# Union re-pin: see the seam entry above the happy digest — measured on the
# combined Unit 9 x Unit 14A tree.
# The review fixture's marginal witness box contains no independently measured
# ink, so it cannot fund a second recovery. Its sole request records a causal
# origin for the page-wide grant, reducing the review tree's file count at exit 3. This pin was
# measured twice in independent roots at canonical run id "r".
# Re-pinned at this merge (14B onto the composed pr tree): edge findings ride
# every ink-map record, the recovery request carries origin as data, and the
# unconfirmed witness pointer no longer spends review's second recovery round
# (118 -> 106); happy holds 95 files. Measured twice at two independent run
# roots through this module's own helpers at canonical run id "r".
# Union re-pin: see the Unit 17 seam entry above the happy digest.
# Unit 14B Sonnet audit: fix pass 2's "restoration" above was the regression,
# not the fix. Unit 10C's own `unclaimed_observations` is a witness's report --
# a native/derived box the Attestatores reported, with no ink evidence behind
# it -- and consult §4.5 (`/out/CONSULT_REPORT.md`, BINDING) requires Unit 9's
# ink map to independently confirm real ink under that box before it may spend
# a bounded recovery or hold an act; "the box is a pointer; the ink is the
# evidence... this is what makes it not a picker." Measured directly against
# the checked-in fixture (`common.residual_ink.ink_runs` over
# `proof.synthetic_pages.page_bytes(1)`): the `review` scenario's marginal
# witness box at page 1, x 0..10 / y 200..240, sits over zero ink. The second
# review recovery round fix pass 2 restored was therefore spent on a2 (which
# `hold_acts` declares should go straight to a hold) for no evidence at all --
# a witness's own unconfirmed report picking a pipeline action for itself,
# exactly what GOVERNANCE 3 forbids. Restoring the ink gate removes that
# wasted round: a1's recovery request now names its true origin ("the crop may
# be incomplete", not the coverage-origin phrase, since its own box is equally
# unconfirmed by ink), and a2 goes directly to held-for-review, without an
# intervening recovery-request/region/reading round. Review returns to 106
# files at unchanged exit 3; happy is untouched (it carries no witness box at
# all) at the digest above. Both pins were measured twice in independent
# temporary roots at canonical run id "r" through this module's own
# `orchestrate` and `semantic_snapshot_digest` helpers: happy reproduced its
# existing digest and review reproduced the value below.
# Unit 14B Opus audit: Sonnet's ink gate above is confirmed and unchanged -- the
# marginal box still sits over measured-zero ink, so review keeps 106 files at
# exit 3 and no recovery round returns. This digest moved once for the Armarium
# source-graph byte named above the happy digest, and once more here: review's
# one recovery request now records its `origin` as data beside the sentence that
# states it, so the one-observation-one-request bound (consult base question 11)
# counts a recorded fact rather than re-reading prose. Happy has no recovery
# request, so its digest above is untouched by that second byte. The bound
# itself changes no fixture behaviour -- no shipped scenario has ink under a
# witness pointer at all -- and review stays at 106 files, exit 3. Measured
# twice in independent temporary roots at canonical run id "r" through this
# module's own `orchestrate` and `semantic_snapshot_digest` helpers.
# Union re-pin (host, Unit 14B joins the composed tree with Unit 17's
# placement seal): both sides above measured trees missing the other's
# change; the value below is measured on THIS tree, twice, independent
# roots, rid "r", via this module's own helpers.
# Unit 19B Sonnet audit round 2: same `page_id` removal as the happy digest
# above. Review's own recovery/audit chains lose the same one key per act;
# no file count or exit change (106 files, exit 3). Measured twice in
# independent temporary roots at canonical run id "r" through this module's
# own `orchestrate` and `semantic_snapshot_digest` helpers.
# Unit 19B build round 3: same combined cross-capture wiring as the happy
# digest above (see that comment for the mechanism). Review's own recovery
# and audit chains gain the same `logical_act_id`/`cross_capture_autopsia`/
# `autopsia` fields, and the run gains the same one new partition blob; exit
# stays 3 and file count moves 106 -> 107. Measured twice in independent
# temporary roots at canonical run id "r" through this module's own
# `orchestrate` and `semantic_snapshot_digest` helpers.
# Unit 19B Opus audit: same `witness_covered` removal from the unprimed
# dossiers as the happy digest above. Review publishes a lectio-prior for
# every act it reads, including the recovered ones, so the same one key leaves
# each of them; file count stays 107 and the exit stays 3. Measured twice in
# independent temporary roots at canonical run id "r" through this module's
# own `orchestrate` and `semantic_snapshot_digest` helpers.
# Unit 19B Sol formal review: same partition-input and canonical page-set
# corrections as happy above. Review's recovery attempts acquire the same
# immutable partition input and its continuation audit uses the same canonical
# denominator. File count remains 107 and exit remains 3. Measured twice in
# independent temporary roots at canonical run id "r" through this module's
# own helpers.
# The review tree records the same field and instrument-absence state without
# changing its one ink-confirmed recrop: 107 files, exit 3. This digest was
# reproduced twice under the same canonical measurement as the happy pin.
# Re-pinned at this merge (19A+19B+19C onto the composed pr tree): each
# scenario gains exactly the one sealed run-partition blob 19B declared
# (95 -> 96 happy, 106 -> 107 review), every review carries 19C's
# cross_capture_coverage field, and audit chains use the canonical page set.
# Measured twice at two independent run roots through this module's own
# helpers at canonical run id "r".
# Cascade re-pin (pr/11 merged into pr/12): both re-pins above are in this tree
# at once. pr/12's ink-confirmation gate still removes review's second recovery
# round (118 -> 106) and pr/11's flag-location basis still carries the span it
# accounts for, so review's draft bytes moved again without adding or removing
# an artifact. Counts are unchanged from the entries above -- happy 95/0,
# review 106/3 -- and both digests were re-measured on the merged tree, twice in
# independent temporary roots through this module's own `orchestrate` and
# `semantic_snapshot_digest` at canonical run id "r".
#
# Re-pinned at the GitHub review of PR #74: a page Testimonium binds every
# retained response it derived from in its envelope `inputs`, not only a Churro
# `native_capture`. `RunTree.read_artifact` verifies `inputs` and nothing else,
# so a Chandra partition's `raw_response_refs` were bytes no ordinary consumer
# re-hashed, although the same envelope already bound the Churro capture that
# way; the Recensor applies its existing capture rule to them too. Reference
# fields only -- each page record that names retained responses gains them as
# inputs. No new blob or artifact file, but the EXISTING page-record artifacts'
# bytes change (their inputs list grows), which is exactly why both digests
# below moved. No exit-code change: counts hold at 90/0 and
# 113/3, and both digests below reproduced twice in independent temporary roots
# through this module's own `orchestrate` and `semantic_snapshot_digest`
# helpers at canonical run id "r".
#
# Re-pinned at this merge (pr/09's landed tip onto the Ink Map tree): the entry
# above was measured on pr/09's own tree, where the Ink Map does not exist, so
# its 90/113 are that tree's counts and not this one's. The two changes are
# independent and both apply here -- the Ink Map's five artifacts per tree, and
# the page records whose `inputs` now bind every retained response they derived
# from -- so the counts are the Ink Map's 95/118 while the digests move again
# for the inputs binding. Neither side's literals could be carried across: both
# were measured on a tree missing the other's change. Exits hold at 0 and 3, and
# both digests below were measured twice in independent temporary roots through
# this module's own `orchestrate` and `semantic_snapshot_digest` helpers at
# canonical run id "r".
# Re-pin at the second pr/11 cascade: both sides of this merge had moved these
# literals, so neither was adopted. pr/12's ink-confirmation gate still holds
# review at 106 files and pr/11's later work moves draft bytes without adding or
# removing an artifact, so happy stays 95/0 and review 106/3, and both digests
# were re-measured on the merged tree, twice in independent temporary roots
# through this module's own `orchestrate` and `semantic_snapshot_digest` at
# canonical run id "r".
#
# Reading this log: it is chronological and append-only, and every entry states
# the counts and the fixture shape as they stood when that entry was written.
# Only the last entry describes the tree now; an earlier entry naming a
# different count, or a different number of declared fixture rows, is the
# measurement it superseded and not a competing claim about today. The four
# literals below are the authority, and each re-pin says what moved them.
# Cascade re-pin (pr/12's tip a8ec9b51c3 merged into pr/13): every re-pin above
# is in this tree at once. pr/13's sealed run-partition blob and cross-capture
# fields are present alongside pr/12's ink-confirmation gate and residue work,
# so neither side's literals below describe this tree -- both were measured on a
# tree missing the other's change. The four literals are re-measured here on the
# merged tree, twice in independent temporary roots through this module's own
# `orchestrate` and `semantic_snapshot_digest` at canonical run id "r".
#
# Reconciling the two happy baselines above, because they do not agree and a
# reader cannot otherwise tell an expected count change from a dropped act.
# This branch's own entries climbed to happy 98 (97 -> 98 for the partition
# blob, then 98 held). The incoming branch's entries stand at happy 95, because
# its ink-confirmation gate removed the recovery round this branch still had.
# Neither number describes the merged tree. Measured here: happy is 96, which is
# the incoming 95 plus exactly the one sealed run-partition blob this branch
# adds -- the same +1 the review side shows, 106 -> 107. So the two files
# between 98 and 96 were removed by the incoming ink gate, not lost here, and
# every entry above naming 97 or 98 is superseded rather than contradicted.
#
# Cascade re-pin (pr/12's final tip aa2b6d4124 merged into pr/13, after pr/12's
# GitHub review loops): the counts held at 96/107 — pr/12's review-round edits
# added and removed no snapshot files — but both digests moved, because those
# edits changed sealed bytes. Re-measured on this merged tree, twice, in two
# independent temporary roots through this module's own `orchestrate` and
# `semantic_snapshot_digest` at canonical run id "r"; happy exited 0 and review
# exited 3, and both roots agreed exactly.
# Unit 2 re-pin: see the happy entry above. `decoding.toml` changes this
# run's sealed `config_digest`, not its artifact count or exit: review remains
# 106 files / exit 3. Measured twice in independent roots, rid "r", through
# this module's own helpers.
# Re-pinned at this merge (2 onto the composed pr tree): `config/decoding.toml`
# joins every run's sealed config_digest, moving both digests while adding no
# artifact -- counts hold at 96/0 and 107/3. Measured twice at two independent
# run roots through this module's own helpers at canonical run id "r".

# Cascade re-pin (pr/13's tip 41910e592f merged into pr/14): every entry above is
# in this tree at once. This branch adds `config/decoding.toml` to the sealed
# `config_digest` and the incoming chain adds its inputs-binding checks, the
# hold-cause gate and denominator, the iterative preference screens, the
# merged-grid continuation fix and the coverage anti-picker screen. None of that
# adds or removes an artifact, so the counts hold at happy 96 / exit 0 and
# review 107 / exit 3, and neither side's digest literals describe this tree --
# both were measured before the other's change existed. The two literals below
# were re-measured here on the merged tree, twice in independent temporary roots
# through this module's own `orchestrate` and `semantic_snapshot_digest` at
# canonical run id "r".
#
# Cascade re-pin (pr/13's final tip b444ecce20 merged into pr/14, after pr/13's
# three GitHub review rounds): counts hold at 96/107 — neither side's review
# edits added or removed a snapshot file — and both digests move again, because
# this branch's `decoding.toml` seal and pr/13's sealed-byte edits each change
# the trees the other side measured. Re-measured on this merged tree, twice, in
# two independent temporary roots through this module's own `orchestrate` and
# `semantic_snapshot_digest` at canonical run id "r"; happy exited 0 and review
# exited 3, and both roots agreed exactly.
HAPPY_SNAPSHOT_FILES = 96
REVIEW_SNAPSHOT_FILES = 107
HAPPY_RUN_TREE_DIGEST = "0db78cb02752dba8eb2cb205913ccfd6af71cbe35b689e3d57c400538754f4b2"
REVIEW_RUN_TREE_DIGEST = "b5e2a1757bbd3915a09c607855f7c1a1958d49e9da81de1f4d2a78fb16d3976d"


def orchestrate(
    run_root: Path,
    run_id: str,
    scenario: str,
    *,
    models_config: Path | None = None,
    serving_recipes_config: Path | None = None,
    recovery_config: Path | None = None,
    hard_failure_config: Path | None = None,
    nuda_per_mille: int | None = None,
    nuda_approval_ref: str | None = None,
    submission_folder: Path | None = None,
    submission_manifest: Path | None = None,
    data_gate_policy: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run the pipeline the way a person would, and return the whole result."""
    if nuda_per_mille and nuda_approval_ref == NUDA_APPROVAL_SUBJECT:
        bindings = run_config_bindings(
            load_models_toml(ROOT / "config" / "models.toml"),
            load_fixture(str(ROOT / "proof")),
            scenario,
            nuda_per_mille=nuda_per_mille,
            nuda_approval_ref=nuda_approval_ref,
        )
        RunTree(run_root, run_id).write_approval_record(
            build_approval_record(
                subject_ids=[NUDA_APPROVAL_SUBJECT],
                action="other",
                reason="test-only Lectio nuda sampling design",
                target_version_hash=bindings["config_digest"],
                timestamp="2026-08-21T00:00:00Z",
            )
        )
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
    if serving_recipes_config is not None:
        command.extend(("--serving-recipes-config", str(serving_recipes_config)))
    if recovery_config is not None:
        command.extend(("--recovery-config", str(recovery_config)))
    if hard_failure_config is not None:
        command.extend(("--hard-failure-config", str(hard_failure_config)))
    if nuda_per_mille is not None:
        command.extend(("--nuda-per-mille", str(nuda_per_mille)))
    if nuda_approval_ref is not None:
        command.extend(("--nuda-approval-ref", nuda_approval_ref))
    if submission_folder is not None:
        command.extend(("--submission-folder", str(submission_folder)))
    if submission_manifest is not None:
        command.extend(("--submission-manifest", str(submission_manifest)))
    if data_gate_policy is not None:
        command.extend(("--data-gate-policy", str(data_gate_policy)))
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _real_submission(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    approved = tmp_path / "approved-storage"
    source = approved / "submitted-pages"
    source.mkdir(parents=True)
    for name in ("page-1.png", "page-2.png"):
        shutil.copyfile(ROOT / "proof" / "fixtures" / FIXTURE / name, source / name)
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(approved)]
    policy_path = tmp_path / "data-gate-policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    manifest = approved / "submission-ledger.json"
    submit.submit(source, manifest, policy_path=policy_path)
    return approved, source, manifest, policy_path


def test_orchestrator_carries_a_real_submission_to_the_door_end_to_end(tmp_path):
    approved, source, manifest, policy = _real_submission(tmp_path)
    result = orchestrate(
        approved / "runs",
        "real-ingress",
        "happy",
        submission_folder=source,
        submission_manifest=manifest,
        data_gate_policy=policy,
    )

    # This unit ends at the Door; real Designator work remains an explicit refusal.
    assert result.returncode != 0
    assert "real structural proposal/model work is outside System 03" in result.stderr
    run_record = RunTree(approved / "runs", "real-ingress").read_run()
    assert run_record["ingress"] == {"mode": "real"}

    # Only the sealed digest proves the gate evaluated the caller's policy.
    assert run_sealed_config_digests(run_record)["data-handling"] == digest_bytes(
        policy.read_bytes()
    )
    assert digest_bytes(policy.read_bytes()) != digest_bytes(
        gate.DEFAULT_POLICY_PATH.read_bytes()
    ), "the caller's policy must differ from the default, or the check above proves nothing"


def test_real_designator_refuses_a_missing_ink_map_boundary(tmp_path):
    """Real ingress must not bypass the producer inserted immediately before it."""
    approved, source, manifest, policy = _real_submission(tmp_path)
    root = approved / "runs"
    first = orchestrate(
        root,
        "real-ink-map-boundary",
        "happy",
        submission_folder=source,
        submission_manifest=manifest,
        data_gate_policy=policy,
    )
    assert "real structural proposal/model work is outside System 03" in first.stderr

    tree = RunTree(root, "real-ink-map-boundary")
    _stage_seal_path(tree, INK_MAP).unlink()
    before = snapshot(tree.root)

    result = invoke_stage(
        root,
        "real-ink-map-boundary",
        "happy",
        "pipeline/2_designator/run.py",
    )

    assert result.returncode == EXIT_FATAL
    assert "predecessor ink-map has no stage-seal" in result.stderr
    assert "never re-derived" in result.stderr
    assert snapshot(tree.root) == before


def test_orchestrator_preserves_the_manifest_without_folder_refusal(tmp_path):
    approved, _source, manifest, policy = _real_submission(tmp_path)
    result = orchestrate(
        approved / "runs",
        "manifest-without-folder",
        "happy",
        submission_manifest=manifest,
        data_gate_policy=policy,
    )

    assert result.returncode != 0
    assert (
        "submission filename ledger is meaningful only with a real submission folder"
        in result.stderr
    )


def test_orchestrator_refuses_a_data_gate_policy_without_a_real_folder(tmp_path):
    result = orchestrate(
        tmp_path / "runs",
        "policy-without-folder",
        "happy",
        data_gate_policy=tmp_path / "policy-that-must-not-be-ignored.json",
    )

    assert result.returncode != 0
    assert "--data-gate-policy is meaningful only with --submission-folder" in result.stderr
    assert not (tmp_path / "runs" / "policy-without-folder" / "run.json").exists()


REAL_INGRESS_FLAGS = frozenset(
    {"--submission-folder", "--submission-manifest", "--data-gate-policy"}
)


def _orchestrator_module(name: str):
    path = ROOT / "pipeline" / "orchestrator" / "run.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _orchestrator_namespace_fields(tmp_path: Path) -> dict:
    return dict(
        run_root=tmp_path / "runs",
        run_id="r",
        scenario="happy",
        fixture_root=ROOT / "proof",
        models_config=ROOT / "config" / "models.toml",
        # The constant, not a second spelling of the path. This stand-in feeds
        # mocked subprocess tests, so a catalogue that moved with only
        # `DEFAULT_SERVING_RECIPES_CONFIG_PATH` updated would leave them passing
        # while handing every stage a path that is not there. The neighbouring
        # literals predate this branch and are left as they are.
        serving_recipes_config=DEFAULT_SERVING_RECIPES_CONFIG_PATH,
        pdf_render_config=ROOT / "config" / "pdf_render.toml",
        designator_padding_config=ROOT / "config" / "designator_padding.toml",
        designator_geometry_config=ROOT / "config" / "designator_geometry.toml",
        alignment_config=ROOT / "config" / "alignment.toml",
        formats_config=ROOT / "config" / "armarium_formats.toml",
        recovery_config=ROOT / "config" / "recovery.toml",
        hard_failure_config=ROOT / "config" / "hard_failure.toml",
        pdf_target_dpi=None,
        witness_context="named",
        witness_context_config=ROOT / "config" / "witness_context.toml",
        nuda_per_mille=0,
        nuda_approval_ref="",
        perlector_instrument_per_mille=0,
        perlector_instrument_approval_ref="",
        perlector_protocol_config=ROOT / "config" / "perlector_protocol.toml",
        perlector_audit_config=ROOT / "config" / "perlector_audit.toml",
        draft_fed=True,
        # The corpus-register argv surface, which `invoke` reads by name on every
        # stage. A stand-in that omits it is not the surface it claims to mirror.
        corpus_register=None,
        submission_folder=None,
        submission_manifest=None,
        data_gate_policy=None,
    )


def test_every_stage_receives_the_runs_selected_serving_recipes_catalogue(monkeypatch, tmp_path):
    """The roster's other half has to travel with it, to every child.

    `--models-config` selects which chairs exist; `--serving-recipes-config`
    selects the vLLM profile each one is served under. Both are sealed into
    `config_digest` (`common/stage.py::run_config_bindings`), so a stage left on
    the fixture-only default while its siblings were handed the real catalogue
    refuses the whole run for a reason that has nothing to do with the corpus.
    Unit 17 added the flag to `stage_parser` alone, which made the real
    catalogue unreachable through the only program that invokes the stages.
    """

    orchestrator = _orchestrator_module("orchestrator_serving_recipes_argv")
    observed: list[list[str]] = []
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        lambda command, **_kwargs: (
            observed.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    selected = ROOT / "config" / "serving_recipes_real.toml"
    args = Namespace(
        **{**_orchestrator_namespace_fields(tmp_path), "serving_recipes_config": selected}
    )

    programs = [program for _name, program in orchestrator.SEQUENCE if program is not None]
    for program in programs:
        orchestrator.invoke(program, args)

    assert len(observed) == len(programs) and programs, "no stage was invoked"
    for command in observed:
        assert "--serving-recipes-config" in command, (
            f"{Path(command[1]).name} was invoked without the run's serving catalogue and "
            "would seal the fixture-only default instead"
        )
        assert command[command.index("--serving-recipes-config") + 1] == str(selected)


def test_real_roster_and_catalogue_reach_the_real_orchestrator_route(tmp_path):
    """The actual subprocess route seals the selected real pair, not the defaults.

    Model materialization is deliberately still red: all-zero manifest digests
    are pre-materialization sentinels. Reaching that named refusal proves the
    real roster passed its native-adapter boundary and that the Door sealed the
    caller-selected catalogue before the Designator tried to resolve a model.
    Catalogue row completeness and unproven state are checked against these same
    literal files in ``operations/serving/test_manager.py``.
    """

    models = ROOT / "config" / "models-real.toml"
    recipes = ROOT / "config" / "serving_recipes_real.toml"
    run_root = tmp_path / "runs"

    result = orchestrate(
        run_root,
        "r",
        "happy",
        models_config=models,
        serving_recipes_config=recipes,
    )

    assert result.returncode == 2
    assert "all-zero pre-materialization sentinel" in result.stderr
    assert "has no witness_adapter" not in result.stderr
    run_record = json.loads((run_root / "r" / "run.json").read_text(encoding="utf-8"))
    expected = run_config_bindings(
        load_models_toml(models),
        load_fixture(ROOT / "proof"),
        "happy",
        serving_recipes_config_path=recipes,
    )
    assert run_record["config_digest"] == expected["config_digest"]
    assert expected["serving_config_inputs"]["serving_recipes_sha256"] == digest_bytes(
        recipes.read_bytes()
    )


def test_real_ingress_changes_only_the_doors_argv(monkeypatch, tmp_path):
    """No stage after the Door receives a second path to source material."""

    orchestrator = _orchestrator_module("orchestrator_real_ingress_argv")
    observed: list[list[str]] = []

    def record(command, **_kwargs):  # type: ignore[no-untyped-def]
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(orchestrator.subprocess, "run", record)
    base = _orchestrator_namespace_fields(tmp_path)
    fixture_args = orchestrator.resolve_caller_paths(Namespace(**base))
    real_args = orchestrator.resolve_caller_paths(
        Namespace(
            **{
                **base,
                "submission_folder": tmp_path / "approved" / "source",
                "submission_manifest": tmp_path / "approved" / "ledger.json",
            }
        )
    )

    # STAGE_PROGRAMS, not SEQUENCE: work/staged-run-modes gave the sequence a
    # ("recovery", None) member, which is a driver step rather than a stage
    # program and has no argv to compare. STAGE_PROGRAMS is the invocable subset
    # in the same order, so the Door is still first and the slice below still
    # means "every stage after the Door".
    for program in orchestrator.STAGE_PROGRAMS.values():
        orchestrator.invoke(program, fixture_args)
    fixture_commands = observed[:]
    observed.clear()
    for program in orchestrator.STAGE_PROGRAMS.values():
        orchestrator.invoke(program, real_args)

    assert observed[1:] == fixture_commands[1:]
    assert "--submission-folder" in observed[0]
    assert "--submission-manifest" in observed[0]
    assert "--data-gate-policy" in observed[0]
    assert not REAL_INGRESS_FLAGS.intersection(fixture_commands[0])

    # Relative equality stays green if both routes leak, so also prohibit flags absolutely.
    for commands in (observed, fixture_commands):
        for command in commands[1:]:
            leaked = REAL_INGRESS_FLAGS.intersection(command)
            assert not leaked, (
                f"{Path(command[1]).name} received {sorted(leaked)}; every stage after the "
                "Door works from the run tree the Door sealed, and a source path on its argv "
                "is a second, unsealed route back to the submitted material"
            )


def test_orchestrator_stage_children_do_not_receive_upload_only_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    orchestrator = _orchestrator_module("orchestrator_stage_environment")
    observed_environment: dict[str, str] = {}
    monkeypatch.setenv("RUNPOD_S3_ACCESS_KEY", "upload-access-secret")
    monkeypatch.setenv("RUNPOD_S3_SECRET_KEY", "upload-secret-secret")
    monkeypatch.setenv("VERBATUS_STAGE_TEST_SENTINEL", "preserved")

    def record(command, **kwargs):  # type: ignore[no-untyped-def]
        observed_environment.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(orchestrator.subprocess, "run", record)
    args = orchestrator.resolve_caller_paths(Namespace(**_orchestrator_namespace_fields(tmp_path)))

    orchestrator.invoke(orchestrator.STAGE_PROGRAMS["door"], args)

    assert "RUNPOD_S3_ACCESS_KEY" not in observed_environment
    assert "RUNPOD_S3_SECRET_KEY" not in observed_environment
    assert observed_environment["VERBATUS_STAGE_TEST_SENTINEL"] == "preserved"


def test_invoke_refuses_a_caller_relative_path_instead_of_resolving_it_late(monkeypatch, tmp_path):
    """Direct invocation must not reinterpret caller paths under the child's cwd."""

    orchestrator = _orchestrator_module("orchestrator_relative_argv_guard")
    invoked: list[list[str]] = []
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        lambda command, **_kwargs: (
            invoked.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    base = _orchestrator_namespace_fields(tmp_path)

    for attribute, flag, value in (
        ("run_root", "--run-root", Path("runs")),
        ("submission_folder", "--submission-folder", Path("approved/source")),
        ("submission_manifest", "--submission-manifest", Path("approved/ledger.json")),
        ("data_gate_policy", "--data-gate-policy", Path("policy.json")),
    ):
        overrides = {attribute: value}
        if attribute in {"submission_manifest", "data_gate_policy"}:
            overrides["submission_folder"] = tmp_path / "approved" / "source"
        args = Namespace(**{**base, **overrides})
        with pytest.raises(ContractError) as refusal:
            orchestrator.invoke(orchestrator.STAGE_PROGRAMS["door"], args)
        assert flag in str(refusal.value)
    assert not invoked, "a stage was launched with a caller-relative path on its argv"

    orchestrator.invoke(
        orchestrator.STAGE_PROGRAMS["door"],
        orchestrator.resolve_caller_paths(Namespace(**{**base, "run_root": Path("runs")})),
    )
    assert invoked


def test_orchestrator_default_data_gate_policy_is_the_gates_own(tmp_path):
    """The common-only import boundary requires duplicate constants to reconcile."""

    orchestrator = _orchestrator_module("orchestrator_default_policy")
    assert orchestrator.DEFAULT_DATA_GATE_POLICY_PATH == gate.DEFAULT_POLICY_PATH
    resolved = orchestrator.resolve_caller_paths(
        Namespace(
            **{
                **_orchestrator_namespace_fields(tmp_path),
                "submission_folder": tmp_path / "approved" / "source",
                "data_gate_policy": None,
            }
        )
    )
    assert resolved.data_gate_policy == gate.DEFAULT_POLICY_PATH
    assert resolved.data_gate_policy.is_file()


def test_orchestrator_upload_credentials_are_the_transfers_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both stripping helpers drop the transfer's own names and keep the rest.

    Both this orchestrator and the operator surface strip the upload-only
    credentials from a stage's environment, and the import boundary keeps this
    one a duplicate. Without this reconciliation a third credential added to the
    transfer would go on reaching stages here while the whole suite stayed
    green -- a live secret in the environment of the process that decodes
    caller-supplied material.

    Comparing the three sets is not enough on its own: three constants can agree
    perfectly while a helper has stopped consulting its own. So each helper is
    run against an environment holding every name, and what it returns is the
    evidence.
    """

    orchestrator = _orchestrator_module("orchestrator_transfer_credentials")
    assert orchestrator._TRANSFER_CREDENTIAL_ENV == volume_s3.TRANSFER_CREDENTIAL_ENV
    assert surface._TRANSFER_CREDENTIAL_ENV == volume_s3.TRANSFER_CREDENTIAL_ENV
    # The names are the transfer's own defaults, not a set that merely happens to
    # match them today.
    spec = volume_s3.VolumeSpec(datacenter_id="EU-CZ-1", volume_id="volume")
    assert {spec.access_key_env, spec.secret_key_env} == set(volume_s3.TRANSFER_CREDENTIAL_ENV)

    for name in volume_s3.TRANSFER_CREDENTIAL_ENV:
        monkeypatch.setenv(name, f"upload-secret-for-{name}")
    monkeypatch.setenv("VERBATUS_STAGE_TEST_SENTINEL", "preserved")

    for label, built in (
        ("orchestrator", orchestrator.stage_environment()),
        ("operator surface", surface._stage_environment()),
    ):
        leaked = volume_s3.TRANSFER_CREDENTIAL_ENV.intersection(built)
        assert not leaked, f"{label} passed {sorted(leaked)} to a stage"
        # The stripper must remove those names and nothing else: an
        # implementation that returned an empty environment, or one that dropped
        # everything it did not recognise, would satisfy the assertion above
        # while breaking every stage that reads its own settings.
        assert built["VERBATUS_STAGE_TEST_SENTINEL"] == "preserved", label


def test_resuming_a_real_run_without_its_ingress_flags_refuses(tmp_path):
    """A fixture route may not take over a run tree sealed as real ingress."""

    approved, source, manifest, policy = _real_submission(tmp_path)
    first = orchestrate(
        approved / "runs",
        "seam",
        "happy",
        submission_folder=source,
        submission_manifest=manifest,
        data_gate_policy=policy,
    )
    assert "real structural proposal/model work is outside System 03" in first.stderr
    sealed = RunTree(approved / "runs", "seam").read_run()

    resumed = orchestrate(approved / "runs", "seam", "happy")

    assert resumed.returncode != 0
    assert "already exists and is bound to different" in resumed.stderr
    assert "ingress" in resumed.stderr
    assert RunTree(approved / "runs", "seam").read_run() == sealed


def test_relative_run_root_from_outside_the_repository_is_one_tree(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        FIXTURE,
        "--scenario",
        "happy",
        "--run-id",
        "outside",
        "--run-root",
        "runs",
        "--fixture-root",
        str(ROOT / "proof"),
    ]

    result = subprocess.run(command, cwd=outside, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert (outside / "runs" / "outside" / "run.json").is_file()
    assert not (ROOT / "runs" / "outside").exists()


def test_relative_submission_folder_from_outside_the_repository_finds_the_real_files(
    tmp_path: Path,
) -> None:
    """Real-ingress paths bind before child processes change cwd to the repository."""
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "approved-storage" / "submitted-pages"
    source.mkdir(parents=True)
    for name in ("page-1.png", "page-2.png"):
        shutil.copyfile(ROOT / "proof" / "fixtures" / FIXTURE / name, source / name)
    policy = json.loads(gate.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["storage_roots"] = [str(outside / "approved-storage")]
    policy_path = outside / "data-gate-policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    manifest = outside / "approved-storage" / "submission-ledger.json"
    submit.submit(source, manifest, policy_path=policy_path)

    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        FIXTURE,
        "--scenario",
        "happy",
        "--run-id",
        "relative-real-ingress",
        "--run-root",
        "approved-storage/runs",
        "--submission-folder",
        "approved-storage/submitted-pages",
        "--submission-manifest",
        "approved-storage/submission-ledger.json",
        "--data-gate-policy",
        "data-gate-policy.json",
    ]

    result = subprocess.run(command, cwd=outside, capture_output=True, text=True)

    # A real route must not require its deliberately unused fixture-root default.
    assert "could not be resolved" not in result.stderr
    assert "outside every approved storage root" not in result.stderr
    run_tree_root = outside / "approved-storage" / "runs"
    assert RunTree(run_tree_root, "relative-real-ingress").read_run()["ingress"] == {"mode": "real"}
    assert not (ROOT / "runs" / "relative-real-ingress").exists()


def test_orchestrator_preserves_a_submitted_folder_symlink_for_the_doors_gate(tmp_path: Path):
    """Making caller paths absolute must not silently dereference real ingress."""

    approved, source, manifest, policy = _real_submission(tmp_path)
    submitted_link = approved / "submitted-link"
    submitted_link.symlink_to(source, target_is_directory=True)

    result = orchestrate(
        approved / "runs",
        "symlink-refusal",
        "happy",
        submission_folder=submitted_link,
        submission_manifest=manifest,
        data_gate_policy=policy,
    )

    assert result.returncode != 0
    assert "submitted folder is a symlink" in result.stderr
    assert not (approved / "runs" / "symlink-refusal" / "run.json").exists()


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
        "pipeline/1_ink_map/run.py",
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
        "pipeline/1_ink_map/run.py",
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
        "pipeline/1_ink_map/run.py",
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


def file_identities(root: Path) -> dict[str, tuple[int, int]]:
    """The device and inode of every file, which is what distinguishes reuse.

    A digest cannot tell a reused artifact from one deleted and rewritten with
    the same bytes, so a test that only compares digests proves the tree is
    right and says nothing about the claim in its own name (GOVERNANCE 10).
    Identity can tell them apart: `RunTree` publishes through a temporary that
    is then `os.link`-ed or `os.replace`-d into place, so every write lands a
    *new* inode, while both reuse paths (`_publish_bytes` on identical bytes,
    and the receipt's equal-bytes short circuit) return without touching the
    file at all. The device is carried beside the inode because inode numbers
    are only unique within a filesystem.
    """
    return {
        str(path.relative_to(root)): (path.stat().st_dev, path.stat().st_ino)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


DERIVED_INVENTORY_SUFFIXES = (
    "/manifest.json",
    "/manifest-door.json",
    "/index.json",
    "run-health/recensor-partition-receipt.json",
)


def is_immutable_evidence(path: str) -> bool:
    """Whether a path is evidence, as against a derived inventory or receipt.

    A resume legitimately rebuilds the inventories and current-state receipts
    that *name* the evidence -- appending to a tree changes what the manifest
    lists, so republishing it is the append, not a rewrite. The evidence those
    inventories name is immutable, and it is the only thing whose identity a
    resume may not disturb. `test_volume_hosted_run_tree` drew this same line
    for the same reason; it is named here so both tests draw it identically.
    """
    return not path.endswith(DERIVED_INVENTORY_SUFFIXES)


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
                or manifest.get("schema") != "armarium-export-manifest.v3"
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


def _semantic_decode_environment(data: bytes) -> bytes | None:
    """Normalize host probes while retaining the stage's semantic decode role."""
    try:
        record = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("kind") != "decode-environment":
        return None
    try:
        environment = _validate_decode_environment(record.get("payload"), "acceptance pin")
    except SchemaRefusal:
        return None
    if canonical_bytes(record) != data or record.get("self_hash") != self_hash(record):
        return None
    semantic = deepcopy(record)
    semantic["payload"] = deepcopy(environment)
    for decoder in semantic["payload"]["decoders"]:
        decoder["version"] = "platform-normalized"
    semantic["payload"]["platform"] = "platform-normalized"
    semantic["payload"]["machine"] = "platform-normalized"
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


def _semantic_envelope(data: bytes, replacements: dict[str, str]) -> bytes | None:
    """Reduce a valid ordinary envelope when it names semantic blob content."""
    try:
        record = json.loads(data)
        validate_envelope(record)
    except (SchemaRefusal, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        record["kind"] in {"decode-environment", "stage-seal"}
        or canonical_bytes(record) != data
        or record["self_hash"] != self_hash(record)
    ):
        return None
    semantic = _replace_semantic_digests(record, replacements)
    if semantic == record:
        return None
    semantic["self_hash"] = self_hash(semantic)
    return canonical_bytes(semantic)


def _semantic_stage_seal(data: bytes, replacements: dict[str, str]) -> bytes | None:
    """Reduce a witnessed stage inventory only when its complete shape is sound."""
    try:
        record = json.loads(data)
        validate_envelope(record)
    except (SchemaRefusal, UnicodeDecodeError, json.JSONDecodeError):
        return None
    payload = record.get("payload")
    if (
        record.get("kind") != "stage-seal"
        or canonical_bytes(record) != data
        or record.get("self_hash") != self_hash(record)
        or not isinstance(payload, dict)
        or set(payload)
        != {
            "stage",
            "attempt_ordinal",
            "attempt_id",
            "config_digest",
            "register_digest",
            "artifact_inventory",
            "blob_inventory",
            "census",
            "decode_environment_artifact_id",
            "decode_environment_sha256",
        }
        or payload["stage"] not in STAGES
        or record.get("stage") != payload["stage"]
        or record.get("subject_id") != payload["stage"]
        or record.get("attempt_id") != payload["attempt_id"]
        or not isinstance(payload["attempt_ordinal"], int)
        or payload["attempt_ordinal"] < 1
        or not isinstance(payload["census"], list)
        or any(
            not isinstance(row, dict)
            or set(row) != {"kind", "outcome", "count"}
            or not isinstance(row["kind"], str)
            or not isinstance(row["outcome"], str)
            or not isinstance(row["count"], int)
            or row["count"] < 1
            for row in payload["census"]
        )
        or payload["census"]
        != sorted(payload["census"], key=lambda row: (row["kind"], row["outcome"]))
        or len({(row["kind"], row["outcome"]) for row in payload["census"]})
        != len(payload["census"])
        or any(
            not isinstance(payload[field], str)
            or len(payload[field]) != 64
            or any(character not in "0123456789abcdef" for character in payload[field])
            for field in (
                "config_digest",
                "register_digest",
                "artifact_inventory",
                "blob_inventory",
                "decode_environment_sha256",
            )
        )
        or not isinstance(payload["attempt_id"], str)
        or not isinstance(payload["decode_environment_artifact_id"], str)
        or payload["artifact_inventory"] not in replacements
        or payload["blob_inventory"] not in replacements
        or payload["decode_environment_sha256"] not in replacements
    ):
        return None
    semantic = _replace_semantic_digests(record, replacements)
    semantic["payload"]["artifact_inventory"] = replacements[payload["artifact_inventory"]]
    semantic["payload"]["blob_inventory"] = replacements[payload["blob_inventory"]]
    semantic["self_hash"] = self_hash(semantic)
    return canonical_bytes(semantic)


def _semantic_stage_seal_inventory_replacements(
    files: list[tuple[Path, bytes]], replacements: dict[str, str]
) -> None:
    """Map each valid seal's raw aggregate values to its semantic inventories."""
    records_by_stage: dict[str, list[tuple[Path, bytes, dict]]] = {stage: [] for stage in STAGES}
    seals: list[tuple[Path, bytes, dict]] = []
    for path, data in files:
        try:
            record = json.loads(data)
            validate_envelope(record)
        except (SchemaRefusal, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if canonical_bytes(record) != data or record.get("self_hash") != self_hash(record):
            continue
        stage = record.get("stage")
        if stage not in records_by_stage:
            continue
        row = (path, data, record)
        records_by_stage[stage].append(row)
        if record.get("kind") == "stage-seal":
            seals.append(row)

    def matching_artifacts(stage: str, payload: dict) -> list[tuple[Path, bytes, dict]] | None:
        census = payload.get("census")
        if not isinstance(census, list):
            return None
        by_kind_outcome: dict[tuple[str, str], list[tuple[Path, bytes, dict]]] = {}
        for row in records_by_stage[stage]:
            if row[2]["kind"] in {"stage-seal", "decode-environment"}:
                continue
            key = (row[2]["kind"], row[2]["outcome"])
            by_kind_outcome.setdefault(key, []).append(row)
        choice_specs = []
        possible = 1
        for row in census:
            if not isinstance(row, dict):
                return None
            key = (row.get("kind"), row.get("outcome"))
            count = row.get("count")
            if (
                not isinstance(key[0], str)
                or not isinstance(key[1], str)
                or not isinstance(count, int)
            ):
                return None
            source = by_kind_outcome.get(key, [])
            if count < 1 or len(source) < count:
                return None
            possible *= comb(len(source), count)
            if possible > 8192:
                return None
            choice_specs.append((source, count))
        matches = []
        for groups in product(*(combinations(source, count) for source, count in choice_specs)):
            selected = [item for group in groups for item in group]
            entries = [
                {
                    "artifact_id": record["artifact_id"],
                    "kind": record["kind"],
                    "subject_id": record["subject_id"],
                    "outcome": record["outcome"],
                    "relative_path": str(path.relative_to(path.parents[3])),
                    "sha256": digest_bytes(data),
                }
                for path, data, record in selected
            ]
            entries.sort(key=lambda entry: entry["artifact_id"])
            if digest_of(entries) == payload.get("artifact_inventory"):
                matches.append(selected)
                if len(matches) > 1:
                    return None
        return matches[0] if matches else None

    def matching_blobs(stage: str, payload: dict) -> list[dict[str, str]] | None:
        stage_root = WRITING_DIRECTORIES[stage]
        prefix = f"{stage_root}/blobs/sha256/"
        candidates = []
        for path, data in files:
            relative = str(path.relative_to(path.parents[3]))
            if relative.startswith(prefix) and "/" not in relative[len(prefix) :]:
                candidates.append({"name": path.name, "sha256_of_content": digest_bytes(data)})
        matches = []
        probes = 0
        for count in range(len(candidates) + 1):
            for chosen in combinations(candidates, count):
                probes += 1
                if probes > 8192:
                    return None
                selected = sorted(chosen, key=lambda row: row["name"])
                if digest_of(selected) == payload.get("blob_inventory"):
                    matches.append(selected)
                    if len(matches) > 1:
                        return None
        return matches[0] if matches else None

    for _, _, seal in seals:
        payload = seal.get("payload")
        stage = payload.get("stage") if isinstance(payload, dict) else None
        if stage not in records_by_stage:
            continue
        selected_artifacts = matching_artifacts(stage, payload)
        blobs = matching_blobs(stage, payload)
        if selected_artifacts is None or blobs is None:
            continue
        artifacts = [
            {
                "artifact_id": record["artifact_id"],
                "kind": record["kind"],
                "subject_id": record["subject_id"],
                "outcome": record["outcome"],
                "relative_path": str(path.relative_to(path.parents[3])),
                "sha256": digest_bytes(data),
            }
            for path, data, record in selected_artifacts
        ]
        artifacts.sort(key=lambda entry: entry["artifact_id"])
        if payload.get("artifact_inventory") != digest_of(artifacts) or payload.get(
            "blob_inventory"
        ) != digest_of(blobs):
            continue
        semantic_artifacts = _replace_semantic_digests(artifacts, replacements)
        for entry, (_, data, _) in zip(
            semantic_artifacts,
            sorted(selected_artifacts, key=lambda row: row[2]["artifact_id"]),
            strict=True,
        ):
            entry["sha256"] = replacements.get(digest_bytes(data), digest_bytes(data))
        semantic_blobs = _replace_semantic_digests(blobs, replacements)
        replacements[payload["artifact_inventory"]] = digest_of(semantic_artifacts)
        replacements[payload["blob_inventory"]] = digest_of(semantic_blobs)


def _semantic_manifest(data: bytes, replacements: dict[str, str]) -> bytes | None:
    """Reduce a derived stage manifest only when it has the exact store shape."""
    try:
        manifest = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema", "run_id", "stage", "artifacts", "blobs"}
        or manifest.get("schema") != "skeleton.v1"
        or manifest.get("stage") not in WRITING_DIRECTORIES
        or not isinstance(manifest["run_id"], str)
        or not isinstance(manifest["artifacts"], list)
        or not isinstance(manifest["blobs"], list)
        or canonical_bytes(manifest) != data
    ):
        return None
    semantic = _replace_semantic_digests(manifest, replacements)
    return None if semantic == manifest else canonical_bytes(semantic)


def semantic_snapshot(root: Path) -> dict[str, str]:
    """Run-tree inventory with platform-written containers reduced to data.

    PNG blobs bind decoded pixels. The Armarium bundle binds its named member
    inventory, with ``acts.sqlite`` reduced to a deterministic schema-and-row
    dump; the package manifest and run-tree manifest bind the corresponding
    semantic digest rather than derivative container hashes. Everything else
    remains byte-bound. The ordinary ``snapshot`` stays byte-exact for all resume
    and no-write assertions.
    """
    if (root / "run.json").is_file():
        raise ValueError(
            "semantic_snapshot requires the runs root, not an individual run directory"
        )
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

    # A blob's content-addressed filename is an encoding of its container bytes.
    # Establish its pixel value first, so every record that names that component
    # can be reduced before inventories digest the record bytes.
    for _, data in files:
        if not data.startswith(PNG_SIGNATURE):
            continue
        try:
            width, height, rows = decode_grayscale_png(data)
            pixel_digest = digest_bytes(b"".join(rows))
        except ValueError:
            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                grayscale = image.convert("L")
                width, height = grayscale.size
                pixel_digest = digest_bytes(grayscale.tobytes())
        replacements[digest_bytes(data)] = digest_of(
            {"width": width, "height": height, "pixel_sha256": pixel_digest}
        )

    semantic_files = {}
    for path, data in files:
        semantic = _semantic_decode_environment(data)
        if semantic is not None:
            semantic_files[path] = semantic
            replacements[digest_bytes(data)] = digest_bytes(semantic)
    for path, data in files:
        semantic = _semantic_export_artifact(data, replacements)
        if semantic is not None:
            semantic_files[path] = semantic
            replacements[digest_bytes(data)] = digest_bytes(semantic)

    # Ordinary envelopes may bind a content-addressed blob directly. Work to a
    # fixed point because later envelopes can name an earlier envelope's digest.
    for _ in range(len(files)):
        changed = False
        for path, data in files:
            semantic = _semantic_envelope(data, replacements)
            if semantic is None:
                continue
            semantic_files[path] = semantic
            raw_digest, semantic_digest = digest_bytes(data), digest_bytes(semantic)
            if replacements.get(raw_digest) != semantic_digest:
                replacements[raw_digest] = semantic_digest
                changed = True
        if not changed:
            break

    _semantic_stage_seal_inventory_replacements(files, replacements)
    for path, data in files:
        semantic = _semantic_stage_seal(data, replacements)
        if semantic is not None:
            semantic_files[path] = semantic
            replacements[digest_bytes(data)] = digest_bytes(semantic)

    for path, data in files:
        semantic = _semantic_armarium_manifest(data, replacements)
        if semantic is None:
            semantic = _semantic_manifest(data, replacements)
        if semantic is not None:
            semantic_files[path] = semantic

    inventory = {}
    for path, data in files:
        relative = str(path.relative_to(root))
        relative = _replace_semantic_digests(relative, replacements)
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


def test_semantic_snapshot_refuses_an_individual_run_directory(tmp_path):
    """A missing run-id path prefix must not masquerade as a platform-local pin."""
    (tmp_path / "run.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="runs root, not an individual run directory"):
        semantic_snapshot_digest(tmp_path)


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


def test_semantic_snapshot_normalizes_stage_seals_over_equivalent_png_containers(
    tmp_path, monkeypatch
):
    """A witnessed raw-container inventory must not make an OS-local pin.

    This is deliberately a complete miniature stage: the page envelope names a
    content-addressed PNG, the completion seal inventories both that blob and
    the page envelope, and the derived manifest names the seal.  It would pass
    the older PNG-only reducer while still producing different tree digests.
    """
    import common.imaging as imaging

    rows = [bytearray([0, 127, 255]), bytearray([255, 127, 0])]

    def write_tree(root: Path) -> None:
        run = root / "r"
        png = imaging.encode_grayscale_png(3, 2, rows)
        png_digest = digest_bytes(png)
        blob_path = run / "1_exemplar" / "blobs" / "sha256" / png_digest
        blob_path.parent.mkdir(parents=True)
        blob_path.write_bytes(png)

        page = build_envelope(
            run_id="r",
            artifact_id=artifact_id(EXEMPLAR, "page", "page-1"),
            subject_id="page-1",
            stage=EXEMPLAR,
            kind="page",
            outcome="sealed",
            config_digest="a" * 64,
            adapter_revision="fixture-exemplar-v0",
            inputs=[],
            payload={
                "image_path": f"1_exemplar/blobs/sha256/{png_digest}",
                "sha256": png_digest,
            },
        )
        page_path = run / "1_exemplar" / "artifacts" / "page" / f"{page['artifact_id']}.json"
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_data = canonical_bytes(page)
        page_path.write_bytes(page_data)

        seal_attempt = attempt_id(EXEMPLAR, "seal", 1)
        environment = build_envelope(
            run_id="r",
            artifact_id=artifact_id(EXEMPLAR, "decode-environment", EXEMPLAR, seal_attempt),
            subject_id=EXEMPLAR,
            stage=EXEMPLAR,
            kind="decode-environment",
            outcome="recorded",
            config_digest="a" * 64,
            adapter_revision="fixture-exemplar-v0",
            inputs=[],
            attempt=seal_attempt,
            payload=_decode_environment(EXEMPLAR),
        )
        environment_path = (
            run
            / "1_exemplar"
            / "artifacts"
            / "decode-environment"
            / f"{environment['artifact_id']}.json"
        )
        environment_path.parent.mkdir(parents=True, exist_ok=True)
        environment_data = canonical_bytes(environment)
        environment_path.write_bytes(environment_data)

        entries = [
            {
                "artifact_id": page["artifact_id"],
                "kind": page["kind"],
                "subject_id": page["subject_id"],
                "outcome": page["outcome"],
                "relative_path": str(page_path.relative_to(run)),
                "sha256": digest_bytes(page_data),
            }
        ]
        payload = {
            "stage": EXEMPLAR,
            "attempt_ordinal": 1,
            "attempt_id": seal_attempt,
            "config_digest": "a" * 64,
            "register_digest": "b" * 64,
            "artifact_inventory": digest_of(entries),
            "blob_inventory": digest_of([{"name": png_digest, "sha256_of_content": png_digest}]),
            "census": [{"kind": "page", "outcome": "sealed", "count": 1}],
            "decode_environment_artifact_id": environment["artifact_id"],
            "decode_environment_sha256": digest_bytes(environment_data),
        }
        seal = build_envelope(
            run_id="r",
            artifact_id=artifact_id(EXEMPLAR, "stage-seal", EXEMPLAR, seal_attempt),
            subject_id=EXEMPLAR,
            stage=EXEMPLAR,
            kind="stage-seal",
            outcome="sealed",
            config_digest="a" * 64,
            adapter_revision="fixture-exemplar-v0",
            inputs=[],
            attempt=seal_attempt,
            payload=payload,
        )
        seal_path = run / "1_exemplar" / "artifacts" / "stage-seal" / f"{seal['artifact_id']}.json"
        seal_path.parent.mkdir(parents=True, exist_ok=True)
        seal_data = canonical_bytes(seal)
        seal_path.write_bytes(seal_data)

        manifest_entries = entries + [
            {
                "artifact_id": record["artifact_id"],
                "kind": record["kind"],
                "subject_id": record["subject_id"],
                "outcome": record["outcome"],
                "relative_path": str(path.relative_to(run)),
                "sha256": digest_bytes(data),
            }
            for record, path, data in (
                (environment, environment_path, environment_data),
                (seal, seal_path, seal_data),
            )
        ]
        manifest = {
            "schema": "skeleton.v1",
            "run_id": "r",
            "stage": EXEMPLAR,
            "artifacts": sorted(manifest_entries, key=lambda entry: entry["artifact_id"]),
            "blobs": [png_digest],
        }
        (run / "1_exemplar" / "manifest.json").write_bytes(canonical_bytes(manifest))

    first = tmp_path / "first"
    second = tmp_path / "second"
    write_tree(first)
    compress = imaging.zlib.compress
    monkeypatch.setattr(
        imaging.zlib,
        "compress",
        lambda data, *args, **kwargs: compress(data, level=0),
    )
    write_tree(second)

    assert snapshot(first) != snapshot(second)
    assert semantic_snapshot_digest(first) == semantic_snapshot_digest(second)


def test_semantic_snapshot_normalizes_monkeypatched_decode_environment_probe(tmp_path, monkeypatch):
    """Decoder probes remain auditable records without becoming host-specific pins."""
    import common.stage as stage_common

    path = tmp_path / "decode-environment.json"
    payload = stage_common._decode_environment(DOOR)

    def write_environment(value):
        record = build_envelope(
            run_id="environment-test",
            artifact_id=artifact_id(DOOR, "decode-environment", DOOR, "att_1234567890abcdef"),
            subject_id=DOOR,
            stage=DOOR,
            kind="decode-environment",
            outcome="recorded",
            config_digest="a" * 64,
            adapter_revision="fixture-door-v0",
            inputs=[],
            attempt="att_1234567890abcdef",
            payload=value,
        )
        path.write_bytes(canonical_bytes(record))

    write_environment(payload)
    raw_before = snapshot(tmp_path)
    semantic_before = semantic_snapshot_digest(tmp_path)
    changed = deepcopy(payload)
    changed["decoders"][0]["version"] = "monkeypatched-decoder-version"
    monkeypatch.setattr(stage_common, "_decode_environment", lambda _: changed)
    write_environment(stage_common._decode_environment(DOOR))

    assert snapshot(tmp_path) != raw_before
    assert semantic_snapshot_digest(tmp_path) == semantic_before


def test_semantic_snapshot_keeps_decode_role_fields_in_the_acceptance_pin(tmp_path):
    """Host versions vary; whether a stage decoded or produced pixels does not."""
    path = tmp_path / "decode-environment.json"
    payload = _decode_environment(DOOR)

    def write_environment(value):
        record = build_envelope(
            run_id="environment-role-test",
            artifact_id=artifact_id(DOOR, "decode-environment", DOOR, "att_1234567890abcdef"),
            subject_id=DOOR,
            stage=DOOR,
            kind="decode-environment",
            outcome="recorded",
            config_digest="a" * 64,
            adapter_revision="fixture-door-v0",
            inputs=[],
            attempt="att_1234567890abcdef",
            payload=value,
        )
        path.write_bytes(canonical_bytes(record))

    write_environment(payload)
    semantic_before = semantic_snapshot_digest(tmp_path)
    changed = deepcopy(payload)
    changed["produced_pixels"] = not changed["produced_pixels"]
    write_environment(changed)

    assert semantic_snapshot_digest(tmp_path) != semantic_before


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
        "schema": "armarium-export-manifest.v3",
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
def continuation_recovery_run(tmp_path_factory):
    """Recrop a cross-page act without re-entering the Attestatores."""
    root = tmp_path_factory.mktemp("continuation-recovery")
    result = orchestrate(root, "r", "continuation-recovery")
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
    for stage in (
        DOOR,
        EXEMPLAR,
        INK_MAP,
        DESIGNATOR,
        ATTESTATORES,
        PERLECTOR,
        RECENSOR,
        ARCHETYPUS,
    ):
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
    # Restored on the phase-2 review. These five assertions were deleted by the
    # R6 pin re-measurement, whose message named only the two digests; they still
    # pass, and under the wave's raw-span contract the zero-length `witness_span`
    # for a genuinely-empty page witness is exactly what keeps this chair's blank
    # from reading as lost page coverage.
    attachment_record = next(
        tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "act-attachment" and entry["subject_id"] == empty["subject_id"]
    )
    empty_attachment = next(
        row
        for row in attachment_record["payload"]["attachments"]
        if row["chair"] == empty["payload"]["chair"]
    )
    assert empty_attachment["attached"] is True
    assert empty_attachment["span"] == {"start": 0, "end": 0}

    empty_dissent = next(
        row for row in reading["payload"]["dissent"] if row["chair"] == empty["payload"]["chair"]
    )
    assert empty_dissent["compared"] is True
    assert empty_dissent["departed"] is True
    assert empty_dissent["departures"] == [
        {
            "reading_span": {"start": 0, "end": len(reading["payload"]["text"])},
            "testimonium_span": {"start": 0, "end": 0},
        }
    ]

    review = next(
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["subject_id"] == empty["subject_id"]
    )
    assert review["payload"]["coverage"]["by_outcome"] == {
        "genuinely-empty": 1,
        "read": 2,
    }
    assert review["payload"]["coverage"]["under_witnessed"] is False
    assert all(region["witness_covered"] for region in reading["payload"]["basis"]["regions"])
    assert export_of(tree)["aggregate"]["status"] == "complete"


def _fallback_testimonia(tree: RunTree) -> list[dict]:
    """Every act-scoped Testimonium for the minted page-3 fallback act.

    Every attempt, not a latest-per-chair collapse: both scenarios that use
    this write exactly one attempt per chair, and the callers assert the
    count. A reread scenario would need the collapse before reusing this.
    """
    records = []
    for artifact in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if artifact["kind"] != "testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "testimonium", artifact["artifact_id"])
        if record["payload"]["act_key"] == "page-fallback:3":
            records.append(record)
    return records


def test_an_ink_free_page_fallback_is_read_but_not_retroactively_witness_covered(tmp_path):
    """A recovery crop outside every observed box remains visibly under-witnessed."""
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "ink-free-page")
    assert result.returncode == 3, result.stderr
    tree = RunTree(root, "r")

    testimonia = _fallback_testimonia(tree)
    assert len(testimonia) == 3
    assert all(record["outcome"] == "genuinely-empty" for record in testimonia)
    assert all(record["payload"]["regions"] for record in testimonia)
    # And each one rests on a declared response to this exact request rather
    # than on the act's identity: `proof/skeleton_fixture.toml` declares an
    # empty witness response per chair for `page-fallback:3` under this
    # scenario, and `ink-free-page-unwitnessed` below is the same page with
    # those three declarations removed.
    assert all(record["payload"]["provenance"]["receipt_ref"] is not None for record in testimonia)
    assert all(record["payload"]["payload"] == "" for record in testimonia)

    reading = next(
        tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio"
        and tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])["payload"]["act_key"]
        == "page-fallback:3"
    )
    assert reading["outcome"] == "no-readable-text"
    assert reading["payload"]["text"] == ""
    assert all(not region["witness_covered"] for region in reading["payload"]["basis"]["regions"])


def test_an_undeclared_fallback_witness_holds_the_act_instead_of_reporting_it_blank(tmp_path):
    """Sol-S1's red demonstration, kept runnable.

    `ink-free-page-unwitnessed` is the identical ink-free page with no witness
    response declared for the minted fallback act. Before the fix the act's
    identity alone produced `genuinely-empty` for every configured chair, with
    the proposal regions attached, the attempt marked attempted, a serving
    receipt minted and trusted-boundary health recorded — and the Recensor then
    sealed `confirmed-blank`, stating that three chairs had actually and
    independently read the page. The conclusion was true of that white page; the
    evidence was not, and the same shape over a page with ink is GOALS 1's worst
    failure arriving as a green run.

    So: no response, no reading. Every chair is `not-run`, nothing claims a
    receipt or a region it was never shown, no page witness reports a reading,
    and the act ends held with the shortfall named rather than sealed blank.
    """
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "ink-free-page-unwitnessed")
    # Exit 3 is "accounted, holdable": the run reached honest terminal states
    # for every act and one of them is held. A zero here would be the vacuous
    # green the finding was about.
    assert result.returncode == 3, result.stderr
    tree = RunTree(root, "r")

    testimonia = _fallback_testimonia(tree)
    assert len(testimonia) == 3
    assert all(record["outcome"] == "not-run" for record in testimonia)
    assert all(record["payload"]["regions"] == [] for record in testimonia)
    assert all(record["payload"]["provenance"]["receipt_ref"] is None for record in testimonia)
    assert all(record["payload"]["payload"] is None for record in testimonia)
    # Not "measured empty": emptiness is unknown, because nothing was asked.
    assert all(record["payload"]["content_health"]["empty"] is None for record in testimonia)
    assert all("reported" not in record["payload"] for record in testimonia)

    page_records = [
        tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "page-testimonium"
    ]
    page_three = [row for row in page_records if row["payload"]["page_ordinal"] == 3]
    assert len(page_three) == 2, "both declared page witnesses must still be accounted for"
    # No underlying request reached either configured page chair. `failed` is
    # receipt-bearing attempted failure; preserving the same word here would
    # force consumers to guess from another field whether it means attempted.
    assert all(row["outcome"] == "not-run" for row in page_three)
    assert all(row["payload"]["presented"] == {} for row in page_three)
    assert all(row["payload"]["provenance"]["receipt_ref"] is None for row in page_three)

    reading = next(
        tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio"
        and tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])["payload"]["act_key"]
        == "page-fallback:3"
    )
    # The Perlector still reads the ink itself — its own autopsia is unaffected
    # by the witnesses being absent, and that is exactly why the witnesses may
    # not be invented to agree with it.
    assert reading["outcome"] == "no-readable-text"
    assert not any(region["witness_covered"] for region in reading["payload"]["basis"]["regions"])

    review = next(
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["subject_id"] == reading["subject_id"]
    )
    assert review["outcome"] == "held-for-review"
    assert review["payload"]["coverage"]["by_outcome"] == {"not-run": 3}
    assert review["payload"]["coverage"]["unresolved_chairs"] == 3
    assert "blank_evidence" not in review["payload"]
    assert "independently" not in review["payload"]["reason"]

    export = export_of(tree)
    entry = next(row for row in export["non_delivered"] if row["act_key"] == "page-fallback:3")
    assert entry["category"] == "held-for-review"
    assert entry["under_witnessed"] is True
    assert export["aggregate"]["status"] == "partial"
    assert not any(
        tree.read_artifact(RECENSOR, row["kind"], row["artifact_id"])["outcome"]
        == "confirmed-blank"
        for row in tree.build_manifest(RECENSOR)["artifacts"]
    )


def test_a_shortened_resealed_proposal_denominator_stops_the_first_consumer(tmp_path):
    """The fixture's a2 cannot silently disappear from the downstream denominator."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
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
    resealing_context = _designator_context_for(root, "r", "happy")
    resealing_context.seal_boundary()
    resealing_context.finish()
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
        [
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ]
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
    act_id = derive_act_id(page_id, "residual", bounds)
    hold = context.publish(
        kind="hold",
        subject_id=act_id,
        outcome="held",
        inputs=[conservation_ref] if conservation_ref is not None else [],
        payload={
            "act_key": f"residual:{page_ordinal}:{index}",
            "page_ordinal": page_ordinal,
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


def _reseal_with_extra_row(
    tree: RunTree, context, row: dict, *, include_hold_evidence: bool = True
) -> None:
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
    context.seal_boundary()
    context.finish()


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
    _reseal_with_extra_row(tree, context, row)

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
    _reseal_with_extra_row(tree, context, row)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == EXIT_FATAL
    assert "does not reference exactly one conservation" in result.stderr


def test_a_residual_whose_bounds_do_not_match_its_own_conservation_record_is_refused(tmp_path):
    """The conservation record the hold references must actually carry this residual.

    The hold's own bounds still recompute the claimed identity correctly, and it
    references a real conservation record — but no component in that record's
    own `residual_components` names this rectangle. Self-consistency plus a
    reference is not the same as a reference that actually corroborates the
    claim.
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
    _reseal_with_extra_row(tree, context, row)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == EXIT_FATAL
    assert "does not carry at those bounds" in result.stderr


def test_a_residual_act_claiming_to_be_proposed_is_refused(tmp_path):
    """A residual may only ever be `held`; it was never a structural proposal."""
    root = tmp_path / "runs"
    _run_through_designator(root)

    tree = RunTree(root, "r")
    context = _designator_context_for(root, "r", "happy")
    page_id = page_identity(context.fixture, 1)
    row = _mint_test_residual_row(context, page_id, 1, 0, {"x": 1, "y": 1, "w": 2, "h": 2})
    row["outcome"] = "proposed"
    _reseal_with_extra_row(tree, context, row)

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
    _reseal_with_extra_row(tree, context, row)

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
    _reseal_with_extra_row(tree, context, row)

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == EXIT_FATAL
    assert "does not verify against the residual class and bounds" in result.stderr


def test_a_residual_act_with_no_hold_record_is_refused(tmp_path):
    """An extra act is not accounted for merely because the seal names it."""
    root = tmp_path / "runs"
    _run_through_designator(root)

    tree = RunTree(root, "r")
    context = _designator_context_for(root, "r", "happy")
    page_id = page_identity(context.fixture, 1)
    bounds = {"x": 1, "y": 1, "w": 2, "h": 2}
    row = {
        "act_id": derive_act_id(page_id, "residual", bounds),
        "act_key": "residual:1:0",
        "page_id": page_id,
        "page_ordinal": 1,
        "has_continuation": False,
        "outcome": "held",
        "evidence": [],
    }
    _reseal_with_extra_row(tree, context, row, include_hold_evidence=False)

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
    context.seal_boundary()
    context.finish()

    result = invoke_stage(root, "r", "happy", "pipeline/3_attestatores/run.py")
    assert result.returncode == EXIT_FATAL
    assert "accounts for no held act for" in result.stderr


def test_recensor_refuses_duplicate_witness_attempt_ordinals_instead_of_selecting_one(tmp_path):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
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
        if entry["kind"] == "review"
        and entry["outcome"] == "recovery-requested"
        # Unit 10C retains a second request for the page witness's unclaimed
        # native observation. Mutate the exact review the selected request
        # binds rather than relying on manifest order to choose one.
        and entry["subject_id"] == request["subject_id"]
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
    rebind_stage_seal(tree, ARCHETYPUS)

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


def test_a_continuation_has_page_scoped_testimony_and_audit_on_its_far_page(happy_run):
    """GOALS 3/5: page two retains and audits the pixels a2 contributes there."""
    _, tree = happy_run
    a2 = next(
        act
        for act in tree.read_artifact(
            DESIGNATOR,
            "proposal-seal",
            artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None),
        )["payload"]["expected_acts"]
        if act["act_key"] == "a2"
    )
    continuation = [
        record
        for record in artifacts(tree, ATTESTATORES, "page-testimonium")
        if record["payload"].get("page_ordinal") == 2
    ]
    assert {record["payload"]["chair"] for record in continuation} == {
        "attestator_1",
        "attestator_3",
    }
    assert {record["payload"]["page_role"] for record in continuation} == {"continuation"}
    attachment = next(
        record
        for record in artifacts(tree, ATTESTATORES, "act-attachment")
        if record["subject_id"] == a2["act_id"]
    )
    assert {
        row["page_ordinal"] for row in attachment["payload"]["attachments"] if row["page_witness"]
    } == {1, 2}
    draft = next(
        record
        for record in artifacts(tree, PERLECTOR, "audit-draft")
        if record["subject_id"] == a2["act_id"]
    )
    # The pair is pinned, not just the sorted set. `page_identity` derives the
    # id from the page's bytes, so two byte-identical fixture pages would
    # collapse to one element and this assertion would still pass -- with a2's
    # audit denominator quietly down to one page, and page two, which is where
    # the continuation evidence this test is named for lives, never audited.
    fixture = load_fixture(str(ROOT / "proof"))
    both_pages = sorted({page_identity(fixture, 1), page_identity(fixture, 2)})
    assert len(both_pages) == 2, "the fixture's two pages share one identity"
    assert draft["payload"]["page_ids"] == both_pages


def test_a_continuation_counts_page_witness_chairs_not_page_pairs(happy_run):
    """The dossier roster count cannot grow when the same chairs span two pages."""
    _, tree = happy_run
    reading = next(
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a2"
    )

    attachment = reading["payload"]["dossier"]["act_attachment"]
    assert attachment["page_witness_count"] == 2
    assert len(attachment["comparison_views"]) == 2


def test_a_recrop_of_a_continuation_act_keeps_its_far_page_in_the_evidence(
    continuation_recovery_run,
):
    """A primary-page recrop must retain the continuation page at attempt two."""
    _, tree = continuation_recovery_run
    a2 = next(
        act
        for act in tree.read_artifact(
            DESIGNATOR,
            "proposal-seal",
            artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None),
        )["payload"]["expected_acts"]
        if act["act_key"] == "a2"
    )
    regions = [
        record
        for record in artifacts(tree, DESIGNATOR, "region")
        if record["subject_id"] == a2["act_id"]
    ]
    origins = sorted(
        (region["payload"]["origin"], region["payload"]["transform"]["source_page_ordinal"])
        for region in regions
    )
    # This distinguishes a real primary-page recrop from a vacuous second pass.
    assert origins == [("proposal", 1), ("proposal", 2), ("recovery", 1)]

    readings = {
        record["payload"]["attempt_ordinal"]: record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["subject_id"] == a2["act_id"]
    }
    assert sorted(readings) == [1, 2], "the recovery round must add exactly one attempt"
    for ordinal, reading in readings.items():
        pages = {basis["source_page_ordinal"] for basis in reading["payload"]["basis"]["regions"]}
        assert pages == {1, 2}, f"attempt {ordinal} lost the continuation page"

    drafts = {
        record["payload"]["attempt_ordinal"]: record["payload"]["page_ids"]
        for record in artifacts(tree, PERLECTOR, "audit-draft")
        if record["subject_id"] == a2["act_id"]
    }
    fixture = load_fixture(str(ROOT / "proof"))
    both_pages = sorted({page_identity(fixture, 1), page_identity(fixture, 2)})
    # The sibling site's pin, for the same reason: the set collapses to one
    # element if the fixture's two pages ever become byte-identical, and both
    # attempts would then reconcile against a one-page denominator while still
    # comparing equal to each other.
    assert len(both_pages) == 2, "the fixture's two pages share one identity"
    assert drafts == {1: both_pages, 2: both_pages}

    attachment = next(
        record
        for record in artifacts(tree, ATTESTATORES, "act-attachment")
        if record["subject_id"] == a2["act_id"]
    )
    # Recovery does not re-enter Attestatores, so attempt two must reconcile to
    # this pre-recrop attachment denominator.
    assert {
        row["page_ordinal"] for row in attachment["payload"]["attachments"] if row["page_witness"]
    } == {1, 2}


def test_a_continuation_act_is_flagged_once_per_witness_not_once_per_page(happy_run):
    """Act-local flags measure witness disagreements, not contributing pages."""
    _, tree = happy_run
    drafts = {
        entry["subject_id"]: tree.read_artifact(PERLECTOR, "audit-draft", entry["artifact_id"])[
            "payload"
        ]
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "audit-draft"
    }
    seal = tree.read_artifact(
        DESIGNATOR, "proposal-seal", artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)
    )["payload"]
    by_key = {entry["act_key"]: entry["act_id"] for entry in seal["expected_acts"]}
    single_page, continuation = drafts[by_key["a1"]], drafts[by_key["a2"]]
    assert len(single_page["page_ids"]) == 1 and len(continuation["page_ids"]) == 2
    for draft in (single_page, continuation):
        flags = draft["flags"]
        assert [flag["class"] for flag in flags] == ["testimony-diff", "testimony-diff"]
        # Distinct spans prove the two rows are witnesses, not page duplicates.
        assert len({(flag["location"]["start"], flag["location"]["end"]) for flag in flags}) == 2
        assert len(draft["flags"]) == len(
            {(flag["class"], flag["location"]["start"], flag["location"]["end"]) for flag in flags}
        )


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
    assert len(recipes) == 9
    assert recipes[INK_MAP] == "deterministic-residual-ink-v1"
    assert all(
        revision.startswith("fake-") for stage, revision in recipes.items() if stage != INK_MAP
    )
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


def test_a_structured_testimonium_is_retained_and_carried_as_an_incomparable_witness(tmp_path):
    """A structured witness ends the run honestly rather than crashing.

    Spec 07 requires `payload` to be the witness's native output, verbatim, never
    coerced into a shared body schema — so a witness whose real output is an object
    lands here as an object. The Perlector's dossier now reads that retained
    `payload` layer natively (`reported_basis` names the derivation), and
    `dissent_against` records the chair as `compared: "unknown"` with the fact
    named rather than raising: a structured report is visible, not silently
    dropped and not a crash. The act still lands under-witnessed and the run
    still ends `partial` rather than a falsely `complete` export.

    **Said exactly, because the mechanism matters more than the exit code.**
    What holds the floor down *in this scenario* is `attached: False`: the page
    join legitimately omits a structured act (its `unjoined_act_attempts` row
    says so by name), the chair's page capture therefore observes no box over
    a1's proposal, and the attachment is geometrically unattached. The
    `comparable` boolean is the SECOND, independent guard, for the case this
    fixture does not produce — a chair whose native geometry does overlap the
    act while its retained testimony is structured. That case is driven over real
    records in `pipeline/5_recensor/test_comparability_floor.py`, and the
    arithmetic in `common/contracts/test_contracts_algebra.py`; claiming this
    scenario exercises it would report an instrument that did not run
    (GOVERNANCE 10).
    """
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "structured-witness")

    assert result.returncode == 3, result.stderr
    assert "act a1 is held-for-review" in result.stdout
    assert "act a1 is under-witnessed (2 of a floor of 3)" in result.stdout

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

    perlectio = next(
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["subject_id"] == structured["subject_id"]
    )
    unknown_row = next(
        row for row in perlectio["payload"]["dissent"] if row["chair"] == "attestator_1"
    )
    assert unknown_row == {
        "chair": "attestator_1",
        "compared": "unknown",
        "reason": "no comparable text for this act: retained derived testimony is not text",
    }

    attachment = next(
        record
        for record in artifacts(tree, ATTESTATORES, "act-attachment")
        if record["subject_id"] == structured["subject_id"]
    )
    rows = [row for row in attachment["payload"]["attachments"] if row["chair"] == "attestator_1"]
    assert rows and all(row["attached"] is False and row["comparable"] is False for row in rows), (
        rows
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
    rebind_stage_seal(tree, ATTESTATORES)

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
        "pipeline/1_ink_map/run.py",
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
        "pipeline/1_ink_map/run.py",
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
    rebind_stage_seal(tree, PERLECTOR)
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
    rebind_stage_seal(tree, PERLECTOR)
    rebind_stage_seal(tree, RECENSOR)

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
    # R8: the canonical uncertainty layer anchors to the text; a forged blank
    # reading must stay schema-consistent or the uncertainty refusal fires
    # before the boundary this test exercises (host fix at R8 integration).
    reading["payload"]["self_revision"] = []
    reading["payload"]["uncertain_spans"] = []
    reading["payload"]["gaps"] = []
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
    rebind_stage_seal(tree, PERLECTOR)
    rebind_stage_seal(tree, RECENSOR)
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
    prove the record it writes remains exportable through the (now
    damage-honest since the T0 export repair) Armarium -- an Archetypus record that cannot survive
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
    # R8: the canonical uncertainty layer anchors to the text; a forged blank
    # reading must stay schema-consistent or the uncertainty refusal fires
    # before the boundary this test exercises (host fix at R8 integration).
    reading["payload"]["self_revision"] = []
    reading["payload"]["uncertain_spans"] = []
    reading["payload"]["gaps"] = []
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
    rebind_stage_seal(tree, PERLECTOR)
    rebind_stage_seal(tree, RECENSOR)

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
    # EXIT_HELD, not 0, since the export became honest about damage: an act
    # delivered with a record that establishes no readable text is a delivered act
    # the run cannot call complete, and the aggregate names it. Surviving the
    # consumer is what this test is about, and the row below is still there.
    assert export_result.returncode == EXIT_HELD, export_result.stderr
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
    # R8: the canonical uncertainty layer anchors to the text; a forged blank
    # reading must stay schema-consistent or the uncertainty refusal fires
    # before the boundary this test exercises (host fix at R8 integration).
    reading["payload"]["self_revision"] = []
    reading["payload"]["uncertain_spans"] = []
    reading["payload"]["gaps"] = []
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
    rebind_stage_seal(tree, PERLECTOR)
    rebind_stage_seal(tree, RECENSOR)

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
    # R8: the canonical uncertainty layer anchors to the text; a forged blank
    # reading must stay schema-consistent or the uncertainty refusal fires
    # before the boundary this test exercises (host fix at R8 integration).
    reading["payload"]["self_revision"] = []
    reading["payload"]["uncertain_spans"] = []
    reading["payload"]["gaps"] = []
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
    rebind_stage_seal(tree, PERLECTOR)
    rebind_stage_seal(tree, RECENSOR)

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
    rebind_stage_seal(tree, RECENSOR)

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
    rebind_stage_seal(tree, PERLECTOR)
    rebind_stage_seal(tree, RECENSOR)

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
    rebind_stage_seal(tree, PERLECTOR)

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
    rebind_stage_seal(tree, ARCHETYPUS)
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/7_armarium/run.py")
    assert result.returncode == 2
    assert "does not exactly preserve the Perlectio" in result.stderr
    assert snapshot(root) == before


def test_armarium_refuses_a_resealed_archetypus_uncertainty_layer_its_parent_never_said(tmp_path):
    """The text's sibling, for the layer that anchors to it.

    R8's canonical layer is bound to its act by exactly one gate: the Armarium
    re-derives it from the accepted Perlectio at export and refuses a stored
    layer that disagrees. Nothing inside a delivered package can catch a layer
    substituted before the package was built -- a bundle verifies its own
    internal agreement, and every format would agree on the forgery -- so this
    is the boundary that makes the exported layer THIS act's uncertainty rather
    than a well-formed one. The forged span is valid against the established
    text and leaves it and its hash untouched, so only the re-derivation can
    refuse it.
    """
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
    perlectio = next(
        tree.read_artifact(PERLECTOR, "perlectio", candidate["artifact_id"])
        for candidate in tree.build_manifest(PERLECTOR)["artifacts"]
        if candidate["kind"] == "perlectio" and candidate["subject_id"] == record["subject_id"]
    )
    assert record["payload"]["uncertainty"] == from_perlectio(perlectio["payload"])
    assert len(record["payload"]["text"]) >= 1
    record["payload"]["uncertainty"]["uncertain_spans"] = [
        {"start": 0, "end": 1, "alternatives": ["?"], "confidence": "low"}
    ]
    record["payload"]["self_hash"] = self_hash(record["payload"])
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    rebind_stage_seal(tree, ARCHETYPUS)
    before = snapshot(root)

    result = invoke_stage(root, "r", "happy", "pipeline/7_armarium/run.py")
    assert result.returncode == 2
    assert "uncertainty layer differs from its accepted Perlectio" in result.stderr
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
    rebind_stage_seal(tree, ARCHETYPUS)
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
    # The count includes two retained Chandra-response blobs, Unit 12's two
    # content-addressed raw Churro responses, Unit 13's retained DAI act
    # responses, and Unit 9's ink-map artifacts.
    assert len(before) == HAPPY_SNAPSHOT_FILES
    assert semantic_snapshot_digest(root) == HAPPY_RUN_TREE_DIGEST
    assert orchestrate(root, "r", "happy").returncode == 0
    after = snapshot(root)

    assert after == before
    assert semantic_snapshot_digest(root) == HAPPY_RUN_TREE_DIGEST


def test_repeating_the_identical_command_with_nuda_enabled_leaves_every_byte_unchanged(tmp_path):
    """The rerun invariant must also hold when deterministic sampling is active."""
    root = tmp_path / "runs"
    assert (
        orchestrate(
            root, "r", "happy", nuda_per_mille=1000, nuda_approval_ref=NUDA_APPROVAL_SUBJECT
        ).returncode
        == 0
    )
    before = snapshot(root)
    assert any("lectio-nuda" in path for path in before), "the run must actually have sampled nuda"

    assert (
        orchestrate(
            root, "r", "happy", nuda_per_mille=1000, nuda_approval_ref=NUDA_APPROVAL_SUBJECT
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

    # Unit 11 adds the same two retained Chandra-response blobs before review's
    # recovery loop; its append-only invariant is unchanged. Unit 14B Sonnet
    # audit: fewer than 118 -- the ink-confirmation gate (consult §4.5) removes
    # the second recovery round a2's unconfirmed witness box used to spend
    # (see the REVIEW_RUN_TREE_DIGEST comment above).
    assert len(before) == REVIEW_SNAPSHOT_FILES
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
    survivor_identities = file_identities(root)
    assert len(survivors) < len(complete)

    assert orchestrate(root, "r", "happy").returncode == 0
    resumed = snapshot(root)
    resumed_identities = file_identities(root)

    # Everything that survived is byte-identical: resume reused it rather than
    # redoing it. And the finished tree is identical to the uninterrupted one.
    for path, digest in survivors.items():
        # Membership first, or a deleted survivor reports as a bare KeyError
        # that reads like a broken test rather than a page that left the run.
        assert path in resumed, f"resume deleted surviving evidence at {path}"
        assert resumed[path] == digest, f"{path} was rewritten on resume"
    # Byte-identity alone cannot tell reuse from an identical rewrite, which is
    # the whole claim in this test's name. Every publication mints a new inode,
    # so an unchanged one is proof the evidence was never republished.
    checked = 0
    for path, identity in survivor_identities.items():
        if not is_immutable_evidence(path):
            continue
        checked += 1
        assert path in resumed_identities, f"resume deleted surviving evidence at {path}"
        assert resumed_identities[path] == identity, (
            f"{path} kept its bytes but was republished on resume"
        )
    assert checked, "the identity check ran over no evidence at all"
    assert resumed == complete


def test_a_run_interrupted_at_every_boundary_resumes_to_the_same_tree_and_tally(tmp_path):
    """Resume must preserve held work, recovery rounds, and incident-based tallying."""
    policy = load_hard_failure_policy(ROOT / "config" / "hard_failure.toml")

    reference_root = tmp_path / "reference"
    assert orchestrate(reference_root, "r", "review").returncode == 3
    reference = snapshot(reference_root)
    reference_tally = tally_hard_failures(RunTree(reference_root, "r"), policy)

    # Every boundary that leaves a partial tree. `armarium` is deliberately not
    # here: it is the last operation in the sequence, so stopping at it is the
    # whole run and there is nothing partial left to resume from -- the
    # `len(survivors) < len(reference)` premise below would be false, and the
    # case it would test is the rerun invariant, which section 2 already covers.
    # Everything before it is included, and the ones after the Perlector matter
    # most: the recovery round and the held-act tally both live there.
    # The expected exit is part of the case, not a constant. The `review`
    # scenario holds an act, and the Recensor is what decides that, so a range
    # ending before it completes cleanly at 0 while one ending at or after it
    # reports the hold at 3 -- the same status the whole run ends on. Asserting a
    # flat 0 would have made every later boundary unreachable and is what kept
    # this loop stopping at the Perlector.
    for stop_at, partial_exit in (
        ("door", 0),
        ("exemplar", 0),
        (INK_MAP, 0),
        ("designator", 0),
        (ATTESTATORES, 0),
        ("perlector", 0),
        ("recensor", 3),
        ("recovery", 3),
        ("archetypus", 3),
    ):
        root = tmp_path / f"stopped-at-{stop_at}"
        partial = subprocess.run(
            [
                sys.executable,
                str(ORCHESTRATOR),
                "--fixture",
                FIXTURE,
                "--scenario",
                "review",
                "--run-id",
                "r",
                "--run-root",
                str(root),
                "--from",
                "door",
                "--to",
                stop_at,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert partial.returncode == partial_exit, partial.stderr
        survivors = snapshot(root)
        survivor_identities = file_identities(root)
        assert survivors, f"stopping at {stop_at} wrote nothing to resume from"
        assert len(survivors) < len(reference)

        assert orchestrate(root, "r", "review").returncode == 3
        resumed = snapshot(root)
        resumed_identities = file_identities(root)
        assert resumed == reference, f"resuming after {stop_at} did not land on the same tree"
        # The tree being right does not mean the resume reused what it found; a
        # stage that redid its work and wrote the same bytes lands the same tree.
        # The inode is what separates the two.
        checked = 0
        for path, identity in survivor_identities.items():
            if not is_immutable_evidence(path):
                continue
            checked += 1
            assert resumed_identities[path] == identity, (
                f"resuming after {stop_at} republished {path} instead of reusing it"
            )
        assert checked, f"stopping at {stop_at} left no evidence to check the identity of"
        assert tally_hard_failures(RunTree(root, "r"), policy) == reference_tally


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


def _pixels(bounds: dict) -> set[tuple[int, int]]:
    """Every page pixel one rectangle covers, enumerated rather than reasoned.

    Deliberately the slow, obvious construction: this is the independent check
    on the Designator's own coverage arithmetic, and a second clever
    implementation of it would agree with the first about the same mistake.
    """
    return {
        (x, y)
        for x in range(bounds["x"], bounds["x"] + bounds["w"])
        for y in range(bounds["y"], bounds["y"] + bounds["h"])
    }


def test_the_recovery_recrop_actually_widened_the_crop_it_was_asked_for(review_run):
    """GOVERNANCE 11: "Recovery exists for **completeness and coverage**."

    This scenario is the walking skeleton's single proof that bounded recovery
    works, so what it spends the `fallback_recrop` budget on has to be a crop
    that recovers something. It did not: the fixture declared act a1's recovery
    rectangle as 16,16,168,88 while the proposal had already cut the padded
    12,15,188,99 -- `[16,184) x [16,104)` strictly inside `[12,200) x [15,114)`.
    Every pixel the recrop "recovered" was a pixel the act already had, and the
    only guard on the path compared transform identity, which a subset passes.

    Measured over the run's own published regions, not over the fixture
    declaration, so a padding change that swallowed the recovery rectangle would
    fail here rather than quietly restore the defect.
    """
    _, tree = review_run
    regions = [
        record
        for record in artifacts(tree, DESIGNATOR, "region")
        if record["payload"]["act_key"] == "a1"
    ]
    by_origin = {record["payload"]["origin"]: record for record in regions}
    assert sorted(by_origin) == ["proposal", "recovery"]

    proposal = by_origin["proposal"]["payload"]["transform"]
    recovery = by_origin["recovery"]["payload"]["transform"]
    # Pixel sets only mean anything within one page's coordinate space.
    assert proposal["source_page_id"] == recovery["source_page_id"]
    assert proposal["source_page_ordinal"] == recovery["source_page_ordinal"] == 1

    already_cut = _pixels(proposal["bounds"])
    recropped = _pixels(recovery["bounds"])
    assert recropped - already_cut, (
        "a recovery that recovers no page pixel is a spent budget and a coverage "
        "caveat about nothing"
    )
    # ARCHITECTURE calls the operation a "fallback or **expanded** recrop". An
    # expansion adds coverage without trading any away: a rectangle that gained
    # a left margin by giving up the right edge would satisfy the guard while
    # dropping ink the proposal had already captured.
    assert not already_cut - recropped, "an expanded recrop must not drop coverage it had"
    assert len(recropped - already_cut) == 200 * 114 - 188 * 99 == 4188

    # And it takes nothing from the neighbouring act: every page pixel stays cut
    # under exactly one act identity. a2's *capture* rectangle begins at y=114,
    # six rows above its declared structural top edge, so "clear of a2" has to
    # be read against the rectangle that was actually cut.
    neighbour = next(
        record
        for record in artifacts(tree, DESIGNATOR, "region")
        if record["payload"]["act_key"] == "a2"
        and record["payload"]["transform"]["source_page_ordinal"] == 1
    )
    assert not recropped & _pixels(neighbour["payload"]["transform"]["bounds"])


def test_the_witness_uncovered_caveat_names_the_region_carrying_the_new_pixels(review_run):
    """The second-order half of the same finding.

    `witness_covered: false` means "ink a recovery uncovered was never shown to a
    witness" (`pipeline/2_designator/run.py::cut_minted_region`). While the
    recovery crop was a strict subset of the proposal crop it uncovered nothing,
    so the export carried that caveat over pixels every witness had already
    seen. Binding the flag to the geometry is what makes the caveat mean
    something; `test_recovery_ink_is_recorded_as_witness_uncovered` asserts the
    flag itself.
    """
    _, tree = review_run
    regions = [
        record
        for record in artifacts(tree, DESIGNATOR, "region")
        if record["payload"]["act_key"] == "a1"
    ]
    by_origin = {record["payload"]["origin"]: record for record in regions}
    new_pixels = _pixels(by_origin["recovery"]["payload"]["transform"]["bounds"]) - _pixels(
        by_origin["proposal"]["payload"]["transform"]["bounds"]
    )
    assert new_pixels

    latest = max(
        (
            record
            for record in artifacts(tree, PERLECTOR, "perlectio")
            if record["payload"]["act_key"] == "a1"
        ),
        key=lambda record: record["payload"]["attempt_ordinal"],
    )
    uncovered = {
        region["region_id"]
        for region in latest["payload"]["basis"]["regions"]
        if not region["witness_covered"]
    }
    assert uncovered == {by_origin["recovery"]["payload"]["region_id"]}


def test_the_recovery_request_and_both_reading_attempts_survive(review_run):
    """Unit 14B Sonnet audit: a2 no longer requests a spurious second recovery.

    Before the correction, a2 (which `hold_acts` declares should go straight
    to a hold) independently satisfied `wants_recovery` from the same page's
    marginal witness box -- a report with zero ink behind it (consult §4.5;
    see the `REVIEW_RUN_TREE_DIGEST` comment above). Only a1's scenario-
    declared request is a real, retained coverage decision here.
    """
    _, tree = review_run
    requests = artifacts(tree, RECENSOR, "recovery-request")
    assert len(requests) == 1
    assert {request["payload"]["act_key"] for request in requests} == {"a1"}

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
    """A continuation is part of the original proposal, not a later attempt.

    The geometric coverage bit says whether a witness observation contains a
    particular region; it is not permission to omit a continuation region from
    the Perlector's image basis.  Both sides must remain there even where a2 is
    never recovered at all -- exactly the case `review` now exercises (Unit
    14B Sonnet audit: a2's own second reading here used to come from the same
    unconfirmed marginal witness box `REVIEW_RUN_TREE_DIGEST` above documents;
    it is refused there for the same lack of ink evidence). The stronger claim
    -- that a genuine recovery recrop keeps the far side of a real continuation
    in the evidence -- is proven end to end by
    `test_a_recrop_of_a_continuation_act_keeps_its_far_page_in_the_evidence`
    against `continuation_recovery_run`, which declares a2 for recovery.
    """
    _, tree = review_run
    readings = [
        record
        for record in artifacts(tree, PERLECTOR, "perlectio")
        if record["payload"]["act_key"] == "a2"
    ]
    assert sorted(record["payload"]["attempt_ordinal"] for record in readings) == [1]
    reading = readings[0]
    regions = reading["payload"]["basis"]["regions"]
    proposal_regions = {
        record["payload"]["region_id"]
        for record in artifacts(tree, DESIGNATOR, "region")
        if record["payload"]["act_key"] == "a2" and record["payload"]["origin"] == "proposal"
    }
    assert len(proposal_regions) == 2
    assert proposal_regions <= {region["region_id"] for region in regions}
    assert {
        region["source_page_ordinal"]
        for region in regions
        if region["region_id"] in proposal_regions
    } == {1, 2}


def test_recovery_stayed_inside_its_budget(review_run):
    """Unit 14B Sonnet audit: one request, not two -- see the comment on
    `test_the_recovery_request_and_both_reading_attempts_survive`."""
    _, tree = review_run
    requests = artifacts(tree, RECENSOR, "recovery-request")
    # pr/12's page-wide grant leaves review with one request, not two. The
    # allowance it carries is still the configured one: `fallback_recrop +
    # page_level_reread`, 1 + 1 in config/recovery.toml, separately bounded by
    # `absolute_cap = 3`. The exact value, not merely "within the cap": `<= 3`
    # is also satisfied by a budget that silently collapsed to 0 or 1, so it
    # could not fail for the regression it names (GOVERNANCE 10).
    assert len(requests) == 1
    allowed = [request["payload"]["budget_allowed"] for request in requests]
    assert allowed == [2], "the configured recovery budget is one recrop plus one reread"
    assert all(
        value <= request["payload"]["recovery_policy"]["absolute_cap"]
        for value, request in zip(allowed, requests, strict=True)
    ), "the absolute cap is a ruling (config/recovery.toml absolute_cap)"


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
    dedicated `witness-capabilities` scenario. R0 left both page-witness chairs
    unknown until R4 provided act-anchored comparison views; now that R4's
    alignment lands a comparison view for both, only the capability-declared
    chair stays unknown, and the reference happy run — where no chair declares
    the capability — compares all three.
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
    assert [row["compared"] for row in reading["payload"]["dissent"]].count("unknown") == 1

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
    assert {row["chair"] for row in happy_dissent if row["compared"] == "unknown"} == set()


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


# --- 7. Every contract handoff refuses corruption ------------------------------

# (producer, consumer, the artifact kind that crosses this boundary)
HANDOFF_ARTIFACTS = (
    (DOOR, EXEMPLAR, "admission"),
    (EXEMPLAR, INK_MAP, "page"),
    (INK_MAP, DESIGNATOR, "ink-map"),
    (DESIGNATOR, ATTESTATORES, "region"),
    (ATTESTATORES, PERLECTOR, "testimonium"),
    (PERLECTOR, RECENSOR, "perlectio"),
    (RECENSOR, ARCHETYPUS, "review"),
    (ARCHETYPUS, ARMARIUM, "archetypus"),
)

# A sibling to HANDOFF_ARTIFACTS: seals prove complete stage boundaries, including
# Armarium's final one, which the orchestrator itself consumes.
SEAL_ARTIFACTS = (
    (DOOR, EXEMPLAR),
    (EXEMPLAR, INK_MAP),
    (INK_MAP, DESIGNATOR),
    (DESIGNATOR, ATTESTATORES),
    (ATTESTATORES, PERLECTOR),
    (PERLECTOR, RECENSOR),
    (RECENSOR, ARCHETYPUS),
    (ARCHETYPUS, ARMARIUM),
    (ARMARIUM, "orchestrator"),
)

CONSUMER_PROGRAMS = {
    EXEMPLAR: "pipeline/1_exemplar/run.py",
    INK_MAP: "pipeline/1_ink_map/run.py",
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


@pytest.mark.full
@pytest.mark.parametrize("producer,consumer", SEAL_ARTIFACTS)
def test_each_stage_seal_corruption_stops_its_named_consumer(
    happy_run, tmp_path, producer, consumer
):
    """Every seal has a downstream reader; Armarium's reader is the orchestrator."""
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    path = _stage_seal_path(tree, producer)
    record = json.loads(path.read_bytes())
    record["schema"] = "skeleton.v99"
    path.write_bytes(canonical_bytes(record))
    before = snapshot(root)

    if consumer == "orchestrator":
        # Do not rerun Armarium: that would let the producer's own scan refuse
        # first and would not exercise the final consumer this row names. The
        # orchestrator now reaches that boundary through `verify_final_seal`,
        # which is the same seal contract plus the export check, so that is what
        # this row drives. `SchemaRefusal` is a `ContractError`, and the message
        # match is kept so the refusal still has to name the forged config.
        with pytest.raises(ContractError, match="skeleton.v99|schema"):
            verify_final_seal(tree)
        assert snapshot(root) == before
        return

    result = invoke_stage(root, "r", "happy", CONSUMER_PROGRAMS[consumer])

    assert result.returncode != 0
    assert "skeleton.v99" in result.stderr or "SchemaRefusal" in result.stderr
    assert snapshot(root) == before


def _stage_seal_path(tree: RunTree, stage: str) -> Path:
    entry = next(
        entry for entry in tree.build_manifest(stage)["artifacts"] if entry["kind"] == "stage-seal"
    )
    return tree.resolve(entry["relative_path"])


@pytest.mark.full
def test_next_stage_refuses_blob_content_changed_under_the_named_exemplar_seal(happy_run, tmp_path):
    """A blob name is content-addressed only until disk bytes are independently read."""
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    blob = next(
        path
        for path in tree.resolve("1_exemplar/blobs").rglob("*")
        if path.is_file() and path.name != tree.read_run()["register_digest"]
    )
    blob.write_bytes(b"tampered bytes under the same filename")

    result = invoke_stage(root, "r", "happy", "pipeline/1_ink_map/run.py")

    assert result.returncode == EXIT_FATAL
    assert "exemplar stage-seal" in result.stderr
    assert "inventory no longer matches disk" in result.stderr


@pytest.mark.full
def test_next_stage_refuses_artifact_added_after_the_named_boundary(happy_run, tmp_path):
    """The Exemplar-to-Ink-Map addition case. Its sibling below is one link later."""
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    forged = build_envelope(
        run_id="r",
        artifact_id=artifact_id(EXEMPLAR, "added-after-seal", "added", None),
        subject_id="added",
        stage=EXEMPLAR,
        kind="added-after-seal",
        outcome="sealed",
        config_digest=tree.read_run()["config_digest"],
        adapter_revision=tree.read_artifact(
            EXEMPLAR,
            "page",
            next(
                entry["artifact_id"]
                for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
                if entry["kind"] == "page"
            ),
        )["producer"]["adapter_revision"],
        inputs=[],
        payload={"deliberately": "unaccounted"},
    )
    tree.publish_artifact(forged)

    result = invoke_stage(root, "r", "happy", "pipeline/1_ink_map/run.py")

    assert result.returncode == EXIT_FATAL
    assert "exemplar stage-seal" in result.stderr
    assert "inventory no longer matches disk" in result.stderr


@pytest.mark.full
def test_next_stage_refuses_an_ink_map_artifact_added_after_the_named_boundary(happy_run, tmp_path):
    """Each predecessor link needs its own added-artifact corruption proof."""
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    forged = build_envelope(
        run_id="r",
        artifact_id=artifact_id(INK_MAP, "added-after-seal", "added", None),
        subject_id="added",
        stage=INK_MAP,
        kind="added-after-seal",
        # An ordinary stage kind may not wear a boundary outcome; the forged
        # addition uses the stage's own vocabulary and is still refused by the
        # seal's inventory.
        outcome="mapped",
        config_digest=tree.read_run()["config_digest"],
        adapter_revision=tree.read_artifact(
            INK_MAP,
            "ink-map",
            next(
                entry["artifact_id"]
                for entry in tree.build_manifest(INK_MAP)["artifacts"]
                if entry["kind"] == "ink-map"
            ),
        )["producer"]["adapter_revision"],
        inputs=[],
        payload={"deliberately": "unaccounted"},
    )
    tree.publish_artifact(forged)

    result = invoke_stage(root, "r", "happy", "pipeline/2_designator/run.py")

    assert result.returncode == EXIT_FATAL
    assert "ink-map stage-seal" in result.stderr
    assert "inventory no longer matches disk" in result.stderr


@pytest.mark.full
def test_next_stage_refuses_an_exemplar_artifact_removed_after_the_boundary(happy_run, tmp_path):
    """A later boundary can stay green while Exemplar removal checks regress."""
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    page = tree.resolve(
        tree.artifact_path(
            EXEMPLAR,
            "page",
            next(
                entry["artifact_id"]
                for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
                if entry["kind"] == "page"
            ),
        )
    )
    assert page.is_file()
    page.unlink()

    result = invoke_stage(root, "r", "happy", "pipeline/1_ink_map/run.py")

    assert result.returncode == EXIT_FATAL
    assert "exemplar stage-seal" in result.stderr
    assert "inventory no longer matches disk" in result.stderr


@pytest.mark.full
def test_next_stage_refuses_an_artifact_removed_after_the_named_boundary(happy_run, tmp_path):
    """The Ink-Map-to-Designator removal case, one stage later than the test above.

    "Added or removed after the boundary" is the one corruption class a
    manifest-derived seal adds over the envelope self-hash. The Exemplar link
    is proved above; this proves the same recompute one stage later so that
    link is not left covered only for addition, never for removal.
    """
    source_root, _ = happy_run
    root = tmp_path / "runs"
    shutil.copytree(source_root, root)
    tree = RunTree(root, "r")
    ink_map = tree.resolve(
        tree.artifact_path(
            INK_MAP,
            "ink-map",
            next(
                entry["artifact_id"]
                for entry in tree.build_manifest(INK_MAP)["artifacts"]
                if entry["kind"] == "ink-map"
            ),
        )
    )
    assert ink_map.is_file()
    ink_map.unlink()

    result = invoke_stage(root, "r", "happy", "pipeline/2_designator/run.py")

    assert result.returncode == EXIT_FATAL
    assert "ink-map stage-seal" in result.stderr
    assert "inventory no longer matches disk" in result.stderr


@pytest.mark.full
def test_next_stage_refuses_an_exemplar_seal_forged_or_deleted_without_rederiving(
    happy_run, tmp_path
):
    """Forged and missing seals must be proved independently at each link."""
    source_root, _ = happy_run
    forged_root = tmp_path / "forged"
    missing_root = tmp_path / "missing"
    shutil.copytree(source_root, forged_root)
    shutil.copytree(source_root, missing_root)

    forged_tree = RunTree(forged_root, "r")
    seal_path = _stage_seal_path(forged_tree, EXEMPLAR)
    seal = json.loads(seal_path.read_bytes())
    seal["payload"]["config_digest"] = "0" * 64
    seal["self_hash"] = self_hash(seal)
    seal_path.write_bytes(canonical_bytes(seal))
    forged = invoke_stage(forged_root, "r", "happy", "pipeline/1_ink_map/run.py")
    assert forged.returncode == EXIT_FATAL
    assert "config_digest differs from run authority" in forged.stderr

    missing_tree = RunTree(missing_root, "r")
    _stage_seal_path(missing_tree, EXEMPLAR).unlink()
    missing = invoke_stage(missing_root, "r", "happy", "pipeline/1_ink_map/run.py")
    assert missing.returncode == EXIT_FATAL
    assert "exemplar has no stage-seal" in missing.stderr
    assert "never re-derived" in missing.stderr


@pytest.mark.full
def test_next_stage_refuses_forged_or_deleted_seal_without_rederiving(happy_run, tmp_path):
    """The Ink-Map-to-Designator link, one stage later than the test above."""
    source_root, _ = happy_run
    forged_root = tmp_path / "forged"
    missing_root = tmp_path / "missing"
    shutil.copytree(source_root, forged_root)
    shutil.copytree(source_root, missing_root)

    forged_tree = RunTree(forged_root, "r")
    seal_path = _stage_seal_path(forged_tree, INK_MAP)
    seal = json.loads(seal_path.read_bytes())
    seal["payload"]["config_digest"] = "0" * 64
    seal["self_hash"] = self_hash(seal)
    seal_path.write_bytes(canonical_bytes(seal))
    forged = invoke_stage(forged_root, "r", "happy", "pipeline/2_designator/run.py")
    assert forged.returncode == EXIT_FATAL
    assert "config_digest differs from run authority" in forged.stderr

    missing_tree = RunTree(missing_root, "r")
    _stage_seal_path(missing_tree, INK_MAP).unlink()
    missing = invoke_stage(missing_root, "r", "happy", "pipeline/2_designator/run.py")
    assert missing.returncode == EXIT_FATAL
    assert "ink-map has no stage-seal" in missing.stderr
    assert "never re-derived" in missing.stderr


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

    The refusal now comes from the orchestrator's own point of use rather than
    from the Door's `config_digest`, and names this file instead of reporting that
    *something* in the run's configuration moved. That is what sealing the policy
    by name buys, and it is why the message this asserts changed: the test beside
    it still shows `config_digest` refusing `recovery.toml` at the Door, because
    the Door is where a run id is reused, while the run-level cap has a reader
    that runs before any stage is invoked at all.
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
    assert "hard-failure configuration changed between" in result.stderr, result.stderr
    assert snapshot(root) == before


@pytest.mark.full
def test_every_handoff_in_the_contract_is_covered_by_this_table():
    """Meta-invariant #91 — a drift check over an agreement surface. If a handoff
    is added to the contracts and not to this table, the boundary test would
    silently cover six of seven."""
    from common.contracts.stages import HANDOFFS

    assert {(producer, consumer) for producer, consumer, _ in HANDOFF_ARTIFACTS} == set(HANDOFFS)
    assert {consumer for _, consumer, _ in HANDOFF_ARTIFACTS} == set(CONSUMER_PROGRAMS)
    assert len(HANDOFF_ARTIFACTS) == 8


def test_every_stage_has_one_seal_battery_row():
    from common.contracts.stages import STAGES

    assert {producer for producer, _ in SEAL_ARTIFACTS} == set(STAGES)
    assert len(SEAL_ARTIFACTS) == 9


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


def test_unclaimed_edge_ink_remains_held_when_designator_cut_no_page_region(tmp_path):
    """The positive edge case: an initial finding releases only after real coverage.

    `structure-failure` leaves the fixture's actual edge ink without any
    Designator crop.  It therefore proves the complementary case to happy: the
    hold reaches the terminal ledger and makes the export visibly partial.
    """
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "structure-failure")
    assert result.returncode == EXIT_HELD

    tree = RunTree(root, "r")
    export = export_of(tree)
    bundle = tree.read_bytes(export["bundle"]["reference"]["relative_path"])
    with ZipFile(BytesIO(bundle)) as archive:
        manifest = json.loads(archive.read("EXPORT_MANIFEST.json"))

    assert manifest["claims"]["status"] == "partial"
    assert manifest["claims"]["ink_map"]["held_pages"] == [1, 2]
    assert any("unclaimed-edge-ink" in reason for reason in manifest["claims"]["partial_reasons"])
    page_reasons = {
        row["unit_id"]: row["reason"]
        for row in manifest["claims"]["terminal_ledger"]["units"]
        if row["unit_type"] == "page"
    }
    assert set(page_reasons) == {"page:1", "page:2"}
    assert all("unclaimed-edge-ink" in reason for reason in page_reasons.values())


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
    reviews_by_key = {
        record["payload"]["act_key"]: record for record in artifacts(tree, RECENSOR, "review")
    }
    for act_key in ("a1", "a2"):
        assert reviews_by_key[act_key]["payload"]["geometry_coverage"] == NO_PAGE_CONSERVATION
        assert (
            reviews_by_key[act_key]["payload"]["testimony_content_coverage"]
            == NO_PAGE_CONTENT_COVERAGE
        )
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
        ("ink-map", "pipeline/1_ink_map/run.py"),
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
    rebind_stage_seal(tree, DESIGNATOR)

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
        "pipeline/1_ink_map/run.py",
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
