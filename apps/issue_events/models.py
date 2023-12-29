import uuid

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.utils import timezone
from psqlextra.models import PostgresPartitionedModel
from psqlextra.types import PostgresPartitioningMethod

from glitchtip.base_models import CreatedModel, SoftDeleteModel
from sentry.constants import MAX_CULPRIT_LENGTH

from .constants import EventStatus, IssueEventType, LogLevel
from .utils import base32_encode


class DeferedFieldManager(models.Manager):
    def __init__(self, defered_fields=[]):
        super().__init__()
        self.defered_fields = defered_fields

    def get_queryset(self, *args, **kwargs):
        return super().get_queryset(*args, **kwargs).defer(*self.defered_fields)


class Issue(SoftDeleteModel):
    culprit = models.CharField(max_length=1024, blank=True, null=True)
    # has_seen = models.BooleanField(default=False)
    is_public = models.BooleanField(default=False)
    level = models.PositiveSmallIntegerField(
        choices=LogLevel.choices, default=LogLevel.ERROR
    )
    metadata = models.JSONField()
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
    search_vector = SearchVectorField(editable=False, default="")
    count = models.PositiveIntegerField(default=1, editable=False)
    first_seen = models.DateTimeField(default=timezone.now, db_index=True)
    last_seen = models.DateTimeField(default=timezone.now, db_index=True)

    objects = DeferedFieldManager(["search_vector"])

    class Meta:
        base_manager_name = "objects"
        unique_together = (("project", "short_id"),)
        indexes = [GinIndex(fields=["search_vector"])]

    @property
    def short_id_display(self):
        """
        Short IDs are per project issue counters. They show as PROJECT_SLUG-ID_BASE32
        The intention is to be human readable identifiers that can reference an issue.
        """
        if self.short_id is not None:
            return f"{self.project.slug.upper()}-{base32_encode(self.short_id)}"
        return ""


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


class UserReport(CreatedModel):
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="+"
    )
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE)
    event_id = models.UUIDField()
    name = models.CharField(max_length=128)
    email = models.EmailField()
    comments = models.TextField()

    class Meta:
        unique_together = (("project", "event_id"),)


class IssueEvent(PostgresPartitionedModel, models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE)
    type = models.PositiveSmallIntegerField(default=0, choices=IssueEventType.choices)
    timestamp = models.DateTimeField(help_text="Time at which event happened")
    received = models.DateTimeField(help_text="Time at which GlitchTip accepted event")
    title = models.CharField(max_length=255)
    transaction = models.CharField(max_length=MAX_CULPRIT_LENGTH)
    level = models.PositiveSmallIntegerField(
        choices=LogLevel.choices, default=LogLevel.ERROR
    )
    data = models.JSONField()
    # This could be HStore, but jsonb is just as good and removes need for
    # 'django.contrib.postgres' which makes several unnecessary SQL calls
    tags = models.JSONField()

    class PartitioningMeta:
        method = PostgresPartitioningMethod.RANGE
        key = ["received"]

    def __str__(self):
        return self.eventID

    @property
    def eventID(self):
        return self.id.hex

    @property
    def message(self):
        """Often the title and message are the same. If message isn't stored, assume it's the title"""
        return self.data.get("message", self.title)

    @property
    def metadata(self):
        """Return metadata if exists, else return just the title as metadata"""
        return self.data.get("metadata", {"title": self.title})

    @property
    def platform(self):
        return self.data.get("platform")
