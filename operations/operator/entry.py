"""Small console boundary that can render an application-import failure safely."""

from __future__ import annotations

from typing import Sequence

from .errors import ErrorCode, OperatorError


def _load_application():  # type: ignore[no-untyped-def]
    """Import the larger application only after the plain-language boundary exists."""

    from .cli import main

    return main


def main(argv: Sequence[str] | None = None) -> int:
    """Run Verbatus without letting an import failure become a traceback."""

    try:
        return _load_application()(argv)
    except OperatorError as error:
        print(error.render())
        return 2
    except Exception as error:
        print(OperatorError(ErrorCode.UNEXPECTED, detail=str(error)).render())
        return 2


if __name__ == "__main__":  # pragma: no cover - console boundary
    raise SystemExit(main())
