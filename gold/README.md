# Gold tooling

`python -m gold.cli sample --run RUN.json --catalog catalog.json --plan plan.json
--output-dir records/` creates a stratified, page-only human-gold sample.  The
catalog has one `{ordinal, sha256, stratum, width, height}` row for every R0 source
page.  A row's `sha256` is the digest that page binds into the run's
`corpus_frame_membership` -- the Door's `computed_sha256` where it inspected the
bytes, the submitted declaration only where it could not -- which for a raster is
the page's own digest and for a page fanned out of a container is that container's
digest composed with the page's index inside it.  One frame identity means one
field, and this is the field `common/runtree/store` seals; the catalog is checked
against it.  The plan carries a quota for both `calibration` and `locked-acceptance` for **every**
stratum the catalog declares.  A stratum the plan does not name would drop out of
gold without saying so, so an unnamed one is refused; quota `0` is how a stratum
is deliberately left unsampled, and it stays visible in the plan file.  Each
page's own sha256 -- not the frame-wide `page_digest` field -- deterministically
partitions pages into one of those two sets;
the frame's existing `seed` ranks pages within their own stratum. Keeping the
partition independent of the frame means the same page cannot switch sets when a
page is added or a shard is resplit. A quota that the partition cannot fill is
refused; the sampler never crosses the boundary.

The same command writes one `gold-sampling-draw.v2` record beside the selected
sample records. It retains the normalized whole-frame catalog, predeclared plan,
and selected sample digests, so `verify-sampling records/ --run RUN.json` needs no
unrecorded catalog or plan bytes. Selected membership is recomputed from those
retained facts; there is no stored member count to trust.

A drawn sample records the catalog and plan digests it came from, and carries no
`claimed_set`; a manual pick carries a `claimed_set` and no catalog or plan.  A
record cannot claim one origin while carrying the other's evidence.  That binding
is what `python -m gold.cli verify-sampling records/ --run RUN.json --catalog
catalog.json --plan plan.json` replays: validating one record proves its page is a
real corpus page in the set the seed assigns it, but only replaying the whole draw
shows that the *sampler* chose those pages.  A hand-picked page minted as
`stratified-seed`, a record quietly removed from the directory, and a catalog
re-described after the fact all fail the replay by name.  A `manual` record filed
in the same directory is validated but is not reconciled against the draw's
*membership*: it never claimed to be draw membership, and drawn samples and manual
picks share one directory by design.  It is still reconciled against the draw's
retained *catalog*, but by `validate-corpus` rather than here — see Custody.

`ingest-manual` accepts Tyrel's `gold-manual-pick.v2` record, which has
`selection_basis`, the bound page/stratum, and his stated set.  It records that
selection unchanged; it does not choose a replacement page.  The persisted
sample's `set` is always the page-derived partition — calibration/locked-acceptance
disjointness is enforced by construction, never by policing a human's claim — but
B1 picks are made in week one, before the R0 frame or its seed exist, so his stated
set can honestly disagree with it. That disagreement is never silently resolved
either way: it is carried unchanged as `claimed_set` alongside the true `set`, so a
predates-the-seed pick is ingested, not refused and sent back for a re-pick.
Automatically sampled records carry `claimed_set: null` (no human claim was made).
Before publishing a manual pick, the CLI reconciles it with the gold records
already beside its output path. One hand-picked page is picked once: a second pick
of the same page is refused before it can be counted twice, whether it restates the
stratum or only the wording of `selection_basis`.  A pick of a page the seed also
drew is *not* refused — the seed can honestly land on a page he chose in week one,
and refusing that would strand a real corpus with no remedy short of discarding his
recorded provenance — but that page is still one act's worth of custody, not two.
The collection rule is symmetric: one page has at most one distinct sample record
under each method, including a legacy seeded corpus whose draw record is absent;
the manual and seeded records may coexist because they preserve different true
selection provenance.

`bind-instrument` creates an append-only `gold-instrument-membership.v1` record
carrying a sample digest, an R0 act identity, and a protocol digest.

