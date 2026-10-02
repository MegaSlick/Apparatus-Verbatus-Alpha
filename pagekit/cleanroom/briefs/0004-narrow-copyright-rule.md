# Brief 0004: narrow the leak scan's copyright rule (the false match)

- Role: build side.
- Issued by the host session to the Claude Opus 5.5 build-side agent of entry 0002 at
  2026-10-02T17:08:50Z.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  1b8639a0d5a68880b113f905ddce0ef97fef860a847c46d705033a62d3b9a9ee
- Saved by the host after the agent finished, copied word for word from the session
  transcript on 2026-10-02.

----- brief below this line, exactly as sent -----
Your gate worked and caught a false flag. I staged pagekit/cleanroom/briefs/0002a-build-cleanroom-task.md and 0002b-build-cleanroom-decisions.md plus the LOG.md entry 0002 brief fill-in (uncommitted in the worktree — leave those three files' content as they are; the briefs must stay byte-for-byte). The commit was refused: foreign_copyright hit line 24 of 0002a, which is prose describing the rule ("GPL/ScanTailor copyright or licence headers"), not a copyright notice. Fix the rule, not the brief: make foreign_copyright match an actual notice (e.g. "Copyright" followed by (C), © or a year, then within the line a name of the projects or their authors), with tests that rule-describing prose passes and real-looking notices (several formats) are caught. Add to LOG.md (as part of entry 0002's record or a short new entry 0004, your call) a plain-language note: the gate refused the host's commit of brief 0002a on a false match; nothing from ScanTailor was involved; the rule was narrowed in <commit>. Then commit your fix, and then commit the three staged files with this message body: "Save brief 0002 and fill its log entry" + trailers (Clean-room: build, Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>, Claude-Session: https://claude.ai/code/session_01JQGcQfwD6ES3eJWJfmkYE5). Run pagekit + .githooks tests and check-static; report hashes.