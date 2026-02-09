from datetime import datetime
from typing import Literal

from ninja import Field, ModelSchema
from pydantic import ConfigDict, EmailStr

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
    id: str
    date_created: datetime = Field(validation_alias="created")
    status: dict[str, str] = {"id": "active", "name": "active"}
    avatar: dict[str, str | None] = {"avatarType": "", "avatarUuid": None}
    is_early_adopter: bool = False
    require2fa: bool = False

    class Meta(OrganizationInSchema.Meta):
        fields = [
            "name",
            "slug",
            "is_accepting_events",
            "event_throttle_rate",
        ]

    @staticmethod
    def resolve_id(obj):
        return str(obj.id)


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
    role: OrgRole
    role_name: str
    created: datetime = Field(alias="dateCreated")
    email: str
    user: UserSchema | None = None
    pending: bool
    is_owner: bool

    class Meta:
        model = OrganizationUser
        fields = ["id"]

    model_config = ConfigDict(coerce_numbers_to_str=True)

    @staticmethod
    def resolve_email(obj):
        return obj.get_email()

    @staticmethod
    def resolve_role(obj):
        return obj.get_role()

    @staticmethod
    def resolve_role_name(obj):
        return obj.get_role_display()

    @staticmethod
    def resolve_is_owner(obj):
        if owner := obj.organization.owner:
            return owner.organization_user_id == obj.id
        return False


class OrganizationUserDetailSchema(OrganizationUserSchema):
    teams: list[str]

    @staticmethod
    def resolve_teams(obj):
        return [team.slug for team in obj.teams.all()]


class AcceptInviteIn(CamelSchema):
    accept_invite: bool


class OrganizationUserOrganizationSchema(OrganizationUserSchema):
    """Organization User Serializer with Organization info"""

    organization: OrganizationSchema


class AcceptInviteSchema(AcceptInviteIn):
    org_user: OrganizationUserOrganizationSchema
