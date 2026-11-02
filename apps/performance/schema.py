from datetime import datetime

from ninja import Field, ModelSchema, Schema
from pydantic import computed_field

from glitchtip.schema import CamelSchema

from .models import TransactionGroup


class TransactionGroupSchema(CamelSchema, ModelSchema):
    project: int = Field(validation_alias="project_id")

    class Meta:
        model = TransactionGroup
        fields = [
            "id",
            "project",
            "transaction",
            "op",
            "method",
            "count",
            "avg_duration",
            "p50",
            "p95",
            "error_count",
            "first_seen",
            "last_seen",
        ]

    @computed_field
    @property
    def error_rate(self) -> float:
        """Delegates to TransactionGroup.error_rate property."""
        return TransactionGroup.error_rate.fget(self)  # type: ignore[attr-defined]

    @computed_field
    @property
    def throughput(self) -> float | None:
        """Delegates to TransactionGroup.throughput property."""
        return TransactionGroup.throughput.fget(self)  # type: ignore[attr-defined]


class SpanGroupSchema(CamelSchema, Schema):
    op: str
    description: str
    count: int
    avg_duration: float
    p95_duration: float
    total_time: float


class NPlusOnePatternSchema(CamelSchema, Schema):
    transaction_name: str
    op: str
    description: str
    total_spans: int
    transaction_count: int
    spans_per_txn: float
    avg_duration: float
    total_time: float


class TransactionTrendSchema(CamelSchema, Schema):
    date: datetime
    count: int
    transaction_count: int
    avg_duration: float
    total_time: float
