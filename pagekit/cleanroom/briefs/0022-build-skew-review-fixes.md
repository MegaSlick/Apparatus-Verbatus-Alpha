# Brief 0022: fix the review findings on skew and the boxes (follow-up to brief 0018)

- Role: build side.
- Issued by the host session at 2026-10-04T13:01:58Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  7338768933221b2ff8cb114702a00f249b367647d53d73b085efffa93a807bcd
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Follow-up to brief 0018, same worktree, branch and rules (clean-room rule, allowed paths, checks, commit trailers, stop conditions all unchanged). An independent build-side reviewer, who saw no GPL source, found that skew is accurate, but that in four ways legible writing ends up outside the page box or the content box with confidence 0.9 and no flag. Losing writing silently is the worst failure this tool can have: when in doubt, keep the ink or flag. For each case, add a failing test first, then fix. Report the head and what each test proved.

B1. Faint ink is dropped when the page also has dark ink (content.py around 165). Paper 228, main text 35, 300 dpi: a marginal note at 150 and a signature at 140 fall outside the box with no flag, because the threshold is min(Otsu split, 255 - blank_contrast) and Otsu sits halfway between dark ink and paper. A page written only in faint ink (paper 210, ink 180) comes back blank at 0.9 with no flag. Fix: use two levels. A strict level seeds; a lenient level near the blank-contrast level, measured against the local, flattened paper, keeps every component above speck size. Flag components between the two levels. A blank answer gets low confidence when low-contrast structure is present.
B2. The page box cuts paper with writing on it (pagebox.py around 140-150 and the walk around 172). With paper brightening from 185 to 225 across the page, the box is cut 33 mm in from the bright side while writing reaches 10 mm from the edge (55 mm with 180 to 250). A 40 mm gutter shadow over writing moves the left edge from 234 to 324. Fix: judge pale backdrop against local paper, or require it to be uniform, not the paper median plus 12. After the walk, look for ink above speck size in each cut strip against a local background; where there is any, do not cut that side, or flag it.
B3. Writing inside a dark area touching the border is removed and never reported (content.py around 201 and 206). A border component holding a solid seed is subtracted whole, including writing joined to it, such as a water stain or gutter shadow over text, and none of it is counted as discarded. Fix: subtract only the seed region, or the flattened non-ink, not the whole component. Count any flattened ink under the border mask as discarded and flag it.
B4. A small mark touching the paper edge is dropped without a flag at low dpi (content.py around 215 and 461). A 3.5 mm cross 0.3 to 0.7 mm from the edge at 150 dpi is treated as debris and excluded. Its 158 source pixels are under the crop check's 200-pixel threshold, so no flag. Fix: flag any excluded edge component above a mark size in mm squared (about the size of that cross), whatever the dpi.

Also do these, smaller:
- Skew trusts pure noise: random pixels give ±0.1-0.2 degrees at confidence 1.0, "4 regions agree" (skew.py around 237 and 295). Score away from the ends of the inked area, or compare against a shuffled baseline, so noise is not trusted.
- When the two skew estimates disagree (rules at a different angle from level writing), return 0 with the flag, not the rules' angle.
- A wildly implausible resolution (for example 1e6 dpi) makes a full page come back blank. Sanity-check the implied paper size, and flag it.
- Uniform noise at 150 dpi takes 20 s for skew and 12 s for the content box. Cap the component count, or exit early on noise, with a flag.
- Test gaps, each of these mutations left all tests passing: removing the line-fit disagreement flag; turning off pale-backdrop detection (pagebox.py around 148); setting the edge-run walk to 1; removing shape-based rule removal in skew. Add tests that fail under each.
