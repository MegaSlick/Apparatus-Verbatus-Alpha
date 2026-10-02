# Incident note template

The host writes an incident note when a check or a person suspects that ScanTailor or
other GPL page-processing code has reached pagekit. Copy it to
`pagekit/cleanroom/incidents/NNNN.md`, fill in every field in plain words (the lead
does not read code), and commit it with `pagekit/cleanroom/HOLD`. The lead fills in
the Decision line; the commit that removes HOLD carries it. See CLEANROOM.md,
"If a leak is suspected".

Copy everything below this line.

---

# Incident NNNN: [a few words naming what happened]

**What was detected.** [What the check or the person found, in plain words. Never paste
the suspect text itself.]

**Where.** [Which part of pagekit, described in words: which tool, which file, which
commit, and which agent or session produced it.]

**Why it matters.** [What it would mean for pagekit's licence and independence if the
suspicion is right.]

**The three choices.**

- *Purge:* the affected work is reverted or deleted and rebuilt from the spec by a
  fresh build-side agent that has never seen it.
- *Minor breach:* the work stays or is lightly changed; this note records exactly what
  was carried over and what was done about it.
- *False flag:* nothing was carried over (for example an idiom everyone uses); this
  note records why.

**Recommendation.** [The host's recommendation and its reason.]

**What was done.** [Filled in after the decision: commits reverted, agents replaced,
files changed.]

[The lead's decision goes on the next line, starting with purge, minor breach or false
flag, then the reason, for example "Decision: false flag, a loop every image program
writes the same way".]

Decision:
