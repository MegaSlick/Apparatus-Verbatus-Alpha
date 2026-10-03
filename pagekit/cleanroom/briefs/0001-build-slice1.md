# Brief 0001: build pagekit slice 1 (the crop check)

- Role: build side.
- Issued by the host session to a Claude Opus 5.5 build-side agent at
  2026-10-02T16:36:21Z.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  c0e21f2dc553987cce059de6f4c4e90c0e54f10b9a8523880a0cb71f65c51754
- This brief was not saved when it was issued. It was recovered word for word from the
  session transcript on 2026-10-02 and saved here afterwards.
- Slice 1 had no written spec before its code and no reading-side input. Its spec,
  `specs/0001-crop-check.md`, was written after the code.

----- brief below this line, exactly as sent -----
Build slice 1 of `pagekit`, a new page-preparation tool, in the worktree /home/user/rv/pagekit on branch review/12-pagekit (from origin/main; .venv symlinked). Commit there in focused commits; NEVER push or touch other branches/worktrees. No network.

CLEAN ROOM (mandatory): you must NOT read, search for, fetch or reproduce ScanTailor or ScanTailor Advanced source code, and must not look at any GPL page-processing code. Work only from the spec below, general image-processing knowledge and published methods. Pagekit is Apache-2.0 (the repo's licence).

Background (from research already done): the repo can apply declared crops/splits/rotations deterministically with provenance (pipeline/0_triage manifest, common/imaging.py, pipeline/1_exemplar/door.py), but nothing checks whether a crop threw away ink: the ink map only sees the already-cropped page, so text cut off cleanly leaves no trace. Roughly 1 page in 25 from the lead's current tool has a crop that cuts text. Slice 1 is the checker for that.

Design:
- New top-level folder `pagekit/` that imports NOTHING from pipeline/, common/ or operations/ (it will become its own repository at beta). Dependencies: Pillow only (already pinned in the repo; numpy is NOT installed locally, so do not use it). Use Pillow's C-level operations (convert, point, histogram, getbbox, crop, resize with BOX for row/column profiles) so large scans are fast.
- Files: `pagekit/__init__.py`, `pagekit/check.py` (the checker), `pagekit/__main__.py` (CLI: `python -m pagekit check --master PATH --crop x0,y0,x1,y1 [--crop ...] [--split-x N] --json`), `pagekit/README.md` (what it does, what it doesn't yet, the airlock rule below, credit), `pagekit/NOTICE` ("Workflow inspired by ScanTailor and ScanTailor Advanced (GPL-3.0) — thank you to their developers; no code from them is used." with project links), `pagekit/pyproject.toml` (name pagekit, Apache-2.0, dependency Pillow — for the later split; it must not change the root project or uv.lock), tests in `pagekit/test_*.py`.
- Checks, each producing measurements plus a flag; every flag means "send to review", never auto-fix:
  1. ink discarded: estimate the page background and ink threshold from the master (e.g. Otsu on the greyscale histogram, cite Otsu 1979), measure ink pixels outside the union of crop boxes but inside the physical page area, as a share of all ink and as absolute pixels; also report the bounding box of discarded ink.
  2. ink cut at the edge: ink density in a thin band just inside vs just outside each crop edge; ink continuing across an edge means writing was cut.
  3. split vs gutter (when --split-x given or two crops given side by side): column darkness profile; find the gutter (lowest-ink valley near the middle) and report the split's distance from it.
  4. resolution: report pixel size and DPI metadata if present; flag missing DPI or a short side below a configurable minimum.
  Thresholds live in one small config dict/TOML in pagekit with every value marked UNMEASURED (not calibrated on real pages), and the report says so.
- Output: a closed, versioned JSON report (`pagekit-crop-check.v1`) with inputs (file digest sha256, size), each crop, measurements, flags with plain-language reasons, and `thresholds_measured: false`. Deterministic: same input → byte-identical JSON.
- Originals are only read, never written.
- Tests with SYNTHETIC images built in the test (never real register material): clean page with crop inside margins (no flag); margin note clipped by crop (flag 1); text line cut by crop edge (flag 2); two-page spread with split off the gutter (flag 3) and on it (no flag); missing DPI (flag 4); determinism (two runs identical); CLI JSON round trip.
- Make sure CI collects the tests: read .githooks/check-all.sh and pyproject's pytest config and confirm `pagekit/test_*.py` is collected by the normal suite; if not, make the smallest change and name it.
- README section "Airlock" (exact intent): build-side agents never read ScanTailor source; later, a separate reader agent may compare ScanTailor's behaviour with ours and report only plain-language findings (situation, general technique, settings) — never code or "copy this"; the build side works from those reports only; credit in NOTICE.

Checks: `.venv/bin/python -m pytest -q -p xdist -n 2 pagekit` and `sh .githooks/check-static.sh` (write output to files, check exit codes). Never skip commit hooks. Commit messages end with:
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JQGcQfwD6ES3eJWJfmkYE5

Report concisely: commits, the report schema, what each check measures and its limits, test exit codes, anything you could not do.