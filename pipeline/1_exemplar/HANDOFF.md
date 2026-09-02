# Exemplar — handoff

The Exemplar is the immutable source of pixels for the rest of the run. The door
writes its admissions into `1_exemplar/`; the Exemplar then seals one `kind="page"`
outcome for every distinct admitted *origin* — not for every submitted ordinal,
because page identity binds the immutable origin bytes and two rows carrying the
same bytes are one page, sealed once and citing both rows in `submission_rows`.
The census still carries a row per submitted ordinal, so nothing submitted goes
unaccounted. It also seals one self-hashed `kind="seal"` corpus census. No later
stage re-renders a source container.

All paths below are existing RunTree shapes: `run.json`, artifacts, content-addressed
blobs, manifests, and approval receipts. This stage invents no separate render or
inventory directory.

## Stage-completion seal

Before this producer's final manifest it publishes one `decode-environment` and
one `stage-seal`, or reuses both on a byte-identical retry. The seal witnesses
this pass's disk inventory and blob contents, and binds the exact decode-environment
bytes, run `config_digest` and `register_digest`, and `(kind, outcome)` census. An exit
held after publishing stage evidence seals it (holds remain in its census); a
pass that never reaches its seal does not seal, whether it was held or refused
before publishing stage evidence or closed fatally after publishing it, so the
successor correctly refuses the missing boundary. Every difference in decoders,
platform, machine, `decode_paths_used`, and `produced_pixels` is reported by
field or decoder name. A valid difference is report-only and never refuses;
Unit 17 owns any fatal policy.

Seals are compared as the SET the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read,
when any named seal is no longer on disk. Ordinals are the contiguous run 1..N,
so removing the latest leaves a prefix that still looks whole — and the earlier
statement would then answer for a boundary it never witnessed.

A refused Door is that second case: it publishes its refusal report and its
duplicate report, announces both, and only then do `require_no_duplicate_sources`
and `require_some_admitted` raise — so the evidence is on disk and no `stage-seal`
is, and the Exemplar's "predecessor door has no stage-seal" names a refused
submission rather than a missing file.

**That sentence now holds on a real submission too, and it was a gap before it
did.** Until the shared constructor landed, `run.py` built its real-ingress
`StageContext` by hand instead of going through `common.stage.open_context` —
the fixture/scenario binding it exists to check has nothing to compare on a real
run — and `verify_predecessor_seal` was called from `open_context` alone, so a
real run whose Door refused still sealed its Exemplar pages when the programs
were driven one at a time; only `pipeline/orchestrator/run.py::invoke`, which
refuses any stage exit outside complete/held/halted, stood between a refused
Door and a sealed corpus. The gap is closed here: the Exemplar, the Ink Map and
the Designator all open through `common.stage.open_stage_context`, which decides
the route from one read of the run authority and asks for the predecessor's
completion seal on both routes, in the same order, before anything writes. A
hand-driven Exemplar over a Door that never sealed its boundary now refuses with
"predecessor door has no stage-seal" and leaves the tree byte-identical
(`test_exemplar_seal.py`'s
`test_a_real_ingress_run_whose_door_refused_seals_no_exemplar_page` and
`test_a_real_ingress_run_whose_door_sealed_still_opens_the_exemplar` pin both,
and `test_door.py`'s
`test_a_real_submission_holding_one_scan_twice_exits_fatal_before_it_completes`
drives the refusing Door that leaves that state). Two consequences ride the same
change: the real Exemplar's `scenario` is `REAL_SCENARIO`, never the unchecked
argv value it used to store, and its context carries the roster and the sealed
digest map like every later stage's, checked name by name against the run before
the seal check.

**A second, unpinned ordering moved with the same commit.** The deleted real-route
`_open` verified the corpus register and read the sealed snapshot before doing
anything else — ahead of the fixture/scenario/registry/binding work `open_context`
does first on the fixture route. `open_context` runs those two checks last, so
opening through the shared constructor moved that ordering onto the real route too:
a run with both register drift and an unloadable fixture now names the fixture
first. Nothing pinned depends on the old order — the drift test's fixture is sound —
so this is recorded rather than reverted.

