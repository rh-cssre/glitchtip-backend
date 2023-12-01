from datetime import datetime
from typing import Annotated, Any, Literal, Optional, Union

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


ListKeyValue = list[list[Optional[str]]]
"""
dict[str, list[str]] but stored as a list[list[:2]] for OSS Sentry compatibility
[["animal", "cat"], ["animal", "dog"], ["thing": "kettle"]]
This format is often used for http needs including headers and querystrings
"""


class BaseRequest(Schema):
    """Base Request class for event ingest and issue event API"""

    api_target: Optional[str] = None
    body_size: Optional[int] = None
    cookies: Optional[
        Union[str, list[list[Optional[str]]], dict[str, Optional[str]]]
    ] = None
    data: Optional[Union[str, dict, list, Any]] = None
    env: Optional[dict[str, Any]] = None
    fragment: Optional[str] = None
    method: Optional[str] = None
    protocol: Optional[str] = None
    url: Optional[str] = None
