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
- `integrate.py` (the U2/U3 seam) — turns a sealed fetch log into the `FetchedPage`
  objects `submission.py` takes, verifying each cache file against the digest its
  own log entry declares.
- `submission.py`, `sidecar.py` (Unit 3) — the submission builder: hard-links cached
  bytes into a Door-shaped folder, writes sidecars outside it, and invokes
  `operations/submit/submit.py`.
- `reference.py`, `compare.py` (Unit 4) — the reference-record family and the
  offline IoU comparator.
- `canary.py` — a private, pass/fail check over a fetched run whose Door sealed a
  canary ledger. It reads private reference text locally, reports only stage
  booleans and named failures in a self-hashed verdict, and never places that
  text in the run tree or export. Fetch-run saves the verdict under the private
  canary root and sends one decision ping when a stage fails.

Build a canary submission from an external RecordGold local set containing
`pages/`, `gold.jsonl`, `page_manifest.jsonl`, and `fetch_receipt.json`:

```sh
.venv/bin/python -m operations.corpus.canary build \
  --source-dir /path/outside/this/repository \
  --page-sha <selected-page-sha256>
```

Repeat `--page-sha` for each selected page. The command verifies reference,
digest, and geometry before writing `private/canary/pages/`,
`private/canary/reference-pages.json`, and a Door-ready
`private/canary/submission-manifest.json`. Pass the `pages/` directory as the
Boot B canary folder and the manifest as its canary manifest.
`--split` defaults to `train`; a checked directory with `reference-pages.json`
and digest-named images under `pages/` is also accepted for local synthetic tests.
- `local_admission.py` — the existing local sets (`recordgold_evaluation_val_v1`,
  `recordgold_production_train_v1`: `pages/`, `page_manifest.jsonl`, `gold.jsonl`,
  `fetch_receipt.json`) admitted as reference truth, every record admitted or refused
  by name in a self-hashed `recordgold-local-admission.v1` ledger with its own
  validator and loader. **Those sets were not written by `fetch.py` and their producer
  is outside this repository**: the receipt's schema string is the only identity the
  material carries, so the receipt is trusted for one thing — that `gold.jsonl` and
  `page_manifest.jsonl` are the bytes it names — and everything else is measured
  against the stored pixels and the row's own `record_url`. Pass `--row-snapshot` and
  every row is also held to the sealed `recordgold-rows.v1` record, which is the only
  witness that was never in the set's own directory; no snapshot is tracked here, so
  without that flag the ledger records `row_snapshot.consulted: false` and the receipt
  is the only witness. It holds the stored page's
  measured dimensions, so it carries the forty records stated in a 180-degree IIIF
  view into the stored frame by `(W - x - w, H - y - h, w, h)` and records the URL,
  rotation, original box, carried box, page digest and dimensions of every crossing;
  `plan.py`'s fetch-time parser still refuses those rows because it has no dimensions
  to convert with. That carry was checked against real pixels on 2026-09-11: the stored
  page is the upright 180-delivered view, and the flipped box is the one that cuts the
  ink `gold.jsonl` transcribes. A rotation other than `0`/`180` stays a named refusal,
  every listed page ends in exactly one outcome (`pages_by_outcome`), and `--split test`
  needs `--release-test-split` exactly as the fetcher does, refused under the fetcher's
  own name (`holdout-ledger-required`) since it is the same condition. Measured over
  the local sets on 2026-09-10: 784 of 784 validation records admitted (769 at 0, 15 at
  180) and 6,178 of 6,178 training records (6,153 at 0, 25 at 180); nothing under
  `OCR_Gold` is written.
- `evaluate.py` — the one caller of `compare_page` that builds its hypotheses from a
  real run: it reads the sealed Armarium export, re-digests every delivered text against
  the Archetypus record that established it (`digest_of(text)`), maps each export
  category to the scorer's response state (a held, refused, blank or excluded act is an
  empty hypothesis against its reference -- counted, never dropped, never perfect), and
  writes one validated, self-hashed `recordgold-evaluation.v1` record carrying run
  configuration digests, export digest, reference ledger digest, the splits scored, and
  the whole denominator: every
  reference record scored, missed or not attempted, every proposal region by export
  category, every unmatched pipeline act reported and not scored. **Two aggregate
  rates, each labelled**: `matched_pairs_only` is the arithmetic of the pairs the
  assignment made, which a missed act cannot move in either direction, and
  `including_missed_records` counts a missed record's reference units as deletions,
  since a missed act is worse than a poorly read one. A not-attempted record is in neither rate and
  is counted on its own. Two facts the record states are measured rather than declared:
  the fixture label is read from the export's own sealed identity (`fixture_id` against
  `submission_id`), not from a flag an operator could omit, and a named reference ledger
  is verified — every reference page must appear in it by `self_hash`. `code_ref`
  remains a declaration, and `code_ref_check` says whether it matched this checkout.

  **An act excluded with Tyrel's approval is scored as a total loss against its
  reference.** That is the conservative choice and it is deliberate — an approved
  exclusion is still an act whose text this pipeline did not deliver — but it means the
  aggregate is not pure model reading quality: a run with approved exclusions scores
  exactly as if those acts had been misread. `denominators.exported_acts_by_category`
  and `reference_records_scored_by_export_category` are where a reader separates the
  two.
