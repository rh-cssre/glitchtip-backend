import uuid
from django.contrib.postgres.fields import JSONField
from django.db import models


class EventType(models.IntegerChoices):
    DEFAULT = 0, "default"
    ERROR = 1, "error"
    CSP = 2, "csp"


class EventStatus(models.IntegerChoices):
    UNRESOLVED = 0, "unresolved"
    RESOLVED = 1, "resolved"
    IGNORED = 2, "ignored"


class Issue(models.Model):
    title = models.CharField(max_length=255)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    type = models.PositiveSmallIntegerField(
        choices=EventType.choices, default=EventType.DEFAULT
    )
    location = models.CharField(
        max_length=1024, blank=True, null=True
    )  # TODO rename culprit
    status = models.PositiveSmallIntegerField(
        choices=EventStatus.choices, default=EventStatus.UNRESOLVED
    )

    def event(self):
        return self.event_set.first()

    def __str__(self):
        return self.title


class Event(models.Model):
    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    exception = JSONField(blank=True, null=True)
    level = models.CharField(max_length=255)
    platform = models.CharField(max_length=255)
    sdk = JSONField()
    release = models.CharField(max_length=255)
    breadcrumbs = JSONField()
    request = JSONField(blank=True, null=True)
    received_at = models.DateTimeField(auto_now_add=True)
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE)

    def __str__(self):
        return self.event_id
