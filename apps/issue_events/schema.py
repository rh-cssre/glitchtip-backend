from typing import Optional
from ninja import ModelSchema, Field
from .models import IssueEvent


class IssueEventSchema(ModelSchema):
    id: str = Field(alias="eventID")
    # previous_event_id: Optional[str] = Field(alias="previousEventID", default=None)

    class Config:
        model = IssueEvent
        model_fields = ["id"]
        populate_by_name = True

    # @staticmethod
    # async def resolve_previous_event_id(obj):
    #     print("start!!!!")
    #     result = await obj.aget_next_by_created()
    #     if result:
    #         str(result)

    # def _get_next_or_previous(self, obj, is_next):
    #     kwargs = self.context["view"].kwargs
    #     filter_kwargs = {}
    #     if kwargs.get("issue_pk"):
    #         filter_kwargs["issue"] = kwargs["issue_pk"]
    #     if is_next:
    #         result = obj.next(**filter_kwargs)
    #     else:
    #         result = obj.previous(**filter_kwargs)
    #     if result:
    #         return str(result)

    # def get_previousEventID(self, obj):
    #     return self.get_next_or_previous(obj, False)
