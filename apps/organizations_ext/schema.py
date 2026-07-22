from datetime import datetime
from typing import Literal

from allauth.socialaccount.models import SocialApp
from django.conf import settings
from django.urls import NoReverseMatch, reverse
from ninja import Field, ModelSchema
from pydantic import ConfigDict, EmailStr

from apps.users.schema import UserSchema
from glitchtip.schema import CamelSchema

from .invitation_backend import InvitationTokenGenerator
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


class OrganizationUserInviteSchema(OrganizationUserSchema):
    """Response for the invite (member create) endpoint.

    Surfaces the acceptance link so the frontend can offer a copy-link invite
    flow. This is the only delivery path when email is disabled, and a harmless
    convenience when email works. The link carries the same time-limited token
    the invite email uses, so possessing it is what grants acceptance.
    """

    invite_link: str | None = None

    @staticmethod
    def resolve_invite_link(obj) -> str | None:
        if obj.user_id is not None:
            # Already accepted -- no pending invite to link to.
            return None
        token = InvitationTokenGenerator().make_token(obj)
        return settings.GLITCHTIP_URL.geturl() + reverse(
            "invitations_register", args=[obj.pk, token]
        )


class OrganizationSocialAppIn(CamelSchema):
    """Create an organization-managed OpenID Connect social app."""

    name: str = Field(min_length=1, max_length=40)
    server_url: str = Field(max_length=500)
    """OIDC issuer or discovery document URL."""
    client_id: str = Field(min_length=1, max_length=191)
    client_secret: str = Field(min_length=1, max_length=191)
    is_public: bool = False
    """Show on the instance-wide login page. Ignored on billing-enabled
    installs, where organization apps are always private."""


class OrganizationSocialAppUpdateIn(OrganizationSocialAppIn):
    client_secret: str | None = Field(default=None, min_length=1, max_length=191)
    """Omit to keep the existing secret."""


class OrganizationSocialAppSchema(CamelSchema, ModelSchema):
    server_url: str | None
    is_public: bool
    callback_url: str | None
    """Redirect URI to register with the identity provider."""
    login_url: str
    """The organization's login page, which lists this sign-in option."""

    class Meta:
        model = SocialApp
        fields = ["id", "name", "provider", "provider_id", "client_id"]

    @staticmethod
    def resolve_server_url(obj):
        return obj.settings.get("server_url")

    @staticmethod
    def resolve_is_public(obj):
        return obj.organizationsocialapp.is_public

    @staticmethod
    def resolve_callback_url(obj):
        try:
            if obj.provider == "openid_connect":
                path = reverse(
                    "openid_connect_callback", kwargs={"provider_id": obj.provider_id}
                )
            else:
                path = reverse(f"{obj.provider}_callback")
        except NoReverseMatch:
            return None
        return settings.GLITCHTIP_URL.geturl() + path

    @staticmethod
    def resolve_login_url(obj):
        return (
            settings.GLITCHTIP_URL.geturl()
            + "/login/"
            + obj.organizationsocialapp.organization.slug
        )


class AcceptInviteIn(CamelSchema):
    accept_invite: bool


class OrganizationUserOrganizationSchema(OrganizationUserSchema):
    """Organization User Serializer with Organization info"""

    organization: OrganizationSchema


class AcceptInviteSchema(AcceptInviteIn):
    org_user: OrganizationUserOrganizationSchema
