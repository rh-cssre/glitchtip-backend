import uuid
from django.db import models
from django.db.models import Q
from django.contrib.postgres.fields import HStoreField
from user_reports.models import UserReport


class EventManager(models.Manager):
    def for_organization(self, organization):
        return (
            super()
            .get_queryset()
            .filter(
                Q(issue__project__organization=organization)
                | Q(transactionevent__project__organization=organization)
            )
        )


class Event(models.Model):
    """
    An individual event. An issue is a set of like-events.
    Most is stored in "data" but some fields benefit from being real
    relational data types such as dates and foreign keys
    """

    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue = models.ForeignKey(
        "issues.Issue",
        on_delete=models.CASCADE,
        null=True,
        help_text="Sentry calls this a group",
    )
    timestamp = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Date created as claimed by client it came from",
    )

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    data = models.JSONField()
    tags = HStoreField(default=dict)
    release = models.ForeignKey(
        "releases.Release", blank=True, null=True, on_delete=models.SET_NULL
    )

    objects = EventManager()

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
        event["tags"] = self.tags.items()
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
