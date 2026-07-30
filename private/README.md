# private

**Local only. Everything here except this file is gitignored.**

Community-specific and personal material: lexicons of names, parish-specific
vocabulary, and other alpha-only local context.

Nothing in this directory except this README may enter Git history; the ingress check
enforces that rule even against `git add -f`. This is a path boundary, not a content-based
personal-data scanner: alpha may still contain recorded personal data elsewhere under
Tyrel's 2026-07-28 ruling.

Credentials are secrets. This directory is where the ones that must live on disk are kept,
and it is gitignored precisely so they can be — Tyrel's ruling. Three files live here, and
only the first is a secret:

- `ntfy.conf` — the notification bearer topic. `operations/notify/notify.sh` reads it when
  `NTFY_TOPIC` is not set in the environment. Anyone holding the topic can read the stream.
- `.notify-start-stamp` — the epoch second of the last *delivered* `start` ping, which
  `operations/notify/notify.sh` uses to suppress a duplicate within fifteen minutes. Not a
  secret and it never carries the topic; deleting it costs one duplicate notification.
- `workcopy.conf` — the absolute path of the working copy a writing Codex seat used. Not a
  secret, but machine-local: a path is right on one laptop and wrong everywhere else, so it
  is declared here rather than tracked in `operations/codex/seats.conf`.

  **No seat reads it today**, and whether a writing seat comes back is an open question for
  Tyrel — the handoff in `workbench/active/` carries it. The file stays until he rules.

A secret may live here. It may not leave: not into a commit, a script, a note, a transcript
or a command line. The old repository hardcoded the topic in five shell scripts and it ended
up in cleartext in a census dump.
