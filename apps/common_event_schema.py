from datetime import datetime
from typing import Annotated, Any, Literal, Optional

from ninja import Schema
from pydantic import (
    WrapValidator,
)

from .common_event_utils import invalid_to_none

Level = Literal["fatal", "error", "warning", "info", "debug"]


class EventBreadcrumb(Schema):
    type: str = "default"
    category: Optional[str] = None
    message: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    level: Annotated[Optional[Level], WrapValidator(invalid_to_none)] = "info"
    timestamp: Optional[datetime] = None
