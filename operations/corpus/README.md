# corpus

RecordGold in: a third-party expert-annotated corpus fetched, sealed, and joined
to pipeline output without ever pretending to be `gold/`.

`Teklia/DAI-CReTDHI-RecordGold-ATR` is 7,720 expert-annotated records over French
parish and civil registers (1548–1835), shipped as three parquets of text and IIIF
references — no embedded images. This package turns that into fetched pages the
Door can admit, a reference-truth record family the Designator and Perlector can be
scored against, and a comparator that does the scoring after the fact. It never
transcribes anything and never adjudicates anything; every human-custody act stays
`gold/`'s.

## What lives here

- `rows.py` — the row snapshot, `recordgold-rows.v1`. The three parquets, read once
  by a one-shot scratch converter outside this package (no `pyarrow` in
  `pyproject.toml` for 1.9 MB of metadata), sealed into one canonical, self-hashed
  JSON file. Every later module reads this file, never a parquet.
- `plan.py` — the fetch plan, `recordgold-fetch-plan.v1`. Parses each row's
  `record_url` (a IIIF Image API 2 crop) into `{identifier, region}`, refusing any
  host, size, rotation, quality, or format it does not recognise by name rather than
  normalising it, and groups rows by the page identifier they share. Also mints each
  page's `pac_` physical identity and records the measured page count, records-per-page
  distribution, and split-overlap count under the plan's own `measurements` field —
  turning the design consult's disk estimate into a fact before a byte is fetched.
  Measured against the sealed row snapshot: 1,165 distinct pages (val 113, test 113,
  train 939), 0 cross-split pages, and 40 rows refused `unsupported-rotation-parameter`
  — against the consult's 2,200–3,100-page estimate.
- `holdout.py` — the hold-out ledger, `recordgold-holdout.v1`, built from the row
  snapshot alone: every IIIF identifier carrying a `test` record is held, and
  `refuse_held_out_page` is the predicate later units call before writing a page
  anywhere. This is the strongest of the hold-out's three layers (§ Hold-out below).
- `fetch.py`, `cache.py` (Unit 2) — the polite, resumable, never-re-fetch fetcher.
- `submission.py`, `sidecar.py` (Unit 3) — the submission builder: hard-links cached
  bytes into a Door-shaped folder, writes sidecars outside it, and invokes
  `operations/submit/submit.py`.
- `reference.py`, `compare.py` (Unit 4) — the reference-record family and the
  offline IoU comparator.

As of this commit only `rows.py`, `plan.py` and `holdout.py` exist; the fetch
protocol, comparator, and hold-out-fetcher sections below describe the shape the
later units are built to, not behaviour that runs today.

## `private/` and the fetch protocol

Everything this package writes lives under `private/corpora/recordgold/`, which
`.gitignore` already excludes and `config/data_handling_policy.json` already names
as an approved storage root — the same root the Door's own admission loop checks.
Nothing here is tracked; nothing here needs to be.

```text
private/corpora/recordgold/
  rows/recordgold-rows.v1.json        the sealed snapshot
  cache/<response-sha256>.jpg         content-addressed bodies, never re-fetched
  cache/requests/<request-key>.json   request-key -> response digest, atomic create
  info/<identifier-digest>.json       retained IIIF info.json per identifier
  submissions/<shard-id>/<source>/<volume>/<page>.jpg    IMAGES ONLY
  sidecars/<shard-id>/<source>/<volume>/<page>.json      OUTSIDE the submission folder
  ledger/fetch-plan.json  holdout.json  fetch-log.json  refusals.json
```

Per identifier: fetch `info.json` once and retain it; request the full-resolution
image (`full/full/0/default.jpg`, falling back to `max` on 400/501, and recording
which one was used — the two differ on servers that cap size, and a corpus mixing
them silently is a corpus whose boxes are wrong by a scale factor); verify the
decoded JPEG's dimensions against `info.json`'s declared `width`/`height` before
trusting a single region, because `record_url`'s `x,y,w,h` is stated in full-page
pixels and a silently downsized page makes every box wrong with nothing downstream
positioned to notice; refuse an EXIF-rotated image (the Door seals the stored raster
as the coordinate space, so a display-rotation tag would put boxes in a different
frame from the pixels); and refuse any record whose region falls outside the page.
The fetch protocol's closed refusal vocabulary is `http-error`, `non-image-body`,
`dimension-mismatch`, `exif-orientation`, `region-outside-page`,
`duplicate-page-bytes`, `unexpected-host`, `unsupported-size-parameter`,
`holdout-page`, `cross-split-page`.

