# Brief 0019: fix the review findings on the preparation core (follow-up to brief 0016)

- Role: build side.
- Issued by the host session at 2026-10-04T12:47:57Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  596b9e0ecab7d2521d58d281cad149636bf1b737e5a112620d585eda7ce34344
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Follow-up to brief 0016, same worktree, branch and rules (clean-room rule, allowed paths, checks, commit trailers, stop conditions all unchanged). An independent build-side reviewer, who saw no GPL source, found these problems in your slice. For each, write a failing test first, then fix. Report the head and what each test proved.

1. Pure black line on the right and bottom edge of default pages (geometry.py around 405-409). Unrotated pages whose crop runs past the source get 0 fill from the crop, and the polygon mask drawn on vertices such as (W, H) includes column W and row H, so a black line survives. With overlap 0 (missing resolution), the same off-by-one plus a half-pixel shift lets about 1 px of the neighbour through along a cut. Fix: paste the in-bounds region onto a paper-coloured canvas before masking, and draw the mask in the continuous pixel convention (vertices shifted by -0.5, far edges excluded, or supersample). Strengthen test_area_outside_the_source_is_paper_colour to assert the whole outside area is paper colour, not two pixels.
2. Exit 2 can leave partial output (output.py around 77-101). Render and encode every page, the manifest and the project to temporary files first; rename only when all succeed; delete the temporaries on any failure. Test: an output path that cannot be replaced (a directory) leaves no new or changed file, and an earlier run's good files are untouched.
3. Re-applying the same overrides file re-stamps a correction as fresh (prepare.py around 321-328). Rule: when an override's value and lock equal the stored manual or locked record, keep the stored record with its original inputs, so it is flagged if what it was set on has moved. Re-stamp only when the value or the lock changes. Test the spec's example: a moved cut in the overrides file flags page 2's unchanged manual skew.
4. Hand-set values of a dropped page are lost (prepare.py around 485-498). Never discard a manual or locked record: keep the records of pages that no longer exist in the project, flag them on every run until a person clears them (say how in the README), and restore them if the page comes back. List left-over output files of dropped pages in the manifest as stale, and do not delete them.
5. Test gaps, each currently survivable by a mutation: a leaning cut with non-zero skew must keep the neighbour's wedge out of the rotated page; a resolution override must carry forward from the old project; resolution must be in the page-step inputs hash; the dropped-pages flag; unknown keys in the overrides file; the source-changed-while-running check; the overlap axis swap for odd quarter turns.
6. Optional, do if cheap: validate a stored resolution in validate_project (reuse the same checks); chmod outputs to 0644 before rename.
