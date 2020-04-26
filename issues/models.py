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

    class Meta:
        unique_together = ("title", "culprit", "project", "type")

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


class EventTag(models.Model):
    key = models.CharField(max_length=255)
    value = models.CharField(max_length=225)


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

    @property
    def contexts(self):
        return self.data.get("contexts")

    @property
    def culprit(self):
        return self.data.get("culprit")

    @property
    def entries(self):
        entries = []

        exception = self.data.get("exception")
        if exception:
            # Some, but not all, keys are made more JS camel case like
            for value in exception["values"]:
                for frame in value["stacktrace"]["frames"]:
                    if "abs_path" in frame:
                        frame["absPath"] = frame.pop("abs_path")
                    if "lineno" in frame:
                        frame["lineNo"] = frame.pop("lineno")
                        base_line_no = frame["lineNo"]
                        context = []
                        pre_context = frame.pop("pre_context", None)
                        if pre_context:
                            context += self._build_context(
                                pre_context, base_line_no, True
                            )
                        context.append([base_line_no, frame.get("context_line")])
                        post_context = frame.pop("post_context", None)
                        if post_context:
                            context += self._build_context(
                                post_context, base_line_no, False
                            )
                        frame["context"] = context

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
        return self.data.get("packages")

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
