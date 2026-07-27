# common

The only project code a stage may import besides its own.

It knows nothing about stages. Stages import it; it never imports back. `tach.toml`
declares this as the shared importable package. That declaration covers only the
shared package; every numbered stage implementation must carry its own executable
boundary tests.

Code enters here **when a second stage needs it** — not in anticipation. Moving
something in is its own pull request, because this is the one place two agents can
genuinely collide.
