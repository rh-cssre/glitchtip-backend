from rest_framework import serializers
from events.serializers import SentrySDKEventSerializer
from glitchtip.serializers import FlexibleDateTimeField
from .models import TransactionEvent


class SpanSerializer(serializers.Serializer):
    data = serializers.JSONField(required=False)
    description = serializers.CharField(required=False)
    op = serializers.CharField(required=False)
    parent_span_id = serializers.CharField(required=False)
    span_id = serializers.CharField(required=False)
    start_timestamp = FlexibleDateTimeField()
    status = serializers.CharField(required=False)
    tags = serializers.JSONField(required=False)
    timestamp = FlexibleDateTimeField()
    trace_id = serializers.UUIDField()
    same_process_as_parent = serializers.BooleanField(required=False)


class TransactionEventSerializer(SentrySDKEventSerializer):
    type = serializers.CharField()
    contexts = serializers.JSONField()
    measurements = serializers.JSONField(required=False)
    spans = serializers.ListField(
        child=SpanSerializer(), required=False, allow_empty=True
    )
    start_timestamp = FlexibleDateTimeField()
    timestamp = FlexibleDateTimeField()
    transaction = serializers.CharField()

    def create(self, data):
        project = self.context.get("project")
        return TransactionEvent.objects.create(
            data={},
            transaction=data["transaction"],
            event_id=data["event_id"],
            timestamp=data["timestamp"],
            start_timestamp=data["start_timestamp"],
            project=project,
        )


class TransactionSerializer(serializers.ModelSerializer):
    eventId = serializers.UUIDField(source="pk")
    startTimestamp = serializers.DateTimeField(source="start_timestamp")

    class Meta:
        model = TransactionEvent
        fields = ("eventId", "transaction", "timestamp", "startTimestamp")
