# Surya, run apart

Surya ([datalab-to/surya](https://github.com/datalab-to/surya), `surya-ocr` 0.22.1,
Apache-2.0 code) is the Designator's independent text-line and layout detector: a check that
no ink goes unseen. It decides nothing. Stage 2 publishes what it finds as `surya-page`,
`surya-line` and `surya-block` records (`pipeline/2_designator/CONTRACT.md`).

Two of Surya's torch detectors run, each on the whole page as its makers wrote it:

- `DetectionPredictor.local()`: text lines. Surya chunks a tall page itself, so nothing here
  tiles or rescales.
- `LayoutEngine.run_batch`: the rf-detr layout detector and its reading-order head, called as
  Surya's own fast-layout server calls it, but hosted in the runner's process (Surya's
  `FastLayoutPredictor` client would attach to any fast-layout server already running and
  leave the one it spawns running).

Surya's VLM layout and OCR models are not used.

## A separate environment

Surya pins Pillow below 11 and OpenCV 4.11.0.86, which the project environment cannot share.
This folder is its own uv project: `pyproject.toml` pins `surya-ocr==0.22.1`, and `uv.lock`
(Linux x86_64 only) pins everything, including torch 2.14.0. The project never imports
Surya: it starts `runner.py` with this environment's `.venv/bin/python` and reads the JSON it
writes, checked against a closed shape by `operations/serving/surya_detector.py`.

| File | What it does |
|---|---|
| `runner.py` | runs both detectors over the given pages in order and writes `page-<n>.json` per page (`n` from `--first-ordinal`): Surya's result models as JSON, the ordering used, and run facts (versions, device, CPU instruction set, machine, threads, settings, checkpoints and every weight file's digest). `--check` prints versions and loads nothing |
| `prefetch.py` | fetches the three checkpoints into one bundle directory and writes its lock, `surya-bundle.json` |
| `contract.py` | standard library only, loaded by both sides: the page document schema, output-shaping settings, reading-order branches, and the bundle lock |
| `standin_bundle.py` | tests only: a locked bundle with seeded random weights |

## Determinism

The runner fixes the CPU as the device, the serving row's thread count, one interop thread,
`torch.use_deterministic_algorithms(True)`, `torch.manual_seed(0)`, eval mode and one page
per call. It refuses to run if any of Surya's output-shaping or checkpoint settings is set in
the environment (in any case) or differs from Surya's default, if a `local.env` settings file
is found, if the bundle's checkpoints are not the ones Surya would load, if the
reading-order head did not load, or if a page's mode would be clipped to 8 bits by Surya's
loader (a 16-bit scan; see `contract.PAGE_MODES`).

There is no network: every checkpoint is handed to Surya as a local directory, and the
Hugging Face libraries run with `HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE`.

Surya raster-sorts a page's blocks instead of using the reading-order head when a page has
more than 128 detections or no feature map came back; the runner records which ordering each
page got (`reading_order`, `reading_order_reason`).

**Guaranteed:** the same bundle, locked environment, thread count and CPU instruction set
give byte-identical page documents. `operations/serving/test_surya_environment.py` checks it
(with stand-in weights, both raster fallbacks, a failed order head, the torch state a run
leaves, and every socket and Hub download refused). CI never syncs this environment, so there
the suite skips: run it on a machine with `uv sync --locked --project operations/serving/surya`
before a change to the runner, environment or lock goes to a pod. **Not guaranteed:**
identical floats on a CPU with another vector instruction set; the run facts name it, and a
resumed run on another one is refused.

## The weight bundle

| Checkpoint | Source (Surya's default) | Pin |
|---|---|---|
| text detection | `s3://text_detection/2025_05_07` on Datalab's model host | the file digests in the lock (the host has no revisions) |
| layout | `hf://datalab-to/surya_layout2` | Hub commit `0aee81d5fd9275c0582e545bf3a56944b1e75679` |
| reading order | `hf://datalab-to/surya_layout2/order` | the same commit |

Surya cannot name a Hub revision, so the bundle is fetched once at the pinned commit
(`contract.LAYOUT_REPOSITORY_REVISION`; moving it is a reviewed change) and each checkpoint is
handed over as a local directory. The whole bundle is pinned by its measured manifest,
`config/manifests/surya2-detection.json`, digest
`ad19b0280bec623e7edd1b7ca5197ded1add35af9ff0ec76e80db8d035b16cb9`, named in
`config/models-real.toml` and `common/chairs/model_store.py::REQUIRED_ARTIFACTS` (a test
holds them equal).

| File | SHA-256 | Checked against |
|---|---|---|
| `text_detection/2025_05_07/model.safetensors` | `38c3749eeb5f06fc93ed71eeee5cbd86b1945d08f8f74746bda035d41324bd3e` | the host's ETag (S3 multipart MD5, 10 parts of 8 MiB) |
| `surya_layout2/rfdetr_layout.pth` | `e01b79f858778cdad8a1384e644ac2b35f9c095fbfd102a34942e23f2f179fe7` | the Hub's LFS SHA-256 at the commit |
| `surya_layout2/order/order_ar.pt` | `f381c38548015cbbe962611cbd2c80c59e88c3c25543ac20df9db16f84923ad8` | the Hub's LFS SHA-256 at the commit |

Every other file is checked the same way (the Hub's git blob SHA-1, or the host's MD5 ETag).
**Licences:** the layout repository declares `openrail` and carries its licence text
(`surya_layout2/LICENSE`); the text-detection checkpoint on Datalab's host carries none.

**To re-pin:** fetch with `prefetch.py`, check each file against its host, promote the bundle
into a scratch store with `common/chairs/model_store.py::promote_verified_snapshot` (artifact
`surya2-detection`, every file required), and copy the published manifest to
`config/manifests/surya2-detection.json`. Write its SHA-256 into `config/models-real.toml` and
`SURYA_BUNDLE_DIGEST_MANIFEST` together. A store holding the old pin is refused, so a pod on
the new pin needs a fresh store.

## On the pod

1. **UV_ENVIRONMENT** builds this environment on container-local disk (`uv sync --locked
   --project operations/serving/surya`) when the pod's stages run a subprocess chair from it,
   or when the model store lacks the bundle.
2. **MODEL_STORE** runs `prefetch.py` in that environment (`surya_detector.SuryaBundleFetcher`)
   into its staging area, refuses the bundle unless its manifest is the pinned one, then
   publishes `manifests/surya2-detection.json` and moves the bundle to
   `local/surya2-detection`. A record naming the bundle `pending-fetch`, or not at all, is
   completed the same way.
3. **CHAIR_CACHE** copies the verified bundle to `config/real-models/designator_surya` on
   container-local disk and verifies the copy before replacing anything.
4. **PREFLIGHT** verifies the weights and runs the runner once on the golden page on the CPU;
   a failure is red with the remedy named. `pod_run` refuses a Designator selection without
   that run.

The `unproven-real-surya` rows in `config/serving_recipes_real.toml` (8 threads, 6 workers,
600 s to start, 60 s a page) are planning values. The stage cuts pages in order into up to
`workers` contiguous slices (never more than the host's CPUs divided by `threads`), runs one
runner per slice at once, and merges the documents in page order; each process loads the
models once. A row without `workers` runs one process over every page.
