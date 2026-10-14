# Training data for the Perlector

Turns finished pipeline runs and references (gold or silver) into training examples for the
Perlector: the same page shown many times under different witness stories, with the same
answer every time. Nothing here trains a model; it writes data a trainer (Unsloth, ms-swift,
mlx-vlm) reads.

## The exporter (`export.py`)

```sh
.venv/bin/python -m operations.training.export --run-tree <run tree> \
  --gold "$HOME/Desktop/Bake-off set/Pages" --gold-glob '*/Prepped/*.txt' \
  --held-out <frozen test list> --out <dir> [--seed 0] [--variants-per-page 4] \
  [--mix honest=40,plant-1=12,...] [--blinded-share 0.5] [--reference <silver dir>]
```

For every page with a reference that is not held out, it draws `--variants-per-page`
scenarios from the feed mix (`operations/bakeoff/mutations.py`; default 40 honest, 25 planted
as 12 / 8 / 5 for one, two and all witnesses, 20 removed with 10 of them blind, 15
structural), rewrites the witness testimony, and writes one example per draw.

**The prompt and images are the Perlector's.** Each example's user text is
`common.page_prompt.build_page_prompt`'s rendering of the shown feed (via
`fed_arm.build_body`), so training prompts equal serving prompts byte for byte. The images
follow the pipeline's order: the page render, then the overlay when the feed draws one
(`fed_arm.request_images`). Each example lists `images` and `image_sha256s`, one `{"type":
"image"}` block per image before the prompt, its `request_digest`, and
`chat_template_kwargs` with thinking off. Before any variant is drawn, every page's source
request must rebuild the run's (prompt, render and overlay digests, `request_digest`), or
the export stops naming the page, unless `--allow-prompt-mismatch` leaves it out
(`prompt_mismatch_excluded`). An honest example's request digest must equal the run's. A
real run tree's stage seals are proven first.

**The answer is the reference as a page answer** (`common.page_answer`): `{"page_type",
"writing", "entries", "set_aside"}`, each entry one of the seven entry kinds
(`common.page_types`) with `n`, `kind`, `label`, `cites`, `text` and both continuation
flags.

- A gold page's type comes from its CATEGORY (`acts-*` register-acts, `index`, `list` table,
  `ledger`, `contract` instrument, `blank` and `near-blank` blank when it has no text) and
  its writing from its FORM; its rows take that type's kind (`index-row`, `table-row`,
  `ledger-entry`). A page the header does not settle (`non-register`, no FORM) is refused.
  `--row-kind` overrides the row kind.
- `set_aside` holds planted units (an invented act, an injection: "not on the page") and
  shown units whose text matches no entry.
- Cites are rebuilt from the shown feed (witness units by word overlap, Surya lines and
  blocks by row) unless the reference brings its own; rebuilt cites are a draft and weigh
  `CITES_WEIGHT` (0.3). A reference's own cites are remapped through the mutation's `id_map`;
  a cite to a unit no longer shown is dropped, and every shown id not cited is set aside.
- Each mutation names its reference (`reference_sha256`, `reference_record_sha256`); the
  target is built only from that same reference, and both digests go into each example.
- An example that fails any rule is not written; `manifest.json`'s `refused` says why.

**Loss weights** are a separate field, `loss_spans`: `[start, end, weight]` character spans
tiling the assistant text. Each word weighs by its reference status: checked 1.0, agreed
0.7, draft 0.3, unresolved 0; the JSON scaffold 1.0; cites as above. Entry text keeps the
reference's doubt marks (`[[?]]`, `[[reading|other]]`; struck text dropped, inserted text
kept), so the model learns to say where the ink is unsure; readings inside a mark are
unresolved, and doubt is tracked by position. A trainer maps the spans onto its own tokens.

