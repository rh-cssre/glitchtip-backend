from ninja import ModelSchema

from glitchtip.schema import CamelSchema

from .models import Environment, EnvironmentProject


class EnvironmentSchema(ModelSchema):
    class Meta:
        model = Environment
        fields = ["id", "name"]


class EnvironmentProjectIn(CamelSchema):
    name: str
    is_hidden: bool


class EnvironmentProjectSchema(CamelSchema, ModelSchema):
    name: str

    class Meta:
        model = EnvironmentProject
        fields = ["id", "is_hidden"]

    @staticmethod
    def resolve_name(obj):
        return obj.environment.name
