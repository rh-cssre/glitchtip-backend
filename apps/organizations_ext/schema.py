from datetime import datetime
from typing import Optional

from ninja import Field, ModelSchema

from apps.teams.schema import ProjectTeamSchema, TeamSchema
from glitchtip.schema import CamelSchema

from .models import Organization


class OrganizationInSchema(CamelSchema, ModelSchema):
    class Meta:
        model = Organization
        fields = [
            "name",
        ]


class OrganizationSchema(OrganizationInSchema, ModelSchema):
    date_created: datetime = Field(validation_alias="created")
    status: dict[str, str] = {"id": "active", "name": "active"}
    avatar: dict[str, Optional[str]] = {"avatarType": "", "avatarUuid": None}
    is_early_adopter: bool = False
    require2fa: bool = False

    class Meta(OrganizationInSchema.Meta):
        fields = [
            "id",
            "name",
            "slug",
            "is_accepting_events",
        ]


class OrganizationDetailSchema(OrganizationSchema, ModelSchema):
    projects: list[ProjectTeamSchema]
    teams: list[TeamSchema]

    class Meta(OrganizationSchema.Meta):
        fields = OrganizationSchema.Meta.fields + ["open_membership"]
