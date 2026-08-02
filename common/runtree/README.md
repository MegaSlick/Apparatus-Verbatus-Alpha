# runtree

Where a run's evidence lives, and the only code that writes to it.

```text
<run>/run.json
<run>/<NN-stage>/artifacts/<kind>/<artifact-id>.json
<run>/<NN-stage>/blobs/sha256/<digest>
<run>/<NN-stage>/manifest.json
```

## Three promises

**Artifacts are immutable.** Publishing identical bytes under an identity that
already exists is a no-op reported as `reused` — that is how a resumed run proves
it did not redo work. Publishing *different* bytes under the same identity is
refused before anything is written, and the existing file is not touched.

**Publication is atomic.** Temp file in the same directory, flushed and fsynced,
then `os.replace`. A crash leaves either the old artifact or the new one. A
half-written artifact that a resume trusts is the failure the sealed tree exists
to prevent.

**Manifests are derived.** `manifest.json` is rebuilt from the artifacts on disk
every time it is written. Delete it and it comes back identical. If it ever
disagrees with the artifacts, the artifacts are right — which is why nothing may
treat a manifest as the evidence that something happened.

## run.json

The immutable authority for what this run *is*: its source pages, its configured
witness seats, its configuration digest, its adapter recipes — self-hashed, so an
edit after sealing is detectable. Reopening a run id whose source, configuration,
recipes, or seat roster have changed is refused before any write: that is a
different run wearing an old name.

It deliberately does not predeclare acts. Pages are given; acts are discovered, and
the Designator's proposal seal is the downstream expected-act authority.

The door owns no directory and writes into the Exemplar's, so a refusal at the door
sits inside the record of what arrived rather than in a drawer nothing reads.
