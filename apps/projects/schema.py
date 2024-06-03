from datetime import datetime
from typing import Optional

from ninja import Field, ModelSchema

from glitchtip.schema import CamelSchema

from .models import Project


class NameSlugProjectSchema(CamelSchema, ModelSchema):
    class Meta:
        model = Project
        fields = [
            "name",
            "slug",
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