**The reference.** A bake-off gold file (unchecked fool's gold), with statuses from witness
agreement (`agreed` where two or more shown witnesses have the word, `unresolved` inside
`[[a|b]]`, `draft` otherwise), or `training-reference.v2` records from `--reference <dir>`,
one JSON per page stem, which win over a gold file of the same stem:

```json
{"schema": "training-reference.v2", "stem": "<page stem>", "status_label": "silver",
 "page_type": "register-acts", "writing": "handwritten",
 "entries": [{"kind": "act", "label": "baptism", "text": "...", "cites": ["A1", "L1"],
              "continues_from_previous_page": false, "continues_to_next_page": false,
              "words": [{"text": "Le", "status": "checked", "cls": "word"}, ...]}]}
```

`page_type` and `writing` are required (a v1 record is refused). `words` is optional (every
word `checked` on a `lead-checked` page, else `draft`); so are `cites` (rebuilt) and `cls`
(the heuristic classifier decides).

**Held out.** `--held-out` is required: a text file of page stems or file names (`#`
comments; only an extension such as `.tif` is cut). A listed page is never exported, nor is
the other half of its original (`X_1L` and `X_2R`). Invented acts and injections borrow text
only from exportable pages, never from a held-out page or the page's own other half.

**Blinded regime.** `--blinded-share` of the examples (default half) show pseudonym witness
labels with the chair hidden, so trust cannot attach to a name; the sidecar keeps the chair.

**Reproducible.** A page's examples are a function of the page and `--seed`, independent of
which other pages are exported. `--out` inside the repository is refused unless under
`private/`, `workbench/` or `scriptorium/`.

## Output

| File | Contents |
|---|---|
| `train.jsonl` | one example per line: `id`, `page`, `images`, `messages`, `loss_spans`, `scenario`, `family`, `witness_regime`, `planted_sites`, `set_aside_ids`, `image_sha256s`, `prompt_sha256`, `request_digest`, `prompt_matches_run`, `request_matches_run`, `reference_status`, `reference_sha256`, `reference_record_sha256`, `tokens` |
| `planted.jsonl` | each example's mutation sidecar (`witness-mutation.v1` without the feed) |
| `images/<stem>.png`, `images/<stem>.overlay-<sha16>.png` | the run's page renders and each distinct overlay sent (`--no-copy-images` references the render in the run; overlays are still written) |
| `manifest.json` | counts by scenario and family, blinded examples, planted sites by class and k, held-out exclusions, prompt and request checks, examples by image count, the voting-must-lose check, and token lengths |

No tokenizer is available offline, so prompt length is reported with the pipeline's sealed
upper bound (`common.request_capacity`) plus the run's image tokens, and the answer with its
characters, bytes and an estimate.

## Bulk export with no witnesses (`bulk.py`)

```sh
.venv/bin/python -m operations.training.bulk \
  --references "$HOME/Verbatus_Corpus/parishes/*/reference" [--references <more>] \
  --images-map "$HOME/Verbatus_Corpus/catalog.csv" --held-out <frozen test list> \
  --out <dir> [--seed 0] [--max-pages N] [--max-per-volume N] [--val-share 0.05] \
  [--weights-by-status agreed=0.7,close=0.5,...] [--long-edge 2560] [--no-copy-images] \
  [--workers 8]
```

Stage 1 of training: many pages of many hands with silver answers, before the witness
training above. It needs no run tree. Each page gives one example: the page render and
the Perlector's prompt for a page with no witness testimony and no Surya census, which is
the feed the pipeline itself builds for such a page (`page_feed.assemble_page_feed` with
`no_testimony=True` and Surya absent), so the prompt is the serving prompt byte for byte.
It shows no ids, so every entry's `cites` is `[]` and nothing is set aside. The render has
the pipeline's pixels (`page_render.render_pixels`), saved as an ordinary PNG.

- **References**: `training-reference.v2` records, from folders, files or globs. The answer
  is built with the exporter's own code and must parse with `common.page_answer`. A kind
  that is not an entry kind becomes `other`; the reference's own cites are dropped.
- **Images map**: JSONL `{stem, image, sha256, volume?, collection?}`, or the corpus
  `catalog.csv` as it is. Each image is checked against its sha256. A page's volume comes
  from the map, else from the folder above `reference/`.
- **Weights**: one `weight` per example from the reference's `provenance.best_status`:
  agreed 0.7, close and majority 0.5, single 0.3, a page with no status 0.3, lead-checked
  1.0; disputed pages are left out. `loss_spans` weigh each word by its status, as above.
- **Held out** as above, the other half of a held-out original included.
- **Spread**: `--max-per-volume` caps each volume, and `--max-pages` takes a page from each
  volume in turn, so no reel dominates. `val.jsonl` holds whole volumes (`--val-share` of
  the pages), never pages of a volume that trains.
- `--no-copy-images` writes no renders: each example lists its source image and the
  transform the trainer must apply.

`manifest.json` counts examples by page type, writing, status, volume and collection,
lists what was left out and why, and estimates tokens: image tokens from the Perlector
row's image geometry, answer tokens as characters / 4, and the prompt's sealed bound.
These are estimates, not a tokenizer's count.

Not built: image counterfactuals (blur, blank, swap) as training examples, which need checked
labels for what the model should say about such a page (`fed_arm --image` serves them for
evaluation).
