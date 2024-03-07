from django.contrib.postgres.fields import HStoreField
from django.contrib.postgres.search import SearchVectorField
from django.db import models

from apps.projects.tasks import update_transaction_event_project_hourly_statistic
from events.models import AbstractEvent
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


class TransactionEvent(AbstractEvent):
    group = models.ForeignKey(TransactionGroup, on_delete=models.CASCADE)
    trace_id = models.UUIDField(db_index=True)
    start_timestamp = models.DateTimeField()
    duration = models.PositiveIntegerField(db_index=True, help_text="Milliseconds")
    tags = HStoreField(default=dict)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return str(self.trace_id)

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new:
            update_transaction_event_project_hourly_statistic(
                args=[self.group.project_id, self.created], countdown=60
            )


class Span(CreatedModel):
    transaction = models.ForeignKey(TransactionEvent, on_delete=models.CASCADE)
    span_id = models.CharField(max_length=16)
    parent_span_id = models.CharField(max_length=16, null=True, blank=True)
    # same_process_as_parent bool - we don't use this currently
    op = models.CharField(max_length=255, blank=True)
    description = models.CharField(max_length=2000, null=True, blank=True)
    start_timestamp = models.DateTimeField()
    timestamp = models.DateTimeField()
    tags = HStoreField(default=dict)
    data = models.JSONField(default=dict)

    def __str__(self):
        return self.span_id
