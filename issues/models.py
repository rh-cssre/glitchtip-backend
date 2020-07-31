import uuid
from django.contrib.postgres.fields import JSONField
from django.contrib.postgres.search import SearchVectorField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from sentry.interfaces.stacktrace import get_context
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
    metadata = JSONField()
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
    last_seen = models.DateTimeField(auto_now_add=True)

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
    data = JSONField()
    tags = models.ManyToManyField(EventTag, blank=True)

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
        return event

    @property
    def contexts(self):
        return self.data.get("contexts")

    @property
    def culprit(self):
        return self.data.get("culprit")

    @property
    def user_report(self):
        return UserReport.objects.filter(event_id=self.pk).first()

    @property
    def entries(self):
        def get_has_system_frames(frames):
            return any(frame.in_app for frame in frames)

        entries = []

        exception = self.data.get("exception")
        # Some, but not all, keys are made more JS camel case like
        if exception and exception.get("values"):
            # https://gitlab.com/glitchtip/sentry-open-source/sentry/-/blob/master/src/sentry/interfaces/stacktrace.py#L487
            # if any frame is "in_app" set this to True
            exception["hasSystemFrames"] = False
            for value in exception["values"]:
                if "stacktrace" in value and "frames" in value["stacktrace"]:
                    for frame in value["stacktrace"]["frames"]:
                        if frame.get("in_app") == True:
                            exception["hasSystemFrames"] = True
                        if "in_app" in frame:
                            frame["inApp"] = frame.pop("in_app")
                        if "abs_path" in frame:
                            frame["absPath"] = frame.pop("abs_path")
                        if "colno" in frame:
                            frame["colNo"] = frame.pop("colno")
                        if "lineno" in frame:
                            frame["lineNo"] = frame.pop("lineno")
                            pre_context = frame.pop("pre_context", None)
                            post_context = frame.pop("post_context", None)
                            frame["context"] = get_context(
                                frame["lineNo"],
                                frame.get("context_line"),
                                pre_context,
                                post_context,
                            )

            entries.append({"type": "exception", "data": exception})

        request = self.data.get("request")
        if request:
            request["inferredContentType"] = request.pop("inferred_content_type")
            entries.append({"type": "request", "data": request})

        breadcrumbs = self.data.get("breadcrumbs")
        if breadcrumbs:
            entries.append({"type": "breadcrumbs", "data": {"values": breadcrumbs}})

        message = self.data.get("message")
        if message:
            entries.append({"type": "message", "data": {"formatted": message}})

        csp = self.data.get("csp")
        if csp:
            entries.append({"type": EventType.CSP.label, "data": csp})

        return entries

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
