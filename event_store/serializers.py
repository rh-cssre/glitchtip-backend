from django.utils.encoding import force_text
from rest_framework import serializers
from issues.event_store.error import ErrorEvent
from issues.models import EventType, EventTag, Event, Issue
from sentry.culprit import generate_culprit


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
    transaction = serializers.CharField(required=False)
    timestamp = serializers.DateTimeField(required=False)

    def create(self, project, data):
        error = ErrorEvent()
        metadata = error.get_metadata(data)
        title = error.get_title(metadata)
        issue, _ = Issue.objects.get_or_create(
            title=title,
            culprit=error.get_location(metadata),
            project=project,
            defaults={"metadata": metadata},
        )

        # level_tag, _ = EventTag.objects.get_or_create(key="level", value=data["level"])
        # release tag
        entries = []
        exception = data.get("exception")
        if exception:
            entries.append({"type": "exception", "data": exception})
        breadcrumbs = data.get("breadcrumbs")
        if breadcrumbs:
            entries.append({"type": "breadcrumbs", "data": {"values": breadcrumbs}})

        
        params = {
            "event_id": data["event_id"],
            "issue": issue,
            "timestamp": data.get("timestamp"),
            "data": {
                "contexts": data.get("contexts"),
                "culprit": self.get_culprit(data),
                # "entries": entries,
                "metadata": metadata,
                "packages": data.get("modules"),
                "platform": data["platform"],
                "sdk": data["sdk"],
                "title": title,
            },
        }
        # if data.get("context"):
        #     params["context"] = data["extra"]

        event = Event.objects.create(**params)
        # event.tags.add(level_tag)

    def get_culprit(self, data):
        """Helper to calculate the default culprit"""
        return force_text(
            data.get("culprit")
            or data.get("transaction")
            or generate_culprit(data)
            or ""
        )


class StoreCSPReportSerializer(serializers.Serializer):
    """ Very different format from others """

    type = EventType.CSP
