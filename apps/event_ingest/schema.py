import logging
import typing
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs

from django.conf import settings
from django.utils.timezone import now
from ninja import Field
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
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
from ..shared.schema.threads import ValueEventThread
from ..shared.schema.user import EventUser
from ..shared.schema.utils import report_error_on_fail

logger = logging.getLogger(__name__)


CoercedStr = Annotated[
    str,
    BeforeValidator(lambda v: v if v is None or isinstance(v, str) else str(v)),
]
"""
Coerced Str that will coerce any non-string, non-None value to str.
None is passed through so that `CoercedStr | None` union types still
resolve None correctly.
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
    params: list[CoercedStr | None] | dict[str, CoercedStr | None] | None = None

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
class JvmDebugImage(BaseModel):
    type: Literal["jvm"]
    debug_id: uuid.UUID


# Important, for some reason using Schema will cause the DebugImage union not to work
class NativeDebugImage(BaseModel):
    type: Literal["macho", "elf", "pe", "pe_dotnet", "wasm"]
    debug_id: uuid.UUID | None = None
    debug_checksum: str | None = None
    image_addr: str | None = None
    image_size: int | None = None
    code_file: str | None = None
    code_id: str | None = None


# Important, for some reason using Schema will cause the DebugImage union not to work
class OtherDebugImage(BaseModel):
    type: str


DebugImage = (
    Annotated[
        SourceMapImage | JvmDebugImage | NativeDebugImage,
        Field(discriminator="type"),
    ]
    | OtherDebugImage
)

# Types we intentionally ignore (no processing needed).
# Anything not in this set AND not handled by a typed model above is unknown.
KNOWN_IGNORED_IMAGE_TYPES = frozenset(
    {
        "proguard",  # Handled via ProguardMapper, not debug images
    }
)


class DebugMeta(LaxIngestSchema):
    images: list[DebugImage]

    @model_validator(mode="after")
    def _warn_unknown_image_types(self):
        for image in self.images:
            if isinstance(image, OtherDebugImage):
                if image.type not in KNOWN_IGNORED_IMAGE_TYPES:
                    logger.warning("Unknown debug image type: %s", image.type)
        return self


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
KeyValueFormat = list[list[CoercedStr | None]] | dict[str, CoercedStr | None]
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
    fingerprint: Annotated[
        list[str | None] | None,
        BeforeValidator(
            lambda v: (
                [str(item) if not isinstance(item, str) else item for item in v]
                if isinstance(v, list)
                else v
            )
        ),
    ] = None
    errors: list[Any] | None = None

    exception: Annotated[
        IngestValueEventException | None, WrapValidator(report_error_on_fail)
    ] = None
    threads: Annotated[ValueEventThread | None, WrapValidator(report_error_on_fail)] = (
        None
    )
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


class SpanSchema(LaxIngestSchema):
    """Schema for individual spans within a transaction."""

    span_id: str = ""
    op: str = ""
    description: str | None = None
    start_timestamp: datetime | None = None
    timestamp: datetime | None = None
    status: str | None = None
    data: JsonValue | None = None


class TransactionEventSchema(LaxIngestSchema):
    type: Literal["transaction"] = "transaction"
    contexts: JsonValue
    measurements: JsonValue | None = None
    start_timestamp: datetime
    timestamp: datetime
    transaction: str
    spans: list[SpanSchema] | None = None

    # # SentrySDKEventSerializer
    breadcrumbs: JsonValue | None = None
    fingerprint: list[str] | None = None
    tags: KeyValueFormat | None = None
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    extra: JsonValue | None = None
    request: IngestRequest | None = None
    server_name: str | None = None
    sdk: ClientSDKInfo | None = None
    platform: str | None = Field(default="other")
    release: str | None = None
    environment: str | None = None
    _meta: JsonValue | None

    @field_validator("start_timestamp")
    @classmethod
    def ensure_time_is_recent(cls, v: datetime) -> datetime:
        """Reject out-of-retention / far-future timestamps; normalize to UTC.

        Cold-storage compaction is idempotent and seals by ingestion time,
        so it does not need a tight freshness window — only a loose guard
        so a broken-clock client can't push garbage years out (which would
        pollute hour bucketing) or backfill beyond retention.
        """
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        current = now()
        if v < current - timedelta(days=settings.GLITCHTIP_TRANSACTION_RETENTION_DAYS):
            raise ValueError("Event time too old.")
        if v > current + settings.GLITCHTIP_TRANSACTION_FUTURE_SKEW:
            raise ValueError("Event time in the future.")
        return v


class EnvelopeHeaderSchema(LaxIngestSchema):
    event_id: uuid.UUID | None = None
    dsn: str | None = None
    sdk: ClientSDKInfo | None = None
    sent_at: datetime = Field(default_factory=now)

    @field_validator("event_id", mode="before")
    def empty_event_id_to_none(cls, v: Any) -> Any:
        # event_id is optional in the envelope spec. Some SDKs (e.g.
        # sentry-go on client_report envelopes, which have no associated
        # event) send an empty string rather than omitting the key. The
        # field default only applies when the key is absent, so coerce a
        # blank value to None instead of failing UUID parsing.
        if isinstance(v, str) and not v.strip():
            return None
        return v


SupportedItemType = Literal[
    "transaction", "event", "user_report", "feedback", "log", "otel_log"
]
IgnoredItemType = Literal[
    "session",
    "sessions",
    "client_report",
    "attachment",
    "check_in",
    "profile",
    "replay_recording",
    "replay_event",
    "replay_video",
    "span",
    "profile_chunk",
    "trace_metric",
]
SUPPORTED_ITEMS = typing.get_args(SupportedItemType)


class ItemHeaderSchema(LaxIngestSchema):
    content_type: str | None = None
    type: SupportedItemType | IgnoredItemType
    length: int | None = None
    attachment_type: str | None = None
    filename: str | None = None


class FeedbackContext(LaxIngestSchema):
    message: str = ""
    name: str = ""
    contact_email: str = ""
    associated_event_id: str | None = None


class FeedbackPayload(LaxIngestSchema):
    """New SDK (v8+) feedback envelope payload"""

    event_id: uuid.UUID | None = None
    contexts: dict[str, Any]

    def to_user_report_data(self) -> dict[str, Any]:
        fb = FeedbackContext.model_validate((self.contexts.get("feedback") or {}))
        return {
            "event_id": fb.associated_event_id,
            "name": fb.name[:128],
            "email": fb.contact_email[:254],
            "comments": fb.message,
        }


class UserReportPayload(LaxIngestSchema):
    """Old SDK user_report envelope payload"""

    event_id: uuid.UUID | None = None
    name: str = ""
    email: str = ""
    comments: str = ""

    def to_user_report_data(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id) if self.event_id else None,
            "name": self.name[:128],
            "email": self.email[:254],
            "comments": self.comments,
        }


class UserReportTaskMessage(LaxIngestSchema):
    """Interchange message for user report/feedback processing"""

    project_id: int
    organization_id: int
    event_id: str | None = None
    name: str = ""
    email: str = ""
    comments: str = ""


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


class TaskIssueEvent(BaseIssueEvent):
    """
    Lightweight schema for task processing - assumes data already validated
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
    threads: ValueEventThread | None = None
    message: str | EventMessage | None = None
    logentry: EventMessage | None = None
    transaction: str | None = None
    fingerprint: list[str | None] | None = None
    type: str | None = None

    # CSP-specific field
    csp: CSPReportSchema | None = None


