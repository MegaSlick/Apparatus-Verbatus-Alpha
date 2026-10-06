# Brief 0024: follow-up fixes to the grey tone view

- Role: build side.
- Issued by the host session at 2026-10-04T13:31:03Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  37b02eaad4245533bdbd3faf484c36b444a1fc1d8d58108467110ec27b9c4431
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Follow-up to brief 0020, same worktree (work/pk-tone), same rules (clean-room rule, allowed paths, checks, commit trailers, stop conditions all unchanged). An independent build-side reviewer, who saw no GPL source, found the view gentle, fast and pixel-deterministic, but three problems block merging. For each, add a failing test first, then fix. Report the head and what each test proved.

T1. Paper clips to pure white and faint strokes are lost (tone.py around 264, 316-317, 357). The box blur of the background estimate dips just outside a sharp stain edge, so the paper there is brightened to 247-255. At a 0.6 stain on a lit page, 5-level strokes 8 px outside the edge fell from 3.5 to 1.0 levels of contrast, and 272 of 408 stroke pixels ended no darker than their paper. On an 80 %-ink page the estimate under-reads the paper and 20 % of all pixels end at 255. Fix: take the lighter of the blurred estimate and the unblurred estimate (the reviewer tried this: stain-edge loss gone, spec tests still pass); and leave headroom so flattened values above the paper level map strictly upward into the range between the paper level and 255 rather than clipping. Tests: faint strokes just outside a sharp stain edge on the shadow side; an 80 %-ink page with faint strokes in the paper gaps.
T2. The written TIFF is not byte-identical on repeat (tone.py around 497-509). Pixels are identical, but when the compressed data ends on an odd byte the writer leaves an uninitialised pad byte before the image directory; 6 of 12 random pages gave several different files over 6 runs. Fix: make the bytes deterministic (for example zero the pad after saving, or another lossless route that is deterministic), add a sha256 of the decoded pixels to the record, and add a test on a page whose compressed data ends on an odd byte, run several times. Check the prepare output writer for the same problem and fix it there too if present, with a test.
T3. `--out` can be the same file as `--in` (__main__.py around 56, tone.py around 508): `tone --in p.tif --out p.tif` replaces the page with the view. Refuse when the two resolve to the same file, and refuse an existing output unless the caller passes a force option.

Also, smaller:
- Large dark areas (a 400 px seal at 60, a blot at 40) are lifted and gain a ring, and marks inside double in contrast; no test covers the background floor (removing it left all tests passing). Add a test on a seal and a blot, and keep their darkness.
- The background window and blur are in pixels, so their size in millimetres changes with dpi. Scale them by dpi when it is known, record what was used, and fall back to the pixel setting with a note when dpi is unknown.
- The minimum-channel and blue-channel grey rules make faint blue-black ink on yellow paper lighter than the paper. Say so where the setting is described and in the record's rule description; keep luminance as the default.
- Record the Pillow version and a digest of pagekit's tone code so a record is reproducible.
- Strong background edges (stain rims): count them or give their share in the record, so a later consumer can mask those bands. Do not refuse such pages.
- Test gaps, each of these mutations left all tests passing: no background floor; no blur; the closing replaced by a plain maximum filter; a window half the size; the blur tripled. The halo test skips 60 px either side of each stain edge and checks only middle rows; tighten it. Add tests that fail under each mutation.
