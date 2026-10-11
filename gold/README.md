# Gold tooling

Human gold: page samples drawn from a run, and each act's reading made by two people
independently and reconciled by a third only where they differ. Gold is what the pipeline
is measured against, so no pipeline output ever enters it.

| Command | What it does |
|---|---|
| `sample --run RUN.json --catalog catalog.json --plan plan.json --output-dir records/` | draws a stratified, page-only sample and its `gold-sampling-draw.v2` record |
| `verify-sampling records/ --run RUN.json [--catalog … --plan …]` | replays the whole draw |
| `ingest-manual` | records a picker's `gold-manual-pick.v2` page pick unchanged |
| `bind-instrument` | creates an append-only `gold-instrument-membership.v1` record (sample digest, act identity, protocol digest) |
| `transcribe` | records one transcriber's reading of one act |
| `adjudicate` | reconciles two transcriptions |
| `validate [--run RUN.json]` | checks record schemas and self-hashes |
| `validate-corpus records/ [--run RUN.json]` | checks what no single record can |

All run as `python -m gold.cli <command>`.

## Sampling

The catalog has one `{ordinal, sha256, stratum, width, height}` row for every source page of
the run. `sha256` is the digest the page binds into the run's `corpus_frame_membership`
(the Door's `computed_sha256` where it inspected the bytes, else the submitted
declaration; for a page fanned out of a container, the container's digest composed with
the page index), the field `common/runtree/store` seals.

The plan gives a quota for both `calibration` and `locked-acceptance` for **every** stratum
the catalog declares. An unnamed stratum is refused (it would drop out of gold silently);
quota `0` leaves one deliberately unsampled and visible.

- Each page's own sha256 deterministically puts it in one of the two sets, so a page cannot
  switch sets when pages are added or a shard is resplit. The frame's `seed` ranks pages
  within their stratum. A quota the partition cannot fill is refused; the sampler never
  crosses the boundary.
- The draw record retains the normalized whole-frame catalog, the plan and the selected
  sample digests, so `verify-sampling` needs no other bytes and recomputes membership rather
  than trusting a count. A hand-picked page minted as `stratified-seed`, a removed record and
  a re-described catalog all fail the replay by name.
- A drawn sample records its catalog and plan digests and no `claimed_set`; a manual pick
  carries a `claimed_set` and no catalog or plan. A record cannot claim one origin while
  carrying the other's evidence.

**Manual picks.** `ingest-manual` records the pick's `selection_basis`, page, stratum and
the picker's stated set unchanged; it never chooses a replacement. The stored `set` is
always the page-derived partition; a stated set that disagrees (a pick made before the seed
existed) is kept as `claimed_set`, not refused. The same page cannot be hand-picked twice. A
pick of a page the seed also drew is allowed (both provenance records are true), but it is
still one act's worth of custody. Manual records share the draw's directory; they are not
reconciled against draw membership but are reconciled against the draw's retained catalog by
`validate-corpus`.

## Act identities

Every act identity (instrument membership, transcription, adjudication) is checked for
shape only (well-formed, `act_`-prefixed): gold reads no Perlector act record, so it cannot
check that the act exists. `validate-corpus` proves that every use of one act identity
resolves through its sample to the same `{ordinal, sha256}` page, not that it is the page the
Perlector bound the act to.

## Transcription and adjudication

```sh
python -m gold.cli transcribe --sample S.json --act-identity act_… --transcriber NAME \
  --text-file F.txt --output T.json [--run RUN.json]
python -m gold.cli adjudicate --first T1.json --second T2.json --output A.json \
  [--adjudicator NAME --text-file F.txt] [--run RUN.json]
```

**Text rules.** The text file is UTF-8; its final newline is dropped and nothing else is
adjusted. Agreement is decided by equality, so surrounding whitespace, a CR, and anything
not in Unicode NFC are refused by name. A transcription is never blank: an unreadable act is
`[ILLEGIBLE]`, the one reserved spelling. Source text that literally says `illegible` is
written `\illegible`; a literal backslash is `\\`; a backslash before anything else is
refused. Escapes read left to right, so `\\illegible` is refused and `\\\illegible` is a
backslash followed by the word. The stored reading maps back to the ink exactly one way,
because records are immutable.

