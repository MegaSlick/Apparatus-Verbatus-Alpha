"""The deliberately plain-language operator surface.

Keep package import light: the console entry must be able to turn an application
import failure into the three-part recovery message instead of a Python traceback.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .surface import OperatorSurface

__all__ = ["OperatorSurface"]


def __getattr__(name: str) -> object:
    if name == "OperatorSurface":
        from .surface import OperatorSurface

        return OperatorSurface
    raise AttributeError(name)
