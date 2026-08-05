# runtree

Where a run's evidence lives, and the only code that writes to it.

```text
<run>/run.json
<run>/<NN-stage>/artifacts/<kind>/<artifact-id>.json
<run>/<NN-stage>/blobs/sha256/<digest>
<run>/<NN-stage>/manifest.json
<run>/receipts/sha256/<digest>.json
```

## Three promises

**Artifacts are immutable.** Publishing identical bytes under an identity that
already exists is a no-op reported as `reused` — that is how a resumed run proves
it did not redo work. Publishing *different* bytes under the same identity is
refused before anything is written, and the existing file is not touched.

**Publication is atomic.** Immutable artifacts and receipts use same-directory
temporary files, flush and fsync them, then atomically hard-link them into an
otherwise-unused identity; a different existing identity is refused. Derived
manifests use `os.replace`. A crash cannot make a half-written artifact trusted by
a resume.

**Manifests are derived.** `manifest.json` is rebuilt from the artifacts on disk
every time it is written. Delete it and it comes back identical. If it ever
disagrees with the artifacts, the artifacts are right — which is why nothing may
treat a manifest as the evidence that something happened.

## run.json

The immutable authority for what this run *is*: its source pages, its configured
witness chairs, its configuration digest, its adapter recipes — self-hashed, so an
edit after sealing is detectable. Reopening a run id whose source, configuration,
recipes, or chair roster have changed is refused before any write: that is a
different run wearing an old name.

It deliberately does not predeclare acts. Pages are given; acts are discovered, and
the Designator's proposal seal is the downstream expected-act authority.

## Run receipts

A serving receipt is a content-addressed record of the endpoint and serving facts
that actually answered. It lives at `receipts/sha256/`, outside every stage's
artifact directory and manifest, because its endpoint and start time are a real
moment rather than deterministic stage output. Stage artifacts carry only its
digest-checked reference and the immutable resolved identity/revision.

The door owns no directory and writes into the Exemplar's, so a refusal at the door
sits inside the record of what arrived rather than in a drawer nothing reads.