- `exactly_once.py` — the proof metric of a run read by page (`reading_unit =
  "page"`). It reads the Perlector's `page-feed`, `page-reading`, `act-region`,
  `perlectio` and `page-accounting` records beside the admitted records of an
  admission ledger and their `gold.jsonl` text, and gives each gold record one
  outcome: **exactly once** (one `act` region holds at least half of it, holds no
  other gold record, and its text is read there), **merged** (a region holding it
  holds half of another gold record too), **duplicated** (two or more hold it) or
  **lost** (no act region holds it, or its text is not read in any that does).
  "Inside" is `common/page_accounting.py`'s rule under the policy the run sealed;
  a policy other than the sealed one, or an accounting sealed under another, is
  refused (`policy-mismatch`), and a perlectio that is not `perlectio.v2` is
  refused (`not-page-read`); so is an admitted gold record with no text to
  measure. "Text read" is this tool's own measure, stricter than the accounting's
  rule (e): the gold text's character error rate against a holding act's reading
  is at most 20%, and no other gold record on the page is closer to that reading.
  A lost or merged record is **caught** only by a held finding located on it
  (names a placed region or a box overlapping it, or a unit such a region cites);
  a hold elsewhere on the page does not catch it. Page-wide catches (a hold naming
  no region, box or unit) and unplaced-only catches (one reaching the record only
  through an unplaced region) are reported beside it and not credited. A page
  with no accounting is **unchecked**, a failure of its own. Beside the outcomes: records split by merge case (a detector record or
  another witness's boxed unit holding two gold records), how often rule (i)
  `merged-detection` fired on regions that truly hold two gold records and on
  those that hold one, which rules caught each failure, hold codes, admitted
  prompt tokens against the engine's `usage.prompt_tokens`, the `length` finish
  rate, the pages that would fit a 65,536-token context, and seconds per page when
  `--seconds-per-page` names a `{page_id: seconds}` file (the tree records no
  durations). Rule (i) cannot see a detector record that itself merged two
  entries the reader read as one act; the merged outcome here measures it. The
  gate is at least 95% of records exactly once, no failure without a located catch
  and no page unchecked; the exit status is 0 only when it passes. The JSON report and the
  printed summary carry counts and identifiers, never text.

  ```sh
  .venv/bin/python -m operations.corpus.exactly_once --run-root runs --run-id <run> \
    --gold /path/to/set/gold.jsonl --ledger /path/to/admission-ledger.json \
    --out /path/outside/the/tree/exactly-once.json
  ```

All four units exist as of this commit; the fetch protocol, comparator, and
hold-out sections below describe behaviour that runs, not a shape still to be
built.

## `private/` and the fetch protocol

The RecordGold fetch tools write under `private/corpora/recordgold/`, which
`.gitignore` excludes and `config/data_handling_policy.json` names as an approved
storage root. The canary builder writes under `private/canary/` by default.
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

Every module in this package carries its own closed refusal set, not that one:
`rows.ROW_REFUSAL_REASONS`, `plan.PLAN_REFUSAL_REASONS`,
`holdout.HOLDOUT_REFUSAL_REASONS`, `cache.CACHE_REFUSAL_REASONS`,
`fetch.FETCH_REFUSAL_REASONS` plus `fetch.FETCH_RUN_REFUSAL_REASONS` (a second,
run-level set — a request-ceiling or 403-stop refusal never reaches a fetch-log
entry, so it cannot share the per-page set), `integrate.INTEGRATE_REFUSAL_REASONS`,
`submission.SUBMISSION_REFUSAL_REASONS`, `sidecar.SIDECAR_REFUSAL_REASONS`,
`reference.REFERENCE_REFUSAL_REASONS`, `compare.COMPARE_REFUSAL_REASONS`,
`local_admission.LOCAL_ADMISSION_REFUSAL_REASONS`, and
`evaluate.EVALUATION_REFUSAL_REASONS`.
Every refusal in this package is a `CorpusRefusal` whose message leads with its
reason token, dispatched by `str(error).split(":", 1)[0]` (`__init__.py`).

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
re-shard. That ladder joins `source` and `volume` into one string before
minting the physical page identity, so `source` must itself be a safe single
path segment carrying no `/` — `plan.py` refuses such a row per-row as
`unsafe-source-value` before a page is ever minted, and `reference.py` raises
the same name on both the build and the load path; without that screen, two
distinct `source`/`volume` splits (`"Tours/geneanet"` joined with nothing, and
`"Tours"` joined with `"geneanet/..."`) would flatten to the identical
`ppg_` identity.

## The comparator is not a picker

`compare.py` runs after a run tree is immutable, reads it read-only alongside a
reference record set, computes IoU between every sealed proposal's region and
every reference box, takes the assignment maximising total IoU under a
predeclared threshold, and writes `reference-comparison.v1` recording the whole
matrix: matched pairs, unmatched reference acts (misses, scored), and
unmatched pipeline acts (reported, never scored, because `completeness` already
says they may be legitimately out of scope). Per-act CER/WER reuses the sealed
instruments this project already has — `operations/spike_perlector/normalization.py`'s
`graphemic-v1` and `scoring.py`'s bare rapidfuzz — rather than a second scorer
invented for this corpus.

Why this is not a picker:
it runs only after the pipeline's own output is sealed and cannot
change; it never returns anything to the pipeline; it selects nothing about
what the pipeline read, only which reference box a given output pairs with for
scoring; and it drops nothing from either side of that pairing — a miss stays a
miss, an unmatched pipeline act stays reported. The mechanical boundary, not
just the docstring, is the import graph: `pipeline/` may not import
`operations.corpus`, and `operations/corpus/` may not import `pipeline/` — the
same one-way rule `operations/submit/` already carries — pinned by
`test_compare.py::test_no_pipeline_module_imports_operations_corpus`, which
walks `pipeline/` and fails on any import of this package. CodeRabbit has
already flagged one picker instruction elsewhere in this repository's planning
documents; that test is what keeps this module from being the next one.

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
explicit second flag, `--release-test-split`, writing to a distinct root —
that flag releases the held split alone and is refused with any other
`--split`, and hold-out enforcement now holds for every split it is on for
(not just `val`), so a ledger is required whichever non-held split is being
fetched; and the submission builder
refuses any page the ledger names, by identifier, including a page that also
carries a non-held split's records (`cross-split-page` — the case where a page
cannot be used for calibration without exposing held-out material). Whether the
splits are page-disjoint or record-disjoint is measured, not assumed: U1's row
snapshot shows the three splits page-disjoint today, so `cross-split-page`
never fires against the real corpus, but the refusal stays load-bearing rather
than decorative because a future re-export is not bound by today's measurement.
Release from hold is an appended, named record — an `advance`, never a
permanent bar.