Door and Exemplar share `1_exemplar/` for evidence but retain separate producer
inventories (`manifest-door.json` and `manifest.json`), so neither can erase the
other's stored deleted-seal trigger.

## Input and filename ledger

For a real submission, `operations/submit/submit.py` first creates a canonical,
self-hashed `submission-manifest.v1`. It is the filename ledger: each original
relative filename is bound to its SHA-256 and byte count before a copy moves.

The real door requires that ledger with `--submission-manifest`. It verifies its
self-hash, compares the post-transfer folder to it, and extends `run.json`'s
self-hashed `source_manifest` rather than making a second inventory. A source row
has:

```text
ordinal               stable page ordinal
relative_path         original filename / citation link
sha256                original source-file digest from the ledger
bytes                 original source-file size from the ledger
ledger_sha256         the one submission-manifest self-hash for this set
container_page_index  present for every fanned source page/frame, zero-based
```

Rows repeat a container's filename/digest for its fanned-out pages. The Exemplar
reconstructs the unique file rows and requires them to reproduce the sealed
`ledger_sha256`. A changed copy creates a `digest-mismatch` alarm under the original
filename; a source never silently drops.

Declared synthetic fixtures remain the only ledger-free route. They carry the
same core source rows but not a real filename ledger. Neither route needs an
approval-record artifact — cut 2026-08-09, see "Data handling and scope" below.

## Decoder routes and alarms

`admission.py` derives how each detected format is read, never a policy
decision to decline it:

- `admit-or-fan-out`: Pillow decodes the source pixels and a one-frame raster is
  sealed unchanged, as its own original bytes. If its decoder reports multiple
  frames, every frame fans out to its own ordinal and is sealed as one lossless PNG
  page. This is every raster format, TIFF included.
- `render-pages`: a format that is always a document of pages — PDF alone, enforced
  by the code-owned route. The door assigns stable ordinals and seals one
  lossless PNG per page.

PNG, JPEG, TIFF, PDF, GIF, BMP, WebP, HEIC and an unknown signature all receive a
decoder attempt by bytes, not extension. A valid image the installed readers do not
yet understand becomes a named `unsupported-variant` or `unrecognized-format`
pipeline alarm, not `refused-format`—that enum member no longer exists. JPEG bytes
after EOI are retained. TIFF, including ordinary multi-page TIFF, BigTIFF, and the
LZW, Deflate, PackBits and CCITT compressions flatbed scanners produce, fans out; a
single-page TIFF keeps its own bytes and is never re-encoded.

HEIC/HEIF is decoded by the pinned `pillow-heif` plugin, including an iPhone-native
single-frame source that seals its original bytes unchanged. AVIF remains Pillow's
native decoder route and is named separately from HEIC; generic HEIF brands are not
mislabelled as either codec. Decoder and bundled libheif versions are bound into the
Door execution recipe, and downstream crop decoding registers the same plugin.

PDF is rendered as a whole page with PDFium at the door. Its visible content stream,
text, vectors, images, rotation, annotations, and initialized form appearances are
painted into the sealed pixels; embedded-image extraction is not used.

`config/pdf_render.toml` supplies the **300-DPI default**, and
`--pdf-target-dpi` overrides it for one run. The chosen value is sealed explicitly
in `run.json`; each page's `render_contract` records the configured target, the
code-bounded target, and the whole `effective_dpi` actually used. Pixel/byte caps and
the 72-DPI floor stay in code. **300 rests on geometry, not on measured accuracy**:
it was chosen on Tyrel's instruction 2026-08-05 from line pitch and x-height against
real material, and because a 400-DPI page exceeds the reading models' own resize
ceiling while costing 1.78x the pixels to render, store and keep until export. It has
still never been checked against reading accuracy on his approved real sample, which
GOVERNANCE 9 asks for before scale. The reasoning is in `config/pdf_render.toml`'s
own header.

