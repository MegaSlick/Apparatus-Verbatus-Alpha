# Party v2 run card
**Verdict in one line:** ready-untested-here on the GPU (installed and ran end to end
here on the CPU with the real model on a synthetic page); cut first if behind schedule

## Identity
- Model: Zenodo record 20642057, DOI `10.5281/zenodo.20642057` (concept DOI
  `10.5281/zenodo.14616980`, which Party's README names), "Pretrained multilingual Party
  base model", 2026-06-11. `model.safetensors` 518,329,816 bytes, Zenodo MD5
  `cf165e67061d492b72f600a6a72b7c61`, SHA-256
  `d6f3c2273687a79dd4852c4cfe63ec4c9e75a2a148fe02a8b787ab6afec236aa` (measured here).
  Metadata: variant `base`, image size 2560 x 1920.
- Code: github.com/mittagessen/party, `main` at `c2589b1b515ed690f883c6afaef6c01ce29bf72d`
  (2026-06-12, "Add part ocr command"); there are no release tags.
- Licence: Apache-2.0 (model record and code). Party also loads two Hub models first:
  `timm/swin_base_patch4_window7_224.ms_in22k_ft_in1k` (MIT) at
  `a6a1eb2321b4f556fa0fa243fb777d47679f13c9` and `mittagessen/bytellama-43m-cc`
  (Apache-2.0) at `05e49f536fbb393a7127055f883d34606bba7712`.
- Family: page-wise generative recogniser (Swin-base encoder, 40M-parameter byte-level
  Llama decoder) prompted with line positions. **Not CTC**: it can write text that is not
  on the page.

## Install
- Own environment, `operations/bakeoff/lines/venvs/party/pyproject.toml`: `party` from git
  at the commit above and `kraken==7.0.3` (Party requires `kraken~=7.0.0b5`, so it cannot
  share the kraken 7.1 environment). `uv.lock` pins torch 2.12.0, lightning 2.6.1, timm
  1.0.30. Install needs git and network (versioningit builds from the git checkout).
- `python -m operations.bakeoff.lines.party install`: 116 s cold here (linux x86_64,
  cached wheels shared with the kraken install); estimate 4-5 min on a fresh pod.
- `fetch` must run once with network: besides the Zenodo model it fills
  `<weights>/hf-cache` with the two base models (501 MB), because Party builds its
  network from them by name before loading `model.safetensors` over them; without that
  the offline run fails ("cannot find the requested files in the local cache", seen here).
  The cache's `main` ref is pointed at the pinned commits.
- No CUDA build step; CUDA only through torch's wheel.

## Native inference path
- `party/cli/ocr.py`@c2589b1 (`ocr`, `_recognize`), `party/cli/set_lang.py`,
  `party/configs.py` `PartyRecognitionInferenceConfig`, `party/party.py`
  (`PartyModel.__init__`, `prepare_for_generation`, `predict_tokens`), README "Inference".
- Our arm, per batch of pages, the vendor's own commands:
  `party set-lang fra <copies of the blla ALTO>` then
  `party -d cuda:0 --precision bf16-mixed ocr -l model.safetensors -a -B 32 --prompt-mode curves --add-lang-token -i <in>.xml <out>.xml ...`
  (CPU: `party --threads N ocr ...`, precision 32-true).

## Prompt
No text prompt. Each line is prompted by its blla baseline curve; `--add-lang-token`
prepends the language token `fra` (Party index 11) set by `set-lang`.

## Preprocessing
The page image named in the ALTO; Party resizes it for the encoder (2560 x 1920 per the
model metadata; Party's resize code not read). Lines: the shared blla segmentation.

## Decoding
Greedy (argmax) until EOS; Party's default 512 tokens a line, clamped by the decoder to
384 (logged here). Batch 32 lines. No LM, no beam.

## Output and normalisation
ALTO from kraken 7.0.3's template; the same rule as kraken (`blla.read_alto`), then NFC
(Party predicts NFD-trained bytes). Each TextLine is one unit in the record, with its
polygon's box and baseline (`blla.alto_units`). On the CPU the arm passes `-d cpu`
(Party's default device is `auto`).

## Resources
- GPU recommended (`--device cuda`, bf16-mixed). VRAM: not found; estimate under 8 GB at
  batch 32 (a 518 MB model), fits the 24 GB card. Not measured.
- CPU measured here (4 vCPU): 98 s for one synthetic page with 16 blla lines, model load
  included (about 80 s of recognition).

## Known failure modes
- Install is the highest risk: git build, kraken 7.0 pin, a hidden Hub fetch.
- Generative: seen here, where blla cut one line into fragments, Party wrote the whole
  line's text for several fragments, so text repeats. It can also invent.
- Mixed transcription conventions in its training (abbreviations sometimes expanded).

## What differs from our vLLM adapter
no vLLM adapter exists

## Untested here
The GPU path (bf16, VRAM, speed); the Mac install; real pages. The Mac runs first:
`python -m operations.bakeoff.lines.party install && python -m operations.bakeoff.lines.party check`

## Our arm
- Module `operations.bakeoff.lines.party`; arm `party-blla`; label `party-blla`.
- Needs the blla segmentation first (`python -m operations.bakeoff.lines.blla prepare`).
- `python -m operations.bakeoff.lines.party fetch --store-root STORE`, then
  `python -m operations.bakeoff.lines.party run --pages DIR --out CACHE --store-root STORE --device cuda`

## Sources
- https://zenodo.org/api/records/20642057 and its `README.md`; record versions list
- https://github.com/mittagessen/party at `c2589b1b515ed690f883c6afaef6c01ce29bf72d`
  (`pyproject.toml`, `README.md`, `party/cli/ocr.py`, `party/cli/set_lang.py`,
  `party/configs.py`, `party/party.py`, `party/fusion.py`, `party/tokenizer.py`)
- Hugging Face API for the two base models at the commits above
