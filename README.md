# Apparatus Verbatus

Reads handwritten historical parish and civil registers and recovers the *ipsissima
verba*, the very words on the page. Several vision models act as witnesses and report
what they see in each entry; a separate reader model, the **Perlector**, then reads the
ink itself, uses the witnesses only as clues, and establishes the text. Every reading
traces back to the exact region of the image it came from, and uncertainty is to be
flagged, never guessed.

It is being developed on French-language parish registers from Quebec, whose
handwriting spans centuries.

**Status: alpha.** The staged pipeline, its accounting and its export are implemented and
tested on synthetic pages. Real pages have run on a GPU pod through the three witnesses,
but no real run has yet produced a final export, so accuracy is not yet established. The
live reader does not yet mark word-level uncertainty; the export says so for every act.

## How it works

```
page images → Exemplar → Ink map → Designator → Attestatores → Perlector → Recensor → Archetypus → Armarium
              sealed     where the   finds the    witness        reads the   checks     established  export
              source     ink lies    entries      models         ink         coverage   reading
```

[ARCHITECTURE.md](ARCHITECTURE.md) explains each stage and why it is shaped that way;
[GLOSSARY.md](GLOSSARY.md) defines the terms. What the project holds itself to is in
[PRINCIPLES.md](PRINCIPLES.md).

| Role | Model |
|---|---|
| Designator, Attestator 1 | `datalab-to/chandra-ocr-2` |
| Attestator 2 | `Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR` |
| Attestator 3 | `stanford-oval/churro-3B` |
| Perlector | `Qwen/Qwen3.8-27B` |

Models are bound to roles in `config/models-real.toml` and can be swapped without code
changes.

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
run on the synthetic pages in `proof/fixtures/` exercises sealing, accounting, recovery
and export — not reading:

```sh
.venv/bin/python pipeline/orchestrator/run.py --fixture synthetic-two-page-v0 \
  --scenario happy --run-id demo --run-root /tmp/verbatus-demo
```

Each stage writes its sealed output under the run root, ending in `7_armarium/`.

**Real pages** need the real roster (`--models-config config/models-real.toml`) and a
Linux GPU machine running vLLM; the RunPod
tooling is in `operations/pod/`. Input can be most raster images, multi-page TIFF, HEIC
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

Apache License 2.0; see [LICENSE](LICENSE). Copyright 2026 Tyrel Somerville, project
lead.
