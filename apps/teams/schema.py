from datetime import datetime

from ninja import Field, ModelSchema, Schema

from apps.projects.schema import ProjectSchema
from apps.shared.schema.fields import SlugStr
from glitchtip.schema import CamelSchema

from .models import Team


class TeamIn(Schema):
    slug: SlugStr


class ProjectTeamSchema(CamelSchema, ModelSchema):
    """TeamSchema but without projects"""

    id: str
    created: datetime = Field(serialization_alias="dateCreated")
    is_member: bool
    member_count: int
    slug: SlugStr

    class Meta:
        model = Team
        fields = ["id", "slug"]

    class Config(CamelSchema.Config):
        coerce_numbers_to_str = True


class TeamSchema(ProjectTeamSchema):
    projects: list[ProjectSchema] = []
