# Teklia PyLaia Belfort run card
**Verdict in one line:** ready (greedy and LM arms ran end to end here with the real model
on synthetic pages, on the CPU; the Mac install is untested)

## Identity
- Repo `Teklia/pylaia-belfort`, commit `d35f921605314afc7324310081bee55a805a0b9f`
  (2024-09-10).
- Licence: MIT (model card front matter `license: mit`; the plan's figure confirmed):
  use, modify and redistribute with the notice.
- Files: `weights.ckpt` 42,819,548 bytes, SHA-256
  `e73ff66f52effd625d4063b5549755bfa0b69ed9fc28edf6c6d30af111f723cd`; `model` (the
  architecture); `syms.txt` (117 symbols: ASCII, French accented letters, `œ`, `§`,
  `«»`, `’`, `…`, `<space>`, `<unk>`); `tokens.txt` and `lexicon.txt` (for the LM);
  `language_model.arpa.gz`, 11,272,705 bytes, SHA-256
  `cbb30d655c189a8f13e3a87c47252a65198493ee6d0859dcd289f97cee4ee6fa`: at this commit
  it is plain ARPA text despite its name.
- Family: CTC line recogniser (`LaiaCRNN`: 4 conv layers 12/24/48/48, 3-layer BLSTM 256).
  French handwriting, Belfort dataset (1790-1946), 25,800 training lines. Card test CER
  10.54% greedy, 9.52% with the LM (WER 28.12 / 23.73).

## Install
- Own environment, `operations/bakeoff/lines/venvs/pylaia/pyproject.toml`: `pylaia==1.1.2`,
  Python 3.10 (PyLaia requires `<3.11`), `setuptools==70.3.0` (torchmetrics 0.7 imports
  `pkg_resources`) and `pip==26.2` (PyLaia runs `python -m pip freeze` at start and stops
  without pip). Both were found missing here and added. `uv.lock` (linux x86_64 and
  macOS arm64) pins torch 1.13.1, torchaudio 0.13.1, pytorch-lightning 1.4.2.
- `python -m operations.bakeoff.lines.pylaia install`. Measured here: 204 s cold on linux
  x86_64 (3.6 GB venv); about 1 s when synced. Mac: estimate 1-2 min.
- Risks: torch 1.13 is old; macOS arm64 wheels exist for Python 3.10 (resolved, not
  installed here).

## Native inference path
- `laia/scripts/htr/decode_ctc.py`@1.1.2 (`run`, `get_args`), `laia/callbacks/decode.py`
  (`Decode.on_test_batch_end`), `laia/decoders/ctc_language_decoder.py`,
  `laia/engine/data_module.py`.
- Per page: `pylaia-htr-decode-ctc --config <stem>.yaml`, the config naming `syms.txt`,
  the page's image list, the image folder, `common.train_path=<weights>`,
  `experiment_dirname=.`, `model_filename=model`, `checkpoint=weights.ckpt`,
  `data.batch_size=8`, `data.color_mode=L`, `decode.join_string=""`,
  `decode.convert_spaces=true`, `trainer.gpus=0` (1 with `--device cuda`).

## Prompt
none: CTC

## Preprocessing
- Grey (`L`), resized to 128 px high, aspect kept, Lanczos (PyLaia's own resize filter).
  The model file fixes it: one input channel and the `none-16` sequencer after three 2x
  poolings means exactly 128 px. The card's height reads "{dimension} pixels" (left
  unfilled). PyLaia's decode does not resize; it inverts the image itself.
- Line source: the shared Surya crops or blla crops (polygon mask on white, the polygon's
  box), then the resize above, cached at `<out>/_lines/<source>-h128/`.

## Decoding
- `pylaia-belfort-*`: greedy CTC (`CTCGreedyDecoder`), temperature 1.0, batch 8.
- `pylaia-belfort-lm-*`: `CTCLanguageDecoder`: torchaudio 0.13.1 `ctc_decoder` (flashlight
  beam search, CPU only) with the 6-gram character LM, `tokens.txt`, `lexicon.txt`,
  blank `<ctc>`, unk `<unk>`, silence `<space>`, `language_model_weight` 1.5; other
  settings torchaudio's defaults (beam 50, beam threshold 50, word score 0). The card
  gives no LM weight: 1.5 is the PyLaia documentation's example. Open question.

## Output and normalisation
PyLaia prints `<id> <text>` per line image (ids are the crop numbers `0001`...). The
normaliser keeps lines matching `^\d{4} `, maps them back to crops, NFC, whitespace
collapsed; a crop with no printed line is that unit's error.

## Resources
CPU (GPU optional; the LM decode is CPU only). VRAM not measured; a few MB of weights.
Measured here (4 vCPU): about 10 s for a 3-line page, of which about 6 s is start-up;
estimate 10-20 s for a 30-line page.

## Known failure modes
Charset limited to `syms.txt`; trained on one archive's hands; the LM pulls toward
Belfort text. A misread letter, never an invented line.

## What differs from our vLLM adapter
no vLLM adapter exists

## Untested here
Real register pages; the Mac install; `--device cuda`. The Mac runs first:
`python -m operations.bakeoff.lines.pylaia install && python -m operations.bakeoff.lines.pylaia check`

## Our arm
- Module `operations.bakeoff.lines.pylaia --model belfort [--lm]`; arms
  `pylaia-belfort-surya`, `pylaia-belfort-blla`, `pylaia-belfort-lm-surya`,
  `pylaia-belfort-lm-blla`.
- `python -m operations.bakeoff.lines.pylaia fetch --model belfort --store-root STORE`, then
  `python -m operations.bakeoff.lines.pylaia run --model belfort --lm --lines surya --lines-dir SURYA_DOCS --pages DIR --out CACHE --store-root STORE`

## Sources
- https://huggingface.co/Teklia/pylaia-belfort at `d35f921605314afc7324310081bee55a805a0b9f`
  (README.md, model, syms.txt, tokens.txt, lexicon.txt; LFS digests from the Hub API)
- pylaia 1.1.2 wheel from PyPI (files above); https://doc.teklia.com/pylaia/usage/prediction/
