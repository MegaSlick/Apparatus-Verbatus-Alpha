"""The bake-off's CTC line arms: line sources (Surya, kraken blla) and line recognisers.

Each arm module runs in the project environment and calls its vendor's own command in
the vendor's own environment (`venvs/<name>/`), reading only the files and output that
command writes. See `harness.py` for the shared command line and cache record.
"""
