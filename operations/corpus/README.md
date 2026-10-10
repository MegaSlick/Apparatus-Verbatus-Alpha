# corpus

RecordGold as reference truth: a third-party expert-annotated corpus admitted, sealed,
and joined to pipeline output after the fact, without ever pretending to be `gold/`.

`Teklia/DAI-CReTDHI-RecordGold-ATR` is 7,720 expert-annotated records over French parish
and civil registers (1548–1835), shipped as three parquets of text and IIIF references (no
images). This package admits the RecordGold page sets already on this machine as a
reference-record family, scores sealed runs against them, and runs the private canary
check. It never transcribes or adjudicates anything; every human-custody act stays in
`gold/`.

## Modules

| Module | What it does |
|---|---|
| `rows.py` | `recordgold-rows.v1`: the three parquets, converted once outside this package, sealed into one canonical self-hashed JSON file that every later module reads |
| `record_url.py` | parses a row's `record_url` (a IIIF Image API 2 crop) into `{identifier, region, rotation}`, refusing any host, size, rotation, quality or format it does not recognise |
| `cache.py` | `write_new_file`, an atomic create-only write |
| `normalization.py`, `scoring.py` | the `graphemic-v1` comparison form and the CER/WER scorer |
| `reference.py`, `compare.py` | the reference-record family and the IoU comparator |
| `local_admission.py` | admits a local set as reference truth |
| `proof_pages.py` | picks proof pages from an admitted set |
| `evaluate.py` | scores a run's exported acts against reference records |
| `exactly_once.py` | the proof metric of a run read by page |
| `witness_evaluate.py` | scores each witness against reference truth |
| `reconstruction_evaluate.py` | measures the Coniector's reconstructions apart from the diplomatic reading |
| `canary.py` | the private pass/fail canary check |
| `crop_census.py` | counts pages that would gain from crops on request |

Every report is written outside the run tree, carries counts and identifiers rather than
reference text, and gives the same bytes for the same inputs.

## A proof run, end to end

On the Mac, before the pod run: admit the set, pick pages, and submit
`private/proof/<name>/pages/` with its `submission-manifest.json`:

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

`fetch-run` runs the canary check itself when `private/canary/` exists. `export` takes only a
run `fetch-run` brought home fully verified. `exactly_once` exits 1 when its gate fails, and
`reconstruction_evaluate` when the reconstructions shown could not be measured; the others
write their record or refuse by name.

## Admission (`local_admission.py`)

Admits a local set (`pages/`, `page_manifest.jsonl`, `gold.jsonl`, `fetch_receipt.json`) as
reference truth. Every record is admitted or refused by name in a self-hashed
`recordgold-local-admission.v1` ledger; every listed page ends in exactly one outcome
(`pages_by_outcome`).

- **The set's producer is outside this repository.** The receipt is trusted for one thing:
  that `gold.jsonl` and `page_manifest.jsonl` are the bytes it names. Everything else is
  measured against the stored pixels and each row's `record_url`.
- `--row-snapshot` also holds every row to a sealed `recordgold-rows.v1` record, the only
  witness from outside the set's directory. No snapshot is tracked here; without it the
  ledger records `row_snapshot.consulted: false`.
- Records stated in a 180-degree IIIF view are carried into the stored (upright) frame by
  `(W - x - w, H - y - h, w, h)`, recording each crossing. Any other rotation is refused.
- A `source` value containing `/` is refused (`unsafe-source-value`), since `source` and
  `volume` are joined into one string when the page identity is minted.

## `proof_pages.py`

From an admitted ledger it takes a declared list (`--page-sha`, repeated) or a seeded draw
(`--count N --seed TEXT`: the N admitted pages first by `sha256(seed:page_sha256)`), copies
each image (digest-checked) into `<output>/pages/`, and writes a Door-ready
`submission-manifest.json`, `reference-pages.jsonl` and a self-hashed `selection.json`. The
set must be outside the repository, and an output inside it must be under `private/`.

## `evaluate.py`

Pairs the act-regions the run's page readings established (unplaced and `other` readings
counted in `excluded_reading_regions`) with the reference boxes, reads the sealed Armarium
export, re-digests every delivered text against the Archetypus record that established it,
and writes a self-hashed `recordgold-evaluation.v3` record with the run's configuration
digests, the export and reference-ledger digests, and the whole denominator.

- A held, refused, blank or excluded act is an empty hypothesis: counted, never dropped.
- **Two rates, each labelled.** `matched_pairs_only` covers the pairs the assignment made;
  `including_missed_records` also counts missed and unmeasured records' reference units as
  deletions, so a runaway reading never scores better than an empty one. Not-attempted
  records are counted on their own.
- **An act excluded with the lead's approval scores as a total loss.** That is deliberate
  (the pipeline did not deliver its text), so the aggregate is not pure reading quality;
  `denominators.exported_acts_by_category` and
  `reference_records_scored_by_export_category` separate the two.
