from typing import Annotated

from pydantic import WrapValidator

from .base import LaxIngestSchema
from .exception import StackTrace
from .utils import invalid_to_none


class Thread(LaxIngestSchema):
    id: int | str | None = None
    current: bool | None = None
    crashed: bool | None = None
    name: str | None = None
    stacktrace: Annotated[StackTrace | None, WrapValidator(invalid_to_none)] = None
    raw_stacktrace: Annotated[StackTrace | None, WrapValidator(invalid_to_none)] = None


class ValueEventThread(LaxIngestSchema):
    values: list[Thread]
