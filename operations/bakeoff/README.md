# Witness bake-off

A small bench, kept apart from the pipeline. It runs each candidate model natively (its
vendor's own prompt, image rule and sampling), caches every raw answer, and scores each
model against the lead's per-page transcriptions. Nothing here touches a run tree, a seal
or a receipt. [RUNBOOK.md](RUNBOOK.md) is the step-by-step for a bake-off day; run cards
for every model are in [cards/](cards/README.md).

| File | What it does |
|---|---|
| `witness_run.py` | starts a model's vLLM server, sends every page, writes one JSON per page |
| `arms.py` | what each model is sent: prompt, image preparation, sampling, answer bound; DAI's record detector |
| `gold.py` | reads the lead's `<stem>.txt` files (the bake-off set's `TEMPLATE.txt` format) |
| `score.py` | normalises each answer to plain text and writes `scores.jsonl` and `scores.md` |
| `roster.py` | roster suggestions from the same scores |
| `queue_runner.py` | runs one pod's whole day from a manifest and ends the pod |
| `weights.py` | fetches and checks the weights each arm reads |
| `fed_arm.py` | `qwen-fed`: the Perlector's own page request, rebuilt from a sealed run tree, sent to any served model |
| `fed_score.py` | the Perlector scorecard |
| `mutations.py` | the trap generator: rewrites witness testimony in a sealed feed |
| `groups.py` | which page groups each arm is scored on |
| `native/`, `lines/` | vendor-native arms and CTC line arms |
| `fake_vllm_server.py` | a stand-in server for the tests; no GPU |

## The vLLM arms

| Name | Model | Shown | Prompt and answer bound |
|---|---|---|---|
| `chandra` | datalab-to/chandra-ocr-2 | the whole page, the vendor's `scale_to_fit` | vendor `ocr_layout` prompt, thinking off, first attempt only; 12,384 tokens when it surely fits, else the context's remainder |
| `dai` | Teklia RecordGold ATR (Qwen2.5-VL-7B) | each record crop from Teklia's YOLO record detector (CPU), width at most 1,500 px | Teklia's `system.txt` and `query.txt`; 1,024 tokens per record; record texts joined in page order (left column first on a spread) |
| `churro` | stanford-oval/churro-3B | the whole page within 2,500 px | vendor `registry-v0.3.0` system prompt; 25,000 tokens |
| `qwen-blind` | any vLLM vision model (`--weights`) | the whole page | a plain verbatim prompt (`[[?]]` for unread ink, one line per written line), greedy, thinking off, 12,288 tokens: the reader with no witnesses |
| `qwen-vendor` | the same Qwen readers | the checkpoint's own pixel bounds (65,536 to 16,777,216, via `--mm-processor-kwargs`) | the Qwen3-VL OCR cookbook's instruction around the project's verbatim rules, no system prompt, the model card's non-thinking sampling (temperature 0.7, top_p 0.8, top_k 20, presence_penalty 1.5) |

**DAI.** A page with no record found is a valid, empty result, unless
`--record-fallback whole-page` shows the whole page as one record (marked `whole-page`).
`--detector-conf` and `--detector-imgsz` change what the detector is asked (defaults: the
serving row's 0.25 and 1024). The `_records/` cache keeps each record's confidence and
settings and is rebuilt when they change.

**`qwen-vendor`.** `--repo` picks the family preset (`arms.VENDOR_PRESETS`), recorded as
`vendor_preset`. A page can reach about 16,400 image tokens, so pass
`--max-num-batched-tokens 16384`.

**Server shape.** Sampling is the sealed per-chair rows of `config/decoding.toml`; the
server shape is each model's row in `config/serving_recipes_real.toml`, except that the
bake-off keeps the card full: `--gpu-memory-utilization 0.92`, `--max-num-seqs 32`, and
twice that many requests in flight (`--concurrency`). `--recipe NAME` serves another row,
from the catalogue or `config/serving_recipes_real_variants.toml` (FP8, NVFP4 and MTP
Perlector shapes). `--allowed-tokens latin-json-v1` (off by default) restricts output to
Latin-script, digit and punctuation tokens (`allowed_tokens.py`); each request record names
the set and its digest.

**The guard (`--guard`).** `perlector`, the default for the reader arms (`qwen-blind`,
`qwen-vendor`), applies the Perlector's two guards: a request with no reply cap gets the
Perlector's page cap (`page_max_tokens`, 12,288, or the context the prompt leaves, whichever
is smaller), and every reply streams through the loop detector (`common/repetition_loop.py`:
the same line 30 times, or the same block of 2 to 8 lines 10 times, abandons it). `none`, the
default for the witness arms, sends the vendor's request unchanged. Each record names its
guard and why any request was cut short (`stops`: `repetition-loop` or `request-timeout`).

