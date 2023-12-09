from collections.abc import Callable
from typing import Any

from pydantic import ValidationError


def invalid_to_none(v: Any, handler: Callable[[Any], Any]) -> Any:
    try:
        return handler(v)
    except ValidationError:
        return None