Every act identity in this module — instrument membership, transcription, and
adjudication — is checked for shape only (well-formed and `act_`-prefixed); R7a has
no act-producing stage before it in the build order, so it cannot check that the
act actually exists or rederive its page binding. Collection validation can prove
the narrower fact available here: every use of one act identity resolves through
its sample to the same `{ordinal, sha256}` page. It cannot prove that the first such
page is the page a later Designator authority would bind.

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
When the source itself literally says `illegible`, write `\illegible`; the backslash
marks source text rather than the reserved unreadability token.  A literal backslash
is written `\\`, and those two are the only escapes gold text defines — a backslash
before anything else is refused by name.  The escapes are read left to right, so
`\\illegible` is a literal backslash followed by an *unescaped* illegibility and is
refused; a backslash before the literal word is `\\\illegible`.  The point of
closing that off is that the stored reading maps back to the ink exactly one way,
and these records are immutable: an ambiguity admitted now could never be
re-recorded out of the hours that produced it.

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

**RecordGold truth cannot be filed here as gold.**  `operations/corpus/` fetches a
third-party expert-annotated corpus and gives it its own `reference.py` record
family — one unnamed expert reading, no adjudication, `provenance:
"third-party-expert-annotation"` — because it cannot satisfy this module's
two-reading custody shape.  `_person` (`gold/core.py:795-811`) catches a
pipeline identity, but it would not catch an invented Teklia annotator name —
nothing here can, which is why the boundary has to be the record family
rather than a name check.  `adjudicate` derives `outcome` from two
independently stored readings that RecordGold never has, and `validate_corpus`
refuses closure without both.  Forcing a reference record in as gold would
mean inventing two transcriber names for one text and minting a fabricated
`agreed` custody chain — the exact fabrication this module's two-reading
requirement exists to make impossible.  A reference record and a gold record
may describe the same page; they are never the same kind of record, and
neither directory is the right home for the other's.

Whether RecordGold stands in for, or beside, the Quebec gold corpus for the
GOVERNANCE 10 acceptance claim is a separate question, and it is Tyrel's, not
this module's (`operations/corpus/README.md`'s "The acceptance corpus is
Tyrel's call" gets this right; his 2026-09-01 direction on RecordGold reads
the other way and is not yet reconciled with it — see
`workbench/standing/RECORDGOLD_CONTAMINATION_LEDGER.md`).  If he rules
RecordGold in for that claim, the route is a named substitution recorded
where the acceptance corpus is chosen, never a forged entry through this
module's custody chain.

## Custody

The layout schema embeds its source `gold-page-sample.v2`, whose page carries
positive pixel `width` and `height`, and has closed
`act`, `non-act-text`, `occlusion`, and `true-blank` rectangle kinds.  The padding
schema also embeds its source sample and carries only rectangles plus the required
`calibrated_for_this_corpus` flag.  `validate` checks all schemas and self-hashes;
for a sample, layout, or padding record, pass `--run` to prove the derived page and
frame facts against the R0 authority again (an embedded sample is otherwise only
checked for internal self-consistency, not that it names a real run).
`bind-instrument` accepts the same optional `--run`.

The dimension-bearing sample, draw, manual-pick, layout, and padding schemas are
version 2. Version 1 did not carry a page size and therefore cannot honestly mean
"this rectangle lies on its page" under the new reader. Existing v1 bytes remain
immutable evidence; this tool refuses rather than silently reinterpret them.

`--run` proves the page facts R0 actually carries, which are its ordinal and
sha256.  A page's `stratum`, `width`, and `height` are not among them: R0's
`source_manifest` records neither, so those three are catalog-declared, and what
holds them honest is the catalog, not the run.  A rectangle is therefore proven
on the page **the catalog says this is**, and `validate-corpus` is where that
declaration is held to the catalog the draw was designed over.
The run authority's schema and self-hash are checked before its frame or seed is
used; an edited seed cannot silently define a different draw.

