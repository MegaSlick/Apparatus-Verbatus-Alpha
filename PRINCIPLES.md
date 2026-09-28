# Principles

Apparatus Verbatus reads handwritten historical registers, Quebec parish records first,
and recovers the very words on the page, so those records can be searched and trusted.
This page says what the project is aiming for. How the code is built and how the work is
done are in CONTRIBUTING.md and AGENTS.md.

## The idea

Several vision models act as witnesses: each reads an entry and reports what it saw. One
reader, the Perlector, then reads the ink itself, uses the witnesses only as clues, and
establishes the text. No step chooses a winner among the witnesses.

## What we are aiming for

- **Read the ink as close to perfectly as it allows.** Accuracy is judged against the
  page itself, through careful human transcriptions, not against what a model reported or
  what a plausible entry of the period would say.
- **Never lose an entry.** A missed entry is worse than a poorly read one: a poor reading
  can be corrected later, but an entry nobody knows exists is gone for good. Nothing is
  silently dropped, and a partial result always looks partial.
- **Flag uncertainty, never fabricate.** What cannot be read is marked as unread. Doubt,
  disagreement and suspected invention are shown, never smoothed over, and a bad reading
  is flagged rather than re-run until it looks better.
- **Keep the source untouched.** The original image is sealed; every reading is added on
  top of it and replaces nothing.
- **Be trustworthy.** Every reading traces back to the ink it came from and to the models
  and steps that produced it, so anyone can check it.
- **Give the same reading everywhere.** Each entry has one established reading, and every
  export format shows that same reading.
- **Measure honestly.** We claim only what we have measured, first on a small real test
  and at scale only once that holds. A test never tells a model what answer to give.
- **Put quality before speed.** Slower, more careful reading is always an acceptable cost.
- **Keep private material private.** Real register images and personal data stay on the
  project lead's own machines and rented servers; credentials never enter the code.
- **Be easy to use.** Page images in; a complete, checkable export out, in the formats
  people need. When something is held back, it says why and what to do next.

## Scope

For now: page images in, established readings out, and measuring how well that works.
Training the reader, searching the output and correcting it come later.

Changes to this page are the project lead's decision.
