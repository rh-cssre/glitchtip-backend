from typing import Optional
from ninja import Field, ModelSchema

from glitchtip.api.schema import CamelSchema

from .models import IssueEvent


class IssueEventSchema(CamelSchema, ModelSchema):
    id: str = Field(validation_alias="id.hex")
    event_id: str
    project_id: int = Field(validation_alias="issue.project_id")
    group_id: int = Field(validation_alias="issue_id")
    dist: Optional[str] = None

    class Config:
        model = IssueEvent
        model_fields = ["id"]
        populate_by_name = True


class IssueEventDetailSchema(IssueEventSchema):
    user_report: list = []  # TODO
    next_event_id: Optional[str] = None
    previous_event_id: Optional[str] = None

    @staticmethod
    def resolve_previous_event_id(obj):
        if event_id := obj.previous:
            return event_id.hex

    @staticmethod
    def resolve_next_event_id(obj):
        if event_id := obj.next:
            return event_id.hex