## What a cached page holds

`<out>/<model>/<page stem>.json`: model, repo, revision, weights path, the server's argv;
per request the prompt digest, sampling, `max_tokens` and why, the image size and digest,
the raw response, `finish_reason`, `usage`, seconds and any error; the page's text, an
`empty` flag and a `loop` flag (the same line 30+ times in a row, or `finish_reason`
length). DAI's record boxes are in `<out>/dai/_records/`. Beside them: `run.json` (wall time
and throughput), `server.log`, and at the top `events.jsonl` (server start, ready, sending,
stop) and `gpu-<time>.csv` (`nvidia-smi` every 10 s).

A page cached without an error is skipped next time, so a run resumes; a page with an error
is sent again.

## Running by hand on a pod

One 24-48 GB card runs `chandra`, `dai` and `churro`; `qwen-blind` with a 27B model needs
the 96 GB card. Start the pod with its guard armed (`operations/pod/README.md`); this tool
never starts or stops a pod. Page images go to the pod, never into git:

```sh
mkdir -p ~/bakeoff-pages && cp "$HOME/Desktop/Bake-off set/Pages"/*/Prepped/*.tif ~/bakeoff-pages/
scp -P <port> -r ~/bakeoff-pages root@<ip>:/workspace/private/bakeoff-pages
```

On the pod:

```sh
V=/workspace/private
cd /opt/verbatus && git fetch && git checkout --detach <commit>
bash operations/pod/prepare_runtime.sh
UV_CACHE_DIR=/tmp/verbatus-uv-cache uv sync --frozen --group pod

# Weights: an existing model store at $V/model-store/hf/<artifact> is used as it is.
.venv/bin/python -m operations.bakeoff.weights fetch --store-root $V/model-store chandra-ocr-2 ...

# A two-page smoke first:
.venv/bin/python -m operations.bakeoff.witness_run run --model chandra --limit 2 \
  --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache --store-root $V/model-store

# The full run, detached. While one model is on the card the next one's pages and
# weights are prepared, so the next server starts as soon as the last answer is in.
setsid nohup .venv/bin/python -m operations.bakeoff.witness_run run-all \
  --models chandra,dai,churro --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache \
  --store-root $V/model-store --stage-dir /var/tmp/bakeoff-weights \
  > $V/bakeoff/run-all.out 2>&1 < /dev/null &
tail -f $V/bakeoff/witness-cache/events.jsonl

# A reader arm, on the big card:
.venv/bin/python -m operations.bakeoff.witness_run run --model qwen-vendor \
  --label qwen35-27b-vendor --repo Qwen/Qwen3.5-27B --revision <commit> \
  --weights <local snapshot> --max-num-batched-tokens 16384 \
  --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache
```

`--label` names the cache folder, so two models of one arm can sit side by side. Re-running
a command finishes whatever is missing. Bring the cache home into the gitignored `private/`
before the pod goes:

```sh
scp -P <port> -r root@<ip>:/workspace/private/bakeoff/witness-cache private/bakeoff/
```

## Queue runner

`queue_runner.py` runs one pod's whole day from a manifest (`queue/*.toml`, schema
`bakeoff-queue.v1`) and ends the pod itself. [RUNBOOK.md](RUNBOOK.md) has the full procedure.

```sh
.venv/bin/python -m operations.bakeoff.queue_runner validate --manifest <manifest>
.venv/bin/python -m operations.bakeoff.queue_runner run --dry-run --manifest <manifest>
setsid nohup .venv/bin/python -m operations.bakeoff.queue_runner run --manifest <manifest> \
  > /workspace/private/bakeoff/queue-<name>.log 2>&1 < /dev/null &
```

`validate` and `run --dry-run` print when the GPU lane and CPU arms would end at 8, 16 and 32
vCPU (and on this machine), and what the arms download, so a pod with too few CPUs shows
before it is rented.

