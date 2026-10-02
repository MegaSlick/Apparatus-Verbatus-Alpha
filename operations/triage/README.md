# Triage tools

Offline tools that run before the Door and write the triage documents it reads. The
document schemas are in `common/contracts/triage.py` and described in
`pipeline/0_triage/CONTRACT.md`. Nothing here calls a model, and nothing here turns
its own output into a link between frames: only a confirmation a person supplies does.

| File | What it does |
|---|---|
| `instrument.py`, `instrument.toml` | Co-visibility candidate evidence for pairs of frames |
| `producer.py` | Decision-manifest rows, cluster records and corpus-register appends |
| `paths.py` | Canonical relative-path checks shared by both |
| `reconcile.py`, `recordgold_midpoint_pilot.py`, `scantailor_bridge.py`, `scantailor_project.py` | One-off measurement and ScanTailor tools, to be retired; no stage or operator command calls them |

## The instrument

`instrument.candidate_evidence` reduces each master to a proxy, computes a 64x48
mean-plus-ink signature, and compares the frame pairs its selector picks (each frame
with its preceding submission window, plus an all-pairs 16-cell prefilter). Each
compared pair gets one `cluster-candidate-evidence.v1` record with a verdict:
`near-duplicate`, `complementary-candidate` or `unrelated`. A verdict is never a link:
`near-duplicate` means two signatures agree, not that two frames show one page, and the
record says so. The pass is closed by a `cluster-candidate-evidence-manifest.v1` that
counts every selected, refused and emitted pair and binds a digest of the records, so a
missing pair or changed record is detectable.

None of the tuning values in `instrument.toml` has been measured on real material. The
shipped triage modes send every row to review, so no value authorizes a link or an
apply by itself. The instrument is blind to two frames that agree because neither
carries ink (blank or near-blank openings of one printed form); the recipe lists this
under `known_blindness`.

`instrument.producer_recipe(load_config())` is the closed `triage-producer-recipe.v1`
description of a pass. Write it beside the decision manifest and hand it to the Door
as `--triage-producer-recipe`; the Door validates it and binds its digest into the
run.

## The producer

`producer.produce` writes exactly one row for every submitted master, with no cluster
unless a confirmation names one. Before producing rows it decodes every master's mode
and dimensions; a mode the deterministic encoder cannot store needs an explicit
per-part colour conversion. Submitted names must be canonical relative POSIX paths, and
traversal, non-canonical spellings and case-folding collisions refuse the whole pass.

A **confirmation** (`triage-re-shoot-confirmation.v1`, canonical JSON, at most 16 MiB)
is `{schema, corpus_id, appending_run, authority, instrument_config_sha256,
evidence_manifest_sha256, clusters}`. Each cluster is `{pages, evidence_pairs}`; a page
is `{volume_id, designation, member_frame_sha256}`. Every evidence pair must be a pair
the supplied evidence records actually compared, under the supplied recipe and
manifest, and every member must be a submitted frame the pass saw. The cluster id is
derived from its physical page ids, so adding members cannot rename it. A confirmed
cluster whose members span more Door ordinals (one per split part, ordered by path)
than `max_pages_per_shard` is refused, since no shard could hold it.

`authority` is a claim the confirmation makes about who made it; nothing verifies it.
What binds a confirmation to an operator's act is that it is a file a person supplied,
that it cannot cite evidence the instrument did not produce, and that it is kept:
`producer.commit_confirmed_production` writes the confirmation immutably before it
appends to the corpus register and before it republishes the manifest and cluster
records. A retry with identical bytes converges; different bytes need a new path.

The register receives one ordered append per confirmation: each new page's
declaration, then that page's membership link. A wrong confirmation is corrected by a
register `retraction` of the current membership head (which restores its predecessor)
and a producer pass without the wrong confirmation, which republishes the manifest and
cluster records whole.

Note that the Door currently refuses any submission containing a re-shoot cluster:
confirmed clusters are recorded in the register, but a run reads one capture per leaf.
