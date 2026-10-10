# corpus

RecordGold in: a third-party expert-annotated corpus admitted, sealed, and joined
to pipeline output without ever pretending to be `gold/`.

`Teklia/DAI-CReTDHI-RecordGold-ATR` is 7,720 expert-annotated records over French
parish and civil registers (1548–1835), shipped as three parquets of text and IIIF
references — no embedded images. This package admits the RecordGold page sets already
on this machine as a reference-truth record family the Designator and Perlector can be
scored against, and scores a sealed run against it after the fact. It never
transcribes anything and never adjudicates anything; every human-custody act stays
`gold/`'s.

## What lives here

- `rows.py` — the row snapshot, `recordgold-rows.v1`. The three parquets, read once
  by a one-shot scratch converter outside this package (no `pyarrow` in
  `pyproject.toml` for 1.9 MB of metadata), sealed into one canonical, self-hashed
  JSON file. Every later module reads this file, never a parquet.
- `record_url.py` — parses each row's `record_url` (a IIIF Image API 2 crop) into
  `{identifier, region, rotation}`, refusing any host, size, rotation, quality, or
  format it does not recognise by name rather than normalising it.
- `cache.py` — `write_new_file`, the atomic create-only write the evaluation reports
  and fetch-run's canary verdict use.
- `normalization.py`, `scoring.py` — the `graphemic-v1` comparison form and the
  CER/WER scorer every evaluation here uses.
- `reference.py`, `compare.py` — the reference-record family and the
  offline IoU comparator.
- `canary.py` — a private, pass/fail check over a fetched run whose Door sealed a
  canary ledger. It reads private reference text locally, reports only stage
  booleans and named failures in a self-hashed verdict, and never places that
  text in the run tree or export. The reader is checked page by page: the readings of
  each canary page, joined in entry order, against that page's reference text, and the
  export's `canary` block must name every one of them. On a run read by page (the
  Perlector published page feeds) a held reading counts as read, since a page with any
  hold holds every entry on it while each still carries its text, and each witness is
  checked from its page Testimonium of each canary page rather than from act
  attachments, which a page-scoped witness never has. Fetch-run saves the verdict under
  the private canary root and sends one decision ping when a stage fails.

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
`private/canary/submission-manifest.json`. Copy both to the volume and pass the
`pages/` directory as `pod_run`'s `--canary-folder` and the manifest as its
`--canary-manifest`.
`--split` defaults to `train`; a checked directory with `reference-pages.json`
and digest-named images under `pages/` is also accepted for local synthetic tests.
- `local_admission.py` — the existing local sets (`recordgold_evaluation_val_v1`,
  `recordgold_production_train_v1`: `pages/`, `page_manifest.jsonl`, `gold.jsonl`,
  `fetch_receipt.json`) admitted as reference truth, every record admitted or refused
  by name in a self-hashed `recordgold-local-admission.v1` ledger with its own
  validator and loader. **Their producer is outside this repository**: the receipt's schema string is the only identity the
  material carries, so the receipt is trusted for one thing — that `gold.jsonl` and
  `page_manifest.jsonl` are the bytes it names — and everything else is measured
  against the stored pixels and the row's own `record_url`. Pass `--row-snapshot` and
  every row is also held to the sealed `recordgold-rows.v1` record, which is the only
  witness that was never in the set's own directory; no snapshot is tracked here, so
  without that flag the ledger records `row_snapshot.consulted: false` and the receipt
  is the only witness. It holds the stored page's
  measured dimensions, so it carries the forty records stated in a 180-degree IIIF
  view into the stored frame by `(W - x - w, H - y - h, w, h)` and records the URL,
  rotation, original box, carried box, page digest and dimensions of every crossing.
  That carry was checked against real pixels on 2026-09-11: the stored
  page is the upright 180-delivered view, and the flipped box is the one that cuts the
  ink `gold.jsonl` transcribes. A rotation other than `0`/`180` stays a named refusal,
  every listed page ends in exactly one outcome (`pages_by_outcome`), and `--split test`
  needs `--release-test-split` (`holdout-ledger-required`). Measured over
  the local sets on 2026-09-10: 784 of 784 validation records admitted (769 at 0, 15 at
  180) and 6,178 of 6,178 training records (6,153 at 0, 25 at 180); nothing under
  `OCR_Gold` is written.
