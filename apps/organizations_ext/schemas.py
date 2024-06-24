from datetime import datetime
from typing import Optional

from ninja import Field, ModelSchema

from glitchtip.schema import CamelSchema

from .models import Organization


class OrganizationSchema(CamelSchema, ModelSchema):
    date_created: datetime = Field(validation_alias="created")
    status: dict[str, str]
    avatar: dict[str, Optional[str]]
    is_early_adopter: bool = False
    require2FA: bool = False

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "slug",
            "is_accepting_events",
        )

    @staticmethod
    def resolve_status(obj):
        return {"id": "active", "name": "active"}

    @staticmethod
    def resolve_avatar(obj):
        return {"avatarType": "", "avatarUuid": None}

    @staticmethod
    def resolve_require2FA(obj):
        return False
