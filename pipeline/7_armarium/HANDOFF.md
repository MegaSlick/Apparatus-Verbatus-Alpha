# Armarium — handoff

The Armarium publishes the terminal `kind="export"` record and one
`kind="manifest-entry"` per expected act. Both are ordinary artifacts under
`7_armarium/artifacts/`; the stage manifest is derived inventory, never a competing
output file.

## Export contract

The export payload contains the aggregate result, the expected-act count, delivered
and review entries, witness coverage, and `pages`. Every `pages` row is one submitted
source ordinal and retains:

```text
ordinal
declared_path
declared_sha256
declared_bytes              when the filename ledger recorded it
ledger_sha256               for a real submission
container_page_index        for a fanned container page or animation frame
outcome and reason
```

This is the final citation link: an output can be matched to the original filename
and source digest, and a PDF/TIFF/animation page can be matched to its zero-based
source page/frame without guessing from the pipeline ordinal.

Each delivered entry's `source_regions` repeats that link for the exact crop used
by its text. A source-region row carries `source_page_ordinal`,
`source_page_id`, `declared_path`, `declared_sha256`, and any applicable byte
count, ledger hash, and `container_page_index`, alongside the crop digest. A
continuation therefore names both original pages it used rather than relying on a
reader to search intermediate artifacts.

## Boundary checks

Before the Armarium publishes any artifact, it reconciles every `run.json`
source-manifest ordinal to exactly one Exemplar page outcome. It independently reads
the one Exemplar `corpus-seal`, verifies its self-hash, page census, and input
references, then compares each row against the source manifest and page artifact.
For every sealed page it also rechecks the Door admission and content-addressed
pixel blob before export. A missing, duplicate, altered, or unaccounted page is
fatal; an Exemplar-refused page remains explicit evidence and contributes to a
visibly partial export rather than disappearing from the page set.

The act-level proposal seal remains the authority for expected acts. The Armarium
places each one in exactly one terminal category and retains a review reason where a
text cannot be delivered. It does not choose among witness readings or put witness
text in output.
