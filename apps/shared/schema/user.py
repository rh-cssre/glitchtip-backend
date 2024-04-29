from typing import Any, Dict, Optional

from .base import LaxIngestSchema


class EventGeo(LaxIngestSchema):
    city: Optional[str] = None
    country_code: Optional[str] = None
    region: Optional[str] = None
    subdivision: Optional[str] = None


class EventUser(LaxIngestSchema):
    id: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    ip_address: Optional[str] = None
    subscription: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    geo: Optional[EventGeo] = None
    name: Optional[str] = None
    segment: Optional[str] = None