**The local-admission route carries one of those three layers, and it is worth
naming which.** `local_admission.py` mirrors the second layer exactly — `--split
test` requires `--release-test-split`, that flag is refused with any other
split, and each row's own `split` is checked against the split the set is being
admitted as, so a `test`-labelled row can never enter a `val` ledger. It does not
consult `holdout.py`'s ledger: the sets it reads carry their own split labels and
were not produced by the fetcher, so there is no plan to reconcile them against.
Hold-out protection on this route therefore rests on those labels being honest,
and the only witness from outside the set's own directory is `--row-snapshot`,
which is optional and whose file is not tracked here. That is a deliberate
limit, not an oversight, and a ledger built without the snapshot says so in
`row_snapshot.consulted`.

## The DAI contamination risk

The real roster (`config/models-real.toml`) puts two Teklia repositories in
these chairs — `attestator_2` = `Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR`
and `secondary_proposer` = `Teklia/YOLOv26-DAI-CReTDHI-Record-Detection`, DAI's
own record detector — both configured and both named in
`common/chairs/model_store.py`'s materialization inventory. The fixture roster
(`config/models.toml`) binds neither. `attestator_2`'s dataset/model card
carries its own fine-tuned-Qwen benchmark row against a DAI test split (CER
9.24 / WER 21.25), and the detector's card at its pinned revision
(`0c57f057…`) declares `datasets: Teklia/DAI-CReTDHI-RecordGold-ATR` in its
own metadata and reports train/val/test figures of 1132/136/139 images.
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

