from django.db import models
from events.models import Event


class TransactionEvent(Event):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    transaction = models.CharField(max_length=1024)
    start_timestamp = models.DateTimeField()
