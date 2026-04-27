from datetime import datetime
from typing import Optional

from django.utils.timezone import now
from ninja import Field, ModelSchema, Schema

from apps.projects.schema import NameSlugProjectSchema
from glitchtip.schema import CamelSchema

from .models import Deploy, Release


class ReleaseUpdate(Schema):
    ref: Optional[str] = None
    released: Optional[datetime] = Field(alias="dateReleased", default_factory=now)


class ReleaseBase(ReleaseUpdate):
    version: str = Field(serialization_alias="shortVersion")


class ReleaseIn(ReleaseBase):
    projects: list[str]


class ReleaseRepositorySchema(CamelSchema):
    id: str
    name: str


class ReleaseSchema(CamelSchema, ReleaseBase, ModelSchema):
    created: datetime = Field(serialization_alias="dateCreated")
    released: Optional[datetime] = Field(serialization_alias="dateReleased")
    short_version: str = Field(validation_alias="version")
    projects: list[NameSlugProjectSchema]
    repository: ReleaseRepositorySchema | None = Field(
        default=None, validation_alias="repository"
    )

    class Meta:
        model = Release
        fields = [
            "url",
            "data",
            "commit_count",
            "deploy_count",
            "projects",
            "version",
        ]


class DeployIn(CamelSchema):
    environment: str
    url: str = ""
    date_started: datetime | None = Field(alias="dateStarted", default=None)
    date_finished: datetime | None = Field(alias="dateFinished", default=None)


class DeploySchema(CamelSchema, ModelSchema):
    created: datetime = Field(serialization_alias="dateCreated")
    date_started: datetime | None = Field(serialization_alias="dateStarted")
    date_finished: datetime | None = Field(serialization_alias="dateFinished")

    class Meta:
        model = Deploy
        fields = ["id", "environment", "url"]


class CommitIn(Schema):
    id: str
    message: str = ""
    author_name: str = Field(alias="authorName", default="")
    author_email: str = Field(alias="authorEmail", default="")


class CommitSchema(CamelSchema):
    """Output schema for commit data, matching documented public API."""

    id: str
    message: str | None = ""
    date_created: str | None = Field(
        default=None, serialization_alias="dateCreated", validation_alias="dateCreated"
    )
    author_name: str | None = Field(
        default=None, serialization_alias="authorName", validation_alias="authorName"
    )
    author_email: str | None = Field(
        default=None, serialization_alias="authorEmail", validation_alias="authorEmail"
    )


class AssembleSchema(Schema):
    checksum: str
    chunks: list[str]
