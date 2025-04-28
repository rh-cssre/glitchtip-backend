from typing import Any

from .base import LaxIngestSchema


class EventGeo(LaxIngestSchema):
    city: str | None = None
    country_code: str | None = None
    region: str | None = None
    subdivision: str | None = None


class EventUser(LaxIngestSchema):
    id: str | None = None
    username: str | None = None
    email: str | None = None
    ip_address: str | None = None
    data: dict[str, Any] | None = None
    geo: EventGeo | None = None
