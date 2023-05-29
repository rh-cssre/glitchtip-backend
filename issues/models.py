from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import connection, models

from events.models import LogLevel
from glitchtip.base_models import CreatedModel
from glitchtip.model_utils import FromStringIntegerChoices

from .utils import base32_encode


class EventType(models.IntegerChoices):
    DEFAULT = 0, "default"
    ERROR = 1, "error"
    CSP = 2, "csp"
    TRANSACTION = 3, "transaction"


class EventStatus(FromStringIntegerChoices):
    UNRESOLVED = 0, "unresolved"
    RESOLVED = 1, "resolved"
    IGNORED = 2, "ignored"


class Issue(CreatedModel):
    """
    Sentry called this a "group". A issue is a collection of events with meta data
    such as resolved status.
    """

    # annotations Not implemented
    # assigned_to Not implemented
    culprit = models.CharField(max_length=1024, blank=True, null=True)
    has_seen = models.BooleanField(default=False)
    # is_bookmarked Not implement - is per user
    is_public = models.BooleanField(default=False)
    level = models.PositiveSmallIntegerField(
        choices=LogLevel.choices, default=LogLevel.ERROR
    )
    metadata = models.JSONField()
    tags = models.JSONField(default=dict)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    title = models.CharField(max_length=255)
    type = models.PositiveSmallIntegerField(
        choices=EventType.choices, default=EventType.DEFAULT
    )
    status = models.PositiveSmallIntegerField(
        choices=EventStatus.choices, default=EventStatus.UNRESOLVED
    )
    # See migration 0004 for trigger that sets search_vector, count, last_seen
    short_id = models.PositiveIntegerField(null=True)
    search_vector = SearchVectorField(null=True, editable=False)
    count = models.PositiveIntegerField(default=1, editable=False)
    last_seen = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        unique_together = (
            ("title", "culprit", "project", "type"),
            ("project", "short_id"),
        )
        indexes = [GinIndex(fields=["search_vector"], name="search_vector_idx")]

    def event(self):
        return self.event_set.first()

    def __str__(self):
        return self.title

    def check_for_status_update(self):
        """
        Determine if issue should regress back to unresolved
        Typically run when processing a new event related to the issue
        """
        if self.status == EventStatus.RESOLVED:
            self.status = EventStatus.UNRESOLVED
            self.save()
            # Delete notifications so that new alerts are sent for regressions
            self.notification_set.all().delete()

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

    def get_detail_url(self):
        return f"{settings.GLITCHTIP_URL.geturl()}/{self.project.organization.slug}/issues/{self.pk}"

    @classmethod
    def update_index(cls, issue_id: int):
        """
        Update search index/tag aggregations
        """
        with connection.cursor() as cursor:
            cursor.execute("CALL update_issue_index(%s)", [issue_id])


class Comment(CreatedModel):
    issue = models.ForeignKey("issues.Issue", on_delete=models.CASCADE)
    user = models.ForeignKey("users.User", null=True, on_delete=models.SET_NULL)
    text = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ("-created",)