class TaskDefaultIssueEvent(TaskIssueEvent):
    type: Literal[IssueEventType.DEFAULT] = IssueEventType.DEFAULT


class TaskErrorIssueEvent(TaskIssueEvent):
    type: Literal[IssueEventType.ERROR] = IssueEventType.ERROR


class TaskCSPIssueEvent(TaskIssueEvent):
    type: Literal[IssueEventType.CSP] = IssueEventType.CSP


class IssueEventSchema(WebIngestIssueEvent):
    """
    Event storage and interchange format
    Used in json view and task interchange
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
    update_first_event: bool = False
    uuid: str | None = None
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


# Log Envelope Schemas


class LogItemSchema(BaseModel):
    """Schema for individual log items from sentry-sdk log envelope.

    Inherits from BaseModel (not ninja Schema) so that extra="allow"
    works correctly — ninja's DjangoGetter wrapper strips unknown fields.

    The SDK sends logs with a nested ``attributes`` dict of typed values::

        {"attributes": {"sentry.service": {"value": "web", "type": "string"}, ...}}

    The ``normalize_attributes`` validator flattens this into top-level fields
    (service, environment, host, severity_number) and flat extra keys stored
    in the LogEvent.data JSONB column by process_log_events.
    """

    model_config = ConfigDict(coerce_numbers_to_str=True, extra="allow")

    timestamp: float  # Unix timestamp with fractional seconds
    level: str  # trace, debug, info, warn, error, fatal
    body: str  # The log message
    service: str = ""
    environment: str = ""
    host: str = ""
    trace_id: str | None = None
    span_id: str | None = None  # OTel span ID (16 hex chars)
    severity_number: int | None = None  # OTel severity number (1-24)

    # Maps SDK attribute keys to top-level schema fields.
    _ATTRIBUTE_FIELD_MAP: typing.ClassVar[dict[str, str]] = {
        "sentry.service": "service",
        "service.name": "service",
        "sentry.environment": "environment",
        "deployment.environment.name": "environment",
        "host.name": "host",
        "sentry.severity_number": "severity_number",
    }
    # Attributes consumed during normalization (not stored in data).
    _ATTRIBUTE_SKIP: typing.ClassVar[set[str]] = {
        "sentry.severity_text",
        "sentry.message.template",
    }

    @model_validator(mode="before")
    @classmethod
    def normalize_attributes(cls, values: Any) -> Any:
        """Flatten the SDK ``attributes`` dict into top-level fields.

        Extracts known Sentry/OTel attribute keys into their corresponding
        schema fields, and promotes remaining attributes as flat extras so
        they end up in the JSONB ``data`` column.

        Also coerces ISO-8601 ``timestamp`` strings to Unix float seconds:
        sentry-dart <= 9.6.0 sends ISO strings (fixed in later versions, but
        shipped clients remain in the wild).
        """
        if not isinstance(values, dict):
            return values

        # Coerce ISO-8601 timestamp strings to Unix float seconds.
        ts = values.get("timestamp")
        if isinstance(ts, str):
            try:
                values["timestamp"] = datetime.fromisoformat(
                    ts.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                pass  # let pydantic surface the float_parsing error

        attributes = values.pop("attributes", None)
        if not attributes or not isinstance(attributes, dict):
            return values

        for attr_key, attr_val in attributes.items():
            # Extract plain value from typed {"value": ..., "type": ...} format
            if isinstance(attr_val, dict) and "value" in attr_val:
                plain_value = attr_val["value"]
            else:
                plain_value = attr_val

            # Map known attributes to schema fields
            if field_name := cls._ATTRIBUTE_FIELD_MAP.get(attr_key):
                # Only set if the field hasn't been explicitly provided
                if not values.get(field_name):
                    values[field_name] = plain_value
            elif attr_key not in cls._ATTRIBUTE_SKIP:
                # Promote as top-level extra (goes into JSONB data)
                values[attr_key] = plain_value

        return values

    @field_validator("level")
    @classmethod
    def normalize_level(cls, v: str) -> str:
        """Normalize level to lowercase."""
        return v.lower() if v else "info"


class LogEnvelopePayload(LaxIngestSchema):
    """Schema for log envelope payload containing multiple log items."""

    items: list[LogItemSchema]


# OTel Log Envelope Schemas
# https://opentelemetry.io/docs/specs/otel/logs/data-model/

# Severity number ranges per OTel spec
_OTEL_SEVERITY_TO_LEVEL = {
    range(1, 5): "trace",
    range(5, 9): "debug",
    range(9, 13): "info",
    range(13, 17): "warn",
    range(17, 21): "error",
    range(21, 25): "fatal",
}


def _otel_severity_to_level(
    severity_number: int | None, severity_text: str | None
) -> str:
    """Convert OTel severity_number/severity_text to a log level string."""
    if severity_number is not None:
        for r, level in _OTEL_SEVERITY_TO_LEVEL.items():
            if severity_number in r:
                return level
    if severity_text:
        return severity_text.lower()
    return "info"


def _extract_otel_value(val: dict | str | int | float | bool | None) -> Any:
    """Extract a plain value from an OTel AnyValue wrapper.

    OTel uses typed wrappers like ``{"string_value": "..."}`` or
    ``{"int_value": 42}``.  Returns the unwrapped value.
    """
    if not isinstance(val, dict):
        return val
    # snake_case keys come from protobuf decoding; camelCase from OTLP/JSON.
    for suffix in (
        "string_value",
        "int_value",
        "double_value",
        "bool_value",
        "stringValue",
        "intValue",
        "doubleValue",
        "boolValue",
    ):
        if suffix in val:
            return val[suffix]
    return val


def otel_log_to_log_item(otel: dict) -> dict:
    """Convert a single OTel log record dict to a LogItemSchema-compatible dict."""
    # Timestamp: nanoseconds (string or int) → seconds (float). Fall back to the
    # observed time when the record carries no original timestamp (valid per the
    # OTel data model), so such records don't collapse to the 1970 epoch.
    time_unix_nano = (
        otel.get("time_unix_nano")
        or otel.get("timeUnixNano")
        or otel.get("observed_time_unix_nano")
        or otel.get("observedTimeUnixNano")
        or "0"
    )
    timestamp = int(time_unix_nano) / 1e9

    # Body: {"string_value": "..."} or plain string
    raw_body = otel.get("body", "")
    body = (
        _extract_otel_value(raw_body) if isinstance(raw_body, dict) else str(raw_body)
    )

    # Severity
    severity_number = otel.get("severity_number") or otel.get("severityNumber")
    if isinstance(severity_number, str):
        severity_number = int(severity_number) if severity_number.isdigit() else None
    severity_text = otel.get("severity_text") or otel.get("severityText")
    level = _otel_severity_to_level(severity_number, severity_text)

    # Trace context
    trace_id = otel.get("trace_id") or otel.get("traceId")
    span_id = otel.get("span_id") or otel.get("spanId")

    # Attributes: list of {"key": k, "value": {...}} → dict for LogItemSchema
    otel_attrs = otel.get("attributes") or []
    attributes: dict[str, Any] = {}
    if isinstance(otel_attrs, list):
        for attr in otel_attrs:
            if isinstance(attr, dict) and "key" in attr:
                attributes[attr["key"]] = {
                    "value": _extract_otel_value(attr.get("value")),
                    "type": "string",
                }
    elif isinstance(otel_attrs, dict):
        # Already in dict form (some senders use this)
        for k, v in otel_attrs.items():
            attributes[k] = {"value": _extract_otel_value(v), "type": "string"}

    return {
        "timestamp": timestamp,
        "level": level,
        "body": body,
        "trace_id": trace_id,
        "span_id": span_id,
        "severity_number": severity_number,
        "attributes": attributes,
    }
