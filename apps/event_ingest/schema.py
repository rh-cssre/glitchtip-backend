import logging
import typing
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs

from django.conf import settings
from django.utils.timezone import now
from ninja import Field
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    JsonValue,
    RootModel,
    ValidationError,
    WrapValidator,
    field_validator,
    model_validator,
)

from apps.issue_events.constants import IssueEventType
from apps.shared.schema.error import EventProcessingError

from ..shared.schema.base import LaxIngestSchema
from ..shared.schema.contexts import Contexts
from ..shared.schema.event import (
    BaseIssueEvent,
    BaseRequest,
    EventBreadcrumb,
    ListKeyValue,
)
from ..shared.schema.exception import (
    EventException,
    ValueEventException,
)
from ..shared.schema.user import EventUser
from ..shared.schema.utils import report_error_on_fail

logger = logging.getLogger(__name__)


CoercedStr = Annotated[
    str, BeforeValidator(lambda v: str(v) if isinstance(v, (bool, list)) else v)
]
"""
Coerced Str that will coerce bool/list to str when found
"""


def coerce_list(v: Any) -> Any:
    """Wrap non-list dict into list: {"a": 1} to [{"a": 1}]"""
    return v if not isinstance(v, dict) else [v]


def truncate_str(v: Any) -> Any:
    """
    Truncates a string if its max_length is set.
    """
    if isinstance(v, str):
        return v[:8192]
    return v


def truncate_on_error(v: Any, handler) -> str:
    """
    A WrapValidator that attempts to validate a string and, upon catching
    a 'string_too_long' validation error, truncates the string and
    returns it instead of raising the error.
    """
    try:
        # Attempt to run the standard validation
        return handler(v)
    except ValidationError as e:
        # Check if the error is the specific one we want to handle
        error = e.errors()[0]
        if error["type"] == "string_too_long" and "ctx" in error:
            # Extract max_length from the error's context
            max_length = error["ctx"]["max_length"]
            # Return the truncated string
            return v[:max_length]

        # If it's a different validation error, re-raise it
        raise


TruncatedStr = Annotated[str, WrapValidator(truncate_on_error)]


class EventMessage(LaxIngestSchema):
    formatted: TruncatedStr = Field(max_length=8192, default="")
    message: str | None = None
    params: list[CoercedStr] | dict[str, str] | None = None

    @model_validator(mode="after")
    def set_formatted(self) -> "EventMessage":
        """
        When the EventMessage formatted string is not set,
        attempt to set it based on message and params interpolation
        """
        if not self.formatted and self.message:
            params = self.params
            if isinstance(params, list) and params:
                formatted_params = tuple(
                    int(p) if isinstance(p, str) and p.isdigit() else p for p in params
                )
                try:
                    self.formatted = self.message % tuple(formatted_params)[:8192]
                except TypeError:
                    pass
            elif isinstance(params, dict):
                self.formatted = self.message.format(**params)[:8192]
        return self


class EventTemplate(LaxIngestSchema):
    lineno: int
    abs_path: str | None = None
    filename: str
    context_line: str
    pre_context: list[str] | None = None
    post_context: list[str] | None = None


# Important, for some reason using Schema will cause the DebugImage union not to work
class SourceMapImage(BaseModel):
    type: Literal["sourcemap"]
    code_file: str
    debug_id: uuid.UUID


# Important, for some reason using Schema will cause the DebugImage union not to work
class OtherDebugImage(BaseModel):
    type: str


DebugImage = Annotated[SourceMapImage, Field(discriminator="type")] | OtherDebugImage


class DebugMeta(LaxIngestSchema):
    images: list[DebugImage]


class ValueEventBreadcrumb(LaxIngestSchema):
    values: list[EventBreadcrumb]


class ClientSDKPackage(LaxIngestSchema):
    name: str | None = None
    version: str | None = None


class ClientSDKInfo(LaxIngestSchema):
    integrations: list[str | None] | None = None
    name: str | None
    packages: list[ClientSDKPackage] | None = None
    version: str | None

    @field_validator("packages", mode="before")
    def name_must_contain_space(cls, v: Any) -> Any:
        return coerce_list(v)


class RequestHeaders(LaxIngestSchema):
    content_type: str | None


class RequestEnv(LaxIngestSchema):
    remote_addr: str | None


QueryString = str | ListKeyValue | dict[str, str | dict[str, Any] | None]
"""Raw URL querystring, list, or dict"""
KeyValueFormat = list[list[str | None]] | dict[str, CoercedStr | None]
"""
key-values in list or dict format. Example {browser: firefox} or [[browser, firefox]]
"""


