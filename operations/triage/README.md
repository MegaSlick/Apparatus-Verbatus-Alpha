# Triage tools

Offline tools that run before the Door and write the triage documents it reads. The
document schemas are in `common/contracts/triage.py` and described in
`pipeline/0_triage/CONTRACT.md`. Nothing here calls a model, and nothing here turns
its own output into a link between frames: only a confirmation a person supplies does.

| File | What it does |
|---|---|
| `instrument.py`, `instrument.toml` | Co-visibility candidate evidence for pairs of frames |
| `producer.py` | Decision-manifest rows, cluster records and corpus-register appends |
| `paths.py` | Canonical relative-path checks for the producer's file arguments |
| `pagekit_recipe.py` | `pagekit-producer-recipe.v1`, the producer recipe that declares pagekit's rows at the Door |
| `pagekit_geometry.py` | Decision-manifest rows that cut pagekit's prepared pages from the original scans (used by `verbatus prepare`) |

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

`instrument.producer_recipe(load_config())` is the closed `triage-producer-recipe.v2`
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
derived from its physical page ids, so adding members cannot rename it.

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

The Door refuses any submission containing a re-shoot cluster: confirmed clusters are
recorded in the register, but a run reads one capture per leaf.

## pagekit's geometry

`pagekit_geometry.map_pages` turns pagekit's chain for each page of one scan (quarter
turn, page polygon, rotation about the page's centre, crop, shrink) into triage parts
of the second operation order, `region-crop-rotate-crop`. The quarter turn and the skew
fold into one clockwise rotation of `90 * turns - skew` degrees; the first crop is the
smallest box of the scan holding everything pagekit's page shows; the crop after
rotation is pagekit's page to the nearest pixel; and the fill is pagekit's paper
colour, in the scan's own mode. A page with no skew is cut exactly, a skewed one to
within half a pixel on each axis (the crop after rotation starts on a whole pixel).
Where triage cannot express what pagekit did, the part is the nearest one that loses no
ink, and a note says why:

- **Gutter** (two pages): the regions must partition the scan, so the scan is split
  along a straight line through the middle of pagekit's cut. pagekit's overlap and the
  lean of its cut fall on one side of it; each page's crops hold everything either page
  shows inside its region, so across the scan nothing pagekit kept is dropped.
- **Shrunk**: the Door's page keeps the scan's resolution.

A scan's orientation tag is read from pagekit's chain: one that turns the scan folds
into the rotation; one that mirrors it is refused (`MappingError`), since triage has no
mirror. pagekit's padding is part of the crop after rotation. `door_colour_mode` maps a
grey page made by luminance to `grayscale` and refuses one made from a single channel.
