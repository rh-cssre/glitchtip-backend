from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import WrapValidator

from .base import LaxIngestSchema
from .error import EventProcessingError
from .utils import invalid_to_none

Level = Literal["fatal", "error", "warning", "info", "debug"]


class EventBreadcrumb(LaxIngestSchema):
    type: str | None = "default"
    category: str | None = None
    message: str | None = None
    data: dict[str, Any] | None = None
    level: Annotated[Level | None, WrapValidator(invalid_to_none)] = "info"
    timestamp: datetime | None = None


ListKeyValue = list[list[str | None]]
"""
dict[str, list[str]] but stored as a list[list[:2]] for OSS Sentry compatibility
[["animal", "cat"], ["animal", "dog"], ["thing": "kettle"]]
This format is often used for http needs including headers and querystrings
"""


class BaseRequest(LaxIngestSchema):
    """Base Request class for event ingest and issue event API"""

    api_target: str | None = None
    body_size: int | None = None
    cookies: str | list[list[str | None]] | dict[str, str | None] | None = None
    data: str | dict | list | Any | None = None
    env: dict[str, Any] | None = None
    fragment: str | None = None
    method: str | None = None
    protocol: str | None = None
    url: str | None = None


class BaseIssueEvent(LaxIngestSchema):
    """
    Base Issue Event for fields present from the SDK data, json event, and api event
    """

    platform: str | None = None
    errors: list[EventProcessingError] | None = None
