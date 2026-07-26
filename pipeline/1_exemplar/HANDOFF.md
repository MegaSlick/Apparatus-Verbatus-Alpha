# Exemplar — handoff

**What this stage writes:** the sealed pages and the record of what arrived

**Where it writes it:** the run's `{run}/<this stage>/` folder.

---

The declared shape of these files is **not yet settled**. It is discovered during
alpha, from real pages, and recorded here as it is learned.

This document is the only thing downstream stages may rely on. They read these
files; they never import this stage's code.
