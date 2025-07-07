import uuid

from django.contrib.postgres.search import SearchVectorField
from django.db import models
from psql_partition.models import PostgresPartitionedModel
from psql_partition.types import PostgresPartitioningMethod

from glitchtip.base_models import CreatedModel


class TransactionGroup(CreatedModel):
    transaction = models.CharField(max_length=1024)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    op = models.CharField(max_length=255)
    method = models.CharField(max_length=255, null=True, blank=True)
    tags = models.JSONField(default=dict)
    search_vector = SearchVectorField(null=True, editable=False)

    class Meta:
        unique_together = (("transaction", "project", "op", "method"),)

    def __str__(self):
        return self.transaction


class TransactionEvent(PostgresPartitionedModel, models.Model):
    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
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
    duration = models.PositiveIntegerField(db_index=True, help_text="Milliseconds")
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