The modules that exist today carry their own closed refusal sets, not that one:
`rows.ROW_REFUSAL_REASONS` (`text-sha256-mismatch`, `duplicate-record-id`,
`unknown-split`, `empty-text`, `self-hash-mismatch`, and the rest — `rows.py:53-67`),
`plan.PLAN_REFUSAL_REASONS` (`unparseable-record-url`, `unsupported-rotation-parameter`,
`unsafe-identifier-segment`, `unmintable-page-identity`,
`inconsistent-source-for-identifier`, and the rest — `plan.py:73-91`), and
`holdout.HOLDOUT_REFUSAL_REASONS` (`holdout.py:45-54`). Every refusal in this package
is a `CorpusRefusal` whose message leads with its reason token, dispatched by
`str(error).split(":", 1)[0]` (`__init__.py:30-37`).

**Politeness is not optional.** One connection, sequential, at least a one-second
delay between requests, `Retry-After` honoured, bounded exponential backoff on
429/503, the whole run stopped on the first 403, a declared `User-Agent` naming the
project and a contact, a per-run request ceiling. Stdlib `urllib.request`, an
explicit opener, bounded reads, timeouts, no cross-host redirects — no new
dependency for talking to one IIIF server politely. The request key is
`sha256(identifier || region || size || rotation || quality || format)`;
`cache/requests/<key>.json` is created atomically, so an interrupt loses at most
one in-flight body and nothing already cached is ever requested again.

## The reference/gold boundary

RecordGold truth never enters `gold/`, and this is a schema-level fact, not a
prose one. `gold/`'s custody chain requires two independently named human
transcribers and an adjudicator's own reading where they differ; RecordGold
supplies one unnamed expert reading with no adjudication. Forcing it through
`gold/`'s shape would mean inventing two transcriber names for one text and
minting a fabricated `agreed` outcome — the exact thing `gold/`'s two-reading
requirement exists to prevent. `reference.py` gives it its own family instead,
canonical and self-hashed the same way, carrying `provenance:
"third-party-expert-annotation"`, `independent_readings: 1`, `adjudicated_by:
null`, `provenance_class: "cleared_public"`. `gold/README.md` names the same
boundary from its own side, so nobody later files a reference record beside a
gold one on the strength of both being "truth".

Reference truth also does not claim to be complete. `completeness:
"records-only"` on `reference-page-truth.v1` says plainly that RecordGold
annotates records, not everything on a page — an unmatched pipeline act (an
index row, marginalia, a note; all acts under `GLOSSARY.md`) is outside
Teklia's annotation scope and must never be scored as a false positive on that
account alone.

Act identity follows the same discipline. `common/contracts/identities.py`
binds an `act_*` identity to bounds the Designator itself minted; a RecordGold
box was minted by Teklia's annotators, never by this project's own structure
pass, so deriving an `act_*` from it would verify against its own bindings and
mean nothing. Reference acts are keyed instead by
`physical_act_id(physical_page_id("recordgold", "<source>/<volume>",
"<page>"), record_id)` — a `pac_` identity, disjoint from `act_*` by prefix,
minted by declaration rather than by structure, and stable across re-fetch and
re-shard.

## The comparator is not a picker

`compare.py` runs after a run tree is immutable, reads it read-only alongside a
reference record set, computes IoU between every sealed proposal's region and
every reference box, takes the assignment maximising total IoU under a
predeclared threshold, and writes `reference-comparison.v1` recording the whole
matrix: matched pairs, unmatched reference acts (misses, scored — GOALS 1), and
unmatched pipeline acts (reported, never scored, because `completeness` already
says they may be legitimately out of scope). Per-act CER/WER reuses the sealed
instruments this project already has — `operations/spike_perlector/normalization.py`'s
`graphemic-v1` and `scoring.py`'s bare rapidfuzz — rather than a second scorer
invented for this corpus.

