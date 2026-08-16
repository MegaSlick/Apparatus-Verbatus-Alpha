# Gold tooling

`python -m gold.cli sample --run RUN.json --catalog catalog.json --plan plan.json
--output-dir records/` creates a stratified, page-only human-gold sample.  The
catalog has one `{ordinal, sha256, stratum}` row for every R0 source page; the
plan carries a quota for both `calibration` and `locked-acceptance` for **every**
stratum the catalog declares.  A stratum the plan does not name would drop out of
gold without saying so, so an unnamed one is refused; quota `0` is how a stratum
is deliberately left unsampled, and it stays visible in the plan file.  The R0
frame's existing `seed` deterministically partitions pages into
one of those two sets and ranks pages within their own stratum.  A quota that the
partition cannot fill is refused; the sampler never crosses the boundary.

A drawn sample records the catalog and plan digests it came from, and carries no
`claimed_set`; a manual pick carries a `claimed_set` and no catalog or plan.  A
record cannot claim one origin while carrying the other's evidence.  That binding
is what `python -m gold.cli verify-sampling records/ --run RUN.json --catalog
catalog.json --plan plan.json` replays: validating one record proves its page is a
real corpus page in the set the seed assigns it, but only replaying the whole draw
shows that the *sampler* chose those pages.  A hand-picked page minted as
`stratified-seed`, a record quietly removed from the directory, and a catalog
re-described after the fact all fail the replay by name.

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

The act identity `bind-instrument` carries is checked for shape only (well-formed
and `act_`-prefixed); R7a has no act-producing stage before it in the build order,
so it cannot check that the act actually exists anywhere.

## The adjudication flow

An act's gold reading is made by two people independently and reconciled by a
third only where they differ.

`transcribe --sample S.json --act-identity act_… --transcriber NAME --text-file
F.txt --output T.json [--run RUN.json]` records one transcriber's reading of one
act on a sampled page.  The text file is UTF-8; its final newline belongs to the
file and is dropped, and nothing else about the bytes is adjusted.  A
transcription is never blank — an act nobody can read is transcribed `[ILLEGIBLE]`,
which is the one reserved spelling, so unreadable spans are counted rather than
guessed at and never quietly dropped.  Surrounding whitespace, a CR, and any
composition other than Unicode NFC are refused by name: agreement between two
transcribers is decided by equality, and an invisible difference would summon an
adjudicator for two identical readings and inflate the disagreement rate.

`adjudicate --first T1.json --second T2.json --output A.json [--adjudicator NAME
--text-file F.txt]` reconciles them.  If the two readings are identical there is
nothing to reconcile: the outcome is `agreed`, no adjudicator is recorded, and
naming one is refused.  If they differ, the adjudicator and their own reading of
the ink are required — **the adjudicator does not choose the better
transcription** (hard rule 8; the transcribers are people making the corpus, not
Attestatores, and no model output reaches these records).  What they read may
match one, both in part, or neither.  Both transcriptions are retained inside the
record unaltered, and `outcome` is derived from them on every read, so a record
cannot claim agreement over two readings that differ.

A name shaped like a pipeline identity is refused wherever a person is named:
gold is what the pipeline is measured against, so gold made of its output would
make the measurement circular.

## Custody

The layout schema embeds its source `gold-page-sample.v1` and has closed
`act`, `non-act-text`, `occlusion`, and `true-blank` rectangle kinds.  The padding
schema also embeds its source sample and carries only rectangles plus the required
`calibrated_for_this_corpus` flag.  `validate` checks all schemas and self-hashes;
for a sample, layout, or padding record, pass `--run` to prove the derived page and
frame facts against the R0 authority again (an embedded sample is otherwise only
checked for internal self-consistency, not that it names a real run).
`bind-instrument` accepts the same optional `--run`.

`validate-corpus records/ [--run RUN.json]` checks what no single record can. A
page's set is derived from its own frame's seed, and the seed is derived from that
frame's page digest, so re-framing the corpus — a page added, a shard resplit —
moves about half the pages to the other set. Records written before and after both
validate against their own frame, and a gold corpus holding both would place one
page in calibration *and* in locked acceptance. Records under two different corpus
frames are refused by name, as is a page stratified or numbered two ways across
records.

The layout schema requires at least one region and the padding schema at least one
rectangle: a page with nothing on it is annotated `true-blank`, and a record that
measured nothing may not carry a calibration verdict.

Every writer creates its file atomically, so a partly written record can never
take its final name.  Republishing byte-identical content is reuse — `sample`
writes one file per page, and an interrupted draw has to be finishable by the same
command — while different bytes under a name already taken are refused and the
existing file is left untouched.  A filesystem that refuses hard links is a named
refusal, not a bare traceback.
