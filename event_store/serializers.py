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
            # https://gitlab.com/glitchtip/sentry-open-source/sentry/blob/master/src/sentry/event_manager.py#L412
            # Sentry SDK primarily uses transaction. It has a fallback of get_culprit but isn't preferred. We don't implement this fallback
            "culprit": self.get_culprit(data),
            "title": title,
            "metadata": metadata,
        }
        if data.get("contexts"):
            params["contexts"] = data["contexts"]
        if data.get("context"):
            params["context"] = data["extra"]
        if data.get("modules"):
            params["packages"] = data["modules"]

        event = Event.objects.create(**params)
        event.tags.add(level_tag)

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
