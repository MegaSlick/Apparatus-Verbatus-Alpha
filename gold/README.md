# Gold tooling

`python -m gold.cli sample --run RUN.json --catalog catalog.json --plan plan.json
--output-dir records/` creates a stratified, page-only human-gold sample.  The
catalog has one `{ordinal, sha256, stratum}` row for every R0 source page; the
plan contains positive quotas for both `calibration` and `locked-acceptance`, by
stratum.  The R0 frame's existing `seed` deterministically partitions pages into
one of those two sets and ranks pages within their own stratum.  A quota that the
partition cannot fill is refused; the sampler never crosses the boundary.

`ingest-manual` accepts Tyrel's `gold-manual-pick.v1` record, which has
`selection_basis`, the bound page/stratum, and his stated set.  It records that
selection unchanged; it does not choose a replacement page.  The persisted
sample's `set` is always the seed-derived partition — calibration/locked-acceptance
disjointness is enforced by construction, never by policing a human's claim — but
B1 picks are made in week one, before the R0 frame or its seed exist, so his stated
set can honestly disagree with it. That disagreement is never silently resolved
either way: it is carried unchanged as `claimed_set` alongside the true `set`, so a
predates-the-seed pick is ingested, not refused and sent back for a re-pick.
Automatically sampled records carry `claimed_set: null` (no human claim was made).

`bind-instrument` creates an append-only `gold-instrument-membership.v1` record
carrying a sample digest, an R0 act identity, and a protocol digest.

The layout schema embeds its source `gold-page-sample.v1` and has closed
`act`, `non-act-text`, `occlusion`, and `true-blank` rectangle kinds.  The padding
schema also embeds its source sample and carries only rectangles plus the required
`calibrated_for_this_corpus` flag.  `validate` checks all schemas and self-hashes;
for a sample, pass `--run` to prove the derived page and frame facts against the
R0 authority again.  Every writer creates a new file atomically and refuses an
existing pathname, preserving append-only custody.
