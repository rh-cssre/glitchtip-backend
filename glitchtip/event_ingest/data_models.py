import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal, Optional, Union

from django.utils.timezone import now
from ninja import Field, Schema
from pydantic import Json, ValidationError, WrapValidator


def invalid_to_none(v: Any, handler: Callable[[Any], Any]) -> Any:
    try:
        return handler(v)
    except ValidationError:
        return None


class TagKeyValue(Schema):
    key: str
    value: str


class Signal(Schema):
    number: int
    code: Optional[int]
    name: Optional[str]
    code_name: Optional[str]


class MachException(Schema):
    number: int
    code: int
    subcode: int
    name: Optional[str]


class NSError(Schema):
    code: int
    domain: str


class Errno(Schema):
    number: int
    name: Optional[str]


class MechanismMeta(Schema):
    signal: Optional[Signal] = None
    match_exception: Optional[MachException] = None
    ns_error: Optional[NSError] = None
    errno: Optional[Errno] = None


class ExceptionMechanism(Schema):
    type: str
    description: Optional[str] = None
    help_link: Optional[str] = None
    handled: Optional[bool] = None
    synthetic: Optional[bool] = None
    meta: Optional[dict] = None
    data: Optional[dict] = None


class StackTraceFrame(Schema):
    filename: Optional[str] = None
    function: Optional[str] = None
    raw_function: Optional[str] = None
    module: Optional[str] = None
    lineno: Optional[int] = None
    colno: Optional[int] = None
    abs_path: Optional[str] = None
    context_line: Optional[str] = None
    pre_context: Optional[list[str]] = None
    post_context: Optional[list[str]] = None
    source_link: Optional[str] = None
    in_app: Optional[bool] = None
    stack_start: Optional[bool] = None
    vars: Optional[dict[str, str]] = None
    instruction_addr: Optional[str] = None
    addr_mode: Optional[str] = None
    symbol_addr: Optional[str] = None
    image_addr: Optional[str] = None
    package: Optional[str] = None
    platform: Optional[str] = None


class StackTrace(Schema):
    frames: list[StackTraceFrame]
    registers: Optional[dict[str, str]] = None


class EventException(Schema):
    type: str
    value: Annotated[Optional[str], WrapValidator(invalid_to_none)]
    module: Optional[str] = None
    thread_id: Optional[str] = None
    mechanism: Optional[ExceptionMechanism] = None
    stacktrace: Optional[StackTrace] = None


class ValueEventException(Schema):
    values: list[EventException]


class EventMessage(Schema):
    formatted: str = Field(max_length=10)  # When None, attempt to generate it
    message: Optional[str] = None
    params: Optional[list[str]] = None


class EventTemplate(Schema):
    lineno: int
    abs_path: Optional[str] = None
    filename: str
    context_line: str
    pre_context: Optional[list[str]] = None
    post_context: Optional[list[str]] = None


Level = Literal["fatal", "error", "warning", "info", "debug"]


class EventBreadcrumb(Schema):
    type: Optional[str] = None
    category: Optional[str] = None
    message: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    level: Optional[Level] = None
    timestamp: Optional[datetime] = None


class ValueEventBreadcrumb(Schema):
    values: list[EventBreadcrumb]


class BaseEventIngestSchema(Schema):
    timestamp: datetime = Field(default_factory=now)
    platform: Optional[str] = None
    level: Optional[str] = "error"
    logger: Optional[str] = None
    transaction: Optional[str] = None
    server_name: Optional[str] = None
    release: Optional[str] = None
    dist: Optional[str] = None
    tags: Optional[Union[dict[str, str], list[TagKeyValue]]] = None
    environment: Optional[str] = None
    modules: Optional[dict[str, str]] = None
    extra: Optional[Json] = None
    fingerprint: Optional[list[str]] = None
    errors: Optional[list[Json]] = None

    exception: Optional[Union[list[EventException], ValueEventException]] = None
    message: Optional[EventMessage] = None
    template: Optional[EventTemplate] = None

    breadcrumbs: Optional[Union[list[EventBreadcrumb], ValueEventBreadcrumb]] = None


class EnvelopeEventIngestSchema(BaseEventIngestSchema):
    type: str


class EventIngestSchema(BaseEventIngestSchema):
    event_id: uuid.UUID


class EnvelopeHeaderSchema(Schema):
    event_id: uuid.UUID
    dsn: Optional[str] = None
    sdk: Optional[Any] = None
    sent_at: datetime = Field(default_factory=now)


class ItemHeaderSchema(Schema):
    content_type: Optional[str]
    type: Literal["transaction", "event"]
    length: Optional[int]


EnvelopeSchema = list[
    Union[EnvelopeHeaderSchema, ItemHeaderSchema, EnvelopeEventIngestSchema]
]
