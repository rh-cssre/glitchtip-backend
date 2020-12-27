import uuid
from django.contrib.postgres.search import SearchVectorField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from user_reports.models import UserReport
from .utils import base32_encode


class EventType(models.IntegerChoices):
    DEFAULT = 0, "default"
    ERROR = 1, "error"
    CSP = 2, "csp"


class EventStatus(models.IntegerChoices):
    UNRESOLVED = 0, "unresolved"
    RESOLVED = 1, "resolved"
    IGNORED = 2, "ignored"

    @classmethod
    def from_string(cls, string: str):
        for status in cls:
            if status.label == string:
                return status


class LogLevel(models.IntegerChoices):
    NOTSET = 0, "sample"
    DEBUG = 1, "debug"
    INFO = 2, "info"
    WARNING = 3, "warning"
    ERROR = 4, "error"
    FATAL = 5, "fatal"


class Issue(models.Model):
    """
    Sentry called this a "group". A issue is a collection of events with meta data
    such as resolved status.
    """

    # annotations Not implemented
    # assigned_to Not implemented
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    culprit = models.CharField(max_length=1024, blank=True, null=True)
    has_seen = models.BooleanField(default=False)
    # is_bookmarked Not implement - is per user
    is_public = models.BooleanField(default=False)
    level = models.PositiveSmallIntegerField(
        choices=LogLevel.choices, default=LogLevel.NOTSET
    )
    metadata = models.JSONField()
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

    @property
    def short_id_display(self):
        """
        Short IDs are per project issue counters. They show as PROJECT_SLUG-ID_BASE32
        The intention is to be human readable identifiers that can reference an issue.
        """
        if self.short_id is not None:
            return f"{self.project.slug.upper()}-{base32_encode(self.short_id)}"


class EventTagKey(models.Model):
    key = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return self.key


class EventTag(models.Model):
    key = models.ForeignKey(EventTagKey, on_delete=models.CASCADE)
    value = models.CharField(max_length=225)

    class Meta:
        unique_together = ("key", "value")


class Event(models.Model):
    """
    An individual event. An issue is a set of like-events.
    Most is stored in "data" but some fields benefit from being real
    relational data types such as dates and foreign keys
    """

    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue = models.ForeignKey(
        Issue, on_delete=models.CASCADE, help_text="Sentry calls this a group"
    )
    timestamp = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Date created as claimed by client it came from",
    )

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    data = models.JSONField()
    tags = models.ManyToManyField(EventTag, blank=True)
    release = models.ForeignKey(
        "releases.Release", blank=True, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return self.event_id_hex

    @property
    def event_id_hex(self):
        """ The public key without dashes """
        if self.event_id:
            if isinstance(self.event_id, str):
                return self.event_id
            return self.event_id.hex

    def event_json(self):
        """
        OSS Sentry Compatible raw event JSON
        Effectively this combines data and relational data
        """
        event = self.data
        event["event_id"] = self.event_id_hex
        event["project"] = self.issue.project_id
        event["tags"] = self.tags.all().values_list("key__key", "value")
        if self.timestamp:
            event["datetime"] = self.timestamp.isoformat().replace("+00:00", "Z")
        if self.release:
            event["release"] = self.release.version
        return event

    @property
    def context(self):
        return self.data.get("extra")

    @property
    def contexts(self):
        return self.data.get("contexts")

    @property
    def culprit(self):
        return self.data.get("culprit")

    @property
    def message(self):
        return self.data.get("message")

    @property
    def user(self):
        return self.data.get("user")

    @property
    def user_report(self):
        return UserReport.objects.filter(event_id=self.pk).first()

    def _build_context(self, context: list, base_line_no: int, is_pre: bool):
        context_length = len(context)
        result = []
        for index, pre_context_line in enumerate(context):
            if is_pre:
                line_no = base_line_no - context_length + index
            else:
                line_no = base_line_no + 1 + index
            result.append(
                [line_no, pre_context_line,]
            )
        return result

    @property
    def metadata(self):
        return self.data.get("metadata")

    @property
    def packages(self):
        return self.data.get("modules")

    @property
    def platform(self):
        return self.data.get("platform")

    @property
    def sdk(self):
        return self.data.get("sdk")

    @property
    def title(self):
        return self.data.get("title")

    @property
    def type(self):
        return self.data.get("type")

    def next(self, *args, **kwargs):
        try:
            return self.get_next_by_created(**kwargs)
        except Event.DoesNotExist:
            return None

    def previous(self, *args, **kwargs):
        """ Get previous object by created date, pass filter kwargs """
        try:
            return self.get_previous_by_created(**kwargs)
        except Event.DoesNotExist:
            return None
