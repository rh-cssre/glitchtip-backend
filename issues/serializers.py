from rest_framework import serializers
from projects.serializers import ProjectReferenceSerializer
from .models import Issue, Event, EventType


class EventSerializer(serializers.ModelSerializer):
    class Meta:
        model = Event
        fields = (
            "event_id",
            "exception",
            "level",
            "platform",
            "sdk",
            "release",
            "breadcrumbs",
            "request",
            "received_at",
        )


class IssueSerializer(serializers.ModelSerializer):
    type = serializers.CharField(source="get_type_display")
    status = serializers.CharField(source="get_status_display")
    project = ProjectReferenceSerializer()
    event = EventSerializer()

    class Meta:
        model = Issue
        fields = (
            "id",
            "title",
            "type",
            "status",
            "project",
            "location",
            "event",
        )


class StoreDefaultSerializer(serializers.Serializer):
    type = EventType.DEFAULT
    breadcrumbs = serializers.JSONField()
    event_id = serializers.UUIDField()
    level = serializers.CharField()
    message = serializers.CharField(required=False)
    platform = serializers.CharField()
    release = serializers.CharField(required=False)
    sdk = serializers.JSONField()
    timestamp = serializers.DateTimeField()

    def create(self, validated_data):
        error = ErrorEvent()


class StoreErrorSerializer(StoreDefaultSerializer):
    type = EventType.ERROR
    exception = serializers.JSONField(required=False)
    request = serializers.JSONField(required=False)

    def create(self, validated_data):
        error = ErrorEvent()
        metadata = error.get_metadata(data)
        issue, _ = Issue.objects.get_or_create(
            title=error.get_title(metadata),
            location=error.get_location(metadata),
            project=project,
        )
        # TODO update mapping
        event = Event.objects.create(
            event_id=data["event_id"],
            exception=data.get("exception"),
            level=data["level"],
            platform=data["platform"],
            sdk=data["sdk"],
            release=data["release"],
            breadcrumbs=data["breadcrumbs"],
            request=data.get("request"),
            issue=issue,
        )


class StoreCSPReportSerializer(serializers.Serializer):
    """ Very different format from others """

    type = EventType.CSP
