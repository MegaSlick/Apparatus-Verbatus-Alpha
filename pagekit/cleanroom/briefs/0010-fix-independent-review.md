# Brief 0010: fix the independent reader's findings on the pagekit branch

- Role: build side.
- Issued by the host session to the Claude Opus 5.5 build-side agent of entry 0002 at
  2026-10-02T17:33:58Z (the independent review round).
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  df8f81b1c284c6ce1ad4f436aba2f1bbe326783fde3135c760d95536a7acf703
- Saved by the host after the agent finished, copied word for word from the session
  transcript on 2026-10-02.

----- brief below this line, exactly as sent -----
An independent reader reviewed review/12-pagekit (head now 63ff801c; I added LOG entry 0009 myself — do not edit LOG.md in this round; I own record entries). Fix these as build-side work, commit, don't push. Same clean-room rule for you as before.

Slice 1:
1. MEDIUM: when ink contrast is under the threshold (ink not detected), the verdict is no_flags. Add a review flag "no ink detected; the check could not run" — never no_flags when ink detection is off.
2. MEDIUM: the 3x3 median despeckle runs before the discarded-ink count, so thin one-pixel pen lines outside the crop go uncounted. Count discarded ink on the map before despeckling (or remove only isolated specks with no neighbours); test with thin lines outside the crop.
3. LOW: __main__ catches only CheckError/OSError; any other exception from reading the image must exit 2 ("cannot check"), not 1. Hash and decode the same bytes (open from BytesIO of the bytes you hashed). Keep the I;16 refusal loud (exit 2) and say so in the README.
Gate and CI:
4. MEDIUM: a hook skipped with --no-verify can delete HOLD and its undecided incident note without CI noticing. Make pagekit/cleanroom/incidents/ append-only against origin/main (like LOG), and in CI replay the gate's HOLD rule over every commit in origin/main..HEAD. Tests for both.
5. CLEANROOM: say the gate can check a Decision line's form, not that the lead wrote it.
Report check / scan:
6. MEDIUM: add cheap rules (with `Source:` lines exempt): identifier shapes (an internal capital in a word like estimateSkew, a word with an internal underscore, CONST_CASE), call/assignment shapes (word followed by "(" , "word = word", "word.word("), and the project owner's account name as scan.py has. Verify the 35 accepted findings still pass (they should; "DeMenthon" is inside a Source line). CLEANROOM must name what still passes (plain numbers, plain-English pseudocode) and say the host's reading is the real check; add one sentence that the denied-word rule does nothing until deny-hashes.txt is filled.
7. LOW: a test that every file in findings/ has its sha256 listed in LOG.md.
8. CLEANROOM "Known pre-existing item": complete the list with operations/operator/scantailor.py, test_scantailor.py, test_scantailor_pin_writes.py, operations/triage/test_scantailor_bridge.py, and say related text also appears in common/contracts/stages.py, pipeline/0_triage/manifest.py and pipeline/1_exemplar/CONTRACT.md; replace the rule "must be gone before any reading-side session" with: no build-side agent may read them, and they must be removed from main before pagekit merges (point to LOG 0009). Also define a `Clean-room: record` trailer for host record commits (briefs, log, findings), distinct from `Clean-room: build`.
.claude/settings.json:
9. MEDIUM: the Bash deny rules naming the project block ordinary work (git rm of the bridge files, test runs, commit messages). Narrow them to fetch/clone/download shapes only: commands containing curl, wget, git clone, or git fetch together with the project or owner name. Keep the honest note that patterns catch accidents.
Run pagekit + .githooks + .claude tests and check-static; exit codes from files; report hashes.