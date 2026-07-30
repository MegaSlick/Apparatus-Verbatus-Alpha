# Armarium — handoff

**What this stage writes:** the export, in the formats config asks for

**Where it writes it:** the run's `{run}/<this stage>/` folder.

---

Before this stage's implementation is accepted, this document must declare the exact
schema and invariants of every file it writes, grounded in proof-page evidence.

This document is the only thing downstream stages may rely on. They read these
files; they never import this stage's code.
