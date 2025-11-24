import uuid
from datetime import datetime

from ninja import Field, ModelSchema
from pydantic import RootModel

from apps.organizations_ext.schema import OrganizationSchema
from glitchtip.schema import CamelSchema

from .models import Project, ProjectKey


class NameSlugProjectSchema(CamelSchema, ModelSchema):
    class Meta:
        model = Project
        fields = [
            "name",
            "slug",
        ]


class ProjectIn(NameSlugProjectSchema):
    platform: str | None = None  # This shouldn't be needed, but is.
    event_throttle_rate: int | None = None  # This shouldn't be needed, but is.

    class Meta(NameSlugProjectSchema.Meta):
        model = Project
        fields = [
            "name",
            "slug",
            "platform",
            "event_throttle_rate",  # Not in Sentry OSS
            # "default_rules",
        ]


class ProjectSchema(NameSlugProjectSchema, ModelSchema):
    """
    A project is an organizational unit for GlitchTip events. It may contain
    DSN keys, be connected to exactly one organization, and provide user permissions
    through teams.
    """

    id: str
    avatar: dict[str, str | None] = {"avatarType": "", "avatarUuid": None}
    color: str = ""
    features: list = []
    has_access: bool = True
    is_bookmarked: bool = False
    is_internal: bool = False
    is_member: bool
    is_public: bool = False
    scrub_ip_addresses: bool = Field(serialization_alias="scrubIPAddresses")
    date_created: datetime
    platform: str | None = None

    class Meta(NameSlugProjectSchema.Meta):
        fields = [
            "first_event",
            "id",
            "name",
            "scrub_ip_addresses",
            "slug",
            "platform",
            "event_throttle_rate",  # Not in Sentry OSS
        ]

    @staticmethod
    def resolve_id(obj):
        return str(obj.id)

    @staticmethod
    def resolve_date_created(obj: Project):
        return obj.created


class KeyRateLimit(CamelSchema):
    window: int
    count: int


class ProjectKeyIn(CamelSchema, ModelSchema):
    name: str | None = None
    rate_limit: KeyRateLimit | None = None

    class Meta:
        model = ProjectKey
        fields = ["name"]


class ProjectKeyUpdate(ProjectKeyIn):
    rate_limit: KeyRateLimit | None = None

    class Meta(ProjectKeyIn.Meta):
        fields = ["name", "is_active"]


class ProjectKeySchema(ProjectKeyUpdate):
    """
    A project key (DSN) provides a public authentication string used for event
    ingestion.
    """

    date_created: datetime = Field(validation_alias="created")
    id: uuid.UUID = Field(validation_alias="public_key")
    dsn: dict[str, str]
    label: str | None = Field(validation_alias="name")
    public: uuid.UUID = Field(validation_alias="public_key")
    project_id: int = Field(validation_alias="project_id")

    class Meta(ProjectKeyUpdate.Meta):
        pass

    @staticmethod
    def resolve_dsn(obj):
        return {
            "public": obj.get_dsn(),
            "secret": obj.get_dsn(),  # Deprecated but required for @sentry/wizard
            "security": obj.get_dsn_security(),
        }

    @staticmethod
    def resolve_rate_limit(obj):
        if count := obj.rate_limit_count:
            return {"window": obj.rate_limit_window, "count": count}


class ProjectOrganizationSchema(ProjectSchema):
    organization: OrganizationSchema

    class Meta(ProjectSchema.Meta):
        pass


class ProjectWithKeysSchema(ProjectOrganizationSchema):
    keys: list[ProjectKeySchema] = Field(validation_alias="projectkey_set")


class StrKeyIntValue(RootModel):
    root: dict[str, int]
