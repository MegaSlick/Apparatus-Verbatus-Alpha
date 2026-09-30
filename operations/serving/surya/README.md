# Surya, run apart

Surya ([datalab-to/surya](https://github.com/datalab-to/surya), `surya-ocr` 0.22.1,
Apache-2.0 code) is the Designator's independent text-line and layout detector: a check
that no ink goes unseen. It decides nothing. Stage 2 publishes what it finds as
`surya-page`, `surya-line` and `surya-block` records
(`pipeline/2_designator/CONTRACT.md`).

## What runs

Two of Surya's torch detectors, each on the whole page as its makers wrote it:

- `DetectionPredictor.local()`: text lines. Surya chunks a tall page itself
  (`DETECTOR_IMAGE_CHUNK_HEIGHT`), so nothing here tiles or rescales a page.
- `LayoutEngine.run_batch`: the rf-detr layout detector and its learned reading-order
  head, called exactly as Surya's own fast-layout server calls it. It is hosted in the
  runner's process because Surya's `FastLayoutPredictor` client attaches to whatever
  fast-layout server is already running on the host, started with whatever settings,
  and leaves the server it spawns running after the caller exits.

Surya's VLM layout and OCR models are not used.

## Why a separate environment

Surya pins Pillow below 11 and OpenCV 4.11.0.86, which the project environment cannot
share. This folder is its own uv project: `pyproject.toml` pins `surya-ocr==0.22.1`, and
the committed `uv.lock` (uv 0.12.1, linux x86_64 only) pins every package, including
torch 2.14.0 from PyPI. The project never imports Surya. It starts `runner.py` with this
environment's interpreter, `.venv/bin/python`, and reads the JSON it writes, checked
against a closed shape by `operations/serving/surya_detector.py`. `contract.py` is the
one file both sides load; it uses the standard library only.

## Files

| File | What it does |
|---|---|
| `runner.py` | Runs both detectors over the page images given, in order, and writes `page-<n>.json` per page: Surya's own result models dumped as JSON, which ordering the block positions came from, and the run facts (versions, device, CPU instruction set and machine, threads, settings, checkpoints and every weight file's digest). `--check` prints the installed versions and loads nothing. |
| `prefetch.py` | Fetches the three checkpoints once into one bundle directory and writes its lock, `surya-bundle.json`. |
| `contract.py` | The page document schema name, the settings that shape output, the reading-order branches, and the bundle lock: its shape, the pinned layout commit, and the check that every file still matches. |
| `standin_bundle.py` | Tests only: a locked bundle of Surya's three architectures with seeded random weights, so the runner's whole path runs where the real weights were never fetched. |

## Determinism

The runner fixes the CPU as the device, a thread count from the serving row, one interop
thread, `torch.use_deterministic_algorithms(True)`, `torch.manual_seed(0)`, eval mode
and one page per call. It refuses to run if any of Surya's output-shaping or checkpoint
settings is set in the environment, in any case, since Surya's settings read the
environment ignoring case; if any differs from Surya's default; if Surya found a
`local.env` settings file above its package; if the bundle's checkpoints are not the
ones Surya would load; if the reading-order head did not load; or if a page is in a
mode Surya's own loader, `Image.open(path).convert("RGB")`, would clip to 8 bits (a
16-bit scan, for one). The modes it reads are those the project replays a vendor's RGB
conversion for (`contract.PAGE_MODES`).

No network rests on two things: every checkpoint is handed to Surya as a directory in
the bundle, which its loaders use as a local path before any fetch, and the Hugging Face
libraries run with `HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE`. Surya has no offline
switch of its own.

Surya raster-sorts a page's blocks instead of running its reading-order head when the
page has more than 128 detections or no feature map came back for the head; it only
logs either. The runner keeps the detections and records which ordering each page got
(`reading_order`, with `reading_order_reason`).

