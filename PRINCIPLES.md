# Principles

What Apparatus Verbatus is for, and the principles every change is held to. Read this
before writing code, whether you are a person or a model. These are principles to apply
with judgement, not a checklist. When following one would cost an act, hide a result or
fight a goal, stop and raise it rather than choosing quietly.

## Goals

1. **Read archival handwriting as close to perfectly as the ink allows.** Accuracy is
   measured against the ink itself, not against what a witness model reported, and not
   against what a plausible entry of that period would say.
2. **Never lose an act.** A missed act is worse than a poorly read one: a poor reading
   can be corrected later, but an act nobody knows exists is lost for good.
3. **Flag uncertainty; never fabricate.** An unclear word stays marked as unclear. A
   gap, a guess or a suspected invention is shown as such, never smoothed over.
4. **Be trustworthy.** Every reading can be traced back to the exact region of ink it
   came from, the models that saw it and the steps that produced it, so anyone can check
   it.
5. **Be easy to use.** Page images in; a complete, checkable export out, in the formats
   people need.

These goals are aims, not claims. A claim that the pipeline achieves any of them is made
only from measurement (principle 8).

## Principles

1. **The reader reads; nothing picks.** Several witness models report on each act. The
   Perlector reads the ink itself and uses their testimony as clues. No step selects a
   winner among witnesses, and a witness's reading is never itself an output. A picker
   rebuilt under another name is still a picker.

2. **Nothing is lost silently.** An act may be uncertain, unreadable or held for review;
   it may never disappear behind a successful status. Partial results are visibly
   partial, and "complete" is refused unless everything reconciles. The same goes for
   what we learn about the pipeline: a finding, a failure or a decision that exists only
   in a transcript nobody reads has been lost.

3. **Uncertainty is an output.** Doubt, disagreement and suspected fabrication are
   recorded and passed on, not resolved by guessing. The pipeline does not try to correct
   a model's behaviour; its job is to feed each model completely, record what it said, and
   flag what looks wrong for review.

4. **Evidence is never overwritten.** The source image is sealed and immutable. Witness
   testimony and readings are layers: each records, none replaces what came before.

5. **One text, everywhere.** Each act has one established text, projected identically
   into every output format. Showing witness testimony *as testimony* is not a second
   text.

6. **Provenance travels with the record.** Every stored reading carries the identity and
   revision of the model that produced it, the image region it read, and the transforms
   applied to that image. Configuration protects future runs; the record protects the
   past.

7. **Recovery restores coverage, never quality.** A stage may ask for rework to recover
   a missed region, a cut-off crop or a continuation, a fixed and recorded number of
   times. A bad reading is flagged, never re-rolled until it looks better. A model's own
   pinned upstream inference recipe (for example, a vendor's documented retry on
   repetition) is part of that model, as long as every attempt is kept.

8. **Measure honestly.** A metric that cannot be measured is a failure, not a pass.
   Claims are made only about what was actually measured. The instrument must not steer
   what it measures: a grading prompt that sets a floor or tells a reader which way to
   argue reports the instruction, not the finding.

9. **Quality over speed.** Extra passes, more careful reading and slower runs are
   acceptable costs. Speed is never a reason to read less carefully.

10. **Prove small before scaling.** A small, representative test has to perform well
    before anything runs at scale.

11. **Write code a stranger can trust.** Code is plain, small and named for what it
    does. Comments explain *why* where the code cannot; they do not narrate history or
    cite a rule to excuse a workaround — fix the workaround. Nothing enters that its
    author cannot explain line by line.

12. **Other people's work is credited, and private material stays out.** Third-party
    code enters only under a licence that permits it, with its source recorded, and code
    adapted from elsewhere is named as such. Real register images, transcriptions,
    personal data and credentials never enter the repository; tests use synthetic
    fixtures.

## Scope

Source images in, established readings out: import to export. Training models,
research, search and correction of the output happen elsewhere.

## Decisions

The project lead decides what counts as proven, when a test is small and good enough to
scale, and any change to these principles. Everything else is ordinary engineering,
decided by whoever is doing the work and recorded with its reason.
