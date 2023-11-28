from datetime import datetime
from typing import Any, Literal, Optional, Union

from ninja import Field, ModelSchema, Schema

from apps.event_ingest.schema import CSPReportSchema, EventException
from glitchtip.api.schema import CamelSchema
from sentry.interfaces.stacktrace import get_context

from ..common_event_schema import EventBreadcrumb
from .constants import IssueEventType
from .models import Issue, IssueEvent


class IssueSchema(CamelSchema, ModelSchema):
    class Config:
        model = Issue
        model_fields = ["id", "title", "metadata"]
        populate_by_name = True


class ExceptionEntryData(Schema):
    values: dict
    exc_omitted: None = None
    has_system_frames: bool


class ExceptionEntry(Schema):
    type: Literal["exception"]
    data: dict


class MessageEntry(Schema):
    type: Literal["message"]
    data: dict


class APIEventBreadcrumb(EventBreadcrumb):
    """Slightly modified Breadcrumb for sentry api compatibility"""

    event_id: None = None


class BreadcrumbsEntry(Schema):
    type: Literal["breadcrumbs"]
    data: dict[Literal["values"], list[APIEventBreadcrumb]]


class IssueEventSchema(CamelSchema, ModelSchema):
    id: str = Field(validation_alias="id.hex")
    event_id: str
    project_id: int = Field(validation_alias="issue.project_id")
    group_id: int = Field(validation_alias="issue_id")
    date_created: datetime = Field(validation_alias="timestamp")
    date_received: datetime = Field(validation_alias="received")
    dist: Optional[str] = None
    culprit: Optional[str] = Field(validation_alias="transaction", default=None)
    packages: Optional[dict[str, Optional[str]]] = Field(
        validation_alias="data.modules", default=None
    )
    platform: Optional[str] = Field(validation_alias="data.platform", default=None)
    type: str = Field(validation_alias="get_type_display")
    message: str
    metadata: dict[str, str] = Field(default_factory=dict)
    tags: list[dict[str, Optional[str]]] = []
    entries: list[Union[BreadcrumbsEntry, ExceptionEntry, MessageEntry]] = Field(
        discriminator="type", default_factory=list
    )

    class Config:
        model = IssueEvent
        model_fields = ["id", "type", "title"]
        populate_by_name = True

    @staticmethod
    def resolve_tags(obj: IssueEvent):
        return [{"key": tag[0], "value": tag[1]} for tag in obj.tags.items()]

    @staticmethod
    def resolve_entries(obj: IssueEvent):
        entries = []
        data = obj.data
        if exception := data.get("exception"):
            exception = {"values": exception, "hasSystemFrames": False}
            # https://gitlab.com/glitchtip/sentry-open-source/sentry/-/blob/master/src/sentry/interfaces/stacktrace.py#L487
            # if any frame is "in_app" set this to True
            for value in exception["values"]:
                if (
                    value.get("stacktrace", None) is not None
                    and "frames" in value["stacktrace"]
                ):
                    for frame in value["stacktrace"]["frames"]:
                        if frame.get("in_app") is True:
                            exception["hasSystemFrames"] = True
                        if "in_app" in frame:
                            frame["inApp"] = frame.pop("in_app")
                        if "abs_path" in frame:
                            frame["absPath"] = frame.pop("abs_path")
                        if "colno" in frame:
                            frame["colNo"] = frame.pop("colno")
                        if "lineno" in frame:
                            frame["lineNo"] = frame.pop("lineno")
                            pre_context = frame.pop("pre_context", None)
                            post_context = frame.pop("post_context", None)
                            frame["context"] = get_context(
                                frame["lineNo"],
                                frame.get("context_line"),
                                pre_context,
                                post_context,
                            )

            entries.append({"type": "exception", "data": exception})

        if breadcrumbs := data.get("breadcrumbs"):
            entries.append({"type": "breadcrumbs", "data": {"values": breadcrumbs}})

        if logentry := data.get("logentry"):
            entries.append({"type": "message", "data": logentry})
        elif message := data.get("message"):
            entries.append({"type": "message", "data": {"formatted": message}})

        if request := data.get("request"):
            request["inferredContentType"] = request.pop("inferred_content_type", None)
            entries.append({"type": "request", "data": request})

        if csp := data.get("csp"):
            entries.append({"type": IssueEventType.CSP.label, "data": csp})
        return entries


class IssueEventDetailSchema(IssueEventSchema):
    user_report: list = []  # TODO
    next_event_id: Optional[str] = None
    previous_event_id: Optional[str] = None

    @staticmethod
    def resolve_previous_event_id(obj):
        if event_id := obj.previous:
            return event_id.hex

    @staticmethod
    def resolve_next_event_id(obj):
        if event_id := obj.next:
            return event_id.hex


class IssueEventJsonSchema(Schema):
    """
    Represents a more raw view of the event, built with open source (legacy) Sentry compatibility
    """

    event_id: str = Field(validation_alias="id.hex")
    timestamp: float = Field()
    x_datetime: datetime = Field(
        validation_alias="timestamp", serialization_alias="datetime"
    )
    breadcrumbs: Optional[Any] = Field(
        validation_alias="data.breadcrumbs", default=None
    )

    @staticmethod
    def resolve_timestamp(obj):
        return obj.timestamp.timestamp()


class IssueEventDataSchema(Schema):
    """IssueEvent model data json schema"""

    metadata: Optional[dict[str, Any]] = None
    breadcrumbs: Optional[list[EventBreadcrumb]] = None
    exception: Optional[list[EventException]] = None


class CSPIssueEventDataSchema(IssueEventDataSchema):
    csp: CSPReportSchema
