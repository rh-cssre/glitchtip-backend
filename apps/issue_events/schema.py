from datetime import datetime
from typing import Annotated, Any, Literal

from django.conf import settings
from ninja import Field, ModelSchema, Schema
from pydantic import ConfigDict, computed_field

from apps.event_ingest.schema import CSPReportSchema
from apps.projects.models import Project
from apps.shared.schema.csp import CSPEntry
from apps.shared.schema.exception import EventException, ExceptionEntry
from apps.shared.schema.message import MessageEntry
from apps.users.models import User
from glitchtip.schema import CamelSchema

from ..shared.schema.contexts import Contexts
from ..shared.schema.event import (
    BaseIssueEvent,
    BaseRequest,
    EventBreadcrumb,
    ListKeyValue,
)
from ..shared.schema.user import EventUser
from .models import Comment, Issue, IssueEvent, UserReport
from .utils import get_entries, to_camel_with_lower_id


class ProjectReference(CamelSchema, ModelSchema):
    id: str

    model_config = ConfigDict(populate_by_name=True)

    class Meta:
        model = Project
        fields = ["platform", "slug", "name"]

    @staticmethod
    def resolve_id(obj: Project):
        return str(obj.id)


class IssueSchema(ModelSchema):
    id: str
    count: str
    type: str = Field(validation_alias="get_type_display")
    level: str = Field(validation_alias="get_level_display")
    status: str = Field(validation_alias="get_status_display")
    project: ProjectReference = Field(validation_alias="project")
    shortId: str = Field(validation_alias="short_id_display")
    numComments: int = Field(validation_alias="num_comments")
    stats: dict[str, list[list[float]]] | None = {"24h": []}
    share_id: int | None = None
    logger: str | None = None
    permalink: str | None = "Not implemented"
    status_details: dict[str, str] | None = {}
    subscription_details: str | None = None
    user_count: int | None = 0
    matching_event_id: str | None = Field(
        default=None, serialization_alias="matchingEventId"
    )
    firstSeen: datetime = Field(validation_alias="first_seen")
    lastSeen: datetime = Field(validation_alias="last_seen")

    @staticmethod
    def resolve_culprit(obj: Issue):
        return obj.culprit or ""

    @staticmethod
    def resolve_matching_event_id(obj: Issue, context):
        if event_id := context["request"].matching_event_id:
            return event_id.hex

    @staticmethod
    def resolve_permalink(obj: Issue, context):
        glitchtip_url = settings.GLITCHTIP_URL.geturl()
        project_slug = obj.project.slug
        return f"{glitchtip_url}/{project_slug}/issues/{obj.id}"

    model_config = ConfigDict(
        alias_generator=to_camel_with_lower_id,
        coerce_numbers_to_str=True,
        populate_by_name=True,
    )

    class Meta:
        model = Issue
        fields = [
            "title",
            "metadata",
            "culprit",
        ]


class IssueDetailSchema(IssueSchema):
    userReportCount: int = Field(validation_alias="user_report_count")


class APIEventBreadcrumb(EventBreadcrumb):
    """Slightly modified Breadcrumb for sentry api compatibility"""

    event_id: str | None = None


class BreadcrumbsEntry(Schema):
    type: Literal["breadcrumbs"]
    data: dict[Literal["values"], list[APIEventBreadcrumb]]


class Request(CamelSchema, BaseRequest):
    headers: ListKeyValue | None = None
    query_string: ListKeyValue | None = Field(default=None, serialization_alias="query")

    @computed_field
    @property
    def inferred_content_type(self) -> str | None:
        if self.headers:
            return next(
                (value for key, value in self.headers if key == "Content-Type"), None
            )
        return None


class RequestEntry(Schema):
    type: Literal["request"]
    data: Request


class IssueEventSchema(CamelSchema, ModelSchema, BaseIssueEvent):
    id: str = Field(validation_alias="id.hex")
    event_id: str
    project_id: int = Field(validation_alias="issue.project_id")
    group_id: str
    date_created: datetime
    date_received: datetime
    dist: str | None = None
    culprit: str | None = Field(validation_alias="transaction", default=None)
    packages: dict[str, str | None] | None = Field(
        validation_alias="data.modules", default=None
    )
    type: str = Field(validation_alias="get_type_display")
    message: str
    metadata: dict[str, str] = Field(default_factory=dict)
    tags: list[dict[str, str | None]] = []
    entries: list[
        Annotated[
            BreadcrumbsEntry | CSPEntry | ExceptionEntry | MessageEntry | RequestEntry,
            Field(..., discriminator="type"),
        ]
    ] = Field(default_factory=list)
    contexts: Contexts | None = None
    context: dict[str, Any] | None = None
    user: Any | None = None
    sdk: dict[str, Any] | None = None

    model_config = ConfigDict(populate_by_name=True)

    class Meta:
        model = IssueEvent
        fields = ["id", "type", "title"]

    @staticmethod
    def resolve_date_created(obj: IssueEvent):
        return obj.timestamp

    @staticmethod
    def resolve_date_received(obj: IssueEvent):
        return obj.received

    @staticmethod
    def resolve_contexts(obj: IssueEvent):
        return obj.data.get("contexts")

    @staticmethod
    def resolve_context(obj: IssueEvent):
        return obj.data.get("extra")

    @staticmethod
    def resolve_user(obj: IssueEvent):
        return obj.data.get("user")

    @staticmethod
    def resolve_sdk(obj: IssueEvent):
        return obj.data.get("sdk")

    @staticmethod
    def resolve_group_id(obj: IssueEvent):
        return str(obj.issue_id)

    @staticmethod
    def resolve_tags(obj: IssueEvent):
        return [{"key": tag[0], "value": tag[1]} for tag in obj.tags.items()]

    @staticmethod
    def resolve_entries(obj: IssueEvent):
        return get_entries(obj.data)


