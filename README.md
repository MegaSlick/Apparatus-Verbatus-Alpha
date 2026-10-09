# Apparatus Verbatus

Reads handwritten historical parish and civil registers and recovers the *ipsissima
verba*, the very words on the page. Several vision models act as witnesses and report
what they see on each page; a separate reader model, the **Perlector**, then reads each
whole page itself, names the entries on it, uses the witnesses only as clues, and
establishes the text. Every reading traces back to the exact region of the image it came
from, and uncertainty is to be flagged, never guessed. A page that cannot be read is a
failure; a page read as having no text is held until its ink, its detected lines and
every witness confirm that it is truly blank.

It is built primarily for Quebec parish registers of the 1700s to 1900s, and developed
and tested on those records and on French records of the same era (the RecordGold pages).
It should also work, to a lesser degree, on other archival records — censuses, fur-trade
ledgers, notarial contracts — and on some English-language records.

**Status: alpha.** The staged pipeline, accounting, and export pass synthetic tests.
73 real pages have run on pods through the witnesses, the Perlector, the Coniector and
the Recensor; the Recensor held every page, so no real export exists yet and accuracy is
unestablished. The live reader is asked to use `[[?]]` for
unreadable ink and `[[reading]]` for doubt; they become gaps and uncertain spans.

## How it works

```
page images → Triage → Exemplar → Ink map → Designator → Attestatores → Perlector → Recensor → Archetypus → Armarium
              optional sealed     where the   lines and    witness        reads each  checks     established  export
              cropping source     ink lies    records      models         page        coverage   reading
                                                                             │                                   ▲
                                                                             └──────────► Coniector ─────────────┘
                                                                                          labelled reconstruction
```

[ARCHITECTURE.md](ARCHITECTURE.md) explains each stage and why it is shaped that way;
[GLOSSARY.md](GLOSSARY.md) defines the terms. What the project holds itself to is in
[PRINCIPLES.md](PRINCIPLES.md).

| Role | Model |
|---|---|
| Designator | Surya line and layout detection (`datalab-to/surya`) and `Teklia/YOLOv26-DAI-CReTDHI-Record-Detection` (records) |
| Attestator 1 | `datalab-to/chandra-ocr-2` |
| Attestator 2 | `Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR` |
| Attestator 3 | `stanford-oval/churro-3B` |
| Perlector | `Qwen/Qwen3.8-27B` |
| Coniector | `Qwen/Qwen3.8-27B` (the Perlector's model, text only) |

Models are bound to roles in `config/models-real.toml`. Moving a role to another
revision of the same model is a configuration change; a model that answers in a
different format also needs its own adapter (`common/witness_adapters.py`).

## Getting started

Requirements: Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) exactly 0.12.1
(the project pins it and uv refuses other versions).

```sh
uv sync --frozen --group test --group audit   # create .venv from the lockfile
sh .githooks/install.sh                        # arm the git hooks
sh .githooks/check-static.sh                   # lint, format and document checks (fast)
```

**Try it without a GPU.** The default model roster (`config/models.toml`) uses fixture
stand-ins in place of models, with answers scripted by the chosen scenario, so a local
run on the synthetic pages in `proof/fixtures/` exercises sealing, accounting, review
and export — not reading:

```sh
.venv/bin/python pipeline/orchestrator/run.py --fixture synthetic-two-page-v0 \
  --scenario happy --run-id demo --run-root /tmp/verbatus-demo
```

Each stage writes its sealed output under the run root, ending in `7_armarium/`. This
scenario ends with `run demo: partial` and exit code 3, by design: the synthetic act
that crosses from page 1 to page 2 is flagged, not silently joined, so the export says it
is partial. Exit codes: 0 complete; 2 the run failed or was refused before it could finish; 3 held
for review or partial, which includes an act refused inside an otherwise finished run;
4 halted.

**Real pages** need a Linux GPU machine running vLLM and the real model configuration,
which is two files given together: `--models-config config/models-real.toml` and
`--serving-recipes-config config/serving_recipes_real.toml`. Always give both: the
operator's `run` and the orchestrator refuse either one alone, and on the pod `pod_run`
refuses any roster but the fixture `config/models.toml` without its catalogue (only that
roster falls back to `config/serving_recipes.toml`), each before any stage or model fetch
starts.
[operations/operator/README.md](operations/operator/README.md) describes a run, and the
RunPod tooling is in `operations/pod/`. Input can be most raster images, multi-page TIFF, HEIC
or PDF. Output is a sealed ZIP bundle with a manifest, and it can include a text bundle,
a searchable SQLite database, JSONL, and the items held for human review
(`config/formats.toml`).

## Repository layout

| Path | What it holds |
|---|---|
| `pipeline/` | the numbered stages and the orchestrator that runs them |
| `common/` | the only code shared between stages |
| `config/` | settings and model rosters |
| `operations/` | operator tools, image intake, GPU pod and serving, notifications, review |
| `proof/` | small synthetic fixtures that are safe to publish |
| `gold/` | tooling for building human-checked reference samples |
| `pagekit/` | page preparation for triage: turns scans upright, splits spreads, straightens and crops pages, and checks crops |
| `docs/` | the separation inventory, the rules for AI contributors, and design notes |
| `.githooks/` | git hooks and the check scripts CI runs |
| `private/`, `scriptorium/` | local only; gitignored except their READMEs |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). AI coding agents follow
[AGENTS.md](AGENTS.md) as well.

**All of the code here is written by AI models**, directed and reviewed by the project
lead. Each commit names the model that wrote it (`Co-Authored-By`) and any model that
reviewed it (`Reviewed-by`).

## Roadmap

- **alpha** (this repository): build the pipeline and prove it on a small set of real
  pages.
- **beta**: a fresh, clean repository carrying forward only what survived alpha.
- **1.0**: the public release.

## Licence

The code is under the Apache License 2.0; see [LICENSE](LICENSE). Copyright 2026 Tyrel
Somerville, project lead.

The models are not part of this repository. They are fetched at a pinned revision when a
run starts, each under its own terms, recorded beside each model in
`config/models-real.toml`. They differ: Churro's weights are for research only;
Chandra's bar commercial use above a revenue threshold and competing use; the DAI reader
declares no licence; the record detector's weights are AGPL-3.0; Surya's layout weights
are OpenRAIL; the Qwen reader is Apache-2.0.
The record detector runs through `ultralytics`, which is AGPL-3.0; it is installed
only on the GPU machine, never in this repository's own environment.
