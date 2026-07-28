# Recensor — handoff

**What this stage writes:** an append-only sequence of review outcomes: accepted, a recorded
coverage-recovery request, or an item held for review. Each recovery attempt and the terminal
outcome remain visible; a later outcome never overwrites the earlier one.

**Where it writes it:** the run's `{run}/<this stage>/` folder.

---

Before this stage's implementation is accepted, this document must declare the exact
schema and invariants of every file it writes, grounded in proof-page evidence.

This document is the only thing downstream stages may rely on. They read these
files; they never import this stage's code.