- The fixture label is read from the export's sealed identity, and a named reference ledger
  is verified page by page. `code_ref` is declared; `code_ref_check` says whether it matches
  the checkout.

## `exactly_once.py`

The proof metric of a run read by page. It reads the Perlector's `page-feed`,
`page-reading`, `act-region`, `perlectio` and `page-accounting` records beside an admission
ledger and its `gold.jsonl`, and gives each gold record one outcome:

| Outcome | Meaning |
|---|---|
| exactly once | one `act` region holds at least half of it, holds no other gold record, and its text is read there |
| merged | a region holding it holds half of another gold record too |
| duplicated | two or more regions hold it |
| lost | no act region holds it, or its text is not read in any that does |

"Inside" is `common/page_accounting.py`'s rule under the run's sealed policy. "Text read"
means a CER of at most 20% against a holding act's reading, with no other gold record on the
page closer. A lost or merged record is **caught** only by a held finding located on it;
page-wide holds, unplaced-only catches and review flags are reported but not credited. A
page with no accounting is **unchecked**, a failure of its own.

**The gate:** at least 95% of records exactly once, no failure without a located catch, and
no page unchecked. Exit 0 only when it passes.

- Only pages the run sealed are scored; ledger pages outside the run are counted under
  `scope`. With `--selection`, a chosen page the run did not seal is lost.
- A re-asked page is judged on its final accounting; `reask` reports the first reading
  alone beside it (pages re-asked, acts recovered, duplicates, before and after).
- `--gold` must be the exact `gold.jsonl` the ledger's receipt sealed, and each record's text
  must match the ledger's `text_sha256` (`reference-mismatch` otherwise).
- Refusals include `policy-mismatch` (another policy than the sealed one) and
  `not-page-read` (a perlectio that is not `perlectio.v3`).
- It also reports merge cases, how often rule (i) `merged-detection` fired, which rules
  caught each failure, prompt tokens against the engine's `usage.prompt_tokens`, the
  `length` finish rate, pages that fit a 65,536-token context, and seconds per page with
  `--seconds-per-page` (a `{page_id: seconds}` file).

## `witness_evaluate.py`

By default (`--basis page-feed`) it scores every witness the page feed showed, from its own
units: a unit belongs to the reference record holding most of its box (at least half), a
record's units are joined in the witness's order and scored, and a record with no unit is an
empty hypothesis. A witness that did not read, has no boxes, or is absent from this page's
feed (`witness-not-in-feed`) gives every record an empty hypothesis, so the denominator is
every record for every witness on the page's roster (a routed witness belongs only to the
pages routed to it). `scoreable_cer_*` covers only records a unit lay on. `--basis
page-testimonium --page-id ...` scores each chair's whole page Testimonium instead.

## `reconstruction_evaluate.py`

Measures the Coniector's reconstructions apart from the diplomatic reading, which never
counts them. It reports the export's continuation joins by status and each shown
reconstruction by unit, maker, departures and flags, and how far its text differs from the
delivered literals. Where reference truth covers every act of a made reconstruction, it
scores CER and WER of both the reconstruction and the literals. An export with
reconstructions but no `coniector.jsonl` has them counted `measured: false`, exit 1. With
`--reference-ledger`, every reference page must be in the ledger byte for byte.

## The canary check (`canary.py`)

A private pass/fail check over a fetched run whose Door sealed a canary ledger. It reads the
private reference text locally and reports only stage booleans and named failures in a
self-hashed verdict; the text never enters the run tree or export. The reader is checked
page by page (each canary page's readings, joined in entry order, against its reference
text), and the export's `canary` block must name every canary page. On a page-read run a
held reading counts as read, and each witness is checked from its page Testimonium.
`fetch-run` saves the verdict under the private canary root and sends one decision ping when
a stage fails.

Build a canary submission from a RecordGold set (`pages/`, `gold.jsonl`,
`page_manifest.jsonl`, `fetch_receipt.json`) outside this repository:

```sh
.venv/bin/python -m operations.corpus.canary build \
  --source-dir /path/outside/this/repository \
  --page-sha <selected-page-sha256>        # repeat for each page
```

It verifies reference, digest and geometry, then writes `private/canary/pages/`,
`private/canary/reference-pages.json` and a Door-ready `private/canary/submission-manifest.json`.
Copy them to the volume and pass `pages/` as `pod_run`'s `--canary-folder` and the manifest
as `--canary-manifest`. `--split` defaults to `train`.

## Storage and refusals

The corpus tools write under `private/corpora/recordgold/` and the canary under
`private/canary/`, both gitignored approved storage. Nothing here is tracked.

Each module has a closed refusal set (`rows.ROW_REFUSAL_REASONS`,
`local_admission.LOCAL_ADMISSION_REFUSAL_REASONS`, `evaluate.EVALUATION_REFUSAL_REASONS`,
and so on). Every refusal is a `CorpusRefusal` whose message leads with its reason token
(`str(error).split(":", 1)[0]`).

