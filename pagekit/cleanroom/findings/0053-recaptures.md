# Finding: repeated captures of the same page are related evidence, not duplicates

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

A register page may have been filmed twice, for example once badly exposed and once again. The two captures look similar. Each may hold detail the other lacks.

## Pagekit's observed behaviour

Already: every source is identified by its content hash and prepared on its own; nothing is ever deleted.

Differently: there is no notion of two captures being related.

Not yet: recording that captures are related; offering them for comparison; keeping both when similarity is detected.

## General technique

Near-duplicate detection (for example comparing compact image signatures or perceptual hashes) can suggest that two captures show the same page. That suggestion opens a comparison for a person; it never authorizes deleting one capture or fusing the two automatically. The relation is stored as a link between source identities with who confirmed it.
Source: general knowledge

## Settings in general terms

- The similarity level that suggests a relation: should depend on how much different exposures of the same page vary, measured on known pairs.
- No setting may turn similarity into deletion or fusion.
