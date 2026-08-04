# Exemplar — handoff

The Exemplar is the immutable source of pixels for the rest of the run. The door
writes its admissions into `1_exemplar/`; the Exemplar then seals one `kind="page"`
outcome for every submitted ordinal and one self-hashed `kind="seal"` corpus census.
No later stage re-renders a source container.

All paths below are existing RunTree shapes: `run.json`, artifacts, content-addressed
blobs, manifests, and approval receipts. This stage invents no separate render or
inventory directory.

## Input and filename ledger

For a real submission, `operations/submit/submit.py` first creates a canonical,
self-hashed `submission-manifest.v0`. It is the filename ledger: each original
relative filename is bound to its SHA-256 and byte count before a copy moves.

The real door requires that ledger with `--submission-manifest`. It verifies its
self-hash and approval reference, compares the post-transfer folder to it, and
extends `run.json`'s self-hashed `source_manifest` rather than making a second
inventory. A source row has:

```text
ordinal               stable page ordinal
relative_path         original filename / citation link
sha256                original source-file digest from the ledger
bytes                 original source-file size from the ledger
ledger_sha256         the one submission-manifest self-hash for this set
container_page_index  present for every fanned source page/frame, zero-based
```

Rows repeat a container's filename/digest for its fanned-out pages. The Exemplar
reconstructs the unique file rows and requires them to reproduce `ledger_sha256`
against the run's sealed approval reference. A changed copy creates a
`digest-mismatch` alarm under the original filename; a source never silently drops.

Declared synthetic fixtures remain the only approval-free route. They carry the
same core source rows but not a real filename ledger.

## Decoder routes and alarms

`config/admitted_formats.toml` names how a detected format is read, never a policy
decision to decline it:

- `admit-or-fan-out`: Pillow decodes the source pixels and a one-frame raster is
  sealed unchanged, as its own original bytes. If its decoder reports multiple
  frames, every frame fans out to its own ordinal and is sealed as one lossless PNG
  page. This is every raster format, TIFF included.
- `render-pages`: a format that is always a document of pages — PDF alone, enforced
  when the routing is loaded. The door assigns stable ordinals and seals one
  lossless PNG per page.

PNG, JPEG, TIFF, PDF, GIF, BMP, WebP, HEIC and an unknown signature all receive a
decoder attempt by bytes, not extension. A valid image the installed readers do not
yet understand becomes a named `unsupported-variant` or `unrecognized-format`
pipeline alarm, not `refused-format`—that enum member no longer exists. JPEG bytes
after EOI are retained. TIFF, including ordinary multi-page TIFF and the LZW, Deflate, PackBits and CCITT
compressions flatbed scanners produce, fans out; a single-page TIFF keeps its own
bytes and is never re-encoded.

PDF is rendered as a whole page with PDFium at the door. Its visible content stream,
text, vectors, images, rotation, annotations, and initialized form appearances are
painted into the sealed pixels; embedded-image extraction is not used.

The render targets **400 DPI**, capped downward to a whole DPI — never below 72 —
when a page's declared size would otherwise exceed the admission pixel bounds, so a
large page is captured at reduced resolution rather than refused. Each page's
`render_contract` records the DPI it was *actually* rendered at alongside the
target, because the target alone does not describe a capped page's pixels. **400 is
an unmeasured choice standing in for a measurement**: no real sample has been
rendered at either 400 or 300, and the number should be checked against Tyrel's own
iPhone-to-PDF output once the data-handling gate is approved.

## Door `kind="admission"`

There is one admission artifact per source ordinal, whether its outcome is
`"admitted"` or `"refused"`. Its payload always carries:

```text
ordinal, declared_path, declared_sha256
declared_bytes, ledger_sha256             (real ledger rows)
data_gate_approval_ref                    (real runs)
```

An admitted payload additionally has `sha256`, `stored_at`, and `geometry`. A page
render also has:

```text
rendered_from = {
  container_format, container_sha256, container_page_index,
  render_contract
}
```

`render_contract` records the renderer/version, exact page index, lossless RGB PNG
output, geometry, and PDF-specific pixel choices. It is a complete explanation for
why a sealed rendered digest differs from the source container digest.

A refused payload has `reason`, whose prefix is one of the closed alarm codes:
`empty`, `unreadable`, `too-large`, `unrecognized-format`, `corrupt`,
`unsupported-variant`, `digest-mismatch`, or `duplicate`. The artifact retains the
filename; a terminal is only presentation.

## Exemplar `kind="page"` and corpus seal

For an admitted source the Exemplar writes a `sealed` page whose identity binds the
sealed-byte digest plus ordinal. Its payload retains `declared_path`,
`declared_sha256`, `source_sha256`, `image_path`, and the ledger facts; rendered
pages also retain `rendered_from`. A refused admission becomes an Exemplar `refused`
page outcome with the same original filename/digest and reason.

The one `kind="seal"`, subject `corpus-seal`, is self-hashed and has one census row
per submitted ordinal. Each row includes outcome, page identity or null,
sealed-byte digest or null, original filename, original digest, and real-ledger
facts where applicable. Its inputs reference every Exemplar page artifact.

Before any design work, `pipeline/2_designator/run.py` independently reconciles the
source manifest, every page outcome, the seal's rows, and the seal's input
references. A missing page fails at that first boundary with the missing filename.
For a sealed page, both the Designator and final Armarium boundary recheck the Door
admission and exact content-addressed pixel blob before they crop or export; no
later stage may turn altered pixels into new evidence. The Armarium repeats the
filename/digest linkage and any `container_page_index` in `export.pages` and in
every delivered crop's `source_regions`, so an individual output can be matched to
all originals it used without guessing from an ordinal.

## Data handling and scope

Real input is fail-closed on a current approval record bound to
`config/data_handling_policy.json`. The local gate package is
[`operations/submit/README.md`](../../operations/submit/README.md).
It retains all run material, exports, and filename ledger until the whole run is
dead/broken or complete/exported; only the lifecycle owner may then destroy the
whole run volume. Nothing here performs transfer, pod provisioning, an upload UI,
or routine deletion.

Tests use only synthetic bytes. No real image or PDF has been read or added.
