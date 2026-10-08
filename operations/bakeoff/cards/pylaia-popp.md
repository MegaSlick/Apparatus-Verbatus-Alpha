# Teklia PyLaia POPP run card
**Verdict in one line:** ready (the LM arm ran end to end here with the real model on
blla crops of a synthetic page, on the CPU; the Mac install is untested)

## Identity
- Repo `Teklia/pylaia-popp`, commit `f002691c2610670dc609d5d7d0a7ae692b643ebc`
  (2024-09-10).
- Licence: MIT (model card front matter; the plan's figure confirmed).
- Files: `weights.ckpt` 42,696,796 bytes, SHA-256
  `535712a733c8d5e7406d90e39a8c82c065a127dffa6762690d4960cf9d206d7a`; `model`;
  `syms.txt` (87 symbols: no `«»`, `§`, `œ`, `É`, `/`, `.` or `,`); `tokens.txt`,
  `lexicon.txt`; `language_model.arpa.gz`, 4,391,627 bytes, SHA-256
  `e7d2dc6ec84ff4302315e61554fe7dfc3a44dc304bb1d83c560d6da3f4582bf6`, plain ARPA
  despite its name.
- Family: CTC line recogniser, the same `LaiaCRNN` shape as Belfort. Trained on POPP
  generic (Paris census tables), 3,835 training lines, 128 px. Card test CER 16.49%
  greedy, 16.09% with the LM (WER 36.26 / 34.52).

## Install
The PyLaia environment, shared with Belfort: see `pylaia-belfort.md`
(`operations/bakeoff/lines/venvs/pylaia/pyproject.toml`; 204 s cold here).

## Native inference path
As `pylaia-belfort.md`: `pylaia-htr-decode-ctc --config <stem>.yaml` per page, with this
repo's files.

## Prompt
none: CTC

## Preprocessing
Grey, 128 px high (the card says 128 px; the model file agrees), Lanczos, aspect kept,
from the shared Surya or blla crops; cached at `<out>/_lines/<source>-h128/`.

## Decoding
Greedy CTC; `-lm` arms: the 6-gram character LM through torchaudio's beam search,
weight 1.5 (not given by the card; the PyLaia documentation's example), as Belfort.

## Output and normalisation
As Belfort: `<id> <text>` lines mapped back to crops, NFC, whitespace collapsed.

## Resources
As Belfort: CPU; about 3.4 s for a page of 16 short blla lines here, the start-up
included.

## Known failure modes
- Trained on census table rows: on running prose it is out of domain; on our table pages
  each Surya or blla line may span several columns, which POPP's training rows (one
  table row a line) partly match.
- The charset lacks full stops, commas and slashes, so dates and abbreviations lose them.

## What differs from our vLLM adapter
no vLLM adapter exists

## Untested here
Real register pages; the Mac install; `--device cuda`. The Mac runs first:
`python -m operations.bakeoff.lines.pylaia check`

## Our arm
- Module `operations.bakeoff.lines.pylaia --model popp [--lm]`; arms `pylaia-popp-surya`,
  `pylaia-popp-blla`, `pylaia-popp-lm-surya`, `pylaia-popp-lm-blla`.
- `python -m operations.bakeoff.lines.pylaia fetch --model popp --store-root STORE`

## Sources
- https://huggingface.co/Teklia/pylaia-popp at `f002691c2610670dc609d5d7d0a7ae692b643ebc`
- pylaia 1.1.2 wheel from PyPI (see `pylaia-belfort.md`)
