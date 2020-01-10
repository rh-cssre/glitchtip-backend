from rest_framework import serializers
from issues.event_store.error import ErrorEvent
from issues.models import EventType, EventTag, Event, Issue


class StoreDefaultSerializer(serializers.Serializer):
    type = EventType.DEFAULT
    breadcrumbs = serializers.JSONField()
    contexts = serializers.JSONField(required=False)
    event_id = serializers.UUIDField()
    extra = serializers.JSONField(required=False)
    level = serializers.CharField()
    message = serializers.CharField(required=False)
    platform = serializers.CharField()
    release = serializers.CharField(required=False)
    sdk = serializers.JSONField()
    timestamp = serializers.DateTimeField(required=False)
    modules = serializers.JSONField(required=False)

    def create(self, project, data):
        error = ErrorEvent()


class StoreErrorSerializer(StoreDefaultSerializer):
    type = EventType.ERROR
    exception = serializers.JSONField(required=False)
    request = serializers.JSONField(required=False)

    def create(self, project, data):
        error = ErrorEvent()
        metadata = error.get_metadata(data)
        issue, _ = Issue.objects.get_or_create(
            title=error.get_title(metadata),
            culprit=error.get_location(metadata),
            project=project,
        )

        level_tag, _ = EventTag.objects.get_or_create(key="level", value=data["level"])
        # release tag
        breadcrumbs = data.get("breadcrumbs")
        entries = [{"type": "breadcrumbs"}, {"data": {"values": breadcrumbs}}]
        params = {
            "event_id": data["event_id"],
            "platform": data["platform"],
            "sdk": data["sdk"],
            "entries": entries,
            "issue": issue,
        }
        if data.get("contexts"):
            params["contexts"] = data["contexts"]
        if data.get("context"):
            params["context"] = data["extra"]
        if data.get("modules"):
            params["packages"] = data["modules"]

        event = Event.objects.create(**params)
        event.tags.add(level_tag)


class StoreCSPReportSerializer(serializers.Serializer):
    """ Very different format from others """

    type = EventType.CSP