class UserReportSchema(CamelSchema, ModelSchema):
    event_id: str = Field(validation_alias="event_id.hex")
    event: dict[str, str]
    date_created: datetime
    user: str | None = None

    model_config = ConfigDict(populate_by_name=True)

    class Meta:
        model = UserReport
        fields = ["id", "name", "email", "comments"]

    @staticmethod
    def resolve_date_created(obj):
        return obj.created

    @staticmethod
    def resolve_event(obj):
        return {
            "eventId": obj.event_id.hex,
        }


# TODO: Sentry includes a full user object with its nested comments,
# so we should drop this schema once we create a full user schema
class CommentUserSchema(CamelSchema, ModelSchema):
    id: str

    model_config = ConfigDict(populate_by_name=True)

    class Meta:
        model = User
        fields = [
            "email",
        ]

    @staticmethod
    def resolve_id(obj: User):
        return str(obj.id)


class CommentSchema(CamelSchema, ModelSchema):
    data: dict[str, str]
    type: str | None = "note"
    date_created: datetime
    user: CommentUserSchema | None

    class Meta:
        model = Comment
        fields = ["id"]

    @staticmethod
    def resolve_data(obj: Comment):
        return {
            "text": obj.text,
        }

    @staticmethod
    def resolve_date_created(obj: Comment):
        return obj.created


class IssueEventDetailSchema(IssueEventSchema):
    user_report: UserReportSchema | None
    next_event_id: str | None = None
    previous_event_id: str | None = None

    @staticmethod
    def resolve_previous_event_id(obj):
        if event_id := obj.previous:
            return event_id.hex

    @staticmethod
    def resolve_next_event_id(obj):
        if event_id := obj.next:
            return event_id.hex


class IssueEventJsonSchema(ModelSchema, BaseIssueEvent):
    """
    Represents a more raw view of the event, built with open source (legacy) Sentry compatibility
    """

    event_id: str = Field(validation_alias="id.hex")
    timestamp: float = Field()
    x_datetime: datetime = Field(
        validation_alias="timestamp", serialization_alias="datetime"
    )
    breadcrumbs: Any | None = Field(validation_alias="data.breadcrumbs", default=None)
    project: int = Field(validation_alias="issue.project_id")
    level: str | None = Field(validation_alias="get_level_display")
    exception: Any | None = Field(validation_alias="data.exception", default=None)
    modules: dict[str, str] | None = Field(
        validation_alias="data.modules", default_factory=dict
    )
    contexts: dict | None = Field(validation_alias="data.contexts", default=None)
    sdk: dict | None = Field(validation_alias="data.sdk", default_factory=dict)
    type: str | None = Field(validation_alias="get_type_display")
    request: Any | None = Field(validation_alias="data.request", default=None)
    environment: str | None = Field(validation_alias="data.environment", default=None)
    extra: dict[str, Any] | None = Field(validation_alias="data.extra", default=None)
    user: EventUser | None = Field(validation_alias="data.user", default=None)

    class Meta:
        model = IssueEvent
        fields = ["title", "transaction", "tags", "hashes"]

    @staticmethod
    def resolve_timestamp(obj):
        return obj.timestamp.timestamp()


class IssueEventDataSchema(Schema):
    """IssueEvent model data json schema"""

    metadata: dict[str, Any] | None = None
    breadcrumbs: list[EventBreadcrumb] | None = None
    exception: list[EventException] | None = None


class CSPIssueEventDataSchema(IssueEventDataSchema):
    csp: CSPReportSchema


class IssueTagTopValue(CamelSchema):
    name: str
    value: str
    count: int
    key: str


class IssueTagSchema(CamelSchema):
    top_values: list[IssueTagTopValue]
    unique_values: int
    key: str
    name: str
    total_values: int


class IssueHashSchema(CamelSchema):
    id: str
    latest_event: IssueEventSchema | None

    @staticmethod
    def resolve_id(obj):
        return obj.value.hex


class StatsDetailSchema(Schema):
    """Represents the 24-hour statistics block."""

    stats_24h: list[list[int]] | None = Field(default=None, alias="24h")
    stats_14d: list[list[int]] | None = Field(default=None, alias="14d")

    model_config = ConfigDict(
        validate_by_alias=False,
        populate_by_name=True,
    )


class IssueStatsResponse(CamelSchema):
    """Defines the structure for a single issue's statistics in the response."""

    id: str
    count: str
    user_count: int
    first_seen: str
    last_seen: str
    is_unhandled: bool
    stats: StatsDetailSchema