- `evaluate.py` — the one caller of `compare_page` that builds its hypotheses from a
  real run: it pairs the act-regions the page readings established their acts over
  (`compare.load_pipeline_reading_acts`; the act-region of an `other` reading, or of an
  unplaced one, which has no rectangle, is counted in `excluded_reading_regions`, and a
  page with no reading has no region at all) with the reference boxes, reads the sealed
  Armarium export, re-digests every delivered text against
  the Archetypus record that established it (`digest_of(text)`), maps each export
  category to the scorer's response state (a held, refused, blank or excluded act is an
  empty hypothesis against its reference -- counted, never dropped, never perfect), and
  writes one validated, self-hashed `recordgold-evaluation.v3` record carrying run
  configuration digests, export digest, reference ledger digest, the splits scored, and
  the whole denominator: every
  reference record scored, unmeasured (its reading beyond the scoring profile's text
  bounds), missed or not attempted, every read act by export
  category, every unmatched pipeline act reported and not scored. **Two aggregate
  rates, each labelled**: `matched_pairs_only` is the arithmetic of the pairs the
  assignment made, which a missed act cannot move in either direction, and
  `including_missed_records` counts a missed record's reference units as deletions,
  since a missed act is worse than a poorly read one. An unmeasured record is counted
  the same way in `including_missed_records`, so a runaway reading never scores better
  than an empty one, and is left out of `matched_pairs_only`. A not-attempted record is
  in neither rate and is counted on its own. Two facts the record states are measured rather than declared:
  the fixture label is read from the export's own sealed identity (`fixture_id` against
  `submission_id`), not from a flag an operator could omit, and a named reference ledger
  is verified — every reference page must appear in it by `self_hash`. `code_ref`
  remains a declaration, and `code_ref_check` says whether it matched this checkout.

  **An act excluded with the lead's approval is scored as a total loss against its
  reference.** That is the conservative choice and it is deliberate — an approved
  exclusion is still an act whose text this pipeline did not deliver — but it means the
  aggregate is not pure model reading quality: a run with approved exclusions scores
  exactly as if those acts had been misread. `denominators.exported_acts_by_category`
  and `reference_records_scored_by_export_category` are where a reader separates the
  two.
