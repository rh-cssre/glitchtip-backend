from datetime import datetime
from typing import Optional

from django.utils.timezone import now
from ninja import Field, ModelSchema, Schema

from apps.projects.schema import NameSlugProjectSchema
from glitchtip.schema import CamelSchema

from .models import Release, ReleaseFile


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


class ReleaseFileSchema(CamelSchema, ModelSchema):
    id: str
    created: datetime = Field(serialization_alias="dateCreated")
    sha1: Optional[str] = Field(validation_alias="file.checksum", default=None)
    headers: Optional[dict[str, str]] = Field(
        validation_alias="file.headers", default=None
    )
    size: int = Field(validation_alias="file.size")

    class Meta:
        model = ReleaseFile
        fields = ["name"]

    class Config(CamelSchema.Config):
        coerce_numbers_to_str = True


class AssembleSchema(Schema):
    checksum: str
    chunks: list[str]
