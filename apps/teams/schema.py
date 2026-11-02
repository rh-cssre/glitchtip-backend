from datetime import datetime

from ninja import Field, ModelSchema, Schema
from pydantic import ConfigDict

from apps.organizations_ext.constants import OrganizationUserRole, Scopes
from apps.organizations_ext.schema import OrganizationSchema
from apps.projects.schema import ProjectSchema
from apps.shared.schema.fields import SlugStr
from glitchtip.schema import CamelSchema

from .models import Team


class TeamIn(Schema):
    slug: SlugStr


class TeamSlugSchema(CamelSchema, ModelSchema):
    """Used in relations including organization projects"""

    id: str

    class Meta:
        model = Team
        fields = ["slug"]

    @staticmethod
    def resolve_id(obj):
        return str(obj.id)


class TeamSchema(TeamSlugSchema):
    created: datetime = Field(serialization_alias="dateCreated")
    is_member: bool
    member_count: int
    slug: SlugStr

    class Meta(TeamSlugSchema.Meta):
        fields = ["slug"]

    model_config = ConfigDict(coerce_numbers_to_str=True)


class TeamProjectSchema(TeamSchema):
    """TeamSchema with related projects"""

    projects: list[ProjectSchema] = []


class ProjectTeamSchema(ProjectSchema):
    """Project Schema with related teams"""

    teams: list[TeamSlugSchema]


# Depends on teams, thus part of the teams app
class OrganizationDetailSchema(OrganizationSchema, ModelSchema):
    projects: list[ProjectTeamSchema]
    teams: list[TeamSchema]
    access: list[Scopes]

    class Meta(OrganizationSchema.Meta):
        fields = OrganizationSchema.Meta.fields + ["open_membership"]

    @staticmethod
    def resolve_access(obj):
        return OrganizationUserRole.get_role(obj.actor_role)["scopes"]
