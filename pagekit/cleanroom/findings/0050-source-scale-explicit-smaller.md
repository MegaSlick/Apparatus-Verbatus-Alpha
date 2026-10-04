# Finding: keep source scale by default; a smaller page is a separate, explicit candidate

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

Each downstream reader resizes pages to its own budget. If the preparation tool reduces pages itself, by default or in response to memory pressure, detail is lost for every later use. Sometimes a person does want a smaller derivative, for a specific purpose.

## Pagekit's observed behaviour

Already: pages keep the source resolution; they are never enlarged; they are shrunk only when a maximum resolution is set explicitly; shrinking uses area averaging in one resampling from the original.

Differently: the shrink setting is a run-wide maximum density rather than a per-page candidate with a pixel target and an aspect policy.

Not yet: a smaller page recorded as its own candidate beside the full-scale one, with its pixel target and aspect rule; display of pixel dimensions and scale as the main resolution controls.

## General technique

Keep a full-scale master and create any reduction as a separate derivative from the original, with a proper anti-aliasing filter matched to the reduction factor (area averaging or a prefiltered kernel). The reduction is named by its target pixel size and by how aspect is kept, and is recorded with its parent. The master is never replaced.
Source: Wolberg, Digital Image Warping, IEEE Computer Society Press, 1990

## Settings in general terms

- Default scale: one, in the decoded source grid.
- A reduction target: set by the person in pixels, with uniform scale on both axes unless an explicit aspect decision says otherwise.
- The reduction filter: should depend on the reduction factor.
