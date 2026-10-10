# Designator: contract

The Designator publishes the page evidence the Perlector's whole-page reading reads: on every
sealed Exemplar page, Surya's line and block census and the record detector's records and
their crops. It marks out no act and establishes no text. It writes only `skeleton.v1`
artifacts below `2_designator/artifacts/`, each with a derived identity, a self-hash and
digest-checked direct inputs. The derived manifest is inventory, not a second authority.

## Scope and input boundary

Before cutting anything, the Designator reconciles `run.json`'s submitted filename ledger
with every Exemplar page outcome and the self-hashed corpus seal, and checks each sealed
page's Door admission and pixel blob again before its pixels are cropped. That reconciliation
refuses a merged page (two byte-identical files deriving one `page_id`); the Door refuses such
a submission first.

Each chair's sealed serving row says how it answers; a `fixture` row answers only a synthetic
run. A real submission whose roster resolves a chair to a fixture row, and any configured chair
this run cannot run, is refused before the first record is written.

## The record detector

**`secondary-provenance`** is published once per run (subject `"secondary-provenance"`): the
resolved `secondary_proposer` chair, absent or configured. The role is resolved on every run,
so `common/stage.py::unaddressed_chairs` can tell a role nothing resolved from one left absent.

When configured, `secondary_proposer` is DAI's own project's record detector (Teklia's YOLOv26
OBB model, one class, `record`). Its records are the units DAI reads in the Attestatores and
nothing else: page evidence that decides nothing.

- **Where it runs.** In this stage, never as an engine. Its row is `in-process` (verified
  weights loaded on the CPU by `operations/serving/detector.py`, with Ultralytics' network
  paths off) or `fixture`; a `vllm` row is refused. The fixture detector answers each page
  with the fixture's `[[detector_record]]` rows (`page_ordinal`, four `corners`, `score_bp`,
  optional `class_id`, optional `scenario`; a scenario that declares rows reads those alone).
  The detector loads after Surya has finished, so one model is resident at a time. Every
  sealed page is asked once.
- **What it is shown.** The sealed page as 8-bit RGB: modes a sealed crop can arrive in are
  converted as `Image.convert("RGB")` does; other modes first take their display conversion
  (a 16-bit scan scaled to 8 bits); an `I` or `F` page is refused
  (`detector.convert_page_to_rgb`).
- **Raw output.** One retained blob per page, `record-detector-output.v1`: the page, the run
  facts (engine, repository, revision, manifest digest, weights file and digest, package
  versions, device, input size, thresholds), and every detection as the engine gave it (four
  float corners, a float score, a class).

**`detector-page`** (subject the page id, one per sealed page): `page_ordinal`,
`detection_count`, `record_subjects` (engine order), `raw_output_ref`, `provenance`. A page
with nothing found has `detection_count: 0`, distinct from a page never asked.

**`detector-record`** (subject `<page_id>-detector-<n>`, one per detection in engine order):
`page_ordinal`, `detector_ordinal`, `raw_output_ref`, `quantization`, `score_quantization`,
`score_bp`, `class_id`, `class_name` (null for an unnamed class), `raw_proposal` (the
`yolo-obb` record from `geometry_layer.yolo_obb`, keeping the oriented polygon), `bounds` (its
axis-aligned hull), `cut`, `authoritative: false`, `authority_effect: "none"`, `region_ref`
and `provenance`. A record names no act. Text fields are refused on both record kinds.

**`detector-region`** (same subject, one per record that was cut): the hull's pixels, cut by
the stage's one crop path, with `origin: "detector"`, `padding: null`, `raw_bounds`,
`record_key`, `region_id`, image digests and `provenance`. It is its own kind, not `region`,
because every reader of `region` treats its subject as an act.

**Quantization** (`obb-corner-floor-clamp.v1`): each float corner is floored to its pixel and
clamped into the page, and the crop is the axis-aligned hull (`aabb-enclose`;
`config/designator_geometry.toml` keeps `rectify = false`, since nothing states DAI's own
pipeline rectifies). The score is in basis points, rounded half to even
(`score-round-half-even-bp.v1`). A detection whose corners collapse to fewer than three
distinct pixels is kept with `cut: false` and null geometry, never dropped. Two detections
with the same box and score share one geometry proposal but keep a record and crop each.

**Determinism.** The in-process detector loads only weights matching the pinned SHA-256, under
the exact package versions its row names, on the CPU with deterministic algorithms and one
thread. That two independent loads agree is configured, not yet measured. A resumed pass
re-derives the records, and a difference refuses at the immutable publish boundary.

Leaving the chair absent publishes none of these kinds and changes no authoritative outcome
(`pipeline/2_designator/test_secondary_proposer.py`).

## Surya

Surya (`datalab-to/surya`) is an independent, deterministic text-line and layout detector: a
check that no ink goes unseen. Its records carry no text (`no_text.refuse_text_fields` walks
every payload), cut no crop, hold no act, and say `authoritative: false`. They are written by
`pipeline/2_designator/surya_detection.py`; how Surya runs is in
[operations/serving/surya/README.md](../../operations/serving/surya/README.md).

**The chair.** `designator_surya` is resolved every run; absent, nothing is published. A
`fixture` row answers from the fixture's `[[surya_line]]` and `[[surya_block]]` rows on the
fixture pass only; a `subprocess` row runs `operations/serving/surya/runner.py` in Surya's own
locked environment on the CPU, over the chair's verified bundle, on the live pass only. Any
other row, or a row on the other pass, is refused. The row, Surya's reported versions and the
weights are checked before anything is published, and Surya runs before the record detector
loads.

