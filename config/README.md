# config

The knobs. One question per planned file, each answerable without reading code.

| File | The question it answers |
|---|---|
| `models.toml` | which model and revision fills each numbered role |
| `recovery.toml` | how many times rework may be asked for before review |
| `admitted_formats.toml` | how byte-detected image formats are decoded or page-rendered |
| `data_handling_policy.json` | how real material is stored, logged, retained and disposed of |
| `spend.toml` | money caps |
| `formats.toml` | which formats the Armarium writes |

`admitted_formats.toml` is decoder routing, not an admission list. Tyrel ruled that
an uncorrupted image is never declined by policy: an `admit-or-fan-out` row gets a
decoder attempt and is sealed as its own unmodified bytes when the decoder reports
one frame, or fanned out to one ordinal per frame when it reports more; a
`render-pages` row is always a document of pages and every page is painted once at
the door. The file names exactly the formats the door can sniff and has no `refuse`
action. A format/variant the installed readers cannot yet decode is a named pipeline
alarm carried with its filename, rather than a routine rejection.

PDF alone uses `render-pages`, and the loader refuses any other format given that
action. PDF is full-page PDFium rasterisation, which paints text, vectors,
annotations, and images together; it is never embedded-image extraction. TIFF is
`admit-or-fan-out`: a single-directory scan seals its own bytes untouched, and a
multi-page one — including the LZW, Deflate, PackBits and CCITT compressions real
flatbed scanners produce — fans out to one ordinal per page. JPEG suffix bytes after
EOI are not called corruption.

`data_handling_policy.json` is the version an approval record names. Its hash is the
canonical digest of its own content, so editing one character of it invalidates
every approval that named the old version — which is the honest behaviour, not a
bug. The **data-handling gate package** is the tracked
`operations/submit/README.md`, and `operations/submit/gate.py` is the machinery
that enforces it. Note that both entry points expose the policy's path as a flag, so
"the current policy" is whichever file the invoker names — a documented limit of a
mechanism `common/contracts/approval.py` already describes as tamper-evidence rather
than access control.

For real submissions, the local submit door writes a self-hashed filename ledger
before any transfer. The Exemplar door requires that ledger and binds its filename,
digest, byte-count, and fanned-page-index rows into `run.json`; an export carries
the same linkage back out. The policy permits no per-stage deletion: retain the whole run until it is
dead/broken or complete/exported, then its lifecycle owner may destroy the whole
volume. See `operations/submit/README.md` for the package being handed to Tyrel;
transfer/pod/UI work is not built here.

`models.toml` is the operational cast list. Model assignments belong there rather
than in stage code or stage documentation, which keeps a swap to one configuration
change. It also owns the three things a run is bound to that follow from the
roster: the witness floor, the adapter recipes, and — with the fixture and the
scenario — the run's configuration digest. `common/chairs/README.md` describes how
it is read and what a malformed pin earns.

Two directories sit beside it because they are resolved relative to it, and could
not be pinned by it from anywhere else:

- `manifests/` — one digest-manifest artifact per configured chair: the sorted
  `{path, sha256, size}` rows whose canonical bytes a chair's `digest_manifest`
  names.
- `model-fixtures/` — the tiny local-repository snapshots the offline walking
  skeleton resolves. **These are not models.** They stand in for a model
  repository exactly as `proof/fixtures/synthetic-two-page-v0/*.png` stand in for
  a scanned register. `proof/build_model_fixtures.py` regenerates both directories
  and prints the pins; a test refuses any drift between them.
