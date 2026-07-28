# private

**Local only. Everything here except this file is gitignored.**

Community-specific and personal material: lexicons of names, parish-specific
vocabulary, and other alpha-only local context.

Nothing in this directory except this README may enter Git history; the ingress check
enforces that rule even against `git add -f`. This is a path boundary, not a content-based
personal-data scanner: alpha may still contain recorded personal data elsewhere under
Tyrel's 2026-07-28 ruling.

Credentials are secrets. This directory is where the ones that must live on disk are kept,
and it is gitignored precisely so they can be — Tyrel's ruling. Two live here today:

- `ntfy.conf` — the notification bearer topic. `operations/notify/notify.sh` reads it when
  `NTFY_TOPIC` is not set in the environment. Anyone holding the topic can read the stream.
- `workcopy.conf` — the absolute path of the working copy a writing Codex seat uses. Not a
  secret, but machine-local: a path is right on one laptop and wrong everywhere else, so it
  is declared here rather than tracked in `operations/codex/seats.conf`.

A secret may live here. It may not leave: not into a commit, a script, a note, a transcript
or a command line. The old repository hardcoded the topic in five shell scripts and it ended
up in cleartext in a census dump.
