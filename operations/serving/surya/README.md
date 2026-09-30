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
| `runner.py` | Runs both detectors over the page images given, in order, and writes `page-<n>.json` per page: Surya's own result models dumped as JSON, plus the run facts (versions, device, threads, settings, checkpoints and every weight file's digest). `--check` prints the installed versions and loads nothing. |
| `prefetch.py` | Fetches the three checkpoints once into one bundle directory and writes its lock, `surya-bundle.json`. |
| `contract.py` | The page document schema name, the settings that shape output, and the bundle lock: its shape, the pinned layout commit, and the check that every file still matches. |

## Determinism

The runner fixes the CPU as the device, a thread count from the serving row, one interop
thread, `torch.use_deterministic_algorithms(True)`, `torch.manual_seed(0)`, eval mode,
one page per call and no network (`HF_HUB_OFFLINE`). It refuses to run if any of
Surya's output-shaping settings is set in the environment, if any differs from Surya's
default, if the bundle's checkpoints are not the ones Surya would load, or if the
reading-order head did not load (Surya would otherwise fall back to raster order with
only a log line).

Guaranteed: the same bundle, the same locked environment, the same thread count and the
same CPU instruction set give byte-identical page documents. This was checked twice
over the synthetic fixture pages with random stand-in weights, through the runner
directly and through `surya_detector.run_surya_subprocess`. Not guaranteed: identical
floats on a CPU with a different vector instruction set, since torch picks kernels by
instruction set. A resumed run that re-derives a different document refuses at
publication instead of overwriting what was sealed.

## The weight bundle

| Checkpoint | Source (Surya's default) | Pin |
|---|---|---|
| text detection | `s3://text_detection/2025_05_07` on Datalab's model host | the file digests in the lock (the host has no revisions) |
| layout | `hf://datalab-to/surya_layout2` | Hub commit `0aee81d5fd9275c0582e545bf3a56944b1e75679` |
| reading order | `hf://datalab-to/surya_layout2/order` | the same commit |

The Hub commit is `contract.LAYOUT_REPOSITORY_REVISION`; moving it is a reviewed change.
No file digest is recorded yet: none can be until the first fetch.

## On the pod

None of these steps has been run yet.

1. The environment is built on container-local disk, beside the project's own, by the
   pod bootstrap's UV_ENVIRONMENT step (`uv sync --locked --project
   operations/serving/surya`); by hand, the same command.
2. Fetch the bundle once onto the network volume, into the model store's staging area:
   `operations/serving/surya/.venv/bin/python operations/serving/surya/prefetch.py
   --out <volume>/store/staging/surya2-detection`. It refuses to overwrite a bundle that
   exists, and a bundle that exists is complete.
3. Promote it into the store (`common/chairs/model_store.py::promote_verified_snapshot`),
   which publishes its digest manifest as `manifests/surya2-detection.json`.
4. Configure the chair in `config/models-real.toml`, replacing its `absent` entry:
   `source = "local-repository"`, `path` naming the promoted bundle, `digest_manifest`
   and `manifest` from step 3, and `serving_recipe = "surya-v0"`.
5. Add one row per placement tier to `config/serving_recipes_real.toml` (the coverage
   check refuses a row for an absent chair, so the rows go in with step 4):

   ```toml
   [[profiles]]
   kind = "subprocess"
   recipe = "surya-v0"
   chair = "designator_surya"
   tier = "generic-24gb"
   engine = "surya"
   environment = "operations/serving/surya"
   device = "cpu"
   threads = 8
   timeout_seconds = 3600
   required_packages = { "surya-ocr" = "0.22.1", torch = "2.14.0" }
   ```

6. Preflight then verifies the chair's weights against its manifest and asks the
   environment for its versions; a mismatch is red with the remedy named.

The thread count and timeout above are planning values until a pod measures a page.
