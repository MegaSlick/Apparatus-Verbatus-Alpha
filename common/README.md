# common

The only project code a stage may import besides its own.

It knows nothing about stages. Stages import it; it never imports back. Add an
executable import-boundary check when real modules exist; a placeholder declaration
before code would prove nothing.

Code enters here **when a second stage needs it** — not in anticipation. Moving
something in is its own pull request, because this is the one place two agents can
genuinely collide.
