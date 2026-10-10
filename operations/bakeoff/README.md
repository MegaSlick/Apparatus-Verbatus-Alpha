# Witness bake-off (Phase W)

A small bench, kept apart from the pipeline: it runs each witness model natively (its
vendor's own prompt, image rule and sampling, as the repository already carries them),
caches every raw answer, and scores each model against the lead's per-page
transcriptions. Nothing here touches a run tree, a seal or a receipt.

| File | What it does |
|---|---|
| `witness_run.py` | starts a model's vLLM server, sends every page, writes one JSON per page |
| `arms.py` | what each model is sent: prompt, image preparation, sampling, answer bound; DAI's record detector |
| `gold.py` | reads the lead's `<stem>.txt` files (the bake-off set's `TEMPLATE.txt` format) |
| `score.py` | normalises each answer to plain text and writes `scores.jsonl` and `scores.md` |
| `fed_arm.py` | `qwen-fed`: the Perlector's own page request, rebuilt from a sealed run tree, sent to any served model, with witness, letter and image variants |
| `fed_score.py` | the Perlector scorecard: reading quality, answer health and scepticism per page group |
| `fake_vllm_server.py` | a stand-in server for the tests; no GPU |

## The models

| Name | Model | Shown | Prompt and answer bound |
|---|---|---|---|
| `chandra` | datalab-to/chandra-ocr-2 | the whole page, the vendor's `scale_to_fit` | vendor `ocr_layout` prompt, thinking off, first attempt only (no retry loop); 12,384 tokens when it surely fits, else the context's remainder |
| `dai` | Teklia RecordGold ATR (Qwen2.5-VL-7B) | each record crop from Teklia's own YOLO record detector (CPU), width at most 1,500 px | Teklia's `system.txt` and `query.txt`; 1,024 tokens per record; the record texts joined in page order (left column first on a spread, then top to bottom). A page with no record found is a valid, empty result, unless `--record-fallback whole-page` shows DAI the whole page as one record (marked `whole-page` in the cache). `--detector-conf` and `--detector-imgsz` change what the detector is asked (defaults: the serving row's 0.25 and 1024); the `_records/` cache keeps each record's confidence and the settings that found it, and is rebuilt when they change |
| `churro` | stanford-oval/churro-3B | the whole page within 2,500 px | vendor `registry-v0.3.0` system prompt; 25,000 tokens |
| `qwen-blind` | any vLLM vision model (`--weights`) | the whole page | a plain verbatim prompt (spelling and abbreviations kept, `[[?]]` for unread ink, one line per written line), greedy, thinking off, 12,288 tokens: the reader with no witnesses |

Sampling is the sealed per-chair rows of `config/decoding.toml`; the server shape is each
model's row in `config/serving_recipes_real.toml` (`max_model_len`, pixel bounds, prefix
caching), except that the bake-off keeps the card full: `--gpu-memory-utilization 0.92`
and `--max-num-seqs 32` by default, and the client keeps twice that many requests in
flight (`--concurrency`), so vLLM's queue is never empty.

## What a cached page holds

