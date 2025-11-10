import uuid
from datetime import timedelta

from django.contrib.postgres.search import SearchVectorField
from django.db import models
from psql_partition.models import PostgresPartitionedModel
from psql_partition.types import PostgresPartitioningMethod

from glitchtip.base_models import AggregationModel, CreatedModel, SoftDeleteModel


class TransactionGroup(CreatedModel, SoftDeleteModel):
    transaction = models.CharField(max_length=1024)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    op = models.CharField(max_length=255)
    method = models.CharField(max_length=255, blank=True)
    tags = models.JSONField(default=dict)
    search_vector = SearchVectorField(null=True, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["transaction", "project", "op", "method"],
                name="unique_transaction_project_op_method",
            )
        ]

    def __str__(self):
        return self.transaction


class TransactionEvent(PostgresPartitionedModel, models.Model):
    pk = models.CompositePrimaryKey("event_id", "organization", "start_timestamp")
    event_id = models.UUIDField(default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    group = models.ForeignKey(TransactionGroup, on_delete=models.CASCADE)
    trace_id = models.UUIDField(db_index=True)
    start_timestamp = models.DateTimeField(
        db_index=True,
        help_text="Datetime reported by client as the time the measurement started",
    )
    timestamp = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Datetime reported by client as the time the measurement finished",
    )
    data = models.JSONField(help_text="General event data that is searchable")
    # This could be HStore, but jsonb is just as good and removes need for
    # 'django.contrib.postgres' which makes several unnecessary SQL calls
    tags = models.JSONField(default=dict)

    class Meta:
        ordering = ["-start_timestamp"]

    class PartitioningMeta:
        method = PostgresPartitioningMethod.RANGE
        key = ["start_timestamp"]

    def __str__(self):
        return str(self.trace_id)

    @property
    def duration(self) -> timedelta | None:
        if self.timestamp is None:
            return None
        duration = self.timestamp - self.start_timestamp
        return max(duration, timedelta(0))

    @property
    def duration_ms(self) -> int | None:
        """Optimized method for getting duration in milliseconds"""
        duration = self.duration
        if duration is None:
            return None
        return (
            (duration.days * 86_400_000)
            + (duration.seconds * 1000)
            + duration.microseconds // 1000
        )


class TransactionGroupAggregate(AggregationModel):
    """Count the number of events for a transaction group per time unit"""

    pk = models.CompositePrimaryKey("group", "organization", "date")
    group = models.ForeignKey(TransactionGroup, on_delete=models.CASCADE)
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    total_duration = models.PositiveBigIntegerField(
        default=0,
        help_text="Sum of all transaction durations (in ms) for calculating the mean.",
    )
    sum_of_squares_duration = models.PositiveBigIntegerField(
        default=0,
        help_text="Sum of squares of durations, for calculating standard deviation.",
    )
    histogram = models.JSONField(
        default=dict,
        help_text="Stores a fixed-bucket histogram for percentile approximation.",
    )

    class PartitioningMeta(AggregationModel.PartitioningMeta):
        pass
