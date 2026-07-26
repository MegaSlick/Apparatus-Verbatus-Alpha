# scriptorium

**Local only. Everything here except this file is gitignored.**

The making-room, as against the Armarium where finished work is kept. This is where
runs happen.

One directory per run. Inside it, one folder per stage, with the same seven names in
the same order as `pipeline/`. Open a finished run and you are looking at the same
flow chart — including every recovery loop record and receipt.

Stage N reads the previous folder's files and writes its own. This directory is where
"stages talk through files on disk" physically lives.