`<out>/<model>/<page stem>.json`: model, repo, revision, weights path, the server's
argv; per request the prompt digest, sampling, `max_tokens` and why, the image size and
digest sent, the raw response body, `finish_reason`, `usage`, seconds (including time
queued) and any error; the page's text, an `empty` flag and a `loop` flag (the same line
30 or more times in a row, or `finish_reason` length). DAI's record boxes are cached in
`<out>/dai/_records/`. Beside them: `run.json` (wall time and throughput of the last
run), `server.log` (vLLM's own log; requests are not logged), and at the top
`events.jsonl` (when each server started, was ready, began and finished sending, and
stopped) and `gpu-<time>.csv` (`nvidia-smi` every 10 s), so idle gaps can be seen
afterwards.

A page cached without an error is skipped next time, so a run resumes; a page with an
error is sent again.

## On the pod

One card of 24-48 GB runs `chandra`, `dai` and `churro` (an A40 48 GB is the plan's
card; an RTX 4090 24 GB also fits each one). `qwen-blind` with a 27B model needs the
96 GB card. Start the pod with its guard armed, as `operations/pod/README.md` says; this
tool never starts or stops a pod.

Page images go to the pod, never into git: copy the prepped images flat into one folder
on the Mac, then to the pod (SSH details from `runpodctl pod get <pod id>`):

```sh
mkdir -p ~/bakeoff-pages && cp "$HOME/Desktop/Bake-off set/Pages"/*/Prepped/*.tif ~/bakeoff-pages/
scp -P <port> -r ~/bakeoff-pages root@<ip>:/workspace/private/bakeoff-pages
```

On the pod, over SSH:

```sh
V=/workspace/private
cd /opt/verbatus && git fetch && git checkout --detach <commit>
bash operations/pod/prepare_runtime.sh
UV_CACHE_DIR=/tmp/verbatus-uv-cache uv sync --frozen --group pod

# Weights: an existing model store on the volume is used as it is
# ($V/model-store/hf/<artifact>; a chair cache with --cache-root also works).
# Without one, fetch the pinned snapshots into the same layout:
.venv/bin/python -m operations.bakeoff.witness_run fetch --models chandra,dai,churro \
  --store-root $V/model-store

# A two-page smoke run first (a minute after the server is up):
.venv/bin/python -m operations.bakeoff.witness_run run --model chandra --limit 2 \
  --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache --store-root $V/model-store

# The full run, detached so a dropped SSH session cannot stop it. While one model is on
# the card, the next one's pages (and DAI's record crops) are prepared and its weights
# copied to local disk; the next server starts as soon as the last answer is in.
setsid nohup .venv/bin/python -m operations.bakeoff.witness_run run-all \
  --models chandra,dai,churro --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache \
  --store-root $V/model-store --stage-dir /var/tmp/bakeoff-weights \
  > $V/bakeoff/run-all.out 2>&1 < /dev/null &
tail -f $V/bakeoff/witness-cache/events.jsonl
```

The reader with no witnesses, on the big card:

```sh
.venv/bin/python -m operations.bakeoff.witness_run run --model qwen-blind \
  --weights <local snapshot> --repo <repo id> --revision <commit> \
  --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache
```

`--label` names the cache folder, so two plain-prompt models can sit side by side.
Re-running the same command finishes whatever is missing.

Bring the cache home before the pod goes (a pod's own disk is deleted with it), into the
gitignored `private/`:

```sh
scp -P <port> -r root@<ip>:/workspace/private/bakeoff/witness-cache ~/verbatus_alpha/private/bakeoff/
```

## Scoring, on the Mac

CPU-light; a few seconds:

```sh
cd ~/verbatus_alpha
.venv/bin/python -m operations.bakeoff.score --cache private/bakeoff/witness-cache \
  --gold "$HOME/Desktop/Bake-off set/Pages" --gold-glob '*/Prepped/*.txt' \
  --out private/bakeoff/scores
```

`scores.md` scores each model on its own page groups (see "Fair scoring and the roster"
below), lists the five worst pages per model, and each model's throughput. While the gold files say `STATUS: fool's gold`, every heading
says **vs fool's gold (ballpark, not accuracy)**.

How it scores:

- The reference is all act text in order, then headings and rows. `[[?]]` is dropped,
  `[[word|other]]` keeps its first reading, struck text is dropped, inserted text kept.
- Each answer becomes plain text: Chandra's layout HTML through the repository's own
  reader (`common/chandra_layout.py`, table rows as lines), Churro's XML through
  `common/churro_document.py`, DAI's `[UNCERTAIN]`/`[CROSSED_OUT]` tokens removed,
  `<think>` blocks and markdown table rules removed.
- CER and WER are `operations/corpus/scoring.py`'s (graphemic-v1: case, accents,
  spelling and punctuation count; line breaks do not). An answer past that scorer's
  20,000-character bound (a loop) is scored by a plain Levenshtein instead, and says so.
- Line recall: each gold row matched to at most one model line at CER <= 0.3, best pairs
  first (greedy, not Hungarian; scipy is not installed).
- A page with an error or no reading still counts: an error is an empty answer, and a
  gold page with no cached answer is counted under `missing`.

## Not done yet

- dots.ocr / dots.mocr: not added. It needs the vendor's prompt read at a pinned
  revision, a check that vLLM 0.30's registry serves it without remote code, and a
  normaliser branch for its JSON layout answer. The `qwen-blind` arm with
  `--prompt-file` and `--label` can serve it once those are settled.
- Act recall and the other Phase W metrics beyond the roster's (`roster.py`) are not
  computed here; the cache has what they need.
- Nothing here has run on a GPU yet: the tests use a fake server, and DAI's detector is
  faked in the tests (it needs `ultralytics`, which only the pod has).

## Queue runner

`queue_runner.py` runs one pod's whole day from a manifest (`queue/example-*.toml`,
schema `bakeoff-queue.v1`) and ends the pod itself, so no laptop has to notice when a job
ends. Each arm runs a smoke of `smoke_pages` pages (`--limit N` appended), then the full
run. A failed arm (one whose program cannot even start included) is retried once at the
end, smoke first, then reported. Time boxes never kill work: an overrun is pinged once,
and the `cut` rule only skips later arms (`overrun`, `behind-schedule`, `install-failed`;
`never` always runs).

Lanes. One arm at a time holds the card: the GPU arms run in manifest order. The
`gpu = false` arms run beside them, several at once: each takes its command's
`--threads` (or the arm's `threads`) from the manifest's `cpu_threads`, a number or
`"auto"` (the pod's CPUs, as its container quota allows, less 2 for the GPU lane's own
processes). Without `cpu_threads` one CPU arm runs at a time. CPU arms start in manifest
order: one that does not fit waits for room and holds back the CPU arms after it (an arm
larger than the whole budget runs alone). An arm's `install` and `prepare` run one at a
time across all lanes (several arms share an environment), and the next arm's run while
an arm's command does; they may use the network, while every arm's command runs offline
(`HF_HUB_OFFLINE=1`).

Dependencies. `after = ["surya-lines"]` names earlier arms that must finish ok first; a
GPU arm waiting on one lets the next GPU arm take the card. When a dependency fails, its
dependants wait for its retry at the end and run only if it then succeeds; when it is
skipped (or fails for good), they are skipped with the reason (`needs X, which failed`).
An arm's pages are counted in `<out>/<name>/`, or in `<out>/<writes>/` for an arm whose
output is elsewhere (the line sources write `_lines/surya` and `_lines/blla`).

`validate` and `run --dry-run` print when the GPU lane and the CPU arms would end by the
time boxes at 8, 16 and 32 vCPU (and, for the dry run, on this machine), so a pod with too
few CPUs shows before it is rented, and what the arms download onto an empty volume.

Weights. A queue may start on an empty volume: each arm's `prepare` is
`python -m operations.bakeoff.weights fetch --store-root STORE NAME ...`, naming what its
command reads, and the command then runs offline. Roster artifacts (`chandra-ocr-2`,
`dai-recordgold-atr`, `churro-3B`, `yolov26-record-detection`, `qwen3.8-27B`,
`surya2-detection`) go through the project's model store
(`materialize_real_roster`, under its store-wide lock) into `<store>/hf/<artifact>` and
are refused unless the store's manifest digest is the one `config/models-real.toml` pins;
a present artifact is hashed again, not downloaded. The bake-off's own snapshots
(`qwen3.5-27b`, `qwen3.5-9b`, `DotsMOCR`) are pinned file by file in `weight_pins.json`
(commit, sizes, Hub digests) and go to `<store>/hf/<name>`, where witness_run and the
native arms look; a `<name>.verified.json` beside each saves re-hashing an unchanged
snapshot. Line-arm weights (`kraken-*`, `pylaia-*`, `party-v2`, `surya-ocr-2`) use their
module's own checked fetch. Preparations run one at a time per lane, so arms sharing a
snapshot fetch it once. No repository is gated; no token is needed.
`python -m operations.bakeoff.weights sizes NAME ...` prints what a name downloads. The
older `witness_run fetch` and the native arms' `fetch` write into `<store>/hf/` outside
the store's record; on a store, use `weights fetch` instead. `hard_stop_min`, off unless set, stops the arm in flight and ends
the day early. `status.json` beside the cache is rewritten every 30 s; the queue's events
join `events.jsonl`; each milestone pings the phone once.

At the end it copies the cache to `sync_to` (`rsync -rt`), compares every file's sha256,
writes `DONE.json` (digests and summary) to both, pings, and ends the pod: with a guard
heartbeat younger than 5 min it moves the guard's deadline to now (the guard deletes, with
its retries and stop fallback); otherwise it runs `operations/pod/pod_delete.sh`. With
`own_disk = true` it refuses unless the copy verified; `end_pod = "none"` keeps the pod.
`run --sync-to PATH --own-disk --keep-pod` (and the same options on `validate`, `status`
and `end-pod`) move the copy, add the refusal and keep the pod (`end_pod = "none"` for
that run) from the command line, so one manifest serves
a network volume and the global-volume route (`RUNBOOK.md` 2.1), where the cache sits on
the pod's own disk and the copy goes to object storage: there `rsync -rt` may be refused
(no times, no rename), so the copy falls back to `rsync -r --inplace`, then to a plain
Python copy, and the sha256 compare is what proves it either way.
SIGTERM stops every arm, pings and exits 143 without ending the pod. Arms never see
`RUNPOD_API_KEY` or `NTFY_TOPIC`; `watch` warns once when the status has not changed (or
cannot be read) for 10 min by the Mac's own clock. `status.json` names the GPU lane's arm
(or a CPU arm when the card is idle) and lists every running CPU arm with its pages under
`cpu_arms`; `watch` prints them after the GPU arm.

On the pod (`validate` and `run --dry-run` first):

```sh
setsid nohup .venv/bin/python -m operations.bakeoff.queue_runner run \
  --manifest operations/bakeoff/queue/example-witness-24gb.toml \
  > /workspace/private/bakeoff/queue-witness-24gb.log 2>&1 < /dev/null &
```

On the Mac: `watch` prints a line per change and exits 0 at `DONE.json`, 1 on failure;
`watch --ntfy --queue witness-24gb` follows the phone topic instead. `fetch` copies the
cache home and checks every file against `DONE.json`:

```sh
.venv/bin/python -m operations.bakeoff.queue_runner watch --ssh "ssh -p <port> root@<ip>" \
  --status /workspace/private/bakeoff/witness-cache/status.json
.venv/bin/python -m operations.bakeoff.queue_runner fetch --ssh "ssh -p <port> root@<ip>" \
  --remote /workspace/private/bakeoff/witness-cache --into private/bakeoff/witness-cache-<date>
```

## The vendor reader arm (`qwen-vendor`)

The same Qwen readers as `qwen-blind`, sent the way Qwen documents a page reading: the
Qwen3-VL OCR cookbook's plain-text instruction around the project's verbatim rules, no
system prompt, the model card's non-thinking sampling (temperature 0.7, top_p 0.8,
top_k 20, presence_penalty 1.5), thinking off, the checkpoint's own pixel bounds
(65,536 to 16,777,216 pixels, served through `--mm-processor-kwargs`), and no reply cap
(`max_tokens` left out, so vLLM answers up to the context's remainder). `--repo` picks
the family preset (`arms.VENDOR_PRESETS`; Qwen3.8 and Qwen3.5 today) and every request
record carries it under `vendor_preset`. A page can reach about 16,400 image tokens, so
pass `--max-num-batched-tokens 16384`:

```sh
.venv/bin/python -m operations.bakeoff.witness_run run --model qwen-vendor \
  --label qwen35-27b-vendor --repo Qwen/Qwen3.5-27B --revision <commit> \
  --weights <local snapshot> --max-num-batched-tokens 16384 \
  --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache
```

Run cards for every model are in `cards/` (index: `cards/README.md`).

## The fed-witness reader arm (`qwen-fed`)

The arms above read a page alone. `fed_arm.py` sends a reader the Perlector's whole page
request, witnesses included, so a base model, a LoRA adapter or a merged checkpoint can be
judged in the Perlector's seat without running the pipeline. It reads a sealed run tree
(stages 1-4 done; for example the cold run extracted from its tar) and never writes to it.

What it sends is the run's own request. For every page it takes the `page-feed` record and
the render the run retained (`4_perlector/blobs/`), rebuilds the prompt with
`common.page_prompt.build_page_prompt`, and rebuilds the whole body with
`operations.serving.http.request_body`: the render, then the text, one user turn,
thinking off, `max_tokens` 12,288, streamed, with the seed the receipt names. `prompts`
checks both against what the run recorded (the feed's `prompt` digests and the call
record's `request_sha256`) and exits 1 on any difference:

```sh
.venv/bin/python -m operations.bakeoff.fed_arm prompts --run-tree <run tree>
# pages 73: prompts byte-identical 73; requests byte-identical 73 of 73 with a recorded call
```

`run` sends every page to a server: `--server-url` for one already up (a base model, or
vLLM serving LoRA adapters under their own names), or `--weights` to start vLLM on a
snapshot with the Perlector's serving row (the same server options as `witness_run`).
`--model-name` is the served name the requests ask for; `--label` names the cache folder.

```sh
.venv/bin/python -m operations.bakeoff.fed_arm run --run-tree $V/runs/cold73-2026-10-09 \
  --out $V/bakeoff/fed-cache --label qwen38-base-greedy --model-name perlector-qwen3.8-27b \
  --server-url http://127.0.0.1:8190 --concurrency 16
```

Sampling: `--sampling greedy` (the default: the sealed row with temperature 0, top_p 1,
top_k 0, min_p 0, so two arms differ by their inputs, not by chance) or `sealed` (the
Perlector's row; with the run's served name and an unchanged feed the request is the
run's own, byte for byte, which is the noise-floor repeat). The reply streams under the
Perlector's sealed repetition-loop guard (`config/decoding.toml`), and a reply stopped
by it has `finish_reason` `repetition-loop`; `--no-stream` sends one plain request with
no guard. A prompt that no longer matches the run's digests on an unchanged feed (the
builder has changed since) is refused unless `--accept-new-builder`.

Variants change the feed before the prompt is rendered, so the text and the instruction
are what the pipeline would build for that feed:

| Option | What the reader is shown |
|---|---|
| `--drop-witness attestator_2` | the feed without that witness; letters reassigned in label order, as `page_feed` assigns them |
| `--add-witness attestator_4=<witness cache>/dots-mocr` | a bake-off witness as one more row: dots.mocr's layout cells as boxed units, any other arm's text as one unit; no cached page is a `failed` witness. Name it like a chair: under the named regime the label is shown |
| `--letter-map attestator_1=C,attestator_3=A` | other letters for the same witnesses (unit ids follow) |
| `--witness-order attestator_3,attestator_2,attestator_1` | the rows in another order; with `--letter-map`, the swap test |
| `--image none` | no image; the builder says "page image: not shown." and asks for a reading from the reports |
| `--image blur` (`--blur-radius 8`), `blank`, `swap` | the render blurred, a white page, or the next page's render; the prompt unchanged |

`prompts --show <ordinal>` with the same options prints that page's variant prompt.

Each page is `<out>/<label>/<page stem>.json` (schema `bakeoff-fed-page.v1`): the variant,
the feed as shown, the prompt digests and whether they match the run's, the request
digest and whether it matches the run's, the sampling, the image digest, the reply's
content, finish reason, usage, loop stop and seconds, the answer grammar's verdict
(`common.page_answer`: parsed or malformed, and why), the answer and its text. The raw
stream is beside it as `<page stem>.sse.gz`. A page cached without an error is skipped
next time; a label that holds another variant, model or sampling is refused. The page
accounting (holds) is not run here.

## The Perlector scorecard

`fed_score.py` scores one answer set (a run tree's own first readings, or a `fed_arm`
cache folder) and, with `--compare`, a second one beside it:

```sh
.venv/bin/python -m operations.bakeoff.fed_score --run-tree <run tree> \
  [--answers <fed cache>/<label>] [--compare <fed cache>/<other label> --names A,B] \
  --gold "$HOME/Desktop/Bake-off set/Pages" --gold-glob '*/Prepped/*.txt' \
  --hard-pages private/bakeoff/hard-pages.txt --out <dir>
```

Each answer is judged against the witnesses its reader was shown (the run's feeds, or the
variant feed a cache record carries); witnesses are named by their bake-off arm
(`chandra`, `dai`, `churro`) or their label, never by letter, so a swap compares like
with like. The reading's text is every entry's text in order, doubt marks reduced; gold
words are graphemic-v1 tokens aligned by unit-cost edit script, as `roster.py` does.
Per page group (act pages split by form) and for the hard pages:

- **reading**: CER median on parsed pages, and on all pages with an unparsed answer read
  as empty; act recall (gold acts matched by a `kind: act` entry at CER <= 0.5) and pages
  with the exact act count; row recall and surname recall on index and table pages;
  inserted words per gold word; false text on pages with no gold text;
- **answer health**: parsed and malformed (by reason), errors, finish reasons,
  loop-guard stops, completion tokens, seconds;
- **scepticism** (pages whose answer parsed): per witness, when only it has a gold word
  right, how often the reader follows; when only it is wrong, how often the reader
  resists; of the words it got wrong, how often the reader wrote its same wrong word;
  all witnesses wrong and the reader right (recovery); all right and the reader wrong
  (damage); beats the vote (right where most witnesses that read the page were wrong,
  minus wrong where most were right); and the share of the reader's errors that are a
  witness's error;
- **with `--compare`**: per page the CER between the two readings, pages read
  identically, gold words whose right/wrong flipped by group, and the headline rows side
  by side. Run the same arm twice (`--sampling sealed`) to see the noise floor before
  reading a change.

`scorecard.md` and `scorecard.json` go to `--out`. Every heading says **vs fool's gold
(ballpark, not accuracy)** while any gold page's STATUS says fool's gold; lead-checked
gold drops the label. On the cold run's own readings it reproduces the follow table of
the 2026-10-09 witness-hints note (handwritten acts: 8,769 gold words, only Chandra right
followed 440 of 582, only DAI 82 of 234, only Churro 378 of 657).

## Fair scoring and the roster

A model is scored only where its design applies, by the metric that fits the page type,
and never pooled across types. `groups.py` holds the table as data, edited by hand and
checked by `validate()`:

| Group | Gold categories | Headline | Also |
|---|---|---|---|
| `acts` | acts-18c, acts-19c, acts-20c | CER | WER |
| `prose-other` | contract | CER | |
| `tables` | ledger | line recall | CER |
| `index-list` | index, list | line recall | surname recall, false-line rate |
| `blank-like` | blank, near-blank, non-register | false-text rate | CER |
| `test` | any page with `TEST PAGE: yes` | per page, for the lead | |

`MODEL_GROUPS` says which groups each arm is scored on: `dai` on `acts` only (it reads
records; empty on an index is no failure), `pylaia-popp-*` on `index-list`, `tables` and
`acts`, every other arm on every group. An arm not in the table is scored everywhere and
the report warns. False text: more than 20 characters of output, on pages with no gold
text only. Surname recall: each gold row's first token among the model's tokens at
distance <= 1. False-line rate: model lines matched to no gold row or heading. Pages are
also split by FORM (handwritten, typed, printed form, mixed): one row per form under each
group, and the cross-model tables compare handwritten pages, then typed ones apart. A
record arm (`dai`, or `record-*`/`whole-page` units) also gets act recall (units matched
to gold acts at CER <= 0.5), units unmatched, per-unit CER and whole-page fallbacks.

`scores.md` has a compact cross-model table per group (headline only), one section per
model (one row per group it is scored on, then `all pages, for reference`, which
leaves out test pages; scores, health and record cells in
separate narrow tables so they read on a phone), the test
pages with their expected behaviour, and the hard pages. `--hard-pages FILE` (one stem per
line) lists those pages in their own table as well; they still count in every median.
`--exclude FILE` drops pages from scoring.

`python -m operations.bakeoff.roster` takes the same arguments as `score`; `roster.md` gives, per group and candidate against `--baselines` (default
`chandra,dai,churro`): rescue rate, phi correlation of wrong tokens with each baseline
(and the baselines' own), shared fabrication, insertion rate beside the leader's, union
line recall on index-list, and the roster rule's suggestion with its inputs. On each group
only the baselines scored there count as witnesses (DAI on `acts` only). The rule is
a suggestion for the lead, never a decision.

## Vendor-native arms (`native/`)

Beside the vLLM arms above, `native/` runs three witnesses through their vendors' own
code, so a weak score cannot be blamed on our re-implementation. Each writes the same
cache record under its own label; each has a run card in `cards/`.

| Arm (label) | Vendor path | Client environment |
|---|---|---|
| `chandra-native` | datalab's `chandra-ocr` 0.2.0 package end to end (its retry loop, its markdown) | `native/venvs/chandra-native` |
| `churro-native` | Stanford's release-time `run_churro_ocr.py` settings, its XML text extractor | `native/venvs/churro-native` |
| `dots-mocr` | rednote-hilab's `dots_mocr` parser: PyMuPDF render, layout prompt, JSON reader | `native/venvs/dots-mocr` |

The client runs in the arm's own small environment; the vLLM server runs from the
project environment (`--vllm-cmd`, default `.venv/bin/python -m vllm.entrypoints.cli.main`).
On the pod:

```sh
for arm in chandra_native churro_native dots_mocr; do
  .venv/bin/python -m operations.bakeoff.native.$arm install --venv-dir /workspace/venvs/$arm
  /workspace/venvs/$arm/bin/python -m operations.bakeoff.native.$arm check
done
/workspace/venvs/dots_mocr/bin/python -m operations.bakeoff.native.dots_mocr fetch --store-root $V/model-store
/workspace/venvs/chandra_native/bin/python -m operations.bakeoff.native.chandra_native run \
  --limit 2 --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache --store-root $V/model-store
```

Every arm takes `run --pages --out [--label] [--limit] [--weights | --store-root]
[--server-url | --vllm-cmd ...]`, resumes like `witness_run`, and exits 0 when every page
is cached, 1 when a page errored, 2 when it refuses (wrong environment, no weights).

## CTC line arms (`lines/`)

Line recognisers, each in its vendor's own environment (`lines/venvs/<name>/`, locked for
linux x86_64 and macOS arm64), driven from the project environment by one module with the
shared command line (`run`, `install`, `check`, `prepare`, `fetch`; `lines/harness.py`).
Run cards: `cards/kraken-ppocrv6.md`, `cards/kraken-mccatmus.md`,
`cards/kraken-mcfondue.md`, `cards/pylaia-belfort.md`, `cards/pylaia-popp.md`,
`cards/party.md`, `cards/surya-recogniser.md`. Two shared line sources write crops to
`<out>/_lines/<source>/<stem>/NNNN.png` with `<stem>.json` listing bounds and order:

```sh
# Surya lines: the repository's runner in its own environment (CPU), then the crops.
# `bundle` takes the model store's verified copy (local/surya2-detection) or fetches the
# pinned bundle into --bundle-dir (Datalab's host and the Hub; no token); both are
# checked against config/manifests/surya2-detection.json. The queue's surya-lines arm
# runs these three steps.
.venv/bin/python -m operations.bakeoff.lines.surya_rec install       # the Surya environment
.venv/bin/python -m operations.bakeoff.lines.surya_lines bundle --store-root $V/model-store \
  --bundle-dir $V/bakeoff/surya-bundle
.venv/bin/python -m operations.bakeoff.lines.surya_lines run --pages $V/bakeoff-pages \
  --lines-dir $V/bakeoff/surya-docs --out $V/bakeoff/witness-cache \
  --store-root $V/model-store --bundle-dir $V/bakeoff/surya-bundle --threads 8
# blla lines: kraken's segmenter in the kraken environment.
.venv/bin/python -m operations.bakeoff.lines.kraken_ppocr install
.venv/bin/python -m operations.bakeoff.lines.blla prepare --pages $V/bakeoff-pages \
  --out $V/bakeoff/witness-cache --threads 4
```

`surya_lines run` runs the runner only from the first page without a readable document
(`--first-ordinal`), so a smoke's pages are not read twice and a stopped run resumes; a
`pages.json` that lists other pages, or these in another order, is refused.
`surya_lines command` still prints the runner's command for a run by hand.

Then, for example:

```sh
.venv/bin/python -m operations.bakeoff.lines.pylaia install
.venv/bin/python -m operations.bakeoff.lines.pylaia fetch --model belfort --store-root $V/model-store
.venv/bin/python -m operations.bakeoff.lines.pylaia run --model belfort --lm --lines surya \
  --lines-dir $V/bakeoff/surya-docs --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache \
  --store-root $V/model-store
```

Arms: `kraken-{ppocrv6,mccatmus,mcfondue}-{blla,surya}` (one module, `--model`, default
`ppocrv6`; McCATMuS and McFondue are kraken 4.x CoreML files the same kraken 7.1.1 reads),
`pylaia-{belfort,popp}[-lm]-{blla,surya}`,
`party-blla` (GPU, cut first), `surya-rec-surya` (a VLM in surya-ocr 0.22.1, not CTC; on
the pod `--serve` starts vLLM on its Hub checkpoint for the run and stops it after).
Measured cold installs here: kraken 279 s, PyLaia 204 s, Party 116 s (warm uv cache).
