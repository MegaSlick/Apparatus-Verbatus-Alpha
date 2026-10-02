# Finding: missing, wrong or unequal resolution metadata

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Physical measurements (margins in millimetres, overlap at the fold, the size of a speck) need the scan resolution. Files may carry no resolution, a default placeholder, an implausibly low value that would imply a page several metres long, or different values for the two axes. The document records that its reference tool validates each axis, refuses implausible values through an interactive dialog, resamples unequal axes to the smaller one before any analysis, and lets a stored override always win over what the file says.

## Pagekit's observed behaviour

Pagekit reads pixel size and resolution metadata and flags a page whose resolution is missing or whose short side is below a minimum. It does not check whether a recorded value is plausible for the pixel size, does not handle unequal axes, and offers no override. Those parts are not built yet.

## General technique

Treat resolution as declared input with a policy. Read it from the file; check each axis against a plausible range and against the physical size it implies for the pixel dimensions; apply a per-image or per-series override from the project when the file is wrong or silent; and record which source each value came from. If the axes differ, analyse in a resampled frame with equal axes, but keep the transform so measurements map back to the original. A missing or implausible value with no override is a reported failure, never a silent default.
Source: general knowledge

## Settings in general terms

The plausible range depends on the capture devices used for the collection. The largest plausible physical page size depends on the largest volumes in the series. The override map belongs in project configuration, not in user settings, so runs are reproducible.