**Adjudication.** Identical readings give `agreed`, with no adjudicator (naming one is
refused). Different readings require an adjudicator and their own reading of the ink: **the
adjudicator does not choose the better transcription**, mirroring the pipeline's rule that
nothing picks among witnesses. Both transcriptions are kept unaltered inside the record, and
`outcome` is derived from them on every read.

A person's name shaped like a pipeline identity is refused wherever a person is named.

**RecordGold is not gold.** `operations/corpus/` admits that third-party corpus in its own
`reference.py` record family (one unnamed expert reading, no adjudication). It cannot
satisfy gold's two-reading custody, and forcing it in would mean inventing transcribers and
a fabricated `agreed` chain. A name check cannot catch an invented annotator, so the
boundary is the record family. Choosing the acceptance corpus is not this module's job.

## Custody

**Records and versions.** Layout records embed their source `gold-page-sample.v2` (positive
pixel `width` and `height`) and closed rectangle kinds `act`, `non-act-text`, `occlusion`
and `true-blank`; a layout needs at least one region (an empty page is `true-blank`).
Padding records embed their sample and carry rectangles plus the required
`calibrated_for_this_corpus` flag, with at least one rectangle. Every record is read only
under the exact schema version it names. v1 sample, draw, manual-pick, layout and padding
records carry no page size, so they are refused: keep their bytes unchanged and migrate them
explicitly, never in place.

**What `--run` proves.** With `--run`, `validate` re-proves a sample, layout or padding
record's page and frame facts against the run authority (whose schema and self-hash are
checked first). The run carries only a page's ordinal and sha256; `stratum`, `width` and
`height` are catalog-declared, and `validate-corpus` holds them to the draw's catalog.
Transcriptions, adjudications and memberships name their sample by digest only, so
`validate --run` refuses them; `validate-corpus --run` resolves them.

**`validate-corpus`** checks the collection:

- Records from two corpus frames, two recorded draws, a page stratified or sized two ways,
  or one `frame_digest` with differing `page_digest` or seed facts are refused. A page is
  identified by ordinal **and** sha256 (the same bytes at two ordinals are two pages).
- Every seeded sample the records reach, including the copy a layout or padding record
  embeds, must be one the retained draw produced; and every page the draw produced must
  still be present as a `stratified-seed` sample.
- A manual pick that restratifies the corpus or declares a page size the catalog does not is
  refused.
- Every transcription, adjudication and membership resolves to a sample in the same corpus.
  An adjudication must embed exactly the two stored transcriptions for its act. Two
  transcriptions by one person, a third transcription, or two adjudications establishing
  different text are refused. An act with a transcription must have its adjudication.
  Names must be NFC and are compared ignoring case. Conflicting layout or padding
  annotations for one page are refused.
- Custody is counted **per act**: a page carried by both a manual and a seeded sample does
  not get two custody chains.

`validate-corpus` proves consistency and closure among the records it sees, **not** act
coverage: gold has no inventory of which acts ought to exist, so a removed act chain leaves
nothing to contradict it. Do not cite a passing check as proof every act was recorded.

**One corpus frame at a time.** One run's sealed manifest is the frame; a corpus sharded at
its sealed shard limit is sampled and validated shard by shard. Uniting frames would need its
own seed and is not built; the refusal makes that boundary visible.

## File safety

Every input must be a regular file of at most 64 MiB, opened once without following its final
path component, with a bounded read, and refused if it changes while read. Symlinks, FIFOs,
oversized documents and huge integers are named refusals. Corpus directories follow the same
no-link rule, and two record names equal after Unicode normalization and case-folding are
refused (they would collide on default APFS).

Every writer creates its file atomically. Republishing identical bytes is reuse (so an
interrupted `sample` can be finished by the same command); different bytes under a taken
name are refused and the existing file is untouched. Writes are bound to the opened, locked
directory's inode, temporary names are unpredictable, and the link and cleanup are
directory-synced before success. A filesystem without hard links is a named refusal.
