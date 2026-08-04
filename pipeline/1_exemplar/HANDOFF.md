# Exemplar — handoff

**What this stage writes:** the record of what arrived (the door's admissions), the
sealed pages, and the corpus seal.

**Where it writes it:** the run's `{run}/1_exemplar/` folder. The door owns no
directory of its own and writes here too — see below.

---

This document is the only thing downstream stages may rely on. They read these
files; they never import this stage's code. Every shape below is exercised by the
proof fixture's `happy`, `review`, `refused-page` and `refused-first-page` scenarios
(`pipeline/orchestrator/test_orchestrator_acceptance.py`), except the PDF path, the
multi-page TIFF fan-out and the data-handling gate, which the walking skeleton's
fixture never carries — those are proven directly in
`pipeline/1_exemplar/test_pdf_render.py`, `pipeline/1_exemplar/test_tiff_render.py`,
`pipeline/1_exemplar/test_door.py` and `operations/submit/test_gate.py`, against
synthetic bytes standing in for real input.

**No real image has been touched.** The real ingress path exists and is gated; the
gate refuses without an approval record naming the current policy version, and no
such approval exists.

## What the shipped admission list admits, and the one limit behind it

**Repaired 2026-08-04 against Tyrel's rulings, which overturned this section's whole
premise.** "Nothing should be rejected — any image and image formats should work. If
things are failing the image got corrupted … or the pipeline is broken." There is no
`refuse` action any more: `config/admitted_formats.toml` ships `png` and `jpeg` as
`admit`, `tiff` as `admit-or-fan-out`, `pdf` as `render-pages`, and `gif`/`heic` as
`gap` — sniffable, with no reader built yet. **The list is still the authority for
every format**, including the multi-page ones: the door asks
`admission.classify_detected_format` before it counts a page and again before it
renders one, and names no format's admission itself.

The three limits this section used to carry are settled:

- **PDF is admitted, rasterised per page.** `pipeline/1_exemplar/pdf_render.py` no
  longer reads a page's resource dictionary and extracts one image XObject — that
  approach never touched `/Contents` and could not tell a scanned page from one that
  draws text or vector marks beside its one image, which is exactly the defect that
  used to hold this row at `refuse`. It now renders the whole page to pixels via
  `pypdfium2` (cleared to enter, ruling 9), so a page's content-stream shape is the
  renderer's problem rather than a hundred named limits: rotation, colour space,
  incremental updates and non-classic cross-reference tables all stop mattering by
  construction.
- **Multi-page TIFF is fanned out**, exactly like PDF, through the door-private
  `pipeline/1_exemplar/tiff_render.py`. A single-directory TIFF is still sealed
  unmodified — nothing about the common case changed — but a file the door finds
  holding more than one directory (`image_formats.tiff_directory_offsets`) is fanned
  into one ordinal per page and each page rendered. **What tiff_render.py cannot yet
  do**, and names as a gap rather than a refusal: a directory using any compression
  codec other than none (LZW, Deflate, PackBits, CCITT, ...), tiled rather than
  stripped storage, or more than 8 bits per sample. Extending it is additive — one
  more branch, not a different door.
- **A JPEG with a trailing byte after EOI is admitted.** Real cameras and scanners do
  append a thumbnail or padding there; refusing it told Tyrel a real photograph was
  corrupt when it was not.

**A format with no reader at all — GIF, HEIC today — is a named gap, not a
refusal by policy.** Its refusal reason is `UNSUPPORTED_VARIANT`, the same code a
genuine undecodable variant of a *supported* format gets, because both mean the same
thing: a pipeline defect, never a decision about the file. Turning either row to
`admit` needs a structural validator in `image_formats.py` first; `admission.py`
refuses to load a policy that admits a format nothing can verify.

## `kind="admission"` — the door's record of every declared source

One per declared source (per **page**, not per file — a multi-page container, a PDF
or a multi-directory TIFF, is fanned out to one ordinal per page before the door
decides anything). `subject_id` is `f"source-{ordinal}"`. `outcome` is `"admitted"`
or `"refused"` and nothing else; one kind carrying an outcome is what makes the
door's census complete.

Admitted payload:

```
declared_path   the path the source was declared under (for a container page, the
                container's own path, shared across its pages)
ordinal         the integer ordinal this source occupies in run.json's source_manifest
sha256          the digest of the bytes actually sealed — for a standalone file its
                own bytes; for a container page the *rendered page's* bytes
stored_at       the blob's relative path, content-addressed under
                1_exemplar/blobs/sha256/
geometry        {width, height}, read off the real container by the structural
                validator — never from a filename or a caller's claim
```

and, **only when the sealed bytes are a render rather than the submitted file**:

```
container_page_index  which page of that container produced these bytes (0-based)
container_sha256      the digest of the whole submitted file, matching run.json
```

**Repaired 2026-08-04.** This field was `pdf_page_index`. Ruling 2026-08-04, item 2
reverses multi-page TIFF's blanket refusal — it is fanned out exactly like PDF
(`pipeline/1_exemplar/tiff_render.py`) — so a fanned-out TIFF directory records the
identical transform, and a PDF-only name was wrong the moment a second container
format started producing it. GLOSSARY's opening rule, "one word per concept."

`container_sha256`, not `source_sha256`: the page payload below already uses
`source_sha256` for the digest of the **sealed** bytes — the one `page_id` binds —
and one word for two concepts is GLOSSARY's opening rule broken three lines apart.
They coincide for a standalone raster, which is what made it easy to miss.

Those two are the recorded transform. ARCHITECTURE's third invariant — the exact
image shown to a model is reproducible from the Exemplar plus recorded transforms —
is only true if the render is recorded, and the Exemplar refuses a sealed page whose
bytes differ from what was submitted with no transform to explain it.

Refused payload:

```
declared_path   as above
ordinal         as above
reason          "<reason-code>: <detail>", the code drawn from the closed set
                admission.RefusalReason — empty, unreadable, too-large,
                unrecognized-format, corrupt, unsupported-variant, digest-mismatch,
                duplicate. Never free text alone; the Exemplar reads the code back
                and refuses anything outside the set.
```

**Repaired 2026-08-04.** `REFUSED_FORMAT` is retired — ruling 2026-08-04, item 2
deletes the whole idea of a format refused by policy. A format nothing here can
decode yet (a `gap` action: GIF, HEIC today) refuses under `UNSUPPORTED_VARIANT`,
the same code an undecodable variant of a *supported* format gets, because both are
the same fact: a pipeline defect this project owes a reader for, never a decision
about the file.

A real (non-fixture) run additionally carries `data_gate_approval_ref` on every
admission, admitted and refused alike — see "The data-handling gate" below.

**Admission is decided by bytes, never by a declared name or extension.** The format
is sniffed from the source's own signature and then structurally validated
(`image_formats.py` for png/jpeg/tiff) or rendered (`pdf_render.py` rasterises a
PDF's page whole; `tiff_render.py` renders a multi-directory TIFF's pages, one
directory at a time). A file whose extension disagrees with its bytes is decided on
the bytes. The list of which formats may enter at all, and how, is
`config/admitted_formats.toml`.

## `kind="page"` — the Exemplar's own outcome for every page

One per page, derived from exactly one `kind="admission"` record. A page the door
refused seals nothing and carries the refusal forward here, so it stays accounted
for at this boundary too. `outcome` is `"sealed"` or `"refused"`.

Sealed: `subject_id` is the page's derived identity (`pg_<hex>`, binding the digest
of the bytes that were **actually admitted** and the ordinal — never a hash of a
path, which was audit Q12's defect). Payload:

```
ordinal          as above
source_sha256    the digest of the sealed bytes (the admission's sha256)
image_path       the blob's relative path (the admission's stored_at)
rendered_from    present only for a rendered page: {container_sha256,
                 container_page_index}, carried through from the admission's
                 recorded transform
```

Refused: `subject_id` is the admission's own `f"source-{ordinal}"` — there is no
page identity to derive, because nothing was sealed and a derived identity would
claim to bind content that was never verified. Payload: `{ordinal, reason}`, the
reason copied from the admission that caused it.

Before any page is published, the Exemplar reconciles the door's census against
`run.json`: every submitted ordinal has exactly one door outcome, no door outcome
names an ordinal nobody submitted, every admission's declared path matches the run
authority's, and every admitted blob's bytes still hash to the digest its admission
claims. A source cannot disappear between submission and sealing.

## `kind="seal"` — the corpus seal, once per run

`subject_id` is the fixed string `"corpus-seal"`. `outcome` is always `"sealed"`:
this artifact is written only once every page has been accounted for, and if no page
sealed at all the stage refuses before reaching it. Payload:

```
page_count   the number of entries in `pages`, sealed and refused together
pages        one entry per ordinal, sorted by ordinal:
             {ordinal, page_id (null for a refused page), outcome,
              source_sha256 (null for a refused page)}
self_hash    the payload's own self-hash (common/contracts/canonical.py) over every
             field above — the same mechanism run.json uses, so an edit after
             sealing is detectable rather than merely undocumented
```

`inputs` names every `kind="page"` artifact the seal accounts for, so its provenance
reaches back to each page's admission in turn. Rerunning an unchanged run reproduces
this artifact byte for byte; a rerun over a **tampered** seal refuses before it
writes anything, rather than quietly building a second one beside the first.

## Blobs

`1_exemplar/blobs/sha256/<digest>` holds admitted bytes, content-addressed:

- a standalone admitted file's own bytes, unmodified — never re-encoded. This is
  every PNG and JPEG, and every single-directory TIFF (the common case even under
  `admit-or-fan-out`);
- a rendered page's bytes, **whenever the admission list asks for a format to be
  fanned out and the source actually is** — PDF always, a TIFF the door finds
  holding more than one directory. Every rendered page, whichever container it came
  from, is encoded as a lossless PNG through `image_formats.encode_png`, the one
  encoder both door-private renderers share. A PDF page is rasterised whole —
  everything the page paints, text and image alike, through `pypdfium2` — never an
  embedded image extracted from the page's resources.

Identical bytes reused across ordinals are one blob referenced by more than one
`stored_at`. That is deliberate — spec 03's "identical bytes reused rather than
rewritten" — and is never evidence of resubmission.

## The filename ledger

**Ruling 2026-08-04, item 1: "We literally need the file name. That is how we link
it."** FamilySearch, archives and microfilm all carry an ID system in the filename of
what you download; that identifier is the citation and the route back to the source.
Filenames are retained and used throughout this stage's record — never scrubbed —
and `run.json`'s `source_manifest` is the ledger itself: every submitted file's name
bound to the sha256 of its bytes, at the moment of submission, sealed into the run
authority's own self-hash before a single byte moves. No second inventory exists
beside it. A byte flipped in transfer is caught by comparing the manifest's declared
digest against the bytes actually read (`admission.RefusalReason.DIGEST_MISMATCH`),
reported with the file named in the admission artifact that refused it.

**The split is between the record and the operator's terminal, not between "has a
name" and "does not."** Every admission artifact — admitted or refused — carries
`declared_path`. What a human sees scroll past a shell is a presentation decision:
`require_some_admitted`'s loud failure names reason codes and counts, never a
filename, and points at `1_exemplar/artifacts/admission/` for the names themselves;
`operations/submit/submit.py`'s CLI does the same, writing a private refusal report
beside the manifest rather than repeating a submitted name on stderr.

## What downstream may rely on, and what it may not assume

- **Filter the manifest to `kind == "page"`** before reading. This directory also
  holds `kind == "admission"` (the door's record) and `kind == "seal"`.
  `pipeline/2_designator/run.py` and `pipeline/7_armarium/run.py` both already do.
- A page's `image_path` is the exact, final, sealed bytes. Nothing downstream may
  re-render, re-decode or regenerate them: `pdf_render.py` and `tiff_render.py` are
  both door-private and `pipeline/1_exemplar/test_import_boundaries.py` enforces
  that statically over the repository's own Python, so there is no API a later
  stage could call.
- **Duplicate submitted files are refused by their bytes and declared path.** Two
  paths carrying the same raster or container source produce a named duplicate
  refusal. Distinct pages within one container (a PDF, or a multi-directory TIFF)
  are not duplicate files: two blank pages can render to identical bytes honestly,
  and refusing the second would lose a real page (GOALS 1). A reader must not treat
  two page artifacts sharing one `image_path` as evidence that either is spurious.
- A refused page carries no `page_id`, no `source_sha256` and seals nothing. Its ink
  was never read and nothing downstream may treat it as though it were.
- Nothing in this stage's admission or sealing path consults a model, a witness, or
  any chair output. Sealing depends on bytes alone — the old sealer's dependence on
  a witness-stage model is the second defect this spec exists to kill.

## The data-handling gate

Real (non-fixture) input is refused before a single file is opened, by
`operations/submit/gate.py`'s `enforce()`, at two boundaries: `submit.py`'s folder
walk, and the door's own admission loop. `enforce()` has no fixture override. The
door derives the route from the self-hashed run ingress, and only the repository's
declared fixture root and loaded manifest can create a synthetic-fixture run — a
caller-named folder is real input whatever it is called.

What a *run* was admitted under is stronger than either check, and is where a reader
should look: `run.json` carries an `ingress` record inside its own self-hash, either

```
{"mode": "synthetic-fixture"}
```

or

```
{"mode": "approval-gated-real",
 "data_gate_policy_hash": <sha256 of config/data_handling_policy.json's content>,
 "data_gate_approval_ref": {"relative_path": "receipts/sha256/<digest>.json",
                            "sha256": "<digest>"}}
```

The approval record itself is stored in the run tree at that content-addressed
receipt path — the same shape a serving receipt uses, because an approval also
records a human act at a moment and cannot be a deterministic stage artifact. The
Exemplar checks that every door admission agrees with the run's ingress: all of them
carry the same approval reference or none does, so a run cannot hold a mixture in
which some pages were gated and others simply were not.

The **data-handling gate package** is the written policy this machinery checks
against, delivered to Tyrel for approval rather than tracked here.
`config/data_handling_policy.json` is its machine-readable half. This document used
to name an absolute path outside the repository for it, which was a container's
scratch mount and could never be opened by a later reader.