## Door `kind="admission"`

There is one admission artifact per source ordinal, whether its outcome is
`"admitted"` or `"refused"`. Its payload always carries:

```text
ordinal, declared_path, declared_sha256
declared_bytes, ledger_sha256             (real ledger rows)
```

An admitted payload additionally has `sha256`, `stored_at`, and `geometry`. A page
render also has:

```text
rendered_from = {
  container_format, container_sha256, container_page_index,
  render_contract
}
```

`render_contract` records the renderer/version, exact page index, lossless PNG or
TIFF output, geometry, and PDF-specific pixel choices. It is a complete explanation for
why a sealed rendered digest differs from the source container digest. Only the
PDFium whole-page renderer forces RGB; the raster fan-out renderer preserves an
already-PNG-legal source mode (grayscale, bilevel, LA, RGBA, 16-bit) unchanged
rather than converting it, and retains `I`/`F` high-precision samples in TIFF.
When a PDF page is memory-capped below the provisional run target, its sealed page
also names configured/resolved/effective DPI and the shortfall explicitly.

A refused payload has `reason`, whose prefix is one of the closed alarm codes:
`empty`, `unreadable`, `too-large`, `unrecognized-format`, `corrupt`,
`unsupported-variant`, or `digest-mismatch`. The artifact retains the
filename; a terminal is only presentation.

Byte-identical submitted sources are not *per-source* refusals. Each ordinal is
still admitted and keeps its filename link, may reuse the same content-addressed
blob, and the Door seals a private `duplicate-report` naming the first observed
ordinal/path and operator-visible duplicate counts. The **run** is then refused
whole once that report exists — see the merged-page section below — because two
files carrying one page identity is a submission the pipeline cannot read
correctly, not a bad file. Two byte-identical *pages inside one container* are a
different thing entirely and are never touched by this: the report groups by
declared filename, so a scanned volume's blank pages stay two pages.

## Exemplar `kind="page"` and corpus seal

For an admitted source the Exemplar writes a `sealed` page whose identity binds the
**immutable origin and the transform** — never the sealed-byte digest and never the
manifest ordinal. The origin is `{kind: "source", sha256}` for bytes admitted as they
arrived, and `{kind: "container-page", container_sha256, container_page_index,
render_contract}` for a rendered page, because rendered bytes are a derivative and
the sealed container is what they came from. The transform is `{operation: "whole"}`
here; splitting is the Designator's business. Inserting a manifest row therefore
cannot rename an existing page.

Its payload retains `declared_path`, `declared_sha256`, `source_sha256`,
`image_path`, and the ledger facts; rendered pages also retain `rendered_from`. It
also carries `submission_rows`, the ordinal-sorted set of submitted rows this page
discharges — ordinarily one. Two rows carrying identical bytes derive one
`page_id`, so the Exemplar seals one page citing both rather than publishing the
same identity twice, and `common/exemplar_boundary.sealed_submission_rows` is where
every consumer reads that set. The top-level `ordinal` and filename facts describe
one of those rows and must agree with it. A refused admission becomes an Exemplar
`refused` page outcome with the same original filename/digest and reason.

**A submission that would merge two files into one page is refused at the Door,
before any of this happens.** `door.py::require_no_duplicate_sources` raises after
the duplicate report is sealed and announced and before the Door seals its own
boundary, naming the submitted ordinals of every group — ordinals only, never
filenames, because the message goes to a terminal and the sealed report is where
the filenames belong. The whole submission is refused and no file is dropped:
choosing which copy to discard is an automated exclusion, which GOVERNANCE reserves
to Tyrel, and choosing to read the merged page once would decide silently whether
identical bytes are one page shot twice or an export that wrote one scan under two
names. There is deliberately no `--allow-duplicate-sources`. The remedy the message
names is a re-submission whose `--submission-manifest` names each distinct scan
once.

