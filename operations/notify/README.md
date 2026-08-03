# Notifications — how a message reaches his phone

**Which four moments may be sent, and what each means, is in CLAUDE.md under Reporting.**
That is the rule and it is not restated here. This file is the mechanism: how to send
one, what a failure means, and the secret it must never expose.

## Sending one

```sh
sh operations/notify/notify.sh <start|milestone|decision|done> "<one line>"
```

The message must be a single non-empty line. A newline or a null in it is refused rather
than truncated, so a multi-line message never arrives as a misleading fragment.

**Main session only. A subagent never notifies.** Nothing in the script enforces that —
it is a rule, and it holds because it is written in CLAUDE.md.

## What each event does

**What each event means is CLAUDE.md's to say, not this file's** — the table below carries
only what the script does with it and who is allowed to send it.

| Event | Title on the phone | Priority | Sent by |
|---|---|---|---|
| `start` | Session started | 2 | the `SessionStart` hook, never a hand |
| `milestone` | Milestone | 3 | the session |
| `decision` | Needs a decision | 4 | the session |
| `done` | Session complete | 3 | `/session-end` |

Any other event name is refused.

## Failure is reported honestly

**Every event exits non-zero when delivery failed**, and prints `NOT DELIVERED` with the
reason. There is no event whose failure is reported as success.

That is deliberate and was not always true: `start` and `milestone` once exited 0 after
printing `NOT DELIVERED`, so a caller checking the status was told the phone had it. Two
reviewers found it. A milestone is often the only announcement of a long unattended
result, which makes it the worst one to lie about.

A failing ping cannot kill a session: the `SessionStart` hook is declared `"async": true`
in `.claude/settings.json`, so it runs detached. Keeping a caller non-blocking is the
caller's job — never buy it by misreporting delivery.

**If a send fails, say so in the session.** A decision ping nobody hears is a session
waiting on a message that was never sent.

## The topic is a bearer secret

Anyone holding the topic can publish to his phone. It lives in `private/ntfy.conf`, which
is gitignored, or in `NTFY_TOPIC` in the environment.

**It never enters a script, a note, a commit, a transcript, or a command line.** The
script reads it from the file and keeps it out of `curl`'s arguments, because arguments
are visible to anything that can list processes. Do not echo it to check it; check that
the file exists instead.

The destination is fixed to `https://ntfy.sh`. Setting `NTFY_SERVER` is refused outright
rather than honoured, so a redirect to another host cannot be arranged by an environment
variable.

## Why `start` is rate-limited and the others are not

The desktop app can open several sessions in one launch, and each fires the hook. Four
pings for one sitting is noise, and noise is what teaches him to ignore the next one. So
`start` is suppressed if another was delivered within fifteen minutes, using a stamp at
`private/.notify-start-stamp`.

**`milestone`, `decision` and `done` are never suppressed.** A rate limit on those could
swallow a real result.

The stamp is treated as evidence rather than trusted for existing: it must be a regular
file, not a symlink, holding a plausible past clock reading. A directory left by a
crashed run, a FIFO, a symlink, or a future-dated stamp each silenced real notifications
before that check existed. Every refusal returns "not fresh" and therefore *sends* the
ping — the cost of being wrong that way is one duplicate, and the cost of the other way
is a session start nobody hears about. The stamp records a clock reading and nothing
else; the topic never enters it.

## Tests

`operations/notify/test_notify.py` covers the event table, the one-line rule, the
honest exit codes, the topic handling, and the stamp's refusals. Run it with the rest of
the suite.