class IngestRequest(BaseRequest):
    headers: KeyValueFormat | None = None
    query_string: QueryString | None = None

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
        cls, v: QueryString | KeyValueFormat | None
    ) -> ListKeyValue | None:
        """Store all querystring, header formats in a list format"""
        result: ListKeyValue | None = None
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


class IngestEventException(EventException):
    @model_validator(mode="after")
    def check_type_value(self):
        if self.type is None and self.value is None:
            return None
        return self


class IngestValueEventException(ValueEventException):
    values: list[IngestEventException]  # type: ignore[assignment]

    @field_validator("values")
    @classmethod
    def strip_null(cls, v: list[EventException]) -> list[EventException]:
        return [e for e in v if e is not None]


class WebIngestIssueEvent(BaseIssueEvent):
    """Heavy validation and normalization for web API ingest"""

    event_id: uuid.UUID | None = None
    timestamp: Annotated[
        datetime | None | dict, WrapValidator(report_error_on_fail)
    ] = Field(default_factory=now)
    level: str | None = "error"
    logentry: EventMessage | None = None
    logger: str | None = None
    transaction: str | None = Field(
        validation_alias=AliasChoices("transaction", "culprit"), default=None
    )
    server_name: str | None = None
    release: str | None = None
    dist: str | None = None
    tags: KeyValueFormat | None = None
    environment: str | None = None
    modules: dict[str, str | None] | None = None
    extra: dict[str, Any] | None = None
    fingerprint: list[str | None] | None = None
    errors: list[Any] | None = None

    exception: IngestValueEventException | None = None
    message: str | EventMessage | None = None
    template: EventTemplate | None = None

    breadcrumbs: Annotated[
        ValueEventBreadcrumb | None, WrapValidator(report_error_on_fail)
    ] = None
    sdk: Annotated[ClientSDKInfo | None, WrapValidator(report_error_on_fail)] = None
    request: Annotated[IngestRequest | None, WrapValidator(report_error_on_fail)] = None
    contexts: Annotated[Contexts | None, WrapValidator(report_error_on_fail)] = None
    user: Annotated[EventUser | None, WrapValidator(report_error_on_fail)] = None
    debug_meta: Annotated[DebugMeta | None, WrapValidator(report_error_on_fail)] = None

    @model_validator(mode="after")
    def process_validation_markers(self) -> "WebIngestIssueEvent":
        """
        Iterates through fields after initial validation, checks for
        ValidationErrorMarker instances, populates the `errors` list,
        and sets the invalid fields to None.
        """
        collected_errors: list[EventProcessingError] = []

        # Iterate over the model's attributes.
        for field_name, field_value in self.__dict__.items():
            if isinstance(field_value, dict) and "__validation_error__" in field_value:
                collected_errors.append(field_value["__validation_error__"])
                # Nullify the field that contained the marker.
                setattr(self, field_name, None)

        if collected_errors:
            if self.errors is None:
                self.errors = []
            self.errors.extend(collected_errors)

        # It would be better to allow null in DB timestamps, but it's too much effort for ~0 benefit.
        if self.timestamp is None:
            self.timestamp = now()

        return self

    @field_validator("tags")
    @classmethod
    def prefer_dict(cls, v: KeyValueFormat | None) -> dict[str, str | None] | None:
        if isinstance(v, list):
            return {key: value for key, value in v if key is not None}
        return v

    @field_validator("exception", "breadcrumbs", mode="before")
    @classmethod
    def normalize_values_format(cls, v: Any) -> dict | None:
        """
        Checks if the incoming exception/etc data is a direct list.
        If it is, it wraps it in the standard {"values": [...]} object format.
        """
        if isinstance(v, list):
            return {"values": v} if v else None
        elif isinstance(v, dict) and "values" in v:
            return v if v["values"] else None
        return v


class EventIngestSchema(WebIngestIssueEvent):
    event_id: uuid.UUID  # type: ignore[assignment]


class TransactionEventSchema(LaxIngestSchema):
    type: Literal["transaction"] = "transaction"
    contexts: JsonValue
    measurements: JsonValue | None = None
    start_timestamp: datetime
    timestamp: datetime
    transaction: str

    # # SentrySDKEventSerializer
    breadcrumbs: JsonValue | None = None
    fingerprint: list[str] | None = None
    tags: KeyValueFormat | None = None
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    extra: JsonValue | None = None
    request: IngestRequest | None = None
    server_name: str | None = None
    sdk: ClientSDKInfo | None = None
    platform: str | None
    release: str | None = None
    environment: str | None = None
    _meta: JsonValue | None

    @field_validator("start_timestamp")
    @classmethod
    def ensure_time_is_recent(cls, v: datetime) -> datetime:
        """Validator to ensure the datetime is recent"""
        minimum_date = now() - timedelta(
            days=settings.GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS
        )
        if v < minimum_date:
            raise ValueError("Event time too old.")
        return v


