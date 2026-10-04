# Finding: automatic or model proposals are bounded and carry no authority

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

Later, automatic helpers (including outside models) may suggest adjustments. Having access to the tool's operations must not let them delete captures, clear holds, override a person's locks, accept results, send images elsewhere or spend money. A local web interface can be reached by other pages in the same browser unless protected.

## Pagekit's observed behaviour

Already: detectors only propose values, which a hand-set value always overrides; a disagreement between a detector and a hand-set value is a note, never a change; the review sheet is one file with no network access.

Differently: there is no interface for outside helpers and no local server, so these questions do not arise yet.

Not yet: proposals limited to bounded source-space adjustments with the right to abstain; operations that work on authorized handles rather than arbitrary paths or addresses; a local server bound to the loopback interface with session and origin checks; treating any image sent to a remote client as an outside disclosure checked against the project's policy.

## General technique

Least privilege: each caller receives only the operations it needs, over typed parameters, and actions with lasting effect (acceptance, deletion, release of holds, disclosure) require a person. For a local web server, bind to the loopback address, require a session token, and check the request's origin and host headers to defeat cross-site requests and rebinding.
Source: Saltzer and Schroeder, The protection of information in computer systems, Proceedings of the IEEE, 1975

## Settings in general terms

- None numeric. The set of operations open to each kind of caller is fixed by the project's policy.
