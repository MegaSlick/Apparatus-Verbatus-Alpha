# Brief 0011a: add the section on outside reading-side sessions

- Role: build side.
- Issued by the host session to the Claude Opus 5.5 build-side agent of entry 0002 at
  2026-10-02T18:21:33Z.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  5f580f51a90ff47717ea4e4151d89d587baa1c27d383d96f36501a93684d5b80
- Saved by the host after the agent finished, copied word for word from the session
  transcript on 2026-10-02.

----- brief below this line, exactly as sent -----
One more build-side doc task on review/12-pagekit (now at a520483d after my hook fix — pull nothing, just work on HEAD). Do not edit LOG.md. Add to CLEANROOM.md a section "Outside reading-side sessions", from the lead's direction: the lead may run a reading-side session in a separate chat (for example a Claude chat) and bring back a document. Rules: before starting, the lead gives the chat the reader-brief template's rules; the lead keeps the chat so its transcript can be checked; the returned document goes straight into the local git-ignored quarantine folder, unread; the host records its arrival time, size and sha256 in the log; the host runs check_report on it before anyone reads it; if it fails (working notes usually will), a reading-side agent distils it into finding reports in quarantine, each checked before the host opens it; the host reads and accepts passing reports into findings/ with their sha256 in the log. Brainstorm chats that never read the other program's code are not reading-side sessions and their ideas may come in directly, but anything that mentions the other program's internals goes through the same path. Point to LOG entries 0006–0008 as the first worked example. Commit with trailer `Clean-room: build` plus the usual two; run pagekit tests and check-static; report the hash. This message is a brief; I will save it.