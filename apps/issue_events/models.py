import uuid

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.utils import timezone
from psql_partition.models import PostgresPartitionedModel
from psql_partition.types import PostgresPartitioningMethod

from glitchtip.base_models import AggregationModel, CreatedModel, SoftDeleteModel
from sentry.constants import MAX_CULPRIT_LENGTH

from .constants import MAX_TAG_LENGTH, EventStatus, IssueEventType, LogLevel
from .utils import base32_encode


class DeferedFieldManager(models.Manager):
    def __init__(self, defered_fields=[]):
        super().__init__()
        self.defered_fields = defered_fields

    def get_queryset(self, *args, **kwargs):
        return super().get_queryset(*args, **kwargs).defer(*self.defered_fields)


class TagKey(models.Model):
    id = models.AutoField(primary_key=True)
    key = models.CharField(max_length=MAX_TAG_LENGTH, unique=True)


class TagValue(models.Model):
    value = models.CharField(max_length=MAX_TAG_LENGTH, unique=True)


class IssueTag(AggregationModel):
    """
    This model is a aggregate of event tags for an issue.
    It is denormalized data that powers fast search results.
    """

    issue = models.ForeignKey("Issue", on_delete=models.CASCADE)
    tag_key = models.ForeignKey(TagKey, on_delete=models.CASCADE)
    tag_value = models.ForeignKey(TagValue, on_delete=models.CASCADE)
    count = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["issue", "date", "tag_key", "tag_value"],
                name="issue_tag_key_value_unique",
            )
        ]

    class PartitioningMeta(AggregationModel.PartitioningMeta):
        pass


class IssueAggregate(AggregationModel):
    """Count the number of events for an issue per time unit"""

    pk = models.CompositePrimaryKey("issue", "organization", "date")
    issue = models.ForeignKey("Issue", on_delete=models.CASCADE)
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )

    class PartitioningMeta(AggregationModel.PartitioningMeta):
        pass


class Issue(SoftDeleteModel):
    culprit = models.CharField(max_length=1024, blank=True, null=True)
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
        constraints = [
            models.UniqueConstraint(
                fields=["project", "short_id"],
                name="project_short_id_unique",
            )
        ]
        indexes = [
            GinIndex(fields=["search_vector"]),
            GinIndex(
                fields=["title"],
                name="issue_title_trgm_idx",
                opclasses=["gin_trgm_ops"],
            ),
        ]

    def __str__(self):
        return self.title

    def get_detail_url(self):
        return f"{settings.GLITCHTIP_URL.geturl()}/{self.project.organization.slug}/issues/{self.pk}"

    def get_hex_color(self):
        if self.level == LogLevel.INFO:
            return "#4b60b4"
        elif self.level is LogLevel.WARNING:
            return "#e9b949"
        elif self.level in [LogLevel.ERROR, LogLevel.FATAL]:
            return "#e52b50"

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
    issue = models.ForeignKey(Issue, null=True, on_delete=models.CASCADE)
    event_id = models.UUIDField()
    name = models.CharField(max_length=128)
    email = models.EmailField()
    comments = models.TextField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "event_id"],
                name="project_event_unique",
            )
        ]


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
    hashes = ArrayField(models.CharField(max_length=32), db_default=[])
    # This could be HStore, but jsonb is just as good and removes need for
    # 'django.contrib.postgres' which makes several unnecessary SQL calls
    tags = models.JSONField()
    release = models.ForeignKey(
        "releases.Release", blank=True, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        indexes = [
            models.Index(fields=["issue", "-received"]),
            GinIndex(fields=["hashes"]),
        ]

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
