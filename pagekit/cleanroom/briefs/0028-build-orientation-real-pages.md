# Brief 0028: orientation on real spreads

- Role: build side.
- Issued by the host session at 2026-10-04T13:54:40Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  6562ca00a4b274ea09f216f5b8e66b06737daf2eb563ba156639991cc941b3ab
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Follow-up to brief 0021, same worktree (work/pk-split), same rules (clean-room rule, allowed paths, checks, commit trailers, stop conditions all unchanged). Your slice is merged into work/pagekit-prepare. The host ran prepare on the lead's two real register spreads (private: never copy them into any repository or send them anywhere; they are at /tmp/claude-0/-home-user-Apparatus-Verbatus-Alpha/0c707ed1-b407-5f91-9dee-17af7164195f/scratchpad/pack/images/01_original_spreads/, and the run is at /tmp/claude-0/-home-user-Apparatus-Verbatus-Alpha/0c707ed1-b407-5f91-9dee-17af7164195f/scratchpad/pkrun2/ with review.html and pagekit-prepare.json). You may run pagekit on them locally to check your work, but every test must use synthetic pages built in the test that reproduce what you see.

Both splits were right and every page was upright, but orientation flagged all four pages as uncertain:
- 018dc88c3b3f47d308d2 (both pages): "whether the lines run across or down is too close to call".
- 2adc37ec376f362ad102 (both pages): "whether the page is upright or upside down is too weak".
These are ordinary upright pages of dense 17th- and 18th-century French cursive with long ascenders and descenders, flourished signatures, show-through from the other side, a dark gutter and a page-edge stack. A flag on every page makes the flag meaningless, so find out why each cue is weak on these pages and strengthen it, with a failing synthetic test first for each change. Ideas to test, not to assume: measure line direction only inside the writing, away from the gutter, page-edge lines and show-through; use a working resolution that keeps line gaps for small cursive; use the periodicity of the row profile, not only its sharpness; for upright against upside down, combine several cues (left-aligned line starts, ascender against descender mass, the position of the first and last line) weighted by their measured strength. Keep the rule: when genuinely unsure, 0 turns and a flag. Synthetic tests: a dense cursive page with long flourishes, show-through and a dark gutter strip, upright and in each of the other three turns, gives the right turn with no flag; a grid of dots still gives the uncertain flag. Report the margins you now see on the two real spreads (without copying any image).

Also: the split overhang flag on 018dc88c3b3f47d308d2 says "a mark overhangs it by 6.6 mm, more than the 3 mm overlap". Check what that mark is (a flourish across the gutter, or the gutter shadow itself). If it is the shadow, it should not count as writing across the cut.
