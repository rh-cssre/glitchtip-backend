from datetime import datetime
from django.utils.timezone import now
from typing import Optional, Union, Any, TypedDict
import uuid
from ninja import Schema, Field
from pydantic import Json


class TagKeyValue(TypedDict):
    key: str
    value: str


class EventIngestSchema(Schema):
    event_id: uuid.UUID
    timestamp: datetime = Field(default_factory=now)
    platform: Optional[str]
    level: Optional[str]
    logger: Optional[str]
    transaction: Optional[str]
    server_name: Optional[str]
    release: Optional[str]
    dist: Optional[str]
    tags: Optional[Union[dict[str, str], list[TagKeyValue]]]


class EnvelopeHeaderSchema(Schema):
    event_id: str
    dsn: Optional[str]
    sdk: Optional[Any]


class ItemHeaderSchema(Schema):
    content_type: Optional[str]
    type: str
    length: Optional[int]


EnvelopeSchema = list[Union[EnvelopeHeaderSchema, ItemHeaderSchema, EventIngestSchema]]