- **Arms.** Each runs a smoke of `smoke_pages` pages, then the full run. A GPU `witness_run`
  arm loads its model once: the smoke leaves its server up (`--keep-server`) and the full run
  adopts it (`--adopt-server`) if it is the same command and still answers.
- **Lanes.** GPU arms hold the card one at a time, in manifest order. `gpu = false` arms run
  beside them, each taking its `--threads` from the manifest's `cpu_threads` (a number, or
  `"auto"`: the pod's CPU quota less 2); without `cpu_threads` one CPU arm runs at a time.
  `install` and `prepare` run one at a time and may use the network; every arm's command runs
  offline (`HF_HUB_OFFLINE=1`).
- **Dependencies.** `after = ["surya-lines"]` names arms that must finish ok first. Dependants
  of a failed arm wait for its retry, and are skipped with the reason if it fails for good.
- **Failures.** A failed arm is retried once at the end, smoke first, in its own lane. A page
  that hit the request timeout or the loop detector is recorded failed with its reason and not
  resent under the same settings (`settings_sha256`). An arm that answered no page is `failed`
  and not retried; one with some failed pages is `ok-with-failures`. Time boxes never kill
  work: an overrun is pinged once, and the `cut` rule only skips later arms (`overrun`,
  `behind-schedule`, `install-failed`; `never` always runs). `hard_stop_min` stops the arm in
  flight and ends the day early.
- **Weights.** Each arm's `prepare` is `python -m operations.bakeoff.weights fetch
  --store-root STORE NAME ...`. Roster artifacts (`chandra-ocr-2`, `dai-recordgold-atr`,
  `churro-3B`, `yolov26-record-detection`, `qwen3.8-27B`, `surya2-detection`) go through the
  project's model store and must match `config/models-real.toml`'s pins; the bake-off's own
  snapshots (`qwen3.5-27b`, `qwen3.5-9b`, `DotsMOCR`) are pinned file by file in
  `weight_pins.json`; line-arm weights use their module's checked fetch. No repository is
  gated. `weights sizes NAME ...` prints sizes.
- **Watching.** `status.json` is rewritten every 30 s and queue events join `events.jsonl`.
  The phone hears `Milestone` (including `arm failed: ...`), `Needs a decision` when the queue
  cannot go on by itself, and `Queue finished` at the end. From the Mac, `queue_runner watch
  --ssh "ssh -p <port> root@<ip>" --status <status.json>` prints changes and exits 0 at
  `DONE.json` (1 on failure; it warns after 10 unchanged minutes), and `queue_runner fetch
  --ssh ... --remote <cache> --into <dir>` copies the cache home, checking every file against
  `DONE.json`.
- **The end.** The queue copies the cache to `sync_to`, compares every file's sha256, writes
  `DONE.json` to both, pings, and ends the pod: through the guard's deadline when its
  heartbeat is under 5 minutes old, otherwise `operations/pod/pod_delete.sh`. With `own_disk =
  true` it refuses to end the pod unless the copy verified; `end_pod = "none"` keeps it. `run
  --sync-to PATH --own-disk --keep-pod` set the same from the command line. On a global volume
  the copy falls back from `rsync -rt` to `rsync -r --inplace` to a plain copy; with
  `--own-disk --keep-pod` it is not read back on the pod (`"verified": null`), and `fetch`
  verifies at home before the pod is deleted.
- SIGTERM stops every arm, pings and exits 143 without ending the pod. Arms never see
  `RUNPOD_API_KEY` or `NTFY_TOPIC`.

## Scoring, on the Mac

```sh
.venv/bin/python -m operations.bakeoff.score --cache private/bakeoff/witness-cache \
  --gold "$HOME/Desktop/Bake-off set/Pages" --gold-glob '*/Prepped/*.txt' \
  --out private/bakeoff/scores
```

- The reference is all act text in order, then headings and rows. `[[?]]` is dropped,
  `[[word|other]]` keeps its first reading, struck text is dropped, inserted text kept.
- Each answer becomes plain text: Chandra's layout HTML through `common/chandra_layout.py`,
  Churro's XML through `common/churro_document.py`, DAI's `[UNCERTAIN]`/`[CROSSED_OUT]`
  tokens removed, `<think>` blocks and markdown table rules removed.
- CER and WER are `operations/corpus/scoring.py`'s (graphemic-v1: case, accents, spelling
  and punctuation count; line breaks do not). An answer past 20,000 characters (a loop) is
  scored by plain Levenshtein, and says so.
- Line recall: each gold row matched to at most one model line at CER <= 0.3, best pairs
  first (greedy).
- A page with an error or no reading still counts as an empty answer; a gold page with no
  cached answer is counted under `missing`.
- While the gold files say `STATUS: fool's gold`, every heading says **vs fool's gold
  (ballpark, not accuracy)**.

