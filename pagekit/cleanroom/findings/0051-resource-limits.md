# Finding: resource limits must queue or report, never quietly shrink a page

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

Large captures, rotated bounds, padding, several pages rendered at once, verification decodes and caches can exhaust memory or disk, especially on an older laptop. A tool under pressure might be tempted to reduce resolution or skip verification to finish.

## Pagekit's observed behaviour

Already: pages are never reduced unless the person asks; detectors work on reduced working copies while the page is made from the original; a full-size scan takes a few seconds.

Differently: the reports read describe no preflight of memory or disk and no resource outcome.

Not yet: a preflight estimate covering input and output dimensions, frames and modes, rotated bounds, padding and scale, the actual in-memory storage of the image library, concurrent work, the verification decode, caches and temporary disk; an aggregate work budget with bounded concurrency; a resource outcome that queues, evicts caches or stops with a named reason.

## General technique

Admission control: estimate each job's peak need before starting, reserve it from a shared budget, and start it only when the budget allows; otherwise queue it. Estimates use the image library's real storage per pixel (multi-band images are often stored with padding per pixel), not the compressed file size or a packed byte count. Plan buffer lifetimes so the source, the render target and the verification decode are not all held longer than needed, and measure the real peak to correct the estimates. A job that cannot fit returns a resource outcome; making a smaller page is a separate, explicit candidate.
Source: general knowledge

## Settings in general terms

- The work budget: should depend on the machine's measured available memory and disk, not on its model year.
- Concurrency: start bounded and raise only on measurement.
- Per-pixel cost: measured for the image library and modes actually used.
