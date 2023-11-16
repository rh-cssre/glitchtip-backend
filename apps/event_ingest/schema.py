import logging
import typing
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal, Optional, Union

from django.utils.timezone import now
from ninja import Field, Schema
from pydantic import (
    AliasChoices,
    RootModel,
    ValidationError,
    WrapValidator,
    model_validator,
)

from apps.issue_events.constants import IssueEventType

logger = logging.getLogger(__name__)


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
    vars: Optional[dict[str, Union[str, dict, list]]] = None
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
    formatted: str = Field(max_length=8192, default="")
    message: Optional[str] = None
    params: Optional[Union[list[str], dict[str, str]]] = None


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
    level: Annotated[Optional[Level], WrapValidator(invalid_to_none)] = None
    timestamp: Optional[datetime] = None


class ValueEventBreadcrumb(Schema):
    values: list[EventBreadcrumb]


class BaseEventIngestSchema(Schema):
    timestamp: datetime = Field(default_factory=now)
    platform: Optional[str] = None
    level: Optional[str] = "error"
    logger: Optional[str] = None
    transaction: Optional[str] = Field(
        validation_alias=AliasChoices("transaction", "culprit"), default=None
    )
    server_name: Optional[str] = None
    release: Optional[str] = None
    dist: Optional[str] = None
    tags: Optional[Union[dict[str, str], list[TagKeyValue]]] = None
    environment: Optional[str] = None
    modules: Optional[dict[str, str]] = None
    extra: Optional[Any] = None
    fingerprint: Optional[list[str]] = None
    errors: Optional[list[Any]] = None

    exception: Optional[Union[list[EventException], ValueEventException]] = None
    message: Optional[Union[str, EventMessage]] = None
    template: Optional[EventTemplate] = None

    breadcrumbs: Optional[Union[list[EventBreadcrumb], ValueEventBreadcrumb]] = None


class EventIngestSchema(BaseEventIngestSchema):
    event_id: uuid.UUID


class EnvelopeHeaderSchema(Schema):
    event_id: uuid.UUID
    dsn: Optional[str] = None
    sdk: Optional[Any] = None
    sent_at: datetime = Field(default_factory=now)


SupportedItemType = Literal["transaction", "event"]
SUPPORTED_ITEMS = typing.get_args(SupportedItemType)


class ItemHeaderSchema(Schema):
    content_type: Optional[str]
    type: SupportedItemType
    length: Optional[int]


class EnvelopeSchema(RootModel[list[dict[str, Any]]]):
    root: list[dict[str, Any]]
    _header: EnvelopeHeaderSchema
    _items: list[tuple[ItemHeaderSchema, BaseEventIngestSchema]] = []

    @model_validator(mode="after")
    def validate_envelope(self) -> "EnvelopeSchema":
        data = self.root
        try:
            header = data.pop(0)
        except IndexError:
            raise ValidationError([{"message": "Envelope is empty"}])
        self._header = EnvelopeHeaderSchema(**header)

        while len(data) >= 2:
            item_header_data = data.pop(0)
            if item_header_data.get("type", None) not in SUPPORTED_ITEMS:
                continue
            item_header = ItemHeaderSchema(**item_header_data)
            if item_header.type == "event":
                try:
                    item = BaseEventIngestSchema(**data.pop(0))
                except ValidationError as err:
                    logger.warning("Envelope Event item invalid", exc_info=True)
                    raise err
                self._items.append((item_header, item))

        return self


class CSPReportSchema(Schema):
    """
    https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy-Report-Only#violation_report_syntax
    """

    blocked_uri: str = Field(alias="blocked-uri")
    disposition: Literal["enforce", "report"] = Field(alias="disposition")
    document_uri: str = Field(alias="document-uri")
    effective_directive: str = Field(alias="effective-directive")
    original_policy: Optional[str] = Field(alias="original-policy")
    script_sample: Optional[str] = Field(alias="script-sample", default=None)
    status_code: Optional[int] = Field(alias="status-code")
    line_number: Optional[int] = None
    column_number: Optional[int] = None


class SecuritySchema(Schema):
    csp_report: CSPReportSchema = Field(alias="csp-report")


## Normalized Interchange Issue Events


class IssueEventSchema(BaseEventIngestSchema):
    """
    Event storage and interchange format
    Used in json view and celery interchange
    Don't use this for api intake
    """

    type: Literal[IssueEventType.DEFAULT] = IssueEventType.DEFAULT


class ErrorIssueEventSchema(BaseEventIngestSchema):
    type: Literal[IssueEventType.ERROR] = IssueEventType.ERROR


class CSPIssueEventSchema(BaseEventIngestSchema):
    type: Literal[IssueEventType.CSP] = IssueEventType.CSP
    csp: CSPReportSchema


class InterchangeIssueEvent(Schema):
    """Normalized wrapper around issue event. Event should not contain repeat information."""

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    project_id: int
    received_at: datetime = Field(default_factory=now)
    payload: Union[
        IssueEventSchema, ErrorIssueEventSchema, CSPIssueEventSchema
    ] = Field(discriminator="type")
