# Training data for the Perlector

Tools that turn finished pipeline runs and references (gold or silver) into training
examples for the Perlector, following the merged training plan: the same page shown many
times under different witness stories, and the same answer every time. Nothing here
trains a model; it writes data a trainer (Unsloth, ms-swift, mlx-vlm) reads.

## The exporter (`export.py`)

```sh
.venv/bin/python -m operations.training.export --run-tree <run tree> \
  --gold "$HOME/Desktop/Bake-off set/Pages" --gold-glob '*/Prepped/*.txt' \
  --held-out <frozen test list> --out <dir> [--seed 0] [--variants-per-page 4] \
  [--mix honest=40,plant-1=12,...] [--blinded-share 0.5] [--reference <silver dir>]
```

For every page of the run tree that has a reference and is not held out, it draws
`--variants-per-page` scenarios from the feed mix (`operations/bakeoff/mutations.py`,
default the plan's: 40 honest; 25 planted as 12 / 8 / 5 for one, two and all witnesses;
20 removed, 10 of them blind; 15 structural), rewrites the witness testimony for each,
and writes one example per draw.

**The prompt is the Perlector's.** Each example's user text is what `fed_arm.build_body`
renders for the shown feed, which is `common.page_prompt.build_page_prompt`, the pipeline's
own builder, so training prompts equal serving prompts byte for byte. An honest, named
example is also checked against the run's recorded prompt digests and the manifest counts
how many matched. The image is the run's page render (copied to `images/`), sent first as
the pipeline sends it; `chat_template_kwargs` records thinking off.

**The answer is the reference in the Perlector's grammar** (`common.page_answer`): the
entries in order with `n`, `kind`, `label`, `cites`, `text` and both continuation flags,
and `set_aside` for the planted units (an invented act, an injection: reason "not on the
page") and for shown units whose text matches no entry. Cites are rebuilt from the shown
feed (witness units by word overlap with the entry, Surya lines and blocks by the rows of
the cited boxes) unless the reference brings its own; rebuilt cites are a draft and weigh
`CITES_WEIGHT` (0.3). Index rows become entries of kind `--row-kind` (default `other`)
until the entry kinds are settled (training plan, decision 4).

**Loss weights** are a separate field, `loss_spans`: `[start, end, weight]` character
spans that tile the assistant text. Each word carries its reference status's weight:
checked 1.0, agreed 0.7, draft 0.3, unresolved 0; the JSON scaffold 1.0; cites as above.
The entry text keeps the Perlector's doubt marks as the reference writes them (`[[?]]` for
unread ink, `[[reading|other]]` for an uncertain one; struck text dropped, inserted text
kept), so the model learns to say where the ink is unsure: the readings inside a mark are
`unresolved` (0) and the mark syntax weighs as a draft word (1.0 on a lead-checked
reference). Doubt is tracked by position, so a certain word spelled like a doubtful one
elsewhere stays certain.
A trainer that masks by token maps these spans onto its tokens; the data never changes
when the trainer does.

**The reference.** Today: a bake-off gold file (fool's gold, an unchecked AI draft), with
statuses from witness agreement (`agreed` where two or more shown witnesses have the word,
`unresolved` for a `[[a|b]]` reading, `draft` otherwise). Tomorrow: `training-reference.v1`
records from the silver tooling, `--reference <dir>`, one JSON per page stem:

```json
{"schema": "training-reference.v1", "stem": "<page stem>", "status_label": "silver",
 "entries": [{"kind": "act", "label": "baptism", "text": "...", "cites": ["A1", "L1"],
              "continues_from_previous_page": false, "continues_to_next_page": false,
              "words": [{"text": "Le", "status": "checked", "cls": "word"}, ...]}]}
```

`words` is optional (then every word is `checked` for a `lead-checked` page, `draft`
otherwise); `cites` is optional (then they are rebuilt); `cls` is optional (then the
heuristic classifier decides). A reference found here wins over a gold file of the same
stem.

**Held out.** `--held-out` is a text file of page stems (or file names; `#` comments),
required. A listed page is never exported, and neither is the other half of its original
(`X_1L` and `X_2R` belong together). Invented acts and injections borrow text only from
pages that may be exported, never from a held-out page or the page's own other half.

**Blinded regime.** `--blinded-share` of the examples (default half, A7a2's regime) show
pseudonym witness labels with the chair hidden, so trust cannot attach to a name; the
sidecar keeps the chair.

## Output

| file | what |
|---|---|
| `train.jsonl` | one example per line: `id`, `page`, `images`, `messages` (user: image + prompt; assistant: the answer JSON), `loss_spans`, `scenario`, `family`, `witness_regime`, `planted_sites`, `set_aside_ids`, `prompt_sha256`, `prompt_matches_run`, `reference_status`, `tokens` |
| `planted.jsonl` | the mutation sidecar per example (`witness-mutation.v1` without the feed): what was planted where, in which witness |
| `images/<stem>.png` | the run's page renders |
| `manifest.json` | counts by scenario and family, blinded examples, planted sites by class and k, held-out pages excluded, honest prompts checked against the run, the voting-must-lose check on the whole set and on the honest feeds alone, and the token-length summary |

Token lengths: no tokenizer is available offline, so the prompt is reported with the
sealed upper bound the pipeline admits requests by (`common.request_capacity`, reported
text at one token per byte, fixed text at the carried rate) plus the run's image tokens,
and the answer with its characters, bytes and the carried-rate estimate.

## Not built here

Image counterfactuals (blur, blank, swap) as training examples: they need checked labels
for what the model should say about a blurred page. `fed_arm --image` still serves them
for evaluation. Up-weighting disputed tokens (A7a2's x3) and the DAI imitation on
RecordGold pages are for the silver tooling and the RecordGold exporter.