## The crop census

`crop_census.py` counts, with no model and no network, how many pages of a
RecordGold page manifest would qualify for crops on request: a second round in
which the Perlector, having read the whole page from its capped render, is sent
up to k regions of the sealed page at native resolution. It decides whether a
paid on/off comparison is worth running; the feature itself stays off
(`[feed] crops = "off"`).

```sh
.venv/bin/python -m operations.corpus.crop_census /path/to/set/page_manifest.jsonl \
  --out /path/outside/the/tree/crop-census.json
```

Per page it records:

- `native`: the manifest's `width` and `height`, taken to be the sealed page's
  (`local_admission.py` checks them against the decoded pixels); no image is
  opened;
- `sent`: the page render the page request embeds, at `[page_context]
  maximum_edge` (`common.page_render.render_size`, the rule the renderer uses);
- `seen`: the size the Perlector row's processor resizes that render to
  (`common.request_capacity.smart_resize`, with the row's `min_pixels`,
  `max_pixels`, `patch_size` and `merge_size`; the token count is the
  processor's `image_grid_thw.prod() // merge_size**2`);
- `page_gain_bp`: how many times finer, linearly, native pixels are than those
  seen, `sqrt(native area / seen area)`, in basis points; a page the processor
  enlarges gains 1, since enlargement adds no detail;
- `crop_native`, `crop_seen` and `gain_bp`: one crop's native size, the size the
  processor resizes it to, and the gain the chair actually gets from it —
  `page_gain_bp` scaled by the crop's own seen-over-native factor, never above
  one, so a crop the processor shrinks gains less;
- `need` and `headroom`: the page request's capacity record against the sealed
  row's 65,536-token context, built as `perlector_request_fit.py` builds it (the
  sealed feed, the page's gold text standing in for three witnesses, Surya lines
  estimated from it);
- `k`, `k_cap` and `k_capped`: the most crops round two fits, the cap it was
  counted up to (`--max-crops`, or fewer if more would take the request past the
  protocol's `max_images`), and whether k reached a cap above 0. Round two is
  the page request as admitted, plus the request's `max_tokens` for the
  round-one reply carried in context (the most the engine lets that reply run,
  so no real reply leaves fewer crops than k), two more chat turns
  (`CHAT_TURN_TOKENS` each, for the carried reply and the new request) and k
  crops, each charged its image tokens (`request_fits`) and `CHAT_IMAGE_TOKENS`,
  answered within the page's answer reserve. Wording that asks for the crops is
  not charged beyond those turns;
- `reserve_clamped`: whether the page's answer reserve was clamped to the page
  cap (`page_max_tokens`), so round two's answer is reserved the full cap; it
  does not bear on k, which carries the round-one reply at its `max_tokens`;
- `qualifies`: `gain_bp` at least `--min-gain` and k at least 1. A page whose
  request is refused at 65,536 tokens has k 0 and does not qualify.

The census counts only the legible page render (`[feed] page_image =
"legible"`) and refuses any other setting. It refuses a manifest that is not
UTF-8 JSON lines, a line without a unique `page_id`, positive integer `width`
and `height`, or a `records` list of objects each with a four-integer `bbox`
and a `text`, and a crop the processor refuses on aspect ratio; a refusal exits
2 and writes no report.

| Argument | Default | Meaning |
|---|---|---|
| `--min-gain` | 1.5 | the least linear gain worth a crop |
| `--crop-width` | 0.5 | a crop's width as a share of the page's |
| `--crop-height` | 0.125 | a crop's height as a share of the page's |
| `--max-crops` | 8 | the most crops round two may ask for |

The default crop is half the page wide and an eighth tall, a few lines of an
act; k falls as the crop's area grows until the crop reaches the processor's
`max_pixels`, where its cost levels off, so the census is worth running at more
than one crop size. The report is sorted-key JSON with no timestamps, carrying
its inputs (the manifest's digest, the digests of the three configs it reads and
of the page-prompt builder), every page and a summary: the share of pages
qualifying, gain quantiles (nearest rank, as `{q, value}` pairs), a histogram
of k, how many pages reached their cap and how many reserves were clamped. The
printed summary carries counts only, and says they are estimates: gold text
stands in for the witnesses and round two has never been measured against the
engine.
