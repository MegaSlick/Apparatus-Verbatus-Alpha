# Designator — contract

The Designator publishes the page evidence the Perlector's whole-page reading
reads: on every sealed Exemplar page, Surya's line and block census and the
record detector's records and their crops. It marks out no act and establishes
no text; the Perlector reads whole pages and names the acts itself. It writes
only ordinary `skeleton.v1` artifacts below `2_designator/artifacts/`; each
envelope has a derived artifact identity, a self-hash, and digest-checked
direct inputs. The derived manifest is inventory, not a second authority.

## Stage-completion seal

Before this producer's final manifest it publishes one `decode-environment` and
one `stage-seal`, or reuses both on a byte-identical retry. The seal witnesses
this pass's disk inventory and blob contents, and binds the exact decode-environment
bytes, run `config_digest` and `register_digest`, and `(kind, outcome)` census. An exit
held after publishing stage evidence seals it (holds remain in its census); a
pass that never reaches its seal does not seal, whether it was held or refused
before publishing stage evidence or closed fatally after publishing it, so the
successor correctly refuses the missing boundary.

Seals are compared as the set the stored inventory names, on both sides of the
boundary: the producer refuses to re-seal, and the successor refuses to read,
when any named seal is no longer on disk.

## Scope and input boundary

Before cutting anything, the Designator reconciles `run.json`'s submitted
filename ledger with every Exemplar page outcome and the one self-hashed corpus
seal. A sealed page's Door admission and pixel blob are checked again before its
pixels are cropped. The check is deliberately before the first record is written.

That reconciliation refuses a merged page (two byte-identical files deriving one
`page_id`), whose evidence would otherwise be published twice; the Door refuses
such a submission first.

Each chair's sealed serving row says how it answers, never a flag and never the
ingress route; a `fixture` row answers only a synthetic run. A real submission
whose roster resolves a chair to a fixture row is refused by name after the
Exemplar boundary check and before anything is published. Every configured
chair this run cannot run is refused before the first record is written.

## `kind="secondary-provenance"`

`secondary-provenance` is published exactly once per run, subject
`"secondary-provenance"`: the resolved `secondary_proposer` chair, absent or
configured, in the shape every chair's provenance takes. The chair is optional
in configuration (an absence is a recorded `AbsentChair`), but the role is
resolved on every run, so `common/stage.py::unaddressed_chairs` can tell a role
nothing resolved from one deliberately left absent.

## `kind="detector-page"`, `kind="detector-record"`, and `kind="detector-region"`

