# config

The knobs. One question per planned file, each answerable without reading code.

| File | Status and question |
|---|---|
| `models.toml` | present — which model and revision fills each numbered role |
| `recovery.toml` | present — how many times rework may be asked for before review |
| `pdf_render.toml` | present — what whole-page PDF resolution the next run targets |
| `data_handling_policy.json` | present — how real material is stored, logged, retained and disposed of |
| `spend.toml` | present, deliberately unconfigured — Tyrel's pod-plus-attached-volume money caps; both paid paths refuse it until configured |
| `pod_placement.toml` | present — planning-only single-resident GPU resource tiers, dtype capability floors, and the reviewed price sheet for the cards this project rents |
| `formats.toml` | planned — which formats the Armarium writes |

Decoder routing is deliberately not configuration. Tyrel ruled that an uncorrupted
image is never declined by policy, and there is exactly one valid route map: every
raster gets a decoder attempt and is sealed unchanged or fanned out when it has more
than one frame; PDF is always painted page by page. `admission.py` derives that map
from the formats the byte sniffer can name, so a new format cannot route by omission.
A format/variant the installed readers cannot yet decode is a named pipeline alarm
carried with its filename, rather than a routine rejection.

PDF alone uses `render-pages`, and the loader refuses any other format given that
action. PDF is full-page PDFium rasterisation, which paints text, vectors,
annotations, and images together; it is never embedded-image extraction. TIFF is
`admit-or-fan-out`: a single-directory scan seals its own bytes untouched, and a
multi-page one — including the LZW, Deflate, PackBits and CCITT compressions real
flatbed scanners produce — fans out to one ordinal per page. JPEG suffix bytes after
EOI are not called corruption.

`pdf_render.toml` supplies the documented default target for whole-page PDF
rasterisation. `--pdf-target-dpi` overrides it for one run. The run authority records
the configured target and the code-bounded target, and every rendered PDF page records
those beside its `effective_dpi`. The 72-DPI floor, pixel ceiling, and decoded-byte
ceiling remain in code; configuration cannot weaken them. The default is **unmeasured**:
making it adjustable does not prove it suitable, and it should be checked against a
real sample of real material (GOVERNANCE 9).

`data_handling_policy.json` names the storage roots real material may occupy, and
`operations/submit/gate.py` refuses a submission folder, run root or ledger outside
them before a byte is read. **It no longer names an approval**: Tyrel's ruling of
2026-08-09 cut the per-run approval record, and with it the policy-version hash that
made an approval stale when the policy changed. Nothing now binds a run to the policy
version that governed it.

Both entry points expose the policy's path as a flag, so "the current policy" is
whichever file the invoker names — a documented limit, and the reason this is
tamper-evidence rather than access control.

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

`spend.toml` is intentionally a refusal, not a placeholder default. A configured version
must name `max_hourly_usd` and `max_estimated_metered_cost_usd` USD ceilings for the
combined metered pod and attached-volume hourly price and cost through the hard lifetime,
plus a bounded `billing_cutoff_margin_seconds`, laptop heartbeat, and shutdown
polling/deadline. It also names an `account_balance_floor_usd` manual reserve: the runtime
does not observe account balance, and the documented `$50.00` default is unverified until
checked against RunPod before a live run.
It does not authorize retaining or deleting a volume after close: that is a separately
named decision, and every close report states the volume's own ongoing price. The file
itself carries the full key list as comments, so filling it in needs no code reading.

`pod_placement.toml` is planning, not permission. Serving is **sequential** — one model
at a time, as much of the card as stays stable, next model after — so every tier is
single-resident, and what a tier changes is the engine memory fraction, context cap,
pixel cap and batch size that one model gets. Its `card_profile` rows are prebuilt plans
for the cards this project actually rents; an unknown card falls back to computed
placement from the generic tiers. Those rows also carry the reviewed hourly price the
launch gate estimates against, because RunPod publishes no endpoint that quotes a GPU's
price without creating a pod. Naming a card here does not choose one, and no number here
has been benchmarked on real silicon.

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