**A merged page is still refused at the next boundary, by name**, and that stays.
`verify_exemplar_corpus_seal` guards the sealed shape rather than one route into
it, so a merged page record reaching a consumer another way — a future producer, a
repaired tree, a caller that never passed a Door — is refused on its own merits
rather than depending on the Door having run. Every stage behind the Exemplar still
keys its work by submitted ordinal and would mint each act on such a page twice.

The operator consequence has changed shape: the same scan under two filenames —
routine in archive exports — now stops at the stage that read the filenames, with a
report naming them, instead of producing a green Exemplar and then a fatal
Designator complaining about an ordinal that was never lost. **What lifts the Door
refusal is a decision, not a code change**: consumers processing merged pages once
per identity is tractable (`sealed_submission_rows` was built for it; the census is
one row per ordinal and `expected_refs` is a set, so only the Designator's
`page_records` keying by ordinal breaks), but it would leave the operator with a run
that silently reads one page where two files were submitted. Whether the pipeline
may make that call automatically is Tyrel's, not a session's.

The one `kind="seal"`, subject `corpus-seal`, is self-hashed and has one census row
per submitted ordinal — per *ordinal*, not per page, so a merged page contributes a
row for each of its submissions and nothing submitted goes unaccounted. Each row
includes outcome, page identity or null, sealed-byte digest or null, original
filename, original digest, and real-ledger facts where applicable. Its inputs
reference every Exemplar page artifact.

Before any design work, `pipeline/2_designator/run.py` independently reconciles the
source manifest, every page outcome, the seal's rows, and the seal's input
references. A missing or changed page fails at that first boundary: an erased page
that a corpus-seal input still names is rejected by its exact artifact path, and a
reconcilable page census names the original filename from the source ledger.
For a sealed page, both the Designator and final Armarium boundary recheck the Door
admission and exact content-addressed pixel blob before they crop or export; no
later stage may turn altered pixels into new evidence. The Armarium repeats the
filename/digest linkage and any `container_page_index` in `export.pages` and in
every delivered crop's `source_regions`, so an individual output can be matched to
all originals it used without guessing from an ordinal.

## Split derivative pages (Unit 7)

A submitted frame that Unit 5's decision manifest gives a split for fans out to one
ordinal per declared part **before** `RunTree.create`, so `source_manifest` is the
post-split page denominator and `require_corpus_frame_shard` counts post-split
pages. `container_page_index` is the index of a page within whatever divided its
source — a PDF or TIFF page index, or a split part index.

The JPEG master is never re-encoded. Its untouched bytes are stored under their own
digest as the admission's `parent_frame`, and the PNG page is a derivative that
inputs both its own pixels and that master. When an exact no-op over an already
deterministic PNG gives both roles the same content address, the admission carries
that reference once rather than violating the artifact contract by double-counting
it. `common/exemplar_boundary.py` re-derives the page from the master's bytes plus
the recorded transform and compares byte for byte, so the pixels are provably the
master's.

**What the recorded render contract asserts, and what it does not.**

- `source_mode` and `source_bands` are the master's own Pillow mode and bands, and
  `output.color_mode` is read out of the encoded PNG's own header. The record
  therefore states what the page's samples actually are, not what the image was on
  the way into the encoder. The Exemplar boundary independently compares those
  fields, the output codec/mode and dimensions, page index, mode-transform label,
  and deterministic-encoder identity against its re-render; correct pixels under a
  forged renderer record are refused too. The embedded row's closed shape, triage
  mode, confidence, override, cluster identity, and resolved actor are independently
  validated even when an attacker recomputes the row's self-digest.
- `colour_mode: "keep"` is a declaration that no lossy colour or sample conversion
  happens. The exact admitted set is the deterministic encoder's direct PNG modes
  (`1`, `L`, `LA`, `RGB`, `RGBA`) plus palette `P`, whose expansion preserves every
  displayed pixel. Every other decoded mode is refused under `keep`; in particular,
  a 16-bit master would otherwise be crushed to 8 bits. An explicit declaration
  (`grayscale`, `rgb`, `bitonal`) is admitted and its actual output mode is read from
  the encoded PNG. Pillow modes such as LAB that do not implement direct conversion
  to `L` take the recipe's explicit LAB-to-RGB-to-L path. Nothing here converts
  evidence silently.