Guaranteed: the same bundle, the same locked environment, the same thread count and the
same CPU instruction set give byte-identical page documents.
`operations/serving/test_surya_environment.py` checks it twice over the synthetic
fixture pages with stand-in weights, along with both raster fallbacks, an order head
that does not load, the state a run leaves torch and the models in (deterministic
algorithms, the row's thread count, eval mode), and a run with every socket connection
and Hub download refused. CI never syncs this environment, so there the suite skips: it
is a gate run on a machine with this environment synced
(`uv sync --locked --project operations/serving/surya`) before a pod runs Surya, and a
change to the runner, this environment or its lock is not ready for a pod until it
passes there. Not guaranteed: identical floats on a CPU with a different vector
instruction set, since torch picks kernels by instruction set; the run facts name the
instruction set and the machine, and a resumed run on another one is refused.

## The weight bundle

| Checkpoint | Source (Surya's default) | Pin |
|---|---|---|
| text detection | `s3://text_detection/2025_05_07` on Datalab's model host | the file digests in the lock (the host has no revisions) |
| layout | `hf://datalab-to/surya_layout2` | Hub commit `0aee81d5fd9275c0582e545bf3a56944b1e75679` |
| reading order | `hf://datalab-to/surya_layout2/order` | the same commit |

Surya has no setting that names a Hub revision: its loaders fetch whatever the
default branch holds. So the bundle is fetched once at the pinned commit
(`contract.LAYOUT_REPOSITORY_REVISION`; moving it is a reviewed change) and Surya is
handed each checkpoint as a local directory. The whole bundle is pinned by the digest
of its measured manifest, `config/manifests/surya2-detection.json`:
`ad19b0280bec623e7edd1b7ca5197ded1add35af9ff0ec76e80db8d035b16cb9`, named in
`config/models-real.toml` and in `common/chairs/model_store.py::REQUIRED_ARTIFACTS`
(a test holds the two equal).

| File | SHA-256 | Checked against |
|---|---|---|
| `text_detection/2025_05_07/model.safetensors` | `38c3749eeb5f06fc93ed71eeee5cbd86b1945d08f8f74746bda035d41324bd3e` | the host's ETag (S3 multipart MD5, 10 parts of 8 MiB) |
| `surya_layout2/rfdetr_layout.pth` | `e01b79f858778cdad8a1384e644ac2b35f9c095fbfd102a34942e23f2f179fe7` | the Hub's LFS SHA-256 at the commit |
| `surya_layout2/order/order_ar.pt` | `f381c38548015cbbe962611cbd2c80c59e88c3c25543ac20df9db16f84923ad8` | the Hub's LFS SHA-256 at the commit |

Every other file was checked the same way: the Hub's git blob SHA-1 for small files
at the commit, and the host's MD5 ETag for each file its `manifest.json` lists. Two
fetches gave byte-identical bundles. The layout repository declares `openrail` and
carries its licence text (`surya_layout2/LICENSE`); the text-detection checkpoint on
Datalab's host carries none.

To re-pin after a deliberate move: fetch with `prefetch.py`, check each file against
its host as above, promote the bundle into a scratch store with
`common/chairs/model_store.py::promote_verified_snapshot` (artifact
`surya2-detection`, every file required), and copy the manifest it publishes to
`config/manifests/surya2-detection.json`. Its SHA-256 is the new pin, written in
`config/models-real.toml` and `SURYA_BUNDLE_DIGEST_MANIFEST` together. A store is
never re-pinned in place: one that holds the bundle at the old pin is refused by name
(its manifest is published once and never replaced), so a pod on the new pin needs a
fresh store, on a new network volume or at an empty store root.

## On the pod

1. The environment is built on container-local disk, beside the project's own, by the
   pod bootstrap's UV_ENVIRONMENT step (`uv sync --locked --project
   operations/serving/surya`) whenever the pod's stages run a configured chair from a
   subprocess row in it, or the model store still lacks the bundle (step 2); by hand,
   the same command.
2. The MODEL_STORE step fetches the bundle onto the network volume: the store runs
   `prefetch.py` in that environment (`surya_detector.SuryaBundleFetcher`) into its
   staging area, measures the manifest, refuses it unless it is the pinned one, and
   only then publishes `manifests/surya2-detection.json` and moves the bundle to
   `local/surya2-detection`. A store whose record names the bundle `pending-fetch`, or
   does not name it, is completed the same way. The store fetches the bundle whatever
   the roster configures, so UV_ENVIRONMENT syncs this environment while the store
   lacks the bundle, and MODEL_STORE checks its interpreter before any download.
3. The CHAIR_CACHE step copies the verified bundle to where the roster binds it,
   `config/real-models/designator_surya` on container-local disk, and verifies the
   copy against the roster's manifest before it replaces anything there.
4. Preflight verifies the chair's weights against its manifest and runs the runner
   once on the golden page, on the CPU; a failure is red with the remedy named, and
   the versions, CPU instruction set and machine that run measured go in its report.
   `pod_run` refuses a Designator selection without that run.

The rows in `config/serving_recipes_real.toml` (`unproven-real-surya`, 8 threads, 600 s
to start, 60 s a page) are planning values, not measurements of a pod. One process
loads the models once and reads every page, so the run's timeout is
the startup allowance plus the per-page allowance for each page, rather than page
batches that would load the models again for each batch.
