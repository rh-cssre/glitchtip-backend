import uuid

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from psqlextra.models import PostgresPartitionedModel
from psqlextra.types import PostgresPartitioningMethod

from .constants import EventStatus, IssueEventType, LogLevel


class Issue(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    culprit = models.CharField(max_length=1024, blank=True, null=True)
    is_public = models.BooleanField(default=False)
    level = models.PositiveSmallIntegerField(
        choices=LogLevel.choices, default=LogLevel.ERROR
    )
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="issues"
    )
    title = models.CharField(max_length=255)
    type = models.PositiveSmallIntegerField(
        choices=IssueEventType.choices, default=IssueEventType.DEFAULT
    )
    status = models.PositiveSmallIntegerField(
        choices=EventStatus.choices, default=EventStatus.UNRESOLVED
    )
    short_id = models.PositiveIntegerField(null=True)

    class Meta:
        unique_together = (("project", "short_id"),)


class IssueStats(models.Model):
    issue = models.OneToOneField(Issue, primary_key=True, on_delete=models.CASCADE)
    search_vector = SearchVectorField(null=True, editable=False)
    search_vector_created = models.DateTimeField(auto_now_add=True)
    count = models.PositiveIntegerField(default=1, editable=False)
    last_seen = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [GinIndex(fields=["search_vector"])]


class IssueHash(models.Model):
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="hashes")
    # Redundant project allows for unique constraint
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="+"
    )
    value = models.UUIDField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "value"], name="issue hash project"
            )
        ]


class Comment(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(
        "users.User", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    text = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ("-created",)


class IssueEvent(PostgresPartitionedModel, models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE)
    type = models.PositiveSmallIntegerField(default=0, choices=IssueEventType.choices)
    date_created = models.DateTimeField(
        auto_now_add=True, help_text="Time at which event happened"
    )
    date_received = models.DateTimeField(
        auto_now_add=True, help_text="Time at which GlitchTip accepted event"
    )
    data = models.JSONField()

    class PartitioningMeta:
        method = PostgresPartitioningMethod.RANGE
        key = ["date_received"]

    def __str__(self):
        return self.eventID

    @property
    def eventID(self):
        return self.id.hex