- **Unit 6 consequence for real 16-bit TIFF masters:** it must emit a deliberate
  per-part `grayscale`, `rgb`, or `bitonal` decision and record the actor that made
  it. It must not default those masters to `keep`. If preserving the 16-bit samples
  is required, Unit 6 must emit no split decision for that frame; the whole-page
  route then seals lossless TIFF samples. This is a doctrine consequence, not a
  guess based on the synthetic test material.
- The row's declared `frame` is compared against the master's actual dimensions,
  at the door and again at the Exemplar boundary. Unit 5 proves a row's parts
  partition its *declared* frame exactly; only this comparison ties that
  declaration to the photograph, and without it a row declaring a smaller frame
  re-derives perfectly while the rest of the master reaches no page.
- Frame dimensions and split coordinates are the decoded **stored raster**, before
  any part-local deskew. Exact equality is accepted even when a 90-degree part
  rotation swaps the derivative's output dimensions; every one-pixel width or
  height mismatch is refused. EXIF orientation metadata is not an unrecorded
  transform: a JPEG tagged for rotated display still has the stored raster's
  dimensions and coordinate space here. Unit 6 must transcribe ScanTailor geometry
  into that raw frame space rather than silently applying EXIF orientation.
- **The apply recipe's library versions are a record, not an enforcement.** The
  frozen `triage-raster-apply-v1` recipe is compared exactly; the Pillow, pillow-heif
  and libheif versions beside it are provenance under GOVERNANCE 6 and are *not*
  compared against the running host. Refusing on version drift would make every
  archived run unverifiable on the next routine upgrade. The byte comparison is the
  property; when it fails and the recorded versions differ from this host's, the
  refusal names the drift, because a decoder upgrade is the ordinary cause and
  "not reproducible" alone would send an operator looking for forgery.

**What binds a triage decision to a run.** The byte digests of the decision
manifest, of any cluster records, and of any producer recipe supplied with them are
bound into `config_digest`, alongside the digest of `config/triage_modes.toml`. The
recipe is validated against the producer's own closed schema before it is bound, so a
run cannot seal a recipe no triage pass could have emitted. A triage pass
re-run between two attempts at one run id can move a gutter without changing the
part count, leaving `source_manifest` byte-identical; the digests are what make
`RunTree.create` refuse that reuse by name rather than leaving it to be caught
incidentally, one ordinal at a time, by write-once artifacts.

**Re-shoot clusters.** A cluster reaches the run with every member accounted by its
own admission outcome, no canonical designation and no winner field, and a private
`re-shoot-cluster-report` makes the link visible without asking a later stage to
reconstruct it. A submission holding only some of a cluster's members is refused:
that is the door's own enforcement of "a seam is never placed inside a cluster",
for the only seam a production run can currently place — the operator's folder cut.
Every triage admission, successful or refused, carries a compact row/part link, and
the cluster report includes every such outcome. A corrupt member therefore remains
visibly in its cluster rather than disappearing from the cluster record while
surviving only in the separate refusal report.

**Not yet wired, and whose job it is.** `door.content_aware_shards` plans seams
that fall at opening boundaries and never inside a split pair or a cluster, and
refuses by name when the page cap leaves no legal seam. Nothing calls it: nothing
in the tree partitions a corpus into shards at all. Unit 8 owns multi-shard
submission and must call it before creating each `RunTree`, passing the sealed
`max_pages_per_shard` from `config/corpus_frame.toml`. It imposes no shard-count
ceiling of its own — the page cap is the sealed policy and the shard count is its
consequence — so a caller with a real ceiling passes `max_shards` explicitly. A
split pair cannot straddle a folder cut, because both halves come from one file.
Pair identity is the submitted path plus digest, not digest alone: byte-identical
copies remain distinct submitted frames, and a legal seam may fall between them.
Nested or interleaved cluster spans are kept as one contiguous shard interval.

