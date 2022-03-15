from django.db import models
from django.contrib.postgres.fields import HStoreField
from glitchtip.base_models import CreatedModel
from events.models import AbstractEvent


class TransactionGroup(CreatedModel):
    title = models.CharField(max_length=1024)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    op = models.CharField(max_length=255)
    method = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        unique_together = (("title", "project", "op", "method"),)


class TransactionEvent(AbstractEvent):
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE
    )  # TODO delete in favor of group
    group = models.ForeignKey(TransactionGroup, on_delete=models.CASCADE)
    trace_id = models.UUIDField(db_index=True)
    transaction = models.CharField(max_length=1024)
    start_timestamp = models.DateTimeField()

    class Meta:
        ordering = ["-created"]


class Span(CreatedModel):
    transaction = models.ForeignKey(TransactionEvent, on_delete=models.CASCADE)
    span_id = models.CharField(max_length=16)
    parent_span_id = models.CharField(max_length=16, null=True, blank=True)
    # same_process_as_parent bool - we don't use this currently
    op = models.CharField(max_length=255)
    description = models.CharField(max_length=1024, null=True, blank=True)
    start_timestamp = models.DateTimeField()
    timestamp = models.DateTimeField()
    tags = HStoreField(default=dict)
    data = HStoreField(default=dict)