class EnvelopeHeaderSchema(LaxIngestSchema):
    event_id: uuid.UUID | None = None
    dsn: str | None = None
    sdk: ClientSDKInfo | None = None
    sent_at: datetime = Field(default_factory=now)


SupportedItemType = Literal["transaction", "event"]
IgnoredItemType = Literal[
    "log",
    "session",
    "sessions",
    "client_report",
    "attachment",
    "user_report",
    "check_in",
    "profile",
    "replay_recording",
    "replay_event",
    "span",
]
SUPPORTED_ITEMS = typing.get_args(SupportedItemType)


class ItemHeaderSchema(LaxIngestSchema):
    content_type: str | None = None
    type: SupportedItemType | IgnoredItemType
    length: int | None = None


class EnvelopeSchema(RootModel[list[dict[str, Any]]]):
    root: list[dict[str, Any]]
    _header: EnvelopeHeaderSchema
    _items: list[
        tuple[ItemHeaderSchema, WebIngestIssueEvent | TransactionEventSchema]
    ] = []


class CSPReportSchema(LaxIngestSchema):
    """
    https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy-Report-Only#violation_report_syntax
    """

    blocked_uri: str = Field(alias="blocked-uri")
    disposition: Literal["enforce", "report"] = Field(alias="disposition")
    document_uri: str = Field(alias="document-uri")
    effective_directive: str = Field(alias="effective-directive")
    original_policy: str | None = Field(alias="original-policy")
    script_sample: str | None = Field(alias="script-sample", default=None)
    status_code: int | None = Field(alias="status-code")
    line_number: int | None = None
    column_number: int | None = None


class SecuritySchema(LaxIngestSchema):
    csp_report: CSPReportSchema = Field(alias="csp-report")


## Normalized Interchange Issue Events


class CeleryIssueEvent(BaseIssueEvent):
    """
    Lightweight schema for Celery - assumes data already validated
    All fields used by process_event.py with simple types
    """

    event_id: uuid.UUID
    timestamp: datetime | None = None
    level: str | None = "error"

    # Fields accessed by process_event.py
    contexts: Contexts | None = None
    request: IngestRequest | None = None
    tags: dict[str, str | None] | None = None
    user: EventUser | None = None
    environment: str | None = None
    release: str | None = None
    server_name: str | None = None
    debug_meta: DebugMeta | None = None
    exception: IngestValueEventException | None = None
    message: str | EventMessage | None = None
    logentry: EventMessage | None = None
    transaction: str | None = None
    fingerprint: list[str | None] | None = None
    type: str | None = None

    # CSP-specific field
    csp: CSPReportSchema | None = None


class CeleryDefaultIssueEvent(CeleryIssueEvent):
    type: Literal[IssueEventType.DEFAULT] = IssueEventType.DEFAULT


class CeleryErrorIssueEvent(CeleryIssueEvent):
    type: Literal[IssueEventType.ERROR] = IssueEventType.ERROR


class CeleryCSPIssueEvent(CeleryIssueEvent):
    type: Literal[IssueEventType.CSP] = IssueEventType.CSP


class IssueEventSchema(WebIngestIssueEvent):
    """
    Event storage and interchange format
    Used in json view and celery interchange
    Don't use this for api intake
    """

    type: Literal[IssueEventType.DEFAULT] = IssueEventType.DEFAULT


class ErrorIssueEventSchema(WebIngestIssueEvent):
    type: Literal[IssueEventType.ERROR] = IssueEventType.ERROR


class CSPIssueEventSchema(WebIngestIssueEvent):
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)  # type: ignore[assignment]
    type: Literal[IssueEventType.CSP] = IssueEventType.CSP
    csp: CSPReportSchema


class InterchangeEvent(LaxIngestSchema):
    """Normalized wrapper around issue event. Event should not contain repeat information."""

    project_id: int
    organization_id: int
    received: datetime
    payload: (
        IssueEventSchema
        | ErrorIssueEventSchema
        | CSPIssueEventSchema
        | TransactionEventSchema
    ) = Field(discriminator="type")


class IssueTaskMessage(InterchangeEvent):
    payload: IssueEventSchema | ErrorIssueEventSchema | CSPIssueEventSchema = Field(
        discriminator="type"
    )


class InterchangeTransactionEvent(InterchangeEvent):
    payload: TransactionEventSchema
