# runtree

Where a run's evidence lives, and the only code that writes to it.

```text
<run>/run.json
<run>/<NN-stage>/artifacts/<kind>/<artifact-id>.json
<run>/<NN-stage>/blobs/sha256/<digest>
<run>/<NN-stage>/manifest.json
<run>/<NN-stage>/index.json
<run>/<NN-stage>/serving-logs/
<run>/1_exemplar/manifest-door.json
<run>/receipts/sha256/<digest>.json
<run>/run-health/recensor-partition-receipt.json
```

Serving receipts and approval records share `receipts/sha256/`. `serving-logs/` is
written by the serving launcher, not by this store, and is never inventoried as
evidence.

## Three promises

**Artifacts are immutable.** Publishing identical bytes under an identity that
already exists is a no-op reported as `reused` — that is how a resumed run proves
it did not redo work. Publishing *different* bytes under the same identity is
refused: the existing file is not touched and nothing is left behind.

**Publication is atomic.** Immutable artifacts and receipts use same-directory
temporary files, flush and fsync them, then atomically hard-link them into an
otherwise-unused identity; a different existing identity is refused. Derived
manifests, indexes and the Recensor partition receipt use `os.replace`. A crash cannot make a half-written artifact trusted by
a resume.

**Manifests are derived.** `manifest.json` is rebuilt from the artifacts on disk
every time it is written. Delete it and it comes back identical. If it ever
disagrees with the artifacts, the artifacts are right — which is why nothing may
treat a manifest as the evidence that something happened.

Door writes into Exemplar's evidence directory, but its producer inventory is
`1_exemplar/manifest-door.json`; Exemplar retains `1_exemplar/manifest.json`.
The files are separate because a later producer must not erase the stored set
of completion seals the earlier producer's last inventory named.

## run.json

The immutable authority for what this run *is*: its source pages, its configured
witness chairs, its configuration digest, its adapter recipes — self-hashed, so an
edit after sealing is detectable. Reopening a run id is refused before any write
when anything it is bound to has changed: the source manifest, configuration digest,
adapter recipes, witness chairs, corpus frame membership, corpus-register digest and
whether a register was required, and, when either run records them, the ingress,
render settings, sealed configuration digests and seal method. That is a different
run wearing an old name.

A replay run (`common/replay.py`) carries one more bound field, `replay`, naming
the run it replays. It is the source's authority under a new run id, with the
replaying code's `repository_commit`, and it holds the source's Door, Exemplar, Ink
map, Designator and Attestatores records as they were sealed, under the source's run
id: `RunTree.holds_run_id` accepts that id for those stages and no other. A replay run
is created only new (`RunTree.create_replay`), never into an existing directory.

It deliberately does not predeclare acts. Pages are given; acts are discovered, and
the Perlector's whole-page reading names them.

## Run receipts

A serving receipt is a content-addressed record of the endpoint and serving facts
that actually answered. It lives at `receipts/sha256/`, outside every stage's
artifact directory and manifest, because its endpoint and start time are a real
moment rather than deterministic stage output. Stage artifacts carry only its
digest-checked reference and the immutable resolved identity/revision.

The door owns no directory and writes into the Exemplar's, so a refusal at the door
sits inside the record of what arrived rather than in a drawer nothing reads.
