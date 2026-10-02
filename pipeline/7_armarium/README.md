# Armarium

Writes the pipeline's product bundle. It projects established Archetypus readings,
their provenance and their links to exact regions of ink into the formats the run
sealed; it does not establish, repair, choose or rewrite text. The pipeline ends here.

`run.py` accounts for every counted reading in one of five categories, builds the
bundle, verifies it and seals it into the run tree. A partial run exits held and says so
in its manifest, its readable text and its acts database; the JSONL files are rows
only and are read with the manifest. `bundle.py` publishes the sealed bundle to a destination outside
the run tree, verifying it again on the way out. Nothing else takes a product out of
this stage.

Verification proves a bundle is internally consistent and closed; a self-hash does not
authenticate the run-derived facts, and authenticity beyond the retained run tree needs
an external trust root.

Read [CONTRACT.md](CONTRACT.md) for what this stage writes and where. That document
is the interface — no other stage reads this one's code.

See the root [ARCHITECTURE.md](../../ARCHITECTURE.md) for how this fits the flow,
and [GLOSSARY.md](../../GLOSSARY.md) for the vocabulary.
