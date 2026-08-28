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

A wholly refused Door is that second case: it publishes its refusal report and
its duplicate report, and only then does `require_some_admitted` raise — so the
evidence is on disk and no `stage-seal` is, and the Exemplar's "predecessor door
has no stage-seal" names a refused submission rather than a missing file.

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

Byte-identical submitted sources are not refusals. Each ordinal is admitted and
keeps its filename link, may reuse the same content-addressed blob, and the Door
seals a private `duplicate-report` naming the first observed ordinal/path and
operator-visible duplicate counts.

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

**A merged page is refused at the next boundary, by name.** Every stage behind the
Exemplar still keys its work by submitted ordinal and would mint each act on such a
page twice, so `verify_exemplar_corpus_seal` stops the run there rather than letting
it read the page twice or report the second row as a lost ordinal it plainly is not.
The operator consequence is worth knowing before a run starts: a submitted folder
holding the same scan under two filenames — routine in archive exports — produces a
green Exemplar and then a fatal Designator. This lifts when consumers process merged
pages once per identity.

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

## Data handling and scope

Real input is fail-closed on living inside a storage root
`config/data_handling_policy.json` names — the submitted folder, the run root, and
the filename ledger all check against it before a byte is read. **Cut 2026-08-09,
per Tyrel's ruling that session:** real input no longer also needs a current
data-gate approval-record artifact; none of this material ever reaches git
regardless of any such sign-off. The local gate package is
[`operations/submit/README.md`](../../operations/submit/README.md).
It retains all run material, exports, and filename ledger until the whole run is
dead/broken or complete/exported; only the lifecycle owner may then destroy the
whole run volume. Nothing here performs transfer, pod provisioning, an upload UI,
or routine deletion.

Tests use only synthetic bytes. No real image or PDF has been read or added.
