from typing import Optional, List, Union, Any
import uuid
from ninja import Schema
from pydantic import Json


class EventIngestSchema(Schema):
    event_id: uuid.UUID


class EnvelopeHeaderSchema(Schema):
    event_id: str
    dsn: Optional[str]
    sdk: Optional[Any]


class ItemHeaderSchema(Schema):
    content_type: Optional[str]
    type: str
    length: Optional[int]


EnvelopeSchema = List[Union[EnvelopeHeaderSchema, ItemHeaderSchema, EventIngestSchema]]
