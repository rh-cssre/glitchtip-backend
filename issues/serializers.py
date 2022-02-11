from rest_framework import serializers
from projects.serializers.base_serializers import ProjectReferenceSerializer
from user_reports.serializers import UserReportSerializer
from sentry.interfaces.stacktrace import get_context
from glitchtip.serializers import FlexibleDateTimeField
from releases.serializers import ReleaseSerializer
from events.models import Event
from .models import Issue, EventType, EventStatus


class EventUserSerializer(serializers.Serializer):
    username = serializers.CharField(allow_null=True)
    name = serializers.CharField(allow_null=True)
    ip_address = serializers.IPAddressField(allow_null=True)
    email = serializers.EmailField(allow_null=True)
    data = serializers.JSONField(default={})
    id = serializers.CharField(allow_null=True)


class BaseBreadcrumbsSerializer(serializers.Serializer):
    category = serializers.CharField()
    level = serializers.CharField(default="info")
    event_id = serializers.CharField(required=False)
    data = serializers.JSONField(required=False)
    message = serializers.CharField(required=False)
    type = serializers.CharField(default="default")


class BreadcrumbsSerializer(BaseBreadcrumbsSerializer):
    timestamp = FlexibleDateTimeField()
    message = serializers.CharField(default=None)
    event_id = serializers.CharField(default=None)
    data = serializers.JSONField(default=None)


class EventEntriesSerializer(serializers.Serializer):
    def to_representation(self, instance):
        def get_has_system_frames(frames):
            return any(frame.in_app for frame in frames)

        entries = []

        exception = instance.get("exception")
        # Some, but not all, keys are made more JS camel case like
        if exception and exception.get("values"):
            # https://gitlab.com/glitchtip/sentry-open-source/sentry/-/blob/master/src/sentry/interfaces/stacktrace.py#L487
            # if any frame is "in_app" set this to True
            exception["hasSystemFrames"] = False
            for value in exception["values"]:
                if (
                    value.get("stacktrace", None) is not None
                    and "frames" in value["stacktrace"]
                ):
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

        breadcrumbs = instance.get("breadcrumbs")
        if breadcrumbs:
            breadcrumbs_serializer = BreadcrumbsSerializer(
                data=breadcrumbs.get("values"), many=True
            )
            if breadcrumbs_serializer.is_valid():
                entries.append(
                    {
                        "type": "breadcrumbs",
                        "data": {"values": breadcrumbs_serializer.validated_data},
                    }
                )

        request = instance.get("request")
        if request:
            request["inferredContentType"] = request.pop("inferred_content_type", None)
            entries.append({"type": "request", "data": request})

        message = instance.get("message")
        if message:
            entries.append({"type": "message", "data": {"formatted": message}})

        csp = instance.get("csp")
        if csp:
            entries.append({"type": EventType.CSP.label, "data": csp})

        return entries


class EventTagField(serializers.HStoreField):
    def to_representation(self, obj):
        return [{"key": tag[0], "value": tag[1]} for tag in obj.items()]


class EventSerializer(serializers.ModelSerializer):
    eventID = serializers.CharField(source="event_id_hex")
    id = serializers.CharField(source="event_id_hex")
    dateCreated = serializers.DateTimeField(source="timestamp")
    dateReceived = serializers.DateTimeField(source="created")
    entries = EventEntriesSerializer(source="data")
    tags = EventTagField()
    user = EventUserSerializer()

    class Meta:
        model = Event
        fields = (
            "eventID",
            "id",
            "issue",
            "context",
            "contexts",
            "culprit",
            "dateCreated",
            "dateReceived",
            "entries",
            # "errors",
            # "location",
            "message",
            "metadata",
            "packages",
            "platform",
            "sdk",
            "tags",
            "title",
            "type",
            "user",
        )


class EventDetailSerializer(EventSerializer):
    projectID = serializers.IntegerField(source="issue.project_id")
    userReport = UserReportSerializer(source="user_report")
    nextEventID = serializers.SerializerMethodField()
    previousEventID = serializers.SerializerMethodField()
    release = ReleaseSerializer()

    class Meta(EventSerializer.Meta):
        fields = EventSerializer.Meta.fields + (
            "projectID",
            "userReport",
            "nextEventID",
            "previousEventID",
            "release",
        )

    def get_next_or_previous(self, obj, is_next):
        kwargs = self.context["view"].kwargs
        filter_kwargs = {}
        if kwargs.get("issue_pk"):
            filter_kwargs["issue"] = kwargs["issue_pk"]
        if is_next:
            result = obj.next(**filter_kwargs)
        else:
            result = obj.previous(**filter_kwargs)
        if result:
            return str(result)

    def get_nextEventID(self, obj):
        return self.get_next_or_previous(obj, True)

    def get_previousEventID(self, obj):
        return self.get_next_or_previous(obj, False)


