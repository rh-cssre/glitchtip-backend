import hashlib
import hmac
from datetime import datetime

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from ninja import Field, ModelSchema
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


class UserIn(CamelSchema, ModelSchema):
    class Meta:
        model = User
        fields = [
            "name",
            "options",
        ]


class UserSchema(CamelSchema, ModelSchema):
    id: str
    username: EmailStr = Field(validation_alias="email")
    created: datetime = Field(serialization_alias="dateJoined")
    email: EmailStr
    has_password_auth: bool = Field(validation_alias="has_usable_password")
    identities: list[SocialAccountSchema] = Field(validation_alias="socialaccount_set")

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



class EmailAddressIn(CamelSchema, ModelSchema):
    email: EmailStr

    class Meta:
        model = EmailAddress
        fields = ["email"]


class EmailAddressSchema(CamelSchema, ModelSchema):
    isPrimary: bool = Field(validation_alias="primary")
    isVerified: bool = Field(validation_alias="verified")

    class Meta(EmailAddressIn.Meta):
        pass


class UserNotificationsSchema(CamelSchema, ModelSchema):
    class Meta:
        model = User
        fields = ("subscribe_by_default",)


class RecoveryCodesSchema(CamelSchema):
    codes: list[str]


class RecoveryCodeSchema(CamelSchema):
    code: str
