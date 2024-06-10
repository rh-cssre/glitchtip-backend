from datetime import datetime
from typing import Optional

from allauth.socialaccount.models import SocialAccount
from ninja import Field, ModelSchema

from glitchtip.schema import CamelSchema

from .models import User


class SocialAccountSchema(CamelSchema, ModelSchema):
    email: Optional[str]
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
    username: str = Field(validation_alias="email")
    created: datetime = Field(serialization_alias="dateJoined")
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
