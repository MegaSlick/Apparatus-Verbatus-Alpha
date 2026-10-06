# Brief 0026: merge the tone view and deterministic outputs

- Role: build side.
- Issued by the host session at 2026-10-04T13:54:40Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  1e137aaab7c94de06c05355ebe92a315105608555da86b65c2891b732cceddb5
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Follow-up to brief 0023, same worktree (/home/user/verbatus-worktrees/pk-integrate, branch work/pk-integrate), same rules (clean-room rule, allowed paths now including everything under pagekit/, checks, commit trailers, stop conditions all unchanged). The host has merged your work and later slices into work/pagekit-prepare (now 34d5d86), and the tone view is finished on work/pk-tone (2131539). Do these in order; add a failing test first for each behaviour change; report the head and what each test proved.

I1. Merge work/pagekit-prepare into your branch, then merge work/pk-tone. Two files conflict: pagekit/NOTICE (keep every slice's citation section word for word) and pagekit/__main__.py (keep both the prepare/measure commands and the tone command with its new --force option). Run the full pagekit suite after each merge.
I2. Connect the hook: tone.py's entry point is `tone(image, overrides=None) -> (view, record)`, not `tone_view`. Make `make_tone_view` call it, and write each tone view with tone.py's own deterministic writer (`write_view` / `tiff_bytes`), refusing to write over a prepared page or a source. Test: `prepare --tone-view` on a synthetic batch writes one view beside each page, records its settings, and gives identical bytes on repeat.
I3. Deterministic prepared TIFFs: a reviewer found that Pillow's TIFF writer can leave an uninitialised pad byte before the image directory when the compressed data ends on an odd byte, so files differ between runs although pixels are identical. Check pagekit's prepare output writer for this; if affected, use the same deterministic writer for prepared pages (keep lossless deflate, the source colour mode, and the resolution tags; write no resolution when the source has none). Test: a page whose compressed strip ends on an odd byte, written in two separate processes, gives identical bytes. Add a sha256 of the decoded pixels to each manifest entry.
I4. A source with no resolution: the lead's sample JPEGs include one with none, so every millimetre setting became 0 px. Add a `--dpi` option (applies to sources that carry none, recorded with origin "override"), and say in the flag text how to set it. Test it.

A pipeline review of your earlier work is still running; its findings will come in a later brief, so do not start other changes.
