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
    event = EventSerializer()

    class Meta:
        model = Issue
        fields = (
            "id",
            "event",
        )


class StoreSerializer(serializers.Serializer):
    exception = serializers.JSONField()
    level = serializers.CharField()
    event_id = serializers.UUIDField()
    platform = serializers.CharField()
    sdk = serializers.JSONField()
    release = serializers.CharField()
    breadcrumbs = serializers.JSONField()
    request = serializers.JSONField()
