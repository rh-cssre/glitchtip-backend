from typing import Annotated

from ninja import Field, ModelSchema
from pydantic.functional_validators import BeforeValidator

from glitchtip.schema import CamelSchema

from .models import TransactionEvent, TransactionGroup


def coerce_int(v) -> int:
    if isinstance(v, float):
        return int(v)
    return v


FlexInt = Annotated[int, BeforeValidator(coerce_int)]


class TransactionEventSchema(CamelSchema, ModelSchema):
    class Meta:
        model = TransactionEvent
        fields = (
            "event_id",
            "timestamp",
            "start_timestamp",
            # "transaction",
            # "op",
            # "method",
        )


class TransactionGroupSchema(CamelSchema, ModelSchema):
    avg_duration: FlexInt | None
    transaction_count: int
    project: int = Field(validation_alias="project_id")

    class Meta:
        model = TransactionGroup
        fields = [
            "id",
            "transaction",
            "op",
            "method",
        ]
