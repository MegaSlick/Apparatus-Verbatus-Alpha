# Door and Exemplar — contract

Two programs share `1_exemplar/`. The **Door** (`door.py`) creates the run: it decides
by bytes alone whether each submitted page may enter, and records an admission or a
refusal for every one. The **Exemplar** (`run.py`) then seals every admitted page as
the immutable pixels every later stage reads. They keep separate producer inventories
(`manifest-door.json` and `manifest.json`) in the same stage directory.

```text
python pipeline/1_exemplar/door.py --run-root <dir> --run-id <id>
python pipeline/1_exemplar/door.py --run-root <dir> --run-id <id> \
    --submission-folder <dir> --submission-manifest <ledger.json> \
    [--canary-folder <dir> --canary-manifest <ledger.json>] \
    [--triage-decision-manifest <json> [--triage-clusters <json>] \
     [--triage-producer-recipe <json>]] [--corpus-register <json>]
python pipeline/1_exemplar/run.py --run-root <dir> --run-id <id>
```

## Inputs

**Fixture route.** Without `--submission-folder`, the Door reads the repository's
declared synthetic fixture (`proof/`) and nothing else; any other folder is real input.

**Real route.** A real submission needs:

- the submitted folder and its self-hashed filename ledger (`submission-manifest.v1`,
  written by `operations/submit/submit.py` before transfer), which binds each original
  relative filename to its SHA-256 and byte count;
- a data-handling policy (`config/data_handling_policy.json` by default). The folder,
  run root, ledgers and every triage document must lie inside one of its approved
  storage roots, checked before any byte is read; no ledger, triage document or run
  root may lie inside the submitted folder (checked by filesystem identity, not path
  spelling).

Optional inputs:

- **Golden canaries:** a second folder and ledger, disjoint from the submission by
  path and digest. Canary pages take the last ordinals and are controls, never the
  submission.
- **Triage documents** (`common.contracts.triage`): a decision manifest that splits,
  crops, rotates and converts submitted frames, its re-shoot cluster records, and the
  producer recipe. Every submitted frame needs a row; rows for frames outside this
  submission are allowed.
- **Corpus register** (`common/corpus_register.py`): sealed into the run as a snapshot.

The folder must hold exactly the ledgered files; an extra file stops the Door before a
run exists.

## Run creation

The Door is the only writer of `run.json`. Its `source_manifest` has one row per page
ordinal, after every container and triage split has been expanded:

```text
ordinal               stable page ordinal, ordered by relative path then page index
relative_path         original filename (the citation link)
sha256                original source-file digest from the ledger
bytes                 original byte count from the ledger          (real route)
ledger_sha256         the ledger's self-hash                         (real route)
container_page_index  page/frame index in a PDF, multi-frame raster or split
```

`require_corpus_frame_shard` caps the post-split page count at the sealed
`config/corpus_frame.toml` limit (at most 1,000).

The run seals `sealed_config_digests` for every configuration a later stage rechecks
by name. On the real route these are `common.stage.real_run_bindings`' names plus
`data-handling` (the policy that gated the input) and, with canaries, `canary-ledger`
(which marks canary rows). The real route's `config_digest` also binds the ledger,
the triage document digests, the format routes, the PDF render settings and the
Door's own implementation recipe, so `RunTree.create` refuses to reuse a run id under
different input or settings. Later stages cannot recompute it; they recheck the named
digests instead.

## Admission

Routing is by sniffed bytes, never the filename extension (`admission.route_for`):

- **PDF** is always a document of pages: each page is painted whole by PDFium and
  sealed as lossless PNG at the sealed target DPI (`config/pdf_render.toml`, default
  300, overridable per run with `--pdf-target-dpi`; the 72-DPI floor and pixel caps
  are in code).
- **Every other format** is decoded. A one-frame raster is sealed as its own
  original bytes; a multi-frame raster (multi-page TIFF, animated GIF/WebP, APNG,
  multi-picture JPEG) fans out to one ordinal per frame, each rendered losslessly
  (PNG, or TIFF where PNG cannot hold the samples).

A submitted raster above 64 MiB is refused `too-large`; PDFs are streamed from an
anchored descriptor and are not held to that bound. A page the Door renders is bounded
by `MAX_RENDERED_PAGE_BYTES` instead.

