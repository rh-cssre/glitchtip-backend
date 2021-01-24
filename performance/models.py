from django.db import models
from events.models import AbstractEvent


class TransactionEvent(AbstractEvent):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    transaction = models.CharField(max_length=1024)
    start_timestamp = models.DateTimeField()

    class Meta:
        ordering = ["-created"]
