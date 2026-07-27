# proof

The small, reasonable test that must pass before anything scales — Governance 9,
given a home so it cannot erode into folklore.

Sample pages here must be public-safe from the day they are committed. They survive
into the public release; anything personal never enters this directory.

Tyrel decides what counts as small, reasonable, and well.

Binary proof pages live under `fixtures/` and are declared one by one in
`fixtures.toml`. Each declaration binds the exact path, SHA-256, byte count, media
type, source, and reason. There is no directory-wide exception: an image that Tyrel
has not deliberately declared is refused before it enters Git history.

Ordinary tracked files are capped at 1 MiB. A declared proof image may be at most
25 MiB, and all declared proof images together may be at most 100 MiB. Git LFS
pointers and every other binary payload are refused; the repository keeps only the
small public evidence needed to prove the pipeline.
