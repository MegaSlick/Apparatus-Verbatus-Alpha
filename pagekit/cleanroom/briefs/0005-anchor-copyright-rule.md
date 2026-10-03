# Brief 0005: anchor the copyright rule to header lines

- Role: build side.
- Issued by the host session to the Claude Opus 5.5 build-side agent of entry 0002 at
  2026-10-02T17:10:58Z (the second false match).
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  e3bebde3984fe6785c58f3edf2dc5414369b29163227227e858ed3cdfc8f1c37
- Saved by the host after the agent finished, copied word for word from the session
  transcript on 2026-10-02.

----- brief below this line, exactly as sent -----
Second false match, same cause: briefs describe the rules. I staged pagekit/cleanroom/briefs/0004-narrow-copyright-rule.md and a LOG.md addition to entry 0004 (a Brief line); the gate refused them because line 12 of that brief is prose that quotes your notice pattern and names the project on the same line. Leave both staged files byte-for-byte as they are.

Fix the rule, not the brief: a real notice is a header line, so anchor foreign_copyright to the START of a line, allowing only leading whitespace and an optional comment leader (#, //, /*, *, --, ;, <!--, or a docstring quote) before the notice word or sign. Prose that merely mentions a notice mid-sentence must pass. Tests: the six notice formats you already catch, each also behind each comment leader, are still caught; the two brief lines that were refused (0002a line 24, 0004 line 12) pass (read them from the files); a mid-sentence notice passes and the docs say header lines are what the rule targets. Then: commit your fix with only scan.py and test_scan.py; then commit the two staged files with message "Save brief 0004 and name it in log entry 0004" + the usual three trailers; then add a short LOG.md entry 0005 in plain words: a second false match on a brief describing the rule; the rule now targets header lines, in <commit>; nothing from the other project was involved; no incident. Run pagekit + .githooks tests and check-static; report hashes.