from datetime import datetime
from typing import Optional

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from ninja import Field, ModelSchema
from pydantic import EmailStr

from glitchtip.schema import CamelSchema

from .models import User


class SocialAccountSchema(CamelSchema, ModelSchema):
    email: Optional[EmailStr]
    username: Optional[str]

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
        if obj.extra_data:
            if "email" in obj.extra_data:
                return obj.extra_data.get("email")
            return obj.extra_data.get("userPrincipalName")  # MS oauth uses this

    @staticmethod
    def resolve_username(obj):
        if obj.extra_data:
            return obj.extra_data.get("username")


class UserIn(CamelSchema, ModelSchema):
    class Meta:
        model = User
        fields = [
            "name",
            "options",
        ]


class UserSchema(CamelSchema, ModelSchema):
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
