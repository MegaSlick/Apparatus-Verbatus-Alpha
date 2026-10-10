# Apparatus Verbatus

Apparatus Verbatus reads handwritten historical registers and recovers the exact words
on the page. It is built for Quebec parish registers of the 1700s to 1900s and tested on
those and on French records of the same period. It should also work, less well, on
other archival records such as censuses, ledgers and notarial contracts.

Several vision models act as **witnesses**. Each one reads the page and reports what it
sees. A separate reader model, the **Perlector**, then reads the whole page itself,
names each entry on it, and establishes the text. It treats the witnesses as clues, not
answers. Every reading points back to the part of the image it came from. Text that
can't be read is flagged, never guessed. A page is never marked blank until its ink,
its detected lines and every witness agree that it is.

**Status: alpha.** The pipeline runs end to end on synthetic pages and on real pages on
a GPU machine. Its accuracy has not been established yet.

## How it works

```
page images → Triage → Exemplar → Ink map → Designator → Attestatores → Perlector → Recensor → Archetypus → Armarium
              optional sealed     where the   lines and    witness        reads each  checks     established  export
              cropping source     ink lies    records      models         page        coverage   reading
                                                                             │                                   ▲
                                                                             └──────────► Coniector ─────────────┘
                                                                                          labelled reconstruction
```

Each stage is a separate program. It reads the sealed records of the stage before it
and writes its own. [ARCHITECTURE.md](ARCHITECTURE.md) explains each stage, and
[GLOSSARY.md](GLOSSARY.md) defines the Latin names. [PRINCIPLES.md](PRINCIPLES.md) sets
out what the project holds itself to.

| Role | Model |
|---|---|
| Designator | Surya line and layout detection (`datalab-to/surya`) and `Teklia/YOLOv26-DAI-CReTDHI-Record-Detection` (records) |
| Attestator 1 | `datalab-to/chandra-ocr-2` |
| Attestator 2 | `Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR` |
| Attestator 3 | `stanford-oval/churro-3B` |
| Perlector | `Qwen/Qwen3.8-27B` |
| Coniector | `Qwen/Qwen3.8-27B` (text only) |

`config/models-real.toml` assigns models to roles. A model that answers in a different
format also needs an adapter in `common/witness_adapters.py`.

## Getting started

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) 0.12.1. The project
pins that exact version, and uv refuses to run under any other.

```sh
uv sync --frozen --group test --group audit   # create .venv from the lockfile
sh .githooks/install.sh                        # turn on the git hooks
sh .githooks/check-static.sh                   # lint, format and document checks
```

### Try it without a GPU

The default model list (`config/models.toml`) uses scripted stand-ins in place of real
models. With it you can run the pipeline on the synthetic pages in `proof/fixtures/`.
This tests sealing, accounting, review and export. It does not test reading.

```sh
.venv/bin/python pipeline/orchestrator/run.py --fixture synthetic-two-page-v0 \
  --scenario happy --run-id demo --run-root /tmp/verbatus-demo
```

Each stage writes its output under the run root. The export lands in `7_armarium/`.
This demo ends with `run demo: partial` and exit code 3. That is deliberate: one
synthetic entry runs from page 1 onto page 2. The pipeline flags it rather than
joining it silently, so the export is marked partial.

| Exit code | Meaning |
|---|---|
| 0 | complete |
| 2 | the run failed or was refused before it could finish |
| 3 | held for review, or partial |
| 4 | halted |

### Real pages

Real pages need a Linux GPU machine running vLLM. Give the real model list and its
serving recipes together:

```sh
--models-config config/models-real.toml --serving-recipes-config config/serving_recipes_real.toml
```

If you give only one of them, the run is refused before anything starts.
[operations/operator/README.md](operations/operator/README.md) walks through a run with
the `verbatus` command. [operations/pod/README.md](operations/pod/README.md) covers
renting a GPU machine on RunPod.

**Input:** most raster images, multi-page TIFF, HEIC and PDF.
**Output:** a sealed ZIP bundle with a manifest. It can also hold a text bundle, a
searchable SQLite database, JSONL, and the items held for human review
(`config/formats.toml`).

## Repository layout

| Path | What it holds |
|---|---|
| `pipeline/` | the numbered stages and the orchestrator that runs them |
| `common/` | code shared between stages |
| `config/` | settings and model lists |
| `operations/` | the `verbatus` operator command, image intake, GPU serving, notifications |
| `proof/` | small synthetic test pages, safe to publish |
| `gold/` | tools for building human-checked reference samples |
| `pagekit/` | page preparation: rotating, splitting, straightening and cropping scans |
| `.githooks/` | git hooks and the checks CI runs |
| `private/`, `scriptorium/`, `workbench/` | local only, never committed |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). AI coding agents also follow
[AGENTS.md](AGENTS.md). The code here is written by AI models, directed and reviewed by
the project lead. Each commit names the model that wrote it.

## Licence

The code is licensed under the Apache License 2.0 (see [LICENSE](LICENSE)). Copyright
2026 Tyrel Somerville, project lead.

The models are not included in this repository. Each is downloaded at a pinned
revision when a run starts, under its own licence (listed in `config/models-real.toml`).
The licences differ:

- Churro's weights are for research use only.
- Chandra's licence restricts commercial use above a revenue threshold, and use that
  competes with its maker.
- The DAI reader declares no licence.
- The record detector's weights are AGPL-3.0.
- Surya's layout weights are OpenRAIL.
- The Qwen reader is Apache-2.0.

The record detector runs through `ultralytics` (AGPL-3.0). It is installed only on the
GPU machine, never in this repository's own environment.