`validate-corpus records/ [--run RUN.json]` checks what no single record can. A
page's set is stable across frames by construction, but every frame has its own
seeded ranking and quota universe. Records under two different corpus frames are
therefore refused by name rather than combined into a draw nobody predeclared, as
is a page stratified or measured two ways across records, and so are two recorded
draws — two draws are two predeclared designs, and neither can speak for the
records beside it. Two records also cannot give one `frame_digest` different
`page_digest` or seed facts: the digest is one frame identity, not a file-order
choice between contradictory restatements. A page is identified by its ordinal
*and* its sha256, because
the corpus admits the same bytes at two ordinals (one page scanned twice); those
are two pages, and the same digest carrying two ordinals is not a contradiction.
Where the corpus retains a draw, every seeded sample the
records reach must be one that draw produced, *including* the copy a layout or
padding record embeds: `verify-sampling` reconciles the sample records in a
directory, so without this a page the sampler never chose could enter gold inside
an annotation and be replayed by nothing. The reverse is checked too: every page
the draw did produce must still be present as a `stratified-seed` sample, so a
page the seed genuinely chose cannot be quietly re-minted `manual` and vanish
from the seeded count while the corpus still reports itself consistent.

A retained draw keeps the whole normalized catalog, not only the selected rows, so
it also carries the predeclared stratum and pixel size of every page a manual pick
could name.  A seeded sample is reconciled against that catalog by its membership
digest; a manual one is reconciled against it here.  A hand-picked page that
restratifies the corpus the draw was designed over, or that declares a page size
the catalog does not, is refused — the first would make the stratification
unmeasurable, and the second would make "the rectangles are proven on-page"
vacuous, since every rectangle fits a page said to be enormous.

Collection validation also resolves every transcription, adjudication, and
instrument membership back to a sample in that same corpus. An adjudication
must embed exactly the two independently stored transcription records for its act;
two transcription records by one transcriber or two adjudications establishing different text
for one act are refused instead of leaving a consumer to choose by file order. An
act with any stored transcription must have its adjudication too, so deleting the
established record leaves a named partial chain rather than a corpus that still
passes. Conflicting page-layout or padding annotations for one ordinal/digest pair
are likewise refused; the same annotation facts may be carried through both a
manual and a seeded sample because both provenance records are true.
Custody is counted **per act**, not per act per sample record: an act identity binds
the page it was marked out on, so it names one act once, and a page carried by both
a manual and a seeded sample record must not thereby acquire two custody chains and
two established readings with nothing but file order between them. Conversely,
duplicate bytes at two ordinals derive different page identities and therefore
different act identities; they remain two pages and may each carry one established
reading.

`validate-corpus` proves consistency and closure among the records it can see; it
does **not** prove act coverage over each sampled page. R7a has no Designator
authority or other inventory enumerating which acts ought to exist, so removing an
entire act chain leaves no local record to contradict. The retained draw is the
narrow exception because it enumerates seeded pages, and a surviving transcription
is another because it requires its adjudication. A successful collection check must
not be cited as proof that every page act was ever recorded.

Every file this CLI reads must be a regular file at most 64 MiB. The reader opens
the file once without following its final path component, bounds the descriptor
read, and refuses if the file changes while those bytes are read. A symlink,
FIFO, oversized JSON document or transcription, and a decimal integer too large
for the interpreter are therefore named input refusals rather than redirects,
unbounded reads, blocking opens, or tracebacks. Corpus directories and their
`*.json` entries follow the same no-link rule. Two record names that compare equal
after Unicode normalization and case-folding are refused even on a case-sensitive
filesystem, because they would collapse to one pathname on default APFS.

Gold is therefore drawn **per corpus frame**: one run's sealed manifest is the
frame, and R0 shards a corpus at its sealed shard limit, so a corpus split across
shards is sampled shard by shard and its records are validated shard by shard.
Uniting several frames into one gold corpus is deliberately not built — the union
would need its own seed, and inventing one now, before any corpus of that size has
been measured, would put the disjointness property on an untested footing.  The
refusal makes the boundary visible at the moment it is reached.

The layout schema requires at least one region and the padding schema at least one
rectangle: a page with nothing on it is annotated `true-blank`, and a record that
measured nothing may not carry a calibration verdict.

Every writer creates its file atomically, so a partly written record can never
take its final name.  Republishing byte-identical content is reuse — `sample`
writes one file per page, and an interrupted draw has to be finishable by the same
command — while different bytes under a name already taken are refused and the
existing file is left untouched. Publication uses the inode of the directory that
was opened and locked, and compares device/inode identity before using a caller's
locked descriptor, so replacing its pathname cannot redirect a checked write.
Temporary names are unpredictable, existing targets are read as regular files
without following links, and both the published link and temporary-name removal
are directory-synced before success returns. A filesystem that refuses hard links
is a named refusal, not a bare traceback.