**Workers.** Pages are cut in order into contiguous slices, one runner process each, run at
once: up to the row's `workers` (one if unset), never more than the host's CPUs divided by
`threads` or the page count. Each process numbers its documents from its slice's first page,
so the output is byte for byte what one process over every page would give. A process's
timeout is `startup_timeout_seconds` plus `seconds_per_page` per page of its slice. A timeout,
a runner that cannot start, a failed process and an empty page set are each refused by name. A
fixture row for a page the Exemplar refused is left out; one for any other unsealed page is
refused.

**What runs.** `DetectionPredictor.local()` for text lines and the `LayoutEngine.run_batch`
call Surya's fast-layout server makes (rf-detr layout and the reading-order head), on the whole
sealed page, one page per call, opened as Surya's own loader does (`convert("RGB")`). A page
that conversion would clip to 8 bits is refused before any model loads. Surya's output-shaping
settings must hold their defaults, no `local.env` may sit where Surya would read it, and the
reading-order head must have loaded.

**Reading order.** Surya raster-sorts blocks (top to bottom, then left to right) on a page with
more than 128 detections or when the layout detector returned no feature map; it only logs
either. Each page records `reading_order` (`surya-order-head` or `raster-fallback`) and
`reading_order_reason` (null, or why). A page with no detection is always `surya-order-head`.

**Determinism.** CPU only, a fixed thread count, one interop thread, deterministic algorithms,
eval mode, one page per batch, and no network (checkpoints are local directories;
`HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE` are set). The same bundle, locked environment,
thread count and CPU instruction set give byte-identical documents. Floats may differ across
vector instruction sets, so the run facts and the receipt's `engine_version` name the
instruction set (`torch.backends.cpu.get_cpu_capability()`) and machine; a resumed run on a
different one is refused before reusing that provenance.

**`surya-provenance`** (subject `"surya-provenance"`) is published once per run: the resolved
chair and its serving receipt, reused on resume. The receipt names no token context or pixel
cap (both 0).

**`surya-page`** (subject the page id), one census per sealed page, including a page with
nothing found:

| Field | Meaning |
|---|---|
| `schema` | `surya-page.v1` |
| `page_id`, `page_ordinal`, `page_width_px`, `page_height_px` | the sealed page |
| `line_count`, `block_count` | how many lines and blocks Surya returned |
| `line_subjects`, `block_subjects` | the records below, in Surya's order |
| `reading_order`, `reading_order_reason` | as above |
| `raw_output_ref` | the retained page document, exactly as the runner wrote it |
| `run` | Surya, torch and Python versions, device, CPU instruction set and machine, threads, settings, the three checkpoints with sources and revisions, and every weight file's digest; or the fixture declaration |
| `quantization`, `confidence_quantization` | as below |
| `authoritative` | `false` |
| `provenance` | the `surya-provenance` payload |

**`surya-line`** (`<page_id>-surya-line-<n>`) and **`surya-block`**
(`<page_id>-surya-block-<n>`), one per detection, `n` from 1 in Surya's order, carry `schema`
(`surya-line.v1` / `surya-block.v1`), `page_id`, `page_ordinal`, `n`, `polygon_px`, `bounds`,
`quantization`, `confidence_bp`, `confidence_quantization`, `raw_output_ref`, `authoritative`
and `provenance`. A block adds `label`, `raw_label`, `reading_order_position` (`n - 1`) and
`reading_order`.

- `quantization` is `surya-corner-floor-clamp.v1`: each float corner floored to its pixel and
  clamped, giving `polygon_px`; `bounds` is the half-open hull
  (`geometry_layer.enclosing_aabb`), so a box never excludes a pixel a corner touches.
- `confidence_quantization` is `confidence-round-half-even-bp.v1` (null where Surya gives
  none). A line's confidence is relative within its page (the strongest line reads 1); a
  block's is the detector's class score.

The runner's page document (`verbatus-surya-page.v1`) is checked against a closed shape by
`operations/serving/surya_detector.py` before anything reads it, including run facts that
match the reported versions and weights that are exactly the files the chair's digest manifest
pins. Anything else is refused by name.

## Exit code and seal

`EXIT_COMPLETE` (0) once every sealed page's evidence is published and the stage seal written.
This stage holds nothing: a chair it cannot run, a page whose pixels moved, or a boundary that
does not reconcile is `EXIT_FATAL`.

Before its final manifest the stage publishes one `decode-environment` and one `stage-seal`
(or reuses both on a byte-identical retry), binding the pass's disk inventory and blob
contents, the decode-environment bytes, the run's `config_digest` and `register_digest`, and
the `(kind, outcome)` census. A pass that never reaches its seal leaves none, and the successor
refuses the missing boundary. Seals are compared as the set the stored inventory names.

## Run binding

`config/designator_geometry.toml` (how a detector record's oriented box becomes DAI's crop) is
sealed into `run.json`'s `config_digest`. The stage reads it from `--designator-geometry-config`
and `StageContext.require_sealed_config` refuses unless it seals to the one the run bound, so a
rewrite between run creation and the crop cut is refused.

## Consumers

The Attestatores read `detector-page`, `detector-record` and `detector-region` only for a
page-scoped chair that reads one detector record at a time (DAI). The Perlector reads
`detector-record`, `surya-page`, `surya-line` and `surya-block` as candidate units and layout
evidence (`pipeline/4_perlector/CONTRACT.md`). The provenance records have no downstream
consumer. Every later reader filters this stage's manifest to the kinds it wants, so a new
kind here changes nothing for them.
