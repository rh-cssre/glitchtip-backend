from datetime import datetime
from typing import Literal

from ninja import Field, ModelSchema
from pydantic import EmailStr

from apps.users.schema import UserSchema
from glitchtip.schema import CamelSchema

from .models import (
    Organization,
    OrganizationUser,
)


class OrganizationInSchema(CamelSchema, ModelSchema):
    class Meta:
        model = Organization
        fields = [
            "name",
        ]


class OrganizationSchema(OrganizationInSchema, ModelSchema):
    date_created: datetime = Field(validation_alias="created")
    status: dict[str, str] = {"id": "active", "name": "active"}
    avatar: dict[str, str | None] = {"avatarType": "", "avatarUuid": None}
    is_early_adopter: bool = False
    require2fa: bool = False

    class Meta(OrganizationInSchema.Meta):
        fields = [
            "id",
            "name",
            "slug",
            "is_accepting_events",
            "event_throttle_rate",
        ]


OrgRole = Literal["member", "admin", "manager", "owner"]


class TeamRole(CamelSchema):
    team_slug: str
    role: str = ""
    """Does nothing at this time"""


class OrganizationUserUpdateSchema(CamelSchema):
    org_role: OrgRole
    team_roles: list[TeamRole] = Field(default_factory=list)


class OrganizationUserIn(OrganizationUserUpdateSchema):
    email: EmailStr
    send_invite: bool = True
    reinvite: bool = True


class OrganizationUserSchema(CamelSchema, ModelSchema):
    id: str
    role: str = Field(validation_alias="get_role")
    role_name: str = Field(validation_alias="get_role_display")
    date_created: datetime = Field(validation_alias="created")
    email: str = Field(validation_alias="get_email")
    user: UserSchema | None = None
    pending: bool

    class Meta:
        model = OrganizationUser
        fields = ["id"]

    class Config(CamelSchema.Config):
        coerce_numbers_to_str = True


class OrganizationUserDetailSchema(OrganizationUserSchema):
    teams: list[str]
    isOwner: bool

    @staticmethod
    def resolve_teams(obj):
        return [team.slug for team in obj.teams.all()]

    @staticmethod
    def resolve_isOwner(obj):
        if owner := obj.organization.owner:
            return owner.organization_user_id == obj.id
        return False


class AcceptInviteIn(CamelSchema):
    accept_invite: bool


class OrganizationUserOrganizationSchema(OrganizationUserSchema):
    """Organization User Serializer with Organization info"""

    organization: OrganizationSchema


class AcceptInviteSchema(AcceptInviteIn):
    org_user: OrganizationUserOrganizationSchema