When `secondary_proposer` is configured it is DAI's own project's record
detector (Teklia's YOLOv26 OBB model, one class, `record`), and these three
kinds are what it found. They are the units DAI reads in the Attestatores and
nothing else: page evidence that decides nothing.

**Where the detector runs.** This stage runs it itself and never launches an
engine for it. Its catalogue row is `in-process` (the verified weights, loaded
on the CPU by `operations/serving/detector.py`, with Ultralytics imported with
its network paths off) or `fixture`; a `vllm` row is refused, and a `fixture`
row answers only the fixture pass. The fixture
detector answers each page with the fixture's `[[detector_record]]` rows for
that page (`page_ordinal`, four `corners`, `score_bp`, optional `class_id`,
optional `scenario`): a scenario that declares rows of its own reads those
alone, and any other scenario reads the unscoped rows. The shipped fixture
declares one record over each act's ink on each page, and the committed
roster configures the detector on that fixture row, so every fixture run reads
them.
The row is checked before anything is published and the detector is loaded
after Surya has finished, so one model is resident at a time; a resumed pass
reuses the `secondary-provenance` it already sealed. Every sealed page is asked
once.

**What the detector is shown.** The sealed page as 8-bit RGB: a page in a mode
a sealed crop can arrive in converted as `Image.convert("RGB")` converts it;
every other mode first takes the display conversion its record crops take (a
16-bit scan scaled to 8 bits), then RGB; an `I` or `F` page, which has no
display conversion, refused by name (`detector.convert_page_to_rgb`).

**The raw output.** One retained blob per page, schema
`record-detector-output.v1`: the page, the run facts (engine, repository,
revision, manifest digest, and for the in-process engine the weights file and
digest, package versions, device, input size and thresholds), and every
detection exactly as the engine gave it — four float corners in page pixels, a
float score, a class.

**`detector-page`**, subject the page identity, one per sealed page:
`page_ordinal`, `detection_count`, `record_subjects` (in the engine's order),
`raw_output_ref` and `provenance` (the sealed `secondary-provenance`). A page
the detector found nothing on has `detection_count: 0`, which reads
differently from a page never asked.

**`detector-record`**, subject `<page_id>-detector-<n>`, one per detection in
the engine's order: `page_ordinal`, `detector_ordinal` (`n`), `raw_output_ref`,
`quantization`, `score_quantization`, `score_bp`, `class_id`, `class_name` (null
for a class the checkpoint does not name),
`raw_proposal` (the `yolo-obb` record `geometry_layer.yolo_obb` builds, which
keeps the oriented polygon), `bounds` (its axis-aligned hull), `cut`,
`authoritative: false`, `authority_effect: "none"`, `region_ref` and
`provenance`. A record names no act: it is page evidence, not a claim about
any act's coverage. Text fields are refused on both record kinds; a detector reports boxes, not words.

**`detector-region`**, same subject, one per record that was cut: the hull's
pixels, cut by the stage's one crop path, with `origin: "detector"`,
`padding: null`, `raw_bounds` equal to the transform's bounds, `record_key`,
`region_id`, the image digests and `provenance`. It is its own kind, not
`region`, because every reader of `region` treats its subject as an act and its
bounds as act coverage, and a detector box is neither.

**Quantization.** `obb-corner-floor-clamp.v1`: each float corner is floored to
the pixel it falls in and clamped into the page, and the crop is the
axis-aligned hull of those four points (`aabb-enclose`;
`config/designator_geometry.toml` keeps `rectify = false`). A rotated record is
not rectified before DAI reads it, because nothing states that DAI's own
pipeline does so. The score is recorded in basis points, rounded half to even
(`score-round-half-even-bp.v1`). A detection whose corners collapse to fewer
than three distinct pixels encloses no crop: its record is kept with
`cut: false`, `bounds: null`, `raw_proposal: null` and `region_ref: null`,
never dropped. Two detections that quantize to the same box and score share one
geometry proposal and still keep a record and a crop each. A proposal's
`observed_ordinals` index the retained `record-detector-output.v1` detections:
`geometry_layer.yolo_obb` takes each detection's source ordinal, so the
ordinals stay true when the caller passes only some detections.

**Determinism.** The in-process detector loads only weights whose SHA-256
matches the pin, only under the exact package versions its catalogue row names,
on the CPU with deterministic algorithms and one thread, so that the same sealed
page gives the same boxes. That two independent loads agree is configured, not
yet measured. A resumed pass re-derives the records, and a difference meets the
RunTree's immutable publish boundary and refuses.

**They decide nothing.** No detector record holds or names an act. Leaving the
chair absent publishes none of the three kinds and changes no authoritative
outcome (`pipeline/2_designator/test_secondary_proposer.py`).

## `kind="surya-provenance"`, `kind="surya-page"`, `kind="surya-line"` and `kind="surya-block"`

Surya (`datalab-to/surya`) is an independent, deterministic text-line and
layout detector: a check that no ink goes unseen. Its records are evidence for
later stages to account against. None carries text
(`no_text.refuse_text_fields` walks every payload), none cuts a crop or holds
an act, and every one says `authoritative: false`. The records are written by
`pipeline/2_designator/surya_detection.py`.

**The chair.** `designator_surya` is resolved every run. Absent, nothing is
published and the sealed roster says why. Configured, its serving row decides
how it answers, and each row answers one pass only, so a run's receipts are
never a mix of declared and real ones: a `fixture` row answers from the
fixture's `[[surya_line]]` and `[[surya_block]]` rows, on the fixture pass
only; a `subprocess` row runs Surya's runner
(`operations/serving/surya/runner.py`) in Surya's own locked environment, on
the CPU, with the row's thread count, over the chair's verified weight bundle,
on the live pass only. Any other row, or a row on the other pass, is refused.
The row, the versions Surya's environment reports and the chair's weights are
checked before anything is published, and Surya runs before the record
detector loads, so it never shares the machine with another model.
The pages are cut in page order into contiguous slices, one runner process
each, run at the same time: as many as the row's `workers` (one when the row
names none), never more than the host's CPUs divided by `threads` and never
more than there are pages. Each process reads its slice with the row's thread
count and numbers its documents from its slice's first page, so every page is
read with exactly the settings one process over every page would use and the
documents, records and receipt are byte for byte what that one process would
give. A process's timeout is the row's `startup_timeout_seconds` plus
`seconds_per_page` for each page of its slice; a timeout, a runner that cannot
start, a failed process (the first in page order, after every process has
finished), and an empty page set are each refused by name. A
fixture row declared for a page the Exemplar refused is left out, since the
door already records that loss by name; a fixture row for any other page that
is not sealed is refused by name.
The fixture roster configures the chair against fixture rows; the real roster
configures it as a local repository, Surya's locked weight bundle pinned by its
measured digest manifest, with a `subprocess` row at every tier. The pod's model
store fetches the bundle at launch and places it where the roster binds it
(`operations/serving/surya/README.md`, "On the pod").

**What runs.** Surya's own `DetectionPredictor.local()` for text lines and the
`LayoutEngine.run_batch` call its fast-layout server makes (rf-detr layout and
the learned reading-order head), each on the whole sealed page, one page per
call, as Surya's own image loader opens it (`convert("RGB")`). A page in a mode
that conversion would clip to 8 bits (a 16-bit scan, for one) is refused by
name before any model loads; the modes read are those the project replays a
vendor's RGB conversion for. Surya chunks a tall page itself
(`DETECTOR_IMAGE_CHUNK_HEIGHT`); nothing here tiles or rescales a page.
Surya's output-shaping settings must hold Surya's defaults, no `local.env`
settings file may sit where Surya would read it, and the reading-order head
must have loaded from the bundle; the runner refuses rather than record a run
that differs.

**Reading order.** Surya orders a page's blocks with its learned reading-order
head, but raster-sorts them (top to bottom, then left to right) on a page with
more detections than the head takes (`MAX_BOXES`, 128), or when the layout
detector returned no feature map for the head to read. Surya only logs either
fallback. The detections are kept either way, and each page records which
ordering its block positions come from: `reading_order` is `surya-order-head`
or `raster-fallback`, and `reading_order_reason` says why a page fell back, or
is null. A page with no detection is always `surya-order-head`, since there is
nothing to order. A page with one detection is `surya-order-head` when the
layout detector returned a feature map, the head's trivial order, and
`raster-fallback` when it did not.

**Determinism.** CPU only, a fixed torch thread count, one interop thread,
`torch.use_deterministic_algorithms(True)`, models in eval mode, batch size one
page, and no network at run time. No network rests on two things: every
checkpoint is handed to Surya as a local directory in the bundle, which Surya's
loaders use before any fetch, and the Hugging Face libraries run with
`HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE` set. Surya has no offline switch of
its own. Two runs with the same weight bundle, the same locked environment, the
same thread count and the same CPU instruction set produce byte-identical page
documents; `operations/serving/test_surya_environment.py` checks this on the
synthetic pages with stand-in weights wherever Surya's environment is synced.
What is not guaranteed: identical floats across CPUs whose vector instruction
sets differ, since torch picks kernels by instruction set. The run facts and
the receipt's `engine_version` therefore name the instruction set torch chose
(`torch.backends.cpu.get_cpu_capability()`) and the machine. A resumed run whose
engine or instruction set differs from the sealed receipt's is refused before it
reuses that provenance, and a resumed run that re-derives a different document
refuses at publication rather than overwrite what was sealed.

`surya-provenance` (subject `"surya-provenance"`) is published once per run:
the resolved chair and its serving receipt, in the shape every chair's
provenance takes. A resumed pass reads it back and reuses it, since a second
receipt would name a second serving moment. The receipt names no token context
or pixel cap (both 0) on either pass: a detector has neither.

`surya-page` (subject: the page id) is one census per sealed page, including a
page Surya found nothing on (`line_count` and `block_count` zero), so a page
found empty reads differently from a page never asked:

| field | meaning |
|---|---|
| `schema` | `surya-page.v1` |
| `page_id`, `page_ordinal`, `page_width_px`, `page_height_px` | the sealed page |
| `line_count`, `block_count` | how many lines and blocks Surya returned |
| `line_subjects`, `block_subjects` | the records below, in Surya's order |
| `reading_order`, `reading_order_reason` | `surya-order-head` with a null reason, or `raster-fallback` with the reason Surya fell back |
| `raw_output_ref` | the retained page document, exactly as the runner wrote it |
| `run` | what ran: Surya, torch and Python versions, device, the CPU instruction set torch used and the machine, threads, the settings it ran with (as text), the three checkpoints with their sources and revisions, and every weight file's digest; or, for a fixture row, the fixture declaration |
| `quantization`, `confidence_quantization` | as below |
| `authoritative` | `false` |
| `provenance` | the `surya-provenance` payload |

`surya-line` (subject `<page_id>-surya-line-<n>`) and `surya-block` (subject
`<page_id>-surya-block-<n>`) are one per detection, `n` counting from 1 in
Surya's own order: text lines as the detector returned them, blocks in Surya's
reading order. Both carry `schema` (`surya-line.v1` / `surya-block.v1`),
`page_id`, `page_ordinal`, `n`, `polygon_px`, `bounds`, `quantization`,
`confidence_bp`, `confidence_quantization`, `raw_output_ref`, `authoritative`
and `provenance`. A block adds Surya's `label` and `raw_label`, its 0-based
`reading_order_position` (equal to `n - 1`, since Surya returns blocks in
reading order), and `reading_order`, the page's ordering that position comes
from: the head's order, or a raster sort.

`quantization` is `surya-corner-floor-clamp.v1`: each of Surya's four float
corners is floored to the pixel it falls in and clamped to the page, giving
`polygon_px`; `bounds` is the half-open hull of those pixels
(`geometry_layer.enclosing_aabb`), so a box never excludes a pixel a corner
touches. Surya's float corners stay in the retained document.
`confidence_quantization` is `confidence-round-half-even-bp.v1`: the confidence
in basis points, rounded half to even from its shortest decimal form, or null
where Surya gives none. A text line's confidence is relative within its page:
Surya divides each line's peak heatmap score by the highest on that page, so
the page's strongest line reads 1 and a value compares lines on one page only.
A layout block's confidence is the detector's own class score.

The runner's page document (`verbatus-surya-page.v1`) is checked against a
closed shape by `operations/serving/surya_detector.py` before anything reads
it: Surya's own `TextDetectionResult` and `LayoutResult` dumps, the page's size
and `image_bbox`, block positions equal to their order, finite coordinates,
confidences in [0, 1], a layout `error` of false, a block `count` of 0 (Surya's
fast layout never sets it, so no record carries it), the reading order and its
reason, and run facts with no floats whose checkpoints and weight rows pass the
bundle lock's own checks (`contract.check_checkpoints`, `contract.check_file_rows`).
The parent also refuses run facts that name other versions than the environment
reported, and weights that are not exactly the files the chair's digest
manifest pins, less the bundle's own lock. Anything else is refused by name.

## Exit code

`EXIT_COMPLETE` (0) once every sealed page's evidence is published and the
stage seal is written. This stage holds nothing: a chair it cannot run, a page
whose pixels moved, or a boundary that does not reconcile is refused (`EXIT_FATAL`)
before or during publication, and a pass that never reaches its seal leaves no
boundary for the next stage to read.

## Run binding

`config/designator_geometry.toml` is sealed into `run.json`'s `config_digest`
with the rest of the run's configuration. It decides how a detector record's
oriented box becomes the crop DAI reads. The stage reads the policy from the
run's own `--designator-geometry-config` argument and
`StageContext.require_sealed_config` refuses unless the policy it read seals to
the one the run bound (`common/sealed_config.py`: the digest of what the file
says, so a comment edit moves none). A rewrite between run creation and the
crop cut is therefore refused rather than cut under a policy `run.json` never
sealed.

## Consumers

`detector-page`, `detector-record` and `detector-region` are read by the
Attestatores, and only for a page-scoped chair that reads its page one detector
record at a time (DAI): they are its units. The Perlector's whole-page reading
reads `detector-record`, `surya-page`, `surya-line` and `surya-block` as the
page's candidate units and layout evidence (`pipeline/4_perlector/CONTRACT.md`).
`secondary-provenance` and `surya-provenance` have no consumer downstream of
this stage. Every later stage's reader of this stage's manifest filters to the
kinds it wants, so a new kind appearing here changes nothing for them by
construction.
