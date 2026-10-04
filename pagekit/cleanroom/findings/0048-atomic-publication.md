# Finding: publish a verified set with a completion record written last

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

Export can be interrupted: cancellation, a full disk, a crash, a stale revision, or wrong pixels found by verification. None of these may leave something that looks like a successful accepted result. Storage differs between a local machine and a rented remote machine with network storage, and a rename on one disk is not a transaction across several files, an index and remote storage.

## Pagekit's observed behaviour

Already: a run that fails at any point leaves the output folder and project file as they were: every file is written in full beside its target, then all are moved into place together; the project file is written atomically.

Differently: the guarantee rests on moves within one local folder; the reports read do not describe a completion record or behaviour on network storage.

Not yet: a completion record written last that marks a set as complete; publishing an already verified frozen candidate without re-running the recipe; recovery tests for each supported deployment, local and remote.

## General technique

Write all artifacts under temporary names, verify them, then write a small completion record last; readers treat a set as present only when its completion record exists and its listed hashes match. Recovery on restart removes or resumes incomplete sets. This is the classic write-ahead and commit-record pattern from transaction processing, applied to files. Its guarantees hold only for the storage actually used, so each deployment is tested for crash and recovery.
Source: Gray and Reuter, Transaction Processing: Concepts and Techniques, Morgan Kaufmann, 1992

## Settings in general terms

- None numeric. What counts as durable should depend on the storage in use; temporary local disk on a rented machine is not durable.