### Fair scoring and the roster

A model is scored only where its design applies, by the metric that fits the page type, and
never pooled across types. `groups.py` holds the table, checked by `validate()`:

| Group | Gold categories | Headline | Also |
|---|---|---|---|
| `acts` | acts-18c, acts-19c, acts-20c | CER | WER |
| `prose-other` | contract | CER | |
| `tables` | ledger | line recall | CER |
| `index-list` | index, list | line recall | surname recall, false-line rate |
| `blank-like` | blank, near-blank, non-register | false-text rate | CER |
| `test` | any page with `TEST PAGE: yes` | per page, for the lead | |

`MODEL_GROUPS` says which groups each arm is scored on: `dai` on `acts` only, `pylaia-popp-*`
on `index-list`, `tables` and `acts`, every other arm on every group (an unlisted arm is
scored everywhere, with a warning). False text: more than 20 characters on a page with no
gold text. Surname recall: each gold row's first token among the model's tokens at distance
<= 1. False-line rate: model lines matched to no gold row or heading. Pages are also split
by form (handwritten, typed, printed form, mixed). A record arm also gets act recall (units
matched to gold acts at CER <= 0.5), unmatched units, per-unit CER and whole-page
fallbacks.

`scores.md` has a cross-model table per group, a section per model, the test pages and the
hard pages, laid out in narrow tables that read on a phone. `--hard-pages FILE` (one stem per
line) lists those pages apart (they still count); `--exclude FILE` drops pages.

`python -m operations.bakeoff.roster` takes the same arguments; `roster.md` gives, per
group and candidate against `--baselines` (default `chandra,dai,churro`): rescue rate, phi
correlation of wrong tokens with each baseline, shared fabrication, insertion rate, union
line recall on index-list, and the roster rule's suggestion. It is a suggestion for the
lead, never a decision.

## The fed-witness reader arm (`qwen-fed`)

`fed_arm.py` sends a reader the Perlector's whole page request, witnesses included, so a base
model, a LoRA adapter or a merged checkpoint can be judged in the Perlector's seat without
running the pipeline. It reads a sealed run tree (stages 1-4 done; Exemplar and Perlector
seals verified, every feed, call record and render digest-checked) and never writes to it.
It rebuilds each request with `common.page_prompt.build_page_prompt` and
`operations.serving.http.request_body`; `prompts` checks the rebuild against what the run
recorded, and that this checkout's sampling and loop guard are the run's sealed ones, and
exits 1 on any difference.

```sh
.venv/bin/python -m operations.bakeoff.fed_arm prompts --run-tree <run tree>
.venv/bin/python -m operations.bakeoff.fed_arm run --run-tree $V/runs/<run id> \
  --out $V/bakeoff/fed-cache --label qwen38-base-greedy \
  --server-url http://127.0.0.1:8190 --concurrency 16
```

- **Server.** `--server-url` for one already up, or `--weights` to start vLLM with the
  Perlector's serving row (`--recipe` for a variant row; a quantized row is refused on a
  snapshot that does not declare it). `--model-name` defaults to the name the run's call
  records carry, since it is part of the request bytes; another name needs
  `--accept-new-model-name`. `--revision` and `--repo` name the checkpoint.
