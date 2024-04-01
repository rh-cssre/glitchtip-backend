import uuid

from django.contrib.postgres.search import SearchVectorField
from django.db import models

from glitchtip.base_models import SoftDeleteModel
from psqlextra.models import PostgresPartitionedModel
from psqlextra.types import PostgresPartitioningMethod


class TransactionGroup(SoftDeleteModel):
    transaction = models.CharField(max_length=1024)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    op = models.CharField(max_length=255)
    method = models.CharField(max_length=255, null=True, blank=True)
    search_vector = SearchVectorField(null=True, editable=False)

    class Meta:
        unique_together = (("transaction", "project", "op", "method"),)


class TransactionEvent(PostgresPartitionedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group = models.ForeignKey(TransactionGroup, on_delete=models.CASCADE)
    trace_id = models.UUIDField(db_index=True)
    timestamp = models.DateTimeField(help_text="Time at which event happened")
    duration = models.PositiveIntegerField(db_index=True, help_text="Milliseconds")
    data = models.JSONField()

    class Meta:
        ordering = ["-timestamp"]

    class PartitioningMeta:
        method = PostgresPartitioningMethod.RANGE
        key = ["timestamp"]
