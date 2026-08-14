"""Run submission. Package marker only — `setuptools` packages this tree with
`namespaces = false`, so without this file `operations.submit` is left out of
the wheel and the installed `verbatus` entry point dies importing it."""