Why this survives GOVERNANCE 3 / hard rule 8's no-picker rule (§ Do not build a
picker): it runs only after the pipeline's own output is sealed and cannot
change; it never returns anything to the pipeline; it selects nothing about
what the pipeline read, only which reference box a given output pairs with for
scoring; and it drops nothing from either side of that pairing — a miss stays a
miss, an unmatched pipeline act stays reported. The mechanical boundary, not
just the docstring, is the import graph: `pipeline/` may not import
`operations.corpus`, and `operations/corpus/` may not import `pipeline/` — the
same one-way rule `operations/submit/` already carries — to be pinned by an
import-graph test in U4. Without that test in place this reads as a picker at review; CodeRabbit has
already flagged one picker instruction elsewhere in this repository's planning
documents, and the import-graph test is what keeps this module from being the
next one.

## Hold-out

`test` (758 records) is never fetched by default and is never the GOVERNANCE 10
acceptance corpus; it is the DAI-comparability set — the split this project's
own number can honestly be compared against Teklia's published one, with a
contamination control available (measure with `attestator_2` withheld). `val`
(784 records) is the calibration and instrument-development split, fetched by
default. `train` (6,178 records) is a fine-tune corpus per Tyrel's ruling and
out of alpha measurement scope entirely.

The hold-out is mechanical, in three layers, strongest first: `holdout.py`
derives the ledger from the row snapshot alone, before a single image is
fetched; the fetcher defaults to `--split val`, and `--split test` requires an
explicit second flag writing to a distinct root; and the submission builder
refuses any page the ledger names, by identifier, including a page that also
carries a non-held split's records (`cross-split-page` — the case where a page
cannot be used for calibration without exposing held-out material). Whether the
splits are page-disjoint or record-disjoint is measured, not assumed: U1's row
snapshot shows the three splits page-disjoint today, so `cross-split-page`
never fires against the real corpus, but the refusal stays load-bearing rather
than decorative because a future re-export is not bound by today's measurement.
Release from hold is an appended, named record — an `advance`, never a
permanent bar.

## The DAI contamination risk

The drafted real roster puts two Teklia repositories in these chairs —
`attestator_2` = `Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR` and
`secondary_proposer` = `Teklia/YOLOv26-DAI-CReTDHI-Record-Detection` — named
live in `common/chairs/model_store.py`'s materialization inventory and
drafted in `config/models.toml`'s commented roster. Neither is bound today:
the live `attestator_2` row is a local fixture identity (`source =
"local-repository"`, `license_note = "fixture identity only; no model weights
or model license apply"`) and `secondary_proposer` is `state = "absent"`. The
contamination bites the day that roster is activated, and the belief behind
it is inference, not a read of Teklia's training config: both repositories
share the `DAI-CReTDHI` lineage in their names, and `attestator_2`'s
dataset/model card carries its own fine-tuned-Qwen benchmark row against a
DAI test split (CER 9.24 / WER 21.25) — `secondary_proposer` does not even
carry `RecordGold` in its name, so the case against it is weaker still.
Whether either chair actually trained or validated on this corpus's exact
splits was never verified against Teklia's own training configuration; that
gap is recorded, not glossed over. If the inference holds, a box or text
score against RecordGold truth for either chair is a model scored against its
own training labels wearing an evaluation's name, and the parroting
instrument this project uses to detect a candidate that has not learned to
read (`ARCHITECTURE.md`'s priming delta, nuda vs primed) would invert on this
corpus for exactly that reason: `attestator_2`'s testimony would approximate
the reference, so a Perlector that copies it would look like it is reading
well. This is a risk about the corpus and the two drafted chairs, taken under
that unresolved asymmetry, not a proven fact and not an argument against
RecordGold — Tyrel ruled RecordGold in, training included. It is the reason
`test` is named the DAI-comparability set rather than an acceptance corpus,
and the reason any number this package's comparator produces against
`attestator_2` or `secondary_proposer` output needs that caveat stated
beside it, not implied. The finding, its evidence, and its limits are
recorded in `workbench/standing/RECORDGOLD_CONTAMINATION_LEDGER.md`.

## The acceptance corpus is Tyrel's call

Nothing this package builds decides whether RecordGold may stand in for, or
alongside, the Quebec gold corpus as the GOVERNANCE 10 acceptance measurement.
That is rule 1's, not a session's, and the design consult that shaped this
package recommends against it: Quebec mission registers with a human-adjudicated
`gold/` corpus remain the acceptance corpus, and RecordGold is a comparability
and calibration set until Tyrel rules otherwise. This package is built so that
ruling, whenever it comes, is a decision about which corpus a number is drawn
from — not a schema migration, because RecordGold truth was never filed where
`gold/` truth lives.
