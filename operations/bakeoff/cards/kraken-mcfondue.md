# kraken Manu McFondue v4 run card
**Verdict in one line:** ready (kraken 7.1.1's own loader read the file and the surya
arm's command ran on synthetic lines on the lead's Intel Mac, but under torch 2.2.2, not
the pinned 2.14.0; the pinned environment is untested with this file)

## Identity
- Model: Zenodo record 10886224, DOI `10.5281/zenodo.10886224` (v4, "Manu McFrench v4"),
  by Simon Gabay, Thibault Clérice and Alix Chagué (Université de Genève / Inria).
- File: `ManuMcFondue.mlmodel`, 16,377,369 bytes (16.4 MB), Zenodo MD5
  `c1c3c628f79f19a7a8116a9afd5c47d2`, SHA-256
  `96e32e782b6627aa57961a6ed5a84c522174630c6bc9f6ebc83eedce1f5e490e` (measured here,
  2026-10-08). The arm's `revision` field records `<file>@sha256:<that digest>`.
- Licence: CC BY 4.0 (Zenodo metadata and the file's `metadata.json`): use and adapt
  with attribution. The file is fetched at run time, never stored in this tree.
- Training (Zenodo `metadata.json`): openly licensed French material from HTR-United,
  17th to 21st century, every French repository mixed evenly. Validation character
  accuracy in the file: 91.2% after 14 epochs.
- Family: kraken 4.x CoreML recogniser, the same VGSL as McCATMuS
  (`[1,120,0,1 ... Lbx200 Do O1c259]`): four convolutions, three bidirectional LSTMs, CTC
  over 258 symbols (French letters with precomposed accents, some Greek, typographic
  signs).

## Install
The kraken environment the PP-OCRv6 arms use; nothing new. See `kraken-ppocrv6.md`.

## Native inference path
- kraken 7.1.1's `coreml` loader, as in `kraken-mccatmus.md`.
- `kraken-mcfondue-blla`: `kraken -a -i <page> <raw>.xml segment -bl ocr -m ManuMcFondue.mlmodel`
- `kraken-mcfondue-surya`: `kraken -i 0001.png 0001.txt ... ocr -s -m ManuMcFondue.mlmodel`

## Prompt
none: CTC

## Preprocessing
120 px high, one channel (grey), any width, 16 px padding, from the file's metadata.
Line sources as in `kraken-ppocrv6.md`.

## Decoding
Greedy CTC, kraken's default; no language model, no beam.

## Output and normalisation
- Same files and units as the PP-OCRv6 arms.
- The model writes NFC (its `hyper_params.normalization`; seen here: `à` came back
  precomposed). `harness.plain` applies NFC anyway, and the scorer's graphemic-v1 profile
  runs NFC, so nothing changes.

## Resources
CPU, `--threads N`. Measured on the Intel Mac (i9-9980HK, 2 threads, torch 2.2.2):
`ocr -s` on two synthetic lines 27 s with start-up. The blla arm costs what
`kraken-mccatmus-blla` costs (same segmenter, same size of model). VRAM: not measured.

## Known failure modes
- On clean synthetic printed lines it misread more than McCATMuS did (`Pait`, `vuit`,
  `fevrier`): it is a handwriting model; printed text is not its ground.
- Zero-shot on these registers; the training mix is French but not civil-register heavy.
- The blla arm inherits blla's over-segmentation; a missed line is lost.

## What differs from our vLLM adapter
no vLLM adapter exists

## Untested here
Real register pages; the blla arm with this model (the same command ran with McCATMuS);
the pinned torch 2.14.0 with this file (no Intel-Mac wheel exists); the GPU path. First
on the pod or the arm64 Mac:
`python -m operations.bakeoff.lines.kraken_ppocr fetch --model mcfondue --store-root STORE`
then a `--limit 2` run.

## Our arm
- Module `operations.bakeoff.lines.kraken_ppocr --model mcfondue`; arms
  `kraken-mcfondue-blla`, `kraken-mcfondue-surya`; weights in `<store>/hf/kraken-mcfondue-v4/`.

## Sources
- https://zenodo.org/api/records/10886224 and its `metadata.json` (read 2026-10-08)
- the file's own CoreML metadata, read with coremltools 9.0
- kraken 7.1.1 wheel from PyPI: `kraken/models/loaders.py`, `kraken/models/_coreml.py`
