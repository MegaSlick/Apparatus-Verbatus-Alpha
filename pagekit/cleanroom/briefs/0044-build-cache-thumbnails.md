# Brief 0044: cache keeps thumbnails by default

- Role: build side.
- Issued by the host session at 2026-10-04T18:50:57Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  c91bdde1beca189bf8599ac5f180a23e68f1d9f87ac7edc11dd567ac02c69eff
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Tenth follow-up to brief 0023, same worktree and rules; do it after brief 0043, in the same run if convenient. The lead has decided the cache's default: pagekit keeps the settings for each page in the project file, and the real images are made only once, at output; the cache is for looking at each step. So by default the stage cache writes only small thumbnails of each step (the opened source, the upright frame with the cut, each page's side and levelled page, and the boxes when cropping is on), linked from the review sheet. Full-resolution lossless step images are written only when asked, with a new option `--cache-full`. Keep everything else about the cache as spec 0008 and brief 0043 say (keys, reuse, deletable, never used for output, never in the source folder, size printed). Tests first: a default run's cache holds thumbnails only and is small (state a bound per source); `--cache-full` writes the full-resolution entries as now; outputs are byte-identical either way. Update the README. Report the head.