Every ordinal gets one `kind="admission"` artifact. Its payload always carries
`ordinal`, `declared_path`, `declared_sha256`, and on the real route `declared_bytes`
and `ledger_sha256`. An admitted payload adds `sha256` and `stored_at` (the page
blob), `geometry`, `admitted_source_sha256` (the submitted file's digest), and for a
rendered page `rendered_from = {container_format, container_sha256,
container_page_index, render_contract}`, where `render_contract` records the
renderer, versions, page index, output codec and mode, and DPI facts. A refused
payload has `reason`, opening with one closed code: `empty`, `unreadable`,
`too-large`, `unrecognized-format`, `corrupt`, `unsupported-variant` or
`digest-mismatch`. A source whose pages could not be counted still occupies its
ordinals, so no page drops out of the denominator.

## Run-level refusals

After every admission is published the Door seals up to three private,
filename-bearing reports, each only when it has something to name, then refuses the
whole run, before its own completion seal, when:

1. **Two submitted files carry identical bytes** (`duplicate-report`). Page identity
   binds the submitted bytes, so they would be one page read twice. Byte-identical
   pages inside one container are not duplicates.
2. **Any re-shoot cluster is present** (`re-shoot-cluster-report`). No later stage
   links two captures of one leaf, so each would be read and exported as its own act.
   The `re-shoot` refusal names every cluster by its position in the report and its
   member ordinals, marking any not confirmed in the run's register. Remedy: submit
   one capture per leaf, with a decision manifest whose rows name no cluster and no
   `--triage-clusters`. A cluster with a member outside the submission is refused
   at source expansion with the same remedy.
3. **No page of the submission was admitted** (`refusal-report`). Admitted canaries do
   not count.
4. **The export could never be sealed.** With `embed_pixels = true` the export
   archive carries every admitted page (canaries excepted) and its crops. The Door
   estimates it as each page's stored bytes plus `CROP_BYTES_PER_PIXEL` per page
   pixel for the crops, and refuses an estimate above `MAX_EXPORT_ARCHIVE_BYTES`
   (both in `common/armarium_formats.py`) here rather than after the reading.
   Remedy: smaller runs, or `embed_pixels = false`.

A refusal names ordinals and the private report's location, never a filename: it is
printed to the terminal, and the reports are where filenames belong. A refused Door
leaves no stage seal, so the Exemplar refuses to open over it.

## Split derivative pages

A frame with a triage row fans out to one ordinal per declared part before the run is
created. The Door checks the frame's bytes against the row's digest and the decoded
frame's dimensions against the row's `frame` (exactly; a stale row is refused rather
than shifting coordinates), then renders each part with
`common.imaging.render_triage_derivative`: cut the frame-space region, crop, rotate
clockwise onto an expanded canvas, convert, encode deterministically.

The master is never re-encoded: its bytes are stored under their own digest as the
admission's `parent_frame`, and the admission inputs both the page and the master.
The `render_contract` carries `derivative_page`: the full triage row, the back-link
to its part, the operation list and the fixed apply recipe
(`common.imaging.TRIAGE_APPLY_RECIPE`). `colour_mode: "keep"` admits only modes the
encoder stores losslessly (`1`, `L`, `LA`, `RGB`, `RGBA`, and palette `P`); a 16-bit
master needs an explicit conversion or no split at all. The recorded library versions
are provenance; the verifier compares bytes, and when a re-render differs on a host
with other library versions its refusal names them.

## Exemplar

The Exemplar refuses to open unless the Door sealed its boundary. It then reconciles
the Door's admissions against `run.json` (one admission per ordinal, none extra),
re-checks every blob against its digest and every render contract, re-derives every
split page from its master, and checks that the source rows reproduce each sealed
filename ledger.

For each admitted ordinal it seals one `kind="page"` artifact, `outcome="sealed"`,
whose `subject_id` is the page identity: a digest of the immutable origin
(`{kind: "source", sha256}` for bytes sealed as submitted, or `{kind:
"container-page", container_sha256, container_page_index, render_contract}` for a
rendered page) and the transform `{operation: "whole"}`, never the ordinal or path.
Its payload carries `ordinal`, `declared_path`, `declared_sha256`, `source_sha256`,
`image_path`, the ledger facts, `rendered_from` where present, and for a PDF page
`render_resolution` (configured, resolved and effective DPI, and any shortfall). Its
inputs are its admission and its blob. A refused admission becomes a page with
`outcome="refused"` and the same reason.

One page per submitted row: two rows deriving one page identity refuse the Exemplar,
as does a run whose only sealable pages are canaries.

One self-hashed `kind="seal"` artifact, subject `corpus-seal`, closes the stage. It
has one row per ordinal (outcome, page id or null, sealed digest or null, original
filename and digest, ledger facts) and inputs every page artifact.

## What later stages may rely on

`common.exemplar_boundary.verify_sealed_page_pixels(tree, run, source, page)` is the
check every stage runs before it uses a page's pixels: the page belongs to this run
and source row, its identity binds its origin, its admission and blob are the ones
sealed, and a split page re-derives from its master. It returns the verified bytes,
which are the bytes to use. `verify_exemplar_corpus_seal` checks the census, and
`verify_reading_region_lineage` checks a crop against the page it was cut from.

Tests use only synthetic bytes.
