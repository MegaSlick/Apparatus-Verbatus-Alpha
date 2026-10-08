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
| `fake_vllm_server.py` | a stand-in server for the tests; no GPU |

## The models

| Name | Model | Shown | Prompt and answer bound |
|---|---|---|---|
| `chandra` | datalab-to/chandra-ocr-2 | the whole page, the vendor's `scale_to_fit` | vendor `ocr_layout` prompt, thinking off, first attempt only (no retry loop); 12,384 tokens when it surely fits, else the context's remainder |
| `dai` | Teklia RecordGold ATR (Qwen2.5-VL-7B) | each record crop from Teklia's own YOLO record detector (CPU), width at most 1,500 px | Teklia's `system.txt` and `query.txt`; 1,024 tokens per record; the record texts joined in page order (left column first on a spread, then top to bottom). A page with no record found is a valid, empty result |
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

`scores.md` has one table per category (median and mean CER, median WER, line recall on
index and list pages, empty, loops, errors, s/page), the five worst pages per model, and
each model's throughput. While the gold files say `STATUS: fool's gold`, every heading
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
- The rescue rate, error correlation, act recall and the other Phase W metrics in the
  plan's section 9 are not computed here; the cache has what they need.
- Nothing here has run on a GPU yet: the tests use a fake server, and DAI's detector is
  faked in the tests (it needs `ultralytics`, which only the pod has).

## Queue runner

`queue_runner.py` runs one pod's whole day from a manifest (`queue/example-*.toml`,
schema `bakeoff-queue.v1`) and ends the pod itself, so no laptop has to notice when a job
ends. Arms run in order; each runs a smoke of `smoke_pages` pages (`--limit N` appended),
then the full run. The next arm's `install` and `prepare` run on the CPU while this arm's
command holds the card; a `gpu = false` arm runs beside the GPU arms. A failed arm is
retried once at the end. Time boxes never kill work: an overrun is pinged once, and the
`cut` rule only skips later arms (`overrun`, `behind-schedule`, `install-failed`;
`never` always runs). `hard_stop_min`, off unless set, stops the arm in flight and ends
the day early. `status.json` beside the cache is rewritten every 30 s; the queue's events
join `events.jsonl`; each milestone pings the phone once.

At the end it copies the cache to `sync_to` (`rsync -rt`), compares every file's sha256,
writes `DONE.json` (digests and summary) to both, pings, and ends the pod: with a guard
heartbeat younger than 5 min it moves the guard's deadline to now (the guard deletes, with
its retries and stop fallback); otherwise it runs `operations/pod/pod_delete.sh`. With
`own_disk = true` it refuses unless the copy verified; `end_pod = "none"` keeps the pod.
SIGTERM stops the arm and exits 143 without ending the pod.

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
