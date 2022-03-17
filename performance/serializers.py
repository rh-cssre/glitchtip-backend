from rest_framework import serializers
from events.serializers import SentrySDKEventSerializer
from glitchtip.serializers import FlexibleDateTimeField
from .models import TransactionEvent, TransactionGroup, Span


class TransactionGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransactionGroup
        fields = [
            "title",
            "project",
            "op",
            "method",
        ]


class SpanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Span
        fields = [
            "span_id",
            "parent_span_id",
            "op",
            "description",
            "start_timestamp",
            "timestamp",
            "tags",
            "data",
        ]


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
        trace_id = data["contexts"]["trace"]["trace_id"]

        group, _ = TransactionGroup.objects.get_or_create(
            project=self.context.get("project"),
            title=data["transaction"],
            op=data["contexts"]["trace"]["op"],
            method=data["request"].get("method"),
        )
        transaction = TransactionEvent.objects.create(
            group=group,
            data={
                "request": data.get("request"),
                "sdk": data.get("sdk"),
                "platform": data.get("platform"),
            },
            trace_id=trace_id,
            transaction=data["transaction"],
            event_id=data["event_id"],
            timestamp=data["timestamp"],
            start_timestamp=data["start_timestamp"],
        )

        first_span = SpanSerializer(
            data=data["contexts"]["trace"]
            | {
                "start_timestamp": data["start_timestamp"],
                "timestamp": data["timestamp"],
            }
        )
        first_span.is_valid()
        spans = data["spans"] + [first_span.validated_data]
        Span.objects.bulk_create(
            [
                Span(
                    transaction=transaction,
                    **span,
                )
                for span in spans
            ]
        )

        return transaction


class TransactionSerializer(serializers.ModelSerializer):
    eventId = serializers.UUIDField(source="pk")
    startTimestamp = serializers.DateTimeField(source="start_timestamp")

    class Meta:
        model = TransactionEvent
        fields = ("eventId", "transaction", "timestamp", "startTimestamp")