- `exactly_once.py` — the proof metric of a run read by page. It reads the Perlector's `page-feed`, `page-reading`, `act-region`,
  `perlectio` and `page-accounting` records beside the admitted records of an
  admission ledger and their `gold.jsonl` text, and gives each gold record one
  outcome: **exactly once** (one `act` region holds at least half of it, holds no
  other gold record, and its text is read there), **merged** (a region holding it
  holds half of another gold record too), **duplicated** (two or more hold it) or
  **lost** (no act region holds it, or its text is not read in any that does).
  "Inside" is `common/page_accounting.py`'s rule under the policy the run sealed;
  a policy other than the sealed one, or an accounting sealed under another, is
  refused (`policy-mismatch`), and a perlectio that is not `perlectio.v3` is
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

  Only gold on pages the run sealed is scored: a ledger page outside the run is counted
  under `scope` (`ledger_pages_outside_run`, `ledger_records_outside_run`), never lost, so
  a proof run over part of a set is judged on its own pages; a sealed page with gold and
  no page feed is still lost. A page may be re-asked once (`attempt_ordinal` 2). Both
  readings and both accountings are kept, told apart by the reading's `attempt_ordinal`
  and the accounting's `answer_basis` (records without those fields are a page's only
  reading), and two records for one reading are refused. The gate is judged on the
  sealed final accounting -- the re-ask's on a re-asked page -- over every act region the
  page holds; `reask` reports the same records on the first reading alone (its regions
  and accounting) beside it: pages re-asked, acts recovered on the re-ask (an added act
  rule (j) holds as a duplicate of a first-reading entry is counted in `reask_duplicates`
  instead), exactly-once before and after, overall and on the re-asked pages. Parse states, the `length` finish
  rate and the 65,536-token fit describe each page's first reading; the re-ask's parse
  states are under `reask`, and prompt tokens compare every call. With `--selection`
  (`proof_pages`' `selection.json`, checked against the ledger) the pages in scope are
  the ones chosen for the run, so a chosen page the run did not seal is lost; `scope.basis`
  says which. `--gold` must be the exact `gold.jsonl` the ledger's receipt sealed, and
  each admitted record's text is the gold row matching the `text_sha256` the ledger
  holds for it (`reference-mismatch` otherwise, before any record is scored), so of two
  rows naming one record the copy admission kept is scored; `reference` names the
  ledger, the gold digest, the split and the gold rows not scored (`rows_not_scored`),
  `scope.selection_self_hash` the selection, and `run` the run id and the digest of its
  verified export (`no-export` without one). The report is a new file outside the run
  tree.

  ```sh
  .venv/bin/python -m operations.corpus.exactly_once --run-root runs --run-id <run> \
    --gold /path/to/set/gold.jsonl --ledger /path/to/admission-ledger.json \
    --out /path/outside/the/tree/exactly-once.json
  ```
- `witness_evaluate.py` — each witness scored against reference truth. By default
  (`--basis page-feed`) it scores every witness the page feed showed from its own units: a unit belongs to
  the reference record holding most of its box (at least half; a tie goes to the lower
  record id), its units on a record are joined in the witness's order and scored with the
  sealed scorer, and a record no unit lies on is an empty hypothesis. A witness that did
  not read, or whose units carry no box, gives every record an empty hypothesis by name,
  and so does a witness shown on another page of the run that this page's feed does not
  show (`witness-not-in-feed`), so the denominator is every record on every page for
  every witness of that page's roster. A routed witness belongs to the roster only of
  the pages its sealed `witness-routing` decision routed to it;
  `scoreable_cer_*` is the rate over the records a unit lay on. With no `--page-id` it
  scores every admitted page the run sealed and lists the rest under
  `reference_pages_outside_run`. `--basis page-testimonium` with one or more
  `--page-id` scores each chair's whole sealed page Testimonium against the page's
  reference acts joined in order instead.
- `reconstruction_evaluate.py` — the Coniector's reconstructions measured apart from the
  diplomatic reading, which never counts them. It reads the sealed export bundle's
  continuation joins and the reconstructions `sources.json` names, and `coniector.jsonl`;
  it reports the joins by status and reason (code joins nothing, so each is
  `not-reconstructed`) and each shown reconstruction by unit (act or join), maker, made or
  not made (by code), its departures and flags, and the characters by which its text
  differs from its delivered literals joined by one line break. Only where reference
  truth has every act of a made reconstruction (paired by IoU as `evaluate.py` pairs
  them) it scores CER and WER of the reconstruction and, on the same records, of the
  diplomatic literals, so the report states what the reconstruction changed; some or none
  of its acts is counted and not scored. An export that shows reconstructions but packaged
  no `coniector.jsonl` (JSONL not selected) has them counted with `measured: false` and
  exit 1. With `--reference-ledger`, every reference page must be one the admission
  ledger carries, byte for byte (`reference-page-not-in-ledger` otherwise), and
  `reference_ledger_verified` says whether one was named. Counts and identifiers only,
  never text.
- `proof_pages.py` — the proof-page picker. From an admitted set's ledger it takes a
  declared list (`--page-sha`, repeated) or a seeded draw (`--count N --seed TEXT`: the N
  admitted pages first by `sha256(seed:page_sha256)`), copies each image, checked against
  its digest, into `<output>/pages/`, and writes a Door-ready `submission-manifest.json`,
  the chosen pages' `reference-pages.jsonl` and a self-hashed `selection.json` naming the
  ledger, the rule and the chosen digests, which it also prints. The set must be outside
  the repository and an output inside it must be under `private/`; nothing leaves the
  machine.

## A proof run, end to end

On the Mac, before the pod run: admit the set, pick the pages, and submit
`private/proof/<name>/pages/` with its `submission-manifest.json` as the run's input.

```sh
.venv/bin/python -m operations.corpus.local_admission /path/to/recordgold_evaluation_val_v1 \
  --split val --output-dir private/corpora/recordgold/admission/val
.venv/bin/python -m operations.corpus.proof_pages \
  --ledger private/corpora/recordgold/admission/val/ledger.json \
  --count 10 --seed proof-1 --output-root private/proof/proof-1
```

After the pod run, with `RUN` the run id and `P=private/proof/proof-1`:

```sh
mkdir -p $P/reports
verbatus fetch-run --run-id $RUN --into runs --network-volume DATACENTER:VOLUME_ID
verbatus export --run-id $RUN
.venv/bin/python -m operations.corpus.exactly_once --run-root runs --run-id $RUN \
  --gold /path/to/recordgold_evaluation_val_v1/gold.jsonl \
  --ledger private/corpora/recordgold/admission/val/ledger.json \
  --selection $P/selection.json --out $P/reports/exactly-once.json
.venv/bin/python -m operations.corpus.evaluate --run-root runs --run-id $RUN \
  --reference-pages $P/reference-pages.jsonl \
  --reference-ledger private/corpora/recordgold/admission/val/ledger.json \
  --code-ref "$(git rev-parse HEAD)" --output $P/reports/evaluation.json
.venv/bin/python -m operations.corpus.witness_evaluate --run-root runs --run-id $RUN \
  --ledger private/corpora/recordgold/admission/val/ledger.json \
  --reference-pages $P/reference-pages.jsonl --output $P/reports/witnesses.json
.venv/bin/python -m operations.corpus.reconstruction_evaluate --run-root runs --run-id $RUN \
  --reference-pages $P/reference-pages.jsonl \
  --reference-ledger private/corpora/recordgold/admission/val/ledger.json \
  --out $P/reports/reconstructions.json
```

`fetch-run` runs the canary check itself when `private/canary/` exists, and `export`
takes the run `fetch-run` brought home verified (not a fetch that stopped, or one that
verified a stage by envelope only). `exactly_once` exits 1 when its gate
fails and `reconstruction_evaluate` when the reconstructions shown could not be measured;
the others write their record or refuse by name. Every report is written outside the
run tree, carries counts and identifiers rather than reference text, and gives the same
bytes for the same inputs.

## `private/` and the refusal vocabularies

The corpus tools write under `private/corpora/recordgold/`, which `.gitignore`
excludes and `config/data_handling_policy.json` names as an approved storage root.
The canary builder writes under `private/canary/` by default. Nothing here is
tracked; nothing here needs to be.

Every module in this package carries its own closed refusal set:
`rows.ROW_REFUSAL_REASONS`, `record_url.RECORD_URL_REFUSAL_REASONS`,
`reference.REFERENCE_REFUSAL_REASONS`, `compare.COMPARE_REFUSAL_REASONS`,
`local_admission.LOCAL_ADMISSION_REFUSAL_REASONS`,
`evaluate.EVALUATION_REFUSAL_REASONS`,
`exactly_once.EXACTLY_ONCE_REFUSAL_REASONS`,
`witness_evaluate.WITNESS_EVALUATION_REFUSAL_REASONS`,
`reconstruction_evaluate.RECONSTRUCTION_EVALUATION_REFUSAL_REASONS`, and
`proof_pages.PROOF_PAGES_REFUSAL_REASONS`.
Every refusal in this package is a `CorpusRefusal` whose message leads with its
reason token, dispatched by `str(error).split(":", 1)[0]` (`__init__.py`).

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
binds an `act_*` identity to bounds this project's own reading minted; a
RecordGold box was minted by Teklia's annotators, never by this pipeline, so
deriving an `act_*` from it would verify against its own bindings and
mean nothing. Reference acts are keyed instead by
`physical_act_id(physical_page_id("recordgold", "<source>/<volume>",
"<page>"), record_id)` — a `pac_` identity, disjoint from `act_*` by prefix,
minted by declaration rather than by structure. That ladder joins `source` and `volume` into one string before
minting the physical page identity, so `source` must itself be a safe single
path segment carrying no `/` — `local_admission.py` refuses such a row as
`unsafe-source-value` before a page is ever minted, and `reference.py` raises
the same name on both the build and the load path; without that screen, two
distinct `source`/`volume` splits (`"Tours/geneanet"` joined with nothing, and
`"Tours"` joined with `"geneanet/..."`) would flatten to the identical
`ppg_` identity.

## The comparator is not a picker

`compare.py` runs after a run tree is immutable, reads it read-only alongside a
reference record set, computes IoU between every pipeline act's region (the
Perlector's act-regions) and
every reference box, takes the assignment maximising total IoU under a
predeclared threshold, and writes `reference-comparison.v3` recording the whole
matrix: matched pairs, unmatched reference acts (misses, scored), and
unmatched pipeline acts (reported, never scored, because `completeness` already
says they may be legitimately out of scope). Per-act CER/WER comes from
`normalization.py`'s `graphemic-v1` profile and `scoring.py`'s bare rapidfuzz.

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
walks `pipeline/` and fails on any import of this package.

## Hold-out

`test` (758 records) is never admitted by default and is not the acceptance corpus;
it is the DAI-comparability set — the split this project's own number can honestly
be compared against Teklia's published one, with a contamination control available
(measure with `attestator_2` withheld). `val` (784 records) is the calibration and
instrument-development split. `train` (6,178 records) is a fine-tune corpus per the
lead's ruling and out of alpha measurement scope entirely.

`local_admission.py` admits `--split test` only with `--release-test-split`, that
flag is refused with any other split, and each row's own `split` is checked against
the split the set is being admitted as, so a `test`-labelled row can never enter a
`val` ledger. The sets carry their own split labels, so hold-out protection rests on
those labels being honest; the only witness from outside the set's own directory is
`--row-snapshot`, which is optional and whose file is not tracked here. A ledger
built without the snapshot says so in `row_snapshot.consulted`.

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
RecordGold — the lead ruled RecordGold in, training included. It is the reason
`test` is named the DAI-comparability set rather than an acceptance corpus,
and the reason any number this package's comparator produces against
`attestator_2` or `secondary_proposer` output needs that caveat stated
beside it, not implied. The finding, its evidence, and its limits are
recorded in `workbench/standing/RECORDGOLD_CONTAMINATION_LEDGER.md`.

## The acceptance corpus is the lead's call

Nothing this package builds decides whether RecordGold may stand in for, or
alongside, the Quebec gold corpus as the acceptance measurement; that is the
project lead's decision. Until then, Quebec mission registers with a
human-adjudicated `gold/` corpus remain the acceptance corpus, and RecordGold is a
comparability and calibration set. Because RecordGold truth was never filed where
`gold/` truth lives, that ruling is a decision about which corpus a number is drawn
from, not a schema migration.

## The crop census

`crop_census.py` counts, with no model and no network, how many pages of a
RecordGold page manifest would qualify for crops on request: a second round in
which the Perlector, having read the whole page from its capped render, is sent
up to k regions of the sealed page at native resolution. It decides whether a
paid on/off comparison is worth running; the feature itself is not built.

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
  counted up to (`--max-crops`, since the protocol seals no image ceiling; 0 for a
  page whose request is refused), and whether k reached a cap above 0. Round two is
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
