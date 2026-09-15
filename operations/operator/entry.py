"""Small console boundary that can render an application-import failure safely."""

from __future__ import annotations

import sys
from typing import Sequence

from common.checkout import NotACheckoutRefusal, require_checkout

from .errors import ErrorCode, OperatorError


def _load_application():  # type: ignore[no-untyped-def]
    """Import the larger application only after the plain-language boundary exists."""

    from .cli import main

    return main


def main(argv: Sequence[str] | None = None) -> int:
    """Run Verbatus without letting an import failure become a traceback."""

    try:
        # Before the application, and before any verb: this code reads its
        # configuration, stage programs and proof material from the checkout it
        # sits in (`common/checkout.py`), and a wheel carries none of them. An
        # operator who has somehow started outside one is told so in one screen
        # here, rather than meeting an absent `config/…json` part way through a
        # run that had already begun.
        require_checkout()
        return _load_application()(argv)
    except NotACheckoutRefusal as error:
        print(OperatorError(ErrorCode.NOT_A_CHECKOUT, detail=str(error)).render())
        return 2
    except OperatorError as error:
        print(error.render())
        return 2
    except KeyboardInterrupt:
        print(OperatorError(ErrorCode.INTERRUPTED).render())
        return 2
    except Exception as error:
        print(_unexpected(error, argv).render())
        return 2


def _unexpected(error: Exception, argv: Sequence[str] | None) -> OperatorError:
    """The unclassified message, with a receipt behind it wherever one can be written.

    This boundary exists to render an import failure of the application, so
    the application's own receipt writer may be exactly what cannot be
    imported; then the message alone is the record, and says so.
    """

    try:
        from .cli import record_unexpected
    except Exception:  # noqa: BLE001 -- the application did not load; render without it
        return OperatorError(
            ErrorCode.UNEXPECTED,
            detail=(
                "No receipt could be saved (the application did not load); this message "
                f"is the only record. {type(error).__name__}: {error}"
            ),
        )
    arguments = list(sys.argv[1:] if argv is None else argv)
    return record_unexpected(error, arguments, None)


if __name__ == "__main__":  # pragma: no cover - console boundary
    raise SystemExit(main())
