# private

**Local only. Everything here except this file is gitignored.**

Local material that must never enter the repository: community-specific context such as
name lexicons and parish vocabulary, and the few credentials that have to live on disk.
The ingress check refuses anything under this path, even with `git add -f`. It is a path
boundary, not a scanner for personal data elsewhere in the tree.

What lives here:

| Path | What it holds |
|---|---|
| `foolsgold/` | the Fool's Gold reference set (unverified AI drafts) and its source material |
| `bakeoff/` | the current bake-off's witness caches and scores |
| `corpora/` | the expected local corpus root (`operations/corpus/README.md`) |
| `tools/` | the Fool's Gold scoring brief and script |
| `RecordGold_test_*/` | the pilot spreads |
| `live-<date>/` | evidence of the latest live pod run; older ones move to `archive/` |
| `archive/` | closed runs and deliveries, one dated folder each |
| `ntfy.conf` | the notification topic, read by `operations/notify/notify.sh` when `NTFY_TOPIC` is not set. Anyone holding the topic can read the stream, so it is a secret, and so is any backup of it. |
| `.notify-start-stamp` | when the last `start` notification was delivered, used to suppress duplicates within fifteen minutes. Not a secret. |

A secret may live here; it may not leave — not into a commit, a script, a note, a
transcript or a command line.
