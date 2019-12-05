from rest_framework import serializers
from .models import Issue, Event


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
    type = serializers.CharField(source="type_name")
    event = EventSerializer()

    class Meta:
        model = Issue
        fields = (
            "id",
            "title",
            "type",
            "location",
            "event",
        )


class StoreSerializer(serializers.Serializer):
    exception = serializers.JSONField(required=False)
    level = serializers.CharField()
    event_id = serializers.UUIDField()
    platform = serializers.CharField()
    sdk = serializers.JSONField()
    release = serializers.CharField()
    breadcrumbs = serializers.JSONField()
    request = serializers.JSONField(required=False)
