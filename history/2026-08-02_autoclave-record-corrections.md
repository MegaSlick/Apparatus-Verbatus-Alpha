# Corrections to the autoclave record, 2026-08-02

Review found false mechanisms stated in commit messages and in two dated records on
this branch. The branch was already published, so hard rule 5 forbids rewriting those
messages: nothing here is amended in place. This is the chronological correction, and
it is evidence rather than instruction like everything else in this directory.

Each claim below was checked against the commit message and against the code that
commit left behind, not against a later summary of either.

- **`54798e8` and `967fd2a`** say every autoclave chamber and every CI run clones this
  repository under `/tmp`. Neither does. A chamber clones to `/work`; GitHub Actions
  checks out under `/home/runner/work`. What sat under `/tmp` was pytest's own Linux
  fixture root. The deletion defect those commits fixed was real and the fix stands —
  the account of how it was reached was wrong. `9c3d60a` and the guard's current
  comments record the corrected paths.
- **`a811754` is not in error, and a first draft of this record said it was.** That
  commit says Claude's OAuth callback goes to `platform.claude.com` and that the
  sign-in can therefore be completed from a phone. The correction drafted against it
  said the flow used a callback at `http://localhost:1455` and needed a keyboard —
  taken from a comment in `autoclave.sh` that asserted as much and had never been
  measured. Measured on 2026-08-03 against the Claude CLI in the image: `claude auth
  login` prints an authorize URL whose `redirect_uri` is
  `https://platform.claude.com/oauth/code/callback`, stands up no local callback
  server, and then waits at `Paste code here if prompted >`. The commit was right and
  the comment was wrong; the comment now records the measurement. This bullet is kept
  rather than deleted because a correction that turned out to be the error is exactly
  the kind of thing this file exists to hold.
- **`26711aa`** says `doctor` reports whether each vendor is signed in. At that revision
  it reported whether a named volume existed, which is not proof that an interrupted or
  failed sign-in ever completed: `cmd_doctor` called `has_volume` and nothing else.
- **`50e763e`** says the brief never becomes a shell argument. At that revision the
  brief crossed the host/container boundary as a file and was then expanded, quoted,
  into one CLI argument inside the chamber through `$(cat /out/brief.md)`. The quoting
  made its punctuation inert; it did not make the claim true.
- **`history/2026-08-01_repository-audit.md`** repeats the `/tmp` explanation from
  `967fd2a`. The first correction above applies to that dated record too.
- **`history/2026-08-01_agent-rework-proposal.md`** lists `Explore` and `Plan` among the
  roles that are already read-only. Neither was: both hold `Bash`, and a shell writes
  whatever a shell writes. `881b6fe` and the current agent roster record the
  correction.
