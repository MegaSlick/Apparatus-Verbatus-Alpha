# RunPod model-manifest pins

Evidence, never instructions.

On 2026-09-15, the authorized RunPod materialization downloaded the exact revisions
named below and built canonical digest manifests. The manifest bytes were hashed again
after retrieval and matched the materializer's download record. The committed
`config/manifests/` artifacts contain only sorted path, SHA-256, and size metadata; no
model or vendor file bytes are carried here.

| Repository at revision | Manifest | SHA-256 |
| --- | --- | --- |
| `datalab-to/chandra-ocr-2@af93b47dba1b47b6640c86ccf487ed2260ab9a09` | `chandra-ocr-2.json` | `6bfe1e4192a251e1f1aced58e720f6ce9c5b915d12f766b09a79e22433e79230` |
| `Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR@e371095d4ffe585f31f4974462931ddbac61ff64` | `dai-recordgold-atr.json` | `aba984924133d6eac951dc9f9e0302ee41cf5a26e5d427537f38f8040cccc2fb` |
| `stanford-oval/churro-3B@ca2150ea465d5a3d67818c50e234b9422619c75d` | `churro-3B.json` | `a5be0ef7a842cb1b159f1793aa3072786dccef882cbaef2b8359fdd9518806c4` |
| `Qwen/Qwen3.8-27B@1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0` | `qwen3.8-27B.json` | `ace17319b4f843f3b746e6039e1c3dfc5e47b4e17bd935b030722a1d96a17f70` |

Chandra's one manifest is the pin for both `designator_structure` and
`attestator_1`. These are materialization attestations only. No inference was attempted,
and the serving profiles remain unproven.
