from datetime import datetime
from django.utils.timezone import now
from typing import Optional, Union, Any
import uuid

from typing_extensions import TypedDict
from ninja import Schema, Field
from pydantic import Json


class TagKeyValue(TypedDict):
    key: str
    value: str


class EventIngestSchema(Schema):
    event_id: uuid.UUID
    timestamp: datetime = Field(default_factory=now)
    platform: Optional[str] = None
    level: Optional[str]
    logger: Optional[str] = None
    transaction: Optional[str] = None
    server_name: Optional[str] = None
    release: Optional[str] = None
    dist: Optional[str] = None
    tags: Optional[Union[dict[str, str], list[TagKeyValue]]] = None


class EnvelopeHeaderSchema(Schema):
    event_id: str
    dsn: Optional[str]
    sdk: Optional[Any]


class ItemHeaderSchema(Schema):
    content_type: Optional[str]
    type: str
    length: Optional[int]


EnvelopeSchema = list[Union[EnvelopeHeaderSchema, ItemHeaderSchema, EventIngestSchema]]
