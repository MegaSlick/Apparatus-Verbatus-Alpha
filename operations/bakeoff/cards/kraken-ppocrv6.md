# kraken PP-OCRv6 (medium) run card
**Verdict in one line:** ready (both arms ran end to end here with the real model on a
synthetic page, on the CPU; the Mac install and the GPU path are untested)

## Identity
- Model: Zenodo record 21788410, DOI `10.5281/zenodo.21788410` (concept DOI
  `10.5281/zenodo.21788409`), published 2026-08-04, by Benjamin Kiessling (Inria).
- File: `medium.safetensors`, 63,779,644 bytes (64 MB), Zenodo MD5
  `e3411a453ce3b9e9efae3f8631d85762`, SHA-256
  `15313b51ace64cbfa81f8f6ef25ad64f04e5a6fb7f7823e67b107527bc081ac9` (measured here).
  The arm's `revision` field records `medium.safetensors@sha256:<that digest>`.
- Licence: Apache-2.0 (Zenodo metadata and the README front matter): use, modify and
  redistribute, with notice.
- Family: CTC line recogniser, about 15.92M parameters (convolutional backbone, no
  recurrent neck); 44 languages, 10 scripts. It misreads letters; it cannot invent text.
- The card's software hint is `kraken_version>=7.1.0`.

## Install
- Own environment, `operations/bakeoff/lines/venvs/kraken/pyproject.toml`: `kraken==7.1.1`,
  Python 3.12; `uv.lock` (resolved here for linux x86_64 and macOS arm64) pins torch
  2.14.0, torchvision 0.29.1, lightning 2.6.1, coremltools 9.0, numpy 2.4.6.
- `python -m operations.bakeoff.lines.kraken_ppocr install [--venv-dir DIR]` runs
  `uv sync --frozen` on that recipe. Measured here: 279 s cold on linux x86_64 (PyPI's
  CUDA torch, 5.8 GB venv); 0.2 s when already synced. On the Mac (CPU torch wheel,
  far smaller): estimate 1-3 min.
- The same environment serves `blla.py` (the shared blla line source).
- Risks: kraken pins torch `<=2.14`; the CUDA torch wheel on the pod is large.

## Native inference path
- `kraken/kraken.py`@7.1.1 (PyPI wheel): `segment` (-bl), `ocr` (`-s/--no-segmentation`:
  "treating each input image as a whole line"), `recognizer`, `segmenter`;
  `kraken/tasks/recognition.py` `RecognitionTaskModel.predict`;
  `kraken/configs/base.py` `RecognitionInferenceConfig` (padding 16, greedy decoder,
  2 line workers).
- `kraken-ppocrv6-blla`, the model card's own command with ALTO out:
  `kraken -a -i <page> <raw>.xml segment -bl ocr -m medium.safetensors`
- `kraken-ppocrv6-surya`, the CLI's documented path for pre-cut lines, one process per page:
  `kraken -i 0001.png 0001.txt -i 0002.png 0002.txt ... ocr -s -m medium.safetensors`

## Prompt
none: CTC

## Preprocessing
- kraken extracts and scales each line itself to the model's input spec. The model file
  declares input `(1, 3, 96, 0)`: RGB, 96 px high, any width. The card says the line
  height was raised to 128 px. The two disagree; kraken follows the file.
- blla arm: kraken's blla baselines and polygons (page segmentation in the same command).
- surya arm: the shared Surya crops (`surya_lines.py`): polygon mask on white, cut to the
  polygon's box, page mode kept (grey or RGB); kraken adds 16 px left and right padding.

## Decoding
Greedy CTC (`kraken.lib.ctc_decoder.greedy_decoder`), temperature 1.0, no language
model, no beam. kraken's default batch size.

## Output and normalisation
- blla: ALTO 4 (kraken's template). Lines in the order of the first `ReadingOrder` group,
  else document order; a line's text is its `String` contents with each `SP` as one space
  (`blla.read_alto`). Each TextLine is one unit in the record, with its polygon's box and
  baseline (`blla.alto_units`).
- surya: one plain-text file per crop, in crop order, one unit per crop.
- Both: NFC (the model writes NFD), whitespace collapsed, empty lines dropped
  (`harness.plain`). One line per written line; no markup.

## Resources
- CPU, `--threads N` (default 4). VRAM: not measured; a 16M-parameter model, well under
  2 GB. `--device cuda` passes `-d cuda:0` and the default `-d cpu` (kraken's own default
  is `auto`, which would take a visible GPU).
- Measured here, a 4-vCPU container, a synthetic 1400 x 500 page: blla arm 95-110 s a
  page, of which blla segmentation is about 80 s (half the network, half scikit-image's
  ridge filter, which stays on the CPU even with a GPU); surya arm 13 s for 3 lines,
  of which about 5 s is start-up, so about 2-3 s a line. A 30-line page: about
  1-1.5 min on 4 vCPU; faster with more threads.

## Known failure modes
- Zero-shot on cursive registers: trained mostly on other material; the card's French
  test CER is 7.56% in-domain. Mixed transcription conventions: it may expand
  abbreviations unpredictably.
- blla over-segments unusual layouts (seen here: one printed line cut into fragments),
  and table rows may be read as many short lines.
- No hallucination, but no recovery either: a line blla misses is lost to this arm.

## What differs from our vLLM adapter
no vLLM adapter exists

## Untested here
Real register pages; the Mac (arm64) install; the GPU path. The Mac runs first:
`python -m operations.bakeoff.lines.kraken_ppocr install && python -m operations.bakeoff.lines.kraken_ppocr check`

## Our arm
- Module `operations.bakeoff.lines.kraken_ppocr`; arms `kraken-ppocrv6-blla`,
  `kraken-ppocrv6-surya`; labels default to the arm names.
- `python -m operations.bakeoff.lines.kraken_ppocr fetch --store-root STORE` (Zenodo,
  MD5 and SHA-256 checked), then
  `python -m operations.bakeoff.lines.kraken_ppocr run --lines blla --pages DIR --out CACHE --store-root STORE`
  or `... run --lines surya --lines-dir SURYA_DOCS ...`.

## Sources
- https://zenodo.org/api/records/21788410 and its `README.md` (read 2026-10-08)
- kraken 7.1.1 wheel from PyPI: `kraken/kraken.py`, `kraken/tasks/recognition.py`,
  `kraken/tasks/segmentation.py`, `kraken/configs/base.py`, `kraken/templates/alto`