- **Sampling.** `--sampling greedy` (default: temperature 0, so arms differ by input, not
  chance) or `sealed` (the Perlector's row: with the run's served name and an unchanged feed,
  the request is the run's own byte for byte, which measures the noise floor). Replies stream
  under the sealed loop guard (`--no-stream` sends one plain request with no guard).
- **Refusals.** A request that no longer matches the run's needs `--accept-new-builder`;
  changed decoding settings need `--accept-new-config`. A page that hits `--request-timeout`
  is a terminal failure, not resent under the same settings. A label holds one setup; a page
  cached under another is refused.

Variants change the feed before the prompt is rendered:

| Option | What the reader is shown |
|---|---|
| `--drop-witness attestator_2` | the feed without that witness |
| `--add-witness attestator_4=<witness cache>/dots-mocr` | a bake-off witness as one more row |
| `--letter-map attestator_1=C,attestator_3=A` | other letters for the same witnesses |
| `--witness-order attestator_3,attestator_2,attestator_1` | the rows in another order |
| `--image none`, `blur` (`--blur-radius 8`), `blank`, `swap` | no image, the render blurred, a white page, or the next page's render |
| `--mutations <dir>` | a trap from `mutations.py` (below) |

`prompts --show <ordinal>` prints a page's variant prompt. Each page is cached as
`<out>/<label>/<page stem>.json` (`bakeoff-fed-page.v1`), with the raw stream beside it as
`<page stem>.sse.gz`; the answer is judged as the pipeline reads it
(`page_answer.parse_page_answer_repaired`).

## The Perlector scorecard

```sh
.venv/bin/python -m operations.bakeoff.fed_score --run-tree <run tree> \
  [--answers <fed cache>/<label>] [--compare <fed cache>/<other label> --names A,B] \
  --gold "$HOME/Desktop/Bake-off set/Pages" --gold-glob '*/Prepped/*.txt' \
  --hard-pages private/bakeoff/hard-pages.txt --out <dir>
```

It scores one answer set (the run's own readings, or a `fed_arm` label) and optionally a
second beside it, writing `scorecard.md` and `scorecard.json`. Witnesses are named by arm or
label, never by letter. Per page group and for the hard pages it reports:

- **reading**: CER on parsed pages and on all pages (unparsed read as empty), act recall
  (CER <= 0.5), row and surname recall, inserted words, false text on blank pages;
- **answer health**: parsed, malformed, repaired, errors, finish reasons, loop stops, tokens,
  seconds;
- **scepticism**: how often the reader follows a witness that alone is right, resists one that
  alone is wrong, copies a witness's wrong word, recovers when all are wrong, or damages what
  all had right, and whether it beats the vote; rates carry a 95% interval from resampling
  pages;
- **failures count**: a page with gold and no answer is an empty reading, so failing hard
  pages cannot raise a score;
- **with `--compare`**: paired per-page differences with intervals. Both sets must share run
  tree, variant, mutation, sampling, seed, token cap and decoding digest. Run the same arm
  twice first to see the noise floor.

Every heading says **vs fool's gold (ballpark, not accuracy)** unless every scored page in
both sets carries a checked status (`gold (<who> <date>)` or `lead-checked`).

## The trap generator (`mutations.py`)

`mutations.py` rewrites a sealed feed's witness testimony, never the page, so one page can be
shown under many witness stories while the right answer stays the same. The training exporter
(`operations/training/`) draws its mix from it.

| Family | Scenarios |
|---|---|
| honest | `honest`: the sealed feed |
| planted | `plant-1` (one witness wrong on 2-5% of its words), `plant-2` (two witnesses with the same wrong word), `plant-3` (every witness wrong on one settled word) |
| removed | `blind` (no witness rows), `drop-one`, `failed-one`, `empty-one` |
| structural | `dropped-act`, `invented-act`, `merged-entries`, `normalised` (`St` to `Saint`, `7bre` to `septembre`, accents added), `name-swap`, `injection` (a witness carries an instruction), `permute`, `blank-chatty` |

Errors are planted only on reference words whose status is `checked` or `agreed` and that every
target witness has right, so the ink settles every trap. Names, dates and numbers are
oversampled four to one, and planted forms look like reading errors. Everything is
deterministic from (page sha, scenario, seed, turn). From a bake-off gold file,
`statuses_from_agreement` marks a word `agreed` when two or more shown witnesses have it,
`unresolved` inside `[[a|b]]`, and `draft` otherwise.

```sh
.venv/bin/python -m operations.bakeoff.mutations --run-tree <run tree> \
  --gold "$HOME/Desktop/Bake-off set/Pages" --gold-glob '*/Prepped/*.txt' \
  --scenario plant-1 --seed 0 --out <dir>
.venv/bin/python -m operations.bakeoff.fed_arm run --run-tree <run tree> --mutations <dir> ...
```

Each record (`witness-mutation.v1`) holds the mutated feed and a sidecar (`planted` sites,
`changes`, `set_aside_ids`, `notes`) and names its reference by `reference_sha256` and
`reference_record_sha256`; `fed_arm` refuses a record without them or planted from another
reference. The scorecard adds a **Planted errors** block (resisted, copied, or wrong another
way). `mutations.json` reports the **voting must lose** check (`vote_check`): how often a
plurality vote of the shown witnesses is wrong on names, dates and numbers, against a 25-35%
target.

## Vendor-native arms (`native/`)

These run three witnesses through their vendors' own code, so a weak score cannot be blamed
on our re-implementation. Each writes the same cache record under its own label.

| Arm | Vendor path | Client environment |
|---|---|---|
| `chandra-native` | datalab's `chandra-ocr` 0.2.0 package end to end | `native/venvs/chandra-native` |
| `churro-native` | Stanford's `run_churro_ocr.py` settings and XML text extractor | `native/venvs/churro-native` |
| `dots-mocr` | rednote-hilab's `dots_mocr` parser: PyMuPDF render, layout prompt, JSON reader | `native/venvs/dots-mocr` |

The client runs in the arm's own environment; the vLLM server runs from the project's
(`--vllm-cmd`). On the pod:

```sh
for arm in chandra_native churro_native dots_mocr; do
  .venv/bin/python -m operations.bakeoff.native.$arm install --venv-dir /workspace/venvs/$arm
  /workspace/venvs/$arm/bin/python -m operations.bakeoff.native.$arm check
done
/workspace/venvs/chandra_native/bin/python -m operations.bakeoff.native.chandra_native run \
  --limit 2 --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache --store-root $V/model-store
```

Every arm takes `run --pages --out [--label] [--limit] [--weights | --store-root]
[--server-url | --vllm-cmd ...]`, resumes like `witness_run`, and exits 0 when every page is
cached, 1 when a page errored, 2 when it refuses.

## CTC line arms (`lines/`)

Line recognisers, each in its vendor's environment (`lines/venvs/<name>/`, locked for Linux
x86_64 and macOS arm64), driven by one module per family with the shared commands `run`,
`install`, `check`, `prepare` and `fetch` (`lines/harness.py`). Two shared line sources
write crops to `<out>/_lines/<source>/<stem>/NNNN.png` with `<stem>.json` listing bounds and
order:

```sh
# Surya lines (CPU). --store-root takes the store's verified copy; --weights a bundle
# folder; either must be the pinned bundle (config/manifests/surya2-detection.json).
.venv/bin/python -m operations.bakeoff.lines.surya_rec install
.venv/bin/python -m operations.bakeoff.lines.surya_lines run --pages $V/bakeoff-pages \
  --lines-dir $V/bakeoff/surya-docs --out $V/bakeoff/witness-cache \
  --store-root $V/model-store --threads 8
# blla lines: kraken's segmenter.
.venv/bin/python -m operations.bakeoff.lines.kraken_ppocr install
.venv/bin/python -m operations.bakeoff.lines.blla prepare --pages $V/bakeoff-pages \
  --out $V/bakeoff/witness-cache --threads 4
# A recogniser on those lines:
.venv/bin/python -m operations.bakeoff.lines.pylaia install
.venv/bin/python -m operations.bakeoff.lines.pylaia fetch --model belfort --store-root $V/model-store
.venv/bin/python -m operations.bakeoff.lines.pylaia run --model belfort --lm --lines surya \
  --lines-dir $V/bakeoff/surya-docs --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache \
  --store-root $V/model-store
```

`surya_lines run` starts from the first page without a readable document
(`--first-ordinal`), so a stopped run resumes; a `pages.json` listing other pages, or these
in another order, is refused. `surya_lines command` prints the runner's command. Without a
store, fetch the bundle with `operations/serving/surya/.venv/bin/python
operations/serving/surya/prefetch.py --out <dir>`.

Arms: `kraken-{ppocrv6,mccatmus,mcfondue}-{blla,surya}` (`--model`, default `ppocrv6`),
`pylaia-{belfort,popp}[-lm]-{blla,surya}`, `party-blla` (GPU), and `surya-rec-surya` (a VLM
in surya-ocr 0.22.1, not CTC; on the pod `--serve` starts vLLM for the run).

## Tests

The tests use a fake vLLM server (`fake_vllm_server.py`), and DAI's detector is faked (it
needs `ultralytics`, which only the pod has).
