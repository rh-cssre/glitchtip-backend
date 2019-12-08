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
    """
    Sentry called this a "group". A issue is a collection of events with meta data
    such as resolved status.
    """

    title = models.CharField(max_length=255)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    type = models.PositiveSmallIntegerField(
        choices=EventType.choices, default=EventType.DEFAULT
    )
    culprit = models.CharField(max_length=1024, blank=True, null=True)
    status = models.PositiveSmallIntegerField(
        choices=EventStatus.choices, default=EventStatus.UNRESOLVED
    )

    def event(self):
        return self.event_set.first()

    def __str__(self):
        return self.title


class Event(models.Model):
    """
    An individual event. An issue is a set of like-events.
    We have options on where to store data. We could store duplicate data on the Issue.
    Data that varies can be stored here.
    """

    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue = models.ForeignKey(
        Issue, on_delete=models.CASCADE, help_text="Sentry calls this a group"
    )
    # exists in default and error
    # maps to extra in python
    #   sys.argv in python.
    # maps to request in JS (but it's normalized to OS, not just user agent)
    context = JSONField()
    # contexts literal. Empty in JS
    contexts = JSONField(blank=True)
    # crashFile = ??? - always null for now
    # location of crash, sometimes a filename
    # .get_location() gives us this
    # Shown in UI as subtitle
    culprit = models.CharField(max_length=1024, blank=True)
    # Maps to timestamp
    # In js, maps to breadcrumbs.timestamp???
    created_at = models.DateTimeField(blank=True, null=True)
    received_at = models.DateTimeField(auto_now_add=True)
    # dist = ??? - always null for now
    # maps to exception
    entries = JSONField()
    # Only shows up in JS event - not sure the difference from entries
    errors = JSONField(blank=True)
    # fingerprints = ??? Presumably a unique way to identify similair events
    # groupingConfig = ??? Probably don't need this yet
    # Top file location in stacktrace maybe?
    # not really shown in the UI - why does it get it's own field? For search maybe?
    location = models.CharField(max_length=1024, blank=True, null=True)
    # Set manually using sentry client sdk - for example a test message sets this
    message = models.CharField(max_length=1024, blank=True, null=True)
    # No idea how this is generated, doesn't match inbound event.
    # metadata = JSONField()
    # Maps to modules
    packages = JSONField(blank=True)
    # Maps to platform
    platform = models.CharField(max_length=255)
    # Will implement release later, client just sends a string
    # release = models.ForeignKey()
    # Maps to sdk
    sdk = JSONField()
    # size = ??? Shown in UI - WHY does it link to something almost but not quite the api!?
    # tags = ??? Probably used to help search
    # title likely comes from event get_title function
    title = models.CharField(max_length=255)
    # type is only stored on the linked Issue
    # User is derived from any known data on the system user who had this event
    user = JSONField(blank=True, null=True)
    # userReport = ?? We don't support user reports at this time
    # meta = ?? Not supported, doesn't seem used much

    def __str__(self):
        return self.event_id
