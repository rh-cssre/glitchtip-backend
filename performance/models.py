from django.db import models
from django.db.models import F, Avg
from django.contrib.postgres.fields import HStoreField
from glitchtip.base_models import CreatedModel
from events.models import AbstractEvent


avg_transactionevent_time = Avg(
    F("transactionevent__timestamp") - F("transactionevent__start_timestamp"),
    distinct=True,
)


class TransactionGroupManager(models.Manager):
    def with_avgs(self):
        return self.annotate(avg_duration=avg_transactionevent_time)


class TransactionGroup(CreatedModel):
    transaction = models.CharField(max_length=1024)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    op = models.CharField(max_length=255)
    method = models.CharField(max_length=255, null=True, blank=True)
    objects = TransactionGroupManager()

    class Meta:
        unique_together = (("transaction", "project", "op", "method"),)

    def __str__(self):
        return self.transaction


class TransactionEvent(AbstractEvent):
    group = models.ForeignKey(TransactionGroup, on_delete=models.CASCADE)
    trace_id = models.UUIDField(db_index=True)
    start_timestamp = models.DateTimeField()

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return str(self.trace_id)


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
    data = models.JSONField(default=dict)

    def __str__(self):
        return self.span_id
