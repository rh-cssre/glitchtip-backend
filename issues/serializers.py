from rest_framework import serializers
from projects.serializers.base_serializers import ProjectReferenceSerializer
from .models import Issue, Event


class EventSerializer(serializers.ModelSerializer):
    eventID = serializers.CharField(source="event_id_hex")
    id = serializers.CharField(source="event_id_hex")
    dateCreated = serializers.DateTimeField(source="timestamp")
    dateReceived = serializers.DateTimeField(source="created")
    tags = serializers.ListField(default=[])

    class Meta:
        model = Event
        fields = (
            "eventID",
            "id",
            "issue",
            # "context",
            "contexts",
            "culprit",
            "dateCreated",
            "dateReceived",
            "entries",
            # "errors",
            # "location",
            # "message",
            "metadata",
            "packages",
            "platform",
            "sdk",
            "tags",
            "title",
            # "user",
        )


class EventDetailSerializer(EventSerializer):
    nextEventID = serializers.SerializerMethodField()
    previousEventID = serializers.SerializerMethodField()

    class Meta(EventSerializer.Meta):
        fields = EventSerializer.Meta.fields + ("nextEventID", "previousEventID",)

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


class IssueSerializer(serializers.ModelSerializer):
    annotations = serializers.JSONField(default=list, read_only=True)
    assignedTo = serializers.CharField(default=None, read_only=True)
    count = serializers.IntegerField(read_only=True)
    firstSeen = serializers.DateTimeField(read_only=True)
    hasSeen = serializers.BooleanField(source="has_seen", read_only=True)
    isBookmarked = serializers.BooleanField(default=False, read_only=True)
    isPublic = serializers.BooleanField(source="is_public", read_only=True)
    isSubscribed = serializers.BooleanField(default=False, read_only=True)
    lastSeen = serializers.DateTimeField(read_only=True)
    level = serializers.CharField(source="get_level_display", read_only=True)
    logger = serializers.CharField(default=None, read_only=True)
    metadata = serializers.JSONField(default=dict, read_only=True)
    numComments = serializers.IntegerField(default=0, read_only=True)
    permalink = serializers.CharField(default="Not implemented", read_only=True)
    project = ProjectReferenceSerializer(read_only=True)
    shareId = serializers.IntegerField(default=None, read_only=True)
    shortId = serializers.CharField(default="Not implemented", read_only=True)
    stats = serializers.JSONField(default=dict, read_only=True)
    status = serializers.CharField(source="get_status_display")
    statusDetails = serializers.JSONField(default=dict, read_only=True)
    subscriptionDetails = serializers.CharField(default=None, read_only=True)
    type = serializers.CharField(source="get_type_display", read_only=True)
    userCount = serializers.IntegerField(default=0, read_only=True)

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
            "userCount",
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