class DisplayChoiceField(serializers.ChoiceField):
    """
    ChoiceField that represents choice only as display value
    Useful if the API should only deal with display values
    """

    def to_representation(self, value):
        return self.choices[value]

    def to_internal_value(self, data):
        if data == "" and self.allow_blank:
            return ""

        choice_strings_to_values = {value: key for key, value in self.choices.items()}
        try:
            return choice_strings_to_values[str(data)]
        except KeyError:
            self.fail("invalid_choice", input=data)


class IssueSerializer(serializers.ModelSerializer):
    annotations = serializers.JSONField(default=list, read_only=True)
    assignedTo = serializers.CharField(default=None, read_only=True)
    count = serializers.CharField(read_only=True)
    firstSeen = serializers.DateTimeField(source="created", read_only=True)
    hasSeen = serializers.BooleanField(source="has_seen", read_only=True)
    id = serializers.CharField(read_only=True)
    isBookmarked = serializers.BooleanField(default=False, read_only=True)
    isPublic = serializers.BooleanField(source="is_public", read_only=True)
    isSubscribed = serializers.BooleanField(default=False, read_only=True)
    lastSeen = serializers.DateTimeField(source="last_seen", read_only=True)
    level = serializers.CharField(source="get_level_display", read_only=True)
    logger = serializers.CharField(default=None, read_only=True)
    metadata = serializers.JSONField(default=dict, read_only=True)
    numComments = serializers.IntegerField(default=0, read_only=True)
    permalink = serializers.CharField(default="Not implemented", read_only=True)
    project = ProjectReferenceSerializer(read_only=True)
    shareId = serializers.IntegerField(default=None, read_only=True)
    shortId = serializers.CharField(source="short_id_display", read_only=True)
    stats = serializers.JSONField(default=dict, read_only=True)
    status = DisplayChoiceField(choices=EventStatus.choices)
    statusDetails = serializers.JSONField(default=dict, read_only=True)
    subscriptionDetails = serializers.CharField(default=None, read_only=True)
    type = serializers.CharField(source="get_type_display", read_only=True)
    userReportCount = serializers.IntegerField(
        source="userreport_set.count", read_only=True
    )
    userCount = serializers.IntegerField(default=0, read_only=True)
    matchingEventId = serializers.SerializerMethodField()

    class Meta:
        model = Issue
        fields = (
            "annotations",
            "assignedTo",
            "count",
            "culprit",
            "firstSeen",
            "hasSeen",
            "id",
            "isBookmarked",
            "isPublic",
            "isSubscribed",
            "lastSeen",
            "level",
            "logger",
            "metadata",
            "numComments",
            "permalink",
            "project",
            "shareId",
            "shortId",
            "stats",
            "status",
            "statusDetails",
            "subscriptionDetails",
            "title",
            "type",
            "userReportCount",
            "userCount",
            "matchingEventId",
        )
        read_only_fields = (
            "annotations",
            "assignedTo",
            "count",
            "culprit",
            "firstSeen",
            "hasSeen",
            "id",
            "isBookmarked",
            "isPublic",
            "isSubscribed",
            "lastSeen",
            "level",
            "logger",
            "metadata",
            "numComments",
            "permalink",
            "project",
            "shareId",
            "shortId",
            "stats",
            "subscriptionDetails",
            "title",
            "type",
            "userCount",
        )

    def to_representation(self, obj):
        """ Workaround for "type" and "matchingEventId" fields """
        primitive_repr = super().to_representation(obj)
        primitive_repr["type"] = obj.get_type_display()

        if primitive_repr["matchingEventId"] is None:
            del primitive_repr["matchingEventId"]

        return primitive_repr

    def get_matchingEventId(self, obj):
        matching_event_id = self.context.get("matching_event_id")
        if matching_event_id:
            return matching_event_id

        return None
