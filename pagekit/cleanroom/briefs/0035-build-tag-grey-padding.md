# Brief 0035: orientation tag, grey main page, padding (spec 0007)

- Role: build side.
- Issued by the host session at 2026-10-04T16:12:41Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  6801b6788c3e905fb22feed54a4dccc7eb2bb87a43dd0754ad59e99784d6fd45
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Fifth follow-up to brief 0023, same worktree (/home/user/verbatus-worktrees/pk-integrate, branch work/pk-integrate) and rules (clean-room rule, allowed paths everything under pagekit/, checks, commit trailers, stop conditions unchanged). First merge work/pagekit-prepare.

Build spec 0007 (pagekit/cleanroom/specs/0007-orientation-tag-grey-page-padding.md): read it in full, with specs 0002, 0005 and 0006 for context. It adds, in this order:
1. The orientation tag applied exactly once, as the first link of the geometry chain, for all eight values, with a trust setting per source, an invalid value flagged, no tag written on output, and the point maps covering it.
2. An output mode per run and per page, `source` (default) or `grey`: a plain named conversion through the same chain; exact when every decoded pixel has equal channels (take the common channel) and recorded as exact, otherwise recorded as reviewed; a colour-evidence check against the page's own paper chroma noise that flags a grey choice on a page holding real colour and keeps that page in source mode unless grey was set by hand or locked; a batch choice scoped to the sources of that run; the review sheet showing the rule, exactness, the flag and override lines.
3. Padding as a separate setting from the margin (millimetres or pixels, default none), filled with the measured paper colour, enlarging the canvas without changing scale, covered by the point maps, with the manifest recording margin box, padding and which output regions are source and which are fill.
4. A nominal density that would change the axis ratio is refused.

Defaults must not change what a reader receives: with no tag, no grey choice and no padding, prepared pages and manifest values are identical to before apart from new recording fields; add a test that pins this against a page made by the current code. For every behaviour in the spec's test list, add a failing synthetic test first, then the code. All settings you add are UNMEASURED and go in a thresholds file. Credit any published method you use in NOTICE (the Exif standard is a specification, not a method; name it in the README). Run the whole pagekit suite under the check script's settings (env -u PYTHONPATH PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1) and check-static. Report the head and what each test proved.
