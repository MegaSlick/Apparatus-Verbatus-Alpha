# Exemplar — handoff

**What this stage writes:** the record of what arrived (the door's admissions), the
sealed pages, and the corpus seal.

**Where it writes it:** the run's `{run}/1_exemplar/` folder. The door owns no
directory of its own and writes here too — see below.

---

This document is the only thing downstream stages may rely on. They read these
files; they never import this stage's code. Every shape below is exercised by the
proof fixture's `happy`, `review`, `refused-page` and `refused-first-page` scenarios
(`pipeline/orchestrator/test_orchestrator_acceptance.py`), except the PDF path and
the data-handling gate, which the walking skeleton's fixture never carries — those
are proven directly in `pipeline/1_exemplar/test_pdf_render.py`,
`pipeline/1_exemplar/test_door.py` and `operations/submit/test_gate.py`, against
synthetic bytes standing in for real input.

**No real image has been touched.** The real ingress path exists and is gated; the
gate refuses without an approval record naming the current policy version, and no
such approval exists.

## `kind="admission"` — the door's record of every declared source

One per declared source (per **page**, not per file — a multi-page PDF is fanned out
to one ordinal per page before the door decides anything). `subject_id` is
`f"source-{ordinal}"`. `outcome` is `"admitted"` or `"refused"` and nothing else;
one kind carrying an outcome is what makes the door's census complete.

Admitted payload:

```
declared_path   the path the source was declared under (for a PDF page, the PDF's
                own path, shared across its pages)
ordinal         the integer ordinal this source occupies in run.json's source_manifest
sha256          the digest of the bytes actually sealed — for a standalone file its
                own bytes; for a PDF page the *rendered page's* bytes
stored_at       the blob's relative path, content-addressed under
                1_exemplar/blobs/sha256/
geometry        {width, height}, read off the real container by the structural
                validator — never from a filename or a caller's claim
```

and, **only when the sealed bytes are a render rather than the submitted file**:

```
pdf_page_index  which page of that PDF produced these bytes (0-based)
source_sha256   the digest of the whole submitted PDF, matching run.json
```

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
                unrecognized-format, refused-format, corrupt, unsupported-variant,
                digest-mismatch, duplicate. Never free text alone; the Exemplar
                reads the code back and refuses anything outside the set.
```

A real (non-fixture) run additionally carries `data_gate_approval_ref` on every
admission, admitted and refused alike — see "The data-handling gate" below.

**Admission is decided by bytes, never by a declared name or extension.** The format
is sniffed from the source's own signature and then structurally validated
(`image_formats.py` for jpeg/png/tiff; `pdf_render.py` for a PDF's one image per
page). A file whose extension disagrees with its bytes is decided on the bytes. The
list of which formats may enter at all is `config/admitted_formats.toml`.

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
rendered_from    present only for a PDF page: {source_sha256, pdf_page_index},
                 carried through from the admission's recorded transform
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

- a standalone admitted file's own bytes, unmodified — never re-encoded;
- a PDF page's *rendered* bytes: a `DCTDecode` page is stored as the embedded JPEG
  unmodified; a `FlateDecode` (or unfiltered) page's raw samples are encoded as a
  PNG through `pdf_render.py`'s own minimal encoder.

Identical bytes reused across ordinals are one blob referenced by more than one
`stored_at`. That is deliberate — spec 03's "identical bytes reused rather than
rewritten" — and is never evidence of resubmission.

## What downstream may rely on, and what it may not assume

- **Filter the manifest to `kind == "page"`** before reading. This directory also
  holds `kind == "admission"` (the door's record) and `kind == "seal"`.
  `pipeline/2_designator/run.py` and `pipeline/7_armarium/run.py` both already do.
- A page's `image_path` is the exact, final, sealed bytes. Nothing downstream may
  re-render, re-decode or regenerate them: `pdf_render.py` is door-private and
  `pipeline/1_exemplar/test_import_boundaries.py` enforces that statically over the
  repository's own Python, so there is no API a later stage could call.
- **Duplicate submitted files are refused by their bytes and declared path.** Two
  paths carrying the same raster or PDF source produce a named duplicate refusal.
  Distinct pages within one PDF are not duplicate files: two blank pages can render
  to identical bytes honestly, and refusing the second would lose a real page
  (GOALS 1). A reader must not treat two page artifacts sharing one `image_path` as
  evidence that either is spurious.
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

`/out/data_handling_gate.md` is the written policy package this machinery checks
against, delivered to Tyrel for approval rather than tracked here.
