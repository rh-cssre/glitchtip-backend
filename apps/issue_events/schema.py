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
    message: Optional[str] = Field(validation_alias="data.message", default=None)
    culprit: Optional[str] = Field(validation_alias="data.culprit", default=None)
    title: Optional[str] = Field(validation_alias="data.title", default=None)
    platform: Optional[str] = Field(validation_alias="data.platform", default=None)
    type: str = Field(validation_alias="get_type_display")
    metadata: dict[str, str] = Field(
        validation_alias="data.metadata", default_factory=dict
    )
    tags: list[dict[str, Optional[str]]] = []

    class Config:
        model = IssueEvent
        model_fields = ["id", "type", "date_created", "date_received"]
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
