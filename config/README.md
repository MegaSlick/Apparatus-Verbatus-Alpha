# config

The knobs. One question per planned file, each answerable without reading code.

| File | The question it answers |
|---|---|
| `models.toml` | which model and revision fills each numbered role |
| `recovery.toml` | how many times rework may be asked for before review |
| `admitted_formats.toml` | which image formats may enter the pipeline at all |
| `data_handling_policy.json` | how real material is stored, logged, retained and disposed of |
| `spend.toml` | money caps |
| `formats.toml` | which formats the Armarium writes |

`admitted_formats.toml` is the admission list the door reads, and it is
configuration precisely so that Tyrel's open ledger rulings — `.heic`, and whether
PDF is admitted at all — are one line each rather than a code change. It is checked
at load: it must name exactly the formats the door can detect, its actions come from
a closed set, and it may not admit a format nothing can structurally verify.

**That claim was untrue for PDF and is now true.** The door decided the PDF fan-out
from a hardcoded format name and never consulted this file, so `pdf = "refuse"`
counted, rendered and sealed the pages anyway — three reviewing seats found it
independently. The door now asks the list before it counts a page and again before
it renders one, and `pipeline/1_exemplar/test_door.py` drives the shipped file end to
end in both positions of the row. The shipped row is `refuse`, and the file says why.

`data_handling_policy.json` is the version an approval record names. Its hash is the
canonical digest of its own content, so editing one character of it invalidates
every approval that named the old version — which is the honest behaviour, not a
bug. The **data-handling gate package** is the written deliverable that explains this
file to Tyrel and is handed to him rather than tracked here;
`operations/submit/gate.py` is the machinery that enforces it. Note that both entry
points expose the policy's path as a flag, so "the current policy" is whichever file
the invoker names — a documented limit of a mechanism `common/contracts/approval.py`
already describes as tamper-evidence rather than access control.

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
