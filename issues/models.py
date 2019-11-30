import uuid
from django.contrib.postgres.fields import JSONField
from django.db import models
from django_enumfield import enum


class EventType(enum.Enum):
    ERROR = 0
    CSP = 1

    __default__ = ERROR


class Issue(models.Model):
    title = models.CharField(max_length=255)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    event_type = enum.EnumField(EventType)
    location = models.CharField(max_length=1024)

    def event(self):
        return self.event_set.first()

    def __str__(self):
        return self.title


class Event(models.Model):
    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    exception = JSONField()
    level = models.CharField(max_length=255)
    platform = models.CharField(max_length=255)
    sdk = JSONField()
    release = models.CharField(max_length=255)
    breadcrumbs = JSONField()
    request = JSONField()
    received_at = models.DateTimeField(auto_now_add=True)
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE)

    def __str__(self):
        return self.event_id
