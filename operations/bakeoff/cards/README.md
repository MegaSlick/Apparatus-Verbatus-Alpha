# Run cards

One card per bake-off model: identity and pin, install, the vendor's own inference path,
prompt, preprocessing, decoding, output, resources, failure modes, how our arm differs,
and what is untested. Settings and model names only.

| Card | Model |
|---|---|
| `qwen3.8-27b.md` | Qwen3.8-27B, the incumbent reader (Perlector); arms `qwen-blind`, `qwen-vendor` |
| `qwen3.5-27b.md` | Qwen3.5-27B reader candidate; arms `qwen-blind`, `qwen-vendor` |
| `qwen3.5-9b.md` | Qwen3.5-9B reader candidate; arms `qwen-blind`, `qwen-vendor` |
| `chandra.md` | Chandra OCR 2 (written by the native-arms work) |
| `churro.md` | Churro-3B (written by the native-arms work) |
| `dots-mocr.md` | dots.mocr (written by the native-arms work) |
| `kraken-ppocrv6.md` | kraken PP-OCRv6 line recogniser (written by the native-arms work) |
| `pylaia-belfort.md` | PyLaia, Belfort model (written by the native-arms work) |
| `pylaia-popp.md` | PyLaia, POPP model (written by the native-arms work) |
| `party.md` | Party line recogniser (written by the native-arms work) |
| `surya-recogniser.md` | Surya recogniser (written by the native-arms work) |

## The readers' Phase R baseline

Phase R should take the **vendor** arm (`qwen-vendor`) as each reader's baseline: its
sampling is the card's non-thinking row, which is also the Perlector's sealed row in
`config/decoding.toml`, so Phase R measures changes against what the pipeline sends, not
against greedy decoding. The plain arm (`qwen-blind`, greedy, run 2026-10-08) stays as the
control that shows what sampling and the presence penalty cost or gain on verbatim text.
