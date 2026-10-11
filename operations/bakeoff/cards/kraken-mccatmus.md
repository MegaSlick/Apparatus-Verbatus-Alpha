# kraken McCATMuS v1 run card
**Verdict in one line:** ready (kraken 7.1.1's own loader read the file and both arms'
commands ran on synthetic images on the lead's Intel Mac, but under torch 2.2.2, not the
pinned 2.14.0; the pinned environment is untested with this file)

## Identity
- Model: Zenodo record 13788177, DOI `10.5281/zenodo.13788177` (v1), by Alix Chagué
  (ALMAnaCH, Inria; Université de Montréal; EPHE).
- File: `McCATMuS_nfd_nofix_V1.mlmodel`, 16,173,802 bytes (16.2 MB), Zenodo MD5
  `e531463f631303c700750784b4f9ed63`, SHA-256
  `dfb911ba25fd11f93efc1b0c340957162981ecfdaac0ee1e26793d491f770244` (measured). The arm's `revision` field records `<file>@sha256:<that digest>`.
- Licence: CC BY 4.0 (Zenodo metadata and the file's `metadata.json`): use and adapt
  with attribution. The file is fetched at run time, never stored in this tree.
- Training (Zenodo description): 22 datasets aggregated under the CATMuS transcription
  guidelines, mostly French, also Latin, Spanish, English, German and Italian;
  handwritten, typewritten and printed; late 16th century to 2023 (the HF dataset
  `CATMuS/modern`). Validation character accuracy in the file: 92.8% after 158 epochs.
- Family: kraken 4.x CoreML recogniser, VGSL
  `[1,120,0,1 Cr3,13,32 Do Mp2,2 Cr3,13,32 Do Mp2,2 Cr3,9,64 Do Mp2,2 Cr3,9,64 Do S1(1x0)1,3 Lbx200 Do Lbx200 Do Lbx200 Do O1c117]`:
  four convolutions, three bidirectional LSTMs, CTC over 116 symbols. It misreads
  letters; it cannot invent text.

## Install
The kraken environment the PP-OCRv6 arms use (`lines/venvs/kraken/`, `kraken==7.1.1`,
coremltools 9.0 in the lock); nothing new. See `kraken-ppocrv6.md`.

## Native inference path
- kraken 7.1.1 reads `.mlmodel` files with its `coreml` loader
  (`kraken/models/loaders.py` `load_coreml`, entry point `kraken.loaders`): it reads the
  `vgsl` and `codec` metadata with coremltools, builds a `TorchVGSLModel` and loads the
  weights from the CoreML layers (`kraken/models/_coreml.py`). No conversion step.
- `kraken-mccatmus-blla`: `kraken -a -i <page> <raw>.xml segment -bl ocr -m McCATMuS_nfd_nofix_V1.mlmodel`
- `kraken-mccatmus-surya`: `kraken -i 0001.png 0001.txt ... ocr -s -m McCATMuS_nfd_nofix_V1.mlmodel`

## Prompt
none: CTC

## Preprocessing
kraken scales each line to the file's input spec: 120 px high, one channel (grey,
`one_channel_mode` L), any width, 16 px padding (the file's `pad`). Line sources as in
`kraken-ppocrv6.md`.

## Decoding
Greedy CTC, kraken's default; no language model, no beam.

## Output and normalisation
- Same files and units as the PP-OCRv6 arms (ALTO for blla, one text file per crop for
  surya).
- **The model writes NFD** (its `hyper_params.normalization` is NFD; seen here: `à` and
  `é` came back as a letter plus U+0300 / U+0301). `harness.plain` turns every answer to
  NFC, and the scorer's graphemic-v1 profile (`operations/corpus/normalization.py`) runs
  NFC before and after its mappings, so NFD and NFC readings score alike.
- CATMuS keeps abbreviations as written and has signs the gold may not use: ⁊ (U+204A),
  ꝑ (U+A751), Ꝯ (U+A76E), ꝰ (U+A770), combining letters above (U+0368, U+036B),
  a private-use sign (U+E8BF). Nothing maps them; where the gold writes plain letters
  they count as errors.

## Resources
CPU, `--threads N`. Measured on the Intel Mac (i9-9980HK, 2 threads, torch 2.2.2):
`ocr -s` on two synthetic lines 28 s with start-up; blla segmentation and recognition of
a synthetic 1000 x 320 page 190 s. VRAM: not measured; a 16 MB model.

## Known failure modes
- Zero-shot on these registers; trained on broad material where most lines are not
  19th-century civil registers.
- On the synthetic printed page blla cut two lines into twelve fragments (as on the
  PP-OCRv6 card's synthetic page); the blla arm inherits whatever blla does.
- No hallucination, but a line the segmenter misses is lost.

## What differs from our vLLM adapter
no vLLM adapter exists

## Untested here
Real register pages; the pinned torch 2.14.0 with this file (no Intel-Mac wheel exists:
the check above used torch 2.2.2 with `add_safe_globals` stubbed); the GPU path. First
on the pod or the arm64 Mac:
`python -m operations.bakeoff.lines.kraken_ppocr fetch --model mccatmus --store-root STORE`
then a `--limit 2` run.

## Our arm
- Module `operations.bakeoff.lines.kraken_ppocr --model mccatmus`; arms
  `kraken-mccatmus-blla`, `kraken-mccatmus-surya`; weights in `<store>/hf/kraken-mccatmus-v1/`.

## Sources
- https://zenodo.org/api/records/13788177 and its `metadata.json`
- the file's own CoreML metadata (`kraken_meta`, `vgsl`, `codec`), read with coremltools 9.0
- kraken 7.1.1 wheel from PyPI: `kraken/models/loaders.py`, `kraken/models/_coreml.py`
