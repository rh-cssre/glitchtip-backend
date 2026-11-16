import hashlib
import hmac
from datetime import datetime

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from ninja import Field, ModelSchema, Schema
from pydantic import EmailStr

from glitchtip.schema import CamelSchema

from .models import User


class SocialAccountSchema(CamelSchema, ModelSchema):
    email: EmailStr | None
    username: str | None

    class Meta:
        model = SocialAccount
        fields = (
            "id",
            "provider",
            "uid",
            "last_login",
            "date_joined",
        )

    @staticmethod
    def resolve_email(obj):
        if data := obj.extra_data:
            # MS oauth uses both principal name and mail
            return (
                data.get("email") or data.get("userPrincipalName") or data.get("mail")
            )

    @staticmethod
    def resolve_username(obj):
        if data := obj.extra_data:
            return data.get("username")


class UserOptions(CamelSchema):
    timezone: str | None = None
    stacktrace_order: int | None = None
    language: str | None = None
    clock24_hours: bool | None = None
    preferred_theme: str | None = None


class UserIn(CamelSchema, ModelSchema):
    options: UserOptions

    class Meta:
        model = User
        fields = [
            "name",
            "options",
        ]


class UserSchema(CamelSchema, ModelSchema):
    id: str
    options: UserOptions
    username: EmailStr = Field(validation_alias="email")
    created: datetime = Field(alias="dateJoined")
    email: EmailStr
    has_usable_password: bool = Field(alias="hasPasswordAuth")
    socialaccount_set: list[SocialAccountSchema] = Field(alias="identities")

    class Meta(UserIn.Meta):
        fields = [
            "last_login",
            "is_superuser",
            # "emails",
            "id",
            "is_active",
            "name",
            "email",
            "options",
        ]

    @staticmethod
    def resolve_id(obj):
        return str(obj.id)


class UserDetailSchema(UserSchema):
    chatwoot_identifier_hash: str | None = None

    @staticmethod
    def resolve_chatwoot_identifier_hash(obj):
        if settings.CHATWOOT_WEBSITE_TOKEN and settings.CHATWOOT_IDENTITY_TOKEN:
            secret = bytes(settings.CHATWOOT_IDENTITY_TOKEN, "utf-8")
            message = bytes(str(obj.id), "utf-8")

            hash = hmac.new(secret, message, hashlib.sha256)
            return hash.hexdigest()


class EmailAddressIn(CamelSchema, Schema):
    email: EmailStr


class EmailAddressSchema(CamelSchema, ModelSchema):
    is_primary: bool
    is_verified: bool

    class Meta:
        model = EmailAddress
        fields = ["email"]

    @staticmethod
    def resolve_is_primary(obj):
        return obj.primary

    @staticmethod
    def resolve_is_verified(obj):
        return obj.verified


class UserNotificationsSchema(CamelSchema, ModelSchema):
    class Meta:
        model = User
        fields = ("subscribe_by_default",)


class RecoveryCodesSchema(CamelSchema):
    codes: list[str]


class RecoveryCodeSchema(CamelSchema):
    code: str
