# proof

The synthetic fixture every stage program and integration test runs on, and the
generators that build it. Nothing here is register material: the pages are abstract
rectangles and pixel runs, and every reading the fake chairs give is `SYNTHETIC ACT`
text. These files are public from the day they are committed, so anything real or
personal never enters this directory.

- `synthetic_pages.py` describes the three fixture pages and renders them to PNG
  through `common/imaging.py`.
- `build_fixture.py` writes the page PNGs under `fixtures/synthetic-two-page-v0/`,
  the ingress declaration `fixtures.toml`, and `skeleton_fixture.toml`, which the
  stages read as data: the pages, the fake chairs' answers and the scenarios.
  Run `python3 proof/build_fixture.py` from the repository root and commit what it
  writes; edit the generator, never the TOML.
- `build_model_fixtures.py` writes the stand-in chair snapshots under
  `config/model-fixtures/` and their manifests under `config/manifests/`, which
  `config/models.toml` pins. They are a few bytes per chair, not models.

The tests beside them rebuild both declarations and the model snapshots and fail
on any difference, check that the checked-in PNGs decode to the pixels the
generator describes, and cross-check the declared answers against the chair
roster and the witness adapters.

Binary proof pages live under `fixtures/` and are declared one by one in
`fixtures.toml`. Each declaration binds the exact path, SHA-256, byte count, media
type, source, and reason. There is no directory-wide exception: the ingress check
refuses an image not deliberately declared before it enters Git history.

Ordinary tracked files are capped at 1 MiB. A declared proof image may be at most
25 MiB, and all declared proof images together may be at most 100 MiB. Git LFS
pointers and every other binary payload are refused.
