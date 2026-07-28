# private

**Local only. Everything here except this file is gitignored.**

Community-specific and personal material: lexicons of names, parish-specific
vocabulary, and other alpha-only local context.

Nothing in this directory except this README may enter Git history; the ingress check
enforces that rule even against `git add -f`. This is a path boundary, not a content-based
personal-data scanner: alpha may still contain recorded personal data elsewhere under
Tyrel's 2026-07-28 ruling.

Credentials and notification topics are secrets and do not belong in any file here. The
notification client refuses `private/ntfy.conf` without reading it and accepts its bearer
topic only from the process environment. Notifications remain off until Tyrel chooses how
that environment value is stored and injected without a file.
