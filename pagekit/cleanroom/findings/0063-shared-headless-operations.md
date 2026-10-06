# Finding: one set of operations shared by the editor and the command line

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

The person wants a small browser editor and also a headless command interface for batches on a remote machine; later other programs may drive the tool. If each interface has its own logic, they drift apart and validation may be skipped in one of them.

## Pagekit's observed behaviour

Already: one command prepares a batch; the review sheet prints the exact command to apply a correction; the existing check command keeps its documented coordinates, schema and exit codes; other code can call the detectors and the writer.

Differently: there is no editor yet, and the review sheet is a static page that hands work back to the command line.

Not yet: a common set of operations (inspect, fetch bounded detail, adjust a candidate, render and validate, compare, accept, export, status, cancel, retrieve) used by every interface with the same validation; job identities for long work; retry keys bound to the request content; rendering outside the editor's event loop.

## General technique

Layered design: a core of typed operations with validation, and thin adapters (browser, command line, later programmatic) that translate requests into those operations. Long-running work returns a job identity; retries carry an idempotency key derived from the request, so a repeated request does not repeat its effect.
Source: general knowledge

## Settings in general terms

- Cache bounds in the browser and on the server: should depend on available memory and the number of pages a person views at once.