**Hard-failure cap across shards — decided per shard/run.** The ruled unit in
`config/hard_failure.toml` is "more than 2 failures within a 1000 page run". Unit 8
creates one run per at-most-1,000-page shard, so it must keep the existing tally per
shard and must not add a corpus-wide aggregate that would silently change the ruled
unit. Two hard failures in each of several shards remain warnings in each run; the
third within any one shard halts that run at its next checkpoint.

**Recovery does not re-render a derivative page.** A recovery pass reads the same
sealed Exemplar page, re-verifies its master/recipe lineage at the ordinary boundary,
and cuts a new Designator region from those already sealed pixels. The deterministic
crop and write-once publication paths accept a byte-identical replay. There is no
recovery call site that invokes the Door renderer, so split fan-out neither expands
the recovery budget nor collides with the derivative page artifact.

**Early failures still keep the post-split denominator.** An unreadable, oversized,
or undecodable frame receives one refused ordinal per part already declared by its
validated triage row. The reasons may repeat because the source-level failure is
the same, but the page count does not collapse: `require_corpus_frame_shard`, the
Exemplar census, and the Armarium all reconcile against declared post-split pages,
including pages whose pixels could not be produced.

## RecordGold admission shape

`operations/corpus/` fetches RecordGold pages and submits them like any other
real material, and the Door admits them **unchanged** — no new route, no new
alarm. Each fetched page is a single-frame JPEG, so it takes `admit-or-fan-out`
and is sealed as its own original bytes, exactly as an iPhone-native HEIC
frame or a single-page TIFF is; the record regions in `record_url` are already
stated in that raster's own pixel coordinates, so nothing is transformed and
nothing needs re-deriving — provided the fetcher's own `dimension-mismatch`
and `exif-orientation` refusals passed. This stage does not and cannot check
the box-to-raster correspondence itself; the coordinate claim rests entirely
on `operations/corpus/`'s decode-time verification against `info.json`. There
is no triage decision manifest, no cluster, and no producer recipe behind any
of it — these pages never go near the
triage split/apply path, so `colour_mode` never applies to them; a builder
that invents a triage manifest "to be safe" would move `config_digest` and put
the pages on a re-encoding path they were never on. Each submission shard
stays at or under the sealed 1,000-page cap this stage already enforces,
partitioned by (split, source, volume) rather than by `content_aware_shards`,
which reasons about triage cluster pairs a RecordGold submission has none of.
The one obligation on the fetcher itself, not this stage: dedupe by response
digest before a folder is ever written, because two IIIF identifiers
returning identical bytes derive one page identity, and this stage would seal
one page citing both submission rows — which the next boundary then refuses as
a merged page. Sidecars carrying each page's records, split, and text sit
outside the submission folder for the same reason any record file must:
`inventory.py` would otherwise name a sidecar JSON as a submitted source and
the Door would refuse it `unrecognized-format`.

## Data handling and scope

Real input is fail-closed on living inside a storage root
`config/data_handling_policy.json` names — the submitted folder, the run root,
the filename ledger, and any triage decision manifest or cluster records all check
against it before a byte is read, and none of the record files may live inside the
submitted folder. **Cut 2026-08-09,
per Tyrel's ruling that session:** real input no longer also needs a current
data-gate approval-record artifact; none of this material ever reaches git
regardless of any such sign-off. The local gate package is
[`operations/submit/README.md`](../../operations/submit/README.md).
It retains all run material, exports, and filename ledger until the whole run is
dead/broken or complete/exported; only the lifecycle owner may then destroy the
whole run volume. Nothing here performs transfer, pod provisioning, an upload UI,
or routine deletion.

Tests use only synthetic bytes. No real image or PDF has been read or added.