## Rules that keep this honest

**Reference truth is not gold.** `gold/` requires two independently named human transcribers
and an adjudicator; RecordGold supplies one unnamed expert reading. Forcing it into `gold/`
would mean inventing transcribers and a fabricated `agreed` outcome. `reference.py` gives it
its own family, with `provenance: "third-party-expert-annotation"`,
`independent_readings: 1`, `adjudicated_by: null`, `provenance_class: "cleared_public"`.
`gold/README.md` states the same boundary from its side.

**Reference truth is records only** (`completeness: "records-only"`): RecordGold annotates
records, not everything on a page. An unmatched pipeline act (an index row, marginalia, a
note) is outside its scope and is never scored as a false positive for that alone.

**Reference acts have their own identity.** An `act_*` identity binds bounds this project's
own reading minted; a RecordGold box was not. Reference acts are keyed by
`physical_act_id(physical_page_id("recordgold", "<source>/<volume>", "<page>"), record_id)`,
a `pac_` identity disjoint from `act_*` by prefix.

**The comparator is not a picker.** `compare.py` runs only after a run tree is sealed, reads
it read-only, computes IoU between every pipeline act-region and every reference box, takes
the assignment maximising total IoU above a predeclared threshold, and writes
`reference-comparison.v3` with the whole matrix: matched pairs, unmatched reference acts
(misses, scored) and unmatched pipeline acts (reported, never scored). It returns nothing to
the pipeline and drops nothing. `pipeline/` may not import `operations.corpus` and
`operations/corpus/` may not import `pipeline/`, pinned by
`test_compare.py::test_no_pipeline_module_imports_operations_corpus`.

**Hold-out.** `test` (758 records) is never admitted by default: it is the
DAI-comparability set, where this project's number can be compared with Teklia's published
one (with `attestator_2` withheld as a contamination control). `local_admission.py` admits
it only with `--release-test-split`, refuses that flag with any other split, and checks each
row's own `split`, so a `test` row cannot enter a `val` ledger. `val` (784) is for
calibration and instrument development; `train` (6,178) is a fine-tune corpus, outside alpha
measurement.

**The DAI contamination risk.** The real roster puts two Teklia models in chairs:
`attestator_2` (`Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR`) and `secondary_proposer`
(`Teklia/YOLOv26-DAI-CReTDHI-Record-Detection`, whose card declares this dataset). Whether
either trained or validated on this corpus's exact splits is not verified. If they did, a
score against RecordGold for either chair is a model scored against its own training labels,
and the priming-delta instrument (`ARCHITECTURE.md`) would invert: a Perlector that copies
`attestator_2` would look like it reads well. Any number this package produces against those
chairs' output must state that caveat beside it.

**The acceptance corpus is the lead's call.** Until the lead decides otherwise, the
human-adjudicated `gold/` corpus of Quebec mission registers is the acceptance corpus and RecordGold is a comparability
and calibration set.

## The crop census (`crop_census.py`)

Counts, with no model and no network, how many pages of a RecordGold page manifest would
qualify for "crops on request" (a second round sending the Perlector up to k regions of the
sealed page at native resolution). It informs whether a paid comparison is worth running;
the feature itself is not built.

```sh
.venv/bin/python -m operations.corpus.crop_census /path/to/set/page_manifest.jsonl \
  --out /path/outside/the/tree/crop-census.json
```

Per page it records `native` (manifest size), `sent` (the page render at `[page_context]
maximum_edge`), `seen` (the size the Perlector row's processor resizes it to), `page_gain_bp`
(`sqrt(native area / seen area)` in basis points; an enlarged page is clamped to 10,000), `crop_native`,
`crop_seen` and `gain_bp` (one crop's sizes and its real gain), `need` and `headroom` (the
request's capacity against the row's 65,536-token context, with gold text standing in for
three witnesses), `k`, `k_cap`, `k_capped` (the most crops round two fits, counting the
round-one reply at its `max_tokens`, two chat turns and each crop's image tokens),
`reserve_clamped`, and `qualifies` (`gain_bp` at least `--min-gain` and k at least 1).

| Argument | Default | Meaning |
|---|---|---|
| `--min-gain` | 1.5 | the least linear gain worth a crop |
| `--crop-width` | 0.5 | a crop's width as a share of the page's |
| `--crop-height` | 0.125 | a crop's height as a share of the page's |
| `--max-crops` | 8 | the most crops round two may ask for |

It counts only the legible page render (`[feed] page_image = "legible"`) and refuses other
settings, malformed manifests, and crops the processor refuses on aspect ratio (exit 2, no
report). The report is sorted-key JSON with no timestamps, carrying its inputs' digests,
every page and a summary (share qualifying, gain quantiles, a histogram of k). The numbers
are estimates: round two has never been measured against the engine. Run it at more than one
crop size, since k falls as crop area grows until the crop reaches `max_pixels`.
