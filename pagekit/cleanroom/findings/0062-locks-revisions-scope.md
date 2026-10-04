# Finding: manual decisions survive reruns; stale jobs cannot overwrite newer decisions

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

A person corrects a page while an older automatic job, started before the correction, is still running. When it finishes, it must not replace the newer decision. Batch actions must say exactly which pages and which revisions they apply to. Undo and redo are expected in an editor.

## Pagekit's observed behaviour

Already: manual values are kept on re-run and flagged if what they were set on changed; locked values are kept and never flagged; values for a page that no longer exists are kept aside and return if the page does; a run that fails leaves everything as it was; only what depends on a change is recomputed.

Differently: work is a single command run, so concurrent jobs and stale results do not arise yet.

Not yet: revision numbers on each page's decisions; a check that refuses a result computed from an older revision; undo and redo; batch actions naming their exact scope and candidate revisions; protected regions that survive reruns.

## General technique

Optimistic concurrency control: every write states the revision it was based on, and the store accepts it only if that is still the current revision; otherwise the write is refused as stale and the job's result is offered as a proposal against the new revision, if at all. Undo and redo keep a history of decisions rather than of images.
Source: Kung and Robinson, On optimistic methods for concurrency control, ACM Transactions on Database Systems, 1981

## Settings in general terms

- None numeric. Locks are bound to the source identity and revision.
