from datetime import datetime
from typing import Optional

from django.utils.timezone import now
from ninja import Field, ModelSchema, Schema

from apps.projects.schema import NameSlugProjectSchema
from glitchtip.schema import CamelSchema

from .models import Release


class ReleaseUpdate(Schema):
    ref: Optional[str] = None
    released: Optional[datetime] = Field(alias="dateReleased", default_factory=now)


class ReleaseBase(ReleaseUpdate):
    version: str = Field(serialization_alias="shortVersion")


class ReleaseIn(ReleaseBase):
    projects: list[str]


class ReleaseSchema(CamelSchema, ReleaseBase, ModelSchema):
    created: datetime = Field(serialization_alias="dateCreated")
    released: Optional[datetime] = Field(serialization_alias="dateReleased")
    short_version: str = Field(validation_alias="version")
    projects: list[NameSlugProjectSchema]

    class Meta:
        model = Release
        fields = [
            "url",
            "data",
            "deploy_count",
            "projects",
            "version",
        ]


class AssembleSchema(Schema):
    checksum: str
    chunks: list[str]
