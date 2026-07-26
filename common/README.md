# common

The only code a stage may import besides its own.

It knows nothing about stages. Stages import it; it never imports back. `tach.toml`
declares this the only shared module and CI enforces it.

Code enters here **when a second stage needs it** — not in anticipation. Moving
something in is its own pull request, because this is the one place two agents can
genuinely collide.
