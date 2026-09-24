# Code size baseline, September 2026

Measured at commit `143cfbf3bc` (main after PR #143) with
`python3 operations/metrics/measure.py .`. Rerun the script and compare to see
what a cleanup actually removed.

| | files | lines | comments | docstrings | # noqa | # type: ignore | # pragma: no cover |
|---|---|---|---|---|---|---|---|
| src | 245 | 151939 | 11010 | 22549 | 263 | 50 | 66 |
| test | 280 | 211429 | 8115 | 25516 | 171 | 400 | 30 |

- Git-tracked `.py` files only; `gold/` and `private/` are excluded (the local
  areas `.venv/`, `build/`, `workbench/` are untracked, so never counted).
- A file is a test if its name starts with `test_` or it sits under `tests/`.
- *comments* are lines whose first non-blank character is `#`; *docstrings* are
  the lines spanned by module, class and function docstrings.
- The three marker columns count occurrences, not lines.
