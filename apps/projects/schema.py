import uuid
from datetime import datetime
from typing import Optional

from ninja import Field, ModelSchema

from apps.organizations_ext.schemas import OrganizationSchema
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
    platform: Optional[str] = None  # This shouldn't be needed, but is.

    class Meta(NameSlugProjectSchema.Meta):
        model = Project
        fields = [
            "name",
            "slug",
            "platform",
            # "default_rules",
        ]


class ProjectSchema(NameSlugProjectSchema):
    avatar: dict[str, Optional[str]] = {"avatarType": "", "avatarUuid": None}
    color: str = ""
    features: list = []
    has_access: bool = True
    is_bookmarked: bool = False
    is_internal: bool = False
    is_member: bool
    is_public: bool = False
    scrub_ip_addresses: bool = Field(serialization_alias="scrubIPAddresses")
    created: datetime = Field(serialization_alias="dateCreated")
    platform: Optional[str] = None

    class Meta:
        model = Project
        fields = [
            "first_event",
            "id",
            "name",
            "scrub_ip_addresses",
            "slug",
            "created",
            "platform",
            "event_throttle_rate",  # Not in Sentry OSS
        ]

    class Config(CamelSchema.Config):
        pass


class ProjectKeySchema(CamelSchema, ModelSchema):
    date_created: datetime = Field(validation_alias="created")
    id: uuid.UUID = Field(validation_alias="public_key")
    dsn: dict[str, str]
    public: uuid.UUID = Field(validation_alias="public_key")
    project_id: int = Field(validation_alias="project_id")

    class Meta:
        model = ProjectKey
        fields = ["label"]

    @staticmethod
    def resolve_dsn(obj):
        return {
            "public": obj.get_dsn(),
            "secret": obj.get_dsn(),  # Deprecated but required for @sentry/wizard
            "security": obj.get_dsn_security(),
        }


class ProjectOrganizationSchema(ProjectSchema):
    organization: OrganizationSchema


class ProjectWithKeysSchema(ProjectOrganizationSchema):
    keys: list[ProjectKeySchema] = Field(validation_alias="projectkey_set")
