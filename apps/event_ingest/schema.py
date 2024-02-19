import logging
import typing
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Optional, Union
from urllib.parse import parse_qs, urlparse

from django.utils.timezone import now
from ninja import Field
from ninja import Schema as BaseSchema
from pydantic import (
    AliasChoices,
    BeforeValidator,
    RootModel,
    ValidationError,
    WrapValidator,
    field_validator,
    model_validator,
)

from apps.issue_events.constants import IssueEventType

from ..shared.schema.contexts import ContextsSchema
from ..shared.schema.event import (
    BaseIssueEvent,
    BaseRequest,
    EventBreadcrumb,
    ListKeyValue,
)
from ..shared.schema.user import EventUser
from ..shared.schema.utils import invalid_to_none

logger = logging.getLogger(__name__)


CoercedStr = Annotated[
    str, BeforeValidator(lambda v: str(v) if isinstance(v, bool) else v)
]
"""
Coerced Str that will coerce bool to str when found
"""


class Schema(BaseSchema):
    """Schema configuration for all event ingest schemas"""

    class Config(BaseSchema.Config):
        coerce_numbers_to_str = True  # Lax is best for ingest


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

    def is_url(self, filename: str) -> bool:
        return filename.startswith(("file:", "http:", "https:", "applewebdata:"))

    @model_validator(mode="after")
    def normalize_files(self):
        if not self.abs_path and self.filename:
            self.abs_path = self.filename
        if self.filename and self.is_url(self.filename):
            self.filename = urlparse(self.filename).path
        return self


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

    @model_validator(mode="after")
    def set_formatted(self) -> "EventMessage":
        """
        When the EventMessage formatted string is not set,
        attempt to set it based on message and params interpolation
        """
        if not self.formatted and self.message:
            params = self.params
            if isinstance(params, list) and params is not None:
                self.formatted = self.message % tuple(params)
            elif isinstance(params, dict):
                self.formatted = self.message.format(**params)
        return self


class EventTemplate(Schema):
    lineno: int
    abs_path: Optional[str] = None
    filename: str
    context_line: str
    pre_context: Optional[list[str]] = None
    post_context: Optional[list[str]] = None


class ValueEventBreadcrumb(Schema):
    values: list[EventBreadcrumb]


class ClientSDKPackage(Schema):
    name: Optional[str] = None
    version: Optional[str] = None


class ClientSDKInfo(Schema):
    integrations: Optional[list[Optional[str]]] = None
    name: Optional[str]
    packages: Optional[list[ClientSDKPackage]] = None
    version: Optional[str]


class RequestHeaders(Schema):
    content_type: Optional[str]


class RequestEnv(Schema):
    remote_addr: Optional[str]


QueryString = Union[str, ListKeyValue, dict[str, Optional[str]]]
"""Raw URL querystring, list, or dict"""
KeyValueFormat = Union[list[list[Optional[str]]], dict[str, Optional[CoercedStr]]]
"""
key-values in list or dict format. Example {browser: firefox} or [[browser, firefox]]
"""


class IngestRequest(BaseRequest):
    headers: Optional[KeyValueFormat] = None
    query_string: Optional[QueryString] = None

    @field_validator("headers", mode="before")
    @classmethod
    def fix_non_standard_headers(cls, v):
        """
        Fix non-documented format used by PHP Sentry Client
        Convert {"Foo": ["bar"]} into {"Foo: "bar"}
        """
        if isinstance(v, dict):
            return {
                key: value[0] if isinstance(value, list) else value
                for key, value in v.items()
            }
        return v

    @field_validator("query_string", "headers")
    @classmethod
    def prefer_list_key_value(
        cls, v: Optional[Union[QueryString, KeyValueFormat]]
    ) -> Optional[ListKeyValue]:
        """Store all querystring, header formats in a list format"""
        result: Optional[ListKeyValue] = None
        if isinstance(v, str) and v:  # It must be a raw querystring, parse it
            qs = parse_qs(v)
            result = [[key, value] for key, values in qs.items() for value in values]
        elif isinstance(v, dict):  # Convert dict to list
            result = [[key, value] for key, value in v.items()]
        elif isinstance(v, list):  # Normalize list (throw out any weird data)
            result = [item[:2] for item in v if len(item) >= 2]

        if result:
            # Remove empty and any key called "Cookie" which could be sensitive data
            entry_to_remove = ["Cookie", ""]
            return sorted(
                [entry for entry in result if entry != entry_to_remove],
                key=lambda x: (x[0], x[1]),
            )
        return result


class IngestIssueEvent(BaseIssueEvent):
    timestamp: datetime = Field(default_factory=now)
    level: Optional[str] = "error"
    logentry: Optional[EventMessage] = None
    logger: Optional[str] = None
    transaction: Optional[str] = Field(
        validation_alias=AliasChoices("transaction", "culprit"), default=None
    )
    server_name: Optional[str] = None
    release: Optional[str] = None
    dist: Optional[str] = None
    tags: Optional[KeyValueFormat] = None
    environment: Optional[str] = None
    modules: Optional[dict[str, Optional[str]]] = None
    extra: Optional[dict[str, Any]] = None
    fingerprint: Optional[list[str]] = None
    errors: Optional[list[Any]] = None

    exception: Optional[Union[list[EventException], ValueEventException]] = None
    message: Optional[Union[str, EventMessage]] = None
    template: Optional[EventTemplate] = None

    breadcrumbs: Optional[Union[list[EventBreadcrumb], ValueEventBreadcrumb]] = None
    sdk: Optional[ClientSDKInfo] = None
    request: Optional[IngestRequest] = None
    contexts: Optional[ContextsSchema] = None
    user: Optional[EventUser] = None

    @field_validator("tags")
    @classmethod
    def prefer_dict(
        cls, v: Optional[KeyValueFormat]
    ) -> Optional[dict[str, Optional[str]]]:
        if isinstance(v, list):
            return {key: value for key, value in v if key is not None}
        return v


class EventIngestSchema(IngestIssueEvent):
    event_id: uuid.UUID


class EnvelopeHeaderSchema(Schema):
    event_id: uuid.UUID
    dsn: Optional[str] = None
    sdk: Optional[ClientSDKInfo] = None
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
    _items: list[tuple[ItemHeaderSchema, IngestIssueEvent]] = []

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
                    item = IngestIssueEvent(**data.pop(0))
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


class IssueEventSchema(IngestIssueEvent):
    """
    Event storage and interchange format
    Used in json view and celery interchange
    Don't use this for api intake
    """

    type: Literal[IssueEventType.DEFAULT] = IssueEventType.DEFAULT


class ErrorIssueEventSchema(IngestIssueEvent):
    type: Literal[IssueEventType.ERROR] = IssueEventType.ERROR


class CSPIssueEventSchema(IngestIssueEvent):
    type: Literal[IssueEventType.CSP] = IssueEventType.CSP
    csp: CSPReportSchema


class InterchangeIssueEvent(Schema):
    """Normalized wrapper around issue event. Event should not contain repeat information."""

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    project_id: int
    organization_id: int
    received: datetime = Field(default_factory=now)
    payload: Union[
        IssueEventSchema, ErrorIssueEventSchema, CSPIssueEventSchema
    ] = Field(discriminator="type")
